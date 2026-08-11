# -*- coding: utf-8 -*-
"""
Claude Code Session 管理器
--------------------------------
自动扫描本机所有 Claude Code 的 session 记录（~/.claude/projects/<项目>/<sessionId>.jsonl），
以表格展示：名称、所属项目、执行目录、创建时间、最后更新时间（按更新时间降序）。
- 支持分页（每页 20 个）、项目筛选、关键词搜索、点表头排序
- 点击某个 session：右侧展示关键信息 + "这个 session 做了哪些事"摘要
- [继续执行] 按钮：打开终端(优先 Windows Terminal)，cd 到该 session 的工作目录并 claude --resume

零第三方依赖，直接运行： python session_manager.py
"""

import os
import json
import queue
import shutil
import sys
import hashlib
import subprocess
import threading
from datetime import datetime, timezone

import tkinter as tk
from tkinter import ttk, messagebox

APP_VERSION = "0.0.1"   # 当前程序版本号（打包时自动 +1，如 0.0.1 -> 0.0.2）

PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
HEAD_LINES = 300        # 扫描时读取的头部行数（用于取标题/cwd/创建时间，保证启动快）

# 设置默认值
DEFAULT_PAGE_SIZE = 40
DEFAULT_WIDTH = 1650
DEFAULT_HEIGHT = 930

# 首次启动自动初始化的默认登录密码
DEFAULT_PASSWORD = "Bsd@2026!"

# 日报 / 周报 固定输出目录
DAILY_DIR = r"D:\nas\工作文档\软件研发部\AA绩效管理\2026\日报"
WEEKLY_DIR = r"D:\nas\工作文档\软件研发部\AA绩效管理\2026\周报"

# 开机启动注册表项
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "CCSessionManager"


def app_dir():
    """项目根目录：打包后为 exe 所在目录；源码运行为 src 的上级目录（项目根）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


CONFIG_PATH = os.path.join(app_dir(), "config", "config.json")


def resource_path(name):
    """只读静态资源路径：打包后在 _MEIPASS 解压目录，源码运行在项目 assets 目录。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, name)
    return os.path.join(app_dir(), "assets", name)


def set_app_icon(win):
    """给窗口设置应用图标（logo.ico），default=True 让子 Toplevel 也复用。"""
    ico = resource_path("logo.ico")
    if os.path.exists(ico):
        try:
            win.iconbitmap(default=ico)
        except Exception:
            pass


def read_config(path=CONFIG_PATH):
    """读取整个 config.json（auth + settings 合一）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_section(path, section, data):
    """只更新 config.json 中指定分区，保留其它分区不动。"""
    full = read_config(path)
    full[section] = data
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(full, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ============================================================
# 数据层
# ============================================================
def parse_iso(ts):
    """把 '2026-08-06T02:15:49.437Z' 解析为带时区的 datetime，失败返回 None。"""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def fmt_time(dt):
    """datetime -> 本地时间字符串。"""
    if dt is None:
        return "-"
    try:
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


def extract_text(content):
    """从消息 content(字符串 或 block 列表) 中提取纯文本。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return " ".join(parts).strip()
    return ""


def scan_one(file_path, project_dir):
    """读取单个 session 文件的头部，提取轻量元数据（不做全量解析，保证启动快）。"""
    title = ""
    first_user = ""
    cwd = ""
    git_branch = ""
    version = ""
    created = None

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= HEAD_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue

                rtype = rec.get("type")

                # 会话名称：优先 ai-title 记录
                if rtype == "ai-title" and rec.get("aiTitle"):
                    title = rec["aiTitle"].strip()

                # 元信息（出现在消息记录上）
                if not cwd and rec.get("cwd"):
                    cwd = rec["cwd"]
                if not git_branch and rec.get("gitBranch"):
                    git_branch = rec["gitBranch"]
                if not version and rec.get("version"):
                    version = rec["version"]

                # 创建时间：第一条带 timestamp 的记录
                if created is None and rec.get("timestamp"):
                    created = parse_iso(rec["timestamp"])

                # 兜底标题：第一条真实用户消息
                if not first_user and rtype == "user":
                    msg = rec.get("message") or {}
                    txt = extract_text(msg.get("content"))
                    # 跳过命令/系统类噪声
                    if txt and not txt.startswith("<") and "command-name" not in txt:
                        first_user = txt
    except Exception:
        pass

    session_id = os.path.splitext(os.path.basename(file_path))[0]

    if not title:
        title = first_user[:60] if first_user else session_id[:8]

    if created is None:
        try:
            created = datetime.fromtimestamp(os.path.getctime(file_path), tz=timezone.utc)
        except Exception:
            created = None

    # 更新时间：用文件修改时间（每次会话更新都会写文件，准确且快）
    try:
        updated = datetime.fromtimestamp(os.path.getmtime(file_path), tz=timezone.utc)
    except Exception:
        updated = None

    try:
        size = os.path.getsize(file_path)
    except Exception:
        size = 0

    # 所属项目：优先用 cwd 的最后一级目录，否则用项目目录名
    if cwd:
        project = os.path.basename(cwd.rstrip("\\/")) or cwd
    else:
        project = project_dir

    return {
        "session_id": session_id,
        "title": title,
        "project": project,
        "project_dir": project_dir,
        "cwd": cwd,
        "git_branch": git_branch,
        "version": version,
        "created": created,
        "updated": updated,
        "size": size,
        "file_path": file_path,
    }


