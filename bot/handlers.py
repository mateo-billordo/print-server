import os
import re
import sqlite3
import subprocess
import threading

from telebot import types

from bot.config import (
    ADMIN_ID, PRINTER_NAME, PRINT_DIR, UNASSIGNED_USER_ID,
    BW_PAGE_LIMIT, COLOR_PAGE_LIMIT,
    bot, MSGS, log,
    user_jobs, jobs_lock, user_state,
    _wiped_jobs, _wiped_jobs_lock,
    email_wake_event, DB_PATH,
)
from bot.db import (
    db_query, get_user_role, get_user_profile, set_nickname_state,
    register_user, get_user_emails, get_all_user_emails,
    add_user_email, remove_user_email,
    get_email_config, set_email_config, encrypt_password,
    get_ink_counters, reset_ink_counters,
)
from bot.keyboards import (
    build_auth_menu, build_nickname_menu, build_print_menu,
    build_main_menu, build_single_menu_button,
    build_users_sub_menu, build_emails_sub_menu,
    build_email_config_sub_menu, build_monitor_sub_menu,
    build_monitor_back_button, build_user_select_buttons,
)
from bot.printer import (
    get_printer_status, reactivate_printer, execute_print_job,
)


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

    state = user_state.pop(chat_id, None)
    if state == "waiting_add_email":
        addr = message.text.strip().lower()
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
        queued = subprocess.run(["lpstat", "-o", PRINTER_NAME], capture_output=True, text=True)
        if queued.stdout:
            with _wiped_jobs_lock:
                for line in queued.stdout.strip().splitlines():
                    parts = line.split()
                    if parts:
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
