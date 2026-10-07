@echo off
title Dang Reels GPM - server (dong cua so nay = tat phan mem)
rem Mo giao dien web cua bo cong cu dang Reels (http://127.0.0.1:8765)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
netstat -ano | find ":8765 " | find "LISTENING" >nul
if %errorlevel%==0 (
  echo Giao dien dang chay san, mo trinh duyet...
  start "" "http://127.0.0.1:8765"
  exit /b 0
)
start "" "http://127.0.0.1:8765"
python app.py
