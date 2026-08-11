@echo off
rem ============================================================
rem Claude Code Session Manager - one-click launcher
rem Located in scripts\ ; source lives in ..\src
rem Double-click to run. Prefer pyw (no console), fallback py / python
rem ============================================================
set SRC=%~dp0..\src\session_manager.py

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw "%SRC%"
    exit /b
)

where py >nul 2>nul
if %errorlevel%==0 (
    start "" py "%SRC%"
    exit /b
)

where python >nul 2>nul
if %errorlevel%==0 (
    start "" python "%SRC%"
    exit /b
)

echo [ERROR] Python not found. Please install Python 3.8+ and add it to PATH.
pause
