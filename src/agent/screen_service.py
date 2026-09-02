# -*- coding: utf-8 -*-
"""
screen_service.py —— 屏幕能力：单次截图(screen.shot) + 远程桌面观看(screen.stream.*)

远程桌面按「截图轮询」实现：start 后后台线程按设定帧率(默认约1fps)截主屏，
JPEG 压缩后经 stream_hub 以 screen.frame 流下发；stop 或连接断开即止。仅观看，不控制。

handler 签名 fn(ctx, msg, send)，模块加载即注册。截图用 PIL.ImageGrab（随包 Pillow）。
"""
import base64
import io
import sys
import threading

from . import cap_dispatch as cap_router
from . import protocol
from .protocol import Cap, Err
from .stream_hub import HUB

try:
    from PIL import ImageGrab
    _HAVE_GRAB = True
except Exception:
    ImageGrab = None
    _HAVE_GRAB = False

# 远程控制：ctypes 注入鼠标/键盘（Windows）。先设 DPI 感知，令截图/坐标/光标同处物理像素。
# 键盘统一用 keybd_event（含 KEYEVENTF_UNICODE 打任意字符），避免 SendInput 结构体 cbSize 坑。
_user32 = None
if sys.platform == "win32":
    try:
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
        _user32 = ctypes.windll.user32
    except Exception:
        _user32 = None

# 鼠标事件标志
_MEF = {"ld": 0x0002, "lu": 0x0004, "rd": 0x0008, "ru": 0x0010,
        "md": 0x0020, "mu": 0x0040, "wheel": 0x0800}
# 键盘特殊键 → 虚拟键码（其余可打印字符走 unicode SendInput）
_VK = {"Enter": 0x0D, "Backspace": 0x08, "Tab": 0x09, "Escape": 0x1B,
       "Delete": 0x2E, "ArrowUp": 0x26, "ArrowDown": 0x28, "ArrowLeft": 0x25,
       "ArrowRight": 0x27, "Home": 0x24, "End": 0x23, "PageUp": 0x21,
       "PageDown": 0x22, "Control": 0x11, "Alt": 0x12, "Shift": 0x10,
       " ": 0x20}

# 物理键码(ev.code) → 虚拟键码：按 keydown/keyup 分别按下/抬起，修饰键(Ctrl/Alt/Shift)才能保持
_CODE_VK = {
    "Enter": 0x0D, "Space": 0x20, "Tab": 0x09, "Backspace": 0x08, "Escape": 0x1B,
    "Delete": 0x2E, "Insert": 0x2D, "CapsLock": 0x14,
    "ArrowUp": 0x26, "ArrowDown": 0x28, "ArrowLeft": 0x25, "ArrowRight": 0x27,
    "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
    "ControlLeft": 0x11, "ControlRight": 0x11, "ShiftLeft": 0x10, "ShiftRight": 0x10,
    "AltLeft": 0x12, "AltRight": 0x12, "MetaLeft": 0x5B, "MetaRight": 0x5C,
    "Minus": 0xBD, "Equal": 0xBB, "BracketLeft": 0xDB, "BracketRight": 0xDD,
    "Backslash": 0xDC, "Semicolon": 0xBA, "Quote": 0xDE, "Backquote": 0xC0,
    "Comma": 0xBC, "Period": 0xBE, "Slash": 0xBF,
}
for _c in range(ord("A"), ord("Z") + 1):
    _CODE_VK["Key" + chr(_c)] = _c          # KeyA→0x41 …
for _d in range(10):
    _CODE_VK["Digit" + str(_d)] = 0x30 + _d  # Digit0→0x30 …
for _f in range(1, 13):
    _CODE_VK["F" + str(_f)] = 0x70 + (_f - 1)  # F1→0x70 …


def _screen_wh():
    return _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)


def _send_unicode(ch):
    """按 unicode 敲一个字符（按下+抬起），支持任意 BMP 字符。"""
    KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002
    code = ord(ch) & 0xFFFF
    _user32.keybd_event(0, code, KEYEVENTF_UNICODE, 0)
    _user32.keybd_event(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)

# stream_id -> threading.Event（置位即停）
_STREAMS = {}
_LOCK = threading.Lock()

# 桌面跟随：切到当前"输入桌面"，跨桌面(锁屏/UAC安全桌面/切换用户)也能抓屏/注入。
# 普通权限对 Winlogon 安全桌面无访问权(锁屏需以 SYSTEM 服务运行)，此处 best-effort、失败静默。
_tls = threading.local()


def _use_input_desktop():
    if not _user32:
        return
    try:
        DESKTOP_ALL = 0x01FF
        h = _user32.OpenInputDesktop(0, False, DESKTOP_ALL)
        if h:
            _user32.SetThreadDesktop(h)
            prev = getattr(_tls, "desk", None)
            _tls.desk = h
            if prev and prev != h:
                try:
                    _user32.CloseDesktop(prev)
                except Exception:
                    pass
    except Exception:
        pass


def _grab_jpeg(quality=60, max_w=1600):
    """抓主屏一帧，等比缩到不超过 max_w，返回 (base64_jpeg, (w,h))。"""
    try:
        img = ImageGrab.grab()             # 常规路径优先（不动线程桌面，最稳）
    except Exception:
        _use_input_desktop()               # 仅失败时兜底切输入桌面重试(锁屏/UAC，需 SYSTEM 才真生效)
        img = ImageGrab.grab()
    w, h = img.size
    if max_w and w > max_w:
        nh = max(1, int(h * max_w / w))
        img = img.resize((max_w, nh))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii"), img.size


