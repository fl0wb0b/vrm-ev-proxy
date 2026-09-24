FROM python:3.12-slim
WORKDIR /app
COPY app.py .
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python", "/app/app.py", "--healthcheck"]
CMD ["python", "-u", "app.py"]
