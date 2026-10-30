#!/usr/bin/env bash
# Lồng tiếng một video từ đầu đến cuối bằng một lệnh.
#
# Dùng:  bash tools/dub.sh <video> <ngôn ngữ gốc: zh|ko> <config dịch> [giọng]
# Vd:    bash tools/dub.sh data/samples/vidtiengtrung1.mp4 zh configs/translator_gemini.json
#        bash tools/dub.sh data/samples/vidtiengtrung1.mp4 zh configs/translator_gemini_flash.json "Ngọc Huyền"
# Giọng bỏ trống thì dùng VOICE_DEFAULT bên dưới (in ra ở dòng đầu để biết đang dùng giọng nào).
#
# Kết quả:  data/projects/<tên video>__<tên config>/  (<tên>_vi.mp4, report.md, segments.json)
# Cache ASR: data/asr/<tên video>.json  — đổi config dịch hay giọng thì không phải nhận dạng lại.
# Chạy lại sau khi lỗi: phần đã xong (ASR, câu đã dịch) được giữ, chỉ làm tiếp phần còn thiếu.

set -euo pipefail

VOICE_DEFAULT="Thiện Minh"
# Độ dài tối đa mỗi đoạn đưa vào ASR/aligner. Đo 2026-10-29 trên test_lap7: đoạn 147s thì aligner
# gán sai mốc (dồn 2 câu ~10s vào 1.5s), đoạn 30s thì đúng; 60s chưa đo. Đổi số này phải có số đo.
ASR_MAX_CHUNK=30
# TTS chạy GPU (env dub_tts_gpu: torch của dub_asr + vieneu), đọc theo lô. Đo 2026-10-30 bằng tools/bench_tts.py,
# 24 câu (68s giọng): CPU ONNX int8 51.5s, chiếm ~14 nhân; GPU lô 16 1.8s, 1 nhân, VRAM đỉnh 856 MiB.
# Quay lại CPU: TTS_DEVICE=cpu (chạy trong dub_tts như trước).
TTS_DEVICE=cuda
TTS_BATCH=16
VIDEO="${1:-}"
LANG_SRC="${2:-}"
CONFIG="${3:-}"
VOICE="${4:-$VOICE_DEFAULT}"
if [[ -z "$VIDEO" || -z "$LANG_SRC" || -z "$CONFIG" ]]; then
	echo "LỖI: thiếu tham số. Dùng: bash tools/dub.sh <video> <zh|ko> <config dịch> [giọng]"
	exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
for f in "$VIDEO" "$CONFIG" ".env"; do
	if [[ ! -f "$f" ]]; then
		echo "LỖI: thiếu $f"
		exit 1
	fi
done

NAME="$(basename "${VIDEO%.*}")"
ASR_JSON="data/asr/$NAME.json"
PROJECT="data/projects/${NAME}__$(basename "${CONFIG%.*}")"
CONDA="${CONDA_EXE:-$HOME/miniconda3/bin/conda}"
export PYTHONDONTWRITEBYTECODE=1
run() { "$CONDA" run --no-capture-output -n "$1" python -m "${@:2}"; }

echo "[dub] video=$VIDEO lang=$LANG_SRC dịch=$CONFIG giọng=$VOICE đoạn ASR=${ASR_MAX_CHUNK}s -> $PROJECT"
T0=$SECONDS
run dub_asr dubber.asr --video "$VIDEO" --lang "$LANG_SRC" --out "$ASR_JSON" --max-chunk-sec "$ASR_MAX_CHUNK"
run dub_asr dubber.v0 segment --video "$VIDEO" --asr-json "$ASR_JSON" --project "$PROJECT"
run dub_tts dubber.v0 translate --project "$PROJECT" --config "$CONFIG" --env .env
if [[ "$TTS_DEVICE" == cuda ]]; then
	# HF_HUB_OFFLINE=1: đường GPU nạp code Python của MOSS-Audio-Tokenizer từ Hugging Face (trust_remote_code);
	# offline thì chỉ dùng bản đã tải (lần chạy tools/bench_tts.py gpu), không tự chạy bản mới chưa ai xem.
	HF_HUB_OFFLINE=1 "$CONDA" run --no-capture-output -n dub_tts_gpu python -m dubber.v0 tts --project "$PROJECT" \
		--voice "$VOICE" --precision int8 --device cuda --batch "$TTS_BATCH"
else
	run dub_tts dubber.v0 tts --project "$PROJECT" --voice "$VOICE" --precision int8
fi
run dub_tts dubber.v0 mix --video "$VIDEO" --project "$PROJECT" --orig-volume 0.12
echo "[dub] XONG sau $((SECONDS - T0))s: $PROJECT/${NAME}_vi.mp4"