@echo off
chcp 65001 >nul
set PORT=9000

echo Dang mo TCP %PORT% cho TikTok Comment TTS tren mang Private...
netsh advfirewall firewall delete rule name="TikTok Comment TTS %PORT%" >nul 2>&1
netsh advfirewall firewall add rule name="TikTok Comment TTS %PORT%" dir=in action=allow protocol=TCP localport=%PORT% profile=private
if errorlevel 1 (
    echo.
    echo [LOI] Hay bam chuot phai file nay va chon Run as administrator.
    pause
    exit /b 1
)

echo.
echo Da mo cong %PORT% tren Windows Firewall - Private network.
pause
