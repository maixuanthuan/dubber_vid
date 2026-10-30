"""Test dubber/translate.py bằng client giả: không gọi mạng, không cần key."""
import json
from types import SimpleNamespace

import pytest

from dubber import translate as tr

CFG = {"name": "fake", "base_url": "x", "model": "m", "api_key_env": "K", "batch_size": 2,
	"context_pairs": 1, "max_retries": 0, "temperature": 0.3, "timeout_sec": 10, "max_wait_sec": 60}


class FakeApiError(Exception):
	"""Giống openai.APIStatusError ở chỗ code dùng tới: có status_code và body."""

	def __init__(self, status_code, body=None):
		super().__init__(f"Error code: {status_code} - {body}")
		self.status_code, self.body = status_code, body


# Body thật Google trả 2026-10-29 khi gemini-3.8-flash hết 20 lượt/ngày (rút gọn).
DAY_QUOTA_BODY = [{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
	{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
		{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]},
	{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "82530s"}]}}]


class FakeClient:
	"""Trả lần lượt các phản hồi trong `replies`. Phần tử là Exception thì ném ra, giống SDK thật khi lỗi."""

	def __init__(self, replies):
		self.replies = list(replies)
		self.calls = []
		self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

	def _create(self, **kw):
		self.calls.append(kw)
		r = self.replies.pop(0)
		if isinstance(r, Exception):
			raise r
		return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=r))],
			usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


def test_parse_json_array_chiu_duoc_khoi_code():
	# Tình huống thật: Gemini hay bọc kết quả trong ```json ... ``` dù đã dặn chỉ trả JSON.
	# Phần giả lập: phản hồi có rào code và chữ thừa.
	# Đỏ thì hỏng gì: cả lô bị coi là hỏng, tốn thêm lượt API vô ích.
	raw = 'Đây:\n```json\n["Câu một.", "Câu hai."]\n```'
	assert tr.parse_json_array(raw, 2) == ["Câu một.", "Câu hai."]


def test_parse_json_array_sai_so_cau_hoac_rong_thi_bao_loi():
	# Tình huống thật: model gộp hai câu thành một, hoặc trả chuỗi rỗng.
	# Phần giả lập: thiếu một câu; một câu rỗng.
	# Đỏ thì hỏng gì: câu dịch bị lệch hàng với câu gốc, giọng Việt đọc sai chỗ cả đoạn sau.
	with pytest.raises(tr.TranslateError, match="cần 2"):
		tr.parse_json_array('["Một câu thôi."]', 2)
	with pytest.raises(tr.TranslateError, match="rỗng"):
		tr.parse_json_array('["Câu một.", " "]', 2)


def test_batch_sai_hai_lan_thi_dich_tung_cau():
	# Tình huống thật: lô 2 câu bị trả thiếu câu hai lần liền.
	# Phần giả lập: 2 phản hồi thiếu câu, rồi 2 phản hồi đúng cho từng câu riêng.
	# Đỏ thì hỏng gì: cả video dừng vì một lô xấu, hoặc tệ hơn là nhét câu gốc vào chỗ bản dịch.
	client = FakeClient(['["a"]', '["a"]', '["Một."]', '["Hai."]'])
	t = tr.Translator(CFG, client, src_lang="zh")
	assert t.batch(["一。", "二。"], context=[]) == ["Một.", "Hai."]
	assert t.requests == 4


def test_loi_api_thi_dung_ngay_khong_thu_tiep():
	# Tình huống thật: hết lượt miễn phí trong ngày (429 RESOURCE_EXHAUSTED); SDK đã tự thử lại rồi.
	# Phần giả lập: lần gọi đầu ném lỗi.
	# Đỏ thì hỏng gì: code đốt thêm hàng loạt request vô ích, hoặc nuốt lỗi và ra video có câu chưa dịch.
	client = FakeClient([RuntimeError("429 RESOURCE_EXHAUSTED per day")])
	t = tr.Translator(CFG, client, src_lang="zh")
	with pytest.raises(tr.ApiError, match="RESOURCE_EXHAUSTED"):
		t.batch(["一。"], context=[])
	assert len(client.calls) == 1


def test_translate_segments_luu_sau_moi_lo_va_bo_qua_cau_da_dich():
	# Tình huống thật: hết lượt giữa video; chạy lại hôm sau phải dịch tiếp chỗ dở, không dịch lại từ đầu.
	# Phần giả lập: 3 câu, câu đầu đã có bản dịch, batch_size 2.
	# Đỏ thì hỏng gì: mất phần đã dịch khi lỗi giữa chừng, hoặc tốn lượt dịch lại câu đã có.
	segs = [{"src": "一。", "tgt": "Một."}, {"src": "二。"}, {"src": "三。"}]
	client = FakeClient(['["Hai.", "Ba."]'])
	saves = []
	n = tr.translate_segments(segs, tr.Translator(CFG, client, src_lang="zh"), CFG,
		save=lambda: saves.append([s.get("tgt") for s in segs]), log=lambda *_: None)
	assert n == 2 and saves == [["Một.", "Hai.", "Ba."]]
	user_msg = client.calls[0]["messages"][1]["content"]
	assert "一。 → Một." in user_msg  # câu trước được gửi làm ngữ cảnh
	assert json.dumps(["二。", "三。"], ensure_ascii=False) in user_msg


