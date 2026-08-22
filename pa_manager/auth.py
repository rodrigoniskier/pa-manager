from __future__ import annotations

import secrets
import time
from collections import OrderedDict, deque

from fastapi import HTTPException, Request, status


class LoginRateLimiter:
    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: int = 300,
        max_keys: int = 2048,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_keys = max(1, max_keys)
        self._attempts: OrderedDict[str, deque[float]] = OrderedDict()

    def _prune(self, key: str, now: float) -> deque[float] | None:
        attempts = self._attempts.get(key)
        if attempts is None:
            return None
        while attempts and attempts[0] <= now - self.window_seconds:
            attempts.popleft()
        if not attempts:
            self._attempts.pop(key, None)
            return None
        return attempts

    def _prune_expired_keys(self, now: float) -> None:
        for key in list(self._attempts):
            self._prune(key, now)

    def allowed(self, key: str) -> bool:
        attempts = self._prune(key, time.monotonic())
        return attempts is None or len(attempts) < self.max_attempts

    def failure(self, key: str) -> None:
        now = time.monotonic()
        attempts = self._prune(key, now)
        if attempts is None:
            self._prune_expired_keys(now)
            while len(self._attempts) >= self.max_keys:
                self._attempts.popitem(last=False)
            attempts = deque()
            self._attempts[key] = attempts
        attempts.append(now)
        self._attempts.move_to_end(key)

    def success(self, key: str) -> None:
        self._attempts.pop(key, None)


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def verify_csrf(request: Request, submitted: object) -> None:
    expected = request.session.get("csrf_token", "")
    actual = submitted if isinstance(submitted, str) else ""
    if not expected or not secrets.compare_digest(str(expected), actual):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF inválido")


def require_login(request: Request) -> None:
    if request.session.get("authenticated") is not True:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login necessário")
