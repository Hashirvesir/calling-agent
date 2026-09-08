"""CRUD API for scripts."""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from app.models.script import ScriptCreate, ScriptUpdate
from app.core.auth import get_current_user
from app.core.database import (
    get_all_scripts, get_script_by_id,
    create_script, update_script, delete_script,
)

router = APIRouter(prefix="/api/scripts", tags=["scripts"])


@router.get("")
async def list_scripts(user_id: str = Depends(get_current_user)):
    return {"scripts": await get_all_scripts(user_id)}


@router.get("/{script_id}")
async def get_script(script_id: str, user_id: str = Depends(get_current_user)):
    script = await get_script_by_id(script_id, user_id)
    if not script:
        raise HTTPException(404, "Script not found")
    return script


@router.post("", status_code=201)
async def add_script(body: ScriptCreate, user_id: str = Depends(get_current_user)):
    script = await create_script(
        name=body.name,
        content=body.content,
        language=body.language,
        extraction_fields=body.extraction_fields,
        user_id=user_id,
    )
    if not script:
        raise HTTPException(500, "Failed to create script")
    return script


@router.patch("/{script_id}")
async def edit_script(script_id: str, body: ScriptUpdate, user_id: str = Depends(get_current_user)):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "No fields to update")
    updated = await update_script(script_id, user_id, **fields)
    if not updated:
        raise HTTPException(404, "Script not found or update failed")
    # Invalidate RAG cache for agents using this script, then rebuild now in the
    # background — get_all_agents() already returns the joined script content,
    # so no extra fetch is needed before prewarming.
    # NOTE: per-process only — with uvicorn workers>1 this reaches just the
    # worker serving this PATCH (see the cache comment in app/services/bot.py).
    from app.services.bot import _rag_cache, prewarm_agent_rag_background
    from app.core.database import get_all_agents
    agents = await get_all_agents(user_id)
    for agent in agents:
        if agent.get("script_id") == script_id:
            _rag_cache.pop(f"{user_id}:{agent['id']}", None)
            prewarm_agent_rag_background(agent, user_id)
    return updated


@router.delete("/{script_id}", status_code=204)
async def remove_script(script_id: str, user_id: str = Depends(get_current_user)):
    ok = await delete_script(script_id, user_id)
    if not ok:
        raise HTTPException(404, "Script not found")


class GenerateScriptRequest(BaseModel):
    topic: str
    language: str = "ur"


_GENERATE_MAX_TOKENS = 4096


@router.post("/generate")
async def generate_script(body: GenerateScriptRequest, user_id: str = Depends(get_current_user)):
    """AI-drafted script from scratch, given a short topic description — same
    GPT-4o client as /optimize. Returns both the script content AND suggested
    extraction fields (via the same logic /api/extraction/suggest-fields
    uses) in one call, so a generated script arrives ready to save without a
    separate manual Suggest click. Never touches the DB — the frontend shows
    the result and the user explicitly saves it."""
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(400, "topic must not be empty")

    lang_name = "English" if body.language == "en" else "Urdu"
    system_prompt = (
        "You are an expert writer of voice AI call-center scripts — the reference "
        "material a phone agent retrieves from (via RAG) to answer callers, not a "
        "line-by-line dialogue transcript."
    )
    user_prompt = f"""Write a complete call-center script for a phone AI agent, in {lang_name} \
only (reply ONLY in {lang_name}, even though these instructions are in English), for:

{topic[:2000]}

Rules:
1. Structure the script into "== Heading ==" sections (this exact format — the \
platform's RAG chunker splits on it).
2. Cover, at minimum:
   - A short intro/overview section.
   - Every core service/product implied by the topic, each with a brief \
practical explanation.
   - A "Frequently Asked Questions" section with at least 5 realistic \
question/answer pairs a caller would actually ask.
   - Any required documents, steps, or eligibility the caller needs to know, \
if the topic implies a process (account opening, booking, ordering, etc.).
   - A branch/contact-info section.
3. NEVER invent specific prices, fees, phone numbers, addresses, dates, or \
policies — you don't know the real ones. For each, use a bracketed placeholder \
in {lang_name} instead (e.g. [یہاں قیمت درج کریں] / [enter price here]), so a \
human can fill in the real value before this goes live.
4. Write for a phone conversation — practical and clear, no filler, no \
markdown headers other than the "== ... ==" convention.
5. Output ONLY the script text — no explanation, no markdown code fences, no \
preamble or summary before/after it."""

    try:
        from app.extraction.service import get_extraction_service, suggest_extraction_fields_from_content
        service = get_extraction_service()
        content = await service._call_llm(
            system_prompt, user_prompt,
            max_tokens=_GENERATE_MAX_TOKENS, temperature=0.5,
        )
        content = content.strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.lower().startswith(("markdown", "text")):
                content = content.split("\n", 1)[1] if "\n" in content else ""
            content = content.strip()
        if not content:
            raise HTTPException(500, "Generator returned empty content")

        try:
            fields = await suggest_extraction_fields_from_content(content)
        except Exception as exc:
            # Field suggestion is a bonus, not worth failing the whole
            # generation over — the user can still click Suggest manually.
            logger.warning(f"generate_script: field suggestion failed: {exc}")
            fields = []

        return {"content": content, "extraction_fields": fields}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Script generation failed: {exc}")


