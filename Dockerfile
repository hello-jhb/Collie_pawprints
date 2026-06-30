# Single-container deploy for Cloud Run (the cheapest path): one image serves the
# engine API and, once built, the static front-end. Scales to zero — idle = $0.
FROM python:3.12-slim

WORKDIR /app

# System deps openpyxl/pillow may want; kept minimal.
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run injects $PORT (default 8080). Bind 0.0.0.0.
ENV PORT=8080
CMD exec uvicorn server:app --host 0.0.0.0 --port ${PORT}
