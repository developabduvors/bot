# -*- coding: utf-8 -*-
"""
Shaxsiy o'quv bot: 2 rejim
  🇬🇧 Ingliz tili  -> Gemini (eski suhbat konteksti bilan davom etadi)
  💻 Code          -> Gemini 2.5 Flash (dasturlash bo'yicha yordam)

Qo'shimcha:
  🎤 Ovozli xabar   — bot eshitib javob beradi (gapirish mashqi)
  🖼 Rasm           — skrinshot/xato rasmini tahlil qiladi
  ⏰ Kunlik dars    — har kuni 20:00 (Toshkent) da bot o'zi dars boshlaydi
  ✨ Formatlash     — kod bloklari Telegram'da chiroyli chiqadi
"""
import datetime
import json
import os
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from telegram import BotCommand, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))  # 0 = hamma foydalanishi mumkin

BASE_DIR = Path(__file__).parent
# Railway'da Volume ulansa DATA_DIR=/data qilib qo'yiladi — ma'lumotlar saqlanib qoladi
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DATA_FILE = DATA_DIR / "users.json"
ENGLISH_CONTEXT_FILE = BASE_DIR / "english_context.txt"

MAX_HISTORY = 40          # har rejimda oxirgi 40 ta xabar saqlanadi
TG_LIMIT = 4000           # Telegram 4096 belgi limiti, zaxira bilan
TASHKENT = ZoneInfo("Asia/Tashkent")

gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

KEYBOARD = ReplyKeyboardMarkup(
    [["🇬🇧 Ingliz tili", "💻 Code"], ["🗑 Suhbatni tozalash"]],
    resize_keyboard=True,
)

ENGLISH_SYSTEM = """Sen ingliz tili o'qituvchisisan. Foydalanuvchi o'zbek, ingliz tilini o'rganyapti.
Uning oldingi suhbatidagi kontekst quyida berilgan — shu suhbatni davom ettir,
muammolarini bilasan, har kuni yechim va mashqlar berasan. Sodda, do'stona, aniq javob ber.
Javoblarni Telegram uchun qisqa qil. Muhim so'zlarni **qalin** qilib belgila.

--- OLDINGI SUHBAT KONTEKSTI ---
{context}
--- KONTEKST TUGADI ---"""

CODE_SYSTEM = """Sen tajribali dasturchi-mentorsan. Foydalanuvchi o'zbek tilida yozadi,
dasturlashni o'rganyapti (React, Tailwind). Kod misollari bilan tushuntir, javoblarni
Telegram uchun qisqa va aniq qil. Kod bloklarini ``` ichida ber."""

VOICE_INSTRUCTION = """Bu ovozli xabar. Quyidagicha javob ber:
1. Avval "📝 Sen dеding:" deb aytganlarimni yozib ber (transkripsiya).
2. Agar inglizcha gapirgan bo'lsam — talaffuz va gap tuzilishimga qisqa feedback ber,
   xatolarimni to'g'rila.
3. Keyin savolimga/gapimga javob ber."""

DAILY_LESSON_PROMPT = """Soat 20:00 bo'ldi — kunlik amaliy mentorlik vaqti (rejamiz bo'yicha).
Bugungi qisqa darsni boshla: bitta aniq mashq yoki savol ber (masalan: kod logikasini
inglizcha tushuntirish, 3-5 ta yangi so'z, yoki kichik test). Qisqa va amaliy bo'lsin.
21:00 da to'xtashimni eslat."""

VAZIFA_PROMPT = """Menga darajamga mos BITTA kichik amaliy kod vazifasi ber (React, JavaScript
yoki Tailwind). Vazifa aniq bo'lsin: nima qilish kerak, qanday natija kutiladi.
Suhbat tarixiga qarab qiyinligini tanlа — oldin qiynalgan mavzularimni mustahkamla.
Men yechimni yozganimda: qattiq tekshir, 10 ballik baho qo'y, xatolarni tushuntir,
to'g'ri yechimni ko'rsat."""

FILE_REVIEW_PROMPT = """Bu kod faylimni review qil:
1. Xatolar va buglar bormi?
2. Clean code bo'yicha nimani yaxshilash mumkin?
3. Qisqa xulosa: nima yaxshi, nima ustida ishlash kerak."""


# ---------- ma'lumotlarni saqlash ----------

def load_data() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {}


