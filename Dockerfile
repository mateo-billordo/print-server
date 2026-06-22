FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    cups-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir pyTelegramBotAPI python-dotenv requests

COPY print_server.py messages.json ./

CMD ["python", "print_server.py"]
