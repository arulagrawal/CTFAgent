#!/usr/bin/env python3
"""
CTFd ingest helper that discovers description-only challenges and launches
solver sessions by uploading the description as a synthetic handout.

Example:
    python scripts/ctfd_ingest.py --token <CTFD_TOKEN> --ctfd-url https://demo.ctfd.io
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

import requests
from requests import Response

DEFAULT_CTFD_URL = "https://trainingmission.challengethecyber.nl/"
DEFAULT_BACKEND_URL = "http://localhost:8000"

CATEGORY_MAP = {
    "web": "web",
    "crypto": "crypto",
    "cryptography": "crypto",
    "rev": "reverse",
    "reverse": "reverse",
    "reverse engineering": "reverse",
    "pwn": "pwnable",
    "pwnable": "pwnable",
    "binary": "pwnable",
    "binary exploitation": "pwnable",
    "forensics": "forensics",
    "misc": "misc",
    "osint": "misc",
}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest description-only CTFd challenges and launch solver sessions."
    )
    parser.add_argument(
        "--ctfd-url",
        default=DEFAULT_CTFD_URL,
        help=f"Base URL for the CTFd instance (default: {DEFAULT_CTFD_URL})",
    )
    parser.add_argument(
        "--backend-url",
        default=DEFAULT_BACKEND_URL,
        help=f"Base URL for the CTFAgent backend (default: {DEFAULT_BACKEND_URL})",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("CTFD_TOKEN"),
        help="CTFd API token (defaults to CTFD_TOKEN env var)",
    )
    parser.add_argument(
        "--backend-timeout",
        type=int,
        default=600,
        help="Session timeout to request from the backend (seconds).",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL"),
        help="Model name to pass through to the backend solver.",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        help="Reasoning effort hint to send to the backend solver.",
    )
    parser.add_argument(
        "--flag-format",
        help="Optional flag regex to override the backend default.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not call the backend, just print the challenges that would be launched.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of challenges to launch (0 means unlimited).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to sleep between backend launches.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Seconds between backend status polls when monitoring sessions.",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=1800,
        help="Maximum seconds to wait for a session to finish before giving up.",
    )
    parser.add_argument(
        "--skip-poll",
        action="store_true",
        help="Do not monitor backend sessions after launch.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=8,
        help="Concurrency level for fetching challenge details from CTFd.",
    )
    parser.add_argument(
        "--keep-files",
        action="store_true",
        help="Keep the generated challenge.md files instead of deleting them.",
    )
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        help="Optional directory to place generated handout files (implies --keep-files).",
    )
    parser.add_argument(
        "--send-description",
        action="store_true",
        help="Also include the challenge description directly in the backend form post.",
    )
    parser.add_argument(
        "--submit-flags",
        action="store_true",
        help="Automatically submit discovered flags back to CTFd.",
    )
    parser.add_argument(
        "--manual-selection",
        action="store_true",
        help="Prompt to choose which eligible challenges to launch.",
    )
    parser.add_argument(
        "--include-handouts",
        action="store_true",
        help="Also include challenges that have file handouts; download and forward them to the backend. Default is off (description-only).",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification when talking to CTFd.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging output.",
    )
    return parser.parse_args(argv)


class CTFdClient:
    """Lightweight helper for talking to the CTFd API."""

    def __init__(self, base_url: str, token: str, verify_tls: bool = True):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {token}",
                "User-Agent": "ctf-agent-ingest/1.0",
            }
        )
        self.verify_tls = verify_tls

    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        response: Response = self.session.request(
            method, url, timeout=30, verify=self.verify_tls, **kwargs
        )
        response.raise_for_status()
        print(response.text)
        data = response.json()
        if isinstance(data, dict) and not data.get("success", True):
            detail = data.get("data") or data
            raise RuntimeError(f"CTFd API error for {path}: {detail}")
        return data

    def list_challenges(self) -> List[Dict[str, Any]]:
        payload = self._request("GET", "/api/v1/challenges")
        return payload.get("data", [])

    def get_challenge(self, challenge_id: int) -> Dict[str, Any]:
        payload = self._request("GET", f"/api/v1/challenges/{challenge_id}")
        return payload.get("data", {})

    def get_challenge_files(self, challenge_id: int) -> List[str]:
        payload = self._request("GET", f"/api/v1/challenges/{challenge_id}/files")
        data = payload.get("data", payload)
        if isinstance(data, list):
            return data
        return []

    def download_file(self, file_url: str, dest_path: Path) -> Path:
        """Download a single file from CTFd, preserving authorization."""
        url = file_url
        if not file_url.startswith("http"):
            url = f"{self.base_url}{file_url}"
        with self.session.get(url, timeout=60, verify=self.verify_tls, stream=True) as resp:
            resp.raise_for_status()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with dest_path.open("wb") as handle:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        handle.write(chunk)
        return dest_path

    def submit_flag(self, challenge_id: int, submission: str) -> Dict[str, Any]:
        """Submit a flag attempt for a specific challenge."""
        payload = {
            "endpoint": "/api/v1/challenges/attempt",
            "payload": {"challenge_id": challenge_id, "submission": submission},
        }
        response = self._request(
            "POST",
            payload["endpoint"],
            json=payload["payload"],
        )
        return response.get("data", response)


def slugify(value: str, fallback: str = "challenge") -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or fallback


def map_category_to_challenge_type(category: Optional[str]) -> Optional[str]:
    if not category:
        return None
    key = category.strip().lower()
    if key in CATEGORY_MAP:
        return CATEGORY_MAP[key]
    if key.endswith("s") and key[:-1] in CATEGORY_MAP:
        return CATEGORY_MAP[key[:-1]]
    if "web" in key:
        return "web"
    if "crypto" in key:
        return "crypto"
    if "rev" in key:
        return "reverse"
    if "pwn" in key or "binary" in key:
        return "pwnable"
    if "forensic" in key:
        return "forensics"
    return "misc"


def parse_connection_info(conn: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    """Extract host and port from a connection info string."""
    if not conn:
        return None, None
    text = conn.strip()
    # Patterns: "nc host port", "ncat host port", "netcat host port"
    m = re.search(r"\b(?:nc|ncat|netcat)\s+([^\s:]+)\s+(\d{1,5})\b", text, re.IGNORECASE)
    if m:
        host, port_str = m.group(1), m.group(2)
        try:
            return host, int(port_str)
        except ValueError:
            pass
    # Pattern: host:port
    m = re.search(r"\b([A-Za-z0-9.\-]+):(\d{1,5})\b", text)
    if m:
        host, port_str = m.group(1), m.group(2)
        try:
            return host, int(port_str)
        except ValueError:
            pass
    # Pattern: tcp://host:port
    m = re.search(r"tcp://([A-Za-z0-9.\-]+):(\d{1,5})", text)
    if m:
        host, port_str = m.group(1), m.group(2)
        try:
            return host, int(port_str)
        except ValueError:
            pass
    return None, None


def build_handout_content(challenge: Dict[str, Any]) -> str:
    lines: List[str] = []
    name = challenge.get("name") or f"Challenge {challenge.get('id')}"
    lines.append(f"# {name}")
    lines.append("")

    tags_field = challenge.get("tags") or []
    tag_values: List[str] = []
    if isinstance(tags_field, list):
        for tag in tags_field:
            if isinstance(tag, dict):
                value = tag.get("value")
            else:
                value = str(tag)
            if value:
                tag_values.append(value)

    metadata = {
        "Category": challenge.get("category") or "Unknown",
        "Value": challenge.get("value"),
        "Type": challenge.get("type"),
        "Connection Info": challenge.get("connection_info"),
        "Tags": ", ".join(tag_values),
    }

    for label, value in metadata.items():
        if value:
            lines.append(f"- {label}: {value}")

    if challenge.get("state"):
        lines.append(f"- State: {challenge['state']}")

    lines.append("")
    lines.append("## Description")
    lines.append("")

    description = challenge.get("description") or "_No description provided._"
    lines.append(description.strip())

    if challenge.get("hints"):
        lines.append("")
        lines.append("## Hints")
        for hint in challenge["hints"]:
            content = hint.get("content") or ""
            lines.append(f"- {content.strip()}")

    return "\n".join(lines)


def prompt_manual_selection(
    challenges: List[Dict[str, Any]], logger: logging.Logger
) -> List[Dict[str, Any]]:
    if not challenges:
        return challenges
    # Group by category, sort categories alphabetically, and sort items by (value, name)
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for ch in challenges:
        category = (ch.get("category") or "Uncategorized").strip() or "Uncategorized"
        grouped[category].append(ch)

    sorted_categories = sorted(grouped.keys(), key=lambda c: c.lower())

    flat_order: List[Dict[str, Any]] = []
    display_rows: List[str] = []
    idx = 1
    logger.info("Manual selection enabled. Grouped by category:")
    for cat in sorted_categories:
        items = grouped[cat]
        items.sort(key=lambda c: ((c.get("value") or 0), (c.get("name") or "").lower()))
        logger.info("=== %s ===", cat)
        for ch in items:
            cid = ch.get("id")
            name = ch.get("name") or f"Challenge {cid}"
            value = ch.get("value")
            value_str = f"{value}" if value is not None else "?"
            row = f"[{idx}] {name} (id={cid}, value={value_str}, category={cat})"
            display_rows.append(row)
            logger.info("%s", row)
            flat_order.append(ch)
            idx += 1

    def _fallback_text_input() -> List[Dict[str, Any]]:
        prompt = (
            "Enter indices or ranges (e.g., 1,3,5-7). "
            "You may also target challenge ids with 'id=<id>' (e.g., id=24). "
            "Use 'all' to run all, or press Enter to run all. "
            "Use 'none' to skip.\nSelection: "
        )
        try:
            selection_input = input(prompt)
        except EOFError:
            logger.warning("No input received; defaulting to run all eligible challenges.")
            return challenges

        selection_input = selection_input.strip()
        if not selection_input or selection_input.lower() in {"all", "a", "*"}:
            return challenges
        if selection_input.lower() in {"none", "n", "skip"}:
            return []

        chosen_indices: Set[int] = set()
        chosen_ids: Set[int] = set()

        parts = [p.strip() for p in selection_input.split(",") if p.strip()]
        max_index = len(flat_order)
        id_map: Dict[int, Dict[str, Any]] = {}
        for ch in flat_order:
            cid_raw = ch.get("id")
            if isinstance(cid_raw, (int, str)):
                try:
                    cid = int(cid_raw)
                    id_map[cid] = ch
                except Exception:
                    continue

        for part in parts:
            lower = part.lower()
            if lower in {"all", "a", "*"}:
                return challenges
            if lower in {"none", "n", "skip"}:
                return []
            if lower.startswith("id=") or lower.startswith("id:"):
                try:
                    cid = int(part.split("=", 1)[1] if "=" in part else part.split(":", 1)[1])
                    chosen_ids.add(cid)
                except Exception:
                    continue
                continue
            if "-" in part:
                try:
                    start_s, end_s = part.split("-", 1)
                    start = int(start_s)
                    end = int(end_s)
                    for i in range(start, end + 1):
                        if 1 <= i <= max_index:
                            chosen_indices.add(i)
                except ValueError:
                    continue
                continue
            try:
                num = int(part)
                if num in id_map:
                    chosen_ids.add(num)
                elif 1 <= num <= max_index:
                    chosen_indices.add(num)
            except ValueError:
                continue

        if not chosen_indices and not chosen_ids:
            logger.warning("No valid selections parsed; nothing will be launched.")
            return []

        selected: List[Dict[str, Any]] = []
        for idx, ch in enumerate(flat_order, start=1):
            cid_int: Optional[int] = None
            cid_val = ch.get("id")
            if isinstance(cid_val, (int, str)):
                try:
                    cid_int = int(cid_val)
                except Exception:
                    cid_int = None
            if idx in chosen_indices or (cid_int is not None and cid_int in chosen_ids):
                selected.append(ch)

        logger.info("Selected %d challenges to launch.", len(selected))
        return selected

    # Prefer InquirerPy checkbox UI if available and interactive
    try:
        from InquirerPy import inquirer  # type: ignore
        from InquirerPy.separator import Separator  # type: ignore
    except Exception:
        logger.info("InquirerPy not available; falling back to text input.")
        return _fallback_text_input()

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        logger.info("Non-interactive TTY detected; falling back to text input.")
        return _fallback_text_input()

    choices: List[Any] = []
    idx = 1
    for cat in sorted_categories:
        choices.append(Separator(f"-- {cat} --"))
        items = [c for c in grouped[cat]]
        items.sort(key=lambda c: ((c.get("value") or 0), (c.get("name") or "").lower()))
        for ch in items:
            cid = ch.get("id")
            name = ch.get("name") or f"Challenge {cid}"
            value = ch.get("value")
            value_str = f"{value}" if value is not None else "?"
            title = f"[{idx:02}] {name} (id={cid}, value={value_str}, category={cat})"
            choices.append({"name": title, "value": ch})
            idx += 1

    try:
        # Fuzzy prompt provides a search bar; multiselect enables checkbox-like behavior.
        # Separators are omitted in the fuzzy view to keep searching simple.
        search_choices = [c for c in choices if not isinstance(c, Separator)]
        selected = inquirer.fuzzy(  # type: ignore[attr-defined]
            message="Search & select challenges (space to toggle, enter to confirm):",
            choices=search_choices,
            multiselect=True,
            instruction="Type to search; Space=toggle; Enter=confirm",
            keybindings={"toggle": [{"key": " ", "modifiers": []}]},
            transformer=lambda result: f"{len(result)} selected",
        ).execute()
    except Exception as exc:
        logger.info("Interactive selection failed (%s); falling back to text input.", exc)
        return _fallback_text_input()

    if not selected:
        logger.info("No challenges selected.")
        return []

    logger.info("Selected %d challenges to launch.", len(selected))
    return selected


def fetch_full_challenges(
    client: CTFdClient, max_workers: int
) -> List[Dict[str, Any]]:
    base_list = client.list_challenges()
    challenges: List[Dict[str, Any]] = []

    def fetch(challenge_summary: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        challenge_id = challenge_summary.get("id")
        if challenge_id is None:
            return None
        detail = client.get_challenge(challenge_id)
        detail.setdefault("category", challenge_summary.get("category"))
        detail.setdefault("value", challenge_summary.get("value"))
        files = detail.get("files")
        if files is None:
            files = client.get_challenge_files(challenge_id)
        detail["files"] = files or []
        return detail

    if max_workers <= 1:
        for summary in base_list:
            try:
                detail = fetch(summary)
                if detail:
                    challenges.append(detail)
            except Exception as exc:
                logging.getLogger(__name__).error(
                    "Failed to fetch challenge %s: %s",
                    summary.get("id"),
                    exc,
                )
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(fetch, summary): summary for summary in base_list}
            for future in as_completed(futures):
                summary = futures[future]
                try:
                    detail = future.result()
                    if detail:
                        challenges.append(detail)
                except Exception as exc:
                    logging.getLogger(__name__).error(
                        "Failed to fetch challenge %s: %s",
                        summary.get("id"),
                        exc,
                    )

    return challenges


def render_description_handouts(
    challenges: List[Dict[str, Any]], base_dir: Path
) -> Dict[int, Path]:
    mapping: Dict[int, Path] = {}
    for challenge in challenges:
        raw_id = challenge.get("id")
        if raw_id is None:
            continue
        try:
            challenge_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        filename = f"{challenge_id}_{slugify(challenge.get('name', 'challenge'))}.md"
        content = build_handout_content(challenge)
        path = base_dir / filename
        path.write_text(content, encoding="utf-8")
        mapping[challenge_id] = path
    return mapping


def post_challenge_to_backend(
    backend_url: str,
    handout_paths: List[Path],
    challenge: Dict[str, Any],
    args: argparse.Namespace,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> Dict[str, Any]:
    url = f"{backend_url.rstrip('/')}/challenges/start"
    payload: Dict[str, Any] = {
        "challenge_name": challenge.get("name", f"Challenge {challenge.get('id')}"),
        **({"model": args.model} if args.model else {}),
        **({"reasoning_effort": args.reasoning_effort} if args.reasoning_effort else {}),
        "timeout": str(args.backend_timeout),
    }

    if host:
        payload["host"] = host
    if port:
        payload["port"] = str(port)

    challenge_type = map_category_to_challenge_type(challenge.get("category"))
    if challenge_type:
        payload["challenge_type"] = challenge_type

    if args.flag_format:
        payload["flag_format"] = args.flag_format

    if args.send_description:
        description = challenge.get("description")
        if description:
            payload["description"] = description

    with contextlib.ExitStack() as stack:
        files = []
        for path in handout_paths:
            handle = stack.enter_context(path.open("rb"))
            files.append(
                (
                    "handouts",
                    (
                        path.name,
                        handle,
                        "application/octet-stream",
                    ),
                )
            )
        response = requests.post(url, data=payload, files=files, timeout=60)
        response.raise_for_status()
        return response.json()


def fetch_session_status(backend_url: str, session_id: str) -> Dict[str, Any]:
    url = f"{backend_url.rstrip('/')}/challenges/{session_id}"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def poll_session_for_flag(
    backend_url: str,
    session_id: str,
    interval: float,
    timeout: int,
    logger: logging.Logger,
) -> Dict[str, Any]:
    deadline = time.time() + timeout
    last_data: Optional[Dict[str, Any]] = None
    while time.time() <= deadline:
        try:
            data = fetch_session_status(backend_url, session_id)
            last_data = data
        except Exception as exc:
            logger.warning("Failed to fetch status for session %s: %s", session_id, exc)
            time.sleep(interval)
            continue

        status = data.get("status")
        found_flag = data.get("found_flag")
        if found_flag:
            return {"status": status, "flag": found_flag, "data": data}
        if status in {"failed", "completed"}:
            return {"status": status, "flag": found_flag, "data": data}

        time.sleep(interval)

    logger.warning(
        "Timeout waiting for session %s to finish after %s seconds",
        session_id,
        timeout,
    )
    return {"status": "timeout", "flag": None, "data": last_data}


def download_challenge_files(
    client: CTFdClient,
    challenge: Dict[str, Any],
    base_dir: Path,
    logger: logging.Logger,
) -> List[Path]:
    downloaded: List[Path] = []
    files_field = challenge.get("files") or []
    if not isinstance(files_field, list):
        return downloaded

    for idx, file_url in enumerate(files_field, start=1):
        if not isinstance(file_url, str):
            continue
        name = Path(file_url).name
        if not name or "." not in name:
            name = f"{challenge.get('id')}_{idx}"
        dest_path = base_dir / name
        try:
            client.download_file(file_url, dest_path)
            downloaded.append(dest_path)
        except Exception as exc:
            logger.error(
                "Failed to download handout %s for challenge %s: %s",
                file_url,
                challenge.get("id"),
                exc,
            )

    return downloaded


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("ctfd-ingest")

    if not args.token:
        logger.error("A CTFd API token is required (use --token or set CTFD_TOKEN env).")
        return 1

    client = CTFdClient(
        args.ctfd_url,
        token=args.token,
        verify_tls=not args.insecure,
    )

    logger.info("Fetching challenges from %s", args.ctfd_url)
    challenges = fetch_full_challenges(client, args.max_workers)
    logger.info("Fetched %d total challenges", len(challenges))

    # Filter out already-solved challenges if the API provides this flag
    unsolved = [ch for ch in challenges if not ch.get("solved_by_me")]
    if len(unsolved) != len(challenges):
        logger.info(
            "Skipping %d challenges already solved by you",
            len(challenges) - len(unsolved),
        )
    challenges = unsolved

    if args.include_handouts:
        eligible = challenges
        logger.info(
            "%d total challenges considered (including those with handouts)",
            len(eligible),
        )
    else:
        eligible = [ch for ch in challenges if not ch.get("files")]
        logger.info("%d challenges have no attached files", len(eligible))

    if not eligible:
        logger.info("Nothing to do.")
        return 0

    if args.manual_selection:
        eligible = prompt_manual_selection(eligible, logger)
        if not eligible:
            logger.info("No challenges selected; exiting.")
            return 0

    exit_code = 0
    with contextlib.ExitStack() as stack:
        if args.tmp_dir:
            temp_dir = args.tmp_dir.expanduser().resolve()
            temp_dir.mkdir(parents=True, exist_ok=True)
            args.keep_files = True
        elif args.keep_files:
            temp_dir = Path(tempfile.mkdtemp(prefix="ctfd_ingest_"))
        else:
            tmp_name = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="ctfd_ingest_")
            )
            temp_dir = Path(tmp_name)

        logger.info("Writing temporary handouts to %s", temp_dir)

        desc_handout_map = render_description_handouts(eligible, temp_dir)

        session_records: List[Dict[str, Any]] = []
        stats = {
            "eligible": len(eligible),
            "launched": 0,
            "failed": 0,
            "skipped": len(challenges) - len(eligible),
            "flags_submitted": 0,
            "flags_correct": 0,
            "flags_incorrect": 0,
        }

        launched = 0

        if args.manual_selection:
            launch_list = eligible  # preserve the order shown to the user
        else:
            launch_list = sorted(
                eligible,
                key=lambda c: int(c.get("id") or 0),  # stable ordering
            )

        for challenge in launch_list:
            raw_id = challenge.get("id")
            if raw_id is None:
                logger.warning("Challenge missing id, skipping: %s", challenge.get("name"))
                continue
            try:
                challenge_id = int(raw_id)
            except (TypeError, ValueError):
                logger.warning(
                    "Challenge id not parseable (%s), skipping %s",
                    raw_id,
                    challenge.get("name"),
                )
                continue
            name = challenge.get("name")
            handout_paths: List[Path] = []
            desc_path = desc_handout_map.get(challenge_id)
            if desc_path:
                handout_paths.append(desc_path)

            socket_host: Optional[str] = None
            socket_port: Optional[int] = None
            conn_host, conn_port = parse_connection_info(challenge.get("connection_info"))
            if conn_host and conn_port:
                socket_host, socket_port = conn_host, conn_port
                logger.info(
                    "Detected socket target for challenge %s: %s:%s",
                    challenge_id,
                    socket_host,
                    socket_port,
                )

            if args.include_handouts and challenge.get("files"):
                downloads = download_challenge_files(
                    client,
                    challenge,
                    temp_dir / f"files_{challenge_id}",
                    logger,
                )
                handout_paths.extend(downloads)

            if not handout_paths:
                logger.warning(
                    "No handouts available for challenge %s (%s)", challenge_id, name
                )
                continue

            if args.limit and launched >= args.limit:
                logger.info("Launch limit reached (%d). Stopping.", args.limit)
                break

            logger.info(
                "Launching challenge %s (%s) via backend handout upload",
                challenge_id,
                name,
            )

            if args.dry_run:
                logger.info("Dry-run enabled; skipping backend call.")
                launched += 1
                if args.delay:
                    time.sleep(args.delay)
                continue

            try:
                response = post_challenge_to_backend(
                    args.backend_url,
                    handout_paths,
                    challenge,
                    args,
                    host=socket_host,
                    port=socket_port,
                )
                stats["launched"] += 1
                launched += 1
                session_id = response.get("session_id")
                if session_id:
                    session_records.append(
                        {
                            "session_id": session_id,
                            "challenge": challenge,
                        }
                    )
                logger.info(
                    "Backend accepted challenge %s (%s): %s",
                    challenge_id,
                    name,
                    json.dumps(response),
                )
            except Exception as exc:
                stats["failed"] += 1
                logger.error(
                    "Failed to launch challenge %s (%s): %s",
                    challenge_id,
                    name,
                    exc,
                )

            if args.delay:
                time.sleep(args.delay)

        if args.dry_run:
            stats["launched"] = launched

        if (
            not args.dry_run
            and not args.skip_poll
            and session_records
        ):
            logger.info(
                "Monitoring %d sessions for flags (interval %.1fs, timeout %ss)",
                len(session_records),
                args.poll_interval,
                args.poll_timeout,
            )
            for record in session_records:
                session_id = record["session_id"]
                challenge = record["challenge"]
                ctfd_id = challenge.get("id")
                result = poll_session_for_flag(
                    args.backend_url,
                    session_id,
                    args.poll_interval,
                    args.poll_timeout,
                    logger,
                )
                status = result.get("status")
                flag = result.get("flag")
                logger.info(
                    "Session %s finished with status %s%s",
                    session_id,
                    status,
                    f" and flag {flag}" if flag else "",
                )
                if not flag:
                    continue

                if not args.submit_flags:
                    logger.info(
                        "Flag submission disabled; skipping submit for challenge %s",
                        ctfd_id,
                    )
                    continue

                if ctfd_id is None:
                    logger.warning(
                        "Cannot submit flag for session %s without a CTFd challenge id",
                        session_id,
                    )
                    continue

                try:
                    submission_result = client.submit_flag(ctfd_id, flag)
                    stats["flags_submitted"] += 1
                    status_text = (
                        submission_result.get("status")
                        or submission_result.get("result")
                        or "unknown"
                    )
                    message_text = submission_result.get("message") or ""
                    status_lower = str(status_text).lower()
                    if status_lower == "correct":
                        stats["flags_correct"] += 1
                    elif status_lower == "incorrect":
                        stats["flags_incorrect"] += 1
                    logger.info(
                        "Submitted flag for challenge %s: status=%s message=%s",
                        ctfd_id,
                        status_text,
                        message_text,
                    )
                except Exception as exc:
                    stats["flags_incorrect"] += 1
                    logger.error(
                        "Flag submission failed for challenge %s (session %s): %s",
                        ctfd_id,
                        session_id,
                        exc,
                    )

        logger.info(
            (
                "Summary: %d eligible, %d launched, %d failed, %d skipped, "
                "%d flags submitted (%d correct, %d incorrect)"
            ),
            stats["eligible"],
            stats["launched"],
            stats["failed"],
            stats["skipped"],
            stats["flags_submitted"],
            stats["flags_correct"],
            stats["flags_incorrect"],
        )

        if args.keep_files:
            logger.info("Handout files kept at %s", temp_dir)

        exit_code = 0 if stats["failed"] == 0 else 2

    return exit_code


if __name__ == "__main__":
    sys.exit(main())


