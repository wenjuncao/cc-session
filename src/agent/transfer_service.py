# -*- coding: utf-8 -*-
"""
transfer_service.py —— 下载旁路（T6）：dl.token 申领 + HTTP /dl 传输

安全模型：WS 侧只发 dl.token（cap handler）申领 token，token 内绑定「已过 path_guard
校验的绝对路径」，存内存表带 TTL。真正下载走 HTTP GET /dl?token=...，**只认 token，绝不
接受原始 path**，杜绝二次穿越。
  - 单文件：支持 HTTP Range 断点续传（Accept-Ranges / Content-Range / 206 分块 yield）
  - 文件夹：流式 zip（边打包边发，非 seekable 数据描述符模式）
  - max_concurrent_downloads 限制同时下载数

handler 签名 fn(ctx, msg, send)，dl.token 在模块加载时注册。
register(app) 由 app_agent 在 create_agent_app() 中调用，挂 /dl 路由。
"""
import os
import time
import zipfile
import threading

from . import cap_dispatch as cap_router
from . import protocol
from .protocol import Cap, Err
from .config_loader import getint
from .path_guard import resolve, PathDenied

_TTL = 600.0            # token 有效期（秒）
_CHUNK = 65536

# token 表：token -> {path, kind, exp}
_TOKENS = {}
_TLOCK = threading.Lock()

# 并发下载计数
_DL_LOCK = threading.Lock()
_DL_ACTIVE = 0


def _new_token():
    return protocol.new_id()


def _gc():
    now = time.time()
    dead = [t for t, r in _TOKENS.items() if r["exp"] < now]
    for t in dead:
        _TOKENS.pop(t, None)


def issue_token(path, kind="file"):
    """校验 path 并签发下载 token；kind: 'file'|'zip'。返回 token 字符串。"""
    real = resolve(path)          # 越界会 raise PathDenied
    if kind == "zip":
        if not os.path.isdir(real):
            raise PathDenied("zip 下载要求目录")
    else:
        kind = "file"
        if not os.path.isfile(real):
            raise PathDenied("file 下载要求普通文件")
    token = _new_token()
    with _TLOCK:
        _gc()
        _TOKENS[token] = {"path": real, "kind": kind, "exp": time.time() + _TTL}
    return token


def _lookup(token):
    with _TLOCK:
        rec = _TOKENS.get(token)
        if not rec:
            return None
        if rec["exp"] < time.time():
            _TOKENS.pop(token, None)
            return None
        return dict(rec)


def _slot_acquire():
    global _DL_ACTIVE
    cap = getint("capability", "max_concurrent_downloads", 5)
    with _DL_LOCK:
        if _DL_ACTIVE >= cap:
            return False
        _DL_ACTIVE += 1
        return True


def _slot_release():
    global _DL_ACTIVE
    with _DL_LOCK:
        if _DL_ACTIVE > 0:
            _DL_ACTIVE -= 1


# ---------- 单文件 Range ----------
def _parse_range(header, size):
    """解析 'bytes=start-end'，返回 (start, end)（含端）；不合法返回 None。"""
    if not header or not header.startswith("bytes="):
        return None
    spec = header[len("bytes="):].split(",")[0].strip()
    if "-" not in spec:
        return None
    a, b = spec.split("-", 1)
    try:
        if a == "":
            # 后缀：最后 b 字节
            n = int(b)
            if n <= 0:
                return None
            start = max(0, size - n)
            return start, size - 1
        start = int(a)
        end = int(b) if b else size - 1
    except ValueError:
        return None
    if start > end or start >= size:
        return None
    return start, min(end, size - 1)


def _serve_file(path, range_header):
    from flask import Response
    size = os.path.getsize(path)
    rng = _parse_range(range_header, size)
    status = 200
    start, end = 0, size - 1
    if rng:
        start, end = rng
        status = 206
    length = end - start + 1

    def gen():
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(_CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
        finally:
            _slot_release()

    resp = Response(gen(), status=status, mimetype="application/octet-stream")
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length)
    resp.headers["Content-Disposition"] = (
        "attachment; filename*=UTF-8''%s" % _quote(os.path.basename(path)))
    if status == 206:
        resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return resp


# ---------- 文件夹流式 zip ----------
class _StreamBuf:
    """给 zipfile 用的仅写缓冲；不提供 tell/seek → zipfile 进入非 seekable 数据描述符模式。"""

    def __init__(self):
        self.data = bytearray()

    def write(self, b):
        self.data += b
        return len(b)

    def flush(self):
        pass


def _iter_zip(folder):
    buf = _StreamBuf()
    base = os.path.dirname(folder.rstrip("\\/")) or folder
    try:
        zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, allowZip64=True)
        for root, _dirs, files in os.walk(folder):
            for name in files:
                full = os.path.join(root, name)
                arc = os.path.relpath(full, base)
                try:
                    with zf.open(arc, "w") as dest, open(full, "rb") as src:
                        while True:
                            chunk = src.read(_CHUNK)
                            if not chunk:
                                break
                            dest.write(chunk)
                            if buf.data:
                                yield bytes(buf.data)
                                del buf.data[:]
                except OSError:
                    # 占用/权限的单个文件跳过
                    continue
                if buf.data:
                    yield bytes(buf.data)
                    del buf.data[:]
        zf.close()
        if buf.data:
            yield bytes(buf.data)
            del buf.data[:]
    finally:
        _slot_release()


def _serve_zip(folder):
    from flask import Response
    resp = Response(_iter_zip(folder), mimetype="application/zip")
    fn = (os.path.basename(folder.rstrip("\\/")) or "download") + ".zip"
    resp.headers["Content-Disposition"] = (
        "attachment; filename*=UTF-8''%s" % _quote(fn))
    return resp


def _quote(s):
    from urllib.parse import quote
    return quote(s)


def serve(token, range_header=None):
    """按 token 返回 flask Response（Range 文件 / 流式 zip）；供整合者或测试直接调用。"""
    from flask import Response
    rec = _lookup(token)
    if not rec:
        return Response("无效或过期的下载令牌", status=404)
    if not _slot_acquire():
        return Response("下载并发数已达上限", status=429)
    try:
        if rec["kind"] == "zip":
            return _serve_zip(rec["path"])
        return _serve_file(rec["path"], range_header)
    except Exception:
        # 构造阶段异常需归还名额（generator 已接管的场景由 finally 归还）
        _slot_release()
        return Response("下载失败", status=500)


def register(app):
    """挂 /dl 下载路由。同组能力模块（fs/proc/screen）由 app_agent 负责导入并注册。"""
    from flask import request

    @app.get("/dl")
    def _dl():
        token = request.args.get("token", "")
        return serve(token, request.headers.get("Range"))

    return app


# ---- dl.token cap handler ----
def _h_token(ctx, msg, send):
    mid = msg.get("id")
    pl = msg.get("payload") or {}
    path = pl.get("path", "")
    kind = pl.get("kind", "file")
    try:
        token = issue_token(path, kind)
    except PathDenied as e:
        send(protocol.error(mid, Err.PATH_DENIED, str(e), cap=Cap.DL_TOKEN))
        return
    send(protocol.resp(mid, Cap.DL_TOKEN,
                       {"token": token, "url": "/dl?token=" + token}))


cap_router.register_handler(Cap.DL_TOKEN, _h_token)
