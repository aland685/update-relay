# Nginx Proxy Manager (NPM) 反向代理配置指南

当 Update Relay 运行在 Debian VPS 宿主机端口 `18080`（无论是 `systemd` 还是 `docker compose` 映射 `18080:18080`），而 NPM 运行在 Docker 容器中时，NPM 可通过 Docker 网桥网关 **`172.17.0.1:18080`** 访问宿主机服务。

## 1. Proxy Host 基础设置（Details）

| 字段 | 填写值 |
| :--- | :--- |
| **Domain Names** | 你的域名（例如 `relay.example.com`，DNS 灰云直连 VPS） |
| **Scheme** | `http` |
| **Forward Hostname / IP** | `172.17.0.1` |
| **Forward Port** | `18080` |
| **Block Common Exploits** | 建议开启 |
| **Websockets Support** | 可选（本服务无需 WebSocket） |

> 若 NPM 无法连通 `172.17.0.1:18080`，请检查 VPS 防火墙（如 `ufw`）是否放行了 `docker0` 网卡到 `18080` 端口的流量：
> ```bash
> sudo ufw allow in on docker0 to any port 18080 proto tcp
> ```

## 2. SSL 证书（SSL）

- 选择申请 Let's Encrypt 证书，并开启 **Force SSL** 与 **HTTP/2 Support**。

## 3. 高级配置（Advanced -> Custom Nginx Configuration）

大文件安装包边下边存流式透传时，必须关闭 Nginx 缓冲并调大超时，否则大文件下载可能卡住或触发 `504 Gateway Time-out`：

```nginx
# 关闭代理缓冲，确保边下边存字节流实时透传给客户端
proxy_buffering off;
proxy_request_buffering off;

# 调大上游读取与发送超时（适配大安装包首次回源拉取）
proxy_connect_timeout 30s;
proxy_read_timeout 300s;
proxy_send_timeout 300s;

# 不限制响应/请求体大小
client_max_body_size 0;

# 传递真实协议与域名，便于中转站自动生成 https:// 外部下载链接
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host $host;
```

## 4. `.env` 配套建议

如果希望绝对稳定，建议在 `.env` 中显式设置：

```env
PUBLIC_BASE_URL=https://relay.example.com
```
