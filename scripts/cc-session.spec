# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 规格：cc-session（Claude Code 会话管理器）。

构建：见根目录 build-cc-session.bat。产物：build/dist/cc-session/cc-session.exe + 依赖文件夹，
再由 scripts/cc-session.iss 打成 Windows 安装包。

要点（仿 kpc 打包方式）：
  - 入口 src/session_manager.py（GUI 主程序，零第三方依赖）。
  - windowed（console=False）：GUI 程序不弹控制台。
  - logo.ico 落 _MEIPASS 根：session_manager.resource_path 冻结态按 sys._MEIPASS/logo.ico 找。
  - Windows 版本资源：任务管理器/文件属性/UAC 弹窗显示中文名而非 cc-session.exe。
"""
import os
import re
import tempfile

# 路径锚到 spec 所在目录（PyInstaller 注入 SPECPATH），与 cwd 解耦。
HERE = os.path.abspath(SPECPATH)          # <仓库根>/scripts
REPO = os.path.dirname(HERE)              # <仓库根>
ROOT = os.path.join(REPO, "src")          # session_manager / version_check 所在目录
ICON = os.path.join(REPO, "assets", "logo.ico")

datas = [
    (ICON, "."),   # -> _MEIPASS/logo.ico，与 resource_path 对齐
]
binaries = []
hiddenimports = []

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
