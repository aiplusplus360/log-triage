"""Turn a wall of logs into templates, then show only what is new.

Two ideas do all the work:

**Templating.** ``GET /users/8812 200 13ms`` and ``GET /users/91 500 4ms`` are
the same event with different values. Replacing the values gives one template
with a count, which is what a human actually reads.

**A baseline.** A log is not interesting because it contains errors. It is
interesting because it contains errors that were not there before the deploy.
Recording the template set of a known-good run turns "here are 40 000 lines"
into "here are the three lines that are new".

>>> template_of("GET /users/8812 200 13ms")[0]
'GET /users/<n> <n> <dur>'

>>> template_of("connect to 10.0.0.4:5432 failed")[0]
'connect to <ip> failed'

>>> [c.template for c in cluster(["a 1", "a 2", "b 3"])]
['a <n>', 'b <n>']
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "Cluster",
    "SEVERITIES",
    "baseline_document",
    "bursts",
    "cluster",
    "compare_with_baseline",
    "severity_of",
    "template_of",
]

SEVERITIES = ("info", "warn", "error")
_RANK = {"info": 0, "warn": 1, "error": 2}

_MASKS: Sequence[Tuple[re.Pattern, str]] = (
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
                r"(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
                re.IGNORECASE), "<uuid>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.IGNORECASE), "<mac>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE), "<hex>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE), "<hash>"),
    (re.compile(r"\b\d{9,}\b"), "<id>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|µs|us|ns|s|m|h|MB|GB|KB|B)\b"), "<dur>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<n>"),
)

_ERROR_WORDS = re.compile(
    r"(?i)\b(error|fatal|critical|panic|traceback|exception|segfault|failed|"
    r"failure|cannot|refused|denied|timeout|oom|killed)\b")
_WARN_WORDS = re.compile(r"(?i)\b(warn|warning|deprecated|retry|retrying|slow)\b")

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def severity_of(line: str) -> str:
    """Classify one line.

    >>> severity_of("ERROR could not connect")
    'error'
    >>> severity_of("WARN retrying in 2s")
    'warn'
    >>> severity_of("listening on :8080")
    'info'
    """
    if _ERROR_WORDS.search(line):
        return "error"
    if _WARN_WORDS.search(line):
        return "warn"
    return "info"


def template_of(line: str) -> Tuple[str, List[str]]:
    """Return ``(template, captured_values)`` for one log line."""
    text = _ANSI.sub("", line.rstrip("\r\n"))
    candidates: List[Tuple[int, int, int, int, str]] = []
    for priority, (pattern, mask) in enumerate(_MASKS):
        for match in pattern.finditer(text):
            candidates.append((match.start(), -(match.end() - match.start()),
                               priority, match.end(), mask))
    candidates.sort()

    pieces: List[str] = []
    values: List[str] = []
    cursor = 0
    for start, _neg, _priority, end, mask in candidates:
        if start < cursor:
            continue
        pieces.append(text[cursor:start])
        pieces.append(mask)
        values.append(text[start:end])
        cursor = end
    pieces.append(text[cursor:])

    result = re.sub(r"\s+", " ", "".join(pieces)).strip()
    return result, values


@dataclass
class Cluster:
    template: str
    count: int = 0
    severity: str = "info"
    first_line: int = 0
    last_line: int = 0
    samples: List[str] = field(default_factory=list)
    values: List[str] = field(default_factory=list)
    half_one: int = 0
    half_two: int = 0

    def as_dict(self) -> dict:
        return {
            "template": self.template,
            "count": self.count,
            "severity": self.severity,
            "first_line": self.first_line,
            "last_line": self.last_line,
            "samples": self.samples,
            "values": self.values,
        }


def cluster(lines: Sequence[str], max_samples: int = 2,
            max_values: int = 6) -> List[Cluster]:
    """Group lines by template; return clusters ordered by count."""
    clusters: Dict[str, Cluster] = {}
    total = max(len(lines), 1)
    midpoint = total / 2.0

    for index, line in enumerate(lines, 1):
        if not line.strip():
            continue
        template, values = template_of(line)
        entry = clusters.get(template)
        if entry is None:
            entry = Cluster(template=template, severity=severity_of(line),
                            first_line=index)
            clusters[template] = entry
        if severity_of(line) == "error":
            entry.severity = "error"
        elif severity_of(line) == "warn" and entry.severity == "info":
            entry.severity = "warn"
        entry.count += 1
        entry.last_line = index
        if index <= midpoint:
            entry.half_one += 1
        else:
            entry.half_two += 1
        if len(entry.samples) < max_samples:
            entry.samples.append(line.rstrip("\r\n")[:300])
        if len(entry.values) < max_values:
            entry.values.extend(values[: max_values - len(entry.values)])

    return sorted(clusters.values(), key=lambda c: (-c.count, c.template))


def bursts(clusters: Sequence[Cluster], minimum: int = 5,
           ratio: float = 3.0) -> List[Cluster]:
    """Clusters that appear far more often in the second half of the log.

    A crash loop does not add a new template; it makes an existing one louder.
    Comparing the two halves finds it without needing timestamps.
    """
    found: List[Cluster] = []
    for entry in clusters:
        if entry.half_two < minimum:
            continue
        baseline = max(entry.half_one, 1)
        if entry.half_two / baseline >= ratio:
            found.append(entry)
    return sorted(found, key=lambda c: -c.half_two)


def baseline_document(clusters: Sequence[Cluster],
                      source: str = "") -> dict:
    """Serialise the template set of a known-good run."""
    return {
        "source": source,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "templates": sorted(
            (
                {
                    "template": entry.template,
                    "severity": entry.severity,
                    "count": entry.count,
                }
                for entry in clusters
            ),
            key=lambda item: item["template"],
        ),
    }


def compare_with_baseline(current: Sequence[Cluster],
                          document: dict) -> Dict[str, List[dict]]:
    """Split clusters into new, gone and changed relative to a baseline."""
    known = {item["template"]: item for item in document.get("templates", [])}
    current_by_template = {entry.template: entry for entry in current}

    new = [
        {"template": entry.template, "severity": entry.severity,
         "count": entry.count, "samples": entry.samples[:1]}
        for entry in current if entry.template not in known
    ]
    gone = [
        {"template": template, "severity": item.get("severity", "info"),
         "count": item.get("count", 0)}
        for template, item in known.items() if template not in current_by_template
    ]
    changed = []
    for template, entry in current_by_template.items():
        item = known.get(template)
        if item is None:
            continue
        before = int(item.get("count", 0))
        if before and entry.count >= before * 3 and entry.count - before >= 10:
            changed.append({
                "template": template,
                "severity": entry.severity,
                "before": before,
                "after": entry.count,
                "factor": round(entry.count / before, 1),
            })

    new.sort(key=lambda item: (-_RANK[item["severity"]], -item["count"]))
    changed.sort(key=lambda item: -item["factor"])
    return {"new": new, "gone": gone, "changed": changed}


def read_baseline(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_lines(paths: Sequence[str]) -> List[str]:
    lines: List[str] = []
    for path in paths:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines.extend(handle.readlines())
    return lines
