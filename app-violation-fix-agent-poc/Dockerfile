FROM python:3.12-slim

# Install curl for downloading binaries
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Install conftest (OPA policy test runner)
RUN curl -fsSL \
    https://github.com/open-policy-agent/conftest/releases/download/v0.57.0/conftest_0.57.0_Linux_x86_64.tar.gz | \
    tar -xz -C /usr/local/bin conftest && \
    chmod +x /usr/local/bin/conftest

# Install Helm (for rendering chart templates before policy scan)
RUN curl -fsSL https://get.helm.sh/helm-v3.16.4-linux-amd64.tar.gz | \
    tar -xz -C /tmp linux-amd64/helm && \
    mv /tmp/linux-amd64/helm /usr/local/bin/helm && \
    rm -rf /tmp/linux-amd64 && \
    chmod +x /usr/local/bin/helm

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY agents/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire poc/ directory into /app
COPY . .

# agents/ directory must be on PYTHONPATH for cross-module imports
ENV PYTHONPATH=/app/agents
ENV BASE_DIR=/app

CMD ["python", "agents/main.py"]
