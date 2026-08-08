@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo  CAI DAT TIKTOK COMMENT TTS
 echo ============================================================

where py >nul 2>&1
if %ERRORLEVEL%==0 (
    set PYTHON=py -3
) else (
    set PYTHON=python
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] Tao moi truong .venv...
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :error
) else (
    echo [1/2] .venv da ton tai.
)

echo [2/2] Cai VieNeu va dependency...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo Cai dat xong.
echo Lan dau chay start.bat, VieNeu co the tu tai model/cache can thiet.
echo Chay: start.bat
pause
exit /b 0

:error
echo.
echo [LOI] Cai dat that bai. Kiem tra Python va ket noi Internet.
pause
exit /b 1
