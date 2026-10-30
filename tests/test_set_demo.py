"""Test tools/set_demo.py: phần dựng dữ liệu ví dụ cho trang giới thiệu (hàm thuần, không chép file)."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("set_demo", Path(__file__).resolve().parent.parent / "tools" / "set_demo.py")
set_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(set_demo)

DOC = {
	"video": "data/samples/x.mp4", "src_lang": "zh", "asr_model": "Qwen/Qwen3-ASR-1.7B",
	"translator": "api:gemini:m", "translate": {"requests": 3},
	"tts": {"voice": "Thiện Minh", "engine": "VieNeu"},
	"segments": [
		{"src": "一寸光阴一寸金。", "tgt": "Một tấc thời gian một tấc vàng.", "start": 0.5, "end": 2.0, "tts_wav": "tts/0000.wav", "tts_sec": 1.8},
		{"src": "好。", "tgt": "Được.", "start": 2.4, "end": 2.8, "tts_wav": "tts/0001.wav", "tts_sec": 0.5},
	],
	"mix": {"video_sec": 30.0, "out_sec": 30.4, "plans": [{"play_start": 0.5, "play_dur": 1.8}, {"play_start": 2.45, "play_dur": 0.5}]},
}
VOICES = [{"name": "Thiện Minh", "desc": "Nam · Bắc · Phong cách kể chuyện"}, {"name": "Mai Anh", "desc": "Nữ · Bắc · Phong cách tin tức"}]


def test_du_lieu_vi_du_chi_co_truong_trang_can():
	# Tình huống thật: trang giới thiệu là trang tĩnh công khai, đọc data/demo/*.json (Thuận chốt 2026-10-30).
	# Phần giả lập: segments.json của một project đã ghép xong, có đường dẫn và thông số nội bộ.
	# Đỏ thì hỏng gì: lộ đường dẫn, model, số request ra trang công khai; hoặc trang thiếu số để vẽ timeline.
	segs, meta = set_demo.build_demo(DOC, VOICES)
	assert segs[1] == {"start": 2.4, "end": 2.8, "src": "好。", "tgt": "Được.", "play_start": 2.45, "play_end": 2.95}
	assert meta == {"schema": "demo/v1", "src_lang": "zh", "voice": "Thiện Minh", "video_sec": 30.0, "out_sec": 30.4,
		"sentences": 2, "max_delay": 0.05, "overlaps": 0, "voices": VOICES}
	text = repr((segs, meta))
	for secret in ("data/samples", "tts/", "Qwen", "gemini", "requests"):
		assert secret not in text


def test_project_chua_ghep_xong_thi_dung():
	# Tình huống thật: chạy set_demo trên project bị dừng trước bước ghép.
	# Phần giả lập: thiếu mix.plans.
	# Đỏ thì hỏng gì: trang có phụ đề nhưng không có mốc phát bản Việt, phụ đề chạy lệch.
	with pytest.raises(ValueError, match="chưa chạy xong bước ghép"):
		set_demo.build_demo({**DOC, "mix": {}}, VOICES)


def test_dem_cau_chong_giong_tu_moc_phat():
	# Tình huống thật: trang giới thiệu ghi "0 câu chồng giọng"; con số này phải đếm từ dữ liệu, không gõ tay.
	# Phần giả lập: câu 2 bắt đầu phát (2.0s) trước khi câu 1 đọc xong (0.5 + 1.8 = 2.3s).
	# Đỏ thì hỏng gì: trang khoe "0 câu chồng giọng" trong khi ví dụ thật có câu đè lên nhau.
	plans = [{"play_start": 0.5, "play_dur": 1.8}, {"play_start": 2.0, "play_dur": 0.5}]
	_, meta = set_demo.build_demo({**DOC, "mix": {**DOC["mix"], "plans": plans}}, VOICES)
	assert meta["overlaps"] == 1 and meta["max_delay"] == 0.0