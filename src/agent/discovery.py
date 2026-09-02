# -*- coding: utf-8 -*-
"""
discovery.py —— 节点发现与在线名单维护（T3 网络层）

职责（见 CONTRACT §6 T3）：
  1. bootstrap 引导发现：读 config [discovery].bootstrap（逗号分隔 host:port），
     启动时向每个 bootstrap 发 POST /peers/register 上报自身，接收回带名单并合并。
  2. peer_registry：内存维护在线名单，线程安全。
  3. heartbeat：后台线程周期对已知 peer 发 GET /ping 刷新 last_seen；
     超 offline_after 标记 offline，超 evict_after 剔除。
  4. gossip：/ping 与 /register 回包携带自身已知名单，最终一致合并。
  5. 可选 UDP 广播（config udp_enabled，默认开：同网段自动发现）。
  6. 暴露 start() / get_peers() / on_change(callback)。

  被动模式：config [discovery] announce=false → 关闭一切【主动广播/上报自身】（UDP 广播、
  bootstrap 上报、ping 回注），本机不再主动暴露存在，只被动应答直连。默认 true 维持原行为。

跨公网/NAT 约束：
  - 本机对外地址取 config [node].advertise_host / advertise_port；留空则回退本机 IP:port。
  - NAT 后的节点必须在 config 填写公网/端口转发后的 advertise_host:port，
    否则别的节点拿到的是内网地址无法回连；纯内网可靠办法是端口转发或组 VPN。

实现约束：
  - 节点间 HTTP 调用一律用标准库 urllib.request，超时短、异常吞掉不影响主流程。
  - 服务端为 wss/https（单端口 TLS，见 CONTRACT §1/§8），故这里用 https:// +
    不校验证书的 SSL 上下文（B级指纹固定在 WS 握手层做，发现层只求可达）。
"""
import json
import socket
import ssl
import threading
import time
import urllib.request

from .config_loader import get, getbool, getint, save_local
from .node_identity import NODE_ID, NODE_NAME, ROLE
from .version import AGENT_VERSION, PRODUCT

# 本节点版本（gossip 时携带，供别的节点判断是否需要自动更新）
_MYVER = AGENT_VERSION

# ---------- 配置项 ----------
_SERVER_PORT = getint("server", "port", 8860)
_HEARTBEAT = max(1, getint("discovery", "heartbeat_interval", 5))
_OFFLINE_AFTER = getint("discovery", "offline_after", 15)
_EVICT_AFTER = getint("discovery", "evict_after", 60)
def udp_effective():
    """UDP 局域网广播是否启用。默认开启（同网段自动发现）；config.ini 里显式设 false 则关闭。"""
    return getbool("discovery", "udp_enabled", True)


def announce_effective():
    """是否【主动广播/上报自身】。默认开启；config.ini [discovery] announce=false 进入被动模式：
    不发 UDP 广播、不向 bootstrap 上报、ping 时也不回注自身——本机不再主动暴露存在，
    仅被动应答直连（别人 scan_lan/手动 add 仍能连上）。比 udp_enabled 更彻底（后者只关 UDP 一路）。"""
    return getbool("discovery", "announce", True)


_UDP_ENABLED = udp_effective()
_ANNOUNCE = announce_effective()
_UDP_PORT = getint("discovery", "udp_port", 8861)
# UDP 广播间隔（秒）。广播只用于"让同网段互相认识"，认识之后靠 /ping 心跳维持在线判定，
# 没必要 5 秒喊一次 —— 默认 60 秒，空闲期几乎不产生流量。
_UDP_INTERVAL = max(5, getint("discovery", "udp_interval", 60))
# 剔除阈值必须明显大于广播间隔，否则"进程活着但 HTTP 面不可达"的节点会在名单里每分钟闪进闪出
# （靠广播续命 → 到 evict_after 被剔 → 下次广播又加回）。这里兜底抬高，
# 使老机器上遗留的 evict_after=30 也不会与 60 秒广播失配。
_EVICT_AFTER = max(_EVICT_AFTER, _UDP_INTERVAL * 2 + 30)
_HTTP_TIMEOUT = 4  # 秒，节点间调用超时要短，宁可漏也别卡主流程
_MAX_PEERS = 512   # 注册表规模上限：防无鉴权 gossip 洪泛撑爆内存（正常机群远达不到）
_TOMBSTONE_TTL = 600  # 移除节点后的墓碑时长(秒)：期间拒绝该 node_id 经 gossip 复活

