import sqlite3
from bot.config import DB_PATH, ADMIN_ID, MSGS, bot, log, BW_PAGE_LIMIT, COLOR_PAGE_LIMIT, ALERT_INTERVAL
from cryptography.fernet import Fernet
from bot.config import ENCRYPTION_KEY


# --- Init ---

def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ink_counters (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                bw_pages INTEGER DEFAULT 0,
                color_pages INTEGER DEFAULT 0,
                last_alert_bw INTEGER DEFAULT 0,
                last_alert_color INTEGER DEFAULT 0,
                log_offset INTEGER DEFAULT 0
            )
        """)
        conn.execute("INSERT OR IGNORE INTO ink_counters (id, bw_pages, color_pages, log_offset) VALUES (1, 0, 0, 0)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                address TEXT DEFAULT '',
                encrypted_password TEXT DEFAULT '',
                timer_minutes INTEGER DEFAULT 0
            )
        """)
        conn.execute("INSERT OR IGNORE INTO email_config (id) VALUES (1)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                email TEXT NOT NULL UNIQUE
            )
        """)
        conn.commit()
    finally:
        conn.close()


# --- Generic query helper ---

def db_query(sql: str, params: tuple = (), fetch_one=False, commit=False):
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


# --- Ink counters ---

def get_ink_counters() -> tuple[int, int]:
    row = db_query("SELECT bw_pages, color_pages FROM ink_counters WHERE id = 1", fetch_one=True)
    return row if row else (0, 0)


def get_log_offset() -> int:
    row = db_query("SELECT log_offset FROM ink_counters WHERE id = 1", fetch_one=True)
    return row[0] if row else 0


def set_log_offset(offset: int):
    db_query("UPDATE ink_counters SET log_offset = ? WHERE id = 1", (offset,), commit=True)


def add_pages(bw: int = 0, color: int = 0):
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
        _check_ink_alerts(row[0], row[1], row[2], row[3])


def _check_ink_alerts(bw_pages: int, color_pages: int, last_alert_bw: int, last_alert_color: int):
    alerts = []
    if bw_pages >= BW_PAGE_LIMIT:
        next_alert_at = BW_PAGE_LIMIT + ((last_alert_bw + 1) * ALERT_INTERVAL)
        if bw_pages >= next_alert_at:
            new_alert_count = ((bw_pages - BW_PAGE_LIMIT) // ALERT_INTERVAL) + 1
            db_query("UPDATE ink_counters SET last_alert_bw = ? WHERE id = 1", (new_alert_count,), commit=True)
            alerts.append(MSGS["ink_alert_bw"].format(count=bw_pages, limit=BW_PAGE_LIMIT))
    if color_pages >= COLOR_PAGE_LIMIT:
        next_alert_at = COLOR_PAGE_LIMIT + ((last_alert_color + 1) * ALERT_INTERVAL)
        if color_pages >= next_alert_at:
            new_alert_count = ((color_pages - COLOR_PAGE_LIMIT) // ALERT_INTERVAL) + 1
            db_query("UPDATE ink_counters SET last_alert_color = ? WHERE id = 1", (new_alert_count,), commit=True)
            alerts.append(MSGS["ink_alert_color"].format(count=color_pages, limit=COLOR_PAGE_LIMIT))
    for alert in alerts:
        try:
            bot.send_message(ADMIN_ID, alert, parse_mode="Markdown")
        except Exception as e:
            log.error("Failed to send ink alert: %s", e)


def reset_ink_counters():
    db_query(
        "UPDATE ink_counters SET bw_pages = 0, color_pages = 0, last_alert_bw = 0, last_alert_color = 0 WHERE id = 1",
        commit=True
    )


# --- Email config ---

def get_email_config() -> tuple[str, str, int]:
    row = db_query("SELECT address, encrypted_password, timer_minutes FROM email_config WHERE id = 1", fetch_one=True)
    return row if row else ("", "", 0)


def set_email_config(field: str, value):
    db_query(f"UPDATE email_config SET {field} = ? WHERE id = 1", (value,), commit=True)


def encrypt_password(plain: str) -> str:
    return Fernet(ENCRYPTION_KEY).encrypt(plain.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    return Fernet(ENCRYPTION_KEY).decrypt(encrypted.encode()).decode()


# --- User emails ---

def get_user_emails(user_id: int) -> list[str]:
    rows = db_query("SELECT email FROM user_emails WHERE user_id = ?", (user_id,))
    return [r[0] for r in rows]


def get_all_user_emails() -> list[tuple[int, str]]:
    return db_query("SELECT user_id, email FROM user_emails")


def add_user_email(user_id: int, addr: str) -> bool:
    try:
        db_query("INSERT INTO user_emails (user_id, email) VALUES (?, ?)", (user_id, addr.lower()), commit=True)
        return True
    except sqlite3.IntegrityError:
        return False


def remove_user_email(user_id: int, addr: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_emails WHERE user_id = ? AND email = ?", (user_id, addr.lower()))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def find_user_by_email(addr: str) -> int | None:
    row = db_query("SELECT user_id FROM user_emails WHERE email = ?", (addr.lower(),), fetch_one=True)
    return row[0] if row else None


# --- User helpers ---

def get_user_role(chat_id: int) -> str | None:
    if chat_id == ADMIN_ID:
        return "VIP"
    row = db_query("SELECT role FROM users WHERE id = ?", (chat_id,), fetch_one=True)
    return row[0] if row else None


def get_user_profile(chat_id: int) -> tuple[str | None, str | None, int]:
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
