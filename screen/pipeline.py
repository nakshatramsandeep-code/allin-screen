"""The run: read, gather, check overlap, score, gate, save, cost."""
import sys
from datetime import datetime

from . import checks, corrections, cost, gate, guard, memory, portfolio, read, scrape, score
from .util import AllInError, db, load_rubric, now, save_error, save_result


def _flags(prof, scraped):
    out = []
    for r in scraped:
        if r["status"] != "ok":
            out.append(f"{r['source']}: {r['status']}. {r['detail']}".strip())
        elif r["detail"]:
            out.append(f"{r['source']}: partly failed. {r['detail']}")
    for f in prof["founders"]:
        if scrape.is_placeholder(f.get("linkedin")):
            out.append(f"LinkedIn link for {f['name']} is a placeholder. Unverified, not looked up. Not held against the founder.")
    if scrape.is_placeholder(prof.get("website")):
        out.append(f"Website {prof['website']} is a placeholder domain. Unverified, not looked up.")
    return out


def assess(cfg, rub, name, prof, sources, scraped, port, run_id):
    """Core of one applicant. Takes data in, returns the result. Used by the run and by the evals."""
    guard.check(cfg)
    flags = _flags(prof, scraped)
    src = dict(sources)
    for r in scraped:
        for it in r["items"]:
            src[f"{r['source']}: {it['url']}"] = it["text"]
    pitch = "\n".join(sources.values())[:2500]
    plist = "\n".join(f"{e['name']} {e['desc']}" for e in port["entries"])
    ov = portfolio.overlap(cfg, name, prof, pitch, port, run_id)
    score.check_numbers([o["reason"] for o in ov], pitch + "\n" + plist, name)
    past = memory.similar(cfg, f"{prof.get('sector', '')} {prof.get('one_line', '')} {pitch[:400]}", run_id, name)
    adj = corrections.retrieve(cfg, prof, run_id, name)
    res = score.score(cfg, rub, name, src, flags, past, run_id, adj)
    conf = checks.consistency(cfg, name, sources, run_id)
    miss = checks.critical_missing(rub, res)
    extra = [f"Deck and form disagree on: {c['what']} (deck: \"{c['deck_quote']}\" / form: \"{c['form_quote']}\")" for c in conf]
    if miss:
        extra.append("Missing critical data: " + ", ".join(miss) + ". Treated as missing, never filled in.")
    g = gate.decide(rub, res, prof, ov, port, extra)
    return {"name": name, "run_id": run_id, "ts": now(), "profile": prof, **g,
            "company": res["company"], "founder": res["founder"],
            "company_pct": res["company_pct"], "founder_pct": res["founder_pct"], "coverage": res["coverage"],
            "injection_attempts": res["injection_attempts"], "note": res["note"],
            "overlaps": ov, "data_flags": flags, "conflicts": conf, "critical_missing": miss,
            "portfolio": {"source": port["source"], "fetched": port["fetched"],
                          "complete": port["complete"], "flags": port["flags"]},
            "scraped": [{"source": r["source"], "status": r["status"], "detail": r["detail"],
                         "items": len(r["items"])} for r in scraped],
            "past_corrections_used": past, "adjustments": adj,
            "summary": memory.summary(prof, g["rec"], g["reasons"])}


def run(cfg, names, refresh=False):
    guard.check(cfg)
    cost.fx(cfg)                                   # fail now if there is no dated FX rate
    rub = load_rubric(cfg)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    with db(cfg) as con:
        con.execute("insert into runs(run_id,ts,kind,live) values(%s,%s,'screen',true)", (run_id, now()))
    port = portfolio.load(cfg, refresh)
    errors, done = [], []
    for name in names:
        guard.check(cfg)
        try:
            sources = read.load_sources(cfg, name)
            prof = read.profile(cfg, name, sources, run_id)
            scraped = scrape.gather(cfg, name, prof, refresh, run_id)
            out = assess(cfg, rub, name, prof, sources, scraped, port, run_id)
            usd = cost.applicant_usd(cfg, run_id, name)
            out["cost_usd"] = round(usd, 5)
            out["cost_inr"] = round(cost.inr(cfg, usd), 2)
            save_result(cfg, run_id, name, out)
            with db(cfg) as con:
                con.execute("insert into recs values(%s,%s,%s)", (run_id, name, out["rec"]))
            done.append(out)
            print(f"{name:12} {out['rec']:13} company {out['company_pct']:5}%  founder {out['founder_pct']:5}%  "
                  f"data {round(out['coverage'] * 100):3}%  Rs {out['cost_inr']}")
        except (guard.Stopped, score.NumberError):
            raise                                  # kill switch and made-up numbers stop the whole run
        except AllInError as e:
            errors.append((name, str(e)))
            scrape.add_handoff(cfg, "scorer", name, f"Screening {name} failed: {e}. Handed to a person; the previous result is kept.", cfg["scrape"]["retries"], run_id)
            print(f"ERROR {name}: {e}", file=sys.stderr)
            save_error(cfg, run_id, name, str(e))
    if errors:
        raise AllInError(f"{len(errors)} applicant(s) failed: " + ", ".join(n for n, _ in errors))
    return run_id, done


def retry(cfg):
    """Redo each applicant a person asked to retry. The cache is bypassed; open hand-offs are closed when it works."""
    with db(cfg) as con:
        todo = con.execute("select distinct name from handoffs where status='retry_requested' and kind in ('fetch','scorer')").fetchall()
    names = [r["name"] for r in todo]
    if not names:
        print("Nothing to retry.")
        return
    run(cfg, names, refresh=True)
    with db(cfg) as con:
        con.execute("update handoffs set status='resolved' where status='retry_requested' and name = any(%s)", (names,))
