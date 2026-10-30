"""Test web/app.py: hàm thuần + Flask test client với một lệnh dub giả (không cần GPU, model, mạng)."""
import io
import json
import time

import pytest

from web import app as web

LOG_10PHUT = """[dub] video=data/samples/v.mp4 lang=zh dịch=c.json giọng=Thiện Minh đoạn ASR=30s -> data/projects/x
[asr] audio 608.5s, 24 đoạn: 0.0–29.9, 29.9–53.8
[asr] nhận dạng đoạn 12/24: 70 ký tự
""".splitlines()


def test_parse_progress_theo_tung_stage():
	# Tình huống thật: log của tools/dub.sh trên video 10 phút (2026-10-29), web đọc để vẽ thanh tiến độ.
	# Phần giả lập: log dừng ở giữa ASR, rồi ở giữa TTS, rồi dòng XONG.
	# Đỏ thì hỏng gì: thanh tiến độ đứng im hoặc nhảy lùi, người dùng tưởng treo.
	p = web.parse_progress(LOG_10PHUT)
	assert p["stage"] == "asr" and 0 < p["percent"] < 20
	mid_tts = LOG_10PHUT + ["[segment] 125 câu -> data/projects/x/segments.json", "[translate] lô 9: câu 120–124 xong (9 request)",
		"[tts] 61: 3.2s  Câu gì đó."]
	p = web.parse_progress(mid_tts)
	assert p["stage"] == "tts" and 55 < p["percent"] < 70
	done = mid_tts + ["[dub] XONG sau 308s: data/projects/x/x_vi.mp4"]
	p = web.parse_progress(done)
	assert (p["stage"], p["percent"], p["result"]) == ("done", 100, "data/projects/x/x_vi.mp4")
	assert (p["project"], p["total"], p["spoken"]) == ("data/projects/x", 125, 62)


FAKE_DUB = """#!/usr/bin/env bash
# Lệnh dub giả: cùng tham số và cùng kiểu log với tools/dub.sh.
set -e
video="$1"; mode="$(basename "$video")"
echo "[dub] video=$video lang=$2 dịch=$3 giọng=$4 đoạn ASR=30s -> data/projects/p"
case "$mode" in
  *hong*) echo "LỖI: gọi API gemini (m): hết lượt miễn phí trong ngày"; exit 1 ;;
  *cham*) echo "[asr] audio 10s, 1 đoạn"; sleep 30 ;;
esac
echo "[asr] audio 10.0s, 1 đoạn: 0.0–10.0"
echo "[asr] nhận dạng đoạn 1/1: 10 ký tự"
echo "[segment] 2 câu -> data/projects/p/segments.json"
echo "[tts] 0: 1.0s  Một."
echo "[tts] 1: 1.0s  Hai."
mkdir -p data/projects/p
printf '0123456789' > data/projects/p/p_vi.mp4
cat > data/projects/p/segments.json <<'J'
{"segments": [{"src": "一。", "tgt": "Một.", "start": 0.5, "end": 1.0}, {"src": "二。", "tgt": "Hai.", "start": 2.0, "end": 2.6}],
 "mix": {"plans": [{"play_start": 0.5}, {"play_start": 2.1}]}}
J
echo "[dub] XONG sau 1s: data/projects/p/p_vi.mp4"
"""


@pytest.fixture
def env(tmp_path):
	(tmp_path / "configs").mkdir()
	(tmp_path / "configs" / "translator_gemini.json").write_text(json.dumps({"name": "gemini", "model": "m"}), encoding="utf-8")
	(tmp_path / "fake_dub.sh").write_text(FAKE_DUB, encoding="utf-8")
	app = web.create_app(root=tmp_path, command=("bash", "fake_dub.sh"), voices=[{"name": "Thiện Minh", "desc": "Nam"}], max_upload_mb=1)
	return tmp_path, app, app.test_client()


def _upload(client, name="clip.mp4", **form):
	data = {"video": (io.BytesIO(b"fake"), name), "lang": "zh", "translator": "configs/translator_gemini.json", "voice": "Thiện Minh"}
	data.update(form)
	return client.post("/api/jobs", data=data, content_type="multipart/form-data")


def _wait(client, job_id, statuses=("done", "failed", "cancelled"), timeout=10):
	end = time.time() + timeout
	while time.time() < end:
		j = client.get(f"/api/jobs/{job_id}").get_json()
		if j["status"] in statuses:
			return j
		time.sleep(0.05)
	raise AssertionError(f"job {job_id} không tới {statuses}: {j['status']}")