def save_data(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_user(data: dict, user_id: int) -> dict:
    key = str(user_id)
    if key not in data:
        data[key] = {"mode": "english", "english": [], "code": []}
    data[key].setdefault("memory", {"english": "", "code": ""})
    data[key].setdefault("words", [])
    data[key].setdefault("stats", {"days": {}, "tasks": 0})
    return data[key]


def today_str() -> str:
    return datetime.datetime.now(TASHKENT).strftime("%Y-%m-%d")


def calc_streak(days: dict) -> int:
    """Ketma-ket faol kunlar soni (bugundan yoki kechadan orqaga sanaladi)."""
    d = datetime.datetime.now(TASHKENT).date()
    if d.strftime("%Y-%m-%d") not in days:
        d -= datetime.timedelta(days=1)  # bugun hali yozmagan bo'lsa, kechadan boshlaymiz
    streak = 0
    while d.strftime("%Y-%m-%d") in days:
        streak += 1
        d -= datetime.timedelta(days=1)
    return streak


def english_context() -> str:
    if ENGLISH_CONTEXT_FILE.exists():
        return ENGLISH_CONTEXT_FILE.read_text(encoding="utf-8").strip()
    return "(kontekst hali kiritilmagan)"


def system_for(mode: str, memory: str = "") -> str:
    base = ENGLISH_SYSTEM.format(context=english_context()) if mode == "english" else CODE_SYSTEM
    if memory:
        base += (
            "\n\n--- UZOQ MUDDATLI XOTIRA (oldingi suhbatlar xulosasi) ---\n"
            + memory
            + "\n--- XOTIRA TUGADI ---"
        )
    return base


# ---------- Gemini ----------

def history_to_contents(history: list[dict]) -> list:
    return [
        genai_types.Content(role=m["role"], parts=[genai_types.Part(text=m["text"])])
        for m in history
    ]


def generate(contents: list, system: str, model: str = "gemini-2.5-flash") -> str:
    config = genai_types.GenerateContentConfig(system_instruction=system)
    # 503 (server band) bo'lsa: kutib qayta urinadi, keyin zaxira modelga o'tadi
    attempts = [model, model, model, "gemini-2.5-flash-lite"]
    last_error = None
    for i, m in enumerate(attempts):
        try:
            resp = gemini.models.generate_content(model=m, contents=contents, config=config)
            return resp.text or "Javob kelmadi, qayta urinib ko'r."
        except Exception as e:
            last_error = e
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                time.sleep(2 * (i + 1))
                continue
            raise
    raise last_error


# ---------- javob yuborish (chiroyli formatlash) ----------

def split_chunks(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Uzun matnni qator chegarasida bo'ladi (kod bloklari buzilmasligi uchun)."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:]
    if text.strip():
        chunks.append(text)
    return chunks


CODE_BLOCK_RE = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)
LANG_EXT = {
    "python": "py", "javascript": "js", "js": "js", "jsx": "jsx",
    "typescript": "ts", "tsx": "tsx", "html": "html", "css": "css",
    "json": "json", "bash": "sh", "sql": "sql",
}


def extract_long_code(text: str) -> tuple[str, list[tuple[str, str]]]:
    """1500+ belgili kod bloklarini ajratib, fayl sifatida yuborishga tayyorlaydi."""
    files: list[tuple[str, str]] = []

    def repl(m: re.Match) -> str:
        lang, code = (m.group(1) or "").lower(), m.group(2)
        if len(code) > 1500:
            name = f"kod_{len(files) + 1}.{LANG_EXT.get(lang, 'txt')}"
            files.append((name, code))
            return f"📄 Uzun kod ilova faylda: {name}"
        return m.group(0)

    return CODE_BLOCK_RE.sub(repl, text), files


async def send_answer(update: Update, text: str) -> None:
    text, files = extract_long_code(text)
    for chunk in split_chunks(text):
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown", reply_markup=KEYBOARD)
        except BadRequest:
            # Markdown buzilgan bo'lsa oddiy matn sifatida yuboriladi
            await update.message.reply_text(chunk, reply_markup=KEYBOARD)
    for name, code in files:
        await update.message.reply_document(document=code.encode("utf-8"), filename=name)


# ---------- umumiy AI so'rov oqimi ----------

def update_memory(user: dict, mode: str) -> None:
    """Tarix limitdan oshsa, eski xabarlarni doimiy xotiraga xulosa qilib qo'shadi."""
    history = user[mode]
    if len(history) <= MAX_HISTORY:
        return
    overflow = history[:-MAX_HISTORY]
    user[mode] = history[-MAX_HISTORY:]
    old_memory = user["memory"].get(mode, "")
    dialog = "\n".join(f"{m['role']}: {m['text'][:500]}" for m in overflow)
    prompt = (
        "Quyida foydalanuvchi bilan eski suhbat qismi va avvalgi xotira bor. "
        "Ikkalasini birlashtirib, YANGI qisqa xotira yoz (maks 300 so'z): "
        "foydalanuvchi nimani o'rgandi, qanday xatolar qilardi, nimalar kelishildi, "
        "muhim faktlar. Faqat xotira matnini qaytar.\n\n"
        f"--- AVVALGI XOTIRA ---\n{old_memory or '(bo`sh)'}\n\n"
        f"--- ESKI SUHBAT ---\n{dialog}"
    )
    try:
        contents = [genai_types.Content(role="user", parts=[genai_types.Part(text=prompt)])]
        user["memory"][mode] = generate(
            contents, "Sen suhbat xulosachisisan.", model="gemini-2.5-flash-lite"
        )
    except Exception as e:
        print(f"[xotira yangilash xatosi] {e}")  # xotira yangilanmasa ham bot ishlayveradi


async def process_request(update: Update, user_text: str, extra_part=None) -> None:
    """Matn/ovoz/rasm — hammasi shu yerdan o'tadi."""
    data = load_data()
    user = get_user(data, update.effective_user.id)
    mode = user["mode"]
    history = user[mode]

    contents = history_to_contents(history)
    parts = [genai_types.Part(text=user_text)]
    if extra_part is not None:
        parts.insert(0, extra_part)
    contents.append(genai_types.Content(role="user", parts=parts))

    await update.message.chat.send_action("typing")
    try:
        answer = generate(contents, system_for(mode, user["memory"].get(mode, "")))
    except Exception as e:
        await update.message.reply_text(f"Xatolik: {e}")
        return

    history.append({"role": "user", "text": user_text})
    history.append({"role": "model", "text": answer})
    update_memory(user, mode)
    # statistika: bugungi faollik +1
    day = today_str()
    user["stats"]["days"][day] = user["stats"]["days"].get(day, 0) + 1
    save_data(data)
    await send_answer(update, answer)


# ---------- handlerlar ----------

def allowed(update: Update) -> bool:
    return OWNER_ID == 0 or (update.effective_user and update.effective_user.id == OWNER_ID)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_text(
        "Salom! Men sening o'quv botingman.\n\n"
        "🇬🇧 Ingliz tili — Gemini bilan suhbat (eski kontekst davom etadi)\n"
        "💻 Code — dasturlash bo'yicha yordam\n"
        "🎤 Ovoz yubor — eshitib javob beraman (gapirish mashqi!)\n"
        "🖼 Rasm yubor — skrinshot/xatoni tahlil qilaman\n"
        "📎 Kod faylini tashla — review qilaman\n"
        "🎯 /vazifa — darajangga mos kod topshirig'i\n"
        "⏰ Har kuni 20:00 da o'zim dars boshlayman\n\n"
        "Rejimni tanla va yozaver 👇",
        reply_markup=KEYBOARD,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update) or not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    data = load_data()
    user = get_user(data, update.effective_user.id)

    if text == "🇬🇧 Ingliz tili":
        user["mode"] = "english"
        save_data(data)
        await update.message.reply_text(
            "🇬🇧 Ingliz tili rejimi. Yozaver yoki 🎤 ovoz yubor!", reply_markup=KEYBOARD
        )
        return
    if text == "💻 Code":
        user["mode"] = "code"
        save_data(data)
        await update.message.reply_text(
            "💻 Code rejimi. Savol yoz yoki 🖼 skrinshot tashla!", reply_markup=KEYBOARD
        )
        return
    if text == "🗑 Suhbatni tozalash":
        user[user["mode"]] = []
        save_data(data)
        await update.message.reply_text("Joriy rejim suhbati tozalandi ✅", reply_markup=KEYBOARD)
        return

    await process_request(update, text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update) or not update.message:
        return
    voice = update.message.voice or update.message.audio
    if voice is None:
        return
    tg_file = await voice.get_file()
    audio_bytes = bytes(await tg_file.download_as_bytearray())
    part = genai_types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg")
    await process_request(update, VOICE_INSTRUCTION, extra_part=part)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update) or not update.message or not update.message.photo:
        return
    photo = update.message.photo[-1]  # eng katta o'lchamdagisi
    tg_file = await photo.get_file()
    img_bytes = bytes(await tg_file.download_as_bytearray())
    part = genai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
    caption = update.message.caption or "Bu rasmni tahlil qilib ber."
    await process_request(update, caption, extra_part=part)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kod faylini qabul qilib review qiladi (.js, .jsx, .css, .py va h.k.)."""
    if not allowed(update) or not update.message or not update.message.document:
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > 300_000:
        await update.message.reply_text("Fayl juda katta (300KB dan oshmasin).")
        return
    tg_file = await doc.get_file()
    raw = bytes(await tg_file.download_as_bytearray())
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        await update.message.reply_text(
            "Bu matn/kod fayliga o'xshamayapti. .js, .jsx, .css, .html, .py "
            "kabi fayllarni yubor."
        )
        return
    task = update.message.caption or FILE_REVIEW_PROMPT
    user_text = f"Fayl: {doc.file_name}\n```\n{content}\n```\n\n{task}"
    await process_request(update, user_text)


async def cmd_vazifa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Darajaga mos kod topshirig'i beradi: /vazifa"""
    if not allowed(update):
        return
    data = load_data()
    user = get_user(data, update.effective_user.id)
    user["mode"] = "code"  # vazifa doim Code rejimida
    user["stats"]["tasks"] = user["stats"].get("tasks", 0) + 1
    save_data(data)
    await process_request(update, VAZIFA_PROMPT)


