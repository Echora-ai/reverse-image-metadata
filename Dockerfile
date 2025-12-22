FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY reverse_image_service.py .

ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn reverse_image_service:app --host 0.0.0.0 --port $PORT
