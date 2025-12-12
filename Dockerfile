FROM python:3.10-slim

# Install system dependencies (Tesseract + PDF libs)
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    libtesseract-dev \
    poppler-utils \
    && apt-get clean

# Set work directory
WORKDIR /app

# Copy project files
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Expose port for FastAPI
EXPOSE 10000

# Use Uvicorn to run the FastAPI webhook server
CMD ["uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "10000"]
