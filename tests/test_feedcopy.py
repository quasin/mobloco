"""Tests for feedcopy.py — run with: python3 -m unittest discover -s tests"""

from __future__ import annotations

import csv
import re
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


class EntriesTests(unittest.TestCase):
    ATOM = (
        "<feed xmlns='http://www.w3.org/2005/Atom'><title>t</title>"
        "<entry><title>First</title><link href='https://a.example/1'/>"
        "<updated>2026-11-21T01:18:42+00:00</updated>"
        "<category term='f-1614' label='Англия'/></entry>"
        "<entry><title>Second</title><link rel='enclosure' href='magnet:?xt=1'/>"
        "<link href='https://a.example/2'/>"
        "<updated>2026-11-20T10:00:00Z</updated></entry>"
        "</feed>"
    )
    STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        feedcopy.setup_logging(self.tmp / "logs.txt")
        self.feed = feedcopy.Feed("A", "https://a.example/f.atom")
        self.mirror = self.tmp / "a.example.atom"
        self.tsv = self.tmp / "feeds" / "a.example.tsv"

    def store(self, body: bytes) -> int:
        self.mirror.write_bytes(body)
        return feedcopy.update_entries(self.feed, self.tmp)[1]

    def rows(self) -> list[list[str]]:
        text = self.tsv.read_text(encoding="utf-8")
        return list(csv.reader(text.splitlines(), delimiter="\t"))

    def lines(self) -> list[str]:
        return self.tsv.read_text(encoding="utf-8").splitlines()

    def assert_stamp(self, value: str) -> None:
        self.assertRegex(value, self.STAMP)

    def test_header_has_category_and_added(self):
        self.store(self.ATOM.encode())
        self.assertEqual(self.rows()[0], ["date", "title", "link", "category", "added"])

    def test_atom_entries_are_written_with_utc_dates_and_category(self):
        self.assertEqual(self.store(self.ATOM.encode()), 2)
        rows = self.rows()
        self.assertEqual(
            rows[1][:4], ["2026-11-21T01:18:42Z", "First", "https://a.example/1", "Англия"]
        )
        self.assertEqual(
            rows[2][:4], ["2026-11-20T10:00:00Z", "Second", "https://a.example/2", ""]
        )
        self.assert_stamp(rows[1][4])
        self.assert_stamp(rows[2][4])

    def test_second_identical_run_adds_nothing_and_keeps_added(self):
        self.store(self.ATOM.encode())
        first_added = [row[4] for row in self.rows()[1:]]
        self.assertEqual(self.store(self.ATOM.encode()), 0)
        self.assertEqual(len(self.lines()), 3)
        self.assertEqual([row[4] for row in self.rows()[1:]], first_added)

    def test_new_entries_are_prepended(self):
        self.store(self.ATOM.encode())
        newer = (
            "<feed xmlns='http://www.w3.org/2005/Atom'>"
            "<entry><title>Third</title><link href='https://a.example/3'/>"
            "<updated>2026-11-21T02:00:00Z</updated></entry>"
            "</feed>"
        )
        self.assertEqual(self.store(newer.encode()), 1)
        self.assertEqual(
            self.rows()[1][:4],
            ["2026-11-21T02:00:00Z", "Third", "https://a.example/3", ""],
        )

    def test_duplicate_links_inside_one_feed_are_collapsed(self):
        doubled = self.ATOM.replace(
            "</feed>",
            "<entry><title>First again</title><link href='https://a.example/1'/>"
            "</entry></feed>",
        )
        self.assertEqual(self.store(doubled.encode()), 2)

    def test_rss_items_and_windows_1251_are_parsed(self):
        rss = (
            '<?xml version="1.0" encoding="windows-1251"?>'
            "<rss><channel><title>t</title>"
            "<item><title>Привет</title><pubDate>Sat, 21 Nov 2026 02:28:41 +0300</pubDate>"
            "<link>https://b.example/1</link><category>Фильмы</category></item>"
            "</channel></rss>"
        )
        feed = feedcopy.Feed("B", "https://b.example/rss.xml")
        (self.tmp / "b.example.xml").write_bytes(rss.encode("windows-1251"))
        feedcopy.update_entries(feed, self.tmp)
        rows = list(
            csv.reader(
                (self.tmp / "feeds" / "b.example.tsv")
                .read_text(encoding="utf-8")
                .splitlines(),
                delimiter="\t",
            )
        )
        self.assertEqual(
            rows[1][:4], ["2026-11-20T23:28:41Z", "Привет", "https://b.example/1", "Фильмы"]
        )

    def test_short_plain_description_is_used_as_category(self):
        body = (
            "<rss><channel><item><title>Game</title><link>https://r.example/1</link>"
            "<description>Игры</description></item></channel></rss>"
        ).encode()
        self.store(body)
        self.assertEqual(self.rows()[1][3], "Игры")

    def test_html_description_is_not_used_as_category(self):
        body = (
            "<rss><channel><item><title>Game</title><link>https://r.example/2</link>"
            "<description>&lt;a href='x'&gt;long html blurb&lt;/a&gt;</description>"
            "</item></channel></rss>"
        ).encode()
        self.store(body)
        self.assertEqual(self.rows()[1][3], "")

    def test_tabs_and_newlines_inside_titles_are_flattened(self):
        body = self.ATOM.replace("First", "Bad\ttitle\nhere").encode()
        self.store(body)
        self.assertEqual(len(self.rows()[1]), 5)
        self.assertIn("Bad title here", self.rows()[1][1])

    def test_titles_without_links_still_deduplicate(self):
        body = (
            "<feed xmlns='http://www.w3.org/2005/Atom'>"
            "<entry><title>No link</title></entry></feed>"
        ).encode()
        self.assertEqual(self.store(body), 1)
        self.assertEqual(self.store(body), 0)

    def test_three_column_file_is_migrated(self):
        self.tsv.parent.mkdir(parents=True, exist_ok=True)
        self.tsv.write_text(
            "date\ttitle\tlink\n"
            "2026-11-21T01:18:42Z\tFirst\thttps://a.example/1\n",
            encoding="utf-8",
        )
        only_first = (
            "<feed xmlns='http://www.w3.org/2005/Atom'>"
            "<entry><title>First</title><link href='https://a.example/1'/>"
            "<category term='f-1614' label='Англия'/>"
            "<updated>2026-11-21T01:18:42Z</updated></entry></feed>"
        ).encode()
        self.assertEqual(self.store(only_first), 0)
        rows = self.rows()
        self.assertEqual(rows[0], ["date", "title", "link", "category", "added"])
        self.assertEqual(
            rows[1][:4],
            ["2026-11-21T01:18:42Z", "First", "https://a.example/1", "Англия"],
        )
        self.assert_stamp(rows[1][4])
        self.assertEqual(len(rows), 2)

    def test_run_cycle_populates_the_feeds_folder(self):
        path = self.tmp / "feeds.tsv"
        path.write_text(
            "name\turl\tdescription\nA\thttps://a.example/f.atom\n", encoding="utf-8"
        )
        original = feedcopy.fetch
        feedcopy.fetch = lambda url, timeout=0: self.ATOM.encode()
        try:
            self.assertEqual(feedcopy.run_cycle(path, self.tmp), 0)
        finally:
            feedcopy.fetch = original
        self.assertTrue(self.tsv.exists())
        self.assertIn("+2 new", (self.tmp / "logs.txt").read_text())
        self.assertFalse(list((self.tmp / "feeds").glob("*.tmp")))

    def test_unparseable_mirror_counts_as_failure(self):
        path = self.tmp / "feeds.tsv"
        path.write_text(
            "name\turl\tdescription\nA\thttps://a.example/f.atom\n", encoding="utf-8"
        )
        original = feedcopy.fetch
        feedcopy.fetch = lambda url, timeout=0: b"<feed><broken>"
        try:
            self.assertEqual(feedcopy.run_cycle(path, self.tmp), 1)
        finally:
            feedcopy.fetch = original


if __name__ == "__main__":
    unittest.main()
