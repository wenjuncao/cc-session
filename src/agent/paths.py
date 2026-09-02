# -*- coding: utf-8 -*-
"""
paths.py —— frozen 感知路径工具（目录结构：src/ + config/ + data/ + static/）（2.0 单一配置 config.ini）

  app_root()     可写根：源码=项目根(src 的上一级)；打包=exe 同级目录
  state_root()   被控端状态根：源码=项目根；打包=机器级固定目录(%PROGRAMDATA%\\cc-session)
  bundle_root()  只读随包资源根：源码=项目根；打包=sys._MEIPASS
  app_dir()      兼容旧调用，等于 app_root()

目录：config/(config.ini)  data/(logs certs node.json)  static/(前端)
首启（打包场景）外部 config/config.ini 缺失时，从随包默认释放一份，保证用户可改。

身份稳定性：node.json/证书/config.ini 全部落 state_root()。打包态特意用机器级固定目录而非
exe 同级目录——否则同一台机器每装一个新版本/换一个安装目录就生成一个新 node_id，主控端会把
同一台机器认成多台节点（0.0.3~0.0.5 多张卡即由此而来）。
"""
import os
import shutil
import sys


def _frozen():
    return getattr(sys, "frozen", False)


def app_root():
    if _frozen():
        return os.path.dirname(sys.executable)
    # 源码模式：本文件在 <root>/src/agent/paths.py，向上三层才是 cc-session 项目根
    # （与 session_manager.app_dir() 的 <root> 对齐，被控端配置落 <root>/config/agent/）
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def state_root():
    """被控端本地状态根（node.json/证书/config.ini 落此）。
    源码模式=项目根，与 app_root() 一致；打包模式=机器级固定目录，与 exe 所在目录解耦，
    保证同机同一身份跨版本/跨安装目录稳定。环境异常时兜底退回 exe 目录（保持旧行为）。"""
    if not _frozen():
        return app_root()
    base = os.environ.get("PROGRAMDATA") or os.environ.get("ALLUSERSPROFILE")
    if base and os.path.isdir(base):
        return os.path.join(base, "cc-session")
    return app_root()


def bundle_root():
    if _frozen():
        return getattr(sys, "_MEIPASS", app_root())
    return app_root()


def app_dir():  # 兼容旧调用
    return app_root()


# ---- 目录 ----
CONFIG_DIR = os.path.join(state_root(), "config", "agent")  # 被控端本地状态目录，整体被 .gitignore 忽略
DATA_DIR = CONFIG_DIR  # node.json/certs/logs/users.json 全部落 config/agent/，与 cc-session 自身 config.json 隔离
LOGS_DIR = os.path.join(DATA_DIR, "logs")
CERTS_DIR = os.path.join(DATA_DIR, "certs")
STATIC_DIR = os.path.join(bundle_root(), "static")

# ---- 文件 ----
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.ini")
LOCAL_CONFIG_PATH = os.path.join(CONFIG_DIR, "config.local.ini")
NODE_JSON = os.path.join(DATA_DIR, "node.json")
AUDIT_LOG = os.path.join(LOGS_DIR, "audit.log")

for _d in (CONFIG_DIR, DATA_DIR, LOGS_DIR, CERTS_DIR):
    os.makedirs(_d, exist_ok=True)

# 打包首启：外部 config 缺失则从随包默认释放
_bundled_cfg = os.path.join(bundle_root(), "config", "config.ini")
if not os.path.exists(CONFIG_PATH) and os.path.exists(_bundled_cfg) \
        and os.path.abspath(_bundled_cfg) != os.path.abspath(CONFIG_PATH):
    try:
        shutil.copyfile(_bundled_cfg, CONFIG_PATH)
    except Exception:
        pass


def data_path(*parts):
    return os.path.join(DATA_DIR, *parts)


def resource_path(*parts):
    return os.path.join(bundle_root(), *parts)
