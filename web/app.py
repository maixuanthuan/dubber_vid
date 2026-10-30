"""Web chạy trên máy: upload video -> lồng tiếng -> xem/tải kết quả (env dub_tts).

Web KHÔNG có pipeline riêng: mỗi job chạy đúng lệnh `bash tools/dub.sh ...` như ở terminal, đọc
log của nó để hiện tiến độ. Một đường chạy duy nhất, không có hai bản code lệch nhau.

Một job một lúc (GPU 6 GB không chạy song song được), job sau xếp hàng. Trạng thái và log của
từng job ghi ra data/jobs/<id>/, tắt web mở lại vẫn còn; job đang chạy dở lúc tắt thì đánh dấu
"interrupted" chứ không im lặng biến mất.

Chạy: bash tools/web.sh          (chỉ máy này)
      bash tools/web.sh share    (chia sẻ qua Cloudflare Tunnel: giới hạn video, chỉ Flash-Lite, mã mời tuỳ chọn)
"""
import argparse
import collections
import hashlib
import hmac
import importlib.util
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_file, session
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent.parent
LANGS = {"zh": "Tiếng Trung", "ko": "Tiếng Hàn"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
DEFAULT_VOICE = "Thiện Minh"  # trùng VOICE_DEFAULT trong tools/dub.sh
ACTIVE = ("queued", "running")
# Ví dụ trên trang giới thiệu: chép vào data/demo bằng tools/set_demo.py. Chỉ phục vụ đúng các tên này.
DEMO_FILES = ("before.mp4", "after.mp4", "segments.json")
# Phụ: giọng, thời lượng (set_demo.py từ 2026-10-30). Thiếu thì trang vẫn chạy, nên không tính vào "available".
DEMO_EXTRA = ("meta.json",)


# ── Phần quyết định: hàm thuần ──────────────────────────────────────────────

# (stage, % bắt đầu, % kết thúc). Ước lượng thô theo số đo vidtrung10phut 2026-10-29
# (308s: ASR ~45s, dịch ~20s, TTS ~205s, ghép ~10s), chỉ để thanh tiến độ không đứng im.
STAGES = [("asr", 0, 20), ("segment", 20, 22), ("translate", 22, 30), ("tts", 30, 95), ("mix", 95, 100)]


def parse_progress(lines):
	"""Từ log của dub.sh -> {stage, percent, result, project, total, spoken}. Dựa vào các dòng
	[asr]/[tts]... mà stage in ra. spoken: số câu đã đọc xong (để tô câu đang đọc trên web)."""
	stage, frac, total, result, project, spoken, audio_sec = None, 0.0, None, None, None, 0, None
	for line in lines:
		m = re.match(r"\[(asr|segment|translate|tts|mix|dub)\]\s*(.*)", line)
		if not m:
			continue
		tag, rest = m.groups()
		if tag == "dub":
			x = re.search(r"XONG sau \d+s: (\S+)", rest)
			if x:
				result = x.group(1)
			x = re.search(r" -> (data/projects/\S+)$", rest)
			if x:
				project = x.group(1)
			continue
		if tag != stage:
			stage, frac = tag, 0.0
		if tag == "asr":
			a = re.match(r"audio (\d+(?:\.\d+)?)s", rest)
			if a:
				audio_sec = float(a.group(1))
			x = re.search(r"(nhận dạng|gán thời gian) đoạn (\d+)/(\d+)", rest)
			if x:
				i, n = int(x.group(2)), int(x.group(3))
				frac = (i / n) * (0.7 if x.group(1) == "nhận dạng" else 0.3) + (0.7 if x.group(1) != "nhận dạng" else 0)
		elif tag == "segment":
			x = re.search(r"(\d+) câu ->", rest)
			if x:
				total = int(x.group(1))
		elif tag == "translate":
			x = re.search(r"câu \d+–(\d+) xong", rest)
			if x and total:
				frac = (int(x.group(1)) + 1) / total
		elif tag == "tts":
			x = re.match(r"(\d+):", rest)
			if x:
				spoken = int(x.group(1)) + 1
				if total:
					frac = spoken / total
	extra = {"project": project, "total": total, "spoken": spoken, "audio_sec": audio_sec}
	if result:
		return {"stage": "done", "percent": 100, "result": result, **extra}
	if stage is None:
		return {"stage": "starting", "percent": 0, "result": None, **extra}
	lo, hi = next((a, b) for s, a, b in STAGES if s == stage)
	return {"stage": stage, "percent": round(lo + (hi - lo) * min(frac, 1.0)), "result": None, **extra}


def speed_ratio(jobs):
	"""Trung vị (thời gian xử lý / thời lượng video) của các job đã xong trên máy này -> (tỉ lệ, số job).
	Trung vị chứ không trung bình: một lần chạy lạ (tải model lần đầu, máy bận) không kéo lệch ước tính.
	Chưa có job nào đủ số liệu thì (None, 0): không bịa ước tính."""
	ratios = sorted(j["elapsed_sec"] / j["audio_sec"] for j in jobs
		if j.get("status") == "done" and j.get("elapsed_sec") and j.get("audio_sec"))
	if not ratios:
		return None, 0
	mid = len(ratios) // 2
	med = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2
	return med, len(ratios)


def timing(job, progress, now, ratio, n):
	"""Thời gian cho giao diện: đã chạy bao lâu, video dài bao nhiêu, còn khoảng bao lâu (nếu ước được)."""
	audio = job.get("audio_sec") or progress.get("audio_sec")
	start, end = job.get("started_ts"), job.get("finished_ts")
	elapsed = (end or now) - start if start else None
	eta = None
	if job["status"] == "running" and ratio and audio and elapsed is not None:
		eta = max(0.0, ratio * audio - elapsed)
	return {"elapsed_sec": round(elapsed, 1) if elapsed is not None else None, "audio_sec": audio,
		"eta_sec": round(eta) if eta is not None else None, "eta_from": n if eta is not None else 0}


# Lỗi thô (log, mã thoát) chỉ ở máy chủ; người dùng thấy một câu nói rõ chuyện gì và làm gì tiếp.
# Thứ tự quan trọng: mẫu đầu tiên khớp thắng.
USER_ERRORS = [
	(("hết lượt", "RESOURCE_EXHAUSTED"), "Dịch vụ dịch đã hết lượt dùng. Thử lại sau, hoặc chọn cách dịch khác."),
	(("503", "overloaded", "quá tải", "timed out", "Timeout"), "Dịch vụ dịch đang quá tải. Thử lại sau ít phút."),
	(("chưa có key",), "Chưa cài khoá cho dịch vụ dịch. Cần người quản trị cài đặt trước."),
	(("out of memory", "CUDA error"), "Máy xử lý đang thiếu bộ nhớ. Thử lại khi máy rảnh hơn."),
	(("không thấy video", "Invalid data", "moov atom", "does not contain any stream"),
		"Không đọc được file video. Kiểm tra file mở được bình thường rồi gửi lại."),
]
USER_FALLBACK = "Lồng tiếng chưa thành công. Lỗi đã được ghi lại để kiểm tra."


def user_message(job):
	if job["status"] == "failed":
		raw = job.get("error") or ""
		return next((msg for keys, msg in USER_ERRORS if any(k in raw for k in keys)), USER_FALLBACK)
	if job["status"] == "interrupted":
		return "Lần chạy bị dừng giữa chừng. Hãy gửi lại video."
	if job["status"] == "cancelled":
		return "Đã huỷ lần chạy này."
	if job["status"] == "queued":
		return "Đang chờ video trước chạy xong."
	return None


def public_view(v):
	"""Chỉ những gì người dùng cần thấy. Không log, không lỗi thô, không đường dẫn trên máy chủ:
	trả về trình duyệt là ai mở DevTools cũng đọc được (Thuận chốt 2026-10-29)."""
	p = v["progress"]
	name = re.sub(r"^\d{8}_\d{6}_[0-9a-f]{4}__", "", Path(v["video"]).name)
	return {
		"id": v["id"], "status": v["status"], "name": name, "lang": v["lang"], "translator": v["translator"],
		"voice": v["voice"], "created": v["created"], "message": user_message(v), "has_result": bool(v.get("result")),
		"progress": {k: p.get(k) for k in ("stage", "percent", "total", "spoken")},
		**{k: v.get(k) for k in ("elapsed_sec", "audio_sec", "eta_sec", "eta_from")},
	}


def list_translators(root, only=None):
	"""only: đường dẫn một config duy nhất được dùng (chia sẻ, deploy); None = mọi config trong configs/."""
	out = []
	for p in sorted((root / "configs").glob("translator_*.json")):
		try:
			cfg = json.loads(p.read_text(encoding="utf-8"))
			if only is None or str(p.relative_to(root)) == only:
				out.append({"file": str(p.relative_to(root)), "name": cfg.get("name", p.stem), "model": cfg.get("model", "?")})
		except (OSError, json.JSONDecodeError):
			continue  # config hỏng thì không hiện; tools/dub.sh vẫn sẽ báo lỗi rõ nếu ai cố dùng
	return out


def load_voices():
	"""Danh sách giọng VieNeu, đọc thẳng file assets của gói (không nạp model)."""
	spec = importlib.util.find_spec("vieneu")
	if spec is None or not spec.submodule_search_locations:
		raise RuntimeError("không thấy gói vieneu: web phải chạy trong env dub_tts (bash tools/web.sh)")
	path = Path(list(spec.submodule_search_locations)[0]) / "assets" / "voices_v3_turbo.json"
	presets = json.loads(path.read_text(encoding="utf-8"))["presets"]
	return [{"name": n, "desc": v.get("description", "")} for n, v in presets.items()]


def probe_seconds(path):
	"""Thời lượng video (giây) bằng ffprobe của env đang chạy (cùng chỗ với ffmpeg mà dubber/ dùng). Đọc không được: None."""
	ffprobe = Path(sys.executable).parent / "ffprobe"
	try:
		out = subprocess.run([str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
			capture_output=True, text=True, timeout=60).stdout
		return float(out.strip())
	except (OSError, ValueError, subprocess.TimeoutExpired):
		return None


LOGIN_PAGE = """<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dubber: nhập mã mời</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#07090D;color:#EEF2F7;font:16px/1.6 system-ui,sans-serif}
form{width:min(360px,calc(100% - 32px));display:grid;gap:14px;padding:30px;border-radius:20px;background:rgba(255,255,255,.04);box-shadow:inset 0 0 0 1px rgba(255,255,255,.1)}
h1{margin:0;font-size:22px;font-weight:600} p{margin:0;color:#929CAE;font-size:14px}
input{font:inherit;color:inherit;padding:12px 14px;border-radius:12px;border:0;background:rgba(0,0,0,.4);box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}
input:focus{outline:2px solid #3DDC97} button{font:inherit;font-weight:500;padding:12px;border:0;border-radius:999px;background:#EEF2F7;color:#07090D;cursor:pointer}
.err{color:#FF8A80}</style></head><body><form method="post" action="/login">
<h1>Dubber đang thử nghiệm</h1><p>Nhập mã mời để vào trang lồng tiếng.</p>{err}
<input name="code" type="password" autocomplete="current-password" placeholder="Mã mời" autofocus required>
<button type="submit">Vào Dubber</button></form></body></html>"""


# ── Phần vào ra: job, tiến trình con, HTTP ──────────────────────────────────

class Jobs:
	def __init__(self, root, command):
		self.root, self.command = root, command
		self.dir = root / "data" / "jobs"
		self.dir.mkdir(parents=True, exist_ok=True)
		self.lock = threading.Lock()
		self.jobs, self.tails, self.procs = {}, {}, {}
		self.q = queue.Queue()
		for f in sorted(self.dir.glob("*/job.json")):
			job = json.loads(f.read_text(encoding="utf-8"))
			if job["status"] in ACTIVE:
				job["status"], job["error"] = "interrupted", "web bị tắt khi job đang chờ/chạy"
				self._save(job)
			self.jobs[job["id"]] = job
		threading.Thread(target=self._worker, daemon=True).start()

	def _save(self, job):
		d = self.dir / job["id"]
		d.mkdir(parents=True, exist_ok=True)
		(d / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")

	def create(self, video_path, lang, translator, voice):
		job_id = video_path.parent.name
		job = {"id": job_id, "status": "queued", "video": str(video_path.relative_to(self.root)), "lang": lang,
			"translator": translator, "voice": voice, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
			"error": None, "result": None}
		with self.lock:
			self.jobs[job_id] = job
			self._save(job)
		self.q.put(job_id)
		return job

	def log_lines(self, job_id):
		with self.lock:
			if job_id in self.tails:
				return list(self.tails[job_id])
		f = self.dir / job_id / "log.txt"
		return f.read_text(encoding="utf-8", errors="replace").splitlines() if f.is_file() else []

	def view(self, job_id):
		job = self.jobs.get(job_id)
		if job is None:
			return None
		lines = self.log_lines(job_id)
		progress = parse_progress(lines)
		ratio, n = speed_ratio(self.jobs.values())
		return {**job, "progress": progress, **timing(job, progress, time.time(), ratio, n)}

	def cancel(self, job_id):
		with self.lock:
			job = self.jobs.get(job_id)
			if job is None or job["status"] not in ACTIVE:
				return False
			proc = self.procs.get(job_id)
			job["status"] = "cancelled"
			self._save(job)
		if proc is not None:
			try:
				os.killpg(proc.pid, signal.SIGTERM)  # cả nhóm: dub.sh -> conda run -> python
			except ProcessLookupError:
				pass  # vừa tự kết thúc
		return True

	def _worker(self):
		while True:
			self._run(self.jobs[self.q.get()])

	def _run(self, job):
		job_id = job["id"]
		cmd = [*self.command, job["video"], job["lang"], job["translator"], job["voice"]]
		log_path = self.dir / job_id / "log.txt"
		tail = collections.deque(maxlen=400)
		with open(log_path, "w", encoding="utf-8") as log:
			# Kiểm trạng thái, tạo tiến trình, ghi procs và đánh dấu running trong CÙNG một lần khoá:
			# trước đây đánh dấu running trước rồi mới tạo tiến trình, bấm Huỷ đúng khe đó thì job hiện
			# "Đã huỷ" mà tiến trình vẫn chạy tới hết, hàng đợi kẹt (test lặp 12 lần, đỏ 1 lần, 2026-10-29).
			with self.lock:
				if job["status"] != "queued":
					return  # đã huỷ khi còn xếp hàng
				proc = subprocess.Popen(cmd, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
					text=True, bufsize=1, start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
				self.procs[job_id] = proc
				self.tails[job_id] = tail
				job["status"], job["started"], job["started_ts"] = "running", time.strftime("%Y-%m-%d %H:%M:%S"), time.time()
				self._save(job)
			for line in proc.stdout:
				line = line.rstrip("\n")
				log.write(line + "\n")
				log.flush()
				with self.lock:
					tail.append(line)
			code = proc.wait()
		with self.lock:
			self.procs.pop(job_id, None)
			lines = list(tail)
			self.tails.pop(job_id, None)
			job["finished"], job["finished_ts"] = time.strftime("%Y-%m-%d %H:%M:%S"), time.time()
			done_progress = parse_progress(lines)
			job["audio_sec"] = done_progress["audio_sec"]
			if job.get("started_ts"):
				job["elapsed_sec"] = round(job["finished_ts"] - job["started_ts"], 1)
			if job["status"] != "cancelled":
				result = parse_progress(lines)["result"]
				if code == 0 and result:
					job["status"], job["result"] = "done", result
				else:
					err = next((l for l in reversed(lines) if "LỖI" in l), None) or (lines[-1] if lines else "")
					job["status"], job["error"] = "failed", f"mã thoát {code}: {err}"
			self._save(job)


def create_app(root=ROOT, command=("bash", "tools/dub.sh"), voices=None, max_upload_mb=2048,
		access_code=None, max_video_sec=None, only_translator=None, probe=probe_seconds):
	"""Tham số cho chế độ chia sẻ (tools/web.sh share) và bản deploy (deploy/modal_app.py), Thuận chốt 2026-10-30.
	Mặc định None = chạy y như trước, chỉ trên máy này:
	access_code: /app và /api/* cần mã mời (trang giới thiệu và ví dụ vẫn công khai); None = ai có link cũng vào;
	max_video_sec: từ chối video dài hơn; only_translator: chỉ cho dùng một config dịch."""
	# Đường dẫn tuyệt đối ngay từ đầu: send_file hiểu đường dẫn tương đối là tính từ thư mục web/
	# chứ không phải thư mục làm việc (lỗi 500 ở /demo khi chạy thử với root=".", 2026-10-29).
	root = Path(root).resolve()
	app = Flask(__name__, static_folder=None)
	app.config["MAX_CONTENT_LENGTH"] = max_upload_mb * 2**20
	jobs = Jobs(Path(root), list(command))
	voices = voices if voices is not None else load_voices()
	uploads = Path(root) / "data" / "uploads"
	app.jobs = jobs
	if access_code:
		# Khoá phiên suy ra từ mã mời: khởi động lại web không bắt nhập lại; đổi mã thì phiên cũ hết hiệu lực.
		app.secret_key = hashlib.sha256(f"dubber-session:{access_code}".encode()).digest()

	@app.before_request
	def gate():
		if not access_code or session.get("ok"):
			return None
		if request.path == "/app":
			return redirect("/login")
		if request.path.startswith("/api/") and request.path != "/api/demo":
			return jsonify(error="cần mã mời"), 401
		return None

	@app.route("/login", methods=["GET", "POST"])
	def login():
		if not access_code:
			return redirect("/app")
		if request.method == "GET":
			return LOGIN_PAGE.replace("{err}", "")
		if hmac.compare_digest(request.form.get("code", "").strip().encode(), access_code.encode()):
			session["ok"], session.permanent = True, True
			return redirect("/app")
		time.sleep(1)  # đoán mò mã chậm lại
		return LOGIN_PAGE.replace("{err}", '<p class="err">Mã mời không đúng.</p>'), 401

	@app.get("/")
	def landing():
		return send_file(Path(__file__).parent / "landing.html")

	@app.get("/api/demo")
	def demo_info():
		return jsonify(available=all((Path(root) / "data" / "demo" / n).is_file() for n in DEMO_FILES))

	@app.get("/demo/<name>")
	def demo_file(name):
		path = Path(root) / "data" / "demo" / name
		if name not in DEMO_FILES + DEMO_EXTRA or not path.is_file():
			abort(404)
		return send_file(path, conditional=True)

	@app.get("/app")
	def workspace():
		return send_file(Path(__file__).parent / "index.html")

	@app.get("/api/options")
	def options():
		# langs là LIST có thứ tự: jsonify sắp key dict theo chữ cái (ko trước zh), ô chọn sẽ mặc định
		# nhầm tiếng Hàn (thấy khi chụp màn hình 2026-10-29).
		return jsonify(langs=[{"code": k, "label": v} for k, v in LANGS.items()],
			translators=list_translators(Path(root), only_translator), voices=voices, default_voice=DEFAULT_VOICE,
			max_video_sec=max_video_sec)

	@app.post("/api/jobs")
	def create_job():
		f = request.files.get("video")
		lang, translator, voice = request.form.get("lang"), request.form.get("translator"), request.form.get("voice")
		errors = []
		if f is None or not f.filename:
			errors.append("chưa chọn video")
		elif Path(f.filename).suffix.lower() not in VIDEO_EXT:
			errors.append(f"đuôi file không hỗ trợ: {Path(f.filename).suffix} (nhận {sorted(VIDEO_EXT)})")
		if lang not in LANGS:
			errors.append(f"ngôn ngữ không hợp lệ: {lang!r}")
		if translator not in {t["file"] for t in list_translators(Path(root), only_translator)}:
			errors.append(f"config dịch không có: {translator!r}")
		if voice not in {v["name"] for v in voices}:
			errors.append(f"giọng không có: {voice!r}")
		if errors:
			return jsonify(error="; ".join(errors)), 400
		job_id = time.strftime("%Y%m%d_%H%M%S_") + os.urandom(2).hex()
		name = secure_filename(Path(f.filename).stem) or "video"
		dest = uploads / job_id / f"{job_id}__{name}{Path(f.filename).suffix.lower()}"
		dest.parent.mkdir(parents=True, exist_ok=True)
		f.save(dest)
		if max_video_sec is not None:
			sec = probe(dest)
			if sec is None or sec > max_video_sec:
				dest.unlink(missing_ok=True)
				dest.parent.rmdir()
				if sec is None:
					return jsonify(error="không đọc được file video"), 400
				return jsonify(error=f"video dài {sec / 60:.1f} phút, tối đa {max_video_sec / 60:.0f} phút"), 400
		return jsonify(public_view(jobs.view(jobs.create(dest, lang, translator, voice)["id"]))), 201

	@app.get("/api/jobs")
	def list_jobs():
		return jsonify([public_view(jobs.view(i)) for i in sorted(jobs.jobs, reverse=True)])

	@app.get("/api/jobs/<job_id>")
	def get_job(job_id):
		v = jobs.view(job_id)
		return jsonify(public_view(v)) if v else abort(404)

	@app.post("/api/jobs/<job_id>/cancel")
	def cancel_job(job_id):
		return jsonify(ok=jobs.cancel(job_id))

	def _inside(path, folder):
		"""Đường dẫn thật phải nằm trong data/<folder>: không để job.json bị sửa tay đọc ra file khác."""
		return (Path(root) / "data" / folder).resolve() in Path(path).resolve().parents

	@app.get("/api/jobs/<job_id>/source")
	def source(job_id):
		job = jobs.jobs.get(job_id)
		path = (Path(root) / job["video"]).resolve() if job else None
		if path is None or not _inside(path, "uploads") or not path.is_file():
			abort(404)
		return send_file(path, conditional=True)

	@app.get("/api/jobs/<job_id>/segments")
	def segments(job_id):
		"""Lời thoại song ngữ của job: có sau stage segment, bản dịch có sau stage translate, mốc phát
		trong video Việt (play_start) có sau stage mix. Chưa tới thì trả danh sách rỗng."""
		view = jobs.view(job_id)
		if view is None:
			abort(404)
		project = view["progress"]["project"]
		f = Path(root) / project / "segments.json" if project else None
		if f is None or not _inside(f, "projects") or not f.is_file():
			return jsonify(segments=[])
		doc = json.loads(f.read_text(encoding="utf-8"))
		plans = doc.get("mix", {}).get("plans", [])
		out = []
		for i, s in enumerate(doc.get("segments", [])):
			p = plans[i] if i < len(plans) else {}
			out.append({"i": i, "start": s["start"], "end": s["end"], "src": s["src"], "tgt": s.get("tgt"),
				"play_start": p.get("play_start")})
		return jsonify(segments=out)

	def _result_file(job_id):
		job = jobs.jobs.get(job_id)
		if job is None or not job.get("result"):
			abort(404)
		path = (Path(root) / job["result"]).resolve()
		if not _inside(path, "projects") or not path.is_file():
			abort(404)
		return path

	@app.get("/api/jobs/<job_id>/video")
	def video(job_id):
		# conditional=True: trả 206 theo Range, trình duyệt tua được video dài.
		return send_file(_result_file(job_id), conditional=True)

	@app.errorhandler(413)
	def too_large(_):
		return jsonify(error=f"file quá {max_upload_mb} MB"), 413

	return app


def main():
	p = argparse.ArgumentParser(description="Web lồng tiếng chạy trên máy")
	p.add_argument("--host", required=True)
	p.add_argument("--port", type=int, required=True)
	p.add_argument("--max-upload-mb", type=int, required=True)
	p.add_argument("--share", action="store_true", help="chế độ chia sẻ; có DUBBER_ACCESS_CODE trong .env thì bắt nhập mã")
	p.add_argument("--max-video-min", type=float, help="chỉ dùng với --share")
	p.add_argument("--only-translator", help="chỉ dùng với --share")
	a = p.parse_args()
	share = {}
	if a.share:
		from dubber.translate import load_dotenv

		load_dotenv(ROOT / ".env")
		code = os.environ.get("DUBBER_ACCESS_CODE", "").strip()
		errors = []
		if code and len(code) < 6:
			errors.append("DUBBER_ACCESS_CODE trong .env quá ngắn (ít nhất 6 ký tự; để trống nếu không dùng mã mời)")
		if not a.max_video_min:
			errors.append("thiếu --max-video-min")
		if not a.only_translator or not (ROOT / a.only_translator).is_file():
			errors.append(f"--only-translator không có file: {a.only_translator!r}")
		if errors:
			sys.exit("LỖI: không mở chế độ chia sẻ: " + "; ".join(errors))
		share = {"access_code": code or None, "max_video_sec": a.max_video_min * 60, "only_translator": a.only_translator}
	app = create_app(max_upload_mb=a.max_upload_mb, **share)
	mode = ""
	if share:
		mode = "  (chế độ chia sẻ: " + ("cần mã mời" if share["access_code"] else "KHÔNG có mã mời, ai có link cũng vào được") + ")"
	print(f"[web] mở trình duyệt: http://{a.host}:{a.port}{mode}")
	app.run(host=a.host, port=a.port, threaded=True)


if __name__ == "__main__":
	main()