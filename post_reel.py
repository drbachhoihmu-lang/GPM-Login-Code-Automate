"""
Đăng / lên lịch Thước phim (Reels) lên Fanpage qua Meta Business Suite,
chạy trên đúng profile GPM Login (Playwright kết nối qua GPM Local API).

Ví dụ:
  python post_reel.py --video "D:\\videos\\clip1.mp4" --caption "abc #thacsianhduc" --schedule "07/10/2026 12:00"
  python post_reel.py --video clip.mp4 --caption "..." --now                       # đăng ngay
  python post_reel.py --video clip.mp4 --caption "..." --schedule "2026-10-07 12:00" --dry-run
  python post_reel.py --caption "..." --schedule "07/10/2026 12:00" --resume      # trình soạn đã có video + mô tả
"""
import argparse
import hashlib
import html
import json
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Console Windows mặc định cp1252 -> in tiếng Việt sẽ lỗi
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ----------------------------------------------------------------- cấu hình
GPM_API = "http://127.0.0.1:9495/api/v1"
PROFILE_ID = "3fcc3942-0973-4809-a4a1-b7b1c89d4468"
PAGE_ASSET_ID = "1105165602680340"
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
DL_DIR = Path(__file__).resolve().parent / "downloads"     # video tải từ Google Drive
DRIVE_DL = "https://drive.usercontent.google.com/download?id={id}&export=download&confirm=t"
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi")

COMPOSER_URL = ("https://business.facebook.com/latest/reels_composer/"
                "?ref=biz_web_home_create_reel&asset_id={asset}&context_ref=HOME")
SCHEDULED_URL = "https://business.facebook.com/latest/posts/scheduled_posts?asset_id={asset}"
PUBLISHED_URL = "https://business.facebook.com/latest/posts/published_posts?asset_id={asset}"
DESC_BOX_NAME = "Hãy viết vào ô hộp thoại để thêm văn bản vào bài viết."
STEPS = ("Tạo", "Chỉnh sửa", "Chia sẻ")

# Bản xem trước là <canvas> vẽ từ một <video> không nằm trong DOM, nút tạm dừng chỉ là hình vẽ.
# Gọi pause() KHÔNG ăn: trình phát của FB gọi play() liên tục rồi reset về 0 và vẽ lại.
# Cách ăn: để video "phát" nhưng playbackRate = 0 + muted -> khung hình đứng yên, im tiếng;
# chặn luôn setter playbackRate/muted để FB không đặt lại trong lúc đang đóng băng.


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ----------------------------------------------------------------- GPM Login
def gpm_profiles():
    """[(id, tên)] các profile trong GPM Login."""
    r = requests.get(f"{GPM_API}/profiles?per_page=500", timeout=30).json()
    return [(p["id"], p["name"]) for p in r["data"]["data"]]


def resolve_profile_id(want):
    """Cột 'Profile' trong Sheet / tham số --profile -> id profile GPM. Nhận id đầy đủ, id rút gọn
    (8 ký tự đầu) hoặc một phần tên profile (không phân biệt hoa thường, dấu)."""
    want = (want or "").strip()
    if not want:
        return PROFILE_ID
    profiles = gpm_profiles()
    for pid, name in profiles:
        if pid == want or pid.startswith(want.lower()):
            return pid
    hits = [(pid, name) for pid, name in profiles if norm_text(want) in norm_text(name)]
    if len(hits) == 1:
        return hits[0][0]
    if not hits:
        raise RuntimeError(f"Không có profile GPM nào khớp '{want}'. Hiện có: " + ", ".join(n for _, n in profiles))
    raise RuntimeError(f"'{want}' khớp nhiều profile: " + ", ".join(n for _, n in hits) + " – ghi rõ hơn")


# Lỗi GPM trả về khi mở profile mà thử lại cũng vô ích -> dừng ngay, báo nguyên nhân + cách xử lý (ngoài phạm vi code)
GPM_START_HINTS = {
    "ProxyCheckFailed":
        "proxy gắn với profile không hoạt động (GPM kiểm tra proxy thất bại). Vào GPM Login → sửa profile → "
        "thay / gia hạn proxy hoặc bỏ proxy, rồi chạy lại. (Bấm 'Kiểm tra GPM' trên web để xem proxy nào chết.)",
    "Sequence contains no matching element":
        "nhân trình duyệt ghi trong profile (vd Chrome 152.0.7977.140) KHÔNG có trong danh sách nhân mà bản GPM đang chạy "
        "hỗ trợ – hay gặp sau khi cài lại GPM bằng bộ cài cũ hơn hoặc bấm bỏ qua cập nhật. Cách sửa: cập nhật GPM lên bản "
        "mới (GPM → Cài đặt → Kiểm tra cập nhật, hoặc tải bộ cài mới ở gpmlogin.com), hoặc vào GPM → sửa profile → đổi "
        "'Browser version' sang bản GPM đang hỗ trợ. (Bấm 'Kiểm tra GPM' trên web để xem profile nào lệch.)",
    "PROFILE_NOT_FOUND": "GPM không có profile với id này (profile đã xoá / đổi?). Bấm Tải lại danh sách profile.",
}


