# -*- coding: utf-8 -*-
"""
updater.py —— 局域网 P2P 自动更新（发现更高版本节点则自动下载其 exe 覆盖本机并静默重启）

从 remote-disp 移植，按 cc-session 形态适配：
  - 版本源：本包 version.AGENT_VERSION（remote 原读 VERSION 文件，cc-session 无此文件）。
  - 服务名：CCSessionAgent（remote 为 RemoteDispLock）。
  - 下载文件名：cc-session-v<版本>.exe，落 app_root()（与 scripts/打包.bat --onefile 产物、
    install.bat 的 cc-session-v*.exe 命名一致）。
  - 【关键差异】cc-session 的 GUI 与被控端是同一个 exe：GUI 进程内也跑被控端后台线程
    （start_background），若在那里自替换会连带 taskkill 掉用户正开着的界面。故 maybe_update
    仅在【专职无界面被控端进程】(命令行带 --agent-silent，即服务 worker 或手动 --agent-silent)
    才自替换；GUI 进程只作更新【源】(serve /update/download)供别人拉取，自己不动。

机制：
  - 每个节点在 gossip 名单里携带自身 version（见 discovery.self_entry）。
  - 心跳轮询后 discovery 调 maybe_update(peers)：名单里存在版本高于本机的节点，
    则从其 https://host:port/update/download 拉取 exe，校验(体积>1MB + sha256)后覆盖并重启。
  - 仅打包(frozen)形态生效：源码模式无 exe 可分发/更新，直接跳过。
  - 下载在独立线程执行(不阻塞心跳)；完成后写 _update_apply.bat 覆盖 exe→静默重启。

打包形态约束：P2P 自升级要求节点为 onefile 形态（打包.bat 产物，exe 自包含）。
  deploy/ 的 onedir 安装包形态不适合做更新【源】——它的 sys.executable 只是个离不开
  _internal\ 的启动器，单独拉走无法运行。若全网都是 onefile 节点，则收发两端均正常。

分发端：register(app) 挂 GET /update/download，流式回传本机正在运行的 exe。
安全说明：该端点不鉴权，面向用户自有机群的局域网同版本分发；wss(B级)已加密传输。
"""
import logging
import os
import ssl
import sys
import threading
import time
import urllib.request

from .version import PRODUCT

log = logging.getLogger("cc-session.agent.updater")

_CHUNK = 65536
_tried = set()          # 本会话已尝试过的目标版本，避免重复下载
_tried_lock = threading.Lock()
_updating = False       # 下载线程进行中标志：防重入，且不阻塞心跳线程
_DL_MAX_SECONDS = 600   # 单次下载的总时长上限：防止读循环被"挤牙膏"式发送无限期挂住


def _frozen():
    return getattr(sys, "frozen", False)


def own_version():
    """本机版本号：直接取被控端单一版本源 AGENT_VERSION。"""
    try:
        from .version import AGENT_VERSION
        return AGENT_VERSION
    except Exception:
        return "0.0.0"


def _vtuple(s):
    """把 '1.2.3' 解析成可比较的整数元组；非数字段按 0。"""
    parts = []
    for seg in str(s or "").strip().split("."):
        num = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts) if parts else (0,)


def is_newer(cand, base):
    """cand 版本是否严格高于 base。"""
    return _vtuple(cand) > _vtuple(base)


