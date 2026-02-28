import asyncio
import logging
import time
import os
import json
from datetime import datetime, timedelta, timezone
from aiohttp import web

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager

import gspread
from google.oauth2.service_account import Credentials

# --- ⚙️ НАСТРОЙКИ ---
TOKEN = "8177741538:AAEqlEsJomzv8Sx7e-5jcM11gp05F5bHvtQ"
DTEK_URL = "https://www.dtek-dnem.com.ua/ua/shutdowns"
CHECK_INTERVAL = 300  # 300 секунд = 5 хвилин

# 🛠 РЕЖИМ РАБОТЫ
IS_LOCAL_TESTING = False  # ОБЯЗАТЕЛЬНО FALSE ДЛЯ СЕРВЕРА
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# База адресов
ADDRS = {
    "addr1": {
        "header": "с-ще Новомиколаївка, вул. Степова, 77",
        "city": "с-ще Новомиколаївка", 
        "street": "вул. Степова", 
        "house": "77"
    },
    "dnipro_1": {
        "header": "м. Дніпро, вул. Севастопольська, 16",
        "city": "м. Дніпро", 
        "street": "вул. Севастопольська", 
        "house": "16"
    },
    "dnipro_2": {
        "header": "м. Дніпро, просп. Мануйлівський, 78",
        "city": "м. Дніпро", 
        "street": "просп. Мануйлівський", 
        "house": "78"
    },
    "dnipro_3": {
        "header": "м. Дніпро, вул. Мазепи Галини, 76",
        "city": "м. Дніпро", 
        "street": "вул. Мазепи Галини", 
        "house": "76"
    },
    "dnipro_4": {
        "header": "м. Дніпро, вул. Володимира Вернадського, 19/21",
        "city": "м. Дніпро", 
        "street": "вул. Володимира Вернадського", 
        "house": "19/21"
    }
}

# --- 💾 ПАМЯТЬ БОТА ---
STORAGE = {
    key: {"last_check": 0, "fingerprint": "", "parsed": {"today": None, "tomorrow": None}, "subscribers": set()}
    for key in ADDRS
}

# Глобальные переменные
DRIVER = None
BROWSER_LOCK = None

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- 📊 ГУГЛ ТАБЛИЦЫ ---
def log_to_sheets(user_name, username, action):
    try:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        sheet_id = os.environ.get("SPREADSHEET_ID")
        
        if not creds_json or not sheet_id:
            return 

        creds_dict = json.loads(creds_json)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(sheet_id).sheet1
        
        now = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%d.%m.%Y %H:%M:%S")
        uname = f"@{username}" if username else "Скрыт" 
        
        row = [now, user_name, uname, action]
        sheet.append_row(row)
    except Exception as e:
        print(f"⚠️ Ошибка записи в таблицу: {e}")

async def async_log(user_name, username, action):
    await asyncio.to_thread(log_to_sheets, user_name, username, action)

# --- 🚀 УПРАВЛЕНИЕ БРАУЗЕРОМ ---
def close_browser():
    global DRIVER
    if DRIVER is not None:
        print("💤 Закрываю браузер (освобождаю память)...")
        try: DRIVER.quit()
        except: pass
        DRIVER = None

async def safe_close_browser():
    global BROWSER_LOCK
    async with BROWSER_LOCK:
        close_browser()

def get_browser():
    global DRIVER
    if DRIVER is not None:
        try:
            _ = DRIVER.title
            return DRIVER
        except:
            close_browser()

    print("🚀 Открываю НОВЫЙ браузер Chrome...")
    chrome_options = Options()
    if IS_LOCAL_TESTING:
        chrome_options.add_argument("--start-maximized")
    else:
        chrome_options.add_argument("--headless=new") 
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
    
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("mobileEmulation", { "deviceName": "iPhone XR" })
    
    try:
        service = Service(ChromeDriverManager().install())
        DRIVER = webdriver.Chrome(service=service, options=chrome_options)
        return DRIVER
    except Exception as e:
        print(f"❌ Ошибка запуска Chrome: {e}")
        return None

