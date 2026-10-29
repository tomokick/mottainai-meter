import unittest
from unittest.mock import patch
import mottainai as m


class CodexDetailsTests(unittest.TestCase):
    def test_owned_does_not_mean_applicable(self):
        d = m.codex_details({"rate_limit": {"primary_window": {}},
            "rate_limit_reset_credits": {"available_count": 2, "applicable_available_count": 0}}, [])
        self.assertEqual(d["reset_tickets"], {"available": 2, "applicable": 0})
        self.assertEqual(d["five_hour"], "not_reported")

    def test_missing_is_unknown_not_zero(self):
        d = m.codex_details({"credits": {"balance": "100"}}, [])
        self.assertIsNone(d["reset_tickets"]["available"])
        self.assertEqual(d["five_hour"], "unknown")
        self.assertIn("未取得", m.codex_detail_lines(d)[1])

    def test_five_hour_and_malformed_counts(self):
        d = m.codex_details({"rate_limit_reset_credits": {
            "available_count": True, "applicable_available_count": -1}}, [{"id": "codex.5h"}])
        self.assertEqual(d["five_hour"], "present")
        self.assertEqual(d["reset_tickets"], {"available": None, "applicable": None})

    def test_snapshot_includes_counts(self):
        cfg = {**m.DEFAULT_CONFIG, "ntfy_data_topic": "test"}
        detail = {"reset_tickets": {"available": 2, "applicable": 0}}
        with patch.object(m, "http_json", return_value=(200, {})) as request:
            self.assertTrue(m.publish_snapshot(cfg, [], {}, "prolite", detail))
        import json
        snapshot = json.loads(request.call_args.args[2]["message"])
        self.assertEqual(snapshot["codex"], detail)

    def test_fetch_failure_does_not_reuse_counts(self):
        with patch.object(m, "fetch_claude", return_value=[]), patch.object(
                m, "fetch_codex", side_effect=RuntimeError("offline")):
            wins, errors, plan, details = m.collect({})
        self.assertEqual(details, {})
        self.assertIn("codex", errors)


if __name__ == "__main__":
    unittest.main()
