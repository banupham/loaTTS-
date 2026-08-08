# Loa TTS Thường Trực

Module loa web độc lập cho hệ thống VieNeu TTS local.

Mục tiêu: mở một trang web trên điện thoại/laptop, bấm **BẬT LOA** một lần, sau đó middleware chỉ cần gửi text vào API. Module tự xếp hàng, gửi job tới loa đang hoạt động và phát PCM realtime trên chính thiết bị mở web.

## Kiến trúc

```text
TikTok / middleware / app khác
          |
          | POST /speak
          v
+-------------------------+
| loaTTS- :8780           |
| - Priority queue        |
| - WebSocket dispatcher  |
| - TTS stream proxy      |
+------------+------------+
             |
             | POST /tts/stream
             v
+-------------------------+
| VieNeu TTS :8765        |
| sinh PCM16 realtime     |
+------------+------------+
             |
             | PCM stream qua :8780
             v
+-------------------------+
| Điện thoại / laptop     |
| Web Audio API           |
| phát loa / tai nghe     |
+-------------------------+
```

Repo này **không chứa model VieNeu** và không tự nạp model. Nó dùng TTS server đang chạy ở project `banupham/tts`.

Mặc định:

```text
VieNeu TTS server : http://127.0.0.1:8765
Loa TTS server    : http://0.0.0.0:8780
```

## Cài trên Windows

```cmd
git clone https://github.com/banupham/loaTTS-.git
cd loaTTS-
install_windows.bat
```

Sau đó chạy TTS server VieNeu ở repo `tts` trước, rồi chạy:

```cmd
start.bat
```

Mở trên PC:

```text
http://127.0.0.1:8780
```

Mở trên điện thoại cùng Wi-Fi/LAN:

```text
http://IP_MAY_CHU:8780
```

Ví dụ:

```text
http://192.168.1.20:8780
```

Xem IPv4 của PC:

```cmd
ipconfig
```

Nếu điện thoại không truy cập được, chạy bằng **Run as administrator**:

```cmd
allow_firewall.bat
```

## Bật loa thường trực

Trên điện thoại/laptop:

1. mở `http://IP_MAY_CHU:8780`;
2. đặt tên thiết bị nếu muốn;
3. chỉnh âm lượng;
4. bấm **BẬT LOA THƯỜNG TRỰC**;
5. giữ trang web mở.

Thiết bị bấm **BẬT LOA** gần nhất sẽ trở thành **loa chính**.

Nếu mở trang trên điện thoại khác và bấm BẬT LOA, quyền phát được chuyển sang điện thoại mới.

WebSocket tự reconnect nếu mạng chập chờn.

## Lưu ý Android / trình duyệt nền

Trình duyệt di động có thể bị Android tạm dừng khi:

- tắt màn hình;
- chuyển app lâu;
- hệ thống tiết kiệm pin đóng tab nền.

Trang web có thử dùng Screen Wake Lock khi trình duyệt cho phép. Tuy nhiên Wake Lock thường bị giới hạn bởi chính sách trình duyệt/secure context, nên khi mở bằng địa chỉ HTTP trong LAN có thiết bị sẽ không cho phép.

Để dùng như loa cố định, nên:

- cắm sạc điện thoại;
- để trang web ở foreground;
- tăng thời gian tắt màn hình hoặc để màn hình luôn sáng;
- tắt tối ưu pin cho trình duyệt nếu cần;
- dùng tai nghe/Bluetooth speaker nối với điện thoại nếu muốn âm thanh tốt hơn.

Sau này có thể đóng gói phần web này thành APK/kiosk để chạy nền ổn định hơn.

## Test nhanh

Sau khi VieNeu `8765`, loaTTS `8780` và điện thoại loa đều đang hoạt động:

```cmd
test_speak.bat
```

Hoặc dùng curl:

