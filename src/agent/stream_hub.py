# -*- coding: utf-8 -*-
"""
stream_hub.py —— 流分块的订阅扇出 + 环形缓冲(断线回放) + 基于 ack 的限速。

供 terminal_session / cmd_exec / claude_driver 三类下行流共用（由 cap_router 统一接线）。
每条流用 stream_id 标识（终端用 session_id、命令用请求 id、claude 用 claude_session）。
下行消息统一用 protocol.stream(stream_id, cap, seq, data, fin) 构造，seq 由本模块递增。

限速：每条流维护 last_ack（客户端已确认的最大 seq）。未确认块数超过 window 时，
emit 会阻塞等待 ack；为避免不发 ack 的客户端把生产者永久卡死，等待带超时，
超时后放行退化为“纯扇出”。断线回放：环形缓冲保留最近 ring_size 条，attach 时重放。
"""
import collections
import threading

from . import protocol
from .config_loader import getint


class _Channel:
    """单条流的状态：环形缓冲、序号、订阅者、ack 水位、背压条件量。"""

    def __init__(self, cap, ring_size, window, block_timeout):
        self.cap = cap
        self.ring = collections.deque(maxlen=ring_size)  # [(seq, msg)]
        self.seq = 0                 # 下一个待分配的 seq
        self.last_ack = -1           # 客户端已确认的最大 seq
        self.subs = []               # 订阅者 send 可调用列表
        self.closed = False
        self.cond = threading.Condition()
        self.window = window
        self.block_timeout = block_timeout


class StreamHub:
    def __init__(self, ring_size=None, window=None, block_timeout=5.0):
        # 环形缓冲大小与背压窗口可配（capability 段），给合理默认
        self.ring_size = ring_size or getint("capability", "stream_ring", 500)
        self.window = window or getint("capability", "stream_window", 64)
        self.block_timeout = block_timeout
        self._chs = {}               # stream_id -> _Channel
        self._lock = threading.Lock()

    def _ring_for(self, cap):
        """按流类型定环形缓冲深度。终端/命令是文本小块，留 500 条便于断线回放；
        屏幕帧是几百 KB 的 JPEG，留 500 帧要吃掉上百 MB，而且回放旧帧毫无意义
        （观看者只关心最新一帧），故只留 2 帧。"""
        if cap == protocol.Cap.SCREEN_FRAME:
            return 2
        return self.ring_size

    def create(self, stream_id, cap, send=None):
        """建流；可选把首个订阅者(send)直接挂上。重复 create 只补挂订阅者。"""
        with self._lock:
            ch = self._chs.get(stream_id)
            if ch is None:
                ch = _Channel(cap, self._ring_for(cap), self.window, self.block_timeout)
                self._chs[stream_id] = ch
        if send is not None:
            self.attach(stream_id, send, replay=False)
        return stream_id

    def attach(self, stream_id, send, replay=True):
        """新增订阅者；replay=True 时先把环形缓冲里的历史块重放给它（断线回放）。"""
        with self._lock:
            ch = self._chs.get(stream_id)
            if ch is None:
                return False
            snapshot = list(ch.ring) if replay else []
            if send not in ch.subs:
                ch.subs.append(send)
        for _seq, m in snapshot:
            try:
                send(m)
            except Exception:
                pass
        return True

    def detach(self, stream_id, send):
        with self._lock:
            ch = self._chs.get(stream_id)
            if ch and send in ch.subs:
                ch.subs.remove(send)

    def emit(self, stream_id, data, fin=False):
        """
        推一个数据块：分配 seq → 存环形缓冲 → 扇出给所有订阅者。返回 seq（流不存在返回 -1）。
        背压：未确认块数达 window 时阻塞，直到收到 ack 或超时放行。
        """
        with self._lock:
            ch = self._chs.get(stream_id)
        if ch is None:
            return -1
        with ch.cond:
            while (ch.seq - 1 - ch.last_ack) >= ch.window and not ch.closed:
                if not ch.cond.wait(timeout=ch.block_timeout):
                    break  # 超时：放弃背压，退化为纯扇出，避免生产者永久卡死
            if ch.closed:
                return -1
            seq = ch.seq
            ch.seq += 1
            msg = protocol.stream(stream_id, ch.cap, seq, data, fin)
            ch.ring.append((seq, msg))
            subs = list(ch.subs)
        dead = []
        for s in subs:
            try:
                s(msg)
            except Exception:
                dead.append(s)   # 该订阅者(WS)已断：本次扇出失败，事后剔除
        # 剔除死订阅者，避免每次 emit 重试 + 断连重连时 subs 无限堆积（与 attach/detach 同锁）
        if dead:
            with self._lock:
                cur = self._chs.get(stream_id)
                if cur is ch:
                    for s in dead:
                        if s in ch.subs:
                            ch.subs.remove(s)
        return seq

    def channel_count(self):
        """当前活跃流条数（0 = 没有任何终端/命令/屏幕流在跑，供空闲判定）。"""
        with self._lock:
            return len(self._chs)

    def subscriber_count(self, stream_id):
        """当前订阅者数（0 表示流不存在或订阅者都已断开）。供屏幕流按观看者在场自停。"""
        with self._lock:
            ch = self._chs.get(stream_id)
            return len(ch.subs) if ch else 0

    def ack(self, stream_id, seq):
        """客户端确认收到 seq：抬高水位并唤醒可能在背压等待的生产者。"""
        if seq is None:
            return
        with self._lock:
            ch = self._chs.get(stream_id)
        if ch is None:
            return
        with ch.cond:
            if seq > ch.last_ack:
                ch.last_ack = seq
            ch.cond.notify_all()

    def close(self, stream_id):
        """关流：唤醒背压等待者并移除通道。"""
        with self._lock:
            ch = self._chs.pop(stream_id, None)
        if ch is None:
            return
        with ch.cond:
            ch.closed = True
            ch.cond.notify_all()


# 全项目共用单例（cap_router 直接使用）
HUB = StreamHub()
