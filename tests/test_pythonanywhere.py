from datetime import date
from pathlib import Path
from typing import Any

import pytest

from pa_manager.models import AccountConfig, Credentials
from pa_manager.pythonanywhere import (
    AuthenticatedHttpClient,
    HealthChecker,
    InterfaceChangedError,
    parse_dashboard,
)

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(
        self,
        text: str,
        url: str,
        status_code: int = 200,
        *,
        history: list[Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.text = text
        self.url = url
        self.status_code = status_code
        self.history = history or []
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("HTTP error")

    def close(self) -> None:
        return None


class FakeSession:
    def __init__(self, gets: list[FakeResponse], posts: list[FakeResponse]) -> None:
        self.gets = gets
        self.posts = posts
        self.headers: dict[str, str] = {}
        self.posted: list[tuple[str, dict[str, str]]] = []
        self.requested_gets: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> FakeResponse:
        self.requested_gets.append(url)
        return self.gets.pop(0)

    def post(self, url: str, data: dict[str, str] | None = None, **_kwargs: Any) -> FakeResponse:
        self.posted.append((url, data or {}))
        return self.posts.pop(0)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def account() -> AccountConfig:
    return AccountConfig(
        id="demo",
        label="Demo",
        username="demo",
        domain="demo.pythonanywhere.com",
        password_env="PA_DEMO_PASSWORD",
        token_env="PA_DEMO_API_TOKEN",
    )


def test_dashboard_parser_extracts_expiry_and_actions() -> None:
    dashboard = parse_dashboard(fixture("dashboard_before.html"), account().domain)
    assert dashboard.snapshot.expiry == date(2026, 8, 22)
    assert dashboard.snapshot.has_extend_action
    assert dashboard.extend_fields["csrfmiddlewaretoken"] == "csrf-before"
    assert dashboard.reload_url and dashboard.reload_url.endswith("/reload/")


def test_authenticated_renewal_posts_csrf_and_confirms_new_date() -> None:
    login = "<form><input name='csrfmiddlewaretoken' value='login-csrf'></form>"
    logged_in = "<html><a href='/logout/'>Log out</a></html>"
    session = FakeSession(
        gets=[
            FakeResponse(login, "https://www.pythonanywhere.com/login/"),
            FakeResponse(fixture("dashboard_before.html"), "https://www.pythonanywhere.com/user/demo/webapps/"),
            FakeResponse(fixture("dashboard_after.html"), "https://www.pythonanywhere.com/user/demo/webapps/"),
        ],
        posts=[
            FakeResponse(logged_in, "https://www.pythonanywhere.com/user/demo/"),
            FakeResponse("ok", "https://www.pythonanywhere.com/user/demo/webapps/"),
        ],
    )
    client = AuthenticatedHttpClient(
        account(), Credentials(password="secret"), session=session, timeout_seconds=5
    )
    result = client.renew(dry_run=False)
    assert result.changed
    assert result.before.expiry == date(2026, 8, 22)
    assert result.after.expiry == date(2026, 9, 22)
    renewal_url, fields = session.posted[1]
    assert renewal_url.endswith("/extend/")
    assert fields == {"csrfmiddlewaretoken": "csrf-before"}


def test_dry_run_never_posts_renewal() -> None:
    login = "<form><input name='csrfmiddlewaretoken' value='login-csrf'></form>"
    session = FakeSession(
        gets=[
            FakeResponse(login, "https://www.pythonanywhere.com/login/"),
            FakeResponse(fixture("dashboard_before.html"), "https://www.pythonanywhere.com/user/demo/webapps/"),
        ],
        posts=[FakeResponse("<a href='/logout/'>Log out</a>", "https://www.pythonanywhere.com/user/demo/")],
    )
    client = AuthenticatedHttpClient(account(), Credentials(password="secret"), session=session)
    result = client.renew(dry_run=True)
    assert not result.changed
    assert len(session.posted) == 1


def test_health_check_accepts_redirected_success() -> None:
    session = FakeSession(
        gets=[
            FakeResponse(
                "",
                "https://demo.pythonanywhere.com/",
                302,
                headers={"Location": "/home?temporary=secret"},
            ),
            FakeResponse("ok", "https://demo.pythonanywhere.com/home?temporary=secret", 200),
        ],
        posts=[],
    )
    result = HealthChecker(timeout_seconds=2, session=session).check(
        "https://demo.pythonanywhere.com"
    )
    assert result.online
    assert result.status_code == 200
    assert result.redirect_count == 1
    assert result.final_url == "https://demo.pythonanywhere.com/home"


def test_health_check_does_not_follow_cross_origin_redirect() -> None:
    start = "https://demo.pythonanywhere.com"
    session = FakeSession(
        gets=[
            FakeResponse(
                "",
                f"{start}/",
                302,
                headers={"Location": "http://169.254.169.254/latest?token=secret"},
            )
        ],
        posts=[],
    )
    result = HealthChecker(timeout_seconds=2, session=session).check(start)
    assert result.online
    assert result.status_code == 302
    assert result.redirect_count == 1
    assert result.final_url == f"{start}/"
    assert session.requested_gets == [start]


def test_renewal_rejects_form_action_on_another_origin() -> None:
    login = "<form><input name='csrfmiddlewaretoken' value='login-csrf'></form>"
    dashboard = """
    <div id="id_demo_pythonanywhere_com">
      <strong class="webapp_expiry">Tuesday 22 September 2026</strong>
      <form action="https://evil.example/user/demo/webapps/demo.pythonanywhere.com/extend/">
        <input name="csrfmiddlewaretoken" value="secret-csrf">
      </form>
    </div>
    """
    session = FakeSession(
        gets=[
            FakeResponse(login, "https://www.pythonanywhere.com/login/"),
            FakeResponse(dashboard, "https://www.pythonanywhere.com/user/demo/webapps/"),
        ],
        posts=[
            FakeResponse(
                "<a href='/logout/'>Log out</a>",
                "https://www.pythonanywhere.com/user/demo/",
            )
        ],
    )
    client = AuthenticatedHttpClient(account(), Credentials(password="secret"), session=session)
    with pytest.raises(InterfaceChangedError, match="origem não confiável"):
        client.renew(dry_run=False)
    assert len(session.posted) == 1