def test_upload_chay_xong_va_xem_duoc_video_co_tua(env):
	# Tình huống thật: upload video, chờ xong, xem ngay trên web và tua tới giữa video dài.
	# Phần giả lập: lệnh dub giả in log đúng định dạng, sinh file kết quả 10 byte.
	# Đỏ thì hỏng gì: job xong mà web không hiện video, hoặc video không tua được (thiếu 206 Range).
	root, _, c = env
	r = _upload(c)
	assert r.status_code == 201
	j = _wait(c, r.get_json()["id"])
	assert j["status"] == "done" and j["has_result"] is True
	assert j["progress"]["percent"] == 100
	part = c.get(f"/api/jobs/{j['id']}/video", headers={"Range": "bytes=2-5"})
	assert part.status_code == 206 and part.data == b"2345"
	assert (root / "data" / "jobs" / j["id"] / "log.txt").read_text(encoding="utf-8").count("[tts]") == 2


def test_form_sai_thi_bao_du_moi_loi_va_khong_tao_job(env):
	# Tình huống thật: chọn nhầm file ảnh, config dịch đã bị xoá.
	# Phần giả lập: đuôi .png, translator không tồn tại, giọng không tồn tại.
	# Đỏ thì hỏng gì: job chạy rồi mới chết giữa chừng, tốn ASR vô ích; hoặc chỉ báo lỗi đầu tiên.
	_, app, c = env
	r = _upload(c, name="anh.png", translator="configs/khong_co.json", voice="Ai Đó")
	assert r.status_code == 400
	err = r.get_json()["error"]
	assert "đuôi file" in err and "config dịch" in err and "giọng" in err
	assert app.jobs.jobs == {}


def test_job_loi_bao_cau_de_hieu_con_loi_tho_giu_tren_may_chu(env):
	# Tình huống thật: hết lượt dịch giữa chừng, dub.sh in LỖI rồi thoát mã 1.
	# Phần giả lập: lệnh giả in đúng dòng LỖI rồi exit 1.
	# Đỏ thì hỏng gì: người dùng thấy lỗi thô kỹ thuật, hoặc người quản trị mất dấu lỗi thật.
	root, _, c = env
	j = _wait(c, _upload(c, name="hong.mp4").get_json()["id"])
	assert j["status"] == "failed" and j["message"] == "Dịch vụ dịch đã hết lượt dùng. Thử lại sau, hoặc chọn cách dịch khác."
	saved = json.loads((root / "data" / "jobs" / j["id"] / "job.json").read_text(encoding="utf-8"))
	assert "hết lượt miễn phí" in saved["error"]  # lỗi thô vẫn giữ trên máy chủ cho người quản trị


def test_huy_job_dang_chay_va_job_sau_van_chay(env):
	# Tình huống thật: chọn nhầm giọng, huỷ job đang chạy; job xếp hàng phía sau phải chạy tiếp.
	# Phần giả lập: job 1 ngủ 30s, job 2 chạy nhanh.
	# Đỏ thì hỏng gì: huỷ không giết được tiến trình con (GPU vẫn bận), hoặc hàng đợi kẹt luôn.
	_, _, c = env
	j1 = _upload(c, name="cham.mp4").get_json()["id"]
	_wait(c, j1, statuses=("running",))
	j2 = _upload(c).get_json()["id"]
	assert c.post(f"/api/jobs/{j1}/cancel").get_json()["ok"] is True
	assert _wait(c, j1)["status"] == "cancelled"
	assert _wait(c, j2)["status"] == "done"


def test_tat_web_khi_dang_chay_thi_danh_dau_interrupted(tmp_path):
	# Tình huống thật: tắt web (Ctrl+C, mất điện) khi job đang chạy.
	# Phần giả lập: job.json ghi trạng thái running, rồi mở lại web.
	# Đỏ thì hỏng gì: job hiện "Đang chạy" mãi, hoặc biến mất không dấu vết.
	d = tmp_path / "data" / "jobs" / "j1"
	d.mkdir(parents=True)
	(d / "job.json").write_text(json.dumps({"id": "j1", "status": "running", "video": "v.mp4", "lang": "zh",
		"translator": "t", "voice": "v", "created": "x", "error": None, "result": None}), encoding="utf-8")
	(tmp_path / "configs").mkdir()
	app = web.create_app(root=tmp_path, command=("true",), voices=[])
	assert app.jobs.jobs["j1"]["status"] == "interrupted"


