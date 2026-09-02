# -*- coding: utf-8 -*-
"""打印 src/session_manager.py 里的 APP_VERSION（唯一事实源），供打包脚本命名安装包用。

用正则读取而非 import：session_manager 会拉起 tkinter，且模块级会解析路径，
仅为取一个版本号没必要付这份代价；正则读一行更快也更稳。
"""
import io
import os
import re

_here = os.path.dirname(os.path.abspath(__file__))
_sm = os.path.join(_here, "..", "src", "session_manager.py")
_src = io.open(_sm, encoding="utf-8").read()
_m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', _src, re.M)
if not _m:
    raise SystemExit("无法从 src/session_manager.py 读到 APP_VERSION")
print(_m.group(1))
