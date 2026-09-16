#!/usr/bin/env python3
"""Linux 巡检中央服务器 — 接收 Agent 上报，存储，HTML 看板，终端表格"""
import json, os, secrets, sqlite3, threading
import unicodedata
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

# ── 配置 ──────────────────────────────────────────────
from pathlib import Path
import yaml

def load_config():
    """优先级：环境变量 > config.yaml > 内置默认值"""
    cfg_path = Path(__file__).with_name("config.yaml")
    cfg = {}
    if cfg_path.exists():
        with cfg_path.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    server = cfg.get("server") or {}
    return {
        "host": os.environ.get("INSPECT_HOST") or server.get("host", "0.0.0.0"),
        "port": int(os.environ.get("INSPECT_PORT") or server.get("port", 8000)),
        "db_path": os.environ.get("INSPECT_DB") or cfg.get("db_path", "inspect.db"),
        "retention_days": int(cfg.get("data_retention_days", 30)),
        "token": os.environ.get("INSPECT_TOKEN") or server.get("token") or "",
        # 看板 HTTP Basic 账号。留空＝不启用鉴权（只适合本机调试）
        "dashboard_user": os.environ.get("INSPECT_DASHBOARD_USER") or server.get("dashboard_user") or "",
        "dashboard_pass": os.environ.get("INSPECT_DASHBOARD_PASS") or server.get("dashboard_pass") or "",
        "dashboard": _parse_dashboard(cfg.get("dashboard") or {}),
    }

# 看板默认配置。config.yaml 里没写 dashboard 段也能正常跑
_DEFAULT_DASHBOARD = {
    "refresh_seconds": 30,
    "offline_minutes": 15.0,
    "history_hours": 6.0,
    "thresholds": {
        "cpu_warn": 80.0, "cpu_err": 95.0,
        "mem_warn": 85.0, "mem_err": 95.0,
        "disk_warn": 90.0, "disk_err": 98.0,
        "failed_warn": 1.0, "failed_err": 4.0,
    },
}

def _num(mapping, key, default, cast=float):
    """安全取数值：缺失或写错类型时回落默认值，绝不让配置手误炸掉服务"""
    try:
        return cast(mapping[key])
    except (KeyError, TypeError, ValueError):
        return default

def _parse_dashboard(raw: dict) -> dict:
    """解析看板配置，逐项兜底默认值"""
    out = {
        "refresh_seconds": _num(raw, "refresh_seconds", _DEFAULT_DASHBOARD["refresh_seconds"], int),
        "offline_minutes": _num(raw, "offline_minutes", _DEFAULT_DASHBOARD["offline_minutes"]),
        "history_hours":   _num(raw, "history_hours",   _DEFAULT_DASHBOARD["history_hours"]),
        "thresholds": dict(_DEFAULT_DASHBOARD["thresholds"]),
    }
    # 刷新间隔做上下界约束，防止有人填 0 把浏览器变成 DDoS
    out["refresh_seconds"] = max(5, min(out["refresh_seconds"], 3600))
    th = raw.get("thresholds") or {}
    if isinstance(th, dict):
        for k in out["thresholds"]:
            out["thresholds"][k] = _num(th, k, out["thresholds"][k])
    return out

CFG = load_config()
DB_PATH = CFG["db_path"]
PORT = CFG["port"]
HOST = CFG["host"]
RETENTION_DAYS = CFG["retention_days"]
TOKEN = CFG["token"]
DASH = CFG["dashboard"]
STATIC_DIR = Path(__file__).with_name("static")
DASH_USER = CFG["dashboard_user"]
DASH_PASS = CFG["dashboard_pass"]

# ── 通用小工具 ────────────────────────────────────────

