"""Đo TTS VieNeu trên CPU (ONNX) và GPU (PyTorch, đọc theo lô) với câu thật, để chọn chỗ chạy TTS bằng số đo.

Bước này trả lời câu hỏi: đẩy TTS sang GPU có đáng không (Thuận hỏi 2026-10-30, sợ TTS ăn CPU).
VieNeu v3 Turbo sinh âm thanh từng khung như LLM sinh chữ (khung sau chờ khung trước), nên đọc từng câu
một thì GPU phí sức; GPU chỉ thắng rõ khi đọc nhiều câu cùng lúc (infer_batch). Pipeline biết trước mọi
câu, nên đo cả các cỡ lô. Đường ONNX của VieNeu 3.8.3 gắn cứng CPUExecutionProvider: muốn GPU phải dùng
đường PyTorch (vieneu[cuda] cần torch==2.8.0, env dub_asr có sẵn).

Mỗi chế độ chạy trong env của nó, ghi kết quả + file wav để nghe so sánh; --report gộp thành bảng.
Chỉ ghi vào data/bench/tts/, không đụng project.

Dùng:  conda activate dub_tts
       python tools/bench_tts.py cpu --project data/projects/vidtrung10phut__translator_gemini --n 24
       conda activate dub_tts_gpu
       python tools/bench_tts.py gpu --project data/projects/vidtrung10phut__translator_gemini --n 24 --batch-sizes 1 4 8 16
       python tools/bench_tts.py report
"""
import argparse
import json
import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "bench" / "tts"
VOICE = "Thiện Minh"  # trùng VOICE_DEFAULT trong tools/dub.sh


# ── Phần quyết định: hàm thuần ──────────────────────────────────────────────

def pick_texts(segments, n):
	"""n câu Việt đầu tiên có chữ, theo thứ tự trong video: đúng loại câu pipeline sẽ đọc."""
	return [s["tgt"] for s in segments if (s.get("tgt") or "").strip()][:n]


def summarize(run):
	"""Một lần đo -> các số để so: RTF (giây xử lý / giây giọng), số nhân CPU trung bình."""
	return {**run, "rtf": round(run["synth_sec"] / run["audio_sec"], 3) if run["audio_sec"] else None,
		"cpu_cores": round(run["cpu_sec"] / run["synth_sec"], 1) if run["synth_sec"] else None}


def report_table(runs):
	lines = ["| Chế độ | Lô | Nạp model (s) | Đọc (s) | Giọng ra (s) | RTF | Nhân CPU TB | VRAM đỉnh (MiB) |",
		"|---|---|---|---|---|---|---|---|"]
	for r in sorted(runs, key=lambda r: (r["mode"], r["batch"])):
		r = summarize(r)
		lines.append(f"| {r['mode']} | {r['batch']} | {r['load_sec']:.1f} | {r['synth_sec']:.1f} | {r['audio_sec']:.1f} "
			f"| {r['rtf']} | {r['cpu_cores']} | {r.get('vram_mib') or '-'} |")
	return "\n".join(lines)


# ── Phần vào ra ─────────────────────────────────────────────────────────────

def cpu_seconds():
	"""CPU đã dùng của tiến trình này (mọi luồng cộng lại): chia cho thời gian thật = số nhân dùng trung bình."""
	u = resource.getrusage(resource.RUSAGE_SELF)
	return u.ru_utime + u.ru_stime


def save_wavs(folder, wavs, sr):
	import soundfile as sf

	folder.mkdir(parents=True, exist_ok=True)
	for i, w in enumerate(wavs):
		sf.write(str(folder / f"{i:03d}.wav"), w, sr)


