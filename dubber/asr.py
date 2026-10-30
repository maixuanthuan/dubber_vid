"""Stage ASR: video -> chữ gốc + mốc thời gian từng ký tự, cho video dài bất kỳ (env dub_asr).

Audio được cắt thành đoạn ngắn hơn --max-chunk-sec, cắt tại chỗ NHỎ TIẾNG NHẤT gần ranh giới để
không cắt ngang câu nói. Hai giới hạn: aligner nhận tối đa 180s (qwen-asr 0.0.6), nhưng đo thật
2026-10-29 (test_lap7, clip 29.4s lặp 7 lần): đoạn 147s thì aligner dồn 2 câu ~10s vào 1.5s,
đoạn 30s thì đúng. Độ dài đoạn vì vậy là tham số, chọn bằng số đo (tools/check_asr_times.py).
Nạp ASR một lần nhận dạng mọi đoạn, nhả VRAM, rồi nạp aligner gán thời gian mọi đoạn: 1.7B + 0.6B
nạp cùng lúc sát mức 5.8 GB của A3000 (bench 2026-10-29: riêng ASR+aligner tuần tự đỉnh ~4.55 GB).

Đầu ra cùng định dạng với kết quả bench (qwen3.json) để stage segment dùng y nguyên, thêm
danh sách chunks để soi chỗ cắt. Có cache theo kích thước + thời điểm sửa của video.

Dùng: python -m dubber.asr --video data/samples/x.mp4 --lang zh --out data/asr/x.json
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

QWEN_ASR = "Qwen/Qwen3-ASR-1.7B"
QWEN_ALIGNER = "Qwen/Qwen3-ForcedAligner-0.6B"
LANGS = {"zh": "Chinese", "ko": "Korean"}
SR = 16000
# Giới hạn cứng của aligner (qwen-asr 0.0.6, MAX_FORCE_ALIGN_INPUT_SECONDS).
ALIGNER_MAX_SEC = 180.0
# Tìm điểm cắt trong tối đa SEARCH_SEC giây cuối của mỗi đoạn (và không quá 1/3 đoạn, để đoạn
# không bị cắt quá ngắn); khung đo âm lượng FRAME_SEC.
SEARCH_SEC = 30.0
FRAME_SEC = 0.05
# Đủ cho ~150s lời nói tiếng Trung (~1 token/chữ); bench 30s dùng 2048 thừa nhiều.
MAX_NEW_TOKENS = 2048
FFMPEG = Path(sys.executable).parent / "ffmpeg"


# ── Phần quyết định: hàm thuần ──────────────────────────────────────────────

def frame_rms(wav, frame=int(SR * FRAME_SEC)):
	import numpy as np

	n = len(wav) // frame
	if n == 0:
		return np.zeros(0)
	return np.sqrt((wav[: n * frame].reshape(n, frame) ** 2).mean(axis=1))


def choose_cuts(rms, total_sec, max_chunk, frame_sec=FRAME_SEC, search=SEARCH_SEC):
	"""Điểm cắt (giây) để mọi đoạn <= max_chunk. Mỗi lần cắt tìm khung nhỏ tiếng nhất trong
	min(search, max_chunk/3) giây cuối của đoạn đang xét. Audio ngắn hơn max_chunk thì không cắt."""
	search = min(search, max_chunk / 3)
	cuts, start = [], 0.0
	while total_sec - start > max_chunk:
		hi = start + max_chunk
		lo = max(start + 1.0, hi - search)
		i0, i1 = int(lo / frame_sec), max(int(lo / frame_sec) + 1, int(hi / frame_sec))
		window = rms[i0:i1]
		k = i0 + int(window.argmin()) if len(window) else int(hi / frame_sec)
		cut = round(k * frame_sec + frame_sec / 2, 3)
		cuts.append(cut)
		start = cut
	return cuts


def merge_chunks(chunk_results):
	"""chunk_results: [(start_sec, text, units)] theo thứ tự; units có mốc tính từ đầu đoạn.
	Nối text và cộng bù mốc thời gian về mốc của cả video."""
	text, units = "", []
	for start, t, us in chunk_results:
		text += t
		units += [{"start": round(u["start"] + start, 3), "end": round(u["end"] + start, 3), "text": u["text"]} for u in us]
	return text, units


# ── Phần vào ra ─────────────────────────────────────────────────────────────

def extract(video, wav_path):
	if not FFMPEG.is_file():
		sys.exit(f"LỖI: không thấy ffmpeg của env ở {FFMPEG}. Đã đúng env dub_asr chưa?")
	subprocess.run([str(FFMPEG), "-nostdin", "-v", "error", "-y", "-i", str(video), "-map", "0:a:0",
		"-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(wav_path)], check=True)


def video_key(video):
	st = Path(video).stat()
	return {"path": str(video), "size": st.st_size, "mtime": int(st.st_mtime)}


def run(video, lang, out, max_chunk, log=print):
	import gc

	import soundfile as sf
	import torch
	from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner

	out = Path(out)
	if out.is_file():
		old = json.loads(out.read_text(encoding="utf-8"))
		params = old.get("params", {})
		if old.get("video") == video_key(video) and params.get("language") == LANGS[lang] \
				and params.get("max_chunk_sec") == max_chunk:
			log(f"[asr] dùng lại kết quả đã có: {out}")
			return
	out.parent.mkdir(parents=True, exist_ok=True)
	wav_path = out.with_suffix(".16k.wav")
	extract(video, wav_path)
	wav, sr = sf.read(str(wav_path), dtype="float32")
	total = len(wav) / sr
	cuts = choose_cuts(frame_rms(wav), total, max_chunk)
	bounds = list(zip([0.0] + cuts, cuts + [total]))
	log(f"[asr] audio {total:.1f}s, {len(bounds)} đoạn: " + ", ".join(f"{a:.1f}–{b:.1f}" for a, b in bounds))

	t0 = time.perf_counter()
	asr = Qwen3ASRModel.from_pretrained(QWEN_ASR, dtype=torch.bfloat16, device_map="cuda:0",
		max_inference_batch_size=1, max_new_tokens=MAX_NEW_TOKENS)
	texts = []
	for i, (a, b) in enumerate(bounds):
		piece = wav[int(a * sr): int(b * sr)]
		texts.append(asr.transcribe(audio=(piece, sr), language=LANGS[lang])[0].text)
		log(f"[asr] nhận dạng đoạn {i + 1}/{len(bounds)}: {len(texts[-1])} ký tự")
	t1 = time.perf_counter()
	del asr
	gc.collect()
	torch.cuda.empty_cache()

	aligner = Qwen3ForcedAligner.from_pretrained(QWEN_ALIGNER, dtype=torch.bfloat16, device_map="cuda:0")
	results = []
	for i, ((a, b), text) in enumerate(zip(bounds, texts)):
		piece = wav[int(a * sr): int(b * sr)]
		units = []
		if text.strip():
			units = [{"start": u.start_time, "end": u.end_time, "text": u.text}
				for u in aligner.align(audio=(piece, sr), text=text, language=LANGS[lang])[0]]
		results.append((a, text, units))
		log(f"[asr] gán thời gian đoạn {i + 1}/{len(bounds)}: {len(units)} đơn vị")
	t2 = time.perf_counter()
	peak = torch.cuda.max_memory_allocated() / 2**20
	del aligner
	gc.collect()
	torch.cuda.empty_cache()

	text, units = merge_chunks(results)
	wav_path.unlink(missing_ok=True)
	out.write_text(json.dumps({
		"model": f"{QWEN_ASR} + {QWEN_ALIGNER}", "video": video_key(video),
		"params": {"language": LANGS[lang], "max_new_tokens": MAX_NEW_TOKENS, "max_chunk_sec": max_chunk},
		"audio_sec": round(total, 3), "asr_sec": round(t1 - t0, 1), "align_sec": round(t2 - t1, 1),
		"torch_peak_mib": round(peak), "chunks": [{"start": a, "end": b, "text": t} for (a, b), (_, t, _) in zip(bounds, results)],
		"text": text, "units": units,
	}, ensure_ascii=False, indent=1), encoding="utf-8")
	log(f"[asr] xong: {len(text)} ký tự, nhận dạng {t1 - t0:.0f}s + gán thời gian {t2 - t1:.0f}s, "
		f"VRAM torch đỉnh {peak:.0f} MiB -> {out}")


def main():
	p = argparse.ArgumentParser(description="Stage ASR cho video dài")
	p.add_argument("--video", type=Path, required=True)
	p.add_argument("--lang", required=True, choices=sorted(LANGS))
	p.add_argument("--out", type=Path, required=True)
	p.add_argument("--max-chunk-sec", type=float, required=True, help="độ dài tối đa mỗi đoạn đưa vào ASR/aligner")
	a = p.parse_args()
	if not a.video.is_file():
		sys.exit(f"LỖI: không thấy video {a.video}")
	if not 5 <= a.max_chunk_sec <= ALIGNER_MAX_SEC:
		sys.exit(f"LỖI: --max-chunk-sec={a.max_chunk_sec} phải trong khoảng 5–{ALIGNER_MAX_SEC:.0f}s (giới hạn aligner)")
	run(a.video, a.lang, a.out, a.max_chunk_sec)


if __name__ == "__main__":
	main()