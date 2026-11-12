"""가상 PC 시뮬레이터

실제 클라이언트 코드(client/screen_client.py 의 Worker)를 그대로 사용하되,
- 화면 캡처 대신 PC마다 다른 가상 화면(해상도/앱/시계/움직이는 요소)을 그리고
- 루프백 주소 127.0.0.x 를 출발지 IP로 사용해 서버가 각각을 별도 PC(IP)로 인식하게 한다.

사용 예 (서버와 같은 PC에서):
    python tools/virtual_pcs.py --count 12
    python tools/virtual_pcs.py --count 16 --first-ip 127.0.0.101 --token XXXX

관리자 웹 > PC 관리에서 127.0.0.x 를 등록하면 화면이 모니터링 화면에 나타난다.
(서버 data/config.json 이 있으면 토큰은 자동으로 읽는다)
"""
from __future__ import annotations

import argparse
import colorsys
import functools
import hashlib
import io
import ipaddress
import json
import math
import queue
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))

import screen_client as sc  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

RESOLUTIONS = [(1920, 1080), (2560, 1440), (1366, 768), (1920, 1200), (3440, 1440), (1280, 1024), (1600, 900)]
APPS = ["문서 편집", "웹 브라우저", "코드 편집기", "스프레드시트", "동영상 재생", "터미널", "메신저", "그래픽 디자인"]


