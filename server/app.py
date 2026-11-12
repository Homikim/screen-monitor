"""Screen Monitor - 서버

네트워크 상의 클라이언트 PC(데스크탑 앱)가 주모니터 화면을 WebSocket 으로 보내면
서버가 모아서, 로그인한 관리자 브라우저에 CCTV 월(wall) 형태로 실시간 전달한다.

실행:
    python app.py                      # 0.0.0.0:8000 으로 실행
    python app.py --port 9000
    python app.py set-password admin 새비밀번호
    python app.py show-token           # 클라이언트 접속 토큰 확인
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sqlite3
import sys
import time
from collections import deque
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.environ.get("MONITOR_DATA_DIR", BASE_DIR / "data"))
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "monitor.db"
INITIAL_PW_FILE = DATA_DIR / "initial_admin_password.txt"

SESSION_COOKIE = "monitor_session"
SESSION_TTL = 12 * 3600

# 평소(썸네일)에는 저화질/저프레임, 관리자가 특정 화면을 크게 열면 고화질/고프레임으로 전환
LOW_PROFILE = {"fps": 1, "max_width": 1280, "quality": 55, "interval": 0}
HIGH_PROFILE = {"fps": 8, "max_width": 1920, "quality": 72, "interval": 0}
# 주기 캡처 모드: 스트리밍하지 않고 N초마다 한 장만 캡처해서 보냄 (가끔 보내므로 화질은 높게)
PERIODIC_PROFILE = {"fps": 1, "max_width": 1920, "quality": 70}
MIN_INTERVAL, MAX_INTERVAL = 2, 3600

MAX_FRAME_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------------- #
# 설정 / DB
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg: dict = {}
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
    if not cfg.get("agent_token"):
        cfg["agent_token"] = secrets.token_urlsafe(18)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), "utf-8")
    if os.environ.get("MONITOR_AGENT_TOKEN"):
        cfg["agent_token"] = os.environ["MONITOR_AGENT_TOKEN"]
    return cfg


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password: str, iterations: int = 240_000) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with closing(db()) as c, c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS devices(
                ip TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            """
        )
        # 전송 모드 컬럼 (이전 버전 DB 자동 업그레이드)
        cols = {r[1] for r in c.execute("PRAGMA table_info(devices)")}
        if "interval" not in cols:
            # 0 = 실시간 스트리밍, N>0 = N초마다 주기 캡처
            c.execute("ALTER TABLE devices ADD COLUMN interval INTEGER NOT NULL DEFAULT 0")
        if "live_on_focus" not in cols:
            # 주기 캡처 PC 를 확대 보기할 때 실시간으로 전환할지
            c.execute("ALTER TABLE devices ADD COLUMN live_on_focus INTEGER NOT NULL DEFAULT 1")
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            pw = os.environ.get("MONITOR_ADMIN_PASSWORD") or secrets.token_urlsafe(9)
            c.execute("INSERT INTO users VALUES(?,?,?)", ("admin", hash_password(pw), time.time()))
            if not os.environ.get("MONITOR_ADMIN_PASSWORD"):
                INITIAL_PW_FILE.write_text(f"username: admin\npassword: {pw}\n", "utf-8")
            print("=" * 60)
            print(" 최초 관리자 계정이 생성되었습니다")
            print(f"   아이디   : admin")
            print(f"   비밀번호 : {pw}")
            print(f" (data/initial_admin_password.txt 에도 저장됨, 로그인 후 변경하세요)")
            print("=" * 60)


def set_password(username: str, password: str) -> None:
    with closing(db()) as c, c:
        c.execute(
            "INSERT INTO users VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash",
            (username, hash_password(password), time.time()),
        )


def load_registry() -> dict[str, dict]:
    with closing(db()) as c:
        return {
            r["ip"]: {"name": r["name"], "interval": r["interval"], "live_on_focus": bool(r["live_on_focus"])}
            for r in c.execute("SELECT ip, name, interval, live_on_focus FROM devices")
        }


# --------------------------------------------------------------------------- #
# 세션
# --------------------------------------------------------------------------- #
sessions: dict[str, tuple[str, float]] = {}
failed_logins: dict[str, deque] = {}


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    sessions[token] = (username, time.time() + SESSION_TTL)
    return token


def session_user(token: str | None) -> str | None:
    if not token:
        return None
    entry = sessions.get(token)
    if not entry:
        return None
    if entry[1] < time.time():
        sessions.pop(token, None)
        return None
    return entry[0]


