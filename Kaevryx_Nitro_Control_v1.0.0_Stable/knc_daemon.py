#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from knc_common import (
    ACER_PROFILES,
    CONFIG_PATH,
    PLATFORM_PROFILE,
    PPD_MAP,
    SOCKET_PATH,
    STATE_PATH,
    VERSION,
    find_acer_hwmon,
    percent_to_pwm,
    profile_choices,
    read_int,
    read_profile,
    read_text,
)

stop_event = threading.Event()
state_lock = threading.RLock()

DEFAULT_CONFIG = {
    "allowed_uid": 1000,
    "allowed_gid": 1000,
    "fan_min_percent": 30,
    "fan_min_pwm": 77,
    "fan_max_pwm": 255,
    "safety_temp_mC": 85000,
    "safety_release_temp_mC": 78000,
    "critical_temp_mC": 93000,
    "fan_control_enabled": True,
    "force_fans_auto_on_daemon_start": True,
}

DEFAULT_STATE = {
    "profile_owner": "kde",
    "fan1_mode": "auto",
    "fan2_mode": "auto",
    "fan1_percent": 50,
    "fan2_percent": 50,
    "safety_override": False,
    "last_error": "",
}


def load_json(path: str, default: dict) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {**default, **data}
    except Exception:
        pass
    return dict(default)


config = load_json(CONFIG_PATH, DEFAULT_CONFIG)
state = load_json(STATE_PATH, DEFAULT_STATE)


