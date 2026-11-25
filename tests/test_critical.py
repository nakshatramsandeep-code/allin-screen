"""Critical checks: kill switch, cache, never auto-reject. Uses its own Neon schema.
Run: python -m unittest discover tests"""
import itertools
import os
import unittest
import uuid
from unittest import mock

from screen import gate, guard, pipeline, scrape, util
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


if __name__ == "__main__":
    unittest.main()
