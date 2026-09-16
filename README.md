# Linux 智能运维巡检平台

自研 Agent-Server 架构，每台 Linux 部署轻量 Agent 采集系统资源/服务状态/安全审计/硬件健康及日志数据，上报到中央服务器统一存储、分析与可视化。

## 目录结构

```
linux-inspect-agent/
├── agent/                  # 每台目标机器部署
│   ├── inspect_agent.py    # 采集脚本（巡检 + 日志）
│   └── config.yaml         # 配置
├── server/                 # 中央服务器
│   ├── server.py           # FastAPI 服务 + 巡检看板 + 日志分析
│   ├── config.yaml         # 配置
│   └── static/             # 前端（巡检看板 + 日志分析页面）
├── deploy/                 # 生产部署（systemd/Nginx/一键脚本）
│   ├── DEPLOY.md           # ★ 上线部署手册
│   ├── setup-server.sh     # 服务器端一键初始化
│   ├── setup-agent.sh      # Agent 端一键安装
│   ├── systemd/            # inspect-server.service / inspect-agent.service
│   └── nginx/              # HTTPS 反代 + 限流配置
├── requirements.txt
└── README.md
```

> 🚀 **要部署到云服务器 + 域名？直接看 [deploy/DEPLOY.md](deploy/DEPLOY.md)**

## 快速开始

### 1. 安装依赖（Agent 和 Server 都需要）

```bash
pip install -r requirements.txt
```

> Agent 需要 psutil + pyyaml，Server 需要全部。

### 2. 启动中央服务器

```bash
cd server
python server.py
# 默认监听 0.0.0.0:8000（本机调试建议设 INSPECT_HOST=127.0.0.1；
# 生产由 Nginx 反代对外提供 HTTPS，此时服务器只监听 127.0.0.1）
# 浏览器打开 http://127.0.0.1:8000 查看看板
```

### 3. 在每台 Linux 上跑 Agent

```bash
cd agent
# 改 config.yaml 里的 server.url 指向中央服务器
python inspect_agent.py --once    # 跑一次测试
python inspect_agent.py           # 常驻循环模式
```

### 4. 配置 cron 定时（推荐）

```bash
# 每 5 分钟采集一次
*/5 * * * * cd /opt/inspect-agent/agent && /usr/bin/python3 inspect_agent.py --once >> /var/log/inspect-agent.log 2>&1
```

## 终端查看汇总

```bash
cd server
python server.py --cli
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `INSPECT_SERVER` | `http://127.0.0.1:8000` | Agent 上报地址 |
| `INSPECT_INTERVAL` | `300` | Agent 采集间隔（秒） |
| `INSPECT_HOST` | `0.0.0.0` | 服务器监听地址 |
| `INSPECT_PORT` | `8000` | 服务器监听端口 |
| `INSPECT_DB` | `inspect.db` | SQLite 数据库路径 |
| `INSPECT_TOKEN` | 空 | Agent 上报鉴权 Token（生产必设） |
| `INSPECT_DASHBOARD_USER` | 空 | 看板/API Basic Auth 用户名（生产必设） |
| `INSPECT_DASHBOARD_PASS` | 空 | 看板/API Basic Auth 密码（生产必设） |
| `INSPECT_LOG_LINES` | `50` | Agent 每个日志文件采集行数 |
| `INSPECT_LOG_RETENTION_DAYS` | `7` | 日志保留天数（独立于巡检记录的保留期） |

## 采集内容

| 类别 | 内容 |
|------|------|
| **系统资源** | CPU、内存、磁盘、负载、网卡流量 |
| **服务状态** | systemd 服务（运行/失败/停止）、进程数、监听端口 |
| **安全审计** | 最近登录、sudo 记录、SSH 配置问题、iptables 规则数 |
| **硬件健康** | CPU 温度、SMART 健康状态、RAID 状态 |
| **日志采集** | 系统日志、认证日志、Nginx 访问/错误日志，自动解析级别 |

## API 接口

| 接口 | 说明 |
|------|------|
| `POST /report` | Agent 上报巡检 + 日志数据 |
| `GET /` | 巡检看板（HTML） |
| `GET /logs` | 日志分析看板（HTML，支持搜索/过滤） |
| `GET /api/hosts` | 各主机最新状态 JSON |
| `GET /api/hosts/{hostname}` | 单台主机历史数据 JSON |
| `GET /api/logs` | 日志查询（支持 hostname/level/keyword/log_type/hours/limit） |
| `GET /api/logs/stats` | 日志统计（按级别/类型/主机分组） |
