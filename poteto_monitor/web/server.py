"""FastAPI アプリ本体（静的 UI + JSON API + SSE）。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import ConfigError, load_config, masked_view, merge_incoming, read_raw, write_raw
from ..history import sample_base
from .auth import COOKIE_NAME, SESSION_TTL, client_key, is_https, token_matches
from .context import AppContext
from .poller import poll_loop

log = logging.getLogger("poteto-monitor.web")
STATIC_DIR = Path(__file__).parent / "static"
HISTORY_RANGES = {"24h": 86400, "7d": 7 * 86400}
HISTORY_POINTS = 240


# 静的 UI は自前ファイルのみなので、外部読み込みもインライン実行も許可しない。
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def _auth_dependencies(ctx: AppContext):
    """(閲覧用, 書き込み用) の依存関数を返す。

    - トークン未設定: 閲覧は誰でも可、書き込み（設定変更・即時更新）は 403（読み取り専用）。
      公開時の保護は Cloudflare Access 等に委ねる。
    - トークン設定済み: 書き込みは常に認証必須。閲覧は web.protect_read が true なら認証必須。
    """
    limiter, sessions = ctx.auth_limiter, ctx.sessions

    def authenticated(request: Request, token: str) -> bool:
        if sessions.valid(token, request.cookies.get(COOKIE_NAME)):
            return True
        supplied = request.headers.get("x-auth-token")
        if supplied is None:
            return False
        key = client_key(request)
        limiter.check(key)
        if token_matches(token, supplied):
            return True
        limiter.record_failure(key)
        return False

    async def read(request: Request) -> None:
        token = ctx.config.web_auth_token
        if token and ctx.config.web_protect_read and not authenticated(request, token):
            raise HTTPException(status_code=401, detail="認証が必要です")

    async def write(request: Request) -> None:
        token = ctx.config.web_auth_token
        if not token:
            raise HTTPException(
                status_code=403,
                detail="web.auth_token が未設定のため読み取り専用です（config.json か WEB_AUTH_TOKEN で設定してください）",
            )
        if not authenticated(request, token):
            raise HTTPException(status_code=401, detail="認証が必要です")

    return read, write


def create_app(ctx: AppContext | None = None) -> FastAPI:
    if ctx is None:
        ctx = AppContext(load_config())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(poll_loop(ctx))
        try:
            yield
        finally:
            ctx.stop.set()
            ctx.wake.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            await ctx.close()

    # API ドキュメントは公開しない（エンドポイントの一覧を外部に見せない）。
    app = FastAPI(title="poteto-monitor", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    read_auth, write_auth = _auth_dependencies(ctx)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    # ── 認証 ─────────────────────────────────────────────────────────
    @app.get("/api/auth")
    async def auth_status(request: Request) -> dict:
        """UI がログイン画面を出すかどうかの判断用。"""
        token = ctx.config.web_auth_token
        return {
            "configured": bool(token),
            "protect_read": bool(token) and ctx.config.web_protect_read,
            "authenticated": ctx.sessions.valid(token, request.cookies.get(COOKIE_NAME)),
        }

    @app.post("/api/login")
    async def login(request: Request, response: Response, payload: dict = Body(...)) -> dict:
        token = ctx.config.web_auth_token
        if not token:
            raise HTTPException(status_code=403, detail="web.auth_token が未設定です")
        key = client_key(request)
        ctx.auth_limiter.check(key)
        if not token_matches(token, str(payload.get("token", ""))):
            ctx.auth_limiter.record_failure(key)
            log.warning("ログイン失敗（%s）", key)
            raise HTTPException(status_code=401, detail="トークンが違います")
        response.set_cookie(
            COOKIE_NAME, ctx.sessions.issue(token), max_age=SESSION_TTL, httponly=True,
            samesite="strict", secure=is_https(request), path="/",
        )
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response) -> dict:
        ctx.sessions.revoke(request.cookies.get(COOKIE_NAME))  # Cookie が漏れていても使えなくする
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    # ── UI ───────────────────────────────────────────────────────────
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ── 状態 API ─────────────────────────────────────────────────────
    @app.get("/api/state", dependencies=[Depends(read_auth)])
    async def get_state() -> dict:
        return ctx.state.snapshot()

    @app.get("/api/stream", dependencies=[Depends(read_auth)])
    async def stream(request: Request) -> StreamingResponse:
        queue = ctx.state.subscribe()

        async def event_source():
            # 接続直後に現在のスナップショットを送る。
            yield f"data: {json.dumps(ctx.state.snapshot())}\n\n"
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {payload}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"  # プロキシのタイムアウト対策
            finally:
                ctx.state.unsubscribe(queue)

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(event_source(), media_type="text/event-stream", headers=headers)

    @app.get("/api/history", dependencies=[Depends(read_auth)])
    def get_history(range_: str = Query("24h", alias="range"), key: str | None = None) -> dict:
        """履歴 DB の時系列（間引き済み）。同期関数なので FastAPI のスレッドプールで動く。"""
        span = HISTORY_RANGES.get(range_)
        if span is None:
            raise HTTPException(status_code=400, detail=f"range は {' / '.join(HISTORY_RANGES)} のいずれかです")
        cfg = ctx.config
        assets = [a for a in cfg.assets if key is None or a.key == key]
        if key is not None and not assets:
            raise HTTPException(status_code=404, detail=f"監視対象に '{key}' がありません")
        until = int(time.time())
        series = {
            a.key: ctx.history.series(a.key, sample_base(a.type, cfg.base_currency), until - span, until, HISTORY_POINTS)
            for a in assets
        }
        return {"range": range_, "series": series}

    # ── ヘルスチェック（Uptime Kuma 等の外部監視用）──────────────────
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        """ポーラーが動いていて、全データソースの直近の取得が成功していれば 200。"""
        now = time.time()
        alive = ctx.last_tick is not None and now - ctx.last_tick < max(3 * ctx.config.poll_interval, 180)
        sources = {
            name: {"ok": h.ok, "failures": h.failures, "last_success": h.last_success}
            for name, h in ctx.fetcher.health().items()
        }
        ok = alive and ctx.state.status != "error" and all(s["ok"] for s in sources.values())
        status = "ok" if ok else ("starting" if ctx.last_tick is None else "unhealthy")
        body = {"status": status, "last_tick": ctx.last_tick, "sources": sources}
        return JSONResponse(body, status_code=200 if ok else 503)

    # ── 設定 API ─────────────────────────────────────────────────────
    # 設定は秘匿値の有無や監視リストを含むため、閲覧にも書き込み用の認証を求める。
    def _read_config() -> dict:
        try:
            return read_raw()
        except ConfigError as exc:
            # 壊れた config.json を既定値で上書きしないよう、読めないときは操作を止める。
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/config", dependencies=[Depends(write_auth)])
    async def get_config() -> dict:
        return masked_view(_read_config())

    @app.put("/api/config", dependencies=[Depends(write_auth)])
    async def put_config(payload: dict) -> dict:
        try:
            merged = merge_incoming(_read_config(), payload)
            write_raw(merged)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.reload()  # ポーラーへ即反映（間隔・銘柄・閾値・トークン）
        return masked_view(_read_config())

    @app.post("/api/refresh", dependencies=[Depends(write_auth)])
    async def refresh() -> dict:
        # 間引かれた要求は直近の取得結果で足りるので、エラーにはしない。
        return {"ok": True, "queued": ctx.trigger_refresh()}

    return app
