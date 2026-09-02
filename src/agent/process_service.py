# -*- coding: utf-8 -*-
"""
process_service.py —— 进程能力（T6）：proc.list / proc.kill（被控端仅暴露"查看 + 关闭"）

优先用 psutil；无 psutil 时降级为 tasklist / taskkill 命令行。
handler 签名 fn(ctx, msg, send)，模块加载时经 cap_dispatch.register_handler 注册。

proc.start（启动任意进程）实现保留但**不注册**——被控端只放开"看/杀"，
与 fs.read 同样靠"不注册"收敛能力面。
"""
import os
import csv
import io
import subprocess

from . import cap_dispatch as cap_router
from . import protocol
from .protocol import Cap, Err
from .path_guard import resolve, PathDenied

try:
    import psutil
    _HAS_PSUTIL = True
except Exception:
    psutil = None
    _HAS_PSUTIL = False

# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP（脱离控制台，独立进程组）
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW（给 tasklist/taskkill 用，避免闪窗）


def _err(send, mid, code, message, cap):
    send(protocol.error(mid, code, message, cap=cap))


# ---------------- proc.list ----------------
def _list_psutil():
    # 只取廉价属性(pid/name/memory_info)，去掉 exe/username/cmdline/create_time
    # —— 这三者在 Windows 上每进程都要 OpenProcess/LookupSid，几百进程会拖慢到超时。
    items = []
    for p in psutil.process_iter(["pid", "name", "memory_info"], ad_value=None):
        info = p.info
        mi = info.get("memory_info")
        items.append({
            "pid": info.get("pid"),
            "name": info.get("name") or "",
            "exe": "",
            "user": "",
            "cpu": 0.0,          # 单次采样无区间，恒 0
            "mem": (mi.rss if mi else 0),
            "cmdline": info.get("name") or "",   # 命令行列回退显示进程名（避免逐进程 OpenProcess）
            "create_time": None,
        })
    return items


def _list_tasklist():
    """无 psutil 回退：tasklist /FO CSV /V 解析（字段有限）。"""
    items = []
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/V", "/NH"],
            creationflags=_NO_WINDOW, text=True, errors="replace")
    except Exception:
        return items
    for row in csv.reader(io.StringIO(out)):
        # 列：映像名, PID, 会话名, 会话#, 内存使用, 状态, 用户名, CPU时间, 窗口标题
        if len(row) < 9:
            continue
        try:
            pid = int(row[1])
        except ValueError:
            continue
        mem_kb = "".join(ch for ch in row[4] if ch.isdigit())
        items.append({
            "pid": pid,
            "name": row[0],
            "exe": "",
            "user": row[6],
            "cpu": 0.0,
            "mem": int(mem_kb) * 1024 if mem_kb else 0,
            "cmdline": row[0],
            "create_time": None,
        })
    return items


def _h_list(ctx, msg, send):
    mid = msg.get("id")
    pl = msg.get("payload") or {}
    flt = (pl.get("filter") or "").strip().lower()
    sort = (pl.get("sort") or "pid").lower()
    offset = int(pl.get("offset", 0) or 0)
    limit = int(pl.get("limit", 200) or 200)

    try:
        items = _list_psutil() if _HAS_PSUTIL else _list_tasklist()
    except Exception as e:
        return _err(send, mid, Err.INTERNAL, f"进程枚举失败: {e}", Cap.PROC_LIST)

    if flt:
        items = [x for x in items
                 if flt in (x["name"] or "").lower()
                 or flt in (x["cmdline"] or "").lower()
                 or flt == str(x["pid"])]

    reverse = sort in ("mem", "cpu", "create_time")
    key = {
        "name": lambda x: (x["name"] or "").lower(),
        "mem": lambda x: x["mem"] or 0,
        "cpu": lambda x: x["cpu"] or 0,
        "create_time": lambda x: x["create_time"] or 0,
    }.get(sort, lambda x: x["pid"] or 0)
    items.sort(key=key, reverse=reverse)

    total = len(items)
    page = items[offset:offset + limit] if limit > 0 else items[offset:]
    send(protocol.resp(mid, Cap.PROC_LIST, {"items": page, "total": total}))


# ---------------- proc.start（保留实现，不注册）----------------
_EXEC_EXT = {".exe", ".com"}
_BAT_EXT = {".bat", ".cmd"}


def _h_start(ctx, msg, send):
    mid = msg.get("id")
    pl = msg.get("payload") or {}
    try:
        target = resolve(pl.get("path", ""))
    except PathDenied as e:
        return _err(send, mid, Err.PATH_DENIED, str(e), Cap.PROC_START)
    if not os.path.exists(target):
        return _err(send, mid, Err.NOT_FOUND, "目标不存在", Cap.PROC_START)

    args = pl.get("args") or []
    if isinstance(args, str):
        args = [args]
    cwd = pl.get("cwd") or os.path.dirname(target) or None
    ext = os.path.splitext(target)[1].lower()

    try:
        if ext in _EXEC_EXT:
            p = subprocess.Popen([target] + list(args), cwd=cwd,
                                 creationflags=_DETACHED, close_fds=True)
            pid = p.pid
        elif ext in _BAT_EXT:
            p = subprocess.Popen(["cmd", "/c", target] + list(args), cwd=cwd,
                                 creationflags=_DETACHED, close_fds=True)
            pid = p.pid
        else:
            # 文档类：交给系统关联程序打开，无独立 pid
            os.startfile(target)  # type: ignore[attr-defined]
            pid = None
    except Exception as e:
        return _err(send, mid, Err.INTERNAL, f"启动失败: {e}", Cap.PROC_START)

    send(protocol.resp(mid, Cap.PROC_START, {"pid": pid}))


# ---------------- proc.kill ----------------
def _kill_psutil(pid):
    p = psutil.Process(pid)
    children = p.children(recursive=True)
    for c in children:
        try:
            c.kill()
        except Exception:
            pass
    p.terminate()
    try:
        p.wait(timeout=3)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def _kill_taskkill(pid):
    subprocess.check_call(["taskkill", "/T", "/F", "/PID", str(pid)],
                          creationflags=_NO_WINDOW)


def _h_kill(ctx, msg, send):
    mid = msg.get("id")
    pl = msg.get("payload") or {}
    try:
        pid = int(pl.get("pid"))
    except (TypeError, ValueError):
        return _err(send, mid, Err.BAD_REQUEST, "pid 非法", Cap.PROC_KILL)
    try:
        if _HAS_PSUTIL:
            _kill_psutil(pid)
        else:
            _kill_taskkill(pid)
    except Exception as e:
        return _err(send, mid, Err.INTERNAL, f"结束进程失败: {e}", Cap.PROC_KILL)
    send(protocol.resp(mid, Cap.PROC_KILL, {"ok": True}))


# ---- 注册（被控端暴露 查看 + 启动 + 关闭，与 remote-agent 一致）----
cap_router.register_handler(Cap.PROC_LIST, _h_list)
cap_router.register_handler(Cap.PROC_START, _h_start)
cap_router.register_handler(Cap.PROC_KILL, _h_kill)
