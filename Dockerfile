# RSSPhaser image — runs both the web (gunicorn) and scheduler processes
# depending on the command. Non-root by default.
FROM python:3.12-slim

# mysqlclient needs build tooling + MariaDB client headers to compile, plus
# curl for the container healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        default-libmysqlclient-dev \
        pkg-config \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_PATH=/app/config.yaml

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application.
COPY . .

# Create and switch to a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Default command runs the web server. The scheduler service overrides this.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", \
     "--timeout", "120", "--graceful-timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
