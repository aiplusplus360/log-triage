import json
import os
import subprocess
import sys
import tempfile
import unittest

from logtriage.core import (
    baseline_document,
    bursts,
    cluster,
    compare_with_baseline,
    severity_of,
    template_of,
)

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(HERE, "..", "examples")


class TemplateTests(unittest.TestCase):
    def test_numbers_become_placeholders(self):
        self.assertEqual(template_of("took 13ms")[0], "took <dur>")

    def test_two_ids_share_one_template(self):
        first, _ = template_of("GET /users/8812 200 13ms")
        second, _ = template_of("GET /users/91 200 4ms")
        self.assertEqual(first, second)

    def test_ip_addresses_are_masked(self):
        self.assertEqual(template_of("connect to 10.0.0.4:5432 failed")[0],
                         "connect to <ip> failed")

    def test_uuid_is_masked(self):
        text = "job 0f8b1c2a-1111-2222-3333-444455556666 done"
        self.assertEqual(template_of(text)[0], "job <uuid> done")

    def test_timestamp_is_masked(self):
        self.assertEqual(template_of("2026-01-01T10:00:00Z started")[0],
                         "<ts> started")

    def test_hex_address_is_masked(self):
        self.assertEqual(template_of("fault at 0xdeadbeef")[0], "fault at <hex>")

    def test_long_hash_is_masked(self):
        self.assertEqual(template_of("commit 23bb055a1b2c3d4e5f60 pushed")[0],
                         "commit <hash> pushed")

    def test_ansi_codes_are_removed(self):
        self.assertEqual(template_of("\x1b[31mERROR\x1b[0m boom")[0], "ERROR boom")

    def test_values_are_captured(self):
        template, values = template_of("GET /users/8812 200 13ms")
        self.assertEqual(template, "GET /users/<n> <n> <dur>")
        self.assertEqual(values, ["8812", "200", "13ms"])

    def test_whitespace_is_collapsed(self):
        self.assertEqual(template_of("a    b")[0], "a b")


class SeverityTests(unittest.TestCase):
    def test_error_words(self):
        for line in ("ERROR x", "Fatal: x", "panic: x", "Traceback (most recent",
                     "connection refused", "request failed"):
            self.assertEqual(severity_of(line), "error", line)

    def test_warning_words(self):
        self.assertEqual(severity_of("WARN retrying"), "warn")
        self.assertEqual(severity_of("deprecated option"), "warn")

    def test_plain_lines_are_info(self):
        self.assertEqual(severity_of("listening on :8080"), "info")


class ClusterTests(unittest.TestCase):
    def test_counts_are_grouped(self):
        entries = cluster(["a 1", "a 2", "b 3"])
        self.assertEqual([(c.template, c.count) for c in entries],
                         [("a <n>", 2), ("b <n>", 1)])

    def test_blank_lines_are_ignored(self):
        self.assertEqual(len(cluster(["", "  ", "a 1"])), 1)

    def test_order_is_by_count(self):
        entries = cluster(["a 1", "b 1", "b 2", "b 3"])
        self.assertEqual(entries[0].template, "b <n>")

    def test_severity_promotes_to_error(self):
        entries = cluster(["GET /x 200 1ms", "ERROR GET /x 500 1ms"])
        self.assertEqual(entries[0].severity, "error")

    def test_first_and_last_line_numbers(self):
        entries = cluster(["a 1", "b 1", "a 2"])
        entry = [c for c in entries if c.template == "a <n>"][0]
        self.assertEqual((entry.first_line, entry.last_line), (1, 3))

    def test_samples_are_capped(self):
        entries = cluster(["a %d" % index for index in range(20)], max_samples=2)
        self.assertEqual(len(entries[0].samples), 2)

    def test_half_split_is_recorded(self):
        entries = cluster(["a 1", "a 2", "a 3", "a 4"])
        self.assertEqual((entries[0].half_one, entries[0].half_two), (2, 2))


class BurstTests(unittest.TestCase):
    def test_a_late_burst_is_found(self):
        lines = ["noise %d" % index for index in range(10)]
        lines += ["ERROR panic boom"] * 12
        loud = bursts(cluster(lines))
        self.assertEqual([c.template for c in loud], ["ERROR panic boom"])

    def test_steady_noise_is_not_a_burst(self):
        lines = (["ERROR panic boom"] * 5
                 + ["noise %d" % index for index in range(10)]
                 + ["ERROR panic boom"] * 5)
        self.assertEqual(bursts(cluster(lines)), [])

    def test_minimum_count_is_respected(self):
        lines = ["noise %d" % index for index in range(10)] + ["ERROR x"] * 2
        self.assertEqual(bursts(cluster(lines)), [])