def _numi(v, d):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def _h_shot(ctx, msg, send):
    mid = msg.get("id")
    if not _HAVE_GRAB:
        return send(protocol.error(mid, Err.INTERNAL, "截图不可用（缺少 Pillow）", Cap.SCREEN_SHOT))
    p = msg.get("payload") or {}
    # 前端拉取式实时观看会带较小 quality/max_w 以降延迟；单次截图不带则用高清默认
    q = max(10, min(_numi(p.get("quality"), 75), 95))
    mw = max(320, min(_numi(p.get("max_w"), 1920), 3840))
    try:
        b64, (w, h) = _grab_jpeg(quality=q, max_w=mw)
    except Exception as e:
        return send(protocol.error(mid, Err.INTERNAL, "截图失败：%s" % e, Cap.SCREEN_SHOT))
    send(protocol.resp(mid, Cap.SCREEN_SHOT, {"image": b64, "w": w, "h": h, "mime": "image/jpeg"}))


def _h_stream_start(ctx, msg, send):
    mid = msg.get("id")
    if not _HAVE_GRAB:
        return send(protocol.error(mid, Err.INTERNAL, "远程桌面不可用（缺少 Pillow）", Cap.SCREEN_STREAM_START))
    p = msg.get("payload") or {}

    def _num(v, d):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d
    fps = max(0.2, min(_num(p.get("fps"), 5.0), 15.0))   # 上限15；非法值(如误传对象)兜底
    interval = 1.0 / fps
    quality = int(_num(p.get("quality"), 45))
    max_w = int(_num(p.get("max_w"), 1280))

    sid = protocol.new_id()
    HUB.create(sid, Cap.SCREEN_FRAME, send)
    stop = threading.Event()
    with _LOCK:
        _STREAMS[sid] = stop

    def loop():
        while not stop.is_set():
            try:
                b64, _ = _grab_jpeg(quality=quality, max_w=max_w)
                HUB.emit(sid, b64, False)
            except Exception:
                pass
            # 观看端断开后（扇出时其死订阅已被剔除）无人订阅 → 自停，避免 CPU 密集的
            # 截图/JPEG 循环在观看者离开却没发 stop 时永久空转
            if HUB.subscriber_count(sid) == 0:
                break
            stop.wait(interval)
        with _LOCK:
            _STREAMS.pop(sid, None)
        HUB.close(sid)

    threading.Thread(target=loop, name="screen-stream", daemon=True).start()
    send(protocol.resp(mid, Cap.SCREEN_STREAM_START, {"stream_id": sid, "fps": fps}))


def _h_stream_stop(ctx, msg, send):
    mid = msg.get("id")
    sid = (msg.get("payload") or {}).get("stream_id")
    with _LOCK:
        ev = _STREAMS.pop(sid, None)
    if ev:
        ev.set()
    send(protocol.resp(mid, Cap.SCREEN_STREAM_STOP, {"ok": True}))


def _h_input(ctx, msg, send):
    """远程控制：把鼠标/键盘事件注入本机。坐标为相对整屏的归一化 0..1。"""
    mid = msg.get("id")
    if not _user32:
        return send(protocol.error(mid, Err.INTERNAL, "远程控制不可用（仅 Windows）", Cap.SCREEN_INPUT))
    p = msg.get("payload") or {}
    act = p.get("act")
    try:
        if act in ("move", "down", "up", "click") and "x" in p:
            w, h = _screen_wh()
            _user32.SetCursorPos(int(float(p["x"]) * w), int(float(p["y"]) * h))
        if act in ("down", "up", "click"):
            btn = p.get("button", "left")
            dn, up = {"left": ("ld", "lu"), "right": ("rd", "ru"),
                      "middle": ("md", "mu")}.get(btn, ("ld", "lu"))
            if act == "down":
                _user32.mouse_event(_MEF[dn], 0, 0, 0, 0)
            elif act == "up":
                _user32.mouse_event(_MEF[up], 0, 0, 0, 0)
            else:
                _user32.mouse_event(_MEF[dn], 0, 0, 0, 0)
                _user32.mouse_event(_MEF[up], 0, 0, 0, 0)
        elif act == "scroll":
            _user32.mouse_event(_MEF["wheel"], 0, 0, int(p.get("dy", 0)), 0)
        elif act in ("keydown", "keyup"):
            # 分开的按下/抬起：修饰键能保持，组合键(Ctrl+T 等)正常
            code = p.get("code", "")
            key = p.get("key", "")
            vk = _CODE_VK.get(code)
            if vk is not None:
                _user32.keybd_event(vk, 0, 0 if act == "keydown" else 2, 0)
            elif act == "keydown" and len(key) == 1:
                _send_unicode(key)     # 无键码映射的可打印字符：unicode 兜底(仅按下时敲一次)
        elif act == "key":
            # 兼容旧前端：单次字符
            key = p.get("key", "")
            if key in _VK:
                vk = _VK[key]
                _user32.keybd_event(vk, 0, 0, 0)
                _user32.keybd_event(vk, 0, 2, 0)
            elif len(key) == 1:
                _send_unicode(key)
    except Exception:
        pass
    # fire-and-forget：不回 resp（前端用 fire 发送，高频）


cap_router.register_handler(Cap.SCREEN_SHOT, _h_shot)
cap_router.register_handler(Cap.SCREEN_STREAM_START, _h_stream_start)
cap_router.register_handler(Cap.SCREEN_STREAM_STOP, _h_stream_stop)
cap_router.register_handler(Cap.SCREEN_INPUT, _h_input)   # 远程键鼠控制