async def cmd_soz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """So'z daftari: /soz apple - olma (saqlash), /soz (ro'yxat)"""
    if not allowed(update):
        return
    data = load_data()
    user = get_user(data, update.effective_user.id)
    args = " ".join(context.args) if context.args else ""

    if not args:
        words = user["words"]
        if not words:
            await update.message.reply_text(
                "So'z daftaring bo'sh. Qo'shish:\n/soz apple - olma", reply_markup=KEYBOARD
            )
            return
        lines = [f"{i+1}. *{w['w']}* — {w['m']}" for i, w in enumerate(words[-50:])]
        text = f"📝 So'z daftaring ({len(words)} ta):\n\n" + "\n".join(lines)
        text += "\n\nTest olish: /test"
        await send_answer(update, text)
        return

    if "-" not in args:
        await update.message.reply_text(
            "Format: /soz inglizcha - tarjimasi\nMasalan: /soz improve - yaxshilamoq"
        )
        return
    word, meaning = (p.strip() for p in args.split("-", 1))
    if not word or not meaning:
        await update.message.reply_text("Format: /soz inglizcha - tarjimasi")
        return
    user["words"].append({"w": word, "m": meaning, "added": today_str(), "last_tested": ""})
    save_data(data)
    await update.message.reply_text(
        f"✅ Saqlandi: *{word}* — {meaning}\nJami: {len(user['words'])} ta so'z",
        parse_mode="Markdown",
        reply_markup=KEYBOARD,
    )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """So'z daftaridan test: /test"""
    if not allowed(update):
        return
    data = load_data()
    user = get_user(data, update.effective_user.id)
    words = user["words"]
    if not words:
        await update.message.reply_text(
            "Avval so'z qo'sh: /soz apple - olma", reply_markup=KEYBOARD
        )
        return
    # eng kam test qilinganlarini tanlaymiz (spaced repetition)
    picked = sorted(words, key=lambda w: w.get("last_tested", ""))[:5]
    for w in picked:
        w["last_tested"] = today_str()
    user["mode"] = "english"
    save_data(data)

    word_list = "\n".join(f"- {w['w']} ({w['m']})" for w in picked)
    prompt = (
        "So'z daftarimdan quyidagi so'zlar bo'yicha test ol:\n"
        f"{word_list}\n\n"
        "Har bir so'z uchun bitta savol ber (tarjima so'ra yoki gap tuzdirib ko'r), "
        "hammasini bitta xabarda raqamlab yubor. Men javob berganimda tekshirib, "
        "har biriga baho qo'y va xatolarimni tushuntir. So'zlarning tarjimasini "
        "savollarda ko'rsatma!"
    )
    await process_request(update, prompt)