# ---------------- 客户端：发现更高版本则下载 ----------------
def maybe_update(peers):
    """在 peers 名单里挑出高于本机的版本来源，按版本从高到低逐个尝试下载其 exe。

    仅在专职被控端进程(--agent-silent)执行自替换：GUI 进程(同一 exe，无该参数)在此直接返回，
    不自杀。收集所有"版本高于本机且带可达地址"的来源并按版本降序逐个尝试，任一成功即应用；
    全失败则本轮不标记、下轮重试（源码态返回 404、离线、内容损坏时自动回退到其他同版本节点）。
    """
    if not _frozen():
        return
    if "--agent-silent" not in sys.argv:
        # GUI 进程只作更新源，不自替换（否则会 taskkill 掉用户正开着的界面）
        return
    mine = own_version()
    cands = []
    for p in peers or []:
        ver, host, port = p.get("version"), p.get("host"), p.get("port")
        # 只从【本产品的被控端】拉取。两道过滤缺一不可：
        #   role != "agent"：排除控制端(role=controller)——它是另一个程序。
        #   app  != PRODUCT：排除"同样自称 role=agent、却是异构程序"的节点(如 remote-agent)。
        #     二者共用同一套握手/发现协议且都 role=agent，仅凭 role 分不开；其版本号(如 2.x)
        #     还常高于本机(0.0.x)，会被误判为"更新版本"而把它的 exe 拉来覆盖自己。
        #   不带 app/role(None) 的旧节点一律跳过——宁可不自动更新，也不拉异构 exe。
        if p.get("role") != "agent" or p.get("app") != PRODUCT:
            continue
        if ver and host and port and is_newer(ver, mine):
            cands.append((ver, host, port))
    if not cands:
        return
    cands.sort(key=lambda c: _vtuple(c[0]), reverse=True)   # 版本从高到低
    top_ver = cands[0][0]
    global _updating
    with _tried_lock:
        if top_ver in _tried:  # 本会话已成功处理过该最高版本
            return
        if _updating:          # 已有下载线程在跑：不重复触发
            return
        _updating = True

    def _worker():
        # 下载(每源最长60秒)放独立线程：不阻塞心跳轮询/老化，目标不可达时也不拖慢发现层
        global _updating
        try:
            for ver, host, port in cands:
                got = _download_from(host, port, ver)
                if got:
                    with _tried_lock:
                        _tried.add(top_ver)   # 成功才标记最高版本，失败下轮可重试
                    _apply_update(got, ver)
                    return
                # 该来源失败(不可达/源码态404/损坏)：自动回退下一个来源
            log.info("[自动更新] %d 个更高版本来源均下载失败，本轮放弃，下轮重试", len(cands))
        finally:
            with _tried_lock:
                _updating = False

    threading.Thread(target=_worker, name="auto-update", daemon=True).start()


def _apply_update(new_exe, ver):
    """下载完成后自动应用：等本进程退出→用新版覆盖当前 exe→重启。仅打包态。

    覆盖目标是 sys.executable（本进程自身路径）。onefile 命名带版本号(cc-session-v0.0.7.exe)，
    move 后文件名仍是旧版本串、但内容已是新版——运行期版本由 exe 内 AGENT_VERSION 决定，
    文件名滞后无害(服务 binPath 指向该固定路径，仍能启动)。
    """
    if not _frozen():
        return
    import subprocess
    from .paths import app_root
    cur = sys.executable
    bat = os.path.join(app_root(), "_update_apply.bat")
    # 批处理：等当前进程退出→覆盖→重启→自删。用 ping 代替 timeout(隐藏窗口下 timeout 会报错)；全程静默无框
    exe_name = os.path.basename(cur)             # 当前运行的 exe 文件名(所有同名实例都要杀掉才能替换)
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        # 清除 PyInstaller onefile 注入变量：防重启的新进程复用旧进程即将被删的 _MEI 目录
        'set "_MEIPASS2="',
        'set "_MEIPASS="',
        "sc stop CCSessionAgent >nul 2>&1",       # 有服务则先停(它会终止自己拉起的 --agent-silent worker)
        "ping -n 2 127.0.0.1 >nul",               # 给服务及其 worker 一点退出时间
        ":wait",
        # 【多开根治】主动强杀所有残留同名 exe(服务/worker/GUI)，直到一个不剩再替换。
        # 本 bat 以 cmd.exe 运行(且是另一个进程名)，taskkill 不会误杀自身。
        'taskkill /F /T /IM "%s" >nul 2>&1' % exe_name,
        'tasklist /FI "IMAGENAME eq %s" 2>nul | find /I "%s" >nul && (ping -n 2 127.0.0.1 >nul & goto wait)' % (exe_name, exe_name),
        "set /a n=0",
        ":movex",
        # 原子替换：move(同卷=重命名)保证 exe 路径要么完整旧版、要么完整新版，绝不出现半写状态。
        'move /y "%s" "%s" >nul && goto moved' % (new_exe, cur),
        "set /a n+=1",
        'if %n% lss 10 (ping -n 2 127.0.0.1 >nul & goto movex)',
        # 替换失败(exe 仍被占用)：本轮放弃，不另起任何实例；心跳下一轮会重新尝试更新。
        "goto done",
        ":moved",
        # cur 已原子替换为完整新版。用 if/else 明确"二选一"启动，杜绝多开：
        #   服务存在→sc start；不存在(手动 --agent-silent 无服务)→带 --agent-silent 重启保住无界面角色
        #   (直接 start "" cur 无参会进 GUI 模式，故显式补 --agent-silent)。
        "sc query CCSessionAgent >nul 2>&1",
        'if errorlevel 1 ( start "" "%s" --agent-silent ) else ( sc start CCSessionAgent >nul 2>&1 )' % cur,
        ":done",
        'del /q "%~f0"',
    ]
    try:
        with open(bat, "w", encoding="utf-8") as f:
            f.write("\r\n".join(lines) + "\r\n")
        # 隐藏窗口启动：STARTUPINFO(SW_HIDE) + CREATE_NO_WINDOW，彻底不弹黑框
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0        # SW_HIDE
        # 剥离 PyInstaller onefile 注入的 _MEIPASS2/_PYI_* 环境变量：否则重启链拉起的新 onefile
        # 进程会复用旧进程那个"即将被删"的 %TEMP%\_MEIxxxx 目录，旧进程退出清理后新进程加载
        # Python DLL 失败("找不到指定的模块")。手动启动因环境干净故正常。
        clean_env = {k: v for k, v in os.environ.items()
                     if not (k.startswith("_MEIPASS") or k.startswith("_PYI"))}
        # 服务模型静默自更新的关键：本进程退出前【先同步停掉服务】，确保服务不再把旧 exe 拉起来
        # (消除"服务重新拉起 vs 更新替换"竞态)。worker 由服务以 SYSTEM 令牌拉起，故有权 sc stop；
        # 非服务模式下找不到该服务，命令无害失败。之后 bat 完成替换并 sc start 重新拉起新版。
        try:
            subprocess.run(["sc", "stop", "CCSessionAgent"], capture_output=True,
                           creationflags=0x08000000, timeout=30)
        except Exception:
            pass
        subprocess.Popen(["cmd", "/c", bat], startupinfo=si,
                         creationflags=0x08000000,  # CREATE_NO_WINDOW
                         close_fds=True, env=clean_env)
        log.warning("[自动更新] 已下载 v%s，正在静默重启到新版本…", ver)
        time.sleep(1)
        os._exit(0)            # 退出当前进程，交给批处理覆盖并拉起新版
    except Exception as e:
        log.info("[自动更新] 自动应用失败：%s（新版已下载到 %s，可手动替换）", e, new_exe)


