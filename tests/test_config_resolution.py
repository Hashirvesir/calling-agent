"""Unit tests for the config-resolution functions — run with:
python tests/test_config_resolution.py

Covers get_llm_config / get_stt_config / get_tts_config / get_pipeline_config
(app/core/*_config.py) — the agent-override-vs-account-default-vs-platform-
default matrix for each. These are pure aside from one DB read
(get_user_settings), so each test monkeypatches
app.core.database.get_user_settings directly rather than hitting Supabase —
every get_*_config function does `from app.core.database import
get_user_settings` INSIDE its own body (not at module level), so patching the
attribute on app.core.database before calling still intercepts it correctly
(the local import resolves against the module's current attribute at call
time).
"""

import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app.core.database as database
from app.core.llm_config import get_llm_config
from app.core.stt_config import get_stt_config
from app.core.tts_config import get_tts_config
from app.core.pipeline_config import get_pipeline_config


class _patch_user_settings:
    """Context manager: temporarily replace database.get_user_settings with
    one that always returns `row` (a dict, or None to simulate no account
    row / no DB configured)."""

    def __init__(self, row):
        self._row = row
        self._original = None

    async def _fake(self, user_id):
        return self._row

    def __enter__(self):
        self._original = database.get_user_settings
        database.get_user_settings = self._fake
        return self

    def __exit__(self, *exc):
        database.get_user_settings = self._original


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# get_llm_config
# ---------------------------------------------------------------------------

def test_llm_platform_default_when_no_account_row_and_no_agent():
    with _patch_user_settings(None):
        provider, model, temperature = run(get_llm_config("u1"))
    assert provider == "groq"
    assert model == "openai/gpt-oss-120b"
    assert temperature is None
    print("PASS  get_llm_config — platform default when no account row, no agent")


def test_llm_account_default_used_when_no_agent_override():
    row = {"llm_provider": "cerebras", "llm_model": "llama-3.3-70b", "llm_temperature": 0.4}
    with _patch_user_settings(row):
        provider, model, temperature = run(get_llm_config("u1"))
    assert (provider, model, temperature) == ("cerebras", "llama-3.3-70b", 0.4)
    print("PASS  get_llm_config — account default used when agent omitted")


def test_llm_agent_override_wins_over_account_default():
    row = {"llm_provider": "groq", "llm_model": "openai/gpt-oss-120b"}
    agent = {"llm_provider": "together", "llm_model": "meta-llama/Llama-3.3-70B"}
    with _patch_user_settings(row):
        provider, model, temperature = run(get_llm_config("u1", agent=agent))
    assert (provider, model) == ("together", "meta-llama/Llama-3.3-70B")
    print("PASS  get_llm_config — agent override wins over account default")


def test_llm_agent_override_requires_both_provider_and_model():
    row = {"llm_provider": "cerebras", "llm_model": "llama-3.3-70b"}
    agent = {"llm_provider": "together", "llm_model": None}  # model missing → not a full override
    with _patch_user_settings(row):
        provider, model, _ = run(get_llm_config("u1", agent=agent))
    assert (provider, model) == ("cerebras", "llama-3.3-70b")
    print("PASS  get_llm_config — agent override ignored when model is missing")


def test_llm_agent_temperature_resolves_independently_of_provider():
    row = {"llm_provider": "cerebras", "llm_model": "llama-3.3-70b", "llm_temperature": 0.4}
    agent = {"llm_temperature": 1.2}  # no provider/model override, just temperature
    with _patch_user_settings(row):
        provider, model, temperature = run(get_llm_config("u1", agent=agent))
    assert (provider, model) == ("cerebras", "llama-3.3-70b")
    assert temperature == 1.2
    print("PASS  get_llm_config — agent temperature overrides independently of provider/model")


def test_llm_invalid_account_provider_falls_back_to_default():
    row = {"llm_provider": "not-a-real-provider", "llm_model": "whatever"}
    with _patch_user_settings(row):
        provider, model, _ = run(get_llm_config("u1"))
    assert provider == "groq"
    print("PASS  get_llm_config — invalid account provider falls back to platform default")


# ---------------------------------------------------------------------------
# get_stt_config  (Deepgram stays in PROVIDERS — see FIXED_MODELS handling)
# ---------------------------------------------------------------------------

def test_stt_platform_default_when_no_account_row_and_no_agent():
    with _patch_user_settings(None):
        provider, model, endpointing_ms = run(get_stt_config("u1"))
    assert provider == "groq"
    assert model == "whisper-large-v3"
    assert endpointing_ms == 1000.0
    print("PASS  get_stt_config — platform default when no account row, no agent")


def test_stt_account_default_together_model_used():
    row = {"stt_provider": "together", "stt_model": "openai/whisper-large-v3-turbo"}
    with _patch_user_settings(row):
        provider, model, _ = run(get_stt_config("u1"))
    assert (provider, model) == ("together", "openai/whisper-large-v3-turbo")
    print("PASS  get_stt_config — account default (together, explicit model)")


