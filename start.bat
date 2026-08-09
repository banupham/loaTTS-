@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title TikTok Comment TTS Speaker

if not exist ".venv\Scripts\python.exe" (
    echo [LOI] Chua co .venv. Hay chay install_windows.bat truoc.
    pause
    exit /b 1
)

if "%LOA_TTS_HOST%"=="" set LOA_TTS_HOST=0.0.0.0
if "%LOA_TTS_PORT%"=="" set LOA_TTS_PORT=9000
if "%LOA_TTS_EVENT_PATH%"=="" set LOA_TTS_EVENT_PATH=/tiktok-event
if "%LOA_TTS_PRECISION%"=="" set LOA_TTS_PRECISION=int8
if "%LOA_TTS_THREADS%"=="" set LOA_TTS_THREADS=2
if "%LOA_TTS_WARMUP%"=="" set LOA_TTS_WARMUP=1
if "%LOA_TTS_QUEUE_MAX%"=="" set LOA_TTS_QUEUE_MAX=10
if "%LOA_TTS_COMMENT_MAX_AGE%"=="" set LOA_TTS_COMMENT_MAX_AGE=8
if "%LOA_TTS_COMMENT_DELAY%"=="" set LOA_TTS_COMMENT_DELAY=1.0
if "%LOA_TTS_CLARITY_PRESET%"=="" set LOA_TTS_CLARITY_PRESET=1

echo ============================================================
echo  TIKTOK COMMENT TTS - PREBUFFER + TEXT PROSODY
echo ============================================================
echo Web       : http://127.0.0.1:%LOA_TTS_PORT%
echo Webhook   : http://127.0.0.1:%LOA_TTS_PORT%%LOA_TTS_EVENT_PATH%
echo Health    : http://127.0.0.1:%LOA_TTS_PORT%/health
echo Precision : %LOA_TTS_PRECISION%
echo ONNX thr  : %LOA_TTS_THREADS%
echo Queue max : %LOA_TTS_QUEUE_MAX%
echo Max age   : %LOA_TTS_COMMENT_MAX_AGE%s
echo Gap       : %LOA_TTS_COMMENT_DELAY%s before every comment
echo Generator : 1 thread - tao PCM hoan chinh truoc
echo Player    : chi truyen PCM da tao san
echo Burst     : lay comment cu nhat, bo cac comment con lai trong dot
echo Ready     : chi giu 1 audio ke tiep
echo Prosody   : chinh tren Web - normal / clear / slow
echo Clarity   : preset ro chu mot lan = %LOA_TTS_CLARITY_PRESET%
echo Share/UI  : OFF - middleware da chuan hoa
echo Speed     : ORIGINAL 1.00x - khong playbackRate
echo TailGuard : OFF
echo.
echo Dien thoai LAN: http://IP_MAY_CHU:%LOA_TTS_PORT%
echo ============================================================
echo.

.venv\Scripts\python.exe prosody_runtime.py
set EXIT_CODE=%ERRORLEVEL%

echo.
echo TikTok Comment TTS da dung. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
