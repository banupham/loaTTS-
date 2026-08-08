@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Loa TTS Thuong Truc

if not exist ".venv\Scripts\python.exe" (
    echo [LOI] Chua co .venv. Hay chay install_windows.bat truoc.
    pause
    exit /b 1
)

if "%LOA_TTS_HOST%"=="" set LOA_TTS_HOST=0.0.0.0
if "%LOA_TTS_PORT%"=="" set LOA_TTS_PORT=8780
if "%LOA_TTS_SERVER%"=="" set LOA_TTS_SERVER=http://127.0.0.1:8765
if "%LOA_TTS_MAX_RETRIES%"=="" set LOA_TTS_MAX_RETRIES=2

echo ============================================================
echo  LOA TTS THUONG TRUC
echo ============================================================
echo WEB/API = http://127.0.0.1:%LOA_TTS_PORT%
echo TTS     = %LOA_TTS_SERVER%
echo.
echo Dien thoai LAN: http://IP_MAY_CHU:%LOA_TTS_PORT%
echo ============================================================
echo.

.venv\Scripts\python.exe app.py
set EXIT_CODE=%ERRORLEVEL%

echo.
echo Loa TTS da dung. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