def test_stt_deepgram_fixed_model_from_account_default():
    row = {"stt_provider": "deepgram"}
    with _patch_user_settings(row):
        provider, model, _ = run(get_stt_config("u1"))
    assert (provider, model) == ("deepgram", "nova-3-general")
    print("PASS  get_stt_config — deepgram account default resolves its fixed model")


def test_stt_agent_override_fixed_model_provider_needs_only_provider():
    row = {"stt_provider": "together", "stt_model": "openai/whisper-large-v3-turbo"}
    agent = {"stt_provider": "deepgram"}  # no stt_model needed — FIXED_MODELS provider
    with _patch_user_settings(row):
        provider, model, _ = run(get_stt_config("u1", agent=agent))
    assert (provider, model) == ("deepgram", "nova-3-general")
    print("PASS  get_stt_config — agent override to deepgram needs only provider set")


def test_stt_agent_override_together_requires_model():
    row = {"stt_provider": "groq"}
    agent = {"stt_provider": "together", "stt_model": None}  # together needs a model to count
    with _patch_user_settings(row):
        provider, model, _ = run(get_stt_config("u1", agent=agent))
    assert (provider, model) == ("groq", "whisper-large-v3")
    print("PASS  get_stt_config — together agent override ignored without a model")


def test_stt_agent_override_together_with_model_wins():
    row = {"stt_provider": "groq"}
    agent = {"stt_provider": "together", "stt_model": "openai/whisper-large-v3-turbo"}
    with _patch_user_settings(row):
        provider, model, _ = run(get_stt_config("u1", agent=agent))
    assert (provider, model) == ("together", "openai/whisper-large-v3-turbo")
    print("PASS  get_stt_config — together agent override with explicit model wins")


def test_stt_endpointing_resolves_independently_of_provider():
    row = {"stt_provider": "groq", "stt_endpointing_ms": 1500.0}
    agent = {"stt_endpointing_ms": 500.0}  # no provider override, just endpointing
    with _patch_user_settings(row):
        provider, model, endpointing_ms = run(get_stt_config("u1", agent=agent))
    assert (provider, model) == ("groq", "whisper-large-v3")
    assert endpointing_ms == 500.0
    print("PASS  get_stt_config — agent endpointing overrides independently of provider")


def test_stt_invalid_account_provider_falls_back_to_default():
    row = {"stt_provider": "not-a-real-provider"}
    with _patch_user_settings(row):
        provider, model, _ = run(get_stt_config("u1"))
    assert (provider, model) == ("groq", "whisper-large-v3")
    print("PASS  get_stt_config — invalid account provider falls back to platform default")


# ---------------------------------------------------------------------------
# get_tts_config
# ---------------------------------------------------------------------------

def test_tts_platform_default_when_no_account_row_and_no_agent():
    with _patch_user_settings(None):
        provider, model, speed = run(get_tts_config("u1"))
    assert provider == "elevenlabs"
    assert model == "eleven_turbo_v2_5"
    assert speed == 1.0
    print("PASS  get_tts_config — platform default when no account row, no agent")


def test_tts_account_default_used_when_no_agent_override():
    row = {"tts_provider": "uplift", "tts_model": "orator-streaming", "tts_speed": 0.8}
    with _patch_user_settings(row):
        provider, model, speed = run(get_tts_config("u1"))
    assert (provider, model, speed) == ("uplift", "orator-streaming", 0.8)
    print("PASS  get_tts_config — account default used when agent omitted")


def test_tts_agent_override_wins_over_account_default():
    row = {"tts_provider": "elevenlabs", "tts_model": "eleven_turbo_v2_5"}
    agent = {"tts_provider": "uplift", "tts_model": "orator-streaming"}
    with _patch_user_settings(row):
        provider, model, _ = run(get_tts_config("u1", agent=agent))
    assert (provider, model) == ("uplift", "orator-streaming")
    print("PASS  get_tts_config — agent override wins over account default")


def test_tts_agent_override_requires_both_provider_and_model():
    row = {"tts_provider": "elevenlabs", "tts_model": "eleven_turbo_v2_5"}
    agent = {"tts_provider": "uplift", "tts_model": None}  # incomplete override
    with _patch_user_settings(row):
        provider, model, _ = run(get_tts_config("u1", agent=agent))
    assert (provider, model) == ("elevenlabs", "eleven_turbo_v2_5")
    print("PASS  get_tts_config — agent override ignored when model is missing")


def test_tts_agent_speed_resolves_independently_of_provider():
    row = {"tts_provider": "elevenlabs", "tts_model": "eleven_turbo_v2_5", "tts_speed": 0.9}
    agent = {"tts_speed": 1.2}  # no provider/model override, just speed
    with _patch_user_settings(row):
        provider, model, speed = run(get_tts_config("u1", agent=agent))
    assert (provider, model) == ("elevenlabs", "eleven_turbo_v2_5")
    assert speed == 1.2
    print("PASS  get_tts_config — agent speed overrides independently of provider/model")


