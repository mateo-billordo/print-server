# Print Server — Telegram Bot

A Telegram bot that turns a legacy laptop into a home print server. Authorized family members can print documents (PDFs) and images by sending them to a private Telegram chat. The printer is also shared on the local network for direct printing from any device.

## Features

- **Access control** — Admin approves users via inline buttons (USER / VIP roles)
- **Interactive print menu** — Choose copies, color mode (B&W / Color) before printing
- **Silent priority queue** — VIP jobs print before USER jobs via CUPS priority
- **Ink tracking** — Background watcher counts ALL pages from CUPS logs (bot + network), alerts admin when refill is needed
- **Nickname system** — Users can set a custom alias via `/apodo`
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
CUPS_RUN_PATH=/var/run/cups
CUPS_LOG_PATH=/var/log/cups
DATA_PATH=/home/mateo/impresora-server/data
```

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

## Commands

### All users

| Command | Description |
|---------|-------------|
| `/start` | Register or refresh your profile |
| `/ayuda` | Show available commands |
| `/apodo` | Manage your nickname |

### Admin only

| Command | Description |
|---------|-------------|
| `/usuarios` | List all authorized users and roles |
| `/ink` | Show current page counters (B&W and Color) |
| `/reset_ink` | Reset counters to zero after refilling cartridges |

### Printing via Telegram

Send any PDF or image to the chat. The bot presents an inline menu to configure copies and color mode before sending to the print queue.

### Printing via network

| Device | How to add printer |
|--------|-------------------|
| iOS | Automatic — appears in Print menu via AirPrint |
| Android | Settings → Printing → Default Print Service (or use `ipp://<server_ip>:631/printers/HP-2515`) |
| Windows | Settings → Printers → Add → `http://<server_ip>:631/printers/HP-2515` |
| macOS/Linux | Automatic via Bonjour, or add IPP printer manually |

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
cd C:\Workspace\Drafts\impresora
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