def _as_float(v, default: float = 0.0) -> float:
    """库里可能存着 NULL 或脏值，统一兜底成 float，避免格式化时抛异常"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def _as_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

def _utc_now_str() -> str:
    """当前 UTC 时间字符串，和库里的 created_at 同一口径"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _parse_json_list(raw) -> list:
    """ssh_issues 这类字段在库里是 JSON 字符串，解析成 list[str]；解不出来返回空表"""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []

# ── 数据模型 ──────────────────────────────────────────
class SystemInfo(BaseModel):
    cpu_percent: float
    memory_total_mb: float
    memory_used_mb: float
    memory_percent: float
    disk_total_gb: float
    disk_used_gb: float
    disk_percent: float
    load_1m: float
    load_5m: float
    load_15m: float
    net_sent_mb: float
    net_recv_mb: float

class ServiceInfo(BaseModel):
    services_total: int
    services_failed: int
    process_count: int
    listening_ports: list

class SecurityInfo(BaseModel):
    recent_logins: list
    sudo_events: list
    ssh_issues: list
    iptables_rules: int

class HardwareInfo(BaseModel):
    temperatures: list
    smart_health: str
    raid_status: str

class LogEntry(BaseModel):
    type: str
    source: str
    level: str
    message: str

class LogData(BaseModel):
    total_collected: int = 0
    error_count: int = 0
    warn_count: int = 0
    entries: list[LogEntry] = []

class Report(BaseModel):
    hostname: str
    timestamp: str
    system: SystemInfo
    services: ServiceInfo
    security: SecurityInfo
    hardware: HardwareInfo
    logs: LogData | None = None

# ── 数据库 ────────────────────────────────────────────

_db_lock = threading.Lock()
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    with _db_lock:
        db = get_db()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hostname TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                cpu_percent REAL,
                memory_percent REAL,
                disk_percent REAL,
                load_1m REAL,
                services_total INTEGER,
                services_failed INTEGER,
                process_count INTEGER,
                ssh_issues TEXT,
                smart_health TEXT,
                raw_json TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_hostname ON reports(hostname);
            CREATE INDEX IF NOT EXISTS idx_created ON reports(created_at);

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hostname TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                log_type TEXT NOT NULL,
                source TEXT,
                level TEXT,
                message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_logs_hostname ON logs(hostname);
            CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
            CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);
        """)
        db.commit()
        db.close()

def cleanup_old_reports():
    """删除超过 RETENTION_DAYS 天的记录"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with _db_lock:
        db = get_db()
        n = db.execute("DELETE FROM reports WHERE created_at < ?", (cutoff,)).rowcount
        db.commit()
        db.close()
    return n
def save_report(report: Report):
    with _db_lock:
        db = get_db()
        db.execute("""
            INSERT INTO reports (hostname, timestamp, cpu_percent, memory_percent,
                disk_percent, load_1m, services_total, services_failed,
                process_count, ssh_issues, smart_health, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            report.hostname, report.timestamp,
            report.system.cpu_percent, report.system.memory_percent,
            report.system.disk_percent, report.system.load_1m,
            report.services.services_total, report.services.services_failed,
            report.services.process_count,
            json.dumps(report.security.ssh_issues, ensure_ascii=False),
            report.hardware.smart_health,
            json.dumps(report.model_dump(), ensure_ascii=False),
        ))
        if report.logs and report.logs.entries:
            for entry in report.logs.entries:
                db.execute("""
                    INSERT INTO logs (hostname, timestamp, log_type, source, level, message)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (report.hostname, report.timestamp, entry.type, entry.source, entry.level, entry.message))
        db.commit()
        db.close()

# 看板需要的列。刻意不含 raw_json —— 那个字段单条可达数 KB
# （里面有 last 登录记录、sudo 日志、50 条端口、全量服务列表），
# 90 台机器每次刷新都传一遍是纯浪费。
_LATEST_COLS = ("id, hostname, timestamp, cpu_percent, memory_percent, disk_percent, "
                "load_1m, services_total, services_failed, process_count, "
                "ssh_issues, smart_health, created_at")

