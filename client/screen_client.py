"""Screen Monitor 클라이언트 (데스크탑 앱)

이 PC의 주모니터 화면을 Screen Monitor 서버로 공유한다.
서버 관리자가 이 PC의 IP를 등록해야 화면이 수집된다.

실행:
    python screen_client.py
    python screen_client.py --server http://192.168.0.10:8000 --token XXXX --connect
옵션:
    --connect       실행하자마자 공유 시작
    --minimized     최소화 상태로 시작
    --low-load N    저부하 모드: 실시간 대신 N초마다 한 장만 캡처 (0 = 끔)
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import io
import json
import os
import platform
import queue
import socket
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import tkinter as tk
from tkinter import messagebox, ttk

import mss
from PIL import Image, ImageTk
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI

APP_NAME = "Screen Monitor 클라이언트"
VERSION = "1.1.0"
# 저부하 모드 캡처 주기 선택지 (초)
LOW_LOAD_CHOICES = [(5, "5초"), (10, "10초"), (30, "30초"), (60, "1분"), (300, "5분"), (600, "10분")]
LOW_LOAD_MIN_GAP = 2.0  # 저부하 모드에서 관리자 '지금 캡처' 요청이 몰려도 최소 이 간격은 유지
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ScreenMonitorClient"
CONFIG_PATH = CONFIG_DIR / "config.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ScreenMonitorClient"

MSS = getattr(mss, "MSS", None) or mss.mss


# --------------------------------------------------------------------------- #
# 설정
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")


def to_ws_url(server: str) -> str:
    server = server.strip().rstrip("/")
    if "://" not in server:
        server = "http://" + server
    u = urlparse(server)
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(u.scheme)
    if not scheme or not u.hostname:
        raise ValueError("서버 주소 형식이 올바르지 않습니다 (예: http://192.168.0.10:8000)")
    netloc = u.netloc if u.port else f"{u.hostname}:8000"
    return f"{scheme}://{netloc}/ws/agent"


def outgoing_ip(host: str) -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 1))
            return s.getsockname()[0]
    except OSError:
        return ""


def set_autorun(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg

    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}" --connect --minimized'
    else:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = pyw if pyw.exists() else Path(sys.executable)
        cmd = f'"{exe}" "{Path(__file__).resolve()}" --connect --minimized'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def set_low_priority(enabled: bool) -> None:
    """저부하 모드에서는 프로세스 우선순위를 '보통 미만'으로 낮춰 사용자의 작업을 방해하지 않게 한다."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000 if enabled else 0x20)  # BELOW_NORMAL / NORMAL
    except Exception:
        pass


