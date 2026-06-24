# Print Server — Telegram Bot

A Telegram bot that turns a legacy laptop into a home print server. Authorized family members can print documents (PDFs) and images by sending them to a private Telegram chat. The printer is also shared on the local network for direct printing from any device.

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Hardware](#hardware)
- [Setup](#setup)
  - [Prerequisites](#prerequisites)
  - [Environment variables](#environment-variables)
  - [CUPS configuration](#cups-configuration)
  - [Deploy](#deploy)
  - [Verify](#verify)
- [How to Use](#how-to-use)
- [Printing via Telegram](#printing-via-telegram)
- [Printing via network](#printing-via-network)
- [Printing via email](#printing-via-email)
- [Project Structure](#project-structure)
- [Database Schema](#database-schema)
- [Ink Alert Logic](#ink-alert-logic)
- [Updating](#updating)

## Features

- **Access control** — Admin approves users via inline buttons (USER / VIP roles)
- **Interactive print menu** — Choose copies, color mode (B&W / Color) before printing
- **Silent priority queue** — VIP jobs print before USER jobs via CUPS priority
- **Ink tracking** — Background watcher counts ALL pages from CUPS logs (bot + network), alerts admin when refill is needed
- **Nickname system** — Users can set a custom alias
- **Inline menu system** — Role-aware inline keyboard appears after every interaction; no command menu needed
- **Email-to-print** — Gmail IMAP reader prints attachments from whitelisted email addresses on a configurable timer
- **Non-blocking** — Print jobs run in background threads; bot stays responsive for all users
- **Network sharing** — Printer available via IPP/AirPrint to all LAN devices (2.4GHz, 5GHz, wired)

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Local Network                         │
│  iOS/Android (AirPrint) ──┐                             │
│  Windows PC (IPP) ────────┤                             │
│  Telegram Bot ────────────┤                             │
│                           ▼                             │
│              Host CUPS (port 631)                        │
│                      │                                  │
│                      ▼                                  │
│            HP DeskJet 2515 (USB)                         │
└─────────────────────────────────────────────────────────┘

Docker Container (python:3.11-slim)
├── print_server.py ──► /var/run/cups (socket mount)
├── messages.json
└── page_log_watcher ──► /var/log/cups/page_log (volume mount)
```

## Hardware

| Component | Details |
|-----------|---------|
| Host | Lenovo ThinkPad T400, Ubuntu Server |
| Printer | HP DeskJet Ink Advantage 2515 (USB) |
| CUPS name | `HP-2515` |

## Setup

### Prerequisites

- Docker & Docker Compose installed on the host
- CUPS configured with the printer registered as `HP-2515`
- Avahi daemon for network discovery (AirPrint)
- HPLIP disabled (to bypass ink chip restrictions)

### Environment variables

Create a `.env` file in the project root:

```env
TELEGRAM_TOKEN=your_bot_token
ADMIN_ID=your_telegram_chat_id
PRINTER_NAME=HP-2515
ENCRYPTION_KEY=your_fernet_key
CUPS_RUN_PATH=/var/run/cups
CUPS_LOG_PATH=/var/log/cups
DATA_PATH=/home/mateo/impresora-server/data
```

> Generate the encryption key once: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

### CUPS configuration

#### Share printer on local network

```bash
sudo cupsctl --share-printers
sudo cupsctl --remote-any
sudo systemctl restart cups
```

Edit `/etc/cups/cupsd.conf` — ensure:

```
Listen *:631

<Location />
  Order allow,deny
  Allow @LOCAL
</Location>

<Location /admin>
  Order allow,deny
  Allow @LOCAL
</Location>

<Location /admin/conf>
  Order allow,deny
  Allow @LOCAL
</Location>
```

#### Ink chip bypass

```bash
sudo lpadmin -p HP-2515 -o printer-error-policy=retry-current-job
sudo systemctl stop hplip
sudo systemctl disable hplip
cupsenable HP-2515
```

#### Network discovery (AirPrint)

```bash
sudo apt install -y avahi-daemon
sudo systemctl enable avahi-daemon
sudo systemctl start avahi-daemon
```

#### Firewall (if UFW is active)

```bash
sudo ufw allow 631/tcp
sudo ufw allow 5353/udp
sudo ufw reload
```

### Deploy

```bash
docker compose build --no-cache
docker compose up -d
```

### Verify

```bash
docker logs -f print_tg_bot
```

## How to Use

The bot is fully operated via **inline buttons** — no need to type commands. The menu appears automatically after every interaction (printing, greeting, etc.).

1. Open the chat → Telegram sends `/start` automatically
2. The bot greets you and shows a **Menú** button
3. Tap **Menú** to access all features (nickname, emails, help, admin tools)
4. To print: just send a PDF or image — the bot shows copy/color options inline

### Hidden Commands (fallback only)

These still work if typed manually but are not shown in any menu:

| Command | Description |
|---------|-------------|
| `/start` | Re-register or refresh profile |
| `/menu` | Force-show the inline menu |

### Menu Navigation

The `/menu` command opens an inline keyboard with sub-menus:

```
Main Menu
├── 👤 Apodo — manage nickname
├── 📧 Emails (sub-menu)
│   ├── 📋 Mis emails — list registered emails
│   ├── ➕ Agregar — register email (type in chat)
│   ├── ➖ Eliminar — remove email (type in chat)
│   ├── 📋 Todos (admin) — all emails with user links
│   ├── ⚙️ Receptor (admin) — email-to-print config
│   └── ← Volver
├── ❓ Ayuda — show help
├── 👥 Usuarios (admin, sub-menu)
│   ├── 📋 Listar — show users and roles
│   ├── 🗑️ Eliminar — pick user to remove
│   ├── 🔄 Cambiar rol — pick user to toggle USER/VIP
│   └── ← Volver
└── 🖨️ Tinta (admin, sub-menu)
    ├── 📊 Estado — page counters
    ├── 🔄 Reiniciar — reset counters
    └── ← Volver
```

The menu reappears after every interaction so you never need to type `/menu` again.

### Printing via Telegram

Send any PDF or image to the chat. The bot presents an inline menu to configure copies and color mode before sending to the print queue.

### Printing via network

| Device | How to add printer |
|--------|-------------------|
| iOS | Automatic — appears in Print menu via AirPrint |
| Android | Settings → Printing → Default Print Service (or use `ipp://<server_ip>:631/printers/HP-2515`) |
| Windows | Settings → Printers → Add → `http://<server_ip>:631/printers/HP-2515` |
| macOS/Linux | Automatic via Bonjour, or add IPP printer manually |

### Printing via email

Send an email with PDF/image attachments to the configured Gmail address from a whitelisted email. The bot checks for new emails on a timer.

**Email body template (optional):**

```
copias: 3, modo: color
```

- `copias` — number of copies (default: 1)
- `modo` — `color` or `bn` (default: bn = black & white)

If the body is blank or unparseable, defaults to 1 copy in B&W.

## Project Structure

```
impresora-server/
├── print_server.py       # Bot logic (English code)
├── messages.json         # UI strings (Spanish)
├── Dockerfile
├── docker-compose.yml
├── .env                  # Secrets (git-ignored)
├── .gitignore
└── data/                 # Persistent volume (git-ignored)
    └── impresora_usuarios.db
```

## Database Schema

**`users`** table:

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER (PK) | Telegram chat_id |
| real_name | TEXT | Telegram first name (auto-updated) |
| nickname | TEXT | User-managed alias |
| role | TEXT | `VIP` or `USER` |
| waiting_for_nickname | INTEGER | FSM flag (0/1) |

**`ink_counters`** table:

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER (PK) | Always 1 (single row) |
| bw_pages | INTEGER | Black & white pages since last reset |
| color_pages | INTEGER | Color pages since last reset |
| last_alert_bw | INTEGER | Alert counter for B&W threshold |
| last_alert_color | INTEGER | Alert counter for Color threshold |
| log_offset | INTEGER | Byte offset in page_log (avoids re-counting) |

**`email_config`** table:

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER (PK) | Always 1 (single row) |
| address | TEXT | Gmail address for IMAP |
| encrypted_password | TEXT | Fernet-encrypted Gmail app password |
| timer_minutes | INTEGER | Check interval in minutes (0 = disabled) |

**`user_emails`** table:

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER (PK) | Auto-increment |
| user_id | INTEGER | Telegram chat_id (or -1 for unassigned) |
| email | TEXT (UNIQUE) | Whitelisted email address |

## Ink Alert Logic

- Thresholds: 200 pages each (B&W and Color)
- Background watcher tails `/var/log/cups/page_log` every 5 seconds
- Bot-submitted jobs: color mode tracked via job ID
- Network jobs: default to color (conservative)
- Alerts sent to admin every 25 pages past the threshold
- `/reset_ink` zeros counters but preserves log offset

## Updating

From your development machine:

```bash
cd C:\personal_workspace\impresora
git add .
git commit -m "description"
git push
```

On the server:

```bash
cd ~/impresora-server
git pull
docker compose build --no-cache && docker compose up -d
```
