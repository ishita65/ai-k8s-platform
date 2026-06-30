FROM python:3.12-slim

WORKDIR /app

# curl is required so job.yaml's shell wrapper can POST to localhost:15020/quitquitquit
# to signal the Istio sidecar to exit after the agent finishes (Job completion fix).
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py .

CMD ["python", "agent.py"]
