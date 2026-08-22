from __future__ import annotations

import secrets
from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pa_manager.auth import LoginRateLimiter, csrf_token, require_login, verify_csrf
from pa_manager.config import Settings, load_accounts
from pa_manager.db import Repository
from pa_manager.logging_setup import configure_logging
from pa_manager.models import Action, ProjectStatus
from pa_manager.service import ActionNotAllowed, ManagerService, OperationInProgress

PACKAGE_DIR = Path(__file__).resolve().parent
MAX_FORM_BYTES = 64 * 1024
STATUS_LABELS = {
    "active": "Ativo",
    "expiring": "Vence em até 7 dias",
    "expired": "Expirado",
    "unknown": "Ainda não verificado",
    "auth_error": "Erro de autenticação",
    "interactive_auth": "Autenticação manual necessária",
    "config_error": "Credencial ausente",
    "renewal_error": "Erro de renovação",
    "offline": "Indisponível",
    "not_applicable": "Sem web app",
}
RESULT_LABELS = {
    "success": "Sucesso",
    "warning": "Atenção",
    "failed": "Falha",
    "would_renew": "Renovaria",
    "skipped": "Ignorado",
}
ACTION_LABELS = {"verify": "Verificação", "renew": "Renovação", "reload": "Reload"}
RUN_LABELS = {
    "queued": "Na fila",
    "running": "Em andamento",
    "completed": "Concluída",
    "completed_with_errors": "Concluída com falhas",
    "failed": "Falha geral",
    "interrupted": "Interrompida",
}


def _date_br(value: object) -> str:
    if not value:
        return "—"
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if "T" in raw or " " in raw:
            return parsed.strftime("%d/%m/%Y %H:%M")
        return parsed.strftime("%d/%m/%Y")
    except ValueError:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            return raw


def _client_key(request: Request, trusted_proxy_hops: int = 0) -> str:
    peer = request.client.host if request.client else "unknown"
    if trusted_proxy_hops <= 0:
        return peer

    forwarded_values = request.headers.getlist("x-forwarded-for")
    forwarded = [
        item.strip()
        for value in forwarded_values
        for item in value.split(",")
        if item.strip()
    ]
    if len(forwarded) < trusted_proxy_hops:
        return peer
    candidate = forwarded[-trusted_proxy_hops]
    try:
        return str(ip_address(candidate))
    except ValueError:
        return peer


