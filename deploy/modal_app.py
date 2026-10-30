"""Deploy Dubber lên Modal: bản chạy thật cho vài người bạn dùng thử, chỉ dùng credit miễn phí (Thuận chốt 2026-10-30).

Cấu trúc giữ đúng như trên máy Thuận, chỉ đổi nơi chạy:
  - asr_stage (image ASR: torch + qwen-asr, GPU L4): nhận audio, trả JSON ASR, stream log từng dòng.
  - web (image TTS/web: vieneu + onnxruntime + Flask, CPU): trang giới thiệu, /app, hàng đợi job.
    Mỗi job chạy deploy/run_job.py: gọi asr_stage từ xa, rồi segment/translate/tts/mix ngay trong container.
  - Volume "dubber-data" gắn vào data/ của web: video tải lên, kết quả, lịch sử job, ví dụ trang giới thiệu.
  - Secret "dubber": GEMINI_API_KEY (tạo trên trang Modal, không nằm trong code).
Model được tải sẵn vào image lúc build: container khởi động không phải tải lại vài GB.

Vì sao GPU L4 chứ không T4: qwen-asr chạy bf16 (dubber/asr.py), cần GPU Ampere trở lên (sm80+). T4 là sm75,
không có bf16 thật. L4 (sm89) có, 24 GB VRAM, giá ~$0.000222/giây (modal.com/pricing, tra 2026-10-30).

Dùng (env conda riêng có gói modal, chạy ở thư mục gốc dự án):
  modal run deploy/modal_app.py       kiểm hai image: GPU, bf16, ffmpeg, model, đọc thử một câu
  modal deploy deploy/modal_app.py    deploy, in ra link web
"""
from pathlib import Path

import modal

APP_NAME = "dubber"  # PHẢI trùng APP_NAME trong deploy/run_job.py (test khoá)
REMOTE = "/root/dubber_vid"  # thư mục dự án trong container: data/, configs/... nằm đúng chỗ như trên máy
LOCAL = Path(__file__).resolve().parent.parent
# Giới hạn bản deploy (Thuận chốt 2026-10-30). MAX_VIDEO_SEC khớp SITE.beta.maxMinutes trong web/landing.html.
MAX_VIDEO_SEC = 600
TRANSLATOR = "configs/translator_gemini.json"  # Flash-Lite ~500 lượt/ngày; Flash chỉ ~20
MAX_UPLOAD_MB = 500  # Modal nhận request tới 4 GiB; 500 MB đủ cho video 10 phút
ASR_MODELS = ("Qwen/Qwen3-ASR-1.7B", "Qwen/Qwen3-ForcedAligner-0.6B")  # trùng dubber/asr.py

# dubber/ gọi ffmpeg nằm cạnh sys.executable (thói quen của env conda). Trên Modal ffmpeg cài bằng apt ở /usr/bin,
# nên tạo link cạnh python thay vì sửa code.
LINK_FFMPEG = ('d="$(dirname "$(python -c "import sys; print(sys.executable)")")" && '
	'ln -sf /usr/bin/ffmpeg "$d/ffmpeg" && ln -sf /usr/bin/ffprobe "$d/ffprobe"')


def _download_asr():
	from huggingface_hub import snapshot_download

	for repo in ASR_MODELS:
		snapshot_download(repo)


def _download_tts():
	import subprocess

	from vieneu import Vieneu

	try:
		Vieneu(mode="v3turbo", backend="onnx", precision="int8")  # lần đầu: tải model về cache trong image
	except Exception as exc:  # gặp 2026-10-29: cache dạng symlink làm onnxruntime từ chối mở; materialize rồi thử lại
		print(f"[build] nạp VieNeu lần đầu lỗi ({exc}), thay symlink bằng file thật rồi thử lại")
	subprocess.run(["python", f"{REMOTE}/tools/hf_materialize.py", "--apply"], check=True)
	Vieneu(mode="v3turbo", backend="onnx", precision="int8")  # lần này phải mở được, không thì build dừng


asr_image = (
	modal.Image.debian_slim(python_version="3.12")
	.apt_install("ffmpeg")
	.run_commands(LINK_FFMPEG)
	.pip_install("torch==2.8.0", "torchaudio==2.8.0", index_url="https://download.pytorch.org/whl/cu126")
	# Cùng phiên bản với env dub_asr trên máy Thuận (envs/dub_asr.txt).
	.pip_install("qwen-asr==0.0.6", "transformers==4.57.6", "accelerate==1.12.0", "huggingface_hub==0.36.2",
		"soundfile==0.14.0", "numpy==2.5.4")
	.run_function(_download_asr)
	.add_local_dir(LOCAL / "dubber", f"{REMOTE}/dubber")
)

