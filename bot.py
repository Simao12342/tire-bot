import os
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import pandas as pd
import gspread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import openai

# ----------------- ФОНОВИЙ ВЕБ-СЕРВЕР ДЛЯ RENDER -----------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_check_server, daemon=True).start()

# ----------------- НАЛАШТУВАННЯ -----------------
TELEGRAM_BOT_TOKEN = "8767386740:AAG_vZSBP-duKwFR_YYgSN-Yr7ia39rSw6c"
OPENAI_API_KEY = "ТВІЙ_OPENAI_API_KEY"  

openai.api_key = OPENAI_API_KEY

def get_worksheet():
    """Підключення до Google Таблиці через сервісний ключ credentials.json"""
    gc = gspread.service_account(filename="credentials.json")
    spreadsheet = gc.open("шини 2026")
    return spreadsheet.sheet1  # Перший аркуш таблиці ("Шини")

# ----------------- ФУНКЦІЯ ЛОГУВАННЯ У ЖУРНАЛ -----------------
def log_to_history(user, code, action, old_qty, new_qty):
    """Записує подію зміни залишку на аркуш 'Історія'"""
    try:
        gc = gspread.service_account(filename="credentials.json")
        spreadsheet = gc.open("шини 2026")
        
        history_sheet = spreadsheet.worksheet("Історія")
        
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username = f"@{user.username}" if user.username else f"{user.first_name} (ID: {user.id})"
        
        if action == "inc":
            action_text = "Додано (+1)"
        elif action == "dec":
            action_text = "Зменшено (-1)"
        else:
            action_text = "Встановлено точну кількість"
        
        history_sheet.append_row([now, username, str(code), action_text, old_qty, new_qty])
    except Exception as e:
        print(f"⚠️ Помилка запису в історію: {e}")

# ----------------- ФУНКЦІЯ ПОШУКУ -----------------
def search_tires(query: str):
    try:
        sheet = get_worksheet()
        data = sheet.get_all_values()
        rows = data[3:]  # Дані починаються з 4-го рядка
        
        df = pd.DataFrame(rows, columns=["pallet", "code", "size", "name", "qty"])
        df = df.dropna(subset=["code"])
        df["code"] = df["code"].astype(str).str.replace(".0", "", regex=False).str.strip()
    except Exception as e:
        return f"⚠️ Помилка завантаження даних з Google Таблиці: {e}"

    query_clean = query.strip().lower()
    results = []
    
    for idx, row in df.iterrows():
        code_str = str(row["code"]).lower()
        size_str = str(row["size"]).lower()
        name_str = str(row["name"]).lower()
        
        if query_clean in code_str or query_clean in size_str or query_clean in name_str:
            sheet_row_index = idx + 4  # Реальний номер рядка в Google Таблиці
            results.append({
                "row_index": sheet_row_index,
                "pallet": row["pallet"],
                "code": row["code"],
                "size": row["size"],
                "name": row["name"],
                "qty": row["qty"]
            })
            
    if not results:
        digits_only = re.sub(r"\D", "", query_clean)
        if len(digits_only) >= 4:
            for idx, row in df.iterrows():
                if digits_only in str(row["code"]):
                    sheet_row_index = idx + 4
                    results.append({
                        "row_index": sheet_row_index,
                        "pallet": row["pallet"],
                        "code": row["code"],
                        "size": row["size"],
                        "name": row["name"],
                        "qty": row["qty"]
                    })
                    
    return results

