import os
import re
import sys
import io
import json
import html
import asyncio
import base64
import difflib
import math
import threading
import time as _time
from datetime import datetime, timedelta, time as dtime
from http.server import HTTPServer, BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

import pandas as pd
import gspread
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ----------------- ФОНОВИЙ ВЕБ-СЕРВЕР ДЛЯ RENDER -----------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()


threading.Thread(target=run_health_check_server, daemon=True).start()

# ----------------- НАЛАШТУВАННЯ -----------------
# Токен береться зі змінної середовища TELEGRAM_BOT_TOKEN (Render -> Environment).
# Якщо змінної немає — встав токен між лапками нижче.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "ВСТАВ_СЮДИ_ТОКЕН"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")      # потрібен ТІЛЬКИ для пошуку по фото цінника
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "шини 2026")

# Склад для звірки: за замовчуванням аркуш «Склад» у цій самій Google Таблиці
# (колонки: Артикул | Назва | Кількість). Можна змінити назву аркуша змінною SKLAD_SHEET
# або вказати окрему таблицю змінною SKLAD_SPREADSHEET (назва, посилання або ID).
SKLAD_SPREADSHEET = os.environ.get("SKLAD_SPREADSHEET", "").strip()
SKLAD_SHEET = os.environ.get("SKLAD_SHEET", "").strip() or "Склад"

# Необов'язково: чати для щоденного звіту й сповіщень про мінімум (через кому). Свій ID: команда /myid
REPORT_CHAT_IDS = [int(x) for x in re.findall(r"-?\d+", os.environ.get("REPORT_CHAT_IDS", ""))]
REPORT_TIME = os.environ.get("REPORT_TIME", "08:30")        # щоденний звіт, київський час
LOW_STOCK = int(os.environ.get("LOW_STOCK", "2"))           # мінімальний залишок по артикулу (разом)

FIRST_DATA_ROW = 4      # дані в таблиці починаються з 4-го рядка
MAX_CARDS = 6           # скільки карток показувати одразу (інакше — список кнопок)
MAX_LIST = 40           # скільки артикулів у списку кнопок
MAX_QTY = 999           # більше — схоже, що замість кількості вписали артикул
SANE_QTY = 5000         # у звітах ігноруємо записи історії з більшими значеннями
CACHE_TTL = 45          # секунд, кеш таблиці
SKLAD_TTL = 120
HISTORY_TTL = 60
UNDO_WINDOW = 15 * 60   # секунд, скільки діє кнопка "Відмінити"
KYIV = ZoneInfo("Europe/Kyiv")

openai_client = None
if OPENAI_API_KEY:
    try:
        import openai
        openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"⚠️ OpenAI недоступний, фото-пошук вимкнено: {e}")

SPECIAL_LOCS = {          # короткі ключі для кнопок -> назви в таблиці
    "zal": "вітрина зал",
    "vul": "вітрина вулиця",
    "fas": "палети фасад",
}


class StaleRow(Exception):
    """Рядок у таблиці змінився (хтось видалив/перемістив рядки)."""


# ----------------- GOOGLE ТАБЛИЦЯ + КЕШ -----------------
_lock = threading.RLock()
_gc = None
_spreadsheet = None
_cache = {"df": None, "ts": 0.0}
_sklad_cache = {"data": None, "ts": 0.0}
_hist_cache = {"rows": None, "ts": 0.0}
_meta = {"blank_rows": []}


def get_client():
    global _gc
    with _lock:
        if _gc is None:
            creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
            if creds_json:
                _gc = gspread.service_account_from_dict(json.loads(creds_json))
            else:
                _gc = gspread.service_account(filename="credentials.json")
        return _gc


def service_email():
    try:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            return json.loads(creds_json).get("client_email", "")
        with open("credentials.json", encoding="utf-8") as f:
            return json.load(f).get("client_email", "")
    except Exception:
        return ""


def get_spreadsheet():
    global _spreadsheet
    with _lock:
        if _spreadsheet is None:
            _spreadsheet = get_client().open(SPREADSHEET_NAME)
        return _spreadsheet


def get_worksheet():
    return get_spreadsheet().sheet1  # перший аркуш ("Шини")


def to_int(v):
    s = str(v).strip().replace(",", ".")
    try:
        return int(float(s))
    except Exception:
        return 0


def load_df(force=False):
    """Таблиця «Шини» як DataFrame. Кешується на CACHE_TTL секунд."""
    with _lock:
        now = _time.time()
        if not force and _cache["df"] is not None and now - _cache["ts"] < CACHE_TTL:
            return _cache["df"].copy()
    data = get_worksheet().get_all_values()
    rows, blank, last_filled = [], [], 0
    for i, r in enumerate(data[FIRST_DATA_ROW - 1:], start=FIRST_DATA_ROW):
        r = (list(r) + [""] * 5)[:5]
        rows.append([i] + [str(x).strip() for x in r])
        if str(r[1]).strip():
            last_filled = i
        elif not any(str(x).strip() for x in r):
            blank.append(i)
    with _lock:
        _meta["blank_rows"] = [b for b in blank if b < last_filled]
    df = pd.DataFrame(rows, columns=["row", "pallet", "code", "size", "name", "qty"])
    df["code"] = df["code"].str.replace(r"\.0$", "", regex=True)
    df = df[df["code"] != ""].copy()
    df["q"] = df["qty"].apply(to_int)
    with _lock:
        _cache["df"] = df
        _cache["ts"] = _time.time()
    return df.copy()


def get_meta():
    with _lock:
        return dict(_meta)


def invalidate_cache():
    with _lock:
        _cache["ts"] = 0.0
        _hist_cache["ts"] = 0.0


def cache_set_qty(row, qty):
    with _lock:
        df = _cache["df"]
        if df is not None:
            df.loc[df["row"] == row, "qty"] = str(qty)
            df.loc[df["row"] == row, "q"] = int(qty)


def cached_code_of_row(row):
    with _lock:
        df = _cache["df"]
        if df is None:
            return None
        m = df[df["row"] == row]
        return None if m.empty else str(m.iloc[0]["code"])


# ----------------- СКЛАД (необов'язково) -----------------
_sklad_meta = {"rows": 0, "dups": [], "asof": None}

_D1 = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?:\D{0,3}(\d{1,2})[:.](\d{2}))?")
_D2 = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?:[ T]+(\d{1,2}):(\d{2}))?")


def find_datetime(text):
    """Шукає дату (31.10.2026, 31.10.2026 08:30, 2026-10-31) у тексті; повертає datetime або None."""
    try:
        m = _D2.search(str(text))
        if m:
            y, mo, d, hh, mm = m.groups()
        else:
            m = _D1.search(str(text))
            if not m:
                return None
            d, mo, y, hh, mm = m.groups()
        return datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0))
    except Exception:
        return None


def load_sklad(force=False):
    """dict: артикул -> {'name', 'qty'}; None, якщо аркуша зі складом немає. Дублі артикулів підсумовуються."""
    with _lock:
        if not force and _time.time() - _sklad_cache["ts"] < SKLAD_TTL and _sklad_cache["ts"] > 0:
            return _sklad_cache["data"]
    try:
        if SKLAD_SPREADSHEET:
            gc = get_client()
            m = re.search(r"/d/([A-Za-z0-9_-]+)", SKLAD_SPREADSHEET)
            if m:
                sh = gc.open_by_key(m.group(1))
            elif re.fullmatch(r"[A-Za-z0-9_-]{30,}", SKLAD_SPREADSHEET):
                sh = gc.open_by_key(SKLAD_SPREADSHEET)
            else:
                sh = gc.open(SKLAD_SPREADSHEET)
            ws = sh.worksheet(SKLAD_SHEET) if os.environ.get("SKLAD_SHEET", "").strip() else sh.sheet1
        else:
            ws = get_spreadsheet().worksheet(SKLAD_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        with _lock:
            _sklad_cache["data"], _sklad_cache["ts"] = None, _time.time()
        return None
    result, seen, dups, n_rows, asof = {}, {}, [], 0, None
    for r in ws.get_all_values():
        cells = [str(c).strip() for c in r]
        code_idx = next((i for i, s_ in enumerate(cells) if re.fullmatch(r"\d{6,9}(\.0)?", s_)), None)
        if code_idx is None:
            txt = " ".join(cells)
            dt = find_datetime(txt)
            if dt and (asof is None or "станом" in txt.lower()):
                asof = dt
            continue
        code = cells[code_idx].split(".")[0]
        qty = None
        for i in range(len(cells) - 1, -1, -1):
            if i != code_idx and re.fullmatch(r"-?\d+([.,]0+)?", cells[i]):
                qty = to_int(cells[i])
                break
        texts = [s_ for i, s_ in enumerate(cells) if i != code_idx and s_ and not re.fullmatch(r"-?[\d.,]+", s_)]
        name = max(texts, key=len) if texts else ""
        n_rows += 1
        e = result.setdefault(code, {"name": name, "qty": 0})
        e["qty"] += qty or 0
        if not e["name"]:
            e["name"] = name
        seen[code] = seen.get(code, 0) + 1
        if seen[code] == 2:
            dups.append(code)
    with _lock:
        _sklad_cache["data"], _sklad_cache["ts"] = result, _time.time()
        _sklad_meta["rows"], _sklad_meta["dups"], _sklad_meta["asof"] = n_rows, dups, asof
    return result


def load_sklad_safe(force=False):
    try:
        data = load_sklad(force)
        return data if data else None
    except Exception as e:
        print(f"⚠️ Не вдалося прочитати склад: {e}")
        return None


# ----------------- ІСТОРІЯ -----------------
def now_str():
    return datetime.now(KYIV).strftime("%Y-%m-%d %H:%M:%S")


def user_label(user):
    return f"@{user.username}" if user.username else f"{user.first_name} (ID: {user.id})"


ACTION_TEXT = {
    "inc": "Додано (+1)",
    "dec": "Зменшено (-1)",
    "add": "Додано нову шину",
    "merge": "Додано кількість до наявної",
    "undo": "Скасовано зміну",
    "inv": "Інвентаризація",
    "invdone": "Інвентаризація завершена",
    "move_out": "Переміщення (−)",
    "move_in": "Переміщення (+)",
    "set": "Встановлено точну кількість",
}


def _append_history(rows):
    """Дописує рядки в «Історію» (7 колонок, а якщо аркуш вужчий — 6)."""
    history = get_spreadsheet().worksheet("Історія")
    try:
        history.append_rows(rows, value_input_option="USER_ENTERED")
    except Exception:
        history.append_rows([r[:6] for r in rows], value_input_option="USER_ENTERED")
    _hist_cache["ts"] = 0.0


def log_to_history(user, code, action, old_qty, new_qty, loc=""):
    try:
        _append_history([[now_str(), user_label(user), str(code), ACTION_TEXT.get(action, ACTION_TEXT["set"]), old_qty, new_qty, loc_label(loc) if loc else ""]])
    except Exception as e:
        print(f"⚠️ Помилка запису в історію: {e}")


def log_inventory_done(user, label, checked, changed):
    try:
        _append_history([[now_str(), user_label(user), "—", ACTION_TEXT["invdone"], checked, changed, label]])
    except Exception as e:
        print(f"⚠️ Помилка запису в історію: {e}")


def read_history():
    """Список записів історії: dict(ts, user, code, action, old, new, loc, done)."""
    with _lock:
        if _hist_cache["rows"] is not None and _time.time() - _hist_cache["ts"] < HISTORY_TTL:
            return _hist_cache["rows"]
    try:
        data = get_spreadsheet().worksheet("Історія").get_all_values()
    except Exception as e:
        print(f"⚠️ Не вдалося прочитати історію: {e}")
        return []
    out = []
    for r in data[1:]:
        r = (list(r) + [""] * 7)[:7]
        try:
            ts = datetime.strptime(r[0].strip(), "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        out.append({"ts": ts, "user": r[1].strip(), "code": r[2].strip(), "action": r[3].strip(),
                    "old": to_int(r[4]), "new": to_int(r[5]), "loc": r[6].strip(),
                    "done": r[3].strip() == ACTION_TEXT["invdone"]})
    with _lock:
        _hist_cache["rows"] = out
        _hist_cache["ts"] = _time.time()
    return out


# ----------------- ЗАПИС У ТАБЛИЦЮ -----------------
def change_qty(user, row, action, value=None, log=True):
    ws = get_worksheet()
    vals = (ws.row_values(row) + [""] * 5)[:5]
    code = str(vals[1]).strip().replace(".0", "")
    expected = cached_code_of_row(row)
    if not code or (expected is not None and expected != code):
        invalidate_cache()
        raise StaleRow()
    old = to_int(vals[4])
    if action == "inc":
        new = old + 1
    elif action == "dec":
        new = max(0, old - 1)
    else:
        new = int(value)
    ws.update_cell(row, 5, new)
    cache_set_qty(row, new)
    if log:
        log_to_history(user, code, action, old, new, vals[0])
    return code, old, new


def apply_qty_batch(user, changes, action="inv", loc=""):
    """changes: список (row, code, old, new). Один запит на запис і один на історію."""
    if not changes:
        return 0
    ws = get_worksheet()
    ws.batch_update(
        [{"range": f"E{row}", "values": [[new]]} for row, _code, _old, new in changes],
        value_input_option="USER_ENTERED",
    )
    for row, _code, _old, new in changes:
        cache_set_qty(row, new)
    try:
        ts, who = now_str(), user_label(user)
        _append_history([[ts, who, str(code), ACTION_TEXT[action], old, new, loc] for _row, code, old, new in changes])
    except Exception as e:
        print(f"⚠️ Помилка запису в історію: {e}")
    return len(changes)


def add_tire(user, pallet, code, size, name, qty, action="add"):
    ws = get_worksheet()
    next_row = max(len(ws.col_values(2)) + 1, FIRST_DATA_ROW)  # перший вільний рядок після останнього артикула
    if ws.row_count < next_row:
        ws.add_rows(next_row - ws.row_count)
    ws.update(
        range_name=f"A{next_row}:E{next_row}",
        values=[[pallet, code, size, name, qty]],
        value_input_option="USER_ENTERED",
    )
    saved = (ws.row_values(next_row) + [""] * 5)[:5]
    if str(saved[1]).strip() != str(code).strip():
        raise RuntimeError(f"запис не з'явився в рядку {next_row}")
    invalidate_cache()
    log_to_history(user, code, action, 0, qty, pallet)
    return next_row


def merge_qty(user, row, extra):
    ws = get_worksheet()
    vals = (ws.row_values(row) + [""] * 5)[:5]
    code = str(vals[1]).strip().replace(".0", "")
    old = to_int(vals[4])
    new = old + int(extra)
    ws.update_cell(row, 5, new)
    cache_set_qty(row, new)
    log_to_history(user, code, "merge", old, new, vals[0])
    return code, old, new


# ----------------- ДОПОМІЖНЕ -----------------
def short_name(name):
    return re.sub(r"^\s*Автошина\s+", "", str(name)).strip()


def loc_label(pallet):
    p = str(pallet).strip()
    return f"палета {p}" if p.isdigit() else (p or "без локації")


def loc_sort_key(p):
    p = str(p).strip()
    return (0, int(p), "") if p.isdigit() else (1, 0, p)


def friendly_error(e):
    print(f"⚠️ Помилка: {type(e).__name__}: {e}")
    text = str(e)
    if isinstance(e, StaleRow):
        return "🔄 Таблиця змінилась, поки ти працював. Знайди шину ще раз і повтори дію."
    if "429" in text or "Quota" in text or "quota" in text:
        return "⏳ Google просить трохи зачекати. Спробуй ще раз за хвилину."
    if "403" in text or "PERMISSION" in text.upper():
        return "🔒 Немає доступу до таблиці. Перевір, що їй надано доступ для сервісного акаунта."
    if "404" in text or "SpreadsheetNotFound" in type(e).__name__:
        return "❓ Не знайшов таблицю. Перевір назву в налаштуваннях бота."
    return "⚠️ Щось пішло не так. Спробуй ще раз; якщо повториться, напиши адміністратору."


def chunk_text(text, limit=3900):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


def get_tire_photo(name):
    photos_dir = "photo"
    if not os.path.exists(photos_dir):
        return None
    name_lower = str(name).lower()
    for filename in os.listdir(photos_dir):
        if filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".avif")):
            keyword = os.path.splitext(filename)[0].lower()
            if len(keyword) >= 3 and keyword in name_lower:
                return os.path.join(photos_dir, filename)
    return None


# ----------------- ПОШУК -----------------
BRAND_ALIASES = {
    "петлас": "petlas", "хенкук": "hankook", "ханкук": "hankook", "хенкок": "hankook",
    "тріангл": "triangle", "трайангл": "triangle", "триангл": "triangle",
    "маршал": "marshal", "росава": "rosava", "аосен": "aosen", "хайфлай": "hifly", "хіфлай": "hifly",
    "преміорі": "premiorri", "премиорри": "premiorri", "премьорі": "premiorri", "естрада": "estrada",
    "горизонт": "horizon", "хорайзон": "horizon", "павертрак": "powertrac", "пауертрак": "powertrac",
    "матадор": "matador", "мішлен": "michelin", "бріджстоун": "bridgestone", "нокіан": "nokian",
}
WORD_ALIASES = {"зим": "зима", "літ": "літо", "всесез": "всесезон"}
SIZE_RE = re.compile(r"(?<!\d)(\d{3})\s*[/\-\s.xх]?\s*(\d{2})\s*[/\-\s]?\s*[rрzZ]?\s*(\d{2})(?!\d)", re.I)


def norm(s):
    """Лишає тільки літери й цифри: '205/55 R16' -> '20555r16'."""
    return re.sub(r"[^0-9a-zа-яіїєґ]", "", str(s).lower())


def alias(t):
    for k, v in BRAND_ALIASES.items():
        if t.startswith(k):
            return v
    for k, v in WORD_ALIASES.items():
        if t.startswith(k):
            return v
    return t


def parse_query(q):
    q = str(q).strip().lower()
    size_key = None
    m = SIZE_RE.search(q)
    if m:
        size_key = f"{m.group(1)}{m.group(2)}r{m.group(3)}"
        q = (q[:m.start()] + " " + q[m.end():]).strip()
    tokens = []
    for t in re.split(r"\s+", q):
        t = t.strip(",.;:()[]\"'")
        if not t:
            continue
        if re.search(r"\d", t):
            t = t.replace("р", "r")
        tokens.append(alias(t))
    return size_key, tokens


def _match(df, size_key, tokens):
    hay = (df["code"] + " " + df["size"] + " " + df["name"]).str.lower()
    hay_c = hay.apply(norm)
    mask = pd.Series(True, index=df.index)
    if size_key:
        mask &= hay_c.str.contains(size_key, regex=False)
    for t in tokens:
        mask &= hay.str.contains(t, regex=False) | hay_c.str.contains(norm(t) or t, regex=False)
    return df[mask]


