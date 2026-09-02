@echo off
rem ============================================================
rem uninstall.bat —— 卸载(逆 install)：停/删被控端服务、删防火墙规则、删桌面快捷方式
rem 放在 cc-session-v*.exe 同目录运行；自动提权。
rem 注意：不删除程序目录与状态目录 %PROGRAMDATA%\cc-session（密码哈希/证书/日志/node.json）。
rem   保留是为了重装复用同一 node_id（不再在主控端生成新卡）；需彻底清理请手动删除该目录。
rem ============================================================
cd /d "%~dp0"

set EXE=
for %%f in ("%~dp0cc-session-v*.exe") do set EXE=%%f
if not defined EXE for %%f in ("%~dp0..\cc-session-v*.exe") do set EXE=%%f

net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo 需要管理员权限，正在提权...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo [1/2] 卸载被控端服务 ...
if defined EXE (
    "%EXE%" --uninstall-service
) else (
    sc stop CCSessionAgent
    sc delete CCSessionAgent
)

echo [2/2] 删除桌面快捷方式 ...
powershell -NoProfile -Command "$p=[System.IO.Path]::Combine([Environment]::GetFolderPath('Desktop'),'cc-session.lnk'); if(Test-Path $p){Remove-Item $p -Force}"

echo.
echo [DONE] 已卸载。程序目录与 %PROGRAMDATA%\cc-session（身份/证书/配置）保留。
pause
exit /b 0
