# OpenMontage — Tài liệu Hệ thống & Thiết kế

> Tài liệu này mô tả kiến trúc, các thành phần, luồng vận hành và những quyết định thiết kế cốt lõi của OpenMontage. Đối tượng đọc: kỹ sư mới onboard, người tích hợp tool/pipeline mới, và người vận hành sản phẩm.

---

## 1. Tổng quan sản phẩm

OpenMontage là một **nền tảng sản xuất video tự động bằng AI agent**, mã nguồn mở. Sản phẩm cho phép người dùng đi từ một ý tưởng (hoặc một đoạn video tham khảo) đến một video hoàn thiện thông qua một quy trình (pipeline) gồm nhiều giai đoạn (stage), mỗi stage có thể sinh kịch bản, ảnh, video clip, lồng tiếng, nhạc nền, phụ đề và dựng (compose) ra file MP4 cuối.

Điểm khác biệt cốt lõi: **AI agent CHÍNH LÀ orchestrator**. Không có một “engine” Python điều phối state machine. Thay vào đó, agent đọc các file hướng dẫn (YAML manifest + Markdown skill) và tự lái pipeline. Python chỉ tồn tại để (1) cung cấp tool có khả năng cụ thể, và (2) đọc/ghi trạng thái (checkpoint, artifact, registry).

Hệ quả thiết kế:
- Logic sáng tạo, logic review, logic chuyển stage đều **không nằm trong code Python** mà nằm trong skill (markdown).
- Thay đổi quy trình sản xuất → sửa YAML/MD, không phải sửa code.
- Thay đổi nhà cung cấp (provider) → thêm một file tool mới, registry tự discover.

---

## 2. Triết lý kiến trúc: Instruction-Driven (Agent-First)

```
Agent đọc pipeline manifest (YAML)
   → đọc stage director skill (MD)
      → gọi tools (Python BaseTool)
         → tự review (meta skill)
            → ghi checkpoint (Python utility)
               → trình human approve
```

Ba nguyên tắc bất biến:

1. **Không có Python orchestrator.** Không có file nào đóng vai trò "main loop" sản xuất. Agent là main loop.
2. **Không có quyết định sáng tạo trong Python.** Code không chọn provider, không chọn prompt, không quyết định cắt ở đâu. Skill và agent quyết.
3. **Không có review logic trong Python.** Reviewer là một meta skill (markdown), advisory, tối đa 2 vòng.

Lợi ích: thay model agent (Claude → GPT → Gemini) không cần đổi code. Đổi pipeline không cần redeploy. Toàn bộ "trí tuệ" có thể version-control như tài liệu.

---

## 3. Kiến trúc 3 lớp tri thức (3-Layer Knowledge Architecture)

| Layer | Vị trí | Trả lời câu hỏi | Ví dụ |
|---|---|---|---|
| **Layer 1 — Tools** | `tools/`, `tools/tool_registry.py` | "Có những tool nào? Available chưa? Cost bao nhiêu? Runtime gì?" | `kling_video.py`, `elevenlabs_tts.py` |
| **Layer 2 — Project Skills** | `skills/` | "OpenMontage MUỐN dùng tool đó như thế nào trong pipeline này?" | `skills/pipelines/cinematic/script-director.md` |
| **Layer 3 — Vendor Skills** | `.agents/skills/` | "Bản thân công nghệ/nhà cung cấp đó hoạt động ra sao?" | Cách viết prompt cho Kling, cú pháp camera direction |

Cầu nối Layer 1 → Layer 3 nằm trong field `agent_skills[]` của mỗi tool. Trước khi gọi bất kỳ tool sinh nội dung nào, agent **bắt buộc** đọc skill Layer 3 tương ứng. Đây là khác biệt giữa "prompt generic" và "prompt cinematic".

> Quy tắc: **Không bao giờ đọc source code Python để biết cách dùng tool.** Nếu cần thông tin đó, nó phải nằm ở Layer 2/3. Đọc `.py` để hiểu input schema là một anti-pattern.

---

## 4. Cấu trúc thư mục

