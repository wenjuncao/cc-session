# -*- coding: utf-8 -*-
"""
cmd_exec.py —— 一次性命令执行（cmd/powershell），逐块流式回传。

对应 CONTRACT §2.2 的 cmd.exec：run(cmd, cwd, timeout, emit)。
  - shell=True 交给系统解释器执行；stdout/stderr 合并逐块读出，emit(seq, data, fin) 上行；
  - 编码 errors="replace" 兜底乱码；超时整树 taskkill /T /F。
策略为 [cmd].policy=open 全放开、不拦截，审计由调用方(cap_router)负责。
返回：正常退出返回 exit_code(int)；超时返回 None（由调用方回 TIMEOUT）。
"""
import subprocess
import sys
import threading

# 中文/各语言 Windows 的控制台程序(ipconfig/dir 等)输出用 OEM 代码页(zh-CN=cp936)，
# 按 utf-8 解码必然乱码。这里统一用 "oem" 编解码器（运行时映射到实际 OEM 代码页）。
_CONSOLE_ENC = "oem" if sys.platform == "win32" else "utf-8"


def _kill_tree(pid):
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=0x08000000)  # CREATE_NO_WINDOW：避免闪黑框
    except Exception:
        pass


def run(cmd, cwd=None, timeout=None, emit=None):
    """
    执行一次性命令。emit(seq, data, fin)：seq 从 0 递增，末帧 fin=True。
    """
    if emit is None:
        emit = lambda seq, data, fin: None
    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=cwd or None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding=_CONSOLE_ENC, errors="replace",
            creationflags=0x08000000,   # CREATE_NO_WINDOW：无控制台 exe 下不闪黑框
        )
    except Exception as e:
        emit(0, "启动命令失败：%s" % e, True)
        return -1

    seq_box = [0]

    def reader():
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            emit(seq_box[0], chunk, False)
            seq_box[0] += 1

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        proc.wait(timeout=timeout if timeout else None)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        t.join(timeout=5)
        emit(seq_box[0], "\n[命令超时，已整树终止]\n", True)
        return None
    t.join(timeout=5)
    emit(seq_box[0], "", True)   # 末帧：无数据、fin=True
    return proc.returncode