def scan_sessions():
    """扫描全部项目下的顶层 session 文件（排除 subagents 子代理日志）。"""
    sessions = []
    if not os.path.isdir(PROJECTS_DIR):
        return sessions
    for project_dir in os.listdir(PROJECTS_DIR):
        pdir = os.path.join(PROJECTS_DIR, project_dir)
        if not os.path.isdir(pdir):
            continue
        for name in os.listdir(pdir):
            if not name.endswith(".jsonl"):
                continue
            fpath = os.path.join(pdir, name)
            if not os.path.isfile(fpath):
                continue
            sessions.append(scan_one(fpath, project_dir))
    return sessions


def summarize(session):
    """全量解析单个 session，统计它做了哪些事（懒加载，仅在选中时调用）。"""
    user_msgs = []
    tool_counts = {}
    files_touched = {}
    msg_count = 0
    first_ts = None
    last_ts = None

    try:
        with open(session["file_path"], "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue

                ts = parse_iso(rec.get("timestamp"))
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                rtype = rec.get("type")
                if rtype not in ("user", "assistant"):
                    continue
                msg_count += 1
                msg = rec.get("message") or {}
                content = msg.get("content")

                if rtype == "user":
                    txt = extract_text(content)
                    if txt and not txt.startswith("<") and "command-name" not in txt:
                        user_msgs.append(txt)
                elif rtype == "assistant" and isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            tname = b.get("name", "?")
                            tool_counts[tname] = tool_counts.get(tname, 0) + 1
                            inp = b.get("input") or {}
                            fp = inp.get("file_path") or inp.get("path")
                            if fp:
                                files_touched[fp] = files_touched.get(fp, 0) + 1
    except Exception as e:
        return {"error": str(e)}

    duration = None
    if first_ts and last_ts:
        duration = last_ts - first_ts

    return {
        "user_msgs": user_msgs,
        "tool_counts": tool_counts,
        "files_touched": files_touched,
        "msg_count": msg_count,
        "tool_total": sum(tool_counts.values()),
        "duration": duration,
    }


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(td):
    if td is None:
        return "-"
    secs = int(td.total_seconds())
    if secs < 60:
        return f"{secs} 秒"
    if secs < 3600:
        return f"{secs // 60} 分 {secs % 60} 秒"
    return f"{secs // 3600} 时 {(secs % 3600) // 60} 分"


# ============================================================
# 终端唤起：在指定目录打开终端并执行 claude 命令
# ============================================================
def launch_terminal(cwd, claude_cmd):
    """在 cwd 打开终端(优先 Windows Terminal, 回退 cmd)并执行 claude_cmd。"""
    if not cwd or not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    wt = shutil.which("wt") or shutil.which("wt.exe")
    if wt:
        subprocess.Popen([wt, "-d", cwd, "cmd", "/k", claude_cmd])
    else:
        subprocess.Popen(
            ["cmd", "/c", "start", "", "cmd", "/k",
             f'cd /d "{cwd}" && {claude_cmd}']
        )


def resume_session(session):
    sid = session["session_id"]
    cwd = session["cwd"]
    if not cwd or not os.path.isdir(cwd):
        cwd = os.path.join(PROJECTS_DIR, session["project_dir"])
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    try:
        launch_terminal(cwd, f"claude --resume {sid} --dangerously-skip-permissions")
        return True, cwd
    except Exception as e:
        return False, str(e)


def generate_report(kind):
    """调用 daily-report / weekly-report 技能生成报告。kind: 'daily' | 'weekly'。"""
    if kind == "daily":
        out_dir, prompt = DAILY_DIR, "生成今日日报"
    else:
        out_dir, prompt = WEEKLY_DIR, "生成本周周报"
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        pass
    # daily-report 写到 cwd -> 在日报目录启动；weekly-report 写到固定路径 -> cwd 用周报目录亦可
    cwd = out_dir if os.path.isdir(out_dir) else os.path.expanduser("~")
    try:
        launch_terminal(cwd, f'claude --dangerously-skip-permissions "{prompt}"')
        return True, out_dir
    except Exception as e:
        return False, str(e)


# ============================================================
# 登录 / 密码管理
# ============================================================
class AuthManager:
    """本地密码认证：PBKDF2-HMAC-SHA256 加盐哈希，保存在 config.json。"""

    ITERATIONS = 120_000

    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self._data = read_config(path).get("auth", {})

    def _save(self):
        write_section(self.path, "auth", self._data)

    def exists(self):
        return bool(self._data.get("hash") and self._data.get("salt"))

    @staticmethod
    def _hash(password, salt_bytes):
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt_bytes, AuthManager.ITERATIONS
        ).hex()

    def verify(self, password):
        if not self.exists():
            return False
        salt = bytes.fromhex(self._data["salt"])
        return self._hash(password, salt) == self._data["hash"]

    def set_password(self, password):
        salt = os.urandom(16)
        self._data = {"salt": salt.hex(), "hash": self._hash(password, salt)}
        self._save()


