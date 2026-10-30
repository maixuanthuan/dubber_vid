# CONTEXT — DUBBER VIDEO

Đọc kèm `CACH_LAM_VIEC.md`. File này ghi **hiện tại và việc còn lại**, không ghi lịch sử.
Cập nhật lần cuối: 2026-10-30, cuối phiên 2 (đã áp tới gói 025; gói 019 KHÔNG áp, bỏ hẳn).

---

# PHẦN 1 — ĐỌC HẾT MỖI PHIÊN

## Hệ là gì (một câu)

Đưa vào một video tiếng Trung (Hàn đang thử), nhận lại bản lồng tiếng Việt bằng AI, khớp thời gian
từng câu. Đích xa là studio, ở đó sửa được từng câu rồi render lại.

## Phạm vi đã chốt

| Mục | Đã chốt |
|---|---|
| Mục đích | Dự án cá nhân, chạy trên host của Thuận. Hiện cho **vài người bạn dùng thử** qua link (Cloudflare Tunnel). Sau này có thể thương mại hoá, khi đó thuê server hoặc platform |
| Nội dung | **Một người kể** (video recap/kể chuyện). Nhiều nhân vật thì không quan tâm. Chưa cần Demucs/diarization |
| Ngôn ngữ | Nguồn: Trung (chạy tốt), Hàn (chưa test). Đích: Việt. Đa ngôn ngữ nguồn: đã phân tích, chưa làm (xem việc mở) |
| Hướng | B: dự án sửa được từng câu. **Chưa làm phần sửa**, hiện chỉ có chạy một lệnh ra video |
| Dịch | LLM qua API (Gemini miễn phí). Bản chia sẻ chỉ cho Flash-Lite (~500 lượt/ngày; Flash ~20) |
| Máy | RTX A3000 Laptop 6 GB (sm86), driver 535.309.01 (CUDA 12.2), i9-11950H, RAM 30 GB, Ubuntu 22.04.5 |
| Trang giới thiệu | Trang **tĩnh** (không gọi API server, có test khoá), nền tối, song ngữ VI/EN, tác giả `@maixuanthuan` ở chân trang |
| Chia sẻ | `bash tools/share.sh` (một lệnh). Video tối đa 10 phút, chỉ Flash-Lite, mã mời **tuỳ chọn** (Thuận chọn không dùng) |

## Ràng buộc bắt buộc (Thuận đặt)

- **Không nâng driver, CUDA, gói hệ thống.** Mọi thứ cài trong conda env, không đụng system.
  File chạy lẻ (cloudflared) đặt trong `~/.local/bin`, không sudo.
- **Claude không tự chạy hay deploy.** Claude chuẩn bị gói, Thuận chạy rồi gửi lại output.
- Giao file theo kiểu: tải về Downloads, áp bằng `tools/apply_pkg.sh`. Không để file rác.
- **Chưa dùng git.** apply_pkg tự sao lưu vào `.backups/`.
- API key để trong `.env` (có `.env.example`). Không dán key vào chat, không đưa key vào gói.
- **Người dùng không được thấy log, debug hay báo cáo kỹ thuật.** Chỉ dành cho người phát triển.
- Không cần cập nhật context liên tục: chỉ chốt một lần cuối phiên.
- Xem video bằng VLC.
- **Lời hứa trên trang giới thiệu chỉ nói cái đã làm được và đã đo.** Cái chưa có ghi "Sắp ra mắt".
  Bảng giá không ghi số khi chưa đo chi phí mỗi phút video.

## Đang ở đâu

Đã áp tới **gói 025** (015–018, 020–025; 019 bỏ). Test: **66 passed**, chạy trong `dub_tts`.
- Trang giới thiệu mới (015–018, 025): hero sóng âm + giải mã chữ, demo kiểu phòng dựng (video, phổ âm
  thật, timeline 2 track), dải câu chạy, con số bằng chứng (tính từ dữ liệu), "Vì sao Dubber" 6 thẻ,
  quy trình, kho giọng (voiceprint + huy hiệu lối đọc, lọc), lộ trình, bảng giá Beta miễn phí, footer.
- TTS chạy **GPU** (023): env `dub_tts_gpu`, đọc theo lô 16. Thuận đã nghe và chốt "dùng GPU là đúng".
- Chia sẻ qua Cloudflare Quick Tunnel bằng một lệnh (021, 024). Đã chạy thật, có link.
- Deploy Modal (020): code xong, kiểm khai báo với modal 1.6.3, **chưa deploy được**: L4 cần gắn thẻ,
  thẻ Visa của Thuận bị từ chối. Giữ code để sau.

