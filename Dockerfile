FROM python:3.12-slim

WORKDIR /app

# Install dependencies via pip (no uv, no lockfile needed)
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    "fastapi[standard]>=0.116.1" \
    "dotenv>=0.9.9" \
    "e2b==2.1.2" \
    "httpx>=0.27.0" \
    "supabase>=2.28.0" \
    "apscheduler>=3.10.0" \
    "python-docx>=1.1.0" \
    "markdown>=3.5" \
    "xhtml2pdf>=0.2.16" \
    "beautifulsoup4>=4.12.0"

# Copy application code
COPY . .

# Start the server
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