class PasswordDialog(tk.Toplevel):
    """通用密码弹窗。mode='login' 登录；mode='set' 设置/修改密码。"""

    def __init__(self, parent, auth, mode="login", first=False):
        super().__init__(parent)
        self.auth = auth
        self.mode = mode
        self.first = first
        self.result = False

        self.title("登录" if mode == "login" else ("首次使用 · 设置密码" if first else "修改密码"))
        self.resizable(False, False)
        # 仅当父窗口可见时才设为 transient；否则(登录前 root 处于 withdraw)
        # transient 会被随 master 一起隐藏，导致对话框不可见。
        if parent.winfo_viewable():
            self.transient(parent)
        self.grab_set()

        frm = ttk.Frame(self, padding=16)
        frm.pack(fill=tk.BOTH, expand=True)

        self.old_var = tk.StringVar()
        self.pw_var = tk.StringVar()
        self.pw2_var = tk.StringVar()

        row = 0
        if mode == "login":
            ttk.Label(frm, text="请输入密码：").grid(row=row, column=0, sticky=tk.W, pady=4)
            e = ttk.Entry(frm, textvariable=self.pw_var, show="*", width=24)
            e.grid(row=row, column=1, pady=4)
            e.focus_set()
        else:
            if not first:
                ttk.Label(frm, text="原密码：").grid(row=row, column=0, sticky=tk.W, pady=4)
                e0 = ttk.Entry(frm, textvariable=self.old_var, show="*", width=24)
                e0.grid(row=row, column=1, pady=4)
                e0.focus_set()
                row += 1
            ttk.Label(frm, text="新密码：").grid(row=row, column=0, sticky=tk.W, pady=4)
            e1 = ttk.Entry(frm, textvariable=self.pw_var, show="*", width=24)
            e1.grid(row=row, column=1, pady=4)
            if first:
                e1.focus_set()
            row += 1
            ttk.Label(frm, text="确认新密码：").grid(row=row, column=0, sticky=tk.W, pady=4)
            ttk.Entry(frm, textvariable=self.pw2_var, show="*", width=24).grid(
                row=row, column=1, pady=4)
        row += 1

        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="确定", command=self._on_ok).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="取消", command=self._on_cancel).pack(side=tk.LEFT, padx=6)

        self.bind("<Return>", lambda e: self._on_ok())
        self.bind("<Escape>", lambda e: self._on_cancel())

        # 居中：父窗口可见则居中于父窗口，否则居中于屏幕
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        if parent.winfo_viewable() and parent.winfo_width() > 1:
            px = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
            py = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        else:
            px = (self.winfo_screenwidth() - w) // 2
            py = (self.winfo_screenheight() - h) // 2
        self.geometry(f"+{max(px, 0)}+{max(py, 0)}")

        # 强制置顶显示并抢占焦点，避免登录窗被隐藏/被其他窗口遮挡
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def _on_ok(self):
        if self.mode == "login":
            if self.auth.verify(self.pw_var.get()):
                self.result = True
                self.destroy()
            else:
                messagebox.showerror("登录失败", "密码错误", parent=self)
                self.pw_var.set("")
            return
        # 设置 / 修改
        if not self.first and not self.auth.verify(self.old_var.get()):
            messagebox.showerror("错误", "原密码不正确", parent=self)
            return
        pw = self.pw_var.get()
        if len(pw) < 4:
            messagebox.showwarning("提示", "密码至少 4 位", parent=self)
            return
        if pw != self.pw2_var.get():
            messagebox.showwarning("提示", "两次输入的密码不一致", parent=self)
            return
        self.auth.set_password(pw)
        self.result = True
        messagebox.showinfo("成功", "密码已设置", parent=self)
        self.destroy()

    def _on_cancel(self):
        self.result = False
        self.destroy()


def run_dialog(parent, auth, mode="login", first=False):
    dlg = PasswordDialog(parent, auth, mode=mode, first=first)
    parent.wait_window(dlg)
    return dlg.result


