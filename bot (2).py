import os
import re
import sys
import json
import html
import asyncio
import base64
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

import pandas as pd
import gspread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")  # потрібен ТІЛЬКИ для пошуку по фото цінника
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "шини 2026")
FIRST_DATA_ROW = 4      # дані в таблиці починаються з 4-го рядка
MAX_RESULTS = 15        # скільки локацій показувати за один пошук

openai_client = None
if OPENAI_API_KEY:
    try:
        import openai
        openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"⚠️ OpenAI недоступний, фото-пошук вимкнено: {e}")

# ----------------- GOOGLE ТАБЛИЦЯ -----------------
_lock = threading.Lock()
_spreadsheet = None


def get_spreadsheet():
    global _spreadsheet
    with _lock:
        if _spreadsheet is None:
            creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
            if creds_json:
                gc = gspread.service_account_from_dict(json.loads(creds_json))
            else:
                gc = gspread.service_account(filename="credentials.json")
            _spreadsheet = gc.open(SPREADSHEET_NAME)
        return _spreadsheet


def get_worksheet():
    return get_spreadsheet().sheet1  # перший аркуш ("Шини")


def load_df():
    data = get_worksheet().get_all_values()
    rows = []
    for i, r in enumerate(data[FIRST_DATA_ROW - 1:], start=FIRST_DATA_ROW):
        r = (list(r) + [""] * 5)[:5]
        rows.append([i] + [str(x).strip() for x in r])
    df = pd.DataFrame(rows, columns=["row", "pallet", "code", "size", "name", "qty"])
    df["code"] = df["code"].str.replace(r"\.0$", "", regex=True)
    return df[df["code"] != ""]


def log_to_history(user, code, action, old_qty, new_qty):
    try:
        history = get_spreadsheet().worksheet("Історія")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username = f"@{user.username}" if user.username else f"{user.first_name} (ID: {user.id})"
        action_text = {
            "inc": "Додано (+1)",
            "dec": "Зменшено (-1)",
            "add": "Додано нову шину",
        }.get(action, "Встановлено точну кількість")
        history.append_row([now, username, str(code), action_text, old_qty, new_qty])
    except Exception as e:
        print(f"⚠️ Помилка запису в історію: {e}")


# ----------------- ПОШУК -----------------
def norm(s):
    """Лишає тільки літери й цифри: '205/55 R16' -> '20555r16'."""
    return re.sub(r"[^0-9a-zа-яіїєґ]", "", str(s).lower())


def search_tires(query: str):
    df = load_df()
    if df.empty:
        return []
    q = query.strip().lower()
    tokens = q.split()
    if not tokens:
        return []

    hay = (df["code"] + " " + df["size"] + " " + df["name"]).str.lower()
    # усі слова запиту мають бути десь у рядку (код / розмір / назва)
    mask = hay.apply(lambda h: all(t in h for t in tokens))
    # або запит збігається без урахування пробілів і розділювачів
    q_c = norm(q)
    if q_c:
        mask = mask | hay.apply(norm).str.contains(q_c, regex=False)
    found = df[mask]

    if found.empty:
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 4:
            found = df[df["code"].str.contains(digits, regex=False)]

    return [
        {
            "row_index": int(r["row"]),
            "pallet": r["pallet"],
            "code": r["code"],
            "size": r["size"],
            "name": r["name"],
            "qty": r["qty"],
        }
        for _, r in found.iterrows()
    ]


def find_same(code: str, pallet: str):
    """Рядки з таким самим артикулом на тій самій палеті."""
    df = load_df()
    same = df[(df["code"] == code) & (df["pallet"].str.lower() == pallet.strip().lower())]
    return [int(x) for x in same["row"]]


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


# ----------------- ЗМІНА КІЛЬКОСТІ / ДОДАВАННЯ -----------------
def change_qty(user, row, action, value=None):
    ws = get_worksheet()
    vals = (ws.row_values(row) + [""] * 5)[:5]
    code, cur = vals[1], str(vals[4]).strip()
    old = int(cur) if cur.isdigit() else 0
    if action == "inc":
        new = old + 1
    elif action == "dec":
        new = max(0, old - 1)
    else:
        new = int(value)
    ws.update_cell(row, 5, new)
    log_to_history(user, code, action, old, new)
    return code, old, new


