"""Fund portfolio from the public site, dated and sourced, plus the business-overlap check."""
import hashlib
import json

from bs4 import BeautifulSoup

from . import guard, llm, scrape
from .util import AllInError, cache_get, cache_put, now


def _parse(html_text):
    """Each company sits in a popup block: title, category, note, link."""
    soup = BeautifulSoup(html_text, "html.parser")
    entries = []
    for pop in soup.select("div.popup"):
        t = pop.select_one(".portfolio-card--title")
        name = t.get_text(" ", strip=True) if t else ""
        if not name:
            continue
        c = pop.select_one(".portfolio-card--category")
        p = pop.find("p")
        entries.append({"name": name, "category": c.get_text(" ", strip=True) if c else "",
                        "desc": " ".join(p.get_text(" ").split()) if p else ""})
    return entries


def load(cfg, refresh=False):
    """Return {entries, source, fetched, complete, flags}. Never silent about gaps."""
    hit = None if refresh else cache_get(cfg, "_fund/portfolio")
    if hit:
        return hit
    fund = cfg["fund"]
    flags, entries = [], []
    st, body = scrape.fetch(cfg, fund["portfolio_url"])
    if st == "ok":
        entries = _parse(body)
        for e in entries:
            e["source"] = fund["portfolio_url"]
        if len(entries) < fund["expected_min"]:
            flags.append(f"Site lists {len(entries)} companies. Press reports at least {fund['expected_min']} "
                         f"({fund['expected_source']}) and the site says 100+ teams. List is incomplete.")
        if not entries:
            flags.append("Portfolio page was read but no companies were parsed. List is incomplete.")
    else:
        flags.append(f"Could not read {fund['portfolio_url']}: {st} {body}. Using the seed floor only. List is incomplete.")
    have = {e["name"].lower() for e in entries}
    for s in fund["seed_floor"]:
        if s["name"].lower() not in have:
            entries.append({"name": s["name"], "category": "", "desc": s["desc"], "source": "deal memos (seed floor)"})
    data = {"entries": entries, "source": fund["portfolio_url"], "fetched": now(),
            "complete": not flags, "flags": flags}
    cache_put(cfg, "_fund/portfolio", data)
    return data


SYS = guard.DATA_LIMITS + """
Task: compare the applicant's BUSINESS with each portfolio company's business. Judge what they do and who they
sell to, not the names. Return JSON only: {"overlaps": [{"company": str, "level": "possible"|"likely", "reason": str}]}.
List only portfolio companies that could compete or overlap. Use an empty list if none. Company must be copied
exactly from the portfolio list. The reason must name what both do."""


def overlap(cfg, name, prof, pitch_text, port, run_id):
    """Cheap-model check. Result cached by applicant text and portfolio."""
    key = hashlib.sha1((pitch_text + json.dumps([e["name"] for e in port["entries"]])).encode()).hexdigest()[:12]
    ckey = f"_overlap/{name.lower().replace(' ', '')}_{key}"
    hit = cache_get(cfg, ckey)
    if hit is not None:
        return hit
    plist = "\n".join(f"- {e['name']}: {e['category']} {e['desc']}".strip() for e in port["entries"])
    user = (guard.data("applicant", f"{prof['company']}: {prof['one_line']}\n{pitch_text}")
            + "\n" + guard.data("portfolio", plist))
    res = llm.parse_json(llm.call(cfg, "cheap", SYS, user, run_id, name, "overlap"))
    known = {e["name"] for e in port["entries"]}
    out = []
    for o in res.get("overlaps", []):
        if o.get("company") not in known:
            raise AllInError(f"{name}: overlap check named '{o.get('company')}', which is not in the portfolio list.")
        if o.get("level") not in ("possible", "likely"):
            raise AllInError(f"{name}: bad overlap level '{o.get('level')}'.")
        out.append(o)
    cache_put(cfg, ckey, out)
    return out
