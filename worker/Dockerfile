FROM python:3.13-slim
RUN useradd -r -s /bin/false worker
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY worker.py .
USER worker
CMD ["python", "worker.py"]
