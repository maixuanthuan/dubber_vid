"""Test phần quyết định của dubber/v0.py: không cần GPU, model hay ffmpeg."""
import pytest

from dubber import v0


def _units(text, step=0.5):
	"""Timestamp giả kiểu aligner Qwen: mỗi chữ một đơn vị, không có dấu câu."""
	chars = [c for c in text if v0.kept(c)]
	return [{"start": i * step, "end": i * step + 0.4, "text": c} for i, c in enumerate(chars)]


def test_split_sentences_cat_dung_dau_ket_cau():
	# Tình huống thật: Qwen trả cả đoạn có 。；, aligner trả mốc từng chữ không kèm dấu câu.
	# Phần giả lập: 2 câu, mỗi chữ cách nhau 0.5s.
	# Đỏ thì hỏng gì: câu bị cắt sai chỗ hoặc lệch giờ, giọng Việt phát lệch hình.
	text = "我明天去北京。你呢？"
	out = v0.split_sentences(text, _units(text))
	assert [s["src"] for s in out] == ["我明天去北京。", "你呢？"]
	assert out[0]["start"] == 0.0 and out[0]["end"] == pytest.approx(2.9)
	assert out[1]["start"] == 3.0


def test_split_sentences_doan_cuoi_khong_co_dau_van_giu():
	# Tình huống thật: video bị cắt ngang, câu cuối không có dấu chấm.
	# Phần giả lập: text kết thúc không dấu.
	# Đỏ thì hỏng gì: mất câu cuối, đoạn cuối video im lặng.
	text = "我明天去。北京"
	assert [s["src"] for s in v0.split_sentences(text, _units(text))] == ["我明天去。", "北京"]


def test_split_sentences_text_va_timestamp_lech_thi_bao_loi():
	# Tình huống thật: phiên bản thư viện khác làm sạch ký tự khác, text và timestamp không còn khớp.
	# Phần giả lập: timestamp thiếu một chữ.
	# Đỏ thì hỏng gì: mốc thời gian bị dồn lệch âm thầm cho mọi câu phía sau.
	text = "我明天去北京。"
	with pytest.raises(ValueError, match="lệch"):
		v0.split_sentences(text, _units("我明天去北"))


def test_attach_translations_bao_du_moi_cau_thieu():
	# Tình huống thật: file dịch thiếu câu (ASR chạy lại ra câu hơi khác).
	# Phần giả lập: 2 câu, chỉ có bản dịch cho 1 câu; key khác dấu câu vẫn phải khớp.
	# Đỏ thì hỏng gì: TTS nhận câu rỗng, video có đoạn câm mà không ai biết vì sao.
	segs = [{"src": "我明天去北京。"}, {"src": "你呢？"}]
	with pytest.raises(ValueError, match="1 câu"):
		v0.attach_translations([dict(s) for s in segs], {"我明天去北京": "Mai tôi đi Bắc Kinh."})
	out = v0.attach_translations([dict(s) for s in segs], {"我明天去北京": "Mai tôi đi Bắc Kinh.", "你呢?": "Còn bạn?"})
	assert [s["tgt"] for s in out] == ["Mai tôi đi Bắc Kinh.", "Còn bạn?"]


def test_plan_timing_khong_bao_gio_chong_giong():
	# Tình huống thật: câu Việt dài hơn câu gốc nhiều (tiếng Trung rất đặc).
	# Phần giả lập: câu 0 cần 6s giọng nhưng chỉ có 2s trống trước câu 1.
	# Đỏ thì hỏng gì: hai câu đọc đè lên nhau, nghe không ra chữ.
	segs = [{"start": 0.0, "tts_sec": 6.0}, {"start": 2.15, "tts_sec": 1.0}]
	plans = v0.plan_timing(segs, video_sec=10.0, pause=0.15, max_speed=1.3)
	assert plans[0]["speed"] == 1.3  # chạm trần, không ép nhanh hơn
	end0 = plans[0]["play_start"] + plans[0]["play_dur"]
	assert plans[1]["play_start"] >= end0 + 0.15 - 1e-6
	assert plans[1]["delay"] > 0


def test_plan_timing_cau_bi_tre_thi_doc_nhanh_de_bu():
	# Tình huống thật: câu trước tràn, đẩy câu này trễ; tính tốc độ theo khoảng trống GỐC thì câu
	# này vẫn đọc 1.0x, phần trễ lan sang các câu sau và video bị kéo dài (thấy khi chạy thử 2026-10-29).
	# Phần giả lập: câu 1 vừa khoảng trống gốc (2.5s trong 2.85s) nhưng bị trễ 0.73s.
	# Đỏ thì hỏng gì: trễ dồn qua từng câu, cuối video giọng Việt lệch hình mỗi lúc một xa.
	segs = [{"start": 0.5, "tts_sec": 4.0}, {"start": 3.0, "tts_sec": 2.5}]
	plans = v0.plan_timing(segs, video_sec=6.0, pause=0.15, max_speed=1.3)
	assert plans[1]["delay"] > 0.7
	assert plans[1]["speed"] > 1.0
	assert plans[1]["ratio"] < 1.0  # số đo độ dài vẫn so với khoảng trống gốc
	assert plans[1]["play_start"] + plans[1]["play_dur"] <= 6.0 - 0.15 + 1e-3  # không kéo dài video


