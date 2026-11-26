# -*- coding: utf-8 -*-
"""GitHub Release 元数据拉取、URL 改写与流式下载（支持 Range 与边下边存）。"""

from __future__ import annotations

import copy
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from .cache_manager import (
    SingleflightRegistry,
    build_rel_path,
    is_cache_expired,
    remove_file_and_prune,
    resolve_cache_path,
)
from .config import Settings
from .storage import RelayStorage

logger = logging.getLogger("update_relay.github")

CHUNK_SIZE = 65536


def _github_headers(settings: Settings) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "update-relay/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


def rewrite_release_urls(
    payload: dict[str, Any], owner: str, repo: str, external_base_url: str
) -> dict[str, Any]:
    """将 Release JSON 中的 assets[*].browser_download_url 改写为中转站地址。"""
    rewritten = copy.deepcopy(payload)
    tag_name = str(rewritten.get("tag_name") or rewritten.get("name") or "").strip()
    assets = rewritten.get("assets")
    if isinstance(assets, list) and tag_name:
        encoded_tag = quote(tag_name, safe="")
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "").strip()
            if not name:
                continue
            encoded_name = quote(name, safe="")
            asset["browser_download_url"] = (
                f"{external_base_url}/dl/{owner}/{repo}/{encoded_tag}/{encoded_name}"
            )
    return rewritten


async def fetch_latest_release(
    client: httpx.AsyncClient,
    storage: RelayStorage,
    settings: Settings,
    owner: str,
    repo: str,
    force_refresh: bool = False,
) -> tuple[dict[str, Any], bool]:
    """获取最新 Release 元数据，返回 (payload, from_cache)。"""
    full_repo = f"{owner}/{repo}"
    now = time.time()

    if not force_refresh:
        cached = storage.get_release_cache(full_repo, "__latest__")
        if cached is not None:
            payload, fetched_at = cached
            if (now - fetched_at) < settings.metadata_ttl_seconds:
                return payload, True

    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        resp = await client.get(api_url, headers=_github_headers(settings), timeout=20.0)
    except httpx.HTTPError as exc:
        logger.warning("请求 GitHub API 失败 %s: %s", api_url, exc)
        raise HTTPException(status_code=502, detail=f"请求 GitHub 上游失败: {exc}") from exc

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="仓库尚未发布任何 Release")
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        raise HTTPException(status_code=502, detail="GitHub API 速率限制已耗尽，建议配置 GITHUB_TOKEN")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"GitHub API 返回异常状态码: {resp.status_code}")

    payload = resp.json()
    storage.set_release_cache(full_repo, "__latest__", payload)
    tag_name = str(payload.get("tag_name") or "").strip()
    if tag_name:
        storage.set_release_cache(full_repo, tag_name, payload)
    return payload, False


async def resolve_asset_upstream_url(
    client: httpx.AsyncClient,
    storage: RelayStorage,
    settings: Settings,
    owner: str,
    repo: str,
    tag: str,
    filename: str,
) -> tuple[str, str]:
    """
    校验 (tag, filename) 是否属于该仓库的合法 Release 资产，
    返回 (upstream_browser_download_url, content_type)。
    """
    full_repo = f"{owner}/{repo}"

    def _find_in_payload(payload: dict[str, Any]) -> tuple[str, str] | None:
        if str(payload.get("tag_name") or "").strip() != tag:
            return None
        for asset in payload.get("assets") or []:
            if isinstance(asset, dict) and str(asset.get("name") or "") == filename:
                dl = str(asset.get("browser_download_url") or "")
                ctype = str(asset.get("content_type") or "application/octet-stream")
                if dl:
                    return dl, ctype
        return None

    # 1. 先查本地缓存的 tag 元数据与 __latest__ 元数据
    for cache_key in (tag, "__latest__"):
        cached = storage.get_release_cache(full_repo, cache_key)
        if cached is not None:
            found = _find_in_payload(cached[0])
            if found is not None:
                return found

    # 2. 缓存未命中该 tag/filename，向 GitHub API 查询指定 tag
    encoded_tag = quote(tag, safe="")
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{encoded_tag}"
    try:
        resp = await client.get(api_url, headers=_github_headers(settings), timeout=20.0)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"校验 Release 资产失败: {exc}") from exc

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="指定的 Release Tag 不存在")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"GitHub API 返回异常状态码: {resp.status_code}")

    payload = resp.json()
    storage.set_release_cache(full_repo, tag, payload)
    found = _find_in_payload(payload)
    if found is None:
        raise HTTPException(status_code=404, detail="指定的 Release 中不存在该资产文件")
    return found


def _parse_range_header(range_header: str, file_size: int) -> tuple[int, int] | None:
    """解析单段 HTTP Range 头（bytes=start-end）。不合法或超出范围返回 None。"""
    if not range_header or not range_header.startswith("bytes="):
        return None
    spec = range_header[6:].strip()
    if "," in spec or "-" not in spec:
        return None
    start_s, end_s = spec.split("-", 1)
    try:
        if start_s == "":
            # bytes=-500 (最后 500 字节)
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start = max(0, file_size - suffix)
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= file_size:
        return None
    end = min(end, file_size - 1)
    return start, end