def push_update(url, expect_sha256, version=None, allow_host=None):
    """控制端主动推送升级：从 url 拉新版 exe → 校验 sha256 → 替换自己并静默重启。

    与 maybe_update(被控端之间自动互相升级)是两条独立入口，共用同一套下载校验与
    替换逻辑。本函数只做受理与校验，真正的下载在独立线程跑，立即返回让 WS 能回包。

    allow_host: 只允许从这个主机下载(取发指令的控制端 IP)。**这道校验不能省** ——
      被控端会把下载到的文件当程序执行，若放任 url 指向任意主机，一旦控制端账号
      被盗就能让全网被控端去拉任意 exe 执行。锁死来源后，攻击者还必须能控制
      那台控制端本机才行。

    返回 (ok, message)：ok=False 表示当场就拒了(参数不合法/非打包态/正在升级)。
    """
    global _updating
    if not _frozen():
        return False, "源码模式不支持替换升级（仅打包 exe 生效）"
    if not url or not expect_sha256:
        return False, "缺少下载地址或校验和"

    from urllib.parse import urlparse
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        return False, "下载地址协议不合法：%s" % u.scheme
    if allow_host and (u.hostname or "") != allow_host:
        return False, "下载地址主机(%s)与下发指令的控制端(%s)不一致，已拒绝" % (
            u.hostname, allow_host)

    with _tried_lock:
        if _updating:          # 已有下载线程在跑(自动更新或上一次推送)：不重复触发
            return False, "已有升级任务在进行中"
        _updating = True

    def _worker():
        global _updating
        try:
            got = _download_url(url, expect_sha256.strip().lower(), version)
            if got:
                _apply_update(got, version or "pushed")   # 成功则本进程被替换并重启
            else:
                log.warning("[推送升级] 下载或校验失败，已放弃本次升级")
        finally:
            with _tried_lock:
                _updating = False

    threading.Thread(target=_worker, name="push-update", daemon=True).start()
    return True, "已受理，正在下载"


def _download_url(url, expect_sha256, version=None):
    """按 url 下载 exe 到 app_root()，强制 sha256 校验。返回落地路径或 None。

    与 _download_from 的区别：那个是"从对端 /update/download 拉"，按版本号命名；
    这个是"按控制端给的任意 url 拉"，且**校验和必填**(控制端一定算得出来)，
    不接受退化为长度校验。
    """
    import hashlib
    from .paths import app_root
    tag = (version or "push").replace("/", "_").replace("\\", "_")
    target = os.path.join(app_root(), "cc-session-%s.exe" % tag)
    tmp = target + ".part"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=120, context=ctx) as r:
            expected = int(r.headers.get("Content-Length") or 0)
            got = 0
            h = hashlib.sha256()
            # 总时长闸门：socket 超时只约束"单次读"，对端若一直挤牙膏式地发数据，
            # 每次读都不超时，整个下载却能挂住几小时，把升级任务永久卡在进行中。
            deadline = time.time() + _DL_MAX_SECONDS
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if time.time() > deadline:
                        raise TimeoutError(
                            "下载超过 %d 秒仍未完成，判定卡死" % _DL_MAX_SECONDS)
    except Exception as e:
        _rm(tmp)
        log.warning("[推送升级] 下载失败：%s", e)
        return None

    if got < 1024 * 1024 or (expected and got != expected):
        _rm(tmp)
        log.warning("[推送升级] 下载不完整(收 %d / 期望 %d 字节)，丢弃", got, expected)
        return None
    if h.hexdigest() != expect_sha256:
        _rm(tmp)
        log.warning("[推送升级] 内容指纹不匹配，丢弃不应用")
        return None
    try:
        os.replace(tmp, target)
    except Exception as e:
        _rm(tmp)
        log.warning("[推送升级] 落地失败：%s", e)
        return None
    log.warning("[推送升级] 已下载 %d 字节(指纹校验通过) → %s", got, target)
    return target