_HISTORY_COLS = "id, hostname, cpu_percent, memory_percent, disk_percent, load_1m, created_at"

def get_latest_reports():
    """每台主机的最新一条。不加时间过滤——掉线的机器更应该看得见。"""
    db = get_db()
    rows = db.execute(f"""
        SELECT {_LATEST_COLS} FROM reports
        WHERE id IN (SELECT MAX(id) FROM reports GROUP BY hostname)
        ORDER BY hostname
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]

def _to_utc(ts) -> datetime | None:
    """把库里的 UTC 时间字符串解析成带时区的 datetime"""
    try:
        return datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None

def age_seconds(created_at) -> float:
    """created_at 是 UTC 字符串，返回距今秒数。解析不出来当作「无限久」→ 判离线"""
    t = _to_utc(created_at)
    if t is None:
        return float("inf")
    return max(0.0, (datetime.now(timezone.utc) - t).total_seconds())

def get_host_history(hostname: str, hours: int = 24, limit: int = 500):
    """单台机器的历史数据，按时间正序返回（方便前端直接画图）"""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    db = get_db()
    rows = db.execute(f"""
        SELECT {_HISTORY_COLS} FROM reports WHERE hostname = ? AND created_at > ?
        ORDER BY id DESC LIMIT ?
    """, (hostname, since, limit)).fetchall()
    db.close()
    return [dict(r) for r in reversed(rows)]

def get_all_history(hours: float, max_points: int = 120) -> dict:
    """一次查询取回所有主机的精简时序，供看板 sparkline 使用。

    返回 {hostname: {"t": [unix秒], "cpu": [...], "mem": [...], "disk": [...]}}。
    单主机点数超过 max_points 时等距降采样，避免机器多了把响应撑大。
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    db = get_db()
    rows = db.execute("""
        SELECT hostname, created_at, cpu_percent, memory_percent, disk_percent
        FROM reports WHERE created_at > ? ORDER BY hostname, id
    """, (since,)).fetchall()
    db.close()

    buckets: dict[str, list] = {}
    for r in rows:
        buckets.setdefault(r["hostname"], []).append(r)

    out = {}
    for host, items in buckets.items():
        step = max(1, len(items) // max_points)
        t, cpu, mem, disk = [], [], [], []
        for r in items[::step]:
            dt = _to_utc(r["created_at"])
            if dt is None:
                continue
            t.append(int(dt.timestamp()))
            cpu.append(round(_as_float(r["cpu_percent"]), 2))
            mem.append(round(_as_float(r["memory_percent"]), 2))
            disk.append(round(_as_float(r["disk_percent"]), 2))
        if t:
            out[host] = {"t": t, "cpu": cpu, "mem": mem, "disk": disk}
    return out

# ── 日志查询 ──────────────────────────────────────────

def query_logs(hostname: str = None, level: str = None, keyword: str = None,
               log_type: str = None, hours: int = 24, limit: int = 200):
    """多条件组合查询日志"""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conditions = ["created_at > ?"]
    params: list = [since]
    if hostname:
        conditions.append("hostname = ?")
        params.append(hostname)
    if level:
        conditions.append("level = ?")
        params.append(level.upper())
    if log_type:
        conditions.append("log_type = ?")
        params.append(log_type)
    if keyword:
        conditions.append("message LIKE ?")
        params.append(f"%{keyword}%")
    where = " AND ".join(conditions)
    db = get_db()
    rows = db.execute(
        f"SELECT * FROM logs WHERE {where} ORDER BY created_at DESC LIMIT ?",
        params + [limit]
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_log_stats(hours: int = 24):
    """日志统计：按级别/类型/主机分组"""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    db = get_db()
    by_level = db.execute(
        "SELECT level, COUNT(*) as cnt FROM logs WHERE created_at > ? GROUP BY level",
        (since,)
    ).fetchall()
    by_type = db.execute(
        "SELECT log_type, COUNT(*) as cnt FROM logs WHERE created_at > ? GROUP BY log_type",
        (since,)
    ).fetchall()
    by_host = db.execute(
        "SELECT hostname, COUNT(*) as total, "
        "SUM(CASE WHEN level IN ('ERROR','CRITICAL','EMERGENCY','ALERT') THEN 1 ELSE 0 END) as errors "
        "FROM logs WHERE created_at > ? GROUP BY hostname",
        (since,)
    ).fetchall()
    db.close()
    return {
        "by_level": {r["level"]: r["cnt"] for r in by_level},
        "by_type": {r["log_type"]: r["cnt"] for r in by_type},
        "by_host": [dict(r) for r in by_host],
    }


def cleanup_old_logs():
    """清理过期日志，与巡检记录保留策略一致"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with _db_lock:
        db = get_db()
        n = db.execute("DELETE FROM logs WHERE created_at < ?", (cutoff,)).rowcount
        db.commit()
        db.close()
    return n

# ── 状态判定 ──────────────────────────────────────────
# 判定逻辑只在这里写一份，/api/hosts、/api/summary、看板全部复用。
# 前端不再自己实现阈值，改 config.yaml 重启即生效。

STATUS_TEXT = {"ok": "正常", "warn": "警告", "err": "严重", "offline": "离线"}
STATUS_ICON = {"ok": "✓", "warn": "!", "err": "×", "offline": "○"}

def evaluate_host(row: dict) -> dict:
    """把一行数据库记录加工成看板要的形态：附加 status / age_seconds / reasons。

    reasons 是「为什么被标黄/标红」的人话列表，运维不用再登机器查。
    """
    th = DASH["thresholds"]
    cpu  = _as_float(row.get("cpu_percent"))
    mem  = _as_float(row.get("memory_percent"))
    disk = _as_float(row.get("disk_percent"))
    failed = _as_int(row.get("services_failed"))
    ssh_issues = _parse_json_list(row.get("ssh_issues"))
    smart = str(row.get("smart_health") or "unknown")
    age = age_seconds(row.get("created_at"))

    warn_hits, err_hits = [], []
    if cpu >= th["cpu_warn"]:   warn_hits.append(f"CPU {cpu:.0f}%")
    if cpu >= th["cpu_err"]:    err_hits.append(f"CPU {cpu:.0f}%")
    if mem >= th["mem_warn"]:   warn_hits.append(f"内存 {mem:.0f}%")
    if mem >= th["mem_err"]:    err_hits.append(f"内存 {mem:.0f}%")
    if disk >= th["disk_warn"]: warn_hits.append(f"磁盘 {disk:.0f}%")
    if disk >= th["disk_err"]:  err_hits.append(f"磁盘 {disk:.0f}%")
    if failed >= th["failed_warn"]: warn_hits.append(f"服务失败 {failed} 个")
    if failed >= th["failed_err"]:  err_hits.append(f"服务失败 {failed} 个")
    if smart.strip().upper() == "FAILED":
        err_hits.append("SMART 检查未通过")
    warn_hits.extend(f"SSH：{x}" for x in ssh_issues)

    if err_hits:
        status = "err"
    elif warn_hits:
        status = "warn"
    else:
        status = "ok"

    # 离线优先级最高：指标再漂亮，不上报就是失联
    if age > DASH["offline_minutes"] * 60:
        status = "offline"

    reasons = list(err_hits if status == "err" else warn_hits)
    if status == "offline":
        # 失联时把失联时长放第一条，同时保留它掉线前就存在的问题
        reasons.insert(0, f"已 {format_age(age)} 未上报")

    return {
        "hostname": str(row.get("hostname") or "?"),
        "status": status,
        "status_text": STATUS_TEXT.get(status, status),
        "status_icon": STATUS_ICON.get(status, "?"),
        "age_seconds": None if age == float("inf") else round(age, 1),
        # 服务器收到这条数据的时间（UTC，权威时间）
        "reported_at": str(row.get("created_at") or ""),
        # Agent 机器自己打的时间戳，仅供参考：机器时钟漂了这个就是错的
        "agent_timestamp": str(row.get("timestamp") or ""),
        "cpu_percent": round(cpu, 1),
        "memory_percent": round(mem, 1),
        "disk_percent": round(disk, 1),
        "load_1m": round(_as_float(row.get("load_1m")), 2),
        "services_total": _as_int(row.get("services_total")),
        "services_failed": failed,
        "process_count": _as_int(row.get("process_count")),
        "smart_health": smart,
        "ssh_issues": ssh_issues,
        "reasons": reasons,
    }

def format_age(seconds) -> str:
    """把秒数转成「3 分钟前」这种人话"""
    if seconds is None or seconds == float("inf"):
        return "未知时间"
    s = float(seconds)
    if s < 60:
        return f"{int(s)} 秒前"
    if s < 3600:
        return f"{int(s // 60)} 分钟前"
    if s < 86400:
        return f"{int(s // 3600)} 小时前"
    return f"{int(s // 86400)} 天前"

def build_host_views() -> list:
    """取最新数据 + 判定状态，返回看板要用的完整列表"""
    return [evaluate_host(row) for row in get_latest_reports()]

# ── FastAPI ───────────────────────────────────────────

def _daily_cleanup():
    """后台线程：每 24 小时清理一次过期数据"""
    while True:
        _time.sleep(24 * 3600)
        try:
            n = cleanup_old_reports()
            nl = cleanup_old_logs()
            print(f"[cleanup] 清理 {n} 条巡检记录 + {nl} 条日志")
        except Exception as e:
            print(f"[cleanup] 失败: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建表 + 清理一次，并启动每日清理线程"""
    init_db()
    cleanup_old_reports()
    cleanup_old_logs()
    threading.Thread(target=_daily_cleanup, daemon=True).start()
    yield

def require_token(authorization: str = Header("")):
    """校验 Bearer Token。TOKEN 为空时不启用鉴权"""
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="invalid token")

_basic_auth = HTTPBasic(auto_error=False)

def require_dashboard(creds: HTTPBasicCredentials = Depends(_basic_auth)):
    """看板/API 的 HTTP Basic 鉴权。
    未配置 INSPECT_DASHBOARD_USER 时放行（仅建议本机调试用），
    生产环境务必配置账号密码，否则主机信息会对外裸奔。
    """
    if not DASH_USER:
        return
    if (creds is None
            or not secrets.compare_digest(creds.username, DASH_USER)
            or not secrets.compare_digest(creds.password, DASH_PASS)):
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="inspect-dashboard"'},
        )

app = FastAPI(title="Linux 巡检看板", version="1.1", lifespan=lifespan)

# 静态资源（app.css / app.js / vendor 里的 uPlot）。
# 这些只是 UI 代码、不含任何主机数据，所以不挂 require_dashboard；
# 真正的数据接口 /api/* 全部受 Basic 鉴权保护。
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.post("/report", dependencies=[Depends(require_token)])
def receive_report(report: Report):
    save_report(report)
    return {"status": "ok", "hostname": report.hostname}

@app.get("/", include_in_schema=False, dependencies=[Depends(require_dashboard)])
def dashboard():
    """看板入口：只负责吐出静态 index.html，页面自己 fetch API 渲染。

    好处是前端改样式/改交互不用重启服务，也不再有 80 行 HTML 卡在 Python 字符串里。
    鉴权沿用 require_dashboard：浏览器首次访问弹 Basic 登录框，
    之后同页面的 fetch 会自动带上凭据，前端不需要额外处理。
    """
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=500, detail="static/index.html 缺失，请检查部署文件")
    return FileResponse(index)

