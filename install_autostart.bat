@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LAUNCHER=%STARTUP%\loa_tts_thuong_truc.cmd"

>"%LAUNCHER%" echo @echo off
>>"%LAUNCHER%" echo cd /d "%~dp0"
>>"%LAUNCHER%" echo start "" /min cmd /c start.bat

echo Da tao autostart:
echo %LAUNCHER%
echo.
echo Tu lan dang nhap Windows tiep theo, Loa TTS server se tu khoi dong.
pause
