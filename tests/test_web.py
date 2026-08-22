from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from starlette.requests import Request

from pa_manager.config import Settings
from pa_manager.web import _client_key, create_app


def make_accounts(path: Path) -> None:
    path.write_text(
        """
accounts:
  - id: demo
    label: Demo
    username: demo
    domain: demo.pythonanywhere.com
    password_env: PA_DEMO_PASSWORD
    token_env: PA_DEMO_API_TOKEN
""",
        encoding="utf-8",
    )


def csrf(html: str) -> str:
    field = BeautifulSoup(html, "html.parser").select_one("input[name='csrf_token']")
    assert field is not None
    return str(field.get("value"))


def test_client_key_uses_configured_proxy_hop_from_the_right() -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/login",
            "headers": [
                (b"x-forwarded-for", b"203.0.113.99, 198.51.100.42"),
            ],
            "client": ("100.64.0.5", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )
    assert _client_key(request, trusted_proxy_hops=0) == "100.64.0.5"
    assert _client_key(request, trusted_proxy_hops=1) == "198.51.100.42"


async def test_dashboard_requires_login_and_sets_security_headers(tmp_path: Path) -> None:
    config = tmp_path / "accounts.yaml"
    make_accounts(config)
    settings = Settings(
        accounts_file=config,
        db_path=tmp_path / "manager.db",
        admin_username="admin",
        admin_password="long-test-password",
        session_secret="s" * 48,
        trusted_hosts=("testserver",),
    )
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        redirected = await client.get("/", follow_redirects=False)
        assert redirected.status_code == 303
        page = await client.get("/login")
        assert page.headers["x-frame-options"] == "DENY"
        token = csrf(page.text)
        invalid = await client.post(
            "/login",
            data={"csrf_token": token, "username": "admin", "password": "wrong-password"},
        )
        assert invalid.status_code == 401
        token = csrf(invalid.text)
        valid = await client.post(
            "/login",
            data={
                "csrf_token": token,
                "username": "admin",
                "password": "long-test-password",
            },
            follow_redirects=False,
        )
        assert valid.status_code == 303
        dashboard = await client.get("/")
        assert dashboard.status_code == 200
        assert "Demo" in dashboard.text
        assert "MODO DE SIMULAÇÃO" in dashboard.text


async def test_forwarded_for_cannot_bypass_login_rate_limit(tmp_path: Path) -> None:
    config = tmp_path / "accounts.yaml"
    make_accounts(config)
    settings = Settings(
        accounts_file=config,
        db_path=tmp_path / "manager.db",
        admin_username="admin",
        admin_password="long-test-password",
        session_secret="s" * 48,
        trusted_hosts=("testserver",),
    )
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        page = await client.get("/login")
        token = csrf(page.text)
        for attempt in range(5):
            response = await client.post(
                "/login",
                headers={"X-Forwarded-For": f"203.0.113.{attempt + 1}"},
                data={
                    "csrf_token": token,
                    "username": "admin",
                    "password": "wrong-password",
                },
            )
            assert response.status_code == 401
            token = csrf(response.text)
        blocked = await client.post(
            "/login",
            headers={"X-Forwarded-For": "198.51.100.10"},
            data={
                "csrf_token": token,
                "username": "admin",
                "password": "wrong-password",
            },
        )
        assert blocked.status_code == 429


async def test_login_rejects_oversized_form_before_parsing(tmp_path: Path) -> None:
    config = tmp_path / "accounts.yaml"
    make_accounts(config)
    settings = Settings(
        accounts_file=config,
        db_path=tmp_path / "manager.db",
        admin_password="long-test-password",
        session_secret="s" * 48,
        trusted_hosts=("testserver",),
    )
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/login",
            content=b"username=" + (b"a" * (64 * 1024)),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 413