```
OpenMontage/
├── AGENT_GUIDE.md            # Hợp đồng vận hành agent (bắt buộc đọc đầu tiên)
├── PROJECT_CONTEXT.md        # Single source of truth về kiến trúc
├── CLAUDE.md / CODEX.md / …  # Trỏ về AGENT_GUIDE
├── api.py                    # HTTP API layer (nếu chạy server mode)
├── config.yaml               # Config global
│
├── pipeline_defs/            # YAML manifest cho từng pipeline
│   ├── animated-explainer.yaml
│   ├── cinematic.yaml
│   └── …
│
├── skills/
│   ├── meta/                 # reviewer, checkpoint-protocol, onboarding…
│   ├── pipelines/<pipeline>/ # Stage director skills (idea → publish)
│   └── INDEX.md
│
├── tools/                    # Tools (Layer 1) — kế thừa BaseTool
│   ├── base_tool.py
│   ├── tool_registry.py
│   ├── cost_tracker.py
│   ├── audio/    video/    image/    avatar/
│   ├── analysis/ enhancement/ graphics/ subtitle/ publishers/
│
├── schemas/                  # JSON Schema cho artifact, checkpoint, manifest, playbook
│   ├── artifacts/   checkpoints/   pipelines/   styles/   tools/
│
├── styles/                   # Style playbook YAML (clean-professional, …)
│
├── lib/                      # Hạ tầng thuần kỹ thuật
│   ├── checkpoint.py
│   ├── pipeline_loader.py
│   ├── config_model.py
│   └── media_profiles.py
│
├── remotion-composer/        # Project Remotion (React) — animation engine
│
├── projects/                 # (gitignored) workspace mỗi lần chạy
│   └── <project-name>/
│       ├── artifacts/  assets/  renders/
│
├── music_library/            # (gitignored) nhạc royalty-free do user thả vào
├── tests/                    # contract tests + qa tests
└── docs/                     # Tài liệu sâu (ARCHITECTURE.md…)
```

---

## 5. Pipeline State Machine

Mỗi pipeline là một state machine có dạng:

```
research → idea → script → scene_plan → assets → edit → compose → publish
```

Không phải pipeline nào cũng dùng đủ 8 stage (ví dụ `framework-smoke` chỉ có 2). Manifest YAML khai báo các stage thực tế, kèm:

- `skill`: đường dẫn director skill (md)
- `produces`: tên artifact canonical sinh ra
- `tools_available`: tool/selector mà stage được phép dùng
- `review_focus`: tiêu chí mà reviewer cần quan tâm
- `success_criteria`: bar chất lượng để pass
- `human_approval_default`: có cần human duyệt không

Mỗi stage sinh đúng **một artifact canonical** (JSON), phải validate được với schema trong `schemas/artifacts/`. Artifact là **hợp đồng** giữa hai stage liên tiếp — stage sau chỉ phụ thuộc artifact, không phụ thuộc cách stage trước thực thi.

| Stage | Artifact canonical | Chất lượng tối thiểu |
|---|---|---|
| `idea` | `brief` | Hook rõ, platform, duration, tone, intent |
| `script` | `script` | Section có cấu trúc, timing hợp lệ, narration mạch lạc |
| `scene_plan` | `scene_plan` | Scene có thứ tự, timing, asset requirement |
| `assets` | `asset_manifest` | Provenance, paths, model/tool metadata |
| `edit` | `edit_decisions` | Cut, overlay, subtitle, music quyết định cụ thể |
| `compose` | `render_report` | Output path, encoding profile, verification |

Checkpoint của stage được ghi tại `pipelines/<project_id>/checkpoint_<stage>.json` với một trong các status: `in_progress`, `completed`, `failed`, `awaiting_human`. Checkpoint `completed`/`awaiting_human` **bắt buộc** chứa artifact canonical hợp lệ — vi phạm là contract violation và phải fail-fast.

---

## 6. Pipeline & Stage Director — Mô hình "Manifest + Director Skill"

Mỗi pipeline gồm 2 phần tách biệt rõ:

**(a) Manifest YAML — phần KHAI BÁO** (`pipeline_defs/<name>.yaml`)
Nói WHAT: gồm những stage nào, mỗi stage cần tool gì, tiêu chí review là gì, có cần human approve không.

**(b) Director Skill MD — phần HƯỚNG DẪN** (`skills/pipelines/<name>/<stage>-director.md`)
Nói HOW: dạy agent cách thực thi stage đó. Bao gồm workflow, ví dụ tốt/xấu, các pitfall, cách craft prompt cho stage này.

Tách như vậy để: manifest có thể được **máy validate** (qua JSON schema), còn director skill có thể **viết bằng ngôn ngữ tự nhiên** với độ phong phú không giới hạn — đúng định dạng mà LLM tiêu hoá tốt nhất.