def gpm_start(profile_id, retries=8):
    """Mở profile qua GPM Local API, trả về cổng remote debugging.
    Thứ tự: mở có bỏ qua kiểm tra proxy (nhanh) -> nếu GPM văng 'Sequence contains no matching element' thì mở lại
    CÓ kiểm tra proxy để lộ nguyên nhân thật (vd ProxyCheckFailed) -> lỗi không thể tự xử lý thì dừng ngay, báo rõ."""
    # Nếu lần trước script đã mở profile và cửa sổ vẫn còn -> dùng lại cổng cũ
    port_file = LOG_DIR / f"port_{profile_id[:8]}.txt"
    if port_file.exists():
        try:
            old = int(port_file.read_text().strip())
            requests.get(f"http://127.0.0.1:{old}/json/version", timeout=3).raise_for_status()
            log(f"GPM: profile đang mở sẵn, dùng lại debug port {old}")
            return old
        except Exception:
            pass
    base = f"{GPM_API}/profiles/start/{profile_id}?window_size=1400,900&window_pos=0,0"
    skip_proxy = True
    last = ""
    for i in range(retries):
        try:
            r = requests.get(base + ("&skip_proxy_check=true" if skip_proxy else ""), timeout=150).json()
        except Exception as e:
            raise RuntimeError(f"Không gọi được GPM Local API ({GPM_API}) – GPM Login có đang chạy không? {str(e)[:80]}")
        if r.get("success"):
            port = r["data"]["remote_debugging_port"]
            port_file.write_text(str(port))
            log(f"GPM: profile đã mở, debug port {port}")
            return port
        msg = last = r.get("message", "") or "lỗi không rõ"
        if msg == "ProfileInUse":
            # Trình duyệt đang mở nhưng không có cổng debug -> đóng rồi mở lại
            log(f"GPM: profile đang mở tay (ProfileInUse), đóng rồi mở lại {i + 1}/{retries}")
            if i == 0:
                requests.get(f"{GPM_API}/profiles/stop/{profile_id}", timeout=60)
            elif i == 2:
                kill_profile_browser(profile_id)
            time.sleep(5)
            continue
        if msg == "Sequence contains no matching element" and skip_proxy:
            log("GPM: lỗi nội bộ khi mở (bỏ qua kiểm tra proxy) -> mở lại CÓ kiểm tra proxy để tìm nguyên nhân (chờ ~20s)")
            skip_proxy = False
            continue
        if msg in GPM_START_HINTS:
            raise RuntimeError(f"GPM không mở được profile: {msg} – {GPM_START_HINTS[msg]}")
        log(f"GPM: start thất bại ({msg}), thử lại {i + 1}/{min(retries, 3)}")
        if i >= 2:
            break
        time.sleep(5)
    raise RuntimeError(f"Không mở được profile GPM qua API ({last})")


def gpm_stop(profile_id):
    try:
        requests.get(f"{GPM_API}/profiles/stop/{profile_id}", timeout=60)
    except Exception:
        pass


# Chạy lệnh con mà không bật cửa sổ console (khi phần mềm chạy ở khay bằng pythonw, mỗi lần gọi
# PowerShell bình thường sẽ nháy một cửa sổ cmd lên rồi tắt).
HIDDEN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
if hasattr(subprocess, "STARTUPINFO"):
    _si = subprocess.STARTUPINFO()
    _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _si.wShowWindow = 0            # SW_HIDE
    HIDDEN["startupinfo"] = _si


def run_hidden(cmd, **kw):
    """subprocess.run không hiện cửa sổ."""
    return subprocess.run(cmd, **HIDDEN, **kw)


