# OpenMontage — Luồng generate video từ đầu tới cuối

> Cập nhật: 2026-04-07

## Tổng quan

```
User (Browser) → POST /api/generate → FastAPI → BackgroundTask → run_pipeline()
                                                                        │
                ┌───────────────────────────────────────────────────────┤
                │                  PARALLEL (ThreadPoolExecutor)         │
                │                                                        │
                ▼                 ▼                    ▼                ▼
            do_tts()         do_music()          do_clip(0)        do_clip(1..N)
          (Vbee TTS)     (Local → Suno)     (useapi.net → Direct → Playwright)
                │                │                    │                │
                └────────────────┴────────────────────┴────────────────┘
                                         │
                                    _compose()
                                  (FFmpeg pipeline)
                                         │
                              final.mp4 → cleanup intermediates
```

---

## 1. Startup (khi khởi động server)

```
uvicorn api:app --port 8000
```

- `startup_event()` → spawn daemon thread `prewarm`
  - Load **Whisper** `base` vào RAM (dùng cho phụ đề khi `subtitle_enabled`)
  - Load **MusicGen** `facebook/musicgen-small` vào RAM *(dự phòng)*
- Mount static directories:
  - `/projects` → `projects/`
  - `/audio` → `audio/`
  - `/static` → `web/`

---

## 2. User nhập form và submit

**Frontend** (`web/index.html`):
- Tất cả fields cache vào `localStorage` key `om_form_v1` → F5 không mất
- Khi load trang: `loadAudioList()` gọi `GET /api/audio/list` → populate dropdown nhạc
- Khi submit → `POST /api/generate` với `multipart/form-data`

**Params gửi lên:**

| Field | Mặc định | Mô tả |
|-------|----------|-------|
| `script` | — | Nội dung kịch bản (bắt buộc) |
| `llm_system_prompt` | `""` | Hướng dẫn thêm cho Veo prompt |
| `veo_style_seed` | `""` | Style seed bổ sung vào mỗi scene prompt |
| `voice_code` | `s_sg_male_thientam_ytstable_vc` | Giọng đọc Vbee |
| `duration` | 30 | Thời lượng video (giây) |
| `aspect_ratio` | `9:16` | Tỉ lệ khung hình |
| `speed_rate` | 0.85 | Tốc độ đọc TTS |
| `subtitle_enabled` | false | Bật/tắt phụ đề |
| `subtitle_fontsize` | 12 | Cỡ chữ phụ đề |
| `subtitle_color` | `white` | Màu phụ đề |
| `subtitle_position` | `bottom` | Vị trí phụ đề |
| `watermark_text` | `""` | Chữ watermark |
| `watermark_position` | `top_left` | Vị trí watermark |
| `enable_music` | true | Bật/tắt nhạc nền |
| `music_file` | `bamboo-breath_bKoDGfET.wav` | File nhạc từ `audio/` |
| `image` | null | Ảnh nền cho scene đầu (image-to-video) |
| `video` | null | Video nguồn (tùy chọn) |
| `watermark_image` | null | Ảnh logo watermark (PNG) |

---

## 3. API endpoints phụ

### Config / Auth

| Endpoint | Mô tả |
|----------|-------|
| `GET /api/useapi/config` | Kiểm tra USEAPI_TOKEN đã set chưa |
| `POST /api/useapi/config` | Lưu `{token, email}` vào .env |
| `GET /api/google-flow/config` | Kiểm tra Google Flow cookies (fallback) |
| `POST /api/google-flow/config` | Lưu `{email, cookies}` vào .env |
| `GET /api/opera/status` | Kiểm tra Opera CDP port 9222 đang chạy không |
| `POST /api/opera/launch` | Launch Opera với `--remote-debugging-port=9222` |
| `GET /api/google-flow/login/status` | Trạng thái login browser |
| `POST /api/google-flow/login` | Mở Opera tới Google Flow để login |
| `POST /api/google-flow/login/confirm` | Capture cookies từ browser đang mở |

### Media

| Endpoint | Mô tả |
|----------|-------|
| `GET /api/audio/list` | Danh sách file nhạc trong `audio/` |
| `GET /api/voices` | Danh sách giọng Vbee |

### Jobs

| Endpoint | Mô tả |
|----------|-------|
| `GET /api/jobs/{job_id}` | Trạng thái job (status, progress, log, output) |
| `GET /api/jobs/{job_id}/download` | Download `final.mp4` |

---

## 4. API nhận request → tạo job

**`POST /api/generate`** (`api.py:generate_video`):

