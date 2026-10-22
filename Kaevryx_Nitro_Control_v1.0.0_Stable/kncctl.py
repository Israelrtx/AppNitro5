#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys

from knc_client import KNCError, request
from knc_common import VERSION


def emit(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(
        prog="kncctl",
        description="Kaevryx Nitro Control CLI",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")
    sub.add_parser("ping")
    sub.add_parser("reset-fans")
    sub.add_parser("clear-error")

    owner = sub.add_parser("owner")
    owner.add_argument("mode", choices=["kde", "acer"])

    profile = sub.add_parser("profile")
    profile.add_argument(
        "profile",
        choices=[
            "low-power",
            "quiet",
            "balanced",
            "balanced-performance",
            "performance",
        ],
    )

    fan = sub.add_parser("fan")
    fan.add_argument("fan", choices=["1", "2", "both"])
    fan.add_argument("mode", choices=["auto", "manual", "turbo"])
    fan.add_argument("percent", nargs="?", type=int)

    args = parser.parse_args()

    try:
        if args.command == "status":
            data = request({"cmd": "status"})
        elif args.command == "ping":
            data = request({"cmd": "ping"})
        elif args.command == "reset-fans":
            data = request({"cmd": "reset_fans"})
        elif args.command == "clear-error":
            data = request({"cmd": "clear_error"})
        elif args.command == "owner":
            data = request({"cmd": "set_owner", "owner": args.mode})
        elif args.command == "profile":
            data = request(
                {"cmd": "set_profile", "profile": args.profile}
            )
        else:
            data = request(
                {
                    "cmd": "set_fan",
                    "fan": args.fan,
                    "mode": args.mode,
                    "percent": args.percent,
                }
            )

        emit(data)
        return 0

    except KNCError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
