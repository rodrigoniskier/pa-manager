from pa_manager.auth import LoginRateLimiter


def test_login_rate_limiter_is_bounded_and_does_not_allocate_on_read() -> None:
    limiter = LoginRateLimiter(max_attempts=2, window_seconds=300, max_keys=2)
    assert limiter.allowed("read-only")
    assert not limiter._attempts

    limiter.failure("first")
    limiter.failure("second")
    limiter.failure("third")
    assert len(limiter._attempts) == 2
    assert "first" not in limiter._attempts


def test_login_rate_limiter_blocks_after_limit() -> None:
    limiter = LoginRateLimiter(max_attempts=2, window_seconds=300)
    limiter.failure("client")
    assert limiter.allowed("client")
    limiter.failure("client")
    assert not limiter.allowed("client")
