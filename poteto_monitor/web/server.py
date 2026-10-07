"""FastAPI アプリ本体（静的 UI + JSON API + SSE）。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import ConfigError, load_config, masked_view, merge_incoming, read_raw, write_raw
from ..history import sample_base
from .context import AppContext
from .poller import poll_loop

log = logging.getLogger("poteto-monitor.web")
STATIC_DIR = Path(__file__).parent / "static"
HISTORY_RANGES = {"24h": 86400, "7d": 7 * 86400}
HISTORY_POINTS = 240


def _auth_dependency(ctx: AppContext):
    """auth_token が設定されていれば API を保護する依存関数を返す。"""

    async def check(
        authorization: str | None = Header(default=None),
        x_auth_token: str | None = Header(default=None),
    ) -> None:
        token = ctx.config.web_auth_token
        if not token:
            return  # 認証未設定なら誰でもアクセス可（Cloudflare Access 等に委ねる）
        supplied = x_auth_token
        if not supplied and authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        if supplied != token:
            raise HTTPException(status_code=401, detail="認証が必要です")

    return check


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

    app = FastAPI(title="poteto-monitor", lifespan=lifespan)
    app.state.ctx = ctx
    auth = _auth_dependency(ctx)

    # ── UI ───────────────────────────────────────────────────────────
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ── 状態 API ─────────────────────────────────────────────────────
    @app.get("/api/state")
    async def get_state() -> dict:
        return ctx.state.snapshot()

    @app.get("/api/stream")
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
                        snap = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(snap)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"  # プロキシのタイムアウト対策
            finally:
                ctx.state.unsubscribe(queue)

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(event_source(), media_type="text/event-stream", headers=headers)

    @app.get("/api/history")
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
    @app.get("/api/config", dependencies=[Depends(auth)])
    async def get_config() -> dict:
        return masked_view(read_raw())

    @app.put("/api/config", dependencies=[Depends(auth)])
    async def put_config(payload: dict) -> dict:
        merged = merge_incoming(read_raw(), payload)
        try:
            write_raw(merged)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.reload()  # ポーラーへ即反映（間隔・銘柄・閾値）
        return masked_view(read_raw())

    @app.post("/api/refresh", dependencies=[Depends(auth)])
    async def refresh() -> dict:
        # 間引かれた要求は直近の取得結果で足りるので、エラーにはしない。
        return {"ok": True, "queued": ctx.trigger_refresh()}

    return app