# ----------------- КОМАНДА СТАТИСТИКИ -----------------
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        sheet = get_worksheet()
        data = sheet.get_all_values()
        rows = data[3:]  # Дані з 4-го рядка
        
        df = pd.DataFrame(rows, columns=["pallet", "code", "size", "name", "qty"])
        df = df.dropna(subset=["code"])
        df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0)
        
        # 1. Загальні показники
        total_positions = len(df)
        total_qty = int(df["qty"].sum())
        zero_qty = len(df[df["qty"] == 0])
        
        # 2. Сезонність
        def get_season(name):
            n = str(name).lower()
            if any(w in n for w in ["зим", "зима", "winter", "шип"]):
                return "❄️ Зимові"
            elif any(w in n for w in ["літ", "літо", "summer"]):
                return "☀️ Літні"
            elif any(w in n for w in ["всесез", "all"]):
                return "🌦 Всесезонні"
            return "📦 Інші"
        
        df["season"] = df["name"].apply(get_season)
        season_stats = df.groupby("season")["qty"].sum().to_dict()
        
        # 3. Радіус
        def get_radius(size_str):
            match = re.search(r'R\d+', str(size_str), re.IGNORECASE)
            return match.group(0).upper() if match else "Інші"
        
        df["radius"] = df["size"].apply(get_radius)
        radius_stats = df.groupby("radius")["qty"].sum().to_dict()
        
        # 4. Виробник
        brands = [
            "Premiori", "Rosava", "Bridgestone", "Michelin", "Nokian", "Hankook", 
            "Continental", "Goodyear", "Matador", "Barum", "Cordiant", "Tigar", 
            "Kormoran", "Sailun", "Triangle", "Windforce", "Imperial", "Orium", 
            "Falken", "Dunlop", "Pirelli", "Yokohama", "Kumho", "Nexen", "BFGoodrich",
            "Firestone", "Toyo", "Lassa", "Mastersteel", "Roadstone"
        ]
        
        def get_brand(name):
            n = str(name)
            n_lower = n.lower()
            for b in brands:
                if b.lower() in n_lower:
                    return b
            words = n.split()
            for w in words:
                if w.lower() not in ["автошина", "шина", "комплект", "шт."] and len(w) > 2:
                    return w.capitalize()
            return "Інший"
        
        df["brand"] = df["name"].apply(get_brand)
        brand_stats = df.groupby("brand")["qty"].sum().to_dict()
        
        # 5. Виробник і розмір
        brand_size_df = df.groupby(["brand", "radius"])["qty"].sum().reset_index()
        brand_size_df = brand_size_df[brand_size_df["qty"] > 0].sort_values(by="qty", ascending=False)
        
        # Формуємо текст звіту
        text = (
            "📊 **Зведена статистика складу шин**\n\n"
            f"📦 **Загалом на складі:** {total_qty} шт. ({total_positions} позицій)\n"
            f"⚠️ **Позицій з нульовим залишком:** {zero_qty}\n\n"
            "❄️ **По сезонності:**\n"
        )
        for season, q in season_stats.items():
            if q > 0:
                text += f"• {season}: {q} шт.\n"
        
        text += "\n⬡ **По радіусу:**\n"
        for rad, q in sorted(radius_stats.items()):
            if q > 0:
                text += f"• {rad}: {q} шт.\n"
        
        text += "\n🏭 **По виробниках:**\n"
        sorted_brands = sorted(brand_stats.items(), key=lambda x: x[1], reverse=True)
        for brand, q in sorted_brands:
            if q > 0:
                text += f"• {brand}: {q} шт.\n"
        
        text += "\n📌 **Топ комбінацій (Виробник + Радіус):**\n"
        for _, row in brand_size_df.head(10).iterrows():
            text += f"• {row['brand']} ({row['radius']}): {row['qty']} шт.\n"
            
        await update.message.reply_text(text, parse_mode="Markdown")
        
    except Exception as e:
        await update.message.reply_text(f"⚠️ Помилка формування статистики: {e}")

