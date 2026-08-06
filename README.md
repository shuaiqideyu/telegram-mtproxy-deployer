# Telegram 代理自动部署

Telegram **MTProxy / SOCKS5** 代理自动部署工具。

用户在 Bot 内提交服务器信息后，系统通过 SSH 完成环境检测、依赖安装、容器/服务部署，并生成可用的代理链接。主协议为 **MTProxy**（Docker），同时支持 **SOCKS5**（`microsocks` + systemd）；另含代理管理、公开代理池与流量采样。

[![Python](https://img.shields.io/badge/Python-3.14-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**作者**：[Telegram @yuuu](https://t.me/yuuu)

---

## 功能特性

| 能力 | 说明 |
| --- | --- |
| 一键部署 | 对话式收集 IP / SSH 账号 / 端口，自动完成远端部署 |
| 双协议 | MTProxy（Docker）与 SOCKS5（systemd + microsocks） |
| 部署前体检 | SSH 连通、系统信息、磁盘、端口占用、线路可用性等检查 |
| 线路策略 | 支持直连与国内镜像源回退，适配不同出网环境 |
| 代理管理 | 我的代理列表、详情、检测、重置 Secret、改端口/备注、删除 |
| 公开代理池 | 可选公开分享；后台定时健康检查，连续失败自动下线 |
| 流量采样 | 定期采集连接数与流量，便于运维观察 |
| 安全存储 | SSH 密码加密入库；会话凭据仅存进程内存并按时过期 |

---

## 架构概览

```text
Telegram User
     │  InlineKeyboard / ReplyKeyboard
     ▼
┌─────────────────────────────┐
│  Bot Handlers               │  交互编排、会话状态
│  bot/handlers/              │
└─────────────┬───────────────┘
              │
     ┌────────┼────────┐
     ▼        ▼        ▼
 Presentation  Memory   Services
 文案/视图     会话缓存   SSH / Deploy / DB / Health
              │
              ▼
        PostgreSQL（业务真值）
              │
              ▼
     目标服务器（Docker MTProxy / SOCKS5）
```

分层约定：

- `handlers`：只做 Telegram 交互编排
- `presentation`：文案与展示结构
- `services`：SSH、部署、数据库、健康检查、流量采样
- `utils`：校验、加密、格式化等无状态工具

会话与短时健康缓存使用进程内 `MemoryStore`，**不依赖 Redis**。进程重启会清空会话，业务数据仍在 PostgreSQL。

---

## 技术栈

- **运行时**：Python 3.14（项目固定版本）
- **Bot 框架**：python-telegram-bot ≥ 22.7
- **远端执行**：asyncssh
- **数据库**：PostgreSQL + asyncpg
- **HTTP**：aiohttp
- **加密**：cryptography（Fernet）
- **事件循环**：uvloop（可用时）

---

## 快速开始

### 1. 环境要求

- Python 3.14
- PostgreSQL 14+（建议）
- 可访问 Telegram Bot API 的出网环境
- Bot 所在机器能 SSH 到用户填写的目标服务器

### 2. 获取代码

```bash
git clone git@github.com:shuaiqideyu/telegram-mtproxy-deployer.git
cd telegram-mtproxy-deployer
```

### 3. 安装依赖

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. 配置环境变量

```bash
cp .env.example .env
```

必填项：

| 变量 | 说明 |
| --- | --- |
| `BOT_TOKEN` | BotFather 签发的 Token |
| `DATABASE_URL` | PostgreSQL 连接串 |
| `ADMIN_USER_ID` | 管理员 Telegram 用户 ID |

完整说明见 [DEPLOY.md](DEPLOY.md) 与 [.env.example](.env.example)。

### 5. 准备数据库

只需先创建空库并授权；程序启动时会自动建表与补齐字段：

- `users`
- `proxies`
- `proxy_traffic_samples`

```bash
psql "$DATABASE_URL" -c "select 1"
```

### 6. 启动

```bash
python3.14 main.py
```

看到日志 `Bot 已就绪，等待消息中...` 即表示 polling 正常。

> 同一 `BOT_TOKEN` 同一时间只能有一个实例，否则会出现 `telegram.error.Conflict`。

---

## 使用流程

1. 私聊 Bot 发送 `/start`
2. 点击 **新建代理**，填写备注并选择协议（MTProxy / SOCKS5）
3. 提交服务器 IP、SSH 用户、密码（密码消息会被立即删除）
4. 查看快速检查结果；存在阻断项时需先处理
5. 确认后自动部署，完成后返回 Telegram 可用链接
6. 在 **我的代理** 中检测、重置、公开、删除

典型部署链路：

```text
环境检测 → 网络线路 → 可选 sysctl → Docker/依赖
→ Telegram middle proxy（MTProxy）→ 镜像/二进制
→ 容器或 systemd 服务 → 生成代理链接
```

---

## 协议说明

### MTProxy

- 远端以 Docker 容器运行，容器名形如 `mtproxy-443`
- 默认端口可通过 `PROXY_PORT` 配置，部署时仍可修改
- `MTP_TAG` 可配置固定广告 TAG；留空则不启用
- 镜像地址由 `MTP_IMAGE` 指定

### SOCKS5

- 远端以 `microsocks` + systemd 运行
- 服务名形如 `microsocks-tg-10808.service`
- 自动生成账号密码，并输出 `tg://socks?...` 链接

### 多端口策略

按 `服务器 IP + 代理端口 + 协议` 识别记录：

- 三者相同：覆盖旧记录
- 同 IP、不同端口：视为独立代理
- 端口被其他进程占用：提示冲突或推荐可用端口

---

## 配置参考

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PROXY_PORT` | `443` | 默认 MTProxy 端口 |
| `TLS_DOMAIN` | `cloudflare.com` | 部署自检 TLS 域名 |
| `TLS_DOMAIN_CANDIDATES` | 逗号分隔列表 | 候选 TLS 域名 |
| `MTP_TAG` | 空 | MTProxy AD_TAG |
| `MTP_IMAGE` | 见 `.env.example` | MTProxy 镜像 |
| `DEPLOY_TIMEOUT_SECONDS` | `300` | 单次部署超时 |
| `COMMAND_TIMEOUT_SECONDS` | `1800` | 单条 SSH 命令超时 |
| `SESSION_TTL_SECONDS` | `600` | 内存会话 TTL |
| `TRAFFIC_SAMPLE_INTERVAL_SECONDS` | `900` | 流量采样间隔 |
| `PASSWORD_ENCRYPTION_KEY` | 空 | 留空则由 `BOT_TOKEN` 派生 |

生产部署、更新与回滚步骤见 [DEPLOY.md](DEPLOY.md)。

---

## 目录结构

```text
.
├── main.py                 # 入口
├── requirements.txt
├── .env.example
├── DEPLOY.md               # 部署与运维手册
├── README.md
├── LICENSE
└── bot/
    ├── main.py             # 应用组装、后台任务
    ├── config.py           # 配置加载
    ├── handlers/           # Telegram 交互
    ├── presentation/       # 展示文案
    ├── services/           # 部署 / DB / SSH / 健康检查
    └── utils/              # 工具函数
```

---

## 安全说明

- 不要将真实 `.env`、Token、数据库密码提交到仓库或发给第三方
- 用户提交的 SSH 密码读取后立即删除原消息，且不写入业务日志
- 入库 SSH 密码使用 Fernet 加密；建议生产环境显式配置 `PASSWORD_ENCRYPTION_KEY`
- Bot 对目标机拥有部署权限，请仅在你拥有或已获授权的服务器上使用
- 建议为 Bot 进程使用最小权限系统账号，并限制其出网与 SSH 目标范围
- 公开代理功能会暴露节点可达性信息，请按合规要求审慎开启

---

## 运维建议

- **进程托管**：systemd / supervisor / 面板进程管理均可；不要用 uwsgi / gunicorn
- **冒烟验证**：

  ```bash
  timeout 15s python3.14 main.py
  ```

- **健康信号**：
  - 进程存活且持续 polling
  - PostgreSQL 可连通
  - 无 Telegram `Conflict`
  - 公开代理检查无持续异常
- **更新**：备份代码与 `.env` → 同步代码 → 重装依赖 → 冒烟 → 再重启常驻进程
- **回滚**：恢复上一版代码与依赖，保留原 `.env`，必要时先备份数据库

---

## 常见问题

**Q: 启动提示 `BOT_TOKEN 未配置`？**  
确认项目根目录存在 `.env`，且变量名拼写正确。

**Q: SSH 认证失败？**  
检查 IP、端口、用户名、密码；确认目标机允许密码登录，且安全组放行 SSH。

**Q: 部署成功但 Telegram 连不上？**  
到「我的代理」执行检测。若外网不可达，通常是云安全组未放行代理端口。

**Q: 为什么公开代理会自动下线？**  
后台健康检查连续失败后会取消公开并通知所有者；修复后可重新开启。

**Q: 为什么重启后正在进行的操作丢失？**  
会话在内存中，重启清空是预期行为；已部署代理记录仍在数据库中。

更多运维问题见 [DEPLOY.md](DEPLOY.md)。

---

## 开发指南

```bash
# 建议使用项目要求的 Python 版本
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 本地运行
cp .env.example .env   # 填入测试 Token 与数据库
python3.14 main.py
```

贡献建议：

1. 保持 handlers / presentation / services 分层，不在 handler 里写 SSH 或 SQL
2. 新增 callback / 状态时同步更新 `bot/handlers/constants.py` 与总路由
3. 所有动态 Telegram 文案使用 HTML，并对用户输入做 escape
4. 提交前确认未包含 `.env`、密钥或真实服务器信息

---

## 免责声明

本项目仅供学习、研究与合法网络调试用途。使用者应遵守所在地法律法规及 Telegram 服务条款，并对自己的部署目标与流量用途负责。作者不对滥用行为或由此产生的任何损失承担责任。

---

## License

[MIT](LICENSE)

**作者**：[Telegram @yuuu](https://t.me/yuuu)

---

## 致谢

- [Telegram Bot API](https://core.telegram.org/bots/api)
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- MTProxy / microsocks 等相关开源组件与镜像维护者
