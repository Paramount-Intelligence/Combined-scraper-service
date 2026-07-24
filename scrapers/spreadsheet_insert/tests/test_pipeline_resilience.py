"""
Pipeline resilience tests: flush-on-failure, deferred retry, model fallback,
runtime guard, and webhook safety.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import insert_to_spreadsheet as ins  # noqa: E402


def _exit_raiser(*args, **kwargs):
    code = args[0] if args else 0
    raise SystemExit(code)


def _row(title="Job"):
    return [
        "07/24/2026 10:00:00", "07/24/2026", "PC", "General Consulting", title,
        "desc", "OTHER", "Consumer Goods - Other", "$799", "$799", "6", "6", "1.0",
        "Consultant", "Remote", "N/A", "BTG", "$95,880", "$95,880",
        "https://example.com", "30", "BTG",
    ]


def _rec(i, title=None):
    return {
        "_id": f"id-{i}",
        "title": title or f"Project {i}",
        "description": f"Description for project {i}",
        "platform": "btg",
        "detected_at": "2026-07-24 10:00:00",
        "url": f"https://example.com/{i}",
        "remote_type": "Remote",
    }


class TestModelFallback(unittest.TestCase):
    def test_primary_failure_fallback_success(self):
        good = {
            "platform_category": "Information Security",
            "category": "Information Technology",
            "category_reasoning": "Security role",
            "category_confidence": 0.9,
            "industry": "Financial Services",
            "industry_secondary": "Financial Services",
            "role_type": "Consultant",
            "duration_months_low": 6,
            "duration_months_high": 6,
            "utilization": 1.0,
            "daily_rate_reasoning": "n/a",
            "_gemini_model": ins.GEMINI_FALLBACK_MODEL,
        }

        def _call(model_name, system_prompt, user_content):
            if model_name == ins.GEMINI_PRIMARY_MODEL:
                raise RuntimeError("503 UNAVAILABLE")
            return dict(good)

        with patch.object(ins, "gemini_client", MagicMock()):
            with patch.object(ins, "_call_model_with_bounded_attempts", side_effect=_call):
                result = ins.query_gemini_semantics("t", "d", {"platform": "btg"})
        self.assertEqual(result["category"], "Information Technology")
        self.assertEqual(result["_gemini_model"], ins.GEMINI_FALLBACK_MODEL)

    def test_low_confidence_escalates(self):
        low = {
            "platform_category": "Support",
            "category": "General Consulting",
            "category_reasoning": "unclear",
            "category_confidence": 0.55,
            "industry": "OTHER",
            "industry_secondary": "Consumer Goods - Other",
            "role_type": "OTHER",
            "duration_months_low": 6,
            "duration_months_high": 6,
            "utilization": 1.0,
            "daily_rate_reasoning": "n/a",
            "_gemini_model": ins.GEMINI_PRIMARY_MODEL,
        }
        high = dict(low)
        high["category_confidence"] = 0.92
        high["category"] = "Information Technology"
        high["_gemini_model"] = ins.GEMINI_FALLBACK_MODEL

        calls = {"n": 0}

        def _call(model_name, system_prompt, user_content):
            calls["n"] += 1
            if model_name == ins.GEMINI_PRIMARY_MODEL:
                return dict(low)
            return dict(high)

        with patch.object(ins, "gemini_client", MagicMock()):
            with patch.object(ins, "_call_model_with_bounded_attempts", side_effect=_call):
                result = ins.query_gemini_semantics("t", "d", {"platform": "btg"})
        self.assertEqual(result["category"], "Information Technology")
        self.assertGreaterEqual(calls["n"], 2)

    def test_both_models_fail_raises(self):
        with patch.object(ins, "gemini_client", MagicMock()):
            with patch.object(
                ins,
                "_call_model_with_bounded_attempts",
                side_effect=RuntimeError("503 UNAVAILABLE"),
            ):
                with self.assertRaises(ins.AIClassificationError):
                    ins.query_gemini_semantics("t", "d", {"platform": "btg"})


class TestFlushOnFailure(unittest.TestCase):
    def test_buffered_rows_flushed_when_record_fails(self):
        """Chunk size 5; records 1-3 succeed, 4 fails → flush 1-3 immediately."""
        records = [_rec(i) for i in range(1, 6)]
        collection = MagicMock()
        flushed = []

        def fake_flush(coll, rows, ids, skipped=None):
            flushed.append((list(rows), list(ids)))
            rows.clear()
            ids.clear()
            if skipped is not None:
                skipped.clear()
            return True

        call_count = {"n": 0}

        def fake_map(rec, prefer_fallback=False):
            call_count["n"] += 1
            if call_count["n"] == 4:
                raise ins.AIClassificationError("both models failed")
            rec["_last_gemini_model"] = ins.GEMINI_PRIMARY_MODEL
            return _row(rec["title"])

        with patch.object(ins, "MongoClient") as mc:
            mc.return_value.__getitem__.return_value.__getitem__.return_value = collection
            collection.find.return_value = records
            with patch.object(ins, "gemini_client", MagicMock()):
                with patch.object(ins, "map_record_to_row", side_effect=fake_map):
                    with patch.object(ins, "flush_pending_successes", side_effect=fake_flush):
                        with patch.object(ins, "save_retry_failure"):
                            with patch.object(ins, "FILTER_ENGLISH_ONLY", False):
                                with patch.object(ins, "RECORD_RETRY_ROUNDS", 0):
                                    with patch.object(ins, "sys") as mock_sys:
                                        mock_sys.argv = ["insert_to_spreadsheet.py"]
                                        mock_sys.exit.side_effect = _exit_raiser
                                        try:
                                            ins.process_uninserted_records()
                                        except SystemExit:
                                            pass

        # First flush should include the 3 buffered successes from before failure
        self.assertTrue(flushed)
        first_ids_len = len(flushed[0][1])
        self.assertEqual(first_ids_len, 3)

    def test_failure_mid_batch_continues_later_records(self):
        records = [_rec(i) for i in range(1, 8)]
        collection = MagicMock()
        mapped_titles = []
        flushed_counts = []

        def fake_flush(coll, rows, ids, skipped=None):
            flushed_counts.append(len(ids))
            rows.clear()
            ids.clear()
            if skipped is not None:
                skipped.clear()
            return True

        def fake_map(rec, prefer_fallback=False):
            title = rec["title"]
            # Fail project 4 only on first pass
            if title == "Project 4" and not prefer_fallback:
                raise ins.AIClassificationError("fail")
            if prefer_fallback and title == "Project 4":
                raise ins.AIClassificationError("still fail")
            mapped_titles.append(title)
            rec["_last_gemini_model"] = ins.GEMINI_PRIMARY_MODEL
            return _row(title)

        with patch.object(ins, "MongoClient") as mc:
            mc.return_value.__getitem__.return_value.__getitem__.return_value = collection
            collection.find.return_value = records
            with patch.object(ins, "gemini_client", MagicMock()):
                with patch.object(ins, "map_record_to_row", side_effect=fake_map):
                    with patch.object(ins, "flush_pending_successes", side_effect=fake_flush):
                        with patch.object(ins, "save_retry_failure"):
                            with patch.object(ins, "FILTER_ENGLISH_ONLY", False):
                                with patch.object(ins, "RECORD_RETRY_ROUNDS", 1):
                                    with patch.object(ins, "CHUNK_SIZE", 5):
                                        with patch.object(ins, "sys") as mock_sys:
                                            mock_sys.argv = ["insert_to_spreadsheet.py"]
                                            mock_sys.exit.side_effect = _exit_raiser
                                            try:
                                                with patch.object(ins.time, "sleep"):
                                                    ins.process_uninserted_records()
                                            except SystemExit as e:
                                                # partial success expected
                                                self.assertIn(e.code, (ins.EXIT_PARTIAL_SUCCESS, 0, None))

        # Projects after the failure should still be mapped
        self.assertIn("Project 5", mapped_titles)
        self.assertIn("Project 7", mapped_titles)
        self.assertNotIn("Project 4", mapped_titles)


class TestRuntimeGuard(unittest.TestCase):
    def test_runtime_guard_stops_new_ai_calls(self):
        records = [_rec(i) for i in range(1, 6)]
        collection = MagicMock()
        mapped = []

        def fake_map(rec, prefer_fallback=False):
            mapped.append(rec["title"])
            rec["_last_gemini_model"] = ins.GEMINI_PRIMARY_MODEL
            return _row(rec["title"])

        # Force guard after first record
        calls = {"n": 0}

        def fake_guard(_start):
            calls["n"] += 1
            return calls["n"] > 2  # allow first classify, then guard

        with patch.object(ins, "MongoClient") as mc:
            mc.return_value.__getitem__.return_value.__getitem__.return_value = collection
            collection.find.return_value = records
            with patch.object(ins, "gemini_client", MagicMock()):
                with patch.object(ins, "map_record_to_row", side_effect=fake_map):
                    with patch.object(ins, "flush_pending_successes", return_value=True):
                        with patch.object(ins, "runtime_limit_approaching", side_effect=fake_guard):
                            with patch.object(ins, "FILTER_ENGLISH_ONLY", False):
                                with patch.object(ins, "sys") as mock_sys:
                                    mock_sys.argv = ["insert_to_spreadsheet.py"]
                                    mock_sys.exit.side_effect = _exit_raiser
                                    try:
                                        with patch.object(ins.time, "sleep"):
                                            ins.process_uninserted_records()
                                    except SystemExit as e:
                                        self.assertEqual(e.code, ins.EXIT_RUNTIME_GUARD)

        self.assertLess(len(mapped), len(records))


class TestWebhookFailure(unittest.TestCase):
    def test_webhook_failure_does_not_mark_inserted(self):
        records = [_rec(i) for i in range(1, 3)]
        collection = MagicMock()

        def fake_map(rec, prefer_fallback=False):
            rec["_last_gemini_model"] = ins.GEMINI_PRIMARY_MODEL
            return _row(rec["title"])

        with patch.object(ins, "MongoClient") as mc:
            mc.return_value.__getitem__.return_value.__getitem__.return_value = collection
            collection.find.return_value = records
            with patch.object(ins, "gemini_client", MagicMock()):
                with patch.object(ins, "map_record_to_row", side_effect=fake_map):
                    with patch.object(ins, "flush_pending_successes", return_value=False):
                        with patch.object(ins, "FILTER_ENGLISH_ONLY", False):
                            with patch.object(ins, "CHUNK_SIZE", 1):
                                with patch.object(ins, "sys") as mock_sys:
                                    mock_sys.argv = ["insert_to_spreadsheet.py"]
                                    mock_sys.exit.side_effect = _exit_raiser
                                    try:
                                        with patch.object(ins.time, "sleep"):
                                            ins.process_uninserted_records()
                                    except SystemExit as e:
                                        self.assertEqual(e.code, ins.EXIT_WEBHOOK_FAILURE)

        collection.update_many.assert_not_called()


class TestFlushHelper(unittest.TestCase):
    def test_flush_marks_only_after_webhook_success(self):
        collection = MagicMock()
        rows = [_row("A"), _row("B")]
        ids = ["a", "b"]
        with patch.object(ins.requests, "post") as post:
            post.return_value.status_code = 200
            ok = ins.flush_pending_successes(collection, rows, ids, [])
        self.assertTrue(ok)
        self.assertEqual(rows, [])
        self.assertEqual(ids, [])
        collection.update_many.assert_called_once()

    def test_flush_leaves_buffer_on_webhook_failure(self):
        collection = MagicMock()
        rows = [_row("A")]
        ids = ["a"]
        with patch.object(ins.requests, "post") as post:
            post.return_value.status_code = 500
            post.return_value.text = "nope"
            ok = ins.flush_pending_successes(collection, rows, ids, [])
        self.assertFalse(ok)
        self.assertEqual(len(rows), 1)
        collection.update_many.assert_not_called()


class TestNoSilentDefaults(unittest.TestCase):
    def test_map_raises_when_gemini_unavailable(self):
        with patch.object(ins, "query_gemini_semantics", side_effect=ins.AIClassificationError("down")):
            with self.assertRaises(ins.AIClassificationError):
                ins.map_record_to_row(_rec(1))


if __name__ == "__main__":
    unittest.main()
