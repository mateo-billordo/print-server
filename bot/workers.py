import os
import re
import time
import imaplib
import email as email_lib
from email.header import decode_header
from pathlib import Path
import subprocess

from bot.config import (
    PRINTER_NAME, PRINTER_CHECK_INTERVAL, PAGE_LOG_PATH,
    LOG_POLL_SECONDS, PRINT_DIR, UNASSIGNED_USER_ID,
    ADMIN_ID, HP_USB_ID,
    tgbot, MSGS, log,
    tracked_jobs, tracked_jobs_lock,
    email_wake_event,
)
from bot.db import (
    get_log_offset, set_log_offset, add_pages,
    get_email_config, decrypt_password, find_user_by_email, get_user_role,
)
from bot.printer import convert_image_to_pdf, is_printer_usb_connected


# --- Printer status watcher state ---
_printer_was_ok = True


def parse_page_log_line(line: str) -> tuple[str, int] | None:
    """Parse a CUPS page_log line. Returns (job_id, num_copies) or None."""
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


def page_log_watcher():
    """Tails CUPS page_log and counts all printed pages."""
    log.info("Page log watcher started, monitoring %s", PAGE_LOG_PATH)

    while True:
        try:
            if not os.path.exists(PAGE_LOG_PATH):
                time.sleep(LOG_POLL_SECONDS)
                continue

            offset = get_log_offset()
            file_size = os.path.getsize(PAGE_LOG_PATH)

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

                with tracked_jobs_lock:
                    color_mode = tracked_jobs.pop(job_id, None)

                if color_mode is None:
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


def printer_status_watcher():
    """Periodically checks printer status and notifies admin on error/disabled."""
    global _printer_was_ok
    log.info("Printer status watcher started (every %ds)", PRINTER_CHECK_INTERVAL)
    time.sleep(10)

    while True:
        try:
            result = subprocess.run(["lpstat", "-p", PRINTER_NAME], capture_output=True, text=True)
            output = result.stdout.lower() if result.stdout else ""

            if "disabled" in output or "stopped" in output:
                if _printer_was_ok:
                    _printer_was_ok = False
                    usb_connected = is_printer_usb_connected()
                    reason = result.stdout.strip()
                    msg_key = "printer_alert" if usb_connected else "printer_alert_hw_off"
                    try:
                        tgbot.send_message(
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
        decoded_parts = decode_header(filename)
        filename = "".join(
            t[0].decode(t[1] or "utf-8") if isinstance(t[0], bytes) else t[0]
            for t in decoded_parts
        )
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

    if printed_files and user_id != UNASSIGNED_USER_ID:
        try:
            file_list = ", ".join(printed_files)
            tgbot.send_message(user_id, MSGS["email_print_notify"].format(
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
                if find_user_by_email(sender) is not None or sender == address:
                    process_email_attachments(msg, sender)
                else:
                    log.info("Email from non-whitelisted sender %s, skipping", sender)

            mail.logout()
        except Exception as e:
            log.error("Email check error: %s", e)

        email_wake_event.wait(timeout=timer * 60)
