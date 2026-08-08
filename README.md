# Loa TikTok Comment TTS

Project độc lập, tự chứa VieNeu TTS và chỉ phục vụ **một mục đích duy nhất**: đọc comment từ TikTok LIVE middleware `banupham/tiktok_live_cmd_active_viewers_v9`.

Không dùng repo `tts` khác. Không cần server `8765`. Model VieNeu được nạp trực tiếp trong project này.

## Luồng hoạt động

```text
TikTok LIVE
    ↓
TikTok middleware
    ↓ POST event
http://127.0.0.1:9000/tiktok-event
    ↓
loaTTS- lọc event
    ├─ comment → queue → VieNeu infer_stream()
    ├─ join    → bỏ qua
    ├─ follow  → bỏ qua
    ├─ like    → bỏ qua
    └─ gift    → bỏ qua
    ↓
PCM16 realtime
    ↓
điện thoại/laptop đang bật LOA TIKTOK
    ↓
loa / tai nghe / Bluetooth
```

## Cài đặt Windows

```cmd
git clone https://github.com/banupham/loaTTS-.git
cd loaTTS-
install_windows.bat
```

Project pin:

```text
vieneu==3.2.4
backend=onnx
precision=int8 mặc định
```

Lần chạy đầu VieNeu có thể tự tải model/cache cần thiết.

## Khởi động loaTTS

```cmd
start.bat
```

Mặc định:

```text
Web      http://127.0.0.1:9000
Health   http://127.0.0.1:9000/health
Webhook  http://127.0.0.1:9000/tiktok-event
```

Server tự:

1. nạp VieNeu ONNX;
2. warm-up model;
3. mở webhook TikTok;
4. chờ điện thoại bật loa;
5. đọc comment theo FIFO.

## Dùng điện thoại làm loa

Xem IPv4 máy tính:

```cmd
ipconfig
```

Ví dụ PC có IP `192.168.1.20`, trên điện thoại cùng Wi-Fi mở:

```text
http://192.168.1.20:9000
```

Nếu không truy cập được, chạy bằng **Run as administrator**:

```cmd
allow_firewall.bat
```

Trên web:

1. chọn giọng;
2. chọn phong cách;
3. chỉnh âm lượng;
4. tùy chọn đọc tên viewer trước comment;
5. bấm **BẬT LOA TIKTOK**;
6. giữ trang mở.

Thiết bị bấm BẬT LOA gần nhất sẽ trở thành loa chính.

## Nối với TikTok middleware

Repo middleware chuẩn:

```text
https://github.com/banupham/tiktok_live_cmd_active_viewers_v9.git
```

Không cần sửa giao thức.

Mở loaTTS trước:

```cmd
start.bat
```

Sau đó tại repo middleware chạy:

```cmd
start_middleware_to_game.bat ten_tiktok
```

Ví dụ:

```cmd
start_middleware_to_game.bat ngocky.ne
```

File middleware đã dùng mặc định:

```text
GAME_EVENT_HOST=127.0.0.1
GAME_EVENT_PORT=9000
GAME_EVENT_PATH=/tiktok-event
WEBHOOK_URLS=http://127.0.0.1:9000/tiktok-event
```

`loaTTS- /health` trả đúng contract handshake mà middleware cần, gồm:

```text
ok=true
service=game-event-server
instanceId
pid
eventPath=/tiktok-event
```

## Event nào được đọc?

Chỉ payload dạng:

```json
{
  "eventType": "comment",
  "user": {
    "id": "duong123",
    "uniqueId": "duong123",
    "displayName": "Dương"
  },
  "payload": {
    "text": "xin chào",
    "normalizedText": "XIN CHÀO"
  }
}
```

Text đem đọc lấy từ:

```text
payload.text
```

`join`, `follow`, `like`, `gift` vẫn trả HTTP 2xx nhưng bị đánh dấu `ignored`, để middleware không retry và không báo lỗi webhook.

## Chống trễ khi LIVE đông comment

Mặc định:

```text
queue tối đa      30 comment
comment quá 20s   bỏ qua
```

Mục tiêu là không để loa đọc những comment đã quá cũ khi chat dồn nhanh.

Có thể đổi trước khi chạy:

```cmd
set LOA_TTS_QUEUE_MAX=50
set LOA_TTS_COMMENT_MAX_AGE=30
start.bat
```

Đặt `LOA_TTS_COMMENT_MAX_AGE=0` nếu không muốn bỏ comment cũ.

## Cấu hình model

Mặc định:

```cmd
set LOA_TTS_PRECISION=int8
set LOA_TTS_THREADS=0
set LOA_TTS_WARMUP=1
start.bat
```

CPU chất lượng cao hơn nhưng nặng hơn:

```cmd
set LOA_TTS_PRECISION=fp32
start.bat
```

Web lưu lựa chọn giọng vào `settings.json` local. File này nằm trong `.gitignore`.

## Test không cần mở TikTok

Khi server và điện thoại loa đã chạy:

```cmd
test_comment.bat
```

File này gửi một event `comment` giả lập đúng schema middleware vào:

```text
POST http://127.0.0.1:9000/tiktok-event
```

Nếu hệ thống đúng, điện thoại sẽ đọc:

```text
Xin chào, đây là comment TikTok thử nghiệm
```

## Theo dõi trạng thái

```cmd
curl http://127.0.0.1:9000/status
```

Có các chỉ số:

- model đã load chưa;
- số comment đã nhận;
- queue hiện tại;
- comment đang đọc;
- số comment đã đọc;
- số event non-comment đã bỏ qua;
- số comment bị bỏ vì queue đầy hoặc quá cũ;
- loa chính hiện tại.

## Tự khởi động cùng Windows

```cmd
install_autostart.bat
```

Gỡ:

```cmd
remove_autostart.bat
```

## Cấu trúc

```text
app.py                  VieNeu + webhook + queue + WebSocket + PCM stream
web/index.html           giao diện loa TikTok
requirements.txt         VieNeu và runtime
start.bat                chạy server 9000
install_windows.bat      tạo .venv và cài dependency
allow_firewall.bat       mở port 9000 cho LAN
test_comment.bat         giả lập TikTok COMMENT
install_autostart.bat    tự chạy cùng Windows
remove_autostart.bat     gỡ autostart
```
