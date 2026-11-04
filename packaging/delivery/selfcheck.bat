@echo off
chcp 65001 >nul
rem OcrTableTool delivery package self-check (customer side, standard library only)
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY ( where python3 >nul 2>nul && set "PY=python3" )

if not defined PY (
    echo [FAIL] No Python interpreter found on PATH.
    echo        Install Python 3.8 or newer, or run:
    echo        python customer_selfcheck.py
    exit /b 1
)

echo Using: %PY%
%PY% customer_selfcheck.py
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" ( echo Self-check finished. ) else ( echo Self-check FAILED ^(exit %RC%^). )
pause
exit /b %RC%
