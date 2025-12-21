FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY reverse_image_service.py .

EXPOSE 8080

CMD ["uvicorn", "reverse_image_service:app", "--host", "0.0.0.0", "--port", "8080"]
