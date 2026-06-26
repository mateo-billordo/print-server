FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    cups-client \
    usbutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir pyTelegramBotAPI python-dotenv requests cryptography img2pdf Pillow

COPY bot/ bot/
COPY messages.json ./

CMD ["python", "-m", "bot.main"]
