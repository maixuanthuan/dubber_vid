"""Soi mốc thời gian trong kết quả ASR: có câu nào bị gán mốc vô lý không.

Bước này trả lời câu hỏi: với một độ dài đoạn (--max-chunk-sec) cho trước, aligner có gán sai mốc
không, và sai ở đâu. Dùng để chọn độ dài đoạn bằng số đo. Chỉ đọc file JSON, không gọi model.

Dùng:  python tools/check_asr_times.py data/asr/test_lap7__c30.json [data/asr/test_lap7__c60.json ...]
       --period 29.4  (chỉ cho video ghép lặp: in thêm độ lệch so với mốc mong đợi)
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dubber import v0  # noqa: E402


def check(path, period=None):
	d = json.loads(Path(path).read_text(encoding="utf-8"))
	sents = v0.split_sentences(d["text"], d["units"])
	cps = [len(v0.norm(s["src"])) / max(1e-6, s["end"] - s["start"]) for s in sents]
	bad = v0.suspicious_timing(sents)
	chunks = ", ".join(f"{c['start']:.1f}–{c['end']:.1f}" for c in d.get("chunks", []))
	print(f"== {path}")
	print(f"   đoạn ({d['params'].get('max_chunk_sec')}s): {chunks}")
	print(f"   {len(sents)} câu, chữ/giây trung vị {statistics.median(cps):.1f}, lớn nhất {max(cps):.1f}; "
		f"VRAM torch đỉnh {d.get('torch_peak_mib')} MiB; ASR {d.get('asr_sec')}s + aligner {d.get('align_sec')}s")
	print(f"   mốc nghi sai: {len(bad)} câu" + (": " + ", ".join(f"#{i} ({c}/s)" for i, c in bad) if bad else ""))
	if period:
		# Video ghép lặp: câu thứ i của mỗi lần lặp phải bắt đầu cách câu thứ i của lần đầu đúng k*period.
		per = next((k for k in range(1, len(sents)) if sents[k]["src"] == sents[0]["src"]), None)
		if per:
			worst = max(abs(s["start"] - (sents[i % per]["start"] + (i // per) * period)) for i, s in enumerate(sents))
			print(f"   lệch lớn nhất so với mốc mong đợi (chu kỳ {period}s, {per} câu/lần lặp): {worst:.2f}s")
	return len(bad)


def main():
	p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
	p.add_argument("files", nargs="+", type=Path)
	p.add_argument("--period", type=float, help="chu kỳ lặp (giây) nếu là video ghép lặp")
	a = p.parse_args()
	return 1 if sum(check(f, a.period) for f in a.files) else 0


if __name__ == "__main__":
	sys.exit(main())