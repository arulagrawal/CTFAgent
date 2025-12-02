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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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
        default="gpt-5-mini",
        help="Model name to pass through to the backend solver.",
    )
    parser.add_argument(
        "--reasoning-effort",
        default="low",
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
        print(path)
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


def render_handout_files(
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
    handout_path: Path,
    challenge: Dict[str, Any],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    url = f"{backend_url.rstrip('/')}/challenges/start"
    payload: Dict[str, Any] = {
        "challenge_name": challenge.get("name", f"Challenge {challenge.get('id')}"),
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "timeout": str(args.backend_timeout),
    }

    challenge_type = map_category_to_challenge_type(challenge.get("category"))
    if challenge_type:
        payload["challenge_type"] = challenge_type

    if args.flag_format:
        payload["flag_format"] = args.flag_format

    if args.send_description:
        description = challenge.get("description")
        if description:
            payload["description"] = description

    with handout_path.open("rb") as handle:
        files = [
            (
                "handouts",
                (
                    handout_path.name,
                    handle,
                    "text/markdown",
                ),
            )
        ]
        response = requests.post(url, data=payload, files=files, timeout=60)
    response.raise_for_status()
    return response.json()


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

    eligible = [ch for ch in challenges if not ch.get("files")]
    logger.info("%d challenges have no attached files", len(eligible))

    if not eligible:
        logger.info("Nothing to do.")
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

        handout_map = render_handout_files(eligible, temp_dir)

        stats = {
            "eligible": len(eligible),
            "launched": 0,
            "failed": 0,
            "skipped": len(challenges) - len(eligible),
        }

        launched = 0

        for challenge in sorted(
            eligible,
            key=lambda c: int(c.get("id") or 0),  # stable ordering
        ):
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
            handout_path = handout_map.get(challenge_id)
            if not handout_path:
                logger.warning(
                    "Missing handout for challenge %s (%s)", challenge_id, name
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
                    handout_path,
                    challenge,
                    args,
                )
                stats["launched"] += 1
                launched += 1
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

        logger.info(
            "Summary: %d eligible, %d launched, %d failed, %d skipped",
            stats["eligible"],
            stats["launched"],
            stats["failed"],
            stats["skipped"],
        )

        if args.keep_files:
            logger.info("Handout files kept at %s", temp_dir)

        exit_code = 0 if stats["failed"] == 0 else 2

    return exit_code


if __name__ == "__main__":
    sys.exit(main())