# ----------------- ОБРОБНИКИ ТЕЛЕГРАМ -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привіт! Я твій розумний складський бот-помічник для обліку шин 🛞\n\n"
        "Ось що я вмію робити:\n\n"
        "🔍 **Пошук товарів:**\n"
        "• Надсилай текстовий запит (артикул, розмір або назву).\n"
        "• Або використовуй **голосові повідомлення**.\n\n"
        "📊 **Керування залишками:**\n"
        "• **`➖ 1` / `➕ 1`** — швидка зміна кількості кнопками.\n"
        "• **`К-сть: X`** — натисни на центральну кнопку, щоб ввести точне число в чаті.\n\n"
        "📈 **Аналітика та звітність:**\n"
        "• Використовуй команду **`/stats`**, щоб отримати повну статистику складу по кількості, сезонності, радіусах та виробниках!\n\n"
        "📋 **Аудит та історія:**\n"
        "• Усі зміни автоматично фіксуються в журналі (`Історія`) у Google Таблиці.\n\n"
        "👇 **Надішли мені артикул або натисни `/stats` для перегляду звіту!**",
        parse_mode="Markdown"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data
    
    if "waiting_for_row" in user_data:
        row_index = user_data.pop("waiting_for_row")
        text_input = update.message.text.strip()
        
        if not text_input.isdigit():
            await update.message.reply_text("⚠️ Будь ласка, введіть числове значення для кількості:")
            user_data["waiting_for_row"] = row_index
            return
            
        new_qty = int(text_input)
        try:
            sheet = get_worksheet()
            current_qty_str = sheet.cell(row_index, 5).value
            current_qty = int(current_qty_str) if current_qty_str and str(current_qty_str).isdigit() else 0
            
            sheet.update_cell(row_index, 5, new_qty)
            item_code = sheet.cell(row_index, 2).value
            
            log_to_history(update.effective_user, item_code, "set", current_qty, new_qty)
            
            await update.message.reply_text(f"✅ Успішно встановлено нову кількість: *{new_qty} шт.* (Артикул: {item_code})", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Помилка оновлення: {e}")
        return

    query = update.message.text
    results = search_tires(query)
    
    if isinstance(results, str):
        await update.message.reply_text(results)
        return
        
    if not results:
        await update.message.reply_text(f"❌ За запитом «{query}» нічого не знайдено.")
        return

    for item in results:
        text = (
            f"📍 Локація / Палета: {item['pallet']}\n"
            f"• Артикул: {item['code']}\n"
            f"• Розмір: {item['size']}\n"
            f"• Назва: {item['name']}\n"
            f"• Кількість: {item['qty']} шт."
        )
        
        keyboard = [
            [
                InlineKeyboardButton("➖ 1", callback_data=f"dec_{item['row_index']}"),
                InlineKeyboardButton(f"К-сть: {item['qty']}", callback_data=f"set_{item['row_index']}"),
                InlineKeyboardButton("➕ 1", callback_data=f"inc_{item['row_index']}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(text, reply_markup=reply_markup)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    action, row_index_str = data.split("_")
    row_index = int(row_index_str)
    
    if action == "set":
        context.user_data["waiting_for_row"] = row_index
        await query.message.reply_text("⌨️ Будь ласка, надішліть у чат **нову точну кількість** (тільки число) для цього товару:")
        return

    try:
        sheet = get_worksheet()
        current_qty_str = sheet.cell(row_index, 5).value
        current_qty = int(current_qty_str) if current_qty_str and str(current_qty_str).isdigit() else 0
        
        if action == "inc":
            new_qty = current_qty + 1
        elif action == "dec":
            new_qty = max(0, current_qty - 1)
        else:
            return
            
        sheet.update_cell(row_index, 5, new_qty)
        item_code = sheet.cell(row_index, 2).value
        log_to_history(query.from_user, item_code, action, current_qty, new_qty)
        
        original_text = query.message.text
        lines = original_text.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("• Кількість:"):
                lines[i] = f"• Кількість: {new_qty} шт."
        updated_text = "\n".join(lines)
        
        keyboard = [
            [
                InlineKeyboardButton("➖ 1", callback_data=f"dec_{row_index}"),
                InlineKeyboardButton(f"К-сть: {new_qty}", callback_data=f"set_{row_index}"),
                InlineKeyboardButton("➕ 1", callback_data=f"inc_{row_index}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text=updated_text, reply_markup=reply_markup)
        
    except Exception as e:
        await query.answer(f"⚠️ Помилка оновлення: {e}", show_alert=True)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OPENAI_API_KEY or OPENAI_API_KEY == "ТВІЙ_OPENAI_API_KEY":
        await update.message.reply_text("⚠️ Голосовий пошук наразі не налаштовано.")
        return

    msg = await update.message.reply_text("🎙 Розпізнаю голосове повідомлення...")
    voice_file = await context.bot.get_file(update.message.voice.file_id)
    file_path = "voice.ogg"
    await voice_file.download_to_drive(file_path)
    
    try:
        with open(file_path, "rb") as audio:
            transcript = openai.Audio.transcribe("whisper-1", audio, language="uk")
        
        recognized_text = transcript["text"]
        await msg.edit_text(f"🗣 Розпізнано: *«{recognized_text}»*", parse_mode="Markdown")
        
        results = search_tires(recognized_text)
        if isinstance(results, str):
            await update.message.reply_text(results)
            return
        if not results:
            await update.message.reply_text(f"❌ За запитом «{recognized_text}» нічого не знайдено.")
            return

        for item in results:
            text = (
                f"📍 Локація / Палета: {item['pallet']}\n"
                f"• Артикул: {item['code']}\n"
                f"• Розмір: {item['size']}\n"
                f"• Назва: {item['name']}\n"
                f"• Кількість: {item['qty']} шт."
            )
            keyboard = [
                [
                    InlineKeyboardButton("➖ 1", callback_data=f"dec_{item['row_index']}"),
                    InlineKeyboardButton(f"К-сть: {item['qty']}", callback_data=f"set_{item['row_index']}"),
                    InlineKeyboardButton("➕ 1", callback_data=f"inc_{item['row_index']}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(text, reply_markup=reply_markup)
            
    except Exception as e:
        await msg.edit_text(f"⚠️ Помилка розпізнавання голосу: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# ----------------- ЗАПУСК -----------------
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    print("🤖 Бот запущений і готовий до роботи зі статистикою!") 
    app.run_polling()
        