# ============================================================
# 设置管理 / 开机启动
# ============================================================
class SettingsManager:
    """应用设置：每页数量、默认宽高、开机启动、以及 session 重命名覆盖。存 settings.json。"""

    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self._data = read_config(path).get("settings", {})

    def save(self):
        write_section(self.path, "settings", self._data)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value

    @property
    def page_size(self):
        try:
            return max(1, int(self._data.get("page_size", DEFAULT_PAGE_SIZE)))
        except Exception:
            return DEFAULT_PAGE_SIZE

    @property
    def width(self):
        try:
            return max(800, int(self._data.get("width", DEFAULT_WIDTH)))
        except Exception:
            return DEFAULT_WIDTH

    @property
    def height(self):
        try:
            return max(600, int(self._data.get("height", DEFAULT_HEIGHT)))
        except Exception:
            return DEFAULT_HEIGHT

    @property
    def auto_start(self):
        return bool(self._data.get("auto_start", False))

    @property
    def require_password(self):
        # 默认开启密码校验；关闭后登录不再需要密码
        return bool(self._data.get("require_password", True))

    # ---- session 置顶 ----
    def pins(self):
        return self._data.get("pins", [])

    def is_pinned(self, session_id):
        return session_id in self._data.get("pins", [])

    def toggle_pin(self, session_id):
        p = self._data.get("pins", [])
        if session_id in p:
            p.remove(session_id)
        else:
            p.append(session_id)
        self._data["pins"] = p
        self.save()

    # ---- session 重命名覆盖 ----
    def renames(self):
        return self._data.get("renames", {})

    def set_rename(self, session_id, name):
        r = self._data.get("renames", {})
        if name:
            r[session_id] = name
        else:
            r.pop(session_id, None)
        self._data["renames"] = r
        self.save()


def _run_command():
    """开机启动要执行的命令行：打包后指向 exe，源码运行指向 pythonw 脚本。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = shutil.which("pythonw") or "pythonw"
    return f'"{pyw}" "{os.path.abspath(__file__)}"'


def set_auto_start(enable):
    """写/删 HKCU Run 注册表项，实现开机自启。返回 (ok, err)。"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                             winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, _run_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True, ""
    except Exception as e:
        return False, str(e)


class SettingsDialog(tk.Toplevel):
    """设置对话框：开机启动 / 每页数量 / 默认宽高。"""

    def __init__(self, parent, settings):
        super().__init__(parent)
        self.settings = settings
        self.result = False
        self.title("设置")
        self.resizable(False, False)
        if parent.winfo_viewable():
            self.transient(parent)
        self.grab_set()

        frm = ttk.Frame(self, padding=16)
        frm.pack(fill=tk.BOTH, expand=True)

        self.auto_var = tk.BooleanVar(value=settings.auto_start)
        self.pwd_var = tk.BooleanVar(value=settings.require_password)
        self.page_var = tk.StringVar(value=str(settings.page_size))
        self.w_var = tk.StringVar(value=str(settings.width))
        self.h_var = tk.StringVar(value=str(settings.height))

        ttk.Checkbutton(frm, text="开机自动启动", variable=self.auto_var).grid(
            row=0, column=0, columnspan=2, sticky=tk.W, pady=6)

        ttk.Checkbutton(frm, text="登录时需要密码校验（取消勾选后启动免密码）",
                        variable=self.pwd_var).grid(
            row=1, column=0, columnspan=2, sticky=tk.W, pady=6)

        ttk.Label(frm, text="每页显示数量：").grid(row=2, column=0, sticky=tk.W, pady=6)
        ttk.Entry(frm, textvariable=self.page_var, width=12).grid(row=2, column=1, sticky=tk.W)

        ttk.Label(frm, text="默认宽度：").grid(row=3, column=0, sticky=tk.W, pady=6)
        ttk.Entry(frm, textvariable=self.w_var, width=12).grid(row=3, column=1, sticky=tk.W)

        ttk.Label(frm, text="默认高度：").grid(row=4, column=0, sticky=tk.W, pady=6)
        ttk.Entry(frm, textvariable=self.h_var, width=12).grid(row=4, column=1, sticky=tk.W)

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="保存", command=self._on_save).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side=tk.LEFT, padx=6)

        self.update_idletasks()
        px = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(px, 0)}+{max(py, 0)}")
        self.lift()
        self.focus_force()

    def _on_save(self):
        try:
            page = int(self.page_var.get())
            w = int(self.w_var.get())
            h = int(self.h_var.get())
            assert page >= 1 and w >= 800 and h >= 600
        except Exception:
            messagebox.showwarning("提示", "请输入有效数字（每页≥1，宽≥800，高≥600）", parent=self)
            return
        self.settings.set("page_size", page)
        self.settings.set("width", w)
        self.settings.set("height", h)
        self.settings.set("auto_start", bool(self.auto_var.get()))
        self.settings.set("require_password", bool(self.pwd_var.get()))
        self.settings.save()
        ok, err = set_auto_start(self.auto_var.get())
        if not ok:
            messagebox.showwarning("开机启动设置失败", err, parent=self)
        self.result = True
        self.destroy()


