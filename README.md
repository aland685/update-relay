# Update Relay · GitHub Release 按需加速与 1 小时自动清理中转站

专为国内亲友访问 GitHub Releases 设计的轻量级按需加速中转站，支持多项目白名单隔离、GitHub API 兼容改写、单飞锁并发去重、边下边存流式透传、HTTP Range 断点续传、1 小时自动清理以及 LRU 容量上限淘汰。

## 核心特性

- **管理员后台 (`/admin`)**：账号密码登录（Session Cookie + Bearer Token）、白名单仓库增删启停、实时查看缓存版本/文件大小/剩余过期时间、手动刷新元数据、一键清理缓存、复制直链。
- **GitHub Releases API 兼容 (`GET /repos/{owner}/{repo}/releases/latest`)**：短时缓存元数据（默认 5 分钟，`?refresh=1` 强制刷新），自动将 `assets[*].browser_download_url` 改写为本中转站地址。
- **按需流式下载 (`GET /dl/{owner}/{repo}/{tag}/{filename}`)**：
  - 严格校验白名单与 Release 资产归属，防止路径穿越与开放代理盗刷；
  - **单飞锁 (Singleflight)**：并发下载同一文件仅回源 GitHub 一次；
  - **边下边存**：流式透传同时写入临时文件，完成后原子落盘；
  - **1 小时自动清理 + LRU 兜底**：后台每 5 分钟扫描清理超期文件，总容量超过 `MAX_CACHE_MB`（默认 2048 MB）时按 LRU 淘汰。

## 快速启动

### 方式一：Docker Compose（直接拉取 GHCR 预构建镜像，推荐）

```bash
cp .env.example .env
# 编辑 .env，务必修改 ADMIN_PASSWORD 与 SESSION_SECRET
docker compose pull && docker compose up -d
```

### 方式二：Debian VPS 一键脚本（systemd）

```bash
sudo bash deploy/install-debian.sh
```

### Nginx Proxy Manager (NPM) 配置

详见 [docs/NPM_CONFIG.md](docs/NPM_CONFIG.md)。

### 客户端接入示例

详见 [docs/CLIENT_INTEGRATION.md](docs/CLIENT_INTEGRATION.md) 与 [examples/updater_relay_patch.py](examples/updater_relay_patch.py)。
