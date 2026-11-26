# -*- coding: utf-8 -*-
"""缓存文件生命周期管理：过期检测、单飞锁、定时清理与 LRU 容量上限淘汰。"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .storage import RelayStorage

logger = logging.getLogger("update_relay.cache")


class SingleflightRegistry:
    """按 (repo, tag, filename) 维护异步单飞锁，避免并发重复回源。"""

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get_lock(self, repo: str, tag: str, filename: str) -> asyncio.Lock:
        key = (repo.lower(), tag, filename)
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock


def build_rel_path(owner: str, repo: str, tag: str, filename: str) -> str:
    return f"{owner.lower()}/{repo.lower()}/{tag}/{filename}"


def resolve_cache_path(cache_dir: Path, rel_path: str) -> Path:
    """将相对路径解析为缓存目录内的绝对路径，严格防止路径穿越。"""
    candidate = (cache_dir / rel_path).resolve()
    root = cache_dir.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"非法缓存路径: {rel_path}")
    return candidate


def reference_timestamp(meta: dict[str, Any], settings: Settings) -> float:
    if settings.cache_ttl_mode == "fixed":
        return float(meta["created_at"])
    return float(meta["last_accessed_at"])


def remaining_ttl_seconds(meta: dict[str, Any], settings: Settings, now: float | None = None) -> int:
    current = now if now is not None else time.time()
    ref = reference_timestamp(meta, settings)
    rem = int(round((ref + settings.cache_ttl_seconds) - current))
    return max(0, rem)


def is_cache_expired(meta: dict[str, Any], settings: Settings, now: float | None = None) -> bool:
    current = now if now is not None else time.time()
    ref = reference_timestamp(meta, settings)
    return (current - ref) >= settings.cache_ttl_seconds


def remove_file_and_prune(cache_dir: Path, rel_path: str) -> None:
    try:
        target = resolve_cache_path(cache_dir, rel_path)
    except ValueError:
        return
    if target.is_file():
        try:
            target.unlink()
        except OSError as exc:
            logger.warning("删除缓存文件失败 %s: %s", target, exc)
    # 向上清理空目录（最多到 cache_dir 之下）
    root = cache_dir.resolve()
    parent = target.parent
    while parent != root and parent.is_relative_to(root):
        try:
            parent.rmdir()
            parent = parent.parent
        except OSError:
            break


def delete_single_cached_file(
    storage: RelayStorage, settings: Settings, repo: str, tag: str, filename: str
) -> bool:
    removed = storage.delete_cached_file(repo, tag, filename)
    if not removed:
        return False
    remove_file_and_prune(settings.cache_dir, removed["rel_path"])
    return True


def purge_repo_cache(storage: RelayStorage, settings: Settings, repo: str) -> int:
    files = storage.delete_repo_cached_files(repo)
    for item in files:
        remove_file_and_prune(settings.cache_dir, item["rel_path"])
    storage.delete_release_cache(repo)
    return len(files)


def purge_all_cache(storage: RelayStorage, settings: Settings) -> int:
    files = storage.list_cached_files()
    count = 0
    for item in files:
        if delete_single_cached_file(storage, settings, item["repo"], item["tag"], item["filename"]):
            count += 1
    for repo_row in storage.list_repos():
        storage.delete_release_cache(repo_row["repo"])
    return count


def cleanup_once(storage: RelayStorage, settings: Settings, now: float | None = None) -> dict[str, int]:
    """执行一次过期清理与 LRU 容量淘汰。"""
    current = now if now is not None else time.time()
    expired_removed = 0
    lru_removed = 0
    tmp_removed = 0

    all_files = storage.list_cached_files()  # 已按 last_accessed_at ASC 排序
    surviving: list[dict[str, Any]] = []

    for item in all_files:
        try:
            disk_path = resolve_cache_path(settings.cache_dir, item["rel_path"])
        except ValueError:
            storage.delete_cached_file(item["repo"], item["tag"], item["filename"])
            expired_removed += 1
            continue

        if not disk_path.is_file():
            storage.delete_cached_file(item["repo"], item["tag"], item["filename"])
            expired_removed += 1
            continue

        if is_cache_expired(item, settings, now=current):
            storage.delete_cached_file(item["repo"], item["tag"], item["filename"])
            remove_file_and_prune(settings.cache_dir, item["rel_path"])
            expired_removed += 1
        else:
            surviving.append(item)

    # 清理超时的遗留临时文件（.tmp.*）
    for tmp_file in settings.cache_dir.rglob("*.tmp.*"):
        try:
            if tmp_file.is_file() and (current - tmp_file.stat().st_mtime) >= settings.cache_ttl_seconds:
                tmp_file.unlink()
                tmp_removed += 1
        except OSError:
            pass

    # 总容量上限兜底（LRU 淘汰）
    total_bytes = sum(int(item["size"]) for item in surviving)
    max_bytes = settings.max_cache_bytes
    for item in surviving:
        if total_bytes <= max_bytes:
            break
        storage.delete_cached_file(item["repo"], item["tag"], item["filename"])
        remove_file_and_prune(settings.cache_dir, item["rel_path"])
        total_bytes -= int(item["size"])
        lru_removed += 1

    if expired_removed or lru_removed or tmp_removed:
        logger.info(
            "缓存清理完成: 过期=%d, LRU淘汰=%d, 临时文件=%d, 剩余占用=%.2f MB",
            expired_removed,
            lru_removed,
            tmp_removed,
            total_bytes / (1024 * 1024),
        )
    return {
        "expired_removed": expired_removed,
        "lru_removed": lru_removed,
        "tmp_removed": tmp_removed,
        "remaining_bytes": total_bytes,
    }


def get_cache_stats(storage: RelayStorage, settings: Settings) -> dict[str, Any]:
    files = storage.list_cached_files()
    total_bytes = sum(int(f["size"]) for f in files)
    return {
        "file_count": len(files),
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "max_cache_mb": settings.max_cache_mb,
        "ttl_seconds": settings.cache_ttl_seconds,
        "ttl_mode": settings.cache_ttl_mode,
        "cleanup_interval_seconds": settings.cleanup_interval_seconds,
    }
