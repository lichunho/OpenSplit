# Slim Python 3.13 to match local dev (see CLAUDE.md: "the venv is Python 3.13").
FROM python:3.13-slim

WORKDIR /app

# Installed before the app code so this layer caches across app changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app

# Shell form so ${PORT} expands: defaults to 8000 for `docker compose up`,
# but respects Render's injected $PORT if this same image runs there.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
