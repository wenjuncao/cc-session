# -*- coding: utf-8 -*-
"""
db.py —— 可选 MySQL 持久化（PooledDB）。连不上则降级：DB_OK=False，调用方回退本地文件。
沿用 auto-task/webconsole 的"DB 挂了不影响主流程"原则。
"""
import threading

from .config_loader import get, getint

POOL = None
DB_OK = False
_LOCK = threading.Lock()

DDL = [
    """CREATE TABLE IF NOT EXISTS disp_config (
        cfg_key   VARCHAR(64) PRIMARY KEY,
        cfg_value TEXT
    ) DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS disp_audit (
        id        BIGINT AUTO_INCREMENT PRIMARY KEY,
        ts        DOUBLE,
        actor     VARCHAR(128),
        peer      VARCHAR(128),
        cap       VARCHAR(32),
        detail    MEDIUMTEXT,
        result    VARCHAR(16),
        prev_hmac CHAR(64),
        hmac      CHAR(64),
        INDEX(ts)
    ) DEFAULT CHARSET=utf8mb4""",
]


def init():
    """尝试建池建表；失败静默降级。返回 DB_OK。"""
    global POOL, DB_OK
    host = get("db", "host")
    if not host:
        return False
    try:
        import pymysql
        from dbutils.pooled_db import PooledDB
        POOL = PooledDB(
            creator=pymysql, maxconnections=10, blocking=True,
            host=host, port=getint("db", "port", 3306),
            user=get("db", "user"), password=get("db", "password"),
            database=get("db", "database", "remote_disp"),
            charset="utf8mb4", autocommit=True,
        )
        with POOL.connection() as conn, conn.cursor() as cur:
            for ddl in DDL:
                cur.execute(ddl)
        DB_OK = True
    except Exception:
        DB_OK = False
    return DB_OK


def conn():
    return POOL.connection()
