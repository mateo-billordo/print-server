import os
import json
import re
import sqlite3
import subprocess
import logging
import threading
import time
from pathlib import Path
from telebot import TeleBot, types
from dotenv import load_dotenv

# --- Configuration ---

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip().strip("'\""))
PRINTER_NAME = os.getenv("PRINTER_NAME", "HP-2515")
DB_PATH = "/data/impresora_usuarios.db"
PRINT_DIR = Path("/data/impresiones")
PAGE_LOG_PATH = "/var/log/cups/page_log"

BW_PAGE_LIMIT = 200
COLOR_PAGE_LIMIT = 200
ALERT_INTERVAL = 25

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

bot = TeleBot(TOKEN, num_threads=4)

with open("messages.json", "r", encoding="utf-8") as f:
    MSGS = json.load(f)

PRINT_DIR.mkdir(parents=True, exist_ok=True)

# In-memory print job state: chat_id -> job dict
user_jobs: dict[int, dict] = {}
jobs_lock = threading.Lock()


# --- Database helpers ---

def db_query(sql: str, params: tuple = (), fetch_one=False, commit=False):
    """Execute a query against the users database."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        if commit:
            conn.commit()
            return None
        return cursor.fetchone() if fetch_one else cursor.fetchall()
    finally:
        conn.close()


def init_db():
    """Ensure ink_counters table exists."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ink_counters (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                bw_pages INTEGER DEFAULT 0,
                color_pages INTEGER DEFAULT 0,
                last_alert_bw INTEGER DEFAULT 0,
                last_alert_color INTEGER DEFAULT 0
            )
        """)
        conn.execute("INSERT OR IGNORE INTO ink_counters (id, bw_pages, color_pages) VALUES (1, 0, 0)")
        conn.commit()
    finally:
        conn.close()


def get_ink_counters() -> tuple[int, int]:
    """Returns (bw_pages, color_pages)."""
    row = db_query("SELECT bw_pages, color_pages FROM ink_counters WHERE id = 1", fetch_one=True)
    return row if row else (0, 0)


def add_pages(bw: int = 0, color: int = 0):
    """Increment page counters and check alert thresholds."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE ink_counters SET bw_pages = bw_pages + ?, color_pages = color_pages + ? WHERE id = 1",
            (bw, color)
        )
        conn.commit()
        cursor.execute("SELECT bw_pages, color_pages, last_alert_bw, last_alert_color FROM ink_counters WHERE id = 1")
        row = cursor.fetchone()
    finally:
        conn.close()

    if row:
        check_ink_alerts(row[0], row[1], row[2], row[3])


