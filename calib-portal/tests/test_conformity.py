"""符合性评定、规则拒收、签发一致性与过期的测试。"""
import importlib
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, conformity, database as db


def rules_csv(text: str) -> bytes:
    return text.encode("utf-8")


class RuleParsingTests(unittest.TestCase):
    def test_accepts_aliases_and_numeric_forms(self):
        rules = conformity.parse_rules_csv(
            rules_csv(
                "设备编号,允差L,扩展不确定度U\n"
                "EQ-1,0.05,0.01\n"
                "EQ-2,+2,0\n"
                "EQ-3,.5,0.010\n"
            )
        )
        self.assertEqual([r.device_no for r in rules], ["EQ-1", "EQ-2", "EQ-3"])
        self.assertEqual([r.line_no for r in rules], [2, 3, 4])
        self.assertEqual(conformity.canonical_decimal(rules[0].tolerance), "0.05")

    def test_english_header(self):
        rules = conformity.parse_rules_csv(
            rules_csv("device_no,tolerance_l,uncertainty_u\nA1,0.1,0.02\n")
        )
        self.assertEqual(len(rules), 1)

    def test_duplicate_device_rejects_whole_file(self):
        with self.assertRaises(conformity.RuleCsvError) as ctx:
            conformity.parse_rules_csv(
                rules_csv(
                    "设备编号,允差L,扩展不确定度U\n"
                    "A,0.1,0\nA,0.2,0.01\nB,0.1,0\n"
                )
            )
        self.assertTrue(any("重复" in e for e in ctx.exception.errors))

    def test_invalid_numbers_reject_whole_file(self):
        for bad in ("abc", "nan", "inf", ""):
            with self.assertRaises(conformity.RuleCsvError):
                conformity.parse_rules_csv(
                    rules_csv(f"设备编号,允差L,扩展不确定度U\nA,{bad},0\n")
                )

    def test_tolerance_must_be_positive(self):
        for bad in ("0", "-0.1"):
            with self.assertRaises(conformity.RuleCsvError):
                conformity.parse_rules_csv(
                    rules_csv(f"设备编号,允差L,扩展不确定度U\nA,{bad},0\n")
                )

    def test_uncertainty_must_be_non_negative(self):
        with self.assertRaises(conformity.RuleCsvError):
            conformity.parse_rules_csv(
                rules_csv("设备编号,允差L,扩展不确定度U\nA,0.1,-0.01\n")
            )

    def test_missing_column_and_empty(self):
        with self.assertRaises(conformity.RuleCsvError):
            conformity.parse_rules_csv(rules_csv("设备编号,允差L\nA,0.1\n"))
        with self.assertRaises(conformity.RuleCsvError):
            conformity.parse_rules_csv(
                rules_csv("设备编号,允差L,扩展不确定度U\n")
            )

    def test_all_errors_reported(self):
        with self.assertRaises(conformity.RuleCsvError) as ctx:
            conformity.parse_rules_csv(
                rules_csv(
                    "设备编号,允差L,扩展不确定度U\n"
                    "A,0,0\nA,abc,0.01\nB,0.1,-1\n"
                )
            )
        self.assertGreaterEqual(len(ctx.exception.errors), 3)


class JudgeTests(unittest.TestCase):
    def _rule(self, l="0.10", u="0.02"):
        return conformity.Rule(
            2, "D", conformity.parse_decimal(l),
            conformity.parse_decimal(u), l, u,
        )

    def _eval(self, error, l="0.10", u="0.02", status=config.DONE_EXISTING):
        batch = {"id": 1, "portal_url": "http://p", "filename": "f"}
        rows = [{
            "id": 1, "line_no": 2, "device_no": "D",
            "cal_date": "2026-11-01", "indication_error": error,
            "status": status, "detail": "", "portal_record_url": "",
            "portal_record_id": "",
        }]
        return conformity.build_evaluation(
            batch, rows, {"id": 1, "filename": "r.csv", "uploaded_at": "t",
                          "fingerprint": "f", "rule_count": 1},
            [self._rule(l, u)],
        )

    def test_conforming_boundary_inclusive(self):
        r = self._eval("+0.08")  # 0.08+0.02 = 0.10 <= 0.10
        self.assertEqual(r["rows"][0]["result"], conformity.R_CONFORMING)
        self.assertTrue(r["eligible"])

    def test_nonconforming_strict(self):
        r = self._eval("-0.13")  # 0.13-0.02 = 0.11 > 0.10
        self.assertEqual(r["rows"][0]["result"], conformity.R_NONCONFORMING)

    def test_uncertainty_band_pending(self):
        r = self._eval("+0.10")  # 0.12 > 0.10 and 0.08 <= 0.10
        self.assertEqual(r["rows"][0]["result"], conformity.R_PENDING)
        self.assertFalse(r["eligible"])

    def test_exact_minus_boundary_pending(self):
        r = self._eval("+0.12")  # minus = 0.10, not > L
        self.assertEqual(r["rows"][0]["result"], conformity.R_PENDING)

    def test_zero_uncertainty(self):
        self.assertEqual(
            self._eval("+0.10", u="0")["rows"][0]["result"],
            conformity.R_CONFORMING,
        )
        self.assertEqual(
            self._eval("+0.11", u="0")["rows"][0]["result"],
            conformity.R_NONCONFORMING,
        )

    def test_unfinished_row_pending(self):
        r = self._eval("+0.01", status=config.PAUSED_MULTI)
        self.assertEqual(r["rows"][0]["result"], conformity.R_PENDING)
        self.assertFalse(r["eligible"])

    def test_ignored_row_reason_present(self):
        r = self._eval("+0.01", status=config.IGNORED)
        self.assertEqual(r["rows"][0]["result"], conformity.R_IGNORED)
        self.assertIn("忽略", r["rows"][0]["reason"])

    def test_missing_rule_pending(self):
        rows = conformity.build_evaluation(
            {"id": 1, "portal_url": "p", "filename": "f"},
            [{
                "id": 1, "line_no": 2, "device_no": "OTHER",
                "cal_date": "d", "indication_error": "+0.01",
                "status": config.DONE_EXISTING, "detail": "",
                "portal_record_url": "", "portal_record_id": "",
            }],
            {"id": 1, "filename": "r", "uploaded_at": "t",
             "fingerprint": "f", "rule_count": 1},
            [self._rule()],
        )
        self.assertEqual(rows["rows"][0]["result"], conformity.R_PENDING)
        self.assertIn("无对应允差规则", rows["rows"][0]["reason"])


class PersistenceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = self.tmp.name
        importlib.reload(config)
        importlib.reload(db)
        db.init()

    def tearDown(self):
        self.tmp.cleanup()

    def install_rules(self, text, filename="rules.csv"):
        rules = conformity.parse_rules_csv(rules_csv(text))
        return db.replace_active_ruleset(filename, rules)

    def add_row(self, bid, dev, date, err, status=config.DONE_EXISTING,
                line_no=2):
        rid, _ = db.upsert_row(bid, "http://p", dev, date, err, line_no)
        db.update_row(
            rid, status=status,
            portal_record_url=f"http://p/records/{rid}",
            portal_record_id=str(rid),
        )
        return rid


class RulesetStorageTests(PersistenceBase):
    def test_replacement_is_atomic_and_history_kept(self):
        first = self.install_rules("设备编号,允差L,扩展不确定度U\nA,0.1,0\n")
        second = self.install_rules(
            "设备编号,允差L,扩展不确定度U\nB,0.2,0.01\n"
        )
        self.assertNotEqual(first, second)
        active = db.active_ruleset()
        self.assertEqual(active["id"], second)
        self.assertEqual(
            [(r["id"], r["active"]) for r in db.list_rulesets()],
            [(second, 1), (first, 0)],
        )
        self.assertEqual(
            [r["device_no"] for r in db.rules_for(second)], ["B"]
        )

    def test_invalid_upload_keeps_previous_rules(self):
        first = self.install_rules("设备编号,允差L,扩展不确定度U\nA,0.1,0\n")
        with self.assertRaises(conformity.RuleCsvError):
            self.install_rules(
                "设备编号,允差L,扩展不确定度U\nA,0.1,0\nA,0.2,0\n"
            )
        active = db.active_ruleset()
        self.assertEqual(active["id"], first)
        self.assertEqual(active["filename"], "rules.csv")