def fuzzy_fix(df, tokens):
    vocab = set()
    for s in df["name"].tolist():
        for w in re.findall(r"[A-Za-z][A-Za-z0-9+]{3,}", s):
            vocab.add(w.lower())
    vocab_l = sorted(vocab)
    fixed, changes = [], []
    for t in tokens:
        if len(t) < 4 or any(ch.isdigit() for ch in t) or any(t in v for v in vocab_l):
            fixed.append(t)
            continue
        m = difflib.get_close_matches(t, vocab_l, n=1, cutoff=0.72)
        if m:
            fixed.append(m[0])
            changes.append((t, m[0]))
        else:
            fixed.append(t)
    return fixed, changes


def search_codes(df, query):
    """Повертає (список артикулів, примітка)."""
    if df.empty:
        return [], ""
    size_key, tokens = parse_query(query)
    if not size_key and not tokens:
        return [], ""
    found = _match(df, size_key, tokens)
    note = ""
    if found.empty and tokens:
        fixed, changes = fuzzy_fix(df, tokens)
        if changes:
            found = _match(df, size_key, fixed)
            if not found.empty:
                note = "🔎 Шукаю " + ", ".join(f"«{b}» замість «{a}»" for a, b in changes)
    if found.empty:
        digits = re.sub(r"\D", "", str(query))
        if len(digits) >= 4:
            found = df[df["code"].str.contains(digits, regex=False)]
    codes = list(dict.fromkeys(found["code"].tolist()))
    q = str(query).strip()
    if q in codes:  # точний збіг артикула — першим
        codes.remove(q)
        codes.insert(0, q)
    return codes, note


# ----------------- КАРТКА ТОВАРУ -----------------
def code_rows(df, code):
    rows = df[df["code"] == code].copy()
    rows["_k"] = rows["pallet"].apply(loc_sort_key)
    return rows.sort_values(by=["_k", "row"]).drop(columns=["_k"])


def code_total(df, code):
    return int(df[df["code"] == code]["q"].sum())


def build_card(code, df, sk=None, undo_row=None):
    rows = code_rows(df, code)
    if rows.empty:
        return f"❓ Артикул {code} не знайдено в таблиці.", None
    first = rows.iloc[0]
    names = {norm(short_name(n)) for n in rows["name"]}
    sizes = {norm(s) for s in rows["size"] if s}
    total = int(rows["q"].sum())
    labels = [loc_label(p) for p in rows["pallet"]]
    dup = {l for l in labels if labels.count(l) > 1}
    ptot = pallet_totals(df)

    lines, kb = [], []
    for (_, r), lab in zip(rows.iterrows(), labels):
        row = int(r["row"])
        shown = f"{lab} (р.{row})" if lab in dup else lab
        q = int(r["q"])
        lines.append(f"• {shown} — {q} шт.{load_tag(r['pallet'], ptot)}")
        kb.append([
            InlineKeyboardButton("➖", callback_data=f"dec_{row}"),
            InlineKeyboardButton(f"{shown}: {q} шт.", callback_data=f"set_{row}"),
            InlineKeyboardButton("➕", callback_data=f"inc_{row}"),
        ])

    text = (
        f"🛞 {short_name(first['name'])}\n"
        f"🔢 Артикул: {code}   📏 {first['size'] or '—'}\n\n"
        "📍 Де лежить:\n" + "\n".join(lines) + "\n\n"
        f"📦 Разом у таблиці: {total} шт."
    )
    if sk is not None:
        if code in sk:
            diff = total - sk[code]["qty"]
            text += f"\n🏬 Склад: {sk[code]['qty']} шт. — " + ("✅ збігається" if diff == 0 else f"⚠️ у таблиці {diff:+d}")
        else:
            text += "\n🏬 Склад: такого артикула немає в базі"
    if total == 0:
        text += "\n⚠️ Немає в наявності"
    elif total <= LOW_STOCK:
        text += f"\n🔻 Мінімальний залишок (≤ {LOW_STOCK} шт. разом)"
    if len(names) > 1 or len(sizes) > 1:
        text += "\n⚠️ У рядках різні назви або розміри: варто перевірити"

    if total > 0:
        kb.append([InlineKeyboardButton("🔁 Перемістити", callback_data=f"mv_{code}")])
    footer = []
    if undo_row:
        footer.append(InlineKeyboardButton("↩️ Відмінити", callback_data=f"undo_{undo_row}"))
    footer.append(InlineKeyboardButton("🔄 Оновити", callback_data=f"rf_{code}"))
    kb.append(footer)
    return text, InlineKeyboardMarkup(kb)


def undo_row_for(context, df, code):
    last = context.application.bot_data.get("last", {})
    rows = set(df[df["code"] == code]["row"].astype(int).tolist())
    now = _time.time()
    cands = [(v["ts"], r) for r, v in last.items() if r in rows and now - v["ts"] < UNDO_WINDOW]
    return max(cands)[1] if cands else None


async def card_payload(context, code, force=False):
    df = await asyncio.to_thread(load_df, force)
    sk = await asyncio.to_thread(load_sklad_safe)
    return build_card(code, df, sk, undo_row_for(context, df, code))


async def send_card(chat_send, context, code, force=False):
    text, kb = await card_payload(context, code, force)
    return await chat_send(text, reply_markup=kb)


def list_keyboard(df, codes):
    kb = []
    for c in codes[:MAX_LIST]:
        rows = df[df["code"] == c]
        if rows.empty:
            continue
        label = f"{c} · {rows.iloc[0]['size']} · {short_name(rows.iloc[0]['name'])}"
        kb.append([InlineKeyboardButton(f"{label[:52]} · {int(rows['q'].sum())} шт.", callback_data=f"card_{c}")])
    return InlineKeyboardMarkup(kb)


# ----------------- ЛОКАЦІЇ -----------------
def resolve_loc_key(text):
    t = str(text).strip().lower()
    if not t:
        return None
    if t.isdigit():
        return t
    m = re.fullmatch(r"(?:палета|палет|пал\.?|№)\s*(\d+)", t)
    if m:
        return m.group(1)
    for k, v in SPECIAL_LOCS.items():
        if t == k or t in v:
            return k
    return None


def loc_name_of_key(key):
    return SPECIAL_LOCS.get(key) or loc_label(key)


def location_rows(df, key):
    if key.isdigit():
        return df[df["pallet"].str.strip() == key].sort_values("row")
    name = SPECIAL_LOCS.get(key, key).lower()
    return df[df["pallet"].str.strip().str.lower() == name].sort_values("row")


def palette_keyboard(prefix):
    nums = [InlineKeyboardButton(str(n), callback_data=f"{prefix}_{n}") for n in range(1, 45)]
    kb = [nums[i:i + 6] for i in range(0, len(nums), 6)]
    kb.append([
        InlineKeyboardButton("🪟 Вітрина зал", callback_data=f"{prefix}_zal"),
        InlineKeyboardButton("🏬 Вулиця", callback_data=f"{prefix}_vul"),
        InlineKeyboardButton("🏗 Фасад", callback_data=f"{prefix}_fas"),
    ])
    return InlineKeyboardMarkup(kb)


def build_location_view(df, key):
    rows = location_rows(df, key)
    label = loc_name_of_key(key)
    if rows.empty:
        return f"📦 {label}: тут нічого не записано.", None, rows
    total = int(rows["q"].sum())
    head = f"📦 {label} — {len(rows)} позицій, {total} шт."
    if key.isdigit():
        head += f" · {load_icon(total)} {total}/{PALLET_MAX}" + (f" (понад ліміт на {total - PALLET_MAX})" if total > PALLET_MAX else f" (вільно {PALLET_MAX - total})")
    lines = [head + "\n"]
    for _, r in rows.iterrows():
        lines.append(f"• {r['code']} · {r['size']} · {short_name(r['name'])} — {int(r['q'])} шт.")
    kb = []
    if len(rows) <= 24:
        btns = [InlineKeyboardButton(f"{r['code']} · {int(r['q'])}", callback_data=f"card_{r['code']}") for _, r in rows.iterrows()]
        kb += [btns[i:i + 2] for i in range(0, len(btns), 2)]
    kb.append([InlineKeyboardButton("🧮 Інвентаризація", callback_data=f"invs_{key}"),
               InlineKeyboardButton("📄 PDF-картка", callback_data=f"pdf_{key}")])
    if key.isdigit() and total > PALLET_MAX:
        kb.append([InlineKeyboardButton("🧭 Куди перенести надлишок", callback_data=f"unl_{key}")])
    kb.append([InlineKeyboardButton("⬅️ Усі палети", callback_data="pals")])
    return "\n".join(lines), InlineKeyboardMarkup(kb), rows


# ----------------- СТАТИСТИКА, ЗВІТИ, ЗВІРКА -----------------
def history_since(hours=None, days=None):
    delta = timedelta(hours=hours) if hours else timedelta(days=days or 1)
    since = datetime.now(KYIV).replace(tzinfo=None) - delta
    return [h for h in read_history() if h["ts"] >= since and not h.get("done")]


def sane(h):
    return h["old"] <= SANE_QTY and h["new"] <= SANE_QTY


def net_of(items):
    items = [h for h in items if sane(h)]
    plus = sum(max(0, h["new"] - h["old"]) for h in items)
    minus = sum(max(0, h["old"] - h["new"]) for h in items)
    return plus, minus


def build_stats():
    df = load_df()
    total_positions = len(df)
    total_qty = int(df["q"].sum())
    zero_qty = int((df["q"] == 0).sum())

    def get_season(name):
        n = str(name).lower()
        if any(w in n for w in ["всесезон", "all weather", "all-season", "4seasons", "в.с", "в/с"]):
            return "🌦 Всесезонні"
        if any(w in n for w in ["зима", "зимов", "winter", "шип", "ice", "snow"]):
            return "❄️ Зимові"
        if any(w in n for w in ["літо", "літній", "summer"]):
            return "☀️ Літні"
        return "📦 Інші"

    def get_radius(size_str):
        m = re.search(r"R\d+", str(size_str), re.IGNORECASE)
        return m.group(0).upper() if m else "Інші"

    brands = [
        "Premiorri", "Premiori", "Rosava", "Bridgestone", "Michelin", "Nokian", "Hankook", "Continental", "Goodyear",
        "Matador", "Barum", "Cordiant", "Tigar", "Kormoran", "Sailun", "Triangle", "Windforce", "Imperial",
        "Orium", "Falken", "Dunlop", "Pirelli", "Yokohama", "Kumho", "Nexen", "BFGoodrich", "Firestone",
        "Toyo", "Lassa", "Mastersteel", "Roadstone", "Aosen", "Estrada", "Powertrac", "Petlas", "Horizon",
        "Hifly", "Marshal",
    ]

    def get_brand(name):
        n = str(name)
        for b in brands:
            if b.lower() in n.lower():
                return b
        return "Інший"

    df["season"] = df["name"].apply(get_season)
    df["radius"] = df["size"].apply(get_radius)
    df["brand"] = df["name"].apply(get_brand)
    df["loc"] = df["pallet"].apply(loc_label)
    e = html.escape

    text = (
        "📊 <b>Статистика складу шин</b>\n\n"
        f"📦 <b>Разом:</b> {total_qty} шт. ({df['code'].nunique()} артикулів, {total_positions} рядків)\n"
        f"⚠️ <b>Рядків з нульовим залишком:</b> {zero_qty}\n"
    )
    day = history_since(hours=24)
    if day:
        plus, minus = net_of(day)
        text += f"🕒 <b>За добу:</b> {len(day)} змін, +{plus} / −{minus} шт.\n"
    text += "\n❄️ <b>За сезонністю:</b>\n"
    for k, q in df.groupby("season")["q"].sum().items():
        if q > 0:
            text += f"• {k}: {int(q)} шт.\n"
    text += "\n⬡ <b>За радіусом:</b>\n"
    for k, q in sorted(df.groupby("radius")["q"].sum().items()):
        if q > 0:
            text += f"• {e(k)}: {int(q)} шт.\n"
    text += "\n🏭 <b>За виробниками:</b>\n"
    for k, q in sorted(df.groupby("brand")["q"].sum().items(), key=lambda x: x[1], reverse=True):
        if q > 0:
            text += f"• {e(k)}: {int(q)} шт.\n"
    text += "\n📍 <b>Найбільші локації:</b>\n"
    for k, q in sorted(df.groupby("loc")["q"].sum().items(), key=lambda x: x[1], reverse=True)[:6]:
        text += f"• {e(k)}: {int(q)} шт.\n"
    combo = df.groupby(["brand", "radius"])["q"].sum().reset_index()
    combo = combo[combo["q"] > 0].sort_values(by="q", ascending=False)
    text += "\n📌 <b>Топ (виробник + радіус):</b>\n"
    for _, r in combo.head(8).iterrows():
        text += f"• {e(r['brand'])} ({e(r['radius'])}): {int(r['q'])} шт.\n"
    return text


def compare_sklad(df, sk):
    tot = df.groupby("code")["q"].sum().to_dict()
    names = df.drop_duplicates("code").set_index("code")["name"].to_dict()
    out = []
    for c in set(tot) | set(sk):
        t, s = int(tot.get(c, 0)), int(sk.get(c, {}).get("qty", 0))
        if c not in sk and t == 0:
            continue
        rows = code_rows(df, c)
        where = "; ".join(f"{loc_label(r['pallet'])}: {int(r['q'])}" for _, r in rows.iterrows() if int(r["q"]) > 0) or "—"
        out.append({"code": c, "name": short_name(sk.get(c, {}).get("name") or names.get(c, "")),
                    "sklad": s, "table": t, "diff": t - s, "where": where})
    out.sort(key=lambda x: (x["diff"] == 0, -abs(x["diff"]), x["code"]))
    return out


def build_zvirka_text(rows):
    ok = [r for r in rows if r["diff"] == 0]
    less = [r for r in rows if r["diff"] < 0 and r["table"] > 0]
    more = [r for r in rows if r["diff"] > 0 and r["sklad"] > 0]
    only_sk = [r for r in rows if r["table"] == 0 and r["sklad"] != 0]
    only_tab = [r for r in rows if r["sklad"] == 0 and r["table"] > 0]
    s_sum, t_sum = sum(r["sklad"] for r in rows), sum(r["table"] for r in rows)
    text = (
        "⚖️ Звірка: склад і таблиця локацій\n\n"
        f"🏬 Склад: {s_sum} шт.   📍 Таблиця: {t_sum} шт.   Різниця: {t_sum - s_sum:+d}\n"
        f"✅ Збігається: {len(ok)} з {len(rows)} артикулів\n"
        f"🔻 У таблиці менше: {len(less)}   🔺 більше: {len(more)}\n"
        f"❓ Є на складі, немає в таблиці: {len(only_sk)}   ➕ Є в таблиці, немає на складі: {len(only_tab)}\n"
    )
    bad = [r for r in rows if r["diff"] != 0]
    if bad:
        text += "\nНайбільші розбіжності:\n"
        for r in bad[:12]:
            text += f"• {r['code']} · {r['name'][:34]} — склад {r['sklad']}, таблиця {r['table']} ({r['diff']:+d})\n"
        if len(bad) > 12:
            text += f"… і ще {len(bad) - 12}. Повний список — в Excel.\n"
    return text


def build_report_text(days=7):
    items = history_since(days=days)
    df = load_df()
    names = df.drop_duplicates("code").set_index("code")["name"].to_dict()
    text = f"📋 Звіт за {days} дн.\n\n"
    if not items:
        text += "За цей період змін не було.\n"
    else:
        plus, minus = net_of(items)
        text += f"✏️ Змін: {len(items)}   +{plus} / −{minus} шт.\n\n👥 Хто що робив:\n"
        by_user = {}
        for h in items:
            if not sane(h):
                continue
            u = by_user.setdefault(h["user"], [0, 0, 0])
            u[0] += 1
            u[1] += max(0, h["new"] - h["old"])
            u[2] += max(0, h["old"] - h["new"])
        for u, (n, p, m) in sorted(by_user.items(), key=lambda x: -x[1][0]):
            text += f"• {u}: {n} змін (+{p} / −{m})\n"
        cnt = {}
        for h in items:
            cnt[h["code"]] = cnt.get(h["code"], 0) + 1
        text += "\n🔥 Найактивніші артикули:\n"
        for c, n in sorted(cnt.items(), key=lambda x: -x[1])[:5]:
            text += f"• {c} · {short_name(names.get(c, ''))[:32]} — {n} змін\n"
    weird = [h for h in items if not sane(h)]
    if weird:
        text += f"\n⚠️ Підозрілих записів: {len(weird)} (схоже, у кількість вписали артикул), у суми не входять\n"
    moved = {h["code"] for h in items}
    stale = [c for c in df[df["q"] > 0]["code"].unique() if c not in moved]
    text += f"\n💤 Без рухів за період: {len(stale)} артикулів\n"
    zero = df[df["q"] == 0]
    text += f"⚠️ Нульові залишки: {len(zero)} рядків\n"
    return text


def build_history_text(n=10):
    items = read_history()[-n:]
    if not items:
        return "Історія змін порожня."
    lines = [f"🕘 Останні {len(items)} змін:\n"]
    for h in reversed(items):
        if h.get("done"):
            lines.append(f"{h['ts'].strftime('%d.%m %H:%M')} · {h['user']} · 🧮 інвентаризація {h['loc']}: перевірено {h['old']}, змінено {h['new']}")
        else:
            lines.append(f"{h['ts'].strftime('%d.%m %H:%M')} · {h['user']} · {h['code']} · {h['action']} · {h['old']} → {h['new']}")
    return "\n".join(lines)


