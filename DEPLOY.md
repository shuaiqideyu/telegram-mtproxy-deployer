# 部署说明

本文档用于部署 **Telegram Proxy Auto Deployer**（本 Bot 服务本身）。机器人通过 Telegram long polling 工作，不需要 Nginx、Gunicorn 或开放 HTTP 端口。

## 运行要求

- Python 3.14
- PostgreSQL
- 可访问 Telegram API 的服务器网络
- 服务器可主动 SSH 到用户填写的代理节点

会话状态和公开代理的短时健康缓存都保存在 bot 进程内存里（`bot/services/memory_store.py`），不再依赖 Redis。bot 进程重启会清空所有会话和延迟缓存，用户在 bot 里的中间操作流程需要重新开始，业务数据不受影响。

项目依赖见 `requirements.txt`。建议在项目根目录执行所有命令。

## 环境变量

复制 `.env.example` 为 `.env`，并按实际环境填写：

```bash
cp .env.example .env
```

必填项：

- `BOT_TOKEN`：BotFather 生成的 Telegram bot token。
- `DATABASE_URL`：PostgreSQL 连接地址，用于保存用户、代理、流量采样等业务数据。
- `ADMIN_USER_ID`：管理员 Telegram 用户 ID。

常用可选项：

- `PROXY_PORT`：MTProxy 默认代理端口，默认 `443`。
- `TLS_DOMAIN`：部署自检使用的 TLS 域名，默认 `cloudflare.com`。
- `TLS_DOMAIN_CANDIDATES`：候选 TLS 域名，多个值用英文逗号分隔。
- `MTP_TAG`：固定 MTProxy 广告 TAG。留空时不启用。
- `MTP_IMAGE`：MTProxy 镜像地址。
- `DEPLOY_TIMEOUT_SECONDS`：单次部署流程超时时间。
- `COMMAND_TIMEOUT_SECONDS`：远端 SSH 命令超时时间。
- `SESSION_TTL_SECONDS`：内存会话过期时间，单位秒（每次写入自动续期）。
- `TRAFFIC_SAMPLE_INTERVAL_SECONDS`：流量采样间隔。
- `PASSWORD_ENCRYPTION_KEY`：可选 Fernet key。留空时从 `BOT_TOKEN` 派生。

不要把真实 `.env`、数据库密码或 bot token 提交到仓库，也不要放进对外压缩包。

## 数据库

PostgreSQL 只需要先创建好数据库并授权给 `DATABASE_URL` 中的用户。机器人启动时会自动检查并创建业务表，已有表会按需补充缺失字段和索引。

自动维护的表包括：

- `users`
- `proxies`
- `proxy_traffic_samples`

部署前建议在服务器上确认连接可用：

```bash
psql "$DATABASE_URL" -c "select 1"
```

## 内存缓存（替代 Redis）

- 用户会话：按 `user_id` 存在 `MemoryStore` 内，`session_ttl_seconds` 内无写入自动丢弃。
- 公开代理延迟与连续失败计数：按 `proxy_id` 存在 `MemoryStore` 内，随进程结束清空。
- 进程重启会让所有用户的当前操作流程重新开始，属预期行为。

## 同服务器多端口

同一个用户可以在同一台服务器上部署多个不同端口的代理。系统按 `服务器 IP + 代理端口 + 协议` 判断是否覆盖旧记录：

- 同 IP、同端口、同协议：继续部署会覆盖原记录。
- 同 IP、不同端口：创建新的代理记录，远端进程也会按端口隔离。
- 同端口被其他非本项目进程占用：部署前会提示端口冲突或自动推荐其他端口。
- 同端口已由本项目代理运行：快速检查会提示“该端口已有正在运行的代理，本次继续执行会进行覆盖”。

远端命名规则：

- MTProxy 容器名：`mtproxy-端口号`，例如 `mtproxy-443`。
- SOCKS5 systemd 服务名：`microsocks-tg-端口号.service`，例如 `microsocks-tg-10808.service`。

## MTProxy TAG

MTProxy 使用 `.env` 中的 `MTP_TAG` 作为固定广告 TAG。留空时不启用 AD_TAG。

机器人新建代理流程不会让用户单独填写 TAG；重置代理也会使用当前 `.env` 中的固定配置。

## 安装依赖

使用 Python 3.14 安装依赖：

```bash
python3.14 -m pip install -r requirements.txt
```

如使用虚拟环境，先创建并激活：

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 启动

在项目根目录执行：

```bash
python3.14 main.py
```

生产环境可交给宝塔 Python 项目管理器、supervisor 或其他进程管理工具托管。启动方式选择命令行启动即可，不要使用 uwsgi 或 gunicorn。

注意：同一个 `BOT_TOKEN` 同一时间只能运行一个 bot 实例，否则 Telegram 会返回 `telegram.error.Conflict`。

## 健康检查

本项目没有 HTTP 健康检查端点，可通过以下方式确认运行状态：

- 进程保持运行，日志出现 `Bot 已就绪，等待消息中...`。
- PostgreSQL `select 1` 正常。
- Telegram 没有 `Conflict` 报错。
- 公开代理相关日志中没有持续的 `public-health-checker` 异常。

前台冒烟验证可用：

```bash
timeout 15s python3.14 main.py
```

如果 15 秒内完成初始化并开始 polling，说明基础配置可用。验证结束后再交给进程管理器常驻运行。

## 更新流程

1. 备份当前项目目录和生产 `.env`。
2. 同步新代码到服务器，排除 `.git`、`.venv`、缓存和本地 `.env`。
3. 使用 Python 3.14 重新安装依赖：

```bash
python3.14 -m pip install -r requirements.txt
```

1. 执行前台冒烟验证：

```bash
timeout 15s python3.14 main.py
```

1. 确认无配置错误后，通过生产进程管理器重启常驻进程。

## 回滚

1. 停止当前版本的常驻进程。
2. 恢复上一版代码和对应依赖。
3. 保留原生产 `.env`，除非本次故障明确由环境变量导致。
4. 执行冒烟验证。
5. 通过进程管理器重新启动。

如果版本涉及数据库结构变更，先备份数据库，再启动新版本让程序自动补齐表结构。没有明确回滚脚本时，不要直接改生产库结构。

## 常见问题

### 启动时报 `BOT_TOKEN 未配置`

检查 `.env` 是否存在，`BOT_TOKEN` 是否填写，启动命令是否在项目根目录执行。

### PostgreSQL 连接失败

检查 `DATABASE_URL`、数据库用户权限、防火墙和数据库监听地址。

### Telegram 报 `Conflict`

说明同一个 bot token 已经在其他进程或服务器上运行。保留一个实例即可。

### 部署代理时 SSH 失败

确认用户填写的服务器 IP、SSH 端口、用户名和密码正确；同时确认目标服务器允许密码登录，并且网络没有拦截 SSH。