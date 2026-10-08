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

    # Tự động kiểm tra Sheet (checkbox + số phút), dùng chung config.json với giao diện web
    cfg0 = webapp.load_config()
    auto_var = tk.BooleanVar(value=bool(cfg0.get("auto_check")))
    min_var = tk.StringVar(value=str(cfg0.get("auto_interval") or 5))
    box = ttk.LabelFrame(frm, text="Tự động kiểm tra Sheet", padding=(10, 6))
    box.grid(column=0, row=4, columnspan=2, sticky="ew", pady=(12, 0))

    def save_auto(*_):
        cfg = webapp.load_config()
        cfg["auto_check"] = bool(auto_var.get())
        try:
            cfg["auto_interval"] = max(1, int(min_var.get()))
        except ValueError:
            cfg["auto_interval"] = 5
            min_var.set("5")
        if cfg["auto_check"]:
            webapp.AUTO["last_tick"] = 0.0        # vừa bật -> kiểm tra ngay
        webapp.save_config(cfg)

    ttk.Checkbutton(box, text="Bật: cứ mỗi", variable=auto_var, command=save_auto).grid(column=0, row=0, sticky="w")
    sp = ttk.Spinbox(box, from_=1, to=1440, width=4, textvariable=min_var, command=save_auto)
    sp.grid(column=1, row=0, padx=4)
    sp.bind("<FocusOut>", save_auto)
    sp.bind("<Return>", save_auto)
    ttk.Label(box, text="phút kiểm tra Sheet, có bài Chờ đăng thì tự đăng").grid(column=2, row=0, sticky="w")

    # Cách đăng (2 ô chọn, chỉ chọn 1): lên lịch trên Facebook / tới giờ mới đăng
    mode_var = tk.StringVar(value=webapp.post_mode_of(cfg0))

    def save_mode():
        cfg = webapp.load_config()
        if cfg.get("post_mode") != mode_var.get():
            cfg["post_mode"] = mode_var.get()
            webapp.AUTO["last_tick"] = 0.0    # đổi cách đăng -> kiểm tra lại ngay
            webapp.save_config(cfg)

    ttk.Label(box, text="Cách đăng khi tới lượt:").grid(column=0, row=1, columnspan=3, sticky="w", pady=(8, 0))
    ttk.Radiobutton(box, text="Lên lịch trên Facebook – đưa bài lên ngay, Facebook tự phát đúng ngày giờ trong Sheet",
                    variable=mode_var, value="schedule", command=save_mode).grid(column=0, row=2, columnspan=3, sticky="w")
    ttk.Radiobutton(box, text="Tới giờ mới đăng – chờ đúng ngày giờ trong Sheet rồi bấm 'Chia sẻ ngay'",
                    variable=mode_var, value="due", command=save_mode).grid(column=0, row=3, columnspan=3, sticky="w")
    ttk.Label(box, text="(Tới giờ mới đăng: máy và GPM Login phải đang bật lúc đó; bài lên trong vài phút sau giờ ghi trong Sheet.)",
              foreground="#6b7385", wraplength=480, justify="left").grid(column=0, row=4, columnspan=3, sticky="w")
    auto_info = ttk.Label(box, text="Chưa kiểm tra lần nào", foreground="#6b7385", wraplength=480, justify="left")
    auto_info.grid(column=0, row=5, columnspan=3, sticky="w", pady=(6, 0))

    def refresh_auto():
        a = webapp.AUTO
        cfg = webapp.load_config()
        if bool(cfg.get("auto_check")) != auto_var.get():     # đổi từ giao diện web
            auto_var.set(bool(cfg.get("auto_check")))
        if webapp.post_mode_of(cfg) != mode_var.get():
            mode_var.set(webapp.post_mode_of(cfg))
        txt = (f"Lần kiểm tra gần nhất {a['last']}: {a['result']}" if a["last"] else "Chưa kiểm tra lần nào")
        if cfg.get("auto_check") and a.get("next"):
            txt += f" · lần tới {a['next']}"
        auto_info.config(text=txt)
        root.after(5000, refresh_auto)
    root.after(1000, refresh_auto)

    ttk.Label(frm, text="Bấm X chỉ ẩn xuống khay cạnh đồng hồ; bấm đúp icon ở khay để mở lại.\n"
                        "Các bài đã lên lịch nằm trên Facebook, tắt phần mềm vẫn tự đăng.",
              foreground="#6b7385", justify="center").grid(column=0, row=5, columnspan=2, pady=(12, 0))

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
