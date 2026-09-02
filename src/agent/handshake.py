# -*- coding: utf-8 -*-
"""
handshake.py —— WS 端点 /ws 的鉴权握手（CONTRACT §2.1，B级：wss 已建连后做应用层鉴权）。

时序：
  server → event auth {stage:"challenge", nonce, ts, ttl:30}
  client → req   auth {user, password, nonce, node_id, node_name}
  server 校验 nonce(一次性+TTL 防重放) + rate_check + verify_login：
    成功 → session_token.issue → resp auth {ok, token, caps, sid}，返回 ctx
    失败 → error AUTH_FAILED / RATE_LIMITED，返回 None（调用方随后关闭连接）

导出：run(ws, remote_ip) -> ctx|None
  ctx = {user, caps, token, sid, conn_id, peer}
"""
import json
import secrets
import threading
import time
import uuid

from . import protocol
from .protocol import Cap, Err

from . import auth
from . import authz
from . import audit
from . import session_token

CHALLENGE_TTL = 30       # 秒，与协议下发 ttl 一致
RECV_TIMEOUT = 35        # 收取 auth req 的等待上限（秒）
_NONCE_MAX = 4096        # nonce LRU 容量上限

# 一次性 nonce 池：nonce -> 过期时间戳（防重放）
_NONCES = {}
_NONCE_LOCK = threading.Lock()


def _issue_nonce():
    nonce = secrets.token_urlsafe(16)
    now = time.time()
    with _NONCE_LOCK:
        # 顺手清理过期项，并在超量时丢弃最旧
        expired = [k for k, exp in _NONCES.items() if exp < now]
        for k in expired:
            _NONCES.pop(k, None)
        if len(_NONCES) >= _NONCE_MAX:
            for k in list(_NONCES.keys())[:len(_NONCES) - _NONCE_MAX + 1]:
                _NONCES.pop(k, None)
        _NONCES[nonce] = now + CHALLENGE_TTL
    return nonce


def _consume_nonce(nonce):
    """一次性消费：存在且未过期返回 True 并移除；否则 False。"""
    if not nonce:
        return False
    with _NONCE_LOCK:
        exp = _NONCES.pop(nonce, None)
    return exp is not None and exp >= time.time()


def _send(ws, obj):
    ws.send(json.dumps(obj, ensure_ascii=False))


def run(ws, remote_ip):
    conn_id = uuid.uuid4().hex
    my_nonce = _issue_nonce()
    _send(ws, protocol.event(Cap.AUTH, {
        "stage": "challenge", "nonce": my_nonce,
        "ts": time.time(), "ttl": CHALLENGE_TTL,
    }))

    # 收取 client 的 auth req
    try:
        raw = ws.receive(timeout=RECV_TIMEOUT)
    except Exception:
        raw = None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")

    try:
        msg = json.loads(raw)
    except Exception:
        _send(ws, protocol.error(None, Err.BAD_REQUEST, "握手报文非法 json", Cap.AUTH))
        return None

    mid = msg.get("id")
    payload = msg.get("payload") or {}
    user = payload.get("user")
    password = payload.get("password")
    client_nonce = payload.get("nonce")
    node_id = payload.get("node_id")
    node_name = payload.get("node_name")

    # 限速键：来源为回环地址时所有 peer 都来自 127.0.0.1，若只按 IP 限速会退化成共享桶
    # ——单个攻击者失败几次即锁死该桶、连带误锁全部合法节点。故回环来源并入 node_id
    # 隔离各 peer；真实远程 IP（端口转发）仍按纯 IP 限速不变。
    rl_key = remote_ip
    if remote_ip in ("127.0.0.1", "::1", "localhost"):
        rl_key = "%s|%s" % (remote_ip, node_id or "-")

    # 限速：锁定期直接拒绝
    if not auth.rate_check(rl_key):
        _send(ws, protocol.error(mid, Err.RATE_LIMITED, "登录尝试过于频繁，请稍后再试", Cap.AUTH))
        audit.log(user or "?", remote_ip, Cap.AUTH, {"node_id": node_id}, "rate_limited")
        return None

    # nonce 校验：必须与本连接下发的一致，且一次性未过期
    if client_nonce != my_nonce or not _consume_nonce(client_nonce):
        auth.note_fail(rl_key)
        _send(ws, protocol.error(mid, Err.AUTH_FAILED, "nonce 无效或已过期", Cap.AUTH))
        audit.log(user or "?", remote_ip, Cap.AUTH, {"reason": "bad_nonce"}, "fail")
        return None

    # 账密校验
    if not auth.verify_login(user, password):
        auth.note_fail(rl_key)
        _send(ws, protocol.error(mid, Err.AUTH_FAILED, "账号或口令错误", Cap.AUTH))
        audit.log(user or "?", remote_ip, Cap.AUTH, {"node_id": node_id}, "fail")
        return None

    # 成功：发 token
    auth.note_ok(rl_key)
    caps = list(authz.ALL_CAPS)
    token = session_token.issue(user, caps, conn_id)
    sid = session_token.sid_of(token)
    _send(ws, protocol.resp(mid, Cap.AUTH, {
        "ok": True, "token": token, "caps": caps, "sid": sid,
    }))
    audit.log(user, remote_ip, Cap.AUTH,
              {"node_id": node_id, "node_name": node_name, "conn_id": conn_id}, "ok")
    return {
        "user": user, "caps": caps, "token": token,
        "sid": sid, "conn_id": conn_id, "peer": remote_ip,
        "node_id": node_id, "node_name": node_name,
    }
