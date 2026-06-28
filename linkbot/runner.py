# linkbot/runner.py — 봇 워치독 (파이썬). bot.py를 계속 살려둠: 종료/크래시 시 5초 후 자동 재시작.
#   스케줄 작업이 이 파일을 로그인 시 실행 → 이게 bot.py를 관리.
import subprocess
import time

PY = r"D:\Study\DIscordBot\venv\Scripts\pythonw.exe"
BOT = r"D:\Study\DIscordBot\AutoLinkBot\linkbot\bot.py"
WD = r"D:\Study\DIscordBot\AutoLinkBot\linkbot"

while True:
    try:
        subprocess.run([PY, BOT], cwd=WD)
    except Exception:
        pass
    time.sleep(5)