def test_plan_timing_cau_ngan_giu_nguyen_toc_do_va_moc():
	# Tình huống thật: câu Việt ngắn hơn khoảng trống.
	# Phần giả lập: 1s giọng trong 3s trống.
	# Đỏ thì hỏng gì: câu ngắn bị đọc chậm hoặc dời mốc vô cớ.
	plans = v0.plan_timing([{"start": 1.0, "tts_sec": 1.0}], video_sec=4.15, pause=0.15, max_speed=1.3)
	assert plans[0] == {"play_start": 1.0, "speed": 1.0, "play_dur": 1.0, "avail": 3.0, "ratio": 0.333, "delay": 0.0}


def test_suspicious_timing_bat_dung_moc_bi_don():
	# Tình huống thật: test_lap7 2026-10-29, aligner gán câu 16 chữ vào 0.8s (20 chữ/giây); nghe như
	# "người kể nói nhanh" nhưng thật ra là mốc sai, kéo theo cả đoạn sau lệch hình.
	# Phần giả lập: đúng câu đó với mốc sai, cùng câu với mốc đúng (4.16s), và một câu 2 chữ rất ngắn.
	# Đỏ thì hỏng gì: lỗi mốc thời gian lọt qua im lặng, video dài lệch hình mà không ai biết vì sao.
	src = "上有养育之恩未还，下有抚养之责要尽。"
	segs = [
		{"src": src, "start": 117.44, "end": 118.24},
		{"src": src, "start": 18.88, "end": 23.04},
		{"src": "嗯嗯。", "start": 5.0, "end": 5.2},
	]
	assert v0.suspicious_timing(segs) == [(0, 20.0)]

class _FakeVieneu:
	"""VieNeu giả: ghi lại cách được gọi; mỗi câu trả về số mẫu = 100 x số ký tự (để kiểm thứ tự câu)."""
	calls = []
	sample_rate = 1000

	def __init__(self, **kw):
		_FakeVieneu.calls.append(("init", kw))

	def infer(self, text, voice):
		_FakeVieneu.calls.append(("infer", text))
		return [0.0] * (100 * len(text))

	def infer_batch(self, texts, voice, batch_size):
		_FakeVieneu.calls.append(("batch", list(texts), batch_size))
		return [[0.0] * (100 * len(t)) for t in texts]


def _tts_project(tmp_path, monkeypatch, texts):
	import json
	import sys
	import types

	monkeypatch.setitem(sys.modules, "vieneu", types.SimpleNamespace(Vieneu=_FakeVieneu))
	_FakeVieneu.calls = []
	(tmp_path / "segments.json").write_text(json.dumps({"segments": [{"src": "x", "tgt": t} for t in texts]}), encoding="utf-8")
	return tmp_path


def test_tts_gpu_doc_theo_lo_va_giu_dung_thu_tu(tmp_path, monkeypatch, capsys):
	# Tình huống thật: TTS chuyển sang GPU đọc theo lô 16 (đo 2026-10-30: 24 câu 1.8s so với CPU 51.5s, ~14 nhân).
	# Phần giả lập: VieNeu giả, 5 câu dài khác nhau, lô 2 -> 3 lần gọi infer_batch.
	# Đỏ thì hỏng gì: câu bị đọc lệch thứ tự (giọng câu này đặt vào chỗ câu khác), hoặc web mất tiến độ từng câu.
	import argparse
	import json

	p = _tts_project(tmp_path, monkeypatch, ["a", "bb", "ccc", "dddd", "eeeee"])
	v0.cmd_tts(argparse.Namespace(project=p, voice="V", precision="int8", device="cuda", batch=2))
	assert _FakeVieneu.calls[0] == ("init", {"mode": "v3turbo", "backend": "pytorch", "device": "cuda", "max_batch_size": 2})
	assert [c[1] for c in _FakeVieneu.calls[1:]] == [["a", "bb"], ["ccc", "dddd"], ["eeeee"]]
	doc = json.loads((p / "segments.json").read_text(encoding="utf-8"))
	assert [s["tts_sec"] for s in doc["segments"]] == [0.1, 0.2, 0.3, 0.4, 0.5]
	assert doc["tts"]["engine"] == "VieNeu v3 Turbo PyTorch CUDA, lô 2"
	out = capsys.readouterr().out
	assert [l.split(":")[0] for l in out.splitlines() if l.startswith("[tts] ") and ":" in l.split()[1]] == \
		["[tts] 0", "[tts] 1", "[tts] 2", "[tts] 3", "[tts] 4"]


def test_tts_cpu_van_nhu_cu(tmp_path, monkeypatch):
	# Tình huống thật: quay lại CPU (TTS_DEVICE=cpu trong tools/dub.sh) hoặc máy không có GPU.
	# Phần giả lập: VieNeu giả, device=cpu.
	# Đỏ thì hỏng gì: đường CPU đã chạy ổn bị đổi hành vi.
	import argparse

	p = _tts_project(tmp_path, monkeypatch, ["a", "bb"])
	v0.cmd_tts(argparse.Namespace(project=p, voice="V", precision="int8", device="cpu", batch=16))
	assert _FakeVieneu.calls == [("init", {"mode": "v3turbo", "backend": "onnx", "precision": "int8"}), ("infer", "a"), ("infer", "bb")]