1. Tạo `job_id` = UUID 8 ký tự
2. Tạo thư mục project:
   ```
   projects/project-{job_id}/
   ├── assets/
   │   ├── audio/       ← narration.mp3, subtitles.srt
   │   ├── video/       ← scene-01.mp4, scene-02.mp4 ...
   │   └── music/       ← background.mp3
   └── renders/
       └── final.mp4
   ```
3. Lưu file upload vào `uploads/`
4. Tạo job object `{id, status:"queued", progress:0, log:[], output:null}`
5. Thêm `run_pipeline(job_id)` vào `BackgroundTasks`
6. Trả về `{"job_id": "..."}` ngay lập tức (non-blocking)
7. Job persist vào `jobs/{id}.json` → không mất sau restart

---

## 5. Pipeline chạy ngầm: `run_pipeline()`

### 5.1 — Lên kế hoạch scenes: `_plan_scenes()`

| Duration | Số clips | Ghi chú |
|----------|----------|---------|
| ≤ 32s | 2 clips | 1 batch, loop để đủ duration |
| ≤ 60s | 3 clips | |
| > 60s | 4 clips | tối đa |

Mỗi scene:
- Lấy 1 chunk script (phân tích bằng `_parse_script_chunks()` → ưu tiên `[SCENE]` marker → paragraph → line)
- Ghép với visual template luân phiên (cinematic, lotus, particles, Buddha, meditation, temple)
- Nếu có `veo_style_seed` → append vào mỗi prompt
- Scene đầu tiên + có ảnh upload → `operation = image_to_video`
- Duration mỗi clip: luôn **8 giây**

### 5.2 — Xác định tool video

```python
_useapi_tool   = GoogleFlowUseAPI()    # ưu tiên 1
_direct_tool   = GoogleFlowDirect()   # ưu tiên 2
_playwright_tool = GoogleFlowVideo()  # ưu tiên 3 (last resort)

_use_useapi = USEAPI_TOKEN + USEAPI_GOOGLE_FLOW_EMAIL đã set
_use_direct = không có useapi.net + GOOGLE_FLOW_COOKIES đã set
```

### 5.3 — Chạy song song (ThreadPoolExecutor)

```
Thread pool: max_workers = 2 (TTS + Music) + N (images) + N (clips)

├── do_tts()        → Vbee API
├── do_music()      → Local file (~0s) → Suno (fallback)
├── do_image(0)     → GoogleFlowImage nano-banana-pro  ┐ song song
├── do_image(1)     → GoogleFlowImage nano-banana-pro  │ (serialize qua
├── do_image(N)     → GoogleFlowImage nano-banana-pro  ┘  _img_browser_lock)
│       ↓ image_futs[i].result() (clip chờ ảnh xong)
├── do_clip(0)     → useapi.net [image_to_video]  ┐ song song
├── do_clip(1)     → useapi.net [image_to_video]  │ (không rate limit
└── do_clip(N)     → useapi.net [image_to_video]  ┘  khi dùng useapi.net)
```

> **NanaBanana Pro:** Chạy song song với TTS/Music. Mỗi scene có 1 ảnh riêng → feed Veo `image_to_video`.  
> Scene 0 xong → `job["overview_image"]` được set ngay → frontend hiển thị preview ảnh trong khi video đang generate.  
> **useapi.net:** Submit tất cả clips cùng lúc, không cần rate limit client-side.  
> **Direct/Playwright:** `_veo_acquire()` sliding window ≤ 2 req/60s tự throttle.

---

## 5.4 — Worker: Ảnh scene — `do_image(i, scene)` *(NanoBanana Pro)*

**Tool:** `GoogleFlowImage` (`tools/graphics/google_flow_image.py`)  
**Model:** `nano-banana-pro`  
**Khi nào chạy:** Chỉ khi `GOOGLE_FLOW_COOKIES` + `GOOGLE_FLOW_EMAIL` đã set (status = `AVAILABLE`)

1. `_nano_banana_prompt(veo_prompt, aspect_ratio)` → convert Veo motion prompt → Nano Banana static image brief
   - Strip motion keywords: slow motion, camera pan, tracking shot, 4K, loop…
   - Append lens directives: 85mm f/2.0, volumetric golden light, photorealistic
   - Format: `Subject + Action + Context + Composition + Lighting` (Perfect Prompt formula)
2. Gọi `GoogleFlowImage.execute({model: "nano-banana-pro", ...})`
3. Lưu vào `assets/images/scene-NN.jpg`
4. Scene 0 → `job["overview_image"] = img_path` (lưu ngay, frontend hiển thị preview)
5. Clip i đợi `image_futs[i].result()` rồi mới submit Veo với `image_to_video`

