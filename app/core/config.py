"""Central settings — all environment variables in one place."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Supabase
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str

    # AI services (system-level — platform pays, shared across all users)
    openai_api_key: str
    uplift_api_key: str           # Urdu TTS (Orator)
    groq_api_key: str = ""        # Fast STT (Whisper-large-v3-turbo) + LLM (Llama) for low-latency calls
    # English TTS (ElevenLabs). voice_id is the system default; an agent may
    # override it via agents.voice_english once that holds an ElevenLabs voice ID.
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_turbo_v2_5"

    # Resend — transactional email for the custom signup/password-reset code
    # flows (app/api/auth_flows.py). Sent from the invenco.pk domain, which
    # must be verified (SPF/DKIM) in the Resend dashboard.
    resend_api_key: str = ""
    resend_from_email: str = "Invenco <no-reply@invenco.pk>"

    # Server
    public_host: str = ""
    port: int = 7860

    # Redis — shared state across multiple uvicorn workers (active-call dedup,
    # outbound-call registry, extraction dedup). Empty string = single-worker
    # mode, falls back to in-process memory (see app/core/redis_client.py).
    redis_url: str = ""

    # Uplift voice IDs — system defaults, overridable per-agent in DB
    voice_urdu_default: str = "v_8eelc901"
    voice_urdu_gen_z: str = "v_kwmp7zxt"
    voice_urdu_dada_jee: str = "v_yypgzenx"
    voice_urdu_news: str = "v_30s70t3a"
    voice_english: str = "v_8eelc901"

    # Cost estimation rates (USD), used only for the call-detail cost breakdown.
    # Groq: real published rates from groq.com/pricing for the exact models bot.py
    #   uses. llama-3.3-70b-versatile: $0.59/1M input tokens, $0.79/1M output
    #   tokens. whisper-large-v3-turbo: $0.04/hour of audio -> $0.04/60 per minute.
    #   Groq is the PRIMARY LLM+STT provider for live calls (see app/services/bot.py)
    #   — these rates drive the live-call cost breakdown, not the OpenAI ones below.
    # OpenAI GPT-4o: current published pricing. Only actually billed for a live
    #   call if the Groq LLM errors mid-call and ServiceSwitcherStrategyFailover
    #   falls back to OpenAI (see app/services/bot.py's openai_llm_fallback) — a
    #   rare edge case. The cost breakdown always assumes Groq rates, so a call
    #   that hit this fallback will be slightly under-reported (Groq is cheaper
    #   than GPT-4o). OpenAI Whisper rate is unused for live-call cost now but
    #   kept in case per-call STT ever needs it elsewhere.
    # ElevenLabs: user's plan is $6 / 30,000 credits -> $0.0002/credit at the
    #   standard 1-credit-per-character rate. But elevenlabs.io/pricing confirms
    #   Turbo/Flash models (bot uses eleven_turbo_v2_5) bill at 0.5 credits/char,
    #   not 1 -> real rate is half: $0.0001/character. Cross-checked against
    #   actual call_metrics usage vs. observed ElevenLabs billing and it matches
    #   (~$0.000105/char measured), confirming the 0.5-credit Turbo discount.
    # UpliftAI: user's plan is $5 / 100,000 credits -> $0.00005/credit, same
    #   1 credit = 1 character assumption (unconfirmed against UpliftAI docs).
    # Telnyx: real published pay-as-you-go rate (inbound, standard 10-digit
    #   number) from the user's Telnyx pricing page. The plain PSTN outbound
    #   termination rate wasn't visible on that page (only WhatsApp/bundle
    #   rates were) — outbound calls currently reuse the inbound rate as an
    #   approximation. Recording is a real, separate per-minute charge (the
    #   bot records every call — see app/api/webhooks.py:_start_recording).
    cost_groq_llm_input_per_1m: float = 0.59
    cost_groq_llm_output_per_1m: float = 0.79
    cost_groq_whisper_per_minute: float = 0.04 / 60
    cost_openai_gpt4o_input_per_1m: float = 2.50
    cost_openai_gpt4o_output_per_1m: float = 10.00
    cost_openai_whisper_per_minute: float = 0.006
    cost_elevenlabs_per_character: float = 0.0001
    cost_uplift_per_character: float = 0.00005
    cost_telnyx_per_minute: float = 0.0035
    cost_telnyx_recording_per_minute: float = 0.002

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
