"""Kiểm key và model của một config dịch trước khi chạy cả video.

Bước này trả lời câu hỏi: key có dùng được không, nhà cung cấp đang có những model nào, model
ghi trong config có tồn tại không, và gửi thử một câu thì dịch ra sao.
Tốn đúng 1 request (bước dịch thử); --no-translate thì chỉ liệt kê model.

Dùng:  conda activate dub_tts
       python tools/llm_check.py configs/translator_gemini.json
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dubber import translate as tr  # noqa: E402

SAMPLE = "三更灯火五更鸡，正是男儿读书时。"


def main():
	p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
	p.add_argument("config", type=Path)
	p.add_argument("--filter", default="", help="chỉ in model có chứa chuỗi này, vd: flash")
	p.add_argument("--no-translate", action="store_true")
	a = p.parse_args()

	tr.load_dotenv(ROOT / ".env")
	try:
		cfg = tr.load_config(a.config)
		client = tr.make_client(cfg)
		ids = sorted(m.id for m in client.models.list())
	except Exception as exc:
		sys.exit(f"LỖI: {type(exc).__name__}: {exc}")

	# Gemini trả id dạng "models/gemini-..."; so cả hai dạng.
	names = {i.split("/", 1)[-1] for i in ids}
	shown = [i for i in ids if a.filter in i]
	print(f"{cfg['name']}: {len(ids)} model, in {len(shown)} model có '{a.filter}':")
	for i in shown:
		print(f"  {i}")
	if cfg["model"] not in names:
		sys.exit(f"LỖI: model trong config ({cfg['model']}) không có trong danh sách. Sửa mục \"model\" trong {a.config}.")
	print(f"Model trong config có tồn tại: {cfg['model']}")

	if a.no_translate:
		return 0
	t = tr.Translator(cfg, client, src_lang="zh")
	try:
		out = t.batch([SAMPLE], context=[])
	except tr.TranslateError as exc:
		sys.exit(f"LỖI khi dịch thử: {exc}")
	print(f"Dịch thử: {SAMPLE}\n       -> {out[0]}")
	print(f"Token vào {t.prompt_tokens} / ra {t.completion_tokens}, {t.requests} request")
	return 0


if __name__ == "__main__":
	sys.exit(main())