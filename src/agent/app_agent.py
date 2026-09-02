# -*- coding: utf-8 -*-
"""
app_agent.py —— 被控端 Flask 应用与 wss 对端面。

只提供 remote-disp 主控端“识别 + 连接 + 文件/屏幕/进程”所需的最小面：
  - 基础设施：db.init → auth.ensure_seed → 访问密码守卫 → tls_config.ensure_cert → discovery.start
  - net_http.register：/peers/register、/ping、/peers/forget、/healthz（gossip + 存活探测）
  - transfer_service.register：/dl 下载旁路（HTTP GET，只认 dl.token）
  - updater.register：/update/download 自升级源（流式回传本机 exe，供 P2P 局域网自动更新）
  - flask-sock /ws：被别的节点连接时经 handshake.run 鉴权 → cap_dispatch.dispatch 执行能力
  - 导入 fs/screen/process/transfer 触发其 handler 注册：
      fs.list/tree/stat、screen.shot/stream.*、proc.list/kill、dl.token

**无** UI http 面、**无** static、**无** /ws/console、**无** 主控 REST——与 cc-session 的 tkinter 界面完全解耦。
"""
import json
import logging

from flask import Flask, request

from . import db, discovery, tls_config
from . import auth
from . import net_http
from . import updater
from . import cap_dispatch
from . import handshake
# 导入即注册能力 handler（fs.* / screen.* / proc.* / dl.token）到 cap_dispatch
from . import fs_service  # noqa: F401
from . import screen_service  # noqa: F401
from . import process_service  # noqa: F401  proc.list / proc.kill
from . import transfer_service  # noqa: F401  dl.token + /dl 下载旁路
from .config_loader import get, getint


def create_agent_app():
    """构建被控端 Flask app（含基础设施初始化与 /ws 挂载）。"""
    db.init()
    auth.ensure_seed()
    if not auth.has_access_password():
        raise SystemExit("未设置本机访问密码，拒绝启动。")
    tls_config.ensure_cert()
    discovery.start()

    app = Flask(__name__)
    app.secret_key = get("server", "secret_key") or auth.cfg_get("secret_key", "cc-session-agent-secret")

    net_http.register(app)
    transfer_service.register(app)   # /dl 下载路由
    updater.register(app)            # /update/download 自升级源（供更高版本以外的节点拉取本机 exe）

    from flask_sock import Sock
    sock = Sock(app)

    @sock.route("/ws")
    def ws_node(ws):
        ctx = handshake.run(ws, request.remote_addr)   # 被连时鉴权
        if not ctx:
            return
        send = lambda msg: ws.send(json.dumps(msg, ensure_ascii=False))
        while True:
            raw = ws.receive()
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            cap_dispatch.dispatch(ctx, msg, send)

    return app


def serve_peer(app):
    """对端面：wss（自签证书）供别的节点连接；无 TLS 则退回明文 http。阻塞常驻。"""
    host = get("server", "host", "0.0.0.0")
    port = getint("server", "port", 8860)
    ssl_ctx = None
    try:
        ssl_ctx = tls_config.ssl_context()
    except Exception as e:
        logging.warning("获取 TLS 上下文失败，对端面退回明文 HTTP：%s", e)
    from werkzeug.serving import run_simple
    logging.info("serve PEER %s on %s:%d", "wss" if ssl_ctx else "http", host, port)
    run_simple(host, port, app, threaded=True, use_reloader=False,
               use_debugger=False, ssl_context=ssl_ctx)
