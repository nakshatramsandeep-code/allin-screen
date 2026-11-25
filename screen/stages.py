"""Eight stages. Each applicant is in exactly one. Stages come from the screening call and the outreach state,
never typed in by hand."""
from . import outreach
from .util import db, latest_results

STAGES = [
    {"id": "partner_review", "label": "Partner review", "who": "Partner",
     "how": "Screened, and the call is 'flag': a score between the cutoffs, or any flag (portfolio overlap, deck/form mismatch, later stage, outside sectors, lending, missing critical data, text aimed at the scorer).",
     "next": "A partner reads the page and decides. Nothing moves on its own."},
    {"id": "take_call", "label": "Take the call", "who": "Partner",
     "how": "Screened, both scores at or above the advance cutoff, no flags, enough data.",
     "next": "A draft email is prepared for a human to review."},
    {"id": "recommended_no", "label": "Recommended no", "who": "Partner",
     "how": "Screened, both scores below the low cutoff, no flags. 'Low priority' is a recommendation only.",
     "next": "A partner confirms or overrides. Nothing is auto-rejected."},
    {"id": "draft_review", "label": "Draft to review", "who": "Human",
     "how": "An applicant has a drafted founder email.",
     "next": "A person reviews and sends it from their own mail client, then marks it sent."},
    {"id": "awaiting_reply", "label": "Awaiting reply", "who": "Founder",
     "how": "The draft was sent by a person.",
     "next": "The founder replies; a person pastes the reply in; it is read and classified."},
    {"id": "needs_human", "label": "Needs a human", "who": "Human",
     "how": "The reply was ambiguous, or a calendar read or booking failed after the retry cap.",
     "next": "A person reads the reply or books by hand."},
    {"id": "meeting_booked", "label": "Meeting booked", "who": "Partner",
     "how": "A slot (Tuesday or Thursday afternoon, 30 minutes) is booked. In this build the calendar is simulated.",
     "next": "Partner prepares for the call. (Deeptech: the second call is Aditya's.)"},
    {"id": "founder_declined", "label": "Founder declined", "who": "Partner",
     "how": "The founder said no.", "next": "Partner informed. Close the record."},
]
BY_ID = {s["id"]: s for s in STAGES}
CALL_STAGE = {"flag": "partner_review", "advance": "take_call", "low priority": "recommended_no"}


def overall(r):
    """Score out of 10: the average of company and founder."""
    return round((r["company_pct"] + r["founder_pct"]) / 20, 1)


def board(cfg):
    """Applicants with their final call and stage."""
    res = latest_results(cfg)
    out_ = outreach.all_records(cfg)
    with db(cfg) as con:
        sets = {r["company"]: r["new_rec"] for r in con.execute(
            "select distinct on (company) company, new_rec from overrides order by company, id desc").fetchall()}
    rows = []
    for r in res:
        call = sets.get(r["name"], r["rec"])
        o = out_.get(r["name"])
        stage = o["state"] if o else CALL_STAGE[call]
        flags = [x for x in r["reasons"] if not x.startswith(("Both ", "Company ", "Between"))]
        rows.append({"r": r, "call": call, "tool_call": r["rec"], "score": overall(r), "stage": stage,
                     "outreach": o, "flags": flag_tags(r), "n_flags": len(flags)})
    return sorted(rows, key=lambda x: x["r"]["name"])


def flag_tags(r):
    tags = []
    t = " ".join(r["reasons"]).lower()
    for key, label in (("overlap with portfolio", "portfolio conflict"), ("deck and form disagree", "deck form inconsistency"),
                       ("missing critical data", "missing critical data"), ("aimed at the scorer", "possible prompt injection"),
                       ("later than pre-seed", "later stage"), ("outside the four", "outside sectors"),
                       ("lending business", "lending"), ("too thin", "thin data")):
        if key in t:
            tags.append(label)
    if any("WRONG COMPANY" in (s.get("detail") or "") for s in r.get("scraped", [])):
        tags.append("wrong company dropped")
    return tags


def funnel(rows):
    n = len(rows)
    started = [x for x in rows if x["outreach"]]
    replied = [x for x in started if x["outreach"].get("reply")]
    booked = [x for x in rows if x["stage"] == "meeting_booked"]
    return [("Received", n), ("Screened", n), ("Take the call or partner review", sum(1 for x in rows if x["call"] != "low priority")),
            ("Outreach started", len(started)), ("Founder replied", len(replied)), ("Meeting booked", len(booked))]
