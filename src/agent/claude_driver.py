# -*- coding: utf-8 -*-
"""
claude_driver.py —— headless 驱动 claude（复用 night_watch 的 subprocess + stream-json 思路，精简版）。

对应 CONTRACT §2.2 的 claude.start / claude.nudge / claude.stop：
  - start(cs, project_path, prompt, opts, emit, on_exit)：无头启动 claude，round 0 跑初始 prompt，
    stream-json 逐行解析后经 emit(text) 上行（cap=claude.output）；之后 worker 阻塞等待 nudge。
  - nudge(cs, text)：信箱式喂话——把文本塞进本会话邮箱队列，worker 取到即以 --resume 续跑一轮。
  - stop(cs)：整树 taskkill 当前子进程并结束 worker，触发 on_exit(cs)。
持久多轮：一次 start 建一个常驻会话，靠 nudge 驱动续跑（不像 night_watch 用定时器）。
仅解析 assistant 文字/工具调用与最终 result 上行，够前端展示即可。
"""
import json
import queue
import shlex
import subprocess
import threading
import time

from . import config_loader

_SESSIONS = {}       # claude_session -> _Session
_LOCK = threading.Lock()

# 空闲回收（对齐 terminal_session）：会话超 IDLE_TIMEOUT 无输出且无喂话即视为废弃，
# 回收线程 stop 之。防止 peer start 后不 stop 就断连导致 worker 线程与会话条目永久滞留。
IDLE_TIMEOUT = 1800.0
_GC_INTERVAL = 60.0
_gc_started = False


def _cmd_prefix(opts):
    """命令前缀：opts.claude_cmd > 配置 [claude].cmd > 默认 'claude'。posix=False 保住反斜杠。"""
    raw = (opts or {}).get("claude_cmd") or config_loader.get("claude", "cmd", "claude")
    try:
        return [t.strip('"') for t in shlex.split(raw, posix=False)]
    except Exception:
        return ["claude"]


def _kill_tree(pid):
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=0x08000000)  # CREATE_NO_WINDOW：避免闪黑框
    except Exception:
        pass