### Pipelines hiện có

| Pipeline | Mô tả | Trạng thái |
|---|---|---|
| `animated-explainer` | Topic → explainer hoàn toàn AI-generated | production |
| `cinematic` | Trailer/teaser/mood-led edit | production |
| `animation` | Motion graphics, animation-first | production |
| `screen-demo` | Screen recording walkthrough | production |
| `hybrid` | Source footage + asset hỗ trợ | production |
| `avatar-spokesperson` | Talking avatar / lip-sync | production |
| `talking-head` | Speaker video footage-led | beta |
| `clip-factory` | Cắt nhiều clip ngắn từ một source dài | beta |
| `podcast-repurpose` | Repurpose podcast | beta |
| `localization-dub` | Subtitle, dub, dịch | beta |
| `framework-smoke` | Test 2-stage tối thiểu | test |

---

## 7. Tool System (Layer 1)

### 7.1. BaseTool contract

Mọi tool production phải kế thừa `tools/base_tool.py::BaseTool` và khai báo:

- `name`, `version`, `tier`
- `capability` (vd `tts`, `video_generation`, `image_generation`, `music_generation`, `video_post`, `audio_processing`, `analysis`, `avatar`, `enhancement`)
- `provider` (vd `elevenlabs`, `kling`, `ffmpeg`, `openai`)
- `runtime` (`LOCAL`, `API`, `LOCAL_GPU`, `HYBRID`)
- `supports`, `fallback_tools`
- `agent_skills[]` (Layer 3 references)
- `install_instructions`, `dependencies` — để Provider Menu tự generate setup guide
- `execute(params_dict) → ToolResult` (không phải `.run()`)

Quy ước đặt tên class: **PascalCase, KHÔNG có suffix `Tool`**. Ví dụ `MusicGen`, `VideoCompose`, `ElevenLabsTTS`.

### 7.2. Tool Registry

`tools/tool_registry.py` là **single source of truth** runtime. Agent **không bao giờ** giữ danh sách tool hardcode; mọi câu hỏi "có tool gì" đều phải đi qua registry:

```python
registry.discover()
registry.support_envelope()      # capability + status + resource
registry.capability_catalog()    # group theo capability
registry.provider_catalog()      # group theo provider
registry.provider_menu()         # X/Y configured cho mỗi capability
```

Registry tự discover bằng cách scan các module trong `tools/`, gọi `get_info()` và phân loại theo `capability`/`provider`/`status`.

### 7.3. Selector Pattern

Với các capability có nhiều provider, OpenMontage dùng pattern **selector + concrete provider tools**. Ba selector hiện có:

| Selector | Route tới | Cách discover |
|---|---|---|
| `tts_selector` | mọi tool `capability="tts"` | `registry.get_by_capability("tts")` |
| `image_selector` | mọi tool `capability="image_generation"` | tương tự |
| `video_selector` | mọi tool `capability="video_generation"` | tương tự |

Selector route theo thứ tự: **user preference > availability > discovery order**, và tự adapt input schema giữa các provider. Hệ quả: thêm provider mới chỉ cần viết một file tool mới, **không cần sửa selector**.

### 7.4. Cost Tracker

`tools/cost_tracker.py` quản lý budget theo vòng đời `estimate → reserve → reconcile`. Mỗi tool sinh nội dung phải thông báo estimate trước khi gọi, reserve khi bắt đầu, reconcile khi xong (kể cả failure).

---

## 8. Render Engine: FFmpeg + Remotion

Module `video_compose` có hai render engine:

| Engine | Dùng cho | Yêu cầu |
|---|---|---|
| **FFmpeg** | Cut/concat/trim, burn subtitle, video-only ops | binary `ffmpeg` (luôn có) |
| **Remotion** | Animation từ ảnh tĩnh, text card, stat card, chart, callout, comparison, transition spring physics | Node.js + project `remotion-composer/` |

Routing tự động qua `_needs_remotion()`. Tuy nhiên agent **phải biết Remotion có sẵn ngay từ stage proposal**, vì điều đó quyết định cách thiết kế phương án visual (animated scene vs Ken Burns trên ảnh tĩnh).

**Quy tắc cứng — Motion-Required Requests:** Với các brief mà tính motion là cốt lõi (trailer sci-fi, teaser cinematic, hype edit, avatar video), Remotion là **hard requirement**. Cấm tự ý fallback sang Ken Burns/animatic mà không có user approval. Nếu Remotion fail → bubble lên ngay, dừng và escalate.

