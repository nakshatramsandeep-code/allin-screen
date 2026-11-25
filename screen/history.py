"""Historical test: score the 12 past memos and compare with what the fund decided.
The scorer sees the memo facts only: no decision, no 'why we invested/passed', no portfolio list, no past corrections."""
import re
from datetime import datetime

import pymupdf

from . import cost, gate, checks, guard, score
from .util import AllInError, _json, db, load_rubric, now, path

MEMO_FILE = "MESA_T2_Case 03_Deal Memos.pdf"


def parse(cfg):
    """Return a list of memos: number, name, sector, stage, decision, and the fact blocks."""
    doc = pymupdf.open(path(cfg, "seed") / MEMO_FILE)
    text = "\n".join(p.get_text() for p in doc)
    parts = re.split(r"\nMemo (\d+): ([^\n]+)\n", text)
    memos = []
    for i in range(1, len(parts), 3):
        num, name, body = int(parts[i]), parts[i + 1].strip(), parts[i + 2]
        head = re.search(r"Sector:\s*(.+?)\s*\|\s*Stage:\s*(.+?)\s*\|\s*Source:\s*(.+?)\s*\|\s*Decision:\s*(\w+)", body.replace("\n", " "))
        if not head:
            raise AllInError(f"Memo {num} header not readable.")
        flat = re.sub(r"[ \t]*\n[ \t]*", " ", body)
        def part(a, b):
            m = re.search(re.escape(a) + r"\s*(.*?)\s*(?=" + re.escape(b) + r")", flat, re.S)
            if not m:
                raise AllInError(f"Memo {num}: section '{a}' not found.")
            return m.group(1).strip()
        why = "Why we invested." if "Why we invested." in flat else "Why we passed."
        memos.append({"n": num, "name": name, "sector": head.group(1), "stage": head.group(2), "source": head.group(3),
                      "decision": head.group(4).upper(),
                      "what": part("What it does.", "Founder assessment."),
                      "founders": part("Founder assessment.", "Risks and open questions."),
                      "risks": part("Risks and open questions.", why)})
    if len(memos) != 12:
        raise AllInError(f"Expected 12 memos, found {len(memos)}.")
    return memos


def run(cfg):
    guard.check(cfg)
    cost.fx(cfg)
    rub = load_rubric(cfg)
    run_id = "hist-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    with db(cfg) as con:
        con.execute("insert into runs(run_id,ts,kind,live) values(%s,%s,'history',true)", (run_id, now()))
    rows = []
    for m in parse(cfg):
        guard.check(cfg)
        who = f"memo-{m['n']:02d}"
        src = {f"{who} what it does": m["what"], f"{who} founder assessment": m["founders"],
               f"{who} risks": m["risks"], f"{who} header": f"Sector: {m['sector']}. Stage: {m['stage']}. Source: {m['source']}."}
        res = score.score(cfg, rub, who, src, ["Memo facts only. No application form, no outside data."], "", run_id)
        prof = {"company": m["name"], "sector": m["sector"].split("(")[0].strip(), "stage": m["stage"], "one_line": m["what"][:160]}
        miss = checks.critical_missing(rub, res)
        extra = ["Missing critical data: " + ", ".join(miss)] if miss else []
        g = gate.decide(rub, res, prof, [], {"complete": True, "flags": []}, extra)
        call = g["rec"]
        invested = m["decision"] == "INVESTED"
        result = "abstain" if call == "flag" else ("agree" if (call == "advance") == invested else "disagree")
        low = [x["id"] for x in res["company"] + res["founder"] if x["score"] is None]
        weak = [f"{x['id']} {x['score']}" for x in res["company"] + res["founder"] if x["score"] is not None and x["score"] <= 3]
        rows.append({"memo": m["n"], "name": m["name"], "decision": m["decision"], "call": call,
                     "company_pct": res["company_pct"], "founder_pct": res["founder_pct"],
                     "result": result, "missing": low, "weak": weak})
        print(f"memo {m['n']:2d} {m['name']:22} {m['decision']:9} -> {call:12} {result}")
    data = {"run_id": run_id, "rows": rows,
            "agree": sum(r["result"] == "agree" for r in rows), "disagree": sum(r["result"] == "disagree" for r in rows),
            "abstain": sum(r["result"] == "abstain" for r in rows)}
    with db(cfg) as con:
        con.execute("insert into tests(ts,kind,live,data) values(%s,'history',true,%s)", (now(), _json(data)))
    return data
