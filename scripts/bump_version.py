# -*- coding: utf-8 -*-
"""
打包前自增版本号：把 src/session_manager.py 里 APP_VERSION 的最后一段 +1，
写回源码并打印新版本号（供 打包.bat 捕获用于命名 exe）。

例： 1.2 -> 1.3 -> 1.4 …
单独运行： python bump_version.py
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src", "session_manager.py"))


def main():
    with open(SRC, encoding="utf-8", newline="") as f:
        text = f.read()

    m = re.search(r'^(APP_VERSION\s*=\s*")([^"]+)(")', text, re.M)
    if not m:
        sys.exit("ERROR: APP_VERSION not found in " + SRC)

    parts = m.group(2).split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)   # 末位 +1
    except ValueError:
        parts.append("1")                     # 末位非数字则追加 .1 兜底
    new = ".".join(parts)

    text = text[:m.start(2)] + new + text[m.end(2):]
    with open(SRC, "w", encoding="utf-8", newline="") as f:
        f.write(text)

    # 同步被控端版本号 agent/version.py 的 AGENT_VERSION，保证广播/服务描述与主程序一致
    ver_py = os.path.normpath(os.path.join(HERE, "..", "src", "agent", "version.py"))
    try:
        with open(ver_py, encoding="utf-8", newline="") as f:
            vt = f.read()
        vm = re.search(r'^(AGENT_VERSION\s*=\s*")([^"]+)(")', vt, re.M)
        if vm:
            vt = vt[:vm.start(2)] + new + vt[vm.end(2):]
            with open(ver_py, "w", encoding="utf-8", newline="") as f:
                f.write(vt)
    except FileNotFoundError:
        pass

    print(new)   # 仅输出新版本号，供 for /f 捕获


if __name__ == "__main__":
    main()