`remotion-composer/src/components/` hiện có 8 component: TextCard, StatCard, ProgressBar, CalloutBox, ComparisonCard và folder `charts/`.

---

## 9. Style Playbook System

Mỗi style là một file YAML trong `styles/`, validate theo `schemas/styles/playbook.schema.json` (v2) — chứa các design token: `chart_palette`, `scale_system`, `weight_matrix`, `color_rules`, typography, motion, audio, asset constraints. Loader: `styles/playbook_loader.py` (kèm design intelligence cho color/typography/a11y).

Playbook hiện có:

| Playbook | Phù hợp |
|---|---|
| `clean-professional` | Corporate, education, SaaS |
| `flat-motion-graphics` | Social, TikTok, startup |
| `minimalist-diagram` | Technical deep-dive, architecture |

Manifest pipeline khai báo những playbook tương thích — agent chỉ chọn trong tập đó.

---

## 10. Artifact, Checkpoint, Schema Layer

`schemas/` là tầng "hợp đồng dữ liệu" của hệ thống:

- `schemas/artifacts/` — schema cho `brief`, `script`, `scene_plan`, `asset_manifest`, `edit_decisions`, `render_report`, `publish_log`
- `schemas/checkpoints/checkpoint.schema.json` — schema checkpoint
- `schemas/pipelines/pipeline_manifest.schema.json` — schema manifest pipeline
- `schemas/styles/playbook.schema.json` — schema playbook v2
- `schemas/tools/` — schema cho tool có I/O phức tạp

Kết hợp với `lib/checkpoint.py` và `lib/pipeline_loader.py`, hệ thống đảm bảo: **mọi giao tiếp giữa các thành phần đều đi qua artifact/checkpoint validate được**, không phụ thuộc pickle hay shared memory.

---

## 11. Project Workspace Convention

Mỗi lần chạy tạo một project workspace gitignored dưới `projects/<project-name>/`:

```
projects/<project-name>/
├── artifacts/        # JSON các stage
└── assets/
    ├── images/       # PNG sinh ra
    ├── video/        # MP4 clip
    ├── audio/        # narration + final mix
    ├── music/        # nhạc nền
    └── subtitles.srt
└── renders/
    └── final.mp4     # deliverable cuối
```

Naming: kebab-case từ tên video (vd `hidden-math-of-nature`). Workspace phải được tạo **trước** khi chạy stage đầu tiên. Tool tuyệt đối không ghi ra repo root hay vị trí ad-hoc.

Người dùng có thể thả nhạc royalty-free vào `music_library/` (gitignored). Asset director bắt buộc kiểm tra folder này **trước** khi fallback sang music generation API.

---

## 12. Communication Protocol

### 12.1. Decision Communication Contract

Trước mỗi call sinh nội dung tốn phí, agent phải announce:
- tên tool, provider, model/variant
- lý do chọn
- sample run hay batch run

### 12.2. Major Change Approval

Bất kỳ thay đổi lớn nào — switch provider, switch model, switch render engine, đổi từ video-led sang still-led, drop narration/music — đều phải xin user duyệt trước. Cấm âm thầm substitute.

### 12.3. Blocker Escalation

Khi gặp blocker, agent dùng cấu trúc 5 bước: (1) đã thử gì, (2) fail vì sao, (3) phân loại (auth / provider access / tool bug / quality), (4) các lựa chọn còn lại, (5) recommend lựa chọn nào.

---

## 13. Reviewer & Checkpoint Protocol

Reviewer là **meta skill** (`skills/meta/reviewer.md`), không phải code:

- Tự review sau mỗi stage, **trước** khi checkpoint
- Đọc `review_focus` từ manifest cho stage đó
- Tối đa 2 vòng review; sau đó pass with warnings và đi tiếp
- Phân loại finding: `critical` (phải fix) / `suggestion` (nên fix) / `nitpick` (đẹp thì sửa)
- `quality_rules` của playbook là constraint, không phải gợi ý

Checkpoint protocol (`skills/meta/checkpoint-protocol.md`) quyết định khi nào pause:

- Đọc `human_approval_default` từ manifest theo stage
- Stage sáng tạo (`idea`, `script`, `scene_plan`) → mặc định cần human duyệt
- Stage kỹ thuật (`assets`, `edit`, `compose`) → mặc định auto-proceed
- Khi cần duyệt: present artifact summary + review findings + cost snapshot, đợi `approve / revise / abort`

