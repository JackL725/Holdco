"""Tests never talk to an owner's running Holdco: in-process tests that build preview URLs (browser_check, the
verification gate) would otherwise reach whatever listens on the default port 8420. A free port is picked once,
before holdco.config reads HOLDCO_PORT; end-to-end tests start their own server on a port they choose."""
import os
import socket

if "HOLDCO_PORT" not in os.environ:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        _s.bind(("127.0.0.1", 0))
        os.environ["HOLDCO_PORT"] = str(_s.getsockname()[1])
os.environ.setdefault("HOLDCO_SANDBOX", "off")   # no container runtime in tests (CI runners may have Docker)
