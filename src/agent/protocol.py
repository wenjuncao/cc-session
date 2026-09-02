# -*- coding: utf-8 -*-
"""
protocol.py —— 节点间 WebSocket 消息协议契约（全项目冻结，勿改结构）

统一信封（envelope）：所有 WS 消息都是一个 JSON 对象，字段如下：
  id      : str   关联 id（req 生成，resp/stream/ack/error 回填同值）
  type    : str   见 MsgType
  cap     : str   能力名，见 CAP_*（仅 req/resp/stream/event 用）
  seq     : int   stream 分块序号（从 0 递增），保证顺序
  fin     : bool  stream 结束标志
  payload : dict  业务载荷
  ts      : float  发送时间戳（time.time()）

设计来源：后端能力引擎设计意见 §4。所有能力 handler 与前端 wsClient 都以本文件为准。
"""
import time
import uuid


# ---------- 消息类型 ----------
class MsgType:
    REQ = "req"        # 一问一答的请求
    RESP = "resp"      # 对 req 的应答
    STREAM = "stream"  # 服务端→客户端连续分块（终端/命令输出/进度）
    EVENT = "event"    # 非请求触发的主动通知（会话关闭/进程退出/打包完成…）
    ACK = "ack"        # 客户端对 stream 的确认（背压/流控）
    ERROR = "error"    # 错误应答，payload={code,message}


# ---------- 能力名（cap）----------
class Cap:
    # 鉴权握手（见 CONTRACT.md 鉴权时序）
    AUTH = "auth"
    # 交互式终端（PTY）
    TERM_OPEN = "term.open"
    TERM_INPUT = "term.input"
    TERM_RESIZE = "term.resize"
    TERM_CLOSE = "term.close"
    TERM_OUTPUT = "term.output"      # stream 下行
    # 一次性命令
    CMD_EXEC = "cmd.exec"
    # 驱动 claude（headless，复用 night_watch）
    CLAUDE_START = "claude.start"
    CLAUDE_NUDGE = "claude.nudge"    # 信箱式喂提示词
    CLAUDE_STOP = "claude.stop"
    CLAUDE_OUTPUT = "claude.output"  # stream 下行
    # 文件
    FS_LIST = "fs.list"
    FS_TREE = "fs.tree"
    FS_STAT = "fs.stat"
    FS_READ = "fs.read"
    DL_TOKEN = "dl.token"            # 申请下载 token（真正下载走 HTTP 旁路）
    # 进程
    PROC_LIST = "proc.list"
    PROC_START = "proc.start"
    PROC_KILL = "proc.kill"
    # 屏幕（单次截图 / 远程桌面观看，截图轮询低帧率）
    SCREEN_SHOT = "screen.shot"
    SCREEN_STREAM_START = "screen.stream.start"
    SCREEN_STREAM_STOP = "screen.stream.stop"
    SCREEN_FRAME = "screen.frame"    # stream 下行：一帧屏幕 JPEG(base64)
    SCREEN_INPUT = "screen.input"    # 远程控制：鼠标/键盘注入到对端
    # 控制端主动推送升级：控制端存好新版 exe 后下发本指令(含下载 URL + sha256)，
    # 被控端反向 HTTP 拉取、校验、替换自己并静默重启，重启后自动重新接入。
    UPDATE_PUSH = "update.push"


# ---------- 错误码 ----------
class Err:
    AUTH_REQUIRED = "AUTH_REQUIRED"    # 通道未授权
    AUTH_FAILED = "AUTH_FAILED"        # 账密错误
    FORBIDDEN = "FORBIDDEN"            # 已授权但缺该能力位（caps）
    RATE_LIMITED = "RATE_LIMITED"      # 登录/请求限速
    BAD_REQUEST = "BAD_REQUEST"        # 载荷不合法
    NOT_FOUND = "NOT_FOUND"
    PATH_DENIED = "PATH_DENIED"        # 路径穿越/越白名单
    SESSION_LIMIT = "SESSION_LIMIT"    # 会话/并发超上限
    TIMEOUT = "TIMEOUT"
    INTERNAL = "INTERNAL"


def new_id():
    return uuid.uuid4().hex


def req(cap, payload=None, mid=None):
    return {"id": mid or new_id(), "type": MsgType.REQ, "cap": cap,
            "payload": payload or {}, "ts": time.time()}


def resp(mid, cap, payload=None):
    return {"id": mid, "type": MsgType.RESP, "cap": cap,
            "payload": payload or {}, "ts": time.time()}


def stream(mid, cap, seq, data, fin=False):
    return {"id": mid, "type": MsgType.STREAM, "cap": cap,
            "seq": seq, "fin": fin, "payload": {"data": data}, "ts": time.time()}


def event(cap, payload=None):
    return {"id": new_id(), "type": MsgType.EVENT, "cap": cap,
            "payload": payload or {}, "ts": time.time()}


def ack(mid, seq):
    return {"id": mid, "type": MsgType.ACK, "seq": seq, "ts": time.time()}


def error(mid, code, message="", cap=None):
    return {"id": mid, "type": MsgType.ERROR, "cap": cap,
            "payload": {"code": code, "message": message}, "ts": time.time()}
