# -*- coding: utf-8 -*-
"""
net_http.py —— 被控端节点发现相关 HTTP/REST 端点（精简版）

被控端无网页 UI、无登录 session，因此只保留 remote-disp 主控端“识别 + gossip”所必需的最小面：
  - POST /peers/register  收对端上报，合并后返回 {peers: 自身已知名单}（gossip）
  - GET  /ping            返回 {node_id, ts, peers}（gossip 增量 + 存活探测）
  - POST /peers/forget    对端转发来的移除请求：仅本地移除+打墓碑，不二次转发
  - GET  /healthz         本机单实例守卫探测（GUI 与服务判断被控端是否已在跑）

已裁掉主控/控制台专用路由（/api/discovery/*、/api/peers、/events/peers SSE）——
这些依赖登录 session，被控端不提供登录入口，保留亦不可用。
"""
import time

from flask import jsonify, request

from . import discovery
from .node_identity import NODE_ID


def register(app):
    """向被控端 Flask app 挂载发现端点。"""

    @app.route("/peers/register", methods=["POST"])
    def peers_register():
        data = request.get_json(silent=True) or {}
        discovery.ingest_peer(data)
        return jsonify({"peers": discovery.gossip_peers()})

    @app.route("/ping", methods=["GET"])
    def ping():
        return jsonify({
            "node_id": NODE_ID,
            "ts": time.time(),
            "peers": discovery.gossip_peers(),
        })

    @app.route("/peers/forget", methods=["POST"])
    def peers_forget():
        nid = (request.get_json(silent=True) or {}).get("node_id")
        return jsonify(discovery.forget_peer(nid, propagate=False))

    @app.route("/healthz", methods=["GET"])
    def healthz():
        return jsonify({"ok": True, "node_id": NODE_ID})
