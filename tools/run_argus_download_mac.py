#!/usr/bin/env python3
"""Mac launcher: read the IMAP password from macOS Keychain, then run the downloader."""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(root / "argus-download.json"))
    parser.add_argument("--state", default=str(root / "argus-download-state.json"))
    parser.add_argument("--log", default=str(root / "argus-download.log"))
    parser.add_argument("--keychain-service", default="argus-imap")
    parser.add_argument("--first-run", action="store_true")
    args = parser.parse_args()

    user = os.environ.get("ARGUS_IMAP_USER", "").strip()
    if not user:
        print("Set ARGUS_IMAP_USER before running.", file=sys.stderr)
        return 2
    try:
        password = subprocess.check_output(
            ["security", "find-generic-password", "-s", args.keychain_service,
             "-a", user, "-w"], text=True, stderr=subprocess.DEVNULL).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("IMAP password not found in macOS Keychain.", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["ARGUS_IMAP_USER"] = user
    env["ARGUS_IMAP_PASSWORD"] = password
    command = [sys.executable, str(root / "download_argus_cdi.py"),
               "--config", args.config, "--state", args.state, "--log", args.log]
    if args.first_run:
        command.append("--first-run")
    try:
        return subprocess.run(command, env=env, check=False).returncode
    finally:
        env["ARGUS_IMAP_PASSWORD"] = ""


if __name__ == "__main__":
    raise SystemExit(main())
