; ============================================================
; Inno Setup 脚本：Claude Code 会话管理器（cc-session）
; 仿 kpc 打包方式：PyInstaller onedir 产物 -> 单个 Windows 安装包。
; 版本号由 build-cc-session.bat 用 /DAppVer=<ver> 传入（唯一事实源 = session_manager.APP_VERSION）。
;
; 安装动作：
;   1) 释放 onedir（cc-session.exe + _internal\）到 {app}
;   2) 桌面 + 开始菜单快捷方式（GUI，无参启动）
;   3) 注册并启动 Windows 服务 CCSessionAgent（开机自启的被控端）
;   4) 放开 {app}\config 的写权限（普通用户 GUI 才能写配置/被控端状态）
; 卸载动作：停并删服务、删快捷方式（服务自带 netsh 防火墙规则清理）。
; ============================================================

#ifndef AppVer
  #define AppVer "0.0.0"
#endif

#define AppName    "Claude Code 会话管理器"
#define ExeName    "cc-session.exe"
#define SvcName    "CCSessionAgent"

[Setup]
AppId={{C26EE76E-C57E-40FA-A043-04AB19747BD6}
AppName={#AppName}
AppVersion={#AppVer}
AppVerName={#AppName} {#AppVer}
DefaultDirName={autopf}\CcSession
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=release
OutputBaseFilename=cc-session-setup-{#AppVer}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=branding\cc-session.ico
UninstallDisplayIcon={app}\{#ExeName}
UninstallDisplayName={#AppName}
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chs"; MessagesFile: "lang\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; onedir 目录整体铺进 {app}；build-cc-session.bat 已保证 deploy\cc-session\ 是最新产物
Source: "cc-session\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; 升级覆盖前清掉旧 onedir 依赖目录，避免 PyInstaller 跨版本残留 .pyd 混用
Type: filesandordirs; Name: "{app}\_internal"

[Dirs]
; {app} 在 Program Files 下默认只读；被控端要写 config\agent\（node.json/certs/logs/users.json），
; GUI 要写 config\config.json —— 给 config 子树放开普通用户写权限（可继承给运行期新建的子目录/文件）。
Name: "{app}\config"; Permissions: users-modify
Name: "{app}\config\agent"; Permissions: users-modify

[Icons]
Name: "{group}\{#AppName}";        Filename: "{app}\{#ExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#AppName}";   Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#ExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 安装向导注册服务（本进程已提权，--install-service 内的 _is_admin() 直接 sc create，不再弹 UAC）
Filename: "{app}\{#ExeName}"; Parameters: "--install-service"; StatusMsg: "正在注册并启动后台服务..."; Flags: runhidden waituntilterminated
; 收尾可选启动 GUI
Filename: "{app}\{#ExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 先停删服务再删文件；waituntilterminated 保证 exe 未被占用后 Inno 才删 {app}
Filename: "{app}\{#ExeName}"; Parameters: "--uninstall-service"; Flags: runhidden waituntilterminated; RunOnceId: "DelCcSessionSvc"