web_image = (
	modal.Image.debian_slim(python_version="3.12")
	.apt_install("ffmpeg")
	.run_commands(LINK_FFMPEG)
	# Cùng phiên bản với env dub_tts trên máy Thuận (envs/dub_tts.txt; Flask lấy theo envs/dub_asr.txt).
	.pip_install("vieneu==3.8.3", "onnxruntime==1.31.1", "huggingface_hub==2.3.0", "soundfile==0.14.0", "numpy==2.5.4",
		"openai==3.41.0", "Flask==3.1.3", "Werkzeug==3.1.9")
	.add_local_file(LOCAL / "tools" / "hf_materialize.py", f"{REMOTE}/tools/hf_materialize.py", copy=True)
	.run_function(_download_tts)
	.add_local_dir(LOCAL / "dubber", f"{REMOTE}/dubber")
	.add_local_dir(LOCAL / "web", f"{REMOTE}/web")
	.add_local_dir(LOCAL / "configs", f"{REMOTE}/configs")
	.add_local_dir(LOCAL / "deploy", f"{REMOTE}/deploy")
)

app = modal.App(APP_NAME)
data = modal.Volume.from_name("dubber-data", create_if_missing=True)
secret = modal.Secret.from_name("dubber")


@app.function(image=asr_image, gpu="L4", timeout=1800)
def asr_stage(audio, lang, max_chunk):
	"""audio (bytes, FLAC 16 kHz mono) -> stream từng dòng log [asr], cuối cùng {"json": nội dung JSON ASR}."""
	import queue
	import sys
	import tempfile
	import threading

	sys.path.insert(0, REMOTE)
	from dubber import asr

	work = Path(tempfile.mkdtemp())
	src, out = work / "audio.flac", work / "asr.json"
	src.write_bytes(audio)
	q, err = queue.Queue(), []

	def go():
		try:
			asr.run(src, lang, out, max_chunk, log=q.put)
		except BaseException as exc:  # gồm cả sys.exit trong asr.py: phải báo về, không im lặng
			err.append(f"{type(exc).__name__}: {exc}")
		finally:
			q.put(None)

	threading.Thread(target=go, daemon=True).start()
	while (line := q.get()) is not None:
		yield line
	if err:
		raise RuntimeError(f"ASR lỗi: {err[0]}")
	yield {"json": out.read_text(encoding="utf-8")}


@app.function(image=web_image, volumes={f"{REMOTE}/data": data}, secrets=[secret], cpu=8.0, memory=16384,
	timeout=3600, max_containers=1, scaledown_window=1200)
@modal.concurrent(max_inputs=50)
@modal.wsgi_app()
def web():
	# max_containers=1: hàng đợi job nằm trong bộ nhớ một tiến trình (web/app.py), hai container sẽ chạy song song
	# và ghi đè lịch sử của nhau. scaledown_window=1200: container sống 20 phút sau request cuối; job video 10 phút
	# chạy ~5-8 phút nên không bị tắt giữa chừng kể cả khi người dùng đóng trang (đóng lâu hơn thì job "Bị ngắt").
	# cpu=8: TTS chạy CPU (ONNX), trên i9 của Thuận video 10 phút mất ~205 giây.
	import os
	import sys

	os.chdir(REMOTE)
	sys.path.insert(0, REMOTE)
	from web.app import create_app

	return create_app(root=REMOTE, command=(sys.executable, "-m", "deploy.run_job"), max_upload_mb=MAX_UPLOAD_MB,
		max_video_sec=MAX_VIDEO_SEC, only_translator=TRANSLATOR)


# ── Kiểm trước khi deploy: modal run deploy/modal_app.py ───────────────────

@app.function(image=asr_image, gpu="L4", timeout=600)
def check_asr():
	import subprocess
	import sys

	import torch

	ffmpeg = Path(sys.executable).parent / "ffmpeg"
	return [
		f"torch {torch.__version__}, CUDA {torch.version.cuda}, GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'KHÔNG CÓ'}",
		f"bf16: {'có' if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else 'KHÔNG'}",
		f"ffmpeg: {subprocess.run([str(ffmpeg), '-version'], capture_output=True, text=True).stdout.splitlines()[0]}",
	]


@app.function(image=web_image, cpu=8.0, memory=16384, timeout=600)
def check_web():
	import subprocess
	import sys
	import time

	from vieneu import Vieneu

	sys.path.insert(0, REMOTE)
	from web.app import load_voices

	t0 = time.perf_counter()
	tts = Vieneu(mode="v3turbo", backend="onnx", precision="int8")
	wav = tts.infer("Xin chào, đây là Dubber chạy trên Modal.", voice="Thiện Minh")
	ffprobe = Path(sys.executable).parent / "ffprobe"
	return [
		f"VieNeu: {len(load_voices())} giọng, đọc thử {len(wav) / tts.sample_rate:.1f}s giọng mất {time.perf_counter() - t0:.1f}s (gồm nạp model)",
		f"ffprobe: {subprocess.run([str(ffprobe), '-version'], capture_output=True, text=True).stdout.splitlines()[0]}",
	]


@app.local_entrypoint()
def main():
	for name, fn in (("image ASR (GPU)", check_asr), ("image web/TTS (CPU)", check_web)):
		print(f"== {name}")
		for line in fn.remote():
			print(f"   {line}")