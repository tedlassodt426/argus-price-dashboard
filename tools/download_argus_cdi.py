import argparse
import datetime as dt
import email
from email.header import decode_header, make_header
import imaplib
import json
import os
from pathlib import Path
import re
import sys


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def text(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def log(log_path: Path, message: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"downloaded_files": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("downloaded_files", [])
        return state
    except Exception:
        return {"downloaded_files": []}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def imap_date(days_back: int) -> str:
    since = dt.date.today() - dt.timedelta(days=days_back)
    return since.strftime("%d-%b-%Y")


def imap_start_date(config: dict, days_back: int) -> str:
    """首次迁移可设置 handover_date，避免接手设备回补更早邮件。"""
    since = dt.date.today() - dt.timedelta(days=days_back)
    handover = str(config.get("handover_date", "")).strip()
    if handover:
        try:
            since = max(since, dt.date.fromisoformat(handover))
        except ValueError:
            raise ValueError("handover_date must use YYYY-MM-DD")
    return since.strftime("%d-%b-%Y")


def safe_write(path: Path, payload: bytes) -> bool:
    if path.exists():
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(payload)
    tmp.replace(path)
    return True


def publication_rules(config: dict) -> list[dict]:
    configured = config.get("publications")
    if not configured:
        configured = [{
            "code": "cdi",
            "subject_contains": config.get("subject_contains", ""),
            "attachment_regex": config["attachment_regex"],
            "download_folder": config["download_folder"],
        }]

    rules = []
    for item in configured:
        code = str(item["code"]).lower()
        folder = Path(item["download_folder"])
        folder.mkdir(parents=True, exist_ok=True)
        rules.append({
            "code": code,
            "subject_contains": str(item.get("subject_contains", "")).lower(),
            "regex": re.compile(item["attachment_regex"], re.IGNORECASE),
            "download_folder": folder,
        })
    return rules


def main() -> int:
    parser = argparse.ArgumentParser(description="Download configured Argus PDF attachments from IMAP.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--first-run", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config)
    state_path = Path(args.state)
    log_path = Path(args.log)
    config = load_config(config_path)
    state = load_state(state_path)

    user = os.environ.get("ARGUS_IMAP_USER")
    password = os.environ.get("ARGUS_IMAP_PASSWORD")
    if not user or not password:
        log(log_path, "ERROR: Missing ARGUS_IMAP_USER or ARGUS_IMAP_PASSWORD.")
        return 2

    rules = publication_rules(config)
    sender_domain = config["sender_domain"].lower()
    days_back = int(config["lookback_days_first_run"] if args.first_run else config["lookback_days_normal"])

    downloaded = 0
    skipped = 0
    matched_messages = 0
    scanned_messages = 0
    downloaded_by_publication = {rule["code"]: 0 for rule in rules}

    try:
        log(log_path, f"Connecting to {config['imap_server']}:{config['imap_port']} as {user}")
        with imaplib.IMAP4_SSL(config["imap_server"], int(config["imap_port"])) as imap:
            imap.login(user, password)
            imap.select(config.get("mailbox", "INBOX"))

            status, data = imap.uid("SEARCH", None, f'(SINCE "{imap_start_date(config, days_back)}")')
            if status != "OK":
                log(log_path, f"ERROR: IMAP search failed: {status}")
                return 3

            uids = data[0].split()
            log(log_path, f"Scanning {len(uids)} messages from the last {days_back} days")

            downloaded_files = set(state.get("downloaded_files", []))

            for uid in uids:
                scanned_messages += 1
                status, msg_data = imap.uid("FETCH", uid, "(RFC822)")
                if status != "OK" or not msg_data:
                    continue

                raw = next((part[1] for part in msg_data if isinstance(part, tuple)), None)
                if not raw:
                    continue

                msg = email.message_from_bytes(raw)
                from_header = text(msg.get("From")).lower()
                subject = text(msg.get("Subject"))
                subject_lower = subject.lower()

                if sender_domain not in from_header:
                    continue

                message_matched = False
                for part in msg.walk():
                    if part.is_multipart():
                        continue
                    filename = text(part.get_filename())
                    if not filename:
                        continue

                    rule = next((
                        candidate for candidate in rules
                        if candidate["regex"].fullmatch(filename)
                        and (
                            not candidate["subject_contains"]
                            or candidate["subject_contains"] in subject_lower
                        )
                    ), None)
                    if rule is None:
                        continue

                    if not message_matched:
                        matched_messages += 1
                        message_matched = True

                    target = rule["download_folder"] / filename
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue

                    if filename in downloaded_files or target.exists():
                        skipped += 1
                        downloaded_files.add(filename)
                        continue

                    if safe_write(target, payload):
                        downloaded += 1
                        downloaded_by_publication[rule["code"]] += 1
                        downloaded_files.add(filename)
                        log(log_path, f"Downloaded {filename} -> {rule['download_folder']}")

            state["downloaded_files"] = sorted(downloaded_files)
            state["last_run"] = dt.datetime.now().isoformat(timespec="seconds")
            save_state(state_path, state)
            imap.logout()

        breakdown = ",".join(
            f"{code}={count}" for code, count in downloaded_by_publication.items()
        )
        log(log_path, f"Done. scanned={scanned_messages}, matched_messages={matched_messages}, downloaded={downloaded}, skipped={skipped}, by_publication={breakdown}")
        return 0
    except imaplib.IMAP4.error as e:
        log(log_path, f"ERROR: IMAP login or mailbox error: {e}")
        return 4
    except Exception as e:
        log(log_path, f"ERROR: {type(e).__name__}: {e}")
        return 1
    finally:
        if password:
            os.environ["ARGUS_IMAP_PASSWORD"] = ""


if __name__ == "__main__":
    sys.exit(main())
