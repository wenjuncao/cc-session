# -*- coding: utf-8 -*-
"""
session_token.py —— 有状态会话 token（内存表，绑定 conn_id 与 caps）。

职责（CONTRACT §6 T4）：
  - issue(user, caps, conn_id) -> token           下发 token。
  - verify(token, conn_id) -> {user,caps,sid}|None 校验过期/连接绑定/token_version，
                                                    并做 15~30 分钟滑动续期。
  - revoke(sid) / revoke_all()                     级联失效（revoke_all 自增 token_version）。
  - sid_of(token) -> sid|None                      供握手取回 sid 回填协议。
"""
import secrets
import threading
import time

SLIDING_TTL = 20 * 60  # 滑动过期窗口（秒），落在 15~30 分钟区间

_LOCK = threading.Lock()
_TOKENS = {}          # token -> {sid,user,caps,conn_id,exp,ver}
_SIDS = {}            # sid -> token
_TOKEN_VERSION = 0    # revoke_all 时自增，令所有旧 token 级联失效


def _drop(token):
    """内部：必须在持锁状态下调用。"""
    rec = _TOKENS.pop(token, None)
    if rec:
        _SIDS.pop(rec["sid"], None)


def _purge_expired(now):
    """内部：持锁状态下清理已过期 token（断开的连接不会再 verify，否则永久滞留）。"""
    dead = [t for t, r in _TOKENS.items() if now > r["exp"]]
    for t in dead:
        _drop(t)


def issue(user, caps, conn_id):
    token = secrets.token_urlsafe(32)
    sid = secrets.token_urlsafe(12)
    with _LOCK:
        _purge_expired(time.time())
        _TOKENS[token] = {
            "sid": sid, "user": user, "caps": list(caps),
            "conn_id": conn_id, "exp": time.time() + SLIDING_TTL,
            "ver": _TOKEN_VERSION,
        }
        _SIDS[sid] = token
    return token


def sid_of(token):
    with _LOCK:
        rec = _TOKENS.get(token)
        return rec["sid"] if rec else None


def verify(token, conn_id):
    """校验成功返回 {user,caps,sid} 并滑动续期；失败返回 None。"""
    with _LOCK:
        rec = _TOKENS.get(token)
        if not rec:
            return None
        if rec["ver"] != _TOKEN_VERSION:
            _drop(token)
            return None
        if rec["conn_id"] != conn_id:
            return None
        if time.time() > rec["exp"]:
            _drop(token)
            return None
        rec["exp"] = time.time() + SLIDING_TTL  # 滑动续期
        return {"user": rec["user"], "caps": list(rec["caps"]), "sid": rec["sid"]}


def revoke(sid):
    with _LOCK:
        tok = _SIDS.pop(sid, None)
        if tok:
            _TOKENS.pop(tok, None)


def revoke_all():
    """token_version 自增，级联失效全部已发 token。"""
    global _TOKEN_VERSION
    with _LOCK:
        _TOKEN_VERSION += 1
        _TOKENS.clear()
        _SIDS.clear()
