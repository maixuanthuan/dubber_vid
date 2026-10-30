"""Bộ khung lồng tiếng v0: chạy được từ đầu đến cuối, chưa tối ưu.

Mục tiêu duy nhất: ra được video tiếng Việt từ video thật để nghe, và đo những thứ chưa biết
(TTS nhanh cỡ nào, câu Việt dài hơn câu gốc bao nhiêu). Ba stage, giao nhau qua segments.json:

  segment   (env bất kỳ) : kết quả ASR (qwen3.json của bench) -> câu + mốc thời gian
                           (+ bản dịch tay nếu truyền --translations)
  translate (env dub_tts): dịch các câu chưa có bản dịch qua LLM (dubber/translate.py)
  tts       (env dub_tts): đọc từng câu tiếng Việt bằng VieNeu (CPU, ONNX)
  mix      (env dub_tts): đặt giọng vào đúng mốc, ép tốc độ khi tràn, trộn, xuất mp4 + report.md

Giới hạn đã biết của v0 (ghi để khỏi quên, không phải lỗi):
  - Dịch qua LLM chưa có ngân sách độ dài: thử từng biến một, lần này chỉ thay dịch tay bằng API.
  - Tiếng gốc chỉ được hạ nhỏ, chưa tách giọng khỏi nhạc nền, nên vẫn nghe lẫn tiếng Trung.
  - Chỉ cắt câu ở 。！？； ; câu quá dài chưa được cắt thêm ở dấu phẩy.
  - Ngưỡng PAUSE và MAX_SPEED là giá trị tạm, chưa đo; report của v0 chính là dữ liệu để đo.

Dùng: bash tools/dub_v0.sh data/samples/vidtiengtrung1.mp4                    (dịch tay, như cũ)
      bash tools/dub_v0.sh data/samples/vidtiengtrung1.mp4 configs/translator_gemini.json
"""
import argparse
import json
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

from dubber.translate import TranslateError

SCHEMA = "segments/v0"
SENTENCE_END = set("。！？；!?;")
# Tạm, chưa đo: nghỉ tối thiểu trước câu sau, và tốc độ đọc nhanh nhất còn nghe được.
PAUSE = 0.15
MAX_SPEED = 1.3
# Người nói thật khoảng 4–6 chữ Hán/giây (2 clip mẫu 2026-10-29: ~3–4). Trên 12 chữ/giây là mốc
# thời gian sai, không phải nói nhanh: aligner trên đoạn 147s từng dồn 16 chữ vào 0.8s (20 chữ/giây).
MAX_CPS = 12.0
FFMPEG = Path(sys.executable).parent / "ffmpeg"


def kept(ch):
	"""Ký tự mà aligner của Qwen giữ lại (chữ, số). Dấu câu không có timestamp."""
	return unicodedata.category(ch)[0] in "LN"


def norm(text):
	return "".join(ch for ch in text if kept(ch))


# ── Stage segment: hàm thuần ────────────────────────────────────────────────

def split_sentences(text, units):
	"""Cắt text ASR thành câu ở dấu kết câu, gán mốc thời gian từ timestamp từng ký tự.

	units: [{start, end, text}] của aligner, theo thứ tự, không có dấu câu. Ghép lần lượt từng
	ký tự giữ lại của text với ký tự trong units; lệch nhau là dừng và báo, không đoán."""
	chars = []  # (ký tự, start, end) theo đúng thứ tự trong units
	for u in units:
		for ch in norm(u["text"]):
			chars.append((ch, u["start"], u["end"]))

	sentences, buf, times, k = [], "", [], 0
	for ch in text:
		buf += ch
		if kept(ch):
			if k >= len(chars) or chars[k][0] != ch:
				got = chars[k][0] if k < len(chars) else "(hết)"
				raise ValueError(f"text ASR và timestamp lệch nhau ở ký tự thứ {k}: text '{ch}', timestamp '{got}'")
			times.append(chars[k][1:])
			k += 1
		if ch in SENTENCE_END and times:
			sentences.append({"src": buf.strip(), "start": times[0][0], "end": times[-1][1]})
			buf, times = "", []
	if times:
		sentences.append({"src": buf.strip(), "start": times[0][0], "end": times[-1][1]})
	if k != len(chars):
		raise ValueError(f"timestamp còn {len(chars) - k} ký tự không khớp với text ASR")
	return sentences


