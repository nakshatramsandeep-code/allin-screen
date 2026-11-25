"""Email drafts and a call slot. Drafts only: nothing is sent. The slot is a SIMULATION:
no calendar is connected, so nothing is booked."""
from datetime import datetime, timedelta

from .util import AllInError, db, latest_result, path, slug

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def next_slot(cfg, today=None):
    w = cfg["workflow"]
    d = (today or datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=w["min_days_ahead"])
    hh, mm = map(int, w["call_time"].split(":"))
    for _ in range(14):
        if DAYS[d.weekday()] in w["call_days"]:
            return d.replace(hour=hh, minute=mm)
        d += timedelta(days=1)
    raise AllInError("No call day found in workflow.call_days.")


def final_rec(cfg, name):
    """The partner's override wins over the tool's output."""
    with db(cfg) as con:
        row = con.execute("select new_rec from overrides where company=%s order by id desc limit 1", (name,)).fetchone()
    return row["new_rec"] if row else None


def drafts(cfg, name):
    w = cfg["workflow"]
    if not w["partner_to"].strip():
        raise AllInError("Set workflow.partner_to in config.yaml (the partner who receives drafts).")
    r = latest_result(cfg, name)
    if not r:
        raise AllInError(f"No saved result for '{name}'. Run it first.")
    rec = final_rec(cfg, name) or r["rec"]
    if rec == "low priority":
        raise AllInError(f"{name} is 'low priority'. No draft is made; a partner can override first.")
    start = next_slot(cfg)
    end = start + timedelta(minutes=w["call_minutes"])
    deeptech = "deeptech" in (r["profile"].get("sector") or "").lower()
    who = "Kushal" + (f" and {w['deeptech_extra']} (deeptech second call is his)" if deeptech else "")
    body = [f"To: {w['partner_to']}",
            f"Subject: {name}: {rec}, partner review",
            "", f"Tool recommendation: {r['rec']}" + (f" (partner set: {rec})" if rec != r["rec"] else ""),
            "Reasons:"] + [f"- {x}" for x in r["reasons"]] + [
            "", f"Next action: {r['next_action']}",
            "", f"SIMULATED call slot (nothing booked, no calendar connected): {start:%A %d %b %Y %H:%M} IST, "
                f"{w['call_minutes']} min. Attendees: {who}.",
            "", "Suggested note to the founder (edit before sending; this tool does not send):",
            f"Hi, thanks for applying with {r['profile']['company']}. We would like a short call. "
            f"Would {start:%A %d %b} at {start:%H:%M} IST work?"]
    out = path(cfg, "out") / "drafts"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{slug(name)}.txt").write_text("\n".join(body), encoding="utf8")
    ics = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//screen//simulation//EN", "BEGIN:VEVENT",
           f"UID:{slug(name)}-{start:%Y%m%d}@screen-simulation",
           f"DTSTART;TZID=Asia/Kolkata:{start:%Y%m%dT%H%M%S}", f"DTEND;TZID=Asia/Kolkata:{end:%Y%m%dT%H%M%S}",
           f"SUMMARY:SIMULATED - not booked - call with {r['profile']['company']}", "STATUS:TENTATIVE",
           "END:VEVENT", "END:VCALENDAR"]
    (out / f"{slug(name)}.ics").write_text("\n".join(ics), encoding="utf8")
    return out / f"{slug(name)}.txt"