async def cmd_statistika(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """O'qish statistikasi: /statistika"""
    if not allowed(update):
        return
    data = load_data()
    user = get_user(data, update.effective_user.id)
    stats = user["stats"]
    days = stats.get("days", {})
    streak = calc_streak(days)
    total_msgs = sum(days.values())
    today_msgs = days.get(today_str(), 0)

    text = (
        "📊 *Statistikang:*\n\n"
        f"🔥 Streak: *{streak} kun* ketma-ket\n"
        f"📅 Jami faol kunlar: {len(days)}\n"
        f"💬 Bugun: {today_msgs} ta xabar (jami: {total_msgs})\n"
        f"📝 So'z daftari: {len(user['words'])} ta so'z\n"
        f"🎯 Olingan vazifalar: {stats.get('tasks', 0)} ta\n"
    )
    if streak == 0:
        text += "\nBugun boshlasang, streak yana yonadi 🔥"
    elif streak >= 7:
        text += f"\nZo'r ketyapsan! {streak} kunlik seriya 💪"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=KEYBOARD)


# ---------- kunlik dars (20:00 Toshkent) ----------

async def daily_lesson(context: ContextTypes.DEFAULT_TYPE) -> None:
    if OWNER_ID == 0:
        return
    data = load_data()
    user = get_user(data, OWNER_ID)
    history = user["english"]

    lesson_prompt = DAILY_LESSON_PROMPT
    if user["words"]:
        due = sorted(user["words"], key=lambda w: w.get("last_tested", ""))[:3]
        word_list = ", ".join(f"{w['w']} ({w['m']})" for w in due)
        lesson_prompt += f"\n\nDars ichida so'z daftarimdagi shu so'zlarni ham takrorlat: {word_list}"
        for w in due:
            w["last_tested"] = today_str()

    contents = history_to_contents(history)
    contents.append(
        genai_types.Content(role="user", parts=[genai_types.Part(text=lesson_prompt)])
    )
    try:
        answer = generate(contents, system_for("english", user["memory"].get("english", "")))
    except Exception as e:
        print(f"[kunlik dars xatosi] {e}")
        return

    history.append({"role": "user", "text": "(kunlik dars vaqti)"})
    history.append({"role": "model", "text": answer})
    user["english"] = history[-MAX_HISTORY:]
    user["mode"] = "english"  # dars boshlanganda ingliz tili rejimiga o'tadi
    save_data(data)

    for chunk in split_chunks("⏰ Kunlik dars vaqti!\n\n" + answer):
        try:
            await context.bot.send_message(OWNER_ID, chunk, parse_mode="Markdown", reply_markup=KEYBOARD)
        except BadRequest:
            await context.bot.send_message(OWNER_ID, chunk, reply_markup=KEYBOARD)


