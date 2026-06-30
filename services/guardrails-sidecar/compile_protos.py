"""Compile all exported Envoy API proto files to Python using grpcio-tools."""
import glob
import os
import subprocess
import sys

import grpc_tools

GRPC_PROTO = os.path.join(grpc_tools.__path__[0], "_proto")
PROTO_ROOT = "/protos"
OUT_DIR = "/generated"

os.makedirs(OUT_DIR, exist_ok=True)

proto_files = glob.glob(os.path.join(PROTO_ROOT, "**/*.proto"), recursive=True)
print(f"Compiling {len(proto_files)} proto files...", flush=True)

result = subprocess.run(
    [
        sys.executable, "-m", "grpc_tools.protoc",
        f"-I{GRPC_PROTO}",
        f"-I{PROTO_ROOT}",
        f"--python_out={OUT_DIR}",
        f"--grpc_python_out={OUT_DIR}",
        *proto_files,
    ],
    capture_output=True,
    text=True,
)

if result.returncode != 0:
    print(result.stderr, file=sys.stderr)
    sys.exit(result.returncode)

warnings = [l for l in result.stderr.splitlines() if l.strip()]
if warnings:
    print(f"{len(warnings)} warnings (non-fatal)", flush=True)

generated = glob.glob(os.path.join(OUT_DIR, "**/*.py"), recursive=True)
print(f"Generated {len(generated)} Python files.", flush=True)