**Việc ngay đầu phiên sau:** hỏi Thuận đã chạy `pip freeze > envs/dub_tts_gpu.txt` chưa; bạn dùng thử
qua link có gặp gì không. Rồi chọn việc trong danh sách việc mở.

## Pipeline hiện tại (v0)

```
video → ASR (Qwen3-ASR-1.7B + ForcedAligner-0.6B, GPU bf16, nạp lần lượt)      env dub_asr
      → tách câu ở 。！？；  (mốc thời gian theo ký tự)                          env dub_asr
      → dịch bằng LLM (lô 15 câu + 6 cặp ngữ cảnh, trả về JSON array)          env dub_tts
      → TTS VieNeu v3 Turbo PyTorch GPU, đọc theo lô 16 (HF_HUB_OFFLINE=1)    env dub_tts_gpu
      → xếp lên timeline (PAUSE 0.15s, MAX_SPEED 1.3) → trộn numpy → mux ffmpeg  env dub_tts
```
Mỗi stage một process, xong thì nhả VRAM. `tools/dub.sh` điều phối bằng `conda run`.
Quay TTS về CPU: `TTS_DEVICE=cpu` ở đầu `tools/dub.sh`.

## Môi trường (conda)

| Env | Nội dung |
|---|---|
| `dub_asr` | python 3.12, torch 2.8.0+cu126, qwen-asr 0.0.6, faster-whisper 1.2.1, transformers 4.57.6, ffmpeg 9.0.2 (conda-forge), pytest |
| `dub_tts` | vieneu 3.8.3 (đường ONNX/CPU), onnxruntime 1.31.1, openai, flask, pytest, ffmpeg. Chạy web, dịch, ghép. `envs/dub_tts.txt` thiếu Flask (file cũ, cần freeze lại) |
| `dub_tts_gpu` | **Nhân bản `dub_asr`** + `vieneu==3.8.3 safetensors` (2026-10-30). Chỉ chạy stage tts |
| `dub_deploy` | python 3.12 + `modal==1.6.3`. Chỉ để deploy Modal |

## Lệnh hay dùng

```bash
cd ~/PycharmProjects/dubber_vid
# Lồng tiếng một video (cache ASR ở data/asr/, kết quả ở data/projects/<tên>__<config>/)
bash tools/dub.sh data/samples/<video>.mp4 zh configs/translator_gemini.json ["Ngọc Huyền"]
# Web chỉ trên máy: http://127.0.0.1:7860 (giới thiệu), /app (làm việc), /?lang=en
bash tools/web.sh
# Chia sẻ cho bạn: một lệnh, in link https://….trycloudflare.com, Ctrl+C tắt cả web và link
bash tools/share.sh
# Chọn video ví dụ cho trang giới thiệu (sinh before/after.mp4, segments.json, meta.json trong data/demo/)
conda activate dub_tts
python tools/set_demo.py --video data/samples/<video>.mp4 --project data/projects/<tên>__<config>
# Đo TTS CPU/GPU
python tools/bench_tts.py cpu|gpu|report --project data/projects/<tên>__<config> --n 24
# Test
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests
# Áp gói (gói tiếp theo là 026)
bash tools/apply_pkg.sh dubber_pkg_NNN
# Modal (khi có thẻ): conda activate dub_deploy; modal run deploy/modal_app.py; modal deploy deploy/modal_app.py
```

## Cấu trúc code

| File | Vai trò |
|---|---|
| `dubber/asr.py` | Cắt audio ở chỗ yên nhất, mỗi đoạn tối đa `--max-chunk-sec` (bắt buộc, 5–180). Cache theo size/mtime/ngôn ngữ/max_chunk |
| `dubber/translate.py` | Đọc config, `.env`. Retry tự viết, `classify()` phân loại lỗi. SDK `max_retries=0`. Lưu sau mỗi lô |
| `dubber/v0.py` | Stage `segment`/`translate`/`tts`/`mix`. `tts --device cpu|cuda --batch N` (mặc định cpu, như cũ). Hợp đồng dữ liệu `segments.json` schema `segments/v0` |
| `web/app.py` | Flask một file, hàng đợi một job. `create_app(..., access_code, max_video_sec, only_translator, probe)`: mặc định None = như cũ. `--share` đọc `DUBBER_ACCESS_CODE` (tuỳ chọn) |
| `web/landing.html` | Trang giới thiệu tĩnh. Chữ nằm trong một bảng JSON `#i18n` (vi/en, test kiểm đủ khoá). Cấu hình ở `SITE` đầu script: `start`, `login`, `demo`, `author`, `beta` |
| `web/index.html` | Trang làm việc (sáng). Báo sớm video quá dài, lỗi 413/401 dễ hiểu |
| `tools/share.sh` | Một lệnh: kiểm đồ cần có → bật `web.sh share` (setsid) → mở Quick Tunnel → in link. Log ở `data/share/` |
| `tools/set_demo.py` | `build_demo()` hàm thuần: chỉ trường trang cần + meta (giọng, thời lượng, `max_delay`, `overlaps` tính từ mốc phát) |
| `tools/bench_tts.py` | Đo TTS CPU (ONNX) và GPU (PyTorch, các cỡ lô), lưu wav để nghe so sánh |
| `deploy/modal_app.py` | Modal: `asr_stage` (image ASR, GPU L4, stream log) + `web` (image TTS/web, CPU 8 nhân, Volume `dubber-data`, Secret `dubber`). `modal run` = kiểm 2 image |
| `deploy/run_job.py` | Thay `dub.sh` trên Modal, in log cùng định dạng (test khoá). ASR từ xa nhận FLAC 16 kHz |

