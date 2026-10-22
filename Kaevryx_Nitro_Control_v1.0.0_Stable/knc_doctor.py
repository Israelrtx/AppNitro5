#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from knc_client import KNCError, request
from knc_common import (
    VERSION,
    PLATFORM_PROFILE,
    PLATFORM_CHOICES,
    find_acer_hwmon,
    read_text,
)


def cmd(args):
    try:
        p = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as exc:
        return 99, str(exc)


def main():
    print(f"Kaevryx Nitro Control Doctor {VERSION}")
    print("=" * 58)

    print(f"Modelo: {read_text('/sys/class/dmi/id/product_name', '?')}")
    print(f"BIOS:   {read_text('/sys/class/dmi/id/bios_version', '?')}")
    print(f"Kernel: {os.uname().release}")
    print(f"Perfil: {read_text(PLATFORM_PROFILE, '?')}")
    print(f"Perfis: {read_text(PLATFORM_CHOICES, '?')}")

    h = find_acer_hwmon()
    print(f"hwmon Acer: {h or 'NÃO ENCONTRADO'}")

    if h:
        for name in (
            "fan1_input",
            "fan2_input",
            "pwm1",
            "pwm2",
            "pwm1_enable",
            "pwm2_enable",
        ):
            p = h / name
            print(
                f"  {name:12}: "
                f"{read_text(p, '<não legível>') if p.exists() else '<ausente>'}"
            )

    rc, out = cmd(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
    print(f"NVIDIA: {out if rc == 0 else 'indisponível: ' + out}")

    print("\nServiços:")
    for service in (
        "kaevryx-nitro-control.service",
        "kaevryx-nitro-hotkey-root.service",
        "power-profiles-daemon.service",
    ):
        rc, _ = cmd(["systemctl", "is-active", "--quiet", service])
        print(f"  {service:40} {'active' if rc == 0 else 'inactive'}")

    print("\nDaemon:")
    try:
        print(json.dumps(request({"cmd": "status"}), indent=2, ensure_ascii=False))
    except KNCError as exc:
        print(f"  ERRO: {exc}")

    hotkey_sock = Path(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    ) / "kaevryx-nitro-hotkey.sock"
    print(
        f"\nBridge hotkey: "
        f"{'OK' if hotkey_sock.exists() else 'AUSENTE'} "
        f"({hotkey_sock})"
    )

    print("\nDiagnóstico somente leitura concluído.")


if __name__ == "__main__":
    main()
