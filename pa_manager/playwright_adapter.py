from __future__ import annotations

from urllib.parse import urljoin, urlparse

from pa_manager.models import AccountConfig, Credentials, ProjectStatus
from pa_manager.pythonanywhere import (
    AuthenticatedHttpClient,
    AuthenticationError,
    DashboardApp,
    InteractiveAuthRequired,
    InterfaceChangedError,
    RemoteActionError,
    RenewalOutcome,
    _origin,
    _provider_action_url,
    parse_dashboard,
)


class PlaywrightRenewalClient:
    """Fallback visual. É importado sob demanda para manter o deploy principal leve."""

    provider_name = "playwright"

    def __init__(
        self,
        account: AccountConfig,
        credentials: Credentials,
        *,
        timeout_seconds: int = 30,
    ) -> None:
        self.account = account
        self.credentials = credentials
        self.timeout_ms = timeout_seconds * 1000
        self.base_url = f"https://{account.host}"
        self.login_url = f"{self.base_url}/login/"
        self.dashboard_url = f"{self.base_url}/user/{account.username}/webapps/"

    @staticmethod
    def _interactive(page: object) -> bool:
        content = page.content().lower()  # type: ignore[attr-defined]
        return any(
            marker in content
            for marker in ("captcha", "one-time-code", "two-factor code", "security challenge")
        )

    def _login(self, page: object) -> None:
        if not self.credentials.password:
            raise AuthenticationError("Senha não configurada para o fallback de navegador.")
        page.goto(self.login_url, wait_until="domcontentloaded")  # type: ignore[attr-defined]
        self._require_provider_origin(page)
        if self._interactive(page):
            raise InteractiveAuthRequired("CAPTCHA, 2FA ou challenge exige autenticação manual.")
        username = page.locator("input[name='auth-username']")  # type: ignore[attr-defined]
        password = page.locator("input[name='auth-password']")  # type: ignore[attr-defined]
        if username.count() != 1 or password.count() != 1:
            raise InterfaceChangedError("Campos de login não localizados pelo navegador.")
        username.fill(self.account.username)
        password.fill(self.credentials.password)
        submit = page.locator("button[type='submit'], input[type='submit']")  # type: ignore[attr-defined]
        if submit.count() < 1:
            raise InterfaceChangedError("Botão de login não localizado pelo navegador.")
        submit.first.click()
        page.wait_for_load_state("domcontentloaded")  # type: ignore[attr-defined]
        self._require_provider_origin(page)
        if self._interactive(page):
            raise InteractiveAuthRequired("CAPTCHA, 2FA ou challenge exige autenticação manual.")
        if urlparse(page.url).path.rstrip("/") == "/login":  # type: ignore[attr-defined]
            raise AuthenticationError("Login recusado no fallback de navegador.")

    @staticmethod
    def _find_form(page: object, action_url: str) -> object | None:
        forms = page.locator("form[action]")  # type: ignore[attr-defined]
        for index in range(forms.count()):
            form = forms.nth(index)
            value = form.get_attribute("action") or ""
            if value == action_url or urljoin(page.url, value) == urljoin(page.url, action_url):  # type: ignore[attr-defined]
                return form
        return None

    def _dashboard(self, page: object) -> DashboardApp:
        page.goto(self.dashboard_url, wait_until="domcontentloaded")  # type: ignore[attr-defined]
        self._require_provider_origin(page)
        if urlparse(page.url).path.rstrip("/") == "/login":  # type: ignore[attr-defined]
            raise AuthenticationError("Sessão expirada no fallback de navegador.")
        return parse_dashboard(page.content(), self.account.domain)  # type: ignore[attr-defined]

    def _require_provider_origin(self, page: object) -> None:
        if _origin(str(page.url)) != _origin(self.base_url):  # type: ignore[attr-defined]
            raise InterfaceChangedError(
                "O navegador foi redirecionado para uma origem não confiável."
            )

    def _submit_form(self, page: object, raw_action: str, action: str) -> None:
        action_url = _provider_action_url(
            self.base_url,
            raw_action,
            self.account.username,
            self.account.domain,
            action,
        )
        form = self._find_form(page, action_url)
        if form is None:
            raise InterfaceChangedError(f"O formulário de {action} desapareceu da página.")
        button = form.locator("button, input[type='submit']")
        if button.count():
            button.first.click()
        else:
            form.evaluate("form => form.requestSubmit()")
        page.wait_for_load_state("domcontentloaded")
        self._require_provider_origin(page)

    def renew(self, *, dry_run: bool) -> RenewalOutcome:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise InterfaceChangedError(
                "Fallback de navegador habilitado, mas Playwright não está instalado."
            ) from exc

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)
            try:
                self._login(page)
                dashboard = self._dashboard(page)
                before = dashboard.snapshot
                if dry_run:
                    return RenewalOutcome(
                        before=before,
                        after=before,
                        changed=False,
                        reactivated=False,
                    )
                if not dashboard.extend_url:
                    raise InterfaceChangedError(
                        "A ação de renovação não foi localizada pelo navegador."
                    )
                self._submit_form(page, dashboard.extend_url, "extend")
                after_dashboard = self._dashboard(page)
                after = after_dashboard.snapshot
                if after.expiry is None:
                    raise InterfaceChangedError("A nova expiração não pôde ser confirmada.")
                if before.expiry and after.expiry < before.expiry:
                    raise RemoteActionError(
                        "A data retornada após a renovação é anterior à data original."
                    )
                if before.status is ProjectStatus.EXPIRED and after.enabled is False:
                    if self.credentials.api_token:
                        AuthenticatedHttpClient(
                            self.account,
                            self.credentials,
                            timeout_seconds=max(5, self.timeout_ms // 1000),
                        ).enable()
                    else:
                        if not after_dashboard.enable_url:
                            raise RemoteActionError(
                                "A validade foi prorrogada, mas a reativação não foi localizada."
                            )
                        self._submit_form(page, after_dashboard.enable_url, "enable")
                    after = self._dashboard(page).snapshot
                    if after.enabled is not True:
                        raise RemoteActionError(
                            "A validade foi prorrogada, mas a reativação não pôde ser confirmada."
                        )
                return RenewalOutcome(
                    before=before,
                    after=after,
                    changed=before.expiry != after.expiry,
                    reactivated=(
                        before.status is ProjectStatus.EXPIRED
                        and after.status is not ProjectStatus.EXPIRED
                        and after.enabled is True
                    ),
                )
            finally:
                context.close()
                browser.close()