def build_daily_report():
    df = load_df(force=True)
    text = f"🌅 Ранковий звіт, {datetime.now(KYIV).strftime('%d.%m.%Y')}\n\n"
    text += f"📦 У таблиці: {int(df['q'].sum())} шт., {df['code'].nunique()} артикулів\n"
    day = history_since(hours=24)
    if day:
        plus, minus = net_of(day)
        text += f"🕒 За добу: {len(day)} змін, +{plus} / −{minus} шт.\n"
    else:
        text += "🕒 За добу змін не було\n"
    totals = df.groupby("code")["q"].sum()
    names = df.drop_duplicates("code").set_index("code")["name"].to_dict()
    low = [(c, int(q)) for c, q in totals.items() if q <= LOW_STOCK]
    if low:
        text += f"\n🔻 Мінімальний залишок (≤ {LOW_STOCK}): {len(low)} артикулів\n"
        for c, q in sorted(low, key=lambda x: x[1])[:8]:
            text += f"• {c} · {short_name(names.get(c, ''))[:34]} — {q} шт.\n"
        if len(low) > 8:
            text += f"… і ще {len(low) - 8}. Повний список: /stats → Огляд.\n"
    d7 = hist_moves(days=1)
    cons = consumption(d7)
    if cons:
        text += "\n🔥 Найбільша витрата за добу:\n"
        for c, v in sorted(cons.items(), key=lambda x: -x[1])[:3]:
            text += f"• {c} · {short_name(names.get(c, ''))[:30]} — {v} шт. (залишок {int(totals.get(c, 0))})\n"
    sk = load_sklad_safe(force=True)
    if sk is not None:
        rows = compare_sklad(df, sk)
        bad = [r for r in rows if r["diff"] != 0]
        text += f"\n⚖️ Розбіжностей зі складом: {len(bad)} артикулів\n"
        for r in bad[:5]:
            text += f"• {r['code']} · {r['name'][:30]} — {r['diff']:+d}\n"
    pt_ = pallet_totals(df)
    over_ = [n for n, t in pt_.items() if t > PALLET_MAX]
    if over_:
        text += f"\n📦 Перевантажені палети (> {PALLET_MAX}): {', '.join(over_[:10])}{'…' if len(over_) > 10 else ''}. Деталі: /unload\n"
    try:
        ordr = order_rows(df)
        if ordr:
            text += "\n🛒 Закінчується (< 14 дн.): " + "; ".join(f"{r['code']} ({r['stock']} шт.)" for r in ordr[:3]) + (f" … ще {len(ordr) - 3}" if len(ordr) > 3 else "") + ". Деталі: /zakupivlya\n"
        plan_ = inventory_plan(df, 3)
        if plan_:
            text += "\n🧮 Сьогодні перевірити: " + ", ".join(f"палета {p}" for p, _ in plan_) + ". План: /plan\n"
    except Exception as e:
        print(f"⚠️ Помилка блоку закупівлі/плану: {e}")
    asof = _sklad_meta.get("asof") if sk else None
    if asof:
        try:
            srows = build_sales_rows(df, sk, asof)
            sbad = len([r for r in srows if r["diff"] != 0])
            text += f"\n💰 Звірка продажів (склад від {asof.strftime('%d.%m %H:%M')}): реальних розбіжностей {sbad}\n"
        except Exception as e:
            print(f"⚠️ Звірка продажів у звіті: {e}")
    qn = quality_count(quality_issues(df, sk))
    text += f"\n🧪 Якість даних: {qn} зауважень\n" if qn else "\n🧪 Якість даних: ✅ без зауважень\n"
    text += "\nДеталі: /stats"
    return text


# ----------------- EXCEL (окремий файл, таблицю не змінює) -----------------
def _xlsx_styles():
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    thin = Side(style="thin", color="BFBFBF")
    return {
        "hdr_font": Font(name="Arial", bold=True, color="FFFFFF", size=11),
        "hdr_fill": PatternFill("solid", fgColor="1F3864"),
        "bad_fill": PatternFill("solid", fgColor="FCE4D6"),
        "body": Font(name="Arial", size=10),
        "bold": Font(name="Arial", size=10, bold=True),
        "border": Border(left=thin, right=thin, top=thin, bottom=thin),
        "wrap": Alignment(wrap_text=True, vertical="top"),
        "center": Alignment(horizontal="center", vertical="center", wrap_text=True),
    }


def _write_sheet(ws, headers, rows, widths, st, bad_rows=()):
    for i, h in enumerate(headers, 1):
        c = ws.cell(1, i, h)
        c.font, c.fill, c.border, c.alignment = st["hdr_font"], st["hdr_fill"], st["border"], st["center"]
    for r_i, row in enumerate(rows, 2):
        for c_i, v in enumerate(row, 1):
            c = ws.cell(r_i, c_i, v)
            c.font, c.border, c.alignment = st["body"], st["border"], st["wrap"]
            if (r_i - 2) in bad_rows:
                c.fill = st["bad_fill"]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{chr(64 + len(headers))}{max(2, len(rows) + 1)}"


def table_xlsx(df):
    from openpyxl import Workbook
    st = _xlsx_styles()
    wb = Workbook()
    ws = wb.active
    ws.title = "Шини"
    rows = [[r["pallet"], int(r["code"]), r["size"], r["name"], int(r["q"])] for _, r in df.sort_values("row").iterrows()]
    _write_sheet(ws, ["№ палети / локація", "Код товару", "Розмір", "Модель", "Кількість, шт."], rows, [20, 14, 13, 56, 14], st)
    ws2 = wb.create_sheet("По артикулах")
    grouped = []
    for c in df["code"].unique():
        rr = code_rows(df, c)
        where = "; ".join(f"{loc_label(r['pallet'])}: {int(r['q'])}" for _, r in rr.iterrows() if int(r["q"]) > 0) or "—"
        grouped.append([int(c), rr.iloc[0]["size"], short_name(rr.iloc[0]["name"]), int(rr["q"].sum()), where])
    grouped.sort(key=lambda x: (-x[3], x[0]))
    _write_sheet(ws2, ["Код товару", "Розмір", "Модель", "Разом, шт.", "Де є"], grouped, [14, 13, 52, 12, 60], st)
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio


def zvirka_xlsx(rows):
    from openpyxl import Workbook
    st = _xlsx_styles()
    wb = Workbook()
    ws = wb.active
    ws.title = "Звірка"
    data = [[int(r["code"]), r["name"], r["sklad"], r["table"], None, r["where"]] for r in rows]
    _write_sheet(ws, ["Код товару", "Модель", "Склад, шт.", "Таблиця, шт.", "Різниця (таблиця − склад)", "Де є в таблиці"],
                 data, [13, 54, 11, 13, 16, 60], st, bad_rows={i for i, r in enumerate(rows) if r["diff"] != 0})
    last = len(rows) + 1
    for i in range(2, last + 1):
        ws.cell(i, 5, f"=D{i}-C{i}")
    ws.cell(last + 1, 2, "РАЗОМ").font = st["bold"]
    for col, L in ((3, "C"), (4, "D"), (5, "E")):
        ws.cell(last + 1, col, f"=SUM({L}2:{L}{last})").font = st["bold"]
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio


# ================= РОЗШИРЕНА СТАТИСТИКА =================
DEAD_DAYS = 30
SEASONS = ["❄️ Зима", "☀️ Літо", "🌦 Всесезон", "📦 Інше"]
BRANDS = [
    "Premiorri", "Premiori", "Rosava", "Bridgestone", "Michelin", "Nokian", "Hankook", "Continental", "Goodyear",
    "Matador", "Barum", "Cordiant", "Tigar", "Kormoran", "Sailun", "Triangle", "Windforce", "Imperial", "Orium",
    "Falken", "Dunlop", "Pirelli", "Yokohama", "Kumho", "Nexen", "BFGoodrich", "Firestone", "Toyo", "Lassa",
    "Mastersteel", "Roadstone", "Aosen", "Estrada", "Powertrac", "Petlas", "Horizon", "Hifly", "Marshal",
]


def season_of(name):
    n = str(name).lower()
    if any(w in n for w in ["всесезон", "all weather", "all-season", "4seasons", "в.с", "в/с"]):
        return SEASONS[2]
    if any(w in n for w in ["зима", "зимов", "winter", "snow", "ice"]):
        return SEASONS[0]
    if any(w in n for w in ["літо", "літній", "summer"]):
        return SEASONS[1]
    return SEASONS[3]


def radius_of(size):
    m = re.search(r"R\s*(\d{2})", str(size), re.I)
    return f"R{m.group(1)}" if m else "Інші"


def size_of(size):
    m = re.search(r"(\d{3})\s*/\s*(\d{2})\D{0,3}R?\s*(\d{2})", str(size), re.I)
    return f"{m.group(1)}/{m.group(2)}R{m.group(3)}" if m else (str(size) or "—")


