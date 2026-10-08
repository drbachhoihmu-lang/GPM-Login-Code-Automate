"""
Đăng / lên lịch hàng loạt Thước phim theo một Google Sheet chia sẻ công khai.

Sheet gồm 3 tab (dựng bằng setupSheet() trong sheet_webhook.gs):
  "Lịch đăng" (hàng đợi, script đọc) – hàng 1 là tiêu đề cột, tên cột linh hoạt (nhận diện theo từ khoá):
    Ngày đăng + Giờ  : 2 cột "08/10/2026" | "19:00" (hoặc 1 cột gộp "08/10/2026 19:00"); ghi "ngay" để đăng ngay
                       Để TRỐNG cả hai -> tự xếp vào khung giờ mặc định trống kế tiếp của page (tính từ hiện tại,
                       không trùng bài đã có); chỉ ghi Ngày -> khung trống đầu tiên của ngày đó; chỉ ghi Giờ -> hôm nay/mai
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
                      Giờ đăng mặc định = các khung giờ, vd "07:00, 10:00, 12:00, 14:00, 18:00" (web sửa được: tab Giờ đăng)
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
from datetime import datetime, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

from post_reel import (LOG_DIR, PAGE_ASSET_ID, PROFILE_ID, VIDEO_EXT, fetch_from_drive, gpm_check, gpm_profiles, gpm_start,
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


def parse_hours(s):
    """Khung giờ của page: '07, 10, 12:30, 14h, 18' / '07:00; 10:00' / list -> ['07:00', '10:00', ...] (sắp xếp, bỏ trùng)."""
    if isinstance(s, (list, tuple)):
        s = ",".join(str(x) for x in s)
    out = set()
    for tok in re.split(r"[,;/|\n]+|\s+", str(s or "")):
        t = norm_time(tok)
        if re.fullmatch(r"\d{2}:\d{2}", t) and int(t[:2]) < 24 and int(t[3:]) < 60:
            out.add(t)
    return sorted(out)


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
        if "date" not in cols:                   # bố cục gộp "dd/mm/yyyy hh:mm" (hoặc chỉ ngày / chỉ giờ)
            parts = t.split()
            if len(parts) >= 2:
                d, t = parts[0], " ".join(parts[1:])
            elif re.match(r"^(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4}|\d{4}-\d{1,2}-\d{1,2})$", t):
                d, t = t, ""
            else:
                d = ""
        immediate = norm(d) in ("ngay", "now", "dang ngay") or norm(t) in ("ngay", "now", "dang ngay")
        date = "" if immediate else norm_date(d)
        hour = "" if immediate else norm_time(t)
        if immediate:
            when = "ngay"
        elif date and hour:
            when = f"{date} {hour}"
        else:
            when = ""                            # thiếu ngày và/hoặc giờ -> tự xếp theo khung giờ của page (assign_slots)
        out.append({"row": i, "profile": get(row, "profile"), "page": get(row, "page"), "video": get(row, "video"),
                    "title": get(row, "title"), "hashtag": get(row, "hashtag"), "time": when,
                    "date": date, "hour": hour, "date_only": bool(date and not hour),
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
def plan(items, sheet_link, state, only_rows=None, retry_errors=False, mode="schedule", now=None, horizon_days=None):
    """Chia hàng thành: sẽ đăng (kèm when) / bỏ qua (kèm lý do).
    Trạng thái trống hoặc 'Chờ đăng' -> sẽ đăng. 'Đã …' (Đã đăng / Đã lên lịch), 'bỏ qua', 'skip', 'done',
    'xong' -> bỏ qua; 'Lỗi' (trên Sheet hoặc trong logs/sheet_state.json) -> bỏ qua trừ khi retry_errors;
    'Đang đăng' (lần trước bị ngắt) -> làm lại.
    mode="schedule": when = giờ trên Sheet (lên lịch trên Facebook); giờ đã qua -> bỏ qua.
    mode="due":      giờ chưa tới -> để lại (it['waiting']=True, lý do 'chưa tới giờ');
                     giờ đã tới   -> đăng ngay (when=None, it['due'] = giờ ghi trên Sheet).
    horizon_days (mode schedule): chỉ lên lịch bài có ngày <= hôm nay + N ngày (1 = tới hết ngày mai);
                     bài xa hơn để lại (waiting, it['wake_at'] = 0h ngày nó tới lượt). None = không giới hạn."""
    now = now or datetime.now()
    limit_date = now.date() + timedelta(days=horizon_days) if horizon_days is not None and horizon_days >= 0 else None
    todo, skipped = [], []
    for it in items:
        reason = None
        when = None
        it["waiting"], it["due"], it["wake_at"], it["wait_label"] = False, "", None, ""
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
        elif not it["time"]:
            reason = it.get("auto_fail") or "thiếu ngày/giờ đăng"
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
                        it["waiting"], it["wake_at"], it["wait_label"] = True, when, "chưa tới giờ"
                        reason = f"chưa tới giờ ({when:%H:%M %d/%m})"
                    else:
                        when = None                       # tới giờ -> bấm "Chia sẻ ngay"
                elif when <= now:
                    reason = f"thời gian {it['time']} đã qua"
                elif limit_date and when.date() > limit_date:     # ngoài giới hạn ngày -> để lại, tới ngày mới lên lịch
                    it["waiting"], it["wait_label"] = True, "chưa tới lượt"
                    it["wake_at"] = datetime.combine(when.date() - timedelta(days=horizon_days), datetime.min.time())
                    reason = (f"chưa tới lượt: hôm nay chỉ lên lịch tới hết {limit_date:%d/%m}, bài này {when:%H:%M %d/%m} "
                              f"(sẽ lên lịch từ 0h {it['wake_at']:%d/%m})")
        if reason:
            skipped.append((it, reason))
        else:
            todo.append((it, when))
    # "ngay" trước, rồi theo giờ (mode due: theo giờ ghi trên Sheet, bài tới giờ sớm nhất đăng trước)
    todo.sort(key=lambda x: (x[1] is not None, x[1] or (parse_schedule(x[0]["due"]) if x[0]["due"] else datetime.min)))
    return todo, skipped


# ----------------------------------------------------------------- tự xếp giờ theo khung giờ của page
def slot_candidates(hours, start, only_date=None, days=60):
    """Các mốc (datetime) theo khung giờ `hours` ['07:00', ...] từ `start` trở đi; chỉ trong ngày `only_date` nếu có."""
    if not hours:
        return
    day0 = only_date or start.date()
    for n in range(1 if only_date else days):
        day = day0 + timedelta(days=n)
        for h in hours:
            dt = datetime.combine(day, datetime.strptime(h, "%H:%M").time())
            if dt >= start:
                yield dt


def assign_slots(items, sheet_link, state, mode="schedule", now=None, lead_min=20, window_min=20, retry_errors=False):
    """Hàng chưa đủ ngày+giờ -> tự xếp vào khung giờ mặc định của page (it['hours']), tính từ hiện tại, tránh trùng
    mốc đã có (hàng khác trên Sheet + bài đã đăng/lên lịch trong logs/sheet_state.json, kể cả hàng đã xoá khỏi Sheet).
    Ghi vào it['time'] (dd/mm/yyyy HH:MM), it['date'], it['hour'], it['auto']=True; không xếp được -> it['auto_fail'].
      schedule: mốc phải cách hiện tại >= lead_min phút (Facebook cần thời gian lên lịch ở tương lai).
      due:      mốc tính từ (hiện tại - window_min) để bài có khung giờ vừa qua được đăng ngay ở lần kiểm tra này.
    Hàm thuần theo (Sheet, state, now): không cần lưu gì, chạy lại cho cùng kết quả.
    Trả về dict occupied {khoá page: set(datetime)} để tính khung trống sắp tới."""
    now = now or datetime.now()
    occupied = {}

    def keys_of(asset, name):
        return [k for k in (str(asset or "").strip(), norm(name)) if k]

    def add_occ(asset, name, dt):
        for k in keys_of(asset, name):
            occupied.setdefault(k, set()).add(dt)

    def is_occ(asset, name, dt):
        return any(dt in occupied.get(k, ()) for k in keys_of(asset, name))

    def skip_row(it):
        st = norm(it["status"])
        return st in ("bo qua", "skip", "done", "xong") or (st.startswith("loi") and not retry_errors)

    for it in items:
        it.setdefault("auto", False)
        it["auto_fail"] = ""
        if it["time"] and it["time"] != "ngay" and not skip_row(it):
            try:
                add_occ(it.get("asset"), it.get("page"), parse_schedule(it["time"]))
            except ValueError:
                pass
    for st in state.values():                      # bài đã đăng / lên lịch (mốc trong 1 ngày qua trở đi)
        slot = st.get("slot") or (st.get("when") if st.get("when") not in (None, "now") else None)
        if st.get("status") == "done" and slot:
            try:
                dt = datetime.fromisoformat(slot)
            except ValueError:
                continue
            if dt > now - timedelta(days=1):
                add_occ(st.get("asset"), st.get("page"), dt)

    start = now + timedelta(minutes=lead_min) if mode == "schedule" else now - timedelta(minutes=window_min)
    for it in items:
        if it["time"] or skip_row(it) or norm(it["status"]).startswith("da "):
            continue
        prev = state.get(row_key(sheet_link, it), {})
        if prev.get("status") == "done" or (prev.get("status") == "error" and not retry_errors):
            continue
        only_date = None
        if it.get("date"):
            try:
                only_date = datetime.strptime(it["date"], "%d/%m/%Y").date()
            except ValueError:
                it["auto_fail"] = f"không hiểu ngày '{it['date']}'"
                continue
        hours = [it["hour"]] if it.get("hour") else list(it.get("hours") or [])
        if not hours:
            it["auto_fail"] = "page chưa có khung giờ mặc định (tab Giờ đăng trên web, hoặc cột Giờ đăng mặc định ở tab Pages)"
            continue
        pick = next((dt for dt in slot_candidates(hours, start, only_date) if not is_occ(it.get("asset"), it.get("page"), dt)), None)
        if pick is None:
            it["auto_fail"] = (f"ngày {it['date']} đã qua hoặc không còn khung giờ trống" if only_date
                               else "không còn khung giờ trống trong 60 ngày tới")
            continue
        it["time"], it["date"], it["hour"], it["auto"] = pick.strftime("%d/%m/%Y %H:%M"), pick.strftime("%d/%m/%Y"), pick.strftime("%H:%M"), True
        add_occ(it.get("asset"), it.get("page"), pick)
    return occupied


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
            retry_errors=False, need_gpm=False, mode="schedule", page_hours=None, lead_min=20, window_min=20, now=None,
            horizon_days=1):
    """Đọc Sheet + tab Pages, gắn asset/profile/khung giờ cho từng hàng, tự xếp giờ cho hàng trống, chia sẽ-đăng / bỏ-qua.
    Dùng chung cho dòng lệnh, giao diện web và bộ tự động kiểm tra.
    page_hours: {asset_id hoặc tên page: ['07:00', ...]} sửa trên web (ghi đè cột Giờ đăng mặc định của tab Pages).
    Trả về dict: items, pages (kèm hours / hours_src / next_free), todo, skipped, profile_names, gpm_ok, messages, mode,
    horizon_days: mode schedule chỉ lên lịch tới hôm nay + N ngày (mặc định 1 = tới hết ngày mai; None = không giới hạn).
    next_due: mốc sớm nhất cần thức dậy (bài chưa tới giờ ở mode due / 0h ngày bài tới lượt ở mode schedule), None nếu không có."""
    if mode not in POST_MODES:
        mode = "schedule"
    now = now or datetime.now()
    page_hours = page_hours or {}

    def hours_of(asset, name):
        web = page_hours.get(str(asset or "").strip()) or next((v for k, v in page_hours.items() if norm(k) == norm(name)), None)
        return (parse_hours(web), "web") if web else (None, "")
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
    for p in pages:                                  # khung giờ: web ghi đè > cột Giờ đăng mặc định trên Sheet
        p["sheet_hours"] = parse_hours(p.get("hour"))
        web, src = hours_of(p["asset"], p["name"])
        p["hours"], p["hours_src"] = (web, "web") if web else (p["sheet_hours"], "sheet" if p["sheet_hours"] else "")
    for it in items:
        p = lookup_page(pages, it["page"]) if it["page"] else None
        if p is None and not it["page"] and pages:
            p = default_page_of(it["profile"] or profile_names.get(default_profile, default_profile))
        it["asset"] = p["asset"] if p else ""
        if p and not it["profile"]:
            it["profile"] = p["profile"]
        if p:
            it["page"], it["hours"] = p["name"], p["hours"]
        else:                                        # không có tab Pages: khung giờ theo tên page sửa trên web
            it["hours"] = hours_of("", it["page"])[0] or []
    state = load_state()
    occupied = assign_slots(items, link, state, mode, now, lead_min, window_min, retry_errors)
    todo, skipped = plan(items, link, state, only_rows, retry_errors, mode, now, horizon_days)
    start = now + timedelta(minutes=lead_min) if mode == "schedule" else now
    for p in pages:                                  # 3 khung giờ trống sắp tới của mỗi page (xem ở tab Giờ đăng)
        free = []
        for dt in slot_candidates(p["hours"], start):
            if not any(dt in occupied.get(k, ()) for k in (p["asset"], norm(p["name"])) if k):
                free.append(dt.strftime("%H:%M %d/%m"))
                if len(free) >= 3:
                    break
        p["next_free"] = free
    waiting = [it["wake_at"] for it, _ in skipped if it.get("waiting") and it.get("wake_at")]
    return {"items": items, "pages": pages, "todo": todo, "skipped": skipped,
            "profile_names": profile_names, "gpm_ok": gpm_ok, "messages": msgs,
            "mode": mode, "horizon_days": horizon_days, "next_due": min(waiting) if waiting else None}


# ----------------------------------------------------------------- quét profile / fanpage
def cmd_check_gpm():
    """Chẩn đoán GPM (chỉ đọc): API, proxy từng profile, thư mục cài, lỗi mới trong log GPM."""
    d = gpm_check()
    log(f"GPM Local API: {'OK – ' + d['version'] if d['api_ok'] else 'KHÔNG kết nối được'}")
    for p in d["profiles"]:
        st = "chưa kiểm" if p["proxy_ok"] is None else ("OK" if p["proxy_ok"] else f"LỖI ({p['proxy_err']})")
        print(f"   {p['name']:28s} {p['browser']:22s} proxy {p['proxy'] or '(không có)':24s} {st}"
              + (f" {p['proxy_ms']} ms" if p["proxy_ms"] is not None else ""))
    if d["install"].get("exe"):
        print(f"   Thư mục cài: {Path(d['install']['exe']).parent}  |  dữ liệu vân tay: {'đủ' if d['install']['fonts_map_ok'] else 'THIẾU ' + d['install']['fonts_map']}")
    for ln in d["log_errors"]:
        print("   log GPM:", ln)
    for a in d["advice"]:
        log("=> " + a)


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
    ap.add_argument("--check-gpm", action="store_true", help="Chẩn đoán GPM: API, proxy từng profile, thư mục cài, log lỗi (chỉ đọc)")
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
    ap.add_argument("--config", help="config.json của giao diện web: lấy khung giờ page sửa trên web (page_hours) + slot_lead_min")
    ap.add_argument("--lead", type=int, default=None, help="Tự xếp giờ: khung giờ phải cách hiện tại ít nhất N phút (mặc định 20)")
    ap.add_argument("--window", type=int, default=20, help="Mode due: khung giờ vừa qua trong N phút vẫn được đăng (mặc định 20)")
    ap.add_argument("--horizon", type=int, default=1,
                    help="Mode schedule: chỉ lên lịch bài tới hôm nay + N ngày (mặc định 1 = tới hết ngày mai; 0 = chỉ hôm nay; -1 = không giới hạn)")
    ap.add_argument("--profile", default=PROFILE_ID, help="Profile GPM mặc định (id hoặc một phần tên) cho hàng không ghi cột Profile")
    ap.add_argument("--asset-id", default=PAGE_ASSET_ID)
    ap.add_argument("--close-after", action="store_true", help="Đóng profile GPM khi xong")
    a = ap.parse_args()
    if a.check_gpm:
        return cmd_check_gpm()
    if a.list_profiles:
        return cmd_list_profiles()
    if a.scan_pages:
        return cmd_scan_pages(a.scan_pages, a.webhook, a.close_after)
    if not a.sheet:
        sys.exit("Thiếu --sheet (hoặc điền SHEET_URL trong post_sheet.py)")
    only_rows = {int(x) for x in a.rows.split(",")} if a.rows else None
    lo, hi = (int(x) for x in a.pause.split("-")) if "-" in a.pause else (int(a.pause), int(a.pause))

    page_hours, lead = {}, 20
    if a.config:
        try:
            cfgj = json.loads(Path(a.config).read_text(encoding="utf-8"))
            page_hours = cfgj.get("page_hours") or {}
            lead = int(cfgj.get("slot_lead_min", 20))
        except Exception as e:
            log(f"(không đọc được {a.config}: {str(e)[:80]})")
    if a.lead is not None:
        lead = a.lead
    try:
        prep = prepare(a.sheet, a.tab, a.pages_tab, a.profile, only_rows, a.retry_errors, need_gpm=not a.plan_only,
                       mode=a.mode, page_hours=page_hours, lead_min=lead, window_min=a.window,
                       horizon_days=a.horizon if a.horizon >= 0 else None)
    except RuntimeError as e:
        sys.exit(str(e))
    for m in prep["messages"]:
        log(m)
    items, pages, todo, skipped = prep["items"], prep["pages"], prep["todo"], prep["skipped"]
    state = load_state()
    if a.max:
        todo = todo[:a.max]

    n_wait = sum(1 for it, _ in skipped if it.get("waiting"))
    limit_txt = (f" tới hết {(datetime.now() + timedelta(days=a.horizon)):%d/%m}" if a.mode == "schedule" and a.horizon >= 0 else "")
    log(f"Sheet có {len(items)} hàng: sẽ đăng {len(todo)}, bỏ qua {len(skipped)}"
        + (f" (trong đó {n_wait} {'chưa tới giờ' if a.mode == 'due' else 'chưa tới lượt'})" if n_wait else "")
        + f" · cách đăng: {'tới giờ mới đăng' if a.mode == 'due' else 'lên lịch trên Facebook' + limit_txt}"
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
        tm += " (tự xếp)" if it.get("auto") else ""
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
                    if not a.dry_run:                    # hàng tự xếp giờ: ghi luôn Ngày/Giờ đã xếp vào Sheet
                        extra = {"date": it["date"], "time": it["hour"]} if it.get("auto") else {}
                        sheet_update(a.webhook, it["row"], gid, status=ST_RUNNING, note="", **extra)
                    try:
                        asset = it.get("asset") or resolve_asset_id(page, it["page"], pid) or a.asset_id
                        video, caption = resolve_video(it)
                        ok = post_reel(page, video, caption, when, a.dry_run, asset, info=info)
                        status = "done" if ok else "dry-run"
                        note = f"Xác nhận {datetime.now():%H:%M %d/%m}" if ok else f"Chạy thử {datetime.now():%H:%M %d/%m}"
                        if it.get("due"):                 # mode due: ghi kèm khung giờ (và độ trễ nếu đáng kể)
                            late = int((datetime.now() - parse_schedule(it["due"])).total_seconds() // 60)
                            note += (f" – tự xếp {it['due']}" if it.get("auto") else f" – lịch Sheet {it['due']}") \
                                    + (f" (trễ {late} phút)" if late >= 15 else "")
                        elif it.get("auto"):
                            note += f" – tự xếp {it['time']}"
                    except Exception as e:
                        shot(page, f"row{it['row']}_error")
                        status, note = "error", str(e)[:300]
                        log(f"LỖI hàng {it['row']}: {note}")
                        page = pick_page(browser.contexts[0])      # tab có thể đã hỏng, chọn lại
                    page_name = info.get("page") or ""
                    results.append((it, when, status, note, page_name))
                    if status in ("done", "error"):
                        slot = when or (parse_schedule(it["due"]) if it.get("due") else None)
                        state[key] = {"status": status, "note": note, "page": page_name, "asset": it.get("asset") or "",
                                      "profile": profile_names.get(pid, pid),
                                      "at": datetime.now().isoformat(timespec="seconds"),
                                      "when": when.isoformat() if when else "now",
                                      "slot": slot.isoformat() if slot else None, "auto": bool(it.get("auto"))}
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
