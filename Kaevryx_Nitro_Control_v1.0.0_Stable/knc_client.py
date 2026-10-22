#!/usr/bin/env python3
from __future__ import annotations

import json
import socket
from typing import Any

from knc_common import SOCKET_PATH


class KNCError(RuntimeError):
    pass


def request(payload: dict[str, Any], timeout: float = 3.0) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)

    try:
        sock.connect(SOCKET_PATH)
        sock.sendall(
            json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        )

        data = b""
        while b"\n" not in data and len(data) < 1024 * 1024:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk

    except Exception as exc:
        raise KNCError(f"Serviço KNC indisponível: {exc}") from exc
    finally:
        sock.close()

    if not data:
        raise KNCError("Serviço KNC não retornou dados")

    try:
        resp = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
    except Exception as exc:
        raise KNCError(f"Resposta inválida do serviço: {exc}") from exc

    if not resp.get("ok"):
        raise KNCError(resp.get("error", "Erro desconhecido"))

    return resp.get("data", resp)
