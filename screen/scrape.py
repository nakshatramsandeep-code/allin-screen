"""Public research per applicant: LinkedIn, Reddit, YouTube, podcasts.
Direct fetches only, bounded timeouts, no bypassing blocks. Every result is cached.
A failure is recorded as a visible flag. Nothing is filled in."""
import html
import json
import re
import time
from urllib.parse import quote, urlparse

import httpx
from bs4 import BeautifulSoup

from . import guard
from .util import cache_get, cache_put, now, slug

UA = {"User-Agent": "Mozilla/5.0 (research client; respects blocks)"}
FETCHES = {"count": 0}          # counted so tests can prove the cache works


def is_placeholder(url):
    """Seed links use invented domains and handles. Do not look them up."""
    if not url:
        return False
    u = url if "//" in url else "https://" + url
    host = urlparse(u).netloc.lower()
    last = host.rsplit(".", 1)[-1] if "." in host else host
    return last in ("example", "test", "invalid", "localhost") or "example" in u.lower()


def fetch(cfg, url, headers=None):
    """Return (status, text). status: ok, blocked, not_found, failed."""
    s = cfg["scrape"]
    err = ""
    for i in range(s["retries"]):
        guard.check(cfg)
        FETCHES["count"] += 1
        try:
            r = httpx.get(url, timeout=s["timeout_s"], follow_redirects=True,
                          headers={**UA, **(headers or {})})
            if r.status_code == 200:
                return "ok", r.text
            if r.status_code == 404:
                return "not_found", ""
            if r.status_code in (401, 403, 999):
                return "blocked", f"HTTP {r.status_code}"     # an access block: do not try to get round it
            err = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            err = type(e).__name__
        time.sleep(s["backoff_s"] * (i + 1))
    return "failed", f"{err} after {s['retries']} tries"


def _clean(text, limit):
    return re.sub(r"\s+", " ", html.unescape(text)).strip()[:limit]


def _relevant(text, names):
    t = text.lower()
    return any(n.lower() in t for n in names if n)


def _record(source, status, items=None, detail=""):
    return {"source": source, "status": status, "detail": detail, "items": items or [], "fetched": now()}


def _linkedin(cfg, prof):
    items, notes = [], []
    for f in prof["founders"]:
        url = f.get("linkedin")
        if not url:
            notes.append(f"{f['name']}: no LinkedIn link given")
        elif is_placeholder(url):
            notes.append(f"{f['name']}: link is a placeholder ({url}). Unverified. Not looked up.")
        else:
            full = url if url.startswith("http") else "https://" + url
            st, body = fetch(cfg, full)
            if st == "ok":
                title = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
                items.append({"url": full, "title": f["name"], "text": _clean(title, cfg["scrape"]["max_chars"])})
            else:
                notes.append(f"{f['name']}: {st} ({body})")
    status = "ok" if items else ("placeholder" if any("placeholder" in n for n in notes) else "no_data")
    return _record("linkedin", status, items, "; ".join(notes))


def _reddit(cfg, names):
    items, bad = [], []
    for n in names:
        st, body = fetch(cfg, f"https://www.reddit.com/search.json?q={quote(chr(34) + n + chr(34))}&limit=5")
        if st != "ok":
            bad.append(f"{n}: {st} {body}")
            continue
        try:
            posts = json.loads(body)["data"]["children"]
        except (ValueError, KeyError):
            bad.append(f"{n}: unreadable reply")
            continue
        for p in posts:
            d = p["data"]
            text = f"{d.get('title', '')} {d.get('selftext', '')}"
            if _relevant(text, names[:1]):   # the company name must appear; a founder name alone could be someone else
                items.append({"url": "https://www.reddit.com" + d.get("permalink", ""),
                              "title": d.get("title", ""), "text": _clean(text, cfg["scrape"]["max_chars"])})
    return _finish("reddit", items, bad, cfg)


def _youtube(cfg, names):
    items, bad = [], []
    for n in names:
        st, body = fetch(cfg, f"https://www.youtube.com/results?search_query={quote(n)}")
        if st != "ok":
            bad.append(f"{n}: {st} {body}")
            continue
        found = re.findall(r'"videoId":"([\w-]{11})".{0,400}?"title":\{"runs":\[\{"text":"(.*?)"\}', body)
        seen = set()
        for vid, title in found:
            if vid in seen or not _relevant(title, names[:1]):
                continue
            seen.add(vid)
            st2, page = fetch(cfg, f"https://www.youtube.com/watch?v={vid}")
            m = re.search(r'"captionTracks":\[\{"baseUrl":"(.*?)"', page) if st2 == "ok" else None
            if not m:
                bad.append(f"{vid}: no transcript available")
                continue
            st3, xml = fetch(cfg, m.group(1).encode().decode("unicode_escape"))
            if st3 != "ok":
                bad.append(f"{vid}: transcript {st3}")
                continue
            text = " ".join(re.findall(r"<text[^>]*>(.*?)</text>", xml, re.S))
            items.append({"url": f"https://www.youtube.com/watch?v={vid}", "title": html.unescape(title),
                          "text": _clean(text, cfg["scrape"]["max_chars"])})
    return _finish("youtube", items, bad, cfg)


def _podcast(cfg, names):
    items, bad = [], []
    for n in names:
        st, body = fetch(cfg, f"https://itunes.apple.com/search?media=podcast&entity=podcastEpisode&limit=5&term={quote(n)}")
        if st != "ok":
            bad.append(f"{n}: {st} {body}")
            continue
        try:
            rows = json.loads(body)["results"]
        except (ValueError, KeyError):
            bad.append(f"{n}: unreadable reply")
            continue
        for r in rows:
            text = f"{r.get('trackName', '')}. {r.get('description', '')}"
            if _relevant(text, names[:1]):   # the company name must appear; a founder name alone could be someone else
                items.append({"url": r.get("trackViewUrl", ""), "title": r.get("trackName", ""),
                              "text": _clean(text, cfg["scrape"]["max_chars"])})
    return _finish("podcast", items, bad, cfg)


def _finish(source, items, bad, cfg):
    items = items[: cfg["scrape"]["max_items"]]
    if items:
        return _record(source, "ok", items, "; ".join(bad))
    if bad:
        return _record(source, "failed", [], "; ".join(bad))
    return _record(source, "no_relevant_results", [], "Searched. Nothing mentioning the company or founders. This is missing data, not a negative.")


def gather(cfg, name, prof, refresh=False):
    """Return a list of source records. Cached per company and source; never fetched twice."""
    names = [prof["company"]] + [f["name"] for f in prof["founders"]]
    jobs = {"linkedin": lambda: _linkedin(cfg, prof),
            "reddit": lambda: _reddit(cfg, names),
            "youtube": lambda: _youtube(cfg, names),
            "podcast": lambda: _podcast(cfg, names)}
    out = []
    for src, job in jobs.items():
        key = f"{slug(name)}/{src}"
        hit = None if refresh else cache_get(cfg, key)
        if hit:
            out.append(hit)
            continue
        guard.check(cfg)
        rec = job()
        cache_put(cfg, key, rec)
        out.append(rec)
    return out