Bộ màu trang làm việc (B, sáng): Lexend, nền #F5F6F8, chữ #1B2230, jade #0F7B5F.
Bộ màu trang giới thiệu (tối): nền #07090D, chữ #EEF2F7, tím (gốc) #8C93FF, jade sáng (Việt) #3DDC97,
jade #0F7B5F là màu nối hai trang. Font phụ JetBrains Mono.

## Số đo đã có

- **ASR (2026-10-29):** Qwen3 đúng cả 7/7 chỗ khác nhau so với Whisper (chủ yếu từ đồng âm). RTF ~0.08.
- **Video 10 phút, TTS CPU (2026-10-29):** 308s cho video 608s; TTS ~205s. Trễ lớn nhất 0.84s.
- **TTS CPU vs GPU (2026-10-30, `bench_tts.py`, 24 câu, 68s giọng):**

  | | Đọc | CPU | VRAM đỉnh |
  |---|---|---|---|
  | CPU ONNX int8 | 51.5s | ~14 nhân | - |
  | GPU lô 1 / 4 / 8 / 16 | 8.2 / 3.2 / 2.4 / 1.8s | 1 nhân | 505 / 589 / 763 / 856 MiB |
- **Video 10 phút, TTS GPU lô 16 (2026-10-30):** 125 câu, 310s giọng: nạp 5.4s + đọc 9.3s (RTF 0.03).
  Cả lệnh 36s khi dùng lại ASR + bản dịch. **Pipeline trọn vẹn trên video mới chưa đo** (ước ~100s).
- **Câu Việt ngắn hơn câu Trung:** tỉ lệ thời lượng 0.6–0.77.
- **Đoạn ASR 147s làm aligner dồn sai mốc**, 30s thì đúng → `ASR_MAX_CHUNK=30`. 60s chưa đo.
- **Chất lượng dịch Flash-Lite:** thơ dịch sai, có từ lạ; **tên riêng không nhất quán**
  (2026-10-30, `vidtrung10phut`: cùng nhân vật lúc "Jaian" lúc "Chaien"; có tên lạ "Toshio", "Kishio").
- **Modal (tra 2026-10-30):** Starter $30 credit/tháng; L4 ~$0.000222/s; **GPU cần gắn phương thức thanh toán**.
- **Cloudflare (tra 2026-10-30):** Quick Tunnel đổi link mỗi lần chạy, 200 request đồng thời, không SSE;
  upload gói Free ~100 MB; Pages mỗi file ≤ 25 MiB.

## Việc mở (theo thứ tự đề xuất)

1. [ ] **Tên riêng nhất quán khi dịch.** Trang giới thiệu đang hứa điều này mà kết quả thật chưa đạt.
   Hướng: LLM đọc trước cả video lập bảng tên riêng rồi dịch theo bảng; hoặc dùng Flash. Đo trước.
2. [ ] Test tiếng Hàn trên video thật.
3. [ ] Đa ngôn ngữ nguồn: gom phần phụ thuộc ngôn ngữ vào `configs/languages.json` (tên Qwen, dấu kết
   câu, ngưỡng chữ/giây, trạng thái beta); tách câu thêm `.` (cẩn thận "3.5", "Mr."). Thứ tự: Anh →
   Nhật… (11 ngôn ngữ có aligner). Ngôn ngữ ngoài aligner (Thái, Indonesia…) cần mốc từ faster-whisper.
