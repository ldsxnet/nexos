FROM python:3.12-slim AS builder

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser

# Copy application code
COPY app.py .
COPY .env.example .env.example
COPY static ./static

# Create data directory and set permissions
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

# Default environment variables
ENV HOST=0.0.0.0
ENV PORT=3000
ENV NEXOS_ACCOUNTS_FILE=/app/data/nexos_accounts.json
ENV CURRENT_CHAT_FILE=/app/data/current-chat.json

EXPOSE 3000

CMD ["python", "app.py"]
