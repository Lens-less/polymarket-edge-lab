# mypy: disable-error-code="untyped-decorator"
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from secrets import compare_digest
from typing import Any, cast
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import DeskSnapshot, MutationRequest
from .pipeline_service import PipelineDeskService
from .service import DeskConflictError, DeskService, InMemoryDeskService

JsonObject = dict[str, Any]
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
_LOOPBACK_CLIENTS = {"127.0.0.1", "::1", "testclient"}


def _require_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")
    return value


def _optional_text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{key} must be a string.")
    return value


def _mutation_from_payload(payload: Mapping[str, object]) -> MutationRequest:
    actor = _optional_text(payload, "actor")
    idempotency_key = _optional_text(payload, "idempotency_key")
    if actor is None or not actor.strip():
        raise HTTPException(status_code=400, detail="actor must be non-empty.")
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="idempotency_key must be non-empty.")
    return MutationRequest(
        actor=actor,
        idempotency_key=idempotency_key,
        opportunity_id=_optional_text(payload, "opportunity_id"),
        order_id=_optional_text(payload, "order_id"),
    )


def _wire_object(value: object) -> JsonObject:
    return cast(JsonObject, value)


def _resolve_static_candidate(static_root: Path, full_path: str) -> Path | None:
    if not full_path:
        return None
    candidate_path = Path(full_path)
    if candidate_path.is_absolute():
        return None
    try:
        candidate = (static_root / candidate_path).resolve(strict=False)
        candidate.relative_to(static_root)
    except ValueError:
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate


def create_app(
    service: DeskService | None = None,
    *,
    static_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="polymm desk", version="0.2.0")
    desk_service = service or PipelineDeskService(
        state_dir=Path(os.environ.get("POLYMM_DESK_STATE_DIR", Path.cwd() / ".desk-state"))
    )

    def scenario_from(request: Request) -> str:
        return str(request.query_params.get("scenario", "paper"))

    def session_from(request: Request) -> str:
        return str(request.query_params.get("session", "default"))

    def snapshot_from(request: Request) -> DeskSnapshot:
        try:
            return desk_service.get_snapshot(
                scenario=scenario_from(request),
                session=session_from(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def ensure_local_request(request: Request) -> None:
        if request.url.hostname not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="Desk API is restricted to loopback hosts.")
        client_host = None if request.client is None else request.client.host
        if client_host not in _LOOPBACK_CLIENTS:
            raise HTTPException(status_code=403, detail="Desk API only accepts local clients.")

    def ensure_browser_protection(request: Request) -> None:
        ensure_local_request(request)
        origin = request.headers.get("origin")
        if not origin:
            raise HTTPException(status_code=403, detail="Missing Origin header.")
        origin_parts = urlparse(origin)
        target = request.url
        if origin_parts.hostname not in _LOOPBACK_HOSTS:
            raise HTTPException(
                status_code=403,
                detail="Desk mutations require a loopback browser origin.",
            )
        if origin_parts.scheme != target.scheme or origin_parts.netloc != target.netloc:
            raise HTTPException(status_code=403, detail="Origin must match the desk host.")
        snapshot = snapshot_from(request)
        csrf_token = request.headers.get("x-desk-csrf")
        if csrf_token is None or not compare_digest(csrf_token, snapshot.csrf_token):
            raise HTTPException(status_code=403, detail="Invalid desk CSRF token.")

    async def mutation_result(request: Request, action: str) -> JsonObject:
        ensure_browser_protection(request)
        payload = _require_mapping(await request.json())
        mutation = _mutation_from_payload(payload)
        scenario = scenario_from(request)
        session = session_from(request)
        try:
            if action == "review":
                result = await run_in_threadpool(
                    desk_service.review,
                    mutation,
                    scenario=scenario,
                    session=session,
                )
                return _wire_object(result.to_wire())
            if action == "confirm":
                result = await run_in_threadpool(
                    desk_service.confirm,
                    mutation,
                    scenario=scenario,
                    session=session,
                )
                return _wire_object(result.to_wire())
            if action == "cancel":
                result = await run_in_threadpool(
                    desk_service.cancel,
                    mutation,
                    scenario=scenario,
                    session=session,
                )
                return _wire_object(result.to_wire())
            if action == "cancel-all":
                result = await run_in_threadpool(
                    desk_service.cancel_all,
                    mutation,
                    scenario=scenario,
                    session=session,
                )
                return _wire_object(result.to_wire())
            result = await run_in_threadpool(
                desk_service.kill,
                mutation,
                scenario=scenario,
                session=session,
            )
            return _wire_object(result.to_wire())
        except DeskConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def get_snapshot(request: Request) -> JsonObject:
        ensure_local_request(request)
        snapshot = snapshot_from(request)
        return _wire_object(snapshot.to_wire())

    def get_opportunities(request: Request) -> JsonObject:
        ensure_local_request(request)
        snapshot = snapshot_from(request)
        return _wire_object({"opportunities": list(snapshot.to_wire()["opportunities"])})

    def get_explanation(request: Request) -> JsonObject:
        ensure_local_request(request)
        snapshot = snapshot_from(request)
        return _wire_object({"explanation": snapshot.to_wire()["explanation"]})

    def get_plan(request: Request) -> JsonObject:
        ensure_local_request(request)
        snapshot = snapshot_from(request)
        return _wire_object({"execution_plan": snapshot.to_wire()["execution_plan"]})

    def get_orders(request: Request) -> JsonObject:
        ensure_local_request(request)
        snapshot = snapshot_from(request)
        return _wire_object({"orders": snapshot.to_wire()["orders"]})

    def get_fills(request: Request) -> JsonObject:
        ensure_local_request(request)
        snapshot = snapshot_from(request)
        return _wire_object({"fills": snapshot.to_wire()["fills"]})

    def get_positions(request: Request) -> JsonObject:
        ensure_local_request(request)
        snapshot = snapshot_from(request)
        return _wire_object({"positions": snapshot.to_wire()["positions"]})

    def get_pnl(request: Request) -> JsonObject:
        ensure_local_request(request)
        snapshot = snapshot_from(request)
        payload = snapshot.to_wire()
        return _wire_object(
            {
                "strategy_pnl": payload["strategy_pnl"],
                "expected_vs_realized": payload["expected_vs_realized"],
                "status_bar": payload["status_bar"],
            }
        )

    def get_reconciliation(request: Request) -> JsonObject:
        ensure_local_request(request)
        snapshot = snapshot_from(request)
        return _wire_object({"reconciliation": snapshot.to_wire()["reconciliation"]})

    async def review(request: Request) -> JsonObject:
        return await mutation_result(request, "review")

    async def confirm(request: Request) -> JsonObject:
        return await mutation_result(request, "confirm")

    async def cancel(request: Request) -> JsonObject:
        return await mutation_result(request, "cancel")

    async def cancel_all(request: Request) -> JsonObject:
        return await mutation_result(request, "cancel-all")

    async def kill(request: Request) -> JsonObject:
        return await mutation_result(request, "kill")

    app.add_api_route("/api/v0.2/desk/snapshot", get_snapshot, methods=["GET"])
    app.add_api_route("/api/v0.2/desk/opportunities", get_opportunities, methods=["GET"])
    app.add_api_route("/api/v0.2/desk/explanations", get_explanation, methods=["GET"])
    app.add_api_route("/api/v0.2/desk/plan", get_plan, methods=["GET"])
    app.add_api_route("/api/v0.2/desk/orders", get_orders, methods=["GET"])
    app.add_api_route("/api/v0.2/desk/fills", get_fills, methods=["GET"])
    app.add_api_route("/api/v0.2/desk/positions", get_positions, methods=["GET"])
    app.add_api_route("/api/v0.2/desk/pnl", get_pnl, methods=["GET"])
    app.add_api_route("/api/v0.2/desk/reconciliation", get_reconciliation, methods=["GET"])
    app.add_api_route("/api/v0.2/desk/review", review, methods=["POST"])
    app.add_api_route("/api/v0.2/desk/confirm", confirm, methods=["POST"])
    app.add_api_route("/api/v0.2/desk/cancel", cancel, methods=["POST"])
    app.add_api_route("/api/v0.2/desk/cancel-all", cancel_all, methods=["POST"])
    app.add_api_route("/api/v0.2/desk/kill", kill, methods=["POST"])

    resolved_static_dir = static_dir or _resolve_static_dir()
    if resolved_static_dir and resolved_static_dir.exists():
        resolved_static_dir = resolved_static_dir.resolve()
        assets_dir = resolved_static_dir / "assets"
        if assets_dir.exists():
            app.mount(
                "/assets",
                StaticFiles(directory=assets_dir),
                name="desk-assets",
            )

        def serve_frontend(request: Request, full_path: str) -> FileResponse:
            ensure_local_request(request)
            requested = _resolve_static_candidate(resolved_static_dir, full_path)
            if requested is not None:
                return FileResponse(requested)
            return FileResponse(resolved_static_dir / "index.html")

        app.add_api_route("/{full_path:path}", serve_frontend, methods=["GET"])

    return app


def create_demo_app() -> FastAPI:
    """Build the explicit deterministic UI fixture used by Playwright.

    The ordinary :func:`create_app` factory deliberately uses the persisted
    pipeline service.  Keeping this separate prevents demo fills and PnL from
    becoming the default operator truth source.
    """

    return create_app(
        service=InMemoryDeskService(
            state_dir=Path(os.environ.get("POLYMM_DESK_STATE_DIR", Path.cwd() / ".desk-state"))
        )
    )


def _resolve_static_dir() -> Path | None:
    configured = os.environ.get("POLYMM_DESK_STATIC_DIR")
    if configured:
        return Path(configured)
    candidate = Path.cwd() / "apps" / "trade-desk" / "dist"
    return candidate if candidate.exists() else None
