"""Exercise the local quality-review command with synthetic request logs."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("review_chat_quality.py").resolve()


class ChatQualityContracts(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.log = Path(directory.name) / "app.log"
        self.reviews = Path(directory.name) / "quality" / "reviews.jsonl"
        self.log.write_text("\n".join(
            f"[chat_request] request_id={index:032x} elapsed_ms={index * 100}.0 "
            f"outcome={outcome} error_kind={error}"
            for index, outcome, error in [(1, "answered", "none"), (2, "answered", "none"),
                                          (3, "answered", "none"), (4, "error", "timeout")]
        ), encoding="utf-8")

    def run_cli(self, *args, success=True):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--log", str(self.log),
                                 "--reviews", str(self.reviews), *args],
                                text=True, capture_output=True, timeout=10)
        if not success:
            self.assertNotEqual(result.returncode, 0)
            return result.stderr
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def record(self, number, grounding, risk, audit="not_audited"):
        return self.run_cli("record", f"{number:032x}", "--grounding", grounding, "--risk", risk, "--audit", audit)

    def test_runtime_and_review_stay_separate_and_amendments_replace_old_labels(self):
        initial = self.run_cli("summary")
        self.assertEqual(initial["server_requests"]["outcomes"], {"answered": 3, "error": 1})
        self.assertEqual(initial["manual_reviews"]["reviewed_answers"], 0)
        self.assertFalse(self.reviews.exists())
        row = self.record(1, "partial", "omitted")
        self.assertEqual(row["elapsed_ms"], 100.0)
        self.assertEqual(row["audit"], "not_audited")
        self.assertNotIn("answer", row)
        self.reviews.write_bytes(self.reviews.read_bytes().rstrip(b"\r\n"))
        self.record(1, "supported", "complete")
        result = self.run_cli("summary")
        self.assertEqual(result["manual_reviews"]["grounding"], {"supported": 1})
        self.assertIsNone(result["manual_reviews"]["audit_pass_rate"])
        self.assertEqual([row["reviewed"] for row in self.run_cli("list")], [False, False, True])

    def test_audit_disagreements_require_an_audit_and_known_human_labels(self):
        self.record(1, "unsupported", "omitted", "passed")
        amended = self.run_cli("record", f"{1:032x}", "--grounding", "partial", "--risk", "omitted")
        self.assertEqual(amended["audit"], "passed")
        self.record(2, "supported", "not_applicable", "rejected")
        self.record(3, "unknown", "unknown", "passed")
        result = self.run_cli("summary")["manual_reviews"]
        self.assertAlmostEqual(result["audit_pass_rate"], 2 / 3)
        self.assertEqual(result["paired_known_reviews"], 2)
        self.assertEqual(result["missed_issues"], 1)
        self.assertEqual(result["false_rejections"], 1)

    def test_refuses_failed_request_or_invalid_file_without_writing(self):
        self.assertIn("answered request", self.run_cli("record", f"{4:032x}", "--grounding", "unknown",
                                                       "--risk", "unknown", success=False))
        self.assertFalse(self.reviews.exists())
        self.reviews.parent.mkdir()
        self.reviews.write_text("{truncated", encoding="utf-8")
        self.assertIn("Invalid review record", self.run_cli("summary", success=False))
        self.assertEqual(self.reviews.read_text(encoding="utf-8"), "{truncated")


if __name__ == "__main__":
    unittest.main()
