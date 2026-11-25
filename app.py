"""Web app for partners. Reads and writes Neon. Runs are started from the command line
(they take longer than a web request allows); everything else is here."""
from flask import Flask, abort, flash, redirect, render_template, request, url_for

from screen import corrections, cost, guard, memory, outreach, stages
from screen.util import REC, AllInError, db, latest_errors, latest_result, load_cfg, load_rubric

app = Flask(__name__)
app.secret_key = "flash-only"          # used only to show one-time messages, not for logins
cfg = load_cfg()


@app.errorhandler(AllInError)
def loud(e):
    return render_template("error.html", msg=str(e)), 500


@app.context_processor
def common():
    return {"kill": guard.is_on(cfg), "REC": REC, "stage_by_id": stages.BY_ID}


def back(default="home"):
    return redirect(request.referrer or url_for(default))


def act(fn, ok):
    """Run an action; show its loud error or a success note. Then go back."""
    try:
        fn()
        flash(ok, "ok")
    except AllInError as e:
        flash(str(e), "err")
    return back()


# ---- pipeline and stages ----

@app.get("/")
def home():
    rows = stages.board(cfg)
    need = [x for x in rows if x["stage"] in ("partner_review", "needs_human")]
    cols = {s["id"]: [x for x in rows if x["stage"] == s["id"]] for s in stages.STAGES}
    booked = [x for x in rows if x["stage"] == "meeting_booked"]
    return render_template("home.html", rows=rows, need=need, cols=cols, stages=stages.STAGES, errs=latest_errors(cfg),
                           flagged=sum(1 for x in cols["partner_review"] if x["flags"]),
                           in_outreach=[x for x in rows if x["outreach"]], booked=booked)


@app.get("/stages")
def stages_page():
    rows = stages.board(cfg)
    return render_template("stages.html", rows=rows, stages=stages.STAGES, funnel=stages.funnel(rows))


@app.get("/applicants")
def applicants():
    rows = stages.board(cfg)
    return render_template("applicants.html", rows=rows, n={c: sum(1 for x in rows if x["call"] == c) for c in REC})


@app.get("/a/<name>")
def applicant(name):
    r = latest_result(cfg, name)
    if not r:
        abort(404)
    mine = [t for t in memory.trail(cfg) if t["company"] == r["name"]]
    return render_template("applicant.html", r=r, trail=mine, codes=cfg["reason_codes"], score=stages.overall(r),
                           tags=stages.flag_tags(r), o=outreach.get(cfg, r["name"]),
                           corr=[c for c in corrections.all_rows(cfg) if c["company"] == r["name"]])


@app.post("/a/<name>/override")
def override(name):
    f = request.form
    return act(lambda: memory.record(cfg, name, f.get("to", ""), f.get("code", ""), f.get("reason", ""), f.get("author", "")),
               "Override saved. The original output is kept.")


# ---- emails ----

@app.get("/emails")
def emails():
    rows = stages.board(cfg)
    return render_template("emails.html", rows=rows,
                           contacts={x["r"]["name"]: outreach.contacts(cfg, x["r"]["name"]) for x in rows},
                           n_booked=sum(1 for x in rows if x["stage"] == "meeting_booked"),
                           n_human=sum(1 for x in rows if x["stage"] == "needs_human"))


@app.post("/emails/<name>/draft")
def email_draft(name):
    return act(lambda: outreach.draft(cfg, name, partner_request=request.form.get("partner") == "1"), "Draft created. Nothing was sent.")


@app.post("/emails/<name>/sent")
def email_sent(name):
    return act(lambda: outreach.mark_sent(cfg, name), "Marked as sent by a person.")


@app.post("/emails/<name>/reply")
def email_reply(name):
    return act(lambda: outreach.handle_reply(cfg, name, request.form.get("text", "")), "Reply read and recorded.")


@app.post("/emails/<name>/resolve")
def email_resolve(name):
    return act(lambda: outreach.resolve_human(cfg, name, request.form.get("outcome", "")), "Recorded.")


@app.post("/emails/<name>/restart")
def email_restart(name):
    return act(lambda: outreach.start_over(cfg, name), "Started over.")


@app.post("/contacts")
def contact():
    f = request.form
    return act(lambda: outreach.save_contact(cfg, f.get("company", ""), f.get("founder", ""), f.get("email", "")), "Email saved.")


# ---- feedback ----

@app.get("/feedback")
def feedback():
    rub = load_rubric(cfg)
    res = {x["r"]["name"]: x["r"] for x in stages.board(cfg)}
    rows = corrections.all_rows(cfg)
    return render_template("feedback.html", res=res, rows=rows, rub=rub, trail=memory.trail(cfg), dis=memory.disagreement(cfg),
                           applied=sum(1 for r in res.values() for g in r["company"] + r["founder"] if g.get("adjustment")))


@app.post("/feedback")
def feedback_save():
    f = request.form

    def go():
        try:
            score = float(f.get("score", ""))
        except ValueError:
            raise AllInError("Enter the new score as a number from 0 to 10.")
        corrections.save(cfg, f.get("company", ""), f.get("criterion", ""), score, f.get("reason", ""), f.get("author", ""))
    return act(go, "Correction stored. It is used on later runs for similar companies, when it applies.")


# ---- retries ----

@app.get("/retries")
def retries():
    with db(cfg) as con:
        rows = con.execute("select * from handoffs order by (status='open') desc, id desc").fetchall()
    return render_template("retries.html", rows=rows, cap=cfg["scrape"]["retries"], never=cfg["scrape"]["never_fetch"])


@app.post("/retries/<int:hid>/<what>")
def retry_action(hid, what):
    status = {"resolve": "resolved", "retry": "retry_requested"}.get(what)
    if not status:
        abort(404)

    def go():
        with db(cfg) as con:
            con.execute("update handoffs set status=%s where id=%s", (status, hid))
    return act(go, "Marked resolved." if what == "resolve" else "Retry requested. Run: python -m screen retry")


# ---- costs ----

@app.get("/costs")
def costs():
    return render_template("costs.html", c=cost.summary(cfg))


# ---- tests ----

@app.get("/tests")
def tests_page():
    def latest(kind):
        with db(cfg) as con:
            row = con.execute("select ts, data from tests where kind=%s order by id desc limit 1", (kind,)).fetchone()
        return row
    return render_template("tests.html", evals=latest("evals"), unit=latest("unit"), hist=latest("history"))


# ---- audit, open points, kill switch ----

@app.get("/audit")
def audit():
    return render_template("audit.html", trail=memory.trail(cfg), dis=memory.disagreement(cfg))


@app.get("/open")
def open_points():
    rub = load_rubric(cfg)
    return render_template("open.html", rub=rub, gate=rub["gate"])


@app.post("/kill")
def kill():
    on = request.form.get("state") == "on"
    guard.set_kill(cfg, on)
    flash("Kill switch " + ("ON: all runs stop." if on else "off."), "ok")
    return back()
