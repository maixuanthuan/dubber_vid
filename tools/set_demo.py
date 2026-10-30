"""Chọn một lần lồng tiếng đã xong làm ví dụ cho trang giới thiệu.

Chép video gốc, video tiếng Việt và lời thoại song ngữ (chỉ các trường trang cần: câu gốc, câu dịch,
mốc thời gian) vào data/demo/. Không chép đường dẫn hay thông số nội bộ ra trang công khai.
Ghi thêm meta.json (danh sách giọng, thời lượng, số câu): trang giới thiệu là trang tĩnh, không gọi
API của server (Thuận chốt 2026-10-30), nên mọi con số trang hiện phải nằm sẵn trong data/demo/.

Dùng:  conda activate dub_tts
       python tools/set_demo.py --video data/samples/vidtiengtrung1.mp4 --project data/projects/vidtiengtrung1__translator_gemini_flash
Lưu ý bản quyền: khi đưa web ra ngoài, video ví dụ phải là nội dung được phép dùng công khai.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO_SCHEMA = "demo/v1"


def build_demo(doc, voices):
	"""segments.json của project đã ghép xong -> (câu cho trang, meta cho trang). Chỉ các trường trang cần."""
	plans = doc.get("mix", {}).get("plans", [])
	if len(plans) != len(doc["segments"]):
		raise ValueError("project chưa chạy xong bước ghép (thiếu mốc phát của bản tiếng Việt)")
	segs = [{"start": s["start"], "end": s["end"], "src": s["src"], "tgt": s["tgt"],
		"play_start": pl["play_start"], "play_end": round(pl["play_start"] + pl["play_dur"], 3)}
		for s, pl in zip(doc["segments"], plans)]
	# Hai con số trang công khai đưa ra làm bằng chứng, nên phải tính từ chính mốc phát, không gõ tay:
	# câu Việt bắt đầu muộn nhất bao nhiêu so với câu gốc, và có câu nào đè lên câu trước không.
	max_delay = round(max((s["play_start"] - s["start"] for s in segs), default=0.0), 3)
	overlaps = sum(1 for a, b in zip(segs, segs[1:]) if b["play_start"] < a["play_end"] - 1e-6)
	meta = {"schema": DEMO_SCHEMA, "src_lang": doc["src_lang"], "voice": doc["tts"]["voice"],
		"video_sec": doc["mix"]["video_sec"], "out_sec": doc["mix"]["out_sec"], "sentences": len(segs),
		"max_delay": max_delay, "overlaps": overlaps, "voices": [{"name": v["name"], "desc": v["desc"]} for v in voices]}
	return segs, meta


def main():
	p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
	p.add_argument("--video", type=Path, required=True)
	p.add_argument("--project", type=Path, required=True)
	a = p.parse_args()

	seg_file = a.project / "segments.json"
	vi = sorted(a.project.glob("*_vi.mp4"))
	errors = [f"không thấy {x}" for x in (a.video, seg_file) if not x.is_file()]
	if not vi:
		errors.append(f"không thấy video tiếng Việt *_vi.mp4 trong {a.project}")
	if errors:
		sys.exit("LỖI: " + "; ".join(errors))
	sys.path.insert(0, str(ROOT))
	from web.app import load_voices  # cùng nguồn giọng với trang làm việc: đọc file assets của vieneu

	try:
		segs, meta = build_demo(json.loads(seg_file.read_text(encoding="utf-8")), load_voices())
	except (ValueError, RuntimeError) as exc:
		sys.exit(f"LỖI: {exc}")

	out = ROOT / "data" / "demo"
	out.mkdir(parents=True, exist_ok=True)
	shutil.copy2(a.video, out / "before.mp4")
	shutil.copy2(vi[0], out / "after.mp4")
	(out / "segments.json").write_text(json.dumps({"segments": segs}, ensure_ascii=False), encoding="utf-8")
	(out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
	print(f"Đã đặt ví dụ: {len(segs)} câu, {len(meta['voices'])} giọng -> {out}")


if __name__ == "__main__":
	main()