def test_tts_invalid_account_provider_falls_back_to_default():
    # No tts_model on the row either — get_tts_config resolves the model
    # against whichever provider it lands on (DEFAULT_MODELS[provider] here),
    # it does not itself validate a model string against the provider.
    row = {"tts_provider": "not-a-real-provider"}
    with _patch_user_settings(row):
        provider, model, _ = run(get_tts_config("u1"))
    assert provider == "elevenlabs"
    assert model == "eleven_turbo_v2_5"  # DEFAULT_MODELS[provider] after fallback
    print("PASS  get_tts_config — invalid account provider falls back to platform default")


# ---------------------------------------------------------------------------
# get_pipeline_config
# ---------------------------------------------------------------------------

def test_pipeline_platform_default_when_no_account_row_and_no_agent():
    with _patch_user_settings(None):
        mode, voice = run(get_pipeline_config("u1"))
    assert mode == "cascaded"
    assert voice == "marin"
    print("PASS  get_pipeline_config — platform default when no account row, no agent")


def test_pipeline_account_default_realtime_used_when_no_agent_override():
    row = {"voice_pipeline_mode": "openai_realtime", "realtime_voice": "cedar"}
    with _patch_user_settings(row):
        mode, voice = run(get_pipeline_config("u1"))
    assert (mode, voice) == ("openai_realtime", "cedar")
    print("PASS  get_pipeline_config — account default realtime mode used when agent omitted")


def test_pipeline_agent_cascaded_override_needs_no_voice():
    row = {"voice_pipeline_mode": "openai_realtime", "realtime_voice": "cedar"}
    agent = {"voice_pipeline_mode": "cascaded"}
    with _patch_user_settings(row):
        mode, voice = run(get_pipeline_config("u1", agent=agent))
    assert mode == "cascaded"
    print("PASS  get_pipeline_config — agent cascaded override wins with no voice needed")


def test_pipeline_agent_realtime_override_requires_voice():
    row = {"voice_pipeline_mode": "cascaded"}
    agent = {"voice_pipeline_mode": "grok_voice", "realtime_voice": None}  # no voice → not a real override
    with _patch_user_settings(row):
        mode, voice = run(get_pipeline_config("u1", agent=agent))
    assert mode == "cascaded"
    print("PASS  get_pipeline_config — agent realtime override ignored without a voice")


def test_pipeline_agent_realtime_override_with_voice_wins():
    row = {"voice_pipeline_mode": "cascaded"}
    agent = {"voice_pipeline_mode": "grok_voice", "realtime_voice": "eve"}
    with _patch_user_settings(row):
        mode, voice = run(get_pipeline_config("u1", agent=agent))
    assert (mode, voice) == ("grok_voice", "eve")
    print("PASS  get_pipeline_config — agent realtime override with voice wins")


def test_pipeline_invalid_account_mode_falls_back_to_default():
    row = {"voice_pipeline_mode": "not-a-real-mode"}
    with _patch_user_settings(row):
        mode, voice = run(get_pipeline_config("u1"))
    assert (mode, voice) == ("cascaded", "marin")
    print("PASS  get_pipeline_config — invalid account mode falls back to platform default")


def test_pipeline_invalid_account_voice_falls_back_to_provider_default():
    row = {"voice_pipeline_mode": "openai_realtime", "realtime_voice": "not-a-real-voice"}
    with _patch_user_settings(row):
        mode, voice = run(get_pipeline_config("u1"))
    assert (mode, voice) == ("openai_realtime", "marin")
    print("PASS  get_pipeline_config — invalid account voice falls back to provider default")


if __name__ == "__main__":
    tests = [
        test_llm_platform_default_when_no_account_row_and_no_agent,
        test_llm_account_default_used_when_no_agent_override,
        test_llm_agent_override_wins_over_account_default,
        test_llm_agent_override_requires_both_provider_and_model,
        test_llm_agent_temperature_resolves_independently_of_provider,
        test_llm_invalid_account_provider_falls_back_to_default,
        test_stt_platform_default_when_no_account_row_and_no_agent,
        test_stt_account_default_together_model_used,
        test_stt_deepgram_fixed_model_from_account_default,
        test_stt_agent_override_fixed_model_provider_needs_only_provider,
        test_stt_agent_override_together_requires_model,
        test_stt_agent_override_together_with_model_wins,
        test_stt_endpointing_resolves_independently_of_provider,
        test_stt_invalid_account_provider_falls_back_to_default,
        test_tts_platform_default_when_no_account_row_and_no_agent,
        test_tts_account_default_used_when_no_agent_override,
        test_tts_agent_override_wins_over_account_default,
        test_tts_agent_override_requires_both_provider_and_model,
        test_tts_agent_speed_resolves_independently_of_provider,
        test_tts_invalid_account_provider_falls_back_to_default,
        test_pipeline_platform_default_when_no_account_row_and_no_agent,
        test_pipeline_account_default_realtime_used_when_no_agent_override,
        test_pipeline_agent_cascaded_override_needs_no_voice,
        test_pipeline_agent_realtime_override_requires_voice,
        test_pipeline_agent_realtime_override_with_voice_wins,
        test_pipeline_invalid_account_mode_falls_back_to_default,
        test_pipeline_invalid_account_voice_falls_back_to_provider_default,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
