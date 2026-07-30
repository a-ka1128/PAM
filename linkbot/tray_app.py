# linkbot/tray_app.py — 봇 관리 GUI (보이는 창 + 트레이 아이콘, 콘솔 없음)
#   실행: pythonw.exe tray_app.py  (또는 LinkBot.vbs 더블클릭)
#   창: 상태등(🟢/⚪) · 시작/중지/재시작 버튼 · 실시간 로그 뷰.
#       [X]로 닫으면 종료가 아니라 트레이로 숨김(봇은 계속 가동). 트레이 아이콘
#       더블클릭/‘열기’로 창 복귀, 트레이 ‘종료’로 완전 종료.
#   워치독: 봇 크래시/종료 시 5초 후 자동 재시작(‘중지’ 상태가 아닐 때만).
#   단일 인스턴스: 고정 포트 바인딩으로 중복 실행 차단.
import os
import sys
import time
import socket
import threading
import subprocess

import tkinter as tk
from tkinter import scrolledtext, font as tkfont

import pystray
from PIL import Image, ImageDraw

# exe(PyInstaller)로 묶이면 __file__은 임시 추출폴더를 가리키므로 경로가 깨진다.
#   frozen이면 실제 linkbot 폴더를 고정 사용(어차피 PYW 경로도 이 머신 고정).
if getattr(sys, "frozen", False):
    HERE = r"D:\Study\DIscordBot\AutoLinkBot\linkbot"
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
PYW = r"D:\Study\DIscordBot\venv\Scripts\pythonw.exe"
BOT = os.path.join(HERE, "bot.py")
LOG = os.path.join(HERE, "linkbot.log")
_LOCK_PORT = 58471                              # 단일 인스턴스 가드용 (임의 고정 포트)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

RUN_COLOR = "#2ea043"                            # 가동(초록)
OFF_COLOR = "#8c8c8c"                            # 중지(회색)


class BotManager:
    """bot.py 서브프로세스 워치독. 원하는 상태(_want)를 유지: 켜짐이면 살려두고,
    크래시 시 5초 후 재시작. 중지/종료는 즉시 반영. (수동 재시작은 백오프 없음)"""

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self._stop = threading.Event()          # 앱 완전 종료
        self._want = threading.Event()          # 원하는 상태: 가동
        self._want.set()
        self._restart = threading.Event()       # 즉시 재시작 요청
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            if not self._want.is_set():         # ‘중지’ 상태 → 봇 안 띄우고 대기
                self._kill()
                time.sleep(0.5)
                continue
            try:
                with self._lock:
                    self._proc = subprocess.Popen(
                        [PYW, BOT], cwd=HERE, creationflags=_NO_WINDOW)
            except Exception:
                time.sleep(5)
                continue
            self._restart.clear()
            while True:                          # 종료/중지/재시작까지 감시
                if self._proc.poll() is not None:
                    break                        # 스스로 종료/크래시
                if self._stop.is_set() or not self._want.is_set() or self._restart.is_set():
                    self._kill()
                    break
                time.sleep(1)
            if self._stop.is_set():
                break
            if self._restart.is_set():
                self._restart.clear()
                continue                         # 즉시 재시작
            if self._want.is_set():
                time.sleep(5)                    # 크래시 백오프

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

    def wants_running(self):
        return self._want.is_set()

    def set_running(self, yes):
        self._want.set() if yes else self._want.clear()

    def restart(self):
        self._want.set()
        self._restart.set()

    def shutdown(self):
        self._stop.set()
        self._kill()


def _make_icon(running):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (46, 160, 67, 255) if running else (140, 140, 140, 255)
    d.ellipse((8, 8, 56, 56), fill=color)
    d.ellipse((8, 8, 56, 56), outline=(255, 255, 255, 90), width=2)
    return img


