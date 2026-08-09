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
if "%LOA_TTS_THREADS%"=="" set LOA_TTS_THREADS=0
if "%LOA_TTS_WARMUP%"=="" set LOA_TTS_WARMUP=1
if "%LOA_TTS_QUEUE_MAX%"=="" set LOA_TTS_QUEUE_MAX=30
if "%LOA_TTS_COMMENT_MAX_AGE%"=="" set LOA_TTS_COMMENT_MAX_AGE=20
if "%LOA_TTS_TAIL_BASE%"=="" set LOA_TTS_TAIL_BASE=1.5
if "%LOA_TTS_TAIL_PER_WORD%"=="" set LOA_TTS_TAIL_PER_WORD=0.65
if "%LOA_TTS_TAIL_MIN%"=="" set LOA_TTS_TAIL_MIN=2.2
if "%LOA_TTS_TAIL_MAX%"=="" set LOA_TTS_TAIL_MAX=15
if "%LOA_TTS_TAIL_FADE_MS%"=="" set LOA_TTS_TAIL_FADE_MS=30

echo ============================================================
echo  TIKTOK COMMENT TTS - SELF CONTAINED
echo ============================================================
echo Web       : http://127.0.0.1:%LOA_TTS_PORT%
echo Webhook   : http://127.0.0.1:%LOA_TTS_PORT%%LOA_TTS_EVENT_PATH%
echo Health    : http://127.0.0.1:%LOA_TTS_PORT%/health
echo Precision : %LOA_TTS_PRECISION%
echo Queue max : %LOA_TTS_QUEUE_MAX%
echo Max age   : %LOA_TTS_COMMENT_MAX_AGE%s
echo Filter    : EXACT PHRASE - xoa cum, giu phan comment con lai
echo Speed     : 0.70x - 1.50x, chinh tren Web
echo Emoji     : tu dat emoji=cach doc tren Web
echo TailGuard : base=%LOA_TTS_TAIL_BASE%s word=%LOA_TTS_TAIL_PER_WORD%s min=%LOA_TTS_TAIL_MIN%s max=%LOA_TTS_TAIL_MAX%s fade=%LOA_TTS_TAIL_FADE_MS%ms
echo Audio     : block 120ms, buffer 200ms, edge fade 8ms
echo.
echo Dien thoai LAN: http://IP_MAY_CHU:%LOA_TTS_PORT%
echo ============================================================
echo.

.venv\Scripts\python.exe tail_guard.py
set EXIT_CODE=%ERRORLEVEL%

echo.
echo TikTok Comment TTS da dung. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
