"""Web UI の認証（Cookie セッション + 試行回数の制限）。

- トークンは ``web.auth_token``（または環境変数 ``WEB_AUTH_TOKEN``）。
- ``POST /api/login`` でトークンを確かめ、署名付きの HttpOnly / SameSite=Strict Cookie を発行する。
  署名鍵はトークンから導くため、トークンを変えると既存のセッションはすべて無効になる。
- スクリプト向けに ``X-Auth-Token`` ヘッダーも受け付ける。
- 認証失敗が続いたら、しばらくの間はすべての認証試行を 429 で断る（総当たり対策）。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import deque

from fastapi import HTTPException, Request

COOKIE_NAME = "poteto_session"
SESSION_TTL = 30 * 86400  # 秒
MAX_FAILURES = 10  # FAILURE_WINDOW 秒の間にこれだけ失敗したらロックする
FAILURE_WINDOW = 600


def _sign(token: str, expires: int) -> str:
    key = hashlib.sha256(b"poteto-session:" + token.encode()).digest()
    return hmac.new(key, str(expires).encode(), hashlib.sha256).hexdigest()


def make_session(token: str, now: float | None = None) -> str:
    expires = int((now or time.time()) + SESSION_TTL)
    return f"{expires}.{_sign(token, expires)}"


def session_valid(token: str, value: str | None, now: float | None = None) -> bool:
    if not token or not value or "." not in value:
        return False
    expires_s, sig = value.split(".", 1)
    if not expires_s.isdigit() or int(expires_s) < (now or time.time()):
        return False
    return hmac.compare_digest(sig, _sign(token, int(expires_s)))


def token_matches(token: str, supplied: str | None) -> bool:
    return bool(token) and supplied is not None and hmac.compare_digest(supplied.encode(), token.encode())


def is_https(request: Request) -> bool:
    """Cloudflare Tunnel 経由では TLS 終端が手前にあるため、転送ヘッダーも見る。"""
    if request.url.scheme == "https":
        return True
    proto = request.headers.get("x-forwarded-proto", "")
    return proto == "https" or '"scheme":"https"' in request.headers.get("cf-visitor", "").replace(" ", "")


class FailureLimiter:
    """認証失敗の回数制限。利用者が少ないツールなので、送信元を区別せず全体で数える。"""

    def __init__(self) -> None:
        self._failures: deque[float] = deque()

    def _trim(self, now: float) -> None:
        while self._failures and now - self._failures[0] > FAILURE_WINDOW:
            self._failures.popleft()

    def check(self) -> None:
        now = time.monotonic()
        self._trim(now)
        if len(self._failures) >= MAX_FAILURES:
            retry = int(FAILURE_WINDOW - (now - self._failures[0])) + 1
            raise HTTPException(
                status_code=429, detail="認証の失敗が続いたため一時的に制限しています",
                headers={"Retry-After": str(retry)},
            )

    def record_failure(self) -> None:
        self._failures.append(time.monotonic())