class BaselineTests(unittest.TestCase):
    def test_document_round_trip(self):
        entries = cluster(["a 1", "a 2", "ERROR b 3"])
        document = baseline_document(entries, "before.log")
        text = json.dumps(document)
        restored = json.loads(text)
        self.assertEqual(len(restored["templates"]), 2)
        self.assertIn("recorded_at", restored)

    def test_new_template_is_reported(self):
        before = baseline_document(cluster(["a 1", "b 2"]))
        after = cluster(["a 1", "b 2", "panic boom"])
        report = compare_with_baseline(after, before)
        self.assertEqual([item["template"] for item in report["new"]],
                         ["panic boom"])

    def test_new_errors_sort_first(self):
        before = baseline_document(cluster(["a 1"]))
        after = cluster(["a 1", "info thing", "ERROR fatal thing"])
        report = compare_with_baseline(after, before)
        self.assertEqual(report["new"][0]["severity"], "error")

    def test_gone_template_is_reported(self):
        before = baseline_document(cluster(["a 1", "b 2"]))
        report = compare_with_baseline(cluster(["a 1"]), before)
        self.assertEqual([item["template"] for item in report["gone"]], ["b <n>"])

    def test_threefold_growth_is_changed(self):
        before = baseline_document(cluster(["a 1"] * 5))
        report = compare_with_baseline(cluster(["a 1"] * 20), before)
        self.assertEqual(len(report["changed"]), 1)
        self.assertEqual(report["changed"][0]["factor"], 4.0)

    def test_mild_growth_is_not_changed(self):
        before = baseline_document(cluster(["a 1"] * 5))
        report = compare_with_baseline(cluster(["a 1"] * 7), before)
        self.assertEqual(report["changed"], [])

    def test_identical_runs_report_nothing(self):
        entries = cluster(["a 1", "b 2"])
        report = compare_with_baseline(entries, baseline_document(entries))
        self.assertEqual((report["new"], report["gone"], report["changed"]),
                         ([], [], []))


class ExampleFixtureTests(unittest.TestCase):
    def test_examples_surface_the_planted_regression(self):
        with open(os.path.join(EXAMPLES, "before.log")) as handle:
            before = cluster(handle.readlines())
        with open(os.path.join(EXAMPLES, "after.log")) as handle:
            after = cluster(handle.readlines())
        report = compare_with_baseline(after, baseline_document(before))
        templates = " ".join(item["template"] for item in report["new"])
        self.assertIn("panic", templates)
        self.assertIn("connection pool exhausted", templates)

    def test_before_against_itself_is_stable(self):
        with open(os.path.join(EXAMPLES, "before.log")) as handle:
            entries = cluster(handle.readlines())
        report = compare_with_baseline(entries, baseline_document(entries))
        self.assertEqual(report["new"], [])


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "logtriage", *args],
            capture_output=True, text=True,
            cwd=os.path.join(HERE, ".."),
        )

    def test_summary_json(self):
        result = self.run_cli("summary", os.path.join(EXAMPLES, "before.log"),
                              "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertGreater(data["total_templates"], 0)
        self.assertIn("templates", data)

    def test_baseline_then_diff_fails_on_new_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = os.path.join(tmp, "b.json")
            first = self.run_cli("baseline", os.path.join(EXAMPLES, "before.log"),
                                 "--output", baseline)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self.run_cli("diff", os.path.join(EXAMPLES, "after.log"),
                                  "--baseline", baseline, "--fail-on-new", "error")
            self.assertEqual(second.returncode, 1, second.stdout)
            self.assertIn("panic", second.stdout)

    def test_diff_against_itself_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = os.path.join(tmp, "b.json")
            self.run_cli("baseline", os.path.join(EXAMPLES, "before.log"),
                         "--output", baseline)
            result = self.run_cli("diff", os.path.join(EXAMPLES, "before.log"),
                                  "--baseline", baseline, "--fail-on-new", "info")
            self.assertEqual(result.returncode, 0, result.stdout)

    def test_missing_file_exits_two(self):
        self.assertEqual(self.run_cli("summary", "nope.log").returncode, 2)


if __name__ == "__main__":
    unittest.main()
