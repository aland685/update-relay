# -*- coding: utf-8 -*-
"""
D:\project\chaoxing\app\updater.py 接入 Update Relay 的参考实现：
1. 检查更新：优先直连 api.github.com（5 秒超时），失败自动切换 VPS 中转站。
2. 下载更新：优先直连（5 秒连接超时），失败自动切换 VPS 中转站 /dl/{owner}/{repo}/{tag}/{filename}。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


def fetch_latest_release_with_relay(
    repo: str,
    relay_base_url: str = "",
    direct_timeout: float = 5.0,
    relay_timeout: float = 12.0,
) -> tuple[dict[str, Any], str]:
    """
    返回 (release_json, source)，其中 source 为 'github' 或 'relay'。
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "chaoxing-updater/1.0",
    }
    candidates: list[tuple[str, str, float]] = [
        ("github", f"https://api.github.com/repos/{repo}/releases/latest", direct_timeout),
    ]
    relay = relay_base_url.strip().rstrip("/")
    if relay:
        candidates.append(
            ("relay", f"{relay}/repos/{repo}/releases/latest", relay_timeout)
        )

    last_exc: Exception | None = None
    for source, url, timeout in candidates:
        try:
            resp = requests.get(url, timeout=timeout, headers=headers)
            if resp.status_code == 404:
                return {}, source
            resp.raise_for_status()
            return resp.json(), source
        except Exception as exc:
            last_exc = exc

    assert last_exc is not None
    raise last_exc


def download_asset_with_relay(
    url: str,
    dest: Path,
    *,
    repo: str = "",
    tag: str = "",
    filename: str = "",
    relay_base_url: str = "",
    mirror_prefix: str = "",
    direct_timeout: float = 5.0,
    relay_timeout: float = 90.0,
) -> Path:
    """
    优先直连下载（5 秒超时），失败后自动切换到 VPS 中转站（或备用镜像前缀）。
    """
    candidates: list[tuple[str, float]] = [(url, direct_timeout)]

    relay = relay_base_url.strip().rstrip("/")
    if relay and repo and tag and filename and not url.startswith(relay):
        relay_dl = f"{relay}/dl/{repo}/{quote(tag, safe='')}/{quote(filename, safe='')}"
        candidates.append((relay_dl, relay_timeout))

    prefix = mirror_prefix.strip()
    if prefix and url.startswith("https://github.com/"):
        if not prefix.endswith("/"):
            prefix += "/"
        candidates.append((prefix + url, relay_timeout))

    last_exc: Exception | None = None
    for candidate_url, timeout in candidates:
        try:
            with requests.get(
                candidate_url,
                stream=True,
                timeout=timeout,
                headers={"User-Agent": "chaoxing-updater/1.0"},
            ) as resp:
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            fh.write(chunk)
            return dest
        except Exception as exc:
            last_exc = exc

    assert last_exc is not None
    raise last_exc