def suspicious_timing(sentences, max_cps=MAX_CPS):
	"""Câu có tốc độ nói vô lý (chữ/giây > max_cps): [(chỉ số câu, chữ/giây)]. Chỉ phát hiện, không sửa."""
	out = []
	for i, s in enumerate(sentences):
		n, dur = len(norm(s["src"])), s["end"] - s["start"]
		cps = n / dur if dur > 0 else float("inf")
		if n >= 3 and cps > max_cps:  # câu 1–2 chữ ("嗯。") quá ngắn để kết luận
			out.append((i, round(cps, 1)))
	return out


def attach_translations(sentences, translations):
	"""translations: {câu gốc: câu dịch}. So theo text đã bỏ dấu câu. Thiếu câu nào thì báo đủ
	mọi câu thiếu, không để câu trống lọt sang TTS."""
	table = {norm(src): tgt for src, tgt in translations.items()}
	missing = [s["src"] for s in sentences if norm(s["src"]) not in table]
	if missing:
		raise ValueError("thiếu bản dịch cho " + str(len(missing)) + " câu:\n  " + "\n  ".join(missing))
	for s in sentences:
		s["tgt"] = table[norm(s["src"])]
	return sentences


# ── Stage mix: hàm thuần ────────────────────────────────────────────────────

def plan_timing(segments, video_sec, pause=PAUSE, max_speed=MAX_SPEED):
	"""Đặt từng câu: bắt đầu đúng mốc câu gốc (hoặc muộn hơn nếu câu trước tràn); nếu giọng dài
	hơn khoảng trống THỰC CÒN LẠI tới câu sau thì đọc nhanh lên, tối đa max_speed; vẫn tràn thì
	đẩy câu sau lùi lại (không bao giờ chồng giọng).

	ratio = giọng / khoảng trống GỐC của câu: đây là số đo độ dài câu dịch, không phụ thuộc câu
	trước có tràn hay không. Tốc độ thì tính theo khoảng trống thực, để câu bị trễ bù được phần
	trễ thay vì đẩy nó sang câu sau.

	Trả về list {play_start, speed, play_dur, avail, ratio, delay} cùng thứ tự segments."""
	plans, cursor = [], 0.0
	for i, s in enumerate(segments):
		window_end = segments[i + 1]["start"] if i + 1 < len(segments) else video_sec
		avail = max(0.01, window_end - s["start"] - pause)
		ratio = s["tts_sec"] / avail
		play_start = max(s["start"], cursor)
		avail_now = max(0.01, window_end - play_start - pause)
		speed = min(max(s["tts_sec"] / avail_now, 1.0), max_speed)
		play_dur = s["tts_sec"] / speed
		plans.append({
			"play_start": round(play_start, 3), "speed": round(speed, 3), "play_dur": round(play_dur, 3),
			"avail": round(avail, 3), "ratio": round(ratio, 3), "delay": round(play_start - s["start"], 3),
		})
		cursor = play_start + play_dur + pause
	return plans


def build_report(name, segments, plans, video_sec, out_sec, tts_info, translator=""):
	ratios = [p["ratio"] for p in plans]
	bad = suspicious_timing(segments)
	over = [p for p in plans if p["ratio"] > 1.0]
	faster = [p for p in plans if p["speed"] > 1.0]
	capped = [p for p in plans if p["speed"] >= MAX_SPEED]
	lines = [
		f"# Lồng tiếng v0: {name}", "",
		f"- Video gốc {video_sec:.2f}s → video ra {out_sec:.2f}s (dài thêm {out_sec - video_sec:.2f}s)",
		f"- Dịch: {translator or '(không ghi)'}",
		f"- TTS: {tts_info['engine']}, giọng {tts_info['voice']}, {tts_info['synth_sec']:.1f}s cho "
		f"{tts_info['audio_sec']:.1f}s giọng đọc (RTF {tts_info['synth_sec'] / tts_info['audio_sec']:.2f})",
		f"- Tỉ lệ giọng Việt / khoảng trống: trung bình {sum(ratios) / len(ratios):.2f}, lớn nhất {max(ratios):.2f}",
		f"- {len(over)}/{len(plans)} câu Việt dài hơn khoảng trống gốc; {len(faster)}/{len(plans)} câu được đọc nhanh lên; "
		f"{len(capped)} câu chạm trần {MAX_SPEED}x (đẩy câu sau lùi lại)",
		f"- Mốc thời gian nghi sai (> {MAX_CPS:.0f} chữ/giây): "
		+ (", ".join(f"#{i} ({c} chữ/s)" for i, c in bad) if bad else "không có"),
		f"- Tham số tạm: PAUSE={PAUSE}s, MAX_SPEED={MAX_SPEED}x", "",
		"| # | Gốc (s) | Khoảng trống (s) | Giọng Việt (s) | Tỉ lệ | Tốc độ | Trễ (s) | Câu gốc | Câu Việt |",
		"|---|---|---|---|---|---|---|---|---|",
	]
	for i, (s, p) in enumerate(zip(segments, plans)):
		lines.append(
			f"| {i} | {s['start']:.2f}–{s['end']:.2f} | {p['avail']:.2f} | {s['tts_sec']:.2f} | {p['ratio']:.2f} "
			f"| {p['speed']:.2f}x | {p['delay']:.2f} | {s['src']} | {s['tgt']} |"
		)
	return "\n".join(lines)


