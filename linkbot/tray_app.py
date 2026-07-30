# linkbot/tray_app.py — 시스템 트레이에서 봇을 관리하는 런처 (콘솔창 없음)
#   실행: pythonw.exe tray_app.py  (또는 LinkBot.vbs 더블클릭)
#   기능: 트레이 아이콘 상주 + 봇 워치독(크래시/종료 시 자동 재시작) + 우클릭 메뉴
#         (상태 표시 / 로그 열기 / 재시작 / 종료). 콘솔이 필요 없다.
#   단일 인스턴스: 고정 포트 바인딩으로 중복 실행 차단.
import os
import sys
import time
import socket
import threading
import subprocess

import pystray
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
PYW = r"D:\Study\DIscordBot\venv\Scripts\pythonw.exe"
BOT = os.path.join(HERE, "bot.py")
LOG = os.path.join(HERE, "linkbot.log")
_LOCK_PORT = 58471                      # 단일 인스턴스 가드용 (임의 고정 포트)

# CREATE_NO_WINDOW: 자식 bot.py도 콘솔창 없이 (pythonw + 이 플래그 이중 안전장치)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class BotManager:
    """bot.py 서브프로세스를 살려두는 워치독. 종료 요청 전까지 크래시 시 5초 후 재시작."""

    def __init__(self):
        self._proc = None
        self._stop = threading.Event()          # set되면 워치독 종료(=앱 종료)
        self._restart = threading.Event()        # set되면 현재 프로세스 죽이고 재시작
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _spawn(self):
        with self._lock:
            self._proc = subprocess.Popen([PYW, BOT], cwd=HERE, creationflags=_NO_WINDOW)

    def _run(self):
        while not self._stop.is_set():
            self._restart.clear()
            try:
                self._spawn()
            except Exception:
                time.sleep(5)
                continue
            # 프로세스 종료 or 재시작 요청까지 폴링
            while True:
                if self._proc.poll() is not None:       # 봇이 스스로 종료/크래시
                    break
                if self._stop.is_set() or self._restart.is_set():
                    self._kill()
                    break
                time.sleep(1)
            if self._stop.is_set():
                break
            if not self._restart.is_set():
                time.sleep(5)                            # 크래시 재시작 백오프(수동 재시작은 즉시)

    def _kill(self):
        with self._lock:
            p = self._proc
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=8)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def is_running(self):
        with self._lock:
            p = self._proc
        return bool(p and p.poll() is None)

    def restart(self):
        self._restart.set()

    def shutdown(self):
        self._stop.set()
        self._kill()


def _make_icon(running):
    """상태색 원형 아이콘 (초록=가동, 회색=중지)."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (46, 160, 67, 255) if running else (140, 140, 140, 255)
    d.ellipse((8, 8, 56, 56), fill=color)
    d.ellipse((8, 8, 56, 56), outline=(255, 255, 255, 90), width=2)
    return img


def main():
    # 단일 인스턴스 가드 — 이미 떠 있으면 조용히 종료
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind(("127.0.0.1", _LOCK_PORT))
    except OSError:
        return                                          # 이미 실행 중
    guard.listen(1)

    mgr = BotManager()
    mgr.start()

    def status_text(_item):
        return "상태: 가동 중 🟢" if mgr.is_running() else "상태: 중지됨 ⚪"

    def on_open_log(_icon, _item):
        try:
            os.startfile(LOG)                           # Windows 기본 편집기로 로그 열기
        except Exception:
            pass

    def on_restart(_icon, _item):
        mgr.restart()

    def on_quit(icon, _item):
        mgr.shutdown()
        icon.stop()

    icon = pystray.Icon(
        "lookp_linkbot",
        icon=_make_icon(True),
        title="LooKP LinkBot",
        menu=pystray.Menu(
            pystray.MenuItem(status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("로그 열기", on_open_log),
            pystray.MenuItem("재시작", on_restart),
            pystray.MenuItem("종료", on_quit),
        ),
    )

    def refresh():
        """아이콘 색/툴팁/메뉴를 상태에 맞춰 주기적으로 갱신."""
        last = None
        while icon.visible or last is None:
            run = mgr.is_running()
            if run != last:
                icon.icon = _make_icon(run)
                icon.title = "LooKP LinkBot — 가동 중" if run else "LooKP LinkBot — 중지됨"
                try:
                    icon.update_menu()
                except Exception:
                    pass
                last = run
            time.sleep(2)

    threading.Thread(target=refresh, daemon=True).start()
    icon.run()                                          # 블로킹 (종료 메뉴 시 반환)


if __name__ == "__main__":
    main()
