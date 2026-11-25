"""Founder outreach. The tool drafts; a person sends. Replies are pasted in by a person and classified.
The calendar is simulated (no calendar is connected). Nothing is ever sent or booked for real."""
import re
from datetime import datetime, timedelta, timezone

from . import guard, llm
from .util import AllInError, _json, add_handoff, db, latest_result, now

IST = timezone(timedelta(hours=5, minutes=30))
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ASK = {"traction": "repeat / retention data", "unit_economics": "CAC and margin numbers",
       "field_immersion": "how you learned from customers before building", "data_fluency": "your key numbers (repeat, CAC, margin)"}


class CalendarError(AllInError):
    pass


class SimCalendar:
    """Sample busy times so the flow can be shown. Not a real calendar."""
    def free_slots(self, cfg, n=3, today=None):
        w = cfg["workflow"]
        d = (today or datetime.now(IST)).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=w["min_days_ahead"])
        slots, k = [], 0
        while len(slots) < n and k < 28:
            if DAYS[d.weekday()] in w["call_days"]:
                for i in range(6):                       # 14:00 to 17:00 in 30-minute slots
                    start = d.replace(hour=14) + timedelta(minutes=30 * i)
                    if (d.toordinal() + i) % 3 != 0 and len(slots) < n:      # every third slot is "busy"
                        slots.append(start)
            d += timedelta(days=1)
            k += 1
        if not slots:
            raise CalendarError("No free slot in the next four weeks.")
        return slots

    def book(self, slot):
        return f"SIM-{slot:%Y%m%d%H%M}"


def with_retries(cap, fn):
    """Try up to cap times. Returns (value, attempts). On failure returns (None, attempts) and the last error."""
    err = None
    for attempt in range(1, cap + 1):
        try:
            return fn(), attempt, None
        except Exception as e:
            err = e
    return None, cap, err


def _get(cfg, name):
    with db(cfg) as con:
        row = con.execute("select state, data from outreach where lower(name)=lower(%s)", (name,)).fetchone()
    return row


def get(cfg, name):
    row = _get(cfg, name)
    return {"state": row["state"], **row["data"]} if row else None


def all_records(cfg):
    with db(cfg) as con:
        return {r["name"]: {"state": r["state"], **r["data"]} for r in con.execute("select name, state, data from outreach").fetchall()}


def _save(cfg, name, state, data, tool, note):
    data["events"] = data.get("events", []) + [{"ts": now(), "tool": tool, "note": note}]
    with db(cfg) as con:
        con.execute("insert into outreach(name,state,data,ts) values(%s,%s,%s,%s) "
                    "on conflict(name) do update set state=excluded.state, data=excluded.data, ts=excluded.ts",
                    (name, state, _json(data), now()))


def contacts(cfg, company):
    with db(cfg) as con:
        rows = con.execute("select founder, email from contacts where lower(company)=lower(%s)", (company,)).fetchall()
    return {r["founder"]: r["email"] for r in rows}


def save_contact(cfg, company, founder, email):
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
        raise AllInError("That does not look like an email address.")
    with db(cfg) as con:
        con.execute("insert into contacts values(%s,%s,%s) on conflict(company,founder) do update set email=excluded.email",
                    (company, founder, email.strip()))


def final_rec(cfg, name, r):
    with db(cfg) as con:
        row = con.execute("select new_rec from overrides where company=%s order by id desc limit 1", (r["name"],)).fetchone()
    return row["new_rec"] if row else r["rec"]


def draft(cfg, name, partner_request=False):
    r = latest_result(cfg, name)
    if not r:
        raise AllInError(f"No saved result for '{name}'. Run it first.")
    if get(cfg, r["name"]):
        raise AllInError(f"A draft already exists for {r['name']}. Use Start over to make a new one.")
    rec = final_rec(cfg, name, r)
    if rec != "advance" and not partner_request:
        raise AllInError(f"The call for {r['name']} is '{rec}', so no draft is made unless a partner asks.")
    asks = [ASK[c] for c in r.get("critical_missing", []) if c in ASK]
    asks += [ASK[g["id"]] for g in r["company"] + r["founder"] if g["score"] is None and g["id"] in ASK and ASK[g["id"]] not in asks]
    founders = r["profile"]["founders"]
    book = contacts(cfg, r["name"])
    first = founders[0]["name"].split()[0]
    body = [f"Hi {first},", "",
            f"Thanks for applying to All In Capital. We read your form and deck for {r['name']} and would like a 30-minute call."]
    if asks:
        body.append("Before we speak, it would help to see: " + "; ".join(asks[:3]) + ".")
    body += [f"We usually speak on {' or '.join(d + ' afternoon' for d in cfg['workflow']['call_days'])} (India time). "
             "If you reply with a day that works, we will send times.", "", "Best,", "The All In Capital team", "",
             "[DRAFT for human review. Not sent. No email account is connected.]"]
    to = [book[f["name"]] for f in founders if f["name"] in book]
    data = {"company": r["name"], "to": to, "subject": f"{r['name']} x All In Capital: 30-minute intro call",
            "body": "\n".join(body), "partner_request": partner_request,
            "missing_contacts": [f["name"] for f in founders if f["name"] not in book]}
    _save(cfg, r["name"], "draft_review", data, "draft_founder_email",
          "Draft created, status pending_human_review. Nothing was sent.")


