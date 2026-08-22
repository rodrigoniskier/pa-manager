from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from pa_manager.dates import classify_expiry, days_remaining, parse_expiry
from pa_manager.models import (
    AccountConfig,
    Credentials,
    HealthResult,
    ProjectStatus,
    WebAppSnapshot,
    utc_now,
)


class PythonAnywhereError(RuntimeError):
    """Erro operacional seguro para exibição no painel."""


class AuthenticationError(PythonAnywhereError):
    pass


class InteractiveAuthRequired(PythonAnywhereError):
    pass


class InterfaceChangedError(PythonAnywhereError):
    pass


class RemoteActionError(PythonAnywhereError):
    pass


@dataclass(slots=True)
class DashboardApp:
    snapshot: WebAppSnapshot
    extend_url: str | None
    extend_fields: dict[str, str]
    reload_url: str | None
    reload_fields: dict[str, str]
    enable_url: str | None
    enable_fields: dict[str, str]


@dataclass(slots=True)
class RenewalOutcome:
    before: WebAppSnapshot
    after: WebAppSnapshot
    changed: bool
    reactivated: bool


def _form_fields(form: Tag | None) -> dict[str, str]:
    if form is None:
        return {}
    result: dict[str, str] = {}
    for field in form.select("input[name]"):
        name = field.get("name")
        if isinstance(name, str):
            result[name] = str(field.get("value") or "")
    return result


def _action_domain(action: str) -> str | None:
    match = re.search(
        r"/webapps/(?P<domain>[^/]+)/(?P<action>extend|reload|enable)/?",
        action,
        re.I,
    )
    return unquote(match.group("domain")) if match else None


def _origin(url: str) -> tuple[str, str, int] | None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    effective_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    return parsed.scheme.lower(), parsed.hostname.lower(), effective_port


