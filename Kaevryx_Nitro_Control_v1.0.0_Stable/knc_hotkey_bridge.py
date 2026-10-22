#!/usr/bin/env python3
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

runtime = Path(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
)
SOCKET_PATH = runtime / "kaevryx-nitro-hotkey.sock"


def launch_gui():
    env = os.environ.copy()
    env.setdefault(
        "PATH",
        "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
    )

    try:
        subprocess.Popen(
            ["kaevryx-nitro-control"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception as exc:
        print(
            f"[hotkey-bridge] falha ao abrir GUI: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return False


def main():
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)

    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(SOCKET_PATH))
    os.chmod(SOCKET_PATH, 0o600)

    print(
        f"[hotkey-bridge] pronto em {SOCKET_PATH}",
        flush=True,
    )

    try:
        while True:
            data = sock.recv(1024)
            if data.strip().upper() == b"SHOW":
                launch_gui()
    finally:
        sock.close()
        try:
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
