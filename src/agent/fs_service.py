# -*- coding: utf-8 -*-
"""
fs_service.py —— 文件浏览能力（T6）：fs.list / fs.tree / fs.stat / fs.read

每个入口先过 path_guard.resolve 做安全校验，再操作磁盘。
handler 签名统一为 fn(ctx, msg, send)：
  - ctx  : 握手授权上下文 {user, caps, token, sid}（本模块不强依赖）
  - msg  : 完整信封 dict（含 id/cap/payload），用 msg["id"] 回填应答
  - send : 整合者注入的 “把 dict 发回本 WS 连接” 可调用对象
所有 handler 在模块加载时经 cap_router.register_handler 注册。
"""
import os
import string

from . import cap_dispatch as cap_router
from . import protocol
from .protocol import Cap, Err
from .config_loader import getint
from .path_guard import resolve, allow_roots, PathDenied


def _roots():
    """根视图条目：配了 allow_roots 白名单则只列白名单根（收窄可见范围，
    避免空路径绕过白名单枚举全部盘符）；未配则列本机所有可用盘符。"""
    roots = allow_roots()
    if roots:
        return [r if r.endswith("\\") else r + "\\"
                for r in roots if os.path.exists(r)]
    out = []
    for c in string.ascii_uppercase:
        d = "%s:\\" % c
        if os.path.exists(d):
            out.append(d)
    return out

# 文件属性位（Windows）
_FA_READONLY = 0x1
_FA_HIDDEN = 0x2
_FA_SYSTEM = 0x4
_FA_DIRECTORY = 0x10
_FA_ARCHIVE = 0x20


def _attr_str(st):
    """把 st_file_attributes 转成简短标记串，如 'HR'（隐藏+只读）。"""
    a = getattr(st, "st_file_attributes", 0) or 0
    s = ""
    if a & _FA_DIRECTORY:
        s += "D"
    if a & _FA_READONLY:
        s += "R"
    if a & _FA_HIDDEN:
        s += "H"
    if a & _FA_SYSTEM:
        s += "S"
    if a & _FA_ARCHIVE:
        s += "A"
    return s


def _err(send, mid, code, message, cap):
    send(protocol.error(mid, code, message, cap=cap))


def _h_list(ctx, msg, send):
    mid = msg.get("id")
    pl = msg.get("payload") or {}
    # 空路径 = 根：直接返回盘符列表，不过 resolve（resolve 拒空路径）
    if not (pl.get("path") or "").strip():
        items = [{"name": d, "is_dir": True, "size": 0, "mtime": 0, "attr": "D"}
                 for d in _roots()]
        return send(protocol.resp(mid, Cap.FS_LIST, {"items": items, "total": len(items)}))
    try:
        base = resolve(pl.get("path", ""))
    except PathDenied as e:
        return _err(send, mid, Err.PATH_DENIED, str(e), Cap.FS_LIST)
    if not os.path.isdir(base):
        return _err(send, mid, Err.BAD_REQUEST, "路径不是目录", Cap.FS_LIST)

    offset = int(pl.get("offset", 0) or 0)
    limit = int(pl.get("limit", 200) or 200)
    sort = (pl.get("sort") or "name").lower()

    items = []
    try:
        with os.scandir(base) as it:
            for e in it:
                try:
                    st = e.stat()
                    is_dir = e.is_dir()
                    items.append({
                        "name": e.name,
                        "is_dir": is_dir,
                        "size": 0 if is_dir else st.st_size,
                        "mtime": st.st_mtime,
                        "attr": _attr_str(st),
                    })
                except OSError:
                    # 单项 stat 失败（权限/占用）跳过，不影响整体列举
                    continue
    except OSError as e:
        return _err(send, mid, Err.INTERNAL, f"读取目录失败: {e}", Cap.FS_LIST)

    key = {
        "size": lambda x: x["size"],
        "mtime": lambda x: x["mtime"],
    }.get(sort, lambda x: x["name"].lower())
    # 目录优先，再按选定字段
    items.sort(key=lambda x: (not x["is_dir"], key(x)))

    total = len(items)
    page = items[offset:offset + limit] if limit > 0 else items[offset:]
    send(protocol.resp(mid, Cap.FS_LIST, {"items": page, "total": total}))


def _has_children(path):
    try:
        with os.scandir(path) as it:
            for _ in it:
                return True
    except OSError:
        return False
    return False


