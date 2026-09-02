# -*- coding: utf-8 -*-
"""
start.py —— 被控端启动入口。

  start_background()  GUI 双击时调用：单实例守卫（对端端口已被占用则不重复起），
                      否则起 daemon 线程常驻 wss(0.0.0.0:8860)+discovery，异常一律吞掉不拖垮界面。
  run_silent()        无界面前台常驻（--agent-silent / 系统服务拉起用）：直接阻塞 serve_peer。
"""
import logging
import socket
import threading

from .config_loader import getint


def _peer_running():
    """对端端口已在监听 → 认为被控端已在运行（服务或另一实例），本进程不再重复起。"""
    port = getint("server", "port", 8860)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except Exception:
        return False


def run_silent():
    """无界面前台常驻：构建 app 并阻塞在 wss 对端面。
    构建前先做单实例守卫——服务与 --agent-silent 都走这里，若本机已有被控端在监听对端端口，
    立即退出，避免第二个实例各自 discovery.start() 广播（discovery 早于 8860 绑定，不守卫会重复广播）。"""
    if _peer_running():
        logging.info("被控端已在运行（对端端口占用），run_silent 退出不重复启动")
        return
    from .app_agent import create_agent_app, serve_peer
    app = create_agent_app()
    from . import memtrim
    memtrim.start()          # 内存看门狗：空闲回收 + 超软限额强制回收（覆盖 GUI 后台线程/--agent-silent/服务 worker）
    serve_peer(app)


def start_background():
    """GUI 进程内后台起被控端；已在运行则跳过。返回是否本次真正启动。"""
    if _peer_running():
        logging.info("被控端已在运行（对端端口占用），跳过后台启动")
        return False

    def _run():
        try:
            run_silent()
        except Exception as e:
            logging.warning("被控端后台启动失败：%s", e)

    threading.Thread(target=_run, name="agent-peer", daemon=True).start()
    return True
