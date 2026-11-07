"""Command line interface for log-triage."""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Sequence

from .core import (
    SEVERITIES,
    Cluster,
    baseline_document,
    bursts,
    cluster,
    compare_with_baseline,
    read_baseline,
    read_lines,
)

__version__ = "0.1.0"


def _format_clusters(clusters: Sequence[Cluster], limit: int) -> str:
    if not clusters:
        return "no lines"
    lines = ["%-6s %-6s %-8s %s" % ("COUNT", "SEV", "LINES", "TEMPLATE")]
    lines.append("-" * 100)
    for entry in clusters[:limit]:
        span = "%d-%d" % (entry.first_line, entry.last_line)
        lines.append("%-6d %-6s %-8s %s"
                     % (entry.count, entry.severity, span, entry.template[:70]))
        if entry.samples:
            lines.append(" " * 22 + "e.g. " + entry.samples[0][:76])
    if len(clusters) > limit:
        lines.append("... %d more template(s)" % (len(clusters) - limit))
    return "\n".join(lines)


def _format_baseline_report(report: dict, limit: int) -> str:
    lines: List[str] = []
    new = report.get("new", [])
    changed = report.get("changed", [])
    gone = report.get("gone", [])

    lines.append("NEW (%d)" % len(new))
    if not new:
        lines.append("  nothing new since the baseline")
    for item in new[:limit]:
        lines.append("  [%-5s] %4d  %s"
                     % (item["severity"], item["count"], item["template"][:70]))
        if item["samples"]:
            lines.append(" " * 16 + "e.g. " + item["samples"][0][:72])

    lines.append("")
    lines.append("LOUDER (%d)" % len(changed))
    if not changed:
        lines.append("  no template grew significantly")
    for item in changed[:limit]:
        lines.append("  %4d -> %-4d (x%-5s) %s"
                     % (item["before"], item["after"], item["factor"],
                        item["template"][:60]))

    lines.append("")
    lines.append("GONE (%d)" % len(gone))
    for item in gone[:limit]:
        lines.append("  %s" % item["template"][:80])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--format", choices=("table", "json"), default="table")

    parser = argparse.ArgumentParser(
        prog="log-triage",
        description="Cluster logs into templates and surface what is new.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="command")

    summary = sub.add_parser("summary", parents=[common],
                             help="show the most frequent templates")
    summary.add_argument("paths", nargs="+")
    summary.add_argument("--top", type=int, default=20)

    diff = sub.add_parser("diff", parents=[common],
                          help="compare a log against a baseline")
    diff.add_argument("paths", nargs="+")
    diff.add_argument("--baseline", required=True)
    diff.add_argument("--top", type=int, default=20)
    diff.add_argument("--fail-on-new", choices=SEVERITIES, default=None,
                      help="exit 1 when a new template at or above this severity "
                           "appears")

    record = sub.add_parser("baseline", parents=[common],
                            help="record the template set of a good run")
    record.add_argument("paths", nargs="+")
    record.add_argument("--output", "-o", default="-")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2

    try:
        lines = read_lines(args.paths)
    except OSError as exc:
        sys.stderr.write("log-triage: %s\n" % exc)
        return 2

    entries = cluster(lines)

    if args.command == "baseline":
        document = json.dumps(baseline_document(entries, ", ".join(args.paths)),
                              indent=2)
        if args.output == "-":
            print(document)
        else:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(document + "\n")
            print("recorded %d template(s) in %s" % (len(entries), args.output))
        return 0

    if args.command == "summary":
        if args.format == "json":
            print(json.dumps({
                "templates": [entry.as_dict() for entry in entries[:args.top]],
                "total_lines": len(lines),
                "total_templates": len(entries),
                "bursts": [entry.template for entry in bursts(entries)],
            }, indent=2))
        else:
            print(_format_clusters(entries, args.top))
            loud = bursts(entries)
            if loud:
                print("")
                print("bursts (loud in the second half of the log):")
                for entry in loud[:5]:
                    print("  %4d lines  %s" % (entry.half_two, entry.template[:70]))
        return 0

    try:
        document = read_baseline(args.baseline)
    except (OSError, ValueError) as exc:
        sys.stderr.write("log-triage: %s\n" % exc)
        return 2

    report = compare_with_baseline(entries, document)
    if args.format == "json":
        print(json.dumps({
            "new": report["new"],
            "gone": report["gone"],
            "changed": report["changed"],
            "total_templates": len(entries),
            "total_lines": len(lines),
        }, indent=2))
    else:
        print(_format_baseline_report(report, args.top))

    if args.fail_on_new:
        order = {"info": 0, "warn": 1, "error": 2}
        threshold = order[args.fail_on_new]
        for item in report["new"]:
            if order[item["severity"]] >= threshold:
                return 1
    return 0
