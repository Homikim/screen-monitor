"""가상 PC 여러 대로 Screen Monitor 를 자동 검증한다.

    python tools/verify_virtual_pcs.py                # 검증 후 정리
    python tools/verify_virtual_pcs.py --hold 300     # 검증 후 300초 동안 가상 PC 유지(브라우저로 눈으로 확인)
    python tools/verify_virtual_pcs.py --cleanup-only # 검증용으로 등록한 127.0.0.x 정리

관리자 비밀번호는 --password 또는 server/data/initial_admin_password.txt 에서 읽는다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import queue
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from http.cookiejar import CookieJar
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from virtual_pcs import ROOT, VirtualWorker, default_token, ip_range, sc  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402
from websockets.exceptions import ConnectionClosed, InvalidStatus  # noqa: E402

FIRST_IP = "127.0.0.101"
TEST_IPS = set(ip_range("127.0.0.100", 40))


class NullQueue(queue.Queue):
    def put(self, *a, **k):  # 미리보기 이벤트는 쓰지 않음
        pass


# --------------------------------------------------------------------------- #
class Admin:
    def __init__(self, base: str):
        self.base = base
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method: str, path: str, body=None, headers=None, auth=True):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json"} if data else {}
        h.update(headers or {})
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        opener = self.opener if auth else urllib.request.build_opener()
        try:
            with opener.open(r, timeout=10) as resp:
                raw = resp.read()
                status = resp.status
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raw, status, ctype = e.read(), e.code, e.headers.get("Content-Type", "")
        return status, (json.loads(raw) if "json" in ctype else raw)

    def session(self) -> str:
        return next((c.value for c in self.jar if c.name == "monitor_session"), "")


class Viewer:
    def __init__(self, base: str, session: str):
        self.url = base.replace("http", "ws", 1) + "/ws/view"
        self.headers = {"Cookie": f"monitor_session={session}", "Origin": base}
        self.devices: dict[str, dict] = {}
        self.pending: list[dict] = []
        self.frames: Counter = Counter()
        self.frame_times: dict[str, deque] = {}
        self.events: list[tuple[str, str, str | None]] = []
        self.ws = None
        self.closed_code = None
        self.ready = asyncio.Event()

    async def run(self) -> None:
        try:
            async with connect(self.url, additional_headers=self.headers, max_size=None) as ws:
                self.ws = ws
                async for msg in ws:
                    if isinstance(msg, bytes):
                        n = msg[0]
                        did = msg[1:1 + n].decode()
                        assert msg[1 + n:1 + n + 2] == b"\xff\xd8", "JPEG 아님"
                        self.frames[did] += 1
                        self.frame_times.setdefault(did, deque(maxlen=200)).append(time.time())
                        continue
                    m = json.loads(msg)
                    t = m["type"]
                    if t == "snapshot":
                        self.devices = {d["id"]: d for d in m["devices"]}
                        self.pending = m.get("pending", [])
                        self.ready.set()
                    elif t == "device":
                        self.devices[m["device"]["id"]] = {**self.devices.get(m["device"]["id"], {}), **m["device"]}
                        self.events.append(("device", m["device"]["id"], m.get("event")))
                    elif t == "remove":
                        self.devices.pop(m["id"], None)
                        self.events.append(("remove", m["id"], None))
                    elif t == "pending":
                        self.pending = m["pending"]
                    elif t == "stats":
                        for k, v in m["stats"].items():
                            if k in self.devices:
                                self.devices[k].update(v)
        except ConnectionClosed as e:
            self.closed_code = e.rcvd.code if e.rcvd else -1
        finally:
            if self.closed_code is None and self.ws is not None:
                self.closed_code = self.ws.close_code

    async def send(self, obj: dict) -> None:
        await self.ws.send(json.dumps(obj))

    def fps(self, did: str, window: float = 3.0) -> float:
        now = time.time()
        return sum(1 for t in self.frame_times.get(did, ()) if now - t <= window) / window


# --------------------------------------------------------------------------- #
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""), flush=True)
    return ok


async def wait_until(fn, timeout: float, interval: float = 0.25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        await asyncio.sleep(interval)
    return bool(fn())


def section(title: str) -> None:
    print(f"\n■ {title}", flush=True)


# --------------------------------------------------------------------------- #
async def run(args) -> int:
    admin = Admin(args.server)
    st, _ = await asyncio.to_thread(admin.req, "POST", "/api/login", {"username": args.user, "password": args.password})
    if st != 200:
        print("관리자 로그인 실패", st)
        return 2

    async def cleanup():
        st, body = await asyncio.to_thread(admin.req, "GET", "/api/devices")
        for d in body["devices"]:
            if d["ip"] in TEST_IPS:
                await asyncio.to_thread(admin.req, "DELETE", f"/api/devices/{d['ip']}")

    await cleanup()
    if args.cleanup_only:
        print("정리 완료")
        return 0

    ips = ip_range(FIRST_IP, 12)
    reg, unreg = ips[:10], ips[10:]
    idle_ip = ips[5]
    bad_ip = "127.0.0.120"
    dup_ip = ips[1]
    nq = NullQueue()
    workers: dict[str, VirtualWorker] = {}

    def start_worker(i: int, ip: str, token: str | None = None) -> VirtualWorker:
        w = VirtualWorker(i, ip, args.server, token or args.token, nq, idle=(ip == idle_ip))
        w.start()
        return w

    # ------------------------------------------------------------------ #
    section("S1 보안: 인증/출처 검사")
    st, _ = await asyncio.to_thread(admin.req, "GET", "/api/devices", auth=False)
    check("로그인 없이 API 접근 차단 (401)", st == 401, f"status={st}")
    try:
        async with connect(args.server.replace("http", "ws", 1) + "/ws/view"):
            ok = False
    except InvalidStatus as e:
        ok = e.response.status_code == 403
    check("로그인 없이 모니터링 WebSocket 차단", ok)
    st, _ = await asyncio.to_thread(admin.req, "POST", "/api/devices", {"ip": "127.0.0.130", "name": "x"},
                                    {"Origin": "http://evil.example"})
    check("다른 사이트(Origin)에서의 등록 요청 차단 (403)", st == 403, f"status={st}")
    st, _ = await asyncio.to_thread(admin.req, "POST", "/api/devices", {"ip": "999.1.1.1", "name": "x"})
    check("잘못된 IP 형식 등록 거부 (400)", st == 400, f"status={st}")

    # ------------------------------------------------------------------ #
    section(f"S2 가상 PC 12대 접속 (등록 10대: {reg[0]}~{reg[-1]}, 미등록 2대, 잘못된 토큰 1대)")
    for n, ip in enumerate(reg, start=1):
        st, _ = await asyncio.to_thread(admin.req, "POST", "/api/devices", {"ip": ip, "name": f"가상 PC {n:02d}"})
        assert st == 200
    viewer = Viewer(args.server, admin.session())
    vtask = asyncio.create_task(viewer.run())
    await asyncio.wait_for(viewer.ready.wait(), 5)
    t0 = time.time()
    for n, ip in enumerate(ips, start=1):
        workers[ip] = start_worker(n, ip)
    bad = start_worker(20, bad_ip, token="wrong-token")

    ok = await wait_until(lambda: all(viewer.devices.get(ip, {}).get("online") for ip in reg), 20)
    check("등록된 10대 모두 온라인", ok, f"{time.time() - t0:.1f}초 소요")
    await wait_until(lambda: all(viewer.frames[ip] > 0 for ip in reg), 8)
    check("10대 모두 화면 프레임 수신", all(viewer.frames[ip] > 0 for ip in reg),
          ", ".join(f"{ip.split('.')[-1]}:{viewer.frames[ip]}" for ip in reg))
    mism = [ip for ip in reg if viewer.devices[ip].get("hostname") != f"VPC-{ips.index(ip) + 1:02d}"
            or tuple(viewer.devices[ip].get("screen") or ()) != workers[ip].capturer.size]
    check("PC별 호스트명/해상도 정보 정확", not mism, f"불일치: {mism}" if mism else
          ", ".join(sorted({f'{w.capturer.size[0]}x{w.capturer.size[1]}' for w in workers.values()})))
    check("클라이언트 앱 상태 = 화면 공유 중 (10대)",
          all(workers[ip].status[0] == "sharing" for ip in reg),
          str({ip.split('.')[-1]: workers[ip].status[0] for ip in reg if workers[ip].status[0] != "sharing"}))
    ok = await wait_until(lambda: {p["ip"] for p in viewer.pending} >= set(unreg), 5)
    check("미등록 2대는 '등록 대기' 목록에 표시", ok, str([p["ip"] for p in viewer.pending]))
    check("미등록 PC 화면은 수집되지 않음", all(viewer.frames[ip] == 0 and ip not in viewer.devices for ip in unreg))
    check("미등록 PC 앱 상태 = 등록 대기", all(workers[ip].status[0] == "pending" for ip in unreg),
          workers[unreg[0]].status[1])
    await wait_until(lambda: bad.status[0] == "error", 5)
    check("잘못된 토큰 PC 거부 + 앱에 토큰 오류 표시", bad.status[0] == "error" and "토큰" in bad.status[1]
          and bad_ip not in viewer.devices and all(p["ip"] != bad_ip for p in viewer.pending), bad.status[1])

    # ------------------------------------------------------------------ #
    section(f"S3 확대 보기(focus) 시 해당 PC만 고화질/고프레임 전환 ({ips[2]})")
    focus_ip = ips[2]
    await viewer.send({"type": "focus", "id": focus_ip})
    ok = await wait_until(lambda: workers[focus_ip].profile["fps"] == 8, 5)
    check("focus 대상 클라이언트 설정 fps 1→8 변경", ok, str(workers[focus_ip].profile))
    await asyncio.sleep(4)
    f_fps = viewer.fps(focus_ip)
    others = {ip: viewer.fps(ip) for ip in reg if ip not in (focus_ip, idle_ip)}
    # 가상 PC 13대가 한 프로세스(GIL 공유)에서 돌기 때문에 절대 fps 대신 상대 비교. 실제 fps 는 S10 에서 측정
    check("focus PC 수신 fps가 다른 PC의 3배 이상", f_fps >= 3 and f_fps >= 3 * max(others.values()),
          f"focus {f_fps:.1f} fps / 기타 최대 {max(others.values()):.1f} fps")
    check("나머지 PC는 저프레임 유지 (≤ 1.7 fps)", max(others.values()) <= 1.7,
          f"최대 {max(others.values()):.1f} fps")
    check("다른 PC 설정은 변경되지 않음", all(workers[ip].profile["fps"] == 1 for ip in others))
    await viewer.send({"type": "unfocus", "id": focus_ip})
    ok = await wait_until(lambda: workers[focus_ip].profile["fps"] == 1, 5)
    check("확대 보기 닫으면 fps 8→1 복귀", ok)

    # ------------------------------------------------------------------ #
    section("S4 클라이언트 종료/재접속 (오프라인 감지)")
    off = [ips[3], ips[4]]
    for ip in off:
        workers[ip].stop()
    ok = await wait_until(lambda: all(not viewer.devices[ip]["online"] for ip in off), 8)
    check("앱 종료한 2대 오프라인 전환", ok)
    check("오프라인 이벤트 알림 발생", all(("device", ip, "offline") in viewer.events for ip in off))
    check("나머지 PC는 영향 없음", all(viewer.devices[ip]["online"] for ip in reg if ip not in off))
    for ip in off:
        workers[ip].join(3)
        workers[ip] = start_worker(ips.index(ip) + 1, ip)
    ok = await wait_until(lambda: all(viewer.devices[ip]["online"] for ip in off), 10)
    check("다시 실행하면 온라인 복귀", ok)
    before = {ip: viewer.frames[ip] for ip in off}
    ok = await wait_until(lambda: all(viewer.frames[ip] > before[ip] for ip in off), 5)
    check("재접속 후 화면 수신 재개", ok)

    # ------------------------------------------------------------------ #
    section(f"S5 등록 대기 PC를 관리자가 등록 ({unreg[0]})")
    t1 = time.time()
    await asyncio.to_thread(admin.req, "POST", "/api/devices", {"ip": unreg[0], "name": "신규 등록 PC"})
    ok = await wait_until(lambda: viewer.devices.get(unreg[0], {}).get("online") and viewer.frames[unreg[0]] > 0, 20)
    check("등록 후 자동 재시도로 화면 수집 시작", ok, f"{time.time() - t1:.1f}초 소요 (재시도 주기 10초)")
    check("등록 대기 목록에서 제거", all(p["ip"] != unreg[0] for p in viewer.pending))

    # ------------------------------------------------------------------ #
    section(f"S6 관리자가 등록 삭제 ({reg[-1]})")
    del_ip = reg[-1]
    await asyncio.to_thread(admin.req, "DELETE", f"/api/devices/{del_ip}")
    ok = await wait_until(lambda: del_ip not in viewer.devices, 3)
    check("모니터링 화면에서 즉시 제거", ok and ("remove", del_ip, None) in viewer.events)
    ok = await wait_until(lambda: workers[del_ip].status[0] == "pending", 5)
    check("해당 클라이언트 연결 끊김 → 앱 상태 등록 대기", ok, workers[del_ip].status[1])
    n_before = viewer.frames[del_ip]
    await asyncio.sleep(2)
    check("삭제 후 화면 더 이상 수신 안 됨", viewer.frames[del_ip] == n_before)
    ok = await wait_until(lambda: any(p["ip"] == del_ip for p in viewer.pending), 15)
    check("재시도 시 등록 대기 목록에 다시 표시", ok)

    # ------------------------------------------------------------------ #
    section(f"S7 같은 IP에서 클라이언트 중복 접속 ({dup_ip})")
    dup = start_worker(99, dup_ip)
    ok = await wait_until(lambda: workers[dup_ip].status[0] == "error" and "같은 IP" in workers[dup_ip].status[1], 8)
    check("기존 연결은 해제되고 앱에 안내 표시", ok, workers[dup_ip].status[1])
    ok = await wait_until(lambda: viewer.devices[dup_ip].get("hostname") == "VPC-99" and viewer.devices[dup_ip]["online"], 5)
    check("새 연결이 해당 IP 화면을 이어서 공유", ok)
    dup.stop()
    ok = await wait_until(lambda: viewer.devices[dup_ip]["online"] and viewer.devices[dup_ip].get("hostname") == "VPC-02", 25)
    check("중복 앱 종료 후 원래 클라이언트 자동 복귀", ok)

    # ------------------------------------------------------------------ #
    section("S8 화면 변화 없는 PC / 스냅샷 / 대역폭")
    await wait_until(lambda: time.time() - t0 > 25, 30)
    age = time.time() - (viewer.devices[idle_ip].get("last_frame_at") or 0)
    check("화면 변화 없는 PC도 10초마다 갱신 (응답 없음 표시 안 됨)", age < 13, f"마지막 프레임 {age:.1f}초 전")
    busy = [ip for ip in reg[:4] if ip not in off]
    check("화면 변화 없는 PC는 전송량이 적음",
          viewer.frames[idle_ip] < min(viewer.frames[ip] for ip in busy),
          f"idle={viewer.frames[idle_ip]}프레임, 일반={min(viewer.frames[ip] for ip in busy)}프레임~")
    st, body = await asyncio.to_thread(admin.req, "GET", f"/api/devices/{ips[0]}/snapshot")
    check("스냅샷 다운로드 (JPEG)", st == 200 and isinstance(body, bytes) and body[:2] == b"\xff\xd8",
          f"{len(body) // 1024} KB" if isinstance(body, bytes) else str(st))
    st, body = await asyncio.to_thread(admin.req, "GET", "/api/devices")
    kb = {d["ip"]: d.get("kbps", 0) for d in body["devices"] if d["online"]}
    print("    PC별 전송량(kbps): " + ", ".join(f"{k.split('.')[-1]}={v}" for k, v in sorted(kb.items())))
    check("1fps 모드 PC당 대역폭 < 1.5 Mbps", bool(kb) and max(kb.values()) < 1500,
          f"최대 {max(kb.values(), default=0)} kbps")

    # ------------------------------------------------------------------ #
    section("S9 관리자 브라우저 다중 접속 / 로그아웃")
    v2 = Viewer(args.server, admin.session())
    v2task = asyncio.create_task(v2.run())
    await asyncio.wait_for(v2.ready.wait(), 5)
    online = [k for k, d in v2.devices.items() if d["online"]]
    ok = await wait_until(lambda: all(v2.frames[k] > 0 for k in online), 3)
    check("새로 접속한 관리자도 즉시 모든 화면 표시", ok, f"{len(online)}대")
    admin2 = Admin(args.server)
    await asyncio.to_thread(admin2.req, "POST", "/api/login", {"username": args.user, "password": args.password})
    v3 = Viewer(args.server, admin2.session())
    v3task = asyncio.create_task(v3.run())
    await asyncio.wait_for(v3.ready.wait(), 5)
    await asyncio.to_thread(admin2.req, "POST", "/api/logout")
    await wait_until(lambda: v3task.done(), 3)
    check("로그아웃 시 해당 세션의 모니터링 연결 종료", v3task.done() and v3.closed_code == 4001, f"code={v3.closed_code}")
    check("다른 관리자 세션은 유지", not vtask.done() and not v2task.done())
    v2task.cancel()

    # ------------------------------------------------------------------ #
    async def count_frames(ip: str, seconds: float) -> int:
        n0 = viewer.frames[ip]
        await asyncio.sleep(seconds)
        return viewer.frames[ip] - n0

    async def set_dev(body: dict) -> int:
        st, _ = await asyncio.to_thread(admin.req, "POST", "/api/devices", body)
        return st

    section(f"S11 주기 캡처 모드 (관리자 설정, {ips[0]})")
    per_ip, live_ip = ips[0], ips[6]
    st = await set_dev({"ip": per_ip, "interval": 5})
    ok = await wait_until(lambda: workers[per_ip].profile.get("interval") == 5, 5)
    check("5초 주기 설정 → 클라이언트에 즉시 전달", st == 200 and ok, str(workers[per_ip].profile))
    check("모니터링 화면에 주기 모드 표시 (eff_interval=5)",
          await wait_until(lambda: viewer.devices[per_ip].get("eff_interval") == 5, 3))
    check("이름은 그대로 유지 (전송 방식만 변경)", viewer.devices[per_ip].get("name") == "가상 PC 01",
          viewer.devices[per_ip].get("name"))
    await asyncio.sleep(1)
    n_per, n_live = await asyncio.gather(count_frames(per_ip, 12), count_frames(live_ip, 12))
    check("12초 동안 주기 PC는 2~3장만 전송", 2 <= n_per <= 3, f"주기 {n_per}장 / 실시간 PC {n_live}장")
    check("실시간 PC 대비 전송량 대폭 감소", n_live >= 3 * n_per, f"{n_live} vs {n_per}")
    n0 = viewer.frames[per_ip]
    await viewer.send({"type": "capture", "id": per_ip})
    t1 = time.time()
    ok = await wait_until(lambda: viewer.frames[per_ip] > n0, 3, 0.05)
    check("'지금 캡처' 요청 → 주기와 관계없이 즉시 한 장 수신", ok, f"{time.time() - t1:.2f}초")
    await viewer.send({"type": "focus", "id": per_ip})
    ok = await wait_until(lambda: workers[per_ip].profile.get("interval") == 0 and workers[per_ip].profile["fps"] == 8, 5)
    check("확대 보기 → 실시간(8fps) 전환 (확대 시 실시간 ON)", ok, str(workers[per_ip].profile))
    await viewer.send({"type": "unfocus", "id": per_ip})
    ok = await wait_until(lambda: workers[per_ip].profile.get("interval") == 5, 5)
    check("확대 보기 닫으면 5초 주기로 복귀", ok)
    await set_dev({"ip": per_ip, "live_on_focus": False})
    await asyncio.sleep(1.5)
    n0 = viewer.frames[per_ip]
    await viewer.send({"type": "focus", "id": per_ip})
    t1 = time.time()
    got_now = await wait_until(lambda: viewer.frames[per_ip] > n0, 3, 0.05)
    await asyncio.sleep(3)
    check("확대 시 실시간 OFF → 열자마자 최신 화면 1장, 이후에도 주기 유지",
          got_now and workers[per_ip].profile.get("interval") == 5 and viewer.frames[per_ip] - n0 <= 2,
          f"{viewer.frames[per_ip] - n0}장 / {time.time() - t1:.1f}초, {workers[per_ip].profile}")
    await viewer.send({"type": "unfocus", "id": per_ip})
    st = await set_dev({"ip": per_ip, "interval": 1})
    check("2초 미만 주기는 거부 (400)", st == 400, f"status={st}")

    bulk = [ips[6], ips[7]]
    st, _ = await asyncio.to_thread(admin.req, "POST", "/api/devices/mode", {"interval": 30, "ips": bulk})
    ok = await wait_until(lambda: all(workers[i].profile.get("interval") == 30 for i in bulk), 5)
    check("선택한 여러 PC 일괄 변경 (30초 주기)", st == 200 and ok)
    online_reg = [i for i, d in viewer.devices.items() if d["online"] and i in workers]
    await set_dev({"ip": per_ip, "interval": 10, "apply_all": True})
    ok = await wait_until(lambda: all(workers[i].profile.get("interval") == 10 for i in online_reg), 5)
    check("'모든 PC에 적용' → 접속 중인 전체 PC 10초 주기", ok, f"{len(online_reg)}대")
    st, body = await asyncio.to_thread(admin.req, "POST", "/api/devices/mode", {"interval": 0, "live_on_focus": True})
    ok = await wait_until(lambda: all(workers[i].profile.get("interval") == 0 for i in online_reg), 5)
    check("전체 실시간으로 되돌리기", st == 200 and ok, f"{body.get('count')}대")

    # ------------------------------------------------------------------ #
    ll_ip = ips[8]
    section(f"S12 클라이언트 저부하 모드 (PC에서 직접 설정, {ll_ip})")
    workers[ll_ip].set_low_load(5)
    ok = await wait_until(lambda: viewer.devices[ll_ip].get("client_interval") == 5, 5)
    check("저부하 모드 켜면 서버/모니터링 화면에 표시", ok and viewer.devices[ll_ip].get("eff_interval") == 5,
          f"client_interval={viewer.devices[ll_ip].get('client_interval')}, eff={viewer.devices[ll_ip].get('eff_interval')}")
    check("클라이언트 앱 상태 문구에 저부하 모드 표시", "저부하 모드" in workers[ll_ip].status[1], workers[ll_ip].status[1])
    await asyncio.sleep(1)
    n = await count_frames(ll_ip, 11)
    check("11초 동안 2~3장만 전송", 2 <= n <= 3, f"{n}장")
    await viewer.send({"type": "focus", "id": ll_ip})
    await asyncio.sleep(0.5)
    n = await count_frames(ll_ip, 4)
    check("관리자가 확대 보기해도 실시간으로 바뀌지 않음", n <= 2 and workers[ll_ip].profile["fps"] == 8,
          f"4초 동안 {n}장 (서버 요청 fps {workers[ll_ip].profile['fps']:g})")
    await viewer.send({"type": "unfocus", "id": ll_ip})
    workers[ll_ip].set_low_load(0)
    ok = await wait_until(lambda: viewer.devices[ll_ip].get("client_interval") == 0, 5)
    await asyncio.sleep(1)
    n = await count_frames(ll_ip, 4)
    check("저부하 모드 끄면 실시간 복귀", ok and n >= 3, f"4초 동안 {n}장")

    # ------------------------------------------------------------------ #
    section("S10 실제 화면 캡처 클라이언트 단독 성능 (127.0.0.100, 이 PC의 주모니터)")
    for w in [*workers.values(), bad]:
        w.stop()
    for w in [*workers.values(), bad]:
        w.join(3)
    real_ip = "127.0.0.100"
    await asyncio.to_thread(admin.req, "POST", "/api/devices", {"ip": real_ip, "name": "실제 화면 클라이언트"})
    real = sc.Worker(args.server, args.token, nq)
    real.connect_kwargs = {"local_addr": (real_ip, 0)}
    real.start()
    ok = await wait_until(lambda: viewer.devices.get(real_ip, {}).get("online") and viewer.frames[real_ip] > 0, 10)
    size = viewer.devices.get(real_ip, {}).get("screen")
    check("실제 주모니터 화면 수신", ok, f"주모니터 {size}")
    await viewer.send({"type": "focus", "id": real_ip})
    await asyncio.sleep(2)
    r_fps = viewer.fps(real_ip, 4)
    await asyncio.sleep(4)
    r_fps = viewer.fps(real_ip, 4)
    check("확대 보기 실제 fps ≥ 6", r_fps >= 6, f"{r_fps:.1f} fps (목표 8)")
    await viewer.send({"type": "unfocus", "id": real_ip})
    await asyncio.sleep(4)
    r_fps = viewer.fps(real_ip, 3)
    check("일반 모드 실제 fps ≤ 1.5", r_fps <= 1.5, f"{r_fps:.1f} fps")

    # 클라이언트 CPU 사용량 비교: 실시간(1fps) vs 10초 주기 캡처 (이 프로세스에는 실제 클라이언트만 동작 중)
    async def cpu_over(seconds: float) -> float:
        c0, w0 = time.process_time(), time.perf_counter()
        await asyncio.sleep(seconds)
        return (time.process_time() - c0) / (time.perf_counter() - w0) * 100

    cpu_live = await cpu_over(10)
    await asyncio.to_thread(admin.req, "POST", "/api/devices", {"ip": real_ip, "interval": 10})
    await wait_until(lambda: real.profile.get("interval") == 10, 5)
    await asyncio.sleep(1)
    cpu_per = await cpu_over(20)
    print(f"    실제 클라이언트 CPU (코어 1개 기준): 실시간 {cpu_live:.1f}% → 10초 주기 {cpu_per:.1f}%", flush=True)
    check("주기 캡처 모드에서 클라이언트 CPU 사용량 절반 이하", cpu_per <= cpu_live / 2,
          f"실시간 {cpu_live:.1f}% → 10초 주기 {cpu_per:.1f}%")
    real.stop()
    workers["real"] = real

    # ------------------------------------------------------------------ #
    passed = sum(ok for _, ok, _ in results)
    print(f"\n결과: {passed}/{len(results)} 통과", flush=True)
    for name, ok, detail in results:
        if not ok:
            print(f"  실패: {name} {detail}")

    if args.hold:
        await asyncio.to_thread(admin.req, "DELETE", f"/api/devices/{real_ip}")
        real.join(3)
        for n, ip in enumerate(ips, start=1):
            workers[ip] = start_worker(n, ip)
        print(f"\n가상 PC를 {args.hold}초 동안 유지합니다. 브라우저에서 확인하세요.", flush=True)
        await asyncio.sleep(args.hold)

    vtask.cancel()
    for w in [*workers.values(), bad]:
        w.stop()
    if not args.keep:
        await cleanup()
    return 0 if passed == len(results) else 1


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(description="가상 PC 기반 Screen Monitor 검증")
    p.add_argument("--server", default="http://127.0.0.1:8000")
    p.add_argument("--token", default=default_token())
    p.add_argument("--user", default="admin")
    p.add_argument("--password", default=None)
    p.add_argument("--hold", type=float, default=0, help="검증 후 가상 PC 유지 시간(초)")
    p.add_argument("--keep", action="store_true", help="끝난 뒤 등록 정보를 지우지 않음")
    p.add_argument("--cleanup-only", action="store_true")
    args = p.parse_args()
    if args.password is None:
        try:
            line = (ROOT / "server" / "data" / "initial_admin_password.txt").read_text("utf-8").splitlines()[1]
            args.password = line.split(":", 1)[1].strip()
        except (OSError, IndexError):
            p.error("--password 를 지정하세요")
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
