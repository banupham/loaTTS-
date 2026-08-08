@echo off
chcp 65001 >nul
set URL=http://127.0.0.1:9000/tiktok-event

echo Gui event COMMENT gia lap toi %URL% ...
curl -X POST "%URL%" -H "Content-Type: application/json" -d "{\"schemaVersion\":1,\"eventId\":\"test-comment-001\",\"eventType\":\"comment\",\"timestamp\":1786000000000,\"receivedAt\":1786000000015,\"source\":{\"platform\":\"tiktok\",\"collector\":\"dom\"},\"user\":{\"id\":\"duong123\",\"uniqueId\":\"duong123\",\"displayName\":\"Dương\"},\"payload\":{\"text\":\"Xin chào, đây là comment TikTok thử nghiệm\",\"normalizedText\":\"XIN CHÀO, ĐÂY LÀ COMMENT TIKTOK THỬ NGHIỆM\"}}"
echo.
pause