# --- 🕵️ СИНХРОННЫЙ ПАРСЕР ---
def sync_parse_dtek(addr_key, addr):
    global DRIVER
    print(f"🕵️ MONITOR: Проверяю {addr['street']} {addr['house']}...")
    driver = get_browser()
    if not driver: return None, None
    
    wait = WebDriverWait(driver, 15)
    parsed_data = {"today": None, "tomorrow": None}
    schedule_fingerprint = "" 
    
    try:
        driver.get(DTEK_URL)
        time.sleep(1.5) 

        def nuke():
            try:
                driver.execute_script("""
                    document.body.style.overflow = 'visible';
                    var bad = document.querySelectorAll('.modal, .modal-backdrop, .popup, .banner, iframe, .header, .cookie');
                    bad.forEach(el => el.remove());
                    var all = document.querySelectorAll('*');
                    for (var i=0; i<all.length; i++) {
                        var style = window.getComputedStyle(all[i]);
                        if (style.position == 'fixed' || style.zIndex > 100) {
                             if (all[i].className.indexOf('header') == -1) all[i].remove();
                        }
                    }
                """)
            except: pass
        nuke()

        def safe_fill(field, val):
            nuke()
            try:
                el = wait.until(EC.presence_of_element_located((By.NAME, field)))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                driver.execute_script(f"arguments[0].value = '{val}';", el)
                driver.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles:true}));", el)
                time.sleep(0.8)
                driver.execute_script(f"""
                    var list = document.getElementById('{field}autocomplete-list');
                    if(list) {{ var items = list.getElementsByTagName('div'); if(items.length>0) items[0].click(); }}
                """)
                time.sleep(0.5)
            except: pass

        safe_fill("city", addr['city'])
        safe_fill("street", addr['street'])
        
        try:
            el_house = wait.until(EC.presence_of_element_located((By.NAME, "house_num")))
            driver.execute_script(f"arguments[0].value = '{addr['house']}';", el_house)
            driver.execute_script("arguments[0].dispatchEvent(new Event('input'));", el_house)
            time.sleep(0.5)
            el_house.send_keys(Keys.ENTER)
        except: pass

        time.sleep(2.5)
        nuke()

        try:
            schedule_fingerprint = driver.execute_script("""
                var cells = document.querySelectorAll('.table2col td');
                var res = [];
                for(var i=0; i<cells.length; i++) {
                    var cls = cells[i].className || "";
                    var state = "🟢"; 
                    if(cls.includes("scheduled") && !cls.includes("non")) state = "🔴"; 
                    if(cls.includes("maybe")) state = "🟡"; 
                    res.push(state);
                }
                return res.join('');
            """)
        except: 
            schedule_fingerprint = "error"

        def get_status():
            try:
                h = (datetime.now(timezone.utc) + timedelta(hours=2)).hour
                t_str = f"{h:02d}-{h+1:02d}"
                script = f"""
                var tds = document.querySelectorAll('td');
                for(var i=0; i<tds.length; i++) {{
                    if(tds[i].innerText.includes('{t_str}')) {{
                        var n = tds[i].nextElementSibling;
                        if(n) return n.className || 'clean';
                    }}
                }}
                return 'not_found';"""
                cls = driver.execute_script(script)
                if cls == 'not_found': return f"❓ Час не знайдено ({t_str})"
                if "scheduled" in cls and "non" not in cls: return "🔴 СВІТЛА НЕМАЄ"
                if "maybe" in cls: return "🟡 МОЖЛИВЕ ВІДКЛЮЧЕННЯ"
                return "🟢 СВІТЛО Є"
            except: return "❓ Статус невідомий"

        status_now = get_status()
        base_caption = f"{status_now}\n🏠 {addr['header']}"

        # ФОТО 1: Сьогодні
        try:
            target = driver.find_element(By.CLASS_NAME, "table2col")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
            path1 = os.path.join(BASE_DIR, f"photo_{addr_key}_today.png")
            target.screenshot(path1)
            parsed_data["today"] = {"photo": path1, "caption": f"{base_caption}"}
        except: pass

        # 🔥 ФОТО 2: СНАЙПЕРСЬКИЙ ПОШУК КНОПКИ "ЗАВТРА" 🔥
        try:
            clicked_text = driver.execute_script("""
                var els = Array.from(document.querySelectorAll('*')).reverse();
                
                // Шукаємо елемент, у якого є слово "завтра", але СУВОРО немає слова "сьогодні"
                var target = els.find(e => {
                    var txt = (e.innerText || "").toLowerCase();
                    return txt.includes("завтра") && !txt.includes("сьогодні");
                });
                
                if (target) {
                    // Симулюємо повноцінний клік мишкою
                    target.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                    try { target.click(); } catch(e) {}
                    
                    // Часто клік працює на батьківському блоці
                    if (target.parentElement) {
                        target.parentElement.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                        try { target.parentElement.click(); } catch(e) {}
                    }
                    return target.innerText.trim(); 
                }
                return null;
            """)
            
            if clicked_text:
                time.sleep(3.5) # Даємо час сайту на підвантаження даних
                nuke()
                
                # Знаходимо всі таблиці і беремо ОСТАННЮ ВИДИМУ
                tables = driver.find_elements(By.CLASS_NAME, "table2col")
                visible_tables = [t for t in tables if t.is_displayed()]
                
                if visible_tables:
                    target2 = visible_tables[-1]
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target2)
                    time.sleep(0.5) 
                    
                    path2 = os.path.join(BASE_DIR, f"photo_{addr_key}_tomorrow.png")
                    target2.screenshot(path2)
                    
                    # Очищаємо текст (беремо тільки останній рядок, якщо їх декілька)
                    d2_txt = clicked_text.split('\n')[-1].strip()
                    if not d2_txt or len(d2_txt) > 30:
                        d2_txt = "Завтра"
                    
                    parsed_data["tomorrow"] = {"photo": path2, "caption": f"ℹ️ Графік на завтра\n🏠 {addr['header']}\n📅 {d2_txt}"}
        except Exception as e:
            print(f"⚠️ ПОМИЛКА ПАРСИНГУ 'ЗАВТРА' ({addr_key}): {e}")

        return parsed_data, schedule_fingerprint

    except Exception as e:
        print(f"❌ Ошибка парсинга: {e}")
        close_browser() 
        return None, None

