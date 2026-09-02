# -*- coding: utf-8 -*-
"""
lockservice.py —— 主系统服务：开机以 SYSTEM 启动，在用户交互会话拉起本程序。

2.0 起这是本程序【开机启动的 Windows 系统服务】。原理：SYSTEM 服务开机自启，轮询活动控制台
会话，用【令牌复制+改会话ID+CreateProcessAsUser】把 `remote-disp.exe --silent` 拉起在该会话的
winsta0\\default——这样跑在用户桌面里，截屏/键鼠控制可用；配合 screen_service._use_input_desktop()
动态跟随输入桌面，连锁屏/UAC 安全桌面(仅 SYSTEM 可访问)也能截图/控制。

纯 ctypes(不引 pywin32)。**64 位关键**：所有句柄相关 API 必须显式声明 restype/argtypes，
否则句柄按 c_int 截断 → 调用失败(存活检测误判→反复重启)。仅经 CLI --service/--install-service/
--uninstall-service 触发，不影响普通模式。安装/卸载需管理员(UAC)。
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

SERVICE_NAME = "CCSessionAgent"
SERVICE_DISPLAY = "CC Session Agent"

# ---- 常量 ----
SERVICE_WIN32_OWN_PROCESS = 0x00000010
SERVICE_RUNNING = 0x00000004
SERVICE_STOPPED = 0x00000001
SERVICE_START_PENDING = 0x00000002
SERVICE_STOP_PENDING = 0x00000003
SERVICE_CONTROL_STOP = 0x00000001
SERVICE_CONTROL_SHUTDOWN = 0x00000005
SERVICE_ACCEPT_STOP = 0x00000001
SERVICE_ACCEPT_SHUTDOWN = 0x00000004

TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_ADJUST_SESSIONID = 0x0100
MAXIMUM_ALLOWED = 0x02000000
SecurityIdentification = 2
TokenPrimary = 1
TokenSessionId = 12
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_TIMEOUT = 0x00000102


# ---- 结构体 ----
class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [("dwServiceType", wintypes.DWORD),
                ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD),
                ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD),
                ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD)]


class STARTUPINFO(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


# ---- DLL + 显式签名（64 位必需） ----
_k32 = ctypes.windll.kernel32
_adv = ctypes.windll.advapi32
_userenv = ctypes.windll.userenv
H = wintypes.HANDLE
LPDW = ctypes.POINTER(wintypes.DWORD)

_k32.GetCurrentProcess.restype = H
_k32.GetCurrentProcess.argtypes = []
_k32.OpenProcess.restype = H
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [H]
_k32.GetExitCodeProcess.restype = wintypes.BOOL
_k32.GetExitCodeProcess.argtypes = [H, LPDW]
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.WaitForSingleObject.argtypes = [H, wintypes.DWORD]
_k32.TerminateProcess.restype = wintypes.BOOL
_k32.TerminateProcess.argtypes = [H, wintypes.UINT]
_k32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
_k32.WTSGetActiveConsoleSessionId.argtypes = []

_adv.OpenProcessToken.restype = wintypes.BOOL
_adv.OpenProcessToken.argtypes = [H, wintypes.DWORD, ctypes.POINTER(H)]
_adv.DuplicateTokenEx.restype = wintypes.BOOL
_adv.DuplicateTokenEx.argtypes = [H, wintypes.DWORD, ctypes.c_void_p,
                                  ctypes.c_int, ctypes.c_int, ctypes.POINTER(H)]
_adv.SetTokenInformation.restype = wintypes.BOOL
_adv.SetTokenInformation.argtypes = [H, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_adv.CreateProcessAsUserW.restype = wintypes.BOOL
_adv.CreateProcessAsUserW.argtypes = [H, wintypes.LPCWSTR, wintypes.LPWSTR,
                                      ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
                                      wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                      ctypes.POINTER(STARTUPINFO),
                                      ctypes.POINTER(PROCESS_INFORMATION)]
_adv.SetServiceStatus.restype = wintypes.BOOL
_adv.SetServiceStatus.argtypes = [H, ctypes.POINTER(SERVICE_STATUS)]
_adv.RegisterServiceCtrlHandlerExW.restype = H

_userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
_userenv.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p), H, wintypes.BOOL]
_userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL
_userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]


def _exe_path():
    return sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ================= 安装 / 卸载 =================
def _run_elevated(args):
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", _exe_path(), args, None, 0)
    return int(rc) > 32


def _firewall_open():
    """放行入站端口：否则别的节点连本机会被 Windows 防火墙静默拦截(表现为连接超时 10060)。"""
    try:
        from .config_loader import getint
        tcp = getint("server", "port", 8860)
        udp = getint("discovery", "udp_port", 8861)
        subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule",
                        "name=cc-session-agent", "dir=in", "action=allow",
                        "protocol=TCP", "localport=%d" % tcp], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
        subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule",
                        "name=cc-session-agent-udp", "dir=in", "action=allow",
                        "protocol=UDP", "localport=%d" % udp], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass


def _firewall_close():
    """删除本程序添加的防火墙放行规则。"""
    for name in ("cc-session-agent", "cc-session-agent-udp"):
        try:
            subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule",
                            "name=%s" % name], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass


def install():
    if not _is_admin():
        return _run_elevated("--install-service")
    bin_path = '"%s" --service' % _exe_path()
    subprocess.run(["sc", "create", SERVICE_NAME, "binPath=", bin_path,
                    "start=", "auto", "DisplayName=", SERVICE_DISPLAY], capture_output=True,
                   creationflags=CREATE_NO_WINDOW)
    try:
        from .version import AGENT_VERSION as _ver
    except Exception:
        _ver = "0.0.0"
    subprocess.run(["sc", "description", SERVICE_NAME,
                    "提供程序远程服务，版本v%s" % _ver], capture_output=True,
                   creationflags=CREATE_NO_WINDOW)
    _firewall_open()   # 放行入站端口，让别的节点能连上本机
    subprocess.run(["sc", "start", SERVICE_NAME], capture_output=True,
                   creationflags=CREATE_NO_WINDOW)
    return True


def uninstall():
    if not _is_admin():
        return _run_elevated("--uninstall-service")
    subprocess.run(["sc", "stop", SERVICE_NAME], capture_output=True,
                   creationflags=CREATE_NO_WINDOW)
    time.sleep(1)
    subprocess.run(["sc", "delete", SERVICE_NAME], capture_output=True,
                   creationflags=CREATE_NO_WINDOW)
    _firewall_close()   # 删除放行规则
    # 顺手杀掉服务拉起的 SYSTEM 实例(排除本卸载进程自身)
    try:
        me = os.getpid()
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-Process cc-session* -ErrorAction SilentlyContinue | "
                        "Where-Object { $_.Id -ne %d } | Stop-Process -Force" % me],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass
    return True


def is_installed():
    r = subprocess.run(["sc", "query", SERVICE_NAME], capture_output=True, text=True,
                       creationflags=CREATE_NO_WINDOW)
    return r.returncode == 0 and "1060" not in (r.stdout + r.stderr)


def is_running():
    """系统服务是否处于 RUNNING(健康检查用)。未安装/查询失败均返回 False。"""
    try:
        r = subprocess.run(["sc", "query", SERVICE_NAME], capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            return False
        return "RUNNING" in (r.stdout + r.stderr)
    except Exception:
        return False


# ================= 会话内以 SYSTEM 拉起 worker =================
def _launch_in_session(sid):
    """在会话 sid 以 SYSTEM 启动 exe --silent，返回进程句柄(HANDLE)或 None。"""
    hTok = H()
    access = (TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY |
              TOKEN_ADJUST_DEFAULT | TOKEN_ADJUST_SESSIONID)
    if not _adv.OpenProcessToken(_k32.GetCurrentProcess(), access, ctypes.byref(hTok)):
        return None
    hDup = H()
    ok = _adv.DuplicateTokenEx(hTok, MAXIMUM_ALLOWED, None,
                               SecurityIdentification, TokenPrimary, ctypes.byref(hDup))
    _k32.CloseHandle(hTok)
    if not ok:
        return None
    sid_c = wintypes.DWORD(sid)
    _adv.SetTokenInformation(hDup, TokenSessionId, ctypes.byref(sid_c), ctypes.sizeof(sid_c))

    env = ctypes.c_void_p()
    _userenv.CreateEnvironmentBlock(ctypes.byref(env), hDup, False)

    si = STARTUPINFO()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = "winsta0\\default"
    pi = PROCESS_INFORMATION()
    cmd = ctypes.create_unicode_buffer('"%s" --agent-silent' % _exe_path())
    ok = _adv.CreateProcessAsUserW(
        hDup, _exe_path(), cmd, None, None, False,
        CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW, env,
        os.path.dirname(_exe_path()) or None, ctypes.byref(si), ctypes.byref(pi))
    if env:
        _userenv.DestroyEnvironmentBlock(env)
    _k32.CloseHandle(hDup)
    if not ok:
        return None
    _k32.CloseHandle(pi.hThread)
    return pi.hProcess          # 保留进程句柄用于存活检测


def _alive(hproc):
    if not hproc:
        return False
    return _k32.WaitForSingleObject(hproc, 0) == WAIT_TIMEOUT


def _terminate(hproc):
    """结束拉起的 worker 进程。CloseHandle 只释放句柄不会杀进程，服务停止时必须显式 TerminateProcess，否则 --silent worker 残留。"""
    if not hproc:
        return
    try:
        if _alive(hproc):
            _k32.TerminateProcess(hproc, 0)
    except Exception:
        pass


def _monitor_loop(stop_flag):
    """轮询活动会话，确保有一个 SYSTEM worker 在跑；失败快速退出则退避，避免 churn。"""
    hproc = None
    launched_at = 0.0
    backoff_until = 0.0
    while not stop_flag[0]:
        try:
            sid = _k32.WTSGetActiveConsoleSessionId()
            now = time.time()
            if sid not in (0xFFFFFFFF, 0) and now >= backoff_until:
                if not _alive(hproc):
                    # 上个实例若“启动没多久就死”，多半是端口占用等持续性问题 → 退避 30s
                    if hproc and (now - launched_at) < 8:
                        backoff_until = now + 30
                    if hproc:
                        _k32.CloseHandle(hproc)
                        hproc = None
                    if now >= backoff_until:
                        hproc = _launch_in_session(sid)
                        launched_at = now
        except Exception:
            pass
        for _ in range(30):
            if stop_flag[0]:
                break
            time.sleep(0.1)
    # 服务停止：一并结束拉起的 worker，避免遗留进程(问题2)
    if hproc:
        _terminate(hproc)
        _k32.CloseHandle(hproc)


# ================= SCM 服务 =================
_SVC_HANDLE = None
_stop_flag = [False]


def _set_status(state, accept=None):
    if not _SVC_HANDLE:
        return
    if accept is None:
        accept = (SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN) if state == SERVICE_RUNNING else 0
    st = SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, state, accept, 0, 0, 0, 3000)
    _adv.SetServiceStatus(_SVC_HANDLE, ctypes.byref(st))


_HANDLER_TYPE = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, ctypes.c_void_p)
_MAIN_TYPE = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))


def _ctrl_handler(control, event_type, event_data, context):
    if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
        _stop_flag[0] = True
        _set_status(SERVICE_STOP_PENDING)
    return 0


def _service_main(argc, argv):
    global _SVC_HANDLE
    _SVC_HANDLE = _adv.RegisterServiceCtrlHandlerExW(SERVICE_NAME, _HANDLER_CB, None)
    if not _SVC_HANDLE:
        return
    _set_status(SERVICE_START_PENDING)
    _set_status(SERVICE_RUNNING)
    threading.Thread(target=_monitor_loop, args=(_stop_flag,), daemon=True).start()
    while not _stop_flag[0]:
        time.sleep(0.2)
    _set_status(SERVICE_STOPPED)


_HANDLER_CB = _HANDLER_TYPE(_ctrl_handler)
_MAIN_CB = _MAIN_TYPE(_service_main)


def run_service():
    class SERVICE_TABLE_ENTRY(ctypes.Structure):
        _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", _MAIN_TYPE)]
    _adv.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRY)]
    table = (SERVICE_TABLE_ENTRY * 2)()
    table[0].lpServiceName = SERVICE_NAME
    table[0].lpServiceProc = _MAIN_CB
    table[1].lpServiceName = None
    _adv.StartServiceCtrlDispatcherW(table)
