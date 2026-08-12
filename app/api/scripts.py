"""CRUD API for scripts."""

from fastapi import APIRouter, Depends, HTTPException
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
    # Invalidate RAG cache for agents using this script
    from app.services.bot import _rag_cache
    from app.core.database import get_all_agents
    agents = await get_all_agents(user_id)
    for agent in agents:
        if agent.get("script_id") == script_id:
            _rag_cache.pop(f"{user_id}:{agent['id']}", None)
    return updated


@router.delete("/{script_id}", status_code=204)
async def remove_script(script_id: str, user_id: str = Depends(get_current_user)):
    ok = await delete_script(script_id, user_id)
    if not ok:
        raise HTTPException(404, "Script not found")