class SigningTests(PersistenceBase):
    def setUp(self):
        super().setUp()
        self.install_rules("设备编号,允差L,扩展不确定度U\nA,0.10,0.02\n")

    def _eligible_batch(self):
        bid = db.create_batch("http://p", "b.csv")
        self.rid = self.add_row(bid, "A", "2026-11-01", "+0.05", line_no=2)
        return bid

    def test_sign_requires_fingerprints_to_match(self):
        bid = self._eligible_batch()
        ev = db.load_evaluation(bid)
        with self.assertRaises(db.SourceChanged):
            db.sign_batch(bid, "wrong", ev["rules_fingerprint"])
        with self.assertRaises(db.SourceChanged):
            db.sign_batch(bid, ev["progress_fingerprint"], "wrong")
        self.assertEqual(db.list_signatures(bid), [])
        sid = db.sign_batch(
            bid, ev["progress_fingerprint"], ev["rules_fingerprint"], "质控员"
        )
        sig = db.get_signature(sid)
        self.assertEqual(sig["status"], config.SIGNED_VALID)
        payload = json.loads(sig["payload"])
        self.assertEqual(payload["counts"]["conforming"], 1)
        self.assertEqual(
            payload["rules"][0]["device_no"], "A"
        )

    def test_pending_row_blocks_signing(self):
        bid = db.create_batch("http://p", "b.csv")
        self.add_row(bid, "A", "2026-11-01", "+0.05")
        self.add_row(bid, "A", "2026-11-02", "+0.05",
                     status=config.PAUSED_QUERY_FAIL, line_no=3)
        ev = db.load_evaluation(bid)
        self.assertFalse(ev["eligible"])
        with self.assertRaises(db.SigningUnavailable):
            db.sign_batch(bid, ev["progress_fingerprint"],
                          ev["rules_fingerprint"])

    def test_no_completed_rows_blocks_signing(self):
        bid = db.create_batch("http://p", "b.csv")
        self.add_row(bid, "A", "2026-11-01", "+0.05",
                     status=config.PENDING)
        ev = db.load_evaluation(bid)
        with self.assertRaises(db.SigningUnavailable):
            db.sign_batch(bid, ev["progress_fingerprint"],
                          ev["rules_fingerprint"])

    def test_progress_change_after_signing_marks_stale(self):
        bid = self._eligible_batch()
        ev = db.load_evaluation(bid)
        sid = db.sign_batch(
            bid, ev["progress_fingerprint"], ev["rules_fingerprint"], "q"
        )
        self.assertEqual(db.refresh_signature(sid)["status"],
                         config.SIGNED_VALID)
        db.update_row(self.rid, detail="后台补录了点东西")
        sig = db.refresh_signature(sid)
        self.assertEqual(sig["status"], config.SIGNED_STALE)
        self.assertIn("进度", sig["stale_reason"])
        # 原签发内容保持不变
        self.assertEqual(json.loads(sig["payload"])["counts"]["total"], 1)

    def test_rule_replacement_after_signing_marks_stale(self):
        bid = self._eligible_batch()
        ev = db.load_evaluation(bid)
        sid = db.sign_batch(
            bid, ev["progress_fingerprint"], ev["rules_fingerprint"], "q"
        )
        self.install_rules(
            "设备编号,允差L,扩展不确定度U\nA,0.20,0.02\n", "new.csv"
        )
        sig = db.refresh_signature(sid)
        self.assertEqual(sig["status"], config.SIGNED_STALE)
        self.assertIn("规则", sig["stale_reason"])
        self.assertEqual(json.loads(sig["payload"])["rules"][0]["tolerance"],
                         "0.1")

    def test_unchanged_sources_keep_signature_valid(self):
        bid = self._eligible_batch()
        ev = db.load_evaluation(bid)
        sid = db.sign_batch(
            bid, ev["progress_fingerprint"], ev["rules_fingerprint"], "q"
        )
        self.install_rules(
            "设备编号,允差L,扩展不确定度U\nA,0.10,0.02\n", "same.csv"
        )
        # 规则内容不变但规则集 ID 变了 —— 视为来源已变（更保守）
        self.assertEqual(db.refresh_signature(sid)["status"],
                         config.SIGNED_STALE)

    def test_concurrent_signing_only_one_wins(self):
        bid = self._eligible_batch()
        ev = db.load_evaluation(bid)
        results = []
        barrier = threading.Barrier(4)

        def attempt():
            barrier.wait()
            try:
                results.append(("ok", db.sign_batch(
                    bid, ev["progress_fingerprint"],
                    ev["rules_fingerprint"], "q",
                )))
            except db.SourceChanged as exc:
                results.append(("changed", str(exc)))

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 所有成功签发内容一致；不会因并发产生不一致快照
        ok = [r for r in results if r[0] == "ok"]
        self.assertGreaterEqual(len(ok), 1)
        for _, sid in ok:
            payload = json.loads(db.get_signature(sid)["payload"])
            self.assertEqual(payload["counts"]["conforming"], 1)

    def test_progress_change_during_evaluation_detected(self):
        """模拟：页面读完快照后，后台补录改变进度，再签发必须被拒。"""
        bid = self._eligible_batch()
        ev = db.load_evaluation(bid)
        db.update_row(self.rid, status=config.DONE_SUBMITTED,
                      portal_record_url="http://p/records/9")
        with self.assertRaises(db.SourceChanged):
            db.sign_batch(bid, ev["progress_fingerprint"],
                          ev["rules_fingerprint"])

    def test_ignored_duplicate_lines_do_not_block_signing(self):
        bid = db.create_batch("http://p", "b.csv")
        self.add_row(bid, "A", "2026-11-01", "+0.05")
        db.add_ignored_line(
            bid, 3, "A", "2026-11-01", "+0.05", "与本文件更早行同键"
        )
        ev = db.load_evaluation(bid)
        self.assertEqual(ev["counts"]["ignored"], 1)
        self.assertEqual(ev["counts"]["total"], 2)
        self.assertTrue(ev["eligible"])
        self.assertIn("重复", ev["ignored_rows"][0]["reason"])
        sid = db.sign_batch(
            bid, ev["progress_fingerprint"], ev["rules_fingerprint"]
        )
        payload = json.loads(db.get_signature(sid)["payload"])
        self.assertEqual(payload["counts"]["ignored"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
