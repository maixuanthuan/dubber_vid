"""Stage dịch: điền câu tiếng Việt cho từng câu gốc qua LLM, dùng API kiểu OpenAI.

Một implementation cho mọi nhà cung cấp có endpoint tương thích OpenAI (Gemini, DeepSeek, Groq,
OpenRouter...). Địa chỉ, tên model và tên biến môi trường chứa key nằm trong file config
(configs/translator_*.json); bản thân key nằm trong .env, không bao giờ trong code hay config.

Dịch theo lô, mỗi lô kèm vài cặp câu đã dịch ngay trước làm ngữ cảnh để giữ xưng hô và tên
riêng nhất quán. Model phải trả về JSON đúng số câu; sai thì thử lại lô, vẫn sai thì dịch từng
câu, vẫn hỏng thì DỪNG. Không bao giờ lấy câu gốc nhét vào chỗ bản dịch.
"""
import json
import os
import re
import time
from pathlib import Path

REQUIRED = ("name", "base_url", "model", "api_key_env", "batch_size", "context_pairs", "max_retries",
	"temperature", "timeout_sec", "max_wait_sec")
LANG_NAMES = {"zh": "tiếng Trung", "ko": "tiếng Hàn", "en": "tiếng Anh", "vi": "tiếng Việt"}


class TranslateError(RuntimeError):
	pass


class ApiError(TranslateError):
	"""Lỗi từ phía nhà cung cấp (hết lượt, sai key, sai model): thử lại không giúp gì."""


def load_config(path):
	"""Config phải đủ mục, không có mặc định ngầm (CACH_LAM_VIEC 5.4)."""
	cfg = json.loads(Path(path).read_text(encoding="utf-8"))
	missing = [k for k in REQUIRED if k not in cfg]
	if missing:
		raise TranslateError(f"config {path} thiếu mục: {missing}")
	if not (isinstance(cfg["batch_size"], int) and cfg["batch_size"] >= 1):
		raise TranslateError(f"config {path}: batch_size phải là số nguyên >= 1, đang là {cfg['batch_size']!r}")
	return cfg


def load_dotenv(path):
	"""Đọc KEY=VALUE từ .env vào os.environ (không ghi đè biến đã có). Viết tay cho khỏi thêm thư viện."""
	path = Path(path)
	if not path.is_file():
		return
	for line in path.read_text(encoding="utf-8").splitlines():
		line = line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, _, value = line.partition("=")
		os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def system_prompt(src_lang, tgt_lang):
	src, tgt = LANG_NAMES.get(src_lang, src_lang), LANG_NAMES.get(tgt_lang, tgt_lang)
	return (
		f"Bạn dịch lời người kể chuyện trong video từ {src} sang {tgt} để lồng tiếng.\n"
		"Quy tắc:\n"
		f"1. Văn nói tự nhiên của người {tgt}, đọc lên trôi chảy.\n"
		"2. Giữ nhất quán xưng hô và cách viết tên riêng trong suốt video, theo các câu đã dịch trước.\n"
		"3. Thơ, thành ngữ, điển cố: dịch theo nghĩa, giữ nhịp và chất văn, không dịch từng chữ.\n"
		"4. Mỗi câu gốc dịch thành đúng một câu. Không gộp, không tách, không thêm giải thích.\n"
		"5. Chỉ trả về một mảng JSON các chuỗi, đúng thứ tự, đúng số lượng câu được yêu cầu. Không thêm chữ nào khác."
	)


def batch_prompt(texts, context):
	parts = []
	if context:
		parts.append("Các câu NGAY TRƯỚC đã dịch (chỉ để giữ nhất quán, KHÔNG dịch lại):")
		parts += [f"- {s} → {t}" for s, t in context]
		parts.append("")
	parts.append(f"Dịch {len(texts)} câu sau, trả về mảng JSON đúng {len(texts)} chuỗi:")
	parts.append(json.dumps(texts, ensure_ascii=False))
	return "\n".join(parts)


def parse_json_array(raw, n):
	"""Lấy mảng JSON đúng n chuỗi khác rỗng từ phản hồi; model hay bọc trong ```json ... ```."""
	text = (raw or "").strip()
	m = re.search(r"\[.*\]", text, flags=re.S)
	if not m:
		raise TranslateError(f"phản hồi không có mảng JSON: {text[:200]!r}")
	try:
		arr = json.loads(m.group(0))
	except json.JSONDecodeError as exc:
		raise TranslateError(f"mảng JSON hỏng ({exc}): {text[:200]!r}") from exc
	if not isinstance(arr, list) or len(arr) != n:
		raise TranslateError(f"cần {n} câu, nhận {len(arr) if isinstance(arr, list) else type(arr).__name__}")
	if not all(isinstance(x, str) and x.strip() for x in arr):
		raise TranslateError("có câu dịch rỗng hoặc không phải chuỗi")
	return [x.strip() for x in arr]


def make_client(cfg):
	from openai import OpenAI

	key = os.environ.get(cfg["api_key_env"], "").strip()
	if not key:
		raise TranslateError(f"chưa có key: đặt {cfg['api_key_env']}=... trong file .env ở thư mục dự án")
	# max_retries=0: tắt cơ chế tự thử lại của SDK. Nó thử lại im lặng, và mỗi lần thử đều trừ
	# lượt: 2026-10-29 các lần thử lại 503 đã đốt hết 20 lượt/ngày của gemini-3.8-flash.
	return OpenAI(base_url=cfg["base_url"], api_key=key, max_retries=0, timeout=cfg["timeout_sec"])


