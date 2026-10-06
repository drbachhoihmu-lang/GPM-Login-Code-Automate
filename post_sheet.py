"""
Đăng / lên lịch hàng loạt Thước phim theo một Google Sheet chia sẻ công khai.

Sheet (hàng 1 là tiêu đề cột, tên cột linh hoạt – script nhận diện theo từ khoá):
  Video            : link file Drive / link thư mục Drive / đường dẫn file trên máy
  Tiêu đề          : mô tả bài (có thể kèm luôn hashtag)
  Hashtag          : (tuỳ chọn) hashtag riêng, sẽ nối vào sau tiêu đề
  Thời gian đăng   : "07/10/2026 19:00" (hoặc 2026-10-07 19:00); ghi "ngay" để đăng ngay; trống = bỏ qua
  Trạng thái       : trống = chờ đăng; ghi "bỏ qua" / "skip" để không đăng
  Ghi chú          : tuỳ ý

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
                       gpm_stop, log, parse_schedule, pick_page, post_reel, resolve_asset_id, resolve_profile_id, shot)

# Link Sheet mặc định (có thể thay bằng --sheet)
SHEET_URL = ""
# URL ứng dụng web Apps Script (xem sheet_webhook.gs) để ghi Page đăng / Trạng thái / Ghi chú ngược vào Sheet.
# Để trống = không ghi (vẫn có báo cáo CSV trong logs/).
SHEET_WEBHOOK = ""
STATE_FILE = LOG_DIR / "sheet_state.json"      # ghi nhớ hàng nào đã đăng để không đăng lại

# Giá trị cột Trạng thái do script ghi
ST_RUNNING, ST_SCHEDULED, ST_POSTED, ST_ERROR, ST_DRY = "Đang đăng", "Đã lên lịch", "Đã đăng", "Lỗi", "Chạy thử OK"


def sheet_update(webhook, row, **fields):
    """Ghi các ô (page/status/note) của hàng `row` qua Apps Script. Lỗi mạng chỉ cảnh báo, không dừng."""
    if not webhook:
        return False
    try:
        r = requests.post(webhook, json={"row": row, **fields}, timeout=30)
        if r.status_code != 200 or '"ok"' not in r.text:
            log(f"  (webhook trả về {r.status_code}: {r.text[:120]})")
            return False
        return True
    except Exception as e:
        log(f"  (không ghi được vào Sheet: {str(e)[:120]})")
        return False


# ----------------------------------------------------------------- đọc Sheet
def sheet_csv_url(link):
    m = re.search(r"/spreadsheets/d/([\w-]+)", link)
    if not m:
        raise ValueError(f"Không nhận ra link Google Sheet: {link}")
    # gid chỉ thêm khi link có (tab đầu của Sheet tạo từ CSV không phải gid=0; thiếu gid = tab đầu tiên)
    gid = re.search(r"[#&?]gid=(\d+)", link)
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")


def norm(s):
    """Chữ thường, bỏ dấu, bỏ ký tự thừa – để so tên cột."""
    s = unicodedata.normalize("NFKD", str(s or "").replace("Đ", "D").replace("đ", "d"))
    return re.sub(r"[^a-z0-9]+", " ", s.encode("ascii", "ignore").decode().lower()).strip()


COLUMN_KEYS = {           # tên cột chuẩn -> các từ khoá nhận diện (sau khi bỏ dấu)
    "profile": ("profile", "tai khoan", "account", "nick", "gpm"),
    "page":    ("page", "fanpage", "trang dang"),
    "video":   ("video", "link", "drive", "duong dan", "path", "file"),
    "title":   ("tieu de", "caption", "mo ta", "noi dung", "title"),
    "hashtag": ("hashtag", "hastag", "tag"),
    "time":    ("thoi gian", "gio dang", "lich", "ngay dang", "schedule", "time"),
    "status":  ("trang thai", "status"),
    "note":    ("ghi chu", "note"),
}


def map_columns(header):
    cols = {}
    for idx, name in enumerate(header):
        n = norm(name)
        if not n:
            continue
        for key, kws in COLUMN_KEYS.items():
            if key not in cols and any(kw in n for kw in kws):
                cols[key] = idx
                break
    missing = [k for k in ("video", "time") if k not in cols]
    if missing:
        raise RuntimeError(f"Sheet thiếu cột {missing}. Tiêu đề cột hiện có: {header}")
    return cols


def read_sheet(link):
    """Trả về danh sách dict: row (số hàng trên Sheet), page, video, title, hashtag, time, status, note.
    `link` là link Google Sheet công khai, hoặc đường dẫn file .csv trên máy (Sheet tải về)."""
    if link.lower().endswith(".csv") and Path(link).is_file():
        text = Path(link).read_text(encoding="utf-8-sig")
    else:
        r = requests.get(sheet_csv_url(link), timeout=60)
        if r.status_code != 200 or "<html" in r.text[:200].lower():
            raise RuntimeError("Không tải được Sheet – kiểm tra link và chia sẻ 'Bất kỳ ai có đường liên kết' (Người xem)")
        text = r.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise RuntimeError("Sheet trống")
    cols = map_columns(rows[0])
    # Bỏ dấu ' đầu ô (dùng để giữ "08/10/2026 19:00" là chữ, không bị Sheets đổi thành ngày)
    get = lambda row, key: (row[cols[key]].strip().lstrip("'").strip() if key in cols and cols[key] < len(row) else "")
    out = []
    for i, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        out.append({"row": i, "profile": get(row, "profile"), "page": get(row, "page"), "video": get(row, "video"),
                    "title": get(row, "title"),
                    "hashtag": get(row, "hashtag"), "time": get(row, "time"), "status": get(row, "status"),
                    "note": get(row, "note")})
    return out


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
def plan(items, sheet_link, state, only_rows=None, retry_errors=False):
    """Chia hàng thành: sẽ đăng (kèm when) / bỏ qua (kèm lý do).
    Trạng thái bắt đầu bằng 'đã' (Đã đăng / Đã lên lịch), 'bỏ qua', 'skip', 'done', 'xong' -> bỏ qua;
    'Lỗi' -> bỏ qua trừ khi --retry-errors; 'Đang đăng' (lần trước bị ngắt) -> làm lại."""
    todo, skipped = [], []
    for it in items:
        reason = None
        when = None
        st = norm(it["status"])
        if only_rows and it["row"] not in only_rows:
            reason = "không nằm trong --rows"
        elif st.startswith("da ") or st in ("bo qua", "skip", "done", "xong"):
            reason = f"trạng thái '{it['status']}'"
        elif st.startswith("loi") and not retry_errors:
            reason = f"trạng thái '{it['status']}' (thêm --retry-errors để đăng lại)"
        elif state.get(row_key(sheet_link, it), {}).get("status") == "done":
            reason = "đã đăng lần chạy trước (logs/sheet_state.json)"
        elif not it["video"]:
            reason = "thiếu video"
        elif not it["time"]:
            reason = "thiếu thời gian đăng"
        elif norm(it["time"]) in ("ngay", "now", "dang ngay"):
            when = None
        else:
            try:
                when = parse_schedule(it["time"])
            except ValueError as e:
                reason = str(e)
            else:
                if when <= datetime.now():
                    reason = f"thời gian {it['time']} đã qua"
        if reason:
            skipped.append((it, reason))
        else:
            todo.append((it, when))
    todo.sort(key=lambda x: (x[1] is not None, x[1] or datetime.min))     # "ngay" trước, rồi theo giờ
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


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Đăng/lên lịch Reels hàng loạt theo Google Sheet công khai")
    ap.add_argument("--sheet", default=SHEET_URL, help="Link Google Sheet (chia sẻ 'Bất kỳ ai có đường liên kết')")
    ap.add_argument("--max", type=int, default=0, help="Tối đa bao nhiêu bài trong lần chạy này (0 = tất cả)")
    ap.add_argument("--rows", help="Chỉ đăng các hàng này, ví dụ 2,3,7 (số hàng trên Sheet)")
    ap.add_argument("--dry-run", action="store_true", help="Làm hết các bước nhưng không bấm nút cuối")
    ap.add_argument("--plan-only", action="store_true", help="Chỉ in kế hoạch, không mở trình duyệt")
    ap.add_argument("--webhook", default=SHEET_WEBHOOK, help="URL Apps Script để ghi Trạng thái/Page ngược vào Sheet")
    ap.add_argument("--retry-errors", action="store_true", help="Đăng lại cả các hàng đang ghi 'Lỗi'")
    ap.add_argument("--pause", default="20-60", help="Nghỉ ngẫu nhiên giữa 2 bài, giây (mặc định 20-60)")
    ap.add_argument("--profile", default=PROFILE_ID, help="Profile GPM mặc định (id hoặc một phần tên) cho hàng không ghi cột Profile")
    ap.add_argument("--asset-id", default=PAGE_ASSET_ID)
    ap.add_argument("--close-after", action="store_true", help="Đóng profile GPM khi xong")
    a = ap.parse_args()
    if not a.sheet:
        sys.exit("Thiếu --sheet (hoặc điền SHEET_URL trong post_sheet.py)")
    only_rows = {int(x) for x in a.rows.split(",")} if a.rows else None
    lo, hi = (int(x) for x in a.pause.split("-")) if "-" in a.pause else (int(a.pause), int(a.pause))

    items = read_sheet(a.sheet)
    state = load_state()
    todo, skipped = plan(items, a.sheet, state, only_rows, a.retry_errors)
    if a.max:
        todo = todo[:a.max]

    log(f"Sheet có {len(items)} hàng: sẽ đăng {len(todo)}, bỏ qua {len(skipped)}"
        + ("" if a.webhook else " (không có webhook: trạng thái chỉ ghi vào logs/, không ghi vào Sheet)"))
    # Cột Profile -> id profile GPM (trống = --profile); gom các hàng cùng profile để mở trình duyệt 1 lần
    profile_names = dict(gpm_profiles())
    default_profile = resolve_profile_id(a.profile)
    groups, bad = {}, []
    for it, when in todo:
        try:
            pid = resolve_profile_id(it["profile"]) if it["profile"] else default_profile
            groups.setdefault(pid, []).append((it, when))
        except Exception as e:
            bad.append((it, str(e)))
    for it, when in todo:
        pid = next((p for p, rows in groups.items() if any(r[0] is it for r in rows)), None)
        log(f"  [hàng {it['row']}] {'ĐĂNG NGAY' if when is None else when.strftime('%d/%m/%Y %H:%M')} | "
            f"profile: {profile_names.get(pid, it['profile'] or '?')} | trang: {it['page'] or '(mặc định)'} | "
            f"{(caption_of(it) or '(mô tả lấy từ kịch bản trong thư mục Drive)')[:50]} | {it['video'][:40]}")
    for it, reason in skipped:
        log(f"  [hàng {it['row']}] bỏ qua: {reason}")
    for it, reason in bad:
        log(f"  [hàng {it['row']}] LỖI profile: {reason}")
    if a.plan_only or not todo:
        return

    results = [(it, when, "error", reason, "") for it, reason in [(b[0], b[1]) for b in bad]]
    for it, reason in bad:
        sheet_update(a.webhook, it["row"], status=ST_ERROR, note=reason[:300])
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
                        sheet_update(a.webhook, it["row"], status=ST_RUNNING, note="")
                    try:
                        asset = resolve_asset_id(page, it["page"], pid) or a.asset_id
                        video, caption = resolve_video(it)
                        ok = post_reel(page, video, caption, when, a.dry_run, asset, info=info)
                        status = "done" if ok else "dry-run"
                        note = f"Xác nhận {datetime.now():%H:%M %d/%m}" if ok else f"Chạy thử {datetime.now():%H:%M %d/%m}"
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
                    if sheet_update(a.webhook, it["row"], page=page_name or None, status=st_text, note=note):
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
            w.writerow([it["row"], it["profile"], page_name, when.strftime("%d/%m/%Y %H:%M") if when else "ngay",
                        {"done": ST_SCHEDULED if when else ST_POSTED, "dry-run": ST_DRY, "error": ST_ERROR}[status], note])
    done = sum(1 for r in results if r[2] == "done")
    err = sum(1 for r in results if r[2] == "error")
    dry = sum(1 for r in results if r[2] == "dry-run")
    log(f"XONG: {done} thành công, {err} lỗi" + (f", {dry} chạy thử (dry-run)" if dry else "") + f". Báo cáo: {rep}")


if __name__ == "__main__":
    main()