def size_sort_key(sz):
    m = re.match(r"(\d{3})/(\d{2})R(\d{2})", sz)
    return (int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else (99, 0, 0)


def brand_of(name):
    n = str(name).lower()
    for b in BRANDS:
        if b.lower() in n:
            return "Premiorri" if b == "Premiori" else b
    return "Інший"


def enrich(df):
    d = df.copy()
    d["season"] = d["name"].apply(season_of)
    d["radius"] = d["size"].apply(radius_of)
    d["sz"] = d["size"].apply(size_of)
    d["brand"] = d["name"].apply(brand_of)
    return d


def pct(part, whole):
    return f"{(100 * part / whole):.0f}%" if whole else "—"


def arrow(n):
    return "▲" if n > 0 else ("▼" if n < 0 else "•")


# ---- історія: допоміжне ----
def hist_moves(days=None, hours=None):
    return [h for h in history_since(hours=hours, days=days) if sane(h)]


def hist_start():
    items = [h for h in read_history() if not h.get("done")]
    return min((h["ts"] for h in items), default=None)


def covered_days(days):
    start = hist_start()
    if not start:
        return 0
    cov = (datetime.now(KYIV).replace(tzinfo=None) - start).total_seconds() / 86400
    return min(days, max(1, int(cov) + 1))


def consumption(items):
    """Витрата по артикулах (≈ продажі): зменшення через ➖ і точну кількість, мінус відмінені."""
    cons = {}
    for h in items:
        if h["action"] in (ACTION_TEXT["dec"], ACTION_TEXT["set"]) and h["old"] > h["new"]:
            cons[h["code"]] = cons.get(h["code"], 0) + h["old"] - h["new"]
        elif h["action"] == ACTION_TEXT["undo"] and h["new"] > h["old"]:
            cons[h["code"]] = cons.get(h["code"], 0) - (h["new"] - h["old"])
    return {c: v for c, v in cons.items() if v > 0}


def net(items):
    return sum(h["new"] - h["old"] for h in items)


def daily_totals(df, days=30):
    cur = int(df["q"].sum())
    by_day = {}
    for h in hist_moves(days=days + 1):
        by_day[h["ts"].date()] = by_day.get(h["ts"].date(), 0) + h["new"] - h["old"]
    today = datetime.now(KYIV).date()
    out = []
    for i in range(days):
        d = today - timedelta(days=i)
        out.append((d, cur))
        cur -= by_day.get(d, 0)
    return list(reversed(out))


# ---- якість даних ----
def quality_issues(df, sk):
    conflicts = []
    for c, g in df.groupby("code"):
        names = {norm(short_name(n)) for n in g["name"]}
        sizes = {norm(s) for s in g["size"] if s}
        if len(names) > 1 or len(sizes) > 1:
            conflicts.append(c)
    dup = [(c, p) for (c, p), g in df.groupby(["code", "pallet"]) if len(g) > 1]
    blank = list(get_meta().get("blank_rows", []))
    big = df[df["q"] > MAX_QTY]
    zero_rows = df[df["q"] == 0]
    tot = df.groupby("code")["q"].sum()
    not_in_sk, missing = [], []
    if sk:
        not_in_sk = [c for c, q in tot.items() if q > 0 and (c not in sk or sk[c]["qty"] <= 0)]
        missing = [c for c, v in sk.items() if v["qty"] > 0 and tot.get(c, 0) <= 0]
    return {"conflicts": conflicts, "dup": dup, "blank": blank, "big": big, "zero_rows": zero_rows,
            "not_in_sk": not_in_sk, "missing": missing, "has_sk": bool(sk)}


def quality_count(q):
    return len(q["conflicts"]) + len(q["dup"]) + len(q["blank"]) + len(q["big"]) + len(q["not_in_sk"]) + len(q["missing"])


def build_quality_text(df, sk):
    q = quality_issues(df, sk)
    names = df.drop_duplicates("code").set_index("code")["name"].to_dict()
    lines = ["🧪 Якість даних\n"]
    if quality_count(q) == 0:
        lines.append("✅ Проблем не знайдено.")
    if q["conflicts"]:
        lines.append(f"⚠️ Різні назви чи розміри під одним артикулом: {len(q['conflicts'])}")
        for c in q["conflicts"][:8]:
            lines.append(f"   • {c} · {short_name(names.get(c, ''))[:34]}")
    if q["dup"]:
        lines.append(f"⚠️ Артикул двічі на одній локації: {len(q['dup'])}")
        for c, p in q["dup"][:8]:
            lines.append(f"   • {c} · {loc_label(p)}")
    if q["blank"]:
        lines.append(f"⚠️ Порожні рядки в таблиці: {len(q['blank'])} (рядки {', '.join(str(r) for r in q['blank'][:10])})")
    if len(q["big"]):
        lines.append(f"⚠️ Підозріла кількість (> {MAX_QTY}): {len(q['big'])}")
        for _, r in q["big"].head(5).iterrows():
            lines.append(f"   • рядок {int(r['row'])}: {r['code']} — {int(r['q'])}")
    if q["has_sk"]:
        if q["not_in_sk"]:
            lines.append(f"❓ Є в таблиці, немає на складі: {len(q['not_in_sk'])}")
            for c in q["not_in_sk"][:8]:
                lines.append(f"   • {c} · {short_name(names.get(c, ''))[:34]}")
        if q["missing"]:
            lines.append(f"❓ Є на складі, немає в таблиці: {len(q['missing'])}")
            for c in q["missing"][:8]:
                lines.append(f"   • {c} · {short_name(sk[c]['name'])[:34]} — {sk[c]['qty']} шт.")
    lines.append(f"\nℹ️ Рядків з нульовою кількістю: {len(q['zero_rows'])} (це не помилка, але захаращує таблицю).")
    return "\n".join(lines)


# ---- розміри ----
def build_sizes_text(df, sk=None):
    en = enrich(df[df["q"] > 0])
    total = int(en["q"].sum())
    if en.empty:
        return "📏 Немає даних."
    mat = en.groupby(["radius", "season"])["q"].sum().unstack(fill_value=0)
    lines = ["📏 Розміри та сезонність\n", "Шин за радіусом і сезоном:"]
    for rad in sorted(mat.index, key=lambda r: (int(r[1:]) if r[1:].isdigit() else 99)):
        row = mat.loc[rad]
        parts = " · ".join(f"{s.split()[0]} {int(row[s])}" for s in SEASONS if s in mat.columns and int(row[s]) > 0)
        lines.append(f"• {rad}: {parts} — разом {int(row.sum())} ({pct(row.sum(), total)})")
    by_season = en.groupby("season")["q"].sum()
    lines.append("\nЧастки сезонів: " + " · ".join(f"{s.split()[0]} {pct(by_season.get(s, 0), total)}" for s in SEASONS if by_season.get(s, 0)))
    top = en.groupby("sz").agg(q=("q", "sum"), n=("code", "nunique")).sort_values("q", ascending=False).head(8)
    lines.append("\n🏆 Найбільші запаси за розмірами:")
    for sz, r in top.iterrows():
        lines.append(f"• {sz} — {int(r['q'])} шт. ({int(r['n'])} арт.)")
    cons = consumption(hist_moves(days=30))
    size_cons = {}
    code_size = en.drop_duplicates("code").set_index("code")["sz"].to_dict()
    for c, v in cons.items():
        if c in code_size:
            size_cons[code_size[c]] = size_cons.get(code_size[c], 0) + v
    size_stock = en.groupby("sz")["q"].sum().to_dict()
    holes = sorted([(s, size_stock.get(s, 0), v) for s, v in size_cons.items() if size_stock.get(s, 0) <= 3], key=lambda x: -x[2])
    if holes:
        lines.append("\n🕳 Ходові розміри, що закінчуються (витрата за 30 дн. + залишок ≤ 3 шт.):")
        for s, st, v in holes[:8]:
            lines.append(f"• {s}: залишок {st}, витрата {v}")
    low = sorted([s for s, q in size_stock.items() if q <= 3], key=size_sort_key)
    if low:
        lines.append(f"\n🔻 Розміри із залишком ≤ 3 шт.: {len(low)}")
        lines.append(", ".join(f"{s} ({size_stock[s]})" for s in low[:14]) + ("…" if len(low) > 14 else ""))
    return "\n".join(lines)


# ---- бренди ----
def build_brands_text(df, sk=None):
    en = enrich(df[df["q"] > 0])
    total = int(en["q"].sum())
    if en.empty:
        return "🏭 Немає даних."
    by = en.groupby("brand").agg(q=("q", "sum"), n=("code", "nunique")).sort_values("q", ascending=False)
    lines = ["🏭 Бренди\n"]
    for b, r in by.iterrows():
        lines.append(f"• {b}: {int(r['q'])} шт. ({pct(r['q'], total)}) · {int(r['n'])} арт.")
    arts = en.groupby("code")["q"].sum().sort_values(ascending=False)
    cum, k = 0, 0
    for q in arts:
        cum += q
        k += 1
        if cum >= 0.8 * total:
            break
    lines.append(f"\n📌 Правило 80/20: {k} з {len(arts)} артикулів ({pct(k, len(arts))}) дають 80% залишку.")
    cb = en.groupby(["brand", "radius"])["q"].sum().reset_index().sort_values("q", ascending=False).head(6)
    lines.append("\n🔝 Топ «бренд + радіус»:")
    for _, r in cb.iterrows():
        lines.append(f"• {r['brand']} {r['radius']}: {int(r['q'])} шт.")
    return "\n".join(lines)


# ---- локації ----
def build_locations_text(df, sk=None):
    lines = ["📍 Локації\n"]
    pal = []
    for n in range(1, 45):
        g = df[df["pallet"].str.strip() == str(n)]
        pal.append((n, len(g), int(g["q"].sum())))
    empty = [n for n, rows, q in pal if q == 0]
    low = [(n, q) for n, rows, q in pal if 0 < q <= 2]
    full = sorted(pal, key=lambda x: -x[2])[:5]
    lines.append(f"📦 Палети 1–44: порожніх {len(empty)}, майже порожніх (≤ 2 шт.) {len(low)}")
    if empty:
        lines.append("Порожні: " + ", ".join(str(n) for n in empty))
    if low:
        lines.append("Майже порожні: " + ", ".join(f"{n} ({q})" for n, q in low))
    lines.append("Найзавантаженіші: " + ", ".join(f"{n} ({q})" for n, rows, q in full))
    ptot = {str(n): q for n, rows, q in pal}
    over = sorted([(int(n), t) for n, t in ptot.items() if t > PALLET_MAX])
    free_total = sum(max(0, PALLET_MAX - t) for t in ptot.values())
    lines.append(f"\n📏 Ліміт {PALLET_MAX} шт. на палету: заповнено {sum(ptot.values())} з {44 * PALLET_MAX} ({pct(sum(ptot.values()), 44 * PALLET_MAX)}), "
                 f"вільних місць {free_total}")
    if over:
        lines.append(f"🔴 Перевантажені ({len(over)}, надлишок {sum(t - PALLET_MAX for _, t in over)} шт.): " + ", ".join(f"{n} ({t})" for n, t in over) + "  → /unload")
    mixed = [(n, df[(df['pallet'].str.strip() == str(n)) & (df['q'] > 0)]['code'].nunique()) for n in range(1, 45)]
    mixed = [(n, k) for n, k in mixed if k >= 5]
    if mixed:
        lines.append("🧩 Палети з 5+ різними артикулами: " + ", ".join(f"{n} ({k})" for n, k in mixed))
    for key in ("zal", "vul", "fas"):
        g = location_rows(df, key)
        lines.append(f"• {SPECIAL_LOCS[key]}: {len(g)} поз., {int(g['q'].sum())} шт.")
    scat = []
    for c, g in df[df["q"] > 0].groupby("code"):
        if g["pallet"].nunique() >= 4:
            scat.append((c, g["pallet"].nunique(), int(g["q"].sum())))
    scat.sort(key=lambda x: -x[1])
    names = df.drop_duplicates("code").set_index("code")["name"].to_dict()
    if scat:
        lines.append(f"\n🧩 Артикули, розкидані по 4+ локаціях ({len(scat)}), можна зібрати докупи:")
        for c, n, q in scat[:8]:
            lines.append(f"• {c} · {short_name(names.get(c, ''))[:30]} — {n} лок., {q} шт.")
    hist = read_history()
    last = {}
    for h in hist:
        if h.get("loc") and h["action"] in (ACTION_TEXT["inv"], ACTION_TEXT["invdone"]):
            last[h["loc"]] = max(last.get(h["loc"], h["ts"]), h["ts"])
    now = datetime.now(KYIV).replace(tzinfo=None)
    stale = [n for n, rows, q in pal if rows > 0 and (f"палета {n}" not in last or (now - last[f"палета {n}"]).days >= 30)]
    if last:
        lines.append(f"\n🧮 Інвентаризація через бота: палет без перевірки 30+ днів — {len(stale)} з {len([1 for n, r, q in pal if r > 0])}")
        if stale:
            lines.append(", ".join(str(n) for n in stale[:30]) + ("…" if len(stale) > 30 else ""))
    else:
        lines.append("\n🧮 Інвентаризацій через бота ще не було. Почни з /inventory 27, і тут з'явиться контроль «давно не перевіряли».")
    return "\n".join(lines)


# ---- рух ----
def build_movement_text(df, sk=None):
    lines = ["🔄 Рух і витрата\n"]
    d7, d30 = hist_moves(days=7), hist_moves(days=30)
    cov = hist_start()
    if not cov:
        return "🔄 В історії ще немає записів."
    cd7, cd30 = covered_days(7), covered_days(30)
    c7, c30 = consumption(d7), consumption(d30)
    adds = sum(max(0, h["new"] - h["old"]) for h in d7)
    lines.append(f"🗓 Історія веде облік з {cov.strftime('%d.%m.%Y')} (охоплено {cd30} дн.). Витрата ≈ зменшення через ➖ і точну кількість.")
    lines.append(f"За 7 дн.: витрата {sum(c7.values())} шт., надійшло/додано {adds} шт., чисто {net(d7):+d} шт.")
    tot = df.groupby("code")["q"].sum().to_dict()
    names = df.drop_duplicates("code").set_index("code")["name"].to_dict()
    if c7:
        lines.append("\n🔥 Найбільша витрата за 7 дн.:")
        for c, v in sorted(c7.items(), key=lambda x: -x[1])[:8]:
            per_day = v / max(1, cd7)
            left = tot.get(c, 0)
            when = f", вистачить ≈ {left / per_day:.0f} дн." if per_day > 0 and left > 0 else (", немає на залишку" if left == 0 else "")
            lines.append(f"• {c} · {short_name(names.get(c, ''))[:28]} — {v} шт. (залишок {left}{when})")
    moved30 = {h["code"] for h in d30}
    dead = [c for c, q in tot.items() if q > 0 and c not in moved30]
    if cd30 >= 7:
        lines.append(f"\n💤 Без рухів за {cd30} дн.: {len(dead)} артикулів, {sum(tot[c] for c in dead)} шт.")
        for c in sorted(dead, key=lambda c: -tot[c])[:8]:
            lines.append(f"• {c} · {short_name(names.get(c, ''))[:28]} — {tot[c]} шт.")
    else:
        lines.append(f"\n💤 «Завмерлі» покажу, коли історія накопичить 7+ днів (зараз {cd30}).")
    users = {}
    for h in d7:
        u = users.setdefault(h["user"], [0, 0, 0])
        u[0] += 1
        u[1] += max(0, h["new"] - h["old"])
        u[2] += max(0, h["old"] - h["new"])
    if users:
        lines.append("\n👥 Хто що робив (7 дн.):")
        for u, (n, p, m) in sorted(users.items(), key=lambda x: -x[1][0])[:6]:
            lines.append(f"• {u}: {n} змін (+{p} / −{m})")
    return "\n".join(lines)


# ---- склад: розбивка ----
def build_sklad_text(df, sk):
    rows = compare_sklad(df, sk)
    less = [r for r in rows if r["diff"] < 0 and r["table"] > 0]
    more = [r for r in rows if r["diff"] > 0 and r["sklad"] > 0]
    only_sk = [r for r in rows if r["table"] == 0 and r["sklad"] > 0]
    only_tab = [r for r in rows if r["sklad"] <= 0 and r["table"] > 0]
    text = build_zvirka_text(rows)
    text += (f"\n📊 Недостача в таблиці: {sum(-r['diff'] for r in less + only_sk)} шт. "
             f"({len(less) + len(only_sk)} арт.) · надлишок: {sum(r['diff'] for r in more + only_tab)} шт. ({len(more) + len(only_tab)} арт.)")
    top = [r for r in rows if r["diff"] != 0][:5]
    if top:
        text += "\n\nТоп-5 розбіжностей і де лежить шина:\n"
        for r in top:
            text += f"• {r['code']} · {r['name'][:26]} — {r['diff']:+d}\n   {r['where'][:90]}\n"
    return text


# ---- вітрина (що не виставлено) ----
def display_data(df, sk, key="zal"):
    zal = location_rows(df, key)
    shown = set(zal[zal["q"] > 0]["code"])
    shown_all = set(zal["code"])
    tot = df.groupby("code")["q"].sum().to_dict()
    names = df.drop_duplicates("code").set_index("code")["name"].to_dict()
    sizes = df.drop_duplicates("code").set_index("code")["size"].to_dict()
    if sk:
        stock = {c: v["qty"] for c, v in sk.items() if v["qty"] > 0}
        for c, v in sk.items():
            names.setdefault(c, v["name"])
        source, source_in = "склад", "складі"
    else:
        stock = {c: q for c, q in tot.items() if q > 0}
        source, source_in = "таблиця", "таблиці"
    missing = []
    for c, q in stock.items():
        if c in shown:
            continue
        rows = code_rows(df, c)
        where = "; ".join(f"{loc_label(r['pallet'])}: {int(r['q'])}" for _, r in rows.iterrows()
                          if int(r["q"]) > 0 and r["pallet"].strip().lower() != SPECIAL_LOCS[key]) or "немає в таблиці"
        missing.append({"code": c, "size": size_of(sizes.get(c, "")), "name": short_name(names.get(c, "")),
                        "stock": q, "table": int(tot.get(c, 0)), "where": where})
    missing.sort(key=lambda r: (size_sort_key(r["size"]), r["code"]))
    zero_shown = [(r["code"], size_of(r["size"]), short_name(r["name"])) for _, r in zal.iterrows() if r["q"] == 0]
    not_in_stock = [c for c in shown if c not in stock]
    return {"label": SPECIAL_LOCS[key], "shown": shown, "shown_all": shown_all, "stock": stock, "missing": missing,
            "zero_shown": zero_shown, "not_in_stock": not_in_stock, "zal": zal, "names": names, "source": source,
            "source_in": source_in}


def build_display_text(df, sk, key="zal"):
    d = display_data(df, sk, key)
    n_stock = len(d["stock"])
    shown_in_stock = len([c for c in d["shown"] if c in d["stock"]])
    label_in = {"вітрина зал": "вітрині зал", "вітрина вулиця": "вітрині вулиця", "палети фасад": "палетах фасаду"}.get(d["label"], d["label"])
    lines = [f"🪟 {d['label'].capitalize()}: аналіз\n",
             f"У залі: {len(d['zal'])} рядків, {int(d['zal']['q'].sum())} шт., {len(d['shown'])} артикулів у наявності.",
             f"База для порівняння: {d['source']} ({n_stock} артикулів із залишком).",
             f"Виставлено {shown_in_stock} з {n_stock} ({pct(shown_in_stock, n_stock)}), не виставлено {len(d['missing'])}.\n"]
    if d["missing"]:
        lines.append(f"❌ Не виставлені у {label_in} ({len(d['missing'])}):")
        cur = None
        for r in d["missing"]:
            rad = radius_of(r["size"])
            if rad != cur:
                cur = rad
                lines.append(f"\n— {rad} —")
            lines.append(f"• {r['code']} · {r['size']} · {r['name'][:34]} — {r['stock']} шт. ({r['where'][:70]})")
    else:
        lines.append("✅ Усе, що є на складі, виставлено.")
    if d["zero_shown"]:
        lines.append(f"\n⚠️ Є в залі, але 0 шт. ({len(d['zero_shown'])}): " + "; ".join(f"{c} {s}" for c, s, n in d["zero_shown"][:12]))
    if d["not_in_stock"]:
        lines.append(f"\nℹ️ Є в залі, але немає на {d['source_in']} ({len(d['not_in_stock'])}): " + ", ".join(d["not_in_stock"][:12]))
    return "\n".join(lines)


def display_xlsx(df, sk, key="zal"):
    from openpyxl import Workbook
    st = _xlsx_styles()
    d = display_data(df, sk, key)
    wb = Workbook()
    ws = wb.active
    ws.title = "Не виставлено"
    rows = [[int(r["code"]), r["size"], r["name"], r["stock"], r["table"], r["where"]] for r in d["missing"]]
    _write_sheet(ws, ["Код товару", "Розмір", "Модель", f"На {d['source_in']}, шт.", "У таблиці, шт.", "Де лежить зараз"], rows, [13, 12, 50, 12, 12, 60], st)
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio


# ---- огляд ----
def build_overview(df, sk):
    total = int(df["q"].sum())
    tot = df.groupby("code")["q"].sum()
    lines = [f"📊 Огляд складу · {datetime.now(KYIV).strftime('%d.%m.%Y')}\n",
             f"📦 У таблиці: {total} шт. · {int((tot > 0).sum())} артикулів · {len(df)} рядків"]
    d1, d7 = hist_moves(hours=24), hist_moves(days=7)
    if d1 or d7:
        n1, n7 = net(d1), net(d7)
        lines.append(f"{arrow(n1)} За добу: {n1:+d} шт. (було {total - n1}) · {arrow(n7)} за тиждень: {n7:+d} (було {total - n7})")
    low = int(((tot > 0) & (tot <= LOW_STOCK)).sum())
    zero = int((tot <= 0).sum())
    lines.append(f"🔻 Мінімальний залишок (≤ {LOW_STOCK}): {low} арт. · ⚠️ нулі: {zero} арт.")
    pt_ = pallet_totals(df)
    lines.append(f"📦 Палети (ліміт {PALLET_MAX}): перевантажено {len([1 for t in pt_.values() if t > PALLET_MAX])}, вільних місць {sum(max(0, PALLET_MAX - t) for t in pt_.values())}")
    if sk:
        rows = compare_sklad(df, sk)
        ok = len([r for r in rows if r["diff"] == 0])
        s_sum = sum(r["sklad"] for r in rows)
        lines.append(f"⚖️ Склад: {s_sum} шт. · збігається {ok} з {len(rows)} ({pct(ok, len(rows))}) · різниця {total - s_sum:+d} шт.")
    q = quality_issues(df, sk)
    lines.append(f"🧪 Якість даних: {quality_count(q)} зауважень" if quality_count(q) else "🧪 Якість даних: ✅ без зауважень")
    cd30 = covered_days(30)
    if cd30 >= 7:
        moved = {h["code"] for h in hist_moves(days=30)}
        dead = [c for c, v in tot.items() if v > 0 and c not in moved]
        lines.append(f"💤 Без рухів за {cd30} дн.: {len(dead)} арт.")
    lines.append("\nОбери розділ нижче 👇")
    return "\n".join(lines)


def stats_keyboard():
    B = InlineKeyboardButton
    return InlineKeyboardMarkup([
        [B("📏 Розміри", callback_data="st_size"), B("🏭 Бренди", callback_data="st_brand")],
        [B("📍 Локації", callback_data="st_loc"), B("🔄 Рух", callback_data="st_move")],
        [B("🧪 Якість даних", callback_data="st_qual"), B("⚖️ Склад", callback_data="st_sklad")],
        [B("🪟 Вітрина зал", callback_data="st_zal"), B("📈 Графіки", callback_data="st_chart")],
        [B("💰 Звірка продажів", callback_data="st_sales"), B("📥 Excel-дашборд", callback_data="st_xls")],
        [B("🛒 Закупівля", callback_data="st_order"), B("🧮 План перевірок", callback_data="st_plan")],
        [B("📋 Повна", callback_data="st_full")],
    ])


# ---- графіки ----
def make_charts(df):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    out = []
    en = enrich(df[df["q"] > 0])
    colors = {SEASONS[0]: "#4472C4", SEASONS[1]: "#ED7D31", SEASONS[2]: "#70AD47", SEASONS[3]: "#A5A5A5"}
    if not en.empty:
        mat = en.groupby(["radius", "season"])["q"].sum().unstack(fill_value=0)
        mat = mat.loc[sorted(mat.index, key=lambda r: (int(r[1:]) if r[1:].isdigit() else 99))]
        fig, ax = plt.subplots(figsize=(8, 4.5))
        bottom = None
        for s in SEASONS:
            if s in mat.columns:
                vals = mat[s].values
                ax.bar(mat.index, vals, bottom=bottom, label=s.split(' ', 1)[1], color=colors[s])
                bottom = vals if bottom is None else bottom + vals
        for i, tot_ in enumerate(mat.sum(axis=1).values):
            ax.text(i, tot_ + 1, str(int(tot_)), ha="center", fontsize=9)
        ax.set_title("Шини за радіусом і сезоном, шт.")
        ax.legend()
        fig.tight_layout()
        bio = io.BytesIO(); fig.savefig(bio, format="png", dpi=130); bio.seek(0); plt.close(fig)
        out.append((bio, "📏 Запаси за радіусом і сезоном"))
        by = en.groupby("brand")["q"].sum().sort_values(ascending=True).tail(10)
        fig, ax = plt.subplots(figsize=(8, 4.5))
        total = by.sum() if len(by) else 1
        ax.barh(by.index, by.values, color="#4472C4")
        for i, v in enumerate(by.values):
            ax.text(v + 0.5, i, f"{int(v)} ({100 * v / en['q'].sum():.0f}%)", va="center", fontsize=9)
        ax.set_title("Бренди: шт. і частка від залишку")
        fig.tight_layout()
        bio = io.BytesIO(); fig.savefig(bio, format="png", dpi=130); bio.seek(0); plt.close(fig)
        out.append((bio, "🏭 Бренди"))
    pt = pallet_totals(df)
    fig, ax = plt.subplots(figsize=(9, 4.2))
    ns = list(range(1, 45))
    vals = [pt[str(n)] for n in ns]
    ax.bar(ns, vals, color=["#C00000" if v > PALLET_MAX else ("#FFC000" if v > PALLET_MAX * 0.75 else "#70AD47") for v in vals])
    ax.axhline(PALLET_MAX, color="black", linestyle="--", linewidth=1)
    ax.set_title(f"Завантаженість палет (ліміт {PALLET_MAX} шт.)")
    ax.set_xticks(ns[::2])
    ax.set_xlabel("Номер палети")
    fig.tight_layout()
    bio = io.BytesIO(); fig.savefig(bio, format="png", dpi=130); bio.seek(0); plt.close(fig)
    out.append((bio, "📦 Завантаженість палет: зелені до 12, жовті до 16, червоні понад ліміт"))
    series = daily_totals(df, 30)
    if any(h for h in hist_moves(days=31)):
        fig, ax = plt.subplots(figsize=(8, 4.2))
        ax.plot([d for d, _ in series], [v for _, v in series], marker="o", color="#2F5597")
        ax.set_title("Залишок за 30 днів (відновлено з історії змін)")
        ax.grid(alpha=0.3)
        fig.autofmt_xdate()
        fig.tight_layout()
        bio = io.BytesIO(); fig.savefig(bio, format="png", dpi=130); bio.seek(0); plt.close(fig)
        out.append((bio, "📈 Динаміка залишку (враховує лише зміни через бота)"))
    return out


# ---- Excel-дашборд ----
def dashboard_xlsx(df, sk):
    from openpyxl import Workbook
    st = _xlsx_styles()
    wb = Workbook()
    en = enrich(df[df["q"] > 0])
    tot = df.groupby("code")["q"].sum()
    names = df.drop_duplicates("code").set_index("code")["name"].to_dict()

    ws = wb.active
    ws.title = "Огляд"
    total = int(df["q"].sum())
    n1, n7 = net(hist_moves(hours=24)), net(hist_moves(days=7))
    rows = [["Шин у таблиці, шт.", total], ["Артикулів із залишком", int((tot > 0).sum())], ["Рядків у таблиці", len(df)],
            ["Зміна за добу, шт.", n1], ["Зміна за тиждень, шт.", n7],
            [f"Артикулів із мінімальним залишком (≤ {LOW_STOCK})", int(((tot > 0) & (tot <= LOW_STOCK)).sum())],
            ["Артикулів без залишку", int((tot <= 0).sum())]]
    if sk:
        cmp_rows = compare_sklad(df, sk)
        rows += [["Склад, шт.", sum(r["sklad"] for r in cmp_rows)], ["Артикулів збігається зі складом", len([r for r in cmp_rows if r["diff"] == 0])],
                 ["Артикулів з розбіжністю", len([r for r in cmp_rows if r["diff"] != 0])]]
    rows.append(["Зауважень до якості даних", quality_count(quality_issues(df, sk))])
    _write_sheet(ws, ["Показник", "Значення"], rows, [52, 16], st)

    if not en.empty:
        ws = wb.create_sheet("Розміри × сезон")
        mat = en.groupby(["radius", "season"])["q"].sum().unstack(fill_value=0)
        rads = sorted(mat.index, key=lambda r: (int(r[1:]) if r[1:].isdigit() else 99))
        data = [[r] + [int(mat.loc[r][s]) if s in mat.columns else 0 for s in SEASONS] + [None] for r in rads]
        _write_sheet(ws, ["Радіус"] + SEASONS + ["Разом"], data, [10, 12, 12, 12, 12, 12], st)
        for i in range(2, len(data) + 2):
            ws.cell(i, 6, f"=SUM(B{i}:E{i})")
        last = len(data) + 1
        ws.cell(last + 1, 1, "РАЗОМ").font = st["bold"]
        for col in "BCDEF":
            ws[f"{col}{last + 1}"] = f"=SUM({col}2:{col}{last})"
            ws[f"{col}{last + 1}"].font = st["bold"]

        ws = wb.create_sheet("Бренди")
        by = en.groupby("brand").agg(q=("q", "sum"), n=("code", "nunique")).sort_values("q", ascending=False)
        data = [[b, int(r["q"]), None, int(r["n"])] for b, r in by.iterrows()]
        _write_sheet(ws, ["Бренд", "Шт.", "Частка", "Артикулів"], data, [20, 10, 10, 12], st)
        last = len(data) + 1
        for i in range(2, last + 1):
            ws.cell(i, 3, f"=B{i}/SUM($B$2:$B${last})").number_format = "0.0%"

    ws = wb.create_sheet("Локації")
    locs = [f"палета {n}" for n in range(1, 45)] + list(SPECIAL_LOCS.values())
    data = []
    for lab in locs:
        g = df[df["pallet"].apply(loc_label) == lab]
        data.append([lab, len(g), int(g["q"].sum())])
    _write_sheet(ws, ["Локація", "Рядків", "Шин, шт."], data, [22, 10, 12], st)
    last = len(data) + 1
    ws.cell(last + 1, 1, "РАЗОМ").font = st["bold"]
    ws[f"B{last + 1}"], ws[f"C{last + 1}"] = f"=SUM(B2:B{last})", f"=SUM(C2:C{last})"

    c7 = consumption(hist_moves(days=7))
    cd7 = max(1, covered_days(7))
    ws = wb.create_sheet("Рух 7 днів")
    data = [[int(c), short_name(names.get(c, "")), v, None, int(tot.get(c, 0)), None] for c, v in sorted(c7.items(), key=lambda x: -x[1])]
    _write_sheet(ws, ["Код", "Модель", "Витрата, шт.", "На день", "Залишок", "Вистачить, днів"], data or [["—", "немає витрати за період", 0, 0, 0, None]], [13, 48, 12, 10, 10, 14], st)
    for i in range(2, len(data) + 2):
        ws.cell(i, 4, f"=C{i}/{cd7}").number_format = "0.0"
        ws.cell(i, 6, f'=IF(D{i}=0,"",E{i}/D{i})').number_format = "0"

    ws = wb.create_sheet("Якість даних")
    q = quality_issues(df, sk)
    data = []
    for c in q["conflicts"]: data.append(["Різні назви/розміри", int(c), short_name(names.get(c, ""))])
    for c, p in q["dup"]: data.append(["Дубль на локації", int(c), loc_label(p)])
    for r_ in q["blank"]: data.append(["Порожній рядок", "", f"рядок {r_}"])
    for _, r in q["big"].iterrows(): data.append(["Підозріла кількість", int(r["code"]), f"рядок {int(r['row'])}: {int(r['q'])}"])
    for c in q["not_in_sk"]: data.append(["Немає на складі", int(c), short_name(names.get(c, ""))])
    for c in q["missing"]: data.append(["Немає в таблиці", int(c), short_name(sk[c]["name"])])
    _write_sheet(ws, ["Проблема", "Код", "Деталі"], data or [["Проблем не знайдено", "", ""]], [26, 14, 56], st)

    if sk:
        ws = wb.create_sheet("Звірка")
        cmp_rows = compare_sklad(df, sk)
        data = [[int(r["code"]), r["name"], r["sklad"], r["table"], None, r["where"]] for r in cmp_rows]
        _write_sheet(ws, ["Код", "Модель", "Склад", "Таблиця", "Різниця", "Де є"], data, [13, 50, 10, 10, 10, 60], st,
                     bad_rows={i for i, r in enumerate(cmp_rows) if r["diff"] != 0})
        for i in range(2, len(data) + 2):
            ws.cell(i, 5, f"=D{i}-C{i}")
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio




# ================= ЗВІРКА ПРОДАЖІВ =================
def parse_asof_args(args):
    return find_datetime(" ".join(args)) if args else None


def movements_since(asof):
    """По артикулах: dec — сума зменшень, inc — сума збільшень, corr — зміни інвентаризацій (це не продаж)."""
    per = {}
    for h in read_history():
        if h.get("done") or h["ts"] < asof or not sane(h):
            continue
        d = h["new"] - h["old"]
        e = per.setdefault(h["code"], {"dec": 0, "inc": 0, "corr": 0, "mv": 0})
        if h["action"] == ACTION_TEXT["move_out"]:
            e["mv"] += -d
        elif h["action"] == ACTION_TEXT["move_in"]:
            pass
        elif h["action"] == ACTION_TEXT["inv"]:
            e["corr"] += d
        elif d < 0:
            e["dec"] += -d
        else:
            e["inc"] += d
    return per


def build_sales_rows(df, sk, asof):
    """Очікувано в таблиці = склад − продажі + надходження. Продаж = чисте зменшення артикула по всіх локаціях,
    тож мінус на одній палеті разом із плюсом на іншій (переміщення) продажем не вважається."""
    per = movements_since(asof)
    tot = df.groupby("code")["q"].sum().to_dict()
    names = df.drop_duplicates("code").set_index("code")["name"].to_dict()
    rows = []
    for c in set(sk) | set(tot) | set(per):
        s_ = int(sk.get(c, {}).get("qty", 0))
        t = int(tot.get(c, 0))
        e = per.get(c, {"dec": 0, "inc": 0, "corr": 0, "mv": 0})
        sold, recv, moved = max(0, e["dec"] - e["inc"]), max(0, e["inc"] - e["dec"]), min(e["dec"], e["inc"]) + e.get("mv", 0)
        if s_ == 0 and t == 0 and not (sold or recv or e["corr"]):
            continue
        expected = s_ - sold + recv
        rr = code_rows(df, c)
        where = "; ".join(f"{loc_label(r['pallet'])}: {int(r['q'])}" for _, r in rr.iterrows() if int(r["q"]) > 0) or "—"
        rows.append({"code": c, "name": short_name(sk.get(c, {}).get("name") or names.get(c, "")), "sklad": s_,
                     "sold": sold, "recv": recv, "moved": moved, "corr": e["corr"], "expected": expected,
                     "table": t, "diff": t - expected, "diff_raw": t - s_, "where": where})
    rows.sort(key=lambda r: (r["diff"] == 0, -abs(r["diff"]), r["code"]))
    return rows


def build_sales_text(df, sk, asof):
    rows = build_sales_rows(df, sk, asof)
    now = datetime.now(KYIV).replace(tzinfo=None)
    age = (now - asof).days
    sold, recv = sum(r["sold"] for r in rows), sum(r["recv"] for r in rows)
    moved, corr = sum(r["moved"] for r in rows), sum(r["corr"] for r in rows)
    s_sum, e_sum, t_sum = sum(r["sklad"] for r in rows), sum(r["expected"] for r in rows), sum(r["table"] for r in rows)
    ok_after = len([r for r in rows if r["diff"] == 0])
    ok_before = len([r for r in rows if r["diff_raw"] == 0])
    explained = len([r for r in rows if r["diff_raw"] != 0 and r["diff"] == 0])
    lines = [f"💰 Звірка продажів\n",
             f"🏬 Склад станом на {asof.strftime('%d.%m.%Y %H:%M')} ({age} дн. тому): {s_sum} шт.",
             f"💸 Продано після цього: {sold} шт. ({len([r for r in rows if r['sold']])} арт.)",
             f"📥 Надійшло: {recv} шт. · 🔁 переміщено/відмінено ≈ {moved} шт. (не продаж)",
             f"🧮 Інвентаризації змінили таблицю на {corr:+d} шт. (це корекції, не продаж)",
             f"\n📐 Очікувано в таблиці: {e_sum} шт. · фактично: {t_sum} шт. · різниця {t_sum - e_sum:+d}",
             f"✅ Збігається після врахування продажів: {ok_after} з {len(rows)} (без врахування було {ok_before}); продажі пояснили {explained} арт."]
    bad = [r for r in rows if r["diff"] != 0]
    if bad:
        lines.append(f"\n⚠️ Реальні розбіжності ({len(bad)}):")
        for r in bad[:10]:
            parts = [f"склад {r['sklad']}"]
            if r["sold"]:
                parts.append(f"продано {r['sold']}")
            if r["recv"]:
                parts.append(f"надійшло {r['recv']}")
            lines.append(f"• {r['code']} · {r['name'][:26]} — {', '.join(parts)} → очікувано {r['expected']}, таблиця {r['table']} ({r['diff']:+d})")
        if len(bad) > 10:
            lines.append(f"… і ще {len(bad) - 10}. Повний список в Excel.")
    start = hist_start()
    if start and start > asof:
        lines.append(f"\nℹ️ Історія змін веде облік з {start.strftime('%d.%m.%Y')}, це пізніше за дату складу: продажі до цього не враховані.")
    if age > 14:
        lines.append(f"\nℹ️ Склад вставлено {age} дн. тому. Якщо ти його оновлював, онови дату на аркуші «{SKLAD_SHEET}».")
    return "\n".join(lines)


def sales_xlsx(df, sk, asof):
    from openpyxl import Workbook
    st = _xlsx_styles()
    rows = build_sales_rows(df, sk, asof)
    wb = Workbook()
    ws = wb.active
    ws.title = "Звірка продажів"
    data = [[int(r["code"]), r["name"], r["sklad"], r["sold"], r["recv"], None, r["table"], None, None, r["moved"], r["corr"], r["where"]] for r in rows]
    _write_sheet(ws, ["Код товару", "Модель", "Склад, шт.", "Продано", "Надійшло", "Очікувано в таблиці", "Таблиця, шт.",
                      "Різниця (таблиця − очікувано)", "Статус", "Переміщено / відмінено", "Корекції інвентаризації", "Де є в таблиці"],
                 data, [13, 50, 10, 10, 10, 13, 11, 15, 14, 14, 14, 60], st,
                 bad_rows={i for i, r in enumerate(rows) if r["diff"] != 0})
    last = len(rows) + 1
    for i in range(2, last + 1):
        ws.cell(i, 6, f"=C{i}-D{i}+E{i}")
        ws.cell(i, 8, f"=G{i}-F{i}")
        ws.cell(i, 9, f'=IF(H{i}=0,"збігається","розбіжність")')
    ws.cell(last + 1, 2, "РАЗОМ").font = st["bold"]
    for col in "CDEFGHJK":
        ws[f"{col}{last + 1}"] = f"=SUM({col}2:{col}{last})"
        ws[f"{col}{last + 1}"].font = st["bold"]
    ws.cell(last + 3, 1, f"Склад станом на {asof.strftime('%d.%m.%Y %H:%M')}. Очікувано = склад − продано + надійшло. "
                         "Продано = чисте зменшення артикула по всіх локаціях (переміщення між локаціями не продаж). "
                         "Інвентаризації винесено окремо як корекції.").font = st["body"]
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio


# ----------------- ОФОРМЛЕННЯ: МЕНЮ І ДОВІДКА -----------------
MENU = ReplyKeyboardMarkup(
    [["🔍 Пошук", "➕ Додати"], ["📦 Палети", "📊 Статистика"], ["⚖️ Звірка", "💰 Звірка продажів"], ["📋 Звіт", "📥 Експорт"], ["❓ Допомога"]],
    resize_keyboard=True,
)

HELP_TEXT = (
    "👋 Складський помічник для обліку шин 🛞\n\n"
    "🔍 Надішли артикул, розмір (205/55R16 або «205 55 16»), назву чи бренд — покажу картку шини з усіма локаціями. "
    "Можна й фото цінника.\n"
    "➖ / ➕ під локацією змінюють кількість на 1, натискання на назву локації дозволяє ввести точну кількість.\n\n"
    "Команди:\n"
    "➕ /add — додати шину (за артикулом; розмір і назву підтягну сам)\n"
    "📦 /pallet 27 — що лежить на палеті, завантаженість X/16, PDF-картка (або /pallet зал)\n"
    "💡 /where 40745801 8 — куди покласти шини (де є місце)\n"
    "🧭 /unload — перевантажені палети й куди перенести надлишок\n"
    "🔁 кнопка «Перемістити» в картці шини — перенос між локаціями\n"
    "🧮 /plan — які палети час перевірити;  🛒 /zakupivlya — що закінчується\n"
    "🧮 /inventory 27 — інвентаризація палети по позиціях\n"
    "📊 /stats — огляд і розділи: розміри, бренди, локації, рух, якість даних, графіки, Excel-дашборд\n"
    "🪟 /zal — що є на складі, але не виставлено у вітрині зал\n"
    "⚖️ /zvirka — звірка з аркушем «Склад» (онови його й повтори команду)\n"
    "💰 /zvirka_prodazhiv — звірка продажів: склад − продажі проти таблиці (дата складу береться з аркуша «Склад»)\n"
    "📋 /report 7 — звіт за N днів, 🕘 /history — останні зміни\n"
    "📥 /export — вивантажити таблицю в Excel\n"
    "🆔 /myid — мій ID для щоденного звіту\n"
    "❌ /cancel — скасувати поточну дію"
)


def guarded(fn):
    async def wrapper(update, context):
        try:
            return await fn(update, context)
        except Exception as e:
            msg = friendly_error(e)
            try:
                if update.callback_query:
                    await update.callback_query.answer(msg[:190], show_alert=True)
                elif update.effective_message:
                    await update.effective_message.reply_text(msg)
            except Exception:
                pass
            return None
    wrapper.__name__ = fn.__name__
    return wrapper


async def safe_edit(query, text, kb):
    try:
        await query.edit_message_text(text, reply_markup=kb)
    except Exception as e:
        if "not modified" not in str(e).lower():
            print(f"⚠️ edit: {e}")


async def notify_chats(context, text):
    for cid in REPORT_CHAT_IDS:
        try:
            await context.bot.send_message(cid, text)
        except Exception as e:
            print(f"⚠️ Не вдалося надіслати в чат {cid}: {e}")


async def after_change(context, user, row, code, old, new):
    last = context.application.bot_data.setdefault("last", {})
    last[row] = {"code": code, "old": old, "new": new, "ts": _time.time(), "uid": user.id}
    df = await asyncio.to_thread(load_df)
    new_total = code_total(df, code)
    old_total = new_total - (new - old)
    if new < old and old_total > LOW_STOCK >= new_total:
        names = df[df["code"] == code]["name"]
        name = short_name(names.iloc[0]) if len(names) else ""
        await notify_chats(context, f"🔻 Мінімальний залишок: {code} · {name} — тепер {new_total} шт. (змінив {user_label(user)})")


# ----------------- КОМАНДИ -----------------
@guarded
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=MENU)


