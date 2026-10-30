"""Đo ASR trên một video thật: chạy từng model, ghi kết quả JSON và một báo cáo so sánh.

Bước này trả lời câu hỏi: trên video của Thuận, model nào nghe tiếng Trung/Hàn tốt hơn,
tốn bao nhiêu VRAM, nhanh cỡ nào, và timestamp ra sao.

Không có đáp án chuẩn (không đọc được tiếng Trung), nên báo cáo dùng cách B: so hai model với
nhau, chỉ ra đoạn hai bên nghe khác nhau. Đoạn lệch là đoạn đáng nghi, chưa phải kết luận ai sai.

Dùng:  conda activate dub_asr
       python tools/bench_asr.py data/samples/vidtiengtrung1.mp4 --lang zh
Kết quả: data/bench/asr/<tên video>/ gồm audio_16k.wav, qwen3.json, whisper.json, report.md
Chỉ ghi vào data/bench/, không đụng video gốc.

Mỗi model chạy trong một tiến trình con riêng: thoát ra là VRAM nhả sạch, và số VRAM đo được
của model sau không bị model trước làm bẩn.
"""
import argparse
import difflib
import json
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT / "data" / "bench" / "asr"
# ffmpeg lấy đúng bản trong env đang chạy, không tra PATH: 2026-10-29 PATH trên máy Thuận từng
# đặt /bin lên trước env, gõ `ffmpeg` ra bản 4.4.2 của hệ thống thay vì 9.0.2 của env.
FFMPEG = Path(sys.executable).parent / "ffmpeg"

QWEN_ASR = "Qwen/Qwen3-ASR-1.7B"
QWEN_ALIGNER = "Qwen/Qwen3-ForcedAligner-0.6B"
# Giới hạn của Qwen3-ForcedAligner trong qwen-asr 0.0.6 (MAX_FORCE_ALIGN_INPUT_SECONDS).
# Bench gọi aligner trực tiếp nên không có bước tự cắt đoạn của thư viện.
QWEN_ALIGN_MAX_SEC = 180
WHISPER = "large-v3"
# Câu mồi quen dùng cho Whisper tiếng Trung: không có nó Whisper hay trả chữ phồn thể và thiếu
# dấu câu, khi so với Qwen (giản thể) sẽ lệch hàng loạt mà không phải do nghe sai.
WHISPER_ZH_PROMPT = "以下是普通话的句子。"

LANGS = {
	"zh": {"qwen3": "Chinese", "whisper": "zh"},
	"ko": {"qwen3": "Korean", "whisper": "ko"},
}
MODELS = ("qwen3", "whisper")


# ── Phần quyết định: hàm thuần, test được không cần GPU ─────────────────────

def normalize(text):
	"""Chỉ giữ chữ và số (giống cách aligner của Qwen làm sạch), bỏ dấu câu và khoảng trắng.
	So hai model trên chuỗi này để khác biệt về dấu câu không bị tính là nghe khác."""
	return "".join(ch for ch in text if unicodedata.category(ch)[0] in "LN")


