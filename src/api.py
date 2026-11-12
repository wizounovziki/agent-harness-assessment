"""Authenticated HTTP API; creation and approval resume return without holding a run open."""

import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config import Settings
from src.errors import ConflictError, NotFoundError, PersistenceError
from src.schemas import AgentState, ApprovalRequest, CancelRequest, RunCreateRequest
from src.service import RunManager


def create_app(
    settings: Settings | None = None, *, auth_tokens: dict[str, str] | None = None, llm_factory=None
) -> FastAPI:
    settings = settings or Settings.from_env()
    if not settings.api_token:
        raise ValueError("API_TOKEN must be nonempty")
    tokens = auth_tokens or {settings.api_token: "demo-user"}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.manager = RunManager(settings, llm_factory=llm_factory)
        await app.state.manager.start()
        try:
            yield
        finally:
            await app.state.manager.close()

    app = FastAPI(
        title="Operations Agent Harness",
        version="0.1.0",
        lifespan=lifespan,
        description="Persistent operations runs with approval and bounded execution.",
    )
    security = HTTPBearer(auto_error=False)

    def authenticated(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    ) -> str:
        if credentials:
            for token, owner in tokens.items():
                if secrets.compare_digest(credentials.credentials, token):
                    return owner
        raise HTTPException(
            401, "Valid bearer token required", headers={"WWW-Authenticate": "Bearer"}
        )

    Owner = Annotated[str, Depends(authenticated)]

    @app.middleware("http")
    async def body_limit(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            if len(await request.body()) > 65536:
                return JSONResponse(
                    status_code=413, content={"detail": "Request body exceeds 64 KiB"}
                )
        return await call_next(request)

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict(request: Request, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(PersistenceError)
    async def persistence_error(request: Request, exc: PersistenceError):
        return JSONResponse(status_code=503, content={"detail": "Run storage unavailable"})

    @app.get("/health")
    async def health():
        return {"status": "ok", "llm_mode": settings.llm_mode}

    @app.get("/scenarios")
    async def scenarios(owner: Owner):
        return [
            {
                "scenario_id": key,
                "objective": value["objective"],
                "description": value["description"],
            }
            for key, value in app.state.manager.dataset.scenarios.items()
        ]

    @app.post("/runs", status_code=202, response_model=AgentState)
    async def create_run(body: RunCreateRequest, owner: Owner):
        try:
            return app.state.manager.create_run(body.objective, body.scenario_id, owner)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/runs/{session_id}", response_model=AgentState)
    async def get_run(session_id: str, owner: Owner):
        return app.state.manager.get(session_id, owner)

    @app.get("/runs/{session_id}/events")
    async def events(session_id: str, owner: Owner, after: int = Query(default=0, ge=0)):
        app.state.manager.get(session_id, owner)
        return app.state.manager.store.events(session_id, after)

    @app.get("/runs/{session_id}/incidents")
    async def incidents(session_id: str, owner: Owner):
        app.state.manager.get(session_id, owner)
        return app.state.manager.store.incidents(session_id)

    @app.get("/runs/{session_id}/trace.jsonl")
    async def trace(session_id: str, owner: Owner):
        app.state.manager.get(session_id, owner)
        path = app.state.manager.tracer.rebuild_export(session_id)
        return FileResponse(path, media_type="application/x-ndjson", filename=f"{session_id}.jsonl")

    @app.post(
        "/runs/{session_id}/approvals/{approval_id}", status_code=202, response_model=AgentState
    )
    async def approve(session_id: str, approval_id: str, body: ApprovalRequest, owner: Owner):
        return app.state.manager.approve(
            session_id, approval_id, body.parameters_hash, body.decision, owner
        )

    @app.post("/runs/{session_id}/cancel", response_model=AgentState)
    async def cancel(session_id: str, body: CancelRequest, owner: Owner):
        return await app.state.manager.cancel(session_id, owner, body.reason)

    return app
