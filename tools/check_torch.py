"""Smoke test: bản torch đang cài có CHẠY THẬT được trên GPU + driver của máy này không.

Bước này trả lời câu hỏi: với driver 535 (CUDA tối đa 12.2), bản torch build cho CUDA mới hơn
(vd cu126) có chạy được nhờ "minor version compatibility" không. Tài liệu CUDA nói được,
nhưng chưa đo trên máy thật thì chưa tin.

Vì sao không dừng ở torch.cuda.is_available(): hàm đó có thể trả True mà lúc chạy kernel vẫn
lỗi ("no kernel image is available", lỗi cuBLAS/cuDNN). Nên ở đây chạy thật các phép tính mà
stack ASR sẽ dùng, rồi so với kết quả CPU.

Dùng:  conda activate dub_asr && python tools/check_torch.py
Thoát 0 = đạt hết, 1 = có mục hỏng (in đủ mọi lý do, không dừng ở lỗi đầu tiên).
Chỉ đọc: không ghi file nào.
"""
import sys
import time

# Ngưỡng sai số tương đối so với CPU fp32. Ngưỡng rộng có chủ ý: chỉ để bắt kết quả rác
# (NaN, sai hẳn do kernel hỏng), không phải để đo độ chính xác số học.
TOL = {"fp32": 1e-3, "fp16": 5e-3, "bf16": 3e-2}


def rel_err(got, ref):
	return ((got.float().cpu() - ref).norm() / ref.norm()).item()


def main():
	try:
		import torch
		import torch.nn.functional as F
	except ImportError:
		print("HỎNG: chưa cài torch trong env này. Kiểm lại đã `conda activate dub_asr` chưa.")
		return 1

	print(f"torch          : {torch.__version__}")
	print(f"build CUDA     : {torch.version.cuda}")
	print(f"cuDNN          : {torch.backends.cudnn.version()}")

	if not torch.cuda.is_available():
		print("HỎNG: torch.cuda.is_available() = False. Bản torch này không nói chuyện được với driver.")
		return 1

	dev = torch.device("cuda:0")
	props = torch.cuda.get_device_properties(dev)
	print(f"GPU            : {props.name}, sm{props.major}{props.minor}, {props.total_memory / 2**30:.2f} GiB")
	print(f"bf16 hỗ trợ    : {torch.cuda.is_bf16_supported()}")
	print()

	torch.manual_seed(0)
	a = torch.randn(1024, 1024)
	b = torch.randn(1024, 1024)
	x = torch.randn(4, 64, 128, 128)
	w = torch.randn(64, 64, 3, 3)
	q = torch.randn(2, 8, 256, 64)
	k = torch.randn(2, 8, 256, 64)
	v = torch.randn(2, 8, 256, 64)

	ref_mm = a @ b
	ref_conv = F.conv2d(x, w, padding=1)
	ref_attn = F.scaled_dot_product_attention(q, k, v)

	dtypes = {"fp32": torch.float32, "fp16": torch.float16, "bf16": torch.bfloat16}
	checks = []
	for name, dt in dtypes.items():
		checks.append((f"matmul {name}", name, lambda dt=dt: a.to(dev, dt) @ b.to(dev, dt), ref_mm))
	# conv2d đi qua cuDNN; attention (SDPA) là thứ transformers dùng cho model ASR.
	checks.append(("conv2d fp16 (cuDNN)", "fp16", lambda: F.conv2d(x.to(dev, torch.float16), w.to(dev, torch.float16), padding=1), ref_conv))
	checks.append(("attention bf16 (SDPA)", "bf16", lambda: F.scaled_dot_product_attention(q.to(dev, torch.bfloat16), k.to(dev, torch.bfloat16), v.to(dev, torch.bfloat16)), ref_attn))

	failures = []
	for label, tol_key, fn, ref in checks:
		try:
			torch.cuda.synchronize()
			t0 = time.perf_counter()
			out = fn()
			torch.cuda.synchronize()
			ms = (time.perf_counter() - t0) * 1000
			err = rel_err(out, ref)
			ok = err == err and err < TOL[tol_key]  # err == err loại NaN
			status = "ĐẠT" if ok else "HỎNG"
			print(f"{status:4} {label:24} sai số {err:.2e} (ngưỡng {TOL[tol_key]:.0e}), {ms:7.1f} ms")
			if not ok:
				failures.append(f"{label}: sai số {err:.2e} > ngưỡng {TOL[tol_key]:.0e}")
		except Exception as exc:
			print(f"HỎNG {label:24} {type(exc).__name__}: {exc}")
			failures.append(f"{label}: {type(exc).__name__}: {exc}")

	print()
	print(f"VRAM đỉnh      : {torch.cuda.max_memory_allocated(dev) / 2**20:.0f} MiB")
	if failures:
		print(f"KẾT LUẬN: HỎNG {len(failures)}/{len(checks)} mục:")
		for f in failures:
			print(f"  - {f}")
		return 1
	print(f"KẾT LUẬN: ĐẠT {len(checks)}/{len(checks)} mục. Bản torch này dùng được trên máy này.")
	return 0


if __name__ == "__main__":
	sys.exit(main())