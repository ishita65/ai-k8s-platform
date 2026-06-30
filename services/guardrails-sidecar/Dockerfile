# ---- Stage 1: generate Envoy ext_proc Python proto bindings ----
FROM python:3.12-slim AS proto-gen

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir grpcio-tools

# buf exports all Envoy API protos from the public BSR — no auth needed
RUN curl -sSfL \
    "https://github.com/bufbuild/buf/releases/download/v1.50.0/buf-Linux-x86_64" \
    -o /usr/local/bin/buf \
    && chmod +x /usr/local/bin/buf

RUN buf export buf.build/envoyproxy/envoy --output /protos

COPY compile_protos.py /
RUN python /compile_protos.py

# ---- Stage 2: runtime image ----
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy pre-generated proto bindings from the build stage
COPY --from=proto-gen /generated/ ./

COPY main.py guardrails_service.py ./

RUN useradd -r -s /sbin/nologin guardrails
USER guardrails

CMD ["python", "main.py"]
