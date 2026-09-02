# -*- coding: utf-8 -*-
"""
node_identity.py —— 节点身份：**按硬件算出的幂等设备编号** + 主机名。

历史问题：旧实现是首启生成随机 uuid4 存进 node.json。重新部署、清空 state_root、
或把 exe 换个目录跑，都会得到一个新 node_id —— 控制端名单里同一台机器会冒出
好几个节点条目。

现在 node_id 由 device_id.device_id(role) 从硬件算出：同一台机器无论重装多少次，
编号完全一样，控制端天然只看到一个节点。node.json 退化为「缓存 + 排查记录」，
不再是编号的来源。

cc-session 只做被控端，故角色恒为 "agent"（device_id 按角色加盐，与控制端区分开）。
node_name 固定取本机主机名，仅当管理员在 config.ini 显式写了 [node].node_name 才以配置为准。
"""
import json
import os
import socket
import time
import uuid

from . import device_id
from .paths import NODE_JSON
from .config_loader import get

ROLE = "agent"


def _machine_name():
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return "unknown"


def load_or_create():
    """返回 {node_id, node_name, role, hw, created_at}。

    node_id 每次启动都按硬件重算（幂等），不从 node.json 读 —— 那正是旧实现
    产生重复节点的根源。node.json 只用于记录首次生成时间与硬件指纹摘要，
    方便排查"为什么这台机器的编号变了"。
    """
    nid = device_id.device_id(ROLE, fallback=uuid.uuid4().hex)
    name = (get("node", "node_name") or "").strip() or _machine_name()
    hw = device_id.hardware_fingerprint()

    created = time.time()
    prev = None
    if os.path.exists(NODE_JSON):
        try:
            with open(NODE_JSON, "r", encoding="utf-8") as f:
                prev = json.load(f)
            created = prev.get("created_at") or created
        except Exception:
            prev = None

    data = {"node_id": nid, "node_name": name, "role": ROLE,
            "hw": hw, "created_at": created, "updated_at": time.time()}

    # 编号变化时留痕：硬件换了(换主板/换系统盘/重装系统)才可能发生，是排查名单异常的关键线索
    if prev and prev.get("node_id") and prev["node_id"] != nid:
        data["prev_node_id"] = prev["node_id"]
        data["prev_hw"] = prev.get("hw")

    # 原子写：先写临时文件再 os.replace，避免崩溃/断电写出半个文件
    try:
        tmp = NODE_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, NODE_JSON)
    except Exception:
        pass       # 记录失败不影响身份本身——编号是算出来的，不依赖这个文件
    return data


IDENTITY = load_or_create()
NODE_ID = IDENTITY["node_id"]
NODE_NAME = IDENTITY["node_name"]