```cmd
curl -X POST http://127.0.0.1:8780/speak -H "Content-Type: application/json" -d "{\"text\":\"Xin chào, cảm ơn bạn vừa follow!\",\"priority\":20,\"style\":\"tu_nhien\"}"
```

## API cho middleware

### POST `/speak`

Thêm một câu vào hàng đợi.

```json
{
  "text": "Cảm ơn bạn vừa gửi quà!",
  "voice": "Minh Đức",
  "style": "tu_nhien",
  "priority": 10,
  "temperature": 0.78,
  "top_k": 25,
  "top_p": 0.93,
  "max_chars": 180,
  "repetition_penalty": 1.2,
  "apply_watermark": false
}
```

Các field tối thiểu:

```json
{
  "text": "Xin chào mọi người"
}
```

Priority số nhỏ hơn được ưu tiên trước.

Gợi ý cho TikTok LIVE:

```text
GIFT    = 10
FOLLOW  = 20
COMMENT = 50
JOIN    = 80
```

### GET `/status`

Trả trạng thái:

- số loa đang kết nối;
- loa chính;
- queue;
- job hiện tại;
- số câu đã đọc;
- số lần lỗi;
- địa chỉ TTS server.

### POST `/clear`

Xóa các câu còn đang chờ trong queue.

Không cắt câu đang phát.

### POST `/stop`

Dừng câu đang phát hiện tại.

### GET `/api/voices`

Proxy danh sách giọng từ VieNeu TTS server.

### GET `/api/tts-health`

Kiểm tra trạng thái VieNeu TTS server.

## Cơ chế queue

`loaTTS` giữ priority queue ở server.

Luồng:

```text
/speak
  -> queue
  -> chờ loa chính online
  -> WebSocket gửi job
  -> điện thoại gọi /api/stream
  -> loaTTS proxy /tts/stream từ VieNeu
  -> điện thoại phát PCM16 realtime
  -> báo completed
  -> server lấy job tiếp theo
```

Nếu loa mất kết nối trong lúc phát, job có thể được thử lại. Mặc định tối đa 2 lần.

Cấu hình:

```cmd
set LOA_TTS_MAX_RETRIES=2
```

## Dùng TTS server ở máy khác

Mặc định `loaTTS` tìm VieNeu tại:

```text
http://127.0.0.1:8765
```

Nếu VieNeu nằm ở máy khác:

```cmd
set LOA_TTS_SERVER=http://192.168.1.10:8765
start.bat
```

Lưu ý TTS server bên kia phải cho phép truy cập qua LAN.

## Cấu hình cổng

```cmd
set LOA_TTS_HOST=0.0.0.0
set LOA_TTS_PORT=8780
set LOA_TTS_SERVER=http://127.0.0.1:8765
set LOA_TTS_MAX_RETRIES=2
start.bat
```

## Tự chạy cùng Windows

Sau khi cài xong:

```cmd
install_autostart.bat
```

Gỡ autostart:

```cmd
remove_autostart.bat
```

Autostart chỉ khởi động server `8780`. Điện thoại vẫn cần mở trang và bật loa.

## Tích hợp Python middleware

```python
import requests

requests.post(
    "http://127.0.0.1:8780/speak",
    json={
        "text": "Cảm ơn bạn vừa follow!",
        "voice": "Minh Đức",
        "style": "tu_nhien",
        "priority": 20,
    },
    timeout=3,
)
```

Middleware không cần biết WebSocket, PCM hay Web Audio. Nó chỉ cần gửi text vào `/speak`.

## File chính

```text
app.py                    server + queue + WebSocket + stream proxy
web/index.html            giao diện loa trên điện thoại/laptop
requirements.txt          dependency
install_windows.bat       cài môi trường
start.bat                 chạy server 8780
allow_firewall.bat        mở LAN port 8780
test_speak.bat            gửi câu test
install_autostart.bat     tự chạy server cùng Windows
remove_autostart.bat      gỡ autostart
```
