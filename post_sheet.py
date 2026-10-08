"""
Đăng / lên lịch hàng loạt Thước phim theo một Google Sheet chia sẻ công khai.

Sheet gồm 3 tab (dựng bằng setupSheet() trong sheet_webhook.gs):
  "Lịch đăng" (hàng đợi, script đọc) – hàng 1 là tiêu đề cột, tên cột linh hoạt (nhận diện theo từ khoá):
    Ngày đăng + Giờ  : 2 cột "08/10/2026" | "19:00" (hoặc 1 cột gộp "08/10/2026 19:00"); ghi "ngay" để đăng ngay
                       --mode schedule (mặc định): lên lịch trên Facebook ngay khi chạy, Facebook tự phát đúng giờ
                       --mode due: tới đúng ngày/giờ đó phần mềm mới bấm "Chia sẻ ngay"; hàng chưa tới giờ để lại
    Page             : tên page (chọn từ tab Pages) -> script tra asset_id + profile GPM trong tab Pages
    Video            : link file Drive / link thư mục Drive / đường dẫn file trên máy
    Tiêu đề          : mô tả bài (có thể kèm luôn hashtag); trống + thư mục Drive -> lấy 'Tiêu đề:' trong .docx
    Hashtag          : (tuỳ chọn) nối vào sau tiêu đề
    Trạng thái       : script ghi (Đang đăng / Đã lên lịch / Đã đăng / Lỗi); tay ghi "bỏ qua" để không đăng
    Ghi chú          : script ghi lý do lỗi / giờ xác nhận
    (Profile         : tuỳ chọn, ghi đè profile lấy từ tab Pages)
  "Pages" (danh mục): Tên page | asset_id | Profile GPM | Người phụ trách | Giờ đăng mặc định | Ghi chú
  "Đã đăng" (lưu trữ): Apps Script dọn bài cũ sang đây

Ví dụ:
  python post_sheet.py --sheet "https://docs.google.com/spreadsheets/d/XXXX/edit?gid=0"
  python post_sheet.py --sheet "..." --dry-run          # chạy thử, không bấm nút cuối
  python post_sheet.py --sheet "..." --max 1            # chỉ đăng 1 bài đầu tiên đang chờ
  python post_sheet.py --sheet "..." --rows 3,5         # chỉ đăng hàng 3 và 5 (số hàng trên Sheet)
"""
import argparse
import csv
import io
import json
import random
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

from post_reel import (LOG_DIR, PAGE_ASSET_ID, PROFILE_ID, VIDEO_EXT, fetch_from_drive, gpm_profiles, gpm_start,
                       gpm_stop, log, parse_schedule, pick_page, post_reel, resolve_asset_id, resolve_profile_id,
                       scan_pages, shot)

# Link Sheet mặc định (có thể thay bằng --sheet)
SHEET_URL = ""
TAB_QUEUE, TAB_PAGES = "Lịch đăng", "Pages"     # tên tab hàng đợi / danh mục page (đổi bằng --tab, --pages-tab)
# URL ứng dụng web Apps Script (xem sheet_webhook.gs) để ghi Page đăng / Trạng thái / Ghi chú ngược vào Sheet.
# Để trống = không ghi (vẫn có báo cáo CSV trong logs/).
SHEET_WEBHOOK = ""
STATE_FILE = LOG_DIR / "sheet_state.json"      # ghi nhớ hàng nào đã đăng để không đăng lại

# Giá trị cột Trạng thái do script ghi
ST_RUNNING, ST_SCHEDULED, ST_POSTED, ST_ERROR, ST_DRY = "Đang đăng", "Đã lên lịch", "Đã đăng", "Lỗi", "Chạy thử OK"
# Cách đăng:  "schedule" = lên lịch trên Facebook ngay khi chạy (Facebook tự phát đúng giờ ghi trong Sheet)
#             "due"      = tới đúng ngày/giờ ghi trong Sheet phần mềm mới bấm "Chia sẻ ngay"; hàng chưa tới giờ để lại
POST_MODES = ("schedule", "due")


def sheet_gid(link):
    """gid của tab trong link Sheet (None nếu link không có -> tab đầu tiên)."""
    m = re.search(r"[#&?]gid=(\d+)", link or "")
    return int(m.group(1)) if m else None


