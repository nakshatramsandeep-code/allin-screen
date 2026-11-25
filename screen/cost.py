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
