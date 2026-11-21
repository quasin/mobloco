#!/usr/bin/env python3
"""Mirror the feeds listed in data/feeds.tsv into data/ every 30 minutes (UTC).

Each feed is downloaded as-is and written to data/<domain>.<ext>, where
<domain> comes from the feed URL and <ext> from the URL path (defaults to
.xml). After every successful copy the entries are extracted and merged into
data/feeds/<domain>.tsv as date/title/link/category/added rows -- new rows only,
so the TSV accumulates history and never repeats an entry. Every run appends to
data/logs.txt.

Usage:
    python3 feedcopy.py           # run forever, on :00 and :30 UTC
    python3 feedcopy.py --once    # one pass, then exit
"""

from __future__ import annotations

import argparse
import csv
import email.utils
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
FEEDS_FILE = DATA_DIR / "feeds.tsv"
LOG_FILE = DATA_DIR / "logs.txt"
ENTRIES_DIR = DATA_DIR / "feeds"

HEADER = ("name", "url", "description")
TSV_HEADER = ("date", "title", "link", "category", "added")
ATOM_NS = "http://www.w3.org/2005/Atom"
DC_NS = "http://purl.org/dc/elements/1.1/"
CATEGORY_MAX = 64
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


def host_for(url: str) -> str:
    """Sanitized host of a feed URL: lowercased, `www.` stripped, port dropped."""
    host = _safe_host(urlsplit(url).hostname or "")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        raise ValueError(f"no host in URL: {url!r}")
    return host


def filename_for(url: str) -> str:
    """data/ file name for a feed URL: <domain><url suffix or .xml>."""
    parts = urlsplit(url)
    host = host_for(url)
    suffix = Path(parts.path).suffix.lower()
    if suffix not in FEED_SUFFIXES:
        suffix = ".xml"
    return f"{host}{suffix}"


def entries_path(url: str, data_dir: Path = DATA_DIR) -> Path:
    """data/feeds/<domain>.tsv for a feed URL."""
    return data_dir / ENTRIES_DIR.name / f"{host_for(url)}.tsv"


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


@dataclass(frozen=True)
class Entry:
    """One feed entry as stored in data/feeds/<domain>.tsv.

    `date` is when the feed published the entry, `added` when the row was first
    written to this computer (both UTC ISO-8601).
    """

    date: str
    title: str
    link: str
    category: str = ""
    added: str = ""


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return " ".join((el.text or "").split())


def _cell(value: str) -> str:
    """Collapse tabs and newlines so a value can never break the TSV layout."""
    return " ".join(value.replace("\t", " ").split())


def normalize_date(raw: str) -> str:
    """Best-effort RFC 822 / ISO timestamp -> `YYYY-MM-DDTHH:MM:SSZ`.

    Unparseable values are kept verbatim so nothing is silently lost.
    """
    value = raw.strip()
    if not value:
        return ""
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        pass
    else:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return iso(moment)
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return iso(moment)


def category_for(node: ET.Element) -> str:
    """Feed-supplied category for an entry, joined if there are several.

    Atom uses <category term label>; RSS/RDF use <category> or dc:subject. Some
    feeds (rutor) carry the section in a short plain-text <description> instead.
    """
    values: list[str] = []
    for cat in node.findall(f"{{{ATOM_NS}}}category"):
        values.append(cat.get("label") or cat.get("term") or "")
    for cat in node.findall("category"):
        values.append(_text(cat))
    for cat in node.findall(f"{{{DC_NS}}}subject"):
        values.append(_text(cat))
    if not any(values):
        desc = node.find("description")
        raw = (desc.text or "").strip() if desc is not None else ""
        if raw and len(raw) <= CATEGORY_MAX and "<" not in raw:
            values.append(_text(desc))

    unique: list[str] = []
    for value in values:
        value = _cell(value)
        if value and value.casefold() not in {seen.casefold() for seen in unique}:
            unique.append(value)
    return ", ".join(unique)


def _atom_entry(node: ET.Element) -> Entry | None:
    title = _text(node.find(f"{{{ATOM_NS}}}title"))
    date = _text(node.find(f"{{{ATOM_NS}}}updated")) or _text(
        node.find(f"{{{ATOM_NS}}}published")
    )
    link = ""
    for link_node in node.findall(f"{{{ATOM_NS}}}link"):
        rel = (link_node.get("rel") or "alternate").lower()
        href = (link_node.get("href") or "").strip()
        if rel == "alternate" and href:
            link = href
            break
    if not link:
        link = _text(node.find(f"{{{ATOM_NS}}}id"))
    if not title and not link:
        return None
    return Entry(normalize_date(date), _cell(title), _cell(link), category_for(node))


