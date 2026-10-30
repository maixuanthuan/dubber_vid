"""Thay symlink trong cache Hugging Face bằng file thật, cho các repo model ONNX có external data.

Vì sao (2026-10-29, máy Thuận, env dub_tts):
  huggingface_hub 2.x lưu file dạng blobs/<2 ký tự>/<hash>, thư mục snapshots/ chỉ còn symlink.
  File .onnx và file .data chứa trọng số của nó rơi vào HAI thư mục khác nhau. onnxruntime 1.31.1
  kiểm external data phải nằm cùng thư mục thật với .onnx, nên từ chối mở:
  "External data path escapes model directory".
  Thay symlink bằng file thật thì cả hai nằm chung thư mục snapshot, onnxruntime mở được.

Dùng:  conda activate dub_tts
       python tools/hf_materialize.py            # chỉ in ra sẽ làm gì, không ghi
       python tools/hf_materialize.py --apply    # thay thật
Đảo ngược: xoá thư mục models--<repo> trong cache; lần chạy sau thư viện tự tải lại.
Tốn thêm ổ bằng tổng dung lượng file được sao (blob gốc vẫn giữ).
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

# Các repo VieNeu v3 Turbo dùng trên đường ONNX/CPU (vieneu 3.8.3).
REPOS = ("pnnbao-ump/VieNeu-TTS-v3-Turbo", "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX")


def hub_cache():
	if os.environ.get("HF_HUB_CACHE"):
		return Path(os.environ["HF_HUB_CACHE"])
	return Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"


def find_links(repo_dir):
	"""Mọi symlink file trong snapshots/ của một repo: list (đường dẫn link, file thật)."""
	links = []
	for p in sorted((repo_dir / "snapshots").rglob("*")):
		if p.is_symlink():
			real = p.resolve()
			if not real.is_file():
				raise FileNotFoundError(f"symlink gãy: {p} -> {real}")
			links.append((p, real))
	return links


def materialize(link, real):
	"""Thay link bằng bản sao thật. Ghi ra file tạm rồi os.replace: hỏng giữa chừng thì link cũ còn nguyên."""
	tmp = link.with_name(link.name + ".tmp_materialize")
	shutil.copy2(real, tmp)
	os.replace(tmp, link)


def main():
	p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
	p.add_argument("--apply", action="store_true", help="thay thật; không có cờ này thì chỉ in ra")
	p.add_argument("--repos", nargs="+", default=list(REPOS))
	a = p.parse_args()

	cache = hub_cache()
	total_files, total_bytes, missing = 0, 0, []
	for repo in a.repos:
		repo_dir = cache / ("models--" + repo.replace("/", "--"))
		if not repo_dir.is_dir():
			missing.append(repo)
			continue
		links = find_links(repo_dir)
		size = sum(r.stat().st_size for _, r in links)
		print(f"{repo}: {len(links)} symlink, {size / 2**20:.1f} MiB")
		for link, real in links:
			print(f"  {'thay' if a.apply else 'sẽ thay'}: {link.relative_to(repo_dir)}")
			if a.apply:
				materialize(link, real)
		total_files += len(links)
		total_bytes += size

	if missing:
		print(f"LỖI: chưa có trong cache {cache}: {missing}. Chạy TTS một lần để thư viện tải về trước.")
		return 1
	verb = "Đã thay" if a.apply else "Sẽ thay"
	print(f"{verb} {total_files} file, tốn thêm {total_bytes / 2**20:.1f} MiB ổ.")
	if not a.apply:
		print("Chưa ghi gì. Chạy lại với --apply để thay thật.")
	return 0


if __name__ == "__main__":
	sys.exit(main())