async def cmd_dars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Darsni qo'lda boshlash (test uchun): /dars"""
    if not allowed(update):
        return
    await update.message.chat.send_action("typing")
    await daily_lesson(context)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    print(f"[xato, qayta ulanyapti] {type(err).__name__}: {err}")


async def setup_commands(app: Application) -> None:
    """'/' yozilganda Telegram'da chiqadigan buyruqlar ro'yxati (podskazka)."""
    await app.bot.set_my_commands([
        BotCommand("start", "Botni ishga tushirish / menyu"),
        BotCommand("vazifa", "🎯 Darajamga mos kod topshirig'i ber"),
        BotCommand("dars", "⏰ Kunlik ingliz tili darsini hozir boshla"),
        BotCommand("soz", "📝 So'z saqlash: /soz apple - olma"),
        BotCommand("test", "🧪 So'z daftaridan test olish"),
        BotCommand("statistika", "📊 Streak va o'qish statistikasi"),
    ])


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(20)
        .read_timeout(30)
        .get_updates_read_timeout(40)
        .post_init(setup_commands)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("dars", cmd_dars))
    app.add_handler(CommandHandler("vazifa", cmd_vazifa))
    app.add_handler(CommandHandler("soz", cmd_soz))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("statistika", cmd_statistika))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_error_handler(on_error)

    if OWNER_ID != 0:
        app.job_queue.run_daily(
            daily_lesson,
            time=datetime.time(hour=20, minute=0, tzinfo=TASHKENT),
            name="kunlik_dars",
        )
        print("Kunlik dars rejalashtirildi: har kuni 20:00 (Toshkent)")

    print("Bot ishga tushdi... (to'xtatish: Ctrl+C)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
