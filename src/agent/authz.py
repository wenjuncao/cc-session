# -*- coding: utf-8 -*-
"""
authz.py —— 能力位授权（当前策略：cmd 全放开，所有 cap 一律授予）。

职责（CONTRACT §6 T4）：
  - ALL_CAPS：全量能力名列表（授权成功即授予全部）。
  - require(ctx, cap) -> bool：校验 ctx['caps'] 是否含该 cap。
"""
from .protocol import Cap

# 全放开：枚举 protocol.Cap 里的全部能力名（含 stream 下行名，无害）。
ALL_CAPS = sorted({
    v for k, v in vars(Cap).items()
    if not k.startswith("_") and isinstance(v, str)
})


def require(ctx, cap):
    if not ctx:
        return False
    return cap in ctx.get("caps", [])
