# -*- coding: utf-8 -*-
"""
device_id.py —— 基于硬件信息的幂等设备编号（共用模块）

解决的问题：原来 node_id 是首启随机 uuid4 存进 data/node.json，一旦重新部署、
清空 data\\、或把 exe 换个目录跑，就会生成一个新的 node_id —— 控制端名单里
同一台机器于是冒出好几个节点条目。

本模块改为**从硬件算**：同一台机器无论重装多少次、data\\ 清空多少回，
只要硬件没变，算出来的编号就完全一样（幂等）。

采集三项硬件标识，任一取不到就跳过（不影响其余项）：
  1. MachineGuid   —— 注册表 HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid，
                      纯注册表读取，微秒级；重装系统才会变。
  2. 系统盘卷序列号 —— ctypes 调 GetVolumeInformationW，微秒级；重新格式化才会变。
  3. 主板 UUID     —— wmic csproduct get uuid，约 0.3~1s；**换主板才会变**，
                      是三项里最贴近"硬件"的，也能区分开 MachineGuid 相同的克隆虚拟机。

【为什么要按角色加盐】
  控制端与被控端可能装在同一台机器上（开发调试、单机自用）。硬件指纹相同，
  不加盐两者会算出同一个 node_id，发现层会把对方当成自己，名单和连接全乱。
  故最终编号 = sha256(硬件指纹 + '|' + 角色)[:32]，同机两端互不相同，
  而同一角色在同一硬件上恒定。

输出格式与旧的 uuid4().hex 一致（32 位小写十六进制），协议、指纹库、
已存凭据的键格式都不用改。
"""
import hashlib
import logging
import os
import subprocess
import sys

log = logging.getLogger("remote-disp.device_id")

CREATE_NO_WINDOW = 0x08000000   # windowed 打包下不弹黑框


def _machine_guid():
    """注册表 MachineGuid：装机时生成，重装系统才变。纯读取，极快。"""
    try:
        import winreg
        # 64 位系统上该键在 64 位视图下，显式带 KEY_WOW64_64KEY 避免被重定向到 WOW6432Node
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return (winreg.QueryValueEx(k, "MachineGuid")[0] or "").strip()
    except Exception:
        return ""


def _volume_serial():
    """系统盘卷序列号：重新格式化才变。ctypes 直调，无子进程。"""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        root = (os.environ.get("SystemDrive") or "C:") + "\\"
        serial = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), None, 0,
            ctypes.byref(serial), None, None, None, 0)
        return "%08X" % serial.value if ok else ""
    except Exception:
        return ""


def _board_uuid():
    """主板 UUID：换主板才变，也能区分 MachineGuid 相同的克隆虚拟机。

    wmic 有几百毫秒开销，且部分新系统已移除，故失败静默跳过 —— 另两项仍足以定位设备。
    全 0 / 全 F 是厂商没写 UUID 的常见占位值，视为无效。
    """
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(["wmic", "csproduct", "get", "uuid"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=CREATE_NO_WINDOW)
        for line in (out.stdout or "").splitlines():
            v = line.strip()
            if not v or v.lower() == "uuid":
                continue
            bare = v.replace("-", "").replace("0", "").replace("F", "").replace("f", "")
            return "" if not bare else v      # 全 0/F 占位值判无效
    except Exception:
        pass
    return ""


def hardware_fingerprint():
    """拼出本机硬件指纹原文（供计算与排查用）。至少要有一项，否则返回空串。"""
    parts = [("guid", _machine_guid()),
             ("vol", _volume_serial()),
             ("board", _board_uuid())]
    got = ["%s=%s" % (k, v) for k, v in parts if v]
    return "|".join(got)


def device_id(role, fallback=""):
    """按硬件 + 角色算出幂等设备编号（32 位小写十六进制）。

    role     : 'agent' | 'controller'，同机两端据此区分。
    fallback : 硬件信息一项都采不到时的兜底种子（调用方传随机 uuid，
               退化为旧的随机身份语义，至少不会崩）。
    """
    fp = hardware_fingerprint()
    if not fp:
        log.warning("[设备编号] 未能采集到任何硬件标识，退化为随机身份（重装后会变）")
        fp = "fallback=" + (fallback or os.urandom(16).hex())
    raw = "%s|role=%s" % (fp, role)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