@guarded
async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 ID цього чату: {update.effective_chat.id}\n"
        "Щоб отримувати ранковий звіт і сповіщення про мінімум, додай цей ID у змінну REPORT_CHAT_IDS на Render."
    )


BACK_KB = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Огляд", callback_data="st_ov")]])


@guarded
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = await asyncio.to_thread(load_df, True)
    sk = await asyncio.to_thread(load_sklad_safe)
    text = await asyncio.to_thread(build_overview, df, sk)
    await update.effective_message.reply_text(text, reply_markup=stats_keyboard())


async def _send_parts(msg, text, kb=None, parse_mode=None):
    parts = chunk_text(text)
    for i, part in enumerate(parts):
        await msg.reply_text(part, reply_markup=kb if i == len(parts) - 1 else None, parse_mode=parse_mode)


@guarded
async def stats_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    sec = q.data.split("_", 1)[1]
    await q.answer("Готую…" if sec in ("chart", "xls") else None)
    msg = q.message
    df = await asyncio.to_thread(load_df)
    sk = await asyncio.to_thread(load_sklad_safe)
    if sec == "ov":
        text = await asyncio.to_thread(build_overview, df, sk)
        await msg.reply_text(text, reply_markup=stats_keyboard())
    elif sec in ("size", "brand", "loc", "move", "qual"):
        fn = {"size": build_sizes_text, "brand": build_brands_text, "loc": build_locations_text,
              "move": build_movement_text, "qual": build_quality_text}[sec]
        await _send_parts(msg, await asyncio.to_thread(fn, df, sk), BACK_KB)
    elif sec == "sklad":
        if not sk:
            await msg.reply_text(ZVIRKA_SETUP.format(sheet=SKLAD_SHEET), reply_markup=BACK_KB)
            return
        sk = await asyncio.to_thread(load_sklad, True)
        text = await asyncio.to_thread(build_sklad_text, df, sk)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Excel зі звіркою", callback_data="zvx")],
                                   [InlineKeyboardButton("⬅️ Огляд", callback_data="st_ov")]])
        await _send_parts(msg, text, kb)
    elif sec == "zal":
        text = await asyncio.to_thread(build_display_text, df, sk, "zal")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Excel зі списком", callback_data="zalx")],
                                   [InlineKeyboardButton("⬅️ Огляд", callback_data="st_ov")]])
        await _send_parts(msg, text, kb)
    elif sec == "chart":
        try:
            charts = await asyncio.to_thread(make_charts, df)
        except ImportError:
            await msg.reply_text("📈 Для графіків додай matplotlib у requirements.txt.", reply_markup=BACK_KB)
            return
        for i, (bio, caption) in enumerate(charts):
            await msg.reply_photo(photo=bio, caption=caption, reply_markup=BACK_KB if i == len(charts) - 1 else None)
    elif sec == "xls":
        bio = await asyncio.to_thread(dashboard_xlsx, df, sk)
        name = f"дашборд_{datetime.now(KYIV).strftime('%d-%m-%Y')}.xlsx"
        await msg.reply_document(document=bio, filename=name, caption="📥 Excel-дашборд (окремий файл, Google Таблицю не змінено)")
    elif sec == "sales":
        if not sk:
            await msg.reply_text(ZVIRKA_SETUP.format(sheet=SKLAD_SHEET), reply_markup=BACK_KB)
            return
        sk = await asyncio.to_thread(load_sklad, True)
        asof = _sklad_meta.get("asof")
        if not asof:
            await msg.reply_text(SALES_NO_DATE.format(sheet=SKLAD_SHEET), reply_markup=BACK_KB)
            return
        context.user_data["sales_asof"] = asof
        text = await asyncio.to_thread(build_sales_text, df, sk, asof)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Excel зі звіркою продажів", callback_data="szx")],
                                   [InlineKeyboardButton("⬅️ Огляд", callback_data="st_ov")]])
        await _send_parts(msg, text, kb)
    elif sec == "order":
        await _send_parts(msg, await asyncio.to_thread(build_order_text, df), BACK_KB)
    elif sec == "plan":
        await _send_parts(msg, await asyncio.to_thread(build_plan_text, df), BACK_KB)
    elif sec == "full":
        text = await asyncio.to_thread(build_stats)
        await _send_parts(msg, text, BACK_KB, parse_mode="HTML")


