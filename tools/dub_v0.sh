#!/usr/bin/env bash
# Chạy bộ khung lồng tiếng v0 cho một video, qua các stage và 2 env.
#
# Dùng:  bash tools/dub_v0.sh data/samples/vidtiengtrung1.mp4                                (dịch tay)
#        bash tools/dub_v0.sh data/samples/vidtiengtrung1.mp4 configs/translator_gemini.json (dịch qua API)
# Cần có sẵn: kết quả bench ASR  data/bench/asr/<tên>/qwen3.json
#             dịch tay: demo/manual_vi/<tên>.json  |  dịch API: file .env có key (xem .env.example)
# Kết quả:    dịch tay -> data/projects/<tên>/ ; dịch API -> data/projects/<tên>__<tên config>/
#             Thư mục riêng để so hai cách dịch trên cùng video.
#
# Vì sao gọi qua `conda run`: mỗi stage chạy trong env riêng của nó (dub_asr có torch/GPU,
# dub_tts không có torch). Một lệnh chạy hết, không phải tự activate từng env.

set -euo pipefail

VIDEO="${1:-}"
CONFIG="${2:-}"
if [[ -z "$VIDEO" || ! -f "$VIDEO" ]]; then
	echo "LỖI: thiếu video hoặc không thấy file. Ví dụ: bash tools/dub_v0.sh data/samples/vidtiengtrung1.mp4"
	exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
NAME="$(basename "${VIDEO%.*}")"
ASR_JSON="data/bench/asr/$NAME/qwen3.json"
CONDA="${CONDA_EXE:-$HOME/miniconda3/bin/conda}"

need=("$ASR_JSON")
if [[ -z "$CONFIG" ]]; then
	TRANS="demo/manual_vi/$NAME.json"
	PROJECT="data/projects/$NAME"
	need+=("$TRANS")
else
	PROJECT="data/projects/${NAME}__$(basename "${CONFIG%.*}")"
	need+=("$CONFIG" ".env")
fi
for f in "${need[@]}"; do
	if [[ ! -f "$f" ]]; then
		echo "LỖI: thiếu $f"
		exit 1
	fi
done

export PYTHONDONTWRITEBYTECODE=1
run() { "$CONDA" run --no-capture-output -n "$1" python -m dubber.v0 "${@:2}"; }

if [[ -z "$CONFIG" ]]; then
	run dub_asr segment --video "$VIDEO" --asr-json "$ASR_JSON" --translations "$TRANS" --project "$PROJECT"
else
	run dub_asr segment --video "$VIDEO" --asr-json "$ASR_JSON" --project "$PROJECT"
	run dub_tts translate --project "$PROJECT" --config "$CONFIG" --env .env
fi
run dub_tts tts --project "$PROJECT" --voice "Thiện Minh" --precision int8
run dub_tts mix --video "$VIDEO" --project "$PROJECT" --orig-volume 0.12