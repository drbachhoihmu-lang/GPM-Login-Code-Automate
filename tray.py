"""
Ứng dụng khay hệ thống (giống Unikey) cho bộ công cụ đăng Reels:
- chạy server giao diện web ngầm (không có cửa sổ đen),
- icon ở khay (cạnh đồng hồ): bấm đúp / "Mở cửa sổ" -> cửa sổ nhỏ; "Mở giao diện web"; "Thoát hẳn",
- cửa sổ nhỏ có nút mở giao diện web; bấm X chỉ ẩn xuống khay, không tắt.

Chạy không hiện console:  pythonw tray.py   (shortcut ngoài Desktop đã trỏ sẵn)
Khởi động ẩn sẵn ở khay (không bật cửa sổ):  pythonw tray.py --tray
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
from PIL import Image, ImageTk            # noqa: E402

import app as webapp                      # noqa: E402

URL = f"http://127.0.0.1:{webapp.UI_PORT}"
ICON = ROOT / "assets" / "gpmreels.ico"
APP_NAME = "GPMReels"

# Bảng màu – trùng với giao diện web (static/index.html)
C = {
    "bg": "#eef2ff", "card": "#ffffff", "line": "#dfe4f3", "text": "#0f172a", "muted": "#5b6b8c",
    "navy": "#0b1437", "navy2": "#1a1050", "sub": "#a5b4fc",
    "blue": "#2563eb", "blue_d": "#1d4ed8", "violet": "#7c3aed", "green": "#22c55e", "red": "#e11d48", "red_d": "#be123c",
    "hover": "#eef2ff",
}
FONT = "Segoe UI"


def port_in_use(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ----------------------------------------------------------------- giao diện
def setup_styles(root):
    """Theme 'clam' cho phép tô màu nút/ô; định nghĩa các style dùng trong cửa sổ."""
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=C["bg"], foreground=C["text"], font=(FONT, 10), bordercolor=C["line"])
    st.configure("Bg.TFrame", background=C["bg"])
    st.configure("Card.TFrame", background=C["card"])
    st.configure("Card.TLabel", background=C["card"], foreground=C["text"])
    st.configure("CardTitle.TLabel", background=C["card"], foreground=C["text"], font=(FONT, 11, "bold"))
    st.configure("CardMuted.TLabel", background=C["card"], foreground=C["muted"], font=(FONT, 9))
    st.configure("Muted.TLabel", background=C["bg"], foreground=C["muted"], font=(FONT, 9))
    for kind, bg, act, prs, fg, bold in (
        ("Primary", C["blue"], C["blue_d"], "#1e40af", "#ffffff", True),
        ("Danger", C["red"], C["red_d"], "#9f1239", "#ffffff", True),
        ("Default", C["card"], C["hover"], "#e0e7ff", C["text"], False),
    ):
        border = C["line"] if kind == "Default" else bg
        st.configure(f"{kind}.TButton", background=bg, foreground=fg, bordercolor=border, lightcolor=bg, darkcolor=bg,
                     focuscolor=bg, padding=(14, 8), font=(FONT, 10, "bold" if bold else "normal"), relief="flat")
        st.map(f"{kind}.TButton", background=[("pressed", prs), ("active", act)],
               lightcolor=[("active", act)], darkcolor=[("active", act)])
    st.configure("Card.TCheckbutton", background=C["card"], foreground=C["text"], indicatorbackground="#ffffff",
                 indicatorforeground="#ffffff", focuscolor=C["card"])
    st.map("Card.TCheckbutton", background=[("active", C["card"])],
           indicatorbackground=[("selected", C["blue"]), ("active", "#eef2ff")], indicatorforeground=[("selected", "#ffffff")])
    st.configure("Card.TRadiobutton", background=C["card"], foreground=C["text"], indicatorbackground="#ffffff",
                 indicatorforeground="#ffffff", focuscolor=C["card"])
    st.map("Card.TRadiobutton", background=[("active", C["card"])],
           indicatorbackground=[("selected", C["blue"]), ("active", "#eef2ff")], indicatorforeground=[("selected", "#ffffff")])
    st.configure("Card.TSpinbox", fieldbackground="#ffffff", background="#ffffff", bordercolor=C["line"], lightcolor="#ffffff",
                 darkcolor="#ffffff", arrowcolor=C["blue"], arrowsize=13, padding=3)
    st.map("Card.TSpinbox", bordercolor=[("focus", C["blue"])])
    return st


def card(parent, padding=(16, 12)):
    """Thẻ trắng có viền 1px như trên web (khung ngoài màu viền, khung trong màu trắng)."""
    outer = tk.Frame(parent, bg=C["line"], bd=0)
    inner = ttk.Frame(outer, style="Card.TFrame", padding=padding)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    return outer, inner


def build_window(root, open_web, hide, quit_app):
    """Dựng toàn bộ nội dung cửa sổ nhỏ. Trả về hàm refresh để cập nhật trạng thái định kỳ."""
    root.title(APP_NAME)
    root.resizable(False, False)
    root.configure(bg=C["bg"])
    try:
        root.iconbitmap(str(ICON))
    except Exception:
        pass
    setup_styles(root)

    # ---- dải đầu: logo + tên + trạng thái server (nền xanh đậm như menu trái trên web)
    head = tk.Frame(root, bg=C["navy"], padx=18, pady=14)
    head.pack(fill="x")
    try:
        logo_img = ImageTk.PhotoImage(Image.open(ICON).convert("RGBA").resize((46, 46), Image.LANCZOS))
        logo = tk.Label(head, image=logo_img, bg=C["navy"], bd=0)
        logo.image = logo_img                 # giữ tham chiếu, không thì ảnh bị thu hồi
        logo.pack(side="left", padx=(0, 14))
    except Exception:
        pass
    htxt = tk.Frame(head, bg=C["navy"])
    htxt.pack(side="left", fill="x", expand=True)
    tk.Label(htxt, text="GPMReels", bg=C["navy"], fg="#ffffff", font=(FONT, 16, "bold"), anchor="w").pack(anchor="w")
    tk.Label(htxt, text="Đăng Reels lên Fanpage qua GPM Login", bg=C["navy"], fg=C["sub"], font=(FONT, 9), anchor="w").pack(anchor="w")
    srow = tk.Frame(htxt, bg=C["navy"])
    srow.pack(anchor="w", pady=(4, 0))
    tk.Label(srow, text="●", bg=C["navy"], fg=C["green"], font=(FONT, 9)).pack(side="left")
    tk.Label(srow, text=" Server đang chạy ngầm tại ", bg=C["navy"], fg="#c7d2fe", font=(FONT, 9)).pack(side="left")
    link = tk.Label(srow, text=URL, bg=C["navy"], fg="#93c5fd", font=(FONT, 9, "underline"), cursor="hand2")
    link.pack(side="left")
    link.bind("<Button-1>", lambda e: open_web())
    tk.Frame(root, bg=C["violet"], height=3).pack(fill="x")     # vạch màu dưới dải đầu

    body = ttk.Frame(root, style="Bg.TFrame", padding=(18, 14, 18, 12))
    body.pack(fill="both", expand=True)

    # ---- hàng nút chính
    btns = ttk.Frame(body, style="Bg.TFrame")
    btns.pack(fill="x")
    ttk.Button(btns, text="▶  Mở giao diện web", style="Primary.TButton", command=open_web).pack(side="left", padx=(0, 8))
    ttk.Button(btns, text="Mở Google Sheet ↗", style="Default.TButton",
               command=lambda: webbrowser.open(webapp.load_config().get("sheet_url") or URL)).pack(side="left", padx=(0, 8))
    ttk.Button(btns, text="⏰ Khung giờ đăng", style="Default.TButton",
               command=lambda: webbrowser.open(URL + "/#gio")).pack(side="left")

    # ---- thẻ: tự động kiểm tra Sheet (dùng chung config.json với giao diện web)
    outer, box = card(body)
    outer.pack(fill="x", pady=(14, 0))
    ttk.Label(box, text="Tự động kiểm tra Sheet", style="CardTitle.TLabel").grid(column=0, row=0, columnspan=3, sticky="w", pady=(0, 6))

    cfg0 = webapp.load_config()
    auto_var = tk.BooleanVar(value=bool(cfg0.get("auto_check")))
    min_var = tk.StringVar(value=str(cfg0.get("auto_interval") or 5))

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

    arow = ttk.Frame(box, style="Card.TFrame")
    arow.grid(column=0, row=1, columnspan=3, sticky="w")
    ttk.Checkbutton(arow, text="Bật: cứ mỗi", variable=auto_var, command=save_auto, style="Card.TCheckbutton").pack(side="left")
    sp = ttk.Spinbox(arow, from_=1, to=1440, width=4, textvariable=min_var, command=save_auto, style="Card.TSpinbox", font=(FONT, 10))
    sp.pack(side="left", padx=6)
    sp.bind("<FocusOut>", save_auto)
    sp.bind("<Return>", save_auto)
    ttk.Label(arow, text="phút kiểm tra Sheet, có bài Chờ đăng thì tự đăng", style="Card.TLabel").pack(side="left")

    # Cách đăng (2 ô chọn, chỉ chọn 1): lên lịch trên Facebook / tới giờ mới đăng
    mode_var = tk.StringVar(value=webapp.post_mode_of(cfg0))

    def save_mode():
        cfg = webapp.load_config()
        if cfg.get("post_mode") != mode_var.get():
            cfg["post_mode"] = mode_var.get()
            webapp.AUTO["last_tick"] = 0.0    # đổi cách đăng -> kiểm tra lại ngay
            webapp.save_config(cfg)

    ttk.Label(box, text="Cách đăng khi tới lượt", style="CardTitle.TLabel").grid(column=0, row=2, columnspan=3, sticky="w", pady=(12, 4))
    ttk.Radiobutton(box, text="Lên lịch trên Facebook – đưa bài lên ngay, Facebook tự phát đúng ngày giờ trong Sheet",
                    variable=mode_var, value="schedule", command=save_mode, style="Card.TRadiobutton").grid(column=0, row=3, columnspan=3, sticky="w")

    # Giới hạn ngày khi lên lịch: hôm nay chạy chỉ lên lịch tới hôm nay + N ngày (1 = tới hết ngày mai)
    hz_var = tk.StringVar(value=str(webapp.horizon_of(cfg0)))
    hz_row = ttk.Frame(box, style="Card.TFrame")
    hz_row.grid(column=0, row=4, columnspan=3, sticky="w", padx=(24, 0), pady=(2, 4))

    def save_hz(*_):
        cfg = webapp.load_config()
        try:
            v = max(0, min(60, int(hz_var.get())))
        except ValueError:
            v = 1
            hz_var.set("1")
        if v != webapp.horizon_of(cfg):
            cfg["schedule_horizon_days"] = v
            webapp.AUTO["last_tick"] = 0.0
            webapp.save_config(cfg)

    ttk.Label(hz_row, text="mỗi lần chạy chỉ lên lịch tới hôm nay +", style="CardMuted.TLabel").pack(side="left")
    hz_sp = ttk.Spinbox(hz_row, from_=0, to=60, width=3, textvariable=hz_var, command=save_hz, style="Card.TSpinbox", font=(FONT, 10))
    hz_sp.pack(side="left", padx=5)
    hz_sp.bind("<FocusOut>", save_hz)
    hz_sp.bind("<Return>", save_hz)
    ttk.Label(hz_row, text="ngày (1 = tới hết ngày mai, 0 = chỉ hôm nay)", style="CardMuted.TLabel").pack(side="left")

    ttk.Radiobutton(box, text="Tới giờ mới đăng – chờ đúng ngày giờ trong Sheet rồi bấm 'Chia sẻ ngay'",
                    variable=mode_var, value="due", command=save_mode, style="Card.TRadiobutton").grid(column=0, row=5, columnspan=3, sticky="w")
    ttk.Label(box, text="(Tới giờ mới đăng: máy và GPM Login phải đang bật lúc đó; bài lên trong vài phút sau giờ ghi trong Sheet.)",
              style="CardMuted.TLabel", wraplength=500, justify="left").grid(column=0, row=6, columnspan=3, sticky="w", padx=(24, 0))

    # dòng trạng thái lần kiểm tra gần nhất (nền nhạt)
    info_wrap = tk.Frame(box, bg=C["bg"], padx=10, pady=7)
    info_wrap.grid(column=0, row=7, columnspan=3, sticky="ew", pady=(10, 0))
    auto_info = tk.Label(info_wrap, text="Chưa kiểm tra lần nào", bg=C["bg"], fg=C["muted"], font=(FONT, 9), wraplength=490, justify="left", anchor="w")
    auto_info.pack(fill="x")

    # ---- hàng nút phụ + ghi chú
    foot = ttk.Frame(body, style="Bg.TFrame")
    foot.pack(fill="x", pady=(14, 0))
    ttk.Button(foot, text="Ẩn xuống khay", style="Default.TButton", command=hide).pack(side="left", padx=(0, 8))
    ttk.Button(foot, text="Thoát hẳn", style="Danger.TButton", command=quit_app).pack(side="left")
    ttk.Label(body, text="Bấm X chỉ ẩn xuống khay cạnh đồng hồ; bấm đúp icon ở khay để mở lại.\n"
                         "Bài đã lên lịch nằm trên Facebook, tắt phần mềm vẫn tự đăng.",
              style="Muted.TLabel", justify="left").pack(anchor="w", pady=(10, 0))

    def refresh_auto():
        a = webapp.AUTO
        cfg = webapp.load_config()
        if bool(cfg.get("auto_check")) != auto_var.get():     # đổi từ giao diện web
            auto_var.set(bool(cfg.get("auto_check")))
        if webapp.post_mode_of(cfg) != mode_var.get():
            mode_var.set(webapp.post_mode_of(cfg))
        if str(webapp.horizon_of(cfg)) != hz_var.get() and root.focus_get() is not hz_sp:
            hz_var.set(str(webapp.horizon_of(cfg)))
        txt = (f"Lần kiểm tra gần nhất {a['last']}: {a['result']}" if a["last"] else "Chưa kiểm tra lần nào")
        if cfg.get("auto_check") and a.get("next"):
            txt += f" · lần tới {a['next']}"
        auto_info.config(text=txt)
        root.after(5000, refresh_auto)

    return refresh_auto


def center(root):
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2 - 40
    root.geometry(f"+{max(x, 0)}+{max(y, 0)}")


# ----------------------------------------------------------------- main
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
    icon_ref = {"icon": None}

    def hide():
        root.withdraw()          # chỉ ẩn xuống khay, không bật thông báo góc màn hình

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
            if icon_ref["icon"]:
                icon_ref["icon"].stop()
        except Exception:
            pass
        root.after(200, lambda: os._exit(0))

    refresh_auto = build_window(root, open_web, hide, quit_app)
    root.after(1000, refresh_auto)
    root.protocol("WM_DELETE_WINDOW", hide)
    center(root)

    # 3. Icon ở khay
    menu = pystray.Menu(
        pystray.MenuItem("Mở cửa sổ", lambda: show(), default=True),
        pystray.MenuItem("Mở giao diện web", lambda: open_web()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Thoát hẳn", lambda: root.after(0, quit_app)),
    )
    icon_ref["icon"] = pystray.Icon(APP_NAME, Image.open(ICON), APP_NAME, menu)
    icon_ref["icon"].run_detached()

    if "--tray" in sys.argv:          # khởi động ẩn sẵn ở khay (vd. khi server tự khởi động lại)
        root.withdraw()
    root.mainloop()


if __name__ == "__main__":
    main()
