"""Chạy một job lồng tiếng trên Modal: bản thay cho tools/dub.sh khi deploy (deploy/modal_app.py).

Vì sao không dùng thẳng tools/dub.sh: trên Modal không có conda, và ASR cần GPU nằm ở một container
khác (image riêng có torch). Các stage còn lại (segment, translate, tts, mix) chạy ngay trong container
web như ở máy Thuận. Log in ra GIỐNG HỆT dub.sh ([dub] ... -> project, [asr] ..., [dub] XONG sau ...),
vì web/app.py đọc log đó để vẽ tiến độ: một hợp đồng log, hai nơi chạy (có test khoá).

ASR từ xa nhận audio (FLAC 16 kHz mono, video 10 phút ~ vài MB) chứ không nhận video: gửi ít dữ liệu
hơn, và không phải chia sẻ file qua Volume giữa hai container (Volume chỉ reload được khi không có file
nào đang mở, mà web luôn mở log của job đang chạy).

Dùng (web gọi, không chạy tay): python -m deploy.run_job <video> <zh|ko> <config dịch> <giọng>
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP_NAME = "dubber"  # tên app trên Modal; deploy/modal_app.py dùng cùng tên
ASR_MAX_CHUNK = 30  # giống tools/dub.sh: đo 2026-10-29, đoạn 147s aligner gán sai mốc, đoạn 30s đúng
ORIG_VOLUME = 0.12  # giống tools/dub.sh
ROOT = Path(__file__).resolve().parent.parent


class StepFailed(RuntimeError):
	pass


def run(video, lang, config, voice, root, asr_remote, step, extract, log=print):
	"""Điều phối các stage. Trả mã thoát: 0 xong, 1 lỗi (dòng cuối là "LỖI: ...")."""
	name = Path(video).stem
	asr_json = Path("data/asr") / f"{name}.json"
	project = f"data/projects/{name}__{Path(config).stem}"
	log(f"[dub] video={video} lang={lang} dịch={config} giọng={voice} đoạn ASR={ASR_MAX_CHUNK}s -> {project}")
	t0 = time.time()
	try:
		result = None
		for item in asr_remote(extract(video), lang, ASR_MAX_CHUNK):
			if isinstance(item, dict):
				result = item["json"]
			else:
				log(item)
		if result is None:
			raise StepFailed("ASR trên GPU không trả kết quả")
		(Path(root) / asr_json).parent.mkdir(parents=True, exist_ok=True)
		(Path(root) / asr_json).write_text(result, encoding="utf-8")
		step("segment", "--video", video, "--asr-json", str(asr_json), "--project", project)
		step("translate", "--project", project, "--config", config, "--env", ".env")
		step("tts", "--project", project, "--voice", voice, "--precision", "int8")
		step("mix", "--video", video, "--project", project, "--orig-volume", str(ORIG_VOLUME))
	except Exception as exc:  # mọi lỗi đều phải thành một dòng LỖI cho web đọc, không để job treo
		log(f"LỖI: {type(exc).__name__}: {exc}")
		return 1
	log(f"[dub] XONG sau {round(time.time() - t0)}s: {project}/{name}_vi.mp4")
	return 0


# ── Phần vào ra thật (chỉ chạy trong container Modal) ──────────────────────

def _step(name, *args):
	"""Một stage của dubber/v0.py trong tiến trình con; log của nó đi thẳng ra stdout cho web đọc."""
	code = subprocess.run([sys.executable, "-m", "dubber.v0", name, *args], cwd=ROOT).returncode
	if code != 0:
		raise StepFailed(f"stage {name} thoát mã {code}")


def _extract(video):
	ffmpeg = Path(sys.executable).parent / "ffmpeg"
	with tempfile.TemporaryDirectory() as d:
		out = Path(d) / "audio.flac"
		subprocess.run([str(ffmpeg), "-nostdin", "-v", "error", "-y", "-i", str(ROOT / video), "-map", "0:a:0",
			"-ac", "1", "-ar", "16000", "-c:a", "flac", str(out)], check=True)
		return out.read_bytes()


def main():
	import modal

	video, lang, config, voice = sys.argv[1:5]
	asr = modal.Function.from_name(APP_NAME, "asr_stage")
	log = lambda line: print(line, flush=True)  # noqa: E731
	sys.exit(run(video, lang, config, voice, ROOT, asr.remote_gen, _step, _extract, log))


if __name__ == "__main__":
	main()