def sheet_update(webhook, row, gid=None, **fields):
    """Ghi các ô (page/status/note) của hàng `row` (ở tab `gid`) qua Apps Script. Lỗi mạng chỉ cảnh báo."""
    if not webhook:
        return False
    try:
        r = requests.post(webhook, json={"row": row, "gid": gid, **fields}, timeout=30)
        if r.status_code != 200 or '"ok"' not in r.text:
            log(f"  (webhook trả về {r.status_code}: {r.text[:120]})")
            return False
        return True
    except Exception as e:
        log(f"  (không ghi được vào Sheet: {str(e)[:120]})")
        return False


# ----------------------------------------------------------------- đọc Sheet
def sheet_id(link):
    m = re.search(r"/spreadsheets/d/([\w-]+)", link or "")
    if not m:
        raise ValueError(f"Không nhận ra link Google Sheet: {link}")
    return m.group(1)


def sheet_tabs(link):
    """{tên tab: gid} của Sheet công khai (đọc từ trang htmlview). {} nếu không đọc được."""
    try:
        r = requests.get(f"https://docs.google.com/spreadsheets/d/{sheet_id(link)}/htmlview", timeout=60)
        if r.status_code != 200:
            return {}
        import html as _html
        tabs = {}
        # htmlview nhúng danh sách tab dạng JS: items.push({name: "Tên", pageUrl: "...htmlview?gid=123..."})
        for name, url in re.findall(r'items\.push\(\{name:\s*"((?:[^"\\]|\\.)*)",\s*pageUrl:\s*"((?:[^"\\]|\\.)*)"', r.text):
            m = re.search(r"gid=(\d+)", url.replace("\\/", "/"))
            if m:
                tabs[json.loads(f'"{name}"').strip()] = int(m.group(1))
        if not tabs:   # dự phòng: menu tab dạng HTML
            tabs = {_html.unescape(n).strip(): int(g)
                    for g, n in re.findall(r'id="sheet-button-(\d+)"[^>]*>\s*<a[^>]*>([^<]+)</a>', r.text)}
        return tabs
    except Exception:
        return {}


def sheet_csv_url(link, tab=None):
    """Link export CSV: tab theo tên (tra gid qua htmlview) > gid trong link > tab đầu tiên.
    Trả về (url, gid|None). Lưu ý gviz 'sheet=tên' KHÔNG dùng được: tên sai nó âm thầm trả tab đầu."""
    sid = sheet_id(link)
    gid = None
    if tab:
        tabs = sheet_tabs(link)
        hit = next((g for n, g in tabs.items() if norm(n) == norm(tab)), None)
        if hit is None:
            return None, None
        gid = hit
    else:
        m = re.search(r"[#&?]gid=(\d+)", link)
        gid = int(m.group(1)) if m else None
    return f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv" + (f"&gid={gid}" if gid is not None else ""), gid


def norm(s):
    """Chữ thường, bỏ dấu, bỏ ký tự thừa – để so tên cột."""
    s = unicodedata.normalize("NFKD", str(s or "").replace("Đ", "D").replace("đ", "d"))
    return re.sub(r"[^a-z0-9]+", " ", s.encode("ascii", "ignore").decode().lower()).strip()


COLUMN_KEYS = {           # tab Lịch đăng: tên cột chuẩn -> các từ khoá nhận diện (sau khi bỏ dấu)
    "profile": ("profile", "tai khoan", "account", "nick", "gpm"),
    "page":    ("page", "fanpage", "trang dang"),
    "video":   ("video", "link", "drive", "duong dan", "path", "file"),
    "title":   ("tieu de", "caption", "mo ta", "noi dung", "title"),
    "hashtag": ("hashtag", "hastag", "tag"),
    "date":    ("ngay dang", "ngay", "date"),                       # cột Ngày riêng (dd/mm/yyyy)
    "time":    ("thoi gian", "gio dang", "gio", "lich", "schedule", "time", "hour"),   # cột Giờ riêng, hoặc cột gộp
    "status":  ("trang thai", "status"),
    "note":    ("ghi chu", "note"),
}
PAGES_KEYS = {            # tab Pages
    "name":    ("ten page", "ten trang", "page", "fanpage", "trang"),
    "asset":   ("asset", "id"),
    "profile": ("profile", "tai khoan", "gpm"),
    "owner":   ("phu trach", "owner"),
    "hour":    ("gio", "hour"),
}


