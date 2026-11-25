"""Critical checks: kill switch, cache, never auto-reject. Uses its own Neon schema.
Run: python -m unittest discover tests"""
import itertools
import os
import unittest
import uuid
from unittest import mock

import json
import re
import subprocess

from screen import cost, gate, guard, outreach, pipeline, scrape, util
from screen.util import REC, ROOT, db, load_cfg, load_rubric


class Isolated(unittest.TestCase):
    """Each test class gets a throwaway schema in Neon, dropped at the end."""
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_cfg()
        cls.schema = "t_" + uuid.uuid4().hex[:8]
        os.environ["ALLIN_SCHEMA"] = cls.schema

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("ALLIN_SCHEMA", None)
        util._ready.discard(cls.schema)
        with db(cls.cfg) as con:
            con.execute(f'drop schema if exists "{cls.schema}" cascade')


class KillSwitch(Isolated):
    def test_run_stops_before_any_work(self):
        guard.set_kill(self.cfg, True)
        try:
            with mock.patch("screen.pipeline.portfolio.load") as load:
                with self.assertRaises(guard.Stopped):
                    pipeline.run(self.cfg, ["Pumpwise"])
                load.assert_not_called()
        finally:
            guard.set_kill(self.cfg, False)

    def test_fetch_checks_it(self):
        guard.set_kill(self.cfg, True)
        try:
            with self.assertRaises(guard.Stopped):
                scrape.fetch(self.cfg, "https://example.org")
        finally:
            guard.set_kill(self.cfg, False)


class Cache(Isolated):
    def test_same_company_is_not_scraped_twice(self):
        prof = {"company": "Acme", "founders": [{"name": "A B", "role": "CEO", "linkedin": None}]}
        with mock.patch("screen.scrape.fetch", return_value=("ok", "{}")) as f:
            scrape.gather(self.cfg, "Acme", prof)
            first = f.call_count
            scrape.gather(self.cfg, "Acme", prof)
            self.assertEqual(f.call_count, first)
            self.assertGreater(first, 0)


class NeverReject(unittest.TestCase):
    def test_every_score_pair_gives_one_of_three(self):
        rub = load_rubric(load_cfg())
        port = {"complete": True, "flags": []}
        for c, f, cov, inj in itertools.product(range(0, 101, 5), range(0, 101, 5), (0.2, 0.9), ([], ["x"])):
            res = {"company_pct": c, "founder_pct": f, "coverage": cov, "injection_attempts": inj}
            out = gate.decide(rub, res, {"sector": "fintech", "stage": "pre-seed", "one_line": ""}, [], port)
            self.assertIn(out["rec"], REC)
            self.assertTrue(out["reasons"])

    def test_no_reject_path_in_code(self):
        for f in (ROOT / "screen").glob("*.py"):
            self.assertNotIn('"reject"', f.read_text(encoding="utf8"), f.name)
        self.assertEqual(REC, ("advance", "flag", "low priority"))


def fake_result(name="Acme", rec="advance"):
    g = {"id": "traction", "score": 7, "base_score": 7, "weight": 25, "status": "scored", "source": "x", "quote": "q", "reason": "r"}
    return {"name": name, "run_id": "r1", "rec": rec, "reasons": ["a"], "critical_missing": [],
            "profile": {"company": name, "sector": "Fintech", "founders": [{"name": "Ana Rao"}]},
            "company": [g], "founder": [], "company_pct": 70, "founder_pct": 70, "coverage": 0.9, "summary": "s"}


class Budget(Isolated):
    def test_call_refused_after_budget_and_partner_told(self):
        cfg = dict(self.cfg, budget_inr=1)
        cost.log(cfg, "r", "Acme", "score", "gemini-3.6-flash", 1_000_000, 1_000_000)      # far over Rs 1
        with self.assertRaises(cost.BudgetReached):
            cost.check_budget(cfg)
        with db(cfg) as con:
            self.assertEqual(con.execute("select count(*) c from handoffs where kind='budget'").fetchone()["c"], 1)


class Outreach(Isolated):
    def setUp(self):
        util.save_result(self.cfg, "r1", "Acme", fake_result())
        with db(self.cfg) as con:
            con.execute("delete from outreach")

    def test_drafts_only_no_send_path(self):
        outreach.draft(self.cfg, "Acme")
        self.assertEqual(outreach.get(self.cfg, "Acme")["state"], "draft_review")
        self.assertEqual([n for n in dir(outreach) if n.startswith("send")], [])

    def test_partner_review_gets_no_draft_unless_asked(self):
        util.save_result(self.cfg, "r1", "Beta", fake_result("Beta", "flag"))
        with self.assertRaises(util.AllInError):
            outreach.draft(self.cfg, "Beta")
        outreach.draft(self.cfg, "Beta", partner_request=True)

    def test_hedged_accept_goes_to_a_human(self):
        outreach.draft(self.cfg, "Acme")
        outreach.mark_sent(self.cfg, "Acme")
        with mock.patch("screen.outreach.llm.call", return_value=json.dumps({"class": "accept", "why": "x"})):
            outreach.handle_reply(self.cfg, "Acme", "Maybe, let me check, can we do next month instead?")
        self.assertEqual(outreach.get(self.cfg, "Acme")["state"], "needs_human")

    def test_failed_booking_retried_to_the_cap_then_handed_over(self):
        class Broken(outreach.SimCalendar):
            calls = 0

            def book(self, slot):
                Broken.calls += 1
                raise outreach.CalendarError("down")
        outreach.draft(self.cfg, "Acme")
        outreach.mark_sent(self.cfg, "Acme")
        with mock.patch("screen.outreach.llm.call", return_value=json.dumps({"class": "accept", "why": "x"})):
            outreach.handle_reply(self.cfg, "Acme", "Happy to talk, Thursday works.", cal=Broken())
        self.assertEqual(Broken.calls, self.cfg["scrape"]["retries"])
        self.assertEqual(outreach.get(self.cfg, "Acme")["state"], "needs_human")
        with db(self.cfg) as con:
            self.assertEqual(con.execute("select count(*) c from handoffs where kind='booking'").fetchone()["c"], 1)


class Fetch(Isolated):
    def test_failed_fetch_stops_at_cap_and_is_handed_over(self):
        import httpx
        cfg = dict(self.cfg, scrape=dict(self.cfg["scrape"], backoff_s=0))
        with mock.patch("screen.scrape.allowed", return_value=(True, "")),                 mock.patch("screen.scrape.httpx.get", side_effect=httpx.ConnectError("x")) as g:
            st, why = scrape.fetch(cfg, "https://example.org/x")
        self.assertEqual((st, g.call_count), ("failed", cfg["scrape"]["retries"]))


class Secrets(unittest.TestCase):
    def test_no_key_or_database_url_in_tracked_files(self):
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        bad = re.compile(r"AQ\.Ab8[\w-]{20,}|npg_[A-Za-z0-9]{8,}|postgresql://\w+:[^@\s]+@")
        for f in files:
            if (ROOT / f).suffix in (".png", ".pdf"):
                continue
            self.assertIsNone(bad.search((ROOT / f).read_text(encoding="utf8", errors="ignore")), f)


if __name__ == "__main__":
    unittest.main()
