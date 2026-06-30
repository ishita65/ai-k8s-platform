FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector.py analyzer.py scorer.py reporter.py orchestrator.py .

CMD ["python", "orchestrator.py"]