def _rss_item(node: ET.Element, added: str = "") -> Entry | None:
    title = _text(node.find("title"))
    date = _text(node.find("pubDate")) or _text(node.find(f"{{{DC_NS}}}date"))
    link = _text(node.find("link")) or _text(node.find("guid"))
    if not title and not link:
        return None
    return Entry(
        normalize_date(date), _cell(title), _cell(link), category_for(node), added
    )


def parse_entries(body: bytes, added: str = "") -> list[Entry]:
    """Extract Atom entries or RSS/RDF items from a feed document."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"malformed XML: {exc}") from exc

    entries: list[Entry] = []
    for node in root.iter(f"{{{ATOM_NS}}}entry"):
        entry = _atom_entry(node)
        if entry is not None:
            entries.append(entry)
    if entries:
        return [replace(entry, added=added) for entry in entries]
    for node in root.iter("item"):
        entry = _rss_item(node, added)
        if entry is not None:
            entries.append(entry)
    return entries


def entry_key(entry: Entry) -> str:
    """Deduplication key: the link, or the title for link-less items."""
    return (entry.link or entry.title).strip().lower()


def read_entry_rows(path: Path) -> list[Entry]:
    """Existing rows of an entries TSV; header and ragged lines are skipped.

    Rows written by older versions (date/title/link only) come back with empty
    category and added so the caller can migrate them.
    """
    rows: list[Entry] = []
    if not path.exists():
        return rows
    with open(path, newline="", encoding="utf-8") as fh:
        for lineno, cells in enumerate(csv.reader(fh, delimiter="\t"), start=1):
            cells = [cell.strip() for cell in cells]
            if not any(cells):
                continue
            if cells[0].lower() == TSV_HEADER[0]:
                continue
            if len(cells) < 3:
                log.warning("%s:%d skipped: need date, title and link", path.name, lineno)
                continue
            category = cells[3] if len(cells) > 3 else ""
            added = cells[4] if len(cells) > 4 else ""
            rows.append(Entry(cells[0], cells[1], cells[2], category, added))
    return rows


def has_current_header(path: Path) -> bool:
    """True when the TSV already uses the date/title/link/category/added header."""
    if not path.exists():
        return True
    with open(path, newline="", encoding="utf-8") as fh:
        first = next(csv.reader(fh, delimiter="\t"), [])
    return [cell.strip() for cell in first] == list(TSV_HEADER)


def update_entries(feed: Feed, data_dir: Path = DATA_DIR) -> tuple[Path, int]:
    """Merge entries from the mirrored copy of `feed` into data/feeds/<domain>.tsv.

    Only rows whose link (or title, for link-less items) has not been stored
    before are added, so the file accumulates history without duplicates.
    Returns the TSV path and the number of rows added.
    """
    source = data_dir / filename_for(feed.url)
    stamp = iso(utc_now())
    entries = parse_entries(source.read_bytes(), stamp)
    target = entries_path(feed.url, data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = read_entry_rows(target)

    if rows and not has_current_header(target):
        # Migrate rows from the 3-column layout: the file's own mtime is when
        # they were recorded here, and the mirror still knows their category.
        written = iso(datetime.fromtimestamp(target.stat().st_mtime, timezone.utc))
        categories = {entry_key(entry): entry.category for entry in entries}
        rows = [
            Entry(
                row.date,
                row.title,
                row.link,
                row.category or categories.get(entry_key(row), ""),
                row.added or written,
            )
            for row in rows
        ]

    seen = {entry_key(row) for row in rows}
    fresh: list[Entry] = []
    for entry in entries:
        key = entry_key(entry)
        if not key or key in seen:
            continue
        seen.add(key)
        fresh.append(entry)

    lines = ["\t".join(TSV_HEADER)]
    for row in [*fresh, *rows]:
        lines.append("\t".join((row.date, row.title, row.link, row.category, row.added)))
    write_atomic(target, ("\n".join(lines) + "\n").encode("utf-8"))
    log.info(
        "TSV   %s -> %s (+%d new, %d total)",
        feed.name,
        target.relative_to(BASE_DIR) if target.is_relative_to(BASE_DIR) else target,
        len(fresh),
        len(fresh) + len(rows),
    )
    return target, len(fresh)


def update_feed(feed: Feed, data_dir: Path = DATA_DIR) -> Path:
    """Fetch one feed, store it in data_dir and merge its entries into the TSV."""
    target = data_dir / filename_for(feed.url)
    body = fetch(feed.url)
    if not body.lstrip().startswith(b"<"):
        raise ValueError(
            f"response is not XML ({len(body)} bytes, starts with {body[:32]!r})"
        )
    write_atomic(target, body)
    update_entries(feed, data_dir)
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