def map_columns(header, keys=COLUMN_KEYS, required=("video",)):
    cols = {}
    for idx, name in enumerate(header):
        n = norm(name)
        if not n:
            continue
        for key, kws in keys.items():
            if key not in cols and any(kw in n for kw in kws):
                cols[key] = idx
                break
    missing = [k for k in required if k not in cols]
    if keys is COLUMN_KEYS and "time" not in cols and "date" not in cols:
        missing.append("thời gian / ngày đăng")
    if missing:
        raise RuntimeError(f"Sheet thiếu cột {missing}. Tiêu đề cột hiện có: {header}")
    return cols


def norm_time(s):
    """'19h', '19h30', '7:00 PM', '19:00:00' -> 'HH:MM' ('' nếu trống)."""
    s = (s or "").strip().lower().replace("h", ":").replace(" ", "").rstrip(":")
    if not s:
        return ""
    m = re.match(r"^(\d{1,2})(?::(\d{1,2}))?(?::\d{1,2})?(am|pm|sa|ch)?$", s)
    if not m:
        return s
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap in ("pm", "ch") and h < 12:
        h += 12
    if ap in ("am", "sa") and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}"


def norm_date(s):
    """'8/10/2026', '08/10/2026', '2026-10-08', '08-10-2026' -> 'dd/mm/yyyy' (giữ nguyên nếu không hiểu)."""
    s = (s or "").strip()
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", s)
    if m:
        return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}"
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{int(m.group(3)):02d}/{int(m.group(2)):02d}/{m.group(1)}"
    return s


def fetch_rows(link, tab=None):
    """Các hàng CSV của một tab (None nếu tab không tồn tại). `link` có thể là file .csv trên máy."""
    if link.lower().endswith(".csv") and Path(link).is_file():
        text = Path(link).read_text(encoding="utf-8-sig")
    else:
        url, _ = sheet_csv_url(link, tab)
        if url is None:
            return None
        r = requests.get(url, timeout=60)
        if r.status_code != 200 or "<html" in r.text[:200].lower():
            raise RuntimeError("Không tải được Sheet – kiểm tra link và chia sẻ 'Bất kỳ ai có đường liên kết' (Người xem)")
        text = r.content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def read_sheet(link, tab=None):
    """Hàng đợi: danh sách dict row (số hàng trên Sheet), profile, page, video, title, hashtag, time, status, note.
    `tab` = tên tab hàng đợi; không có tab đó thì dùng gid trong link / tab đầu tiên."""
    rows = fetch_rows(link, tab)
    if rows is None:
        log(f"(không có tab '{tab}', đọc tab đầu tiên / tab trong link)")
        rows = fetch_rows(link, None)
    if not rows:
        raise RuntimeError("Sheet trống")
    cols = map_columns(rows[0])
    # Bỏ dấu ' đầu ô (dùng để giữ "08/10/2026 19:00" là chữ, không bị Sheets đổi thành ngày)
    get = lambda row, key: (row[cols[key]].strip().lstrip("'").strip() if key in cols and cols[key] < len(row) else "")
    out = []
    for i, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        d, t = get(row, "date"), get(row, "time")
        if "date" in cols:                       # bố cục 2 cột: Ngày | Giờ
            if norm(d) in ("ngay", "now", "dang ngay") or norm(t) in ("ngay", "now", "dang ngay"):
                when = "ngay"
            elif d:
                when = norm_date(d) + (" " + norm_time(t) if t else "")   # thiếu giờ -> điền sau từ tab Pages
            else:
                when = ""
        else:                                    # bố cục gộp "dd/mm/yyyy hh:mm"
            when = t
        out.append({"row": i, "profile": get(row, "profile"), "page": get(row, "page"), "video": get(row, "video"),
                    "title": get(row, "title"), "hashtag": get(row, "hashtag"), "time": when,
                    "date_only": bool(d and not t and norm(d) not in ("ngay", "now", "dang ngay")),
                    "status": get(row, "status"), "note": get(row, "note")})
    return out