def get_autorun() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 화면 캡처 (전용 스레드에서만 사용)
# --------------------------------------------------------------------------- #
class Capturer:
    def __init__(self) -> None:
        self.sct = None

    @staticmethod
    def primary(monitors: list[dict]) -> dict:
        for m in monitors[1:]:
            if m.get("is_primary"):
                return m
        for m in monitors[1:]:  # Windows 주모니터는 항상 (0,0) 에서 시작
            if m["left"] == 0 and m["top"] == 0:
                return m
        return monitors[1] if len(monitors) > 1 else monitors[0]

    def grab(self, max_width: int, quality: int, last_digest, force: bool, want_thumb: bool = False):
        try:
            if self.sct is None:
                self.sct = MSS()
            shot = self.sct.grab(self.primary(self.sct.monitors))
        except Exception:
            # 해상도 변경/화면 잠금 등으로 실패하면 다음에 새로 생성
            try:
                if self.sct:
                    self.sct.close()
            finally:
                self.sct = None
            raise
        raw = shot.bgra
        size = (shot.width, shot.height)
        # 화면 변화 감지용 (crc32 는 20MB 화면도 1ms 수준; 10초마다 강제 전송하므로 충돌 영향 없음)
        digest = (zlib.crc32(raw), size)
        if digest == last_digest and not force:
            return digest, None, size, None
        img = Image.frombytes("RGB", shot.size, raw, "raw", "BGRX")
        if img.width > max_width:
            img = img.resize((max_width, max(1, round(img.height * max_width / img.width))), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        thumb = None
        if want_thumb:
            thumb = img.copy()
            thumb.thumbnail((360, 200))
        return digest, buf.getvalue(), size, thumb


# --------------------------------------------------------------------------- #
# 전송 워커 (백그라운드 스레드 + asyncio)
# --------------------------------------------------------------------------- #
def fmt_interval(sec: int) -> str:
    return f"{sec // 60}분" if sec >= 60 and sec % 60 == 0 else f"{sec}초"


class Worker(threading.Thread):
    """서버 설정에 따라 화면을 보낸다.

    - 실시간(interval=0): fps 만큼 캡처하되 화면이 바뀐 경우에만 전송 (10초마다는 강제 전송)
    - 주기 캡처(interval=N): N초마다 한 장만 캡처해서 전송, 그 사이에는 캡처/인코딩을 전혀 하지 않음
    - 저부하 모드(low_load=N): 이 PC에서 직접 켜는 주기 캡처. 서버 설정보다 긴 주기가 적용되고
      관리자가 확대 보기를 해도 실시간 스트리밍으로 바뀌지 않는다.
    """

    def __init__(self, server: str, token: str, events: queue.Queue, low_load: int = 0):
        super().__init__(daemon=True)
        self.ws_url = to_ws_url(server)
        self.host = urlparse(self.ws_url).hostname or ""
        self.token = token.strip()
        self.events = events
        self.stop_flag = threading.Event()
        self.profile = {"fps": 1, "max_width": 1280, "quality": 55, "interval": 0}
        self.local_interval = max(0, int(low_load))
        self.capture_req = False
        self.force = True
        self.loop: asyncio.AbstractEventLoop | None = None
        self.wake: asyncio.Event | None = None
        self.capturer = Capturer()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
        self.connect_kwargs: dict = {}  # 테스트(가상 PC)에서 출발지 주소 지정 등에 사용

    def hello_info(self) -> dict:
        return {
            "hostname": socket.gethostname(),
            "user": getpass.getuser(),
            "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        }

    # GUI 로 상태 전달
    def emit(self, kind: str, **kw) -> None:
        self.events.put((kind, kw))

    def call_in_loop(self, fn) -> bool:
        if self.loop and self.wake:
            try:
                self.loop.call_soon_threadsafe(fn)
                return True
            except RuntimeError:  # 이미 종료된 워커
                pass
        return False

    def stop(self) -> None:
        self.stop_flag.set()
        self.call_in_loop(lambda: self.wake.set())

    def set_low_load(self, seconds: int) -> None:
        """GUI 스레드에서 호출: 공유 중에도 저부하 모드를 바로 바꾼다."""
        def apply() -> None:
            self.local_interval = max(0, int(seconds))
            self.force = True
            self.wake.set()
        if not self.call_in_loop(apply):
            self.local_interval = max(0, int(seconds))

    def effective_interval(self) -> int:
        return max(self.local_interval, self.profile.get("interval", 0))

    def mode_text(self) -> str:
        iv = self.effective_interval()
        if iv:
            by_pc = self.local_interval and self.local_interval >= self.profile.get("interval", 0)
            return f"화면 공유 중 · {fmt_interval(iv)}마다 한 장 캡처 ({'저부하 모드' if by_pc else '관리자 설정'})"
        if self.profile["fps"] > 1:
            return f"화면 공유 중 · 관리자가 확대 보기 중 ({self.profile['fps']:g} fps)"
        return "화면 공유 중 · 실시간 모니터링 (최대 1 fps)"

    def run(self) -> None:
        try:
            asyncio.run(self.main())
        finally:
            self.pool.shutdown(wait=False)
            self.emit("stopped")

    async def pause(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stop_flag.is_set() and time.monotonic() < end:
            await asyncio.sleep(0.2)

    async def main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.wake = asyncio.Event()
        backoff = 2
        while not self.stop_flag.is_set():
            wait = backoff
            try:
                self.emit("status", state="connecting", text="서버에 연결 중…")
                await self.session()
                backoff = 2
                wait = 1
            except InvalidStatus as e:
                code = e.response.status_code
                if code in (401, 403):
                    self.emit("status", state="error", text="접속 토큰이 올바르지 않습니다. 관리자에게 토큰을 확인하세요.")
                    wait = 30
                else:
                    self.emit("status", state="error", text=f"서버 응답 오류 (HTTP {code})")
            except ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd else None
                if code == 4003:
                    ip = self.connect_kwargs.get("local_addr", (outgoing_ip(self.host),))[0]
                    self.emit("status", state="pending",
                              text=f"등록 대기 중 — 관리자에게 이 PC의 IP({ip}) 등록을 요청하세요.")
                    wait = 10
                elif code == 4000:
                    self.emit("status", state="error", text="같은 IP의 다른 클라이언트가 접속하여 연결이 해제되었습니다.")
                    wait = 15
                elif not self.stop_flag.is_set():
                    self.emit("status", state="error", text="서버와 연결이 끊어졌습니다. 다시 연결합니다…")
            except (OSError, asyncio.TimeoutError, InvalidURI) as e:
                self.emit("status", state="error", text=f"서버에 연결할 수 없습니다 ({type(e).__name__})")
            except Exception as e:  # noqa: BLE001 - 앱이 죽지 않도록
                self.emit("status", state="error", text=f"오류: {e}")
            if self.stop_flag.is_set():
                break
            await self.pause(wait)
            backoff = min(backoff * 2, 30)

    async def receiver(self, ws) -> None:
        async for msg in ws:
            if isinstance(msg, bytes):
                continue
            try:
                obj = json.loads(msg)
            except ValueError:
                continue
            if obj.get("type") == "config":
                self.profile = {
                    "fps": max(0.2, min(30.0, float(obj.get("fps", 1)))),
                    "max_width": max(320, min(3840, int(obj.get("max_width", 1280)))),
                    "quality": max(20, min(95, int(obj.get("quality", 55)))),
                    "interval": max(0, min(3600, int(obj.get("interval", 0)))),
                }
                self.force = True
                self.wake.set()
                self.emit("status", state="sharing", text=self.mode_text())
            elif obj.get("type") == "capture":
                # 관리자가 '지금 캡처'를 요청 - 주기와 관계없이 바로 한 장 보낸다
                self.capture_req = True
                self.wake.set()

    async def session(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}
        async with ws_connect(self.ws_url, additional_headers=headers, open_timeout=8,
                              ping_interval=20, ping_timeout=20, max_size=2 ** 20,
                              **self.connect_kwargs) as ws:
            local_ip = ws.local_address[0] if self.connect_kwargs.get("local_addr") else outgoing_ip(self.host)
            size = await self.loop.run_in_executor(self.pool, self.primary_size)
            await ws.send(json.dumps({
                "type": "hello",
                **self.hello_info(),
                "version": VERSION,
                "screen": list(size),
                "local_ips": [local_ip] if local_ip else [],
                "client_interval": self.local_interval,
            }))
            self.emit("info", ip=local_ip, size=size)
            recv = asyncio.create_task(self.receiver(ws))
            # 긴 주기로 쉬는 중에도 서버 연결이 끊기면 바로 깨어나서 재접속
            recv.add_done_callback(lambda _: self.wake.set())
            last_digest = None
            last_sent = 0.0
            last_preview = 0.0
            last_capture = -1e9
            reported_local = self.local_interval
            sent = 0
            try:
                while not self.stop_flag.is_set():
                    t0 = time.monotonic()
                    if recv.done():
                        recv.result()  # 수신 중 발생한 예외를 그대로 올림
                        await ws.recv()  # 정상 종료였다면 종료 코드가 담긴 ConnectionClosed 발생
                    if self.local_interval != reported_local:
                        # 공유 중에 저부하 모드를 바꾼 경우 서버에 알림
                        reported_local = self.local_interval
                        await ws.send(json.dumps({"type": "info", "client_interval": reported_local}))
                        self.emit("status", state="sharing", text=self.mode_text())
                    if self.local_interval and t0 - last_capture < LOW_LOAD_MIN_GAP:
                        # 저부하 모드: 캡처 요청이 몰려도 최소 간격 유지
                        await asyncio.sleep(LOW_LOAD_MIN_GAP - (t0 - last_capture))
                        continue
                    p = dict(self.profile)
                    interval = self.effective_interval()
                    if interval:
                        # 주기 캡처: 매번 새로 찍은 화면을 그대로 보냄 (변화 감지 불필요)
                        force, want_thumb = True, True
                    else:
                        force = self.force or self.capture_req or t0 - last_sent > 10
                        want_thumb = t0 - last_preview > 1.5
                    self.force = self.capture_req = False
                    last_capture = t0
                    try:
                        digest, data, cur_size, thumb = await self.loop.run_in_executor(
                            self.pool, self.capturer.grab, p["max_width"], p["quality"], last_digest, force,
                            want_thumb)
                    except Exception:
                        # 잠금 화면 등 일시적 캡처 실패 - 잠시 후 재시도
                        await self.sleep_or_wake(1)
                        continue
                    if cur_size != size:
                        size = cur_size
                        await ws.send(json.dumps({"type": "info", "screen": list(size)}))
                        self.emit("info", ip=local_ip, size=size)
                    if data:
                        await ws.send(data)
                        last_digest, last_sent = digest, t0
                        sent += 1
                        if thumb is not None:
                            last_preview = t0
                            self.emit("preview", image=thumb, sent=sent)
                    period = interval if interval else 1 / p["fps"]
                    await self.sleep_or_wake(period - (time.monotonic() - t0))
            finally:
                recv.cancel()

    def primary_size(self) -> tuple[int, int]:
        with MSS() as sct:
            m = Capturer.primary(sct.monitors)
            return m["width"], m["height"]

    async def sleep_or_wake(self, seconds: float) -> None:
        # 캡처 중에 들어온 설정 변경/캡처 요청은 잃어버리지 않고 바로 처리
        if seconds <= 0 or self.force or self.capture_req or self.stop_flag.is_set():
            return
        self.wake.clear()
        try:
            await asyncio.wait_for(self.wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
STATE_COLORS = {
    "idle": "#8a94a6",
    "connecting": "#f59e0b",
    "sharing": "#22c55e",
    "pending": "#f59e0b",
    "error": "#ef4444",
}
STATE_TITLES = {
    "idle": "공유 중지됨",
    "connecting": "연결 중",
    "sharing": "화면 공유 중",
    "pending": "등록 대기",
    "error": "연결 오류",
}


class App:
    def __init__(self, root: tk.Tk, args: argparse.Namespace):
        self.root = root
        self.cfg = load_config()
        self.events: queue.Queue = queue.Queue()
        self.worker: Worker | None = None
        self.state = "idle"
        self.preview_img = None

        root.title(APP_NAME)
        root.resizable(False, False)
        root.configure(bg="#10141a")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        style = ttk.Style(root)
        style.theme_use("clam")
        font = ("Malgun Gothic", 10)
        style.configure(".", background="#10141a", foreground="#e6ebf2", font=font)
        style.configure("TLabel", background="#10141a", foreground="#e6ebf2")
        style.configure("Muted.TLabel", foreground="#8a94a6", font=("Malgun Gothic", 9))
        style.configure("Title.TLabel", font=("Malgun Gothic", 13, "bold"))
        style.configure("State.TLabel", font=("Malgun Gothic", 12, "bold"))
        style.configure("TEntry", fieldbackground="#0b0e13", foreground="#e6ebf2", insertcolor="#e6ebf2",
                        bordercolor="#232a35", lightcolor="#232a35", darkcolor="#232a35")
        style.map("TEntry", fieldbackground=[("disabled", "#14181f")], foreground=[("disabled", "#8a94a6")])
        style.configure("TCombobox", fieldbackground="#0b0e13", background="#1c222c", foreground="#e6ebf2",
                        arrowcolor="#e6ebf2", bordercolor="#232a35", lightcolor="#232a35", darkcolor="#232a35")
        style.map("TCombobox", fieldbackground=[("readonly", "#0b0e13")], foreground=[("readonly", "#e6ebf2")],
                  selectbackground=[("readonly", "#0b0e13")], selectforeground=[("readonly", "#e6ebf2")])
        root.option_add("*TCombobox*Listbox.background", "#161b23")
        root.option_add("*TCombobox*Listbox.foreground", "#e6ebf2")
        root.option_add("*TCombobox*Listbox.selectBackground", "#3d8bfd")
        style.configure("TCheckbutton", background="#10141a", foreground="#e6ebf2")
        style.map("TCheckbutton", background=[("active", "#10141a")])
        style.configure("TButton", background="#1c222c", foreground="#e6ebf2", bordercolor="#232a35", padding=(14, 6))
        style.map("TButton", background=[("active", "#262e3a"), ("disabled", "#14181f")],
                  foreground=[("disabled", "#555e6c")])
        style.configure("Accent.TButton", background="#3d8bfd", foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", "#2a6fd6"), ("disabled", "#1b2d4a")])

        frm = ttk.Frame(root, padding=18, style="TFrame")
        style.configure("TFrame", background="#10141a")
        frm.grid(sticky="nsew")

        ttk.Label(frm, text="Screen Monitor 클라이언트", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frm, text="이 PC의 주모니터 화면을 관리자 모니터링 서버로 공유합니다.",
                  style="Muted.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 12))

        # 상태 표시
        st = ttk.Frame(frm)
        st.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.dot = tk.Canvas(st, width=14, height=14, bg="#10141a", highlightthickness=0)
        self.dot_id = self.dot.create_oval(2, 2, 12, 12, fill=STATE_COLORS["idle"], outline="")
        self.dot.grid(row=0, column=0, padx=(0, 6))
        self.state_lbl = ttk.Label(st, text=STATE_TITLES["idle"], style="State.TLabel")
        self.state_lbl.grid(row=0, column=1, sticky="w")
        self.detail_lbl = ttk.Label(frm, text="서버 주소와 접속 토큰을 입력하고 [공유 시작]을 누르세요.",
                                    style="Muted.TLabel", wraplength=380, justify="left")
        self.detail_lbl.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 10))

        # 미리보기
        self.preview = tk.Label(frm, bg="#000000", width=360, height=200, text="미리보기 없음",
                                fg="#555e6c", font=("Malgun Gothic", 9), compound="center")
        self.preview.grid(row=4, column=0, columnspan=2, pady=(0, 12))

        # 입력
        ttk.Label(frm, text="서버 주소").grid(row=5, column=0, sticky="w", pady=4)
        self.server_var = tk.StringVar(value=args.server or self.cfg.get("server", "http://"))
        self.server_entry = ttk.Entry(frm, textvariable=self.server_var, width=36)
        self.server_entry.grid(row=5, column=1, sticky="ew", pady=4)
        ttk.Label(frm, text="접속 토큰").grid(row=6, column=0, sticky="w", pady=4)
        self.token_var = tk.StringVar(value=args.token or self.cfg.get("token", ""))
        self.token_entry = ttk.Entry(frm, textvariable=self.token_var, width=36, show="•")
        self.token_entry.grid(row=6, column=1, sticky="ew", pady=4)

        self.autoconnect_var = tk.BooleanVar(value=self.cfg.get("autoconnect", False))
        ttk.Checkbutton(frm, text="프로그램 실행 시 자동으로 공유 시작", variable=self.autoconnect_var,
                        command=self.persist).grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.autorun_var = tk.BooleanVar(value=get_autorun())
        cb = ttk.Checkbutton(frm, text="Windows 로그인 시 자동 실행", variable=self.autorun_var,
                             command=self.toggle_autorun)
        cb.grid(row=8, column=0, columnspan=2, sticky="w")
        if sys.platform != "win32":
            cb.state(["disabled"])

        # 저부하 모드 (주기 캡처)
        if args.low_load is not None:
            self.cfg["low_load"] = args.low_load > 0
            if args.low_load > 0:
                self.cfg["low_load_interval"] = args.low_load
        ll = ttk.Frame(frm)
        ll.grid(row=9, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.low_load_var = tk.BooleanVar(value=bool(self.cfg.get("low_load", False)))
        ttk.Checkbutton(ll, text="저부하 모드:  실시간 대신", variable=self.low_load_var,
                        command=self.apply_low_load).grid(row=0, column=0, sticky="w")
        labels = [t for _, t in LOW_LOAD_CHOICES]
        cur = int(self.cfg.get("low_load_interval", 30))
        if cur not in [s for s, _ in LOW_LOAD_CHOICES]:
            labels.append(fmt_interval(cur))
        self.low_load_combo = ttk.Combobox(ll, values=labels, width=6, state="readonly")
        self.low_load_combo.set(fmt_interval(cur))
        self.low_load_combo.grid(row=0, column=1, padx=6)
        self.low_load_combo.bind("<<ComboboxSelected>>", lambda e: self.apply_low_load())
        ttk.Label(ll, text="마다 한 장만 캡처").grid(row=0, column=2, sticky="w")
        ttk.Label(frm, text="PC 부하와 네트워크 사용량을 줄입니다. 켜져 있으면 관리자가 확대 보기를 해도\n"
                            "실시간으로 전환되지 않습니다. (공유 중에도 바로 적용)",
                  style="Muted.TLabel", justify="left").grid(row=10, column=0, columnspan=2, sticky="w", padx=(22, 0))
        set_low_priority(self.low_load_var.get())

        btns = ttk.Frame(frm)
        btns.grid(row=11, column=0, columnspan=2, sticky="e", pady=(14, 0))
        self.start_btn = ttk.Button(btns, text="공유 시작", style="Accent.TButton", command=self.start)
        self.start_btn.grid(row=0, column=0, padx=(0, 8))
        self.stop_btn = ttk.Button(btns, text="공유 중지", command=self.stop)
        self.stop_btn.grid(row=0, column=1)
        self.stop_btn.state(["disabled"])

        self.info_lbl = ttk.Label(frm, text="", style="Muted.TLabel")
        self.info_lbl.grid(row=12, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Label(frm, text=f"v{VERSION}", style="Muted.TLabel").grid(row=12, column=1, sticky="e", pady=(12, 0))

        self.root.after(100, self.poll)
        if args.minimized:
            root.iconify()
        if args.connect or (self.autoconnect_var.get() and self.server_var.get().strip() not in ("", "http://")):
            root.after(300, self.start)

    # ---------- 동작 ----------
    def low_load_seconds(self) -> int:
        """저부하 모드가 꺼져 있으면 0, 켜져 있으면 선택한 주기(초)."""
        if not self.low_load_var.get():
            return 0
        text = self.low_load_combo.get()
        for sec, label in LOW_LOAD_CHOICES:
            if label == text:
                return sec
        return int(self.cfg.get("low_load_interval", 30))

    def apply_low_load(self) -> None:
        sec = self.low_load_seconds()
        if sec:
            self.cfg["low_load_interval"] = sec
        set_low_priority(bool(sec))
        if self.worker:
            self.worker.set_low_load(sec)
        self.persist()

    def persist(self) -> None:
        self.cfg.update(server=self.server_var.get().strip(), token=self.token_var.get().strip(),
                        autoconnect=self.autoconnect_var.get(), low_load=self.low_load_var.get())
        try:
            save_config(self.cfg)
        except OSError:
            pass

    def toggle_autorun(self) -> None:
        try:
            set_autorun(self.autorun_var.get())
        except OSError as e:
            messagebox.showerror(APP_NAME, f"자동 실행 설정에 실패했습니다.\n{e}")
            self.autorun_var.set(get_autorun())

    def start(self) -> None:
        if self.worker:
            return
        server, token = self.server_var.get().strip(), self.token_var.get().strip()
        if not token:
            messagebox.showwarning(APP_NAME, "접속 토큰을 입력하세요.")
            return
        try:
            self.worker = Worker(server, token, self.events, low_load=self.low_load_seconds())
        except ValueError as e:
            messagebox.showwarning(APP_NAME, str(e))
            return
        self.persist()
        self.worker.start()
        self.start_btn.state(["disabled"])
        self.stop_btn.state(["!disabled"])
        self.server_entry.state(["disabled"])
        self.token_entry.state(["disabled"])
        self.set_state("connecting", "서버에 연결 중…")

    def stop(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker = None
        self.start_btn.state(["!disabled"])
        self.stop_btn.state(["disabled"])
        self.server_entry.state(["!disabled"])
        self.token_entry.state(["!disabled"])
        self.set_state("idle", "화면 공유가 중지되었습니다.")
        self.root.title(APP_NAME)

    def set_state(self, state: str, text: str) -> None:
        self.state = state
        self.dot.itemconfigure(self.dot_id, fill=STATE_COLORS.get(state, "#8a94a6"))
        self.state_lbl.configure(text=STATE_TITLES.get(state, state))
        self.detail_lbl.configure(text=text)
        self.root.title(f"● {APP_NAME} - 공유 중" if state == "sharing" else APP_NAME)

    def poll(self) -> None:
        try:
            while True:
                kind, kw = self.events.get_nowait()
                if kind == "status" and self.worker:
                    self.set_state(kw["state"], kw["text"])
                elif kind == "info":
                    w, h = kw["size"]
                    self.info_lbl.configure(text=f"내 IP {kw['ip'] or '-'} · 주모니터 {w}×{h}")
                elif kind == "preview" and self.worker:
                    self.preview_img = ImageTk.PhotoImage(kw["image"])
                    self.preview.configure(image=self.preview_img, text="")
        except queue.Empty:
            pass
        self.root.after(150, self.poll)

    def on_close(self) -> None:
        if self.worker:
            ans = messagebox.askyesnocancel(
                APP_NAME, "화면 공유 중입니다.\n\n[예] 공유를 중지하고 종료\n[아니오] 최소화하고 계속 공유")
            if ans is None:
                return
            if ans is False:
                self.root.iconify()
                return
            self.stop()
        self.persist()
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--server", help="서버 주소 (예: http://192.168.0.10:8000)")
    parser.add_argument("--token", help="접속 토큰")
    parser.add_argument("--connect", action="store_true", help="실행 즉시 공유 시작")
    parser.add_argument("--minimized", action="store_true", help="최소화 상태로 시작")
    parser.add_argument("--low-load", type=int, metavar="초", default=None,
                        help="저부하 모드: N초마다 한 장만 캡처 (0 = 끔, 설정에 저장됨)")
    args = parser.parse_args()

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        # 같은 PC에서 중복 실행 방지
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW(None, False, "Local\\ScreenMonitorClientMutex")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            r = tk.Tk(); r.withdraw()
            messagebox.showinfo(APP_NAME, "Screen Monitor 클라이언트가 이미 실행 중입니다.")
            return

    root = tk.Tk()
    App(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