# ============================================================
# UI 层
# ============================================================
class SessionManagerApp:
    COLUMNS = [
        ("title", "会话名称", 300),
        ("project", "所属项目", 130),
        ("cwd", "执行目录", 260),
        ("created", "创建时间", 150),
        ("updated", "最后更新", 150),
    ]

    def __init__(self, root, auth=None, settings=None):
        self.root = root
        self.auth = auth
        self.settings = settings or SettingsManager()
        self.root.title(f"Claude Code Session 管理器  v{APP_VERSION}")
        self.root.geometry(f"{self.settings.width}x{self.settings.height}")

        self.page_size = self.settings.page_size
        self.all_sessions = []      # 全部
        self.filtered = []          # 筛选后
        self.page = 0               # 当前页(0-based)
        self.sort_key = "updated"
        self.sort_reverse = True
        self.selected = None
        self._queue = queue.Queue()   # 后台线程 -> 主线程 的消息队列

        self._build_ui()
        self.root.after(120, self._set_sash)   # 默认左右 2:1
        self.root.after(100, self._poll_queue)
        self.refresh()

    def _poll_queue(self):
        """主线程轮询后台线程结果，避免跨线程操作 Tk。"""
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "scan_done":
                    self._on_scan_done(payload)
                elif kind == "detail":
                    s, summary = payload
                    self._render_detail(s, summary)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _set_sash(self):
        """把分隔条设到 2/3 处，实现左:右 = 2:1 的默认宽度。"""
        w = self.paned.winfo_width()
        if w <= 1:                       # 窗口尚未完成布局，稍后重试
            self.root.after(50, self._set_sash)
            return
        self.paned.sashpos(0, int(w * 2 / 3))

    # ---------- 构建界面 ----------
    def _build_ui(self):
        self.paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.paned.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(self.paned)
        right = ttk.Frame(self.paned)
        self.paned.add(left, weight=2)    # 左:右 = 2:1
        self.paned.add(right, weight=1)

        # --- 工具栏 ---
        toolbar = ttk.Frame(left)
        toolbar.pack(fill=tk.X, padx=6, pady=6)
        ttk.Button(toolbar, text="🔄 刷新", command=self.refresh).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="📅 生成日报", command=lambda: self.on_gen_report("daily")).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="🗓 生成周报", command=lambda: self.on_gen_report("weekly")).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="⚙ 设置", command=self.on_settings).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="🔑 修改密码", command=self.on_change_password).pack(side=tk.LEFT, padx=4)
        ttk.Label(toolbar, text="  项目:").pack(side=tk.LEFT)
        self.project_var = tk.StringVar(value="全部项目")
        self._all_projects = []          # 全量项目名（供输入时模糊过滤下拉候选）
        self.project_combo = ttk.Combobox(toolbar, textvariable=self.project_var,
                                           width=22)   # 可输入：支持模糊查询
        self.project_combo.pack(side=tk.LEFT, padx=4)
        self.project_combo.bind("<<ComboboxSelected>>", lambda e: self.apply_filter())
        self.project_combo.bind("<KeyRelease>", self.on_project_type)  # 边输入边模糊过滤
        ttk.Label(toolbar, text="  搜索:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        entry = ttk.Entry(toolbar, textvariable=self.search_var, width=20)
        entry.pack(side=tk.LEFT, padx=4)
        entry.bind("<KeyRelease>", lambda e: self.apply_filter())
        ttk.Label(toolbar, text="  执行目录:").pack(side=tk.LEFT)
        self.dir_var = tk.StringVar(value="全部执行目录")
        self.dir_combo = ttk.Combobox(toolbar, textvariable=self.dir_var,
                                       state="readonly", width=30)  # 拉下框选择
        self.dir_combo.pack(side=tk.LEFT, padx=4)
        self.dir_combo.bind("<<ComboboxSelected>>", lambda e: self.apply_filter())
        ttk.Label(toolbar, text=f"v{APP_VERSION}", foreground="#888").pack(side=tk.RIGHT, padx=(8, 4))
        self.count_label = ttk.Label(toolbar, text="")
        self.count_label.pack(side=tk.RIGHT)

        # --- 表格 ---
        table_frame = ttk.Frame(left)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=6)
        cols = [c[0] for c in self.COLUMNS]
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings")
        for key, label, width in self.COLUMNS:
            self.tree.heading(key, text=label,
                              command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, anchor=tk.W)
        vsb = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", self.on_double_click)     # 双击继续执行
        self.tree.bind("<Button-3>", self.on_right_click)      # 右键菜单

        # 右键上下文菜单
        self.ctx_menu = tk.Menu(self.tree, tearoff=0)
        self.ctx_menu.add_command(label="✏ 修改名称", command=self.on_rename)
        self.ctx_menu.add_command(label="📌 固定置顶", command=self.on_toggle_pin)
        self.pin_index = 1          # "固定置顶" 在菜单中的索引，用于弹出时动态切换标签
        self.ctx_menu.add_command(label="📂 打开目录", command=self.on_open_dir)
        self.ctx_menu.add_command(label="🚀 继续执行", command=self.on_resume)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label="🗑 删除会话", command=self.on_delete)

        # --- 分页栏 ---
        pager = ttk.Frame(left)
        pager.pack(fill=tk.X, padx=6, pady=6)
        self.prev_btn = ttk.Button(pager, text="◀ 上一页", command=self.prev_page)
        self.prev_btn.pack(side=tk.LEFT)
        self.page_label = ttk.Label(pager, text="")
        self.page_label.pack(side=tk.LEFT, padx=12)
        self.next_btn = ttk.Button(pager, text="下一页 ▶", command=self.next_page)
        self.next_btn.pack(side=tk.LEFT)

        # --- 右侧详情 ---
        ttk.Label(right, text="会话详情", font=("", 11, "bold")).pack(anchor=tk.W, padx=8, pady=(8, 2))
        self.detail = tk.Text(right, wrap=tk.WORD, font=("Consolas", 10), state=tk.DISABLED)
        dsb = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.detail.yview)
        self.detail.configure(yscrollcommand=dsb.set)
        self.detail.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=4)
        dsb.pack(side=tk.LEFT, fill=tk.Y)
        self.resume_btn = ttk.Button(right, text="🚀 继续执行 (claude --resume)",
                                     command=self.on_resume, state=tk.DISABLED)
        self.resume_btn.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=8)

    # ---------- 数据加载 ----------
    def refresh(self):
        self._set_detail("正在扫描 session 记录，请稍候...")
        self.count_label.config(text="加载中...")
        threading.Thread(target=self._scan_thread, daemon=True).start()

    def _scan_thread(self):
        sessions = scan_sessions()
        self._queue.put(("scan_done", sessions))

    def _on_scan_done(self, sessions):
        # 应用用户的重命名覆盖
        renames = self.settings.renames()
        for s in sessions:
            if s["session_id"] in renames:
                s["title"] = renames[s["session_id"]]
        self.all_sessions = sessions
        projects = sorted({s["project"] for s in sessions})
        self._all_projects = projects
        self.project_combo["values"] = ["全部项目"] + projects
        if self.project_var.get() not in self.project_combo["values"]:
            self.project_var.set("全部项目")
        # 执行目录下拉候选：去重排序
        dirs = sorted({s["cwd"] for s in sessions if s.get("cwd")})
        self.dir_combo["values"] = ["全部执行目录"] + dirs
        if self.dir_var.get() not in self.dir_combo["values"]:
            self.dir_var.set("全部执行目录")
        self._set_detail("← 请在左侧选择一个 session 查看详情")
        self.apply_filter()

    # ---------- 筛选 / 排序 / 分页 ----------
    def on_project_type(self, event=None):
        """项目框输入时：按已输入内容模糊过滤下拉候选，并实时筛选列表。"""
        if event is not None and event.keysym in (
                "Up", "Down", "Left", "Right", "Return", "Escape"):
            return  # 方向/回车/取消键交给下拉导航，不重置候选
        text = self.project_var.get().strip().lower()
        if text and text != "全部项目":
            cand = [p for p in self._all_projects if text in p.lower()]
        else:
            cand = list(self._all_projects)
        self.project_combo["values"] = ["全部项目"] + cand
        self.apply_filter()

    def apply_filter(self):
        proj = self.project_var.get().strip()
        kw = self.search_var.get().strip().lower()
        dir_sel = self.dir_var.get().strip()
        result = []
        for s in self.all_sessions:
            # 项目：模糊子串匹配（忽略大小写）；空或“全部项目”不过滤
            if proj and proj != "全部项目" and proj.lower() not in (s["project"] or "").lower():
                continue
            # 执行目录：下拉精确选择；“全部执行目录”不过滤
            if dir_sel and dir_sel != "全部执行目录" and (s["cwd"] or "") != dir_sel:
                continue
            if kw:
                blob = f"{s['title']} {s['cwd']} {s['session_id']} {s['project']}".lower()
                if kw not in blob:
                    continue
            result.append(s)
        self.filtered = result
        self.page = 0
        self._sort_filtered()
        self.render_page()

    def sort_by(self, key):
        if self.sort_key == key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key = key
            self.sort_reverse = key in ("created", "updated")  # 时间默认降序
        self._sort_filtered()
        self.page = 0
        self.render_page()

    def _sort_filtered(self):
        key = self.sort_key

        def sort_val(s):
            v = s.get(key)
            if key in ("created", "updated"):
                return v or datetime.min.replace(tzinfo=timezone.utc)
            return str(v or "").lower()

        self.filtered.sort(key=sort_val, reverse=self.sort_reverse)
        # 置顶项浮到最前（稳定排序，保留组内原有顺序）
        pins = self.settings.pins()
        self.filtered.sort(key=lambda s: 0 if s["session_id"] in pins else 1)

    def total_pages(self):
        return max(1, (len(self.filtered) + self.page_size - 1) // self.page_size)

    def render_page(self):
        self.tree.delete(*self.tree.get_children())
        start = self.page * self.page_size
        page_items = self.filtered[start:start + self.page_size]
        self._row_map = {}
        for s in page_items:
            title = ("📌 " + s["title"]) if self.settings.is_pinned(s["session_id"]) else s["title"]
            iid = self.tree.insert("", tk.END, values=(
                title,
                s["project"],
                s["cwd"] or "-",
                fmt_time(s["created"]),
                fmt_time(s["updated"]),
            ))
            self._row_map[iid] = s

        tp = self.total_pages()
        self.page_label.config(text=f"第 {self.page + 1} / {tp} 页")
        self.count_label.config(
            text=f"共 {len(self.filtered)} 个 session（全机 {len(self.all_sessions)} 个）")
        self.prev_btn.config(state=tk.NORMAL if self.page > 0 else tk.DISABLED)
        self.next_btn.config(state=tk.NORMAL if self.page < tp - 1 else tk.DISABLED)

    def prev_page(self):
        if self.page > 0:
            self.page -= 1
            self.render_page()

    def next_page(self):
        if self.page < self.total_pages() - 1:
            self.page += 1
            self.render_page()

    # ---------- 选中 / 详情 ----------
    def on_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        s = self._row_map.get(sel[0])
        if not s:
            return
        self.selected = s
        self.resume_btn.config(state=tk.NORMAL)
        self._set_detail("正在读取会话内容...")
        threading.Thread(target=self._detail_thread, args=(s,), daemon=True).start()

    def _detail_thread(self, s):
        summary = summarize(s)
        self._queue.put(("detail", (s, summary)))

    def _render_detail(self, s, summary):
        if self.selected is not s:   # 用户已切换选择
            return
        lines = []
        lines.append("【关键信息】")
        lines.append(f"  会话名称 : {s['title']}")
        lines.append(f"  Session ID: {s['session_id']}")
        lines.append(f"  所属项目 : {s['project']}")
        lines.append(f"  执行目录 : {s['cwd'] or '-'}")
        lines.append(f"  Git 分支 : {s['git_branch'] or '-'}")
        lines.append(f"  Claude 版本: {s['version'] or '-'}")
        lines.append(f"  创建时间 : {fmt_time(s['created'])}")
        lines.append(f"  最后更新 : {fmt_time(s['updated'])}")
        lines.append(f"  文件大小 : {human_size(s['size'])}")

        if "error" in summary:
            lines.append(f"\n(读取会话内容失败: {summary['error']})")
            self._set_detail("\n".join(lines))
            return

        lines.append(f"  消息轮数 : {summary['msg_count']}")
        lines.append(f"  工具调用 : {summary['tool_total']} 次")
        lines.append(f"  会话时长 : {human_duration(summary['duration'])}")

        lines.append("\n【这个 session 做了哪些事】")
        if summary["tool_counts"]:
            tc = sorted(summary["tool_counts"].items(), key=lambda x: -x[1])
            lines.append("  · 操作统计: " + "、".join(f"{k}×{v}" for k, v in tc))
        else:
            lines.append("  · 操作统计: (无工具调用)")

        if summary["files_touched"]:
            ft = sorted(summary["files_touched"].items(), key=lambda x: -x[1])[:10]
            lines.append("  · 涉及文件 (Top 10):")
            for fp, cnt in ft:
                lines.append(f"      - {fp}  (×{cnt})")

        if summary["user_msgs"]:
            lines.append(f"\n【用户提出的需求 (共 {len(summary['user_msgs'])} 条)】")
            for i, m in enumerate(summary["user_msgs"][:15], 1):
                one = " ".join(m.split())
                if len(one) > 120:
                    one = one[:120] + "…"
                lines.append(f"  {i}. {one}")
            if len(summary["user_msgs"]) > 15:
                lines.append(f"  ... 还有 {len(summary['user_msgs']) - 15} 条")

        self._set_detail("\n".join(lines))

    def _set_detail(self, text):
        self.detail.config(state=tk.NORMAL)
        self.detail.delete("1.0", tk.END)
        self.detail.insert("1.0", text)
        self.detail.config(state=tk.DISABLED)

    # ---------- 继续执行 ----------
    def on_resume(self):
        if not self.selected:
            return
        ok, info = resume_session(self.selected)
        if ok:
            messagebox.showinfo("已启动",
                                f"已在新终端中恢复会话:\n\n目录: {info}\n"
                                f"命令: claude --resume {self.selected['session_id']} "
                                f"--dangerously-skip-permissions")
        else:
            messagebox.showerror("启动失败", f"无法打开终端:\n{info}")

    # ---------- 打开执行目录 ----------
    def on_open_dir(self):
        if not self.selected:
            return
        cwd = self.selected.get("cwd", "")
        if not cwd or not os.path.isdir(cwd):
            messagebox.showwarning("目录不存在", f"该会话的执行目录不存在或已删除:\n{cwd or '(空)'}")
            return
        try:
            os.startfile(cwd)
        except Exception as e:
            messagebox.showerror("打开失败", f"无法打开目录:\n{cwd}\n\n{e}")

    # ---------- 删除会话 ----------
    def on_delete(self):
        if not self.selected:
            return
        s = self.selected
        if not messagebox.askyesno(
                "确认删除",
                f"确定要删除该会话吗？此操作不可恢复。\n\n"
                f"名称: {s['title']}\n"
                f"目录: {s.get('cwd', '(空)')}\n"
                f"文件: {s['file_path']}"):
            return
        try:
            os.remove(s["file_path"])
        except FileNotFoundError:
            pass          # 文件已不存在，视为已删除
        except Exception as e:
            messagebox.showerror("删除失败", f"无法删除会话文件:\n{s['file_path']}\n\n{e}")
            return
        # 清理重命名覆盖，并从内存列表移除，避免重复扫描后残留
        self.settings.set_rename(s["session_id"], "")
        self.all_sessions = [x for x in self.all_sessions if x is not s]
        self.selected = None
        self._set_detail("← 会话已删除，请在左侧选择其它 session")
        self.apply_filter()

    # ---------- 修改密码 ----------
    def on_change_password(self):
        if not self.auth:
            messagebox.showinfo("提示", "未启用登录，无需修改密码")
            return
        run_dialog(self.root, self.auth, mode="set", first=False)

    # ---------- 双击：继续执行 ----------
    def on_double_click(self, event):
        if not self.tree.identify_row(event.y):   # 双击空白区域忽略
            return
        if self.selected:
            self.on_resume()

    # ---------- 右键：重命名 ----------
    def on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)          # 右键选中该行
        s = self._row_map.get(row)
        if s:                                 # 按当前置顶状态切换菜单文案
            pinned = self.settings.is_pinned(s["session_id"])
            self.ctx_menu.entryconfig(
                self.pin_index, label="📌 取消置顶" if pinned else "📌 固定置顶")
        self.ctx_menu.tk_popup(event.x_root, event.y_root)

    def on_toggle_pin(self):
        if not self.selected:
            return
        self.settings.toggle_pin(self.selected["session_id"])
        self._sort_filtered()
        self.render_page()

    def on_rename(self):
        if not self.selected:
            return
        s = self.selected
        dlg = RenameDialog(self.root, s["title"])
        self.root.wait_window(dlg)
        if dlg.result is None:
            return
        new_name = dlg.result.strip()
        if not new_name:
            return
        s["title"] = new_name
        self.settings.set_rename(s["session_id"], new_name)   # 持久化覆盖
        self.render_page()
        # 刷新详情面板
        self.selected = s
        threading.Thread(target=self._detail_thread, args=(s,), daemon=True).start()

    # ---------- 设置 ----------
    def on_settings(self):
        dlg = SettingsDialog(self.root, self.settings)
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        # 应用新设置
        self.page_size = self.settings.page_size
        self.root.geometry(f"{self.settings.width}x{self.settings.height}")
        self.page = 0
        self.render_page()

    # ---------- 生成日报 / 周报 ----------
    def on_gen_report(self, kind):
        name = "日报" if kind == "daily" else "周报"
        ok, info = generate_report(kind)
        if ok:
            messagebox.showinfo(
                "已启动",
                f"已打开终端调用「{name}」技能。\n\n"
                f"生成后{name}将保存在：\n{info}\n\n"
                f"请在终端中查看进度（首次可能需要片刻）。")
        else:
            messagebox.showerror("启动失败", f"无法打开终端:\n{info}")


