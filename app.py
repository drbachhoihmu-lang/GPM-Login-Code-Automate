"""
Giao diện web cục bộ cho bộ công cụ đăng Reels: xem Sheet, profile GPM <-> fanpage, mở/đóng Chrome,
chạy kế hoạch / chạy thử / đăng thật và xem log ngay trên trình duyệt.

Chạy:  python app.py          (mở http://127.0.0.1:8765)
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
import post_reel as pr                                   # noqa: E402
import post_sheet as ps                                  # noqa: E402

ROOT = Path(__file__).resolve().parent
LOG_DIR = pr.LOG_DIR
CONFIG_FILE = ROOT / "config.json"
UI_PORT = 8765
DEFAULT_CONFIG = {
    "sheet_url": "https://docs.google.com/spreadsheets/d/1966ErI9SHTrLOFM1MPor3EPovJOz6CNsWPH7Q4b_aQ8/edit",
    "webhook": "",
    "default_profile": pr.PROFILE_ID,
    "tab": ps.TAB_QUEUE,
    "pages_tab": ps.TAB_PAGES,
    "auto_check": False,          # tự động kiểm tra Sheet và đăng bài chờ
    "auto_interval": 5,           # phút
    "post_mode": "schedule",      # schedule = lên lịch trên Facebook | due = tới giờ mới đăng (Chia sẻ ngay)
    "page_hours": {},             # {asset_id: ["07:00", "10:00", ...]} khung giờ sửa trên web (ghi đè tab Pages)
    "slot_lead_min": 20,          # tự xếp giờ: khung giờ phải cách hiện tại ít nhất N phút
    "schedule_horizon_days": 1,   # mode schedule: chỉ lên lịch tới hôm nay + N ngày (1 = tới hết ngày mai)
}

app = FastAPI(title="GPMReels")
app.mount("/logs", StaticFiles(directory=str(LOG_DIR)), name="logs")
app.mount("/assets", StaticFiles(directory=str(ROOT / "assets")), name="assets")


# ----------------------------------------------------------------- cấu hình
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


class Config(BaseModel):
    sheet_url: str = ""
    webhook: str = ""
    default_profile: str = ""
    tab: str = ps.TAB_QUEUE
    pages_tab: str = ps.TAB_PAGES
    auto_check: bool = False
    auto_interval: int = 5
    post_mode: str = "schedule"
    slot_lead_min: int = 20
    schedule_horizon_days: int = 1


def post_mode_of(cfg):
    return cfg.get("post_mode") if cfg.get("post_mode") in ps.POST_MODES else "schedule"


def horizon_of(cfg):
    try:
        return max(0, min(60, int(cfg.get("schedule_horizon_days", 1))))
    except (TypeError, ValueError):
        return 1


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/config")
def get_config():
    return load_config()


@app.post("/api/config")
def set_config(c: Config):
    cfg = load_config()
    before = horizon_of(cfg)
    cfg.update(c.model_dump(exclude_unset=True))      # chỉ ghi đè các khoá được gửi lên
    if not cfg.get("default_profile"):
        cfg["default_profile"] = pr.PROFILE_ID
    cfg["post_mode"] = post_mode_of(cfg)
    cfg["schedule_horizon_days"] = horizon_of(cfg)
    if cfg["schedule_horizon_days"] != before:
        AUTO["last_tick"] = 0.0                       # đổi giới hạn ngày -> bộ tự động kiểm tra lại ngay
    save_config(cfg)
    return cfg


# ----------------------------------------------------------------- profile GPM
_proc_cache = {"t": 0, "ids": {}}


def running_profiles():
    """{profile_id: có cổng debug?} cho các profile đang có chrome chạy (cache 5s)."""
    if time.time() - _proc_cache["t"] < 20:      # cache 20s: gọi PowerShell ít thôi
        return _proc_cache["ids"]
    ids = {}
    try:
        ps_cmd = ("Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
                  "Where-Object { $_.CommandLine -match 'user-data-dir' -and $_.CommandLine -notmatch '--type=' } | "
                  "ForEach-Object { $_.CommandLine }")
        out = pr.run_hidden(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, timeout=20).stdout
        for line in out.splitlines():
            m = re.search(r"Profile\\([0-9a-f-]{36})", line)
            if m:
                ids[m.group(1)] = "remote-debugging-port" in line
    except Exception:
        pass
    _proc_cache.update(t=time.time(), ids=ids)
    return ids


def debug_port(profile_id):
    f = LOG_DIR / f"port_{profile_id[:8]}.txt"
    try:
        port = int(f.read_text().strip())
        requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2).raise_for_status()
        return port
    except Exception:
        return None


def pages_for_profile(profile_id, profile_name, sheet_pages):
    """Fanpage gắn với profile theo tab Pages (mỗi profile thường = 1 fanpage; nhiều hàng thì liệt kê hết)."""
    out = []
    for p in sheet_pages:
        prof = (p.get("profile") or "").strip()
        if prof and (pr.norm_text(prof) in pr.norm_text(profile_name) or prof == profile_id or profile_id.startswith(prof.lower())):
            out.append({"name": p["name"], "asset": p["asset"]})
    return out


@app.get("/api/profiles")
def api_profiles():
    cfg = load_config()
    try:
        profiles = pr.gpm_profiles()
        gpm_ok = True
    except Exception:
        profiles, gpm_ok = [], False
    sheet_pages = []
    try:
        sheet_pages = ps.read_pages(cfg["sheet_url"], cfg["pages_tab"])
    except Exception:
        pass
    run = running_profiles()
    items = []
    for pid, name in profiles:
        port = debug_port(pid)
        if port:
            status = "controlled"
        elif pid in run:
            status = "manual"
        else:
            status = "closed"
        items.append({"id": pid, "name": name, "status": status, "port": port,
                      "default": pid == cfg["default_profile"],
                      "pages": pages_for_profile(pid, name, sheet_pages)})
    return {"gpm_ok": gpm_ok, "profiles": items}


class OpenReq(BaseModel):
    url: str | None = None


def _connect(port):
    from playwright.sync_api import sync_playwright
    return sync_playwright, port


def open_in_profile(profile_id, url):
    """Mở profile (nếu chưa) và điều hướng tab tới url. Chạy trong thread riêng."""
    from playwright.sync_api import sync_playwright
    port = pr.gpm_start(profile_id)
    time.sleep(4)
    if url:
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = pr.pick_page(b.contexts[0])
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
    return port


@app.post("/api/profiles/{pid}/start")
def api_start(pid: str, req: OpenReq):
    try:
        port = open_in_profile(pid, req.url)
    except Exception as e:
        raise HTTPException(500, str(e)[:300])
    _proc_cache["t"] = 0
    return {"ok": True, "port": port}


@app.post("/api/profiles/{pid}/stop")
def api_stop(pid: str):
    pr.gpm_stop(pid)
    time.sleep(2)
    _proc_cache["t"] = 0
    return {"ok": True}


@app.get("/api/gpm_check")
def api_gpm_check():
    """Chẩn đoán GPM (chỉ đọc): API, proxy từng profile, thư mục cài, lỗi mới trong log GPM."""
    return pr.gpm_check()


# ----------------------------------------------------------------- Sheet
def window_of(cfg):
    """Mode due: khung giờ vừa qua trong N phút vẫn được đăng – phải phủ được chu kỳ kiểm tra."""
    return max(20, int(cfg.get("auto_interval") or 5) + 5)


def compute_plan(cfg, retry_errors=False):
    """Kế hoạch đăng theo Sheet (dùng chung cho /api/sheet và bộ tự động)."""
    link = cfg["sheet_url"]
    prep = ps.prepare(link, cfg["tab"], cfg["pages_tab"], cfg["default_profile"], None, retry_errors,
                      mode=post_mode_of(cfg), page_hours=cfg.get("page_hours") or {},
                      lead_min=int(cfg.get("slot_lead_min", 20)), window_min=window_of(cfg), horizon_days=horizon_of(cfg))
    rows = []
    for it, when in prep["todo"]:
        rows.append({**it, "when": when.strftime("%d/%m/%Y %H:%M") if when else (it.get("due") or "ngay"),
                     "will": True, "reason": ""})
    for it, reason in prep["skipped"]:
        rows.append({**it, "when": it["time"], "will": False, "reason": reason})
    rows.sort(key=lambda r: r["row"])
    prep["rows"] = rows
    return prep


@app.get("/api/sheet")
def api_sheet():
    cfg = load_config()
    link = cfg["sheet_url"]
    try:
        sid = ps.sheet_id(link)
    except Exception:
        return {"ok": False, "error": "Chưa có link Sheet hợp lệ"}
    try:
        tabs = ps.sheet_tabs(link)
        prep = compute_plan(cfg)
        gid = tabs.get(cfg["tab"])
        return {"ok": True, "id": sid, "tabs": tabs, "queue_gid": gid, "rows": prep["rows"], "pages": prep["pages"],
                "gpm_ok": prep["gpm_ok"], "pending": sum(1 for r in prep["rows"] if r["will"]),
                "waiting": sum(1 for r in prep["rows"] if r.get("waiting")), "mode": prep["mode"], "horizon": horizon_of(cfg),
                "next_due": prep["next_due"].strftime("%H:%M %d/%m/%Y") if prep.get("next_due") else None}
    except Exception as e:
        return {"ok": False, "id": sid, "error": str(e)[:300]}


# ----------------------------------------------------------------- khung giờ đăng mặc định của từng page
class HoursReq(BaseModel):
    asset: str | None = None          # page (asset_id); None + all=True = áp dụng cho mọi page
    hours: list[str] | None = None    # None = bỏ bản sửa trên web, dùng lại cột Giờ đăng mặc định của Sheet
    all: bool = False
    lead: int | None = None           # slot_lead_min


@app.get("/api/page_hours")
def api_page_hours():
    cfg = load_config()
    try:
        prep = compute_plan(cfg)
    except Exception as e:
        return {"ok": False, "error": str(e)[:300], "lead": int(cfg.get("slot_lead_min", 20)), "pages": []}
    web = cfg.get("page_hours") or {}
    pages = [{"name": p["name"], "asset": p["asset"], "profile": p.get("profile", ""),
              "sheet_hours": p.get("sheet_hours", []), "web_hours": web.get(p["asset"]),
              "hours": p.get("hours", []), "src": p.get("hours_src", ""), "next_free": p.get("next_free", [])}
             for p in prep["pages"]]
    return {"ok": True, "lead": int(cfg.get("slot_lead_min", 20)), "mode": prep["mode"], "pages": pages,
            "webhook": bool(cfg.get("webhook"))}


@app.post("/api/page_hours")
def api_page_hours_set(r: HoursReq):
    cfg = load_config()
    msg = ""
    if r.lead is not None:
        cfg["slot_lead_min"] = max(0, min(1440, int(r.lead)))
    if r.all or r.asset:
        hours = ps.parse_hours(r.hours) if r.hours is not None else None
        if r.hours is not None and not hours:
            raise HTTPException(400, "Không có giờ hợp lệ (vd 07:00, 10:00, 14:00)")
        ph = dict(cfg.get("page_hours") or {})
        try:
            pages = ps.read_pages(cfg["sheet_url"], cfg["pages_tab"]) if r.all else []
        except Exception:
            pages = []
        targets = [p["asset"] for p in pages if p.get("asset")] if r.all else [r.asset]
        for a in targets:
            if hours is None:
                ph.pop(a, None)
            else:
                ph[a] = hours
        cfg["page_hours"] = ph
        if cfg.get("webhook") and hours is not None:          # có webhook: ghi luôn vào cột Giờ đăng mặc định trên Sheet
            try:
                body = {"action": "upsert_pages", "pages": [{"asset": a, "hours": ", ".join(hours)} for a in targets]}
                rr = requests.post(cfg["webhook"], json=body, timeout=30)
                msg = "đã ghi vào tab Pages trên Sheet" if rr.status_code == 200 and '"ok"' in rr.text else f"Sheet: {rr.text[:100]}"
            except Exception as e:
                msg = f"không ghi được vào Sheet: {str(e)[:100]}"
    save_config(cfg)
    out = api_page_hours()
    out["message"] = msg
    return out


# ----------------------------------------------------------------- tự động kiểm tra Sheet
# next_due: (mode "tới giờ mới đăng") mốc thời gian của bài chưa tới giờ sớm nhất -> vòng lặp thức đúng lúc đó,
# không phải đợi hết chu kỳ N phút.
AUTO = {"last": None, "result": "", "next": None, "last_tick": 0.0, "job": None, "next_due": None}


def auto_check_once(cfg):
    """Một lần kiểm tra: có bài chờ đăng + không có việc đang chạy + GPM đang bật -> chạy đăng thật.
    Mode due: bài chưa tới giờ chỉ được ghi nhớ (next_due) để đăng đúng giờ."""
    AUTO["next_due"] = None
    if any(j["proc"].poll() is None for j in JOBS.values()):
        return "đang có việc khác chạy, bỏ qua lần này"
    prep = compute_plan(cfg)
    if prep.get("next_due"):
        AUTO["next_due"] = prep["next_due"].timestamp()
    pending = [r for r in prep["rows"] if r["will"]]
    waiting = [r for r in prep["rows"] if r.get("waiting")]
    due_mode = prep["mode"] == "due"
    if not pending:
        if waiting and due_mode:
            return f"{len(waiting)} bài chưa tới giờ, gần nhất {prep['next_due']:%H:%M %d/%m} – sẽ đăng đúng lúc đó"
        if waiting:
            return (f"{len(waiting)} bài chưa tới lượt (chỉ lên lịch tới hôm nay + {horizon_of(cfg)} ngày), "
                    f"lượt kế từ 0h {prep['next_due']:%d/%m}")
        return "không có bài chờ đăng"
    if not prep["gpm_ok"]:
        return f"có {len(pending)} bài {'tới giờ' if due_mode else 'chờ'} nhưng GPM Login chưa chạy"
    wl = "chưa tới giờ" if due_mode else "chưa tới lượt"
    jid = _spawn(_base_args(cfg), f"Tự động: {'đăng' if due_mode else 'lên lịch'} {len(pending)} bài"
                 + (f" ({len(waiting)} bài khác {wl})" if waiting else ""))
    AUTO["job"] = jid
    return (f"đã bắt đầu {'đăng' if due_mode else 'lên lịch'} {len(pending)} bài (job {jid})"
            + (f"; {len(waiting)} bài {wl}" if waiting else ""))


def auto_loop():
    while True:
        try:
            cfg = load_config()
            interval = max(1, int(cfg.get("auto_interval") or 5)) * 60
            if cfg.get("auto_check"):
                now = time.time()
                due_hit = bool(AUTO.get("next_due")) and now >= AUTO["next_due"] and now - AUTO["last_tick"] >= 20
                if now - AUTO["last_tick"] >= interval or due_hit:
                    AUTO["last_tick"] = now
                    AUTO["last"] = datetime.now().strftime("%H:%M:%S %d/%m")
                    try:
                        AUTO["result"] = auto_check_once(cfg)
                    except Exception as e:
                        AUTO["result"] = f"lỗi: {str(e)[:200]}"
                    print(f"[auto] {AUTO['last']}: {AUTO['result']}", flush=True)
                nxt = AUTO["last_tick"] + interval
                if AUTO.get("next_due") and AUTO["last_tick"] < AUTO["next_due"] < nxt:
                    nxt = AUTO["next_due"]           # thức sớm hơn: đúng giờ bài tới lượt
                AUTO["next"] = datetime.fromtimestamp(nxt).strftime("%H:%M:%S")
            else:
                AUTO["next"] = None
        except Exception as e:
            print(f"[auto] lỗi vòng lặp: {e!r}", flush=True)
        time.sleep(10)


@app.on_event("startup")
def _start_auto():
    threading.Thread(target=auto_loop, daemon=True).start()


class AutoReq(BaseModel):
    enabled: bool | None = None
    interval: int | None = None
    mode: str | None = None       # schedule | due
    horizon: int | None = None    # schedule_horizon_days


@app.get("/api/auto")
def api_auto():
    cfg = load_config()
    return {"enabled": bool(cfg.get("auto_check")), "interval": int(cfg.get("auto_interval") or 5),
            "mode": post_mode_of(cfg), "horizon": horizon_of(cfg),
            "last": AUTO["last"], "result": AUTO["result"], "next": AUTO["next"],
            "job": AUTO["job"],
            "next_due": datetime.fromtimestamp(AUTO["next_due"]).strftime("%H:%M %d/%m") if AUTO.get("next_due") else None}


@app.post("/api/auto")
def api_auto_set(r: AutoReq):
    cfg = load_config()
    if r.enabled is not None:
        cfg["auto_check"] = bool(r.enabled)
        if r.enabled:
            AUTO["last_tick"] = 0.0          # vừa bật -> kiểm tra ngay trong ~10 giây
    if r.interval is not None:
        cfg["auto_interval"] = max(1, int(r.interval))
    if r.mode is not None and r.mode in ps.POST_MODES and r.mode != post_mode_of(cfg):
        cfg["post_mode"] = r.mode
        AUTO["last_tick"] = 0.0              # đổi cách đăng -> kiểm tra lại ngay
    if r.horizon is not None and max(0, min(60, int(r.horizon))) != horizon_of(cfg):
        cfg["schedule_horizon_days"] = max(0, min(60, int(r.horizon)))
        AUTO["last_tick"] = 0.0
    save_config(cfg)
    return api_auto()


# ----------------------------------------------------------------- chạy script (tiến trình con) + log
JOBS = {}


class RunReq(BaseModel):
    mode: str                 # plan | dry | real | scan
    rows: str | None = None
    max: int | None = None
    profile: str | None = None   # cho scan
    retry: bool = False          # đăng lại cả hàng 'Lỗi'


def _base_args(cfg):
    base = ["post_sheet.py", "--sheet", cfg["sheet_url"], "--tab", cfg["tab"], "--pages-tab", cfg["pages_tab"],
            "--profile", cfg["default_profile"], "--mode", post_mode_of(cfg),
            "--config", str(CONFIG_FILE), "--window", str(window_of(cfg)), "--horizon", str(horizon_of(cfg))]
    if cfg.get("webhook"):
        base += ["--webhook", cfg["webhook"]]
    return base


def _spawn(args, label):
    if any(j["proc"].poll() is None for j in JOBS.values()):
        raise HTTPException(409, "Đang có việc khác chạy, chờ xong đã")
    jid = datetime.now().strftime("%Y%m%d_%H%M%S")
    logf = LOG_DIR / f"ui_{jid}.log"
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    fh = open(logf, "w", encoding="utf-8")
    # Chạy bằng python.exe (có stdout/stderr thật) nhưng ẩn cửa sổ; log ghi ra file.
    # stdin=DEVNULL: dưới pythonw không có stdin, để kế thừa sẽ lỗi.
    exe = sys.executable.replace("pythonw.exe", "python.exe")
    print(f"[ui] spawn {label}: {exe} {' '.join(args)}", flush=True)
    try:
        proc = subprocess.Popen([exe] + args, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=fh,
                                stderr=subprocess.STDOUT, env=env, **pr.HIDDEN)
    except Exception as e:
        fh.close()
        print(f"[ui] spawn lỗi: {e!r}", flush=True)
        raise HTTPException(500, f"Không chạy được script: {e}")
    print(f"[ui] spawn OK pid={proc.pid}", flush=True)
    JOBS[jid] = {"proc": proc, "log": logf, "label": label, "started": datetime.now().isoformat(timespec="seconds"), "fh": fh}
    return jid


@app.post("/api/run")
def api_run(r: RunReq):
    cfg = load_config()
    base = _base_args(cfg)
    if r.retry:
        base += ["--retry-errors"]
    if r.mode == "scan":
        args = ["post_sheet.py", "--scan-pages", r.profile or "all"] + (["--webhook", cfg["webhook"]] if cfg.get("webhook") else [])
        label = "Quét fanpage"
    elif r.mode in ("plan", "dry", "real"):
        args = base + (["--plan-only"] if r.mode == "plan" else ["--dry-run"] if r.mode == "dry" else [])
        if r.rows:
            args += ["--rows", r.rows]
        if r.max:
            args += ["--max", str(r.max)]
        label = {"plan": "Xem kế hoạch", "dry": "Chạy thử (dry-run)",
                 "real": "Đăng các bài tới giờ" if post_mode_of(cfg) == "due" else "Lên lịch các bài chờ"}[r.mode]
        if r.retry and r.mode == "real":
            label = "Đăng lại hàng lỗi + bài chờ"
    else:
        raise HTTPException(400, "mode không hợp lệ")
    jid = _spawn(args, label)
    return {"ok": True, "job": jid}


@app.get("/api/jobs/{jid}")
def api_job(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "không có job")
    rc = j["proc"].poll()
    if rc is not None:
        try:
            j["fh"].close()
        except Exception:
            pass
    try:
        text = j["log"].read_text(encoding="utf-8", errors="replace")
    except Exception:
        text = ""
    # Bỏ các dòng traceback dài dòng
    lines = [ln for ln in text.splitlines() if not re.match(r"^\s*(File |\^|await |return |self\._|raise )", ln)]
    return {"job": jid, "label": j["label"], "running": rc is None, "code": rc, "log": "\n".join(lines[-400:])}


@app.get("/api/jobs")
def api_jobs():
    return [{"job": k, "label": v["label"], "running": v["proc"].poll() is None, "started": v["started"]}
            for k, v in sorted(JOBS.items(), reverse=True)]


@app.post("/api/jobs/{jid}/stop")
def api_job_stop(jid: str):
    j = JOBS.get(jid)
    if j and j["proc"].poll() is None:
        j["proc"].kill()
    return {"ok": True}


@app.post("/api/shutdown")
def api_shutdown():
    """Tắt server (nút 'Tắt phần mềm' trên giao diện). Dừng cả việc đang chạy nếu có."""
    for j in JOBS.values():
        if j["proc"].poll() is None:
            j["proc"].kill()
    threading.Timer(0.7, lambda: os._exit(0)).start()
    return {"ok": True}


@app.get("/api/screens")
def api_screens():
    files = sorted(LOG_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)[:12]
    return [{"name": f.name, "time": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d/%m %H:%M")} for f in files]


if __name__ == "__main__":
    print(f"Mở giao diện: http://127.0.0.1:{UI_PORT}")
    uvicorn.run(app, host="127.0.0.1", port=UI_PORT, log_level="warning")
