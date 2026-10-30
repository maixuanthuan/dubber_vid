"""Test tools/hf_materialize.py trên một cache giả dựng trong thư mục tạm của pytest."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("hf_materialize", Path(__file__).resolve().parent.parent / "tools" / "hf_materialize.py")
hm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hm)


def _fake_repo(root):
	"""Cache kiểu huggingface_hub 2.x: blob ở hai thư mục con khác nhau, snapshot chỉ có symlink."""
	(root / "blobs" / "d1").mkdir(parents=True)
	(root / "blobs" / "45").mkdir(parents=True)
	(root / "blobs" / "d1" / "aaa").write_bytes(b"graph")
	(root / "blobs" / "45" / "bbb").write_bytes(b"weights")
	snap = root / "repo" / "snapshots" / "rev1" / "onnx_int8"
	snap.mkdir(parents=True)
	(snap / "m.onnx").symlink_to(root / "blobs" / "d1" / "aaa")
	(snap / "m.data").symlink_to(root / "blobs" / "45" / "bbb")
	return root / "repo", snap


def test_materialize_dat_onnx_va_data_chung_thu_muc_that(tmp_path):
	# Tình huống thật: onnxruntime đòi file .data nằm cùng thư mục thật với .onnx.
	# Phần giả lập: .onnx và .data là symlink trỏ vào hai thư mục blob khác nhau.
	# Đỏ thì hỏng gì: VieNeu vẫn không mở được model, TTS chết ngay lúc khởi động.
	repo, snap = _fake_repo(tmp_path)
	links = hm.find_links(repo)
	assert len(links) == 2
	assert links[0][1].parent != links[1][1].parent  # đúng là hai thư mục khác nhau
	for link, real in links:
		hm.materialize(link, real)
	for name, content in (("m.onnx", b"graph"), ("m.data", b"weights")):
		f = snap / name
		assert not f.is_symlink()
		assert f.read_bytes() == content
		assert f.resolve().parent == snap.resolve()
	assert hm.find_links(repo) == []  # chạy lại không còn gì để thay
	assert (tmp_path / "blobs" / "d1" / "aaa").read_bytes() == b"graph"  # blob gốc giữ nguyên


def test_find_links_bao_symlink_gay(tmp_path):
	# Tình huống thật: cache bị xoá dở, symlink trỏ vào blob không còn.
	# Phần giả lập: một symlink trỏ vào file không tồn tại.
	# Đỏ thì hỏng gì: tool âm thầm bỏ qua, Thuận tưởng đã sửa xong.
	snap = tmp_path / "repo" / "snapshots" / "rev1"
	snap.mkdir(parents=True)
	(snap / "m.onnx").symlink_to(tmp_path / "khong_co")
	with pytest.raises(FileNotFoundError, match="gãy"):
		hm.find_links(tmp_path / "repo")