class RenameDialog(tk.Toplevel):
    """重命名会话名称的小弹窗。"""

    def __init__(self, parent, current):
        super().__init__(parent)
        self.result = None
        self.title("修改会话名称")
        self.resizable(False, False)
        if parent.winfo_viewable():
            self.transient(parent)
        self.grab_set()

        frm = ttk.Frame(self, padding=16)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text="新名称：").grid(row=0, column=0, sticky=tk.W, pady=6)
        self.var = tk.StringVar(value=current)
        e = ttk.Entry(frm, textvariable=self.var, width=40)
        e.grid(row=0, column=1, pady=6)
        e.focus_set()
        e.select_range(0, tk.END)

        btns = ttk.Frame(frm)
        btns.grid(row=1, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="确定", command=self._ok).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side=tk.LEFT, padx=6)
        self.bind("<Return>", lambda ev: self._ok())
        self.bind("<Escape>", lambda ev: self.destroy())

        self.update_idletasks()
        px = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(px, 0)}+{max(py, 0)}")
        self.lift()
        self.focus_force()

    def _ok(self):
        self.result = self.var.get()
        self.destroy()


def main():
    root = tk.Tk()
    set_app_icon(root)   # 设置应用图标（窗口/任务栏）
    root.withdraw()   # 登录前先隐藏主窗口

    settings = SettingsManager()
    auth = AuthManager()
    if settings.require_password:
        if not auth.exists():
            # 首次使用：自动初始化默认密码，无需用户手动设置
            auth.set_password(DEFAULT_PASSWORD)
            messagebox.showinfo(
                "首次使用",
                f"已初始化默认登录密码：{DEFAULT_PASSWORD}\n\n"
                f"请使用该密码登录，建议登录后在「修改密码」中更改。")
        # 登录校验
        if not run_dialog(root, auth, mode="login"):
            root.destroy()
            return

    root.deiconify()
    SessionManagerApp(root, auth, settings)
    root.mainloop()


if __name__ == "__main__":
    main()