# --- 🚀 АСИНХРОННАЯ ОБЕРТКА ---
async def parse_dtek(addr_key, addr):
    global BROWSER_LOCK
    async with BROWSER_LOCK:
        return await asyncio.to_thread(sync_parse_dtek, addr_key, addr)

# --- ОТПРАВКА ---
async def send_schedule(user_id, addr_key, is_instant=False):
    data = STORAGE[addr_key]
    parsed = data["parsed"]
    today = parsed["today"]
    tmr = parsed["tomorrow"]

    if not today or not os.path.exists(today["photo"]):
        await bot.send_message(user_id, "⚠️ Вибачте, сталася помилка з отриманням графіка.")
        return

    caption = today["caption"]
    if is_instant:
        dt_utc = datetime.fromtimestamp(data["last_check"], tz=timezone.utc)
        dt_kyiv = dt_utc + timedelta(hours=2)
        update_time = dt_kyiv.strftime("%H:%M")
        caption += f"\n\n⚡ Миттєва відповідь (за {update_time})"

    reply_markup = None
    if tmr and os.path.exists(tmr["photo"]):
        caption += "\n\n✅ Доступний графік на завтра"
        btn = InlineKeyboardButton(text="📅 Показати на завтра", callback_data=f"tmr_{addr_key}")
        reply_markup = InlineKeyboardMarkup(inline_keyboard=[[btn]])
    else:
        caption += "\n\n❌ Графік на завтра поки відсутній"

    await bot.send_photo(chat_id=user_id, photo=FSInputFile(today["photo"]), caption=caption, reply_markup=reply_markup)

