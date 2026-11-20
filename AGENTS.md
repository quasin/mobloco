# Mobloco

Single-file Python service that mirrors a list of RSS/Atom feeds into `data/`
on :00 and :30 UTC. `feedcopy.py` is the whole application; `install.sh` is the
only deployment path (systemd unit `mobloco-feeds.service` on an Ubuntu host).

## Dev environment

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt     # only feedparser, currently unused by code
mkdir -p data && printf 'name\turl\tdescription\n' > data/feeds.tsv
```

Python 3.10+ (`sys`/`pathlib` style, `X | None` hints). Stdlib only in practice:
`feedcopy.py` uses `urllib`, not `requests`/`feedparser`. Do not add a third-party
dependency without a reason — feeds are copied as raw bytes, never parsed.

## Run & test

```
python3 feedcopy.py --once          # single pass, exit 1 if any feed failed
python3 feedcopy.py                 # loop forever on :00/:30 UTC (what systemd runs)
python3 feedcopy.py --feeds path/to/feeds.tsv
python3 -m unittest discover -s tests        # full suite; unittest, not pytest
python3 -m unittest tests.test_feedcopy.CycleTests.test_failed_feed_keeps_previous_copy
```

No linter/formatter/CI config exists in the repo; don't invent one.

## Data layout

- `data/feeds.tsv` — `name<TAB>url<TAB>description`, optional header and `#` comments.
- `data/<domain><ext>` — mirrored feed, filename derived from URL host (`www.`
  stripped, port dropped) and path suffix (`.atom/.xml/.rss/.rdf`, else `.xml`).
- `data/logs.txt` — appended every run; also mirrored to stdout.

`data/` is gitignored and generated at runtime. Never commit it.

## Conventions

- Functions return values that callers can assert on (`run_cycle` → failure count,
  `main` → exit code), and logging goes through the module `log` logger with
  `OK    `/`FAIL  ` prefixes — keep that shape so tests and `logs.txt` stay readable.
- All timestamps are UTC (`UtcFormatter`, `utc_now`, `iso`); never use local time.
- Writes go through `write_atomic` (tmp file + `os.replace`) so a failed download
  never truncates the previous copy. Route any new file write the same way.
- Validate responses before storing: bodies not starting with `<` are rejected.
- Type hints and `from __future__ import annotations` in every module; docstrings
  on public functions.
- Commit messages: short sentence-case imperative, no prefix — `Add install.sh`,
  `Update README with requirements and installation steps`.

## Pitfalls

- The suite includes `test_real_feed_list_is_parseable`, which reads the real
  `data/feeds.tsv`. On a fresh clone that test errors with `FileNotFoundError`
  until you create `data/feeds.tsv` (header line is enough). All other tests pass
  without it.
- Tests stub network access by assigning `feedcopy.fetch = lambda ...` and restore
  it in `tearDown`; do real HTTP only in manual `--once` runs.
- `install.sh` runs `sudo apt install`, writes the systemd unit, and ends with
  `sudo reboot` — never run it as part of a dev check.
- `install.sh` truncates `data/feeds.tsv` when re-run; back up a populated feed
  list before reinstall.
- In daemon mode a cycle failure is caught and logged; a crash must not stop the
  scheduler (`run_forever`), so don't wrap the loop in tighter error handling.
- Host requirement quoted in the README is a deployment box (Ubuntu 26.04, port
  4011 open), not a dev constraint.
