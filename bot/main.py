import threading

from telebot import types

from bot.config import bot, log
from bot.db import init_db, db_query
import bot.handlers  # noqa: F401 — registers all handlers via decorators
from bot.workers import page_log_watcher, printer_status_watcher, email_check_loop


def clear_all_commands():
    """Remove any previously registered command menus."""
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


def main():
    init_db()
    clear_all_commands()

    threading.Thread(target=page_log_watcher, daemon=True).start()
    threading.Thread(target=printer_status_watcher, daemon=True).start()
    threading.Thread(target=email_check_loop, daemon=True).start()

    log.info("Print server bot starting (threads=%d)...", 4)
    bot.infinity_polling()


if __name__ == '__main__':
    main()
