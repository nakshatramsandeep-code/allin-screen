"""Partner score corrections: store four linked things, reuse them for similar companies."""
import re

from . import guard, llm
from .util import AllInError, db, latest_result, now

MIN_MOVE = 1.0      # smaller changes are treated as noise and not stored
CAP = 2.0           # most a correction can move a score
MAX_PER_CRITERION = 3


def save(cfg, company, criterion, partner_score, reason, author):
    """Store: tool score, partner score, company + criterion, reason. Returns the stored row id."""
    r = latest_result(cfg, company)
    if not r:
        raise AllInError(f"No saved result for '{company}'. Run it first.")
    g = next((x for x in r["company"] + r["founder"] if x["id"] == criterion), None)
    if not g:
        raise AllInError(f"Unknown criterion '{criterion}'.")
    if not reason.strip() or not author.strip():
        raise AllInError("A reason and an author are required. A correction without a reason is not stored.")
    if not 0 <= partner_score <= 10:
        raise AllInError("Score must be between 0 and 10.")
    tool = g.get("base_score", g["score"])
    if tool is None:
        raise AllInError(f"{criterion} is 'insufficient data' for {company}. There is no tool score to correct.")
    if abs(partner_score - tool) < MIN_MOVE:
        raise AllInError(f"Change of {abs(partner_score - tool):g} is under {MIN_MOVE:g}. Small edits are not stored.")
    p = r["profile"]
    with db(cfg) as con:
        row = con.execute("insert into corrections(ts,company,sector,summary,criterion,tool_score,partner_score,reason,author) "
                          "values(%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
                          (now(), r["name"], p.get("sector"), f"{p.get('sector', '')}; {p.get('one_line', '')}",
                           criterion, tool, partner_score, reason, author)).fetchone()
    return row["id"]


def all_rows(cfg):
    with db(cfg) as con:
        return con.execute("select * from corrections order by id desc").fetchall()


PICK = guard.DATA_LIMITS + """
Task: a partner corrected a criterion score for a past company. Decide which corrections truly apply to the NEW
applicant: same kind of business and the same kind of evidence. Unrelated companies get nothing; when unsure, leave it out.
Return JSON only: {"ids": [int, ...]} using ids from the list."""


def _tok(s):
    return set(re.findall(r"[a-z]{4,}", s.lower()))


def retrieve(cfg, prof, run_id, name):
    """Return {criterion: {delta, ids, note}} to apply to this run. Empty when nothing applies."""
    rows = all_rows(cfg)
    if not rows:
        return {}
    summ = f"{prof.get('sector', '')}; {prof.get('one_line', '')}"
    q = _tok(summ)
    cand = sorted(rows, key=lambda r: -len(q & _tok(r["summary"])))[:12]
    cand = [r for r in cand if r["company"].lower() != prof["company"].lower()]
    if not cand:
        return {}
    listing = "\n".join(f"{r['id']}: [{r['criterion']}] {r['summary']} | tool {r['tool_score']:g} -> partner {r['partner_score']:g} | {r['reason']}"
                        for r in cand)
    res = llm.parse_json(llm.call(cfg, "cheap", PICK, guard.data("applicant", summ) + "\n" + guard.data("past", listing),
                                  run_id, name, "correction_match"))
    ok = {i for i in res.get("ids", []) if isinstance(i, int)}
    use = [r for r in cand if r["id"] in ok]
    out = {}
    for crit in {r["criterion"] for r in use}:
        group = [r for r in use if r["criterion"] == crit]
        # when corrections disagree: the more specific (same sector) wins; tie: the most recent
        group.sort(key=lambda r: (r["sector"] == prof.get("sector"), r["id"]), reverse=True)
        group = group[:MAX_PER_CRITERION]
        lead = group[0]
        delta = max(-CAP, min(CAP, lead["partner_score"] - lead["tool_score"]))
        out[crit] = {"delta": delta, "ids": [r["id"] for r in group],
                     "note": f"{lead['company']}: {lead['reason']} (tool {lead['tool_score']:g} -> partner {lead['partner_score']:g})"}
    return out