def add_tire(user, pallet, code, size, name, qty):
    ws = get_worksheet()
    ws.append_row(
        [pallet, code, size, name, qty],
        value_input_option="USER_ENTERED",
        table_range="A1",
    )
    log_to_history(user, code, "add", 0, qty)


# ----------------- ВІДПРАВКА РЕЗУЛЬТАТІВ -----------------
def qty_keyboard(row_index, qty):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("➖ 1", callback_data=f"dec_{row_index}"),
        InlineKeyboardButton(f"К-сть: {qty}", callback_data=f"set_{row_index}"),
        InlineKeyboardButton("➕ 1", callback_data=f"inc_{row_index}"),
    ]])


async def send_search_results(update: Update, results):
    first = results[0]
    photo_path = get_tire_photo(first["name"])
    if photo_path and os.path.exists(photo_path):
        try:
            with open(photo_path, "rb") as photo_file:
                await update.message.reply_photo(photo=photo_file, caption=f"📷 Фото моделі: {first['name']}")
        except Exception as e:
            print(f"⚠️ Не вдалося надіслати фото: {e}")

    for item in results[:MAX_RESULTS]:
        text = (
            f"📍 Локація / Палета: {item['pallet']}\n"
            f"• Артикул: {item['code']}\n"
            f"• Розмір: {item['size']}\n"
            f"• Назва: {item['name']}\n"
            f"• Кількість: {item['qty']} шт."
        )
        await update.message.reply_text(text, reply_markup=qty_keyboard(item["row_index"], item["qty"]))

    if len(results) > MAX_RESULTS:
        await update.message.reply_text(
            f"Показано {MAX_RESULTS} з {len(results)}. Уточни запит, щоб звузити пошук."
        )


async def run_search(update: Update, query: str):
    try:
        results = await asyncio.to_thread(search_tires, query)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Помилка завантаження даних з Google Таблиці: {e}")
        return
    if not results:
        clean = query.strip()
        code = clean if clean.isdigit() and len(clean) >= 4 else "0"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Додати як нову шину", callback_data=f"addnew_{code}")]])
        await update.message.reply_text(f"❌ За запитом «{query}» нічого не знайдено.", reply_markup=kb)
        return
    await send_search_results(update, results)


# ----------------- КОМАНДИ -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привіт! Я складський бот-помічник для обліку шин 🛞\n\n"
        "🔍 Пошук: надсилай артикул, розмір, назву або фото цінника.\n"
        "➕ /add — додати нову шину в таблицю.\n"
        "📈 /stats — статистика."
    )


def chunk_text(text, limit=4000):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        parts.append(cur)
    return parts


