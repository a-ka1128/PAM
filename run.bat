@echo off
chcp 65001 >nul
set PYTHONUTF8=1
title Collector Bot
cd /d "D:\Study\DIscordBot\AutoLinkBot"
echo ============================================
echo   Collector Bot starting...
echo   (Close this window to stop the bot)
echo ============================================
echo.
"D:\Study\DIscordBot\venv\Scripts\python.exe" bot.py
echo.
echo === Bot stopped. Press any key to close. ===
pause >nul
