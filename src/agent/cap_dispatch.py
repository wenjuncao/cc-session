# -*- coding: utf-8 -*-
"""
cap_dispatch.py —— 被控端 WS 业务消息分发器（cap_router 的相对导入版）。

“路由 + 鉴权 + 分发”骨架，并自实现 term.* / cmd.exec / claude.* / update.push 的 handler
（terminal_session 内 winpty 为函数内惰性导入，顶层 import 不拉起 pywinpty）。其余能力 handler
由各能力模块在导入时自行注册：
  - fs_service   注册 fs.list / fs.tree / fs.stat / fs.read / dl.token
  - screen_service 注册 screen.shot / screen.stream.start|stop / screen.input
  - proc_service 注册 proc.list / proc.start / proc.kill
未注册的能力一律回 NOT_FOUND；能力面同时受 authz 能力位约束。

dispatch(ctx, msg, send)：
  1. ACK → 交 stream_hub 抬背压水位（属已授权连接的流控，不再二次鉴权）。
  2. 业务 req → token 校验(session_token.verify) → authz.require(能力位) → 按 cap 分发。
  3. 未知 cap 回 NOT_FOUND；handler 异常回 INTERNAL。
handler 统一签名 fn(ctx, msg, send)。
对 session_token/authz/audit 延迟导入，避免加载期依赖未就绪时崩溃。
"""
import base64
import threading
import time

from . import protocol
from .protocol import Cap, Err, MsgType
from .stream_hub import HUB
from . import terminal_session, cmd_exec, claude_driver

# cap -> handler(ctx, msg, send)
_HANDLERS = {}

# 最近一次业务调用时间戳（被控端判断“是否正在被操控”的唯一依据）。GIL 下 float 赋值原子，无需锁。
LAST_ACTIVITY = 0.0


def register_handler(cap, fn):
    """注册/覆盖一个 cap 的 handler（供 fs_service / screen_service 接入）。"""
    _HANDLERS[cap] = fn


# ---------- 延迟导入包装（未就绪时安全失败） ----------
def _verify(token, conn_id):
    try:
        from . import session_token
        return session_token.verify(token, conn_id)
    except Exception:
        return None


def _require(ctx, cap):
    try:
        from . import authz
        return authz.require(ctx, cap)
    except Exception:
        return False


def _audit(actor, peer, cap, detail, result):
    try:
        from . import audit
        audit.log(actor, peer, cap, detail, result)
    except Exception:
        pass


# ---------- 主分发 ----------
def dispatch(ctx, msg, send):
    global LAST_ACTIVITY
    if not isinstance(msg, dict):
        return
    LAST_ACTIVITY = time.time()
    mtype = msg.get("type")
    cap = msg.get("cap")
    mid = msg.get("id")

    if mtype == MsgType.ACK:
        HUB.ack(mid, msg.get("seq"))
        return

    payload = msg.get("payload") or {}
    token = payload.get("_token") or (ctx or {}).get("token")
    if not _verify(token, (ctx or {}).get("conn_id")):
        send(protocol.error(mid, Err.AUTH_REQUIRED, "通道未授权或 token 失效", cap))
        return

    if not _require(ctx, cap):
        send(protocol.error(mid, Err.FORBIDDEN, "缺少能力位：%s" % cap, cap))
        _audit(ctx.get("user"), ctx.get("peer"), cap, {"reason": "forbidden"}, "forbidden")
        return

    handler = _HANDLERS.get(cap)
    if handler is None:
        send(protocol.error(mid, Err.NOT_FOUND, "未知能力：%s" % cap, cap))
        return

    try:
        handler(ctx, msg, send)
    except Exception as e:
        send(protocol.error(mid, Err.INTERNAL, "处理异常：%s" % e, cap))


# ================= 自实现 handler（签名 fn(ctx, msg, send)） =================

def _h_term_open(ctx, msg, send):
    mid = msg.get("id")
    p = msg.get("payload") or {}
    sid = protocol.new_id()
    HUB.create(sid, Cap.TERM_OUTPUT, send)   # 建流并把本连接挂为订阅者

    def emit(b64):
        HUB.emit(sid, b64)

    def on_exit(session_id):
        HUB.close(session_id)
        send(protocol.event(Cap.TERM_OUTPUT, {"session_id": session_id, "closed": True}))

    try:
        terminal_session.open_session(
            sid, shell=p.get("shell", "powershell"), cwd=p.get("cwd"),
            cols=int(p.get("cols") or 80), rows=int(p.get("rows") or 24),
            emit=emit, on_exit=on_exit,
        )
    except terminal_session.SessionLimit as e:
        HUB.close(sid)
        send(protocol.error(mid, Err.SESSION_LIMIT, str(e), Cap.TERM_OPEN))
        return
    except terminal_session.PtyUnavailable as e:
        HUB.close(sid)
        send(protocol.error(mid, Err.INTERNAL,
                            "PTY 不可用（需 pywinpty，可改用 cmd.exec 降级）：%s" % e, Cap.TERM_OPEN))
        return
    send(protocol.resp(mid, Cap.TERM_OPEN, {"session_id": sid}))
    _audit(ctx.get("user"), ctx.get("peer"), Cap.TERM_OPEN,
           {"session_id": sid, "shell": p.get("shell"), "cwd": p.get("cwd")}, "ok")


def _h_term_input(ctx, msg, send):
    p = msg.get("payload") or {}
    terminal_session.write_input(p.get("session_id"), p.get("data", ""))
    # 无 resp（输出走 term.output 流）


def _h_term_resize(ctx, msg, send):
    mid = msg.get("id")
    p = msg.get("payload") or {}
    ok = terminal_session.resize(p.get("session_id"), p.get("cols", 80), p.get("rows", 24))
    send(protocol.resp(mid, Cap.TERM_RESIZE, {"ok": bool(ok)}))