def test_khong_phuc_vu_file_ngoai_data_projects(env):
	# Tình huống thật: job.json bị sửa tay, result trỏ ra ngoài thư mục kết quả.
	# Phần giả lập: result = "../../etc/passwd".
	# Đỏ thì hỏng gì: web đọc được file bất kỳ trên máy.
	_, app, c = env
	app.jobs.jobs["x"] = {"id": "x", "status": "done", "result": "../../etc/passwd"}
	assert c.get("/api/jobs/x/video").status_code == 404


def test_ngon_ngu_mac_dinh_la_tieng_trung(env):
	# Tình huống thật: chụp màn hình 2026-10-29 thấy ô ngôn ngữ mặc định "Tiếng Hàn" vì JSON bị sắp key.
	# Phần giả lập: gọi /api/options.
	# Đỏ thì hỏng gì: bấm "Lồng tiếng" không để ý, video tiếng Trung bị nhận dạng như tiếng Hàn.
	_, _, c = env
	assert [l["code"] for l in c.get("/api/options").get_json()["langs"]] == ["zh", "ko"]


def test_huy_o_moi_thoi_diem_deu_giai_phong_hang_doi(env):
	# Tình huống thật: bấm Huỷ ngay sau khi gửi, đúng lúc job vừa chuyển sang chạy. Lỗi cũ: job hiện
	# "Đã huỷ" nhưng tiến trình vẫn chạy (GPU bận), job sau kẹt ở "Đang chờ" (bắt được 2026-10-29).
	# Phần giả lập: 5 lần liền, mỗi lần gửi job ngủ 30s rồi huỷ ngay, không chờ trạng thái nào.
	# Đỏ thì hỏng gì: huỷ trúng khe thời gian là hàng đợi kẹt tới khi tiến trình mồ côi chạy xong.
	_, _, c = env
	for _ in range(5):
		j = _upload(c, name="cham.mp4").get_json()["id"]
		assert c.post(f"/api/jobs/{j}/cancel").get_json()["ok"] is True
		assert _wait(c, j)["status"] == "cancelled"
	assert _wait(c, _upload(c).get_json()["id"], timeout=10)["status"] == "done"


def test_loi_thoai_song_ngu_va_video_goc_cho_trang_lam_viec(env):
	# Tình huống thật: trang làm việc hiện câu gốc + câu dịch, bấm câu thì nhảy tới đúng mốc trong
	# video gốc hoặc video Việt; nút chuyển Bản gốc / Tiếng Việt cần phát được video gốc.
	# Phần giả lập: lệnh dub giả ghi segments.json 2 câu kèm mốc phát của bản Việt.
	# Đỏ thì hỏng gì: danh sách song ngữ trống, bấm câu nhảy sai chỗ, nút Bản gốc không phát được.
	root, _, c = env
	j = _wait(c, _upload(c).get_json()["id"])
	segs = c.get(f"/api/jobs/{j['id']}/segments").get_json()["segments"]
	assert [(s["src"], s["tgt"], s["start"], s["play_start"]) for s in segs] == [("一。", "Một.", 0.5, 0.5), ("二。", "Hai.", 2.0, 2.1)]
	src = c.get(f"/api/jobs/{j['id']}/source", headers={"Range": "bytes=0-1"})
	assert src.status_code == 206 and src.data == b"fa"


def test_job_chua_toi_buoc_cat_cau_thi_loi_thoai_rong(env):
	# Tình huống thật: vừa gửi video, ASR chưa xong, trang làm việc đã hỏi lời thoại.
	# Phần giả lập: job chậm, hỏi ngay khi đang chạy.
	# Đỏ thì hỏng gì: web báo lỗi 500 hoặc hiện dữ liệu của job khác.
	_, _, c = env
	j = _upload(c, name="cham.mp4").get_json()["id"]
	_wait(c, j, statuses=("running",))
	assert c.get(f"/api/jobs/{j}/segments").get_json() == {"segments": []}
	c.post(f"/api/jobs/{j}/cancel")


def test_trang_gioi_thieu_va_trang_lam_viec_deu_mo_duoc(env):
	# Tình huống thật: "/" là trang giới thiệu, "/app" là chỗ làm việc.
	# Phần giả lập: GET cả hai.
	# Đỏ thì hỏng gì: mở web ra trang trắng hoặc 404.
	_, _, c = env
	assert c.get("/").status_code == 200 and "Dubber" in c.get("/").get_data(as_text=True)
	assert c.get("/app").status_code == 200