class _Session:
    """一个 claude 会话：worker 线程跑 round 0，然后循环消费邮箱队列续跑。"""

    def __init__(self, cs, project, prompt, prefix, emit, on_exit):
        self.cs = cs
        self.project = project or None
        self.first_prompt = prompt or ""
        self.prefix = prefix
        self.emit = emit or (lambda text: None)
        self.on_exit = on_exit
        self.mailbox = queue.Queue()     # 信箱：nudge 的文本
        self.proc = None
        self.stopped = False
        self.last = time.time()          # 最后活动时间（有输出/被 nudge 时刷新），供空闲 GC

    def _round(self, prompt, resume):
        """跑一轮 claude 子进程，stream-json 逐行 emit。resume=True 用 --resume 续会话。"""
        # 不生成/传 --session-id（claude 要求标准 UUID，随手的 hex 会报 Invalid session ID）；
        # 首轮直接跑，续轮用 --continue 接最近一次会话。
        if resume:
            args = ["-p", "--continue",
                    "--dangerously-skip-permissions",
                    "--output-format", "stream-json", "--verbose"]
        else:
            args = ["-p",
                    "--dangerously-skip-permissions",
                    "--output-format", "stream-json", "--verbose"]
        # Windows 上 claude 通常是 claude.cmd(npm 垫片)，shell=False 找不到 → 必须 shell=True。
        # prompt 走 stdin，不进命令行，故拼接命令行安全。
        cmdline = " ".join(self.prefix + args)
        try:
            proc = subprocess.Popen(
                cmdline, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=self.project, shell=True,
                encoding="utf-8", errors="replace",
                creationflags=0x08000000,   # CREATE_NO_WINDOW：不闪黑框
            )
        except Exception as e:
            self.emit("[claude 启动失败] %s" % e)
            return
        self.proc = proc
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass  # 子进程秒退时管道已断
        # 并发抽干 stderr：否则 stderr 管道缓冲(约64KB)写满后 claude 会阻塞在 stderr 写入、
        # 停止产出 stdout，而本线程正卡在读 stdout → 双管道死锁，该轮永久挂死。
        err_chunks = []

        def _drain_err():
            try:
                for eline in proc.stderr:
                    err_chunks.append(eline)
            except Exception:
                pass

        et = threading.Thread(target=_drain_err, name="claude-stderr", daemon=True)
        et.start()
        got = False
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            got = True
            self._emit_event(ev)
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        et.join(timeout=5)
        err = "".join(err_chunks)
        if not got:
            # 没有任何 stream-json 输出：多半是 claude 未安装/不在 PATH，或参数不被支持
            tail = (err or "").strip()
            tail = tail[-600:] if tail else "(无 stderr)"
            self.emit("[claude 未产生输出，退出码=%s] %s\n请确认对端已安装 claude CLI 且在 PATH"
                      "（Windows 为 claude.cmd），可先在‘cmd’里执行 `claude --version` 验证。"
                      % (proc.returncode, tail))
        self.proc = None

    def _emit_event(self, ev):
        """把 stream-json 事件精简成文本上行：assistant 文字/工具调用 + 最终 result。"""
        self.last = time.time()   # 有输出=活跃，刷新活动时间，避免长轮次会话被空闲 GC 误杀
        t = ev.get("type")
        if t == "assistant":
            for b in ((ev.get("message") or {}).get("content") or []):
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    self.emit(b["text"])
                elif b.get("type") == "tool_use":
                    self.emit("▶ 工具 %s" % b.get("name", "?"))
        elif t == "result":
            r = ev.get("result")
            if r:
                self.emit(r)

    def worker(self):
        self._round(self.first_prompt, resume=False)
        while not self.stopped:
            try:
                text = self.mailbox.get(timeout=1.0)
            except queue.Empty:
                continue
            if text is None or self.stopped:
                break
            self._round(text, resume=True)
        self._finish()

    def _finish(self):
        cb = self.on_exit
        if cb:
            try:
                cb(self.cs)
            except Exception:
                pass


def _gc_loop():
    """周期回收空闲会话：超 IDLE_TIMEOUT 无活动即 stop（触发 on_exit 通知前端）。"""
    while True:
        time.sleep(_GC_INTERVAL)
        now = time.time()
        with _LOCK:
            stale = [cs for cs, s in _SESSIONS.items()
                     if now - s.last > IDLE_TIMEOUT]
        for cs in stale:
            stop(cs)


def _ensure_gc():
    global _gc_started
    with _LOCK:
        if _gc_started:
            return
        _gc_started = True
    threading.Thread(target=_gc_loop, name="claude-gc", daemon=True).start()


def start(cs, project_path, prompt, opts, emit, on_exit):
    """启动会话，返回 claude_session（即传入的 cs）。"""
    s = _Session(cs, project_path, prompt, _cmd_prefix(opts), emit, on_exit)
    with _LOCK:
        _SESSIONS[cs] = s
    _ensure_gc()
    threading.Thread(target=s.worker, daemon=True).start()
    return cs


def nudge(cs, text):
    """信箱式喂话：文本入队，worker 取到后 --resume 续跑一轮。"""
    with _LOCK:
        s = _SESSIONS.get(cs)
    if not s or s.stopped:
        return False
    s.last = time.time()          # 喂话=活跃，刷新活动时间
    s.mailbox.put(text or "")
    return True


def stop(cs):
    """整树查杀当前子进程并结束 worker（触发 on_exit）。"""
    with _LOCK:
        s = _SESSIONS.pop(cs, None)
    if not s:
        return False
    s.stopped = True
    if s.proc:
        _kill_tree(s.proc.pid)
    s.mailbox.put(None)   # 唤醒 worker 尽快收尾
    return True
