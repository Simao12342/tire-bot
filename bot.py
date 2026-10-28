import os
import re
import threading
import base64
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
OPENAI_API_KEY = "ТВІЙ_OPENAI_API_KEY"  # Замініть або налаштуйте в Render

openai.api_key = OPENAI_API_KEY

def get_worksheet():
    """Підключення до Google Таблиці через сервісний ключ credentials.json"""
    gc = gspread.service_account(filename="credentials.json")
    spreadsheet = gc.open("шини 2026")
    return spreadsheet.sheet1  # Перший аркуш таблиці ("Шини")

# ----------------- ГЛОБАЛЬНИЙ ОБРОБНИК ПОМИЛОК -----------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"⚠️ Сталася помилка при обробці оновлення: {context.error}")

# ----------------- ФУНКЦІЯ ЛОГУВАННЯ У ЖУРНАЛ -----------------
def log_to_history(user, code, action, old_qty, new_qty):
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
        elif action == "add_new":
            action_text = "Створено новий рядок"
        else:
            action_text = "Встановлено точну кількість"
        
        history_sheet.append_row([now, username, str(code), action_text, old_qty, new_qty])
    except Exception as e:
        print(f"⚠️ Помилка запису в історію: {e}")

# ----------------- ВИПРАВЛЕНА ФУНКЦІЯ ПОШУКУ -----------------
def search_tires(query: str):
    try:
        sheet = get_worksheet()
        data = sheet.get_all_values()
        rows = data[3:]  # Дані починаються з 4-го рядка
        
        df = pd.DataFrame(rows, columns=["pallet", "code", "size", "name", "qty"])
        df = df.dropna(subset=["code"])
        # Очищуємо коди від крапок та пробілів для ідеального збігу
        df["code"] = df["code"].astype(str).str.split('.').str[0].str.strip()
    except Exception as e:
        return f"⚠️ Помилка завантаження даних з Google Таблиці: {e}"

    query_clean = str(query).strip().lower()
    results = []
    
    for idx, row in df.iterrows():
        code_str = str(row["code"]).lower()
        size_str = str(row["size"]).lower()
        name_str = str(row["name"]).lower()
        
        if query_clean in code_str or query_clean in size_str or query_clean in name_str:
            sheet_row_index = idx + 4
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

# ----------------- ВИПРАВЛЕНА ФУНКЦІЯ ДОДАВАННЯ НОВОГО ТОВАРУ -----------------
def add_tire_to_sheet(pallet: str, code: str, qty: int):
    try:
        sheet = get_worksheet()
        data = sheet.get_all_values()
        rows = data[3:]
        
        df = pd.DataFrame(rows, columns=["pallet", "code", "size", "name", "qty"])
        # Очищуємо код від .0
        df["code_clean"] = df["code"].astype(str).str.split('.').str[0].str.strip()
        
        target_code = str(code).strip()
        matched_rows = df[df["code_clean"] == target_code]
        
        if matched_rows.empty:
            # Шукаємо за частковим збігом (останні цифри)
            matched_rows = df[df["code_clean"].str.contains(target_code)]
            
        if matched_rows.empty:
            return None, f"❌ Артикул `{code}` не знайдено у записаних позиціях таблиці."
            
        found_item = matched_rows.iloc[0]
        size = found_item["size"]
        name = found_item["name"]
        
        # Додаємо новий рядок у таблицю
        new_row = [str(pallet), str(target_code), str(size), str(name), int(qty)]
        sheet.append_row(new_row)
        
        return {
            "pallet": pallet,
            "code": target_code,
            "size": size,
            "name": name,
            "qty": qty
        }, None
    except Exception as e:
        return None, f"⚠️ Помилка виконання запису у Google Таблицю: {e}"

# ----------------- ПОШУК ФОТО В ПАПЦІ PHOTO -----------------
def get_tire_photo(name):
    photos_dir = "photo"
    if not os.path.exists(photos_dir):
        return None
    
    name_lower = str(name).lower()
    for filename in os.listdir(photos_dir):
        if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.avif')):
            keyword = os.path.splitext(filename)[0].lower()
            if len(keyword) >= 3 and keyword in name_lower:
                return os.path.join(photos_dir, filename)
    return None

# ----------------- КОМАНДА СТАТИСТИКИ -----------------
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        sheet = get_worksheet()
        data = sheet.get_all_values()
        rows = data[3:]
        
        df = pd.DataFrame(rows, columns=["pallet", "code", "size", "name", "qty"])
        df = df.dropna(subset=["code"])
        df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0)
        
        total_positions = len(df)
        total_qty = int(df["qty"].sum())
        zero_qty = len(df[df["qty"] == 0])
        
        def get_season(name):
            n = str(name).lower()
            if any(w in n for w in ["зима", "зимовий", "winter", "шип", "ice", "snow"]):
                return "❄️ Зимові"
            elif any(w in n for w in ["літо", "літній", "summer"]):
                return "☀️ Літні"
            elif any(w in n for w in ["всесезон", "all weather", "all-season", "4seasons"]):
                return "🌦 Всесезонні"
            return "📦 Інші"
        
        df["season"] = df["name"].apply(get_season)
        season_stats = df.groupby
