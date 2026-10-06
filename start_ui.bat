@echo off
rem Mo giao dien web cua bo cong cu dang Reels (http://127.0.0.1:8765)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
start "" "http://127.0.0.1:8765"
python app.py
pause