class OptimizeScriptRequest(BaseModel):
    content: str
    language: str = "ur"


_OPTIMIZE_MAX_TOKENS = 4096


@router.post("/optimize")
async def optimize_script(body: OptimizeScriptRequest, user_id: str = Depends(get_current_user)):
    """AI-assisted script rewrite — same GPT-4o client extraction/suggest-fields
    already use (see app/extraction/service.py), just with a longer max_tokens
    since a rewritten script is far longer output than a JSON field list.
    Returns the rewritten text only; the frontend shows a before/after diff
    and the user explicitly applies it — this never touches the saved script."""
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "content must not be empty")

    lang_name = "English" if body.language == "en" else "Urdu"
    system_prompt = (
        "You are an expert editor for voice AI call-center scripts. You improve a "
        "draft script's completeness and clarity for a live phone agent to read from "
        "— without changing its purpose, tone, or any facts, prices, or numbers it "
        "already states."
    )
    user_prompt = f"""Improve the following call script. Reply ONLY in {lang_name} \
(match the script's own language exactly, even if these instructions are in English). Rules:

1. Keep the "== Heading ==" section format already used in the script.
2. Fill in gaps: add missing FAQs, flesh out thin sections, clarify vague \
instructions for the agent — but NEVER invent specific prices, phone numbers, \
addresses, or policies that aren't already implied by the script. For anything \
you genuinely can't infer, add a bracketed placeholder like [یہاں تفصیل درج کریں] \
matching the script's language, exactly like the placeholders already in the script.
3. Keep every existing placeholder (bracketed text) as a placeholder — never \
invent a value for one that's already there.
4. Improve wording and structure without changing the facts already present.
5. Output ONLY the improved script text — no explanation, no markdown code \
fences, no preamble or summary before/after it.

Script:
{content[:8000]}"""

    try:
        from app.extraction.service import get_extraction_service
        service = get_extraction_service()
        optimized = await service._call_llm(
            system_prompt, user_prompt,
            max_tokens=_OPTIMIZE_MAX_TOKENS, temperature=0.4,
        )
        optimized = optimized.strip()
        if optimized.startswith("```"):
            optimized = optimized.strip("`")
            if optimized.lower().startswith(("markdown", "text")):
                optimized = optimized.split("\n", 1)[1] if "\n" in optimized else ""
            optimized = optimized.strip()
        if not optimized:
            raise HTTPException(500, "Optimizer returned empty content")
        return {"optimized_content": optimized}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Script optimization failed: {exc}")
