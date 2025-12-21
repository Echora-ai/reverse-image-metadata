# Use official Playwright image with Python and browsers pre-installed
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY reverse_image_service.py .

ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn reverse_image_service:app --host 0.0.0.0 --port $PORT
