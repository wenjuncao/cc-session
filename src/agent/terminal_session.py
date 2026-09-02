# -*- coding: utf-8 -*-
"""
terminal_session.py —— 基于 pywinpty(ConPTY) 的有状态交互式终端。

对应 CONTRACT §2.2 的 term.open/input/resize/close：
  - open_session：spawn powershell/cmd，返回 session_id，起读线程把输出经 emit(base64) 上行；
  - write_input：把字节串写入 pty（回车用 \r，由调用方保证）；
  - resize：调整行列；
  - close_session：整树 taskkill /T /F 收掉进程。
每会话记入 registry，超过 capability.max_pty_sessions 拒绝；空闲超时由 GC 线程回收。
pywinpty 不可用时抛 PtyUnavailable，由 cap_router 优雅回错。
"""
import base64
import subprocess
import threading
import time

from .config_loader import getint


class PtyUnavailable(Exception):
    """pywinpty 未安装或导入失败。"""


class SessionLimit(Exception):
    """并发会话数超过上限。"""


# session_id -> {"pty":..., "emit":..., "on_exit":..., "last":ts, "closed":bool, "pid":int}
_sessions = {}
_lock = threading.Lock()
_gc_started = False

IDLE_TIMEOUT = 1800.0   # 空闲 30 分钟无输入即回收
_GC_INTERVAL = 60.0


def _spawn_pty(shell, cwd, cols, rows):
    """惰性导入 winpty 并 spawn；导入失败抛 PtyUnavailable。"""
    try:
        from winpty import PtyProcess
    except Exception as e:
        raise PtyUnavailable("pywinpty 不可用：%s" % e)
    argv = "cmd.exe" if str(shell).lower() == "cmd" else "powershell.exe"
    # winpty dimensions=(rows, cols)
    return PtyProcess.spawn(argv, cwd=cwd or None, dimensions=(int(rows), int(cols)))


def _kill_tree(pid):
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=0x08000000)  # CREATE_NO_WINDOW：避免闪黑框
    except Exception:
        pass


def _reader(session_id):
    """读线程：持续 pty.read()，base64 后经 emit 上行；进程结束触发 on_exit。"""
    sess = _sessions.get(session_id)
    if not sess:
        return
    pty = sess["pty"]
    emit = sess["emit"]
    while True:
        try:
            data = pty.read(4096)
        except EOFError:
            break
        except Exception:
            break
        if data == "":
            # 部分版本 EOF 返回空串；进程已死则退出，否则短歇再读
            try:
                if not pty.isalive():
                    break
            except Exception:
                break
            time.sleep(0.02)
            continue
        b64 = base64.b64encode(data.encode("utf-8", "replace")).decode("ascii")
        emit(b64)
    _fire_exit(session_id)


def _fire_exit(session_id):
    """幂等触发退出：整树查杀 + 摘除 registry + 回调 on_exit（只执行一次）。"""
    with _lock:
        sess = _sessions.get(session_id)
        if not sess or sess["closed"]:
            return
        sess["closed"] = True
        _sessions.pop(session_id, None)
    _kill_tree(sess.get("pid"))
    cb = sess.get("on_exit")
    if cb:
        try:
            cb(session_id)
        except Exception:
            pass


def _gc_loop():
    while True:
        time.sleep(_GC_INTERVAL)
        now = time.time()
        stale = []
        with _lock:
            for sid, s in _sessions.items():
                if now - s["last"] > IDLE_TIMEOUT:
                    stale.append(sid)
        for sid in stale:
            _fire_exit(sid)


def _ensure_gc():
    global _gc_started
    with _lock:
        if _gc_started:
            return
        _gc_started = True
    threading.Thread(target=_gc_loop, daemon=True).start()


def open_session(session_id, shell="powershell", cwd=None, cols=80, rows=24,
                 emit=None, on_exit=None):
    """开一个终端会话。emit(b64str) 上行输出；on_exit(session_id) 进程退出回调。"""
    limit = getint("capability", "max_pty_sessions", 10)
    with _lock:
        if len(_sessions) >= limit:
            raise SessionLimit("PTY 会话已达上限 %d" % limit)
    pty = _spawn_pty(shell, cwd, cols, rows)
    with _lock:
        # spawn 是慢操作、无锁，并发 open 可能都通过了上面的检查；插入前再核一次，
        # 超限则杀掉这个刚 spawn 的进程并拒绝，确保上限不被并发架空
        if len(_sessions) >= limit:
            _kill_tree(getattr(pty, "pid", None))
            raise SessionLimit("PTY 会话已达上限 %d" % limit)
        _sessions[session_id] = {
            "pty": pty, "emit": emit, "on_exit": on_exit,
            "last": time.time(), "closed": False, "pid": getattr(pty, "pid", None),
        }
    _ensure_gc()
    threading.Thread(target=_reader, args=(session_id,), daemon=True).start()
    return session_id


def write_input(session_id, data):
    """把数据原样写入 pty（回车需调用方传入 \r）。"""
    with _lock:
        sess = _sessions.get(session_id)
    if not sess:
        return False
    sess["last"] = time.time()
    try:
        sess["pty"].write(data if isinstance(data, str) else str(data))
        return True
    except Exception:
        return False


def resize(session_id, cols, rows):
    with _lock:
        sess = _sessions.get(session_id)
    if not sess:
        return False
    try:
        sess["pty"].setwinsize(int(rows), int(cols))  # winpty: (rows, cols)
        return True
    except Exception:
        return False


def close_session(session_id):
    """主动关闭：触发整树查杀与 on_exit（幂等）。"""
    if session_id not in _sessions:
        return False
    _fire_exit(session_id)
    return True


def count():
    with _lock:
        return len(_sessions)
