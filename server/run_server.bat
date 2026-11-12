@echo off
chcp 65001 >nul
cd /d %~dp0
python -m pip install -q -r requirements.txt
python app.py --host 0.0.0.0 --port 8000
pause