def kill_profile_browser(profile_id):
    """GPM stop đôi khi không đóng được cửa sổ mở bằng tay -> tự tắt chrome của profile."""
    ps = ("Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
          f"Where-Object {{ $_.CommandLine -match '{profile_id}' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    run_hidden(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    log("GPM: đã tắt tiến trình chrome của profile")
    time.sleep(4)


GPM_DATA_DIR = Path(os.environ.get("APPDATA", "")) / "GPMLoginGlobal"   # dữ liệu GPM – chỉ đọc để chẩn đoán


def gpm_supported_cores():
    """Nhân trình duyệt mà bản GPM đang chạy hỗ trợ, đọc từ Data\\browser_list_data.json (GPM tự tải về mỗi lần khởi động).
    Trả về {'chrome': ['151.0.7922.76', ...], 'firefox': [...]}; rỗng nếu không đọc được."""
    cores = {}
    try:
        d = json.loads((GPM_DATA_DIR / "Data" / "browser_list_data.json").read_text(encoding="utf-8"))
        for key, name in (("chromium", "chrome"), ("firefox", "firefox")):
            cores[name] = [v.get("display_version", "") for v in d.get(key, {}).get("versions", []) if v.get("display_version")]
    except Exception:
        pass
    return cores


def gpm_skipped_update():
    """Bản GPM mới mà người dùng đã bấm 'bỏ qua' (Updates\\SkipUpdate.json); '' nếu không có."""
    try:
        items = json.loads((GPM_DATA_DIR / "Updates" / "SkipUpdate.json").read_text(encoding="utf-8"))
        return ", ".join(i.get("Version", "") for i in items if i.get("Version"))
    except Exception:
        return ""


def gpm_check():
    """Chẩn đoán GPM (chỉ đọc, không sửa gì): API, nhân trình duyệt + proxy (TCP) từng profile, bản cập nhật bị bỏ qua,
    thư mục cài, lỗi mới trong log GPM. Trả về dict có 'advice' = các việc người dùng cần làm trong GPM."""
    out = {"api_ok": False, "version": "", "profiles": [], "cores": gpm_supported_cores(), "skipped_update": gpm_skipped_update(),
           "install": {}, "log_errors": [], "advice": []}
    try:
        r = requests.get(f"{GPM_API}/profiles?per_page=100", timeout=10).json()
        out["api_ok"] = bool(r.get("success"))
        out["version"] = r.get("sender", "")
        for p in r.get("data", {}).get("data", []):
            raw = p.get("raw_proxy") or ""
            parts = raw.split(":")
            host, port = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
            ok, ms, err = None, None, ""
            if host and port.isdigit():
                t0 = time.time()
                s = socket.socket()
                s.settimeout(6)
                try:
                    s.connect((host, int(port)))
                    ok = True
                except Exception as e:
                    ok, err = False, (str(e)[:60] or type(e).__name__)
                finally:
                    s.close()
                ms = int((time.time() - t0) * 1000)
            b = p.get("browser") or {}
            bname, bver = (b.get("name") or "").lower(), b.get("version") or ""
            supported = out["cores"].get(bname)
            core_ok = None if not supported else (bver in supported)      # None = không có danh sách để so
            out["profiles"].append({"id": p["id"], "name": p.get("name", ""), "browser": f"{b.get('name', '')} {bver}".strip(),
                                    "core_ok": core_ok, "proxy": f"{host}:{port}" if host else "",
                                    "proxy_ok": ok, "proxy_ms": ms, "proxy_err": err})
    except Exception as e:
        out["advice"].append(f"Không kết nối được GPM Local API ({GPM_API}): {str(e)[:100]} → mở GPM Login (bản có Local API) rồi thử lại.")
    try:                                   # thư mục cài: lấy từ tiến trình đang chạy (chỉ đọc)
        ps = "Get-Process GPMLoginGlobal -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Path"
        exe = run_hidden(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=20).stdout.strip()
        if exe:
            fonts = Path(exe).parent / "Assets" / "Data" / "Fonts" / "fonts_file_map.json"
            out["install"] = {"exe": exe, "fonts_map_ok": fonts.exists(), "fonts_map": str(fonts)}
        else:
            out["install"] = {"exe": "", "fonts_map_ok": None}
    except Exception:
        pass
    try:                                   # log GPM: các dòng [ERROR] mới nhất
        logs = GPM_DATA_DIR / "Logs"
        files = sorted(logs.glob("logs_*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)[:2]
        lines = []
        for f in reversed(files):
            lines += [ln[:170] for ln in f.read_text(encoding="utf-8", errors="replace").splitlines() if ln.startswith("[ERROR]")]
        out["log_errors"] = lines[-8:]
    except Exception:
        pass
    bad_core = [p for p in out["profiles"] if p["core_ok"] is False]
    if bad_core:
        have = ", ".join(out["cores"].get("chrome") or []) or "không đọc được"
        how = (f"bạn đã bấm bỏ qua bản {out['skipped_update']} – vào GPM → Cài đặt → Kiểm tra cập nhật, hoặc tải bộ cài mới ở gpmlogin.com"
               if out["skipped_update"] else "GPM → Cài đặt → Kiểm tra cập nhật, hoặc tải bộ cài mới ở gpmlogin.com")
        out["advice"].append("Profile " + ", ".join(f"{p['name']} ({p['browser']})" for p in bad_core)
                             + f" dùng nhân trình duyệt KHÔNG có trong danh sách bản GPM đang chạy hỗ trợ (Chrome: {have})"
                               " → GPM báo 'Sequence contains no matching element' khi mở. Cách sửa: (1) cập nhật GPM lên bản mới "
                               f"({how}); hoặc (2) vào GPM → sửa từng profile → đổi 'Browser version' sang một bản trong danh sách trên.")
    dead = [p for p in out["profiles"] if p["proxy_ok"] is False]
    if dead:
        out["advice"].append("Proxy của profile " + ", ".join(f"{p['name']} ({p['proxy']})" for p in dead)
                             + " không kết nối được (TCP timeout) → GPM báo ProxyCheckFailed khi mở. "
                               "Vào GPM Login → sửa profile → thay / gia hạn proxy hoặc bỏ proxy.")
    if not bad_core and any("Sequence contains no matching element" in ln for ln in out["log_errors"]):
        out["advice"].append("Log GPM có 'Sequence contains no matching element' dù nhân trình duyệt các profile đều nằm trong danh sách "
                             "hỗ trợ → mở thử profile bằng tay trong GPM; nếu vẫn lỗi thì cập nhật GPM hoặc đổi 'Browser version' "
                             "của profile sang bản khác rồi thử lại.")
    if out["api_ok"] and not out["advice"]:
        out["advice"].append("Không thấy vấn đề: API chạy, nhân trình duyệt các profile được GPM hỗ trợ, proxy kết nối được.")
    return out


# ----------------------------------------------------------------- Google Drive
def drive_id(link):
    """('folder', id) hoặc ('file', id) từ link Drive."""
    m = re.search(r"/folders/([\w-]+)", link)
    if m:
        return "folder", m.group(1)
    m = re.search(r"/file/d/([\w-]+)", link) or re.search(r"[?&]id=([\w-]+)", link)
    if m:
        return "file", m.group(1)
    raise ValueError(f"Không nhận ra link Google Drive: {link}")


def drive_list_folder(folder_id):
    """[(id, tên)] trong thư mục chia sẻ 'Bất kỳ ai có đường liên kết' (không cần API key)."""
    r = requests.get(f"https://drive.google.com/embeddedfolderview?id={folder_id}#list", timeout=60)
    r.raise_for_status()
    items = re.findall(r'id="entry-([\w-]+)".*?flip-entry-title">(.*?)</div>', r.text, flags=re.S)
    if not items:
        raise RuntimeError("Không đọc được thư mục Drive (trống, hoặc chưa chia sẻ 'Bất kỳ ai có đường liên kết')")
    return [(fid, html.unescape(name)) for fid, name in items]


def safe_name(name):
    """Tên file ASCII không dấu, không ký tự cấm của Windows (tránh lỗi khi đưa đường dẫn cho Chrome)."""
    base = unicodedata.normalize("NFKD", name.replace("Đ", "D").replace("đ", "d"))
    base = base.encode("ascii", "ignore").decode()
    base = re.sub(r'[<>:"/\\|?*\s]+', "_", base).strip("_.")
    return base or "file"


def drive_download(file_id, dest=None):
    """Tải file công khai về DL_DIR. drive.usercontent + confirm=t để qua trang cảnh báo virus với file lớn.
    Trả về đường dẫn đã lưu (tên lấy theo Drive nếu dest=None)."""
    DL_DIR.mkdir(exist_ok=True)
    with requests.get(DRIVE_DL.format(id=file_id), stream=True, timeout=120) as r:
        r.raise_for_status()
        if "text/html" in r.headers.get("content-type", ""):
            raise RuntimeError("Drive trả về trang web thay vì file: file chưa chia sẻ công khai hoặc đã hết lượt tải")
        if dest is None:
            cd = r.headers.get("content-disposition", "")
            m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="([^"]+)"', cd)
            dest = DL_DIR / safe_name(unquote(m.group(1)) if m else f"{file_id}.mp4")
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return Path(dest)


def docx_title(path):
    """Dòng 'Tiêu đề: ...' trong file kịch bản .docx (làm mô tả bài), None nếu không có."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf8")
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        t = html.unescape(re.sub(r"<[^>]+>", "", p)).strip()
        m = re.match(r"(?i)tiêu\s*đề\s*:\s*(.+)", t)
        if m:
            return m.group(1).strip()
    return None


def fetch_from_drive(link, caption=None):
    """Tải video từ link Drive (file hoặc thư mục). Với thư mục: lấy file video đầu tiên; nếu không
    truyền --caption thì lấy dòng 'Tiêu đề:' trong .docx cùng thư mục, không có thì dùng tên video.
    Trả về (đường dẫn video, mô tả)."""
    kind, fid = drive_id(link)
    if kind == "file":
        log("Drive: tải file video...")
        dest = drive_download(fid)
        if not dest.suffix.lower() in VIDEO_EXT:
            raise RuntimeError(f"Link Drive không phải file video: {dest.name}")
        return str(dest), caption or dest.stem

    items = drive_list_folder(fid)
    log("Drive: thư mục có " + ", ".join(n for _, n in items))
    vids = [(i, n) for i, n in items if n.lower().endswith(VIDEO_EXT)]
    if not vids:
        raise RuntimeError("Thư mục Drive không có file video")
    vid_id, vid_name = vids[0]
    log(f"Drive: tải {vid_name}")
    dest = drive_download(vid_id, DL_DIR / safe_name(vid_name))
    log(f"Drive: đã lưu {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    if not caption:
        docs = [(i, n) for i, n in items if n.lower().endswith(".docx")]
        if docs:
            caption = docx_title(drive_download(docs[0][0], DL_DIR / safe_name(docs[0][1])))
            if caption:
                log(f"Drive: mô tả lấy từ kịch bản: {caption}")
        if not caption:
            caption = Path(vid_name).stem
            log(f"Drive: không có 'Tiêu đề:' trong kịch bản, dùng tên video làm mô tả: {caption}")
    return str(dest), caption


# ----------------------------------------------------------------- tiện ích Playwright
def footer_button(page, name):
    """Nút `name` ở thanh đáy (Hủy / Quay lại / Tiếp / Lên lịch / Chia sẻ).
    Cùng tên còn có nút ở stepper đầu trang, mũi tên cuộn hình thu nhỏ, lựa chọn 'Lên lịch'...
    Thanh đáy căn phải nên lấy nút nằm xa nhất về bên phải."""
    loc = page.get_by_role("button", name=name, exact=True)
    best, best_x = None, -1
    for i in range(loc.count()):
        try:
            b = loc.nth(i).bounding_box(timeout=2000)
        except Exception:        # nút vừa bị React vẽ lại -> bỏ qua, vòng sau tìm lại
            continue
        if b and b["x"] > best_x:
            best, best_x = loc.nth(i), b["x"]
    if best is None:
        raise RuntimeError(f"Không thấy nút '{name}'")
    return best


def footer_enabled(page, name):
    """True nếu nút thanh đáy `name` có mặt và đang bấm được; False nếu không thấy / đang vẽ lại."""
    try:
        return footer_button(page, name).is_enabled(timeout=2000)
    except Exception:
        return False


def current_step(page):
    """Bước đang chọn trên stepper Tạo · Chỉnh sửa · Chia sẻ (aria-current="step")."""
    return page.evaluate("""() => {
        for (const b of document.querySelectorAll('[role=button][aria-current="step"]')) {
            const t = (b.innerText || '').trim();
            if (['Tạo', 'Chỉnh sửa', 'Chia sẻ'].includes(t)) return t;
        }
        return null;
    }""")


def body_text(page):
    return page.evaluate("() => document.body.innerText")


def wait_copyright_check(page, max_s=600):
    """Bước Chỉnh sửa: FB quét bản quyền, video dài có thể mất vài phút; chờ xong rồi mới bấm Tiếp."""
    t0 = last = time.time()
    while "Đang kiểm tra nội dung có bản quyền" in body_text(page):
        if time.time() - t0 > max_s:
            log("CẢNH BÁO: kiểm tra bản quyền quá lâu, vẫn đi tiếp")
            return
        if time.time() - last >= 30:
            log(f"  ...FB đang kiểm tra bản quyền ({time.time() - t0:.0f}s)")
            last = time.time()
        page.wait_for_timeout(2000)


def goto_step(page, target, max_s=900):
    """Bấm Tiếp cho tới khi stepper ở `target`; nếu trang chưa chuyển thì bấm lại."""
    t0 = time.time()
    while True:
        cur = current_step(page)
        if cur == target:
            page.wait_for_timeout(1500)
            return
        if time.time() - t0 > max_s:
            shot(page, "step_stuck")
            raise RuntimeError(f"Không chuyển được sang bước '{target}' (đang ở '{cur}')")
        if not footer_enabled(page, "Tiếp"):
            page.wait_for_timeout(2000)
            continue
        footer_button(page, "Tiếp").click()
        for _ in range(15):                 # chờ tối đa 15s cho stepper đổi
            page.wait_for_timeout(1000)
            if current_step(page) != cur:
                break
        else:
            # Bấm rồi mà không chuyển: ở bước Chỉnh sửa thường do FB còn đang quét bản quyền
            log(f"  ...bấm Tiếp ở bước '{cur}' chưa chuyển trang"
                + (", chờ FB kiểm tra bản quyền xong rồi bấm lại" if cur == "Chỉnh sửa" else ", bấm lại"))
            if cur == "Chỉnh sửa":
                wait_copyright_check(page)


def upload_via_cdp(page, open_button, path):
    """Playwright từ chối chuyển file >50MB khi nối qua cổng debug ("not co-located").
    Thay vào đó tự chặn hộp thoại chọn file bằng DevTools rồi gọi DOM.setFileInputFiles với
    đường dẫn trên đĩa: Chrome tự đọc file, không giới hạn dung lượng."""
    cdp = page.context.new_cdp_session(page)
    chooser = {}
    cdp.on("Page.fileChooserOpened", lambda ev: chooser.update(ev))
    cdp.send("Page.enable")
    cdp.send("Page.setInterceptFileChooserDialog", {"enabled": True})
    try:
        open_button.click()
        for _ in range(100):
            if chooser:
                break
            page.wait_for_timeout(100)
        if "backendNodeId" not in chooser:
            raise RuntimeError("Không bắt được hộp thoại chọn file của Facebook")
        cdp.send("DOM.setFileInputFiles", {"files": [path], "backendNodeId": chooser["backendNodeId"]})
    finally:
        try:
            cdp.send("Page.setInterceptFileChooserDialog", {"enabled": False})
        except Exception:
            pass
        cdp.detach()


def upload_percent(page):
    """Số % Facebook hiển thị cạnh tên file trong khối 'File phương tiện' (None nếu không còn)."""
    return page.evaluate("""() => {
        const t = document.body.innerText;
        const a = t.indexOf('File phương tiện'), b = t.indexOf('Chi tiết về thước phim');
        const m = t.slice(a < 0 ? 0 : a, b < 0 ? undefined : b).match(/(\\d{1,3})%/);
        return m ? parseInt(m[1]) : null;
    }""")


def preview_pause_button(page):
    """Nút tạm dừng thật của bản xem trước: thẻ <a> đầu tiên trong nhóm 3 icon (tạm dừng / loa / ...)
    nằm đè góc trên-phải canvas. Không có aria-label nên tìm theo cấu trúc + vị trí. Trả về locator hoặc None."""
    try:
        box = page.locator("canvas").first.bounding_box(timeout=3000)
    except Exception:
        box = None
    if not box:
        return None
    found = page.evaluate("""(box) => {
        document.querySelectorAll('[data-gpm-pause]').forEach(e => e.removeAttribute('data-gpm-pause'));
        for (const div of document.querySelectorAll('div')) {
            const as = [...div.children].filter(c => c.tagName === 'A' && c.querySelector('div[style*="mask-image"]'));
            if (as.length >= 2 && as.length === div.children.length) {
                const r = as[0].getBoundingClientRect();
                if (r.width > 0 && r.x >= box.x && r.x <= box.x + box.width && r.y >= box.y && r.y <= box.y + box.height) {
                    as[0].setAttribute('data-gpm-pause', '1');
                    return true;
                }
            }
        }
        return false;
    }""", box)
    return page.locator("a[data-gpm-pause]").first if found else None


def pause_preview_by_button(page, tries=2):
    """Bấm nút tạm dừng thật rồi đo lại. Trả về True (đã đứng yên / đã bấm) / False (bấm nhưng vẫn chạy) / None (không có nút).
    Bấm khi trình phát chưa chạy thì cú bấm bị bỏ qua -> chờ thấy khung hình chuyển động rồi mới bấm.
    Chỉ bấm tối đa `tries` lần: mỗi cú bấm làm trình phát hiện hiệu ứng, bấm nhiều = nhấp nháy."""
    btn = preview_pause_button(page)
    if btn is None:
        return None
    for _ in range(10):                   # tối đa ~15s chờ trình phát bắt đầu chạy
        st = preview_frozen(page)
        if st is False or st is None:
            break
        page.wait_for_timeout(500)
    for _ in range(tries):
        btn = preview_pause_button(page) or btn
        btn.click()
        page.wait_for_timeout(800)
        st = preview_frozen(page)
        if st is None:                    # không đo được -> coi như đã bấm, không bấm thêm
            return True
        if st:
            return True
        page.wait_for_timeout(1500)
    return False


# Đọc vài điểm ảnh của canvas xem trước bằng JS (không chụp ảnh qua DevTools: chụp liên tục làm trang vẽ lại -> nhấp nháy)
_CANVAS_SAMPLE_JS = """() => {
    const cv = document.querySelector('canvas');
    if (!cv) return null;
    try {
        const ctx = cv.getContext('2d');
        const w = cv.width, h = cv.height;
        if (!ctx || !w || !h) return null;
        const d = ctx.getImageData(Math.floor(w * 0.25), Math.floor(h * 0.25),
                                   Math.max(1, Math.floor(w * 0.5)), Math.max(1, Math.floor(h * 0.5))).data;
        let s = 0;
        for (let i = 0; i < d.length; i += 97) s = (s * 31 + d[i]) >>> 0;
        return s;
    } catch (e) { return null; }
}"""


def preview_frozen(page):
    """Đo thật nhưng không chụp ảnh: lấy mẫu điểm ảnh canvas 3 lần cách 0.6s, không đổi -> đang đứng yên.
    None = không đo được (không có canvas / canvas không đọc được)."""
    try:
        hs = set()
        for _ in range(3):
            v = page.evaluate(_CANVAS_SAMPLE_JS)
            if v is None:
                return None
            hs.add(v)
            page.wait_for_timeout(600)
        return len(hs) == 1
    except Exception:
        return None


def shot(page, name):
    p = LOG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{name}.png"
    try:
        page.screenshot(path=str(p))
    except Exception:
        pass
    return p


def parse_schedule(s):
    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Không hiểu thời gian '{s}' (dùng DD/MM/YYYY HH:MM hoặc YYYY-MM-DD HH:MM)")


# ----------------------------------------------------------------- fanpage
def pages_file(profile_id):
    """Cache tên trang -> asset_id, riêng cho từng profile (mỗi tài khoản FB có trang khác nhau)."""
    return LOG_DIR / f"pages_{profile_id[:8]}.json"


def norm_text(s):
    """Chữ thường, bỏ dấu, gọn khoảng trắng – để so tên trang."""
    s = unicodedata.normalize("NFKD", str(s or "").replace("Đ", "D").replace("đ", "d"))
    return re.sub(r"\s+", " ", s.encode("ascii", "ignore").decode().lower()).strip()


def composer_page_name(page):
    """Tên trang đang chọn ở ô 'Đăng lên' trong trình soạn thước phim ('' nếu không đọc được)."""
    try:
        return page.evaluate("""() => {
            const lab = [...document.querySelectorAll('label')].find(l => (l.innerText || '').trim() === 'Đăng lên');
            const cb = lab ? document.getElementById(lab.getAttribute('for')) : document.querySelector('[role=combobox]');
            return cb ? (cb.innerText || '').replace(/\\u200b/g, '').trim() : '';
        }""")
    except Exception:
        return ""


def open_switcher(page):
    """Mở trang chủ Business Suite rồi bung bộ chọn trang (góc trên-trái). Lớp phủ/hộp thoại giới thiệu
    có thể chặn cú bấm -> đóng rồi bấm lại."""
    page.goto("https://business.facebook.com/latest/home/", wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(5000)
    for attempt in range(4):
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        for name in ("Đóng", "Bỏ qua", "Xong"):
            btn = page.get_by_role("button", name=name, exact=True)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(800)
        try:
            page.get_by_role("combobox").first.click(timeout=8000)
            break
        except Exception:
            if attempt == 3:
                raise
            page.wait_for_timeout(2000)
    page.wait_for_timeout(2500)


def switcher_pages(page):
    """Mở bộ chọn trang và trả về [(tên trang, nền tảng)] – mục trang là heading có dòng
    'Facebook'/'Instagram' ngay dưới tên. Popup vẫn đang mở khi hàm trả về."""
    open_switcher(page)
    js = """() => [...document.querySelectorAll('[role=heading]')]
        .filter(h => h.getAttribute('aria-level') !== '1')
        .map(h => ({name: (h.innerText || '').replace(/\\u200b/g, '').trim(),
                    parent: (h.parentElement ? h.parentElement.innerText : '').replace(/\\u200b/g, '').replace(/\\s+/g, ' ').trim()}))
        .filter(e => e.name && /(Facebook|Instagram)$/.test(e.parent) && e.parent !== e.name)
        .map(e => [e.name, e.parent.endsWith('Instagram') ? 'Instagram' : 'Facebook'])"""
    ents, last = [], None
    for _ in range(10):               # popup vẽ dần: chờ tới khi danh sách có mục và không đổi nữa
        ents = page.evaluate(js)
        if ents and ents == last:
            break
        last = ents
        page.wait_for_timeout(800)
    seen, out = set(), []
    for name, platform in ents:
        if name not in seen:
            seen.add(name)
            out.append((name, platform))
    return out


def scan_pages(page, profile_id):
    """Danh sách fanpage Facebook mà profile này thấy, kèm asset_id: [{name, asset, platform}]."""
    entries = switcher_pages(page)
    page.keyboard.press("Escape")
    out = []
    for name, platform in entries:
        if platform != "Facebook":
            out.append({"name": name, "asset": "", "platform": platform})
            continue
        try:
            aid = resolve_asset_id(page, name, profile_id)
        except Exception as e:
            log(f"  không lấy được asset_id của '{name}': {str(e)[:80]}")
            aid = ""
        out.append({"name": name, "asset": aid, "platform": platform})
    return out


def resolve_asset_id(page, want, profile_id=PROFILE_ID):
    """Cột 'Page đăng' trong Sheet -> asset_id. Nhận asset_id (toàn số) hoặc một phần tên trang.
    Tên trang được tra trong cache logs/pages_<profile>.json; chưa có thì mở bộ chọn trang ở
    Business Suite, bấm vào trang đó và đọc asset_id trên URL."""
    want = (want or "").strip()
    if not want:
        return None
    if want.isdigit():
        return want
    cache_file = pages_file(profile_id)
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    for name, aid in cache.items():
        if norm_text(want) in norm_text(name):
            return aid
    log(f"Tìm asset_id của trang '{want}' trong bộ chọn trang...")
    hit, names = None, []
    for attempt in range(2):          # popup đôi khi chưa đủ mục -> mở lại lần nữa
        names = [n for n, _ in switcher_pages(page)]
        hit = next((n for n in names if norm_text(want) in norm_text(n)), None)
        if hit:
            break
        page.keyboard.press("Escape")
        page.wait_for_timeout(1500)
    if not hit:
        page.keyboard.press("Escape")
        raise RuntimeError(f"Không thấy trang nào khớp '{want}' trong bộ chọn trang ({names})")
    # Tên trang hiện tại cũng là tiêu đề lớn (aria-level=1) của trang chủ -> chỉ bấm mục trong popup
    heads = page.get_by_role("heading", name=hit, exact=True)
    target = None
    for i in range(heads.count() - 1, -1, -1):
        h = heads.nth(i)
        if h.get_attribute("aria-level") != "1" and h.is_visible():
            target = h
            break
    if target is None:
        page.keyboard.press("Escape")
        raise RuntimeError(f"Không bấm được mục '{hit}' trong bộ chọn trang")
    target.click()
    aid = None
    for _ in range(30):
        page.wait_for_timeout(1000)
        m = re.search(r"asset_id=(\d+)", page.url)
        if m:
            aid = m.group(1)
            if norm_text(hit) in norm_text(page.get_by_role("combobox").first.inner_text()):
                break
    if not aid:
        raise RuntimeError(f"Chọn trang '{hit}' nhưng không đọc được asset_id trên URL")
    cache[hit] = aid
    cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"Trang '{hit}' -> asset_id {aid} (đã lưu cache)")
    return aid


# ----------------------------------------------------------------- luồng đăng reel
def open_and_upload(page, video, caption, asset_id, pause_during_upload=True, info=None):
    """Mở trình soạn, tải video, nhập mô tả. Kết thúc ở bước 'Tạo' với video đã lên FB.
    info (dict) nhận thêm: info['page'] = tên trang ở ô 'Đăng lên'."""
    video = str(Path(video).resolve())

    # 1. Mở trình soạn thước phim
    log("Mở trình soạn thước phim")
    page.goto(COMPOSER_URL.format(asset=asset_id), wait_until="domcontentloaded", timeout=90000)
    add_video = page.get_by_role("button", name="Thêm video")
    try:
        add_video.wait_for(timeout=30000)
    except PWTimeout:
        # Dự phòng: về trang chủ rồi bấm "Tạo thước phim"
        page.goto(f"https://business.facebook.com/latest/home/?asset_id={asset_id}", wait_until="domcontentloaded")
        page.get_by_role("button", name="Tạo thước phim").click()
        add_video.wait_for(timeout=30000)
    page.wait_for_timeout(2000)
    page_name = composer_page_name(page)
    log(f"Đăng lên trang: {page_name or '(không đọc được tên)'}")
    if info is not None:
        info["page"] = page_name
        want = info.get("expect_page")
        if want and not want.isdigit() and norm_text(want) not in norm_text(page_name):
            raise RuntimeError(f"Trình soạn đang chọn trang '{page_name}', không khớp cột Page đăng '{want}'")

    # 2. Tải video lên
    size_mb = Path(video).stat().st_size / 1e6
    log(f"Tải video: {video} ({size_mb:.0f} MB)")
    upload = {"receive": False}

    def _on_resp(r):   # FB gọi .../upload/requests/receive/ khi đã nhận đủ file
        if "video/upload/requests/receive" in r.url and r.ok:
            upload["receive"] = True

    page.on("response", _on_resp)
    upload_via_cdp(page, add_video, video)
    # Tạm dừng bản xem trước = CHỈ bấm nút tạm dừng thật của Facebook (như người dùng). Không can thiệp trình phát
    # (ép playbackRate = 0 / chụp canvas liên tục như bản cũ): Facebook gọi play() liên tục nên mọi can thiệp tạo
    # vòng lặp "play -> ép dừng -> vẽ lại" làm trang nhấp nháy suốt lúc tải.
    pause_mode = None                 # "button" = đã bấm nút tạm dừng thật
    reclicks = 0
    if pause_during_upload:
        page.wait_for_timeout(2500)
        r = pause_preview_by_button(page)
        if r:
            pause_mode = "button"
            log("Đã bấm nút tạm dừng bản xem trước")
        else:
            log(f"Không bấm được nút tạm dừng bản xem trước ({'không thấy nút' if r is None else 'bấm xong vẫn chạy'}) "
                "– để phát bình thường, không can thiệp trình phát")

    # 3. Chờ tải lên xong. LƯU Ý: nút "Tiếp" sáng ngay khi mới tải được vài %, nên phải
    #    dựa vào số % cạnh tên file + tín hiệu FB báo đã nhận đủ (upload/requests/receive).
    max_wait = int(max(300, 120 + size_mb * 2))      # ~2s/MB qua proxy, tối thiểu 5 phút
    log(f"Chờ tải lên/xử lý video (tối đa {max_wait // 60} phút)...")
    t0 = last_log = time.time()
    pct_gone_since = None
    while True:
        page.wait_for_timeout(2000)
        elapsed = time.time() - t0
        pct = upload_percent(page)
        ready = footer_enabled(page, "Tiếp")
        if upload["receive"] and ready and (pct is None or pct >= 100):
            break
        # Dự phòng nếu FB đổi endpoint: % đã biến mất 30s, nút Tiếp sáng -> coi như xong
        if pct is None and ready and elapsed > 20:
            pct_gone_since = pct_gone_since or time.time()
            if time.time() - pct_gone_since > 30:
                break
        else:
            pct_gone_since = None
        if time.time() - last_log >= 30:
            extra = ""
            if pause_mode == "button":
                frozen = preview_frozen(page)
                if frozen is False and reclicks < 2:     # FB dựng lại trình phát -> bấm lại, tối đa 2 lần cả đợt tải
                    reclicks += 1
                    frozen = pause_preview_by_button(page, tries=1)
                extra = f", xem trước đứng yên = {frozen}"
            log(f"  ...đang tải lên {pct if pct is not None else '?'}% ({elapsed:.0f}s{extra})")
            last_log = time.time()
        body = body_text(page)
        if any(k in body for k in ("không hỗ trợ", "Không thể tải", "Đã xảy ra lỗi", "ít nhất 5 ảnh")):
            shot(page, "upload_error")
            raise RuntimeError("Facebook báo lỗi khi tải video (xem ảnh trong logs/)")
        if "reels_composer" not in page.url or "File phương tiện" not in body:
            shot(page, "composer_closed")
            raise RuntimeError("Trình soạn thước phim đã bị đóng giữa lúc tải (đừng bấm vào trang khi script đang chạy)")
        if elapsed > max_wait:
            shot(page, "upload_timeout")
            raise RuntimeError("Quá thời gian chờ tải lên/xử lý video")
    page.remove_listener("response", _on_resp)
    log(f"Video sẵn sàng sau ~{time.time() - t0:.0f}s (FB đã nhận đủ file: {upload['receive']})")
    if pause_mode == "button" and preview_frozen(page):
        btn = preview_pause_button(page)
        if btn is not None:
            btn.click()
            log("Bấm lại nút để bản xem trước phát tiếp")

    # 4. Mô tả
    log("Nhập mô tả")
    box = page.get_by_role("textbox", name=DESC_BOX_NAME)
    box.scroll_into_view_if_needed()
    box.click()
    page.keyboard.type(caption, delay=30)
    page.wait_for_timeout(1200)
    page.keyboard.press("Escape")       # đóng gợi ý hashtag nếu có
    page.wait_for_timeout(500)
    got = box.inner_text().strip()
    if got != caption.strip():
        log(f"CẢNH BÁO: mô tả đọc lại khác: {got!r}")


def finish_post(page, caption, when=None, dry_run=False, asset_id=PAGE_ASSET_ID):
    """Từ bước hiện tại của trình soạn: sang 'Chia sẻ', chọn lịch, bấm nút cuối, xác nhận.
    when=None -> đăng ngay; when=datetime -> lên lịch. Trả về True nếu đã thấy bài trong danh sách."""
    # 5. Tiếp -> Chỉnh sửa (chờ kiểm tra bản quyền) -> Tiếp -> Chia sẻ
    log(f"Đang ở bước '{current_step(page)}', chuyển sang 'Chia sẻ'")
    goto_step(page, "Chia sẻ")
    page.get_by_role("button", name="Chia sẻ ngay", exact=True).wait_for(timeout=30000)
    page.wait_for_timeout(1500)

    # 6. Chọn đăng ngay / lên lịch
    if when:
        log(f"Lên lịch: {when:%d/%m/%Y %H:%M}")
        opt = page.get_by_role("button", name="Lên lịch", exact=True).first
        opt.scroll_into_view_if_needed()
        opt.click()
        d = page.get_by_placeholder("dd/mm/yyyy").first
        d.wait_for(timeout=15000)
        d.click()
        page.keyboard.press("Control+A")
        page.keyboard.type(when.strftime("%d/%m/%Y"), delay=50)
        page.wait_for_timeout(600)
        page.keyboard.press("Enter")
        page.wait_for_timeout(600)
        page.keyboard.press("Escape")
        for label, val in (("giờ", f"{when.hour:02d}"), ("phút", f"{when.minute:02d}")):
            sp = page.get_by_role("spinbutton", name=label).first
            sp.click()
            page.keyboard.press("Control+A")
            page.keyboard.type(val, delay=60)
            page.wait_for_timeout(300)
        page.keyboard.press("Tab")
        page.wait_for_timeout(800)
        rb = page.evaluate("""() => ({
            date: document.querySelector('input[placeholder="dd/mm/yyyy"]')?.value,
            h: document.querySelector('input[aria-label="giờ"]')?.getAttribute('aria-valuenow'),
            m: document.querySelector('input[aria-label="phút"]')?.getAttribute('aria-valuenow')})""")
        log(f"Đọc lại lịch: {rb}")
        ok = (rb.get("date") or "").startswith(f"{when.day} ") and str(when.year) in (rb.get("date") or "") \
            and int(rb.get("h") or -1) == when.hour and int(rb.get("m") or -1) == when.minute
        if not ok:
            shot(page, "schedule_mismatch")
            raise RuntimeError(f"Ngày giờ không khớp sau khi nhập: {rb}")
        final_name, list_url = "Lên lịch", SCHEDULED_URL
    else:
        log("Đăng ngay")
        final_name, list_url = "Chia sẻ", PUBLISHED_URL

    shot(page, "before_submit")
    if dry_run:
        log("DRY-RUN: dừng trước nút cuối, không bấm. Bỏ bản nháp và quay về danh sách.")
        page.goto(list_url.format(asset=asset_id), wait_until="domcontentloaded", timeout=90000)
        return False

    # 7. Bấm nút cuối
    footer_button(page, final_name).click()
    log(f"Đã bấm '{final_name}', chờ Facebook xác nhận...")
    page.wait_for_timeout(15000)

    # 8. Kiểm tra trong danh sách (trình soạn đôi khi treo vòng xoay dù đã thành công)
    check = page.context.new_page()
    try:
        for attempt in range(12):     # ~3 phút
            check.goto(list_url.format(asset=asset_id), wait_until="domcontentloaded", timeout=90000)
            check.wait_for_timeout(8000)
            if caption.strip()[:25] in body_text(check):
                log("XÁC NHẬN: đã thấy bài trong danh sách " + ("Đã lên lịch" if when else "Đã đăng"))
                shot(check, "confirmed")
                return True
            log(f"Chưa thấy bài trong danh sách, thử lại {attempt + 1}/12")
            check.wait_for_timeout(7000)
    finally:
        check.close()
    shot(page, "not_confirmed")
    raise RuntimeError("Không thấy bài trong danh sách sau 3 phút – kiểm tra tay trên Business Suite")


def pick_page(ctx, resume=False):
    """Tab để làm việc. Tab trống/tab nội bộ của GPM (url không phải http) điều hướng sẽ bị
    'Frame detached' nên bỏ qua; --resume thì cần đúng tab đang mở trình soạn."""
    pages = [pg for pg in ctx.pages if not pg.is_closed()]
    if resume:
        live = [pg for pg in pages if "reels_composer" in pg.url]
    else:
        live = [pg for pg in pages if pg.url.startswith("http")]
    page = live[0] if live else ctx.new_page()
    page.bring_to_front()
    return page


def post_reel(page, video, caption, when=None, dry_run=False, asset_id=PAGE_ASSET_ID, resume=False, info=None):
    """info (dict, tuỳ chọn): vào = {'expect_page': tên trang mong đợi}; ra = info['page'] tên trang thật."""
    if not getattr(page, "_gpm_dialog_hooked", False):     # đăng nhiều bài trên cùng tab: chỉ gắn 1 lần
        page.on("dialog", lambda d: d.accept())
        page._gpm_dialog_hooked = True
    if resume:
        if "reels_composer" not in page.url:
            raise RuntimeError("--resume cần tab đầu tiên của profile đang mở trình soạn thước phim")
        if info is not None:
            info["page"] = composer_page_name(page)
    else:
        open_and_upload(page, video, caption, asset_id, info=info)
    return finish_post(page, caption, when, dry_run, asset_id)


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Đăng/lên lịch Reels lên Fanpage qua profile GPM Login")
    ap.add_argument("--video", help="Đường dẫn file video (.mp4) trên máy")
    ap.add_argument("--drive", help="Link Google Drive (file video hoặc thư mục chứa video + kịch bản .docx)")
    ap.add_argument("--caption", help="Mô tả / hashtag (với --drive có thể bỏ: lấy dòng 'Tiêu đề:' trong .docx)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--schedule", help='Thời điểm đăng, ví dụ "07/10/2026 12:00" hoặc "2026-10-07 12:00"')
    g.add_argument("--now", action="store_true", help="Đăng ngay")
    ap.add_argument("--resume", action="store_true",
                    help="Trình soạn đang mở sẵn với video + mô tả: bỏ qua tải lên, chạy tiếp tới cuối")
    ap.add_argument("--profile", default=PROFILE_ID, help="Profile GPM: id hoặc một phần tên (vd 'profile 002')")
    ap.add_argument("--asset-id", default=PAGE_ASSET_ID, help="asset_id của fanpage trong Business Suite")
    ap.add_argument("--dry-run", action="store_true", help="Làm hết các bước nhưng không bấm nút cuối")
    ap.add_argument("--close-after", action="store_true", help="Đóng profile GPM khi xong")
    a = ap.parse_args()

    when = parse_schedule(a.schedule) if a.schedule else None
    if when and when <= datetime.now():
        sys.exit("Thời điểm lên lịch phải ở tương lai")
    if a.drive:
        a.video, a.caption = fetch_from_drive(a.drive, a.caption)
    elif not a.resume:
        if not a.video:
            sys.exit("Thiếu --video hoặc --drive (hoặc dùng --resume)")
        if not Path(a.video).is_file():
            sys.exit(f"Không thấy file video: {a.video}")
    if not a.caption:
        sys.exit("Thiếu --caption")

    a.profile = resolve_profile_id(a.profile)
    port = gpm_start(a.profile)
    time.sleep(5)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        page = pick_page(browser.contexts[0], a.resume)
        try:
            ok = post_reel(page, a.video, a.caption, when, a.dry_run, a.asset_id, a.resume)
        except Exception as e:
            shot(page, "error")
            log(f"LỖI: {e}")
            raise
        finally:
            if a.close_after:
                gpm_stop(a.profile)
    log("Hoàn tất." if ok else "Kết thúc (dry-run).")


if __name__ == "__main__":
    main()
