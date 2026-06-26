import os
import json
import re
import sqlite3
import subprocess
import logging
import threading
import time
import imaplib
import email as email_lib
from email.header import decode_header
from pathlib import Path
from telebot import TeleBot, types
from dotenv import load_dotenv
from cryptography.fernet import Fernet

import img2pdf
from PIL import Image

# --- Configuration ---

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip().strip("'\""))
PRINTER_NAME = os.getenv("PRINTER_NAME", "HP-2515")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").encode()
DB_PATH = "/data/impresora_usuarios.db"
PRINT_DIR = Path("/data/impresiones")
PAGE_LOG_PATH = "/var/log/cups/page_log"

BW_PAGE_LIMIT = 200
COLOR_PAGE_LIMIT = 200
ALERT_INTERVAL = 25
LOG_POLL_SECONDS = 5
UNASSIGNED_USER_ID = -1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

bot = TeleBot(TOKEN, num_threads=4)

with open("messages.json", "r", encoding="utf-8") as f:
    MSGS = json.load(f)

PRINT_DIR.mkdir(parents=True, exist_ok=True)

# In-memory state
user_jobs: dict[int, dict] = {}
jobs_lock = threading.Lock()

# User interaction state for multi-step flows (email add/remove)
user_state: dict[int, str] = {}

# Tracks bot-submitted jobs: cups_job_id (str) -> color_mode ("Gray" | "Color")
tracked_jobs: dict[str, str] = {}
tracked_jobs_lock = threading.Lock()

# Email thread wake event
email_wake_event = threading.Event()


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
    """Ensure all tables exist."""
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


def get_ink_counters() -> tuple[int, int]:
    """Returns (bw_pages, color_pages)."""
    row = db_query("SELECT bw_pages, color_pages FROM ink_counters WHERE id = 1", fetch_one=True)
    return row if row else (0, 0)


def get_log_offset() -> int:
    row = db_query("SELECT log_offset FROM ink_counters WHERE id = 1", fetch_one=True)
    return row[0] if row else 0


def set_log_offset(offset: int):
    db_query("UPDATE ink_counters SET log_offset = ? WHERE id = 1", (offset,), commit=True)


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
    """Reset all counters to zero (keeps log_offset to avoid re-counting)."""
    db_query(
        "UPDATE ink_counters SET bw_pages = 0, color_pages = 0, last_alert_bw = 0, last_alert_color = 0 WHERE id = 1",
        commit=True
    )


# --- Email config helpers ---

def get_email_config() -> tuple[str, str, int]:
    """Returns (address, encrypted_password, timer_minutes)."""
    row = db_query("SELECT address, encrypted_password, timer_minutes FROM email_config WHERE id = 1", fetch_one=True)
    return row if row else ("", "", 0)


def set_email_config(field: str, value):
    db_query(f"UPDATE email_config SET {field} = ? WHERE id = 1", (value,), commit=True)


