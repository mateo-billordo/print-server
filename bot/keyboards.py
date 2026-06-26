from telebot import types

from bot.config import ADMIN_ID, MSGS, bot, jobs_lock, user_jobs
from bot.db import db_query


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
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(MSGS["btn_back"], callback_data="menu_monitor_sub"))
    return markup


def build_user_select_buttons(action_prefix: str) -> types.InlineKeyboardMarkup:
    rows = db_query("SELECT id, real_name, nickname, role FROM users")
    markup = types.InlineKeyboardMarkup(row_width=1)
    for uid, name, nick, role in rows:
        if uid == ADMIN_ID:
            continue
        display = f"{name} ({nick})" if nick else name
        label = f"{display} [{role}]"
        markup.add(types.InlineKeyboardButton(label, callback_data=f"{action_prefix}_{uid}"))
    markup.add(types.InlineKeyboardButton(MSGS["btn_back"], callback_data="menu_users_sub"))
    return markup
