#!/usr/bin/env bash
# Chia sẻ Dubber cho vài người bạn bằng MỘT lệnh: bật web (chế độ chia sẻ) + mở link https công khai
# qua Cloudflare Quick Tunnel. Ctrl+C là tắt cả hai.
#
# Dùng:  bash tools/share.sh
# In ra link dạng https://xxx.trycloudflare.com. Gửi link cho bạn (kèm mã mời nếu có đặt trong .env).
# Giữ terminal này mở trong lúc bạn dùng; máy phải bật và không ngủ.
#
# Vì sao Quick Tunnel (2026-10-30): miễn phí, không cần tài khoản hay tên miền, không mở cổng router;
# máy này tự nối ra Cloudflare. Giới hạn (tài liệu Cloudflare): link đổi mỗi lần chạy, chỉ để thử
# nghiệm, tối đa 200 request cùng lúc. Upload gói Free bị chặn ở ~100 MB.
# cloudflared là một file chạy đặt trong ~/.local/bin: không cài vào hệ thống, không cần sudo.
# Web vẫn chỉ nghe ở 127.0.0.1 (tools/web.sh): người ngoài chỉ vào được qua đường hầm.

set -euo pipefail

HOST=127.0.0.1
PORT=7860
CF="${CLOUDFLARED:-$HOME/.local/bin/cloudflared}"
CONDA="${CONDA_EXE:-$HOME/miniconda3/bin/conda}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGDIR="$ROOT/data/share"
WEB_LOG="$LOGDIR/web.log"
CF_LOG="$LOGDIR/cloudflared.log"
WEB_WAIT_SEC=90
CF_WAIT_SEC=60

port_open() { (exec 3<>"/dev/tcp/$HOST/$PORT") 2>/dev/null; }

# ── Kiểm trước: thiếu gì báo hết một lần, chưa bật gì ─────────────────────
errors=()
if [[ ! -x "$CF" ]]; then
	errors+=("chưa có cloudflared ở $CF. Tải (chỉ ghi vào ~/.local/bin):
    mkdir -p ~/.local/bin && curl -fL -o ~/.local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x ~/.local/bin/cloudflared")
fi
if ! "$CONDA" env list 2>/dev/null | grep -qE "^dub_tts_gpu[[:space:]]"; then
	errors+=("chưa có env dub_tts_gpu (TTS chạy GPU, tools/dub.sh cần). Tạo:
    conda create -n dub_tts_gpu --clone dub_asr -y && conda run -n dub_tts_gpu pip install vieneu==3.8.3 safetensors")
fi
if [[ ! -f "$ROOT/.env" ]]; then
	errors+=("thiếu file .env (chứa GEMINI_API_KEY). Chép .env.example thành .env rồi điền key.")
fi
if port_open; then
	errors+=("đã có chương trình chạy ở $HOST:$PORT (có thể là bash tools/web.sh đang mở ở terminal khác). Tắt nó trước.")
fi
if (( ${#errors[@]} )); then
	for e in "${errors[@]}"; do echo "LỖI: $e"; done
	exit 1
fi

mkdir -p "$LOGDIR"
WEB_PID="" CF_PID=""
cleanup() {
	trap - EXIT INT TERM
	[[ -n "$CF_PID" ]] && kill "$CF_PID" 2>/dev/null || true
	# web chạy trong nhóm tiến trình riêng (setsid): web.sh -> conda run -> python; tắt cả nhóm, không sót.
	[[ -n "$WEB_PID" ]] && kill -TERM -- "-$WEB_PID" 2>/dev/null || true
	echo
	echo "Đã tắt web và đóng link."
}
trap cleanup EXIT INT TERM

# ── 1. Web ở chế độ chia sẻ ───────────────────────────────────────────────
echo "Đang bật web... (log: $WEB_LOG)"
setsid bash "$ROOT/tools/web.sh" share >"$WEB_LOG" 2>&1 &
WEB_PID=$!
for _ in $(seq 1 "$WEB_WAIT_SEC"); do
	port_open && break
	if ! kill -0 "$WEB_PID" 2>/dev/null; then
		echo "LỖI: web không bật được. 20 dòng cuối của $WEB_LOG:"
		tail -20 "$WEB_LOG"
		exit 1
	fi
	sleep 1
done
if ! port_open; then
	echo "LỖI: sau $WEB_WAIT_SEC giây web vẫn chưa sẵn sàng. Xem $WEB_LOG"
	exit 1
fi
grep -m1 "^\[web\]" "$WEB_LOG" || true

# ── 2. Đường hầm Cloudflare ───────────────────────────────────────────────
echo "Đang mở link... (log: $CF_LOG)"
"$CF" tunnel --no-autoupdate --url "http://$HOST:$PORT" >"$CF_LOG" 2>&1 &
CF_PID=$!
URL=""
for _ in $(seq 1 "$CF_WAIT_SEC"); do
	URL="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$CF_LOG" | head -1 || true)"
	[[ -n "$URL" ]] && break
	if ! kill -0 "$CF_PID" 2>/dev/null; then
		echo "LỖI: cloudflared đã dừng. 20 dòng cuối của $CF_LOG:"
		tail -20 "$CF_LOG"
		exit 1
	fi
	sleep 1
done
if [[ -z "$URL" ]]; then
	echo "LỖI: sau $CF_WAIT_SEC giây vẫn chưa có link. Xem $CF_LOG"
	exit 1
fi

echo
echo "  Link gửi bạn:     $URL"
echo "  Trang lồng tiếng: $URL/app"
echo
echo "Giữ terminal này mở. Ctrl+C là tắt web và đóng link. Chạy lại thì link sẽ khác."
# Chờ tới khi một trong hai dừng (hoặc Ctrl+C); cái nào tự chết thì báo, rồi tắt cái còn lại.
while kill -0 "$WEB_PID" 2>/dev/null && kill -0 "$CF_PID" 2>/dev/null; do sleep 2; done
kill -0 "$WEB_PID" 2>/dev/null || { echo "LỖI: web đã dừng bất ngờ. 20 dòng cuối của $WEB_LOG:"; tail -20 "$WEB_LOG"; }
kill -0 "$CF_PID" 2>/dev/null || { echo "LỖI: đường hầm đã đóng bất ngờ. 20 dòng cuối của $CF_LOG:"; tail -20 "$CF_LOG"; }
exit 1