import hashlib

from fastapi import Request


def cookie_name(request: Request) -> str:
    return f"shtab_direct_{request.state.direct_shtab_user_id.hex}"


def session_hash(request: Request) -> str:
    token = request.cookies.get(cookie_name(request), "")
    return hashlib.sha256(token.encode()).hexdigest()