# ---------- 在线名单（线程安全）----------
# 结构：{node_id: {node_name, host, port, status, last_seen}}
_registry = {}
_reg_lock = threading.Lock()
# 墓碑：node_id -> 过期时间戳。移除节点后短时内拒绝其经 gossip 复活（清残留/无效节点）
_tombstones = {}

# 名单变化回调（供 SSE 推送）
_callbacks = []
_cb_lock = threading.Lock()

_started = False


# ---------- 自身对外地址 ----------
def _local_ip():
    """探测本机对外网卡 IP（无实际发包，仅用于回退）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


def _local_ipv4s():
    """本机所有非回环 IPv4 地址（含虚拟网卡）。

    UDP 广播必须【逐网卡】发：直接 sendto("255.255.255.255") 不绑定源地址时，
    Windows 按路由表只挑一块网卡，装了 WSL/Hyper-V/VMware 的机器往往挑中虚拟网卡
    (如 172.29.208.1)，广播全打进虚拟网段，物理局域网里的节点一个都收不到
    ——表现为"我认识它们、它们不认识我"，别人自然也就发现不了我的新版本。"""
    out = []
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = ai[4][0]
            if ip and not ip.startswith("127.") and ip not in out:
                out.append(ip)
    except Exception:
        pass
    mine = _local_ip()
    if mine and not mine.startswith("127.") and mine not in out:
        out.append(mine)
    return out or [""]


def _self_host():
    # 优先级：手填 advertise（跨网/端口转发时填公网地址）> 本机 IP 回退
    return get("node", "advertise_host") or _local_ip()


def _self_port():
    # 优先级：手填 advertise 端口 > 本机服务端口回退
    port = getint("node", "advertise_port", 0)
    if port:
        return port
    return _SERVER_PORT


def self_entry():
    """本节点在名单中的表示（gossip 时带上，让别人认识自己）。
    额外携带内网直连地址(local_host/local_port)：即便对外走公网/端口转发，同网段对端也能
    优先走内网直连（更快、不经公网），跨网段则回退 host/port(公网/端口转发)。"""
    return {
        "node_id": NODE_ID,
        "node_name": NODE_NAME,
        "host": _self_host(),
        "port": _self_port(),
        "local_host": _local_ip(),
        "local_port": _SERVER_PORT,
        "status": "online",
        "last_seen": time.time(),
        "version": _MYVER,
        "role": ROLE,
        "app": PRODUCT,
    }


# ---------- HTTP 调用（标准库 urllib）----------
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _call(host, port, path, method="GET", body=None, timeout=None):
    """向对端节点发 https 请求；失败返回 None（异常一律吞掉）。"""
    url = "https://%s:%s%s" % (host, port, path)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout or _HTTP_TIMEOUT, context=_SSL_CTX) as r:
            raw = r.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}
    except Exception:
        return None


# ---------- 名单合并 / 变化通知 ----------
def _notify():
    """名单发生变化时回调所有订阅者（异常隔离，互不影响）。"""
    peers = get_peers()
    with _cb_lock:
        cbs = list(_callbacks)
    for cb in cbs:
        try:
            cb(peers)
        except Exception:
            pass


def ingest_peer(info):
    """合并单个对端（来自 /peers/register 上报）。返回是否有变化。"""
    return _merge_one(info)


def merge_peers(peer_list):
    """批量合并对端名单（gossip）。返回是否有变化。"""
    changed = False
    for p in peer_list or []:
        if _merge_one(p, notify=False):
            changed = True
    if changed:
        _notify()
    return changed


def _merge_one(info, notify=True):
    """合并一条 peer 记录；忽略自身。返回是否有变化。
    对无鉴权 gossip 来源做输入加固：字段限长、端口合法、注册表规模封顶，
    防匿名者经公网端点(POST /peers/register)灌注表撑爆内存或扩散超大载荷。

    【只学地址，不判在线】本函数的来源(UDP 广播/gossip 转述/对端自报)都只能证明"有人这么说"，
    不能证明【本机能连上它】。在线与否一律由 _ping_round 亲自 /ping 成功来认定(写 verified_at)。
    否则会出现：对端 HTTP 面卡死、UDP 仍在广播 → 名单常亮"在线"，但连上就被判离线断开。"""
    if not isinstance(info, dict):
        return False
    nid = info.get("node_id")
    host = info.get("host")
    port = info.get("port")
    if not nid or nid == NODE_ID or not host or not port:
        return False
    # 字段限长 + 端口范围校验：非法直接丢弃，不入表
    if not isinstance(nid, str) or len(nid) > 64:
        return False
    if not isinstance(host, str) or len(host) > 255:
        return False
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if not (1 <= port <= 65535):
        return False
    name = str(info.get("node_name") or nid)[:64]
    ver = info.get("version")
    if ver is not None:
        ver = str(ver)[:32]
    role = info.get("role")
    if role is not None:
        role = str(role)[:16]
    app = info.get("app")
    if app is not None:
        app = str(app)[:32]
    # 内网直连地址（可选）：供接收端同网段时优先直连，跨网段回退 host/port(公网/端口转发)
    lh = info.get("local_host")
    lh = str(lh)[:255] if lh else None
    try:
        lp = int(info.get("local_port")) if info.get("local_port") else None
    except (TypeError, ValueError):
        lp = None
    if lp is not None and not (1 <= lp <= 65535):
        lp = None
    now = time.time()
    changed = False
    with _reg_lock:
        # 墓碑期内拒绝复活（移除残留/无效节点后，防 gossip 立刻把它加回来）
        exp = _tombstones.get(nid)
        if exp is not None:
            if now < exp:
                return False
            _tombstones.pop(nid, None)   # 墓碑已过期，清理
        old = _registry.get(nid)
        if old is None:
            # 注册表规模封顶：满了且是新节点则拒绝（洪泛防护，正常机群远达不到此上限）
            if len(_registry) >= _MAX_PEERS:
                return False
            # 新节点先入表为 offline，等首轮 /ping 成功再转 online（在线以实测为准）
            _registry[nid] = {
                "node_id": nid, "node_name": name,
                "host": host, "port": int(port),
                "local_host": lh, "local_port": lp,
                "status": "offline", "last_seen": now, "verified_at": 0,
                "version": ver, "role": role, "app": app,
            }
            changed = True
        else:
            # 地址/名字变化算变化；last_seen 刷新(证明进程还在自报，用于老化剔除)，
            # 但 status/verified_at 不动 —— 在线只由 /ping 实测认定
            if (old.get("host") != host or int(old.get("port")) != int(port)
                    or old.get("node_name") != name):
                changed = True
            old.update({"node_name": name, "host": host,
                        "port": int(port), "last_seen": now,
                        "local_host": lh or old.get("local_host"),
                        "local_port": lp or old.get("local_port"),
                        "version": ver or old.get("version"),
                        "role": role or old.get("role"),
                        "app": app or old.get("app")})
    if changed and notify:
        _notify()
    return changed


# ---------- 对外查询 ----------
def get_peers():
    """返回在线名单（不含自身）：[{node_id,node_name,host,port,status,last_seen}]。"""
    with _reg_lock:
        return [dict(p) for p in _registry.values()]


def gossip_peers():
    """gossip 回包用：自身名单 + 自身条目（让对端认识自己）。"""
    return get_peers() + [self_entry()]


def on_change(callback):
    """注册名单变化回调（供 SSE 推送）。callback(peers_list)。"""
    with _cb_lock:
        _callbacks.append(callback)


# ---------- bootstrap 引导 ----------
def _parse_bootstrap():
    """解析 config [discovery].bootstrap → [(host, port), ...]。"""
    out = []
    raw = get("discovery", "bootstrap") or ""
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            host, _, port = item.rpartition(":")
            try:
                out.append((host.strip(), int(port.strip())))
            except ValueError:
                pass
        else:
            out.append((item, _SERVER_PORT))
    return out


def _self_report():
    """自身对外表示（上报/注册用），携带版本与内网直连地址。"""
    return {"node_id": NODE_ID, "node_name": NODE_NAME,
            "host": _self_host(), "port": _self_port(),
            "local_host": _local_ip(), "local_port": _SERVER_PORT,
            "version": _MYVER, "role": ROLE, "app": PRODUCT}


def _register_bootstrap():
    """向每个 bootstrap 上报自身并合并其回带名单。"""
    me = _self_report()
    for host, port in _parse_bootstrap():
        resp = _call(host, port, "/peers/register", method="POST", body=me)
        if resp:
            merge_peers(resp.get("peers", []))


# ---------- 局域网主动扫描 / 手动添加节点 ----------
def scan_lan(timeout=1.0, workers=128):
    """扫描本机所在 /24 网段的 :port/ping，发现并合并节点。返回 {found}。"""
    import concurrent.futures
    base = _local_ip()
    if not base or base.startswith("127."):
        return {"found": 0, "error": "无法确定本机内网地址"}
    prefix = base.rsplit(".", 1)[0]
    port = _SERVER_PORT
    hosts = ["%s.%d" % (prefix, i) for i in range(1, 255)]
    found = set()

    def probe(host):
        resp = _call(host, port, "/ping", method="GET", timeout=timeout)
        if resp and resp.get("node_id") and resp.get("node_id") != NODE_ID:
            merge_peers(resp.get("peers", []))
            return resp.get("node_id")
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for nid in ex.map(probe, hosts):
            if nid:
                found.add(nid)
    return {"found": len(found)}


def add_peer(host, port=None):
    """手动添加节点：向 host:port 注册自身并合并其名单（双向发现），持久化到 bootstrap。"""
    host = (host or "").strip()
    if not host:
        return {"ok": False, "error": "请填写主机地址"}
    try:
        port = int(port) if port else _SERVER_PORT
    except (TypeError, ValueError):
        return {"ok": False, "error": "端口无效"}
    resp = _call(host, port, "/peers/register", method="POST", body=_self_report(), timeout=4)
    if resp is None:
        return {"ok": False,
                "error": "无法连接 %s:%d（对端未运行 / 端口或防火墙不通）" % (host, port)}
    merge_peers(resp.get("peers", []))
    _add_bootstrap(host, port)
    return {"ok": True, "found": len(resp.get("peers", []))}


def forget_peer(node_id, propagate=True):
    """移除一个节点：本地删除 + 打墓碑（墓碑期内拒绝 gossip 复活）。
    propagate=True 时把移除请求转发给当前已知对端，令其一并移除——从而清掉在整个网络里
    残留/无效的节点（如误注入的测试节点）。转发到的对端不再二次转发，避免风暴。"""
    node_id = (node_id or "").strip()
    if not node_id or node_id == NODE_ID:
        return {"ok": False, "error": "无效 node_id"}
    now = time.time()
    with _reg_lock:
        _tombstones[node_id] = now + _TOMBSTONE_TTL
        existed = _registry.pop(node_id, None) is not None
        # 顺手清理过期墓碑，避免长期累积
        for k in [k for k, e in _tombstones.items() if now >= e]:
            _tombstones.pop(k, None)
        targets = [(p["host"], p["port"]) for p in _registry.values()] if propagate else []
    if existed:
        _notify()
    if propagate:
        body = {"node_id": node_id}
        for host, port in targets:
            _call(host, port, "/peers/forget", method="POST", body=body, timeout=3)
    return {"ok": True, "removed": existed}


def _add_bootstrap(host, port):
    """把 host:port 去重追加进 config.ini [discovery].bootstrap（持久化，重启仍在）。"""
    cur = get("discovery", "bootstrap") or ""
    items = [x.strip() for x in cur.split(",") if x.strip()]
    entry = "%s:%d" % (host, int(port))
    if entry not in items:
        items.append(entry)
        save_local("discovery", "bootstrap", ",".join(items))


# ---------- 心跳线程 ----------
def _heartbeat_loop():
    while True:
        try:
            if _ANNOUNCE:
                _register_bootstrap()      # 周期重报，容忍 bootstrap 掉线后恢复；被动模式不上报
            _ping_round()
            _age_out()
            # P2P 局域网自动更新：发现更高版本节点则拉取其 exe 覆盖并静默重启。
            # 仅专职被控端进程(--agent-silent)自替换；GUI 进程在 maybe_update 内直接返回，只作更新源。
            try:
                from . import updater
                updater.maybe_update(get_peers())
            except Exception:
                pass
        except Exception:
            pass
        time.sleep(_HEARTBEAT)


def _ping_round():
    """并发 ping 已知 peer，刷新 last_seen 并合并其回带名单。
    （串行会被每个不可达节点拖满 4 秒超时，整轮超过 offline_after 时活节点被误判离线）"""
    with _reg_lock:
        targets = [(p["node_id"], p["host"], p["port"]) for p in _registry.values()]
    if not targets:
        return

    flipped = []

    def _ping_one(t):
        nid, host, port = t
        resp = _call(host, port, "/ping", method="GET")
        if resp:
            now = time.time()
            with _reg_lock:
                p = _registry.get(nid)
                if p is not None:
                    if p.get("status") != "online":
                        flipped.append(nid)      # 离线→在线，需推给前端
                    p["status"] = "online"
                    p["last_seen"] = now
                    p["verified_at"] = now       # 唯一的"在线"认定处：本机实测连得上
            peers = resp.get("peers", [])
            merge_peers(peers)
            # 【双向可见自愈】GET /ping 不携带自身身份，对端从我的心跳里学不到"我是谁"。
            # 若它回带的名单里没有我，就主动登记一次——否则一旦 UDP 广播不通(跨网段、
            # 广播被交换机拦、或本机广播走错网卡)，对端会永远不知道我存在，
            # 也就永远发现不了我的新版本，自动更新静默失效。发现即停，不会反复打扰。
            if _ANNOUNCE and not any((q or {}).get("node_id") == NODE_ID for q in peers):
                _call(host, port, "/peers/register", method="POST", body=_self_report())
        # 失败不动，交给 _age_out 老化处理

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(targets))) as ex:
        list(ex.map(_ping_one, targets))
    if flipped:
        _notify()


def _age_out():
    """超 evict_after 无任何音讯则剔除；超 offline_after 未 ping 通则标 offline。

    两个时钟分工：last_seen = 收到过任何自报(含 UDP 广播)，只管"是否还留在名单里"；
    verified_at = 本机 /ping 实测成功，管"是否显示在线"。对端进程活着但 HTTP 面卡死时，
    它会留在名单里并显示【离线】，而不是常亮在线却连不上。"""
    now = time.time()
    changed = False
    with _reg_lock:
        for nid in list(_registry.keys()):
            p = _registry[nid]
            if now - p.get("last_seen", 0) > _EVICT_AFTER:
                del _registry[nid]
                changed = True
            elif (now - p.get("verified_at", 0) > _OFFLINE_AFTER
                  and p.get("status") != "offline"):
                p["status"] = "offline"
                changed = True
    if changed:
        _notify()


# ---------- 可选 UDP 广播（同网段加速；跨公网无效，默认关）----------
def _udp_listener():
    """监听广播，收到对端自报后合并。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", _UDP_PORT))
    except Exception:
        return
    while True:
        try:
            raw, _addr = s.recvfrom(4096)
            info = json.loads(raw.decode("utf-8", errors="replace"))
            ingest_peer(info)
        except Exception:
            pass