def test_load_config_thieu_muc_thi_bao_ten_muc(tmp_path):
	# Tình huống thật: sửa tay file config, xoá nhầm một dòng.
	# Phần giả lập: config thiếu "model".
	# Đỏ thì hỏng gì: chạy với mặc định ngầm, gọi nhầm model mà không ai biết.
	p = tmp_path / "c.json"
	p.write_text(json.dumps({k: v for k, v in CFG.items() if k != "model"}), encoding="utf-8")
	with pytest.raises(tr.TranslateError, match="model"):
		tr.load_config(p)


def test_load_dotenv_khong_ghi_de_bien_da_co(tmp_path, monkeypatch):
	# Tình huống thật: key đặt trong .env; người dùng có khi đã export biến cùng tên trong terminal.
	# Phần giả lập: .env có 2 biến, một biến đã có sẵn trong môi trường.
	# Đỏ thì hỏng gì: đọc sai key, hoặc .env âm thầm đè giá trị người dùng vừa đặt.
	monkeypatch.setenv("KEY_A", "tu_terminal")
	monkeypatch.delenv("KEY_B", raising=False)
	p = tmp_path / ".env"
	p.write_text('# chú thích\nKEY_A=tu_file\nKEY_B="abc"\n', encoding="utf-8")
	tr.load_dotenv(p)
	import os
	assert os.environ["KEY_A"] == "tu_terminal" and os.environ["KEY_B"] == "abc"


def test_classify_phan_biet_het_luot_ngay_voi_gioi_han_phut():
	# Tình huống thật: Google trả 429 cho cả hết lượt/ngày lẫn quá nhanh/phút; xử lý hai cái ngược nhau.
	# Phần giả lập: body 429 thật (PerDay, chờ 82530s) và một 429 theo phút (chờ 20s).
	# Đỏ thì hỏng gì: hết lượt ngày mà vẫn thử lại (đốt lượt), hoặc giới hạn phút mà dừng cả video.
	assert tr.classify(FakeApiError(429, DAY_QUOTA_BODY))[:2] == ("day", 82530.0)
	minute = [{"error": {"details": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"},
		{"retryDelay": "20s"}]}}]
	assert tr.classify(FakeApiError(429, minute))[:2] == ("minute", 20.0)
	assert tr.classify(FakeApiError(503))[0] == "transient"
	assert tr.classify(FakeApiError(401))[0] == "fatal"

	class APITimeoutError(Exception):
		pass
	assert tr.classify(APITimeoutError("Request timed out."))[0] == "transient"


def test_het_luot_ngay_thi_dung_ngay_chi_ton_mot_luot():
	# Tình huống thật: 2026-10-29 SDK thử lại 503 nhiều lần đã đốt hết 20 lượt/ngày của gemini-3.8-flash.
	# Phần giả lập: lần gọi đầu nhận 429 hết lượt ngày.
	# Đỏ thì hỏng gì: code tiếp tục gọi, mỗi lần gọi đều mất lượt mà không bao giờ thành công.
	cfg = dict(CFG, max_retries=4)
	client = FakeClient([FakeApiError(429, DAY_QUOTA_BODY)])
	sleeps = []
	t = tr.Translator(cfg, client, src_lang="zh", sleep=sleeps.append, log=lambda *_: None)
	with pytest.raises(tr.ApiError, match="hết lượt miễn phí trong ngày, còn khoảng 22.9 giờ"):
		t.batch(["一。"], context=[])
	assert len(client.calls) == 1 and sleeps == []


def test_loi_tam_thoi_thu_lai_co_in_ra_va_gian_dan():
	# Tình huống thật: Gemini quá tải trả 503 vài lần rồi lại chạy.
	# Phần giả lập: hai lần 503, lần ba trả kết quả đúng.
	# Đỏ thì hỏng gì: chờ im lặng (trông như treo), hoặc bỏ cuộc dù chỉ cần chờ thêm chút.
	cfg = dict(CFG, max_retries=4)
	client = FakeClient([FakeApiError(503), FakeApiError(503), '["Một."]'])
	sleeps, logs = [], []
	t = tr.Translator(cfg, client, src_lang="zh", sleep=sleeps.append, log=logs.append)
	assert t.batch(["一。"], context=[]) == ["Một."]
	assert sleeps == [5, 10] and len(logs) == 2 and "503" in logs[0]
	assert t.requests == 3 and t.failed == 2


def test_cho_qua_max_wait_thi_dung_khong_ngoi_cho():
	# Tình huống thật: nhà cung cấp bảo chờ rất lâu cho một lỗi không phải hết lượt ngày.
	# Phần giả lập: 429 theo phút nhưng retryDelay 600s, max_wait_sec 60.
	# Đỏ thì hỏng gì: chương trình ngồi chờ 10 phút mà không ai biết vì sao.
	minute = [{"error": {"details": [{"retryDelay": "600s"}]}}]
	client = FakeClient([FakeApiError(429, minute)])
	t = tr.Translator(dict(CFG, max_retries=4), client, src_lang="zh", sleep=lambda *_: None, log=lambda *_: None)
	with pytest.raises(tr.ApiError, match="quá max_wait_sec=60"):
		t.batch(["一。"], context=[])