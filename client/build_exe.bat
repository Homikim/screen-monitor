@echo off
REM Screen Monitor 클라이언트를 단일 exe 로 빌드 (클라이언트 PC 에 Python 설치 불필요)
chcp 65001 >nul
cd /d %~dp0
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name ScreenMonitorClient screen_client.py
echo.
echo 빌드 완료: %~dp0dist\ScreenMonitorClient.exe
