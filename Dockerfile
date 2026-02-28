# 1. Базовый образ
FROM python:3.10-slim

# 2. Обновляем списки пакетов и ставим ТОЛЬКО wget (чтобы скачать хром)
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# 3. Скачиваем Chrome
RUN wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb

# 4. Устанавливаем Chrome. 
# Хитрость: мы передаем путь к файлу прямо в apt-get install.
# Apt сам увидит, чего не хватает (libxss, fonts и т.д.), и докачает это.
RUN apt-get update && apt-get install -y ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb

# 5. Настройка приложения
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

CMD ["python", "main.py"]