@functools.lru_cache(maxsize=64)
def font(size: int):
    for name in ("malgun.ttf", "malgunbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


class SyntheticCapturer:
    """Capturer.grab 과 같은 인터페이스로 가상 화면을 만든다."""

    def __init__(self, idx: int, ip: str, size: tuple[int, int], app: str, idle: bool = False):
        self.idx, self.ip, self.size, self.app, self.idle = idx, ip, size, app, idle
        hue = (idx * 0.137) % 1.0
        self.c1 = tuple(int(v * 255) for v in colorsys.hsv_to_rgb(hue, 0.55, 0.55))
        self.c2 = tuple(int(v * 255) for v in colorsys.hsv_to_rgb((hue + 0.12) % 1, 0.65, 0.25))
        self._base: dict[int, Image.Image] = {}
        self.frame_no = 0

    def base(self, w: int, h: int) -> Image.Image:
        if w in self._base:
            return self._base[w]
        s = w / self.size[0]
        img = Image.new("RGB", (w, h), self.c2)
        d = ImageDraw.Draw(img)
        for y in range(0, h, 4):  # 바탕화면 그라데이션
            t = y / h
            col = tuple(int(self.c1[i] * (1 - t) + self.c2[i] * t) for i in range(3))
            d.rectangle((0, y, w, y + 4), fill=col)
        # 작업 표시줄
        tb = int(48 * s)
        d.rectangle((0, h - tb, w, h), fill=(24, 26, 32))
        for i in range(6):
            x = int(w / 2 - 3 * tb + i * tb)
            d.rounded_rectangle((x + 8 * s, h - tb + 8 * s, x + tb - 8 * s, h - 8 * s), radius=int(4 * s) or 1,
                                fill=(60 + i * 20, 120, 200 - i * 15))
        # 바탕화면 아이콘
        for i in range(4):
            y = int(30 * s + i * 100 * s)
            d.rounded_rectangle((int(24 * s), y, int(84 * s), y + int(60 * s)), radius=int(6 * s) or 1, fill=(235, 235, 240))
        # 앱 창
        wx0, wy0, wx1, wy1 = int(w * .14), int(h * .08), int(w * .86), int(h - tb - h * .06)
        d.rectangle((wx0 + 6, wy0 + 6, wx1 + 6, wy1 + 6), fill=(0, 0, 0))
        d.rectangle((wx0, wy0, wx1, wy1), fill=(250, 250, 252))
        d.rectangle((wx0, wy0, wx1, wy0 + int(36 * s)), fill=(32, 36, 44))
        d.text((wx0 + int(14 * s), wy0 + int(7 * s)), f"{self.app} - VPC-{self.idx:02d}", font=font(max(10, int(18 * s))),
               fill=(230, 230, 235))
        for i, col in enumerate([(237, 106, 94), (245, 191, 79), (98, 197, 84)]):
            cx = wx1 - int((24 + 26 * i) * s)
            d.ellipse((cx - 7 * s, wy0 + 11 * s, cx + 7 * s, wy0 + 25 * s), fill=col)
        # 본문 (앱마다 다른 모양)
        top = wy0 + int(60 * s)
        if self.app in ("스프레드시트",):
            for r in range(14):
                for c in range(8):
                    x = wx0 + int(20 * s) + c * (wx1 - wx0 - int(40 * s)) // 8
                    y = top + r * int(34 * s)
                    d.rectangle((x, y, x + (wx1 - wx0) // 8 - 4, y + int(30 * s)), outline=(210, 214, 222))
        elif self.app in ("코드 편집기", "터미널"):
            bg = (30, 32, 38) if self.app == "코드 편집기" else (12, 12, 12)
            d.rectangle((wx0, wy0 + int(36 * s), wx1, wy1), fill=bg)
            for r in range(22):
                ln = (r * 37 + self.idx * 11) % 60 + 15
                d.rectangle((wx0 + int(30 * s), top + r * int(26 * s), wx0 + int(30 * s) + int(ln * 9 * s),
                             top + r * int(26 * s) + int(12 * s)),
                            fill=[(86, 156, 214), (206, 145, 120), (78, 201, 176), (180, 180, 180)][r % 4])
        else:
            for r in range(16):
                ln = (r * 53 + self.idx * 7) % 50 + 40
                d.rectangle((wx0 + int(40 * s), top + r * int(32 * s), wx0 + int(40 * s) + int(ln * 11 * s),
                             top + r * int(32 * s) + int(12 * s)), fill=(200, 204, 212))
        # 큰 식별 라벨
        d.text((wx0 + int(40 * s), wy1 - int(150 * s)), f"VPC-{self.idx:02d}", font=font(max(14, int(80 * s))),
               fill=(40, 90, 200))
        d.text((wx0 + int(44 * s), wy1 - int(55 * s)), f"{self.ip}  ·  {self.size[0]}×{self.size[1]}",
               font=font(max(10, int(26 * s))), fill=(90, 96, 110))
        self._base[w] = img
        return img

    def grab(self, max_width: int, quality: int, last_digest, force: bool, want_thumb: bool = False):
        W, H = self.size
        w = min(W, max_width)
        h = max(1, round(H * w / W))
        now = time.time()
        tick = int(now / 60) if self.idle else int(now * 4)  # idle PC 는 1분에 한 번만 변함
        digest = hashlib.blake2b(f"{tick}:{w}".encode(), digest_size=16).digest()
        if digest == last_digest and not force:
            return digest, None, self.size, None
        s = w / W
        img = self.base(w, h).copy()
        d = ImageDraw.Draw(img)
        # 시계
        d.text((w - int(120 * s), h - int(38 * s)), time.strftime("%H:%M:%S"), font=font(max(9, int(18 * s))),
               fill=(235, 235, 235))
        if not self.idle:
            # 움직이는 요소 (마우스 커서 / 재생 중인 영상 등)
            t = now * (1.6 + self.idx * 0.1)
            if self.app == "동영상 재생":
                cx, cy = w * (0.5 + 0.25 * math.sin(t)), h * (0.45 + 0.2 * math.cos(t * 1.3))
                d.ellipse((cx - 60 * s, cy - 60 * s, cx + 60 * s, cy + 60 * s), fill=(230, 60, 90))
            cx, cy = w * (0.5 + 0.35 * math.sin(t * 0.7)), h * (0.5 + 0.3 * math.sin(t * 1.1))
            d.polygon([(cx, cy), (cx, cy + 26 * s), (cx + 7 * s, cy + 19 * s), (cx + 18 * s, cy + 19 * s)],
                      fill=(255, 255, 255), outline=(0, 0, 0))
        self.frame_no += 1
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        thumb = None
        if want_thumb:
            thumb = img.copy()
            thumb.thumbnail((360, 200))
        return digest, buf.getvalue(), self.size, thumb


class VirtualWorker(sc.Worker):
    def __init__(self, idx: int, ip: str, server: str, token: str, events: queue.Queue, idle: bool = False):
        super().__init__(server, token, events)
        self.idx, self.ip = idx, ip
        size = RESOLUTIONS[idx % len(RESOLUTIONS)]
        app = APPS[idx % len(APPS)]
        self.capturer = SyntheticCapturer(idx, ip, size, "웹 브라우저" if idle else app, idle)
        self.connect_kwargs = {"local_addr": (ip, 0)}
        self.status: tuple[str, str] = ("idle", "")

    def hello_info(self) -> dict:
        return {"hostname": f"VPC-{self.idx:02d}", "user": f"user{self.idx:02d}", "os": "Windows 11 Pro (가상 PC)"}

    def primary_size(self) -> tuple[int, int]:
        return self.capturer.size

    def emit(self, kind: str, **kw) -> None:
        if kind == "status":
            self.status = (kw["state"], kw["text"])
        super().emit(kind, vidx=self.idx, vip=self.ip, **kw)


def ip_range(first: str, count: int) -> list[str]:
    base = ipaddress.ip_address(first)
    return [str(base + i) for i in range(count)]


def default_token() -> str:
    try:
        return json.loads((ROOT / "server" / "data" / "config.json").read_text("utf-8"))["agent_token"]
    except (OSError, ValueError, KeyError):
        return ""


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(description="Screen Monitor 가상 PC 시뮬레이터")
    p.add_argument("--server", default="http://127.0.0.1:8000")
    p.add_argument("--token", default=default_token())
    p.add_argument("--count", type=int, default=12)
    p.add_argument("--first-ip", default="127.0.0.101")
    p.add_argument("--duration", type=float, default=0, help="초 단위 실행 시간 (0=Ctrl+C 까지)")
    args = p.parse_args()
    if not args.token:
        p.error("--token 이 필요합니다")

    events: queue.Queue = queue.Queue()
    workers = []
    for i, ip in enumerate(ip_range(args.first_ip, args.count), start=1):
        w = VirtualWorker(i, ip, args.server, args.token, events)
        w.start()
        workers.append(w)
    print(f"가상 PC {len(workers)}대 시작: {workers[0].ip} ~ {workers[-1].ip}")
    last: dict[int, str] = {}
    end = time.time() + args.duration if args.duration else float("inf")
    try:
        while time.time() < end:
            try:
                kind, kw = events.get(timeout=0.5)
            except queue.Empty:
                continue
            if kind == "status" and last.get(kw["vidx"]) != kw["text"]:
                last[kw["vidx"]] = kw["text"]
                print(f"  VPC-{kw['vidx']:02d} {kw['vip']:<15} {kw['text']}")
    except KeyboardInterrupt:
        pass
    for w in workers:
        w.stop()
    for w in workers:
        w.join(3)
    print("종료")


if __name__ == "__main__":
    main()
