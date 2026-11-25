"""Command line.  python -m screen <command> -h"""
import argparse
import sys

from . import corrections, cost, guard, history, memory, outreach, pipeline, read, workflow
from .util import AllInError, REC, load_cfg


def main():
    ap = argparse.ArgumentParser(prog="screen", description="First-pass screen for All In Capital. Partners decide.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="screen applicants")
    r.add_argument("names", nargs="*", help="applicant names (default: all)")
    r.add_argument("--refresh", action="store_true", help="scrape again instead of using the cache")
    o = sub.add_parser("override", help="record a partner correction")
    o.add_argument("company")
    o.add_argument("--to", required=True, choices=REC)
    o.add_argument("--code", required=True)
    o.add_argument("--reason", required=True)
    o.add_argument("--author", required=True)
    k = sub.add_parser("kill", help="kill switch")
    k.add_argument("state", choices=["on", "off", "status"])
    sub.add_parser("fx-refresh", help="read the latest public USD/INR reference rate")
    d = sub.add_parser("drafts", help="email draft and simulated call slot")
    d.add_argument("company")
    sub.add_parser("evals", help="run the golden set")
    sub.add_parser("unittests", help="run the critical unit tests and store the result")
    sub.add_parser("history", help="historical test: score the 12 memos and compare with the fund's decisions")
    sub.add_parser("retry", help="redo applicants whose retry was requested on the web, and failed ones")
    a = ap.parse_args()
    cfg = load_cfg()
    try:
        if a.cmd == "run":
            names = a.names or read.applicants(cfg)
            pipeline.run(cfg, names, a.refresh)
        elif a.cmd == "override":
            memory.record(cfg, a.company, a.to, a.code, a.reason, a.author)
            print("Saved.")
        elif a.cmd == "kill":
            if a.state == "status":
                print("ON" if guard.is_on(cfg) else "off")
            else:
                guard.set_kill(cfg, a.state == "on")
                print("Kill switch", a.state)
        elif a.cmd == "fx-refresh":
            print(cost.refresh(cfg))
        elif a.cmd == "drafts":
            print(workflow.drafts(cfg, a.company))
        elif a.cmd == "history":
            d = history.run(cfg)
            print(f"agree {d['agree']}, disagree {d['disagree']}, partner review {d['abstain']}")
        elif a.cmd == "unittests":
            from evals import run_evals
            sys.exit(run_evals.run_unit(cfg))
        elif a.cmd == "retry":
            pipeline.retry(cfg)
        elif a.cmd == "evals":
            from evals import run_evals
            sys.exit(run_evals.main(cfg))
    except AllInError as e:
        print(f"\nSTOPPED: {e}", file=sys.stderr)
        sys.exit(2)


main()
