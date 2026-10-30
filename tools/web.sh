#!/usr/bin/env bash
# Mở web lồng tiếng trên máy này. Dừng: Ctrl+C.
#
# Dùng:  bash tools/web.sh          chỉ máy này, như trước
#        bash tools/web.sh share    chế độ chia sẻ, dùng cùng tools/share.sh (Cloudflare Tunnel)
# Rồi mở trình duyệt: http://127.0.0.1:7860
#
# Luôn chỉ nghe ở 127.0.0.1, kể cả khi chia sẻ: người ngoài vào qua đường hầm Cloudflare, không qua mạng LAN.
# Chế độ chia sẻ (Thuận chốt 2026-10-30, cho vài người bạn dùng thử): mã mời tuỳ chọn (DUBBER_ACCESS_CODE
# trong .env), video tối đa SHARE_MAX_VIDEO_MIN phút, chỉ dùng config dịch SHARE_TRANSLATOR.

set -euo pipefail

HOST=127.0.0.1
PORT=7860
MAX_UPLOAD_MB=4096
# Khớp SITE.beta.maxMinutes trong web/landing.html (bảng giá ghi con số này).
SHARE_MAX_VIDEO_MIN=10
# Flash-Lite: ~500 lượt/ngày. Flash chỉ ~20 lượt/ngày, bạn chọn vài lần là hết cả ngày.
SHARE_TRANSLATOR=configs/translator_gemini.json
# Cloudflare gói Free chặn request lớn hơn 100 MB ngay ở Cloudflare.
SHARE_MAX_UPLOAD_MB=100

MODE="${1:-local}"
case "$MODE" in
	local) ARGS=(--max-upload-mb "$MAX_UPLOAD_MB") ;;
	share) ARGS=(--max-upload-mb "$SHARE_MAX_UPLOAD_MB" --share --max-video-min "$SHARE_MAX_VIDEO_MIN" --only-translator "$SHARE_TRANSLATOR") ;;
	*) echo "LỖI: chế độ lạ '$MODE'. Dùng: bash tools/web.sh  hoặc  bash tools/web.sh share"; exit 1 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
CONDA="${CONDA_EXE:-$HOME/miniconda3/bin/conda}"
export PYTHONDONTWRITEBYTECODE=1
exec "$CONDA" run --no-capture-output -n dub_tts python -m web.app --host "$HOST" --port "$PORT" "${ARGS[@]}"
