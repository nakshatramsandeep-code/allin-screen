"""Web app for partners: results, override form, audit trail, kill switch. Reads and writes Neon.
Runs are started from the command line (they take longer than a web request allows)."""
from flask import Flask, abort, flash, redirect, render_template, request, url_for

from screen import guard, memory
from screen.util import REC, AllInError, db, latest_errors, latest_result, latest_results, load_cfg, load_rubric

app = Flask(__name__)
app.secret_key = "flash-only"          # used only to show one-time messages, not for logins
cfg = load_cfg()


@app.errorhandler(AllInError)
def loud(e):
    return render_template("error.html", msg=str(e), kill=None), 500


def _partner_sets():
    with db(cfg) as con:
        rows = con.execute("select distinct on (company) company, new_rec, author, reason_code, reason, ts "
                           "from overrides order by company, id desc").fetchall()
    return {r["company"]: r for r in rows}


@app.context_processor
def common():
    return {"kill": guard.is_on(cfg), "REC": REC}


@app.get("/")
def home():
    res = sorted(latest_results(cfg), key=lambda r: ({"flag": 0, "advance": 1, "low priority": 2}[r["rec"]], r["name"]))
    return render_template("home.html", res=res, sets=_partner_sets(), errs=latest_errors(cfg),
                           dis=memory.disagreement(cfg))


@app.get("/a/<name>")
def applicant(name):
    r = latest_result(cfg, name)
    if not r:
        abort(404)
    mine = [t for t in memory.trail(cfg) if t["company"] == r["name"]]
    return render_template("applicant.html", r=r, trail=mine, codes=cfg["reason_codes"])


@app.post("/a/<name>/override")
def override(name):
    f = request.form
    try:
        memory.record(cfg, name, f.get("to", ""), f.get("code", ""), f.get("reason", ""), f.get("author", ""))
        flash("Override saved. The original output is kept.", "ok")
    except AllInError as e:
        flash(str(e), "err")
    return redirect(url_for("applicant", name=name))


@app.get("/audit")
def audit():
    return render_template("audit.html", trail=memory.trail(cfg), dis=memory.disagreement(cfg))


@app.get("/open")
def open_points():
    return render_template("open.html", rub=load_rubric(cfg), gate=load_rubric(cfg)["gate"])


@app.post("/kill")
def kill():
    on = request.form.get("state") == "on"
    guard.set_kill(cfg, on)
    flash("Kill switch " + ("ON: all runs stop." if on else "off."), "ok")
    return redirect(request.referrer or url_for("home"))
