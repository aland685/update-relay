# -*- coding: utf-8 -*-
"""Update Relay 主服务入口：FastAPI 路由、后台定时清理与管理后台。"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .cache_manager import (
    SingleflightRegistry,
    cleanup_once,
    delete_single_cached_file,
    get_cache_stats,
    purge_all_cache,
    purge_repo_cache,
    remaining_ttl_seconds,
)
from .config import Settings
from .github_client import (
    fetch_latest_release,
    rewrite_release_urls,
    stream_and_cache_asset,
)
from .security import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE,
    create_session_token,
    get_authenticated_admin,
    get_external_base_url,
    is_safe_segment,
    is_safe_tag,
    normalize_repo,
    verify_credentials,
)
from .storage import RelayStorage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("update_relay")

BASE_DIR = Path(__file__).resolve().parent


class RepoCreateRequest(BaseModel):
    repo: str
    note: str = ""
    enabled: bool = True


class ToggleRequest(BaseModel):
    enabled: bool


def _format_bytes(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    for unit in ("KB", "MB", "GB"):
        num_f = num / 1024.0
        if num_f < 1024.0 or unit == "GB":
            return f"{num_f:.2f} {unit}"
        num = int(num_f)
    return f"{num} B"


def create_app(
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    cfg = settings or Settings.from_env()
    storage = RelayStorage(cfg.db_path)
    singleflight = SingleflightRegistry()
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(cfg.upstream_timeout_seconds, connect=20.0),
        )
        app.state.settings = cfg
        app.state.storage = storage
        app.state.singleflight = singleflight
        app.state.http_client = client

        async def _cleanup_loop() -> None:
            while True:
                try:
                    await asyncio.sleep(cfg.cleanup_interval_seconds)
                    cleanup_once(storage, cfg)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.exception("后台缓存清理任务异常: %s", exc)

        cleanup_task = asyncio.create_task(_cleanup_loop())
        logger.info(
            "Update Relay 已启动: %s:%s | TTL=%ds (%s) | 上限=%d MB",
            cfg.host,
            cfg.port,
            cfg.cache_ttl_seconds,
            cfg.cache_ttl_mode,
            cfg.max_cache_mb,
        )
        try:
            yield
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            if owns_client:
                await client.aclose()

    app = FastAPI(
        title="Update Relay",
        description="多项目 GitHub Release 按需加速与 1 小时自动清理中转站",
        version="1.0.0",
        lifespan=lifespan,
    )

    static_dir = BASE_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def require_admin(request: Request) -> str:
        user = get_authenticated_admin(request, app.state.settings)
        if not user:
            raise HTTPException(status_code=401, detail="未登录或会话已过期")
        return user

    def _build_overview_data(request: Request) -> dict[str, Any]:
        now = time.time()
        base_url = get_external_base_url(request, app.state.settings)
        repos = app.state.storage.list_repos()
        all_files = app.state.storage.list_cached_files()
        files_by_repo: dict[str, list[dict[str, Any]]] = {}
        for f in all_files:
            key = f["repo"].lower()
            rem = remaining_ttl_seconds(f, app.state.settings, now=now)
            encoded_tag = quote(f["tag"], safe="")
            encoded_name = quote(f["filename"], safe="")
            files_by_repo.setdefault(key, []).append(
                {
                    **f,
                    "size_human": _format_bytes(int(f["size"])),
                    "ttl_remaining_seconds": rem,
                    "download_url": f"{base_url}/dl/{f['repo']}/{encoded_tag}/{encoded_name}",
                }
            )

        enriched_repos: list[dict[str, Any]] = []
        for r in repos:
            repo_name = r["repo"]
            cached_latest = app.state.storage.get_release_cache(repo_name, "__latest__")
            latest_tag = None
            meta_ttl_remaining = 0
            assets_preview: list[dict[str, str]] = []
            if cached_latest is not None:
                payload, fetched_at = cached_latest
                latest_tag = str(payload.get("tag_name") or "").strip() or None
                meta_ttl_remaining = max(
                    0, int(round((fetched_at + app.state.settings.metadata_ttl_seconds) - now))
                )
                if latest_tag:
                    encoded_tag = quote(latest_tag, safe="")
                    for asset in payload.get("assets") or []:
                        if isinstance(asset, dict) and asset.get("name"):
                            aname = str(asset["name"])
                            assets_preview.append(
                                {
                                    "name": aname,
                                    "download_url": f"{base_url}/dl/{repo_name}/{encoded_tag}/{quote(aname, safe='')}",
                                }
                            )

            enriched_repos.append(
                {
                    **r,
                    "enabled": bool(r["enabled"]),
                    "latest_tag": latest_tag,
                    "meta_ttl_remaining": meta_ttl_remaining,
                    "api_url": f"{base_url}/repos/{repo_name}/releases/latest",
                    "cached_files": files_by_repo.get(repo_name.lower(), []),
                    "assets_preview": assets_preview,
                }
            )

        return {
            "stats": get_cache_stats(app.state.storage, app.state.settings),
            "repos": enriched_repos,
            "base_url": base_url,
        }

    # ---- 健康检查与状态页 ----

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        user = get_authenticated_admin(request, app.state.settings)
        if user:
            return RedirectResponse(url="/admin", status_code=302)
        return templates.TemplateResponse(
            request=request,
            name="status.html",
            context={"stats": get_cache_stats(app.state.storage, app.state.settings)},
        )

    # ---- 管理员网页与登录 ----

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page(request: Request, error: str = ""):
        user = get_authenticated_admin(request, app.state.settings)
        if not user:
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"error": error},
            )
        overview = _build_overview_data(request)
        return templates.TemplateResponse(
            request=request,
            name="admin.html",
            context={"user": user, **overview},
        )

    @app.post("/admin/login")
    async def admin_login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        if not verify_credentials(username, password, app.state.settings):
            if "application/json" in request.headers.get("accept", ""):
                raise HTTPException(status_code=401, detail="用户名或密码错误")
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"error": "用户名或密码错误，请重试。"},
                status_code=401,
            )

        token = create_session_token(username, app.state.settings)
        resp = RedirectResponse(url="/admin", status_code=303)
        resp.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
        )
        return resp

    @app.post("/admin/logout")
    async def admin_logout():
        resp = RedirectResponse(url="/admin", status_code=303)
        resp.delete_cookie(SESSION_COOKIE_NAME)
        return resp

    # ---- 管理员 JSON API ----

    @app.get("/api/admin/overview")
    async def api_overview(request: Request, _admin: str = Depends(require_admin)):
        return _build_overview_data(request)

    @app.post("/api/admin/repos")
    async def api_add_repo(body: RepoCreateRequest, _admin: str = Depends(require_admin)):
        parts = body.repo.strip().split("/")
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail="仓库格式必须为 owner/repo")
        normalized = normalize_repo(parts[0], parts[1])
        if not normalized:
            raise HTTPException(status_code=400, detail="仓库名称包含非法字符")
        item = app.state.storage.upsert_repo(normalized, enabled=body.enabled, note=body.note)
        return {"ok": True, "repo": item}

    @app.patch("/api/admin/repos/{owner}/{repo}/toggle")
    async def api_toggle_repo(
        owner: str,
        repo: str,
        body: ToggleRequest,
        _admin: str = Depends(require_admin),
    ):
        normalized = normalize_repo(owner, repo)
        if not normalized:
            raise HTTPException(status_code=400, detail="非法仓库名")
        if not app.state.storage.set_repo_enabled(normalized, body.enabled):
            raise HTTPException(status_code=404, detail="仓库不在白名单中")
        return {"ok": True, "repo": normalized, "enabled": body.enabled}

    @app.post("/api/admin/repos/{owner}/{repo}/refresh")
    async def api_refresh_repo(
        owner: str,
        repo: str,
        _admin: str = Depends(require_admin),
    ):
        normalized = normalize_repo(owner, repo)
        if not normalized:
            raise HTTPException(status_code=400, detail="非法仓库名")
        if not app.state.storage.get_repo(normalized):
            raise HTTPException(status_code=404, detail="仓库不在白名单中")
        payload, _ = await fetch_latest_release(
            app.state.http_client,
            app.state.storage,
            app.state.settings,
            owner,
            repo,
            force_refresh=True,
        )
        return {"ok": True, "tag_name": payload.get("tag_name")}

    @app.delete("/api/admin/repos/{owner}/{repo}/cache")
    async def api_purge_repo_cache(
        owner: str,
        repo: str,
        _admin: str = Depends(require_admin),
    ):
        normalized = normalize_repo(owner, repo)
        if not normalized:
            raise HTTPException(status_code=400, detail="非法仓库名")
        removed = purge_repo_cache(app.state.storage, app.state.settings, normalized)
        return {"ok": True, "removed_files": removed}

    @app.delete("/api/admin/repos/{owner}/{repo}")
    async def api_delete_repo(
        owner: str,
        repo: str,
        _admin: str = Depends(require_admin),
    ):
        normalized = normalize_repo(owner, repo)
        if not normalized:
            raise HTTPException(status_code=400, detail="非法仓库名")
        purge_repo_cache(app.state.storage, app.state.settings, normalized)
        deleted = app.state.storage.delete_repo(normalized)
        if not deleted:
            raise HTTPException(status_code=404, detail="仓库不存在")
        return {"ok": True}

    @app.delete("/api/admin/cache/file")
    async def api_delete_cached_file(
        repo: str = Query(...),
        tag: str = Query(...),
        filename: str = Query(...),
        _admin: str = Depends(require_admin),
    ):
        removed = delete_single_cached_file(app.state.storage, app.state.settings, repo, tag, filename)
        if not removed:
            raise HTTPException(status_code=404, detail="缓存文件不存在")
        return {"ok": True}

    @app.post("/api/admin/cache/purge")
    async def api_purge_all(_admin: str = Depends(require_admin)):
        removed = purge_all_cache(app.state.storage, app.state.settings)
        return {"ok": True, "removed_files": removed}

    # ---- 对外中转接口：GitHub Releases API 兼容 ----

    @app.get("/repos/{owner}/{repo}/releases/latest")
    async def proxy_latest_release(
        request: Request,
        owner: str,
        repo: str,
        refresh: int = Query(0),
    ):
        normalized = normalize_repo(owner, repo)
        if not normalized:
            raise HTTPException(status_code=400, detail="非法的仓库路径")
        if not app.state.storage.is_repo_enabled(normalized):
            raise HTTPException(status_code=403, detail="该仓库未在白名单中或已禁用")

        payload, from_cache = await fetch_latest_release(
            app.state.http_client,
            app.state.storage,
            app.state.settings,
            owner,
            repo,
            force_refresh=(refresh == 1),
        )
        base_url = get_external_base_url(request, app.state.settings)
        rewritten = rewrite_release_urls(payload, owner, repo, base_url)
        return JSONResponse(
            content=rewritten,
            headers={"X-Relay-Meta-Cache": "HIT" if from_cache else "MISS"},
        )

    # ---- 对外中转接口：按需流式下载与 1 小时缓存 ----

    @app.api_route("/dl/{owner}/{repo}/{tag}/{filename}", methods=["GET", "HEAD"])
    async def download_asset(
        request: Request,
        owner: str,
        repo: str,
        tag: str,
        filename: str,
    ):
        normalized = normalize_repo(owner, repo)
        if not normalized or not is_safe_tag(tag) or not is_safe_segment(filename):
            raise HTTPException(status_code=400, detail="非法请求路径或文件名")

        if not app.state.storage.is_repo_enabled(normalized):
            raise HTTPException(status_code=403, detail="该仓库未在白名单中或已禁用")

        return await stream_and_cache_asset(
            request=request,
            client=app.state.http_client,
            storage=app.state.storage,
            settings=app.state.settings,
            singleflight=app.state.singleflight,
            owner=owner,
            repo=repo,
            tag=tag,
            filename=filename,
            head_only=(request.method == "HEAD"),
        )

    return app


app = create_app()
