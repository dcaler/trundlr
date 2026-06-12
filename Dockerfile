FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY runner.py ./runner.py

# Create directory for SQLite database persistence
RUN mkdir -p /app/data

# Expose port 8251
EXPOSE 8251

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8251"]
