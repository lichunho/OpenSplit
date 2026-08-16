"""
Session helpers, scrypt password hash/verify, and the require_member
dependency. See CLAUDE.md trap #2 for why the scrypt parameters below are not
negotiable.
"""
import hashlib
import hmac
import secrets

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select
from starlette.responses import Response

from app.db import get_session
from app.models import Group, Member

# CPython's hashlib.scrypt defaults maxmem=0, which OpenSSL caps at 32 MiB.
# Memory used is 128*N*r bytes, so N=2**15 r=8 hits that cap exactly and
# raises ValueError on the first real login. N=2**14 (~16 MiB) stays under it
# and would still OOM-protect a 512 MB free instance if p or r ever grow.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1

# A session can identify the browser in several groups at once, keyed by
# group_id. Cap the map so a browser that's joined many groups can't grow the
# cookie past the ~4 KB limit.
_MAX_IDENTITIES = 50


def hash_password(password: str) -> str:
    """Return a self-describing 'scrypt$N$r$p$salt$hash' string so the cost
    parameters can be raised later without a migration — verify() reads N/r/p
    back out of the stored string instead of assuming today's constants."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, salt.hex(), digest.hex()
    )


def verify_password(password: str, stored: str) -> bool:
    """Recompute the hash with the parameters embedded in `stored` and compare
    with hmac.compare_digest — never `==`, which leaks timing information."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        # Malformed stored hash. Treat as a failed verification, not a 500.
        return False


def get_identity_map(request: Request) -> dict:
    """{group_id (str, session keys are always strings): member_id}."""
    return request.session.get("identities", {})


def _save_identities(request: Request, identities: dict) -> None:
    # Starlette's Session only instruments the *top-level* dict's own
    # __setitem__/pop/etc. for its modified-flag dirty-tracking; mutating the
    # nested "identities" dict in place (e.g. via the object get_identity_map
    # returns) never sets that flag, so the cookie silently fails to update.
    # Assigning back through request.session[...] = ... routes through the
    # tracked __setitem__ instead.
    request.session["identities"] = identities


def set_identity(request: Request, group_id: int, member_id: int) -> None:
    identities = dict(get_identity_map(request))
    identities[str(group_id)] = member_id
    # Evict the oldest entry once the cap is hit. dict preserves insertion
    # order, so the first key is the oldest.
    while len(identities) > _MAX_IDENTITIES:
        identities.pop(next(iter(identities)))
    _save_identities(request, identities)


def clear_identity(request: Request, group_id: int) -> None:
    identities = dict(get_identity_map(request))
    identities.pop(str(group_id), None)
    _save_identities(request, identities)


def require_member(slug: str, request: Request, session: Session = Depends(get_session)):
    """FastAPI dependency: returns (group, member) for the current session's
    identity in this group, or redirects to the identify page.

    FastAPI dependencies can't short-circuit a route with a redirect on their
    own, so a missing identity is signalled by returning a RedirectResponse
    instead of a tuple; every route using this dependency must check for that
    before unpacking.
    """
    group = session.exec(select(Group).where(Group.slug == slug)).first()
    if group is None:
        return RedirectResponse(url="/", status_code=303)

    member_id = get_identity_map(request).get(str(group.id))
    member = session.get(Member, member_id) if member_id is not None else None
    if member is None or member.group_id != group.id:
        return RedirectResponse(url=f"/g/{slug}/identify", status_code=303)

    return group, member


def is_redirect(result) -> bool:
    """Helper for routes to check what require_member gave back."""
    return isinstance(result, Response)