def build_stats():
    df = load_df()
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0)
    total_positions = len(df)
    total_qty = int(df["qty"].sum())
    zero_qty = len(df[df["qty"] == 0])

    def get_season(name):
        n = str(name).lower()
        if any(w in n for w in ["зима", "зимовий", "winter", "шип", "ice", "snow"]):
            return "❄️ Зимові"
        if any(w in n for w in ["літо", "літній", "summer"]):
            return "☀️ Літні"
        if any(w in n for w in ["всесезон", "all weather", "all-season", "4seasons"]):
            return "🌦 Всесезонні"
        return "📦 Інші"

    def get_radius(size_str):
        m = re.search(r"R\d+", str(size_str), re.IGNORECASE)
        return m.group(0).upper() if m else "Інші"

    brands = [
        "Premiori", "Rosava", "Bridgestone", "Michelin", "Nokian", "Hankook",
        "Continental", "Goodyear", "Matador", "Barum", "Cordiant", "Tigar",
        "Kormoran", "Sailun", "Triangle", "Windforce", "Imperial", "Orium",
        "Falken", "Dunlop", "Pirelli", "Yokohama", "Kumho", "Nexen", "BFGoodrich",
        "Firestone", "Toyo", "Lassa", "Mastersteel", "Roadstone", "Aosen",
        "Estrada", "Powertrac", "Petlas", "Horizon", "Hifly",
    ]

    def get_brand(name):
        n = str(name)
        for b in brands:
            if b.lower() in n.lower():
                return b
        for w in n.split():
            if w.lower() not in ["автошина", "шина", "комплект", "шт."] and len(w) > 2:
                return w.capitalize()
        return "Інший"

    df["season"] = df["name"].apply(get_season)
    df["radius"] = df["size"].apply(get_radius)
    df["brand"] = df["name"].apply(get_brand)

    e = html.escape
    text = (
        "📊 <b>Зведена статистика складу шин</b>\n\n"
        f"📦 <b>Загалом:</b> {total_qty} шт. ({total_positions} позицій)\n"
        f"⚠️ <b>Позицій з нульовим залишком:</b> {zero_qty}\n\n"
        "❄️ <b>По сезонності:</b>\n"
    )
    for season, q in df.groupby("season")["qty"].sum().items():
        if q > 0:
            text += f"• {season}: {int(q)} шт.\n"
    text += "\n⬡ <b>По радіусу:</b>\n"
    for rad, q in sorted(df.groupby("radius")["qty"].sum().items()):
        if q > 0:
            text += f"• {e(rad)}: {int(q)} шт.\n"
    text += "\n🏭 <b>По виробниках:</b>\n"
    for brand, q in sorted(df.groupby("brand")["qty"].sum().items(), key=lambda x: x[1], reverse=True):
        if q > 0:
            text += f"• {e(brand)}: {int(q)} шт.\n"
    combo = df.groupby(["brand", "radius"])["qty"].sum().reset_index()
    combo = combo[combo["qty"] > 0].sort_values(by="qty", ascending=False)
    text += "\n📌 <b>Топ комбінацій (виробник + радіус):</b>\n"
    for _, r in combo.head(10).iterrows():
        text += f"• {e(r['brand'])} ({e(r['radius'])}): {int(r['qty'])} шт.\n"
    return text


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = await asyncio.to_thread(build_stats)
        for part in chunk_text(text):
            await update.message.reply_text(part, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Помилка формування статистики: {e}")


async def cancel_plain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("waiting_for_row", None)
    await update.message.reply_text("Скасовано.")


# ----------------- ДОДАВАННЯ НОВОЇ ШИНИ (/add) -----------------
PALLET, CODE, SIZE, NAME, QTY, CONFIRM = range(6)


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("waiting_for_row", None)
    new = {}
    context.user_data["new"] = new
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        code = q.data.split("_", 1)[1]
        if code.isdigit() and code != "0":
            new["code"] = code
        send = q.message.reply_text
    else:
        send = update.message.reply_text
    prefix = f"Артикул: {new['code']}\n\n" if "code" in new else ""
    await send(f"➕ Додаємо нову шину (/cancel — скасувати).\n{prefix}📍 Палета / локація?")
    return PALLET


async def add_pallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    new = context.user_data.setdefault("new", {})
    new["pallet"] = text
    if "code" in new:
        await update.message.reply_text("📏 Розмір? (наприклад 205/55 R16)")
        return SIZE
    await update.message.reply_text("🔢 Артикул?")
    return CODE


async def add_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = re.sub(r"\D", "", update.message.text or "")
    if len(code) < 4:
        await update.message.reply_text("⚠️ Артикул має містити цифри (мінімум 4). Спробуй ще раз:")
        return CODE
    context.user_data.setdefault("new", {})["code"] = code
    await update.message.reply_text(f"Артикул: {code}\n📏 Розмір? (наприклад 205/55 R16)")
    return SIZE


async def add_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("new", {})["size"] = (update.message.text or "").strip()
    await update.message.reply_text("🏷 Назва (бренд, модель, сезон)?")
    return NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("new", {})["name"] = (update.message.text or "").strip()
    await update.message.reply_text("🔢 Кількість (шт.)?")
    return QTY


async def add_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.fullmatch(r"\s*(\d+)\s*", update.message.text or "")
    if not m:
        await update.message.reply_text("⚠️ Введи кількість числом:")
        return QTY
    new = context.user_data.setdefault("new", {})
    new["qty"] = int(m.group(1))

    if not all(k in new for k in ("pallet", "code", "size", "name")):
        await update.message.reply_text("⚠️ Щось пішло не так. Почни спочатку: /add")
        context.user_data.pop("new", None)
        return ConversationHandler.END

    warn = ""
    try:
        dup = await asyncio.to_thread(find_same, new["code"], new["pallet"])
        if dup:
            warn = (
                f"\n\n⚠️ Такий артикул на цій палеті вже є (рядок {dup[0]}). "
                "Можливо, краще змінити кількість через пошук."
            )
    except Exception:
        pass

    summary = (
        "Перевір і підтверди:\n\n"
        f"📍 Палета: {new['pallet']}\n"
        f"• Артикул: {new['code']}\n"
        f"• Розмір: {new['size']}\n"
        f"• Назва: {new['name']}\n"
        f"• Кількість: {new['qty']} шт."
        f"{warn}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Додати", callback_data="add_ok"),
        InlineKeyboardButton("❌ Скасувати", callback_data="add_no"),
    ]])
    await update.message.reply_text(summary, reply_markup=kb)
    return CONFIRM