async def _iter_file_range(path: Path, start: int, end: int) -> AsyncIterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as fh:
        fh.seek(start)
        while remaining > 0:
            chunk = fh.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def build_cached_file_response(
    request: Request,
    file_path: Path,
    filename: str,
    content_type: str,
    head_only: bool = False,
) -> StreamingResponse:
    """从本地缓存文件构造支持 Range 的流式响应。"""
    file_size = file_path.stat().st_size
    encoded_fn = quote(filename, safe="")
    common_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_fn}",
        "X-Relay-Cache": "HIT",
    }

    range_header = request.headers.get("range", "")
    if range_header:
        parsed = _parse_range_header(range_header, file_size)
        if parsed is None:
            raise HTTPException(
                status_code=416,
                detail="Requested Range Not Satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        start, end = parsed
        length = end - start + 1
        headers = {
            **common_headers,
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
        }
        if head_only:
            return StreamingResponse(iter(()), status_code=206, media_type=content_type, headers=headers)
        return StreamingResponse(
            _iter_file_range(file_path, start, end),
            status_code=206,
            media_type=content_type,
            headers=headers,
        )

    headers = {**common_headers, "Content-Length": str(file_size)}
    if head_only:
        return StreamingResponse(iter(()), status_code=200, media_type=content_type, headers=headers)
    return StreamingResponse(
        _iter_file_range(file_path, 0, file_size - 1) if file_size > 0 else iter(()),
        status_code=200,
        media_type=content_type,
        headers=headers,
    )


async def stream_and_cache_asset(
    request: Request,
    client: httpx.AsyncClient,
    storage: RelayStorage,
    settings: Settings,
    singleflight: SingleflightRegistry,
    owner: str,
    repo: str,
    tag: str,
    filename: str,
    head_only: bool = False,
) -> StreamingResponse:
    """
    核心按需流式下载逻辑：
    1. 命中有效本地缓存 -> 直接流式/Range 下发并刷新访问时间。
    2. 未命中 -> 获取单飞锁 -> 再次检查缓存 -> 边下边存流式透传，完成后原子落盘。
    """
    full_repo = f"{owner}/{repo}"
    rel_path = build_rel_path(owner, repo, tag, filename)
    final_path = resolve_cache_path(settings.cache_dir, rel_path)

    # 快速路径：缓存命中且未过期
    meta = storage.get_cached_file(full_repo, tag, filename)
    if meta is not None and final_path.is_file() and not is_cache_expired(meta, settings):
        storage.touch_cached_file(full_repo, tag, filename)
        return build_cached_file_response(
            request, final_path, filename, meta.get("content_type") or "application/octet-stream", head_only=head_only
        )

    # 获取单飞锁（同一文件并发回源时只发起 1 次上游拉取）
    lock = await singleflight.get_lock(full_repo, tag, filename)
    await lock.acquire()

    try:
        # 持锁后二次检查：可能前一个并发请求已经下载完毕
        meta = storage.get_cached_file(full_repo, tag, filename)
        if meta is not None and final_path.is_file():
            if not is_cache_expired(meta, settings):
                storage.touch_cached_file(full_repo, tag, filename)
                resp = build_cached_file_response(
                    request,
                    final_path,
                    filename,
                    meta.get("content_type") or "application/octet-stream",
                    head_only=head_only,
                )
                lock.release()
                return resp
            else:
                storage.delete_cached_file(full_repo, tag, filename)
                remove_file_and_prune(settings.cache_dir, rel_path)

        # 校验资产合法性并获取上游下载地址
        upstream_url, fallback_ctype = await resolve_asset_upstream_url(
            client, storage, settings, owner, repo, tag, filename
        )

        # 发起上游流式请求（在生成器开始前先拿到响应头）
        upstream_cm = client.stream(
            "GET",
            upstream_url,
            follow_redirects=True,
            headers={"User-Agent": "update-relay/1.0"},
            timeout=httpx.Timeout(settings.upstream_timeout_seconds, connect=20.0),
        )
        upstream_resp = await upstream_cm.__aenter__()
    except Exception:
        lock.release()
        raise

    # 检查上游状态码
    if upstream_resp.status_code >= 400:
        status = upstream_resp.status_code
        await upstream_cm.__aexit__(None, None, None)
        lock.release()
        raise HTTPException(status_code=502, detail=f"上游文件拉取失败 (HTTP {status})")

    content_type = upstream_resp.headers.get("content-type") or fallback_ctype or "application/octet-stream"
    content_length = upstream_resp.headers.get("content-length")
    encoded_fn = quote(filename, safe="")
    resp_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_fn}",
        "X-Relay-Cache": "MISS",
    }
    if content_length:
        resp_headers["Content-Length"] = content_length

    if head_only:
        await upstream_cm.__aexit__(None, None, None)
        lock.release()
        return StreamingResponse(iter(()), status_code=200, media_type=content_type, headers=resp_headers)

    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = final_path.with_name(f"{final_path.name}.tmp.{uuid.uuid4().hex[:10]}")

    async def tee_stream() -> AsyncIterator[bytes]:
        total_written = 0
        completed = False
        try:
            with tmp_path.open("wb") as fh:
                async for chunk in upstream_resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    total_written += len(chunk)
                    yield chunk
            os.replace(tmp_path, final_path)
            storage.upsert_cached_file(
                repo=full_repo,
                tag=tag,
                filename=filename,
                rel_path=rel_path,
                size=total_written,
                content_type=content_type,
            )
            completed = True
        finally:
            if not completed and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            try:
                await upstream_cm.__aexit__(None, None, None)
            finally:
                lock.release()

    return StreamingResponse(tee_stream(), status_code=200, media_type=content_type, headers=resp_headers)
