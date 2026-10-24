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
        
        # Відкриваємо аркуш 'Історія'
        history_sheet = spreadsheet.worksheet("Історія")
        
        # Формуємо дані про час та користувача
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username = f"@{user.username}" if user.username else f"{user.first_name} (ID: {user.id})"
        
        if action == "inc":
            action_text = "Додано (+1)"
        elif action == "dec":
            action_text = "Зменшено (-1)"
        else:
            action_text = "Встановлено точну кількість"
        
        # Додаємо новий рядок із даними в журнал
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

# ----------------- ОБРОБНИКИ ТЕЛЕГРАМ -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привіт! Я твій розумний складський бот-помічник для обліку шин 🛞\n\n"
        "Ось що я вмію робити:\n\n"
        "🔍 **Пошук товарів:**\n"
        "• Надсилай текстовий запит (артикул, розмір або назву).\n"
        "• Або використовуй **голосові повідомлення** (Whisper розпізнає їх автоматично).\n\n"
        "📊 **Керування залишками:**\n"
        "• **`➖ 1` / `➕ 1`** — швидка зміна кількості прямо кнопками.\n"
        "• ** `К-сть: X` ** — натисни на центральну кнопку, щоб ввести точне нове число в чаті.\n\n"
        "📋 **Аудит та історія:**\n"
        "• Усі зміни залишків автоматично та миттєво фіксуються в журналі (`Історія`) у Google Таблиці з урахуванням часу та користувача.\n\n"
        "👇 **Просто надішли мені артикул або розмір шини, щоб почати роботу!**",
        parse_mode="Markdown"
    )
    

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data
    
    # Якщо користувач натиснув кнопку введення точної кількості і тепер пише число:
    if "waiting_for_row" in user_data:
        row_index = user_data.pop("waiting_for_row")
        text_input = update.message.text.strip()
        
        if not text_input.isdigit():
            await update.message.reply_text("⚠️ Будь ласка, введіть числове значення для кількості:")
            user_data["waiting_for_row"] = row_index  # Повертаємо стан очікування
            return
            
        new_qty = int(text_input)
        try:
            sheet = get_worksheet()
            current_qty_str = sheet.cell(row_index, 5).value
            current_qty = int(current_qty_str) if current_qty_str and str(current_qty_str).isdigit() else 0
            
            # Оновлюємо значення в таблиці
            sheet.update_cell(row_index, 5, new_qty)
            item_code = sheet.cell(row_index, 2).value
            
            # Записуємо в історію
            log_to_history(update.effective_user, item_code, "set", current_qty, new_qty)
            
            await update.message.reply_text(f"✅ Успішно встановлено нову кількість: *{new_qty} шт.* (Артикул: {item_code})", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Помилка оновлення: {e}")
        return

    # Звичайний текстовий пошук шин
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
        
        # Кнопки керування: -1, Точне введення (по центру), +1
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
    
    # Якщо клікнули на центральну кнопку з кількістю — активуємо режим введення точної кількості
    if action == "set":
        context.user_data["waiting_for_row"] = row_index
        await query.message.reply_text("⌨️ Будь ласка, надішліть у чат **нову точну кількість** (тільки число) для цього товару:")
        return

    try:
        sheet = get_worksheet()
        current_qty_str = sheet.cell(row_index, 5).value  # Стовпець E — кількість
        current_qty = int(current_qty_str) if current_qty_str and str(current_qty_str).isdigit() else 0
        
        if action == "inc":
            new_qty = current_qty + 1
        elif action == "dec":
            new_qty = max(0, current_qty - 1)
        else:
            return
            
        # 1. Записуємо нове значення у головний аркуш Google Таблиці
        sheet.update_cell(row_index, 5, new_qty)
        
        # 2. Отримуємо артикул товару для журналу дій (стовпець B)
        item_code = sheet.cell(row_index, 2).value
        
        # 3. Додаємо факт зміни у файл журналювання ("Історія")
        log_to_history(query.from_user, item_code, action, current_qty, new_qty)
        
        # 4. Оновлюємо текст повідомлення в Telegram
        original_text = query.message.text
        lines = original_text.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("• Кількість:"):
                lines[i] = f"• Кількість: {new_qty} шт."
        updated_text = "\n".join(lines)
        
        # 5. Оновлюємо кнопки з новою кількістю
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    print("🤖 Бот запущений і готовий до роботи!") 
    app.run_polling()