def _h_tree(ctx, msg, send):
    mid = msg.get("id")
    pl = msg.get("payload") or {}
    # 空路径 = 根：返回盘符作为顶层节点
    if not (pl.get("path") or "").strip():
        children = [{"name": d, "is_dir": True, "has_children": True} for d in _roots()]
        return send(protocol.resp(mid, Cap.FS_TREE, {"children": children}))
    try:
        base = resolve(pl.get("path", ""))
    except PathDenied as e:
        return _err(send, mid, Err.PATH_DENIED, str(e), Cap.FS_TREE)
    if not os.path.isdir(base):
        return _err(send, mid, Err.BAD_REQUEST, "路径不是目录", Cap.FS_TREE)

    children = []
    try:
        with os.scandir(base) as it:
            for e in it:
                try:
                    is_dir = e.is_dir()
                except OSError:
                    continue
                # 目录一律标记可展开（不再对每个子目录 scandir 探测，避免大目录雪崩变慢/超时）；
                # 点开时按需再取，空目录展开后自然无子项。
                children.append({
                    "name": e.name,
                    "is_dir": is_dir,
                    "has_children": is_dir,
                })
    except OSError as e:
        return _err(send, mid, Err.INTERNAL, f"读取目录失败: {e}", Cap.FS_TREE)

    children.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    send(protocol.resp(mid, Cap.FS_TREE, {"children": children}))


def _h_stat(ctx, msg, send):
    mid = msg.get("id")
    pl = msg.get("payload") or {}
    try:
        p = resolve(pl.get("path", ""))
    except PathDenied as e:
        return _err(send, mid, Err.PATH_DENIED, str(e), Cap.FS_STAT)
    try:
        st = os.stat(p)
    except OSError as e:
        return _err(send, mid, Err.NOT_FOUND, f"目标不存在: {e}", Cap.FS_STAT)

    is_dir = os.path.isdir(p)
    limit = getint("capability", "read_inline_limit", 2097152)
    previewable = (not is_dir) and st.st_size <= limit
    send(protocol.resp(mid, Cap.FS_STAT, {
        "name": os.path.basename(p) or p,
        "is_dir": is_dir,
        "size": 0 if is_dir else st.st_size,
        "mtime": st.st_mtime,
        "previewable": previewable,
    }))


def _h_read(ctx, msg, send):
    mid = msg.get("id")
    pl = msg.get("payload") or {}
    try:
        p = resolve(pl.get("path", ""))
    except PathDenied as e:
        return _err(send, mid, Err.PATH_DENIED, str(e), Cap.FS_READ)
    if not os.path.isfile(p):
        return _err(send, mid, Err.BAD_REQUEST, "路径不是文件", Cap.FS_READ)

    limit = getint("capability", "read_inline_limit", 2097152)
    offset = int(pl.get("offset", 0) or 0)
    length = pl.get("length")
    encoding = pl.get("encoding") or "utf-8"

    size = os.path.getsize(p)
    if length is None:
        # 未指定窗口：整文件超上限则拒绝内联，引导走下载旁路
        if size - offset > limit:
            return _err(send, mid, Err.BAD_REQUEST,
                        "文件超过内联预览上限，请使用 dl.token 下载", Cap.FS_READ)
        length = limit
    else:
        length = int(length)
        if length > limit:
            return _err(send, mid, Err.BAD_REQUEST,
                        "请求窗口超过内联上限，请使用 dl.token 下载", Cap.FS_READ)

    try:
        with open(p, "rb") as f:
            f.seek(max(0, offset))
            data = f.read(max(0, length))
    except OSError as e:
        return _err(send, mid, Err.INTERNAL, f"读取失败: {e}", Cap.FS_READ)

    eof = (offset + len(data)) >= size
    text = data.decode(encoding, errors="replace")
    send(protocol.resp(mid, Cap.FS_READ, {"text": text, "eof": eof}))


# ---- 注册到能力路由（模块加载即注册）----
cap_router.register_handler(Cap.FS_LIST, _h_list)
cap_router.register_handler(Cap.FS_TREE, _h_tree)
cap_router.register_handler(Cap.FS_STAT, _h_stat)
cap_router.register_handler(Cap.FS_READ, _h_read)   # 文件内联读取（大文件仍引导走 dl.token 旁路）
