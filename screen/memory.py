"""Partner overrides: save, retrieve similar ones, and track disagreement over time."""
import json
import re

from . import guard, llm
from .util import REC, AllInError, _json, db, latest_result, now


def summary(prof, rec, reasons):
    return f"{prof.get('sector', '')}; {prof.get('one_line', '')}; tool said {rec}; " + " ".join(reasons[:2])


def record(cfg, company, new_rec, code, reason, author):
    """Save tool output + partner change + reason code as one linked row."""
    if new_rec not in REC:
        raise AllInError(f"New recommendation must be one of {REC}.")
    if code not in cfg["reason_codes"]:
        raise AllInError(f"Unknown reason code '{code}'. Use one of {cfg['reason_codes']}.")
    if not reason.strip() or not author.strip():
        raise AllInError("A reason and an author are required.")
    out = latest_result(cfg, company)
    if not out:
        raise AllInError(f"No saved result for '{company}'. Run it first.")
    if out["rec"] == new_rec:
        raise AllInError("New recommendation equals the tool's. Nothing to override.")
    with db(cfg) as con:
        con.execute("insert into overrides(ts,author,company,run_id,old_rec,new_rec,reason_code,reason,summary,tool_output)"
                    " values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (now(), author, out["name"], out["run_id"], out["rec"], new_rec, code, reason,
                     out["summary"], _json(out)))


def _tok(s):
    return set(re.findall(r"[a-z]{4,}", s.lower()))


PICK = guard.DATA_LIMITS + """
Task: pick the past partner corrections most similar in meaning to the new applicant (same business type,
same kind of risk or data gap). Return JSON only: {"ids": [int, ...]} with at most 4 ids from the list."""


def similar(cfg, query, run_id, name, k=4):
    """Keyword match first, then one cheap-model call to pick by meaning. Returns text for the scorer."""
    with db(cfg) as con:
        rows = con.execute("select id,company,old_rec,new_rec,reason_code,reason,summary from overrides").fetchall()
    if not rows:
        return ""
    q = _tok(query)
    ranked = sorted(rows, key=lambda r: -len(q & _tok(r["summary"] + " " + r["reason"])))[:8]
    if len(ranked) > k:
        cand = "\n".join(f"{r['id']}: {r['summary']} | partner reason: {r['reason']}" for r in ranked)
        res = llm.parse_json(llm.call(cfg, "cheap", PICK, guard.data("applicant", query) + "\n" + guard.data("past", cand),
                                      run_id, name, "override_match"))
        ids = [i for i in res.get("ids", []) if i in {r["id"] for r in ranked}][:k]
        ranked = [r for r in ranked if r["id"] in ids]
    return "\n".join(f"- {r['company']}: tool said {r['old_rec']}, partner changed to {r['new_rec']} "
                     f"[{r['reason_code']}]. Why: {r['reason']}" for r in ranked)


def disagreement(cfg):
    """Per run: recommendations made, overrides, and the disagreement rate."""
    with db(cfg) as con:
        runs = con.execute("select run_id, ts from runs order by ts").fetchall()
        res = []
        for r in runs:
            n = con.execute("select count(*) c from recs where run_id=%s", (r["run_id"],)).fetchone()["c"]
            o =con.execute("select count(*) c from overrides where run_id=%s", (r["run_id"],)).fetchone()["c"]
            res.append({"run_id": r["run_id"], "ts": r["ts"], "recs": n, "overrides": o,
                        "rate": round(100 * o / n, 1) if n else None})
    return res


def trail(cfg):
    with db(cfg) as con:
        return [dict(r) for r in con.execute(
            "select ts,author,company,run_id,old_rec,new_rec,reason_code,reason from overrides order by ts desc")]
