"""Cost log in USD per call, reported in INR with a dated public FX rate."""
import re
from datetime import date, datetime

import httpx
import yaml

from .util import AllInError, db, now, path

FX_URL = "https://www.msei.in/markets/currency/historical-data/rbireferenceratearchives"
FX_NOTE = "RBI reference rate USD/INR, as published in the MSEI archive"


def log(cfg, run_id, applicant, kind, model, tok_in, tok_out):
    price = cfg["prices_usd_per_mtok"].get(model)
    if not price:
        raise AllInError(f"No price for model '{model}' in config.yaml. Add it under prices_usd_per_mtok.")
    usd = (tok_in * price["in"] + tok_out * price["out"]) / 1_000_000
    with db(cfg) as con:
        con.execute("insert into costs values(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (run_id, applicant, kind, model, tok_in, tok_out, usd, now()))
    return usd


def fx(cfg):
    f = path(cfg, "fx")
    if not f.exists():
        raise AllInError("No FX rate. Run: python -m screen fx-refresh")
    d = yaml.safe_load(f.read_text(encoding="utf8"))
    age = (date.today() - date.fromisoformat(d["as_of"])).days
    if age > cfg["fx_max_age_days"]:
        raise AllInError(f"FX rate is {age} days old (as of {d['as_of']}). Run: python -m screen fx-refresh")
    return d


def refresh(cfg):
    """Deliberate refresh: read the latest published rate and save it with source and date."""
    r = httpx.get(FX_URL, timeout=cfg["scrape"]["timeout_s"], follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0 (research client)"})
    r.raise_for_status()
    m = re.search(r"Date\s+USDINR[^0-9]*?(\d{2}-[A-Za-z]{3}-\d{4})\s+([\d.]+)", re.sub(r"<[^>]+>", " ", r.text))
    if not m:
        raise AllInError("Could not read the FX table at " + FX_URL + ". Set fx.yaml by hand.")
    as_of = datetime.strptime(m.group(1), "%d-%b-%Y").date().isoformat()
    d = {"usd_inr": float(m.group(2)), "source": FX_NOTE, "source_url": FX_URL,
         "as_of": as_of, "fetched": now()}
    f = path(cfg, "fx")
    f.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf8")
    return d


def inr(cfg, usd):
    return usd * fx(cfg)["usd_inr"]


def applicant_usd(cfg, run_id, applicant):
    with db(cfg) as con:
        row = con.execute("select coalesce(sum(usd),0) s from costs where run_id=%s and applicant=%s",
                          (run_id, applicant)).fetchone()
    return row["s"]


class BudgetReached(AllInError):
    pass


def spent_usd(cfg):
    with db(cfg) as con:
        return con.execute("select coalesce(sum(usd),0) s from costs").fetchone()["s"]


def check_budget(cfg):
    """Refuse a model call once the INR budget is used up, and leave a note for a partner."""
    budget = cfg["budget_inr"]
    spent = inr(cfg, spent_usd(cfg))
    if spent >= budget:
        with db(cfg) as con:
            open_ = con.execute("select 1 from handoffs where kind='budget' and status='open'").fetchone()
            if not open_:
                con.execute("insert into handoffs(ts,kind,name,reason,attempts,status) values(%s,'budget','all',%s,0,'open')",
                            (now(), f"Budget of Rs {budget} reached (spent Rs {round(spent, 2)}). Runs paused. A partner decides whether to raise it."))
        raise BudgetReached(f"Budget of Rs {budget} reached (spent Rs {round(spent, 2)}). No more model calls. Raise budget_inr in config.yaml to go on.")


def summary(cfg):
    """Numbers for the costs page, in INR."""
    rate = fx(cfg)["usd_inr"]
    with db(cfg) as con:
        tot = con.execute("select coalesce(sum(usd),0) u, count(*) n, coalesce(sum(tok_in),0) ti, coalesce(sum(tok_out),0) tout "
                          "from costs").fetchone()
        runs = con.execute("select c.run_id, r.ts rts, coalesce(r.kind,'screen') kind, coalesce(r.live,true) live, "
                           "count(distinct c.applicant) apps, count(*) calls, sum(c.usd) usd from costs c "
                           "left join runs r on r.run_id=c.run_id group by c.run_id, r.ts, r.kind, r.live "
                           "order by max(c.ts) desc").fetchall()
        apps = con.execute("select applicant, count(*) calls, sum(tok_in) ti, sum(tok_out) tout, sum(usd) usd from costs "
                           "group by applicant order by sum(usd) desc").fetchall()
        models = con.execute("select model, count(*) calls, sum(usd) usd from costs group by model order by sum(usd) desc").fetchall()
        steps = con.execute("select kind, count(*) calls, sum(usd) usd from costs group by kind order by sum(usd) desc").fetchall()
        days = con.execute("select substr(ts,1,10) as day, sum(usd) as usd from costs group by 1 order by 1 desc").fetchall()
    f = lambda rows: [{**r, "inr": r["usd"] * rate} for r in rows]
    spent = tot["u"] * rate
    return {"budget": cfg["budget_inr"], "spent": spent, "remaining": cfg["budget_inr"] - spent,
            "used_pct": round(100 * spent / cfg["budget_inr"], 1), "calls": tot["n"], "tok_in": tot["ti"], "tok_out": tot["tout"],
            "runs": f(runs), "apps": f(apps), "models": f(models), "steps": f(steps), "days": f(days),
            "per_app": (spent / max(1, len([a for a in apps if not a["applicant"].startswith(("smoke", "eval:"))]))),
            "fx": fx(cfg)}
