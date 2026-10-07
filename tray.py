"""
Ứng dụng khay hệ thống (giống Unikey) cho bộ công cụ đăng Reels:
- chạy server giao diện web ngầm (không có cửa sổ đen),
- icon ở khay (cạnh đồng hồ): bấm đúp / "Mở cửa sổ" -> cửa sổ nhỏ; "Mở giao diện web"; "Thoát hẳn",
- cửa sổ nhỏ có nút mở giao diện web; bấm X chỉ ẩn xuống khay, không tắt.

Chạy không hiện console:  pythonw tray.py   (shortcut ngoài Desktop đã trỏ sẵn)
"""
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# pythonw không có stdout/stderr -> mọi print/log (uvicorn) sẽ lỗi; chuyển vào logs/tray.log
if sys.stdout is None or sys.stderr is None:
    _log = open(ROOT / "logs" / "tray.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log

import tkinter as tk                      # noqa: E402
from tkinter import ttk                   # noqa: E402

import pystray                            # noqa: E402
import uvicorn                            # noqa: E402
from PIL import Image                     # noqa: E402

import app as webapp                      # noqa: E402

URL = f"http://127.0.0.1:{webapp.UI_PORT}"
ICON = ROOT / "assets" / "gpmreels.ico"
APP_NAME = "GPMReels"


def port_in_use(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    # Đã có bản chạy sẵn -> chỉ mở giao diện web rồi thoát (không chạy 2 bản)
    if port_in_use(webapp.UI_PORT):
        webbrowser.open(URL)
        return

    # 1. Server web chạy ngầm trong thread
    server = uvicorn.Server(uvicorn.Config(webapp.app, host="127.0.0.1", port=webapp.UI_PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()

    # 2. Cửa sổ nhỏ
    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)
    try:
        root.iconbitmap(str(ICON))
    except Exception:
        pass
    frm = ttk.Frame(root, padding=18)
    frm.grid()
    ttk.Label(frm, text="GPMReels – Đăng Reels lên Fanpage", font=("Segoe UI", 12, "bold")).grid(column=0, row=0, columnspan=2, pady=(0, 6))
    status = ttk.Label(frm, text=f"Server đang chạy ngầm tại {URL}", foreground="#1f9d55")
    status.grid(column=0, row=1, columnspan=2, pady=(0, 12))
    ttk.Button(frm, text="Mở giao diện web", width=24, command=lambda: webbrowser.open(URL)).grid(column=0, row=2, padx=4, pady=3)
    ttk.Button(frm, text="Mở Google Sheet", width=24, command=lambda: webbrowser.open(webapp.load_config().get("sheet_url") or URL)).grid(column=1, row=2, padx=4, pady=3)
    ttk.Button(frm, text="Ẩn xuống khay (X)", width=24, command=lambda: hide()).grid(column=0, row=3, padx=4, pady=3)
    ttk.Button(frm, text="Thoát hẳn", width=24, command=lambda: quit_app()).grid(column=1, row=3, padx=4, pady=3)
    ttk.Label(frm, text="Bấm X chỉ ẩn xuống khay cạnh đồng hồ; bấm đúp icon ở khay để mở lại.\n"
                        "Các bài đã lên lịch nằm trên Facebook, tắt phần mềm vẫn tự đăng.",
              foreground="#6b7385", justify="center").grid(column=0, row=4, columnspan=2, pady=(12, 0))

    icon = None
    hidden_once = {"v": False}

    def hide():
        root.withdraw()
        if icon and not hidden_once["v"]:
            hidden_once["v"] = True
            try:
                icon.notify("Vẫn chạy ở khay cạnh đồng hồ. Bấm đúp icon để mở lại.", APP_NAME)
            except Exception:
                pass

    def show():
        root.after(0, lambda: (root.deiconify(), root.lift(), root.focus_force()))

    def open_web():
        webbrowser.open(URL)

    def quit_app():
        try:
            for j in webapp.JOBS.values():
                if j["proc"].poll() is None:
                    j["proc"].kill()
        except Exception:
            pass
        server.should_exit = True
        try:
            if icon:
                icon.stop()
        except Exception:
            pass
        root.after(200, lambda: os._exit(0))

    root.protocol("WM_DELETE_WINDOW", hide)

    # Đặt cửa sổ ra giữa màn hình
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2 - 40
    root.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # 3. Icon ở khay
    menu = pystray.Menu(
        pystray.MenuItem("Mở cửa sổ", lambda: show(), default=True),
        pystray.MenuItem("Mở giao diện web", lambda: open_web()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Thoát hẳn", lambda: root.after(0, quit_app)),
    )
    icon = pystray.Icon(APP_NAME, Image.open(ICON), APP_NAME, menu)
    icon.run_detached()

    root.mainloop()


if __name__ == "__main__":
    main()
