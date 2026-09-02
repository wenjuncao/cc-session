# -*- coding: utf-8 -*-
"""
audit.py —— append-only 审计日志 + HMAC 防篡改链。

职责（CONTRACT §6 T4）：
  - log(actor, peer, cap, detail, result)：追加一条审计。
  - DB_OK 时插 disp_audit（带 prev_hmac 链）；否则每行一条 json 追加写 paths.AUDIT_LOG。
  - hmac = HMAC(secret_key, prev_hmac + 本条规范 json)，前后相扣成链，任何改动都会断链。
  secret 取 auth.cfg_get('secret_key')（ensure_seed 已保证存在），再兜 config[server].secret_key。
"""
import hashlib
import hmac as _hmac
import json
import os
import threading
import time

from . import db
from . import paths
from . import config_loader

_LOCK = threading.Lock()
_last = None  # 缓存上一条 hmac，避免每次回表/回读文件

# 文件模式审计日志滚动（未配 MySQL 时生效）：防止长期公网节点 audit.log 撑爆磁盘。
# HMAC 链靠内存 _last 逐条相扣，跨滚动文件仍连续；重启冷启动时 _get_prev 会回退读 .1。
_AUDIT_MAX = 5 * 1024 * 1024   # 单文件上限 5MB
_AUDIT_BACKUPS = 5             # 保留 audit.log.1 ~ .5


def _rotate_if_needed():
    """持锁调用：audit.log 超上限则 .4→.5 … .1→.2、audit.log→.1，腾出新文件继续写。"""
    try:
        if os.path.getsize(paths.AUDIT_LOG) < _AUDIT_MAX:
            return
    except OSError:
        return
    for i in range(_AUDIT_BACKUPS - 1, 0, -1):
        src = "%s.%d" % (paths.AUDIT_LOG, i)
        dst = "%s.%d" % (paths.AUDIT_LOG, i + 1)
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    try:
        os.replace(paths.AUDIT_LOG, paths.AUDIT_LOG + ".1")
    except OSError:
        pass


def _secret():
    try:
        from . import auth
        s = auth.cfg_get("secret_key")
    except Exception:
        s = None
    if not s:
        s = config_loader.get("server", "secret_key", "") or "dev-audit-secret"
    return s.encode("utf-8")


def _calc(prev, body_json):
    return _hmac.new(_secret(), (prev + body_json).encode("utf-8"),
                     hashlib.sha256).hexdigest()


def _get_prev():
    global _last
    if _last is not None:
        return _last
    prev = ""
    if getattr(db, "DB_OK", False):
        try:
            with db.conn() as c, c.cursor() as cur:
                cur.execute("SELECT hmac FROM disp_audit ORDER BY id DESC LIMIT 1")
                row = cur.fetchone()
                if row and row[0]:
                    prev = row[0]
        except Exception:
            pass
    else:
        # 冷启动读最后一条 hmac 接续链；若 audit.log 恰好刚滚动为空，回退读 .1 备份不断链
        last = None
        for path in (paths.AUDIT_LOG, paths.AUDIT_LOG + ".1"):
            try:
                cur_last = None
                with open(path, "r", encoding="utf-8") as f:
                    for cur_last in f:
                        pass
                if cur_last and cur_last.strip():
                    last = cur_last
                    break
            except Exception:
                continue
        if last and last.strip():
            try:
                prev = json.loads(last).get("hmac", "")
            except Exception:
                pass
    _last = prev
    return prev


def log(actor, peer, cap, detail, result):
    """写入一条审计并扣链。detail 可为 dict 或 str（需 json 可序列化）。"""
    global _last
    with _LOCK:
        ts = time.time()
        body = {"ts": ts, "actor": actor, "peer": peer, "cap": cap,
                "detail": detail, "result": result}
        # 规范 json（sort_keys）用于 hmac 计算，校验方须以同样方式重算
        body_json = json.dumps(body, ensure_ascii=False, sort_keys=True)
        prev = _get_prev()
        h = _calc(prev, body_json)

        if getattr(db, "DB_OK", False):
            try:
                with db.conn() as c, c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO disp_audit"
                        "(ts,actor,peer,cap,detail,result,prev_hmac,hmac) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                        (ts, str(actor), str(peer), str(cap),
                         json.dumps(detail, ensure_ascii=False), str(result), prev, h))
                _last = h
                return
            except Exception:
                pass

        line = dict(body)
        line["prev_hmac"] = prev
        line["hmac"] = h
        try:
            _rotate_if_needed()      # 写前按需滚动，超上限则腾新文件
            with open(paths.AUDIT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception:
            pass
        _last = h
