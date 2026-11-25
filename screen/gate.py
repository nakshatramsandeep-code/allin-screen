"""Recommendation gate. Outputs advance, flag or low priority. There is no reject.
When unsure, it flags for a partner."""
import re

from .util import REC


def decide(rub, res, prof, overlaps, port):
    """res: score result. overlaps: list from portfolio.overlap. Returns {rec, reasons, next_action}."""
    g = rub["gate"]
    c, f, cov = res["company_pct"], res["founder_pct"], res["coverage"]
    flag, low_ok = [], True              # flag: reasons that force a partner look

    for o in overlaps:
        flag.append(f"Possible business overlap with portfolio company {o['company']} ({o['level']}): {o['reason']}")
    if not port["complete"]:
        flag.append("Portfolio list is incomplete (" + " ".join(port["flags"]) + "). Overlap not fully checked.")
    sector = (prof.get("sector") or "").lower()
    allowed = rub["checks"]["allowed_sectors"]
    if sector and not any(a in sector for a in allowed):
        flag.append(f"Applicant sector '{prof['sector']}' is outside the four stated sectors. Partners decide (open point 1).")
    stage = (prof.get("stage") or "").lower()
    if re.search(r"series\s*[a-z]|growth|late", stage):
        flag.append(f"Stage '{prof['stage']}' is later than pre-seed or seed (open point 6).")
    if re.search(r"\b(credit|lend\w*|loans?|nbfc|wage access|bnpl|buy-now)\b", f"{sector} {prof.get('one_line') or ''}".lower()):
        flag.append("Lending business. Fit depends on the founders owning credit risk and a credit-risk hire plan (open point 2).")
    if cov < g["min_coverage"]:
        flag.append(f"Only {round(cov * 100)}% of the rubric has data. Too thin to call.")
    if res["injection_attempts"]:
        flag.append("The material contains text aimed at the scorer. It was ignored. Partner should read it.")

    split = (f >= g["strong"] and c < g["weak"]) or (c >= g["strong"] and f < g["weak"])
    if split:
        who = "founder strong, company weak" if f >= g["strong"] else "company strong, founder weak"
        flag.append(f"Split result: {who}. Needs a partner's view.")

    if flag:
        rec = "flag"
    elif c >= g["advance_min"] and f >= g["advance_min"]:
        rec = "advance"
    elif c < g["low_max"] and f < g["low_max"]:
        rec = "low priority"
    else:
        rec = "flag"
        flag.append("Between the cutoffs. Not sure, so a partner decides.")

    assert rec in REC
    reasons = flag + [f"Company {c}% and founder {f}% of full weight; data coverage {round(cov * 100)}% (computed)."]
    if rec == "advance":
        reasons.insert(0, "Both company and founder clear the advance cutoff with enough data.")
    if rec == "low priority":
        reasons.insert(0, "Both sides below the low cutoff. A partner still sees this result; nothing is rejected.")
    nxt = {"advance": "Partner confirms and books the call.",
           "flag": "Partner reviews the reasons and decides.",
           "low priority": "Partner skims; override if the tool missed something."}[rec]
    if overlaps:
        nxt = "Partner speaks to the portfolio founder before anything else (notes, meeting 4)."
    return {"rec": rec, "reasons": reasons, "next_action": nxt}