# --- 🔥 ЛОГИКА УМНОЙ ПОДПИСКИ 🔥 ---
def switch_subscription(user_id, new_addr_key):
    for key in STORAGE:
        if user_id in STORAGE[key]["subscribers"]:
            STORAGE[key]["subscribers"].remove(user_id)
    STORAGE[new_addr_key]["subscribers"].add(user_id)

# --- ЛОГИКА ПРОВЕРКИ ---
async def perform_check(user_id, addr_key):
    switch_subscription(user_id, addr_key)
    
    data = STORAGE[addr_key]
    parsed = data["parsed"]
    
    need_refresh = False
    if not parsed["today"]: need_refresh = True
    elif not os.path.exists(parsed["today"]["photo"]): need_refresh = True
    elif parsed["tomorrow"] and not os.path.exists(parsed["tomorrow"]["photo"]): need_refresh = True
    
    if need_refresh:
        status_message = await bot.send_message(user_id, "🐢 Оновлюю дані для цієї адреси...")
        new_parsed, new_fp = await parse_dtek(addr_key, ADDRS[addr_key])
        await safe_close_browser()
        
        if new_parsed and new_parsed["today"]:
            STORAGE[addr_key]["parsed"] = new_parsed
            STORAGE[addr_key]["fingerprint"] = new_fp
            STORAGE[addr_key]["last_check"] = time.time()
            await status_message.delete()
            await send_schedule(user_id, addr_key)
        else:
            await status_message.edit_text("❌ Не вдалося отримати графік.")
    else:
        await send_schedule(user_id, addr_key, is_instant=True)

# --- 🔄 ЦИКЛ МОНИТОРИНГА ---
async def monitoring_loop():
    print("🤖 Запускаю цикл мониторинга...")
    await asyncio.sleep(5)
    
    while True:
        try:
            print("--- 🔍 НАЧИНАЮ ПЛАНОВУЮ ПРОВЕРКУ ВСЕХ АДРЕСОВ ---")
            for addr_key, addr_data in ADDRS.items():
                new_parsed, new_fingerprint = await parse_dtek(addr_key, addr_data)
                
                if new_parsed and new_parsed["today"]:
                    old_fingerprint = STORAGE[addr_key]["fingerprint"]
                    
                    STORAGE[addr_key]["parsed"] = new_parsed
                    STORAGE[addr_key]["fingerprint"] = new_fingerprint
                    STORAGE[addr_key]["last_check"] = time.time()
                    
                    if old_fingerprint and old_fingerprint != "error" and new_fingerprint != "error" and new_fingerprint != old_fingerprint:
                        subs = STORAGE[addr_key]["subscribers"]
                        if subs:
                            for user_id in subs:
                                try:
                                    await bot.send_message(user_id, f"🔔 <b>Увага! Графік оновився!</b>\n{addr_data['header']}", parse_mode="HTML")
                                    await send_schedule(user_id, addr_key)
                                except: pass
                                
            await safe_close_browser()
            print(f"⏳ Проверка завершена. Сплю {CHECK_INTERVAL // 60} минут...")
            await asyncio.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            print(f"⚠️ Ошибка в цикле мониторинга: {e}")
            await safe_close_browser()
            await asyncio.sleep(60)

# --- 🤖 КЛАВИАТУРЫ ---
def get_main_kb():
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="🏠 Новомиколаївка"))
    builder.add(KeyboardButton(text="🏢 Дніпро"))
    builder.adjust(2) 
    return builder.as_markup(resize_keyboard=True)

