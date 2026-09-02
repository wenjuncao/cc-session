# -*- coding: utf-8 -*-
"""
path_guard.py —— 全项目路径安全强制入口（T6）

职责：resolve(path) 把外来路径规范化成「已校验的绝对路径」，任何 fs.*/dl.*/proc.start
入口都必须先过本函数。校验内容：
  - 拒绝空路径 / 非绝对路径 / 含 NUL 字节
  - 拒绝 UNC 共享（\\host\share、//host/share）
  - 拒绝备用数据流 ADS（盘符之外再出现 ':'，如 C:\\foo:stream、::$DATA）
  - 拒绝 Windows 设备名（CON/NUL/PRN/AUX/COMx/LPTx…）
  - os.path.realpath 解析 junction/symlink 后，再校验落在 [capability].allow_roots 白名单内
    （白名单为空 = 允许所有盘符，但仍做上述规范化与危险模式拒绝）
  - junction 逃逸：realpath 会把 junction 指到真实目标，再查白名单即可挡住越界跳板

越界一律 raise PathDenied（自定义异常）。
"""
import os

from .config_loader import get


class PathDenied(Exception):
    """路径穿越 / 越白名单 / 危险路径，被安全网关拒绝。"""
    pass


# Windows 保留设备名（不区分大小写，忽略扩展名）
_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def allow_roots():
    """读取白名单根目录列表；空则返回 []（= 允许所有盘符）。供 fs 根视图收窄可见范围。"""
    raw = get("capability", "allow_roots", "") or ""
    roots = []
    for part in raw.split(","):
        p = part.strip().strip('"')
        if p:
            roots.append(p)
    return roots


def _allow_roots():
    """内部兼容别名。"""
    return allow_roots()


def _within_any(real, roots):
    """real 是否位于任一白名单根之内（含根本身）。"""
    rl = os.path.normcase(real)
    for r in roots:
        rr = os.path.normcase(os.path.realpath(r))
        try:
            if os.path.commonpath([rl, rr]) == rr:
                return True
        except ValueError:
            # 不同盘符 → commonpath 抛错，视为不在此根内
            continue
    return False


def resolve(path):
    """规范化 + 白名单 jail 校验，返回真实绝对路径；越界 raise PathDenied。"""
    if not path or not isinstance(path, str):
        raise PathDenied("空路径或非法路径类型")
    p = path.strip().strip('"')
    if "\x00" in p:
        raise PathDenied("路径含 NUL 字节")

    # UNC 共享直接拒绝
    if p.startswith("\\\\") or p.startswith("//"):
        raise PathDenied("拒绝 UNC 共享路径")

    norm = os.path.normpath(p)
    if not os.path.isabs(norm):
        raise PathDenied("必须为带盘符的绝对路径")

    drive, rest = os.path.splitdrive(norm)
    # 盘符之外再出现 ':' 即为备用数据流（ADS）
    if ":" in rest:
        raise PathDenied("拒绝备用数据流(ADS)")

    # 逐段检查设备名
    for seg in rest.replace("/", "\\").split("\\"):
        if not seg:
            continue
        base = seg.split(".", 1)[0].strip().upper()
        if base in _RESERVED:
            raise PathDenied(f"拒绝设备名: {seg}")

    # 解析 junction/symlink 到真实目标
    real = os.path.realpath(norm)
    # 解析后若指向 UNC，同样拒绝
    if real.startswith("\\\\") or real.startswith("//"):
        raise PathDenied("解析后指向 UNC 共享")

    roots = _allow_roots()
    if roots and not _within_any(real, roots):
        raise PathDenied("路径越出 allow_roots 白名单")

    return real
