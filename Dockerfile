FROM python:3.11-slim-bookworm

# Install pinned Chromium, ChromeDriver, and all required system libraries
# Versions are locked to match the working reference Dockerfile.
# apt-mark hold prevents accidental upgrades via apt-get upgrade.
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium=147.0.7727.137-1~deb12u1 \
    chromium-common=147.0.7727.137-1~deb12u1 \
    chromium-driver=147.0.7727.137-1~deb12u1 \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libgdk-pixbuf2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    xdg-utils \
    && apt-mark hold chromium chromium-common chromium-driver \
    && rm -rf /var/lib/apt/lists/*

# Browser paths and headless flag — consumed by scrapers/chrome_helper.py
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV HEADLESS=True

WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Run the orchestrator
CMD ["python", "-u", "run_all.py"]