def get_dnipro_kb():
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="📍 Севастопольська, 16"))
    builder.row(KeyboardButton(text="📍 просп. Мануйлівський, 78"))
    builder.row(KeyboardButton(text="📍 вул. Мазепи Галини, 76"))
    builder.row(KeyboardButton(text="📍 вул. Володимира Вернадського, 19/21"))
    builder.row(KeyboardButton(text="🔙 Назад"))
    return builder.as_markup(resize_keyboard=True)

# --- 🤖 БОТ ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    asyncio.create_task(async_log(message.from_user.full_name, message.from_user.username, "🚀 Натиснув /start"))
    await message.answer("⚡ Фрафік на зв'язку! Обери населений пункт:", reply_markup=get_main_kb())

@dp.message(F.text == "🔙 Назад")
async def process_back(message: types.Message):
    asyncio.create_task(async_log(message.from_user.full_name, message.from_user.username, "🔙 Повернувся назад"))
    await message.answer("Обери населений пункт:", reply_markup=get_main_kb())

@dp.message(F.text == "🏠 Новомиколаївка")
async def process_novo(message: types.Message):
    asyncio.create_task(async_log(message.from_user.full_name, message.from_user.username, "🏠 Новомиколаївка"))
    await perform_check(message.from_user.id, "addr1")

@dp.message(F.text == "🏢 Дніпро")
async def process_dnipro_menu(message: types.Message):
    asyncio.create_task(async_log(message.from_user.full_name, message.from_user.username, "🏢 Відкрив меню Дніпра"))
    await message.answer("📍 Оберіть вулицю в м. Дніпро:", reply_markup=get_dnipro_kb())

@dp.message(F.text == "📍 Севастопольська, 16")
async def process_dnipro_1(message: types.Message):
    asyncio.create_task(async_log(message.from_user.full_name, message.from_user.username, "📍 Севастопольська"))
    await perform_check(message.from_user.id, "dnipro_1")

@dp.message(F.text == "📍 просп. Мануйлівський, 78")
async def process_dnipro_2(message: types.Message):
    asyncio.create_task(async_log(message.from_user.full_name, message.from_user.username, "📍 Мануйлівський"))
    await perform_check(message.from_user.id, "dnipro_2")

@dp.message(F.text == "📍 вул. Мазепи Галини, 76")
async def process_dnipro_3(message: types.Message):
    asyncio.create_task(async_log(message.from_user.full_name, message.from_user.username, "📍 Мазепи Галини"))
    await perform_check(message.from_user.id, "dnipro_3")

@dp.message(F.text == "📍 вул. Володимира Вернадського, 19/21")
async def process_dnipro_4(message: types.Message):
    asyncio.create_task(async_log(message.from_user.full_name, message.from_user.username, "📍 Вернадського"))
    await perform_check(message.from_user.id, "dnipro_4")

@dp.callback_query(F.data.startswith("tmr_"))
async def process_tomorrow(callback: types.CallbackQuery):
    addr_key = callback.data.split("_", 1)[1]
    asyncio.create_task(async_log(callback.from_user.full_name, callback.from_user.username, f"📅 Дивився на завтра ({addr_key})"))
    
    data = STORAGE.get(addr_key)
    if data and data["parsed"] and data["parsed"]["tomorrow"]:
        tmr = data["parsed"]["tomorrow"]
        if os.path.exists(tmr["photo"]):
            await callback.message.answer_photo(FSInputFile(tmr["photo"]), caption=tmr["caption"])
        else:
            await callback.answer("⚠️ Файл втрачено, оновіть графік", show_alert=True)
    else:
        await callback.answer("Графік на завтра відсутній.", show_alert=True)
    await callback.answer()

# --- 🌍 ВЕБ-СЕРВЕР ---
async def health_check(request): return web.Response(text="Grafik is watching!", status=200)

async def start_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌍 Server started on port {port}")

async def main():
    print("🚀 Фрафік запускається...")
    
    global BROWSER_LOCK
    BROWSER_LOCK = asyncio.Lock()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(dp.start_polling(bot), start_server(), monitoring_loop())

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: print("Бот остановлен")
