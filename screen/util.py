"""Shared helpers: config, env keys, and the Neon (Postgres) store."""
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REC = ("advance", "flag", "low priority")   # the only outputs. There is no reject.


class AllInError(Exception):
    """Base for every loud error. The run prints it and stops that step."""


def load_env():
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def key_for(cfg, provider):
    name = cfg["keys"][provider]
    val = os.environ.get(name, "")
    if not val:
        raise AllInError(f"Missing key: set {name} in .env (see .env.example).")
    return val


def load_cfg():
    load_env()
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf8"))


def load_rubric(cfg):
    return yaml.safe_load((ROOT / cfg["paths"]["rubric"]).read_text(encoding="utf8"))


def path(cfg, key):
    return ROOT / cfg["paths"][key]


def slug(name):
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- Neon store ----

TABLES = """
create table if not exists runs(run_id text primary key, ts text, kind text default 'screen', live boolean default true);
alter table runs add column if not exists kind text default 'screen';
alter table runs add column if not exists live boolean default true;
create table if not exists outreach(name text primary key, state text, data jsonb, ts text);
create table if not exists contacts(company text, founder text, email text, primary key(company, founder));
create table if not exists handoffs(id serial primary key, ts text, kind text, name text, reason text,
    attempts integer, status text default 'open', run_id text);
create table if not exists corrections(id serial primary key, ts text, company text, sector text, summary text,
    criterion text, tool_score double precision, partner_score double precision, reason text, author text);
create table if not exists tests(id serial primary key, ts text, kind text, live boolean, data jsonb);
create table if not exists recs(run_id text, company text, rec text);
create table if not exists costs(run_id text, applicant text, kind text, model text,
    tok_in integer, tok_out integer, usd double precision, ts text);
create table if not exists overrides(id serial primary key, ts text, author text, company text,
    run_id text, old_rec text, new_rec text, reason_code text, reason text, summary text, tool_output jsonb);
create table if not exists results(run_id text, name text, ts text, data jsonb, primary key(run_id, name));
create table if not exists errors(run_id text, name text, ts text, error text);
create table if not exists cache(key text primary key, value jsonb, ts text);
create table if not exists state(key text primary key, value text);
"""
_ready = set()


def db(cfg=None):
    """Connection to Neon. Use as: with db(cfg) as con: con.execute(sql, params).
    ALLIN_SCHEMA (tests only) puts everything in its own schema."""
    import psycopg
    from psycopg.rows import dict_row
    load_env()
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise AllInError("Missing DATABASE_URL. Set it in .env (Neon connection string).")
    try:
        con = psycopg.connect(url, row_factory=dict_row, autocommit=False, prepare_threshold=None, connect_timeout=15)
    except psycopg.Error as e:
        raise AllInError(f"Cannot reach the Neon database: {e}")
    # Neon's pooler hands each statement to any backend, so search_path must be set inside the same
    # transaction as the work. `with db(cfg) as con:` is one transaction: it commits when the block ends.
    schema = os.environ.get("ALLIN_SCHEMA", "") or "public"
    if schema != "public":
        con.execute(f'create schema if not exists "{schema}"')
    con.execute(f'set local search_path to "{schema}"')
    if schema not in _ready:
        con.execute(TABLES)
        _ready.add(schema)
    return con


def _json(obj):
    from psycopg.types.json import Jsonb
    return Jsonb(obj)


def cache_get(cfg, key):
    with db(cfg) as con:
        row = con.execute("select value from cache where key=%s", (key,)).fetchone()
    return row["value"] if row else None


def cache_put(cfg, key, value):
    with db(cfg) as con:
        con.execute("insert into cache(key,value,ts) values(%s,%s,%s) "
                    "on conflict(key) do update set value=excluded.value, ts=excluded.ts", (key, _json(value), now()))


def state_get(cfg, key, default=""):
    with db(cfg) as con:
        row = con.execute("select value from state where key=%s", (key,)).fetchone()
    return row["value"] if row else default


def state_set(cfg, key, value):
    with db(cfg) as con:
        con.execute("insert into state(key,value) values(%s,%s) on conflict(key) do update set value=excluded.value",
                    (key, value))


def save_result(cfg, run_id, name, data):
    with db(cfg) as con:
        con.execute("insert into results(run_id,name,ts,data) values(%s,%s,%s,%s) "
                    "on conflict(run_id,name) do update set data=excluded.data, ts=excluded.ts",
                    (run_id, name, now(), _json(data)))
        con.execute("delete from errors where name=%s and run_id=%s", (name, run_id))


def save_error(cfg, run_id, name, error):
    with db(cfg) as con:
        con.execute("insert into errors(run_id,name,ts,error) values(%s,%s,%s,%s)", (run_id, name, now(), error))


def latest_results(cfg):
    """Newest result per applicant."""
    with db(cfg) as con:
        rows = con.execute("select distinct on (name) name, data from results order by name, ts desc").fetchall()
    return [r["data"] for r in rows]


def latest_result(cfg, name):
    with db(cfg) as con:
        row = con.execute("select data from results where lower(name)=lower(%s) order by ts desc limit 1", (name,)).fetchone()
    return row["data"] if row else None


def latest_errors(cfg):
    """Errors from runs newer than the applicant's newest good result."""
    with db(cfg) as con:
        return con.execute("select e.name, e.error, e.run_id from errors e where not exists "
                           "(select 1 from results r where r.name=e.name and r.ts > e.ts) "
                           "order by e.ts desc").fetchall()