def mark_sent(cfg, name):
    o = get(cfg, name)
    if not o or o["state"] != "draft_review":
        raise AllInError("There is no draft waiting to be marked as sent.")
    _save(cfg, o["company"], "awaiting_reply", _strip(o), "human", "A person reviewed and sent the draft from their own mail client.")


def _strip(o):
    return {k: v for k, v in o.items() if k != "state"}


CLASSIFY = guard.DATA_LIMITS + """
Task: classify a founder's reply to a request for a 30-minute call. Return JSON only:
{"class": "accept"|"decline"|"ambiguous", "why": str}
accept = clearly agrees to talk. decline = clearly says no. ambiguous = hedged, mixed, asks for something else, or unclear.
Do not guess what the founder 'really' means."""
HEDGE = re.compile(r"\b(maybe|not sure|let me check|next month|depends|might|possibly|perhaps|but|however)\b", re.I)


def read_reply(cfg, name, text, run_id="web"):
    """Classify with the model, then downgrade any hedged 'accept' to ambiguous."""
    res = llm.parse_json(llm.call(cfg, "cheap", CLASSIFY, guard.data("reply", text), run_id, name, "classify_reply"))
    cls = res.get("class")
    if cls not in ("accept", "decline", "ambiguous"):
        raise AllInError(f"Reply classifier returned '{cls}'.")
    if cls == "accept" and HEDGE.search(text):
        cls = "ambiguous"
    return cls


def handle_reply(cfg, name, text, cal=None, run_id="web"):
    o = get(cfg, name)
    if not o or o["state"] != "awaiting_reply":
        raise AllInError("A reply can only be recorded after a person has sent the draft.")
    if not text.strip():
        raise AllInError("Paste the reply text.")
    company = o["company"]
    data = _strip(o)
    data["reply"] = text
    cls = read_reply(cfg, company, text, run_id)
    data["reply_class"] = cls
    if cls == "decline":
        _save(cfg, company, "founder_declined", data, "read_founder_reply", "Classified as decline.")
        return
    if cls == "ambiguous":
        why = "The reply is ambiguous; a person should read it."
        _save(cfg, company, "needs_human", data, "read_founder_reply", "Classified as ambiguous.")
        _save(cfg, company, "needs_human", _strip(get(cfg, company)), "handoff", why)
        add_handoff(cfg, "reply", company, why, 0, run_id)
        return
    cal = cal or SimCalendar()
    cap = cfg["scrape"]["retries"]
    data["events"] = data.get("events", []) + [{"ts": now(), "tool": "read_founder_reply", "note": "Classified as accept."}]
    slots, tries, err = with_retries(cap, lambda: cal.free_slots(cfg))
    if err:
        why = f"Calendar read failed {tries} times ({err}). Handed to a person to book by hand."
        _save(cfg, company, "needs_human", data, "handoff", why)
        add_handoff(cfg, "calendar", company, why, tries, run_id)
        return
    data["offered"] = [s.strftime("%A %d/%m %H:%M IST") for s in slots]
    data["events"].append({"ts": now(), "tool": "offer_times", "note": "Offered " + " / ".join(data["offered"])})
    booking, tries, err = with_retries(cap, lambda: cal.book(slots[0]))
    if err:
        why = f"Booking failed {tries} times ({err}). Handed to a person to book by hand."
        _save(cfg, company, "needs_human", data, "handoff", why)
        add_handoff(cfg, "booking", company, why, tries, run_id)
        return
    data["booked"] = {"slot": data["offered"][0], "id": booking, "simulated": True}
    _save(cfg, company, "meeting_booked", data, "book_meeting", f"Booked {data['offered'][0]} (simulated).")
    _save(cfg, company, "meeting_booked", _strip(get(cfg, company)), "update_record", "Record updated.")


def resolve_human(cfg, name, outcome):
    """A person closes a needs_human record."""
    o = get(cfg, name)
    if not o or o["state"] != "needs_human":
        raise AllInError("This record is not waiting for a person.")
    nxt = {"booked": "meeting_booked", "declined": "founder_declined", "awaiting": "awaiting_reply"}.get(outcome)
    if not nxt:
        raise AllInError("Outcome must be booked, declined or awaiting.")
    _save(cfg, o["company"], nxt, _strip(o), "human", f"A person handled it: {outcome}.")
    with db(cfg) as con:
        con.execute("update handoffs set status='resolved' where name=%s and status='open' and kind in ('reply','calendar','booking')",
                    (o["company"],))


def start_over(cfg, name):
    with db(cfg) as con:
        con.execute("delete from outreach where lower(name)=lower(%s)", (name,))
        con.execute("update handoffs set status='resolved' where lower(name)=lower(%s) and status='open' and kind in ('reply','calendar','booking')", (name,))