def test_api_khong_lo_log_duong_dan_hay_loi_tho(env):
	# Tình huống thật: người dùng mở DevTools xem phản hồi API (Thuận chốt 2026-10-29: không lộ log/debug).
	# Phần giả lập: một job lỗi và một job xong; quét toàn bộ JSON trả về.
	# Đỏ thì hỏng gì: log, đường dẫn trên máy chủ hay lỗi thô lọt ra trình duyệt.
	_, _, c = env
	_wait(c, _upload(c, name="hong.mp4").get_json()["id"])
	_wait(c, _upload(c).get_json()["id"])
	raw = c.get("/api/jobs").get_data(as_text=True)
	for leak in ("log_tail", "data/projects", "data/uploads", "LỖI", "mã thoát", '"error"', '"result"'):
		assert leak not in raw, leak
	assert c.get("/api/jobs/x/report").status_code == 404


def test_cau_bao_loi_theo_tung_loai():
	# Tình huống thật: các lỗi đã gặp 2026-10-29 (hết lượt ngày, 503 quá tải) và lỗi chưa biết.
	# Phần giả lập: job lỗi với từng kiểu chuỗi lỗi thô.
	# Đỏ thì hỏng gì: người dùng không biết nên chờ, đổi cách dịch hay gửi lại file.
	def msg(err):
		return web.user_message({"status": "failed", "error": err})
	assert "hết lượt" in msg("LỖI: gemini: hết lượt miễn phí trong ngày")
	assert "quá tải" in msg("InternalServerError: Error code: 503")
	assert "đọc được file video" in msg("Invalid data found when processing input")
	assert msg("thứ gì đó lạ") == web.USER_FALLBACK
	assert web.user_message({"status": "running"}) is None


def test_vi_du_trang_gioi_thieu_chi_phuc_vu_dung_file(env):
	# Tình huống thật: trang giới thiệu phát video ví dụ từ data/demo.
	# Phần giả lập: chưa có ví dụ, rồi có đủ 3 file, rồi hỏi một tên file lạ.
	# Đỏ thì hỏng gì: trang hỏng khi chưa có ví dụ, hoặc đọc được file khác trên máy chủ.
	root, _, c = env
	assert c.get("/api/demo").get_json() == {"available": False}
	d = root / "data" / "demo"
	d.mkdir(parents=True)
	for n in ("before.mp4", "after.mp4", "segments.json"):
		(d / n).write_text("x", encoding="utf-8")
	assert c.get("/api/demo").get_json() == {"available": True}
	assert c.get("/demo/after.mp4").status_code == 200
	assert c.get("/demo/meta.json").status_code == 404  # meta.json là phụ: thiếu thì trang vẫn chạy
	(d / "meta.json").write_text("{}", encoding="utf-8")
	assert c.get("/demo/meta.json").status_code == 200
	assert c.get("/demo/..%2f..%2fconfigs%2ftranslator_gemini.json").status_code == 404


def test_trang_gioi_thieu_la_trang_tinh():
	# Tình huống thật: khi thương mại hoá, trang giới thiệu host riêng (host tĩnh), server GPU chỉ chạy app
	# (Thuận chốt 2026-10-30). Trang gọi API của server thì server tắt là trang giới thiệu chết theo.
	# Phần giả lập: đọc thẳng file landing.html.
	# Đỏ thì hỏng gì: trang giới thiệu lại phụ thuộc server, không chuyển sang host tĩnh được.
	from pathlib import Path

	html = (Path(web.__file__).parent / "landing.html").read_text(encoding="utf-8")
	assert "/api/" not in html
	assert '"/demo/"' in html


