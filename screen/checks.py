"""Checks beside scoring: deck versus form contradictions, and missing critical data."""
import hashlib
import re

from . import guard, llm
from .util import cache_get, cache_put, slug

SYS = guard.DATA_LIMITS + """
Task: compare the pitch deck with the application form for the same company. List only real contradictions:
the same fact stated two different ways (for example different revenue, customer counts, team size, amount raised).
Differences in detail or wording are not contradictions. Return JSON only:
{"conflicts": [{"what": str, "deck_quote": str, "form_quote": str}]}
Each quote must be copied exactly from its own text. Use an empty list if there are none."""


def _norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


def consistency(cfg, name, sources, run_id):
    """Returns verified conflicts. A conflict whose quotes are not in the texts is dropped."""
    deck = "\n".join(t for k, t in sources.items() if "_Deck" in k)
    form = "\n".join(t for k, t in sources.items() if "_Form" in k)
    if not deck or not form:
        return []
    key = f"_consistency/{slug(name)}/{hashlib.sha1((deck + form).encode()).hexdigest()[:10]}"
    hit = cache_get(cfg, key)
    if hit is not None:
        return hit
    res = llm.parse_json(llm.call(cfg, "cheap", SYS, guard.data("deck", deck) + "\n" + guard.data("form", form),
                                  run_id, name, "consistency"))
    out = [c for c in res.get("conflicts", [])
           if c.get("deck_quote") and c.get("form_quote")
           and _norm(c["deck_quote"]) in _norm(deck) and _norm(c["form_quote"]) in _norm(form)]
    cache_put(cfg, key, out)
    return out


def critical_missing(rub, res):
    """Names of critical criteria with no data."""
    need = set(rub.get("critical", []))
    return [g["id"] for g in res["company"] + res["founder"] if g["id"] in need and g["score"] is None]
