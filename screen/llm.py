"""One call point for every model. Checks the kill switch, retries, logs cost."""
import json
import re
import time

from . import cost, guard
from .util import AllInError, key_for


class LLMError(AllInError):
    pass


def _gemini(cfg, model, system, user, want_json):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key_for(cfg, "gemini"))
    conf = types.GenerateContentConfig(
        system_instruction=system, temperature=0,
        response_mime_type="application/json" if want_json else None)
    r = client.models.generate_content(model=model, contents=user, config=conf)
    u = r.usage_metadata
    out = (u.candidates_token_count or 0) + (getattr(u, "thoughts_token_count", 0) or 0)
    return r.text or "", u.prompt_token_count or 0, out


def call(cfg, role, system, user, run_id, applicant, kind, want_json=True):
    """role is scorer, cheap or checker (a key under models in config.yaml)."""
    model = cfg["models"][role]
    if not model.startswith("gemini"):
        raise LLMError(f"Model '{model}' is not a Gemini model. This app uses the Gemini API only.")
    last = None
    for attempt in range(3):
        guard.check(cfg)
        cost.check_budget(cfg)
        try:
            text, tin, tout = _gemini(cfg, model, system, user, want_json)
            cost.log(cfg, run_id, applicant, kind, model, tin, tout)
            if not text.strip():
                raise LLMError("empty reply")
            return text
        except AllInError:
            raise
        except Exception as e:
            if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e)[:20]:
                raise LLMError(f"{model}: Gemini quota used up for this key (free tier is limited per day). "
                               "Wait for the reset or enable billing on the key's project. " + str(e)[:160])
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"{model} failed 3 times for {applicant} ({kind}): {last}")


def parse_json(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        raise LLMError(f"Model reply was not valid JSON: {e}. Reply started: {t[:200]!r}")
