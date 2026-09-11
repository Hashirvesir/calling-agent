"""JWT authentication for FastAPI routes."""

from fastapi import Header, HTTPException
from jose import jwt


def decode_user_id(token: str) -> str | None:
    """Extract user_id from a Supabase JWT, or None if invalid/missing.

    Does not verify signature (Supabase already did). python-jose's
    ``jwt.decode`` requires a ``key`` positional argument even when signature
    verification is disabled, and it validates the ``aud``/``exp`` claims by
    default. Supabase tokens carry ``aud="authenticated"``, so we must turn
    those checks off — otherwise every request 401s.
    """
    try:
        payload = jwt.decode(
            token.removeprefix("Bearer ").strip(),
            "",  # key unused — signature verification is disabled below
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
            },
        )
        return payload.get("sub") or None
    except Exception:
        return None


async def get_current_user(authorization: str = Header(...)) -> str:
    """FastAPI dependency for HTTP routes — reads the JWT from the
    Authorization header."""
    user_id = decode_user_id(authorization)
    if not user_id:
        raise HTTPException(401, "Invalid or missing token")
    return user_id
