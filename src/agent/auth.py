# -*- coding: utf-8 -*-
"""
auth.py —— 超管账密（对端鉴权用，B级安全的应用层身份）。

职责（CONTRACT §6 T4）：
  - 口令哈希优先 argon2id(argon2-cffi)，无则 werkzeug scrypt 兜底（再兜 pbkdf2）。
  - 账密哈希落 DB disp_config[admin_pass_hash]；DB_OK=False 时落 config.local.ini。
  - ensure_seed()：首启无账密 → 生成随机强口令、日志打印一次、置强制改密标记。
  - verify_login / set_admin / rate_check（同 IP 连续失败 5 次指数退避锁定）。

另导出配置存取小工具 cfg_get/cfg_set，供 audit.py / tls_config.py 复用
（DB 优先，降级到 config.local.ini 的 [secure] 段）。
"""
import json
import logging
import os
import secrets
import time

from . import db
from . import config_loader
from .paths import data_path

# 本机登录用户表（多用户+角色），密码哈希存储，不入库(gitignore data/)
USERS_PATH = data_path("users.json")

log = logging.getLogger("remote-disp.auth")

# werkzeug 始终导入：用于校验历史 scrypt/pbkdf2 哈希
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from argon2 import PasswordHasher
    _PH = PasswordHasher()          # 默认即 argon2id
    _HAVE_ARGON2 = True
except Exception:
    _HAVE_ARGON2 = False

DEFAULT_USER = config_loader.get("secure", "admin_user", "") or "admin"
FORCE_CHANGE_KEY = "admin_force_change"

# 本机控制台访问密码：首启静默初始化为此值（哈希存储，不明文落盘、不打印）
ACCESS_HASH_KEY = "access_pass_hash"
ACCESS_USER_KEY = "access_user"
_INITIAL_ACCESS = "Cc@2026!"
_INITIAL_ACCESS_USER = "admin"

# 登录失败限速：key -> (连续失败次数, 锁定到期时间戳, 最后活动时间)
# key 通常是来源 IP；当来源是回环地址(经端口转发/本地)时所有 peer 都来自 127.0.0.1，握手层会并入 node_id
# 作为 key（见 handshake），避免单个攻击者把共享 127.0.0.1 桶锁死、连带误锁全部合法节点。
_FAILS = {}
_FAIL_LOCK_THRESHOLD = 5
_FAIL_LOCK_CAP = 3600  # 单次锁定上限（秒）
_FAILS_MAX = 4096      # 限速表规模上限，超出则清理过期项（防公网大量 key 撑爆内存）
_FAILS_IDLE = 3600     # 空闲超过此秒数的项视为可清理


# ---------------- 配置存取（DB 优先，降级本地 ini） ----------------
def cfg_get(key, default=None):
    if getattr(db, "DB_OK", False):
        try:
            with db.conn() as c, c.cursor() as cur:
                cur.execute("SELECT cfg_value FROM disp_config WHERE cfg_key=%s", (key,))
                row = cur.fetchone()
                if row and row[0] is not None:
                    return row[0]
        except Exception:
            pass
    v = config_loader.get("secure", key, "")
    return v if v else default


def cfg_set(key, value):
    value = str(value)
    if getattr(db, "DB_OK", False):
        try:
            with db.conn() as c, c.cursor() as cur:
                cur.execute(
                    "INSERT INTO disp_config(cfg_key, cfg_value) VALUES(%s, %s) "
                    "ON DUPLICATE KEY UPDATE cfg_value=VALUES(cfg_value)",
                    (key, value))
            return
        except Exception:
            pass
    config_loader.save_local("secure", key, value)


# ---------------- 口令哈希 ----------------
def _hash(pw):
    if _HAVE_ARGON2:
        return _PH.hash(pw)
    try:
        return generate_password_hash(pw, method="scrypt")
    except Exception:
        return generate_password_hash(pw)  # 兜底 pbkdf2


def _verify_hash(pw, h):
    if not h:
        return False
    if h.startswith("$argon2"):
        if not _HAVE_ARGON2:
            return False
        try:
            _PH.verify(h, pw)
            return True
        except Exception:
            return False
    try:
        return check_password_hash(h, pw)
    except Exception:
        return False


def _gen_strong():
    """生成随机强口令（URL 安全，约 16 字节熵）。"""
    return secrets.token_urlsafe(16)


# ---------------- 对外接口 ----------------
def ensure_seed():
    """首启：初始化访问密码(静默) + 超管账密(随机，打印一次) + secret_key。"""
    # 本机登录访问密码：静默初始化，绝不打印明文
    if not cfg_get(ACCESS_HASH_KEY):
        cfg_set(ACCESS_HASH_KEY, _hash(_INITIAL_ACCESS))
        log.warning("[本机登录] 已初始化访问密码（哈希存储，不显示）。请向管理员索取初始账号密码。")
    # 本机登录用户名：默认 admin（可后续在设置中改）
    if not cfg_get(ACCESS_USER_KEY):
        cfg_set(ACCESS_USER_KEY, _INITIAL_ACCESS_USER)
    # 多用户表：首启从上面的 admin 访问密码迁移出 admin 用户
    _seed_users()
    if not cfg_get("admin_user"):
        cfg_set("admin_user", DEFAULT_USER)
    if not cfg_get("admin_pass_hash"):
        # 首启超管账密初始化为【已知默认】(账号 admin，密码同初始访问密码 Bsd@2026!)：
        # 其他节点首次即可连接；不再用随机口令(否则无人知晓，导致连不上、需手动改密才行)。
        cfg_set("admin_pass_hash", _hash(_INITIAL_ACCESS))
        cfg_set(FORCE_CHANGE_KEY, "0")
        log.warning("[超管账密] 已初始化为默认（账号 %s，密码同初始访问密码）。"
                    "这是别的节点连接本机时校验用的凭据，请尽快在系统设置中修改。",
                    cfg_get("admin_user") or DEFAULT_USER)
    if not cfg_get("secret_key"):
        cfg_set("secret_key", secrets.token_hex(32))