def save_state() -> None:
    Path(STATE_PATH).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(STATE_PATH + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def run_quiet(args: list[str], timeout: float = 15.0) -> tuple[int, str]:
    try:
        p = subprocess.run(
            args, text=True, capture_output=True, timeout=timeout, check=False
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as exc:
        return 99, str(exc)


def ppd_active() -> bool:
    return (
        run_quiet(
            ["systemctl", "is-active", "--quiet", "power-profiles-daemon.service"]
        )[0]
        == 0
    )


def set_ppd(active: bool) -> tuple[bool, str]:
    action = "start" if active else "stop"
    rc, out = run_quiet(
        ["systemctl", action, "power-profiles-daemon.service"], timeout=20
    )
    return rc == 0, out


def write_sysfs(path: Path, value: str | int) -> None:
    path.write_text(f"{value}\n", encoding="ascii")


def wait_profile(expected: str, timeout: float = 1.5) -> str:
    deadline = time.monotonic() + timeout
    current = read_profile()
    while time.monotonic() < deadline:
        current = read_profile()
        if current == expected:
            break
        time.sleep(0.05)
    return current


def owner_set(owner: str) -> dict:
    if owner not in ("kde", "acer"):
        raise ValueError("owner deve ser 'kde' ou 'acer'")

    with state_lock:
        current = read_profile()

        if owner == "kde":
            ok, out = set_ppd(True)
            if not ok:
                raise RuntimeError(
                    f"Falha ao iniciar power-profiles-daemon: {out}"
                )

            # Quiet e balanced-performance não existem na API padrão do PPD.
            target = current if current in PPD_MAP else "balanced"
            rc, out = run_quiet(["powerprofilesctl", "set", PPD_MAP[target]])
            if rc != 0:
                raise RuntimeError(f"powerprofilesctl falhou: {out}")
        else:
            ok, out = set_ppd(False)
            if not ok:
                raise RuntimeError(
                    f"Falha ao parar power-profiles-daemon: {out}"
                )

        state["profile_owner"] = owner
        state["last_error"] = ""
        save_state()

    return {
        "owner": owner,
        "ppd_active": ppd_active(),
        "profile": read_profile(),
    }


def set_profile(profile: str) -> dict:
    if profile not in ACER_PROFILES:
        raise ValueError(f"Perfil inválido: {profile}")

    choices = profile_choices()
    if profile not in choices:
        raise RuntimeError(f"Firmware não anuncia o perfil '{profile}'")

    owner = str(state.get("profile_owner", "kde"))

    if owner == "kde":
        if profile not in PPD_MAP:
            raise RuntimeError(
                f"'{profile}' é um perfil Acer exclusivo. "
                "Selecione 'Controle Acer completo'."
            )
        rc, out = run_quiet(["powerprofilesctl", "set", PPD_MAP[profile]])
        if rc != 0:
            raise RuntimeError(f"powerprofilesctl falhou: {out}")
    else:
        if ppd_active():
            ok, out = set_ppd(False)
            if not ok:
                raise RuntimeError(f"Não foi possível isolar o PPD: {out}")
        write_sysfs(PLATFORM_PROFILE, profile)

    current = wait_profile(profile)
    if current != profile:
        raise RuntimeError(
            f"Firmware retornou '{current}' após solicitar '{profile}'"
        )

    with state_lock:
        state["last_error"] = ""
        save_state()

    return {"profile": current, "owner": owner}


def fan_paths() -> tuple[Path, dict[str, Path]]:
    h = find_acer_hwmon()
    if not h:
        raise RuntimeError("hwmon Acer não encontrado")

    paths = {
        "fan1_input": h / "fan1_input",
        "fan2_input": h / "fan2_input",
        "pwm1": h / "pwm1",
        "pwm2": h / "pwm2",
        "pwm1_enable": h / "pwm1_enable",
        "pwm2_enable": h / "pwm2_enable",
    }

    for key in ("pwm1", "pwm2", "pwm1_enable", "pwm2_enable"):
        if not paths[key].exists():
            raise RuntimeError(f"Interface ausente: {paths[key]}")

    return h, paths


def set_one_fan(
    fan: int,
    mode: str,
    percent: int | None = None,
    *,
    safety: bool = False,
) -> dict:
    if not config.get("fan_control_enabled", True):
        raise RuntimeError("Controle de ventoinha desativado")
    if fan not in (1, 2):
        raise ValueError("fan deve ser 1 ou 2")
    if mode not in ("auto", "manual", "turbo"):
        raise ValueError("modo deve ser auto, manual ou turbo")

    _, paths = fan_paths()
    enable = paths[f"pwm{fan}_enable"]
    pwm = paths[f"pwm{fan}"]

    if mode == "manual":
        if percent is None:
            percent = int(state.get(f"fan{fan}_percent", 50))
        min_percent = int(config.get("fan_min_percent", 30))
        percent = max(min_percent, min(100, int(percent)))
        raw = percent_to_pwm(
            percent, int(config.get("fan_min_pwm", 77))
        )
        raw = min(raw, int(config.get("fan_max_pwm", 255)))

        # acer-wmi hwmon: 1 = custom/manual
        write_sysfs(enable, 1)
        write_sysfs(pwm, raw)
        state[f"fan{fan}_percent"] = percent

    elif mode == "turbo":
        # acer-wmi hwmon: 0 = turbo
        write_sysfs(enable, 0)
        state[f"fan{fan}_percent"] = 100

    else:
        # acer-wmi hwmon: 2 = automatic
        write_sysfs(enable, 2)

    state[f"fan{fan}_mode"] = mode
    if not safety:
        state["safety_override"] = False
        state["last_error"] = ""
    save_state()

    return {
        "fan": fan,
        "mode": mode,
        "percent": state.get(f"fan{fan}_percent"),
    }


def set_fans(
    target: str,
    mode: str,
    percent: int | None = None,
    *,
    safety: bool = False,
) -> dict:
    with state_lock:
        if target == "both":
            r1 = set_one_fan(1, mode, percent, safety=safety)
            r2 = set_one_fan(2, mode, percent, safety=safety)
            return {"fan1": r1, "fan2": r2}

        try:
            fan = int(target)
        except Exception as exc:
            raise ValueError("fan deve ser 1, 2 ou both") from exc

        return set_one_fan(fan, mode, percent, safety=safety)


def set_manual_pair(fan1_percent: int, fan2_percent: int) -> dict:
    with state_lock:
        return {
            "fan1": set_one_fan(1, "manual", fan1_percent),
            "fan2": set_one_fan(2, "manual", fan2_percent),
        }


def cpu_temperature_mC() -> int | None:
    values: list[int] = []

    # Prefer coretemp package sensor.
    for hw in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        name = read_text(hw / "name")
        if name == "coretemp":
            for label in hw.glob("temp*_label"):
                if read_text(label) == "Package id 0":
                    inp = label.with_name(label.name.replace("_label", "_input"))
                    v = read_int(inp)
                    if v and 0 < v < 130000:
                        values.append(v)

    # Acer exposes thermal readings useful as fallback.
    h = find_acer_hwmon()
    if h:
        for temp in h.glob("temp*_input"):
            v = read_int(temp)
            if v and 0 < v < 130000:
                values.append(v)

    return max(values) if values else None


def gpu_temperature_mC() -> int | None:
    rc, out = run_quiet(
        [
            "nvidia-smi",
            "--query-gpu=temperature.gpu",
            "--format=csv,noheader,nounits",
        ],
        timeout=3,
    )
    if rc != 0:
        return None

    try:
        first = out.splitlines()[0].strip()
        return int(float(first) * 1000)
    except Exception:
        return None


def thermal_status() -> dict[str, int | None]:
    cpu = cpu_temperature_mC()
    gpu = gpu_temperature_mC()
    valid = [v for v in (cpu, gpu) if v is not None]
    return {
        "cpu_temp_mC": cpu,
        "gpu_temp_mC": gpu,
        "max_control_temp_mC": max(valid) if valid else None,
    }


def status() -> dict[str, Any]:
    h = find_acer_hwmon()
    acer: dict[str, int | None] = {}

    if h:
        for name in (
            "fan1_input",
            "fan2_input",
            "temp1_input",
            "temp2_input",
            "temp3_input",
            "pwm1",
            "pwm2",
            "pwm1_enable",
            "pwm2_enable",
        ):
            p = h / name
            if p.exists():
                acer[name] = read_int(p)

    temps = thermal_status()

    return {
        "version": VERSION,
        "profile": read_profile(),
        "profile_choices": profile_choices(),
        "profile_owner": state.get("profile_owner", "kde"),
        "ppd_active": ppd_active(),
        "acer_hwmon": str(h) if h else None,
        "acer": acer,
        "fan1_mode": state.get("fan1_mode", "auto"),
        "fan2_mode": state.get("fan2_mode", "auto"),
        "fan1_percent": state.get("fan1_percent", 50),
        "fan2_percent": state.get("fan2_percent", 50),
        "fan_control_enabled": bool(config.get("fan_control_enabled", True)),
        "safety_override": bool(state.get("safety_override", False)),
        "last_error": state.get("last_error", ""),
        **temps,
    }


def safety_loop() -> None:
    threshold = int(config.get("safety_temp_mC", 85000))
    release = int(config.get("safety_release_temp_mC", 78000))
    critical = int(config.get("critical_temp_mC", 93000))

    while not stop_event.wait(2.0):
        try:
            thermal = thermal_status()
            temp = thermal["max_control_temp_mC"]
            if temp is None:
                continue

            manual = (
                state.get("fan1_mode") == "manual"
                or state.get("fan2_mode") == "manual"
            )

            if temp >= critical or (temp >= threshold and manual):
                with state_lock:
                    set_fans("both", "turbo", safety=True)
                    state["safety_override"] = True
                    state["last_error"] = (
                        f"Proteção térmica ativa: {temp / 1000:.1f} °C. "
                        "Ventoinhas forçadas para TURBO."
                    )
                    save_state()

            elif state.get("safety_override") and temp <= release:
                # Depois de um evento térmico, retornamos para AUTO em vez de
                # restaurar manual automaticamente.
                with state_lock:
                    set_fans("both", "auto", safety=True)
                    state["safety_override"] = False
                    state["last_error"] = ""
                    save_state()

        except Exception as exc:
            with state_lock:
                state["last_error"] = f"Safety watchdog: {exc}"
                save_state()


def initialize_hardware() -> None:
    with state_lock:
        state["safety_override"] = False
        state["last_error"] = ""

        if config.get("force_fans_auto_on_daemon_start", True):
            try:
                set_fans("both", "auto", safety=True)
            except Exception as exc:
                state["last_error"] = f"Inicialização das ventoinhas: {exc}"

        owner = str(state.get("profile_owner", "kde"))
        try:
            if owner == "acer":
                set_ppd(False)
            else:
                state["profile_owner"] = "kde"
                set_ppd(True)
        except Exception as exc:
            state["last_error"] = f"Inicialização de energia: {exc}"

        save_state()


def handle_request(req: dict) -> dict:
    cmd = req.get("cmd")

    if cmd == "ping":
        return {"ok": True, "version": VERSION}
    if cmd == "status":
        return {"ok": True, "data": status()}
    if cmd == "set_owner":
        return {
            "ok": True,
            "data": owner_set(str(req.get("owner", ""))),
        }
    if cmd == "set_profile":
        return {
            "ok": True,
            "data": set_profile(str(req.get("profile", ""))),
        }
    if cmd == "set_fan":
        return {
            "ok": True,
            "data": set_fans(
                str(req.get("fan", "")),
                str(req.get("mode", "")),
                req.get("percent"),
            ),
        }
    if cmd == "set_manual_pair":
        return {
            "ok": True,
            "data": set_manual_pair(
                int(req.get("fan1_percent", 50)),
                int(req.get("fan2_percent", 50)),
            ),
        }
    if cmd == "reset_fans":
        return {"ok": True, "data": set_fans("both", "auto")}
    if cmd == "clear_error":
        with state_lock:
            state["last_error"] = ""
            state["safety_override"] = False
            save_state()
        return {"ok": True, "data": status()}

    raise ValueError(f"Comando desconhecido: {cmd}")


def peer_uid(conn: socket.socket) -> int:
    raw = conn.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize("3i"),
    )
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def client_thread(conn: socket.socket) -> None:
    with conn:
        try:
            uid = peer_uid(conn)
            allowed_uid = int(config.get("allowed_uid", 1000))

            if uid not in (0, allowed_uid):
                conn.sendall(
                    json.dumps(
                        {"ok": False, "error": "Acesso negado"}
                    ).encode("utf-8")
                    + b"\n"
                )
                return

            data = b""
            while b"\n" not in data and len(data) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk

            if not data:
                return
            if len(data) >= 65536 and b"\n" not in data:
                raise ValueError("Requisição excedeu 64 KiB")

            req = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("Requisição deve ser um objeto JSON")

            try:
                resp = handle_request(req)
            except Exception as exc:
                with state_lock:
                    state["last_error"] = str(exc)
                    save_state()
                resp = {"ok": False, "error": str(exc)}

            conn.sendall(
                json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n"
            )

        except Exception as exc:
            try:
                conn.sendall(
                    json.dumps(
                        {"ok": False, "error": str(exc)},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    + b"\n"
                )
            except Exception:
                pass


def safe_shutdown() -> None:
    # Segurança térmica: daemon saindo => firmware reassume as ventoinhas.
    try:
        set_fans("both", "auto", safety=True)
    except Exception:
        pass


def on_signal(_sig, _frame) -> None:
    stop_event.set()


def main() -> None:
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    initialize_hardware()

    sock_path = Path(SOCKET_PATH)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)

    gid = int(config.get("allowed_gid", 1000))
    os.chown(SOCKET_PATH, 0, gid)
    os.chmod(SOCKET_PATH, 0o660)

    server.listen(16)
    server.settimeout(1.0)

    watchdog = threading.Thread(
        target=safety_loop,
        daemon=True,
        name="knc-safety-watchdog",
    )
    watchdog.start()

    try:
        while not stop_event.is_set():
            try:
                conn, _ = server.accept()
                threading.Thread(
                    target=client_thread,
                    args=(conn,),
                    daemon=True,
                ).start()
            except socket.timeout:
                continue
    finally:
        safe_shutdown()
        server.close()
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
