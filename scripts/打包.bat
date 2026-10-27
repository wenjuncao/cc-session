@echo off
rem ============================================================
rem Build cc-session into a single-file Windows EXE (PyInstaller)
rem Located in scripts\ ; source ..\src, assets ..\assets
rem Output: ..\cc-session-v<VERSION>.exe (project root, NOT dist)
rem VERSION is auto-bumped (+1 on last part) in src\session_manager.py BEFORE each
rem build, so both the exe filename and the in-app version show the NEW number.
rem Note: use "if errorlevel N" (runtime) instead of %errorlevel%
rem       inside blocks, which is expanded at parse time.
rem ============================================================
cd /d "%~dp0"

set ROOT=%~dp0..
set SRC=%~dp0..\src\session_manager.py
set ICO=%~dp0..\assets\logo.ico

set PYEXE=
where py >nul 2>nul && set PYEXE=py
if not defined PYEXE (where python >nul 2>nul && set PYEXE=python)
if not defined PYEXE goto nopy

echo [1/2] Ensuring PyInstaller is installed...
%PYEXE% -m pip show pyinstaller >nul 2>nul
if not errorlevel 1 goto build
%PYEXE% -m pip install pyinstaller
if errorlevel 1 goto pipfail

:build
rem --- (B) Auto-bump version in source FIRST, so exe name + in-app version match ---
set APPVER=
for /f "delims=" %%v in ('%PYEXE% "%~dp0bump_version.py"') do set APPVER=%%v
if not defined APPVER goto bumpfail
set EXENAME=cc-session-v%APPVER%
echo New version: %APPVER%

echo [2/2] Building EXE %EXENAME%.exe (this may take 1-2 minutes)...
rem --distpath ..  : output exe to project root instead of dist\
rem --workpath / --specpath : keep intermediate files under ..\build\
%PYEXE% -m PyInstaller --noconfirm --onefile --windowed --name %EXENAME% --icon "%ICO%" --add-data "%ICO%;." --distpath "%ROOT%" --workpath "%ROOT%\build" --specpath "%ROOT%\build" "%SRC%"
if errorlevel 1 goto buildfail

echo.
echo [DONE] EXE created: %ROOT%\%EXENAME%.exe  (version %APPVER%)
pause
exit /b 0

:nopy
echo [ERROR] Python not found. Install Python 3.8+ and add to PATH.
pause
exit /b 1

:pipfail
echo [ERROR] Failed to install PyInstaller.
pause
exit /b 1

:bumpfail
echo [ERROR] Failed to bump version (bump_version.py). Aborting.
pause
exit /b 1

:buildfail
echo [ERROR] Build failed. See messages above.
pause
exit /b 1