def read_pages(link, tab=TAB_PAGES):
    """Tab Pages -> [{name, asset, profile, owner, hour}]; [] nếu không có tab (hoặc link là file .csv)."""
    if link.lower().endswith(".csv"):
        return []
    try:
        rows = fetch_rows(link, tab)
    except Exception as e:
        log(f"(không đọc được tab {tab}: {str(e)[:80]})")
        return []
    if not rows or len(rows) < 2:
        return []
    try:
        cols = map_columns(rows[0], PAGES_KEYS, required=("name", "asset"))
    except RuntimeError:
        return []
    get = lambda row, key: (row[cols[key]].strip().lstrip("'").strip() if key in cols and cols[key] < len(row) else "")
    out = []
    for row in rows[1:]:
        if get(row, "name"):
            out.append({"name": get(row, "name"), "asset": get(row, "asset"), "profile": get(row, "profile"),
                        "owner": get(row, "owner"), "hour": get(row, "hour")})
    return out


def lookup_page(pages, want):
    """Tìm page trong tab Pages theo tên (một phần, không dấu) hoặc asset_id."""
    w = norm(want)
    if not w:
        return None
    exact = [p for p in pages if norm(p["name"]) == w or p["asset"] == want.strip()]
    if exact:
        return exact[0]
    hits = [p for p in pages if w in norm(p["name"])]
    return hits[0] if len(hits) == 1 else None


# ----------------------------------------------------------------- trạng thái cục bộ
def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def row_key(sheet_link, item):
    m = re.search(r"/spreadsheets/d/([\w-]+)", sheet_link)
    sid = m.group(1) if m else Path(sheet_link).stem
    return f"{sid}:r{item['row']}:{item['video']}"


# ----------------------------------------------------------------- lập kế hoạch
def plan(items, sheet_link, state, only_rows=None, retry_errors=False, mode="schedule", now=None):
    """Chia hàng thành: sẽ đăng (kèm when) / bỏ qua (kèm lý do).
    Trạng thái trống hoặc 'Chờ đăng' -> sẽ đăng. 'Đã …' (Đã đăng / Đã lên lịch), 'bỏ qua', 'skip', 'done',
    'xong' -> bỏ qua; 'Lỗi' (trên Sheet hoặc trong logs/sheet_state.json) -> bỏ qua trừ khi retry_errors;
    'Đang đăng' (lần trước bị ngắt) -> làm lại.
    mode="schedule": when = giờ trên Sheet (lên lịch trên Facebook); giờ đã qua -> bỏ qua.
    mode="due":      giờ chưa tới -> để lại (it['waiting']=True, lý do 'chưa tới giờ');
                     giờ đã tới   -> đăng ngay (when=None, it['due'] = giờ ghi trên Sheet)."""
    now = now or datetime.now()
    todo, skipped = [], []
    for it in items:
        reason = None
        when = None
        it["waiting"], it["due"] = False, ""
        st = norm(it["status"])
        prev = state.get(row_key(sheet_link, it), {})
        if only_rows and it["row"] not in only_rows:
            reason = "không nằm trong --rows"
        elif st.startswith("da ") or st in ("bo qua", "skip", "done", "xong"):
            reason = f"trạng thái '{it['status']}'"
        elif st.startswith("loi") and not retry_errors:
            reason = f"trạng thái '{it['status']}' (bấm 'Đăng lại hàng lỗi' / --retry-errors để đăng lại)"
        elif prev.get("status") == "done":
            reason = "đã đăng lần chạy trước (logs/sheet_state.json)"
        elif prev.get("status") == "error" and not retry_errors:
            reason = f"lỗi lần trước: {(prev.get('note') or '')[:60]} (sửa hàng, hoặc 'Đăng lại hàng lỗi')"
        elif not it["video"]:
            reason = "thiếu video"
        elif not it["time"] or (it.get("date_only") and re.fullmatch(r"\d{2}/\d{2}/\d{4}", it["time"])):
            reason = ("thiếu giờ đăng (điền cột Giờ, hoặc Giờ đăng mặc định ở tab Pages)"
                      if it.get("date_only") else "thiếu thời gian đăng")
        elif norm(it["time"]) in ("ngay", "now", "dang ngay"):
            when = None
        else:
            try:
                when = parse_schedule(it["time"])
            except ValueError as e:
                reason = str(e)
            else:
                if mode == "due":
                    it["due"] = when.strftime("%d/%m/%Y %H:%M")
                    if when > now:
                        it["waiting"] = True
                        reason = f"chưa tới giờ ({when:%H:%M %d/%m})"
                    else:
                        when = None                       # tới giờ -> bấm "Chia sẻ ngay"
                elif when <= now:
                    reason = f"thời gian {it['time']} đã qua"
        if reason:
            skipped.append((it, reason))
        else:
            todo.append((it, when))
    # "ngay" trước, rồi theo giờ (mode due: theo giờ ghi trên Sheet, bài tới giờ sớm nhất đăng trước)
    todo.sort(key=lambda x: (x[1] is not None, x[1] or (parse_schedule(x[0]["due"]) if x[0]["due"] else datetime.min)))
    return todo, skipped


