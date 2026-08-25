FROM python:3.11-slim

# Install system dependencies:
# - ffmpeg and ffprobe for audio trimming and loudness normalization
# - curl for healthchecks and external fetches
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Prevent Python from buffering stdout/stderr (ensuring immediate log streaming)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and migrations
COPY ingestion/ /app/ingestion/
COPY migrations/ /app/migrations/

# Default command: run the continuous ingestion worker
CMD ["python", "-m", "ingestion.worker"]
