# AutoClip Studio

Công cụ mã nguồn mở tự động tạo video ngắn (YouTube Shorts, TikTok, Facebook Reels, YouTube 16:9) và đăng lên mạng xã hội. Phần AI chạy bằng **tài khoản Claude bạn đang dùng cho Claude Code**.

```
 Chủ đề / Ngách ──► Claude (Agent SDK)  ──► MoneyPrinterTurbo ──► Postiz ──► TikTok / YouTube / Facebook / Instagram / ...
                    ý tưởng, kịch bản,       footage Pexels,        OAuth kênh,
                    từ khoá, caption,        TTS, phụ đề, nhạc,      đăng ngay / hẹn giờ
                    hashtag, tag SEO         ghép video
```

## Dùng những open-source nào

| Thành phần | Dự án | Vai trò |
|---|---|---|
| LLM | [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) | Viết kịch bản, gợi ý ý tưởng. Dùng lại login của Claude Code (Pro/Max/Team). |
| Render video | [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) (MIT, đang trending) | Footage Pexels/Pixabay, giọng đọc edge-tts, phụ đề, nhạc nền, ghép MoviePy/FFmpeg. |
| Đăng bài | [Postiz](https://github.com/gitroomhq/postiz-app) (AGPL-3.0) | Kết nối 20+ mạng: TikTok, YouTube, Facebook, Instagram, Threads, X, LinkedIn... |
| Web UI / API | FastAPI + Alpine.js | Giao diện tạo video, ý tưởng, autopilot. |

## Cài đặt

Yêu cầu: Python 3.11+, Docker, [Claude Code](https://claude.com/claude-code) đã đăng nhập.

```bash
# 1. Engine render + đăng bài (Docker)
./scripts/setup-engines.sh
#    - Sửa vendor/MoneyPrinterTurbo/config.toml: pexels_api_keys = ["..."]  (miễn phí)
#    - Mở http://localhost:4007, tạo tài khoản Postiz, kết nối TikTok/YouTube/Facebook
#      (mỗi nền tảng cần tạo app developer — xem https://docs.postiz.com/providers)
#    - Postiz → Settings → Public API → copy key

# 2. App
cp .env.example .env        # dán POSTIZ_API_KEY
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000
```

Mở http://localhost:8000 — 3 chấm trạng thái trên header (Claude / MoneyPrinterTurbo / Postiz) phải xanh.

## Kết nối Claude subscription

App gọi Claude qua **Claude Agent SDK**, SDK chạy Claude Code CLI ở dưới nên dùng đúng cách đăng nhập của Claude Code:

1. **Máy cá nhân**: chỉ cần đã chạy `claude` và đăng nhập — không cần cấu hình gì.
2. **Server / Docker / không có trình duyệt**: chạy `claude setup-token` trên máy bạn, dán token vào `CLAUDE_CODE_OAUTH_TOKEN` trong `.env`.
3. **Dùng API key thay subscription**: đặt `ANTHROPIC_API_KEY`.

**Dùng acc Claude khác với acc đang login ở terminal:**

- Thư mục cấu hình riêng: chạy `CLAUDE_CONFIG_DIR=~/.claude-autoclip claude`, login bằng acc thứ 2, rồi đặt `CLAUDE_CONFIG_DIR=/Users/<you>/.claude-autoclip` trong `.env`.
- Hoặc chạy `claude setup-token`, approve bằng acc thứ 2 trên trình duyệt (cửa sổ ẩn danh), rồi dán vào `CLAUDE_CODE_OAUTH_TOKEN`. Lệnh này không ghi đè login hiện tại.

Thứ tự ưu tiên: `ANTHROPIC_API_KEY` > `CLAUDE_CODE_OAUTH_TOKEN` > login trong `CLAUDE_CONFIG_DIR` (mặc định `~/.claude`). Rê chuột vào đèn **Claude** trên UI để xem đang dùng acc nào.

> ⚠️ Dùng subscription cho **cá nhân bạn**. Từ 15/06/2026, lượt gọi qua Agent SDK trên gói subscription trừ vào hạn mức "Agent SDK credit" hằng tháng, tách riêng khỏi hạn mức chat. Không chia sẻ login/token của bạn cho người khác dùng chung; nếu triển khai cho nhiều người, mỗi người dùng tài khoản của họ hoặc dùng API key. Xem [Authentication docs](https://code.claude.com/docs/en/authentication).

## Tính năng

- **Tạo video**: nhập chủ đề → Claude viết kịch bản + caption + hashtag → render → xem trước → đăng.
- **Ý tưởng**: nhập ngách, Claude gợi ý các chủ đề dễ viral kèm câu hook.
- **Autopilot**: mỗi N giờ tự nghĩ ý tưởng → render → tự đăng lên các kênh đã chọn.
- **Đăng đa kênh** một lần qua Postiz; TikTok hỗ trợ `DIRECT_POST` hoặc `UPLOAD` (vào inbox để duyệt tay).

## API

| Method | Path | |
|---|---|---|
| GET | `/api/status` | Trạng thái Claude / MPT / Postiz |
| GET | `/api/integrations` | Các kênh đã kết nối trong Postiz |
| POST | `/api/ideas` | `{niche, language, count}` |
| POST | `/api/projects` | `{topic, language, duration, format, style, voice, auto_publish, integrations}` |
| POST | `/api/projects/{id}/publish` | `{integrations, publish_options}` |
| GET/PUT | `/api/autopilot` | Cấu hình autopilot |

`publish_options`: `youtube_visibility` (`public|unlisted|private`), `tiktok_privacy`, `tiktok_method` (`DIRECT_POST|UPLOAD`), `schedule_at` (ISO 8601).

## Lưu ý nền tảng

- **TikTok**: app chưa qua audit chỉ đăng được ở chế độ `SELF_ONLY` (riêng tư). Muốn public cần submit app cho TikTok duyệt Content Posting API.
- **YouTube**: project Google Cloud mới bị giới hạn quota (~6 video/ngày) và video upload từ app chưa verify bị khoá private cho tới khi qua audit.
- **Facebook/Instagram**: cần Meta app với quyền `pages_manage_posts` / `instagram_content_publish`.
- Gắn nhãn nội dung AI theo chính sách từng nền tảng (TikTok settings đã bật `video_made_with_ai`).

## Lộ trình gợi ý

- Thêm giọng đọc tự nhiên hơn: [Kokoro TTS](https://github.com/hexgrad/kokoro), [F5-TTS](https://github.com/SWivid/F5-TTS) (clone giọng).
- Video AI thay stock footage: ComfyUI + Wan 2.x / LTX-Video.
- Template motion graphics: [Remotion](https://github.com/remotion-dev/remotion) hoặc Hyperframes.
- Cắt clip từ video dài: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) + Claude chọn đoạn hay.

## License

[MIT](LICENSE). AutoClip Studio chỉ gọi MoneyPrinterTurbo (MIT) và Postiz (AGPL-3.0) qua API, không đóng gói code của hai dự án này. `scripts/setup-engines.sh` clone chúng vào `vendor/` (không commit), mỗi dự án giữ license riêng.