@guarded
async def zal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = await asyncio.to_thread(load_df, True)
    sk = await asyncio.to_thread(load_sklad_safe, True)
    text = await asyncio.to_thread(build_display_text, df, sk, "zal")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Excel зі списком", callback_data="zalx")]])
    await _send_parts(update.effective_message, text, kb)


@guarded
async def zal_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Готую файл…")
    df = await asyncio.to_thread(load_df)
    sk = await asyncio.to_thread(load_sklad_safe)
    bio = await asyncio.to_thread(display_xlsx, df, sk, "zal")
    await q.message.reply_document(document=bio, filename=f"не_виставлено_зал_{datetime.now(KYIV).strftime('%d-%m-%Y')}.xlsx",
                                   caption="📥 Артикули, які є в наявності, але не виставлені у вітрині зал")


@guarded
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days = 7
    if context.args and context.args[0].isdigit():
        days = max(1, min(90, int(context.args[0])))
    text = await asyncio.to_thread(build_report_text, days)
    for part in chunk_text(text):
        await update.message.reply_text(part)


@guarded
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = 10
    if context.args and context.args[0].isdigit():
        n = max(1, min(40, int(context.args[0])))
    text = await asyncio.to_thread(build_history_text, n)
    for part in chunk_text(text):
        await update.message.reply_text(part)


@guarded
async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = await asyncio.to_thread(load_df, True)
    bio = await asyncio.to_thread(table_xlsx, df)
    name = f"шини_{datetime.now(KYIV).strftime('%d-%m-%Y')}.xlsx"
    await update.message.reply_document(document=bio, filename=name, caption="📥 Поточна таблиця (окремий файл, Google Таблицю не змінено)")


ZVIRKA_SETUP = (
    "⚖️ Не бачу даних складу.\n\n"
    "Створи в цій Google Таблиці аркуш «{sheet}» з колонками: Артикул | Назва | Кількість, "
    "встав туди склад і повтори /zvirka."
)


@guarded
async def zvirka_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sk = await asyncio.to_thread(load_sklad, True)
    if not sk:
        await update.effective_message.reply_text(ZVIRKA_SETUP.format(sheet=SKLAD_SHEET))
        return
    df = await asyncio.to_thread(load_df, True)
    rows = await asyncio.to_thread(compare_sklad, df, sk)
    text = build_zvirka_text(rows)
    text += f"\n🗂 Аркуш «{SKLAD_SHEET}»: {_sklad_meta['rows']} рядків, {len(sk)} артикулів."
    if _sklad_meta["dups"]:
        d = _sklad_meta["dups"]
        text += f"\nℹ️ {len(d)} артикулів на аркуші повторюються, кількості підсумовано: " + ", ".join(d[:6]) + ("…" if len(d) > 6 else "")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Excel зі звіркою", callback_data="zvx")]])
    parts = chunk_text(text)
    for i, part in enumerate(parts):
        await update.effective_message.reply_text(part, reply_markup=kb if i == len(parts) - 1 else None)


SALES_NO_DATE = (
    "💰 Не бачу дати, коли вставлено склад.\n\n"
    "Допиши на аркуші «{sheet}» у верхній порожній клітинці (наприклад, E1) текст на кшталт "
    "«Склад станом на: 31.10.2026 08:30» або вкажи дату в команді: /zvirka_prodazhiv 31.10.2026 08:30"
)


@guarded
async def sales_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    sk = await asyncio.to_thread(load_sklad, True)
    if not sk:
        await msg.reply_text(ZVIRKA_SETUP.format(sheet=SKLAD_SHEET))
        return
    asof = parse_asof_args(context.args) or _sklad_meta.get("asof")
    if not asof:
        await msg.reply_text(SALES_NO_DATE.format(sheet=SKLAD_SHEET))
        return
    context.user_data["sales_asof"] = asof
    df = await asyncio.to_thread(load_df, True)
    text = await asyncio.to_thread(build_sales_text, df, sk, asof)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Excel зі звіркою продажів", callback_data="szx")]])
    await _send_parts(msg, text, kb)


@guarded
async def sales_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Готую файл…")
    asof = context.user_data.get("sales_asof") or _sklad_meta.get("asof")
    sk = await asyncio.to_thread(load_sklad, True)
    if not sk or not asof:
        await q.message.reply_text(SALES_NO_DATE.format(sheet=SKLAD_SHEET))
        return
    df = await asyncio.to_thread(load_df, True)
    bio = await asyncio.to_thread(sales_xlsx, df, sk, asof)
    await q.message.reply_document(document=bio, filename=f"звірка_продажів_{datetime.now(KYIV).strftime('%d-%m-%Y')}.xlsx",
                                   caption="📥 Звірка продажів: склад − продажі проти таблиці локацій")


@guarded
async def zvirka_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Готую файл…")
    sk = await asyncio.to_thread(load_sklad, True)
    if not sk:
        await q.message.reply_text(ZVIRKA_SETUP.format(sheet=SKLAD_SHEET))
        return
    df = await asyncio.to_thread(load_df, True)
    rows = await asyncio.to_thread(compare_sklad, df, sk)
    bio = await asyncio.to_thread(zvirka_xlsx, rows)
    name = f"звірка_{datetime.now(KYIV).strftime('%d-%m-%Y')}.xlsx"
    await q.message.reply_document(document=bio, filename=name, caption="📥 Звірка складу й таблиці локацій")


async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        text = await asyncio.to_thread(build_daily_report)
    except Exception as e:
        print(f"⚠️ Помилка ранкового звіту: {e}")
        return
    await notify_chats(context, text)
    if datetime.now(KYIV).weekday() == 0:  # понеділок: підсумок тижня
        try:
            df = await asyncio.to_thread(load_df)
            sk = await asyncio.to_thread(load_sklad_safe)
            week = await asyncio.to_thread(build_overview, df, sk)
            move = await asyncio.to_thread(build_movement_text, df, sk)
            await notify_chats(context, "📅 Підсумок тижня\n\n" + week.replace("\n\nОбери розділ нижче 👇", "") + "\n\n" + move)
        except Exception as e:
            print(f"⚠️ Помилка тижневого звіту: {e}")


# ----------------- ЛОКАЦІЇ ТА ПАЛЕТИ -----------------
async def show_location(msg, context, key):
    df = await asyncio.to_thread(load_df)
    text, kb, _rows = build_location_view(df, key)
    parts = chunk_text(text)
    for i, part in enumerate(parts):
        await msg.reply_text(part, reply_markup=kb if i == len(parts) - 1 else None)


@guarded
async def pallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = resolve_loc_key(" ".join(context.args)) if context.args else None
    if not key:
        await update.message.reply_text("📦 Обери палету або локацію:", reply_markup=palette_keyboard("pal"))
        return
    await show_location(update.message, context, key)


@guarded
async def pallet_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await show_location(q.message, context, q.data.split("_", 1)[1])


@guarded
async def pallets_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("📦 Обери палету або локацію:", reply_markup=palette_keyboard("pal"))


# ----------------- ПОШУК І КАРТКИ -----------------
async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    msg = update.effective_message
    df = await asyncio.to_thread(load_df)
    codes, note = search_codes(df, query)
    if not codes:
        clean = query.strip()
        code = clean if clean.isdigit() and len(clean) >= 4 else "0"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Додати як нову шину", callback_data=f"addnew_{code}")]])
        await msg.reply_text(f"❌ За запитом «{query}» нічого не знайдено.", reply_markup=kb)
        return
    if note:
        await msg.reply_text(note)
    if len(codes) <= MAX_CARDS:
        first = df[df["code"] == codes[0]]
        photo_path = get_tire_photo(first.iloc[0]["name"]) if len(first) else None
        if photo_path and os.path.exists(photo_path):
            try:
                with open(photo_path, "rb") as f:
                    await msg.reply_photo(photo=f, caption=f"📷 {short_name(first.iloc[0]['name'])}")
            except Exception as e:
                print(f"⚠️ Не вдалося надіслати фото: {e}")
        for c in codes:
            await send_card(msg.reply_text, context, c)
    else:
        extra = f" (показано {MAX_LIST})" if len(codes) > MAX_LIST else ""
        await msg.reply_text(f"🔍 Знайшов {len(codes)} артикулів{extra}. Обери потрібний або уточни запит:",
                             reply_markup=list_keyboard(df, codes))


@guarded
async def open_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    code = q.data.split("_", 1)[1]
    await send_card(q.message.reply_text, context, code)


@guarded
async def refresh_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Оновлено")
    code = q.data.split("_", 1)[1]
    text, kb = await card_payload(context, code, force=True)
    await safe_edit(q, text, kb)


@guarded
async def row_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, row_s = q.data.split("_", 1)
    row = int(row_s)
    if action == "set":
        await q.answer()
        context.user_data["waiting_for_row"] = {"row": row, "chat": q.message.chat_id, "msg": q.message.message_id}
        await q.message.reply_text("⌨️ Надішли нову точну кількість для цієї локації (тільки число):")
        return
    code, old, new = await asyncio.to_thread(change_qty, q.from_user, row, action)
    df_now = await asyncio.to_thread(load_df)
    prow = df_now[df_now["row"] == row]
    warn = pallet_warning(df_now, prow.iloc[0]["pallet"]) if len(prow) else ""
    await q.answer(f"{old} → {new} шт." + (f"\n{warn}" if warn else ""), show_alert=bool(warn and action == "inc"))
    await after_change(context, q.from_user, row, code, old, new)
    text, kb = await card_payload(context, code)
    await safe_edit(q, text, kb)


@guarded
async def undo_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    row = int(q.data.split("_", 1)[1])
    last = context.application.bot_data.get("last", {})
    entry = last.get(row)
    if not entry or _time.time() - entry["ts"] > UNDO_WINDOW:
        await q.answer("Нічого відміняти (минуло забагато часу).", show_alert=True)
        return

    def _undo():
        ws = get_worksheet()
        vals = (ws.row_values(row) + [""] * 5)[:5]
        if str(vals[1]).strip().replace(".0", "") != entry["code"]:
            invalidate_cache()
            raise StaleRow()
        cur = to_int(vals[4])
        if cur != entry["new"]:
            return None
        ws.update_cell(row, 5, entry["old"])
        cache_set_qty(row, entry["old"])
        log_to_history(q.from_user, entry["code"], "undo", cur, entry["old"], vals[0])
        return cur

    cur = await asyncio.to_thread(_undo)
    if cur is None:
        await q.answer("Після цієї зміни кількість змінювали ще раз, тому відміна небезпечна.", show_alert=True)
        return
    last.pop(row, None)
    await q.answer(f"Повернув {cur} → {entry['old']}")
    text, kb = await card_payload(context, entry["code"], force=True)
    await safe_edit(q, text, kb)


MENU_PROMPT = "🔍 Надішли артикул, розмір (наприклад 205/55R16), назву чи бренд — або фото цінника."


@guarded
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    # кнопки меню
    if text == "🔍 Пошук":
        await update.message.reply_text(MENU_PROMPT)
        return
    if text == "📦 Палети":
        await update.message.reply_text("📦 Обери палету або локацію:", reply_markup=palette_keyboard("pal"))
        return
    if text == "📊 Статистика":
        return await stats_command(update, context)
    if text == "⚖️ Звірка":
        return await zvirka_command(update, context)
    if text == "💰 Звірка продажів" or text.lower() in ("звірка продажів", "звірка продажу"):
        context.args = []
        return await sales_check_command(update, context)
    if text == "📋 Звіт":
        context.args = []
        return await report_command(update, context)
    if text == "📥 Експорт":
        return await export_command(update, context)
    if text == "❓ Допомога":
        await update.message.reply_text(HELP_TEXT, reply_markup=MENU)
        return

    # очікуємо точну кількість
    w = context.user_data.get("waiting_for_row")
    if w:
        if re.fullmatch(r"\d+", text):
            if int(text) > MAX_QTY:
                await update.message.reply_text(f"⚠️ {text} шт. — забагато, схоже на артикул. Введи кількість (до {MAX_QTY}) ще раз:")
                return
            context.user_data.pop("waiting_for_row", None)
            code, old, new = await asyncio.to_thread(change_qty, update.effective_user, w["row"], "set", int(text))
            await after_change(context, update.effective_user, w["row"], code, old, new)
            df_now = await asyncio.to_thread(load_df)
            prow = df_now[df_now["row"] == w["row"]]
            warn = pallet_warning(df_now, prow.iloc[0]["pallet"]) if len(prow) else ""
            await update.message.reply_text(f"✅ {code}: було {old}, стало {new} шт." + (f"\n{warn}" if warn else ""))
            card_text, kb = await card_payload(context, code)
            try:
                await context.bot.edit_message_text(card_text, chat_id=w["chat"], message_id=w["msg"], reply_markup=kb)
            except Exception:
                await update.message.reply_text(card_text, reply_markup=kb)
            return
        context.user_data.pop("waiting_for_row", None)  # це не число — вважаємо новим пошуком

    # «палета 27», «зал», «фасад»
    low = text.lower()
    m = re.fullmatch(r"(?:палета|палет|пал\.?|№)\s*(\d+)", low)
    key = m.group(1) if m else (resolve_loc_key(low) if low in SPECIAL_LOCS.values() or low in SPECIAL_LOCS else None)
    if key:
        await show_location(update.message, context, key)
        return

    await run_search(update, context, text)


@guarded
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not openai_client:
        await update.message.reply_text("⚠️ Пошук по фото не налаштовано. Надішли артикул текстом.")
        return
    msg = await update.message.reply_text("🔍 Аналізую цінник і шукаю артикул...")
    file_path = f"photo_{update.message.chat_id}_{update.message.message_id}.jpg"

    def ocr():
        with open(file_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Знайди на цьому ціннику 8-значний артикул після слів 'арт.' або 'арт'. Поверни ТІЛЬКИ ці цифри і нічого більше. Якщо немає, поверни 0."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
            max_tokens=20,
        )
        return resp.choices[0].message.content.strip()

    try:
        photo_file = await update.message.photo[-1].get_file()
        await photo_file.download_to_drive(file_path)
        code = re.sub(r"\D", "", await asyncio.to_thread(ocr))
        if not code or code == "0":
            await msg.edit_text("❌ Не вдалося знайти артикул на фото.")
            return
        await msg.edit_text(f"🏷 Розпізнано артикул: {code}")
        await run_search(update, context, code)
    except Exception as e:
        await msg.edit_text(friendly_error(e))
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙 Голосові повідомлення не підтримуються. Напиши текстом.")


async def cancel_plain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("waiting_for_row", None)
    await update.message.reply_text("Скасовано.", reply_markup=MENU)


# ----------------- ДОДАВАННЯ ШИНИ (/add): артикул → палета → кількість -----------------
CODE, NEWCONFIRM, SIZE, NAME, PALLET, QTY, CONFIRM = range(7)


def parse_pallet(text):
    m = re.fullmatch(r"(?:палет[аиуі]?\s*)?№?\s*(\d+)", text.strip(), re.IGNORECASE)
    return m.group(1) if m else text.strip()


def locations_text(rows):
    return "\n".join(f"• {loc_label(r['pallet'])} — {int(r['q'])} шт." for _, r in rows.head(10).iterrows())


def find_same(df, code, pallet):
    same = df[(df["code"] == code) & (df["pallet"].str.strip().str.lower() == pallet.strip().lower())]
    return [int(x) for x in same["row"]]


@guarded
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("waiting_for_row", None)
    context.user_data["new"] = {}
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        code = q.data.split("_", 1)[1]
        if code.isdigit() and code != "0":
            context.user_data["new"]["code"] = code
            await q.message.reply_text(f"➕ Нова шина, артикул {code} (/cancel — скасувати).\n📏 Розмір? (наприклад 205/55 R16)")
            return SIZE
        await q.message.reply_text("➕ Додаємо шину (/cancel — скасувати).\n🔢 Артикул?")
        return CODE
    await update.message.reply_text("➕ Додаємо шину (/cancel — скасувати).\n🔢 Артикул?")
    return CODE


@guarded
async def add_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = re.sub(r"\D", "", update.message.text or "")
    if len(code) < 4:
        await update.message.reply_text("⚠️ Артикул має містити цифри (мінімум 4). Спробуй ще раз:")
        return CODE
    new = context.user_data.setdefault("new", {})
    new["code"] = code
    df = await asyncio.to_thread(load_df)
    rows = code_rows(df, code)
    if not rows.empty:
        first = rows.iloc[0]
        new["size"], new["name"] = first["size"], first["name"]
        await update.message.reply_text(
            "✅ Знайдено в таблиці:\n"
            f"• Артикул: {code}\n• Розмір: {first['size']}\n• Назва: {short_name(first['name'])}\n\n"
            f"Вже є:\n{locations_text(rows)}\n\n"
            f"{suggest_text(suggest_pallets(df, code, 1), 1).replace('Куди покласти 1 шт.', 'Де є місце')}\n\n📍 На яку палету додаємо?"
        )
        return PALLET
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Додати як нову", callback_data="nt_yes"),
        InlineKeyboardButton("❌ Скасувати", callback_data="nt_no"),
    ]])
    await update.message.reply_text(f"❌ Артикула {code} в таблиці немає. Додати як нову шину?", reply_markup=kb)
    return NEWCONFIRM


@guarded
async def add_new_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    new = context.user_data.get("new")
    if q.data == "nt_no" or not new:
        context.user_data.pop("new", None)
        await q.edit_message_text("❌ Скасовано.")
        return ConversationHandler.END
    await q.edit_message_text(f"➕ Нова шина, артикул {new['code']}")
    await q.message.reply_text("📏 Розмір? (наприклад 205/55 R16)")
    return SIZE


@guarded
async def add_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("new", {})["size"] = (update.message.text or "").strip()
    await update.message.reply_text("🏷 Назва (бренд, модель, сезон)?")
    return NAME


@guarded
async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if not name.lower().startswith("автошина"):
        name = f"Автошина {name}"
    context.user_data.setdefault("new", {})["name"] = name
    df = await asyncio.to_thread(load_df)
    nw = context.user_data["new"]
    sug = suggest_text(suggest_pallets(df, nw.get("code", ""), 1, size=size_of(nw.get("size", ""))), 1).replace("Куди покласти 1 шт.", "Де є місце")
    await update.message.reply_text(f"{sug}\n\n📍 На яку палету додаємо?")
    return PALLET


