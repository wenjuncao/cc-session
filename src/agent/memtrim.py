# -*- coding: utf-8 -*-
"""
memtrim.py —— 内存看门狗：空闲期回收 + 超限强制回收

背景：本程序绝大部分时间是"挂着等被控"，真正干活（截图/终端/列进程）只是偶发。
Python 为某次任务申请过的内存，即使对象已释放也仍留在进程工作集里不还给系统，
任务管理器上就一直显示几十 MB —— 被控端跑过一轮远程桌面后尤其明显。

实测基线（1920p 超清 25fps 连拍 + 列 500 进程 + 列 200 文件）：
    装配后空闲 38.9 MB → 被控峰值 62.3 MB → 回收后 7.5 MB

两条策略，每 CHECK_INTERVAL 秒巡检一次：
  1. 空闲回收：确实空闲(见 _busy)且距上次回收满 IDLE_INTERVAL 秒 → gc + 修剪工作集。
  2. 超限回收：常驻超过 SOFT_LIMIT_MB → 即便正在被控也强制回收，并记 WARNING。
     这是【软限额】不是硬上限：不用 Job Object 之类的硬限制，因为那会让超限时的内存
     申请直接失败、把进程打崩在半路（正传大文件/正截图时崩尤其糟）。软限额只回收+告警，
     功能永不因限额中断。

"正在被操控"的判定必须靠 cap_dispatch.LAST_ACTIVITY：远程桌面走 screen.shot 请求/响应
不建流，被控端也没有出站连接，只看流数/连接数会把连拍误判成空闲、在操控中途修剪工作集。
"""
import ctypes
import gc
import logging
import sys
import threading
import time

log = logging.getLogger("remote-disp.memtrim")

CHECK_INTERVAL = 15         # 巡检间隔（秒）
IDLE_INTERVAL = 60          # 两次空闲回收的最小间隔（秒）
ACTIVE_QUIET = 60           # 最后一次业务调用后静默满这么久才算空闲（秒）
SOFT_LIMIT_MB = 200         # 软限额：常驻超过它就强制回收并告警
_STARTUP_DELAY = 20         # 启动后延迟首次回收，等各模块惰性导入稳定

_k32 = ctypes.windll.kernel32 if sys.platform == "win32" else None


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def rss_mb():
    """当前常驻内存(MB)。用 ctypes 直读 psapi，避免为了看个数字把 psutil 拉进常驻内存。"""
    if _k32 is None:
        return 0.0
    try:
        c = _PROCESS_MEMORY_COUNTERS()
        c.cb = ctypes.sizeof(c)
        _k32.GetCurrentProcess.restype = ctypes.c_void_p
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.c_void_p(_k32.GetCurrentProcess()), ctypes.byref(c), c.cb):
            return 0.0
        return c.WorkingSetSize / 1024.0 / 1024.0
    except Exception:
        return 0.0


def _trim_working_set():
    """把本进程工作集交还系统（仅 Windows）。"""
    if _k32 is None:
        return False
    try:
        # 两个 -1 = 让系统尽可能修剪。SIZE_T 在 64 位下是 8 字节，必须显式声明签名，
        # 否则按 c_int 截断、传进去的不是 -1 → 调用失败（与 lockservice 同一个坑）。
        _k32.SetProcessWorkingSetSize.argtypes = [ctypes.c_void_p,
                                                  ctypes.c_size_t, ctypes.c_size_t]
        _k32.SetProcessWorkingSetSize.restype = ctypes.c_int
        _k32.GetCurrentProcess.restype = ctypes.c_void_p
        return bool(_k32.SetProcessWorkingSetSize(
            ctypes.c_void_p(_k32.GetCurrentProcess()),
            ctypes.c_size_t(-1), ctypes.c_size_t(-1)))
    except Exception:
        return False


def _busy():
    """是否正在干活。两个信号任一为真即算忙，不做回收。

    【必须用 sys.modules 查，不能 import】本模块也跑在 --service 服务进程里，
    那个进程只轮询会话、压根没加载业务模块。若这里写 import，每 15 秒巡检一次就会把
    cap_dispatch/terminal_session/claude_driver/discovery 整条依赖链拖进服务进程
    ——与"压缩内存"的目标正好相反。模块没被加载 ⇒ 该子系统根本不存在 ⇒ 它不可能忙，直接跳过即可。

    按 __package__ 拼出本包内的模块键（cc-session 以 `agent.` 前缀加载，
    模块名为 agent.cap_dispatch / agent.stream_hub），故不能写死裸名。"""
    now = time.time()
    pkg = __package__ or ""
    m = sys.modules.get(pkg + ".cap_dispatch")
    if m is not None and now - getattr(m, "LAST_ACTIVITY", 0.0) < ACTIVE_QUIET:
        return True              # 刚有过业务调用（含远程桌面连拍）——最关键的一条
    m = sys.modules.get(pkg + ".stream_hub")
    if m is not None:
        try:
            if m.HUB.channel_count() > 0:
                return True      # 有终端/命令/屏幕流在跑
        except Exception:
            pass
    return False


def _trim_once():
    gc.collect()
    _trim_working_set()


def _loop():
    time.sleep(_STARTUP_DELAY)
    _trim_once()                       # 启动完成后先压一次，把导入期的临时内存还掉
    last_trim = time.time()
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            cur = rss_mb()
            if cur > SOFT_LIMIT_MB:
                # 超软限额：即便正在被控也回收，并告警（正常负载下不该触发，触发即有异常堆积）
                _trim_once()
                log.warning("[内存] 常驻 %.0f MB 超过软限额 %d MB，已强制回收 -> %.0f MB",
                            cur, SOFT_LIMIT_MB, rss_mb())
                last_trim = time.time()
            elif not _busy() and time.time() - last_trim >= IDLE_INTERVAL:
                _trim_once()
                last_trim = time.time()
        except Exception:
            pass


def start():
    """启动内存看门狗线程（daemon）。"""
    threading.Thread(target=_loop, name="memtrim", daemon=True).start()