@app.get("/api/hosts", dependencies=[Depends(require_dashboard)])
def api_hosts():
    """所有主机的最新状态，已附加 status / age_seconds / reasons。

    状态判定只在后端做一份，前端不重复实现阈值逻辑——改 config.yaml 即刻生效。
    汇总条的计数由前端基于这份数据自己算，避免首屏发两个请求把库查两遍。
    """
    return build_host_views()

@app.get("/api/summary", dependencies=[Depends(require_dashboard)])
def api_summary():
    """轻量汇总，给告警脚本 / curl 巡检用（前端首屏不走这个）"""
    hosts = build_host_views()
    counts = {k: 0 for k in STATUS_TEXT}
    attention = []
    for h in hosts:
        counts[h["status"]] = counts.get(h["status"], 0) + 1
        if h["status"] in ("err", "offline", "warn"):
            attention.append({
                "hostname": h["hostname"],
                "status": h["status"],
                "age_text": format_age(h["age_seconds"]),
                "reasons": h["reasons"],
            })
    # 最严重的排最前，告警脚本直接取 attention[0] 即可
    order = {"err": 0, "offline": 1, "warn": 2}
    attention.sort(key=lambda x: (order.get(x["status"], 9), x["hostname"]))
    return {
        "total": len(hosts),
        **counts,
        "attention": attention,
        "server_time": _utc_now_str(),
    }

