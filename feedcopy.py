#!/usr/bin/env python3
"""Mirror the feeds listed in data/feeds.tsv into data/ every 30 minutes (UTC).

Each feed is downloaded as-is (the raw XML is copied, not parsed) and written
to data/<domain>.<ext>, where <domain> comes from the feed URL and <ext> from
the URL path (defaults to .xml). Every run appends to data/logs.txt.

Usage:
    python3 feedcopy.py           # run forever, on :00 and :30 UTC
    python3 feedcopy.py --once    # one pass, then exit
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
FEEDS_FILE = DATA_DIR / "feeds.tsv"
LOG_FILE = DATA_DIR / "logs.txt"

HEADER = ("name", "url", "description")
FEED_SUFFIXES = {".atom", ".xml", ".rss", ".rdf"}
FETCH_TIMEOUT = 60
USER_AGENT = "Mobloco/0.0.1 (+https://github.com/quasin/mobloco)"
ACCEPT = (
    "application/atom+xml, application/rss+xml, application/xml, "
    "text/xml;q=0.9, */*;q=0.8"
)

log = logging.getLogger("mobloco.feeds")


class UtcFormatter(logging.Formatter):
    """Log timestamps in UTC regardless of the host timezone."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def setup_logging(log_file: Path = LOG_FILE) -> None:
    """Log to data/logs.txt and to stdout (for `journalctl -u mobloco-feeds`)."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    for handler in list(log.handlers):
        handler.close()
    log.handlers.clear()
    fmt = UtcFormatter("%(asctime)s %(levelname)-5s %(message)s")

    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    log.addHandler(stream_handler)


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    description: str = ""


def read_feeds(path: Path = FEEDS_FILE) -> list[Feed]:
    """Parse the TSV feed list: name<TAB>url<TAB>description."""
    feeds: list[Feed] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for lineno, row in enumerate(csv.reader(fh, delimiter="\t"), start=1):
            cells = [cell.strip() for cell in row]
            if not any(cells):
                continue
            if cells[0].startswith("#"):
                continue
            if cells[0].lower() == HEADER[0] and len(cells) > 1 and cells[1].lower() == HEADER[1]:
                continue
            if len(cells) < 2 or not cells[0] or not cells[1]:
                log.warning("%s:%d skipped: need at least name and url", path.name, lineno)
                continue
            feeds.append(Feed(cells[0], cells[1], cells[2] if len(cells) > 2 else ""))
    return feeds


def _safe_host(host: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789.-")
    return "".join(ch if ch in allowed else "_" for ch in host.lower())


def filename_for(url: str) -> str:
    """data/ file name for a feed URL: <domain><url suffix or .xml>."""
    parts = urlsplit(url)
    host = _safe_host(parts.hostname or "")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        raise ValueError(f"no host in URL: {url!r}")
    suffix = Path(parts.path).suffix.lower()
    if suffix not in FEED_SUFFIXES:
        suffix = ".xml"
    return f"{host}{suffix}"


def fetch(url: str, timeout: float = FETCH_TIMEOUT) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": ACCEPT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def write_atomic(path: Path, data: bytes) -> None:
    """Write through a temp file so a failed download never truncates the old copy."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def update_feed(feed: Feed, data_dir: Path = DATA_DIR) -> Path:
    """Fetch one feed and store it in data_dir; returns the written path."""
    target = data_dir / filename_for(feed.url)
    body = fetch(feed.url)
    if not body.lstrip().startswith(b"<"):
        raise ValueError(
            f"response is not XML ({len(body)} bytes, starts with {body[:32]!r})"
        )
    write_atomic(target, body)
    log.info(
        "OK    %s -> %s (%d bytes)",
        feed.name,
        target.relative_to(BASE_DIR) if target.is_relative_to(BASE_DIR) else target,
        len(body),
    )
    return target


def run_cycle(feeds_file: Path = FEEDS_FILE, data_dir: Path = DATA_DIR) -> int:
    """One pass over every feed; returns the number of failed feeds."""
    try:
        feeds = read_feeds(feeds_file)
    except OSError as exc:
        log.error("cannot read %s: %s", feeds_file, exc)
        return 1

    if not feeds:
        log.warning("no feeds found in %s", feeds_file)
        return 0

    failures = 0
    for feed in feeds:
        try:
            update_feed(feed, data_dir)
        except urllib.error.HTTPError as exc:
            failures += 1
            log.error("FAIL  %s: HTTP %s %s", feed.name, exc.code, exc.reason)
        except urllib.error.URLError as exc:
            failures += 1
            log.error("FAIL  %s: %s", feed.name, exc.reason)
        except (ValueError, OSError) as exc:
            failures += 1
            log.error("FAIL  %s: %s", feed.name, exc)

    log.info(
        "cycle done: %d ok, %d failed of %d", len(feeds) - failures, failures, len(feeds)
    )
    return failures


def next_boundary(now: datetime) -> datetime:
    """Next :00 or :30 UTC strictly after `now`."""
    now = now.astimezone(timezone.utc)
    candidate = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    while candidate <= now:
        candidate += timedelta(minutes=30)
    return candidate


def run_forever(feeds_file: Path = FEEDS_FILE, data_dir: Path = DATA_DIR) -> None:
    log.info("scheduler started: runs on :00 and :30 UTC, feeds=%s", feeds_file)
    while True:
        target = next_boundary(utc_now())
        log.info("next run at %s", iso(target))
        while True:
            remaining = (target - utc_now()).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 30.0))
        try:
            run_cycle(feeds_file, data_dir)
        except Exception:  # a broken cycle must not stop the scheduler
            log.exception("cycle crashed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mirror feeds from data/feeds.tsv into data/ on :00 and :30 UTC."
    )
    parser.add_argument(
        "--once", action="store_true", help="run a single pass and exit"
    )
    parser.add_argument(
        "--feeds", type=Path, default=FEEDS_FILE, help="feed list (default: data/feeds.tsv)"
    )
    args = parser.parse_args(argv)

    setup_logging()
    if args.once:
        failures = run_cycle(args.feeds)
        return 1 if failures else 0

    try:
        run_forever(args.feeds)
    except KeyboardInterrupt:
        log.info("stopped by user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