4. [ ] Đo pipeline trọn vẹn trên một video mới sau khi TTS chạy GPU.
5. [ ] Báo cáo cho dev: số giây tràn, trễ lớn nhất. Bỏ qua câu cảm thán đứng riêng (啊！哦。).
6. [ ] Hướng B: sửa từng câu (sửa bản dịch, render lại câu đó).
7. [ ] Nghe thử mẫu giọng trước khi chọn (chỗ đã chừa ở thẻ giọng; file mẫu sinh sẵn, vẫn là file tĩnh).
8. [ ] Hiệu ứng lớn trang giới thiệu: khối Quy trình dạng cuộn (scrollytelling), chữ tan thành hạt ở hero.
9. [ ] Script dựng env (`envs/` + setup). Freeze `dub_tts_gpu` và freeze lại `dub_tts` (thiếu Flask).
10. [ ] Modal khi có thẻ: image web vẫn TTS CPU (`run_job` gọi `tts` mặc định cpu) — đổi sang GPU nếu deploy.
    Container giữ model (cold start) chỉ làm khi đo thấy cần.
11. [ ] Phân quyền admin/khách; thu log lỗi cho admin.
12. [ ] Trước khi public: video ví dụ có quyền dùng (hiện chỉ 4 câu, nên chọn đoạn 1–2 phút); license
    weight VieNeu; ghim phiên bản code MOSS-Audio-Tokenizer (trust_remote_code).

---

# PHẦN 2 — TRA KHI CẦN

## Sự cố đã gặp và cách xử lý

- **PATH bị hỏng:** `.bashrc` dòng 140 có biến rỗng. Đã comment (backup `~/.bashrc.bak_20261029`).
- **VieNeu ORT báo "External data path escapes model directory":** cache HF hub 2.x dạng symlink.
  `tools/hf_materialize.py` thay symlink bằng file thật.
- **Gemini 503 rồi 429 hết lượt ngày:** SDK âm thầm retry. Sửa bằng retry tự viết, `max_retries=0`.
- **awk dấu phẩy thập phân theo locale** → luôn `LC_ALL=C`.
- **Web:** jsonify sắp key → trả list có thứ tự; race khi hủy job → một khối lock; `send_file` với root
  tương đối → `resolve()`; mobile `scrollIntoView` kéo cả trang → chỉ cuộn trong khung.
- **Gói 009 không bao giờ được áp** vì `ls … && apply` im lặng bỏ qua.
- **Modal báo "Please add a payment method to use L4 GPU functions"** (2026-10-30); gắn thẻ Visa bị từ chối.
  Thử: bật thanh toán quốc tế/online trong app ngân hàng, thẻ khác, thẻ ảo. Chưa làm được → quay về Tunnel.
- **Modal "Secret 'dubber' not found":** tạo secret `dubber` (key `GEMINI_API_KEY`) trên trang Modal.
- **VieNeu đường GPU tải code Python của MOSS-Audio-Tokenizer** ("A new version … was downloaded"):
  dub.sh chạy stage tts với `HF_HUB_OFFLINE=1` để không tự chạy bản mới chưa ai xem.
- **VieNeu 3.8.3 đường ONNX gắn cứng `CPUExecutionProvider`:** cài `onnxruntime-gpu` không giúp gì;
  muốn GPU phải dùng đường PyTorch (`vieneu[cuda]` cần torch==2.8.0).

## Dự án tham khảo: video-dubber của bạn Thuận

Flask, Modal (GPU T4), faster-whisper, DeepSeek/Gemini, VieNeu/edge-tts. Chỉ học ý tưởng, không chép code.
Đã lấy: dịch theo lô kèm ngữ cảnh; xếp câu không chồng giọng; retry ở mọi chỗ gọi ra ngoài.
Làm khác: ASR Qwen3; cache theo stage; WAV trung gian, trộn numpy; chạy local.

## Quyết định đã bỏ

- Demucs + diarization ở bản đầu: nội dung chỉ có một người kể.
- OCR phụ đề cứng; LLM dịch chạy local; chạy trên server V100 công ty.
- Mockup A (tối, phòng dựng) và C cho trang làm việc: chọn B. (Trang giới thiệu thì tối, trộn A+B.)
- Gói 019 (chia sẻ, mã mời bắt buộc): không áp; phần cần thiết gộp vào 021 với mã mời tuỳ chọn.
- Hugging Face ZeroGPU: chỉ chạy Gradio (phải bỏ giao diện), tài khoản > 30 ngày, khách 5 phút GPU/ngày.
- Modal: tạm dừng vì cần thẻ thanh toán (code giữ trong `deploy/`).
- Bảng giá có số: chưa đo chi phí mỗi phút video thì không ghi số (chọn A: Beta miễn phí + "Sắp có").
- Ảnh mặt người / emoji cho thẻ giọng: dùng voiceprint sinh từ tên giọng.