def diff_spans(a, b):
	"""Các đoạn khác nhau giữa hai chuỗi đã normalize: list (vị trí trong a, đoạn a, đoạn b)."""
	sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
	return [(i1, a[i1:i2], b[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def similarity(a, b):
	if not a and not b:
		return 1.0
	return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def time_at(units, index):
	"""Thời điểm (giây) của ký tự thứ index trong chuỗi normalize, dựa trên timestamp từng
	đơn vị của aligner. Trả None khi không suy ra được chắc chắn: thà không ghi giờ còn hơn ghi sai."""
	pos = 0
	for u in units:
		n = len(normalize(u["text"]))
		if index < pos + n:
			return u["start"]
		pos += n
	return None


def build_report(video, results):
	"""results: {tên model: dict kết quả}. Trả về nội dung report.md."""
	lines = [f"# Bench ASR: {video}", ""]
	lines += ["| Model | RTF | Nạp model (s) | Nhận dạng (s) | VRAM đỉnh (MiB) | Số ký tự |", "|---|---|---|---|---|---|"]
	for name, r in results.items():
		lines.append(
			f"| {r['model']} | {r['rtf']:.3f} | {r['load_sec']:.1f} | {r['infer_sec']:.1f} "
			f"| {r['vram_peak_mib'] - r['vram_baseline_mib']} (nền {r['vram_baseline_mib']}) | {len(normalize(r['text']))} |"
		)
	lines += ["", "RTF = thời gian nhận dạng / thời lượng audio (nhỏ hơn 1 là nhanh hơn thời gian thật).",
		"VRAM đo cả GPU bằng nvidia-smi, trừ mức nền lúc model chưa nạp.", ""]

	for name, r in results.items():
		lines += [f"## Text: {r['model']}", "", r["text"], ""]

	if "qwen3" in results and "whisper" in results:
		qa = normalize(results["qwen3"]["text"])
		wb = normalize(results["whisper"]["text"])
		spans = diff_spans(qa, wb)
		lines += ["## Hai model nghe khác nhau ở đâu", "",
			f"Độ giống nhau (sau khi bỏ dấu câu): **{similarity(qa, wb):.1%}**, {len(spans)} đoạn lệch.", ""]
		if spans:
			lines += ["| Thời điểm (Qwen) | Qwen3 nghe | Whisper nghe |", "|---|---|---|"]
			for pos, sa, sb in spans:
				t = time_at(results["qwen3"].get("units", []), pos)
				lines.append(f"| {'%.2fs' % t if t is not None else '?'} | {sa or '(không có)'} | {sb or '(không có)'} |")
	return "\n".join(lines)


# ── Phần vào ra: ffmpeg, nvidia-smi, model ──────────────────────────────────

class VramMonitor:
	"""Lấy mẫu VRAM của cả GPU 5 lần/giây. Đo cả GPU vì ctranslate2 (Whisper) không đi qua
	bộ cấp phát của torch, nên torch.cuda.max_memory_allocated() không thấy được nó."""

	def __init__(self):
		self.baseline = self._read()
		self.peak = self.baseline
		self._stop = threading.Event()
		self._thread = threading.Thread(target=self._loop, daemon=True)

	@staticmethod
	def _read():
		out = subprocess.run(
			["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits", "-i", "0"],
			capture_output=True, text=True, check=True,
		).stdout
		return int(out.strip())

	def _loop(self):
		while not self._stop.wait(0.2):
			self.peak = max(self.peak, self._read())

	def __enter__(self):
		self._thread.start()
		return self

	def __exit__(self, *exc):
		self._stop.set()
		self._thread.join()


def extract_audio(video, wav):
	if not FFMPEG.is_file():
		sys.exit(f"LỖI: không thấy ffmpeg của env ở {FFMPEG}. Đã `conda activate dub_asr` chưa?")
	# -map 0:a:0 chọn luồng audio theo LOẠI: vidtiengtrung2.mp4 để audio là luồng số 0,
	# vidtiengtrung1.mp4 để audio là luồng số 1. Chọn theo số thứ tự sẽ lấy nhầm.
	subprocess.run(
		[str(FFMPEG), "-nostdin", "-v", "error", "-y", "-i", str(video),
		 "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
		check=True,
	)


def run_qwen3(wav, sr, lang):
	import gc
	import torch
	from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner

	duration = len(wav) / sr
	if duration > QWEN_ALIGN_MAX_SEC:
		sys.exit(f"LỖI: audio {duration:.0f}s > {QWEN_ALIGN_MAX_SEC}s, giới hạn của aligner. Bench chưa cắt đoạn.")

	t0 = time.perf_counter()
	asr = Qwen3ASRModel.from_pretrained(
		QWEN_ASR, dtype=torch.bfloat16, device_map="cuda:0", max_inference_batch_size=1, max_new_tokens=2048,
	)
	t1 = time.perf_counter()
	res = asr.transcribe(audio=(wav, sr), language=lang)[0]
	t2 = time.perf_counter()
	# Nhả ASR trước khi nạp aligner: nạp cả hai cùng lúc (cách mặc định của thư viện) thì
	# 1.7B + 0.6B ở bf16 sát mức 5.8 GB của A3000.
	del asr
	gc.collect()
	torch.cuda.empty_cache()
	t3 = time.perf_counter()
	aligner = Qwen3ForcedAligner.from_pretrained(QWEN_ALIGNER, dtype=torch.bfloat16, device_map="cuda:0")
	t4 = time.perf_counter()
	units = aligner.align(audio=(wav, sr), text=res.text, language=lang)[0]
	t5 = time.perf_counter()
	return {
		"model": f"{QWEN_ASR} + {QWEN_ALIGNER}",
		"params": {"dtype": "bfloat16", "language": lang, "max_new_tokens": 2048, "load_order": "asr, nhả, aligner"},
		"load_sec": (t1 - t0) + (t4 - t3),
		"infer_sec": (t2 - t1) + (t5 - t4),
		"text": res.text,
		"segments": [],
		"units": [{"start": u.start_time, "end": u.end_time, "text": u.text} for u in units],
	}


def run_whisper(wav, sr, lang):
	from faster_whisper import WhisperModel

	prompt = WHISPER_ZH_PROMPT if lang == "zh" else None
	t0 = time.perf_counter()
	model = WhisperModel(WHISPER, device="cuda", compute_type="float16")
	t1 = time.perf_counter()
	segments, _ = model.transcribe(wav, language=lang, beam_size=5, word_timestamps=True, initial_prompt=prompt)
	segments = list(segments)  # transcribe() trả generator: duyệt hết mới thật sự chạy
	t2 = time.perf_counter()
	return {
		"model": f"faster-whisper {WHISPER}",
		"params": {"compute_type": "float16", "language": lang, "beam_size": 5, "initial_prompt": prompt},
		"load_sec": t1 - t0,
		"infer_sec": t2 - t1,
		"text": "".join(s.text for s in segments).strip(),
		"segments": [{"start": s.start, "end": s.end, "text": s.text} for s in segments],
		"units": [{"start": w.start, "end": w.end, "text": w.word} for s in segments for w in (s.words or [])],
	}


def worker(model, wav_path, lang, out_json):
	import soundfile as sf

	wav, sr = sf.read(wav_path, dtype="float32")
	runner = {"qwen3": run_qwen3, "whisper": run_whisper}[model]
	with VramMonitor() as vram:
		result = runner(wav, sr, LANGS[lang][model])
	audio_sec = len(wav) / sr
	result.update({
		"audio_sec": round(audio_sec, 3),
		"rtf": result["infer_sec"] / audio_sec,
		"vram_baseline_mib": vram.baseline,
		"vram_peak_mib": vram.peak,
	})
	Path(out_json).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
	p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
	p.add_argument("video", type=Path)
	p.add_argument("--lang", required=True, choices=sorted(LANGS))
	p.add_argument("--models", default=",".join(MODELS), help="vd: qwen3,whisper")
	p.add_argument("--worker", nargs=3, metavar=("MODEL", "WAV", "OUT_JSON"), help=argparse.SUPPRESS)
	args = p.parse_args()

	if args.worker:
		worker(args.worker[0], args.worker[1], args.lang, args.worker[2])
		return 0

	models = [m.strip() for m in args.models.split(",") if m.strip()]
	unknown = [m for m in models if m not in MODELS]
	if unknown:
		sys.exit(f"LỖI: model không có: {unknown}. Chọn trong {list(MODELS)}")
	if not args.video.is_file():
		sys.exit(f"LỖI: không thấy video {args.video}")

	out_dir = OUT_ROOT / args.video.stem
	out_dir.mkdir(parents=True, exist_ok=True)
	wav = out_dir / "audio_16k.wav"
	print(f"[bench] ffmpeg: {FFMPEG}")
	extract_audio(args.video, wav)

	failed = []
	for m in models:
		out_json = out_dir / f"{m}.json"
		print(f"[bench] {m}: đang chạy (lần đầu sẽ tải model về ~/.cache/huggingface)...")
		code = subprocess.run([sys.executable, __file__, str(args.video), "--lang", args.lang,
			"--worker", m, str(wav), str(out_json)]).returncode
		if code != 0:
			failed.append(f"{m} (mã thoát {code})")
			out_json.unlink(missing_ok=True)  # không để JSON cũ bị nhầm là kết quả lần này

	results = {m: json.loads((out_dir / f"{m}.json").read_text(encoding="utf-8"))
		for m in models if (out_dir / f"{m}.json").is_file()}
	if results:
		(out_dir / "report.md").write_text(build_report(args.video.name, results), encoding="utf-8")
		print(f"[bench] báo cáo: {out_dir / 'report.md'}")
	if failed:
		print(f"[bench] HỎNG: {', '.join(failed)}. Xem lỗi in phía trên.")
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main())