# Bắt cả dạng JSON ("retryDelay": "20s") lẫn dạng repr Python ('retryDelay': '20s') mà SDK hay in.
_RETRY_DELAY = re.compile(r"""retryDelay['"]?\s*[:=]\s*['"]?(\d+(?:\.\d+)?)s""")


def classify(exc):
	"""Phân loại lỗi gọi API -> (loại, số giây nhà cung cấp bảo chờ hoặc None, chi tiết).

	day       : hết lượt trong ngày (429 kèm quota PerDay)  -> dừng, thử lại chỉ đốt lượt
	minute    : giới hạn ngắn hạn (429 khác)                 -> chờ đúng thời gian được bảo
	transient : 5xx, timeout, đứt kết nối                    -> thử lại, giãn dần
	fatal     : sai key, sai model, request sai (4xx khác)   -> dừng
	Đọc theo thuộc tính (status_code, body) chứ không theo lớp, để test bằng lỗi giả được."""
	status = getattr(exc, "status_code", None)
	body = getattr(exc, "body", None)
	text = f"{exc} {json.dumps(body, ensure_ascii=False, default=str) if body is not None else ''}"
	m = _RETRY_DELAY.search(text)
	delay = float(m.group(1)) if m else None
	detail = re.sub(r"\s+", " ", text).strip()[:400]
	if status == 429:
		return ("day" if "PerDay" in text else "minute"), delay, detail
	name = type(exc).__name__
	if (isinstance(status, int) and status >= 500) or "Timeout" in name or "Connection" in name:
		return "transient", delay, detail
	return "fatal", delay, detail


class Translator:
	def __init__(self, cfg, client, src_lang, tgt_lang="vi", sleep=time.sleep, log=print):
		self.cfg, self.client = cfg, client
		self.system = system_prompt(src_lang, tgt_lang)
		self.sleep, self.log = sleep, log
		self.prompt_tokens = 0
		self.completion_tokens = 0
		self.requests = 0  # mọi lần gọi, kể cả lần lỗi: lần lỗi cũng có thể bị trừ lượt
		self.failed = 0

	def _call(self, messages):
		"""Gọi API, tự thử lại theo loại lỗi (xem classify). In ra từng lần thử, không im lặng."""
		who = f"{self.cfg['name']} ({self.cfg['model']})"
		retries = self.cfg["max_retries"]
		for attempt in range(retries + 1):
			self.requests += 1
			t0 = time.perf_counter()
			try:
				return self.client.chat.completions.create(
					model=self.cfg["model"], temperature=self.cfg["temperature"], messages=messages)
			except Exception as exc:
				self.failed += 1
				kind, delay, detail = classify(exc)
				took = time.perf_counter() - t0
				if kind == "day":
					hours = f", còn khoảng {delay / 3600:.1f} giờ mới có lượt lại" if delay else ""
					raise ApiError(f"{who}: hết lượt miễn phí trong ngày{hours}. Đổi config sang model khác "
						f"hoặc chờ. Chi tiết: {detail}") from exc
				if kind == "fatal" or attempt == retries:
					why = "lỗi không thử lại được" if kind == "fatal" else f"hết {retries} lần thử lại"
					raise ApiError(f"{who}: {why}. {type(exc).__name__} sau {took:.1f}s: {detail}") from exc
				wait = delay if delay is not None else min(2 ** attempt * 5, self.cfg["max_wait_sec"])
				if wait > self.cfg["max_wait_sec"]:
					raise ApiError(f"{who}: nhà cung cấp bảo chờ {wait:.0f}s, quá max_wait_sec="
						f"{self.cfg['max_wait_sec']}. Chi tiết: {detail}") from exc
				self.log(f"[translate] {type(exc).__name__} ({kind}) sau {took:.1f}s, chờ {wait:.0f}s rồi thử lại "
					f"({attempt + 1}/{retries}): {detail[:160]}")
				self.sleep(wait)

	def _ask(self, texts, context):
		resp = self._call([{"role": "system", "content": self.system},
			{"role": "user", "content": batch_prompt(texts, context)}])
		usage = getattr(resp, "usage", None)
		if usage is not None:
			self.prompt_tokens += usage.prompt_tokens or 0
			self.completion_tokens += usage.completion_tokens or 0
		return parse_json_array(resp.choices[0].message.content, len(texts))

	def batch(self, texts, context):
		"""Lô -> thử lại lô một lần -> dịch từng câu. ApiError thì dừng luôn, không thử tiếp."""
		errors = []
		for _ in range(2):
			try:
				return self._ask(texts, context)
			except ApiError:
				raise
			except TranslateError as exc:
				errors.append(str(exc))
		out = []
		for t in texts:
			try:
				out.append(self._ask([t], context)[0])
			except ApiError:
				raise
			except TranslateError as exc:
				raise TranslateError(f"không dịch được câu {t!r} sau khi thử theo lô ({'; '.join(errors)}): {exc}") from exc
		return out


def translate_segments(segments, translator, cfg, save, log=print):
	"""Điền s['tgt'] cho các câu chưa có. Gọi save() sau mỗi lô để chạy lại thì dịch tiếp chỗ dở."""
	todo = [i for i, s in enumerate(segments) if not s.get("tgt")]
	size = cfg["batch_size"]
	for b in range(0, len(todo), size):
		idx = todo[b:b + size]
		first = idx[0]
		context = [(s["src"], s["tgt"]) for s in segments[max(0, first - cfg["context_pairs"]):first] if s.get("tgt")]
		out = translator.batch([segments[i]["src"] for i in idx], context)
		for i, t in zip(idx, out):
			segments[i]["tgt"] = t
		save()
		log(f"[translate] lô {b // size + 1}: câu {idx[0]}–{idx[-1]} xong ({translator.requests} request)")
	return len(todo)