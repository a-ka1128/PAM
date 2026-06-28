@echo off
chcp 65001 >nul
set PYTHONUTF8=1
title LooKP LinkBot (visible)
cd /d D:\Study\DIscordBot\AutoLinkBot\linkbot
:loop
echo === LooKP LinkBot starting (close this window to stop) ===
"D:\Study\DIscordBot\venv\Scripts\python.exe" bot.py
echo.
echo [watchdog] bot exited - restarting in 5s (close window or Ctrl+C to stop)
timeout /t 5 /nobreak >nul
goto loop
