# -*- coding: utf-8 -*-
"""Update Relay 自动化测试：白名单拦截、API URL 改写、单飞与流式缓存、1 小时过期清理、路径穿越防护。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from app.cache_manager import cleanup_once
from app.config import Settings
from app.main import create_app
from app.storage import RelayStorage


def _make_settings(tmp_path: Path, max_cache_mb: int = 2048, cache_ttl_seconds: int = 3600) -> Settings:
    data_dir = tmp_path / "data"
    cache_dir = data_dir / "cache"
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        host="127.0.0.1",
        port=18080,
        admin_username="admin",
        admin_password="test-password-123",
        session_secret="test-session-secret-xyz",
        public_base_url="https://relay.test.local",
        github_token="",
        data_dir=data_dir,
        cache_dir=cache_dir,
        db_path=data_dir / "relay.db",
        metadata_ttl_seconds=300,
        cache_ttl_seconds=cache_ttl_seconds,
        cache_ttl_mode="sliding",
        cleanup_interval_seconds=300,
        max_cache_mb=max_cache_mb,
        upstream_timeout_seconds=30,
    )


class MockUpstreamState:
    def __init__(self) -> None:
        self.api_calls = 0
        self.dl_calls = 0
        self.payload_bytes = b"ZIP_BINARY_CONTENT_0123456789_ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def build_transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == "https://api.github.com/repos/aland685/super-start-for-ag/releases/latest":
                self.api_calls += 1
                return httpx.Response(
                    200,
                    json={
                        "tag_name": "v1.2.3",
                        "name": "v1.2.3",
                        "body": "Release notes",
                        "html_url": "https://github.com/aland685/super-start-for-ag/releases/tag/v1.2.3",
                        "assets": [
                            {
                                "name": "chaoxing-app-update.zip",
                                "size": len(self.payload_bytes),
                                "content_type": "application/zip",
                                "browser_download_url": "https://github.com/aland685/super-start-for-ag/releases/download/v1.2.3/chaoxing-app-update.zip",
                            }
                        ],
                    },
                )
            if url == "https://api.github.com/repos/aland685/super-start-for-ag/releases/tags/v1.2.3":
                self.api_calls += 1
                return httpx.Response(
                    200,
                    json={
                        "tag_name": "v1.2.3",
                        "assets": [
                            {
                                "name": "chaoxing-app-update.zip",
                                "size": len(self.payload_bytes),
                                "content_type": "application/zip",
                                "browser_download_url": "https://github.com/aland685/super-start-for-ag/releases/download/v1.2.3/chaoxing-app-update.zip",
                            }
                        ],
                    },
                )
            if url == "https://github.com/aland685/super-start-for-ag/releases/download/v1.2.3/chaoxing-app-update.zip":
                self.dl_calls += 1
                await asyncio.sleep(0.05)  # 模拟上游延迟以测试单飞锁
                return httpx.Response(
                    200,
                    content=self.payload_bytes,
                    headers={
                        "content-type": "application/zip",
                        "content-length": str(len(self.payload_bytes)),
                    },
                )
            return httpx.Response(404, json={"message": "Not Found"})

        return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_whitelist_blocking_and_admin_auth(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path)
    upstream = MockUpstreamState()
    async with httpx.AsyncClient(transport=upstream.build_transport()) as mock_http:
        app = create_app(settings=cfg, http_client=mock_http)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # 未登录访问管理 API -> 401
                r = await client.get("/api/admin/overview")
                assert r.status_code == 401

                # 非白名单仓库请求 -> 403
                r = await client.get("/repos/aland685/super-start-for-ag/releases/latest")
                assert r.status_code == 403
                r = await client.get("/dl/aland685/super-start-for-ag/v1.2.3/chaoxing-app-update.zip")
                assert r.status_code == 403

                # 使用 Bearer Token 添加白名单
                headers = {"Authorization": f"Bearer {cfg.admin_password}"}
                r = await client.post(
                    "/api/admin/repos",
                    headers=headers,
                    json={"repo": "aland685/super-start-for-ag", "note": "测试项目", "enabled": True},
                )
                assert r.status_code == 200

                # 禁用仓库 -> 403
                r = await client.patch(
                    "/api/admin/repos/aland685/super-start-for-ag/toggle",
                    headers=headers,
                    json={"enabled": False},
                )
                assert r.status_code == 200
                r = await client.get("/repos/aland685/super-start-for-ag/releases/latest")
                assert r.status_code == 403


@pytest.mark.asyncio
async def test_api_url_rewrite_and_metadata_cache(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path)
    upstream = MockUpstreamState()
    async with httpx.AsyncClient(transport=upstream.build_transport()) as mock_http:
        app = create_app(settings=cfg, http_client=mock_http)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Authorization": f"Bearer {cfg.admin_password}"}
                await client.post(
                    "/api/admin/repos",
                    headers=headers,
                    json={"repo": "aland685/super-start-for-ag", "enabled": True},
                )

                # 首次请求：MISS 并改写 browser_download_url
                r1 = await client.get("/repos/aland685/super-start-for-ag/releases/latest")
                assert r1.status_code == 200
                assert r1.headers.get("x-relay-meta-cache") == "MISS"
                data = r1.json()
                assert data["tag_name"] == "v1.2.3"
                assert (
                    data["assets"][0]["browser_download_url"]
                    == "https://relay.test.local/dl/aland685/super-start-for-ag/v1.2.3/chaoxing-app-update.zip"
                )
                assert upstream.api_calls == 1

                # 二次请求：命中元数据缓存
                r2 = await client.get("/repos/aland685/super-start-for-ag/releases/latest")
                assert r2.status_code == 200
                assert r2.headers.get("x-relay-meta-cache") == "HIT"
                assert upstream.api_calls == 1

                # ?refresh=1 强制刷新
                r3 = await client.get("/repos/aland685/super-start-for-ag/releases/latest?refresh=1")
                assert r3.status_code == 200
                assert r3.headers.get("x-relay-meta-cache") == "MISS"
                assert upstream.api_calls == 2


@pytest.mark.asyncio
async def test_streaming_download_singleflight_and_range(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path)
    upstream = MockUpstreamState()
    async with httpx.AsyncClient(transport=upstream.build_transport()) as mock_http:
        app = create_app(settings=cfg, http_client=mock_http)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Authorization": f"Bearer {cfg.admin_password}"}
                await client.post(
                    "/api/admin/repos",
                    headers=headers,
                    json={"repo": "aland685/super-start-for-ag", "enabled": True},
                )

                # 并发发起 3 个相同下载请求 -> 单飞锁应保证仅回源 1 次
                dl_url = "/dl/aland685/super-start-for-ag/v1.2.3/chaoxing-app-update.zip"
                r1, r2, r3 = await asyncio.gather(
                    client.get(dl_url),
                    client.get(dl_url),
                    client.get(dl_url),
                )
                for resp in (r1, r2, r3):
                    assert resp.status_code == 200
                    assert resp.content == upstream.payload_bytes
                assert upstream.dl_calls == 1

                # 命中缓存后支持 Range 断点续传
                r_range = await client.get(dl_url, headers={"Range": "bytes=0-9"})
                assert r_range.status_code == 206
                assert r_range.headers.get("x-relay-cache") == "HIT"
                assert r_range.headers.get("content-range") == f"bytes 0-9/{len(upstream.payload_bytes)}"
                assert r_range.content == upstream.payload_bytes[:10]
                assert upstream.dl_calls == 1


@pytest.mark.asyncio
async def test_path_traversal_and_invalid_asset_protection(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path)
    upstream = MockUpstreamState()
    async with httpx.AsyncClient(transport=upstream.build_transport()) as mock_http:
        app = create_app(settings=cfg, http_client=mock_http)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Authorization": f"Bearer {cfg.admin_password}"}
                await client.post(
                    "/api/admin/repos",
                    headers=headers,
                    json={"repo": "aland685/super-start-for-ag", "enabled": True},
                )

                # 路径穿越字符（URL 编码 ..）应被拦截
                r_trav = await client.get("/dl/aland685/super-start-for-ag/v1.2.3/..%2Fsecret.txt")
                assert r_trav.status_code in (400, 404)

                r_trav2 = await client.get("/dl/aland685/super-start-for-ag/..%2Fv1/app.zip")
                assert r_trav2.status_code in (400, 404)

                # 不在 Release 资产列表中的文件名 -> 404
                r_missing = await client.get("/dl/aland685/super-start-for-ag/v1.2.3/not-in-release.zip")
                assert r_missing.status_code == 404


def test_one_hour_expiration_and_lru_cleanup(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path, max_cache_mb=1, cache_ttl_seconds=3600)
    storage = RelayStorage(cfg.db_path)

    # 写入两个模拟缓存文件
    f1_rel = "owner/repo/v1/old.bin"
    f2_rel = "owner/repo/v1/new.bin"
    p1 = cfg.cache_dir / f1_rel
    p2 = cfg.cache_dir / f2_rel
    p1.parent.mkdir(parents=True, exist_ok=True)
    p1.write_bytes(b"a" * 600_000)
    p2.write_bytes(b"b" * 600_000)

    storage.upsert_cached_file("owner/repo", "v1", "old.bin", f1_rel, 600_000, "application/octet-stream")
    time.sleep(0.02)
    storage.upsert_cached_file("owner/repo", "v1", "new.bin", f2_rel, 600_000, "application/octet-stream")

    # 1. 容量超过 1MB（1.2MB > 1MB）时，LRU 淘汰较旧的 old.bin
    res_lru = cleanup_once(storage, cfg)
    assert res_lru["lru_removed"] == 1
    assert not p1.exists()
    assert p2.exists()

    # 2. 模拟时间推进 3601 秒（超过 1 小时），new.bin 应被过期清理
    res_exp = cleanup_once(storage, cfg, now=time.time() + 3601)
    assert res_exp["expired_removed"] == 1
    assert not p2.exists()