**Graceful degradation:** Nếu không có cookies → skip NanaBanana, clips chạy text-to-video như cũ.  
**Serialize:** `_img_browser_lock` — chỉ 1 browser session image tại 1 thời điểm (tránh conflict Opera profile).

---

## 6. Worker: TTS — `do_tts()`

**Tool:** `VbeeTTS` (`tools/audio/vbee_tts.py`)

1. Preprocess script: `…` / `...` → `, ` (tránh TTS đọc sai)
2. Gọi Vbee API với `voice_code`, `speed_rate`
3. Poll + download audio → `assets/audio/narration.mp3`
4. Nếu `subtitle_enabled`:
   - Whisper transcribe → SRT → `assets/audio/subtitles.srt`

**Fallback:** TTS fail → pipeline raise lỗi

---

## 7. Worker: Nhạc nền — `do_music()`

```
1. Local file: audio/{music_file} tồn tại → shutil.copy2 → instant ✅

2. Suno API (chỉ khi local fail):
   └─ suno.execute({ instrumental:True, model:"V4", prompt:"ambient meditation..." })

3. Không có nhạc → tiếp tục chỉ với narration
```

> Default `bamboo-breath_bKoDGfET.wav` → copy tức thì, critical path không bị block.

---

## 8. Worker: Video clip — `do_clip(i, scene)`

### Tool priority chain:

```
┌─────────────────────────────────────────────────────────┐
│ 1. GoogleFlowUseAPI (useapi.net)               [STABLE]  │
│    POST https://api.useapi.net/v1/google-flow/videos     │
│    Auth: Bearer USEAPI_TOKEN                             │
│    Async: true → poll GET /jobs/{jobid} mỗi 5s           │
│    Video URL: response.operations[0].operation           │
│               .metadata.video.fifeUrl                    │
│    Retry: 3 lần POST timeout (60s/attempt)               │
│    Rate limit: none từ client — useapi.net tự queue      │
├─────────────────────────────────────────────────────────┤
│ 2. GoogleFlowDirect (direct HTTP)         [EXPERIMENTAL] │
│    POST https://aisandbox-pa.googleapis.com/...          │
│    Auth: SAPISIDHASH (SHA1 từ SAPISID cookie)            │
│    Optional: CapSolver reCAPTCHA v3 (~$0.003/video)      │
│    Poll: GET operation endpoint mỗi 5s                   │
│    Rate limit: _veo_acquire() ≤ 2 req/60s                │
├─────────────────────────────────────────────────────────┤
│ 3. GoogleFlowVideo (Playwright CDP)       [EXPERIMENTAL] │
│    Connect to Opera thật qua CDP port 9222               │
│    Không inject webdriver flag → Google không detect     │
│    Fallback chain:                                       │
│      a. connect_over_cdp (Opera đang chạy với 9222)      │
│      b. launch Opera với --remote-debugging-port=9222    │
│      c. launch_persistent_context (Opera profile)        │
│      d. headless Chromium + cookie inject                │
│    Rate limit: _veo_acquire() ≤ 2 req/60s                │
└─────────────────────────────────────────────────────────┘
```

### Stop conditions (không fallback, báo lỗi ngay):
- useapi.net: HTTP 401 (bad token)
- Direct HTTP: session expired / 401
- Playwright: session expired

### Output: `assets/video/scene-NN.mp4`

---

## 9. Compose video: `_compose()`

Toàn bộ dùng **FFmpeg** — 5 bước tuần tự:

### Step 1: Trim từng clip
```bash
ffmpeg -i scene-NN.mp4 -t 8 -an -c:v libx264 -crf 20 trim_NN.mp4
```

### Step 2: Concat + loop clips để đủ duration
```bash
# Concat 1 lần
ffmpeg -f concat -safe 0 -i concat.txt -an -c:v libx264 concat_once.mp4

# Loop nếu tổng clip < duration
ffmpeg -stream_loop N -i concat_once.mp4 -t {duration} raw_video.mp4
```

### Step 3: Mix audio

**Có nhạc:**
```
[narration] + [background loop] → amix
  - Nhạc: volume 0.2 (20%), fade in 1s, fade out 3s cuối
  - Narration: apad để đủ duration
```

**Không nhạc:**
```
[narration] map thẳng vào video
```

### Step 4: Burn phụ đề + watermark (nếu có)

| Feature | Filter |
|---------|--------|
| Phụ đề SRT | `subtitles=...` (libass) |
| Watermark text | `drawtext=...` |
| Watermark ảnh | `scale 15% + overlay` |

### Step 5: Cleanup intermediates
Xóa `trim_*.mp4`, `concat.txt`, `concat_once.mp4`, `raw_video.mp4`, `with_audio.mp4`.
Chỉ giữ `renders/final.mp4`.