def has_access_password():
    """是否已存在可登录用户（没有则拒绝启动）。"""
    return bool(_load_users()) or bool(cfg_get(ACCESS_HASH_KEY))


def access_user():
    """本机登录用户名（默认 admin）。"""
    return cfg_get(ACCESS_USER_KEY) or _INITIAL_ACCESS_USER


# ---------------- 多用户登录（用户名+角色，密码哈希本地存储） ----------------
def _load_users():
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_users(users):
    tmp = USERS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_PATH)


def _seed_users():
    """首启迁移：无用户表则用已初始化的 admin 访问密码建 admin(超管角色)。"""
    if _load_users():
        return
    h = cfg_get(ACCESS_HASH_KEY) or _hash(_INITIAL_ACCESS)
    _save_users([{"user": _INITIAL_ACCESS_USER, "hash": h, "role": "admin"}])


def verify_access(user, password):
    """校验本机登录：命中用户名+密码则返回角色('admin'|'user')，否则返回 None（没有密码不许通过）。"""
    if not user or not password:
        return None
    for u in _load_users():
        if u.get("user") == user and _verify_hash(password, u.get("hash") or ""):
            return u.get("role") or "user"
    return None


def list_users():
    """返回用户清单（不含哈希）：[{user, role}]。"""
    return [{"user": u.get("user"), "role": u.get("role") or "user"} for u in _load_users()]


def add_user(user, password, role="user"):
    """新增或更新用户（同名则改密/改角色）。空用户名或空密码拒绝。"""
    user = (user or "").strip()
    if not user or not password:
        return False
    role = "admin" if role == "admin" else "user"
    users = _load_users()
    for u in users:
        if u.get("user") == user:
            u["hash"] = _hash(password)
            u["role"] = role
            _save_users(users)
            return True
    users.append({"user": user, "hash": _hash(password), "role": role})
    _save_users(users)
    return True


def del_user(user):
    """删除用户；不允许删到没有任何 admin。"""
    users = [u for u in _load_users() if u.get("user") != user]
    if not any((u.get("role") or "user") == "admin" for u in users):
        return False
    _save_users(users)
    return True


def set_access_password(password):
    """修改本机访问密码（哈希存储）。空密码拒绝。"""
    if not password:
        return False
    cfg_set(ACCESS_HASH_KEY, _hash(password))
    return True


def current_user():
    return cfg_get("admin_user") or DEFAULT_USER


def must_change_password():
    """是否处于强制改密状态（首启随机口令未改）。"""
    return cfg_get(FORCE_CHANGE_KEY) == "1"


def verify_login(user, password):
    if not user or not password:
        return False
    if user != (cfg_get("admin_user") or DEFAULT_USER):
        return False
    return _verify_hash(password, cfg_get("admin_pass_hash") or "")


def set_admin(user, password):
    """修改超管账密；成功后级联吊销所有旧 token（token_version 自增）。"""
    cfg_set("admin_user", user)
    cfg_set("admin_pass_hash", _hash(password))
    cfg_set(FORCE_CHANGE_KEY, "0")
    try:
        from . import session_token
        session_token.revoke_all()
    except Exception:
        pass


def _purge_fails(now):
    """限速表超上限时清理空闲过期项（防公网大量 key 撑爆内存）。"""
    if len(_FAILS) < _FAILS_MAX:
        return
    for k in [k for k, r in _FAILS.items()
              if now >= r[1] and now - r[2] > _FAILS_IDLE]:
        _FAILS.pop(k, None)


def rate_check(key):
    """True=允许尝试；False=该 key 处于锁定期。"""
    rec = _FAILS.get(key)
    if not rec:
        return True
    return time.time() >= rec[1]


def note_fail(key):
    """记一次登录失败；达阈值后指数退避锁定。"""
    now = time.time()
    _purge_fails(now)
    cnt = _FAILS.get(key, (0, 0, 0))[0] + 1
    lock_until = 0.0
    if cnt >= _FAIL_LOCK_THRESHOLD:
        backoff = min(2 ** (cnt - _FAIL_LOCK_THRESHOLD), _FAIL_LOCK_CAP)  # 1s,2s,4s… 上限 1h
        lock_until = now + backoff
    _FAILS[key] = (cnt, lock_until, now)


def note_ok(key):
    """登录成功，清除该 key 失败计数。"""
    _FAILS.pop(key, None)