async def _read_small_form(request: Request) -> Any:
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > MAX_FORM_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="Formulário excede o limite permitido",
                )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Content-Length inválido",
            ) from None

    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_FORM_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Formulário excede o limite permitido",
            )
        chunks.append(chunk)
    request._body = b"".join(chunks)  # type: ignore[attr-defined]
    return await request.form(max_files=10, max_fields=50, max_part_size=MAX_FORM_BYTES)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.validate_for_web()
    configure_logging(settings.log_level)
    accounts = load_accounts(settings.accounts_file)
    repository = Repository(settings.db_path)
    repository.initialize()
    service = ManagerService(settings, repository, accounts)
    limiter = LoginRateLimiter()

    app = FastAPI(
        title="PA Manager",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    app.state.settings = settings
    app.state.accounts = accounts
    app.state.repository = repository
    app.state.service = service
    app.state.login_limiter = limiter

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="pam_session",
        max_age=8 * 60 * 60,
        same_site="strict",
        https_only=settings.secure_cookies,
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters["date_br"] = _date_br

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; form-action 'self'; "
            "base-uri 'self'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        return response

    def context(request: Request, **extra: Any) -> dict[str, Any]:
        return {
            "request": request,
            "csrf_token": csrf_token(request),
            "authenticated": request.session.get("authenticated") is True,
            "status_labels": STATUS_LABELS,
            "result_labels": RESULT_LABELS,
            "action_labels": ACTION_LABELS,
            "run_labels": RUN_LABELS,
            "dry_run": settings.dry_run,
            **extra,
        }

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Any:
        if request.session.get("authenticated") is True:
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(request, "login.html", context(request))

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Any:
        form = await _read_small_form(request)
        verify_csrf(request, form.get("csrf_token"))
        key = _client_key(request, settings.trusted_proxy_hops)
        if not limiter.allowed(key):
            return templates.TemplateResponse(
                request,
                "login.html",
                context(request, error="Muitas tentativas. Aguarde cinco minutos."),
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        username = str(form.get("username") or "")
        password = str(form.get("password") or "")
        user_ok = secrets.compare_digest(username, settings.admin_username)
        password_ok = secrets.compare_digest(password, settings.admin_password)
        if not (user_ok and password_ok):
            limiter.failure(key)
            return templates.TemplateResponse(
                request,
                "login.html",
                context(request, error="Usuário ou senha inválidos."),
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        limiter.success(key)
        request.session.clear()
        request.session["authenticated"] = True
        csrf_token(request)
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        require_login(request)
        form = await _read_small_form(request)
        verify_csrf(request, form.get("csrf_token"))
        request.session.clear()
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Any:
        if request.session.get("authenticated") is not True:
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        states = repository.get_project_states()
        rows: list[dict[str, Any]] = []
        summary = {
            "active": 0,
            "expiring": 0,
            "expired": 0,
            "attention": 0,
            "unknown": 0,
            "not_applicable": 0,
        }
        for account in accounts:
            state = states.get(account.id, {})
            project_status = (
                ProjectStatus.NOT_APPLICABLE.value
                if not account.has_webapp
                else state.get("project_status", ProjectStatus.UNKNOWN.value)
            )
            if project_status in {"active", "expiring", "expired", "not_applicable"}:
                summary[project_status] += 1
            elif project_status == "unknown":
                summary["unknown"] += 1
            else:
                summary["attention"] += 1
            rows.append(
                {
                    "account": account,
                    "state": state,
                    "project_status": project_status,
                    "configured": service.credentials.configured(account),
                }
            )
        flash_error = request.session.pop("flash_error", None)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            context(
                request,
                rows=rows,
                summary=summary,
                total=len(accounts),
                history=repository.list_runs(12),
                global_renew_enabled=settings.allow_global_renew,
                reload_enabled=settings.allow_reload and not settings.dry_run,
                flash_error=flash_error,
            ),
        )

    async def start_action(
        request: Request,
        action: Action,
        account_ids: list[str] | None = None,
    ) -> RedirectResponse:
        require_login(request)
        form = await _read_small_form(request)
        verify_csrf(request, form.get("csrf_token"))
        try:
            run_id = service.launch(action, account_ids=account_ids)
        except (OperationInProgress, ActionNotAllowed) as exc:
            request.session["flash_error"] = str(exc)
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        return RedirectResponse(f"/runs/{run_id}", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/actions/verify-all")
    async def verify_all(request: Request) -> RedirectResponse:
        return await start_action(request, Action.VERIFY)

    @app.post("/actions/renew-all")
    async def renew_all(request: Request) -> RedirectResponse:
        return await start_action(request, Action.RENEW)

    @app.post("/accounts/{account_id}/verify")
    async def verify_one(account_id: str, request: Request) -> RedirectResponse:
        return await start_action(request, Action.VERIFY, [account_id])

    @app.post("/accounts/{account_id}/renew")
    async def renew_one(account_id: str, request: Request) -> RedirectResponse:
        return await start_action(request, Action.RENEW, [account_id])

    @app.post("/accounts/{account_id}/reload")
    async def reload_one(account_id: str, request: Request) -> RedirectResponse:
        return await start_action(request, Action.RELOAD, [account_id])

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_detail(run_id: str, request: Request) -> Any:
        if request.session.get("authenticated") is not True:
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        data = repository.get_run(run_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Execução não encontrada")
        return templates.TemplateResponse(
            request,
            "run.html",
            context(request, run=data["run"], results=data["results"]),
        )

    @app.get("/api/runs/{run_id}")
    async def run_json(run_id: str, request: Request) -> JSONResponse:
        require_login(request)
        data = repository.get_run(run_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Execução não encontrada")
        return JSONResponse(data)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        repository.get_project_states()
        return {"status": "ready"}

    @app.exception_handler(401)
    async def unauthorized(request: Request, _exc: HTTPException) -> RedirectResponse:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    return app
