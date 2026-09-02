# -*- coding: utf-8 -*-
"""
version.py —— 被控端版本号（供 discovery 广播、服务描述使用）。

单一事实源是 session_manager.py 的 APP_VERSION；打包脚本 bump_version.py 会在自增版本时
同步写回此处，保证两者一致。此文件保持为纯字面量，避免在无界面服务进程中 import tkinter。
"""
AGENT_VERSION = "0.0.14"

# 产品标识：区分"同为 role=agent 但不是同一套程序"的异构节点（如 remote-agent）。
# P2P 自动更新只认 app==PRODUCT 的来源，避免把别的程序的 exe 拉来覆盖自己。
PRODUCT = "cc-session"