@app.get("/api/config", dependencies=[Depends(require_dashboard)])
def api_config():
    """把阈值和刷新间隔下发给前端，保证前后端判定口径一致"""
    return {
        "refresh_seconds": DASH["refresh_seconds"],
        "offline_minutes": DASH["offline_minutes"],
        "history_hours": DASH["history_hours"],
        "thresholds": DASH["thresholds"],
        "status_text": STATUS_TEXT,
        "status_icon": STATUS_ICON,
        "server_time": _utc_now_str(),
    }

@app.get("/api/history", dependencies=[Depends(require_dashboard)])
def api_history(hours: float | None = None):
    """所有主机的精简时序，喂给卡片上的 sparkline。

    不传 hours 就用 config.yaml 里的 dashboard.history_hours。
    单独开一个批量接口，是为了避免「90 台机器 = 90 个请求」的雪崩。
    """
    h = hours if (hours and hours > 0) else DASH["history_hours"]
    h = min(h, 24 * RETENTION_DAYS)      # 上限＝数据保留期，防止被拿来拖库
    return get_all_history(h)

@app.get("/api/hosts/{hostname}", dependencies=[Depends(require_dashboard)])
def api_host_detail(hostname: str, hours: int = 24):
    """单台主机历史（时间正序），给详情页 / 趋势图用"""
    rows = get_host_history(hostname, hours=min(max(hours, 1), 24 * RETENTION_DAYS))
    if not rows:
        raise HTTPException(status_code=404, detail=f"没有 {hostname} 的历史数据")
    return rows