def test_uoc_tinh_thoi_gian_tu_cac_lan_chay_truoc():
	# Tình huống thật: trên máy Thuận video 29s mất 51s, video 608s mất 308s; video mới 300s đang chạy được 60s.
	# Phần giả lập: 3 job đã xong (tỉ lệ 1.76, 0.51, 9.0 do lần đầu tải model) và 1 job đang chạy.
	# Đỏ thì hỏng gì: ước tính bị một lần chạy lạ kéo lệch, hoặc bịa ra ước tính khi chưa có số liệu.
	done = [{"status": "done", "elapsed_sec": 51, "audio_sec": 29}, {"status": "done", "elapsed_sec": 308, "audio_sec": 608},
		{"status": "done", "elapsed_sec": 270, "audio_sec": 30}, {"status": "failed", "elapsed_sec": 5, "audio_sec": 30}]
	ratio, n = web.speed_ratio(done)
	assert n == 3 and abs(ratio - 51 / 29) < 1e-9  # trung vị, không bị 9.0 kéo lên
	running = {"status": "running", "started_ts": 1000.0}
	t = web.timing(running, {"audio_sec": 300.0}, now=1060.0, ratio=ratio, n=n)
	assert t["elapsed_sec"] == 60.0 and t["eta_sec"] == round(ratio * 300 - 60) and t["eta_from"] == 3
	assert web.speed_ratio([]) == (None, 0)
	assert web.timing(running, {"audio_sec": 300.0}, now=1060.0, ratio=None, n=0)["eta_sec"] is None


def test_job_xong_ghi_thoi_luong_video_va_thoi_gian_xu_ly(env):
	# Tình huống thật: xong job thì trang hiện "Video dài X, xử lý xong trong Y".
	# Phần giả lập: lệnh dub giả in dòng "[asr] audio 10s".
	# Đỏ thì hỏng gì: dòng thời gian trống, hoặc ước tính các lần sau không có số liệu để tính.
	root, _, c = env
	j = _wait(c, _upload(c).get_json()["id"])
	assert j["audio_sec"] == 10.0 and j["elapsed_sec"] is not None and j["elapsed_sec"] >= 0
	saved = json.loads((root / "data" / "jobs" / j["id"] / "job.json").read_text(encoding="utf-8"))
	assert saved["audio_sec"] == 10.0 and "elapsed_sec" in saved


def test_root_tuong_doi_van_phuc_vu_dung_file(tmp_path, monkeypatch):
	# Tình huống thật: chạy web với root tương đối; send_file tính đường dẫn tương đối từ thư mục web/.
	# Phần giả lập: chdir vào thư mục tạm, create_app(root="."), đặt file ví dụ.
	# Đỏ thì hỏng gì: trang giới thiệu báo có ví dụ nhưng tải video ví dụ lỗi 500.
	monkeypatch.chdir(tmp_path)
	(tmp_path / "configs").mkdir()
	d = tmp_path / "data" / "demo"
	d.mkdir(parents=True)
	for n in ("before.mp4", "after.mp4", "segments.json"):
		(d / n).write_text("x", encoding="utf-8")
	c = web.create_app(root=".", command=("true",), voices=[]).test_client()
	assert c.get("/demo/segments.json").status_code == 200

def test_trang_gioi_thieu_du_ban_dich_hai_ngon_ngu():
	# Tình huống thật: trang giới thiệu song ngữ VI/EN (Thuận chốt 2026-10-30); chữ nằm trong một bảng JSON,
	# HTML và script chỉ trỏ tới khoá.
	# Phần giả lập: đọc bảng JSON trong landing.html, gom mọi khoá mà HTML (data-i18n...) và script (t("...")) dùng.
	# Đỏ thì hỏng gì: đổi sang một ngôn ngữ thì chỗ thiếu bản dịch hiện ra mã khoá như "hero.cta".
	import re
	from pathlib import Path

	html = (Path(web.__file__).parent / "landing.html").read_text(encoding="utf-8")
	table = json.loads(re.search(r'<script type="application/json" id="i18n">(.*?)</script>', html, re.S).group(1))
	assert set(table["vi"]) == set(table["en"])
	used = set(re.findall(r'data-i18n(?:-html|-aria)?="([\w.]+)"', html)) | set(re.findall(r'\bt\("([\w.]+)"', html))
	assert used and not used - set(table["vi"])


def _limited_app(tmp_path, probe_sec, access_code=None):
	(tmp_path / "configs").mkdir(parents=True)
	for name, model in (("translator_gemini", "lite"), ("translator_gemini_flash", "flash")):
		(tmp_path / "configs" / f"{name}.json").write_text(json.dumps({"name": name, "model": model}), encoding="utf-8")
	(tmp_path / "fake_dub.sh").write_text(FAKE_DUB, encoding="utf-8")
	return web.create_app(root=tmp_path, command=("bash", "fake_dub.sh"), voices=[{"name": "Thiện Minh", "desc": "Nam"}],
		max_upload_mb=1, max_video_sec=600, only_translator="configs/translator_gemini.json", probe=lambda path: probe_sec,
		access_code=access_code)


