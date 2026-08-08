@echo off
chcp 65001 >nul
curl -X POST http://127.0.0.1:8780/speak ^
  -H "Content-Type: application/json" ^
  -d "{\"text\":\"Xin chào. Loa TTS thường trực đang hoạt động.\",\"priority\":50,\"style\":\"tu_nhien\"}"
echo.
pause
