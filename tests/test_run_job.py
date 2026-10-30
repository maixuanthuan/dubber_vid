"""Test deploy/run_job.py: phần điều phối job trên Modal (thay cho tools/dub.sh), không cần Modal hay GPU."""
import importlib.util
import json
from pathlib import Path

import pytest

from web import app as web

_spec = importlib.util.spec_from_file_location("run_job", Path(__file__).resolve().parent.parent / "deploy" / "run_job.py")
run_job = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_job)


def _fake_asr(lines, result):
	def asr_remote(audio_bytes, lang, max_chunk):
		assert audio_bytes == b"flac" and lang == "zh" and max_chunk == run_job.ASR_MAX_CHUNK
		yield from lines
		if result is not None:
			yield {"json": result}
	return asr_remote


def test_log_cua_job_tren_modal_web_doc_duoc_nhu_dub_sh(tmp_path):
	# Tình huống thật: web đọc log để vẽ thanh tiến độ; trên Modal job chạy bằng run_job chứ không phải tools/dub.sh.
	# Phần giả lập: ASR từ xa giả trả 2 dòng log + JSON; các stage còn lại giả (chỉ ghi lại lệnh).
	# Đỏ thì hỏng gì: trên bản deploy thanh tiến độ đứng im, job xong mà web không biết, không hiện video.
	out, steps = [], []
	code = run_job.run("data/uploads/j/j__clip.mp4", "zh", "configs/translator_gemini.json", "Thiện Minh", root=tmp_path,
		asr_remote=_fake_asr(["[asr] audio 10.0s, 1 đoạn: 0.0–10.0", "[asr] nhận dạng đoạn 1/1: 10 ký tự"], '{"text": ""}'),
		step=lambda *a: steps.append(a[0]), extract=lambda video: b"flac", log=out.append)
	assert code == 0
	assert steps == ["segment", "translate", "tts", "mix"]
	assert json.loads((tmp_path / "data" / "asr" / "j__clip.json").read_text(encoding="utf-8")) == {"text": ""}
	p = web.parse_progress(out)
	assert p["stage"] == "done" and p["audio_sec"] == 10.0
	assert p["project"] == "data/projects/j__clip__translator_gemini"
	assert p["result"] == "data/projects/j__clip__translator_gemini/j__clip_vi.mp4"


def test_loi_o_buoc_nao_cung_in_LOI_va_dung(tmp_path):
	# Tình huống thật: ASR trên GPU từ xa hỏng (thiếu thư viện, hết bộ nhớ), hoặc một stage thoát mã khác 0.
	# Phần giả lập: ASR giả không trả JSON; lần sau stage translate giả ném lỗi.
	# Đỏ thì hỏng gì: job treo hoặc báo xong, các stage sau chạy trên dữ liệu thiếu; lỗi không có dòng LỖI để web đọc.
	out = []
	assert run_job.run("data/uploads/j/x.mp4", "zh", "c.json", "v", root=tmp_path, asr_remote=_fake_asr([], None),
		step=lambda *a: None, extract=lambda video: b"flac", log=out.append) == 1
	assert out[-1].startswith("LỖI:")

	def boom(name, *args):
		if name == "translate":
			raise run_job.StepFailed("translate")
	out, steps = [], []
	assert run_job.run("data/uploads/j/x.mp4", "zh", "c.json", "v", root=tmp_path, asr_remote=_fake_asr([], "{}"),
		step=boom, extract=lambda video: b"flac", log=out.append) == 1
	assert out[-1].startswith("LỖI:") and not any("XONG" in l for l in out)


def test_ten_app_va_gioi_han_khop_giua_cac_file():
	# Tình huống thật: run_job gọi ASR bằng tên app trên Modal; bảng giá trang giới thiệu ghi giới hạn của bản deploy.
	# Phần giả lập: đọc chữ trong deploy/modal_app.py và web/landing.html (không cần cài gói modal).
	# Đỏ thì hỏng gì: đổi tên app một chỗ thì mọi job trên Modal lỗi "không tìm thấy hàm"; bảng giá ghi sai giới hạn.
	import re

	root = Path(__file__).resolve().parent.parent
	src = (root / "deploy" / "modal_app.py").read_text(encoding="utf-8")
	assert re.search(r'^APP_NAME = "([^"]+)"', src, re.M).group(1) == run_job.APP_NAME
	sec = int(re.search(r"^MAX_VIDEO_SEC = (\d+)", src, re.M).group(1))
	landing = (root / "web" / "landing.html").read_text(encoding="utf-8")
	assert int(re.search(r"maxMinutes: (\d+)", landing).group(1)) * 60 == sec
