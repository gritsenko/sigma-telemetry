FROM python:3.12-slim
WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/app.py .
ENV DB_PATH=/data/telemetry.db TRUST_PROXY=1
VOLUME /data
EXPOSE 8000
# One worker: the app keeps one SQLite connection and an in-memory rate limiter.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-server-header", "--proxy-headers", "--forwarded-allow-ips", "*"]