def _h_term_close(ctx, msg, send):
    mid = msg.get("id")
    p = msg.get("payload") or {}
    terminal_session.close_session(p.get("session_id"))  # 触发 on_exit→closed 事件
    send(protocol.resp(mid, Cap.TERM_CLOSE, {"ok": True}))


def _h_cmd_exec(ctx, msg, send):
    mid = msg.get("id")
    p = msg.get("payload") or {}
    cmdline = p.get("cmd", "")
    cwd = p.get("cwd")
    timeout = p.get("timeout")
    _audit(ctx.get("user"), ctx.get("peer"), Cap.CMD_EXEC, {"cmd": cmdline, "cwd": cwd}, "start")

    HUB.create(mid, Cap.TERM_OUTPUT, send)   # 复用 term.output 流下发命令输出

    def worker():
        def emit(seq, data, fin):
            b64 = base64.b64encode((data or "").encode("utf-8", "replace")).decode("ascii")
            HUB.emit(mid, b64, fin)
        # 整体兜底：run/emit 若抛异常(如非法 timeout 触发 TypeError)，worker 线程会静默
        # 死亡，导致 HUB 流泄漏且客户端永久挂起。此处必须回收流并回错，绝不让请求悬空。
        try:
            code = cmd_exec.run(cmdline, cwd=cwd, timeout=timeout, emit=emit)
        except Exception as e:
            HUB.close(mid)
            send(protocol.error(mid, Err.INTERNAL, "命令执行异常：%s" % e, Cap.CMD_EXEC))
            _audit(ctx.get("user"), ctx.get("peer"), Cap.CMD_EXEC,
                   {"cmd": cmdline, "error": str(e)}, "error")
            return
        HUB.close(mid)
        if code is None:
            send(protocol.error(mid, Err.TIMEOUT, "命令执行超时", Cap.CMD_EXEC))
        else:
            send(protocol.resp(mid, Cap.CMD_EXEC, {"exit_code": code}))
        _audit(ctx.get("user"), ctx.get("peer"), Cap.CMD_EXEC, {"cmd": cmdline, "exit": code}, "done")

    threading.Thread(target=worker, daemon=True).start()


def _h_claude_start(ctx, msg, send):
    mid = msg.get("id")
    p = msg.get("payload") or {}
    cs = protocol.new_id()
    HUB.create(cs, Cap.CLAUDE_OUTPUT, send)

    def emit(text):
        HUB.emit(cs, text)

    def on_exit(claude_session):
        HUB.close(claude_session)
        send(protocol.event(Cap.CLAUDE_OUTPUT, {"claude_session": claude_session, "closed": True}))

    try:
        claude_driver.start(cs, p.get("project_path"), p.get("prompt", ""),
                            p.get("opts"), emit, on_exit)
    except Exception as e:
        HUB.close(cs)
        send(protocol.error(mid, Err.INTERNAL, "启动 claude 失败：%s" % e, Cap.CLAUDE_START))
        return
    send(protocol.resp(mid, Cap.CLAUDE_START, {"claude_session": cs}))
    _audit(ctx.get("user"), ctx.get("peer"), Cap.CLAUDE_START,
           {"claude_session": cs, "project_path": p.get("project_path")}, "ok")


def _h_claude_nudge(ctx, msg, send):
    mid = msg.get("id")
    p = msg.get("payload") or {}
    ok = claude_driver.nudge(p.get("claude_session"), p.get("text", ""))
    send(protocol.resp(mid, Cap.CLAUDE_NUDGE, {"ok": bool(ok)}))


def _h_claude_stop(ctx, msg, send):
    mid = msg.get("id")
    p = msg.get("payload") or {}
    claude_driver.stop(p.get("claude_session"))
    send(protocol.resp(mid, Cap.CLAUDE_STOP, {"ok": True}))
    _audit(ctx.get("user"), ctx.get("peer"), Cap.CLAUDE_STOP,
           {"claude_session": p.get("claude_session")}, "ok")


def _h_update_push(ctx, msg, send):
    """控制端推送升级：校验来源后交给 updater 异步下载并替换自己。

    立刻回 resp 是必须的——_apply_update 成功后会杀掉本进程重启，
    要是等下载完再回包，控制端只会看到连接断开，拿不到"已受理"。
    """
    mid = msg.get("id")
    p = msg.get("payload") or {}
    from . import updater
    ok, message = updater.push_update(
        p.get("url"), p.get("sha256"), p.get("version"),
        allow_host=(ctx or {}).get("peer"),      # 只许从下发指令的控制端拉
    )
    _audit(ctx.get("user"), ctx.get("peer"), Cap.UPDATE_PUSH,
           {"url": p.get("url"), "version": p.get("version"), "msg": message},
           "ok" if ok else "reject")
    if not ok:
        send(protocol.error(mid, Err.BAD_REQUEST, message, Cap.UPDATE_PUSH))
        return
    send(protocol.resp(mid, Cap.UPDATE_PUSH, {"accepted": True, "message": message}))


# ---------- 内建 handler 注册 ----------
_HANDLERS.update({
    Cap.TERM_OPEN: _h_term_open,
    Cap.TERM_INPUT: _h_term_input,
    Cap.TERM_RESIZE: _h_term_resize,
    Cap.TERM_CLOSE: _h_term_close,
    Cap.CMD_EXEC: _h_cmd_exec,
    Cap.CLAUDE_START: _h_claude_start,
    Cap.CLAUDE_NUDGE: _h_claude_nudge,
    Cap.CLAUDE_STOP: _h_claude_stop,
    Cap.UPDATE_PUSH: _h_update_push,
})