---

## 14. Preflight & Provider Menu

Trước bất kỳ creative work nào, agent **bắt buộc** chạy preflight:

1. `registry.support_envelope()` — biết tổng quan capability
2. `registry.provider_menu()` — biết X/Y provider đã configured cho từng capability
3. Đọc manifest pipeline đã chọn, đối chiếu `required_tools` với registry
4. Báo cáo trạng thái: `passed` / `degraded` / `blocked`
5. Trình "Capability Menu" cho user — KHÔNG được trình flat tool list

Quy tắc trình bày:
- Group theo capability, không theo từng tool lẻ
- Luôn show ratio "X of Y configured"
- Show ngay những gì user CÓ THỂ làm → rồi mới show những gì CÓ THỂ unlock
- KHÔNG hardcode tên provider / API key / setup URL — đọc từ `install_instructions` và `dependencies`
- User từ chối setup → đi tiếp với best available, không nhắc lại

---

## 15. Quy trình thêm thành phần mới

### Thêm pipeline mới
1. Tạo manifest YAML trong `pipeline_defs/` (validate qua `pipeline_manifest.schema.json`)
2. Tạo các director skill trong `skills/pipelines/<name>/` (đủ stage idea → publish)
3. Reference các meta skill (reviewer, checkpoint-protocol) trong manifest
4. Khai báo các playbook tương thích
5. Thêm contract test trong `tests/contracts/`

### Thêm tool mới
1. Kế thừa `tools/base_tool.py::BaseTool`
2. Đặt vào đúng package theo capability (`tools/audio/`, `tools/video/`, …)
3. Theo pattern selector + provider nếu là capability đa nhà cung cấp
4. Set đầy đủ contract field (capability, provider, supports, fallback_tools, agent_skills, install_instructions, dependencies…)
5. Implement `execute()` trả `ToolResult`
6. Để registry tự discover, **không** import ad-hoc
7. Thêm JSON schema trong `schemas/tools/` nếu I/O phức tạp
8. Test sau khi runtime path đã đúng

### Thêm provider mới cho capability đã có
- Chỉ cần viết 1 file tool mới với đúng `capability=…`. Selector tự động pick up.

---

## 16. Anti-patterns (Tuyệt đối tránh)

- Viết script ad-hoc gọi tool trực tiếp, bỏ qua pipeline → vi phạm Rule Zero
- Gọi tool sinh nội dung mà không đọc Layer 3 skill của tool đó
- Skip stage director skill, "đoán" cách làm
- Đọc `tools/*.py` để hiểu input schema (đó là việc của skill, không phải của code)
- Hardcode tên provider, API key name, URL setup trong prompt
- Bắt đầu generate asset trước khi user approve production plan
- Âm thầm substitute provider/model/render path khi blocker xảy ra
- Dùng tên legacy đã xoá: `tts_cloud`, `tts_engine`, `video_gen`
- Trình tool unavailable lẻ tẻ thay vì show toàn cảnh capability
- Skip Provider Menu khi preflight
- Fallback motion-led video sang still-led mà không có user approval
- Maintain hardcoded tool list trong prompt (luôn query registry)

---

## 17. Tóm tắt nguyên lý thiết kế

| Nguyên lý | Hệ quả |
|---|---|
| Agent là orchestrator | Đổi model agent không cần đổi code |
| Instruction-driven (YAML + MD) | Đổi quy trình không cần redeploy |
| 3-layer knowledge (tool / project skill / vendor skill) | Tách rõ "có gì" / "dùng ra sao trong project" / "công nghệ đó hoạt động thế nào" |
| Selector + provider | Thêm vendor mới = thêm 1 file, không sửa selector |
| Artifact canonical + JSON schema | Stage decoupled qua hợp đồng dữ liệu |
| Reviewer/checkpoint là meta skill | Không có review/checkpoint logic chôn trong code |
| Registry là single source of truth runtime | Không có hardcoded tool list |
| Project workspace tách biệt | Mọi output regenerable, gitignored |
| Decision communication contract | User không bao giờ phải đoán agent đã chọn gì |

---

*File này được sinh tự động dựa trên `AGENT_GUIDE.md`, `PROJECT_CONTEXT.md` và cấu trúc thư mục thực tế của repository tại thời điểm ngày 2026-04-07.*
