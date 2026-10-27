@echo off
rem ============================================================
rem  Build cc-session (kpc-style): PyInstaller onedir + Inno Setup installer
rem  Run this file (double-click or from cmd). ASCII name on purpose:
rem  a CJK-named .bat piped from Git Bash opens cmd interactively and never runs.
rem
rem  Layout:  <repo>\         this bat
rem           <repo>\deploy\  cc-session.spec, cc-session.iss, lang
rem           <repo>\src\     session_manager.py (entry)
rem  Output:  <repo>\build\dist\cc-session\cc-session.exe  (onedir, intermediate)
rem           <repo>\release\cc-session-setup-<ver>.exe    (installer)
rem  Version: single source = src\session_manager.py APP_VERSION.
rem           Auto-bump: last segment +1 on every run (scripts\bump_version.py),
rem           written back before build so exe version + installer name use the NEW number.
rem ============================================================
setlocal
cd /d "%~dp0"

for %%I in ("%~dp0.") do set "REPO=%%~fI"
set "ROOT=%REPO%\src"
set "ENTRY=%ROOT%\session_manager.py"

if not exist "%ENTRY%" (
  echo [ERROR] Entry not found: %ENTRY%
  goto fail
)

rem --- pick Python launcher ---
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo [ERROR] Python not found. Install Python 3.8+ and add to PATH.
  goto fail
)

rem --- bump version: last segment +1, write back to src (single source), capture new ---
rem     Bumps on every run (including a run that later fails).
set "APPVER="
for /f "usebackq delims=" %%v in (`%PY% "%REPO%\scripts\bump_version.py"`) do set "APPVER=%%v"
if not defined APPVER (
  echo [ERROR] Failed to bump APP_VERSION in src\session_manager.py
  goto fail
)
echo === cc-session build  version %APPVER% (bumped) ===

rem --- deps ---
echo [1/4] Ensuring PyInstaller...
%PY% -m pip show pyinstaller >nul 2>nul || %PY% -m pip install pyinstaller
if errorlevel 1 goto fail

rem --- PyInstaller onedir via spec ---
echo [2/4] PyInstaller onedir (this may take 1-2 minutes)...
%PY% -m PyInstaller "%REPO%\deploy\cc-session.spec" --clean --noconfirm --distpath "%REPO%\build\dist" --workpath "%REPO%\build"
if errorlevel 1 goto fail
if not exist "%REPO%\build\dist\cc-session\cc-session.exe" (
  echo [ERROR] onedir exe missing: %REPO%\build\dist\cc-session\cc-session.exe
  goto fail
)
echo     onedir OK: %REPO%\build\dist\cc-session\cc-session.exe

rem --- locate ISCC (Inno Setup 6) ---
echo [3/4] Locating Inno Setup (ISCC)...
set "ISCC="
if defined ISCC_PATH if exist "%ISCC_PATH%" set "ISCC=%ISCC_PATH%"
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  echo [WARN] ISCC.exe not found. onedir built, but installer skipped.
  echo        Install Inno Setup 6, or set ISCC_PATH, then re-run.
  goto doneonedir
)

rem --- compile installer ---
echo [4/4] Building installer with %ISCC% ...
"%ISCC%" /DAppVer=%APPVER% "%REPO%\deploy\cc-session.iss"
if errorlevel 1 goto fail
echo.
echo [DONE] Installer: %REPO%\release\cc-session-setup-%APPVER%.exe
goto end

:doneonedir
echo.
echo [DONE] onedir only: %REPO%\build\dist\cc-session\  (installer step skipped)
goto end

:fail
echo.
echo [FAILED] See messages above.
endlocal
pause
exit /b 1

:end
endlocal
pause
exit /b 0
