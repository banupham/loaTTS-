@echo off
setlocal
chcp 65001 >nul
set "LAUNCHER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\loa_tts_thuong_truc.cmd"

if exist "%LAUNCHER%" (
    del /q "%LAUNCHER%"
    echo Da go autostart Loa TTS.
) else (
    echo Khong tim thay autostart Loa TTS.
)
pause