def with_tags(caption, tags):
    """Nối cột Hashtag vào sau tiêu đề (bỏ qua nếu tiêu đề đã chứa sẵn)."""
    caption, tags = (caption or "").strip(), (tags or "").strip()
    if tags and tags not in caption:
        caption = (caption + " " + tags).strip()
    return caption


def caption_of(it):
    return with_tags(it["title"], it["hashtag"]) or None


def resolve_video(it):
    """Trả về (đường dẫn file trên máy, caption). Link Drive -> tải về; đường dẫn máy -> dùng luôn.
    Thư mục Drive mà Tiêu đề để trống -> lấy dòng 'Tiêu đề:' trong .docx, rồi nối thêm cột Hashtag."""
    src, title, tags = it["video"].strip(), it["title"].strip(), it["hashtag"].strip()
    if "drive.google.com" in src:
        path, cap = fetch_from_drive(src, title or None)
        return path, with_tags(cap, tags)
    p = Path(src)
    if not p.is_file():
        raise RuntimeError(f"Không thấy file: {src}")
    if p.suffix.lower() not in VIDEO_EXT:
        raise RuntimeError(f"Không phải file video: {src}")
    return str(p), with_tags(title or p.stem, tags)


# ----------------------------------------------------------------- lập kế hoạch dùng chung
def prepare(link, tab=TAB_QUEUE, pages_tab=TAB_PAGES, default_profile=PROFILE_ID, only_rows=None,
            retry_errors=False, need_gpm=False, mode="schedule"):
    """Đọc Sheet + tab Pages, gắn asset/profile/giờ mặc định cho từng hàng, chia sẽ-đăng / bỏ-qua.
    Dùng chung cho dòng lệnh, giao diện web và bộ tự động kiểm tra.
    Trả về dict: items, pages, todo, skipped, profile_names, gpm_ok, messages, mode,
    next_due (mode due: giờ sớm nhất trong các hàng chưa tới giờ, None nếu không có)."""
    if mode not in POST_MODES:
        mode = "schedule"
    msgs = []
    items = read_sheet(link, tab)
    pages = read_pages(link, pages_tab)
    if pages:
        msgs.append(f"Tab {pages_tab}: {len(pages)} page – " + ", ".join(f"{p['name']} ({p['profile'] or 'profile mặc định'})" for p in pages))
    else:
        msgs.append(f"(không có tab {pages_tab}: page sẽ được tra qua bộ chọn trang trong Business Suite)")
    try:
        profile_names = dict(gpm_profiles())
        gpm_ok = True
    except Exception as e:
        profile_names, gpm_ok = {}, False
        if need_gpm:
            raise RuntimeError("Không kết nối được GPM Login (cổng 9495) – hãy mở GPM Login rồi chạy lại") from e
        msgs.append("(GPM Login chưa chạy – chỉ lập kế hoạch)")

    def default_page_of(profile_want):
        for p in pages:
            if p["profile"] and (norm(p["profile"]) in norm(profile_want) or norm(profile_want) in norm(p["profile"])):
                return p
        return None
    for it in items:
        p = lookup_page(pages, it["page"]) if it["page"] else None
        if p is None and not it["page"] and pages:
            p = default_page_of(it["profile"] or profile_names.get(default_profile, default_profile))
        it["asset"] = p["asset"] if p else ""
        if p and not it["profile"]:
            it["profile"] = p["profile"]
        if p:
            it["page"] = p["name"]
        if it.get("date_only") and it["time"]:
            hour = norm_time(p["hour"]) if p and p.get("hour") else ""
            it["time"] = f"{it['time']} {hour}" if hour else ""
    todo, skipped = plan(items, link, load_state(), only_rows, retry_errors, mode)
    waiting = [parse_schedule(it["due"]) for it, _ in skipped if it.get("waiting") and it.get("due")]
    return {"items": items, "pages": pages, "todo": todo, "skipped": skipped,
            "profile_names": profile_names, "gpm_ok": gpm_ok, "messages": msgs,
            "mode": mode, "next_due": min(waiting) if waiting else None}


