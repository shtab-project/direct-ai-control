from contextlib import aclosing
from inspect import isasyncgenfunction, iscoroutinefunction

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from shtab_core.db import get_session_factory
from shtab_core.dependencies import current_user, get_auth_service
from shtab_core.services.errors import UnauthorizedError
from shtab_core.sessions import get_sessions
from shtab_core.uow import UnitOfWork
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from shtab_direct_ai_control.paths import MOUNT_PATH


def register(app: FastAPI) -> None:
    from shtab_direct_ai_control.direct.main import app as direct_app
    from shtab_direct_ai_control.direct.main import initialize

    async def authenticated_direct(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        overrides = app.dependency_overrides
        if override := overrides.get(current_user):
            if isasyncgenfunction(override):
                async with aclosing(override()) as users:
                    user = await anext(users)
                    user_id = user.id if user is not None else None
            elif iscoroutinefunction(override):
                user = await override()
                user_id = user.id if user is not None else None
            else:
                user = await run_in_threadpool(override)
                user_id = user.id if user is not None else None
        else:
            provider = overrides.get(get_session_factory, get_session_factory)
            sessions = overrides.get(get_sessions, get_sessions)()
            async with UnitOfWork(provider()) as uow:
                try:
                    user = await current_user(request, get_auth_service(uow), sessions)
                    user_id = user.id
                except UnauthorizedError:
                    await JSONResponse({"detail": "Войдите в Shtab"}, 401)(
                        scope, receive, send
                    )
                    return
        if user_id is None:
            await JSONResponse({"detail": "Войдите в Shtab"}, 401)(scope, receive, send)
            return
        request.state.direct_shtab_user_id = user_id
        await run_in_threadpool(initialize)
        await direct_app(scope, receive, send)

    app.mount(MOUNT_PATH, authenticated_direct, name="direct-ai-control")
