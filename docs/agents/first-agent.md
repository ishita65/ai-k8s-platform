# First Agent

A [LangGraph](https://langchain-ai.github.io/langgraph/) agent that connects to AWS Bedrock through the Envoy AI Gateway and runs a fixed set of completion tasks. It demonstrates the full request path: agent → Envoy AI Gateway → AWS Bedrock.

## Architecture

```
first-agent Pod (default namespace)
  │  OpenAI-compatible POST /v1/chat/completions
  │  model: us.meta.llama3-3-70b-instruct-v1:0
  ▼
Envoy AI Gateway (envoy-gateway-system)
  │  reads model field → sets x-ai-eg-model header
  │  AIGatewayRoute matches → AIServiceBackend (AWSBedrock schema)
  │  BackendSecurityPolicy injects AWS credentials
  ▼
AWS Bedrock  bedrock-runtime.us-east-1.amazonaws.com:443
```

The agent graph has a single `process` node and a conditional edge that loops through 4 prompts then exits:

```
[START] → process → (more tasks?) → process → … → [END]
```

## Files

| Path | Purpose |
|---|---|
| `agents/first-agent/agent.py` | LangGraph agent — State, graph, task loop |
| `agents/first-agent/requirements.txt` | Python dependencies |
| `agents/first-agent/Dockerfile` | Container image (`python:3.12-slim`) |
| `manifests/first-agent/job.yaml` | Kubernetes Job manifest |

## Prerequisites

- Gateway setup complete — see [setup.md](../setup.md).
- AWS Bedrock credentials applied (`templates/aws-bedrock/sample.yaml`).
- Local image registry running at `localhost:5001`.
- `GATEWAY_URL` in `manifests/first-agent/job.yaml` updated with the generated service name.

## Running

### 1. Build and push the image

```bash
docker build -t localhost:5001/first-agent:latest agents/first-agent
docker push localhost:5001/first-agent:latest
```

### 2. Apply the Job

```bash
kubectl apply -f manifests/first-agent/job.yaml
```

### 3. View output

```bash
kubectl logs -f job/first-agent
```

Expected output:

```
=== First Agent ===
Gateway : http://envoy-default-envoy-ai-gateway-basic-21a9f8f8.envoy-gateway-system.svc.cluster.local
Model   : us.meta.llama3-3-70b-instruct-v1:0
Tasks   : 4

[1/4] Prompt: Explain Kubernetes in one short paragraph.
Response: Kubernetes is an open-source container orchestration system...

[2/4] Prompt: What are the key benefits of using Envoy as a service proxy?
Response: ...

[3/4] Prompt: How does AWS Bedrock simplify deploying foundation models...
Response: ...

[4/4] Prompt: Write a two-sentence summary of what a LangGraph agent is.
Response: ...

=== All tasks completed ===
```

### 4. Re-run the Job

Jobs are immutable — delete before reapplying:

```bash
kubectl delete job first-agent
kubectl apply -f manifests/first-agent/job.yaml
```

## Configuration

| Env var | Default | Description |
|---|---|---|
| `GATEWAY_URL` | `http://envoy-default-...svc.cluster.local` | Envoy Gateway service URL |
| `MODEL_ID` | `us.meta.llama3-3-70b-instruct-v1:0` | Bedrock model ID sent in the `model` field |