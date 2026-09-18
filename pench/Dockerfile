# syntax=docker/dockerfile:1

FROM python:3.13-slim

# ============================================================
# PLAYWRIGHT BROWSER DEPENDENCIES
# Comment out this section if using hosted browsers (Browserbase, etc.)
# ============================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 libx11-xcb1 \
    && rm -rf /var/lib/apt/lists/*
# ============================================================

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project

# ============================================================
# PLAYWRIGHT BROWSER INSTALL
# Comment out if using hosted browsers
# ============================================================
RUN .venv/bin/playwright install chromium
# ============================================================

COPY . .
RUN uv sync --frozen --no-dev

CMD ["uv", "run", "scrape", "all", "--loop", "--headless", "--workers", "1"]