def _safe_url_for_storage(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname.lower()
    netloc = f"{hostname}:{port}" if port else hostname
    return urlunparse((parsed.scheme.lower(), netloc, parsed.path or "/", "", "", ""))


def _provider_action_url(
    base_url: str,
    raw_action: str,
    username: str,
    domain: str,
    action: str,
) -> str:
    candidate = urljoin(base_url, raw_action)
    parsed = urlparse(candidate)
    if _origin(candidate) != _origin(base_url):
        raise InterfaceChangedError("A ação localizada aponta para uma origem não confiável.")
    expected_path = re.compile(
        rf"^/(?:user/{re.escape(username)}/)?webapps/"
        rf"{re.escape(domain)}/{re.escape(action)}/?$",
        re.I,
    )
    if not expected_path.fullmatch(unquote(parsed.path)):
        raise InterfaceChangedError("A ação localizada não corresponde ao web app configurado.")
    return candidate


def _find_action_form(soup: BeautifulSoup, domain: str, action_name: str) -> Tag | None:
    for form in soup.select("form[action]"):
        action = str(form.get("action") or "")
        if f"/{action_name}" not in action.lower():
            continue
        form_domain = _action_domain(action)
        if form_domain and form_domain.lower() == domain.lower():
            return form
    return None


def _find_pane(soup: BeautifulSoup, domain: str, form: Tag | None) -> Tag | BeautifulSoup:
    identifiers = (
        f"id_{domain.replace('.', '_')}",
        f"id_{domain.lower().replace('.', '_')}",
    )
    for identifier in identifiers:
        pane = soup.find(id=identifier)
        if isinstance(pane, Tag):
            return pane
    if form:
        for parent in form.parents:
            if isinstance(parent, Tag) and parent.name in {"section", "article", "div", "li"}:
                text = parent.get_text(" ", strip=True).lower()
                if domain.lower() in text or "webapp_expiry" in str(parent.get("class", "")):
                    return parent
    return soup


def _extract_expiry(container: Tag | BeautifulSoup) -> tuple[date | None, str | None]:
    selectors = (
        ".webapp_expiry strong",
        ".webapp-expiry strong",
        "[data-testid='webapp-expiry']",
        ".webapp_expiry",
        ".webapp-expiry",
    )
    candidates: list[str] = []
    for selector in selectors:
        for element in container.select(selector):
            text = element.get_text(" ", strip=True)
            if text and text not in candidates:
                candidates.append(text)
    for text_node in container.find_all(string=re.compile(r"expir|run until|running until", re.I)):
        parent = text_node.parent
        if isinstance(parent, Tag):
            text = parent.get_text(" ", strip=True)
            if text and text not in candidates:
                candidates.append(text)
    for candidate in candidates:
        parsed = parse_expiry(candidate)
        if parsed:
            return parsed, candidate
    return None, candidates[0] if candidates else None


def parse_dashboard(html: str, domain: str) -> DashboardApp:
    soup = BeautifulSoup(html, "html.parser")
    extend_form = _find_action_form(soup, domain, "extend")
    reload_form = _find_action_form(soup, domain, "reload")
    enable_form = _find_action_form(soup, domain, "enable")
    container = _find_pane(soup, domain, extend_form or reload_form)
    expiry, raw_expiry = _extract_expiry(container)
    container_text = container.get_text(" ", strip=True).lower()
    enabled: bool | None
    disabled_marker = any(
        marker in container_text for marker in ("web app is disabled", "webapp is disabled")
    )
    expired_marker = bool("expired" in container_text and expiry and expiry < date.today())
    if disabled_marker or expired_marker:
        enabled = False
    elif extend_form or reload_form:
        enabled = True
    else:
        enabled = None

    status = classify_expiry(expiry)
    snapshot = WebAppSnapshot(
        domain=domain,
        expiry=expiry,
        raw_expiry=raw_expiry,
        days_remaining=days_remaining(expiry),
        status=status,
        enabled=enabled,
        has_extend_action=extend_form is not None,
    )
    return DashboardApp(
        snapshot=snapshot,
        extend_url=str(extend_form.get("action")) if extend_form else None,
        extend_fields=_form_fields(extend_form),
        reload_url=str(reload_form.get("action")) if reload_form else None,
        reload_fields=_form_fields(reload_form),
        enable_url=str(enable_form.get("action")) if enable_form else None,
        enable_fields=_form_fields(enable_form),
    )


def _detect_interactive_auth(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    selectors = (
        "iframe[src*='captcha']",
        "[class*='captcha']",
        "input[name*='otp']",
        "input[name*='totp']",
        "input[autocomplete='one-time-code']",
    )
    return any(soup.select_one(selector) for selector in selectors) or any(
        phrase in text
        for phrase in ("verify that you are human", "security challenge", "two-factor code")
    )


class AuthenticatedHttpClient:
    provider_name = "authenticated_http"

    def __init__(
        self,
        account: AccountConfig,
        credentials: Credentials,
        *,
        timeout_seconds: int = 20,
        session: requests.Session | None = None,
    ) -> None:
        self.account = account
        self.credentials = credentials
        self.timeout = timeout_seconds
        self.base_url = f"https://{account.host}"
        self.login_url = f"{self.base_url}/login/"
        self.dashboard_url = f"{self.base_url}/user/{account.username}/webapps/"
        self.session = session or self._build_session()
        self._authenticated = False

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            status=3,
            allowed_methods=frozenset({"GET", "HEAD"}),
            status_forcelist=(429, 500, 502, 503, 504),
            backoff_factor=0.6,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        session.mount("https://", adapter)
        session.headers.update(
            {
                "User-Agent": "PA-Manager/1.0 (personal PythonAnywhere maintenance)",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        return session

    def login(self) -> None:
        if self._authenticated:
            return
        if not self.credentials.password:
            raise AuthenticationError("Senha não configurada para esta conta.")
        try:
            page = self.session.get(self.login_url, timeout=self.timeout)
            page.raise_for_status()
        except requests.Timeout as exc:
            raise PythonAnywhereError("Tempo esgotado ao abrir o login do PythonAnywhere.") from exc
        except requests.RequestException as exc:
            raise PythonAnywhereError("Falha de rede ao abrir o login do PythonAnywhere.") from exc
        if _detect_interactive_auth(page.text):
            raise InteractiveAuthRequired("CAPTCHA, 2FA ou challenge exige autenticação manual.")

        soup = BeautifulSoup(page.text, "html.parser")
        csrf = soup.select_one("input[name='csrfmiddlewaretoken']")
        if not csrf or not csrf.get("value"):
            raise InterfaceChangedError("O formulário de login mudou: token CSRF não localizado.")
        payload = {
            "csrfmiddlewaretoken": str(csrf.get("value")),
            "auth-username": self.account.username,
            "auth-password": self.credentials.password,
            "login_view-current_step": "auth",
        }
        try:
            response = self.session.post(
                self.login_url,
                data=payload,
                headers={"Referer": self.login_url},
                timeout=self.timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
        except requests.Timeout as exc:
            raise PythonAnywhereError("Tempo esgotado durante o login no PythonAnywhere.") from exc
        except requests.RequestException as exc:
            raise PythonAnywhereError("Falha de rede durante o login no PythonAnywhere.") from exc
        if _detect_interactive_auth(response.text):
            raise InteractiveAuthRequired("CAPTCHA, 2FA ou challenge exige autenticação manual.")
        after = BeautifulSoup(response.text, "html.parser")
        still_has_login = after.select_one("input[name='auth-password']") is not None
        still_on_login = urlparse(response.url).path.rstrip("/") == "/login"
        if still_has_login or still_on_login:
            raise AuthenticationError("Login recusado. Confira username e senha.")
        self._authenticated = True

    def _fetch_dashboard(self) -> DashboardApp:
        self.login()
        try:
            response = self.session.get(self.dashboard_url, timeout=self.timeout)
            response.raise_for_status()
        except requests.Timeout as exc:
            raise PythonAnywhereError("Tempo esgotado ao consultar a aba Web.") from exc
        except requests.RequestException as exc:
            raise PythonAnywhereError("Falha de rede ao consultar a aba Web.") from exc
        if urlparse(response.url).path.rstrip("/") == "/login":
            self._authenticated = False
            raise AuthenticationError("A sessão expirou durante a consulta.")
        return parse_dashboard(response.text, self.account.domain)

    def inspect(self) -> WebAppSnapshot:
        return self._fetch_dashboard().snapshot

    def renew(self, *, dry_run: bool) -> RenewalOutcome:
        dashboard = self._fetch_dashboard()
        before = dashboard.snapshot
        if dry_run:
            return RenewalOutcome(before=before, after=before, changed=False, reactivated=False)
        if not dashboard.extend_url:
            raise InterfaceChangedError("A ação de renovação não foi localizada na aba Web.")
        action_url = _provider_action_url(
            self.base_url,
            dashboard.extend_url,
            self.account.username,
            self.account.domain,
            "extend",
        )
        if not any(key.lower().startswith("csrf") for key in dashboard.extend_fields):
            raise InterfaceChangedError("A renovação não contém proteção CSRF reconhecível.")
        try:
            response = self.session.post(
                action_url,
                data=dashboard.extend_fields,
                headers={"Referer": self.dashboard_url},
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.Timeout as exc:
            raise RemoteActionError(
                "A renovação demorou além do limite; verifique a data antes de repetir."
            ) from exc
        except requests.RequestException as exc:
            raise RemoteActionError(
                "A conexão falhou durante a renovação; verifique a data antes de repetir."
            ) from exc
        if response.status_code not in range(200, 400):
            raise RemoteActionError(
                f"PythonAnywhere recusou a renovação (HTTP {response.status_code})."
            )
        if urlparse(response.url).path.rstrip("/") == "/login":
            self._authenticated = False
            raise AuthenticationError("A sessão expirou antes de confirmar a renovação.")

        after_dashboard = self._fetch_dashboard()
        after = after_dashboard.snapshot
        if after.expiry is None:
            raise InterfaceChangedError("A nova data de expiração não pôde ser confirmada.")
        if before.expiry and after.expiry < before.expiry:
            raise RemoteActionError("A data retornada após a renovação é anterior à data original.")
        if before.status is ProjectStatus.EXPIRED and after.enabled is False:
            self._enable(after_dashboard)
            after = self._fetch_dashboard().snapshot
            if after.enabled is not True:
                raise RemoteActionError(
                    "A validade foi prorrogada, mas a reativação não pôde ser confirmada."
                )
        changed = before.expiry != after.expiry
        reactivated = (
            before.status is ProjectStatus.EXPIRED
            and after.status is not ProjectStatus.EXPIRED
            and after.enabled is True
        )
        return RenewalOutcome(
            before=before,
            after=after,
            changed=changed,
            reactivated=reactivated,
        )

    def reload(self) -> None:
        if self.credentials.api_token:
            self._reload_via_api()
            return
        dashboard = self._fetch_dashboard()
        if not dashboard.reload_url:
            raise InterfaceChangedError(
                "A ação de reload não foi localizada e não há API token configurado."
            )
        if not any(key.lower().startswith("csrf") for key in dashboard.reload_fields):
            raise InterfaceChangedError("O reload não contém proteção CSRF reconhecível.")
        try:
            response = self.session.post(
                _provider_action_url(
                    self.base_url,
                    dashboard.reload_url,
                    self.account.username,
                    self.account.domain,
                    "reload",
                ),
                data=dashboard.reload_fields,
                headers={"Referer": self.dashboard_url},
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise RemoteActionError("Falha ao executar reload pela interface.") from exc
        if response.status_code not in range(200, 400):
            raise RemoteActionError(f"Reload recusado (HTTP {response.status_code}).")

    def enable(self) -> None:
        if self.credentials.api_token:
            self._enable_via_api()
            return
        dashboard = self._fetch_dashboard()
        self._enable(dashboard)
        if self._fetch_dashboard().snapshot.enabled is not True:
            raise RemoteActionError("A reativação do web app não pôde ser confirmada.")

    def _enable(self, dashboard: DashboardApp) -> None:
        if self.credentials.api_token:
            self._enable_via_api()
            return
        if not dashboard.enable_url:
            raise RemoteActionError(
                "A validade foi prorrogada, mas a reativação exige um API token ou "
                "um formulário de enable reconhecível."
            )
        if not any(key.lower().startswith("csrf") for key in dashboard.enable_fields):
            raise InterfaceChangedError("A reativação não contém proteção CSRF reconhecível.")
        try:
            response = self.session.post(
                _provider_action_url(
                    self.base_url,
                    dashboard.enable_url,
                    self.account.username,
                    self.account.domain,
                    "enable",
                ),
                data=dashboard.enable_fields,
                headers={"Referer": self.dashboard_url},
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise RemoteActionError("Falha ao reativar o web app pela interface.") from exc
        if response.status_code not in range(200, 400):
            raise RemoteActionError(f"Reativação recusada (HTTP {response.status_code}).")

    def _enable_via_api(self) -> None:
        self._post_api_action("enable", "reativar")

    def _reload_via_api(self) -> None:
        self._post_api_action("reload", "executar reload")

    def _post_api_action(self, action: str, description: str) -> None:
        url = (
            f"{self.base_url}/api/v0/user/{self.account.username}/webapps/"
            f"{self.account.domain}/{action}/"
        )
        try:
            response = self.session.post(
                url,
                headers={"Authorization": f"Token {self.credentials.api_token}"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RemoteActionError(f"Falha ao {description} pela API oficial.") from exc
        if response.status_code not in {200, 201, 202, 204}:
            if response.status_code in {401, 403}:
                raise AuthenticationError("API token recusado pelo PythonAnywhere.")
            raise RemoteActionError(
                f"A API recusou a ação de {action} (HTTP {response.status_code})."
            )


class HealthChecker:
    def __init__(
        self,
        timeout_seconds: int = 12,
        session: requests.Session | None = None,
        max_redirects: int = 5,
    ) -> None:
        self.timeout = timeout_seconds
        self.max_redirects = max_redirects
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "PA-Manager-Health/1.0"})

    def check(self, url: str) -> HealthResult:
        started = time.perf_counter()
        current_url = url
        initial_origin = _origin(url)
        redirect_count = 0
        try:
            while True:
                response = self.session.get(
                    current_url,
                    timeout=self.timeout,
                    allow_redirects=False,
                    stream=True,
                )
                status_code = response.status_code
                response_url = str(getattr(response, "url", current_url) or current_url)
                location = str(getattr(response, "headers", {}).get("Location") or "")
                is_redirect = 300 <= status_code < 400 and bool(location)
                if is_redirect:
                    candidate = urljoin(response_url, location)
                    redirect_count += 1
                    response.close()
                    if _origin(candidate) != initial_origin:
                        return HealthResult(
                            online=True,
                            status_code=status_code,
                            response_ms=round((time.perf_counter() - started) * 1000),
                            checked_at=utc_now(),
                            final_url=_safe_url_for_storage(response_url),
                            redirect_count=redirect_count,
                        )
                    if redirect_count > self.max_redirects:
                        raise requests.TooManyRedirects()
                    current_url = candidate
                    continue

                response.close()
                online = 200 <= status_code < 400
                return HealthResult(
                    online=online,
                    status_code=status_code,
                    response_ms=round((time.perf_counter() - started) * 1000),
                    checked_at=utc_now(),
                    final_url=_safe_url_for_storage(response_url),
                    redirect_count=redirect_count,
                    error=None if online else f"HTTP {status_code}",
                )
        except requests.Timeout:
            error = "Tempo esgotado"
        except requests.TooManyRedirects:
            error = "Redirecionamentos em excesso"
        except requests.ConnectionError:
            error = "Falha de conexão"
        except requests.RequestException:
            error = "Falha na verificação HTTP"
        return HealthResult(
            online=False,
            status_code=None,
            response_ms=round((time.perf_counter() - started) * 1000),
            checked_at=utc_now(),
            redirect_count=redirect_count,
            error=error,
        )