def test_ban_deploy_chi_cho_dung_mot_cau_hinh_dich(tmp_path):
	# Tình huống thật: bản trên Modal cho 3 người bạn chỉ dùng Flash-Lite (~500 lượt/ngày); Flash chỉ ~20 (Thuận chốt 2026-10-30).
	# Phần giả lập: có 2 config; app chỉ cho translator_gemini.json; gửi form tự sửa sang config flash.
	# Đỏ thì hỏng gì: ô chọn vẫn hiện Flash, hoặc ai đó sửa form gửi thẳng config Flash lên vẫn chạy, hết lượt cả ngày.
	c = _limited_app(tmp_path, 60.0).test_client()
	assert [t["file"] for t in c.get("/api/options").get_json()["translators"]] == ["configs/translator_gemini.json"]
	r = _upload(c, translator="configs/translator_gemini_flash.json")
	assert r.status_code == 400 and "config dịch" in r.get_json()["error"]


def test_ban_deploy_tu_choi_video_qua_dai_va_xoa_file(tmp_path):
	# Tình huống thật: giới hạn 10 phút mỗi video trên bản deploy (đã đo video 10 phút chạy ổn, 2026-10-29).
	# Phần giả lập: probe giả trả 700s (quá 600s), rồi 300s.
	# Đỏ thì hỏng gì: video 1 giờ chiếm GPU (tốn credit) cả buổi; hoặc file bị từ chối vẫn nằm lại chiếm ổ.
	app = _limited_app(tmp_path / "a", 700.0)
	c = app.test_client()
	r = _upload(c)
	assert r.status_code == 400 and "10 phút" in r.get_json()["error"]
	assert app.jobs.jobs == {} and not any((tmp_path / "a" / "data" / "uploads").rglob("*.mp4"))
	assert c.get("/api/options").get_json()["max_video_sec"] == 600
	assert _upload(_limited_app(tmp_path / "b", 300.0).test_client()).status_code == 201


def test_mac_dinh_khong_gioi_han_nhu_truoc(env):
	# Tình huống thật: chạy trên máy Thuận (bash tools/web.sh) không có giới hạn nào, như trước gói 020.
	# Phần giả lập: app mặc định, có 1 config dịch.
	# Đỏ thì hỏng gì: thêm giới hạn cho bản deploy làm đổi hành vi bản trên máy.
	_, _, c = env
	assert c.get("/api/options").get_json()["max_video_sec"] is None
	assert _upload(c).status_code == 201



def test_chia_se_co_ma_moi_thi_phai_nhap_dung_moi_vao_app(tmp_path):
	# Tình huống thật: chia sẻ qua Cloudflare Tunnel; Thuận đặt DUBBER_ACCESS_CODE trong .env (tuỳ chọn, chốt 2026-10-30).
	# Phần giả lập: app có mã mời; người lạ vào /app và gọi API, rồi nhập sai, rồi nhập đúng mã.
	# Đỏ thì hỏng gì: đặt mã rồi mà ai có link vẫn dùng được GPU và quota Gemini; hoặc trang giới thiệu bị khoá theo.
	c = _limited_app(tmp_path, 60.0, access_code="ma-moi-123").test_client()
	assert c.get("/").status_code == 200  # trang giới thiệu vẫn công khai
	r = c.get("/app")
	assert r.status_code == 302 and "/login" in r.headers["Location"]
	assert c.get("/api/jobs").status_code == 401 and c.get("/api/options").status_code == 401
	assert c.post("/login", data={"code": "doan-bua"}).status_code == 401
	assert c.get("/api/jobs").status_code == 401
	r = c.post("/login", data={"code": "ma-moi-123"})
	assert r.status_code == 302 and r.headers["Location"].endswith("/app")
	assert c.get("/app").status_code == 200 and c.get("/api/jobs").status_code == 200


def test_chia_se_khong_dat_ma_thi_vao_thang(tmp_path):
	# Tình huống thật: Thuận để trống DUBBER_ACCESS_CODE: ai có link cũng vào /app được, không có trang nhập mã.
	# Phần giả lập: app chia sẻ không có mã; vào /app, gọi API, mở /login.
	# Đỏ thì hỏng gì: bạn của Thuận bị chặn ở trang nhập mã mà không ai có mã.
	c = _limited_app(tmp_path, 60.0).test_client()
	assert c.get("/app").status_code == 200 and c.get("/api/jobs").status_code == 200
	r = c.get("/login")
	assert r.status_code == 302 and r.headers["Location"].endswith("/app")