"""Web UI の認証（Cookie セッション + 送信元ごとの試行回数の制限）。

- トークンは ``web.auth_token``（または環境変数 ``WEB_AUTH_TOKEN``）。
- ``POST /api/login`` でトークンを確かめ、ランダムなセッション ID を HttpOnly / SameSite=Strict の Cookie で渡す。
  セッションはサーバー側（メモリ）で管理するので、ログアウトやトークン変更で即座に無効にできる。
  プロセスを再起動するとセッションは消える（再ログインが必要）。
- スクリプト向けに ``X-Auth-Token`` ヘッダーも受け付ける。
- 認証失敗が続いた送信元は、しばらくの間 429 で断る（総当たり対策）。送信元ごとに数えるので、
  他人が失敗を繰り返しても正しいトークンを持つ人は締め出されない。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import deque

from fastapi import HTTPException, Request

COOKIE_NAME = "poteto_session"
SESSION_TTL = 30 * 86400  # 秒
MAX_SESSIONS = 1000  # 溢れたら期限の近いものから捨てる
MAX_FAILURES = 10  # FAILURE_WINDOW 秒の間に同じ送信元がこれだけ失敗したらロックする
FAILURE_WINDOW = 600
MAX_TRACKED_CLIENTS = 10_000
LOOPBACK = {"127.0.0.1", "::1"}


def token_matches(token: str, supplied: str | None) -> bool:
    return bool(token) and supplied is not None and hmac.compare_digest(supplied.encode(), token.encode())


def is_https(request: Request) -> bool:
    """Cloudflare Tunnel 経由では TLS 終端が手前にあるため、転送ヘッダーも見る。"""
    if request.url.scheme == "https":
        return True
    proto = request.headers.get("x-forwarded-proto", "")
    return proto == "https" or '"scheme":"https"' in request.headers.get("cf-visitor", "").replace(" ", "")


def client_key(request: Request) -> str:
    """送信元の識別子。ループバック（同じホストの cloudflared）からの接続に限り CF-Connecting-IP を信じる。"""
    peer = request.client.host if request.client else ""
    if peer in LOOPBACK:
        forwarded = request.headers.get("cf-connecting-ip", "").strip()
        if forwarded:
            return forwarded
    return peer or "unknown"


class SessionStore:
    """サーバー側のセッション表。トークンが変わったら全セッションを破棄する。"""

    def __init__(self) -> None:
        self._sessions: dict[str, float] = {}
        self._fingerprint = b""

    def _bind(self, token: str) -> None:
        fp = hashlib.sha256(token.encode()).digest()
        if not hmac.compare_digest(fp, self._fingerprint):
            self._sessions.clear()
            self._fingerprint = fp

    def issue(self, token: str) -> str:
        self._bind(token)
        now = time.time()
        self._sessions = {sid: exp for sid, exp in self._sessions.items() if exp > now}
        while len(self._sessions) >= MAX_SESSIONS:
            del self._sessions[min(self._sessions, key=self._sessions.__getitem__)]
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = now + SESSION_TTL
        return sid

    def valid(self, token: str, sid: str | None) -> bool:
        if not token or not sid:
            return False
        self._bind(token)
        expires = self._sessions.get(sid)
        if expires is None:
            return False
        if expires < time.time():
            del self._sessions[sid]
            return False
        return True

    def revoke(self, sid: str | None) -> None:
        if sid:
            self._sessions.pop(sid, None)


class FailureLimiter:
    """送信元ごとの認証失敗回数の制限。"""

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = {}

    def _trim(self, key: str, now: float) -> deque[float]:
        q = self._failures.get(key, deque())
        while q and now - q[0] > FAILURE_WINDOW:
            q.popleft()
        if not q:
            self._failures.pop(key, None)
        return q

    def check(self, key: str) -> None:
        now = time.monotonic()
        q = self._trim(key, now)
        if len(q) >= MAX_FAILURES:
            retry = int(FAILURE_WINDOW - (now - q[0])) + 1
            raise HTTPException(
                status_code=429, detail="認証の失敗が続いたため一時的に制限しています",
                headers={"Retry-After": str(retry)},
            )

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        q = self._trim(key, now)
        if key not in self._failures and len(self._failures) >= MAX_TRACKED_CLIENTS:
            # 記録が溢れたら古い送信元から捨てる（メモリを使い切らせない）。
            oldest = min(self._failures, key=lambda k: self._failures[k][-1])
            del self._failures[oldest]
        q.append(now)
        self._failures[key] = q
