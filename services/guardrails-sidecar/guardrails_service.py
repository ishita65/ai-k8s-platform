"""
Envoy ext_proc gRPC service that enforces a list of AWS Bedrock Guardrails.

Protocol summary (Envoy External Processing v3):
  - Envoy streams ProcessingRequest messages per lifecycle phase.
  - We act on REQUEST_BODY only: parse the OpenAI JSON body, extract user text,
    call ApplyGuardrail for each guardrail in GUARDRAILS list (sequential, short-circuit
    on first GUARDRAIL_INTERVENED), and return 403 ImmediateResponse if blocked.
  - All other phases return a no-op CONTINUE response.
  - EnvoyExtensionPolicy must set processingMode.request.body: BUFFERED so we
    receive the full body in a single message (not streaming chunks).
  - ext_proc runs BEFORE Envoy AI Gateway's protocol translation, so the body
    is still in OpenAI /v1/chat/completions format at this stage.

GUARDRAIL_IDS env var format (JSON array, loaded from per-gateway ConfigMap):
  [{"id": "abc123", "version": "DRAFT", "name": "violence-filter"}, ...]
  Each gateway's EnvoyProxy references a different ConfigMap, giving per-application
  guardrail policies without changing or rebuilding the sidecar image.
"""

import json
import logging
import os
from typing import Iterator

import boto3
from botocore.exceptions import ClientError

from envoy.config.core.v3 import base_pb2 as core_pb2
from envoy.service.ext_proc.v3 import external_processor_pb2 as ext_proc_pb2
from envoy.service.ext_proc.v3 import external_processor_pb2_grpc as ext_proc_pb2_grpc
from envoy.type.v3 import http_status_pb2

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
FAIL_OPEN = os.environ.get("FAIL_OPEN", "true").lower() == "true"

# GUARDRAIL_IDS: JSON array loaded from the per-gateway ConfigMap (03-guardrail-config.yaml).
# Format: [{"id": "abc123", "version": "DRAFT", "name": "violence-filter"}, ...]
# Applied sequentially — first guardrail to INTERVENE blocks the request (short-circuit).
_raw = os.environ.get("GUARDRAIL_IDS", "[]")
GUARDRAILS: list[dict] = json.loads(_raw)
if not GUARDRAILS:
    raise ValueError("GUARDRAIL_IDS env var must be a non-empty JSON array")
for _g in GUARDRAILS:
    _g.setdefault("version", "DRAFT")
    _g.setdefault("name", _g["id"])

_bedrock = None


def _get_bedrock():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _bedrock


def _extract_user_text(body_bytes: bytes) -> str | None:
    """Extract concatenated user-role text from an OpenAI messages array."""
    try:
        payload = json.loads(body_bytes)
        parts = []
        for msg in payload.get("messages", []):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            parts.append(part.get("text", ""))
        return "\n".join(parts) if parts else None
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("Body parse error: %s", exc)
        return None


def _apply_single_guardrail(text: str, guardrail_id: str, version: str, name: str) -> tuple[bool, str]:
    """Calls one guardrail. Returns (blocked, reason)."""
    try:
        response = _get_bedrock().apply_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=version,
            source="INPUT",
            content=[{"text": {"text": text}}],
        )
        action = response.get("action", "NONE")
        if action == "GUARDRAIL_INTERVENED":
            outputs = response.get("outputs", [])
            reason = (
                outputs[0].get("text", f"Blocked by guardrail '{name}'") if outputs
                else f"Blocked by guardrail '{name}'"
            )
            logger.info("BLOCKED guardrail=%s action=%s reason=%s", name, action, reason[:100])
            return True, reason
        logger.debug("PASSED guardrail=%s action=%s", name, action)
        return False, ""
    except ClientError as exc:
        logger.error("ApplyGuardrail error guardrail=%s: %s", name, exc)
        if FAIL_OPEN:
            logger.warning("FAIL_OPEN=true — allowing despite API error on guardrail=%s", name)
            return False, ""
        return True, f"Guardrail service temporarily unavailable (guardrail: {name})"
    except Exception as exc:
        logger.error("Unexpected error guardrail=%s: %s", name, exc)
        if FAIL_OPEN:
            return False, ""
        return True, f"Guardrail service error (guardrail: {name})"


def _apply_guardrails(text: str) -> tuple[bool, str]:
    """Applies all configured guardrails in order. Short-circuits on first block."""
    for g in GUARDRAILS:
        blocked, reason = _apply_single_guardrail(text, g["id"], g["version"], g["name"])
        if blocked:
            return True, reason
    return False, ""


def _block_response(message: str) -> ext_proc_pb2.ProcessingResponse:
    """ProcessingResponse that short-circuits with HTTP 403 Forbidden."""
    return ext_proc_pb2.ProcessingResponse(
        immediate_response=ext_proc_pb2.ImmediateResponse(
            status=http_status_pb2.HttpStatus(
                code=http_status_pb2.StatusCode.Value("Forbidden")
            ),
            body=json.dumps({
                "error": {
                    "message": message,
                    "type": "content_filter",
                    "code": "content_policy_violation",
                }
            }).encode(),
            headers=ext_proc_pb2.HeaderMutation(
                set_headers=[
                    core_pb2.HeaderValueOption(
                        header=core_pb2.HeaderValue(
                            key="content-type", value="application/json"
                        )
                    ),
                    core_pb2.HeaderValueOption(
                        header=core_pb2.HeaderValue(
                            key="x-guardrail-blocked", value="true"
                        )
                    ),
                ]
            ),
        )
    )


def _continue_body() -> ext_proc_pb2.ProcessingResponse:
    return ext_proc_pb2.ProcessingResponse(
        request_body=ext_proc_pb2.BodyResponse(
            response=ext_proc_pb2.CommonResponse(
                status=ext_proc_pb2.CommonResponse.ResponseStatus.CONTINUE
            )
        )
    )


def _continue_headers() -> ext_proc_pb2.ProcessingResponse:
    return ext_proc_pb2.ProcessingResponse(
        request_headers=ext_proc_pb2.HeadersResponse(
            response=ext_proc_pb2.CommonResponse(
                status=ext_proc_pb2.CommonResponse.ResponseStatus.CONTINUE
            )
        )
    )


class GuardrailsService(ext_proc_pb2_grpc.ExternalProcessorServicer):
    """
    Bidirectional gRPC stream handler for Envoy ext_proc.
    Inspects REQUEST_BODY phase; all other phases pass through unchanged.
    """

    def Process(
        self,
        request_iterator: Iterator[ext_proc_pb2.ProcessingRequest],
        context,
    ) -> Iterator[ext_proc_pb2.ProcessingResponse]:
        for request in request_iterator:
            phase = request.WhichOneof("request")

            if phase == "request_headers":
                yield _continue_headers()

            elif phase == "request_body":
                text = _extract_user_text(request.request_body.body)
                if text:
                    blocked, reason = _apply_guardrails(text)
                    if blocked:
                        yield _block_response(reason)
                        return  # close stream; Envoy delivers the 403
                yield _continue_body()

            else:
                yield ext_proc_pb2.ProcessingResponse()
