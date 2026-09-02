# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 规格：cc-session（Claude Code 会话管理器）。

构建：见 deploy/build-cc-session.bat。产物：deploy/cc-session/cc-session.exe + 依赖文件夹，
再由 deploy/cc-session.iss 打成 Windows 安装包。

要点（仿 kpc 打包方式）：
  - 入口 src/session_manager.py（GUI 主程序；同一 exe 另经 CLI 参数充当被控端服务）。
  - windowed（console=False）：GUI 程序不弹控制台；--service/--agent-silent 静默常驻也无窗口。
  - 被控端子包 agent 由 collect_submodules 全量收集；其运行期依赖（flask_sock/simple_websocket/
    werkzeug.serving/PIL.ImageGrab/cryptography）静态分析看不全，显式 hiddenimports。
  - logo.ico 落 _MEIPASS 根：session_manager.resource_path 冻结态按 sys._MEIPASS/logo.ico 找。
  - Windows 版本资源：任务管理器/文件属性/UAC 弹窗显示中文名而非 cc-session.exe。
"""
import os
import re
import sys
import tempfile

from PyInstaller.utils.hooks import collect_submodules

# 路径锚到 spec 所在目录（PyInstaller 注入 SPECPATH），与 cwd 解耦。
HERE = os.path.abspath(SPECPATH)          # <仓库根>/deploy
REPO = os.path.dirname(HERE)              # <仓库根>
ROOT = os.path.join(REPO, "src")          # import agent.* / session_manager 的父目录
ICON = os.path.join(REPO, "assets", "logo.ico")

# 让 collect_submodules('agent') 能找到包（spec 在 PyInstaller 进程里执行，pathex 尚未生效）。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

datas = [
    (ICON, "."),   # -> _MEIPASS/logo.ico，与 resource_path 对齐
]
binaries = []
hiddenimports = [
    "flask_sock", "simple_websocket", "werkzeug.serving",
    "PIL.ImageGrab", "cryptography",
] + collect_submodules("agent")

# ── Windows 版本资源：显示中文程序名 ─────────────────────────────────────
# 版本号从 session_manager.py 正则读出（唯一事实源），不在此另写一份。
_sm_src = open(os.path.join(ROOT, "session_manager.py"), encoding="utf-8").read()
APP_VER = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', _sm_src, re.M).group(1)
APP_NAME = "Claude Code 会话管理器"
_vt = tuple((list(map(int, APP_VER.split("."))) + [0, 0, 0, 0])[:4])

_VERSION_FILE = os.path.join(tempfile.gettempdir(), "cc-session-versioninfo.txt")
with open(_VERSION_FILE, "w", encoding="utf-8") as _f:
    _f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={_vt}, prodvers={_vt}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404B0', [
      StringStruct('CompanyName', '{APP_NAME}'),
      StringStruct('FileDescription', '{APP_NAME}'),
      StringStruct('FileVersion', '{APP_VER}'),
      StringStruct('InternalName', 'cc-session'),
      StringStruct('LegalCopyright', ''),
      StringStruct('OriginalFilename', 'cc-session.exe'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{APP_VER}'),
    ])]),
    VarFileInfo([VarStruct('Translation', [0x804, 1200])])
  ]
)
""")

a = Analysis(
    [os.path.join(ROOT, "session_manager.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="cc-session",
    console=False,                 # GUI：不弹控制台
    disable_windowed_traceback=False,
    version=_VERSION_FILE,
    icon=ICON,
)
coll = COLLECT(exe, a.binaries, a.datas, name="cc-session")
