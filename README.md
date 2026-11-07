# log-triage

**Turn a deploy's worth of logs into templates, then show only what is new — the lines that were not in the log before the deploy.**

[![CI](https://github.com/aiplusplus360/log-triage/actions/workflows/ci.yml/badge.svg)](https://github.com/aiplusplus360/log-triage/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

```console
$ log-triage diff after.log --baseline before.json --fail-on-new error
NEW (2)
  [error]    7  ERROR panic: database/sql: connection pool exhausted
                  e.g. ERROR panic: database/sql: connection pool exhausted
  [error]    1  ERROR GET /api/v2/reports/<id> <n> <dur>

LOUDER (0)
  no template grew significantly

GONE (0)
```

---

## Why this exists

A log is not interesting because it contains errors. If it is an error that the last release also logged forty thousand times, it is the background hum of the system and nobody will read it.

What matters is the **delta**: `panic: connection pool exhausted` appears nine times and was not there yesterday. That is the deploy. Everything else is noise that buries it.

Two mechanisms make the delta visible.

**Templating.** `GET /users/8812 200 13ms` and `GET /users/91 200 4ms` are the same event. Mask the variable parts — timestamps, UUIDs, IPs, hex, numbers, durations — and they collapse into one template with a count. This is the difference between 40,000 lines and 30 things that happened.

**A baseline.** Record the template set of a known-good run once. Every later run is reported as `new`, `louder`, and `gone`.

## The masking rules, and why order matters

The obvious implementation loops over a list of regexes and calls `sub()`. That produces values in *regex* order, not *line* order, and — worse — it lets a broad pattern eat a specific one. `took 13ms` must become `<dur>`, not `<n>ms`.

`template_of` collects every match from every pattern with its position, sorts them leftmost-longest, and applies the non-overlapping winners in one pass:

```python
>>> template_of("GET /users/8812 200 13ms")
('GET /users/<n> <n> <dur>', ['8812', '200', '13ms'])
```

Only the numeric patterns are greedy enough to collide; the `finditer` over a single pass also handles IPv4-with-port, MAC addresses and ANSI colour codes without leaving `\x1b[31m` in the template.

## Bursts without timestamps

A crash loop does not add a template. It makes an existing one louder. If a large fraction of the second half of a log is one template that was rare in the first half, that template is the problem — no parsing of log timestamps required:

```console
$ log-triage summary app.log
bursts (loud in the second half of the log):
  1412 lines  ERROR connect to <ip> refused
```

## Install and use

No dependencies. Python 3.9+.

```bash
pip install git+https://github.com/aiplusplus360/log-triage
```

```bash
log-triage summary app.log                       # templates by frequency
log-triage summary app.log --top 10 --format json
log-triage baseline app.log -o baseline.json     # record a good run
log-triage diff app.log --baseline baseline.json
log-triage diff app.log --baseline baseline.json --fail-on-new error
```

Exit codes: `0` clean, `1` a new template at or above `--fail-on-new`, `2` unreadable input.

In CI, the baseline is committed and the diff runs on the first log after a deploy:

```yaml
- run: log-triage diff "$LOG" --baseline .log-baseline.json --fail-on-new error
```

## Library use

```python
from logtriage import cluster, baseline_document, compare_with_baseline

current = cluster(open("app.log").readlines())
report = compare_with_baseline(current, json.load(open("baseline.json")))

for item in report["new"]:
    print(item["severity"], item["template"])
```

## Tests

```bash
python -m unittest discover -s tests -v
```

36 tests: masking equivalence, leftmost-longest selection, severity classification, cluster ordering and line spans, burst detection true and false positives, baseline round-trip, growth thresholds, and CLI end-to-end including the stored baseline in `examples/`.
