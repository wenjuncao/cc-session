# -*- coding: utf-8 -*-
"""
config_loader.py —— 单一配置文件 config.ini（2.0 起不再用 config.local.ini）

用法：
    from .config_loader import CONF, save_local, get, getbool, getint
    port = getint("server", "port", 8860)

说明：
  - 运行期改动（密码哈希、密钥、节点名、端口、bootstrap 等）一律经 save_local 写回 config.ini。
  - 首次加载时若发现旧版 config.local.ini，自动把其内容并入 config.ini 后删除（迁移，一次性）。
  - 注意：configparser 写回会丢弃注释，故 config.ini 一旦被程序写过，默认注释会消失（属正常）。
"""
import configparser
import os

from .paths import CONFIG_PATH, LOCAL_CONFIG_PATH


def _migrate_local():
    """一次性迁移：把旧 config.local.ini 内容并入 config.ini 后删除；跳过已废弃的 [frp]/[update]。"""
    if not os.path.exists(LOCAL_CONFIG_PATH):
        return
    try:
        base = configparser.ConfigParser()
        base.read(CONFIG_PATH, encoding="utf-8-sig")
        loc = configparser.ConfigParser()
        loc.read(LOCAL_CONFIG_PATH, encoding="utf-8-sig")
        for s in loc.sections():
            if s in ("frp", "update"):      # 2.0 已移除 frp 及其自更新开关，不迁移
                continue
            if not base.has_section(s):
                base.add_section(s)
            for k, v in loc.items(s):
                base.set(s, k, v)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            base.write(f)
        os.remove(LOCAL_CONFIG_PATH)
    except Exception:
        pass


_migrate_local()

CONF = configparser.ConfigParser()
# -sig 容忍记事本 BOM
CONF.read(CONFIG_PATH, encoding="utf-8-sig")


def get(section, key, fallback=""):
    return CONF.get(section, key, fallback=fallback)


def getbool(section, key, fallback=False):
    try:
        return CONF.getboolean(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
        return fallback


def getint(section, key, fallback=0):
    try:
        return CONF.getint(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
        return fallback


def save_local(section, key, value):
    """把改动写入单一 config.ini（并同步内存 CONF）。"""
    if not CONF.has_section(section):
        CONF.add_section(section)
    CONF.set(section, key, str(value))
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        CONF.write(f)