async def add_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    new = context.user_data.pop("new", None)
    if q.data == "add_no" or not new:
        await q.edit_message_text("❌ Скасовано.")
        return ConversationHandler.END
    try:
        await asyncio.to_thread(
            add_tire, q.from_user, new["pallet"], new["code"], new["size"], new["name"], new["qty"]
        )
        await q.edit_message_text(
            f"✅ Додано: {new['name']} ({new['size']}), арт. {new['code']}, "
            f"палета {new['pallet']}, {new['qty']} шт."
        )
    except Exception as e:
        await q.edit_message_text(f"⚠️ Не вдалося додати в таблицю: {e}")
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new", None)
    await update.message.reply_text("❌ Додавання скасовано.")
    return ConversationHandler.END


async def add_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new", None)
    return ConversationHandler.END


# ----------------- ЗВИЧАЙНІ ПОВІДОМЛЕННЯ -----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    if "waiting_for_row" in context.user_data:
        if re.fullmatch(r"\d+", text):
            row_index = context.user_data.pop("waiting_for_row")
            try:
                code, old, new = await asyncio.to_thread(
                    change_qty, update.effective_user, row_index, "set", int(text)
                )
                await update.message.reply_text(f"✅ Встановлено нову кількість: {new} шт. (Артикул: {code})")
            except Exception as e:
                await update.message.reply_text(f"⚠️ Помилка оновлення: {e}")
            return
        # це не число — вважаємо, що це новий пошук
        context.user_data.pop("waiting_for_row", None)

    await run_search(update, text)


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
        await run_search(update, code)
    except Exception as e:
        await msg.edit_text(f"⚠️ Помилка обробки фото: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙 Голосові повідомлення не підтримуються. Напиши текстом.")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, row_str = query.data.split("_", 1)
    row_index = int(row_str)

    if action == "set":
        await query.answer()
        context.user_data["waiting_for_row"] = row_index
        await query.message.reply_text("⌨️ Надішли нову точну кількість (тільки число):")
        return

    try:
        code, old, new_qty = await asyncio.to_thread(change_qty, query.from_user, row_index, action)
    except Exception as e:
        await query.answer(f"⚠️ Помилка оновлення: {e}", show_alert=True)
        return
    await query.answer()

    lines = (query.message.text or "").split("\n")
    for i, line in enumerate(lines):
        if line.startswith("• Кількість:"):
            lines[i] = f"• Кількість: {new_qty} шт."
    try:
        await query.edit_message_text("\n".join(lines), reply_markup=qty_keyboard(row_index, new_qty))
    except Exception:
        pass  # наприклад, "message is not modified"


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"⚠️ Сталася помилка при обробці оновлення: {context.error}")


# ----------------- ЗАПУСК -----------------
if __name__ == "__main__":
    if TELEGRAM_BOT_TOKEN == "ВСТАВ_СЮДИ_ТОКЕН":
        print("❌ Не задано токен: додай змінну TELEGRAM_BOT_TOKEN або встав токен у bot.py")
        sys.exit(1)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    plain_text = filters.TEXT & ~filters.COMMAND

    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            CallbackQueryHandler(add_start, pattern=r"^addnew_\d+$"),
        ],
        states={
            PALLET: [MessageHandler(plain_text, add_pallet)],
            CODE: [MessageHandler(plain_text, add_code)],
            SIZE: [MessageHandler(plain_text, add_size)],
            NAME: [MessageHandler(plain_text, add_name)],
            QTY: [MessageHandler(plain_text, add_qty)],
            CONFIRM: [CallbackQueryHandler(add_confirm, pattern=r"^add_(ok|no)$")],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, add_timeout)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
        conversation_timeout=900,
    )

    app.add_handler(add_conv)  # має бути першим
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("cancel", cancel_plain))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^(inc|dec|set)_\d+$"))
    app.add_handler(MessageHandler(plain_text, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_error_handler(error_handler)

    print("🤖 Бот успішно запущений!")
    app.run_polling()