---

## 10. Job hoàn thành

```python
job["status"] = "done"
job["output"] = "projects/project-{id}/renders/final.mp4"
_save_job(job)  # → jobs/{id}.json
```

---

## 11. Frontend poll trạng thái

```javascript
// Poll mỗi 2 giây
GET /api/jobs/{job_id}
→ { status, progress, stage, log, output }

// Khi status == "done":
//   Hiển thị video player + nút download
//   URL: /projects/project-{id}/renders/final.mp4
//   Download: GET /api/jobs/{id}/download
```

---

## 12. Setup video generation

### Cách 1 — useapi.net (recommended)
```
USEAPI_TOKEN=user:XXXX-YYYYYYY
USEAPI_GOOGLE_FLOW_EMAIL=your@gmail.com
```
Qua UI: `POST /api/useapi/config` với `{token, email}`.

### Cách 2 — Opera CDP (fallback, free)
```bash
# Tắt Opera cũ, chạy lại với debug port:
/Applications/Opera.app/Contents/MacOS/Opera --remote-debugging-port=9222 --no-first-run

# Hoặc qua script:
python3 tools/video/google_flow_video.py --launch-opera

# Hoặc qua API:
POST /api/opera/launch
```
Đăng nhập Google Flow trong Opera như bình thường. Tool tự connect vào session.

### Cách 3 — Direct HTTP + Cookies (fallback)
```
GOOGLE_FLOW_EMAIL=your@gmail.com
GOOGLE_FLOW_COOKIES=[{"name":"SID","value":"..."}]
CAPSOLVER_API_KEY=xxx  # optional ~$0.003/video
```

---

## Thời gian ước tính

### Dùng useapi.net (30s video, 2 clips song song)

| Bước | Thời gian |
|------|-----------|
| TTS (Vbee) | ~5-10s |
| Nhạc local (copy) | ~0s ✅ |
| Veo clip 1 (useapi.net) | ~60-90s |
| Veo clip 2 (song song) | ~60-90s *(chạy cùng lúc)* |
| FFmpeg compose | ~10-20s |
| **Tổng (critical path)** | **~1.5-2 phút** |

### Dùng Direct HTTP / Playwright (rate limited)

| Bước | Thời gian |
|------|-----------|
| Veo clip 1 | ~60-120s |
| Veo clip 2 | ~60-120s *(đợi rate limit slot)* |
| **Tổng** | **~2-4 phút** |

---

## Cấu trúc thư mục

```
audio/                          ← nhạc local (user đặt vào)
│   bamboo-breath_bKoDGfET.wav
│   Sandstonebelss.wav

projects/project-{id}/
├── assets/
│   ├── audio/
│   │   ├── narration.mp3
│   │   └── subtitles.srt       (nếu subtitle_enabled)
│   ├── video/
│   │   ├── scene-01.mp4
│   │   └── scene-02.mp4
│   └── music/
│       └── background.mp3
└── renders/
    └── final.mp4               ← đầu ra (intermediates đã xóa)

jobs/
└── {id}.json                   ← persist qua restart

uploads/
└── {id}_{filename}             ← file upload từ user

tools/video/
├── google_flow_useapi.py       ← useapi.net API (ưu tiên 1)
├── google_flow_direct.py       ← direct HTTP reverse-engineered (ưu tiên 2)
└── google_flow_video.py        ← Playwright CDP Opera (ưu tiên 3)
```

---

## Danh sách vấn đề đã biết

| # | Vấn đề | Nhóm |
|---|--------|------|
| 1 | Whisper redundant — Vbee không trả timestamps | Audio |
| 2 | stream_loop -1 + amix — tiềm ẩn race condition FFmpeg | Audio |
| 3 | Không có global Veo rate limiter across nhiều jobs | Performance |
| 4 | BackgroundTasks không phải real queue — mất job khi restart giữa chừng | Reliability |
| 5 | Frontend polling 2s — nên dùng SSE/WebSocket | UX |
| 6 | Video loop lộ từ giây 16 | Output |
| 7 | Không có scene transition — hard cut | Output |
| 8 | Subtitle tắt mặc định | Output |
| 9 | Không có audio ducking | Audio |
| 10 | Loop nhạc lộ điểm lặp — thiếu crossfade | Audio |
| 11 | Narration thiếu hậu kỳ (EQ, normalize) | Audio |
| 12 | Visual prompt cứng, generic | Output |
| 13 | Không có intro/outro branded, không có CTA | Output |
| 14 | Opera CDP: phải chạy thủ công khi restart máy | DevEx |
