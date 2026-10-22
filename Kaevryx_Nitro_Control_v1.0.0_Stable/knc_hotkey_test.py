#!/usr/bin/env python3
import os
import socket
from pathlib import Path

sock_path = Path(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
) / "kaevryx-nitro-hotkey.sock"

sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

try:
    sock.connect(str(sock_path))
    sock.send(b"SHOW\n")
    print(f"OK: SHOW enviado para {sock_path}")
except Exception as exc:
    print(f"ERRO: bridge indisponível em {sock_path}: {exc}")
    raise SystemExit(1)
finally:
    sock.close()
