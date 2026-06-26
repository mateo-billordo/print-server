import os
import json
import logging
import threading
from pathlib import Path
from dotenv import load_dotenv
from telebot import TeleBot

load_dotenv()

# --- Telegram ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip().strip("'\""))

# --- Printer ---
PRINTER_NAME = os.getenv("PRINTER_NAME", "HP-2515")
HP_USB_ID = os.getenv("HP_USB_ID", "03f0")

# --- Encryption ---
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").encode()

# --- Paths ---
DB_PATH = "/data/impresora_usuarios.db"
PRINT_DIR = Path("/data/impresiones")
PAGE_LOG_PATH = "/var/log/cups/page_log"

# --- Limits ---
BW_PAGE_LIMIT = 200
COLOR_PAGE_LIMIT = 200
ALERT_INTERVAL = 25
LOG_POLL_SECONDS = 5
PRINTER_CHECK_INTERVAL = 30
JOB_POLL_INTERVAL = 2
JOB_POLL_TIMEOUT = 60
UNASSIGNED_USER_ID = -1

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# --- Bot instance ---
bot = TeleBot(TOKEN, num_threads=4)

# --- Messages ---
with open("messages.json", "r", encoding="utf-8") as f:
    MSGS = json.load(f)

# --- Shared state ---
PRINT_DIR.mkdir(parents=True, exist_ok=True)

user_jobs: dict[int, dict] = {}
jobs_lock = threading.Lock()

user_state: dict[int, str] = {}

tracked_jobs: dict[str, str] = {}
tracked_jobs_lock = threading.Lock()

_wiped_jobs: set[str] = set()
_wiped_jobs_lock = threading.Lock()

email_wake_event = threading.Event()
