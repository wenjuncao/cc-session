; ============================================================
; Inno Setup 脚本：Claude Code 会话管理器（cc-session）
; 仿 kpc 打包方式：PyInstaller onedir 产物 -> 单个 Windows 安装包。
; 版本号由 build-cc-session.bat 用 /DAppVer=<ver> 传入（唯一事实源 = session_manager.APP_VERSION）。
;
; 安装动作：
;   1) 清理旧版残留的被控端服务 CCSessionAgent 与其防火墙规则（升级覆盖时）
;   2) 释放 onedir（cc-session.exe + _internal\）到 {app}
;   3) 桌面 + 开始菜单快捷方式（GUI，无参启动）
;   4) 放开 {app}\config 的写权限（普通用户 GUI 才能写配置）
; 卸载动作：删快捷方式与程序文件。
; ============================================================

#ifndef AppVer
  #define AppVer "0.0.0"
#endif

#define AppName    "Claude Code 会话管理器"
#define ExeName    "cc-session.exe"
#define OldSvcName "CCSessionAgent"

[Setup]
AppId={{C26EE76E-C57E-40FA-A043-04AB19747BD6}
AppName={#AppName}
AppVersion={#AppVer}
AppVerName={#AppName} {#AppVer}
DefaultDirName={autopf}\CcSession
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=..\release
OutputBaseFilename=cc-session-setup-{#AppVer}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\logo.ico
UninstallDisplayIcon={app}\{#ExeName}
UninstallDisplayName={#AppName}
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chs"; MessagesFile: "lang\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; onedir 目录整体铺进 {app}；build-cc-session.bat 已保证 build\dist\cc-session\ 是最新产物
Source: "..\build\dist\cc-session\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; 升级覆盖前清掉旧 onedir 依赖目录，避免 PyInstaller 跨版本残留 .pyd 混用
Type: filesandordirs; Name: "{app}\_internal"

[Dirs]
; {app} 在 Program Files 下默认只读；GUI 要写 config\config.json —— 给 config 子树放开普通用户写权限。
Name: "{app}\config"; Permissions: users-modify

[Icons]
Name: "{group}\{#AppName}";        Filename: "{app}\{#ExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#AppName}";   Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#ExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 收尾可选启动 GUI
Filename: "{app}\{#ExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
// 旧版（0.0.13/0.0.14）会注册被控端服务并放行 TCP 8860 / UDP 8861；
// 覆盖安装前停删服务、删防火墙规则，避免远程入口残留、旧服务占用 exe。不存在时命令静默失败。
procedure CurStepChanged(CurStep: TSetupStep);
var
  R: Integer;
begin
  if CurStep = ssInstall then
  begin
    Exec('sc.exe', 'stop {#OldSvcName}', '', SW_HIDE, ewWaitUntilTerminated, R);
    Exec('sc.exe', 'delete {#OldSvcName}', '', SW_HIDE, ewWaitUntilTerminated, R);
    Exec('netsh.exe', 'advfirewall firewall delete rule name=cc-session-agent', '', SW_HIDE, ewWaitUntilTerminated, R);
    Exec('netsh.exe', 'advfirewall firewall delete rule name=cc-session-agent-udp', '', SW_HIDE, ewWaitUntilTerminated, R);
  end;
end;
