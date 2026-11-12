# Screen Monitor

Collects the **primary monitor screen** of each client PC on the network and shows them in the web browser as a **CCTV-wall style** monitoring screen.

```
[Client PC 1] ScreenMonitorClient ─┐
[Client PC 2] ScreenMonitorClient ─┼─ WebSocket ─▶ [Server app.py :8000] ─▶ admin browser (login)
[Client PC N] ScreenMonitorClient ─┘   (JPEG frames)      (registered IPs only)
```

> 📘 **Korean user guide (사용 가이드)**: open `http://<server IP>:8000/guide` (no login needed), or the **?** button in the dashboard.
> Source: `server/static/guide.html` — it has a print / save-as-PDF button.

## Layout

| Path | Purpose |
|---|---|
| `server/app.py` | FastAPI server: login, PC (IP) registration, frame relay |
| `server/static/` | Login page and CCTV-wall dashboard |
| `server/data/` | DB, client access token, initial admin password (created automatically) |
| `client/screen_client.py` | Client desktop app (Tkinter GUI) |
| `client/build_exe.bat` | Builds the client as a single exe (`dist/ScreenMonitorClient.exe`) |
| `tools/virtual_pcs.py` | Virtual PC simulator (multiple clients with different IPs and screens) |
| `tools/verify_virtual_pcs.py` | Automated verification of the whole flow with virtual PCs |

## 1. Start the server (admin PC)

```bat
cd server
run_server.bat
```

On first run, the console prints the **admin account** (`admin` / random password) and the **client access token**.
The password is also saved in `server/data/initial_admin_password.txt`, and that file is deleted when you change the password in the web UI.

- Admin web: `http://<server IP>:8000`
- Allow inbound TCP port 8000 in Windows Firewall
- Reset the password: `python app.py set-password admin <new password>`
- Show the token: `python app.py show-token`

> ⚠️ If the server PC has a public IP, restrict port 8000 in the firewall to the internal network range (e.g. 192.168.0.0/24).

## 2. Register the PC (IP) to monitor (admin web)

1. Log in, then open **PC 관리** (PC management) at the top right.
2. Enter the client PC's **IP + display name** and register it.
   - When a client app that isn't registered yet tries to connect, it appears in the **등록 대기** (pending registration) list, and you can register it with one click.
3. Copy the **server address / access token** from the **클라이언트 앱 연결 정보** (client app connection info) section at the bottom of the drawer and pass them to each PC.

Screens are collected only from registered IPs. When you delete an IP, its connection is closed immediately.

## 3. Run the client app (each client PC)

- With the exe: copy `ScreenMonitorClient.exe` to each PC and run it.
- With Python: `pip install -r client/requirements.txt`, then `python client/screen_client.py`

