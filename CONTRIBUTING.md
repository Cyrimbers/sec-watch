# Contributing

Bug reports and pull requests are welcome — particularly filings that this parses wrongly, since
that's where the real edge cases live.

## Reporting a parsing bug

The most useful report includes the **accession number** (e.g. `0001193125-26-397056`), what the
dashboard showed, and what the filing actually says. With the accession number I can reproduce it
exactly.

## Running it locally

```bash
pip install -r requirements-dev.txt
python tools/demo_seed.py
SECWATCH_DEMO=1 uvicorn app.main:app --port 8080
```

Demo mode needs no SEC access, so it's the fastest way to work on the dashboard.

## Before opening a PR

```bash
pytest -q
ruff check .
```

Both run in CI on Python 3.11 and 3.12 alongside a Docker build.

## Adding a test for a filing

Tests use fixture XML in `tests/fixtures/` following SEC's real ownership-document schema, so no
network access is needed. If you're fixing a parsing bug, add a fixture that reproduces it —
trimmed to the relevant elements, with any personal details replaced.

## Style

- Keep SEC's own vocabulary. A transaction code is "P", not "buy_type"; call it what the filing
  calls it.
- Comment the *why*, especially where EDGAR behaves unexpectedly. The timezone and transaction-code
  comments in `edgar.py` and `form4.py` exist because both cost real debugging time.
- Prefer a failing test over a description of the failure.