class App:
    def __init__(self):
        self.mgr = BotManager()
        self.root = tk.Tk()
        self.root.title("LooKP LinkBot")
        self.root.geometry("620x460")
        self.root.minsize(460, 320)
        self._log_pos = 0
        self._build_ui()
        self._build_tray()
        self.root.protocol("WM_DELETE_WINDOW", self._hide)   # [X] → 트레이로 숨김
        self.mgr.start()
        self._init_log_tail()
        self._tick_status()
        self._tick_log()

    # ---------- UI ----------
    def _build_ui(self):
        top = tk.Frame(self.root, padx=12, pady=10)
        top.pack(fill="x")
        self.dot = tk.Canvas(top, width=18, height=18, highlightthickness=0)
        self.dot_id = self.dot.create_oval(3, 3, 15, 15, fill=OFF_COLOR, outline="")
        self.dot.pack(side="left")
        f = tkfont.Font(size=11, weight="bold")
        self.status_lbl = tk.Label(top, text="상태 확인 중…", font=f)
        self.status_lbl.pack(side="left", padx=8)

        btns = tk.Frame(self.root, padx=12)
        btns.pack(fill="x")
        self.btn_start = tk.Button(btns, text="시작", width=8, command=self._on_start)
        self.btn_stop = tk.Button(btns, text="중지", width=8, command=self._on_stop)
        self.btn_restart = tk.Button(btns, text="재시작", width=8, command=self._on_restart)
        self.btn_folder = tk.Button(btns, text="로그 폴더", width=9, command=self._open_folder)
        for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_folder):
            b.pack(side="left", padx=(0, 6), pady=6)

        tk.Label(self.root, text="로그 (linkbot.log)", anchor="w",
                 padx=12).pack(fill="x")
        self.logview = scrolledtext.ScrolledText(
            self.root, height=16, state="disabled", wrap="none",
            font=("Consolas", 9), bg="#101418", fg="#d7dde3", padx=6, pady=4)
        self.logview.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    # ---------- 트레이 ----------
    def _build_tray(self):
        self.icon = pystray.Icon(
            "lookp_linkbot", icon=_make_icon(True), title="LooKP LinkBot",
            menu=pystray.Menu(
                pystray.MenuItem("열기", self._tray_show, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("재시작", lambda i, it: self.root.after(0, self._on_restart)),
                pystray.MenuItem("종료", lambda i, it: self.root.after(0, self._quit)),
            ))
        self.icon.run_detached()                 # 별도 스레드에서 트레이 구동

    # ---------- 액션 ----------
    def _on_start(self):
        self.mgr.set_running(True)

    def _on_stop(self):
        self.mgr.set_running(False)

    def _on_restart(self):
        self.mgr.restart()

    def _open_folder(self):
        try:
            os.startfile(HERE)
        except Exception:
            pass

    def _hide(self):
        self.root.withdraw()                     # 창만 숨김(봇 유지)

    def _tray_show(self, _icon=None, _item=None):
        self.root.after(0, self._show)

    def _show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _quit(self):
        self.mgr.shutdown()
        try:
            self.icon.stop()
        except Exception:
            pass
        try:
            self._guard.close()
        except Exception:
            pass
        self.root.destroy()

    # ---------- 상태/로그 갱신 ----------
    def _tick_status(self):
        run = self.mgr.is_running()
        want = self.mgr.wants_running()
        self.dot.itemconfig(self.dot_id, fill=RUN_COLOR if run else OFF_COLOR)
        if run:
            txt = "가동 중"
        elif want:
            txt = "재시작 중…"
        else:
            txt = "중지됨"
        self.status_lbl.config(text=txt)
        self.btn_start.config(state=("disabled" if want else "normal"))
        self.btn_stop.config(state=("normal" if want else "disabled"))
        try:
            self.icon.icon = _make_icon(run)
            self.icon.title = f"LooKP LinkBot — {txt}"
        except Exception:
            pass
        self.root.after(1000, self._tick_status)

    def _init_log_tail(self):
        try:
            sz = os.path.getsize(LOG)
            self._log_pos = max(0, sz - 8192)    # 마지막 ~8KB만 먼저 로드
        except OSError:
            self._log_pos = 0

    def _tick_log(self):
        try:
            sz = os.path.getsize(LOG)
            if sz < self._log_pos:               # 로그 로테이트/초기화 감지
                self._log_pos = 0
                self._set_log("")
            with open(LOG, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._log_pos)
                new = f.read()
                self._log_pos = f.tell()
            if new:
                self._append_log(new)
        except FileNotFoundError:
            pass
        self.root.after(1500, self._tick_log)

    def _append_log(self, text):
        self.logview.config(state="normal")
        self.logview.insert("end", text)
        # 과도 성장 방지: 2000줄 초과분 앞에서 잘라냄
        if int(self.logview.index("end-1c").split(".")[0]) > 2000:
            self.logview.delete("1.0", "end-2000l")
        self.logview.see("end")
        self.logview.config(state="disabled")

    def _set_log(self, text):
        self.logview.config(state="normal")
        self.logview.delete("1.0", "end")
        self.logview.insert("end", text)
        self.logview.config(state="disabled")

    def run(self, guard):
        self._guard = guard
        self.root.mainloop()


def main():
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind(("127.0.0.1", _LOCK_PORT))
    except OSError:
        return                                   # 이미 실행 중
    guard.listen(1)
    App().run(guard)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # --noconsole(exe)에선 stderr가 없으니 파일로 남겨 진단 가능하게
        import traceback
        try:
            with open(os.path.join(HERE, "tray_error.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S\n"))
                traceback.print_exc(file=f)
                f.write("\n")
        except Exception:
            pass
        raise
