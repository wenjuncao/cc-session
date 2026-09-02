@echo off
rem ============================================================
rem install.bat —— cc-session 首装/升级（放在 cc-session-v*.exe 同目录运行）
rem   1) 自动提权(UAC)
rem   2) 预建机器级状态目录 %PROGRAMDATA%\cc-session 并放开权限
rem      (服务 SYSTEM 与 GUI 用户共享同一 node_id/证书/配置，避免首建权限摩擦与身份增殖)
rem   3) 安装被控端 Windows 服务(开机自启，含防火墙放行 TCP 8860 / UDP 8861)
rem   4) 生成桌面快捷方式
rem 被控端监听 0.0.0.0:8860(wss 自签) + UDP 8861 广播，是刻意开放的受控远程入口，
rem 供同网段 remote-disp 主控端发现与连接（登录 admin / Cc@2026!）。
rem ============================================================
cd /d "%~dp0"

rem ---- 定位同目录（或上级目录）的 exe（文件名带版本号）----
set EXE=
for %%f in ("%~dp0cc-session-v*.exe") do set EXE=%%f
if not defined EXE for %%f in ("%~dp0..\cc-session-v*.exe") do set EXE=%%f
if not defined EXE (
    echo [ERROR] 未找到 cc-session-v*.exe，请把本脚本放到 exe 同目录后再运行。
    pause
    exit /b 1
)

rem ---- 自提权(UAC) ----
net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo 需要管理员权限，正在提权...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo [1/3] 预建机器级状态目录 %PROGRAMDATA%\cc-session 并放开权限（服务 SYSTEM 与 GUI 用户共享同一身份）...
if not exist "%PROGRAMDATA%\cc-session" md "%PROGRAMDATA%\cc-session"
icacls "%PROGRAMDATA%\cc-session" /grant *S-1-5-32-545:(OI)(CI)M /T /C >nul

echo [2/3] 安装被控端服务 CCSessionAgent（开机自启，含防火墙放行 TCP 8860 / UDP 8861）...
"%EXE%" --install-service

echo [3/3] 创建桌面快捷方式 ...
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([System.IO.Path]::Combine([Environment]::GetFolderPath('Desktop'),'cc-session.lnk')); $s.TargetPath='%EXE%'; $s.WorkingDirectory=(Split-Path '%EXE%'); $s.IconLocation='%EXE%,0'; $s.Save()"

echo.
echo [DONE] 已安装：服务 CCSessionAgent 已设为开机自启，桌面已生成 cc-session 快捷方式。
pause
exit /b 0
