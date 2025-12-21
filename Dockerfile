FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for cloudscraper and its JavaScript engine
RUN apt-get update && apt-get install -y \
    gcc \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all Python files
COPY *.py .

EXPOSE 8080

CMD ["uvicorn", "reverse_image_service:app", "--host", "0.0.0.0", "--port", "8080"]