def _rm(path):
    try:
        os.remove(path)
    except Exception:
        pass


def _download_from(host, port, ver):
    from .paths import app_root
    target = os.path.join(app_root(), "cc-session-v%s.exe" % ver)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = "https://%s:%d/update/download" % (host, int(port))
    tmp = target + ".part"
    try:
        import hashlib
        with urllib.request.urlopen(url, timeout=60, context=ctx) as r:
            # 硬闸：来源必须自证是 cc-session(缺失或不符即拒)。这道校验独立于发现层的 app 过滤，
            # 即便对端在名单里被误标/漏标，真正的字节替换在此仍拦得住异构 exe。
            prod = (r.headers.get("X-App-Product") or "").strip()
            if prod != PRODUCT:
                log.warning("[自动更新] 来源 %s:%d 非 %s(app=%r)，拒绝拉取其 exe",
                            host, int(port), PRODUCT, prod or None)
                return None
            expected = int(r.headers.get("Content-Length") or 0)
            exp_hash = (r.headers.get("X-App-SHA256") or "").strip().lower()
            # 已有完整文件则直接用：优先按内容指纹校验(有 hash 时)，否则退回大小校验，
            # 避免复用之前截断/损坏、或大小巧合相同但内容坏的缓存
            if os.path.exists(target):
                if exp_hash:
                    if _sha256_file(target) == exp_hash:
                        return target
                elif expected and os.path.getsize(target) == expected:
                    return target
            got = 0
            h = hashlib.sha256()
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
        # 完整性校验：长度收满 + 体积合理(>1MB) + 内容指纹匹配(分发端提供时)，任一不过即判损坏丢弃
        if (expected and got != expected) or got < 1024 * 1024:
            try:
                os.remove(tmp)
            except Exception:
                pass
            log.info("[自动更新] 下载不完整(收 %d / 期望 %d 字节)，丢弃不应用", got, expected)
            return None
        if exp_hash and h.hexdigest() != exp_hash:
            try:
                os.remove(tmp)
            except Exception:
                pass
            log.warning("[自动更新] 内容指纹不匹配(下载已损坏)，丢弃不应用，下轮重试")
            return None
        os.replace(tmp, target)
        log.warning("[自动更新] 已下载 v%s(%d 字节，%s校验通过) 到 %s",
                    ver, got, "指纹" if exp_hash else "长度", target)
        return target
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        log.info("[自动更新] 下载 v%s 失败：%s", ver, e)
        return None


# ---------------- 分发端：提供本机 exe 供别的节点拉取 ----------------
def _sha256_file(path):
    """算文件 sha256（十六进制小写）；失败返回 ""。"""
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def serve_self():
    """流式回传本机正在运行的 exe（更新源）。仅打包(onefile)态有意义。"""
    from flask import Response
    if not _frozen():
        return Response("源码模式无可分发的可执行文件", status=404)
    exe = sys.executable
    try:
        size = os.path.getsize(exe)
    except OSError:
        return Response("找不到可执行文件", status=404)
    # 预先算内容指纹供接收端校验（长度相同但内容损坏也能识别）；空则接收端退回仅长度校验
    digest = _sha256_file(exe)

    def gen():
        with open(exe, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                yield chunk

    resp = Response(gen(), mimetype="application/octet-stream")
    resp.headers["Content-Length"] = str(size)
    resp.headers["Content-Disposition"] = "attachment; filename=cc-session.exe"
    resp.headers["X-App-Version"] = own_version()
    resp.headers["X-App-Product"] = PRODUCT   # 接收端据此确认"确是 cc-session 的 exe"再落地
    if digest:
        resp.headers["X-App-SHA256"] = digest
    return resp


def register(app):
    """挂 /update/download：流式回传本机正在运行的 exe（自动更新源）。"""
    @app.get("/update/download")
    def _update_download():
        return serve_self()

    return app
