"""Test phần quyết định của dubber/asr.py: cắt đoạn và nối kết quả, không cần GPU hay model."""
import numpy as np

from dubber import asr


def _rms_with_quiet(total_sec, quiet_at, frame=asr.FRAME_SEC):
	"""Âm lượng giả: to đều, riêng các khoảng 0.5s quanh `quiet_at` thì gần như im lặng."""
	rms = np.ones(int(total_sec / frame))
	for q in quiet_at:
		rms[int((q - 0.25) / frame): int((q + 0.25) / frame)] = 0.01
	return rms


def test_choose_cuts_audio_ngan_thi_khong_cat():
	# Tình huống thật: clip TikTok 30s, phần lớn video của Thuận.
	# Phần giả lập: 100s audio, giới hạn 150s.
	# Đỏ thì hỏng gì: clip ngắn bị cắt vô cớ, sinh chỗ nối có thể làm vỡ câu.
	assert asr.choose_cuts(_rms_with_quiet(100, []), 100.0, 150.0) == []


def test_choose_cuts_cat_dung_cho_im_lang_va_moi_doan_duoi_gioi_han():
	# Tình huống thật: video 5 phút, người kể ngừng ở giây 140 và giây 270.
	# Phần giả lập: âm lượng to đều trừ 2 chỗ lặng đó.
	# Đỏ thì hỏng gì: cắt ngang giữa câu nói, ASR nghe sai hai chữ ở chỗ nối; hoặc đoạn > 180s làm aligner hỏng.
	cuts = asr.choose_cuts(_rms_with_quiet(300, [140, 270]), 300.0, 150.0)
	assert [round(c) for c in cuts] == [140, 270]
	bounds = list(zip([0.0] + cuts, cuts + [300.0]))
	assert all(b - a <= 150.0 for a, b in bounds)


def test_choose_cuts_khong_co_cho_lang_van_ton_trong_gioi_han():
	# Tình huống thật: nhạc nền to liên tục, không có chỗ nào im.
	# Phần giả lập: âm lượng phẳng tuyệt đối.
	# Đỏ thì hỏng gì: vòng lặp không dừng, hoặc ra đoạn dài hơn giới hạn của aligner.
	cuts = asr.choose_cuts(np.ones(int(400 / asr.FRAME_SEC)), 400.0, 150.0)
	bounds = list(zip([0.0] + cuts, cuts + [400.0]))
	assert len(cuts) >= 2 and all(0 < b - a <= 150.0 for a, b in bounds)


def test_merge_chunks_cong_bu_moc_thoi_gian():
	# Tình huống thật: đoạn 2 bắt đầu ở giây 140; aligner trả mốc tính từ đầu đoạn 2.
	# Phần giả lập: 2 đoạn, mỗi đoạn 1 chữ ở giây 1.0 của đoạn.
	# Đỏ thì hỏng gì: mọi câu của đoạn 2 trở đi phát lệch hình đúng 140 giây.
	text, units = asr.merge_chunks([
		(0.0, "我。", [{"start": 1.0, "end": 1.2, "text": "我"}]),
		(140.0, "你。", [{"start": 1.0, "end": 1.2, "text": "你"}]),
	])
	assert text == "我。你。"
	assert [(u["text"], u["start"]) for u in units] == [("我", 1.0), ("你", 141.0)]


def test_choose_cuts_doan_ngan_khong_bi_cat_qua_vun():
	# Tình huống thật: max_chunk 30s (đo 2026-10-29: đoạn 147s làm aligner gán sai mốc, 30s thì đúng).
	# Phần giả lập: 100s âm lượng phẳng, không chỗ lặng nào.
	# Đỏ thì hỏng gì: vùng tìm điểm cắt 30s bằng cả đoạn, có thể cắt ra đoạn 1s, gọi ASR vô ích hàng loạt.
	cuts = asr.choose_cuts(np.ones(int(100 / asr.FRAME_SEC)), 100.0, 30.0)
	bounds = list(zip([0.0] + cuts, cuts + [100.0]))
	assert all(b - a <= 30.0 for a, b in bounds)
	assert all(b - a >= 20.0 - asr.FRAME_SEC for a, b in bounds[:-1])