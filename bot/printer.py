import os
import re
import subprocess

from PIL import Image
import img2pdf

from bot.config import (
    PRINTER_NAME, HP_USB_ID, PRINT_DIR,
    tracked_jobs, tracked_jobs_lock,
    tgbot, MSGS, log,
)
from bot.db import add_pages


def convert_image_to_pdf(file_path: str, grayscale: bool = False) -> str | None:
    """Convert image to PDF for reliable printing. Returns PDF path or None if not an image."""
    ext = os.path.splitext(file_path)[1].lower()
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


def is_printer_usb_connected() -> bool:
    """Check if the printer's USB device is visible to the system."""
    try:
        result = subprocess.run(["lsusb"], capture_output=True, text=True)
        return HP_USB_ID in result.stdout.lower()
    except Exception:
        return True


def get_printer_status() -> str:
    """Get formatted printer status for the admin monitor."""
    usb_connected = is_printer_usb_connected()

    result = subprocess.run(["lpstat", "-p", PRINTER_NAME], capture_output=True, text=True)
    printer_line = result.stdout.strip() if result.stdout else "No se pudo obtener estado"

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

    active = subprocess.run(["lpstat", "-o", PRINTER_NAME], capture_output=True, text=True)
    active_jobs = [l.strip() for l in active.stdout.strip().splitlines() if l.strip()] if active.stdout else []

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
    """Re-enable printer queue without canceling pending jobs."""
    enable_result = subprocess.run(["cupsenable", PRINTER_NAME], capture_output=True, text=True)
    subprocess.run(["cupsaccept", PRINTER_NAME], capture_output=True, text=True)
    if enable_result.returncode == 0:
        return "ok"
    return enable_result.stderr.strip() or "Error desconocido"


def execute_print_job(chat_id: int, job: dict):
    """Executes lp command in a background thread. Reports queue submission result only."""
    from bot.keyboards import build_single_menu_button

    if not is_printer_usb_connected():
        tgbot.send_message(chat_id, MSGS["printer_hw_off_user"], parse_mode="Markdown")
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

    if print_path != job["file_path"]:
        try:
            os.remove(print_path)
        except OSError:
            pass

    if result.returncode != 0:
        log.error("lp failed for user %d: %s", chat_id, result.stderr)
        tgbot.send_message(chat_id, MSGS["print_error"], parse_mode="Markdown")
        return

    # Track job for ink counting
    match = re.search(r"request id is \S+-(\d+)", result.stdout)
    if match:
        with tracked_jobs_lock:
            tracked_jobs[match.group(1)] = job["color"]

    # Count pages on submission
    copies = job["copies"]
    if job["color"] == "Gray":
        add_pages(bw=copies, color=0)
    else:
        add_pages(bw=0, color=copies)

    tgbot.send_message(chat_id, MSGS["print_sent"].format(file=job["file_name"]), parse_mode="Markdown")
    tgbot.send_message(chat_id, MSGS["menu_prompt"],
                     reply_markup=build_single_menu_button(), parse_mode="Markdown")