@guarded
async def add_pallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("⚠️ Вкажи палету або локацію:")
        return PALLET
    context.user_data.setdefault("new", {})["pallet"] = parse_pallet(text)
    await update.message.reply_text("🔢 Кількість (шт.)?")
    return QTY


@guarded
async def add_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.fullmatch(r"\s*(\d+)\s*", update.message.text or "")
    if not m or int(m.group(1)) > MAX_QTY:
        await update.message.reply_text(f"⚠️ Введи кількість числом (до {MAX_QTY}):")
        return QTY
    new = context.user_data.setdefault("new", {})
    new["qty"] = int(m.group(1))
    if not all(k in new for k in ("pallet", "code", "size", "name")):
        await update.message.reply_text("⚠️ Щось пішло не так. Почни спочатку: /add")
        context.user_data.pop("new", None)
        return ConversationHandler.END

    df = await asyncio.to_thread(load_df)
    sk = await asyncio.to_thread(load_sklad_safe)
    dup = find_same(df, new["code"], new["pallet"])
    cap_note = ""
    if str(new["pallet"]).isdigit():
        t = pallet_totals(df).get(str(new["pallet"]), 0)
        after = t + new["qty"]
        if after > PALLET_MAX:
            cap_note = (f"\n\n⚠️ Палета {new['pallet']}: зараз {t}/{PALLET_MAX}, після додавання буде {after}/{PALLET_MAX}.\n"
                        + suggest_text(suggest_pallets(df, new["code"], new["qty"], exclude={str(new["pallet"])}, size=size_of(new["size"])), new["qty"]))
        else:
            cap_note = f"\n\n{load_icon(after)} Палета {new['pallet']}: {t} → {after}/{PALLET_MAX}"

    summary = (
        "Перевір і підтверди:\n\n"
        f"📍 Палета: {new['pallet']}\n"
        f"• Артикул: {new['code']}\n"
        f"• Розмір: {new['size']}\n"
        f"• Назва: {short_name(new['name'])}\n"
        f"• Кількість: {new['qty']} шт."
    )
    summary += cap_note
    if sk is not None and new["code"] not in sk:
        summary += "\n\n⚠️ Такого артикула немає в базі складу. Переконайся, що код правильний."
    if dup:
        new["merge_row"] = dup[0]
        summary += (f"\n\n⚠️ Цей артикул на палеті {new['pallet']} уже є (рядок {dup[0]}). "
                    "Додати кількість до наявного рядка чи створити окремий?")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"➕ Додати +{new['qty']} до наявного", callback_data="add_merge")],
            [InlineKeyboardButton("📄 Окремий новий рядок", callback_data="add_ok")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="add_no")],
        ])
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Додати", callback_data="add_ok"),
            InlineKeyboardButton("❌ Скасувати", callback_data="add_no"),
        ]])
    await update.message.reply_text(summary, reply_markup=kb)
    return CONFIRM


@guarded
async def add_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    new = context.user_data.pop("new", None)
    if q.data == "add_no" or not new:
        await q.edit_message_text("❌ Скасовано.")
        return ConversationHandler.END
    try:
        if q.data == "add_merge" and new.get("merge_row"):
            code, old, total = await asyncio.to_thread(merge_qty, q.from_user, new["merge_row"], new["qty"])
            await q.edit_message_text(
                f"✅ Додано {new['qty']} шт. до рядка {new['merge_row']} (палета {new['pallet']}, арт. {code}): "
                f"було {old}, стало {total}."
            )
        else:
            row = await asyncio.to_thread(add_tire, q.from_user, new["pallet"], new["code"], new["size"], new["name"], new["qty"])
            await q.edit_message_text(
                f"✅ Додано в рядок {row}: {short_name(new['name'])} ({new['size']}), арт. {new['code']}, "
                f"палета {new['pallet']}, {new['qty']} шт."
            )
        await send_card(q.message.reply_text, context, new["code"], force=True)
    except Exception as e:
        await q.edit_message_text(friendly_error(e))
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new", None)
    await update.message.reply_text("❌ Додавання скасовано.", reply_markup=MENU)
    return ConversationHandler.END


async def add_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new", None)
    return ConversationHandler.END


# ----------------- ІНВЕНТАРИЗАЦІЯ ПАЛЕТИ (/inventory) -----------------
INV_ASK, INV_CONFIRM = 20, 21


def inv_keyboard(old):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Так, {old} шт.", callback_data="ivok")],
        [InlineKeyboardButton("⏭ Пропустити", callback_data="ivskip"), InlineKeyboardButton("🛑 Завершити", callback_data="ivend")],
    ])


async def inv_strip_buttons(context, inv):
    if inv.get("msg"):
        try:
            await context.bot.edit_message_reply_markup(chat_id=inv["chat"], message_id=inv["msg"], reply_markup=None)
        except Exception:
            pass


async def inv_ask(context, inv):
    items, idx = inv["items"], inv["idx"]
    if idx >= len(items):
        return await inv_finish(context, inv)
    it = items[idx]
    text = (
        f"🧮 Інвентаризація: {inv['label']} ({idx + 1}/{len(items)})\n\n"
        f"🛞 {it['name']}\n🔢 {it['code']}   📏 {it['size']}\n"
        f"📋 У таблиці: {it['old']} шт.\n\n"
        "Скільки лежить насправді? Напиши число або натисни кнопку."
    )
    sent = await context.bot.send_message(inv["chat"], text, reply_markup=inv_keyboard(it["old"]))
    inv["msg"] = sent.message_id
    return INV_ASK


async def inv_finish(context, inv):
    diffs = [(it, inv["res"][it["row"]]) for it in inv["items"] if it["row"] in inv["res"] and inv["res"][it["row"]] != it["old"]]
    checked = len(inv["res"])
    if not diffs:
        await context.bot.send_message(inv["chat"], f"✅ Інвентаризація {inv['label']}: перевірено {checked} позицій, розбіжностей немає.", reply_markup=MENU)
        inv["done"] = True
        await asyncio.to_thread(log_inventory_done, inv["user"], inv["label"], checked, 0)
        return ConversationHandler.END
    lines = [f"🧮 Підсумок інвентаризації: {inv['label']}\nПеревірено: {checked} з {len(inv['items'])} позицій.\n\nРозбіжності:"]
    for it, new in diffs:
        lines.append(f"• {it['code']} · {it['size']}: {it['old']} → {new} ({new - it['old']:+d})")
    lines.append("\nЗастосувати ці зміни в таблиці?")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Застосувати", callback_data="ivapply"),
        InlineKeyboardButton("❌ Скасувати", callback_data="ivcancel"),
    ]])
    inv["diffs"] = [(it["row"], it["code"], it["old"], new) for it, new in diffs]
    await context.bot.send_message(inv["chat"], "\n".join(lines), reply_markup=kb)
    return INV_CONFIRM


async def inv_begin(update: Update, context: ContextTypes.DEFAULT_TYPE, key):
    context.user_data.pop("waiting_for_row", None)
    chat_id = update.effective_chat.id
    if update.callback_query:
        await update.callback_query.answer()
    df = await asyncio.to_thread(load_df, True)
    rows = location_rows(df, key)
    if rows.empty:
        await context.bot.send_message(chat_id, f"📦 {loc_name_of_key(key)}: нічого інвентаризувати.")
        return ConversationHandler.END
    items = [{"row": int(r["row"]), "code": r["code"], "size": r["size"], "name": short_name(r["name"]), "old": int(r["q"])}
             for _, r in rows.iterrows()]
    inv = {"key": key, "label": loc_name_of_key(key), "items": items, "idx": 0, "res": {}, "chat": chat_id, "msg": None,
           "user": update.effective_user}
    context.user_data["inv"] = inv
    await context.bot.send_message(chat_id, f"🧮 Починаємо інвентаризацію: {inv['label']}, {len(items)} позицій. /cancel — перервати без змін.")
    return await inv_ask(context, inv)


@guarded
async def inv_entry_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = resolve_loc_key(" ".join(context.args)) if context.args else None
    if not key:
        await update.message.reply_text("🧮 Інвентаризація: обери палету або локацію:", reply_markup=palette_keyboard("invs"))
        return ConversationHandler.END
    return await inv_begin(update, context, key)


@guarded
async def inv_entry_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await inv_begin(update, context, update.callback_query.data.split("_", 1)[1])


async def _inv_current(context):
    inv = context.user_data.get("inv")
    if not inv or inv["idx"] >= len(inv["items"]):
        return None, None
    return inv, inv["items"][inv["idx"]]


@guarded
async def inv_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    inv, it = await _inv_current(context)
    if not inv:
        return ConversationHandler.END
    await inv_strip_buttons(context, inv)
    if q.data == "ivend":
        return await inv_finish(context, inv)
    if q.data == "ivok":
        inv["res"][it["row"]] = it["old"]
    inv["idx"] += 1
    return await inv_ask(context, inv)


@guarded
async def inv_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inv, it = await _inv_current(context)
    if not inv:
        return ConversationHandler.END
    m = re.fullmatch(r"\s*(\d+)\s*", update.message.text or "")
    if not m or int(m.group(1)) > MAX_QTY:
        await update.message.reply_text(f"⚠️ Напиши кількість числом (до {MAX_QTY}) або натисни кнопку.")
        return INV_ASK
    await inv_strip_buttons(context, inv)
    inv["res"][it["row"]] = int(m.group(1))
    inv["idx"] += 1
    return await inv_ask(context, inv)


@guarded
async def inv_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    inv = context.user_data.pop("inv", None)
    if q.data == "ivcancel" or not inv:
        await q.edit_message_text("❌ Зміни не застосовано.")
        return ConversationHandler.END
    try:
        n = await asyncio.to_thread(apply_qty_batch, q.from_user, inv["diffs"], "inv", inv["label"])
        await asyncio.to_thread(log_inventory_done, q.from_user, inv["label"], len(inv["res"]), n)
        await q.edit_message_text(f"✅ Оновлено {n} позицій ({inv['label']}).")
        for _row, code, old, new in inv["diffs"]:
            if new < old:
                df = await asyncio.to_thread(load_df)
                total = code_total(df, code)
                if total <= LOW_STOCK < total + (old - new):
                    await notify_chats(context, f"🔻 Мінімальний залишок: {code} — тепер {total} шт. (інвентаризація, {user_label(q.from_user)})")
    except Exception as e:
        await q.edit_message_text(friendly_error(e))
    return ConversationHandler.END


async def inv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("inv", None)
    await update.message.reply_text("❌ Інвентаризацію перервано, змін у таблиці немає.", reply_markup=MENU)
    return ConversationHandler.END


async def inv_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("inv", None)
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"⚠️ Сталася помилка при обробці оновлення: {context.error}")


async def post_init(app):
    try:
        await app.bot.set_my_commands([
            ("start", "Меню й довідка"),
            ("add", "Додати шину"),
            ("pallet", "Що лежить на палеті"),
            ("where", "Куди покласти шини"),
            ("unload", "Перевантажені палети"),
            ("plan", "Які палети перевірити"),
            ("zakupivlya", "Що закінчується"),
            ("inventory", "Інвентаризація палети"),
            ("stats", "Статистика й огляд"),
            ("zal", "Вітрина зал: що не виставлено"),
            ("zvirka", "Звірка зі складом"),
            ("zvirka_prodazhiv", "Звірка продажів"),
            ("report", "Звіт за N днів"),
            ("history", "Останні зміни"),
            ("export", "Вивантажити в Excel"),
            ("myid", "Мій ID для звітів"),
            ("cancel", "Скасувати дію"),
        ])
    except Exception as e:
        print(f"⚠️ Не вдалося встановити команди: {e}")


# ================= ПАЛЕТИ: ЛІМІТ, ПІДКАЗКИ, ПЕРЕМІЩЕННЯ, ЗАКУПІВЛЯ, ПЛАН ПЕРЕВІРОК =================
PALLET_MAX = int(os.environ.get("PALLET_MAX", "16"))     # бажаний максимум шин на палеті 1–44


def pallet_totals(df):
    g = df.groupby(df["pallet"].str.strip())["q"].sum()
    return {str(n): int(g.get(str(n), 0)) for n in range(1, 45)}


def load_icon(total):
    if total <= 0:
        return "⚪"
    if total <= PALLET_MAX * 0.75:
        return "🟢"
    if total <= PALLET_MAX:
        return "🟡"
    return "🔴"


def load_tag(pallet, totals):
    p = str(pallet).strip()
    if not p.isdigit() or p not in totals:
        return ""
    return f"  {load_icon(totals[p])} {totals[p]}/{PALLET_MAX}"


def pallet_warning(df, pallet):
    p = str(pallet).strip()
    if not p.isdigit():
        return ""
    t = pallet_totals(df).get(p, 0)
    return f"⚠️ Палета {p}: {t}/{PALLET_MAX} (понад ліміт на {t - PALLET_MAX})" if t > PALLET_MAX else ""


def suggest_pallets(df, code, qty, exclude=(), limit=3, size=None):
    """Куди покласти qty шт.: спершу палета з тим самим артикулом, потім з тим самим розміром, потім будь-яка з місцем."""
    totals = pallet_totals(df)
    rows = df[df["code"] == code]
    if size is None and len(rows):
        size = size_of(rows.iloc[0]["size"])
    same_code = {p.strip() for p in rows[rows["q"] > 0]["pallet"]}
    en = df[df["q"] > 0]
    same_size = {p.strip() for p in en[en["size"].apply(size_of) == size]["pallet"]} if size else set()
    cands = []
    for n, tot in totals.items():
        free = PALLET_MAX - tot
        if n in exclude or free <= 0:
            continue
        if n in same_code:
            score, why = 0, "там уже є цей артикул"
        elif n in same_size:
            score, why = 1, "той самий розмір"
        elif tot == 0:
            score, why = 3, "порожня"
        else:
            score, why = 2, "є вільне місце"
        cands.append({"pallet": n, "free": free, "why": why, "score": score, "fits": free >= qty})
    cands.sort(key=lambda c: (not c["fits"], c["score"], -c["free"], int(c["pallet"])))
    return cands[:limit]


def suggest_text(cands, qty):
    if not cands:
        return "💡 Вільних місць на палетах 1–44 немає, розглянь вітрину чи фасад."
    lines = [f"💡 Куди покласти {qty} шт.:"]
    for c in cands:
        mark = "" if c["fits"] else f" (влізе лише {c['free']})"
        lines.append(f"• палета {c['pallet']} — вільно {c['free']}, {c['why']}{mark}")
    if not any(c["fits"] for c in cands):
        lines.append("Жодна палета не вміщає все одразу: розділи між кількома.")
    return "\n".join(lines)


def unload_plan(df, n, max_moves=4):
    totals = pallet_totals(df)
    excess = totals.get(str(n), 0) - PALLET_MAX
    if excess <= 0:
        return []
    rows = df[(df["pallet"].str.strip() == str(n)) & (df["q"] > 0)].sort_values("q")
    plan, left = [], excess
    for _, r in rows.iterrows():
        if left <= 0 or len(plan) >= max_moves:
            break
        move = min(int(r["q"]), left)
        sug = suggest_pallets(df, r["code"], move, exclude={str(n)}, limit=1)
        plan.append({"code": r["code"], "size": r["size"], "name": short_name(r["name"]), "qty": move, "to": sug[0] if sug else None})
        left -= move
    return plan


def build_unload_text(df, only=None):
    totals = pallet_totals(df)
    over = [n for n, t in totals.items() if t > PALLET_MAX and (only is None or n == str(only))]
    if not over:
        return f"✅ Перевантажених палет немає (ліміт {PALLET_MAX} шт.)." if only is None else f"✅ Палета {only}: в межах ліміту ({totals.get(str(only), 0)}/{PALLET_MAX})."
    lines = [f"🧭 Перевантажені палети (ліміт {PALLET_MAX}): {len(over)}\n"]
    for n in sorted(over, key=int):
        lines.append(f"🔴 Палета {n}: {totals[n]}/{PALLET_MAX}, надлишок {totals[n] - PALLET_MAX}")
        for m in unload_plan(df, n):
            to = f"палета {m['to']['pallet']} (вільно {m['to']['free']}, {m['to']['why']})" if m["to"] else "місця на палетах немає"
            lines.append(f"   → {m['qty']} шт. {m['code']} · {m['size']} · {m['name'][:22]} → {to}")
    lines.append("\nПеренести можна кнопкою «🔁 Перемістити» в картці шини.")
    return "\n".join(lines)


def order_rows(df, horizon=14):
    cd30 = covered_days(30)
    if cd30 < 3:
        return None
    cons = consumption(hist_moves(days=30))
    tot = df.groupby("code")["q"].sum().to_dict()
    names = df.drop_duplicates("code").set_index("code")["name"].to_dict()
    sizes = df.drop_duplicates("code").set_index("code")["size"].to_dict()
    out = []
    for c, v in cons.items():
        per_day = v / cd30
        stock = int(tot.get(c, 0))
        left = stock / per_day if per_day > 0 else 999
        if left < horizon:
            out.append({"code": c, "name": short_name(names.get(c, "")), "size": size_of(sizes.get(c, "")), "stock": stock,
                        "per_day": per_day, "left": left, "need": max(0, math.ceil(per_day * 30) - stock)})
    out.sort(key=lambda r: (r["left"], r["code"]))
    return out


def build_order_text(df, sk=None):
    rows = order_rows(df)
    if rows is None:
        return "🛒 Закупівля: історія змін ще замала (потрібно хоча б 3 дні), щоб оцінити витрату."
    cd = covered_days(30)
    lines = [f"🛒 Що закінчується (вистачить менше ніж на 14 днів)\nВитрата за {cd} дн. (мінуси через бот, без інвентаризацій і переміщень).\n"]
    if not rows:
        lines.append("✅ Нічого не закінчується найближчими 14 днями.")
    for r in rows[:20]:
        when = "немає на залишку" if r["stock"] == 0 else f"вистачить ≈ {r['left']:.0f} дн."
        lines.append(f"• {r['code']} · {r['size']} · {r['name'][:24]} — залишок {r['stock']}, ≈{r['per_day']:.1f}/день, {when}; докупити до 30 дн.: {r['need']}")
    if len(rows) > 20:
        lines.append(f"… і ще {len(rows) - 20}.")
    return "\n".join(lines)


