# linkbot/runner.py — 봇 워치독 (파이썬). bot.py를 계속 살려둠: 종료/크래시 시 5초 후 자동 재시작.
#   스케줄 작업이 이 파일을 로그인 시 실행 → 이게 bot.py를 관리.
import os
import subprocess
import sys
import time

_VENV_PYW = r"D:\Study\DIscordBot\venv\Scripts\pythonw.exe"
PY = _VENV_PYW if os.path.exists(_VENV_PYW) else sys.executable   # 다른 PC/경로면 이 러너를 띄운 파이썬
WD = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.join(WD, "bot.py")

while True:
    try:
        subprocess.run([PY, BOT], cwd=WD)
    except Exception:
        pass
    time.sleep(5)