# ----------------------------------------------------------------- quét profile / fanpage
def cmd_list_profiles():
    profiles = gpm_profiles()
    log(f"GPM Login có {len(profiles)} profile:")
    for pid, name in profiles:
        print(f"   {name:30s}  {pid}")


def cmd_scan_pages(which, webhook, close_after=False):
    """Mở từng profile GPM, đọc danh sách fanpage (kèm asset_id), xuất logs/pages_scan.csv theo đúng cột
    tab Pages; có webhook thì thêm/cập nhật thẳng vào tab Pages."""
    profiles = gpm_profiles()
    if which.lower() != "all":
        wanted = [resolve_profile_id(w.strip()) for w in which.split(",") if w.strip()]
        profiles = [(pid, name) for pid, name in profiles if pid in wanted]
    rows = []
    with sync_playwright() as p:
        for pid, pname in profiles:
            log(f"##### Quét fanpage của profile '{pname}' #####")
            try:
                port = gpm_start(pid)
                time.sleep(5)
                browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                page = pick_page(browser.contexts[0])
                page.on("dialog", lambda d: d.accept())
                found = scan_pages(page, pid)
            except Exception as e:
                log(f"  LỖI profile '{pname}': {str(e)[:150]}")
                continue
            finally:
                if close_after:
                    gpm_stop(pid)
            for f in found:
                log(f"  {f['platform']:9s} {f['name']}  asset_id={f['asset'] or '?'}")
                if f["platform"] == "Facebook" and f["asset"]:
                    rows.append([f["name"], f["asset"], pname, "", "", f"quét {datetime.now():%d/%m/%Y}"])
    out = LOG_DIR / "pages_scan.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Tên page", "asset_id", "Profile GPM", "Người phụ trách", "Giờ đăng mặc định", "Ghi chú"])
        w.writerows(rows)
    log(f"Đã ghi {len(rows)} fanpage vào {out} (dán vào tab Pages)")
    if webhook and rows:
        try:
            r = requests.post(webhook, json={"action": "upsert_pages",
                                             "pages": [{"name": a, "asset": b, "profile": c} for a, b, c, *_ in rows]},
                              timeout=60)
            log(f"Tab Pages trên Sheet: {r.text[:160]}")
        except Exception as e:
            log(f"(không ghi được tab Pages qua webhook: {str(e)[:120]})")


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Đăng/lên lịch Reels hàng loạt theo Google Sheet công khai")
    ap.add_argument("--list-profiles", action="store_true", help="In danh sách profile GPM rồi thoát")
    ap.add_argument("--scan-pages", metavar="PROFILE", nargs="?", const="all",
                    help="Quét fanpage (kèm asset_id) của profile GPM ('all' hoặc tên/id, cách nhau dấu phẩy) -> logs/pages_scan.csv; "
                         "có --webhook thì ghi thẳng vào tab Pages")
    ap.add_argument("--sheet", default=SHEET_URL, help="Link Google Sheet (chia sẻ 'Bất kỳ ai có đường liên kết')")
    ap.add_argument("--tab", default=TAB_QUEUE, help=f"Tên tab hàng đợi (mặc định '{TAB_QUEUE}')")
    ap.add_argument("--pages-tab", default=TAB_PAGES, help=f"Tên tab danh mục page (mặc định '{TAB_PAGES}')")
    ap.add_argument("--max", type=int, default=0, help="Tối đa bao nhiêu bài trong lần chạy này (0 = tất cả)")
    ap.add_argument("--rows", help="Chỉ đăng các hàng này, ví dụ 2,3,7 (số hàng trên Sheet)")
    ap.add_argument("--dry-run", action="store_true", help="Làm hết các bước nhưng không bấm nút cuối")
    ap.add_argument("--plan-only", action="store_true", help="Chỉ in kế hoạch, không mở trình duyệt")
    ap.add_argument("--webhook", default=SHEET_WEBHOOK, help="URL Apps Script để ghi Trạng thái/Page ngược vào Sheet")
    ap.add_argument("--retry-errors", action="store_true", help="Đăng lại cả các hàng đang ghi 'Lỗi'")
    ap.add_argument("--mode", choices=POST_MODES, default="schedule",
                    help="schedule = lên lịch trên Facebook ngay khi chạy (mặc định); "
                         "due = tới đúng ngày/giờ ghi trong Sheet mới đăng ('Chia sẻ ngay'), hàng chưa tới giờ để lại")
    ap.add_argument("--pause", default="20-60", help="Nghỉ ngẫu nhiên giữa 2 bài, giây (mặc định 20-60)")
    ap.add_argument("--profile", default=PROFILE_ID, help="Profile GPM mặc định (id hoặc một phần tên) cho hàng không ghi cột Profile")
    ap.add_argument("--asset-id", default=PAGE_ASSET_ID)
    ap.add_argument("--close-after", action="store_true", help="Đóng profile GPM khi xong")
    a = ap.parse_args()
    if a.list_profiles:
        return cmd_list_profiles()
    if a.scan_pages:
        return cmd_scan_pages(a.scan_pages, a.webhook, a.close_after)
    if not a.sheet:
        sys.exit("Thiếu --sheet (hoặc điền SHEET_URL trong post_sheet.py)")
    only_rows = {int(x) for x in a.rows.split(",")} if a.rows else None
    lo, hi = (int(x) for x in a.pause.split("-")) if "-" in a.pause else (int(a.pause), int(a.pause))

    try:
        prep = prepare(a.sheet, a.tab, a.pages_tab, a.profile, only_rows, a.retry_errors, need_gpm=not a.plan_only,
                       mode=a.mode)
    except RuntimeError as e:
        sys.exit(str(e))
    for m in prep["messages"]:
        log(m)
    items, pages, todo, skipped = prep["items"], prep["pages"], prep["todo"], prep["skipped"]
    state = load_state()
    if a.max:
        todo = todo[:a.max]

    n_wait = sum(1 for it, _ in skipped if it.get("waiting"))
    log(f"Sheet có {len(items)} hàng: sẽ đăng {len(todo)}, bỏ qua {len(skipped)}"
        + (f" (trong đó {n_wait} chưa tới giờ)" if n_wait else "")
        + f" · cách đăng: {'tới giờ mới đăng' if a.mode == 'due' else 'lên lịch trên Facebook'}"
        + ("" if a.webhook else " (không có webhook: trạng thái chỉ ghi vào logs/, không ghi vào Sheet)"))
    # Cột Profile -> id profile GPM (trống = --profile); gom các hàng cùng profile để mở trình duyệt 1 lần
    profile_names, gpm_ok = prep["profile_names"], prep["gpm_ok"]
    default_profile = resolve_profile_id(a.profile) if gpm_ok else a.profile
    groups, bad = {}, []
    for it, when in todo:
        try:
            if gpm_ok:
                pid = resolve_profile_id(it["profile"]) if it["profile"] else default_profile
            else:                                   # plan-only khi GPM chưa chạy: chỉ gom theo tên ghi trên Sheet
                pid = it["profile"] or default_profile
            groups.setdefault(pid, []).append((it, when))
        except Exception as e:
            bad.append((it, str(e)))
    for it, when in todo:
        pid = next((p for p, rows in groups.items() if any(r[0] is it for r in rows)), None)
        tm = (when.strftime('%d/%m/%Y %H:%M') if when
              else f"TỚI GIỜ {it['due']} -> đăng ngay" if it.get("due") else "ĐĂNG NGAY")
        log(f"  [hàng {it['row']}] {tm} | "
            f"profile: {profile_names.get(pid, it['profile'] or '?')} | trang: {it['page'] or '(mặc định)'} | "
            f"{(caption_of(it) or '(mô tả lấy từ kịch bản trong thư mục Drive)')[:50]} | {it['video'][:40]}")
    for it, reason in skipped:
        log(f"  [hàng {it['row']}] bỏ qua: {reason}")
    for it, reason in bad:
        log(f"  [hàng {it['row']}] LỖI profile: {reason}")
    if a.plan_only or not todo:
        return

    gid = sheet_csv_url(a.sheet, a.tab)[1] if not a.sheet.lower().endswith(".csv") else None
    if gid is None:
        gid = sheet_gid(a.sheet)
    results = [(it, when, "error", reason, "") for it, reason in [(b[0], b[1]) for b in bad]]
    for it, reason in bad:
        sheet_update(a.webhook, it["row"], gid, status=ST_ERROR, note=reason[:300])
    n_total = sum(len(rows) for rows in groups.values())
    n = 0
    with sync_playwright() as p:
        for pid, rows in groups.items():
            log(f"##### Profile GPM: {profile_names.get(pid, pid)} ({len(rows)} bài) #####")
            port = gpm_start(pid)
            time.sleep(5)
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = pick_page(browser.contexts[0])
            try:
                for it, when in rows:
                    n += 1
                    key = row_key(a.sheet, it)
                    log(f"===== Bài {n}/{n_total} – hàng {it['row']} =====")
                    info = {"expect_page": it["page"]}
                    if not a.dry_run:
                        sheet_update(a.webhook, it["row"], gid, status=ST_RUNNING, note="")
                    try:
                        asset = it.get("asset") or resolve_asset_id(page, it["page"], pid) or a.asset_id
                        video, caption = resolve_video(it)
                        ok = post_reel(page, video, caption, when, a.dry_run, asset, info=info)
                        status = "done" if ok else "dry-run"
                        note = f"Xác nhận {datetime.now():%H:%M %d/%m}" if ok else f"Chạy thử {datetime.now():%H:%M %d/%m}"
                        if it.get("due"):                 # mode due: ghi kèm giờ trên Sheet (và độ trễ nếu đáng kể)
                            late = int((datetime.now() - parse_schedule(it["due"])).total_seconds() // 60)
                            note += f" – lịch Sheet {it['due']}" + (f" (trễ {late} phút)" if late >= 15 else "")
                    except Exception as e:
                        shot(page, f"row{it['row']}_error")
                        status, note = "error", str(e)[:300]
                        log(f"LỖI hàng {it['row']}: {note}")
                        page = pick_page(browser.contexts[0])      # tab có thể đã hỏng, chọn lại
                    page_name = info.get("page") or ""
                    results.append((it, when, status, note, page_name))
                    if status in ("done", "error"):
                        state[key] = {"status": status, "note": note, "page": page_name,
                                      "profile": profile_names.get(pid, pid),
                                      "at": datetime.now().isoformat(timespec="seconds"),
                                      "when": when.isoformat() if when else "now"}
                        save_state(state)
                    st_text = {"done": ST_SCHEDULED if when else ST_POSTED, "error": ST_ERROR, "dry-run": ST_DRY}[status]
                    if sheet_update(a.webhook, it["row"], gid, page=page_name or None, status=st_text, note=note):
                        log(f"  Sheet hàng {it['row']}: Trạng thái = {st_text}" + (f", Page = {page_name}" if page_name else ""))
                    if n < n_total:
                        wait = random.randint(lo, hi)
                        log(f"Nghỉ {wait}s trước bài tiếp theo...")
                        time.sleep(wait)
            finally:
                if a.close_after:
                    gpm_stop(pid)

    # Báo cáo: file CSV để dán cột Trạng thái/Ghi chú ngược vào Sheet nếu muốn
    rep = LOG_DIR / f"sheet_report_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(rep, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Hàng", "Profile", "Page đăng", "Thời gian", "Trạng thái", "Ghi chú"])
        for it, when, status, note, page_name in results:
            w.writerow([it["row"], it["profile"], page_name,
                        when.strftime("%d/%m/%Y %H:%M") if when else (it.get("due") or "ngay"),
                        {"done": ST_SCHEDULED if when else ST_POSTED, "dry-run": ST_DRY, "error": ST_ERROR}[status], note])
    done = sum(1 for r in results if r[2] == "done")
    err = sum(1 for r in results if r[2] == "error")
    dry = sum(1 for r in results if r[2] == "dry-run")
    log(f"XONG: {done} thành công, {err} lỗi" + (f", {dry} chạy thử (dry-run)" if dry else "") + f". Báo cáo: {rep}")


if __name__ == "__main__":
    main()
