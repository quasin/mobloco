"""Tests for feedcopy.py — run with: python3 -m unittest discover -s tests"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedcopy  # noqa: E402


class FilenameTests(unittest.TestCase):
    def test_rutracker(self):
        self.assertEqual(
            feedcopy.filename_for("https://feed.rutracker.cc/atom/f/0.atom"),
            "feed.rutracker.cc.atom",
        )

    def test_domain_without_suffix_defaults_to_xml(self):
        self.assertEqual(
            feedcopy.filename_for("https://example.org/feed/"), "example.org.xml"
        )

    def test_www_prefix_and_port_are_stripped(self):
        self.assertEqual(
            feedcopy.filename_for("http://www.example.org:8080/rss"),
            "example.org.xml",
        )

    def test_rss_suffix_is_kept(self):
        self.assertEqual(
            feedcopy.filename_for("https://news.example.net/top.rss"),
            "news.example.net.rss",
        )

    def test_missing_host(self):
        with self.assertRaises(ValueError):
            feedcopy.filename_for("not-a-url")


class ReadFeedsTests(unittest.TestCase):
    def write(self, text: str) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "feeds.tsv"
        tmp.write_text(text, encoding="utf-8")
        return tmp

    def test_header_is_skipped(self):
        path = self.write(
            "# comment\n"
            "name\turl\tdescription\n"
            "RuTracker.Org\thttps://feed.rutracker.cc/atom/f/0.atom\tВсе разделы\n"
        )
        feeds = feedcopy.read_feeds(path)
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0].name, "RuTracker.Org")
        self.assertEqual(feeds[0].url, "https://feed.rutracker.cc/atom/f/0.atom")
        self.assertEqual(feeds[0].description, "Все разделы")

    def test_missing_description_and_blank_lines(self):
        path = self.write("name\turl\tdescription\n\nA\thttps://a.example/f.xml\n")
        feeds = feedcopy.read_feeds(path)
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0].description, "")

    def test_broken_row_is_skipped(self):
        path = self.write("name\turl\tdescription\nonly-name\n")
        self.assertEqual(feedcopy.read_feeds(path), [])

    def test_real_feed_list_is_parseable(self):
        feeds = feedcopy.read_feeds(feedcopy.FEEDS_FILE)
        self.assertTrue(feeds)
        self.assertTrue(all(f.url.startswith("http") for f in feeds))


class BoundaryTests(unittest.TestCase):
    def test_boundaries(self):
        cases = {
            "2026-11-18T07:00:00Z": "2026-11-18T07:30:00Z",
            "2026-11-18T07:00:01Z": "2026-11-18T07:30:00Z",
            "2026-11-18T07:29:59Z": "2026-11-18T07:30:00Z",
            "2026-11-18T07:30:00Z": "2026-11-18T08:00:00Z",
            "2026-11-18T07:45:00Z": "2026-11-18T08:00:00Z",
            "2026-11-18T23:59:59Z": "2026-11-19T00:00:00Z",
        }
        for now, expected in cases.items():
            with self.subTest(now=now):
                moment = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
                self.assertEqual(
                    feedcopy.next_boundary(moment).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    expected,
                )

    def test_next_boundary_is_never_in_the_past(self):
        moment = datetime(2026, 11, 18, 7, 29, 59, 999999, tzinfo=timezone.utc)
        self.assertGreater(feedcopy.next_boundary(moment), moment)

    def test_naive_local_time_is_treated_as_now(self):
        moment = datetime(2026, 11, 18, 7, 10)
        boundary = feedcopy.next_boundary(moment)
        self.assertEqual(boundary.tzinfo, timezone.utc)
        self.assertGreater(boundary, moment.replace(tzinfo=timezone.utc) - timedelta(1))


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        feedcopy.setup_logging(self.tmp / "logs.txt")
        self.original_fetch = feedcopy.fetch

    def tearDown(self):
        feedcopy.fetch = self.original_fetch

    def feeds_file(self, text: str) -> Path:
        path = self.tmp / "feeds.tsv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_cycle_writes_domain_named_file_and_logs(self):
        feedcopy.fetch = lambda url, timeout=0: b"<feed><title>x</title></feed>"
        path = self.feeds_file(
            "name\turl\tdescription\n"
            "RuTracker.Org\thttps://feed.rutracker.cc/atom/f/0.atom\tall\n"
        )
        self.assertEqual(feedcopy.run_cycle(path, self.tmp), 0)
        written = self.tmp / "feed.rutracker.cc.atom"
        self.assertEqual(written.read_bytes(), b"<feed><title>x</title></feed>")
        self.assertIn("RuTracker.Org", (self.tmp / "logs.txt").read_text())
        self.assertFalse(list(self.tmp.glob("*.tmp")))

    def test_failed_feed_keeps_previous_copy(self):
        path = self.feeds_file("name\turl\tdescription\nA\thttps://a.example/f.atom\n")
        (self.tmp / "a.example.atom").write_bytes(b"<old/>")

        def boom(url, timeout=0):
            raise feedcopy.urllib.error.URLError("connection refused")

        feedcopy.fetch = boom
        self.assertEqual(feedcopy.run_cycle(path, self.tmp), 1)
        self.assertEqual((self.tmp / "a.example.atom").read_bytes(), b"<old/>")

    def test_non_xml_response_is_rejected(self):
        path = self.feeds_file("name\turl\tdescription\nA\thttps://a.example/f.atom\n")
        feedcopy.fetch = lambda url, timeout=0: b"<html>blocked</html>"
        # HTML also starts with '<', so use a plainly non-XML body instead.
        feedcopy.fetch = lambda url, timeout=0: b'{"blocked": true}'
        self.assertEqual(feedcopy.run_cycle(path, self.tmp), 1)
        self.assertFalse((self.tmp / "a.example.atom").exists())


if __name__ == "__main__":
    unittest.main()
