# All In Capital screen

First-pass screen of startup applications. The tool recommends (advance, flag, low priority). Partners decide. Nothing is rejected automatically.

- **Command line** (runs, evals, overrides, kill switch): needs the seed PDFs in `data/seed/` (not in this repo).
- **Web app** (`app.py`): results, override form, audit trail, kill switch. Reads and writes Neon.
- **Models:** Gemini only. Names and prices are in `config.yaml`. Rubric is `rubric/rubric.yaml`.

## Setup
```
python -m venv .venv
.venv\Scripts\pip install -r requirements-run.txt
copy .env.example .env      # then fill in GEMINI_API_KEY and DATABASE_URL
```

## Use
```
python -m screen fx-refresh          # dated USD/INR rate, needed for INR cost
python -m screen run                 # all applicants, or: run Pumpwise Talkbook
python -m screen override Pumpwise --to advance --code thesis_fit --reason "..." --author Name
python -m screen kill on|off|status
python -m screen drafts Pumpwise     # email draft + SIMULATED call slot (needs workflow.partner_to)
python -m screen evals               # golden set
python -m unittest discover tests    # critical checks (uses a throwaway Neon schema)
```

## Deploy
Vercel needs one env var: `DATABASE_URL`. The web app does not use the Gemini key.
