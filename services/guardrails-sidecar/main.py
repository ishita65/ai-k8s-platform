"""gRPC server for the guardrails ext_proc service. Listens on a Unix domain socket."""

import logging
import os
import signal
import time
from concurrent import futures

import grpc

from guardrails_service import GuardrailsService
from envoy.service.ext_proc.v3 import external_processor_pb2_grpc as ext_proc_pb2_grpc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [guardrails] %(message)s",
)
logger = logging.getLogger(__name__)

SOCKET_PATH = os.environ.get("SOCKET_PATH", "/shared/guardrails/guardrails.sock")
MAX_WORKERS = int(os.environ.get("GRPC_WORKERS", "4"))


def serve() -> None:
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=MAX_WORKERS),
        options=[
            ("grpc.max_receive_message_length", 10 * 1024 * 1024),
            ("grpc.max_send_message_length", 10 * 1024 * 1024),
        ],
    )
    ext_proc_pb2_grpc.add_ExternalProcessorServicer_to_server(GuardrailsService(), server)
    server.add_insecure_port(f"unix://{SOCKET_PATH}")
    server.start()
    # Envoy runs as UID 65532; allow it to connect to the socket created by our non-root user.
    os.chmod(SOCKET_PATH, 0o777)
    logger.info("Listening on socket %s", SOCKET_PATH)

    signal.signal(signal.SIGTERM, lambda *_: server.stop(grace=5))

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop(grace=5)


if __name__ == "__main__":
    serve()
