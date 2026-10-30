"""Test tools/bench_tts.py: phần chọn câu và tính số đo (hàm thuần, không cần VieNeu hay GPU)."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("bench_tts", Path(__file__).resolve().parent.parent / "tools" / "bench_tts.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def test_chon_cau_that_theo_thu_tu_bo_cau_trong():
	# Tình huống thật: đo TTS bằng chính các câu pipeline sẽ đọc, lấy từ segments.json của một project đã dịch.
	# Phần giả lập: 4 câu, có câu chưa dịch và câu chỉ có khoảng trắng.
	# Đỏ thì hỏng gì: đo trên câu rỗng (TTS chạy 0 giây), số đo đẹp giả.
	segs = [{"tgt": "Một."}, {"tgt": ""}, {"tgt": "  "}, {}, {"tgt": "Hai."}, {"tgt": "Ba."}]
	assert bench.pick_texts(segs, 2) == ["Một.", "Hai."]


def test_rtf_va_so_nhan_cpu_tinh_dung():
	# Tình huống thật: quyết định chạy TTS ở CPU hay GPU dựa vào RTF và số nhân CPU trung bình.
	# Phần giả lập: đọc 30s giọng mất 15s, tốn 90s CPU (mọi luồng cộng lại) -> RTF 0.5, trung bình 6 nhân.
	# Đỏ thì hỏng gì: bảng so sánh sai, chọn nhầm chỗ chạy TTS.
	r = bench.summarize({"mode": "cpu", "batch": 1, "load_sec": 3, "synth_sec": 15.0, "cpu_sec": 90.0, "audio_sec": 30.0})
	assert r["rtf"] == 0.5 and r["cpu_cores"] == 6.0
	table = bench.report_table([r, {"mode": "gpu", "batch": 8, "load_sec": 9, "synth_sec": 5.0, "cpu_sec": 5.0,
		"audio_sec": 30.0, "vram_mib": 2100}])
	assert "| gpu | 8 |" in table and "2100" in table and "| cpu | 1 |" in table