# ── Phần vào ra ─────────────────────────────────────────────────────────────

def ffmpeg(*args):
	if not FFMPEG.is_file():
		sys.exit(f"LỖI: không thấy ffmpeg của env ở {FFMPEG}")
	subprocess.run([str(FFMPEG), "-nostdin", "-v", "error", "-y", *args], check=True)


def probe_duration(video):
	out = subprocess.run(
		[str(FFMPEG.parent / "ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
		capture_output=True, text=True, check=True,
	).stdout
	return float(out.strip())


def load_json(path):
	return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, data):
	Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# Tên ngôn ngữ Qwen3-ASR ghi trong params -> mã ngắn dùng trong dự án.
ASR_LANGS = {"Chinese": "zh", "Korean": "ko"}


def cmd_segment(a):
	asr = load_json(a.asr_json)
	asr_lang = asr.get("params", {}).get("language")
	if asr_lang not in ASR_LANGS:
		raise ValueError(f"không biết ngôn ngữ nguồn: {a.asr_json} ghi language={asr_lang!r}, cần một trong {list(ASR_LANGS)}")
	sentences = split_sentences(asr["text"], asr["units"])
	translator, translate_info = "", None
	if a.translations:
		sentences = attach_translations(sentences, load_json(a.translations))
		translator = f"manual:{a.translations}"
	a.project.mkdir(parents=True, exist_ok=True)
	path = a.project / "segments.json"
	if path.is_file() and not a.translations:
		# Giữ bản dịch đã có của đúng câu đó: chạy lại không tốn lượt API dịch lại từ đầu.
		old = {s["src"]: s.get("tgt") for s in load_json(path)["segments"]}
		for s in sentences:
			if old.get(s["src"]):
				s["tgt"] = old[s["src"]]
		translator = load_json(path).get("translator", "")
		translate_info = load_json(path).get("translate")
	doc = {
		"schema": SCHEMA, "video": str(a.video), "asr_model": asr["model"], "src_lang": ASR_LANGS[asr_lang],
		"translator": translator, "segments": sentences,
	}
	if translate_info:
		doc["translate"] = translate_info
	save_json(path, doc)
	print(f"[segment] {len(sentences)} câu -> {a.project / 'segments.json'}")
	bad = suspicious_timing(sentences)
	if bad:
		print(f"[segment] CẢNH BÁO: {len(bad)} câu có mốc thời gian vô lý (> {MAX_CPS:.0f} chữ/giây), "
			f"giọng Việt ở đó sẽ lệch hình: " + ", ".join(f"#{i} ({c} chữ/s)" for i, c in bad[:10]))


def cmd_translate(a):
	from dubber import translate as tr

	tr.load_dotenv(a.env)
	cfg = tr.load_config(a.config)
	path = a.project / "segments.json"
	doc = load_json(path)
	translator = tr.Translator(cfg, tr.make_client(cfg), src_lang=doc["src_lang"])
	t0 = time.perf_counter()
	n = tr.translate_segments(doc["segments"], translator, cfg, save=lambda: save_json(path, doc))
	if n == 0:
		print("[translate] mọi câu đã có bản dịch, không gọi API.")
		return
	doc["translator"] = f"api:{cfg['name']}:{cfg['model']}"
	doc["translate"] = {"config": str(a.config), "requests": translator.requests, "sec": round(time.perf_counter() - t0, 1),
		"prompt_tokens": translator.prompt_tokens, "completion_tokens": translator.completion_tokens}
	save_json(path, doc)
	print(f"[translate] dịch {n} câu, {translator.requests} request, token vào {translator.prompt_tokens} / ra {translator.completion_tokens}")


def cmd_tts(a):
	doc = load_json(a.project / "segments.json")
	missing = [i for i, s in enumerate(doc["segments"]) if not s.get("tgt")]
	if missing:
		raise ValueError(f"{len(missing)} câu chưa có bản dịch (vd câu {missing[:5]}). Chạy stage translate trước.")

	import soundfile as sf  # nạp sau khi kiểm: thiếu bản dịch thì báo ngay, không chờ nạp VieNeu
	from vieneu import Vieneu

	tts_dir = a.project / "tts"
	tts_dir.mkdir(exist_ok=True)
	t0 = time.perf_counter()
	# GPU: đường PyTorch của VieNeu, đọc theo lô (đường ONNX của vieneu 3.8.3 gắn cứng CPU). Đo 2026-10-30,
	# tools/bench_tts.py, 24 câu (68s giọng): CPU ONNX int8 51.5s chiếm ~14 nhân; GPU lô 16 1.8s, 1 nhân, VRAM 856 MiB.
	gpu = a.device == "cuda"
	if gpu and a.batch < 1:
		raise ValueError(f"--batch phải >= 1, đang là {a.batch}")
	try:
		if gpu:
			tts = Vieneu(mode="v3turbo", backend="pytorch", device="cuda", max_batch_size=a.batch)
		else:
			tts = Vieneu(mode="v3turbo", backend="onnx", precision=a.precision)
	except Exception as exc:
		# Gặp thật 2026-10-29: cache huggingface_hub 2.x tách .onnx và .data ra hai thư mục.
		if "External data path escapes model directory" in str(exc):
			sys.exit("LỖI: onnxruntime không mở được model VieNeu vì cache để file .onnx và .data ở hai "
				"thư mục khác nhau. Chạy: python tools/hf_materialize.py --apply  rồi chạy lại.")
		raise
	load_sec = time.perf_counter() - t0
	synth_sec = 0.0
	segs = doc["segments"]
	step = a.batch if gpu else 1  # GPU: gọi theo từng lô để web vẫn thấy tiến độ, không chờ hết cả video
	for b in range(0, len(segs), step):
		group = segs[b:b + step]
		t1 = time.perf_counter()
		if gpu:
			wavs = tts.infer_batch([s["tgt"] for s in group], voice=a.voice, batch_size=a.batch)
		else:
			wavs = [tts.infer(group[0]["tgt"], voice=a.voice)]
		synth_sec += time.perf_counter() - t1
		for i, (s, wav) in enumerate(zip(group, wavs), start=b):
			path = tts_dir / f"{i:04d}.wav"
			sf.write(str(path), wav, tts.sample_rate)
			s["tts_wav"] = str(path.relative_to(a.project))
			s["tts_sec"] = round(len(wav) / tts.sample_rate, 3)
			print(f"[tts] {i}: {s['tts_sec']:.2f}s  {s['tgt']}")
	audio_sec = sum(s["tts_sec"] for s in doc["segments"])
	doc["tts"] = {
		"engine": f"VieNeu v3 Turbo PyTorch CUDA, lô {a.batch}" if gpu else f"VieNeu v3 Turbo ONNX {a.precision}", "voice": a.voice, "sample_rate": tts.sample_rate,
		"load_sec": round(load_sec, 2), "synth_sec": round(synth_sec, 2), "audio_sec": round(audio_sec, 2),
	}
	save_json(a.project / "segments.json", doc)
	print(f"[tts] nạp {load_sec:.1f}s, đọc {audio_sec:.1f}s giọng mất {synth_sec:.1f}s (RTF {synth_sec / audio_sec:.2f})")


def cmd_mix(a):
	import numpy as np
	import soundfile as sf

	doc = load_json(a.project / "segments.json")
	segs = doc["segments"]
	sr = doc["tts"]["sample_rate"]
	video_sec = probe_duration(a.video)
	plans = plan_timing(segs, video_sec)

	voice_parts = []
	for s, p in zip(segs, plans):
		src = a.project / s["tts_wav"]
		if p["speed"] > 1.0:
			fast = src.with_name(src.stem + "_fast.wav")
			ffmpeg("-i", str(src), "-filter:a", f"atempo={p['speed']:.3f}", str(fast))  # atempo giữ cao độ giọng
			src = fast
		wav, wsr = sf.read(str(src), dtype="float32")
		if wav.ndim > 1:
			wav = wav.mean(axis=1)
		if wsr != sr:
			sys.exit(f"LỖI: {src} có sample rate {wsr}, mong đợi {sr}")
		voice_parts.append((p["play_start"], wav))

	out_sec = max(video_sec, max(start + len(w) / sr for start, w in voice_parts))
	orig_wav = a.project / "orig_48k.wav"
	ffmpeg("-i", str(a.video), "-map", "0:a:0", "-ac", "1", "-ar", str(sr), str(orig_wav))
	orig, _ = sf.read(str(orig_wav), dtype="float32")
	track = np.zeros(int(out_sec * sr) + 1, dtype=np.float32)
	track[: len(orig)] += orig[: len(track)] * a.orig_volume
	for start, w in voice_parts:
		i = int(start * sr)
		track[i: i + len(w)] += w[: len(track) - i]
	peak = float(np.abs(track).max())
	if peak > 0.99:
		track *= 0.99 / peak  # tránh rè do vượt ngưỡng; ghi lại để biết
		print(f"[mix] đỉnh {peak:.2f} > 0.99, đã hạ cả track xuống {0.99 / peak:.2f} lần")
	mix_wav = a.project / "mix_48k.wav"
	sf.write(str(mix_wav), track, sr)

	out = a.project / f"{a.video.stem}_vi.mp4"
	extra = out_sec - video_sec
	if extra > 0.05:
		# Giọng Việt dài hơn video: giữ khung hình cuối thêm `extra` giây, nên phải mã hoá lại hình.
		video_args = ["-vf", f"tpad=stop_mode=clone:stop_duration={extra:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
	else:
		video_args = ["-c:v", "copy"]
	ffmpeg("-i", str(a.video), "-i", str(mix_wav), "-map", "0:v:0", "-map", "1:a:0", *video_args,
		"-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out))

	doc["mix"] = {"orig_volume": a.orig_volume, "pause": PAUSE, "max_speed": MAX_SPEED, "plans": plans,
		"video_sec": round(video_sec, 3), "out_sec": round(out_sec, 3)}
	save_json(a.project / "segments.json", doc)
	translator = doc.get("translator", "")
	if doc.get("translate"):
		t = doc["translate"]
		translator += f" ({t['requests']} request, token vào {t['prompt_tokens']} / ra {t['completion_tokens']})"
	(a.project / "report.md").write_text(build_report(a.video.name, segs, plans, video_sec, out_sec, doc["tts"], translator), encoding="utf-8")
	print(f"[mix] video: {out}")
	print(f"[mix] báo cáo: {a.project / 'report.md'}")


def main():
	p = argparse.ArgumentParser(description="Bộ khung lồng tiếng v0")
	sub = p.add_subparsers(dest="cmd", required=True)
	s = sub.add_parser("segment")
	s.add_argument("--video", type=Path, required=True)
	s.add_argument("--asr-json", type=Path, required=True)
	s.add_argument("--translations", type=Path, help="file dịch tay; bỏ trống thì để stage translate dịch")
	s.add_argument("--project", type=Path, required=True)
	r = sub.add_parser("translate")
	r.add_argument("--project", type=Path, required=True)
	r.add_argument("--config", type=Path, required=True)
	r.add_argument("--env", type=Path, required=True, help="file .env chứa key")
	t = sub.add_parser("tts")
	t.add_argument("--project", type=Path, required=True)
	t.add_argument("--voice", required=True)
	t.add_argument("--precision", required=True, choices=["int8", "fp32"], help="chỉ dùng cho đường CPU (ONNX)")
	# Mặc định giữ hành vi cũ (CPU, từng câu); tools/dub.sh chọn GPU.
	t.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
	t.add_argument("--batch", type=int, default=1, help="chỉ dùng cho GPU: số câu đọc cùng lúc")
	m = sub.add_parser("mix")
	m.add_argument("--video", type=Path, required=True)
	m.add_argument("--project", type=Path, required=True)
	m.add_argument("--orig-volume", type=float, required=True)
	a = p.parse_args()
	try:
		{"segment": cmd_segment, "translate": cmd_translate, "tts": cmd_tts, "mix": cmd_mix}[a.cmd](a)
	except (ValueError, TranslateError) as exc:
		sys.exit(f"LỖI: {exc}")


if __name__ == "__main__":
	main()