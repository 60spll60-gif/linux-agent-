# 上线部署手册

把巡检系统部署到你的云服务器 + 域名，全程约 20 分钟。

架构：

```
各台被巡检 Linux (inspect-agent.service)
        │  HTTPS + Bearer Token
        ▼
https://inspect.你的域名.com
        │
   Nginx (80/443, Let's Encrypt 证书, /report 限流)
        │  反代
        ▼
   server.py (只监听 127.0.0.1:8000, systemd 守护)
        │
   SQLite (/var/lib/inspect-server/inspect.db)
```

> 下文以 Ubuntu 22.04/24.04 为例，域名以 `inspect.example.com` 为占位，请替换成你自己的。
> CentOS/Alibaba Cloud Linux 把 `apt` 换成 `dnf` 即可。

---

## 第 0 步：域名解析

在你的域名服务商控制台添加 A 记录：

| 主机记录 | 类型 | 值 |
|---|---|---|
| `inspect`（或你想要的子域） | A | 你的云服务器公网 IP |

解析生效验证（本机执行）：

```bash
ping inspect.example.com   # 能解析到你的服务器 IP 即可
```

## 第 1 步：上传项目到服务器

在你本地电脑执行（Windows 可用 scp / WinSCP / 宝塔面板上传）：

```bash
scp -r linux-inspect-agent root@你的服务器IP:/opt/
```

> ⚠️ 如果脚本在 Windows 上编辑过，先转换换行符，否则 bash 会报错：
> ```bash
> sed -i 's/\r$//' /opt/linux-inspect-agent/deploy/*.sh
> ```

登录服务器，放行防火墙端口（只开 22/80/443，**不要开 8000**）：

```bash
# 云厂商安全组：控制台里放行 22/80/443
# 系统防火墙：
ufw allow 22,80,443/tcp && ufw enable     # Ubuntu
```

## 第 2 步：一键初始化服务器端

```bash
cd /opt/linux-inspect-agent
bash deploy/setup-server.sh
```

脚本会自动完成：

1. 创建低权限运行用户 `inspect`
2. 建 venv、安装依赖
3. **生成随机上报 Token 和看板登录密码**，写入 `/etc/inspect-server/inspect.env`（chmod 600）
4. 安装并启动 `inspect-server.service`（开机自启、崩溃自动拉起）

执行完屏幕上会打印两组凭据，**务必保存**：

- `Agent 上报 Token` → 第 5 步装 Agent 时要用
- `看板登录账号/密码` → 浏览器打开看板时用

之后随时可查：`cat /etc/inspect-server/inspect.env`

验证服务在本机正常：

```bash
curl -i http://127.0.0.1:8000/
# 返回 401 + WWW-Authenticate: Basic → 鉴权生效，正常
```

## 第 3 步：安装 Nginx

```bash
apt install -y nginx certbot python3-certbot-nginx
```

## 第 4 步：配置 HTTPS 反代

```bash
# 1. 复制配置并替换占位域名
cp deploy/nginx/inspect.conf       /etc/nginx/conf.d/
cp deploy/nginx/inspect_ratelimit.conf /etc/nginx/conf.d/
sed -i 's/inspect.example.com/你的域名/g' /etc/nginx/conf.d/inspect.conf

# 2. 先用 HTTP 跑 certbot 签证书（inspect.conf 的 443 段此时会因缺证书报错，
#    所以第一次先临时注释掉 443 那个 server 块，或者用下面的 webroot 方式）
mkdir -p /var/www/certbot
certbot certonly --webroot -w /var/www/certbot -d 你的域名 --agree-tos -m 你的邮箱

# 3. 证书签发成功后，恢复完整配置并测试加载
nginx -t && systemctl reload nginx
```

> 也可以更省事：先只保留 80 端口的 server 块启动，然后执行
> `certbot --nginx -d 你的域名`，让 certbot 自动改写配置并配置续期。

验证证书自动续期（Let's Encrypt 证书 90 天有效，certbot 自带定时任务）：

```bash
certbot renew --dry-run
```

## 第 5 步：在每台被巡检机器上安装 Agent

把 `deploy/` 和 `agent/` 目录拷到目标机器，然后：

```bash
bash setup-agent.sh https://你的域名 <第2步生成的Token>
# 可选第三个参数：采集间隔秒数，默认 300
```

脚本会安装 psutil、注册 `inspect-agent.service`（开机自启 + 崩溃自动重启），并立即跑一次上报测试，看到 `上报测试通过 ✔` 即成功。

手动验证：

```bash
journalctl -u inspect-agent -n 20      # 看到 "上报成功: HTTP 200"
```

## 第 6 步：访问看板

浏览器打开 `https://你的域名`，输入第 2 步生成的看板账号密码（默认用户 `admin`），即可看到所有主机的巡检卡片。

API 用法（同样需要 Basic Auth）：

```bash
curl -u admin:看板密码 https://你的域名/api/hosts
```

---

## 日常运维

| 操作 | 命令 |
|---|---|
| 看服务状态 | `systemctl status inspect-server` |
| 看服务日志 | `journalctl -u inspect-server -f` |
| 重启服务 | `systemctl restart inspect-server` |
| 改 Token/看板密码 | 编辑 `/etc/inspect-server/inspect.env` 后 `systemctl restart inspect-server`（Agent 端同步改 `/etc/inspect-agent/agent.env`） |
| 备份数据库 | `cp /var/lib/inspect-server/inspect.db ~/backup-$(date +%F).db`（建议加 crontab 每日备份） |
| 终端速览 | `cd /opt/linux-inspect-agent/server && /opt/linux-inspect-agent/venv/bin/python server.py --cli` |

## 升级代码

```bash
# 上传新代码覆盖 /opt/linux-inspect-agent 后：
systemctl restart inspect-server
# 密钥在 /etc/inspect-server/inspect.env，不受代码更新影响
```

## 安全清单（上线前过一遍）

- [ ] `/etc/inspect-server/inspect.env` 里 Token 是随机生成的，不是 `my-secret-token`
- [ ] 看板账号密码已设置（启动日志里没有"未设置看板账号"告警）
- [ ] 云安全组 + 系统防火墙只开 22/80/443，8000 不对公网开放
- [ ] `server.py` 监听 `127.0.0.1`（默认已是），公网流量必须经过 Nginx HTTPS
- [ ] SSH 建议改密钥登录、禁密码、禁 root 密码登录
- [ ] `certbot renew --dry-run` 通过
