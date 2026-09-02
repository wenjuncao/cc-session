# -*- coding: utf-8 -*-
"""
logging_setup.py —— 滚动日志 + stdout/stderr 兜底重定向

职责（CONTRACT §6 T8）：
  setup(tag) 把 root logger 输出到 paths.LOGS_DIR/<tag>.log（RotatingFileHandler，滚动；
  worker=app.log，服务进程=service.log，分开写避免轮转互锁），
  并把 sys.stdout / sys.stderr 重定向到日志——重点兜底「无控制台（pythonw / 打包 windowed）
  时 sys.stdout 为 None」的情况，避免任意模块里的 print() 直接抛 AttributeError。
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .paths import LOGS_DIR

_DONE = False


class _StreamToLogger:
    """把写入当作日志行转发；供替换 sys.stdout / sys.stderr。"""

    def __init__(self, logger, level):
        self._logger = logger
        self._level = level
        self._buf = ""

    def write(self, msg):
        try:
            self._buf += msg
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self._logger.log(self._level, line)
        except Exception:
            # 日志本身不能反过来把程序搞崩
            pass

    def flush(self):
        try:
            if self._buf.strip():
                self._logger.log(self._level, self._buf.rstrip())
            self._buf = ""
        except Exception:
            pass

    def isatty(self):
        return False


def setup(tag="app"):
    """幂等：配置滚动文件日志并接管 stdout/stderr。返回日志文件路径。

    tag 区分进程：服务进程(--service)与业务 worker 必须写【不同文件】。二者共写一个
    app.log 时，Windows 下另一进程持有句柄会让 RotatingFileHandler 的 os.rename 恒失败
    → 日志卡在 2MB 阈值上不再前进 → werkzeug 每条访问日志都堵在该 handler 上
    → 所有 HTTP 响应永不返回(8759 页面打不开、/ping 无响应被判离线)。
    """
    global _DONE
    os.makedirs(LOGS_DIR, exist_ok=True)
    logfile = os.path.join(LOGS_DIR, "%s.log" % tag)
    if _DONE:
        return logfile

    handler = RotatingFileHandler(
        logfile, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 避免重复安装文件 handler（幂等）
    root.handlers = [h for h in root.handlers if not isinstance(h, RotatingFileHandler)]
    root.addHandler(handler)

    # 关掉 werkzeug 的逐条访问日志：它占了日志量的 99%(前端每 6 秒轮询 /api/health)，
    # 2MB 日志一天就写满；更要命的是它位于【每条 HTTP 响应的必经路径】上——写日志一旦
    # 卡住，整个 HTTP 面就一起卡死。降到 WARNING 后仅保留真正的错误，响应路径不再写日志。
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    # 若确实存在真实控制台（开发机 py run.py），额外回显一份，方便调试
    orig = sys.__stdout__
    if orig is not None and getattr(orig, "isatty", lambda: False)():
        con = logging.StreamHandler(orig)
        con.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(con)

    # 兜底重定向：无控制台时 sys.stdout / sys.stderr 可能为 None 或已关闭
    sys.stdout = _StreamToLogger(logging.getLogger("stdout"), logging.INFO)
    sys.stderr = _StreamToLogger(logging.getLogger("stderr"), logging.ERROR)

    _DONE = True
    logging.getLogger("logging_setup").info("日志已就绪 -> %s", logfile)
    return logfile