Enter the **server address** (`http://192.168.x.x:8000`) and the **access token**, then click **공유 시작** (start sharing).
- **프로그램 실행 시 자동으로 공유 시작** (start sharing automatically on launch) / **Windows 로그인 시 자동 실행** (run at Windows login) options
- Status display: 화면 공유 중 (sharing) / 등록 대기 (pending registration; shows this PC's IP) / 토큰 오류 (token error) / 연결 끊김 (disconnected; reconnects automatically)
- The window title shows `● … 공유 중` (sharing), so the user can always see that their screen is being shared.
- Closing the window gives a choice: stop sharing and quit, or minimize.

- **저부하 모드** (low-load mode): capture one frame every N seconds (5 s – 10 min) instead of streaming, at below-normal process priority. Applies immediately while sharing; the longer of this and the admin setting wins, and the admin's enlarged view does not switch it to live.

Command-line options: `--server http://… --token … --connect --minimized --low-load N` (`N` seconds, `0` = off)

> If an unsigned exe is blocked by Windows **Smart App Control / application control policy**, you need to sign the exe with a code-signing certificate, or have the organization's administrator allow it in the policy.

## Monitoring screen

- **자동** (auto, fits the window with no scrolling) or a 2–6 column grid, **채우기 / 전체** (fill / full) view modes, search by name/IP/host
- Status label at the top-left of each tile (green: live, yellow: no response for 15 s or more, red: offline)
- **Click a tile → enlarged view**: high quality, ~8 fps (thumbnails are 1 fps), ←/→ to move to another PC, F for full screen, snapshot (JPG) download
- **지금 캡처** (capture now) in the enlarged view: request one fresh frame immediately, regardless of the PC's interval
- **그리드 / 슬라이드** (grid / slideshow, keys G / S): slideshow shows one PC at a time full-size and advances automatically
  - Controls: prev/next (←/→), pause/play (Space or click), dwell time 5 s – 5 min with a progress bar, thumbnail strip to jump, fullscreen (F; the control bar auto-hides after 3 s idle), open in enlarged view (Enter)
  - **오프라인 건너뛰기** (skip offline, default on) and **고화질** (HQ, default on: the PC on screen gets the enlarged-view profile; off = no extra client load)
  - The search box also filters the slideshow. Open `http://<server IP>:8000/#slide` to start in slideshow mode (e.g. for a control-room screen)
- Notifications for PC connect / disconnect / unregistered connection attempts

## Transfer mode per PC (reducing client load)

In **PC 관리 → 수정** (or the registration form), pick **실시간 스트리밍** (live, default) or **N초마다 캡처** (periodic: 5 s / 10 s / 30 s / 1 min / 5 min / 10 min).

- Periodic mode captures and sends one frame per interval and does nothing in between. Measured on a 3440×1440 PC: client CPU went from 4.8 % of one core (live) to 0.6 % (10 s interval).
- **확대 보기를 열면 실시간으로 전환** (switch to live in the enlarged view; default on). When off, opening the enlarged view only fetches one fresh frame.
- **이 전송 방식을 등록된 모든 PC에 적용** applies the mode to every registered PC. `POST /api/devices/mode {"interval": N, "ips": [...]}` does the same for selected PCs.
- Changes reach connected clients immediately. Periodic mode needs client **v1.1.0+**; older clients ignore it and keep streaming. The edit dialog warns about this.
- The "no response" (yellow) threshold becomes interval + 15 s.

## Verifying with virtual PCs

You can check the server's behavior without physical PCs. Each virtual PC uses the real client code (`Worker`) as-is, but
- connects from **its own IP (127.0.0.x)**, so the server recognizes each one as a separate PC, and
- shares a **virtual screen with a different resolution/app per PC** instead of a real screen capture.

```bat
REM Automated verification (60 scenario checks, run on the server PC while the server is running)
python tools\verify_virtual_pcs.py

REM After verification, keep the virtual PCs alive for 5 minutes to check them yourself in the browser
python tools\verify_virtual_pcs.py --hold 300

REM Demo only: start 16 virtual PCs (register 127.0.0.101~ in PC 관리 to see their screens)
python tools\virtual_pcs.py --count 16
```

What's verified: login/origin blocking · screen collection only from registered IPs · unregistered IPs → pending registration · token errors ·
enlarged view switches only that PC to high fps · offline/reconnect · registering a pending PC · deleting a registration · duplicate connections from the same IP ·
refresh of PCs whose screen doesn't change · snapshot · bandwidth · multiple admins/logout · periodic capture (admin setting, capture now,
live-on-focus on/off, bulk change) · client low-load mode · real-screen client fps and CPU (live vs periodic)

## Bandwidth / performance

| Mode | Resolution (max width) | fps | Approx. bandwidth/PC |
|---|---|---|---|
| Wall (default) | 1280px, JPEG q55 | 1 (no transmission when the screen doesn't change) | ~0.3–1 Mbps |
| Enlarged view | 1920px, JPEG q72 | 8 | ~5–8 Mbps |
| Periodic capture | 1920px, JPEG q70 | 1 frame / N s | ~0.03–0.08 Mbps at 30 s |

You can change these with `LOW_PROFILE` / `HIGH_PROFILE` / `PERIODIC_PROFILE` in `server/app.py`.