# ── 日志 API ──────────────────────────────────────────

@app.get("/api/logs", dependencies=[Depends(require_dashboard)])
def api_logs(hostname: str = None, level: str = None, keyword: str = None,
             log_type: str = None, hours: int = 24, limit: int = 200):
    """日志查询，支持多条件组合过滤"""
    return query_logs(hostname, level, keyword, log_type,
                      hours=min(max(hours, 1), 24 * RETENTION_DAYS),
                      limit=min(max(limit, 1), 1000))

@app.get("/api/logs/stats", dependencies=[Depends(require_dashboard)])
def api_log_stats(hours: int = 24):
    """日志统计：按级别/类型/主机分组"""
    return get_log_stats(hours=min(max(hours, 1), 24 * RETENTION_DAYS))

@app.get("/logs", include_in_schema=False, dependencies=[Depends(require_dashboard)])
def logs_page():
    """日志分析看板入口"""
    logs_html = STATIC_DIR / "logs.html"
    if not logs_html.exists():
        raise HTTPException(status_code=500, detail="static/logs.html 缺失")
    return FileResponse(logs_html)

# ── 终端表格输出 ──────────────────────────────────────

def dwidth(s: str) -> int:
    """计算字符串在终端里的显示宽度：中日韩全角字符算 2 列"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)

def pad(s: str, width: int, align: str = "<") -> str:
    """按显示宽度补位。align: '<' 左对齐, '>' 右对齐"""
    s = str(s)
    gap = max(0, width - dwidth(s))
    return s + " " * gap if align == "<" else " " * gap + s

def truncate(s: str, width: int) -> str:
    """按显示宽度截断，超出部分截掉（避免长主机名挤歪整行）"""
    s = str(s)
    if dwidth(s) <= width:
        return s
    out = ""
    for ch in s:
        if dwidth(out) + (2 if unicodedata.east_asian_width(ch) in "WF" else 1) > width:
            break
        out += ch
    return out

def print_terminal_table():
    """在终端打印汇总表格"""
    hosts = get_latest_reports()
    if not hosts:
        print("暂无巡检数据")
        return

    print(f"\n{'='*90}")
    print(f"{'Linux 巡检汇总':^90}")
    print(f"{'='*90}")
    print(pad('主机名', 20) + ' ' + pad('CPU%', 7, '>') + ' ' + pad('内存%', 7, '>') + ' '
          + pad('磁盘%', 7, '>') + ' ' + pad('负载', 7, '>') + ' ' + pad('服务状态', 13, '>') + ' '
          + pad('SMART', 10) + ' ' + pad('时间', 16))
    print(f"{'-'*90}")
    for h in hosts:
        failed = h.get('services_failed') or 0
        total = h.get('services_total') or 0
        svc = f"{failed}失败/{total}总"
        print(pad(truncate(h.get('hostname', '?'), 20), 20) + ' '
              + pad(f"{(h.get('cpu_percent') or 0):.1f}%", 7, '>') + ' '
              + pad(f"{(h.get('memory_percent') or 0):.1f}%", 7, '>') + ' '
              + pad(f"{(h.get('disk_percent') or 0):.1f}%", 7, '>') + ' '
              + pad(f"{(h.get('load_1m') or 0):.2f}", 7, '>') + ' '
              + pad(truncate(svc, 13), 13, '>') + ' '
              + pad(truncate(h.get('smart_health') or '?', 10), 10) + ' '
              + pad(truncate(h.get('timestamp') or '', 16), 16))
    print(f"{'='*90}\n共 {len(hosts)} 台主机\n")

# ── 启动 ──────────────────────────────────────────────

def _check_security():
    """启动时做安全检查，发现弱配置就大声告警"""
    warnings = []
    if not TOKEN:
        warnings.append("未设置上报 Token（INSPECT_TOKEN），任何人都能往数据库里伪造数据！")
    elif TOKEN == "my-secret-token":
        warnings.append("仍在使用默认 Token 'my-secret-token'，请立即更换为随机强密码！")
    if not DASH_USER:
        warnings.append("未设置看板账号（INSPECT_DASHBOARD_USER），看板和 API 对外无需登录即可访问！")
    if HOST == "0.0.0.0":
        warnings.append(f"监听在 {HOST}，建议改为 127.0.0.1 并通过 Nginx 反代对外提供 HTTPS 服务。")
    for w in warnings:
        print(f"[安全告警] {w}")

def main():
    import argparse
    p = argparse.ArgumentParser(description="Linux 巡检中央服务器")
    p.add_argument("--cli", action="store_true", help="只打印终端表格后退出")
    p.add_argument("--port", type=int, default=PORT, help="监听端口")
    args = p.parse_args()

    if args.cli:
        init_db()
        print_terminal_table()
    else:
        _check_security()
        print(f"巡检服务器启动: http://{HOST}:{args.port}")
        uvicorn.run(app, host=HOST, port=args.port, log_level="info")

if __name__ == "__main__":
    main()
