#!/usr/bin/env python3
"""Linux 巡检 Agent — 每台机器跑一个，采集指标 POST 到中央服务器"""
import json, socket, time, subprocess, os
import urllib.request, urllib.error
from pathlib import Path
import yaml
import psutil

# ── 配置 ──────────────────────────────────────────────
def load_config():
    """优先级：环境变量 > config.yaml > 内置默认值"""
    cfg_path = Path(__file__).with_name("config.yaml")
    cfg = {}
    if cfg_path.exists():
        with cfg_path.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    server = cfg.get("server") or {}
    url = os.environ.get("INSPECT_SERVER") or server.get("url", "http://127.0.0.1:8000")
    hostname = os.environ.get("INSPECT_HOSTNAME") or cfg.get("hostname") or ""
    raw_interval = os.environ.get("INSPECT_INTERVAL") or cfg.get("interval", 300)
    try:
        interval = int(raw_interval)
    except (TypeError, ValueError):
        interval = 300
    return {
        "server_url": str(url).rstrip("/"),
        "interval": interval,
        "hostname": hostname or socket.gethostname(),
        "token": os.environ.get("INSPECT_TOKEN") or server.get("token") or "",
    }

CFG = load_config()
SERVER_URL = CFG["server_url"]
REPORT_URL = f"{SERVER_URL}/report"
HOSTNAME = CFG["hostname"]
INTERVAL = CFG["interval"]
TOKEN = CFG["token"]

# ── 采集函数 ──────────────────────────────────────────

def collect_system():
    """系统资源：CPU / 内存 / 磁盘 / 负载 / 网卡"""
    result = {
        "cpu_percent": 0.0,
        "memory_total_mb": 0.0,
        "memory_used_mb": 0.0,
        "memory_percent": 0.0,
        "disk_total_gb": 0.0,
        "disk_used_gb": 0.0,
        "disk_percent": 0.0,
        "load_1m": 0.0,
        "load_5m": 0.0,
        "load_15m": 0.0,
        "net_sent_mb": 0.0,
        "net_recv_mb": 0.0,
    }

    try:
        result["cpu_percent"] = psutil.cpu_percent(interval=1)
    except Exception:
        pass

    try:
        mem = psutil.virtual_memory()
        result["memory_total_mb"] = round(mem.total / 1024 / 1024, 1)
        result["memory_used_mb"] = round(mem.used / 1024 / 1024, 1)
        result["memory_percent"] = mem.percent
    except Exception:
        pass

    try:
        disk = psutil.disk_usage('/')
        result["disk_total_gb"] = round(disk.total / 1024 / 1024 / 1024, 1)
        result["disk_used_gb"] = round(disk.used / 1024 / 1024 / 1024, 1)
        result["disk_percent"] = disk.percent
    except Exception:
        pass

    try:
        load1, load5, load15 = os.getloadavg()
        result["load_1m"] = load1
        result["load_5m"] = load5
        result["load_15m"] = load15
    except Exception:
        pass

    try:
        net = psutil.net_io_counters()
        result["net_sent_mb"] = round(net.bytes_sent / 1024 / 1024, 1)
        result["net_recv_mb"] = round(net.bytes_recv / 1024 / 1024, 1)
    except Exception:
        pass

    return result

def collect_services():
    """服务状态：systemd 服务 / 进程数 / 端口"""
    services = []
    result = {
        "services_total": 0,
        "services_failed": 0,
        "process_count": 0,
        "listening_ports": [],
    }
    try:
        r = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=running,exited,failed",
             "--no-pager", "--no-legend"],
            capture_output=True, text=True, timeout=10
        )
        for line in r.stdout.strip().split('\n'):
            parts = line.split()
            if len(parts) >= 4:
                services.append({
                    "name": parts[0],
                    "load": parts[1],
                    "active": parts[2],
                    "sub": parts[3],
                    "status": "failed" if parts[2] == "failed" else ("running" if parts[2] == "active" else "stopped")
                })
    except Exception:
        services = []  # 采集失败保持空列表，避免被当成「1 个服务」

    # 监听端口
    result["listening_ports"] = collect_ports()

    result["services_total"] = len(services)
    result["services_failed"] = sum(1 for s in services if s.get("status") == "failed")
    try:
        result["process_count"] = len(psutil.pids())
    except Exception:
        pass
    return result