def check_ink_alerts(bw_pages: int, color_pages: int, last_alert_bw: int, last_alert_color: int):
    """Send admin alerts every ALERT_INTERVAL pages past threshold."""
    alerts = []

    if bw_pages >= BW_PAGE_LIMIT:
        pages_over = bw_pages - BW_PAGE_LIMIT
        next_alert_at = BW_PAGE_LIMIT + ((last_alert_bw + 1) * ALERT_INTERVAL)
        if bw_pages >= next_alert_at:
            new_alert_count = (pages_over // ALERT_INTERVAL) + 1
            db_query("UPDATE ink_counters SET last_alert_bw = ? WHERE id = 1", (new_alert_count,), commit=True)
            alerts.append(MSGS["ink_alert_bw"].format(count=bw_pages, limit=BW_PAGE_LIMIT))

    if color_pages >= COLOR_PAGE_LIMIT:
        pages_over = color_pages - COLOR_PAGE_LIMIT
        next_alert_at = COLOR_PAGE_LIMIT + ((last_alert_color + 1) * ALERT_INTERVAL)
        if color_pages >= next_alert_at:
            new_alert_count = (pages_over // ALERT_INTERVAL) + 1
            db_query("UPDATE ink_counters SET last_alert_color = ? WHERE id = 1", (new_alert_count,), commit=True)
            alerts.append(MSGS["ink_alert_color"].format(count=color_pages, limit=COLOR_PAGE_LIMIT))

    for alert in alerts:
        try:
            bot.send_message(ADMIN_ID, alert, parse_mode="Markdown")
        except Exception as e:
            log.error("Failed to send ink alert: %s", e)


def reset_ink_counters():
    """Reset all counters to zero."""
    db_query(
        "UPDATE ink_counters SET bw_pages = 0, color_pages = 0, last_alert_bw = 0, last_alert_color = 0 WHERE id = 1",
        commit=True
    )


# --- User helpers ---

def get_user_role(chat_id: int) -> str | None:
    if chat_id == ADMIN_ID:
        return "VIP"
    row = db_query("SELECT role FROM users WHERE id = ?", (chat_id,), fetch_one=True)
    return row[0] if row else None


def get_user_profile(chat_id: int) -> tuple[str | None, str | None, int]:
    """Returns (nickname, real_name, waiting_for_nickname)."""
    row = db_query(
        "SELECT nickname, real_name, waiting_for_nickname FROM users WHERE id = ?",
        (chat_id,), fetch_one=True
    )
    return row if row else (None, None, 0)


def set_nickname_state(chat_id: int, nickname: str | None = None, waiting: int = 0):
    if nickname is not None:
        db_query("UPDATE users SET nickname = ?, waiting_for_nickname = ? WHERE id = ?",
                 (nickname, waiting, chat_id), commit=True)
    else:
        db_query("UPDATE users SET waiting_for_nickname = ? WHERE id = ?",
                 (waiting, chat_id), commit=True)


def register_user(chat_id: int, real_name: str, role: str):
    db_query(
        "INSERT OR REPLACE INTO users (id, real_name, nickname, role, waiting_for_nickname) "
        "VALUES (?, ?, '', ?, 0)",
        (chat_id, real_name, role), commit=True
    )


# --- CUPS page_log parsing ---

def count_job_pages(job_id: str, max_wait: float = 10.0) -> int:
    """Parse CUPS page_log for a specific job ID and return total pages printed.
    Polls briefly since the log may not be written immediately."""
    pattern = re.compile(
        rf"^{re.escape(PRINTER_NAME)}\s+\S+\s+{re.escape(job_id)}\s+\S+\s+\d+\s+(\d+)"
    )
    deadline = time.time() + max_wait
    total = 0

    while time.time() < deadline:
        time.sleep(2)
        total = 0
        try:
            with open(PAGE_LOG_PATH, "r") as f:
                for line in f:
                    match = pattern.match(line)
                    if match:
                        total += int(match.group(1))  # num-copies per page
        except FileNotFoundError:
            pass

        if total > 0:
            break

    return total


# --- Print execution (runs in dedicated thread) ---

def execute_print_job(chat_id: int, job: dict):
    """Executes lp command in a background thread to avoid blocking the bot."""
    cmd = [
        "lp", "-d", PRINTER_NAME,
        "-n", str(job["copies"]),
        "-o", f"job-priority={job['priority']}",
        "-o", f"ColorModel={job['color']}",
        job["file_path"],
    ]
    log.info("Printing for user %d: %s", chat_id, " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        bot.send_message(chat_id, MSGS["print_success"].format(file=job["file_name"]), parse_mode="Markdown")

        # Extract job ID from lp output: "request id is HP-2515-123 (1 file(s))"
        match = re.search(r"request id is \S+-(\d+)", result.stdout)
        if match:
            job_id = match.group(1)
            pages = count_job_pages(job_id)
            if pages > 0:
                if job["color"] == "Gray":
                    add_pages(bw=pages)
                else:
                    add_pages(color=pages)
                log.info("Job %s: %d pages (%s)", job_id, pages, job["color"])
            else:
                # Fallback: count as 1 page × copies if page_log unavailable
                fallback = job["copies"]
                if job["color"] == "Gray":
                    add_pages(bw=fallback)
                else:
                    add_pages(color=fallback)
                log.warning("Job %s: page_log unavailable, counted %d pages (fallback)", job_id, fallback)
    else:
        log.error("lp failed for user %d: %s", chat_id, result.stderr)
        bot.send_message(chat_id, MSGS["print_error"])


# --- Inline keyboard builders ---

def build_auth_menu(target_id: int, target_name: str) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(MSGS["btn_auth_user"], callback_data=f"auth_USER_{target_id}_{target_name}"),
        types.InlineKeyboardButton(MSGS["btn_auth_vip"], callback_data=f"auth_VIP_{target_id}_{target_name}"),
    )
    return markup


def build_nickname_menu(has_nickname: bool) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    if has_nickname:
        markup.add(
            types.InlineKeyboardButton(MSGS["btn_nickname_change"], callback_data="nickname_change"),
            types.InlineKeyboardButton(MSGS["btn_nickname_delete"], callback_data="nickname_delete"),
        )
    else:
        markup.add(
            types.InlineKeyboardButton(MSGS["btn_nickname_create"], callback_data="nickname_change"),
        )
    return markup


def build_print_menu(chat_id: int) -> types.InlineKeyboardMarkup:
    with jobs_lock:
        job = user_jobs.get(chat_id, {"copies": 1, "color": "Gray"})

    color_label = MSGS["btn_color_mode"] if job["color"] == "Color" else MSGS["btn_bw_mode"]
    copies_label = MSGS["btn_copies_fmt"].format(count=job["copies"])

    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(
        types.InlineKeyboardButton(MSGS["btn_minus"], callback_data="copies_minus"),
        types.InlineKeyboardButton(copies_label, callback_data="noop"),
        types.InlineKeyboardButton(MSGS["btn_plus"], callback_data="copies_plus"),
    )
    markup.add(types.InlineKeyboardButton(f"Modo: {color_label}", callback_data="toggle_color"))
    markup.add(
        types.InlineKeyboardButton(MSGS["btn_cancel"], callback_data="print_cancel"),
        types.InlineKeyboardButton(MSGS["btn_print"], callback_data="print_confirm"),
    )
    return markup


# --- Command handlers ---

@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    tg_name = message.from_user.first_name
    role = get_user_role(chat_id)

    if not role:
        bot.reply_to(message, MSGS["pending_approval"])
        alert = MSGS["admin_access_request"].format(name=tg_name)
        try:
            bot.send_message(ADMIN_ID, alert, reply_markup=build_auth_menu(chat_id, tg_name))
        except Exception as e:
            log.error("Failed to notify admin: %s", e)
        return

    db_query("UPDATE users SET real_name = ? WHERE id = ?", (tg_name, chat_id), commit=True)
    bot.reply_to(message, MSGS["welcome"].format(name=tg_name))


@bot.message_handler(commands=['ayuda'])
def cmd_help(message):
    chat_id = message.chat.id
    if not get_user_role(chat_id):
        return
    if chat_id == ADMIN_ID:
        bot.reply_to(message, MSGS["help_admin"], parse_mode='Markdown')
    else:
        bot.reply_to(message, MSGS["help"], parse_mode='Markdown')


@bot.message_handler(commands=['usuarios'])
def cmd_list_users(message):
    if message.chat.id != ADMIN_ID:
        return

    rows = db_query("SELECT id, real_name, nickname, role FROM users")
    response = MSGS["user_list_title"]
    for uid, name, nick, role in rows:
        nick_display = f" ({nick})" if nick else ""
        response += f"• **ID:** `{uid}` | **Nombre:** {name}{nick_display} | **Rol:** `{role}`\n"

    bot.send_message(message.chat.id, response, parse_mode="Markdown")


@bot.message_handler(commands=['reset_ink'])
def cmd_reset_ink(message):
    if message.chat.id != ADMIN_ID:
        return
    reset_ink_counters()
    bot.reply_to(message, MSGS["ink_reset_success"])


@bot.message_handler(commands=['ink'])
def cmd_ink_status(message):
    if message.chat.id != ADMIN_ID:
        return
    bw, color = get_ink_counters()
    bot.reply_to(
        message,
        MSGS["ink_status"].format(bw=bw, bw_limit=BW_PAGE_LIMIT, color=color, color_limit=COLOR_PAGE_LIMIT),
        parse_mode="Markdown",
    )


@bot.message_handler(commands=['apodo'])
def cmd_nickname(message):
    if not get_user_role(message.chat.id):
        return

    nickname, _, _ = get_user_profile(message.chat.id)
    if nickname:
        text = MSGS["nickname_current"].format(nickname=nickname)
        markup = build_nickname_menu(has_nickname=True)
    else:
        text = MSGS["nickname_empty"]
        markup = build_nickname_menu(has_nickname=False)

    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")


# --- Text message handlers ---

@bot.message_handler(func=lambda m: m.text and "hola" in m.text.lower(), content_types=['text'])
def handle_greeting(message):
    if not get_user_role(message.chat.id):
        return

    nickname, real_name, _ = get_user_profile(message.chat.id)
    display_name = nickname or real_name or message.from_user.first_name
    bot.reply_to(message, MSGS["greeting"].format(name=display_name))


@bot.message_handler(func=lambda m: m.text and not m.text.startswith('/'), content_types=['text'])
def handle_text(message):
    if not get_user_role(message.chat.id):
        return

    _, _, waiting = get_user_profile(message.chat.id)

    if waiting == 1:
        new_nickname = message.text.strip()
        if len(new_nickname) > 20:
            bot.reply_to(message, MSGS["nickname_too_long"])
            return
        set_nickname_state(message.chat.id, nickname=new_nickname, waiting=0)
        bot.reply_to(message, MSGS["nickname_saved"].format(nickname=new_nickname), parse_mode="Markdown")
        return

    bot.reply_to(message, MSGS["unknown_command"])


# --- File/photo handler ---

@bot.message_handler(content_types=['document', 'photo'])
def handle_file(message):
    chat_id = message.chat.id
    role = get_user_role(chat_id)
    if not role:
        return

    priority = "5" if role == "VIP" else "3"

    if message.content_type == 'document':
        file_info = bot.get_file(message.document.file_id)
        file_name = message.document.file_name
    else:
        file_info = bot.get_file(message.photo[-1].file_id)
        file_name = f"photo_{message.message_id}.jpg"

    downloaded = bot.download_file(file_info.file_path)
    local_path = str(PRINT_DIR / file_name)

    with open(local_path, 'wb') as f:
        f.write(downloaded)

    with jobs_lock:
        user_jobs[chat_id] = {
            "file_path": local_path,
            "file_name": file_name,
            "copies": 1,
            "color": "Gray",
            "priority": priority,
        }

    bot.send_message(
        chat_id,
        MSGS["file_received"].format(file=file_name),
        reply_markup=build_print_menu(chat_id),
        parse_mode="Markdown",
    )


# --- Callback query handler ---

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    # --- Authorization callbacks (admin only) ---
    if call.data.startswith("auth_"):
        if chat_id != ADMIN_ID:
            return
        parts = call.data.split("_", 3)
        chosen_role = parts[1]
        target_id = int(parts[2])
        target_name = parts[3]

        register_user(target_id, target_name, chosen_role)
        bot.edit_message_text(
            MSGS["admin_auth_confirmed"].format(name=target_name, role=chosen_role),
            chat_id, msg_id
        )
        bot.answer_callback_query(call.id)
        bot.send_message(target_id, MSGS["user_auth_granted"].format(admin=call.from_user.first_name))
        return

    # --- Nickname callbacks ---
    if call.data == "nickname_change":
        set_nickname_state(chat_id, waiting=1)
        bot.edit_message_text(MSGS["nickname_prompt"], chat_id, msg_id)
        bot.answer_callback_query(call.id)
        return

    if call.data == "nickname_delete":
        set_nickname_state(chat_id, nickname="", waiting=0)
        bot.edit_message_text(MSGS["nickname_deleted"], chat_id, msg_id)
        bot.answer_callback_query(call.id)
        return

    # --- Print job callbacks ---
    with jobs_lock:
        if chat_id not in user_jobs:
            bot.answer_callback_query(call.id, MSGS["job_expired"])
            return
        job = user_jobs[chat_id]

    if call.data == "copies_plus":
        with jobs_lock:
            job["copies"] += 1
        bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=build_print_menu(chat_id))
        bot.answer_callback_query(call.id, str(job["copies"]))

    elif call.data == "copies_minus":
        with jobs_lock:
            if job["copies"] > 1:
                job["copies"] -= 1
        bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=build_print_menu(chat_id))
        bot.answer_callback_query(call.id, str(job["copies"]))

    elif call.data == "toggle_color":
        with jobs_lock:
            job["color"] = "Color" if job["color"] == "Gray" else "Gray"
        bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=build_print_menu(chat_id))
        bot.answer_callback_query(call.id)

    elif call.data == "print_cancel":
        with jobs_lock:
            user_jobs.pop(chat_id, None)
        try:
            os.remove(job["file_path"])
        except OSError:
            pass
        bot.edit_message_text(MSGS["cancelled"], chat_id, msg_id)
        bot.answer_callback_query(call.id)

    elif call.data == "print_confirm":
        with jobs_lock:
            user_jobs.pop(chat_id, None)
        bot.edit_message_text(MSGS["sending_to_queue"], chat_id, msg_id)
        bot.answer_callback_query(call.id)

        threading.Thread(
            target=execute_print_job,
            args=(chat_id, job),
            daemon=True,
        ).start()

    elif call.data == "noop":
        bot.answer_callback_query(call.id)


# --- Entry point ---

if __name__ == '__main__':
    init_db()
    log.info("Print server bot starting (threads=%d)...", 4)
    bot.infinity_polling()