def inventory_plan(df, n=3):
    last = {}
    for h in read_history():
        if h.get("loc") and h["action"] in (ACTION_TEXT["inv"], ACTION_TEXT["invdone"]):
            last[h["loc"]] = max(last.get(h["loc"], h["ts"]), h["ts"])
    totals = pallet_totals(df)
    now = datetime.now(KYIV).replace(tzinfo=None)
    cands = []
    for p, tot in totals.items():
        if df[df["pallet"].str.strip() == p].empty:
            continue
        lastd = last.get(f"палета {p}")
        days = (now - lastd).days if lastd else None
        score = (10000 + tot) if days is None else days
        reason = "ще не перевірялась через бота" if days is None else f"остання перевірка {days} дн. тому"
        if tot > PALLET_MAX:
            score += 100
            reason += f", перевантажена {tot}/{PALLET_MAX}"
        cands.append((score, p, reason))
    cands.sort(reverse=True)
    return [(p, reason) for _, p, reason in cands[:n]]


def build_plan_text(df, sk=None):
    plan = inventory_plan(df, 4)
    lines = ["🧮 План інвентаризації: перевір найближчим часом\n"]
    for p, reason in plan:
        lines.append(f"• палета {p} — {reason}  (/inventory {p})")
    lines.append("\nОрієнтуюсь на записи про інвентаризації через бота: що давно не перевіряли, ті палети першими.")
    return "\n".join(lines)


def pallet_pdf(df, key):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    rows = location_rows(df, key)
    label = loc_name_of_key(key).capitalize()
    total = int(rows["q"].sum())
    cap = f"{total}/{PALLET_MAX}" if key.isdigit() else f"{total} шт."
    per_page = 26
    items = [[str(i + 1), r["code"], r["size"], short_name(r["name"])[:44], str(int(r["q"])), ""] for i, (_, r) in enumerate(rows.iterrows())]
    pages = max(1, math.ceil(len(items) / per_page))
    bio = io.BytesIO()
    with PdfPages(bio) as pdf:
        for pg in range(pages):
            fig = plt.figure(figsize=(8.27, 11.69))
            ax = fig.add_axes([0.05, 0.05, 0.9, 0.82])
            ax.axis("off")
            fig.text(0.05, 0.94, label, fontsize=30, fontweight="bold")
            fig.text(0.05, 0.905, f"Заповнення: {cap} · {len(rows)} позицій · {datetime.now(KYIV).strftime('%d.%m.%Y %H:%M')}", fontsize=11, color="#444444")
            chunk = items[pg * per_page:(pg + 1) * per_page]
            if chunk:
                tbl = ax.table(cellText=chunk, colLabels=["№", "Артикул", "Розмір", "Модель", "У таблиці", "Факт"],
                               colWidths=[0.05, 0.13, 0.13, 0.44, 0.1, 0.12], loc="upper center", cellLoc="left")
                tbl.auto_set_font_size(False)
                tbl.set_fontsize(9)
                tbl.scale(1, 1.9)
                for (r_i, c_i), cell in tbl.get_celld().items():
                    if r_i == 0:
                        cell.set_facecolor("#1F3864")
                        cell.set_text_props(color="white", fontweight="bold")
            if pages > 1:
                fig.text(0.9, 0.02, f"{pg + 1}/{pages}", fontsize=9)
            pdf.savefig(fig)
            plt.close(fig)
    bio.seek(0)
    return bio


# ---------- команди ----------
@guarded
async def where_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    code = re.sub(r"\D", "", args[0]) if args else ""
    qty = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    if len(code) < 4:
        await update.effective_message.reply_text("💡 Напиши так: /where 40745801 8 (артикул і скільки шт. покласти).")
        return
    df = await asyncio.to_thread(load_df)
    if df[df["code"] == code].empty:
        await update.effective_message.reply_text(f"❓ Артикула {code} немає в таблиці. Спочатку додай його через /add.")
        return
    text = suggest_text(suggest_pallets(df, code, qty), qty)
    await update.effective_message.reply_text(text)


@guarded
async def unload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = await asyncio.to_thread(load_df, True)
    await _send_parts(update.effective_message, await asyncio.to_thread(build_unload_text, df))


@guarded
async def unload_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    n = q.data.split("_", 1)[1]
    df = await asyncio.to_thread(load_df)
    await _send_parts(q.message, await asyncio.to_thread(build_unload_text, df, n))


@guarded
async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = await asyncio.to_thread(load_df)
    await update.effective_message.reply_text(await asyncio.to_thread(build_plan_text, df))


@guarded
async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = await asyncio.to_thread(load_df)
    await _send_parts(update.effective_message, await asyncio.to_thread(build_order_text, df))


@guarded
async def pallet_pdf_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Готую PDF…")
    key = q.data.split("_", 1)[1]
    df = await asyncio.to_thread(load_df, True)
    try:
        bio = await asyncio.to_thread(pallet_pdf, df, key)
    except ImportError:
        await q.message.reply_text("📄 Для PDF додай matplotlib у requirements.txt.")
        return
    await q.message.reply_document(document=bio, filename=f"картка_{key}_{datetime.now(KYIV).strftime('%d-%m-%Y')}.pdf",
                                   caption=f"📄 Картка: {loc_name_of_key(key)} (для друку й перевірки)")


# ---------- переміщення між локаціями ----------
MV_FROM, MV_TO, MV_QTY, MV_CONFIRM = 30, 31, 32, 33


def mv_dest_value(key):
    return key if key.isdigit() else SPECIAL_LOCS[key]


def do_move(user, mv):
    ws = get_worksheet()
    vals = (ws.row_values(mv["src_row"]) + [""] * 5)[:5]
    if str(vals[1]).strip().replace(".0", "") != mv["code"]:
        invalidate_cache()
        raise StaleRow()
    cur = to_int(vals[4])
    if cur < mv["qty"]:
        raise ValueError(f"на локації вже лише {cur} шт.")
    change_qty(user, mv["src_row"], "move_out", cur - mv["qty"])
    df = load_df(True)
    dest = mv["dest"].strip().lower()
    same = df[(df["code"] == mv["code"]) & (df["pallet"].str.strip().str.lower() == dest)]
    try:
        if not same.empty:
            r = same.iloc[0]
            change_qty(user, int(r["row"]), "move_in", int(r["q"]) + mv["qty"])
        else:
            add_tire(user, mv["dest"], mv["code"], mv["size"], mv["name"], mv["qty"], action="move_in")
    except Exception:
        change_qty(user, mv["src_row"], "set", cur, log=False)  # відкат джерела
        raise


def mv_cancel_kb():
    return [InlineKeyboardButton("❌ Скасувати", callback_data="mvno")]


@guarded
async def mv_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    code = q.data.split("_", 1)[1]
    df = await asyncio.to_thread(load_df, True)
    rows = code_rows(df, code)
    rows = rows[rows["q"] > 0]
    if rows.empty:
        await q.message.reply_text("Немає що переміщувати: на всіх локаціях 0 шт.")
        return ConversationHandler.END
    context.user_data["mv"] = {"code": code}
    if len(rows) == 1:
        return await mv_pick_from(update, context, int(rows.iloc[0]["row"]), df)
    kb = [[InlineKeyboardButton(f"{loc_label(r.pallet)}: {int(r.q)} шт.", callback_data=f"mvf_{int(r.row)}")] for r in rows.itertuples()]
    kb.append(mv_cancel_kb())
    await q.message.reply_text(f"🔁 Переміщення {code}. Звідки?", reply_markup=InlineKeyboardMarkup(kb))
    return MV_FROM


async def mv_pick_from(update, context, row, df=None):
    mv = context.user_data.get("mv")
    if not mv:
        return ConversationHandler.END
    df = df if df is not None else await asyncio.to_thread(load_df)
    r = df[df["row"] == row].iloc[0]
    mv.update(src_row=row, src_label=loc_label(r["pallet"]), src_pallet=r["pallet"].strip(), src_q=int(r["q"]), size=r["size"], name=r["name"])
    sug = suggest_pallets(df, mv["code"], 1, exclude={mv["src_pallet"]}, limit=3)
    kb = [[InlineKeyboardButton(f"палета {c['pallet']} · вільно {c['free']}", callback_data=f"mvt_{c['pallet']}")] for c in sug]
    kb.append([InlineKeyboardButton("🪟 Зал", callback_data="mvt_zal"), InlineKeyboardButton("🏬 Вулиця", callback_data="mvt_vul"),
               InlineKeyboardButton("🏗 Фасад", callback_data="mvt_fas")])
    kb.append(mv_cancel_kb())
    await context.bot.send_message(update.effective_chat.id,
        f"🔁 {mv['code']} · {short_name(mv['name'])[:30]}\nЗвідки: {mv['src_label']} ({mv['src_q']} шт.)\n"
        "Куди? Обери кнопку або напиши номер палети / зал / вулиця / фасад.", reply_markup=InlineKeyboardMarkup(kb))
    return MV_TO


@guarded
async def mv_from_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    return await mv_pick_from(update, context, int(q.data.split("_", 1)[1]))


async def mv_ask_qty(update, context, key):
    mv = context.user_data.get("mv")
    if not mv:
        return ConversationHandler.END
    dest = mv_dest_value(key)
    if dest.strip().lower() == mv["src_pallet"].lower():
        await context.bot.send_message(update.effective_chat.id, "⚠️ Це та сама локація. Обери іншу:")
        return MV_TO
    mv["dest"], mv["dest_label"] = dest, loc_label(dest)
    kb = [[InlineKeyboardButton("1 шт.", callback_data="mvq_1"), InlineKeyboardButton(f"Усі {mv['src_q']}", callback_data="mvq_all")], mv_cancel_kb()]
    await context.bot.send_message(update.effective_chat.id, f"Куди: {mv['dest_label']}\nСкільки шт. перемістити? (є {mv['src_q']}) Натисни кнопку або напиши число.",
                                   reply_markup=InlineKeyboardMarkup(kb))
    return MV_QTY


@guarded
async def mv_to_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    return await mv_ask_qty(update, context, q.data.split("_", 1)[1])


@guarded
async def mv_to_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = resolve_loc_key(update.message.text or "")
    if not key or (key.isdigit() and not 1 <= int(key) <= 44):
        await update.message.reply_text("⚠️ Не зрозумів локацію. Напиши номер палети 1–44, «зал», «вулиця» або «фасад».")
        return MV_TO
    return await mv_ask_qty(update, context, key)


async def mv_show_confirm(update, context, qty):
    mv = context.user_data.get("mv")
    if not mv:
        return ConversationHandler.END
    if qty < 1 or qty > mv["src_q"]:
        await context.bot.send_message(update.effective_chat.id, f"⚠️ Введи число від 1 до {mv['src_q']}:")
        return MV_QTY
    mv["qty"] = qty
    df = await asyncio.to_thread(load_df)
    text = (f"🔁 Перемістити {qty} шт.\n🔢 {mv['code']} · {mv['size']} · {short_name(mv['name'])[:32]}\n"
            f"З: {mv['src_label']} → На: {mv['dest_label']}")
    if mv["dest"].isdigit():
        t = pallet_totals(df).get(mv["dest"], 0)
        text += f"\nПалета {mv['dest']}: {t} → {t + qty} / {PALLET_MAX} " + ("⚠️ понад ліміт" if t + qty > PALLET_MAX else "✅")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Перемістити", callback_data="mvok"), InlineKeyboardButton("❌ Скасувати", callback_data="mvno")]])
    await context.bot.send_message(update.effective_chat.id, text, reply_markup=kb)
    return MV_CONFIRM


@guarded
async def mv_qty_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mv = context.user_data.get("mv") or {}
    qty = mv.get("src_q", 1) if q.data.endswith("all") else 1
    return await mv_show_confirm(update, context, qty)


@guarded
async def mv_qty_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.fullmatch(r"\s*(\d+)\s*", update.message.text or "")
    if not m:
        await update.message.reply_text("⚠️ Введи кількість числом:")
        return MV_QTY
    return await mv_show_confirm(update, context, int(m.group(1)))


@guarded
async def mv_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mv = context.user_data.pop("mv", None)
    if q.data == "mvno" or not mv:
        await q.edit_message_text("❌ Скасовано.")
        return ConversationHandler.END
    try:
        await asyncio.to_thread(do_move, q.from_user, mv)
        await q.edit_message_text(f"✅ Переміщено {mv['qty']} шт.: {mv['src_label']} → {mv['dest_label']} (арт. {mv['code']}).")
        await send_card(q.message.reply_text, context, mv["code"], force=True)
        df = await asyncio.to_thread(load_df)
        w = pallet_warning(df, mv["dest"])
        if w:
            await q.message.reply_text(w)
    except Exception as e:
        await q.edit_message_text(friendly_error(e))
    return ConversationHandler.END


@guarded
async def mv_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.pop("mv", None)
    await q.edit_message_text("❌ Скасовано.")
    return ConversationHandler.END


async def mv_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("mv", None)
    await update.message.reply_text("❌ Переміщення скасовано.", reply_markup=MENU)
    return ConversationHandler.END


async def mv_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("mv", None)
    return ConversationHandler.END



# ----------------- ЗАПУСК -----------------
def main():
    if TELEGRAM_BOT_TOKEN == "ВСТАВ_СЮДИ_ТОКЕН":
        print("❌ Не задано токен: додай змінну TELEGRAM_BOT_TOKEN або встав токен у bot.py")
        sys.exit(1)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    plain_text = filters.TEXT & ~filters.COMMAND
    MENU_RE = r"^(🔍 Пошук|➕ Додати|📦 Палети|📊 Статистика|⚖️ Звірка|💰 Звірка продажів|📋 Звіт|📥 Експорт|❓ Допомога)$"
    conv_text = plain_text & ~filters.Regex(MENU_RE)

    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            MessageHandler(filters.Regex(r"^➕ Додати$"), add_start),
            CallbackQueryHandler(add_start, pattern=r"^addnew_\d+$"),
        ],
        states={
            CODE: [MessageHandler(conv_text, add_code)],
            NEWCONFIRM: [CallbackQueryHandler(add_new_decision, pattern=r"^nt_(yes|no)$")],
            SIZE: [MessageHandler(conv_text, add_size)],
            NAME: [MessageHandler(conv_text, add_name)],
            PALLET: [MessageHandler(conv_text, add_pallet)],
            QTY: [MessageHandler(conv_text, add_qty)],
            CONFIRM: [CallbackQueryHandler(add_confirm, pattern=r"^add_(ok|no|merge)$")],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, add_timeout)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
        conversation_timeout=900,
        name="add_conv",
        allow_reentry=True,
    )

    inv_conv = ConversationHandler(
        entry_points=[
            CommandHandler("inventory", inv_entry_cmd),
            CallbackQueryHandler(inv_entry_cb, pattern=r"^invs_(\d+|zal|vul|fas)$"),
        ],
        states={
            INV_ASK: [
                CallbackQueryHandler(inv_button, pattern=r"^iv(ok|skip|end)$"),
                MessageHandler(conv_text, inv_number),
            ],
            INV_CONFIRM: [CallbackQueryHandler(inv_confirm, pattern=r"^iv(apply|cancel)$")],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, inv_timeout)],
        },
        fallbacks=[CommandHandler("cancel", inv_cancel)],
        conversation_timeout=1800,
        name="inv_conv",
    )

    mv_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(mv_start, pattern=r"^mv_\d+$")],
        states={
            MV_FROM: [CallbackQueryHandler(mv_from_cb, pattern=r"^mvf_\d+$"), CallbackQueryHandler(mv_cancel_cb, pattern=r"^mvno$")],
            MV_TO: [CallbackQueryHandler(mv_to_cb, pattern=r"^mvt_(\d+|zal|vul|fas)$"), MessageHandler(conv_text, mv_to_text),
                    CallbackQueryHandler(mv_cancel_cb, pattern=r"^mvno$")],
            MV_QTY: [CallbackQueryHandler(mv_qty_cb, pattern=r"^mvq_(1|all)$"), MessageHandler(conv_text, mv_qty_text),
                     CallbackQueryHandler(mv_cancel_cb, pattern=r"^mvno$")],
            MV_CONFIRM: [CallbackQueryHandler(mv_confirm, pattern=r"^mv(ok|no)$")],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, mv_timeout)],
        },
        fallbacks=[CommandHandler("cancel", mv_cancel_cmd)],
        conversation_timeout=900,
        name="mv_conv",
    )

    app.add_handler(add_conv)   # розмови — першими
    app.add_handler(mv_conv)
    app.add_handler(inv_conv)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("zal", zal_command))
    app.add_handler(CommandHandler("where", where_command))
    app.add_handler(CommandHandler("unload", unload_command))
    app.add_handler(CommandHandler("plan", plan_command))
    app.add_handler(CommandHandler("zakupivlya", order_command))
    app.add_handler(CommandHandler("pallet", pallet_command))
    app.add_handler(CommandHandler("zvirka", zvirka_command))
    app.add_handler(CommandHandler(["zvirka_prodazhiv", "prodazhi"], sales_check_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("cancel", cancel_plain))
    app.add_handler(CallbackQueryHandler(row_action, pattern=r"^(inc|dec|set)_\d+$"))
    app.add_handler(CallbackQueryHandler(undo_action, pattern=r"^undo_\d+$"))
    app.add_handler(CallbackQueryHandler(refresh_card, pattern=r"^rf_\d+$"))
    app.add_handler(CallbackQueryHandler(open_card, pattern=r"^card_\d+$"))
    app.add_handler(CallbackQueryHandler(pallet_pick, pattern=r"^pal_(\d+|zal|vul|fas)$"))
    app.add_handler(CallbackQueryHandler(pallets_back, pattern=r"^pals$"))
    app.add_handler(CallbackQueryHandler(zvirka_excel, pattern=r"^zvx$"))
    app.add_handler(CallbackQueryHandler(sales_excel, pattern=r"^szx$"))
    app.add_handler(CallbackQueryHandler(stats_section, pattern=r"^st_[a-z]+$"))
    app.add_handler(CallbackQueryHandler(zal_excel, pattern=r"^zalx$"))
    app.add_handler(CallbackQueryHandler(pallet_pdf_cb, pattern=r"^pdf_(\d+|zal|vul|fas)$"))
    app.add_handler(CallbackQueryHandler(unload_pick, pattern=r"^unl_\d+$"))
    app.add_handler(MessageHandler(plain_text, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_error_handler(error_handler)

    if REPORT_CHAT_IDS:
        if app.job_queue is None:
            print("⚠️ JobQueue недоступний: додай python-telegram-bot[job-queue] у requirements.txt для ранкового звіту")
        else:
            try:
                hh, mm = [int(x) for x in REPORT_TIME.split(":")]
            except Exception:
                hh, mm = 8, 30
            app.job_queue.run_daily(daily_job, time=dtime(hour=hh, minute=mm, tzinfo=KYIV), name="daily_report")
            print(f"🕗 Ранковий звіт щодня о {hh:02d}:{mm:02d} (Київ) → {REPORT_CHAT_IDS}")

    print("🤖 Бот успішно запущений!")
    app.run_polling()


if __name__ == "__main__":
    main()
