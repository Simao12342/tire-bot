[23.10.2026 1:36] Сергій Олійник: import os
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import pandas as pd
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
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

# Запускаємо сервер у фоновому потоці для Render
threading.Thread(target=run_health_check_server, daemon=True).start()

# ----------------- НАЛАШТУВАННЯ -----------------
TELEGRAM_BOT_TOKEN = "8767386740:AAHkzTYMlhlZqRMX_O0_MaD5zWI8oGGukiA"
OPENAI_API_KEY = "ТВІЙ_OPENAI_API_KEY"  

# ВСТАВТЕ СЮДИ ПОСИЛАННЯ НА ОПУБЛІКОВАНИЙ CSV З GOOGLE ТАБЛИЦІ:
GOOGLE_SHEET_CSV_URL = "ВСТАВТЕ_СЮДИ_ПОСИЛАННЯ_НА_CSV"

openai.api_key = OPENAI_API_KEY

def load_data():
    """Завантаження та очищення даних безпосередньо з Google Таблиці"""
    # Читаємо актуальну CSV-версію прямо з інтернету
    df = pd.read_csv(GOOGLE_SHEET_CSV_URL, skiprows=2)
    df.columns = ["pallet", "code", "size", "name", "qty"]
    df = df.dropna(subset=["code"])
    df["code"] = df["code"].astype(str).str.replace(".0", "", regex=False).str.strip()
    return df

# ----------------- ФУНКЦІЯ ПОШУКУ -----------------
def search_tires(query: str) -> str:
    try:
        df = load_data()
    except Exception as e:
        return f"⚠️ Помилка завантаження даних з Google Таблиці: {e}"

    query_clean = query.strip().lower()
    
    # Пошук за кодом (артикулом), розміром або назвою
    results = df[
        df["code"].str.lower().str.contains(query_clean) |
        df["size"].astype(str).str.lower().str.contains(query_clean) |
        df["name"].astype(str).str.lower().str.contains(query_clean)
    ]
    
    if results.empty:
        digits_only = re.sub(r"\D", "", query_clean)
        if len(digits_only) >= 4:
            results = df[df["code"].str.contains(digits_only)]
            
    if results.empty:
        return f"❌ За запитом «{query}» нічого не знайдено."
    
    text = f"🔍 Результати пошуку за запитом: *{query}*\n\n"
    for _, row in results.iterrows():
        pallet_info = row['pallet']
        text += (
            f"📍 Локація / Палета: {pallet_info}\n"
            f"• Артикул: {row['code']}\n"
            f"• Розмір: {row['size']}\n"
            f"• Назва: {row['name']}\n"
            f"• Кількість: {row['qty']} шт.\n"
            f"-----------------------------------\n"
        )
    return text

# ----------------- ОБРОБНИКИ ТЕЛЕГРАМ -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! Я твій складський бот-помічник 🛞\n\n"
        "Надішли мені артикул, розмір або назву шини, "
        "і я підкажу, де вона лежить!"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text
    response = search_tires(query)
    await update.message.reply_markdown(response)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OPENAI_API_KEY or OPENAI_API_KEY == "ТВІЙ_OPENAI_API_KEY":
        await update.message.reply_text("⚠️ Голосовий пошук наразі не налаштовано. Будь ласка, пишіть текстові запити.")
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
        
        response = search_tires(recognized_text)
        await update.message.reply_markdown(response)
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
    
    print("🤖 Бот запущений і готовий до роботи!")
    app.run_polling()