def collect_ports():
    """采集监听端口。非 root / hidepid 环境下静默返回空列表。"""
    ports = []
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        return ports          # 权限不足，跳过
    for c in conns:
        if c.status != "LISTEN":
            continue
        name = "unknown"
        if c.pid:
            try:
                name = psutil.Process(c.pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass          # 进程刚好退出或看不到，不算错误
        ports.append({
            "port": c.laddr.port,
            "pid": c.pid,
            "process": name,
        })
    return ports[:50]         # 限制数量


def collect_security():
    """安全审计：登录 / sudo / SSH / 防火墙"""
    result = {
        "recent_logins": [],
        "sudo_events": [],
        "ssh_issues": [],
        "iptables_rules": -1,
    }

    # 最近登录
    try:
        r = subprocess.run(["last", "-n", "10"], capture_output=True, text=True, timeout=5)
        out = r.stdout.strip()
        result["recent_logins"] = out.split('\n') if out else []
    except Exception:
        result["recent_logins"] = []

    # sudo 记录
    try:
        r = subprocess.run(["journalctl", "-u", "sudo", "--since", "1 hour ago", "--no-pager", "-q"],
                           capture_output=True, text=True, timeout=5)
        out = r.stdout.strip()
        result["sudo_events"] = out.split('\n')[:20] if out else []
    except Exception:
        result["sudo_events"] = []

    # SSH 配置检查
    ssh_issues = []
    try:
        with open("/etc/ssh/sshd_config") as f:
            for line in f:
                line = line.strip()
                if line.startswith("PermitRootLogin yes"):
                    ssh_issues.append("root 登录未禁止")
                if line.startswith("PasswordAuthentication yes"):
                    ssh_issues.append("密码认证未禁用")
    except Exception:
        pass  # 读不到配置不算安全问题，避免非 root 机器永久带警告
    result["ssh_issues"] = ssh_issues

    # 防火墙
    try:
        r = subprocess.run(["iptables", "-L", "-n"], capture_output=True, text=True, timeout=5)
        result["iptables_rules"] = len([l for l in r.stdout.split('\n') if l and not l.startswith('Chain')])
    except Exception:
        result["iptables_rules"] = -1

    return result

def collect_hardware():
    """硬件健康：温度 / SMART / RAID"""
    result = {
        "temperatures": [],
        "smart_health": "unknown",
        "raid_status": "no RAID",
    }

    # 温度
    try:
        r = subprocess.run(["sensors", "-u"], capture_output=True, text=True, timeout=5)
        temps = []
        for line in r.stdout.split('\n'):
            if "temp" in line.lower() and "_input" in line:
                temps.append(line.strip())
        result["temperatures"] = temps[:10]
    except Exception:
        result["temperatures"] = []

    # SMART（第一块盘）
    try:
        r = subprocess.run(["lsblk", "-d", "-o", "NAME", "--noheadings"],
                           capture_output=True, text=True, timeout=5)
        first_disk = r.stdout.strip().split('\n')[0].strip()
        if first_disk:
            r = subprocess.run(["smartctl", "-H", f"/dev/{first_disk}"],
                               capture_output=True, text=True, timeout=10)
            result["smart_health"] = "PASSED" if "PASSED" in r.stdout else ("FAILED" if "FAILED" in r.stdout else "unknown")
    except Exception:
        result["smart_health"] = "unknown"

    # RAID
    try:
        r = subprocess.run(["cat", "/proc/mdstat"], capture_output=True, text=True, timeout=5)
        result["raid_status"] = r.stdout.strip() if r.stdout.strip() else "no RAID"
    except Exception:
        result["raid_status"] = "no RAID"

    return result

# ── 主逻辑 ────────────────────────────────────────────

def run_once():
    """执行一次采集并上报"""
    payload = {
        "hostname": HOSTNAME,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "system": collect_system(),
        "services": collect_services(),
        "security": collect_security(),
        "hardware": collect_hardware(),
    }
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(REPORT_URL, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[{payload['timestamp']}] 上报成功: HTTP {resp.status}")
            return True
    except urllib.error.HTTPError as e:
        print(f"[ERROR] HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
    except Exception as e:
        print(f"[ERROR] {e}")
    return False

def main():
    import argparse
    p = argparse.ArgumentParser(description="Linux 巡检 Agent")
    p.add_argument("--once", action="store_true", help="只跑一次（适合 cron）")
    p.add_argument("--interval", type=int, default=INTERVAL, help="循环间隔秒")
    args = p.parse_args()

    if args.once:
        run_once()
    else:
        print(f"巡检 Agent 启动，目标服务器: {REPORT_URL}，间隔: {args.interval}s")
        while True:
            try:
                run_once()
            except Exception as e:
                # 单次采集失败不应终止 Agent，打印后继续下一轮
                print(f"[ERROR] 本轮采集异常: {e}")
            time.sleep(args.interval)

if __name__ == "__main__":
    main()
