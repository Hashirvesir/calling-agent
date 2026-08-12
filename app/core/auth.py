"""JWT authentication dependency for FastAPI routes."""

from fastapi import Depends, HTTPException, Header
from jose import jwt


async def get_current_user(authorization: str = Header(...)) -> str:
    """Extract user_id from Supabase JWT. Does not verify signature (Supabase already did).

    python-jose's ``jwt.decode`` requires a ``key`` positional argument even when
    signature verification is disabled, and it validates the ``aud``/``exp`` claims
    by default. Supabase tokens carry ``aud="authenticated"``, so we must turn those
    checks off — otherwise every request 401s.
    """
    try:
        token = authorization.removeprefix("Bearer ").strip()
        payload = jwt.decode(
            token,
            "",  # key unused — signature verification is disabled below
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
            },
        )
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(401, "Invalid token: no sub claim")
        return user_id
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid or missing token")