def require_user(request: Request) -> str:
    user = session_user(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    return user


def normalize_ip(host: str | None) -> str:
    if not host:
        return "unknown"
    if host.startswith("::ffff:"):
        host = host[7:]
    return host


# --------------------------------------------------------------------------- #
# Hub: 클라이언트(에이전트) 와 관리자 화면(뷰어) 를 중계
# --------------------------------------------------------------------------- #
class AgentConn:
    def __init__(self, ws: WebSocket, ip: str):
        self.ws = ws
        self.ip = ip
        self.lock = asyncio.Lock()

    async def send_json(self, obj: dict) -> None:
        try:
            async with self.lock:
                await self.ws.send_text(json.dumps(obj))
        except Exception:
            pass

    async def close(self, code: int = 4000) -> None:
        # 4000: 같은 IP의 새 연결로 교체됨, 4003: 등록 해제됨
        try:
            await self.ws.close(code=code)
        except Exception:
            pass


class DeviceState:
    def __init__(self, ip: str):
        self.ip = ip
        self.info: dict = {}
        self.agent: AgentConn | None = None
        self.connected_at: float | None = None
        self.last_seen: float | None = None
        self.last_frame: bytes | None = None
        self.last_frame_at: float | None = None
        self.last_capture_req = 0.0
        self.frames: deque[tuple[float, int]] = deque()

    def stats(self) -> dict:
        now = time.time()
        while self.frames and now - self.frames[0][0] > 5:
            self.frames.popleft()
        n = len(self.frames)
        total = sum(b for _, b in self.frames)
        return {"fps": round(n / 5, 1), "kbps": round(total * 8 / 5 / 1000), "last_frame_at": self.last_frame_at}


class Viewer:
    def __init__(self, ws: WebSocket, user: str, session: str):
        self.ws = ws
        self.user = user
        self.session = session
        self.frames: dict[str, bytes] = {}
        self.messages: deque[str] = deque()
        self.event = asyncio.Event()
        self.focus: set[str] = set()

    def push_msg(self, obj: dict) -> None:
        self.messages.append(json.dumps(obj, ensure_ascii=False))
        self.event.set()

    def push_frame(self, device_id: str, data: bytes) -> None:
        # 느린 브라우저는 최신 프레임만 받는다(중간 프레임은 버림)
        self.frames[device_id] = data
        self.event.set()

    async def sender(self) -> None:
        while True:
            await self.event.wait()
            self.event.clear()
            while self.messages:
                await self.ws.send_text(self.messages.popleft())
            frames, self.frames = self.frames, {}
            for device_id, data in frames.items():
                idb = device_id.encode()
                await self.ws.send_bytes(bytes([len(idb)]) + idb + data)


class Hub:
    def __init__(self) -> None:
        self.devices: dict[str, DeviceState] = {}
        self.viewers: set[Viewer] = set()
        self.focus_count: dict[str, int] = {}
        self.registry: dict[str, dict] = {}  # ip -> {name, interval, live_on_focus}
        # 등록되지 않은 IP 에서 접속 시도한 클라이언트 (관리자가 보고 등록할 수 있도록)
        self.pending: dict[str, dict] = {}

    def pending_list(self) -> list[dict]:
        now = time.time()
        for ip in [ip for ip, p in self.pending.items() if now - p["last_try"] > 120 or ip in self.registry]:
            self.pending.pop(ip, None)
        return sorted(self.pending.values(), key=lambda p: p["ip"])

    def add_pending(self, ip: str, info: dict) -> None:
        is_new = ip not in self.pending
        self.pending[ip] = {"ip": ip, "hostname": info.get("hostname", ""), "user": info.get("user", ""),
                            "os": info.get("os", ""), "last_try": time.time()}
        self.broadcast({"type": "pending", "pending": self.pending_list(), "new": ip if is_new else None})

    def reload_registry(self) -> None:
        self.registry = load_registry()

    def name_of(self, ip: str) -> str:
        return self.registry.get(ip, {}).get("name", "")

    def device_view(self, ip: str) -> dict:
        st = self.devices.get(ip)
        info = st.info if st else {}
        reg = self.registry.get(ip, {})
        client_iv = info.get("client_interval", 0)
        view = {
            "id": ip,
            "ip": ip,
            "name": reg.get("name", ""),
            "registered": ip in self.registry,
            # 전송 모드: interval=관리자 설정(0=실시간), client_interval=클라이언트 PC 자체 저부하 모드,
            # eff_interval=지금 실제로 적용 중인 캡처 주기(0=스트리밍)
            "interval": reg.get("interval", 0),
            "live_on_focus": reg.get("live_on_focus", True),
            "client_interval": client_iv,
            "eff_interval": max(client_iv, self.profile_for(ip)["interval"]),
            "online": bool(st and st.agent),
            "hostname": info.get("hostname", ""),
            "user": info.get("user", ""),
            "os": info.get("os", ""),
            "screen": info.get("screen"),
            "local_ips": info.get("local_ips", []),
            "version": info.get("version", ""),
            "connected_at": st.connected_at if st else None,
            "last_seen": st.last_seen if st else None,
            "has_frame": bool(st and st.last_frame),
            "focused": self.focus_count.get(ip, 0) > 0,
        }
        if st:
            view.update(st.stats())
        return view

    def list_devices(self) -> list[dict]:
        return [self.device_view(i) for i in self.registry]

    def broadcast(self, obj: dict) -> None:
        for v in list(self.viewers):
            v.push_msg(obj)

    def profile_for(self, ip: str) -> dict:
        reg = self.registry.get(ip, {})
        interval = reg.get("interval", 0)
        if self.focus_count.get(ip, 0) > 0 and (interval == 0 or reg.get("live_on_focus", True)):
            return HIGH_PROFILE
        if interval > 0:
            return {**PERIODIC_PROFILE, "interval": interval}
        return LOW_PROFILE

    async def push_config(self, ip: str) -> None:
        # 설정이 바뀌면 클라이언트에 새 전송 설정을 보내고(클라이언트는 즉시 한 장 캡처) 화면 정보를 갱신
        st = self.devices.get(ip)
        if st and st.agent:
            await st.agent.send_json({"type": "config", **self.profile_for(ip)})
        self.broadcast({"type": "device", "device": self.device_view(ip)})

    async def request_capture(self, ip: str) -> bool:
        # 관리자의 '지금 캡처' 요청 (PC당 1초에 한 번까지)
        st = self.devices.get(ip)
        now = time.time()
        if not st or not st.agent or now - st.last_capture_req < 1:
            return False
        st.last_capture_req = now
        await st.agent.send_json({"type": "capture"})
        return True

    async def agent_connected(self, ip: str, conn: AgentConn, info: dict) -> None:
        st = self.devices.setdefault(ip, DeviceState(ip))
        old = st.agent
        st.agent = conn
        st.info = info
        st.connected_at = st.last_seen = time.time()
        if old and old is not conn:
            await old.close()
        await conn.send_json({"type": "config", **self.profile_for(ip)})
        self.broadcast({"type": "device", "device": self.device_view(ip), "event": "online"})

    async def agent_frame(self, ip: str, conn: AgentConn, data: bytes) -> None:
        st = self.devices.get(ip)
        if not st or st.agent is not conn:
            return
        now = time.time()
        st.last_frame = data
        st.last_frame_at = st.last_seen = now
        st.frames.append((now, len(data)))
        for v in list(self.viewers):
            v.push_frame(ip, data)

    async def agent_disconnected(self, ip: str, conn: AgentConn) -> None:
        st = self.devices.get(ip)
        if not st or st.agent is not conn:
            return
        st.agent = None
        st.last_seen = time.time()
        self.broadcast({"type": "device", "device": self.device_view(ip), "event": "offline"})

    async def set_focus(self, viewer: Viewer, ip: str, on: bool) -> None:
        if on and ip not in viewer.focus:
            viewer.focus.add(ip)
            self.focus_count[ip] = self.focus_count.get(ip, 0) + 1
            changed = self.focus_count[ip] == 1
        elif not on and ip in viewer.focus:
            viewer.focus.discard(ip)
            self.focus_count[ip] = max(0, self.focus_count.get(ip, 0) - 1)
            changed = self.focus_count[ip] == 0
        else:
            return
        if changed and ip in self.devices:
            # 실시간 전환이 없는 주기 캡처 PC 도 같은 설정을 다시 받으면 즉시 최신 화면을 한 장 보낸다
            await self.push_config(ip)

    async def unregister(self, ip: str) -> None:
        # 등록 해제되면 연결을 끊고 화면 정보도 지운다
        st = self.devices.pop(ip, None)
        if st and st.agent:
            await st.agent.send_json({"type": "rejected", "reason": "unregistered"})
            await st.agent.close(code=4003)


hub = Hub()
CONFIG: dict = {}


async def stats_loop() -> None:
    while True:
        await asyncio.sleep(2)
        if not hub.viewers:
            continue
        stats = {ip: st.stats() for ip, st in hub.devices.items() if st.agent}
        hub.broadcast({"type": "stats", "stats": stats, "now": time.time()})


@asynccontextmanager
async def lifespan(app: FastAPI):
    CONFIG.update(load_config())
    init_db()
    hub.reload_registry()
    task = asyncio.create_task(stats_loop())
    yield
    task.cancel()


app = FastAPI(title="Screen Monitor", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def same_origin_guard(request: Request, call_next):
    # 쿠키 기반 인증이므로 다른 사이트에서의 변경 요청(CSRF) 차단
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "허용되지 않은 출처"}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        # 업데이트 후 브라우저가 예전 JS/CSS 를 쓰지 않도록 항상 재검증(ETag 304)
        response.headers["Cache-Control"] = "no-cache"
    return response


# --------------------------------------------------------------------------- #
# 페이지
# --------------------------------------------------------------------------- #
@app.get("/")
async def index(request: Request):
    if not session_user(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "dashboard.html", headers={"Cache-Control": "no-store"})


@app.get("/login")
async def login_page(request: Request):
    if session_user(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/")
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/guide")
async def guide_page():
    # 사용 가이드는 로그인 없이 열람 가능 (클라이언트 PC 사용자도 설치 방법을 볼 수 있도록)
    return FileResponse(STATIC_DIR / "guide.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
class LoginBody(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class PasswordBody(BaseModel):
    current: str = Field(max_length=256)
    new: str = Field(min_length=6, max_length=256)


class DeviceBody(BaseModel):
    ip: str = Field(max_length=64)
    name: str | None = Field(default=None, max_length=64)
    # None 이면 기존 값 유지 (신규 등록 시 실시간 / 확대 시 실시간 전환)
    interval: int | None = Field(default=None, ge=0, le=MAX_INTERVAL)
    live_on_focus: bool | None = None
    apply_all: bool = False  # 전송 모드를 등록된 모든 PC 에 적용


class ModeBody(BaseModel):
    interval: int = Field(ge=0, le=MAX_INTERVAL)
    live_on_focus: bool = True
    ips: list[str] | None = None  # None = 등록된 모든 PC


def check_interval(v: int | None) -> None:
    if v is not None and 0 < v < MIN_INTERVAL:
        raise HTTPException(400, f"캡처 주기는 {MIN_INTERVAL}초 이상이어야 합니다")


@app.post("/api/login")
async def api_login(body: LoginBody, request: Request):
    client = normalize_ip(request.client.host if request.client else None)
    window = failed_logins.setdefault(client, deque())
    now = time.time()
    while window and now - window[0] > 300:
        window.popleft()
    if len(window) >= 5:
        raise HTTPException(429, "로그인 시도가 너무 많습니다. 5분 후 다시 시도하세요")

    with closing(db()) as c:
        row = c.execute("SELECT password_hash FROM users WHERE username=?", (body.username,)).fetchone()
    if not row or not verify_password(body.password, row["password_hash"]):
        window.append(now)
        await asyncio.sleep(0.5)
        raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다")

    window.clear()
    token = create_session(body.username)
    resp = JSONResponse({"ok": True, "username": body.username})
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        sessions.pop(token, None)
        for v in list(hub.viewers):
            if v.session == token:
                try:
                    await v.ws.close(code=4001)
                except Exception:
                    pass
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    return {"username": require_user(request)}


@app.post("/api/password")
async def api_password(body: PasswordBody, request: Request):
    user = require_user(request)
    with closing(db()) as c:
        row = c.execute("SELECT password_hash FROM users WHERE username=?", (user,)).fetchone()
    if not row or not verify_password(body.current, row["password_hash"]):
        raise HTTPException(400, "현재 비밀번호가 올바르지 않습니다")
    set_password(user, body.new)
    INITIAL_PW_FILE.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/devices")
async def api_devices(request: Request):
    require_user(request)
    return {"devices": hub.list_devices()}


@app.get("/api/info")
async def api_info(request: Request):
    require_user(request)
    return {"agent_token": CONFIG.get("agent_token"), "server_urls": server_urls(request)}


@app.post("/api/devices")
async def api_device_save(body: DeviceBody, request: Request):
    require_user(request)
    try:
        ip = str(ipaddress.ip_address(body.ip.strip()))
    except ValueError:
        raise HTTPException(400, "올바른 IP 주소가 아닙니다")
    check_interval(body.interval)
    old = hub.registry.get(ip)
    name = body.name.strip() if body.name is not None else (old or {}).get("name", "")
    interval = body.interval if body.interval is not None else (old or {}).get("interval", 0)
    live = body.live_on_focus if body.live_on_focus is not None else (old or {}).get("live_on_focus", True)
    with closing(db()) as c, c:
        c.execute(
            "INSERT INTO devices(ip, name, created_at, interval, live_on_focus) VALUES(?,?,?,?,?) "
            "ON CONFLICT(ip) DO UPDATE SET name=excluded.name, interval=excluded.interval, "
            "live_on_focus=excluded.live_on_focus",
            (ip, name, time.time(), interval, int(live)),
        )
    targets = [ip]
    if body.apply_all:
        targets = await set_mode_bulk(interval, live, None)
    hub.reload_registry()
    for t in targets:
        await hub.push_config(t)
    hub.broadcast({"type": "pending", "pending": hub.pending_list()})
    return hub.device_view(ip)


async def set_mode_bulk(interval: int, live: bool, ips: list[str] | None) -> list[str]:
    with closing(db()) as c, c:
        if ips is None:
            c.execute("UPDATE devices SET interval=?, live_on_focus=?", (interval, int(live)))
        else:
            c.executemany("UPDATE devices SET interval=?, live_on_focus=? WHERE ip=?",
                          [(interval, int(live), i) for i in ips])
    return list(load_registry()) if ips is None else [i for i in ips if i in hub.registry]


@app.post("/api/devices/mode")
async def api_device_mode(body: ModeBody, request: Request):
    """여러 PC 의 전송 모드를 한 번에 변경 (ips 생략 시 전체)."""
    require_user(request)
    check_interval(body.interval)
    targets = await set_mode_bulk(body.interval, body.live_on_focus, body.ips)
    hub.reload_registry()
    for t in targets:
        await hub.push_config(t)
    return {"ok": True, "count": len(targets)}


@app.delete("/api/devices/{ip}")
async def api_device_delete(ip: str, request: Request):
    require_user(request)
    with closing(db()) as c, c:
        c.execute("DELETE FROM devices WHERE ip=?", (ip,))
    hub.reload_registry()
    await hub.unregister(ip)
    hub.broadcast({"type": "remove", "id": ip})
    return {"ok": True}


@app.get("/api/pending")
async def api_pending(request: Request):
    require_user(request)
    return {"pending": hub.pending_list()}


@app.get("/api/devices/{ip}/snapshot")
async def api_snapshot(ip: str, request: Request):
    require_user(request)
    st = hub.devices.get(ip)
    if not st or not st.last_frame:
        raise HTTPException(404, "화면이 없습니다")
    name = (hub.name_of(ip) or ip).replace('"', "")
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(st.last_frame_at or time.time()))
    filename = f"{ip}_{stamp}.jpg"
    return Response(
        st.last_frame,
        media_type="image/jpeg",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "X-Device-Name": name.encode().hex()},
    )


def server_urls(request: Request | None = None) -> list[str]:
    port = request.url.port if request and request.url.port else 8000
    return [f"http://{ip}:{port}" for ip in local_ips()]


def local_ips() -> list[str]:
    """이 서버의 IPv4 주소들. 기본 경로(실제 LAN)의 주소를 맨 앞에 둔다."""
    primary = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # 실제 패킷은 보내지 않음, 라우팅 경로만 조회
            primary = s.getsockname()[0]
    except OSError:
        pass
    others: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            others.add(info[4][0])
    except OSError:
        pass
    others.discard("127.0.0.1")
    others.discard(primary)
    ips = ([primary] if primary else []) + sorted(others)
    return ips or ["127.0.0.1"]


# --------------------------------------------------------------------------- #
# WebSocket: 클라이언트 데스크탑 앱
# --------------------------------------------------------------------------- #
def _clean_info(raw: dict) -> dict:
    def s(v, n=128):
        return str(v)[:n] if v is not None else ""

    screen = raw.get("screen")
    if not (isinstance(screen, list) and len(screen) == 2 and all(isinstance(x, int) for x in screen)):
        screen = None
    local = raw.get("local_ips") if isinstance(raw.get("local_ips"), list) else []
    civ = raw.get("client_interval", 0)
    civ = civ if isinstance(civ, int) and 0 <= civ <= MAX_INTERVAL else 0
    return {
        "client_interval": civ,
        "hostname": s(raw.get("hostname"), 64),
        "user": s(raw.get("user"), 64),
        "os": s(raw.get("os"), 96),
        "version": s(raw.get("version"), 32),
        "screen": screen,
        "local_ips": [s(x, 45) for x in local[:8]],
    }


@app.websocket("/ws/agent")
async def ws_agent(ws: WebSocket):
    auth = ws.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ws.query_params.get("token", "")
    if not hmac.compare_digest(token.encode(), CONFIG["agent_token"].encode()):
        await ws.close(code=1008)
        return
    await ws.accept()
    ip = normalize_ip(ws.client.host if ws.client else None)
    conn = AgentConn(ws, ip)
    registered = False
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            await ws.close(code=1008)
            return
        info = _clean_info(hello)
        if ip not in hub.registry:
            # 관리자가 IP 를 등록하기 전까지는 화면을 받지 않는다
            hub.add_pending(ip, info)
            await conn.send_json({"type": "rejected", "reason": "unregistered", "ip": ip})
            await ws.close(code=4003)
            return
        await hub.agent_connected(ip, conn, info)
        registered = True
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                if 2 < len(data) <= MAX_FRAME_BYTES and data[:2] == b"\xff\xd8":
                    await hub.agent_frame(ip, conn, data)
            elif msg.get("text"):
                try:
                    obj = json.loads(msg["text"])
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "info":
                    st = hub.devices.get(ip)
                    if st and st.agent is conn:
                        st.info.update(_clean_info({**st.info, **obj}))
                        hub.broadcast({"type": "device", "device": hub.device_view(ip)})
    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError, ValueError):
        pass
    finally:
        if registered:
            await hub.agent_disconnected(ip, conn)


# --------------------------------------------------------------------------- #
# WebSocket: 관리자 브라우저
# --------------------------------------------------------------------------- #
@app.websocket("/ws/view")
async def ws_view(ws: WebSocket):
    token = ws.cookies.get(SESSION_COOKIE)
    user = session_user(token)
    origin = ws.headers.get("origin")
    if not user or (origin and urlparse(origin).netloc != ws.headers.get("host")):
        await ws.close(code=1008)
        return
    await ws.accept()
    viewer = Viewer(ws, user, token)
    hub.viewers.add(viewer)
    viewer.push_msg({"type": "snapshot", "devices": hub.list_devices(), "pending": hub.pending_list(),
                     "user": user, "now": time.time()})
    for ip, st in hub.devices.items():
        if st.last_frame:
            viewer.push_frame(ip, st.last_frame)
    sender = asyncio.create_task(viewer.sender())
    try:
        while True:
            msg = await ws.receive_json()
            if not isinstance(msg, dict):
                continue
            if msg.get("type") in ("focus", "unfocus") and isinstance(msg.get("id"), str):
                await hub.set_focus(viewer, msg["id"], msg["type"] == "focus")
            elif msg.get("type") == "capture" and isinstance(msg.get("id"), str):
                await hub.request_capture(msg["id"])
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        hub.viewers.discard(viewer)
        sender.cancel()
        for ip in list(viewer.focus):
            await hub.set_focus(viewer, ip, False)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="Screen Monitor 서버")
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="서버 실행 (기본)")
    for p in (parser, run):
        p.add_argument("--host", default="0.0.0.0")
        p.add_argument("--port", type=int, default=8000)
    sp = sub.add_parser("set-password", help="계정 비밀번호 설정/계정 추가")
    sp.add_argument("username")
    sp.add_argument("password")
    sub.add_parser("show-token", help="클라이언트 접속 토큰 출력")
    args = parser.parse_args()

    if args.cmd == "set-password":
        init_db()
        set_password(args.username, args.password)
        print(f"'{args.username}' 비밀번호가 설정되었습니다.")
        return
    if args.cmd == "show-token":
        print(load_config()["agent_token"])
        return

    import uvicorn

    cfg = load_config()
    init_db()
    print("=" * 60)
    print(" Screen Monitor 서버")
    for ip in local_ips():
        print(f"   관리자 웹   : http://{ip}:{args.port}")
    print(f"   클라이언트 토큰 : {cfg['agent_token']}")
    print("=" * 60)
    uvicorn.run(app, host=args.host, port=args.port, ws_max_size=MAX_FRAME_BYTES + 1024, log_level="warning")


if __name__ == "__main__":
    main()
