# 客户端接入指南（适配 `D:\project\chaoxing\app\updater.py`）

## 1. 接入思路

现有 `D:\project\chaoxing\app\updater.py` 存在两个痛点：
1. `check_for_updates()` 直接请求 `https://api.github.com/repos/{repo}/releases/latest`，国内网络容易超时或失败。
2. `_download_with_mirror()` 使用公共镜像站 `https://ghfast.top/`，稳定性不可控。

接入 **Update Relay** 后，可实现：
- **优先直连 GitHub**（`timeout=5` 秒快速探测）；
- **5 秒超时或请求失败时自动切换到你的 VPS 中转站**（例如 `https://relay.example.com`）：
  - 版本检查接口：`https://relay.example.com/repos/{owner}/{repo}/releases/latest`（返回与 GitHub 完全兼容的 JSON，且 `assets[*].browser_download_url` 已自动改写为中转站直链）；
  - 文件下载：`browser_download_url` 若已是中转站链接则直接流式下载；若直连阶段拿到了 GitHub 原生链接但下载失败，也可自动回退到 `{relay_base_url}/dl/{owner}/{repo}/{tag}/{filename}`。

## 2. 配置项扩展

在 `DEFAULT_UPDATER_CONFIG` 中增加 `relay_base_url`：

```python
DEFAULT_UPDATER_CONFIG: dict[str, str] = {
    "github_repo": "aland685/super-start-for-ag",
    "relay_base_url": "https://relay.example.com",
    "mirror_prefix": "https://ghfast.top/",
    "app_asset_name": "chaoxing-app-update.zip",
    "installer_prefix": "Chaoxing-Setup-v",
}
```

## 3. 完整接入示例

可直接参考 [updater_relay_patch.py](../examples/updater_relay_patch.py) 中的 `check_for_updates_with_relay` 与 `_download_with_fallback` 实现，将其替换/合并到 `D:\project\chaoxing\app\updater.py` 即可。
