#!/usr/bin/env python3
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

from evdev import InputDevice, ecodes, list_devices

CONFIG = Path("/etc/kaevryx-nitro-control.json")
TARGET_KEY_CODE = 425
TARGET_SCAN_CODE = 245
DEBOUNCE_SECONDS = 0.45

lock = threading.Lock()
active_paths: set[str] = set()
last_trigger = 0.0


def read_uid() -> int:
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        return int(data.get("allowed_uid", 1000))
    except Exception:
        return 1000


ALLOWED_UID = read_uid()


def notify_bridge(source: str) -> bool:
    global last_trigger

    now = time.monotonic()
    with lock:
        if now - last_trigger < DEBOUNCE_SECONDS:
            return True
        last_trigger = now

    sock_path = f"/run/user/{ALLOWED_UID}/kaevryx-nitro-hotkey.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    try:
        sock.settimeout(1.0)
        sock.connect(sock_path)
        sock.send(b"SHOW\n")
        print(
            f"[hotkey] SHOW enviado; origem={source}; "
            f"socket={sock_path}",
            flush=True,
        )
        return True
    except Exception as exc:
        print(f"[hotkey] bridge indisponível: {exc}", flush=True)
        return False
    finally:
        sock.close()


def device_worker(path: str) -> None:
    try:
        dev = InputDevice(path)
    except Exception as exc:
        with lock:
            active_paths.discard(path)
        print(f"[hotkey] falha abrindo {path}: {exc}", flush=True)
        return

    print(
        f"[hotkey] monitorando {path}: {dev.name!r} "
        f"phys={dev.phys!r}",
        flush=True,
    )

    last_scan_245 = 0.0

    try:
        for event in dev.read_loop():
            if (
                event.type == ecodes.EV_KEY
                and event.code == TARGET_KEY_CODE
                and event.value == 1
            ):
                notify_bridge(
                    f"{path}:{dev.name}:EV_KEY:{TARGET_KEY_CODE}"
                )
                continue

            if (
                event.type == ecodes.EV_MSC
                and event.code == ecodes.MSC_SCAN
                and event.value == TARGET_SCAN_CODE
            ):
                now = time.monotonic()
                if now - last_scan_245 > DEBOUNCE_SECONDS:
                    last_scan_245 = now
                    notify_bridge(
                        f"{path}:{dev.name}:MSC_SCAN:{TARGET_SCAN_CODE}"
                    )

    except Exception as exc:
        print(
            f"[hotkey] leitura encerrada em {path}: {exc}",
            flush=True,
        )
    finally:
        try:
            dev.close()
        except Exception:
            pass
        with lock:
            active_paths.discard(path)


def candidate(path: str) -> bool:
    try:
        dev = InputDevice(path)
        caps = dev.capabilities()
        key_codes = caps.get(ecodes.EV_KEY, [])
        has_target_key = TARGET_KEY_CODE in key_codes
        has_scan = ecodes.EV_MSC in caps
        dev.close()
        return has_target_key or has_scan
    except Exception:
        return False


def main():
    print(
        "[hotkey] Kaevryx Nitro Hotkey Stable\n"
        f"[hotkey] uid={ALLOWED_UID}; EV_KEY={TARGET_KEY_CODE}; "
        f"fallback MSC_SCAN={TARGET_SCAN_CODE}",
        flush=True,
    )

    while True:
        try:
            paths = list_devices()
        except Exception as exc:
            print(
                f"[hotkey] erro listando dispositivos: {exc}",
                flush=True,
            )
            time.sleep(2)
            continue

        for path in paths:
            with lock:
                if path in active_paths:
                    continue

            if not candidate(path):
                continue

            with lock:
                active_paths.add(path)

            threading.Thread(
                target=device_worker,
                args=(path,),
                daemon=True,
                name=f"knc-{Path(path).name}",
            ).start()

        time.sleep(2)


if __name__ == "__main__":
    main()