def encrypt_password(plain: str) -> str:
    f = Fernet(ENCRYPTION_KEY)
    return f.encrypt(plain.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    f = Fernet(ENCRYPTION_KEY)
    return f.decrypt(encrypted.encode()).decode()


def get_user_emails(user_id: int) -> list[str]:
    rows = db_query("SELECT email FROM user_emails WHERE user_id = ?", (user_id,))
    return [r[0] for r in rows]


def get_all_user_emails() -> list[tuple[int, str]]:
    """Returns list of (user_id, email)."""
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
    """Returns user_id for an email address, or None."""
    row = db_query("SELECT user_id FROM user_emails WHERE email = ?", (addr.lower(),), fetch_one=True)
    return row[0] if row else None


# --- Page log watcher (background thread) ---

def page_log_watcher():
    """Tails CUPS page_log and counts all printed pages (bot + network jobs)."""
    log.info("Page log watcher started, monitoring %s", PAGE_LOG_PATH)

    while True:
        try:
            if not os.path.exists(PAGE_LOG_PATH):
                time.sleep(LOG_POLL_SECONDS)
                continue

            offset = get_log_offset()
            file_size = os.path.getsize(PAGE_LOG_PATH)

            # Log was rotated or truncated
            if file_size < offset:
                offset = 0

            if file_size == offset:
                time.sleep(LOG_POLL_SECONDS)
                continue

            with open(PAGE_LOG_PATH, "r") as f:
                f.seek(offset)
                new_lines = f.readlines()
                new_offset = f.tell()

            bw_total = 0
            color_total = 0

            for line in new_lines:
                parsed = parse_page_log_line(line)
                if not parsed:
                    continue

                job_id, num_copies = parsed

                # Determine color mode: check tracked bot jobs, default to color for network prints
                with tracked_jobs_lock:
                    color_mode = tracked_jobs.pop(job_id, None)

                if color_mode is None:
                    # Unknown job (network print) — assume color (conservative)
                    color_total += num_copies
                elif color_mode == "Gray":
                    bw_total += num_copies
                else:
                    color_total += num_copies

            if bw_total > 0 or color_total > 0:
                add_pages(bw=bw_total, color=color_total)
                log.info("Page log: +%d BW, +%d Color pages", bw_total, color_total)

            set_log_offset(new_offset)

        except Exception as e:
            log.error("Page log watcher error: %s", e)

        time.sleep(LOG_POLL_SECONDS)


def parse_page_log_line(line: str) -> tuple[str, int] | None:
    """Parse a CUPS page_log line. Returns (job_id, num_copies) or None.
    Format: printer user job-id date-time page num-copies ..."""
    parts = line.split()
    if len(parts) < 6:
        return None
    if parts[0] != PRINTER_NAME:
        return None
    try:
        job_id = parts[2]
        num_copies = int(parts[5])
        return (job_id, num_copies)
    except (IndexError, ValueError):
        return None


# --- Printer status watcher (proactive admin notification) ---

PRINTER_CHECK_INTERVAL = 30  # seconds between printer status checks
HP_USB_ID = os.getenv("HP_USB_ID", "03f0")  # HP vendor ID for lsusb detection
_printer_was_ok = True  # tracks last known state to avoid repeated alerts


def is_printer_usb_connected() -> bool:
    """Check if the printer's USB device is visible to the system."""
    try:
        result = subprocess.run(["lsusb"], capture_output=True, text=True)
        return HP_USB_ID in result.stdout.lower()
    except Exception:
        return True  # assume connected if lsusb fails


def printer_status_watcher():
    """Periodically checks printer status and notifies admin on error/disabled."""
    global _printer_was_ok
    log.info("Printer status watcher started (every %ds)", PRINTER_CHECK_INTERVAL)
    time.sleep(10)  # initial delay to let bot start up

    while True:
        try:
            result = subprocess.run(["lpstat", "-p", PRINTER_NAME], capture_output=True, text=True)
            output = result.stdout.lower() if result.stdout else ""

            if "disabled" in output or "stopped" in output:
                if _printer_was_ok:
                    _printer_was_ok = False
                    usb_connected = is_printer_usb_connected()
                    reason = result.stdout.strip()
                    if usb_connected:
                        msg_key = "printer_alert"
                    else:
                        msg_key = "printer_alert_hw_off"
                    try:
                        bot.send_message(
                            ADMIN_ID,
                            MSGS[msg_key].format(status=reason),
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        log.error("Failed to send printer alert: %s", e)
            else:
                _printer_was_ok = True

        except Exception as e:
            log.error("Printer status watcher error: %s", e)

        time.sleep(PRINTER_CHECK_INTERVAL)


# --- Email processing thread ---

def parse_email_body(body: str) -> tuple[int, str]:
    """Parse 'copias: N, modo: color|bn'. Returns (copies, color_mode)."""
    copies = 1
    color_mode = "Gray"
    if not body:
        return copies, color_mode
    body_lower = body.lower().strip()
    m = re.search(r'copias:\s*(\d+)', body_lower)
    if m:
        copies = max(1, int(m.group(1)))
    m = re.search(r'modo:\s*(color|bn)', body_lower)
    if m:
        color_mode = "Color" if m.group(1) == "color" else "Gray"
    return copies, color_mode


def get_email_text_body(msg) -> str:
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def process_email_attachments(msg, sender_email: str):
    """Download printable attachments and queue them for printing."""
    user_id = find_user_by_email(sender_email)
    if user_id is None:
        user_id = UNASSIGNED_USER_ID

    # Determine priority from user role
    if user_id == UNASSIGNED_USER_ID:
        priority = "3"
    else:
        role = get_user_role(user_id)
        priority = "5" if role == "VIP" else "3"

    body = get_email_text_body(msg)
    copies, color_mode = parse_email_body(body)

    printed_files = []
    for part in msg.walk():
        disposition = str(part.get("Content-Disposition") or "")
        if "attachment" not in disposition:
            continue
        filename = part.get_filename()
        if not filename:
            continue
        # Decode encoded filenames
        decoded_parts = decode_header(filename)
        filename = "".join(
            t[0].decode(t[1] or "utf-8") if isinstance(t[0], bytes) else t[0]
            for t in decoded_parts
        )
        # Only print PDFs and images
        ext = Path(filename).suffix.lower()
        if ext not in (".pdf", ".jpg", ".jpeg", ".png"):
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            continue

        local_path = str(PRINT_DIR / f"email_{int(time.time())}_{filename}")
        with open(local_path, "wb") as f:
            f.write(payload)

        print_path = convert_image_to_pdf(local_path, grayscale=(color_mode == "Gray")) or local_path
        cmd = [
            "lp", "-d", PRINTER_NAME,
            "-n", str(copies),
            "-o", f"job-priority={priority}",
            "-o", f"ColorModel={color_mode}",
            print_path,
        ]
        log.info("Email print: %s from %s", filename, sender_email)
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            printed_files.append(filename)
            match = re.search(r"request id is \S+-(\d+)", result.stdout)
            if match:
                with tracked_jobs_lock:
                    tracked_jobs[match.group(1)] = color_mode
        else:
            log.error("Email print failed for %s: %s", filename, result.stderr)

        try:
            os.remove(local_path)
        except OSError:
            pass
        if print_path != local_path:
            try:
                os.remove(print_path)
            except OSError:
                pass

    # Notify associated user via Telegram
    if printed_files and user_id != UNASSIGNED_USER_ID:
        try:
            file_list = ", ".join(printed_files)
            bot.send_message(user_id, MSGS["email_print_notify"].format(
                files=file_list, copies=copies, mode="Color" if color_mode == "Color" else "B&N"
            ), parse_mode="Markdown")
        except Exception as e:
            log.error("Failed to notify user %d about email print: %s", user_id, e)


def email_check_loop():
    """Background thread: connect to IMAP, fetch unseen emails with attachments, print them."""
    log.info("Email processing thread started")

    while True:
        email_wake_event.clear()
        address, encrypted_pw, timer = get_email_config()

        if not address or not encrypted_pw or timer <= 0:
            # Disabled — wait indefinitely until woken by config change
            log.info("Email processing disabled, waiting for config...")
            email_wake_event.wait()
            continue

        try:
            password = decrypt_password(encrypted_pw)
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(address, password)
            mail.select("inbox")

            _, data = mail.search(None, "UNSEEN")
            mail_ids = data[0].split()

            for mid in mail_ids:
                _, msg_data = mail.fetch(mid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)

                sender = email_lib.utils.parseaddr(msg.get("From", ""))[1].lower()
                # Only process emails from whitelisted addresses
                if find_user_by_email(sender) is not None or sender == address:
                    process_email_attachments(msg, sender)
                else:
                    log.info("Email from non-whitelisted sender %s, skipping", sender)

            mail.logout()
        except Exception as e:
            log.error("Email check error: %s", e)

        # Sleep until timer expires or config changes
        email_wake_event.wait(timeout=timer * 60)


# --- Command cleanup on startup ---

def clear_all_commands():
    """Remove any previously registered command menus (default + per-user scopes)."""
    try:
        bot.delete_my_commands()
    except Exception as e:
        log.error("Failed to clear default commands: %s", e)

    rows = db_query("SELECT id FROM users")
    for (uid,) in rows:
        try:
            bot.delete_my_commands(scope=types.BotCommandScopeChat(uid))
        except Exception:
            pass


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


def convert_image_to_pdf(file_path: str, grayscale: bool = False) -> str | None:
    """Convert image to PDF for reliable printing. Returns PDF path or None if not an image."""
    ext = Path(file_path).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png"):
        return None
    src = file_path
    if grayscale:
        src = file_path + ".gray.jpg"
        Image.open(file_path).convert("L").save(src)
    pdf_path = file_path + ".pdf"
    with open(pdf_path, "wb") as f:
        f.write(img2pdf.convert(src))
    if grayscale:
        os.remove(src)
    return pdf_path


# --- Print execution (runs in dedicated thread) ---

JOB_POLL_INTERVAL = 2  # seconds between lpstat checks
JOB_POLL_TIMEOUT = 120  # max seconds to wait for a job to complete
JOB_STALL_THRESHOLD = 20  # seconds before checking for stall/paper issues


# Tracks jobs canceled by admin wipe — poll_job_completion checks this
_wiped_jobs: set[str] = set()
_wiped_jobs_lock = threading.Lock()


def extract_printer_reason(lpstat_output: str) -> str:
    """Extract the error reason from lpstat -p output (text after the last dash)."""
    for line in lpstat_output.strip().splitlines():
        if "disabled" in line.lower():
            # Format: "printer HP-2515 disabled since <date> -\n\treason" or "... - reason"
            idx = line.rfind(" - ")
            if idx != -1:
                return line[idx + 3:].strip()
    # Check indented continuation line
    lines = lpstat_output.strip().splitlines()
    if len(lines) > 1:
        return lines[-1].strip()
    return ""


def poll_job_completion(job_id: str) -> tuple[str, str]:
    """Poll lpstat until job completes. Returns (status, reason).
    status: 'completed', 'error', 'canceled', or 'timeout'. reason: printer error detail or empty."""
    full_id = f"{PRINTER_NAME}-{job_id}"
    elapsed = 0
    while elapsed < JOB_POLL_TIMEOUT:
        time.sleep(JOB_POLL_INTERVAL)
        elapsed += JOB_POLL_INTERVAL

        # Check if job was wiped by admin
        with _wiped_jobs_lock:
            if job_id in _wiped_jobs:
                _wiped_jobs.discard(job_id)
                return ("canceled", "")

        # Check printer state (detects errors even while job is still queued)
        printer_status = subprocess.run(["lpstat", "-p", PRINTER_NAME], capture_output=True, text=True)
        if "disabled" in printer_status.stdout.lower() or "stopped" in printer_status.stdout.lower():
            reason = extract_printer_reason(printer_status.stdout)
            return ("error", reason)

        # Check if job is still in active queue
        result = subprocess.run(["lpstat", "-o", PRINTER_NAME], capture_output=True, text=True)
        if full_id not in result.stdout:
            return ("completed", "")

        # After stall threshold, check for extended state messages (paper out, etc.)
        # With error-policy=retry-current-job, CUPS retries without disabling
        if elapsed >= JOB_STALL_THRESHOLD:
            stall_reason = detect_printer_stall()
            if stall_reason:
                return ("error", stall_reason)

    return ("timeout", "")


def detect_printer_stall() -> str | None:
    """Check lpstat -l for extended state messages indicating paper/hardware issues.
    Returns reason string or None if printer seems fine."""
    result = subprocess.run(["lpstat", "-l", "-p", PRINTER_NAME], capture_output=True, text=True)
    if not result.stdout:
        return None
    lines = result.stdout.strip().splitlines()
    # Extended state messages are indented lines after the printer status line
    state_messages = [l.strip() for l in lines[1:] if l.startswith(("\t", "    ")) and l.strip()]
    if state_messages:
        return "; ".join(state_messages)
    return None


def get_printer_status() -> str:
    """Get formatted printer status for the admin monitor."""
    # USB connectivity (check first — lpstat can be stale)
    usb_connected = is_printer_usb_connected()

    # Printer state
    result = subprocess.run(["lpstat", "-p", PRINTER_NAME], capture_output=True, text=True)
    printer_line = result.stdout.strip() if result.stdout else "No se pudo obtener estado"

    # Determine status emoji — USB takes priority
    if not usb_connected:
        status_emoji = "⚫"
        printer_line = "Impresora apagada (USB no detectado)"
    elif "idle" in printer_line.lower():
        status_emoji = "🟢"
    elif "printing" in printer_line.lower():
        status_emoji = "🔵"
    elif "disabled" in printer_line.lower():
        status_emoji = "🔴"
    else:
        status_emoji = "⚪"

    # Active jobs in queue
    active = subprocess.run(["lpstat", "-o", PRINTER_NAME], capture_output=True, text=True)
    active_jobs = [l.strip() for l in active.stdout.strip().splitlines() if l.strip()] if active.stdout else []

    # Recent completed jobs (last 5)
    completed = subprocess.run(["lpstat", "-W", "completed", "-o", PRINTER_NAME], capture_output=True, text=True)
    completed_jobs = [l.strip() for l in completed.stdout.strip().splitlines() if l.strip()] if completed.stdout else []
    recent = completed_jobs[-5:] if completed_jobs else []

    lines = [f"{status_emoji} *Estado:* `{printer_line}`"]
    lines.append(f"🔌 *USB:* {'conectada' if usb_connected else '⚠️ NO DETECTADA — impresora apagada?'}")
    lines.append("")

    if active_jobs:
        lines.append(f"📋 *Cola activa ({len(active_jobs)}):*")
        for j in active_jobs[:5]:
            lines.append(f"  `{j}`")
    else:
        lines.append("📋 *Cola activa:* vacía")

    lines.append("")
    if recent:
        lines.append(f"✅ *Últimos trabajos ({len(completed_jobs)} total):*")
        for j in recent:
            lines.append(f"  `{j}`")
    else:
        lines.append("✅ *Últimos trabajos:* ninguno")

    return "\n".join(lines)


def reactivate_printer() -> str:
    """Re-enable printer queue without canceling pending jobs. Returns status message."""
    # Re-enable the printer
    enable_result = subprocess.run(["cupsenable", PRINTER_NAME], capture_output=True, text=True)
    # Accept new jobs
    subprocess.run(["cupsaccept", PRINTER_NAME], capture_output=True, text=True)

    if enable_result.returncode == 0:
        return "ok"
    return enable_result.stderr.strip() or "Error desconocido"


def execute_print_job(chat_id: int, job: dict):
    """Executes lp command in a background thread, polls for completion."""
    # Check if printer is physically connected before attempting
    if not is_printer_usb_connected():
        bot.send_message(chat_id, MSGS["printer_hw_off_user"], parse_mode="Markdown")
        try:
            os.remove(job["file_path"])
        except OSError:
            pass
        return

    print_path = convert_image_to_pdf(job["file_path"], grayscale=(job["color"] == "Gray")) or job["file_path"]
    cmd = [
        "lp", "-d", PRINTER_NAME,
        "-n", str(job["copies"]),
        "-o", f"job-priority={job['priority']}",
        "-o", f"ColorModel={job['color']}",
        print_path,
    ]
    log.info("Printing for user %d: %s", chat_id, " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        log.error("lp failed for user %d: %s", chat_id, result.stderr)
        bot.send_message(chat_id, MSGS["print_error"])
        if print_path != job["file_path"]:
            try:
                os.remove(print_path)
            except OSError:
                pass
        return

    # Register job for ink tracking
    match = re.search(r"request id is \S+-(\d+)", result.stdout)
    job_id = match.group(1) if match else None
    if job_id:
        with tracked_jobs_lock:
            tracked_jobs[job_id] = job["color"]
        log.info("Registered job %s as %s", job_id, job["color"])

    bot.send_message(chat_id, MSGS["print_queued"].format(file=job["file_name"]), parse_mode="Markdown")

    # Poll for actual completion
    if job_id:
        status, reason = poll_job_completion(job_id)
        if status == "completed":
            # Increment page counter directly (page_log is deprecated in CUPS 2.x)
            copies = job["copies"]
            if job["color"] == "Gray":
                add_pages(bw=copies, color=0)
            else:
                add_pages(bw=0, color=copies)
            log.info("Job %s completed: +%d %s pages", job_id, copies, job["color"])
            bot.send_message(chat_id, MSGS["print_success"].format(file=job["file_name"]), parse_mode="Markdown")
            bot.send_message(chat_id, MSGS["menu_prompt"],
                             reply_markup=build_single_menu_button(), parse_mode="Markdown")
        elif status == "canceled":
            bot.send_message(chat_id, MSGS["print_canceled"].format(file=job["file_name"]), parse_mode="Markdown")
            bot.send_message(chat_id, MSGS["menu_prompt"],
                             reply_markup=build_single_menu_button(), parse_mode="Markdown")
        elif status == "error":
            if reason:
                msg = MSGS["print_failed_reason"].format(file=job["file_name"], reason=reason)
            else:
                msg = MSGS["print_failed"].format(file=job["file_name"])
            bot.send_message(chat_id, msg, parse_mode="Markdown")
            bot.send_message(chat_id, MSGS["menu_prompt"],
                             reply_markup=build_single_menu_button(), parse_mode="Markdown")
        else:
            bot.send_message(chat_id, MSGS["print_timeout"].format(file=job["file_name"]), parse_mode="Markdown")
            bot.send_message(chat_id, MSGS["menu_prompt"],
                             reply_markup=build_single_menu_button(), parse_mode="Markdown")

    # Cleanup converted PDF if any
    if print_path != job["file_path"]:
        try:
            os.remove(print_path)
        except OSError:
            pass


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
    markup.add(types.InlineKeyboardButton(MSGS["btn_back"], callback_data="menu_back"))
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


def build_main_menu(chat_id: int) -> types.InlineKeyboardMarkup:
    """Build role-appropriate main menu inline keyboard."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(MSGS["menu_nickname"], callback_data="menu_apodo"),
        types.InlineKeyboardButton(MSGS["menu_emails"], callback_data="menu_emails_sub"),
    )
    markup.add(
        types.InlineKeyboardButton(MSGS["menu_help"], callback_data="menu_ayuda"),
    )
    if chat_id == ADMIN_ID:
        markup.add(
            types.InlineKeyboardButton(MSGS["menu_users"], callback_data="menu_users_sub"),
            types.InlineKeyboardButton(MSGS["menu_ink"], callback_data="menu_monitor_sub"),
        )
    return markup


def build_single_menu_button() -> types.InlineKeyboardMarkup:
    """Single 'Menú' button that transforms the message into the full menu when clicked."""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(MSGS["btn_menu"], callback_data="menu_back"))
    return markup


def build_users_sub_menu() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(MSGS["sub_users_list"], callback_data="usub_list"),
        types.InlineKeyboardButton(MSGS["sub_users_remove"], callback_data="usub_remove"),
    )
    markup.add(
        types.InlineKeyboardButton(MSGS["sub_users_role"], callback_data="usub_role"),
    )
    markup.add(types.InlineKeyboardButton(MSGS["btn_back"], callback_data="menu_back"))
    return markup


def build_emails_sub_menu(chat_id: int) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(MSGS["sub_emails_list"], callback_data="esub_list"),
        types.InlineKeyboardButton(MSGS["sub_emails_add"], callback_data="esub_add"),
    )
    markup.add(
        types.InlineKeyboardButton(MSGS["sub_emails_remove"], callback_data="esub_remove"),
    )
    if chat_id == ADMIN_ID:
        markup.add(
            types.InlineKeyboardButton(MSGS["sub_emails_all"], callback_data="esub_all"),
            types.InlineKeyboardButton(MSGS["sub_emails_config"], callback_data="esub_config"),
        )
    markup.add(types.InlineKeyboardButton(MSGS["btn_back"], callback_data="menu_back"))
    return markup


def build_email_config_sub_menu() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(MSGS["sub_ecfg_view"], callback_data="ecfg_view"),
        types.InlineKeyboardButton(MSGS["sub_ecfg_address"], callback_data="ecfg_address"),
    )
    markup.add(
        types.InlineKeyboardButton(MSGS["sub_ecfg_password"], callback_data="ecfg_password"),
        types.InlineKeyboardButton(MSGS["sub_ecfg_timer"], callback_data="ecfg_timer"),
    )
    markup.add(types.InlineKeyboardButton(MSGS["btn_back"], callback_data="menu_emails_sub"))
    return markup


def build_monitor_sub_menu() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(MSGS["sub_monitor_printer"], callback_data="tsub_monitor"),
        types.InlineKeyboardButton(MSGS["sub_monitor_reactivar"], callback_data="tsub_reactivar"),
    )
    markup.add(
        types.InlineKeyboardButton(MSGS["sub_monitor_wipe_queue"], callback_data="tsub_wipe"),
        types.InlineKeyboardButton(MSGS["sub_monitor_testpage"], callback_data="tsub_testpage"),
    )
    markup.add(
        types.InlineKeyboardButton(MSGS["sub_monitor_status"], callback_data="tsub_status"),
    )
    markup.add(types.InlineKeyboardButton(MSGS["btn_back"], callback_data="menu_back"))
    return markup


def build_monitor_back_button() -> types.InlineKeyboardMarkup:
    """Single back button returning to the Monitorear sub-menu."""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(MSGS["btn_back"], callback_data="menu_monitor_sub"))
    return markup


def build_user_select_buttons(action_prefix: str) -> types.InlineKeyboardMarkup:
    """Build inline buttons listing users for selection (remove/role change)."""
    rows = db_query("SELECT id, real_name, nickname, role FROM users")
    markup = types.InlineKeyboardMarkup(row_width=1)
    for uid, name, nick, role in rows:
        if uid == ADMIN_ID:
            continue  # Can't remove/change admin
        display = f"{name} ({nick})" if nick else name
        label = f"{display} [{role}]"
        markup.add(types.InlineKeyboardButton(label, callback_data=f"{action_prefix}_{uid}"))
    markup.add(types.InlineKeyboardButton(MSGS["btn_back"], callback_data="menu_users_sub"))
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
    bot.reply_to(message, MSGS["welcome"].format(name=tg_name), reply_markup=build_main_menu(chat_id))


@bot.message_handler(commands=['menu'])
def cmd_menu(message):
    chat_id = message.chat.id
    if not get_user_role(chat_id):
        return
    bot.send_message(chat_id, MSGS["menu_title"], reply_markup=build_main_menu(chat_id), parse_mode="Markdown")




# --- Text message handlers ---

@bot.message_handler(func=lambda m: m.text and "hola" in m.text.lower(), content_types=['text'])
def handle_greeting(message):
    if not get_user_role(message.chat.id):
        return

    nickname, real_name, _ = get_user_profile(message.chat.id)
    display_name = nickname or real_name or message.from_user.first_name
    bot.reply_to(message, MSGS["greeting"].format(name=display_name),
                 reply_markup=build_single_menu_button(), parse_mode="Markdown")


@bot.message_handler(func=lambda m: m.text and not m.text.startswith('/'), content_types=['text'])
def handle_text(message):
    if not get_user_role(message.chat.id):
        return

    chat_id = message.chat.id
    _, _, waiting = get_user_profile(chat_id)

    if waiting == 1:
        new_nickname = message.text.strip()
        if len(new_nickname) > 20:
            bot.reply_to(message, MSGS["nickname_too_long"])
            return
        set_nickname_state(chat_id, nickname=new_nickname, waiting=0)
        bot.reply_to(message, MSGS["nickname_saved"].format(nickname=new_nickname),
                     reply_markup=build_main_menu(chat_id), parse_mode="Markdown")
        return

    # Email add/remove waiting states
    state = user_state.pop(chat_id, None)
    if state == "waiting_add_email":
        addr = message.text.strip().lower()
        # Admin can do: email user_id
        target_id = chat_id
        parts = addr.split()
        if chat_id == ADMIN_ID and len(parts) == 2:
            addr = parts[0]
            try:
                target_id = int(parts[1])
            except ValueError:
                bot.reply_to(message, MSGS["email_add_invalid_id"])
                return
        if add_user_email(target_id, addr):
            bot.reply_to(message, MSGS["email_added"].format(email=addr),
                         reply_markup=build_emails_sub_menu(chat_id), parse_mode="Markdown")
        else:
            bot.reply_to(message, MSGS["email_already_exists"].format(email=addr),
                         reply_markup=build_emails_sub_menu(chat_id), parse_mode="Markdown")
        return

    if state == "waiting_remove_email":
        addr = message.text.strip().lower()
        if chat_id == ADMIN_ID:
            # Admin can remove any email
            conn = sqlite3.connect(DB_PATH)
            try:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM user_emails WHERE email = ?", (addr,))
                conn.commit()
                removed = cursor.rowcount > 0
            finally:
                conn.close()
        else:
            removed = remove_user_email(chat_id, addr)

        if removed:
            bot.reply_to(message, MSGS["email_removed"].format(email=addr),
                         reply_markup=build_emails_sub_menu(chat_id), parse_mode="Markdown")
        else:
            bot.reply_to(message, MSGS["email_not_found"].format(email=addr),
                         reply_markup=build_emails_sub_menu(chat_id), parse_mode="Markdown")
        return

    if state == "waiting_ecfg_address":
        val = message.text.strip()
        set_email_config("address", val)
        email_wake_event.set()
        bot.reply_to(message, MSGS["email_config_updated"].format(field="dirección", value=val),
                     reply_markup=build_email_config_sub_menu(), parse_mode="Markdown")
        return

    if state == "waiting_ecfg_password":
        val = message.text.strip()
        encrypted = encrypt_password(val)
        set_email_config("encrypted_password", encrypted)
        email_wake_event.set()
        bot.reply_to(message, MSGS["email_config_updated"].format(field="contraseña", value="••••••••"),
                     reply_markup=build_email_config_sub_menu(), parse_mode="Markdown")
        return

    if state == "waiting_ecfg_timer":
        try:
            val = int(message.text.strip())
        except ValueError:
            bot.reply_to(message, "⚠️ Ingresá un número válido.")
            return
        set_email_config("timer_minutes", val)
        email_wake_event.set()
        bot.reply_to(message, MSGS["email_config_updated"].format(
            field="timer", value=f"{val} min" if val > 0 else "desactivado"),
                     reply_markup=build_email_config_sub_menu(), parse_mode="Markdown")
        return

    bot.reply_to(message, MSGS["unknown_command"],
                 reply_markup=build_single_menu_button(), parse_mode="Markdown")


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
        bot.send_message(target_id, MSGS["user_auth_granted"].format(admin=call.from_user.first_name),
                         reply_markup=build_main_menu(target_id), parse_mode="Markdown")
        return

    # --- Nickname callbacks ---
    if call.data == "nickname_change":
        set_nickname_state(chat_id, waiting=1)
        bot.edit_message_text(MSGS["nickname_prompt"], chat_id, msg_id)
        bot.answer_callback_query(call.id)
        return

    if call.data == "nickname_delete":
        set_nickname_state(chat_id, nickname="", waiting=0)
        bot.edit_message_text(MSGS["nickname_deleted"], chat_id, msg_id, reply_markup=build_main_menu(chat_id))
        bot.answer_callback_query(call.id)
        return

    # --- Main menu callbacks ---
    if call.data == "menu_back":
        bot.answer_callback_query(call.id)
        bot.edit_message_text(MSGS["menu_title"], chat_id, msg_id,
                             reply_markup=build_main_menu(chat_id), parse_mode="Markdown")
        return

    if call.data == "menu_apodo":
        bot.answer_callback_query(call.id)
        nickname, _, _ = get_user_profile(chat_id)
        if nickname:
            text = MSGS["nickname_current"].format(nickname=nickname)
            markup = build_nickname_menu(has_nickname=True)
        else:
            text = MSGS["nickname_empty"]
            markup = build_nickname_menu(has_nickname=False)
        bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode="Markdown")
        return

    if call.data == "menu_ayuda":
        bot.answer_callback_query(call.id)
        help_text = MSGS["help_admin"] if chat_id == ADMIN_ID else MSGS["help"]
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(MSGS["btn_back"], callback_data="menu_back"))
        bot.edit_message_text(help_text, chat_id, msg_id, reply_markup=markup, parse_mode="Markdown")
        return

    # --- Users sub-menu ---
    if call.data == "menu_users_sub":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        bot.edit_message_text(MSGS["sub_users_title"], chat_id, msg_id,
                             reply_markup=build_users_sub_menu(), parse_mode="Markdown")
        return

    if call.data == "usub_list":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        rows = db_query("SELECT id, real_name, nickname, role FROM users")
        response = MSGS["user_list_title"]
        for uid, name, nick, role in rows:
            nick_display = f" ({nick})" if nick else ""
            response += f"• {name}{nick_display} — `{role}`\n"
        bot.edit_message_text(response, chat_id, msg_id,
                             reply_markup=build_users_sub_menu(), parse_mode="Markdown")
        return

    if call.data == "usub_remove":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        bot.edit_message_text(MSGS["sub_users_select_remove"], chat_id, msg_id,
                             reply_markup=build_user_select_buttons("rmuser"))
        return

    if call.data == "usub_role":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        bot.edit_message_text(MSGS["sub_users_select_role"], chat_id, msg_id,
                             reply_markup=build_user_select_buttons("chrole"))
        return

    if call.data.startswith("rmuser_"):
        if chat_id != ADMIN_ID:
            return
        target_id = int(call.data.split("_")[1])
        row = db_query("SELECT real_name FROM users WHERE id = ?", (target_id,), fetch_one=True)
        name = row[0] if row else str(target_id)
        db_query("DELETE FROM users WHERE id = ?", (target_id,), commit=True)
        bot.answer_callback_query(call.id)
        bot.edit_message_text(MSGS["user_removed"].format(name=name), chat_id, msg_id,
                             reply_markup=build_users_sub_menu())
        return

    if call.data.startswith("chrole_"):
        if chat_id != ADMIN_ID:
            return
        target_id = int(call.data.split("_")[1])
        row = db_query("SELECT real_name, role FROM users WHERE id = ?", (target_id,), fetch_one=True)
        if row:
            name, current_role = row
            new_role = "VIP" if current_role == "USER" else "USER"
            db_query("UPDATE users SET role = ? WHERE id = ?", (new_role, target_id), commit=True)
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                MSGS["user_role_changed"].format(name=name, role=new_role), chat_id, msg_id,
                reply_markup=build_users_sub_menu()
            )
        return

    # --- Emails sub-menu ---
    if call.data == "menu_emails_sub":
        bot.answer_callback_query(call.id)
        bot.edit_message_text(MSGS["sub_emails_title"], chat_id, msg_id,
                             reply_markup=build_emails_sub_menu(chat_id), parse_mode="Markdown")
        return

    if call.data == "esub_list":
        bot.answer_callback_query(call.id)
        emails = get_user_emails(chat_id)
        if emails:
            listing = "\n".join(f"• `{e}`" for e in emails)
            text = MSGS["email_list"].format(emails=listing)
        else:
            text = MSGS["email_list_empty"]
        bot.edit_message_text(text, chat_id, msg_id,
                             reply_markup=build_emails_sub_menu(chat_id), parse_mode="Markdown")
        return

    if call.data == "esub_add":
        bot.answer_callback_query(call.id)
        user_state[chat_id] = "waiting_add_email"
        bot.edit_message_text(MSGS["email_prompt_add"], chat_id, msg_id)
        return

    if call.data == "esub_remove":
        bot.answer_callback_query(call.id)
        user_state[chat_id] = "waiting_remove_email"
        bot.edit_message_text(MSGS["email_prompt_remove"], chat_id, msg_id)
        return

    if call.data == "esub_all":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        all_emails = get_all_user_emails()
        if not all_emails:
            text = MSGS["email_admin_empty"]
        else:
            lines = []
            for uid, addr in all_emails:
                if uid == UNASSIGNED_USER_ID:
                    lines.append(f"• `{addr}` → _(sin asignar)_")
                else:
                    row = db_query("SELECT real_name FROM users WHERE id = ?", (uid,), fetch_one=True)
                    name = row[0] if row else str(uid)
                    lines.append(f"• `{addr}` → {name}")
            text = MSGS["email_admin_list"].format(emails="\n".join(lines))
        bot.edit_message_text(text, chat_id, msg_id,
                             reply_markup=build_emails_sub_menu(chat_id), parse_mode="Markdown")
        return

    if call.data == "esub_config":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        bot.edit_message_text(MSGS["sub_ecfg_title"], chat_id, msg_id,
                             reply_markup=build_email_config_sub_menu(), parse_mode="Markdown")
        return

    # --- Email config sub-menu ---
    if call.data == "ecfg_view":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        address, _, timer = get_email_config()
        status = "✅ Activo" if address and timer > 0 else "❌ Inactivo"
        bot.edit_message_text(
            MSGS["email_config_show"].format(status=status, address=address or "(no configurado)", timer=timer),
            chat_id, msg_id, reply_markup=build_email_config_sub_menu(), parse_mode="Markdown"
        )
        return

    if call.data == "ecfg_address":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        user_state[chat_id] = "waiting_ecfg_address"
        bot.edit_message_text(MSGS["ecfg_prompt_address"], chat_id, msg_id)
        return

    if call.data == "ecfg_password":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        user_state[chat_id] = "waiting_ecfg_password"
        bot.edit_message_text(MSGS["ecfg_prompt_password"], chat_id, msg_id)
        return

    if call.data == "ecfg_timer":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        user_state[chat_id] = "waiting_ecfg_timer"
        bot.edit_message_text(MSGS["ecfg_prompt_timer"], chat_id, msg_id)
        return

    # --- Monitor sub-menu ---
    if call.data == "menu_monitor_sub":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        bot.edit_message_text(MSGS["sub_monitor_title"], chat_id, msg_id,
                             reply_markup=build_monitor_sub_menu(), parse_mode="Markdown")
        return

    if call.data == "tsub_status":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        bw, color = get_ink_counters()
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(MSGS["sub_monitor_reset"], callback_data="tsub_reset"))
        markup.add(types.InlineKeyboardButton(MSGS["btn_back"], callback_data="menu_monitor_sub"))
        bot.edit_message_text(
            MSGS["ink_status"].format(bw=bw, bw_limit=BW_PAGE_LIMIT, color=color, color_limit=COLOR_PAGE_LIMIT),
            chat_id, msg_id, reply_markup=markup, parse_mode="Markdown"
        )
        return

    if call.data == "tsub_reset":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        reset_ink_counters()
        bot.edit_message_text(MSGS["ink_reset_success"], chat_id, msg_id,
                             reply_markup=build_monitor_back_button())
        return

    if call.data == "tsub_testpage":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        result = subprocess.run(
            ["lp", "-d", PRINTER_NAME, "/usr/share/cups/data/testprint"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            bot.edit_message_text(MSGS["testpage_sent"], chat_id, msg_id,
                                 reply_markup=build_monitor_back_button())
        else:
            log.error("Test page failed: %s", result.stderr)
            bot.edit_message_text(MSGS["testpage_error"], chat_id, msg_id,
                                 reply_markup=build_monitor_back_button())
        return

    if call.data == "tsub_monitor":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        status_text = get_printer_status()
        bot.edit_message_text(status_text, chat_id, msg_id,
                             reply_markup=build_monitor_back_button(), parse_mode="Markdown")
        return

    if call.data == "tsub_reactivar":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        result = reactivate_printer()
        if result == "ok":
            bot.edit_message_text(MSGS["printer_reactivated"], chat_id, msg_id,
                                 reply_markup=build_monitor_back_button())
        else:
            bot.edit_message_text(
                MSGS["printer_reactivate_error"].format(error=result),
                chat_id, msg_id, reply_markup=build_monitor_back_button()
            )
        return

    if call.data == "tsub_wipe":
        if chat_id != ADMIN_ID:
            return
        bot.answer_callback_query(call.id)
        # Capture current job IDs before wiping so poll_job_completion can detect cancellation
        queued = subprocess.run(["lpstat", "-o", PRINTER_NAME], capture_output=True, text=True)
        if queued.stdout:
            with _wiped_jobs_lock:
                for line in queued.stdout.strip().splitlines():
                    parts = line.split()
                    if parts:
                        # Job ID format: "HP-2515-123" → extract "123"
                        job_full = parts[0]
                        job_num = job_full.rsplit("-", 1)[-1]
                        _wiped_jobs.add(job_num)
        subprocess.run(["cancel", "-a", PRINTER_NAME], capture_output=True, text=True)
        bot.edit_message_text(MSGS["queue_wiped"], chat_id, msg_id,
                             reply_markup=build_monitor_back_button())
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
    clear_all_commands()

    # Start page log watcher in background
    threading.Thread(target=page_log_watcher, daemon=True).start()

    # Start printer status watcher in background
    threading.Thread(target=printer_status_watcher, daemon=True).start()

    # Start email check loop in background
    threading.Thread(target=email_check_loop, daemon=True).start()

    log.info("Print server bot starting (threads=%d)...", 4)
    bot.infinity_polling()