def _udp_beacon():
    """周期广播自身信息。

    启动初期先密集广播 5 轮(每 5 秒)，让刚上线的机器能被同网段秒级发现；之后降到
    _UDP_INTERVAL(默认 60 秒)长间隔——彼此认识之后在线判定靠 /ping 心跳，广播只是"打招呼"，
    没必要一直喊，空闲期几乎不产生流量。"""
    sent = 0
    while True:
        # 每轮现取对外地址：公网/端口转发连通或端口变化后广播地址才会跟着更新，
        # 否则与 gossip 报的地址不一致，同网段对端的名单会在两个地址间来回跳变
        me = {"node_id": NODE_ID, "node_name": NODE_NAME,
              "host": _self_host(), "port": _self_port(), "version": _MYVER,
              "role": ROLE, "app": PRODUCT}
        data = json.dumps(me, ensure_ascii=False).encode("utf-8")
        # 逐网卡发：bind 到具体源地址才能强制包从该网卡出去（见 _local_ipv4s 的说明）
        for src in _local_ipv4s():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                if src:
                    s.bind((src, 0))
                s.sendto(data, ("255.255.255.255", _UDP_PORT))
                s.close()
            except Exception:
                pass
        sent += 1
        time.sleep(5 if sent < 5 else _UDP_INTERVAL)


# ---------- 启动 ----------
def start():
    """启动发现：bootstrap 引导 + 心跳后台线程（+ 可选 UDP）。幂等。"""
    global _started
    if _started:
        return
    _started = True
    # 首次引导（同步一次，让启动后立刻有名单）；被动模式不主动上报
    if _ANNOUNCE:
        try:
            _register_bootstrap()
        except Exception:
            pass
    threading.Thread(target=_heartbeat_loop, name="disc-heartbeat", daemon=True).start()
    if _UDP_ENABLED:
        # 监听是被动收包，保留；广播是主动暴露自身，被动模式下不启
        threading.Thread(target=_udp_listener, name="disc-udp-listen", daemon=True).start()
        if _ANNOUNCE:
            threading.Thread(target=_udp_beacon, name="disc-udp-beacon", daemon=True).start()
