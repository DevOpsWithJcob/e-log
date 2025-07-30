# Dockerfile
FROM python:3.9-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir \
    kubernetes \
    colorama \
    flask \
    redis

# Copy application code
COPY app.py .

# Expose port
EXPOSE 5000

# Command to run the app
CMD ["python", "app.py"]
