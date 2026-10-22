#!/usr/bin/env python3
from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import Optional

VERSION = "1.0.0"

PLATFORM_PROFILE = Path("/sys/firmware/acpi/platform_profile")
PLATFORM_CHOICES = Path("/sys/firmware/acpi/platform_profile_choices")
SOCKET_PATH = "/run/kaevryx-nitro-control/control.sock"
CONFIG_PATH = "/etc/kaevryx-nitro-control.json"
STATE_PATH = "/var/lib/kaevryx-nitro-control/state.json"

ACER_PROFILES = (
    "low-power",
    "quiet",
    "balanced",
    "balanced-performance",
    "performance",
)

PPD_MAP = {
    "low-power": "power-saver",
    "balanced": "balanced",
    "performance": "performance",
}


def read_text(path: os.PathLike | str, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return default


def read_int(path: os.PathLike | str) -> Optional[int]:
    try:
        return int(read_text(path))
    except Exception:
        return None


def find_acer_hwmon() -> Optional[Path]:
    for item in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        p = Path(item)
        if read_text(p / "name") == "acer":
            return p
    return None


def read_profile() -> str:
    return read_text(PLATFORM_PROFILE, "unknown")


def profile_choices() -> list[str]:
    s = read_text(PLATFORM_CHOICES)
    return s.split() if s else []


def percent_to_pwm(percent: int, minimum_pwm: int = 77) -> int:
    percent = max(0, min(100, int(percent)))
    raw = round(percent * 255 / 100)
    if percent > 0:
        raw = max(minimum_pwm, raw)
    return max(0, min(255, raw))
