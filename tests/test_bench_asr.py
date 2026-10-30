"""Test phần quyết định của tools/bench_asr.py: chạy vài giây, không cần GPU hay model."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("bench_asr", Path(__file__).resolve().parent.parent / "tools" / "bench_asr.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def test_normalize_bo_dau_cau_giu_chu_so():
	# Tình huống thật: Qwen trả "我明天去北京。", Whisper trả "我明天去北京" (thiếu dấu chấm).
	# Phần giả lập: hai chuỗi chỉ khác dấu câu và khoảng trắng.
	# Đỏ thì hỏng gì: báo cáo đếm khác biệt dấu câu thành "nghe khác", độ giống nhau bị kéo xuống oan.
	assert bench.normalize("我明天去北京。") == bench.normalize("我 明天，去北京")
	assert bench.normalize("第3集！") == "第3集"


def test_diff_spans_chi_ra_dung_cho_lech():
	# Tình huống thật: một model nghe 北京 (Bắc Kinh), model kia nghe 背景 (bối cảnh).
	# Phần giả lập: hai chuỗi giống nhau trừ đúng hai ký tự đó.
	# Đỏ thì hỏng gì: báo cáo chỉ sai vị trí đoạn đáng nghi, Thuận đi soi nhầm chỗ.
	spans = bench.diff_spans("我明天去北京", "我明天去背景")
	assert spans == [(4, "北京", "背景")]


def test_diff_spans_hai_chuoi_giong_nhau_thi_rong():
	# Tình huống thật: hai model nghe y hệt nhau.
	# Phần giả lập: hai chuỗi bằng nhau.
	# Đỏ thì hỏng gì: báo cáo bịa ra đoạn lệch không có thật.
	assert bench.diff_spans("我明天去北京", "我明天去北京") == []
	assert bench.similarity("abc", "abc") == 1.0
	assert bench.similarity("", "") == 1.0


def test_time_at_khop_ky_tu_voi_timestamp():
	# Tình huống thật: aligner của Qwen trả mỗi chữ Hán một mốc thời gian, không có dấu câu.
	# Phần giả lập: 3 đơn vị, đơn vị giữa có hai chữ.
	# Đỏ thì hỏng gì: báo cáo ghi sai giây của đoạn lệch, Thuận mở video tới sai chỗ để nghe.
	units = [{"start": 0.0, "text": "我"}, {"start": 0.5, "text": "明天"}, {"start": 1.2, "text": "去"}]
	assert bench.time_at(units, 0) == 0.0
	assert bench.time_at(units, 2) == 0.5
	assert bench.time_at(units, 3) == 1.2
	assert bench.time_at(units, 9) is None  # ngoài phạm vi: không đoán bừa


def test_build_report_co_bang_lech_khi_du_hai_model():
	# Tình huống thật: chạy xong cả hai model, cần một báo cáo đọc được.
	# Phần giả lập: hai kết quả giả, khác nhau đúng một đoạn.
	# Đỏ thì hỏng gì: báo cáo thiếu phần so sánh, là phần trả lời câu hỏi chính của bước đo.
	base = {"rtf": 0.1, "load_sec": 5.0, "infer_sec": 3.0, "vram_baseline_mib": 150, "vram_peak_mib": 4150, "segments": []}
	results = {
		"qwen3": {**base, "model": "Q", "text": "我明天去北京。", "units": [{"start": float(i), "text": c} for i, c in enumerate("我明天去北京")]},
		"whisper": {**base, "model": "W", "text": "我明天去背景", "units": []},
	}
	report = bench.build_report("v.mp4", results)
	assert "| 4.00s | 北京 | 背景 |" in report
	assert "1 đoạn lệch" in report
	assert "4000 (nền 150)" in report