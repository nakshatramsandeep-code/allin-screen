"""Read an applicant's deck and form, then pull a small profile with the cheap model."""
import re

import pymupdf

from . import guard, llm
from .util import AllInError, cache_get, cache_put, path, slug


class DataMissing(AllInError):
    pass


def applicants(cfg):
    d = path(cfg, "seed")
    return sorted(f.name[:-len("_Deck.pdf")] for f in d.glob("*_Deck.pdf"))


def _pages(pdf):
    doc = pymupdf.open(pdf)
    out = {}
    for i, page in enumerate(doc, 1):
        text = page.get_text().strip()
        if text:
            out[f"{pdf.name} p{i}"] = text
    if not out:
        raise DataMissing(f"No text found in {pdf.name}. It may be a scan. Fix the file; nothing was guessed.")
    return out


def load_sources(cfg, name):
    """Return {source id: text}. Source id is the file name and page."""
    d = path(cfg, "seed")
    deck = d / f"{name}_Deck.pdf"
    forms = sorted(d.glob(f"{name}_Form*.pdf"))
    if not deck.exists():
        raise DataMissing(f"Deck not found: {deck}")
    if not forms:
        raise DataMissing(f"Application form not found for {name} in {d}")
    src = _pages(deck)
    src.update(_pages(forms[0]))
    return src


PROFILE_SYS = guard.DATA_LIMITS + """
Task: read the application form and return JSON only:
{"company": str, "website": str|null, "one_line": str, "sector": str, "stage": str,
 "founders": [{"name": str, "role": str, "linkedin": str|null}]}
Copy values exactly as written. Use null when a field is not in the text."""


def profile(cfg, name, sources, run_id):
    key = f"{slug(name)}/profile"
    hit = cache_get(cfg, key)
    if hit:
        return hit
    form_text = "\n".join(t for k, t in sources.items() if "_Form" in k)
    raw = llm.call(cfg, "cheap", PROFILE_SYS, guard.data("form", form_text), run_id, name, "profile")
    p = llm.parse_json(raw)
    norm = lambda s: re.sub(r"\s+", " ", s).lower()
    blob = norm(form_text)
    for fo in p.get("founders", []):
        if norm(fo["name"]) not in blob:
            raise DataMissing(f"{name}: founder name '{fo['name']}' is not in the form. Profile rejected.")
        squash = lambda s: re.sub(r"\s+|https?://|www\.", "", s.lower())
        if fo.get("linkedin") and squash(fo["linkedin"]) not in squash(form_text):
            raise DataMissing(f"{name}: LinkedIn link for {fo['name']} is not in the form. Profile rejected.")
    if not p.get("founders"):
        raise DataMissing(f"{name}: no founders found in the form.")
    cache_put(cfg, key, p)
    return p