def run_cpu(texts, threads):
	from vieneu import Vieneu

	t0 = time.perf_counter()
	tts = Vieneu(mode="v3turbo", backend="onnx", precision="int8", threads=threads)
	load = time.perf_counter() - t0
	c0, t1 = cpu_seconds(), time.perf_counter()
	wavs = [tts.infer(t, voice=VOICE) for t in texts]  # đúng cách pipeline đang đọc (dubber/v0.py)
	synth, cpu = time.perf_counter() - t1, cpu_seconds() - c0
	name = f"cpu_t{threads}" if threads else "cpu"
	save_wavs(OUT / name, wavs, tts.sample_rate)
	return [{"mode": name, "batch": 1, "load_sec": round(load, 2), "synth_sec": round(synth, 2), "cpu_sec": round(cpu, 2),
		"audio_sec": round(sum(len(w) for w in wavs) / tts.sample_rate, 2), "n": len(texts)}]


def run_gpu(texts, batch_sizes):
	import torch
	from vieneu import Vieneu

	t0 = time.perf_counter()
	tts = Vieneu(mode="v3turbo", backend="pytorch", device="cuda", max_batch_size=max(batch_sizes))
	load = time.perf_counter() - t0
	tts.infer_batch(texts[:2], voice=VOICE, batch_size=2)  # khởi động: lần gọi đầu trên GPU chậm, không tính vào số đo
	torch.cuda.synchronize()
	out = []
	for bs in batch_sizes:
		torch.cuda.reset_peak_memory_stats()
		c0, t1 = cpu_seconds(), time.perf_counter()
		wavs = tts.infer_batch(texts, voice=VOICE, batch_size=bs)
		torch.cuda.synchronize()
		synth, cpu = time.perf_counter() - t1, cpu_seconds() - c0
		save_wavs(OUT / f"gpu_b{bs}", wavs, tts.sample_rate)
		out.append({"mode": "gpu", "batch": bs, "load_sec": round(load, 2), "synth_sec": round(synth, 2), "cpu_sec": round(cpu, 2),
			"audio_sec": round(sum(len(w) for w in wavs) / tts.sample_rate, 2), "n": len(texts),
			"vram_mib": round(torch.cuda.max_memory_allocated() / 2**20)})
		print(f"[bench] gpu lô {bs}: {synth:.1f}s cho {out[-1]['audio_sec']:.1f}s giọng, VRAM đỉnh {out[-1]['vram_mib']} MiB")
	return out


def main():
	p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
	p.add_argument("mode", choices=["cpu", "gpu", "report"])
	p.add_argument("--project", type=Path)
	p.add_argument("--n", type=int, default=24)
	p.add_argument("--batch-sizes", type=int, nargs="+", default=[1, 4, 8, 16])
	p.add_argument("--threads", type=int, default=0, help="cpu: số luồng ONNX; 0 = mặc định của VieNeu")
	a = p.parse_args()

	results = OUT / "results.json"
	if a.mode == "report":
		if not results.is_file():
			sys.exit(f"LỖI: chưa có {results}. Chạy chế độ cpu và gpu trước.")
		print(report_table(json.loads(results.read_text(encoding="utf-8"))))
		print(f"\nNghe so sánh: {OUT}/<chế độ>/000.wav ... (cùng số thứ tự = cùng câu)")
		return
	if a.project is None or not (a.project / "segments.json").is_file():
		sys.exit("LỖI: cần --project là một project đã dịch xong (có segments.json)")
	texts = pick_texts(json.loads((a.project / "segments.json").read_text(encoding="utf-8"))["segments"], a.n)
	if not texts:
		sys.exit(f"LỖI: {a.project} chưa có câu dịch nào")
	print(f"[bench] {a.mode}: {len(texts)} câu từ {a.project}")
	runs = run_cpu(texts, a.threads) if a.mode == "cpu" else run_gpu(texts, a.batch_sizes)
	old = json.loads(results.read_text(encoding="utf-8")) if results.is_file() else []
	keep = [r for r in old if (r["mode"], r["batch"]) not in {(n["mode"], n["batch"]) for n in runs}]
	OUT.mkdir(parents=True, exist_ok=True)
	results.write_text(json.dumps(keep + runs, ensure_ascii=False, indent=1), encoding="utf-8")
	print(report_table(keep + runs))


if __name__ == "__main__":
	main()