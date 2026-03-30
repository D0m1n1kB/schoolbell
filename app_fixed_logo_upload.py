import os
import json
import time
import glob
import gzip
import shutil
import threading
import subprocess
import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, render_template_string, jsonify, Response, send_file, abort
from werkzeug.utils import secure_filename
import RPi.GPIO as GPIO


APP_DIR = "/opt/schoolbell"
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
CONFIG_BACKUP_DIR = os.path.join(APP_DIR, "config_backups")
LOG_PATH = os.path.join(APP_DIR, "events.log")
LOGO_DIR = os.path.join(APP_DIR, "uploads")
LOGO_PREFIX = "school_logo"

os.makedirs(LOGO_DIR, exist_ok=True)
os.makedirs(CONFIG_BACKUP_DIR, exist_ok=True)

app = Flask(__name__)

_lock = threading.Lock()
_gpio_ready = False
_last_ring_key = None

DAYS = [
    ("mon", "Poniedziałek"),
    ("tue", "Wtorek"),
    ("wed", "Środa"),
    ("thu", "Czwartek"),
    ("fri", "Piątek"),
    ("sat", "Sobota"),
    ("sun", "Niedziela"),
]

ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".svg"}


# ---------------- config ----------------
def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _append_log_raw(msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def backup_config_file(keep: int = 30) -> None:
    if not os.path.exists(CONFIG_PATH):
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = os.path.join(CONFIG_BACKUP_DIR, f"config_{ts}.json")
    try:
        shutil.copy2(CONFIG_PATH, target)
        _append_log_raw(f"CONFIG BACKUP created ({os.path.basename(target)})")
    except Exception:
        return

    backups = sorted(
        glob.glob(os.path.join(CONFIG_BACKUP_DIR, "config_*.json")),
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )
    for p in backups[keep:]:
        try:
            os.remove(p)
            _append_log_raw(f"CONFIG BACKUP removed ({os.path.basename(p)})")
        except Exception:
            pass


def save_config(cfg: dict) -> None:
    backup_config_file(keep=30)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------------- auth ----------------
def require_auth():
    cfg = load_config()
    token = ((cfg.get("auth") or {}).get("token") or "").strip()
    if not token:
        return
    supplied = (request.headers.get("X-Token") or request.args.get("token") or "").strip()
    if supplied != token:
        abort(401)


@app.before_request
def _auth_guard():
    if request.path.startswith("/health"):
        return
    require_auth()


# ---------------- logo helpers ----------------
def get_logo_file():
    for p in glob.glob(os.path.join(LOGO_DIR, f"{LOGO_PREFIX}.*")):
        ext = os.path.splitext(p)[1].lower()
        if ext in ALLOWED_LOGO_EXTENSIONS and os.path.isfile(p):
            return p
    return None


def logo_exists() -> bool:
    return get_logo_file() is not None


def delete_existing_logo():
    for p in glob.glob(os.path.join(LOGO_DIR, f"{LOGO_PREFIX}.*")):
        try:
            os.remove(p)
        except Exception:
            pass


# ---------------- log helpers ----------------
def _log_keep_days(cfg: dict) -> int:
    try:
        return int((cfg.get("log") or {}).get("keep_days", 31))
    except Exception:
        return 31


def _log_tail_lines(cfg: dict) -> int:
    try:
        return int((cfg.get("log") or {}).get("tail_lines", 30))
    except Exception:
        return 30


def _today_str(tz: ZoneInfo) -> str:
    return datetime.now(tz).date().isoformat()


def rotate_log_if_needed(cfg: dict) -> None:
    tz = ZoneInfo(cfg.get("timezone", "Europe/Warsaw"))
    keep_days = _log_keep_days(cfg)

    if not os.path.exists(LOG_PATH):
        return

    try:
        if os.path.getsize(LOG_PATH) <= 0:
            return
    except Exception:
        return

    mtime_day = datetime.fromtimestamp(os.path.getmtime(LOG_PATH), tz).date().isoformat()
    today = _today_str(tz)
    if mtime_day == today:
        return

    archive_name = os.path.join(APP_DIR, f"events.log.{mtime_day}.gz")
    try:
        with open(LOG_PATH, "rb") as src:
            data = src.read()
        with gzip.open(archive_name, "wb") as gz:
            gz.write(data)
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        return

    archives = sorted(glob.glob(os.path.join(APP_DIR, "events.log.????-??-??.gz")))
    if len(archives) > keep_days:
        archives = sorted(archives, key=lambda p: os.path.getmtime(p))
        for p in archives[:-keep_days]:
            try:
                os.remove(p)
            except Exception:
                pass


def log_event(msg: str) -> None:
    cfg = load_config()
    rotate_log_if_needed(cfg)
    ts = datetime.now().isoformat(timespec="seconds")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def tail_log(cfg: dict) -> str:
    n = _log_tail_lines(cfg)
    if not os.path.exists(LOG_PATH):
        return "(brak logu)"
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if not lines:
            return "(pusty log)"
        return "".join(lines[-n:])
    except Exception:
        return "(błąd odczytu logu)"


def list_log_files():
    files = []
    if os.path.exists(LOG_PATH):
        files.append(LOG_PATH)

    archives = sorted(
        glob.glob(os.path.join(APP_DIR, "events.log.????-??-??.gz")),
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )
    files.extend(archives)

    out = []
    for p in files:
        name = os.path.basename(p)
        try:
            sz = os.path.getsize(p)
        except Exception:
            sz = 0
        out.append({"name": name, "size": sz})
    return out


def safe_log_path(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return LOG_PATH
    if "/" in name or "\\" in name:
        raise ValueError("Nieprawidłowa nazwa pliku.")
    if not (name == "events.log" or (name.startswith("events.log.") and name.endswith(".gz"))):
        raise ValueError("Nieprawidłowy plik.")
    p = os.path.join(APP_DIR, name)
    if not os.path.exists(p):
        raise FileNotFoundError("Plik nie istnieje.")
    return p


def read_log_text(path: str) -> str:
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            return f.read()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def truncate_file(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("")


def remove_lines_for_day(path: str, day_str: str) -> int:
    if path.endswith(".gz"):
        raise ValueError("Nie można usuwać pojedynczych dni z archiwum.")
    if not os.path.exists(path):
        return 0

    prefix = day_str.strip()
    if len(prefix) != 10 or prefix[4] != "-" or prefix[7] != "-":
        raise ValueError("Zły format daty. Użyj YYYY-MM-DD.")

    removed = 0
    kept = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith(prefix):
                removed += 1
            else:
                kept.append(line)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(kept)
    os.replace(tmp, path)
    return removed


# ---------------- time / ntp helpers ----------------
def get_time_info(cfg: dict) -> dict:
    tz_name = cfg.get("timezone", "Europe/Warsaw")
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    info = {
        "timezone": tz_name,
        "server_time": now.isoformat(timespec="seconds"),
        "server_time_pretty": now.strftime("%Y-%m-%d %H:%M:%S"),
        "ntp_synchronized": None,
        "details": "",
    }

    try:
        out = subprocess.check_output(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip().lower()

        if out in ("yes", "no"):
            info["ntp_synchronized"] = (out == "yes")
    except Exception:
        pass

    if info["ntp_synchronized"] is True:
        info["details"] = "NTP zsynchronizowany"
    elif info["ntp_synchronized"] is False:
        info["details"] = "NTP nie jest jeszcze zsynchronizowany"
    else:
        info["details"] = "Brak danych o NTP"

    return info


# ---------------- gpio ----------------
def gpio_init(cfg: dict) -> None:
    global _gpio_ready
    if _gpio_ready:
        return

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)

    relay_pin = int(cfg["gpio"]["pin"])
    active = (cfg["gpio"].get("active_level", "LOW") or "LOW").upper()

    if active == "LOW":
        GPIO.setup(relay_pin, GPIO.OUT, initial=GPIO.HIGH)
    else:
        GPIO.setup(relay_pin, GPIO.OUT, initial=GPIO.LOW)

    _gpio_ready = True


def set_relay(on: bool, cfg: dict) -> None:
    pin = int(cfg["gpio"]["pin"])
    active = (cfg["gpio"].get("active_level", "LOW") or "LOW").upper()
    if active == "LOW":
        GPIO.output(pin, GPIO.LOW if on else GPIO.HIGH)
    else:
        GPIO.output(pin, GPIO.HIGH if on else GPIO.LOW)


def ring(duration_s: int, reason: str) -> None:
    cfg = load_config()
    gpio_init(cfg)
    duration_s = max(1, min(int(duration_s), 120))
    log_event(f"RING {duration_s}s ({reason})")
    set_relay(True, cfg)
    time.sleep(duration_s)
    set_relay(False, cfg)


# ---------------- physical button ----------------
def button_loop():
    try:
        cfg = load_config()
        btn = cfg.get("button") or {}
        if not btn.get("enabled", False):
            return

        gpio_init(cfg)

        pin = int(btn.get("pin", 27))
        pull = (btn.get("pull", "UP") or "UP").upper()
        debounce_ms = int(btn.get("debounce_ms", 250))
        cooldown_ms = int(btn.get("cooldown_ms", 1200))
        ring_duration = int(btn.get("ring_duration", 3))

        pud = GPIO.PUD_UP if pull == "UP" else GPIO.PUD_DOWN
        GPIO.setup(pin, GPIO.IN, pull_up_down=pud)
        active_state = GPIO.LOW if pull == "UP" else GPIO.HIGH

        last_press_ms = 0
        last_fire_ms = 0

        while True:
            now_ms = int(time.time() * 1000)

            if GPIO.input(pin) == active_state:
                if last_press_ms == 0:
                    last_press_ms = now_ms

                if (now_ms - last_press_ms) >= debounce_ms:
                    if (now_ms - last_fire_ms) >= cooldown_ms:
                        last_fire_ms = now_ms
                        last_press_ms = 0
                        ring(ring_duration, "button")
                        time.sleep(0.05)
            else:
                last_press_ms = 0

            time.sleep(0.01)

    except Exception as e:
        log_event(f"ERROR button_loop: {repr(e)}")
        time.sleep(2)


# ---------------- schedule helpers ----------------
def weekday_key(d: date) -> str:
    return d.strftime("%a").lower()[:3]


def parse_hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def fmt_hhmm(m: int) -> str:
    h = (m // 60) % 24
    mm = m % 60
    return f"{h:02d}:{mm:02d}"


def _in_disabled_ranges(cfg: dict, d: date):
    ds = d.isoformat()
    for r in (cfg.get("disabled_ranges") or []):
        f = (r or {}).get("from")
        t = (r or {}).get("to")
        if not f or not t:
            continue
        if f <= ds <= t:
            return (r or {}).get("label") or "Wyłączone (zakres)"
    return None


def resolve_profile(cfg: dict, d: date) -> str:
    ds = d.isoformat()
    if ds in (cfg.get("disabled_dates") or []):
        return "off"
    if _in_disabled_ranges(cfg, d) is not None:
        return "off"

    overrides = cfg.get("date_overrides") or {}
    if ds in overrides:
        return overrides[ds]

    weekly = cfg.get("weekly_profile") or {}
    return weekly.get(weekday_key(d), "off")


def set_today_override(cfg: dict, profile_or_auto: str) -> None:
    tz = ZoneInfo(cfg.get("timezone", "Europe/Warsaw"))
    today = datetime.now(tz).date().isoformat()
    overrides = cfg.get("date_overrides") or {}
    if profile_or_auto == "auto":
        overrides.pop(today, None)
    else:
        overrides[today] = profile_or_auto
    cfg["date_overrides"] = overrides


def build_timeline(cfg: dict, profile: str):
    tt = (cfg.get("timetables") or {}).get(profile)
    if not tt:
        return []

    start = parse_hhmm(tt.get("day_start", "08:30"))
    lessons = [int(x) for x in (tt.get("lessons") or [])]
    breaks = [int(x) for x in (tt.get("breaks") or [])]
    if len(lessons) < 1:
        return []

    t = start
    segs = []
    for i, lmin in enumerate(lessons):
        lmin = max(1, int(lmin))
        segs.append({"type": "lesson", "idx": i + 1, "start_m": t, "end_m": t + lmin})
        t += lmin
        if i < len(lessons) - 1:
            bmin = breaks[i] if i < len(breaks) else 10
            bmin = max(1, int(bmin))
            segs.append({"type": "break", "idx": i + 1, "start_m": t, "end_m": t + bmin})
            t += bmin
    return segs


def find_next_first_bell(cfg: dict, now: datetime, lookahead_days: int = 60):
    tz = ZoneInfo(cfg.get("timezone", "Europe/Warsaw"))
    now = now.astimezone(tz)
    today = now.date()

    for i in range(0, lookahead_days + 1):
        d = today + timedelta(days=i)
        prof = resolve_profile(cfg, d)
        if prof == "off":
            continue
        segs = build_timeline(cfg, prof)
        if not segs:
            continue

        first_start_m = segs[0]["start_m"]
        target = datetime(d.year, d.month, d.day, first_start_m // 60, first_start_m % 60, 0, tzinfo=tz)

        if i == 0 and target <= now:
            continue

        if i == 0:
            when = f"Dziś o {target.strftime('%H:%M')}"
        elif i == 1:
            when = f"Jutro o {target.strftime('%H:%M')}"
        else:
            when = f"{target.strftime('%Y-%m-%d')} o {target.strftime('%H:%M')}"

        return target, when

    return None


def _school_open_window(cfg: dict):
    school = cfg.get("school") or {}
    open_from = (school.get("open_from") or "07:30").strip()
    open_to = (school.get("open_to") or "18:00").strip()
    try:
        ofm = parse_hhmm(open_from)
    except Exception:
        ofm = parse_hhmm("07:30")
    try:
        otm = parse_hhmm(open_to)
    except Exception:
        otm = parse_hhmm("18:00")
    return ofm, otm, open_from, open_to


def compute_status(cfg: dict):
    tz = ZoneInfo(cfg.get("timezone", "Europe/Warsaw"))
    now = datetime.now(tz)
    today = now.date()
    ds = today.isoformat()

    weekly_prof = (cfg.get("weekly_profile") or {}).get(weekday_key(today), "off")
    override_prof = (cfg.get("date_overrides") or {}).get(ds)
    prof = resolve_profile(cfg, today)

    now_m = now.hour * 60 + now.minute
    open_from_m, open_to_m, _, _ = _school_open_window(cfg)

    def dt_today(mins: int) -> datetime:
        return datetime(today.year, today.month, today.day, mins // 60, mins % 60, 0, 0, tzinfo=tz)

    def pack(state: str, label_small, label: str, end, target_dt: datetime | None, start_dt: datetime | None = None):
        if target_dt is not None:
            target_ts_ms = int(target_dt.timestamp() * 1000)
            secs_left = max(0.0, (target_dt - now).total_seconds())
            target_at = target_dt.isoformat(timespec="milliseconds")
        else:
            target_ts_ms = None
            secs_left = None
            target_at = None

        segment_start_ts_ms = int(start_dt.timestamp() * 1000) if start_dt is not None else None

        return {
            "now": now.isoformat(timespec="milliseconds"),
            "server_now_ts_ms": int(now.timestamp() * 1000),
            "state": state,
            "label_small": label_small,
            "label": label,
            "end": end,
            "secs_left": secs_left,
            "target_at": target_at,
            "target_ts_ms": target_ts_ms,
            "segment_start_ts_ms": segment_start_ts_ms,
            "profile": prof,
            "today_override": override_prof,
            "weekly_profile": weekly_prof
        }

    if now_m < open_from_m or now_m >= open_to_m:
        nxt = find_next_first_bell(cfg, now, lookahead_days=60)
        if nxt:
            target, when = nxt
            return pack("closed", "Szkoła zamknięta", f"Następny dzwonek: {when}", None, target)
        return pack("closed", "Szkoła zamknięta", "", None, None)

    if prof == "off":
        nxt = find_next_first_bell(cfg, now, lookahead_days=60)
        if nxt:
            target, when = nxt
            return pack("off", None, f"Następny dzwonek: {when}", None, target)
        return pack("off", None, "Wyłączone", None, None)

    segs = build_timeline(cfg, prof)
    if not segs:
        return pack("outside", None, "Brak planu", None, None)

    first_start_m = segs[0]["start_m"]

    if open_from_m <= now_m < first_start_m:
        return pack("prestart", None, "Do pierwszego dzwonka", None, dt_today(first_start_m), dt_today(open_from_m))

    cur = None
    for s in segs:
        if s["start_m"] <= now_m < s["end_m"]:
            cur = s
            break

    if cur:
        label = ("LEKCJA " if cur["type"] == "lesson" else "PRZERWA ") + str(cur["idx"])
        return pack(cur["type"], None, label, fmt_hhmm(cur["end_m"]), dt_today(cur["end_m"]), dt_today(cur["start_m"]))

    # Po zakończeniu ostatniej lekcji / poza segmentami w godzinach otwarcia:
    # pokazujemy następny dzwonek jako pierwszy dostępny w przyszłości.
    nxt = find_next_first_bell(cfg, now, lookahead_days=60)
    if nxt:
        target, when = nxt
        return pack("outside", None, f"Następny dzwonek: {when}", None, target)

    return pack("outside", None, "Brak zaplanowanych dzwonków", None, None)


# ---------------- scheduler ----------------
def scheduler_loop():
    global _last_ring_key
    while True:
        try:
            cfg = load_config()
            rotate_log_if_needed(cfg)

            bell = cfg.get("bell") or {}
            if not bell.get("ring_on_transitions", True):
                time.sleep(0.2)
                continue

            tz = ZoneInfo(cfg.get("timezone", "Europe/Warsaw"))
            now = datetime.now(tz)

            now_m = now.hour * 60 + now.minute
            open_from_m, open_to_m, _, _ = _school_open_window(cfg)
            if not (open_from_m <= now_m < open_to_m):
                time.sleep(0.2)
                continue

            prof = resolve_profile(cfg, now.date())
            if prof == "off":
                time.sleep(0.2)
                continue

            segs = build_timeline(cfg, prof)
            if not segs:
                time.sleep(0.2)
                continue

            hhmm = now.strftime("%H:%M")

            # dzwonek tylko dokładnie na początku minuty
            if now.second != 0:
                time.sleep(0.2)
                continue
            ds = now.date().isoformat()

            for s in segs:
                if fmt_hhmm(s["end_m"]) != hhmm:
                    continue

                key = f"{ds}|{hhmm}|{prof}|{s['type']}|{s['idx']}"
                with _lock:
                    if _last_ring_key == key:
                        continue
                    _last_ring_key = key

                if s["type"] == "lesson":
                    dur = int(bell.get("duration_lesson_end", 3))
                    ring(dur, f"auto:{prof}:koniec lekcji {s['idx']}")
                else:
                    dur = int(bell.get("duration_break_end", 3))
                    ring(dur, f"auto:{prof}:koniec przerwy {s['idx']}")

            time.sleep(0.2)
        except Exception as e:
            log_event(f"ERROR scheduler: {repr(e)}")
            time.sleep(2)


# ---------------- calendar validation ----------------
def normalize_ranges(ranges: list) -> list:
    clean = []
    for r in ranges or []:
        if not isinstance(r, dict):
            continue
        f = (r.get("from") or "").strip()
        t = (r.get("to") or "").strip()
        if len(f) != 10 or len(t) != 10:
            continue
        clean.append({"from": f, "to": t, "label": (r.get("label") or "").strip()})
    return sorted(clean, key=lambda x: (x["from"], x["to"], x.get("label", "")))


def validate_new_range(existing: list, new_from: str, new_to: str):
    nf, nt = new_from, new_to
    for r in existing:
        rf, rt = r["from"], r["to"]
        if rf == nf and rt == nt:
            return False, "Taki sam zakres już istnieje."
        if nf <= rt and nt >= rf:
            return False, f"Zakres nachodzi na istniejący ({rf} → {rt})."
    return True, ""


def sanitize_profile_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    if len(name) > 32:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return ""
    return name


def profile_in_use(cfg: dict, profile: str):
    weekly = cfg.get("weekly_profile") or {}
    for day_key, value in weekly.items():
        if value == profile:
            return True, f"Profil jest używany w tygodniu ({day_key})."

    overrides = cfg.get("date_overrides") or {}
    for d, value in overrides.items():
        if value == profile:
            return True, f"Profil jest używany w nadpisaniu daty ({d})."

    return False, ""


# ---------------- HTML blocks ----------------
BASE_STYLE = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
  :root{--b:#0f172a;--muted:#475569;--border:#e2e8f0;--card:#ffffff;--bg:#f8fafc;--danger:#b91c1c;--ok:#065f46;--bad:#9f1239;}
  body{font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--b);max-width:1100px;margin:18px auto;padding:0 14px;}
  h1{margin:0 0 12px;letter-spacing:-0.02em;}
  h2{margin:0 0 10px;letter-spacing:-0.01em;}
  .card{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:18px;margin:16px 0;box-shadow:0 1px 2px rgba(0,0,0,.04);}
  .center{text-align:center;}
  .clock{font-size:78px;font-weight:900;letter-spacing:-0.03em;}
  .label{font-size:28px;font-weight:900;margin-top:10px;}
  .sub{color:var(--muted);margin-top:6px;font-weight:700;font-size:18px;}
  .subsmall{color:#64748b;margin-top:8px;font-weight:700;font-size:14px;}
  .big{font-size:72px;font-weight:900;margin-top:10px;letter-spacing:-0.02em;}
  .status-progress-wrap{display:flex;justify-content:center;align-items:center;margin-top:16px;}
  .status-progress{width:80%;max-width:760px;height:16px;border:1px solid #deeffb;border-radius:0;background:#f7fbff;overflow:hidden;position:relative;display:none;box-shadow:none;margin:14px auto 0 auto;}
  .status-progress-fill{position:absolute;left:0;top:0;bottom:0;width:100%;background:repeating-linear-gradient(45deg, #dff7df 0 10px, #c9f0c9 10px 20px);background-size:28px 28px;animation:progressStripeMove 1.8s linear infinite;transition:width .08s linear;}
  .status-progress-label{font-size:12px;font-weight:800;color:#64748b;letter-spacing:.06em;text-transform:uppercase;display:none;text-align:center;margin:0 auto 6px auto;width:80%;max-width:760px;}
  .switchline{display:flex;align-items:center;gap:10px;color:#475569;font-weight:700;font-size:14px;}
  .switchline input{width:auto;transform:scale(1.1);margin:0;}
  @keyframes progressStripeMove{from{background-position:0 0;}to{background-position:28px 0;}}
  @keyframes progressPulse{0%{opacity:1;}50%{opacity:.6;}100%{opacity:1;}}
  {from{background-position:0 0;}to{background-position:28px 0;}}

  .pill{display:inline-flex;align-items:center;gap:10px;padding:10px 14px;border-radius:999px;border:1px solid var(--border);background:#fff;font-weight:700;color:var(--muted);}
  .lesson{background:#e9f7ef;}
  .break{background:#e8f2ff;}
  .off,.outside,.prestart,.closed{background:#f1f5f9;}
  .row{display:flex;gap:12px;flex-wrap:wrap;align-items:center;justify-content:flex-start;}
  .grid{display:grid;grid-template-columns:repeat(auto-fit, minmax(260px, 1fr));gap:12px;align-items:end;}
  .field label{display:block;font-size:12px;font-weight:800;color:var(--muted);margin:0 0 6px;}
  input,select,textarea{width:100%;padding:12px 14px;border:1px solid var(--border);border-radius:12px;font-size:13px;outline:none;background:#fff;box-sizing:border-box;font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;font-weight:300;letter-spacing:-0.01em;}
  input::placeholder, textarea::placeholder{color:#94a3b8;font-weight:300;}
  select{appearance:none;background-image:linear-gradient(45deg, transparent 50%, #64748b 50%),linear-gradient(135deg, #64748b 50%, transparent 50%);background-position:calc(100% - 18px) calc(50% - 3px),calc(100% - 12px) calc(50% - 3px);background-size:6px 6px, 6px 6px;background-repeat:no-repeat;padding-right:36px;}
  input:focus,select:focus,textarea:focus{border-color:#94a3b8;box-shadow:0 0 0 3px rgba(148,163,184,.25);}
  button{padding:12px 16px;border:1px solid #0f172a;border-radius:12px;background:#0f172a;color:#fff;cursor:pointer;font-size:16px;font-weight:900;white-space:nowrap;}
  button.light{background:#fff;color:#0f172a;border-color:var(--border);}
  button.danger{background:var(--danger);border-color:var(--danger);}
  button:disabled{opacity:.45;cursor:not-allowed;}
  pre{background:#f8fafc;border:1px solid #eef2f7;padding:12px;border-radius:12px;overflow:auto;margin:8px 0 0;max-height:260px;}
  details{border:1px solid var(--border);border-radius:18px;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.04);margin:16px 0;}
  summary{list-style:none;cursor:pointer;padding:16px 18px;font-weight:900;font-size:18px;display:flex;align-items:center;justify-content:space-between;}
  summary::-webkit-details-marker{display:none;}
  .chev{transition:transform .2s ease; color:#475569; font-weight:900;}
  details[open] .chev{transform:rotate(180deg);}
  .detailsBody{padding:0 18px 18px;}
  .divider{height:1px;background:var(--border);margin:0 18px;}
  dialog{border:1px solid var(--border);border-radius:18px;padding:0;max-width:980px;width:calc(100% - 20px);max-height:86vh;overflow:auto;box-shadow:0 20px 50px rgba(0,0,0,.25);}
  dialog::backdrop{background:rgba(15,23,42,.35);}
  .dialog-head{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border-bottom:1px solid var(--border);position:sticky;top:0;background:#fff;z-index:2;}
  .dialog-body{padding:16px;}
  .logbox{white-space:pre;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace;border:1px solid #eef2f7;background:#f8fafc;padding:12px;border-radius:12px;overflow:auto;max-height:60vh;}
  .logo-preview-box{display:flex;align-items:center;justify-content:center;width:100%;max-width:none;min-height:140px;margin:12px 0 0 0;padding:12px;border:1px dashed #cbd5e1;border-radius:14px;background-color:#f8fafc;background-image:linear-gradient(45deg,#eef2f7 25%,transparent 25%,transparent 75%,#eef2f7 75%,#eef2f7),linear-gradient(45deg,#eef2f7 25%,transparent 25%,transparent 75%,#eef2f7 75%,#eef2f7);background-position:0 0,12px 12px;background-size:24px 24px;box-sizing:border-box;}
  .logo-preview-box img{max-width:300px;max-height:300px;width:auto;height:auto;object-fit:contain;display:block;}
  .logo-preview-empty{color:#64748b;font-weight:700;font-size:14px;text-align:center;}
  .status-ok{color:var(--ok);}
  .status-bad{color:var(--bad);}
</style>
"""

CALENDAR_EMBED = """
<div id="calMsg" style="display:none"></div>

<div class="card" style="margin:0 0 14px">
  <div style="color:#475569;font-weight:700;font-size:14px">
    Dodaj zakres dat, w których dzwonki nie będą działały (wakacje, ferie, święta).
    Zakresy nie mogą się powtarzać ani nachodzić na siebie.
  </div>
</div>

<div class="card" style="margin:0 0 14px">
  <h2>Dodaj zakres</h2>
  <form id="calAddForm" method="post" action="/calendar/add">
    <div class="grid">
      <div class="field">
        <label>Od (YYYY-MM-DD)</label>
        <input name="from" placeholder="2026-07-01" required>
      </div>
      <div class="field">
        <label>Do (YYYY-MM-DD)</label>
        <input name="to" placeholder="2026-08-31" required>
      </div>
      <div class="field" style="grid-column:1/-1">
        <label>Opis (opcjonalnie)</label>
        <input name="label" placeholder="Wakacje">
      </div>
    </div>
    <div class="row" style="margin-top:12px">
      <button type="submit">Dodaj</button>
    </div>
  </form>
</div>

<div class="card" style="margin:0">
  <h2>Aktualne zakresy</h2>
  {% if ranges|length == 0 %}
    <div style="color:#475569;font-weight:700;font-size:14px">Brak zakresów.</div>
  {% else %}
    <div style="overflow:auto">
      <table style="width:100%;border-collapse:collapse">
        <thead>
          <tr>
            <th style="text-align:left;padding:10px;border-bottom:1px solid #e2e8f0">Od</th>
            <th style="text-align:left;padding:10px;border-bottom:1px solid #e2e8f0">Do</th>
            <th style="text-align:left;padding:10px;border-bottom:1px solid #e2e8f0">Opis</th>
            <th style="text-align:right;padding:10px;border-bottom:1px solid #e2e8f0">Akcja</th>
          </tr>
        </thead>
        <tbody>
          {% for r in ranges %}
          <tr>
            <td style="padding:10px;border-bottom:1px solid #f1f5f9">{{r.from}}</td>
            <td style="padding:10px;border-bottom:1px solid #f1f5f9">{{r.to}}</td>
            <td style="padding:10px;border-bottom:1px solid #f1f5f9">{{r.label}}</td>
            <td style="padding:10px;border-bottom:1px solid #f1f5f9;text-align:right">
              <form class="calDeleteForm" method="post" action="/calendar/delete" style="margin:0;display:inline">
                <input type="hidden" name="idx" value="{{ loop.index0 }}">
                <button type="submit" class="light">Usuń</button>
              </form>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  {% endif %}
</div>
"""

FULL_LOG_EMBED = """
<div id="logMsg" style="display:none"></div>

<div class="card" style="margin:0 0 14px">
  <div style="color:#475569;font-weight:700;font-size:14px">
    Wybierz plik logu (dzisiejszy lub archiwum), odśwież, pobierz albo wyczyść.
  </div>
</div>

<div class="card" style="margin:0 0 14px">
  <h2>Plik logu</h2>
  <div class="grid">
    <div class="field">
      <label>Pliki</label>
      <select id="logFileSelect">
        {% for f in files %}
          <option value="{{f.name}}">{{f.name}} ({{f.size}} B)</option>
        {% endfor %}
      </select>
    </div>
    <div class="field">
      <label>Operacja na logach</label>
      <input value="Wyczyszczenie całego logu" disabled>
    </div>
  </div>

  <div class="row" style="margin-top:12px;justify-content:flex-start">
    <button type="button" class="light" id="btnLogRefresh">Odśwież</button>
    <button type="button" class="light" id="btnLogDownload">Pobierz</button>
  </div>

  <div class="row" style="margin-top:12px;justify-content:flex-start">
    <button type="button" class="danger" id="btnLogClearAll">Usuń wszystkie logi</button>
    <button type="button" class="danger" id="btnLogDeleteFile">Usuń wybrany plik</button>
  </div>
</div>

<div class="card" style="margin:0">
  <h2>Podgląd</h2>
  <div id="fullLogBox" class="logbox">(ładowanie...)</div>
</div>
"""

INDEX_HTML = """
<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Dzwonki szkolne</title>
{{ base_style|safe }}
</head>
<body>

<div style="display:flex;align-items:center;justify-content:space-between;gap:16px;margin:0 0 12px;">
  <h1 style="margin:0;line-height:1.1">Dzwonki szkolne</h1>

  <img id="schoolLogo"
       src="/logo-image?v={{ logo_version }}"
       alt="Logo szkoły"
       style="height:32px;max-width:160px;width:auto;object-fit:contain;display:{% if logo_exists %}block{% else %}none{% endif %};flex-shrink:0;">
</div>

<div class="card center" id="statusCard">
  <div class="clock" id="clock">--:--:--</div>

  <div class="row" style="justify-content:center;margin-top:10px">
    <div class="pill" id="pillProfile">Profil: —</div>
    <button class="danger" type="button" onclick="openCalendar()">Kalendarz wyłączeń</button>
  </div>

  <div id="smallLine" class="subsmall" style="display:none"></div>
  <div class="label" id="stateLine">Ładowanie…</div>
  <div class="sub" id="endLine"></div>

  <div id="counterBlock" style="margin-top:18px">
    <div id="counterTitle" style="font-weight:900;letter-spacing:0.08em;color:#334155">KOLEJNY DZWONEK ZA</div>
    <div class="big" id="counter">--:--</div>
  </div>

  <div class="status-progress-wrap">
    <div style="width:100%">
      <div class="status-progress-label" id="progressLabel">POSTĘP LEKCJI / PRZERWY</div>
      <div class="status-progress" id="statusProgress">
        <div class="status-progress-fill" id="statusProgressFill"></div>
      </div>
    </div>
  </div>

  <div class="row" style="justify-content:center;margin-top:14px">
    <form class="ajax-today" data-title="Wyłączono" data-msg="Wyłączono dzwonki." method="post" action="/today_off" style="margin:0">
      <button id="btnTodayOff" class="danger" type="submit">Wyłącz DZISIAJ</button>
    </form>
    <form class="ajax-today" data-title="Włączono z powrotem" data-msg="Włączono z powrotem dzwonki." method="post" action="/today_on" style="margin:0">
      <button id="btnTodayOn" class="light" type="submit">Włącz z powrotem DZISIAJ</button>
    </form>
  </div>
</div>

<dialog id="calDialog">
  <div class="dialog-head">
    <div style="font-weight:900">Kalendarz wyłączeń</div>
    <button class="light" type="button" onclick="closeCalendar()">Zamknij</button>
  </div>
  <div class="dialog-body">
    <div id="calContent" style="color:#475569;font-weight:700">Ładowanie…</div>
  </div>
</dialog>

<dialog id="logDialog">
  <div class="dialog-head">
    <div style="font-weight:900">Pełny log</div>
    <div class="row">
      <button class="light" type="button" onclick="refreshFullLog()">Odśwież</button>
      <button class="light" type="button" onclick="closeLog()">Zamknij</button>
    </div>
  </div>
  <div class="dialog-body">
    <div id="logContent" style="color:#475569;font-weight:700">Ładowanie…</div>
  </div>
</dialog>

<dialog id="toastDialog">
  <div class="dialog-head">
    <div style="font-weight:900">Informacja</div>
    <button class="light" type="button" onclick="closeToast()">Zamknij</button>
  </div>
  <div style="padding:16px">
    <div id="toastTitle" style="font-weight:900;font-size:18px;margin-bottom:6px">—</div>
    <div id="toastText" style="color:#475569;font-weight:700">—</div>
    <div id="toastBig" style="display:none;font-size:42px;font-weight:900;margin-top:10px">—</div>
  </div>
</dialog>

<div class="card">
  <h2>Szybkie dzwonienie</h2>
  <div class="row">
    <button type="button" id="btnQuickShort" onclick="ringNow(window.quickShortDuration || 3)">Krótki</button>
    <button type="button" class="light" id="btnQuickLong" onclick="ringNow(window.quickLongDuration || 10)">Długi</button>
  </div>
</div>

<details id="settingsPanel">
  <summary>
    <span>Ustawienia</span>
    <span class="chev">▾</span>
  </summary>
  <div class="divider"></div>
  <div class="detailsBody">

    <div class="card" style="margin:16px 0 0">
      <h2>Logo szkoły</h2>
      <form id="logoForm" enctype="multipart/form-data">
        <div class="grid">
          <div class="field" style="grid-column:1/-1">
            <label>Plik logo (PNG, JPG, JPEG, WEBP, SVG)</label>
            <input type="file" name="logo" id="logoInput" accept=".png,.jpg,.jpeg,.webp,.svg">
          </div>
        </div>

        <div class="logo-preview-box">
          <img id="logoPreviewImage" src="" alt="Podgląd logo" style="display:none;">
          <div id="logoPreviewEmpty" class="logo-preview-empty">Brak podglądu</div>
        </div>

        <div class="row" style="margin-top:12px">
          <button id="btnUploadLogo" type="submit" disabled>Wgraj logo</button>
          <button id="btnDeleteLogo" type="button" class="light" {% if not logo_exists %}disabled{% endif %}>Usuń logo</button>
          <div id="logoDirtyMsg" style="display:none;color:#475569;font-weight:700;font-size:14px">Wybrano plik – możesz wgrać.</div>
        </div>
      </form>
    </div>

    <div class="card">
      <h2>Długości dzwonków</h2>
      <form id="bellForm" class="grid">
        <div class="field">
          <label>Krótki ręczny (sekundy)</label>
          <input name="manual_short" value="{{manual_short}}" placeholder="3">
        </div>
        <div class="field">
          <label>Długi ręczny (sekundy)</label>
          <input name="manual_long" value="{{manual_long}}" placeholder="10">
        </div>
        <div class="field">
          <label>Przycisk fizyczny (sekundy)</label>
          <input name="button_ring_duration" value="{{button_ring_duration}}" placeholder="3">
        </div>
        <div class="field">
          <label>Koniec lekcji (sekundy)</label>
          <input name="duration_lesson_end" value="{{duration_lesson_end}}" placeholder="3">
        </div>
        <div class="field">
          <label>Koniec przerwy (sekundy)</label>
          <input name="duration_break_end" value="{{duration_break_end}}" placeholder="3">
        </div>
        <div class="row" style="grid-column:1/-1">
          <button id="btnSaveBell" type="submit" disabled>Zapisz długości</button>
          <div id="bellDirtyMsg" style="display:none;color:#475569;font-weight:700;font-size:14px">Zmieniono długości – możesz zapisać.</div>
        </div>
      </form>
    </div>

    <div class="card">
      <h2>Czas systemowy i NTP</h2>
      <div class="grid">
        <div class="field">
          <label>Strefa czasowa</label>
          <div class="pill" id="timeZonePill">{{timezone_name}}</div>
        </div>
        <div class="field">
          <label>Czas serwera</label>
          <div class="pill" id="serverTimePill">{{server_time_pretty}}</div>
        </div>
        <div class="field">
          <label>Status NTP</label>
          <div class="pill" id="ntpPill">{{time_details}}</div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>Widok licznika</h2>
      <label class="switchline">
        <input type="checkbox" id="toggleProgressBar" {% if progress_bar_enabled %}checked{% endif %}>
        <span>Pokaż poziomy pasek postępu lekcji / przerwy</span>
      </label>
    </div>

    <div class="card">
      <h2>Godziny otwarcia szkoły</h2>
      <form id="hoursForm" class="grid">
        <div class="field">
          <label>Otwarcie (HH:MM)</label>
          <input name="open_from" value="{{open_from}}" placeholder="07:30">
        </div>
        <div class="field">
          <label>Zamknięcie (HH:MM)</label>
          <input name="open_to" value="{{open_to}}" placeholder="18:00">
        </div>
        <div class="row" style="grid-column:1/-1">
          <button id="btnSaveHours" type="submit" disabled>Zapisz</button>
          <div id="hoursDirtyMsg" style="display:none;color:#475569;font-weight:700;font-size:14px">Zmieniono godziny – możesz zapisać.</div>
        </div>
      </form>
    </div>

    <div class="card">
      <h2>W jakie dni mają działać dzwonki</h2>
      <form id="weeklyForm" class="grid">
        {% for key,label in days %}
          <div class="field">
            <label>{{label}}</label>
            <select name="day_{{key}}">
              {% for p in profile_choices %}
                <option value="{{p}}" {% if weekly.get(key,'off')==p %}selected{% endif %}>{{p}}</option>
              {% endfor %}
            </select>
          </div>
        {% endfor %}
        <div class="row" style="grid-column:1/-1">
          <button id="btnSaveWeekly" type="submit" disabled>Zapisz tydzień</button>
          <div id="weeklyDirtyMsg" style="display:none;color:#475569;font-weight:700;font-size:14px">Zmieniono ustawienia – możesz zapisać.</div>
        </div>
      </form>
    </div>

    <div class="card">
      <h2>Plan (profil)</h2>

      <div class="grid">
        <div class="field">
          <label>Nowy profil</label>
          <input id="newProfileName" placeholder="np. skrócony">
        </div>
        <div class="row" style="align-items:end">
          <button id="btnAddProfile" type="button">Dodaj profil</button>
        </div>
      </div>
      <div id="profileManageMsg" style="display:none;margin-top:10px;color:#475569;font-weight:700;font-size:14px"></div>

      <div style="height:34px"></div>

      <div class="row" style="justify-content:space-between;align-items:end;gap:18px">
        <div class="row" style="gap:14px;align-items:end;flex-wrap:nowrap">
          <div class="field" style="min-width:260px;max-width:360px">
            <label>Edytowany profil</label>
            <select id="profileSelect" name="profile">
              {% for p in profiles %}
                <option value="{{p}}" {% if p==active_profile %}selected{% endif %}>{{p}}</option>
              {% endfor %}
            </select>
          </div>
          <button id="btnDeleteProfile" type="button" class="danger">Usuń ten profil</button>
        </div>
        <div class="pill">Dzisiaj ustawione: <b>{{today_profile}}</b></div>
      </div>

      <div style="height:18px"></div>

      <form id="planForm" class="grid">
        <input type="hidden" id="planProfileInput" name="profile" value="{{active_profile}}">
        <div class="field">
          <label>Start pierwszej lekcji (HH:MM)</label>
          <input id="dayStartInput" name="day_start" value="{{day_start}}" placeholder="08:30">
        </div>
        <div class="field">
          <label>Długości lekcji (min, po przecinku)</label>
          <input id="lessonsInput" name="lessons" value="{{lessons_csv}}" placeholder="45,45,45,45">
        </div>
        <div class="field">
          <label>Długości przerw (min, po przecinku)</label>
          <input id="breaksInput" name="breaks" value="{{breaks_csv}}" placeholder="10,10,10">
          <div id="planRuleMsg" style="display:none;margin-top:8px;color:#9f1239;font-weight:900;font-size:13px"></div>
        </div>
        <div class="row" style="grid-column:1/-1">
          <button id="btnSavePlan" type="submit" disabled>Zapisz plan</button>
          <div style="color:#475569;font-weight:700;font-size:14px">Przerw powinno być o 1 mniej niż lekcji.</div>
          <div id="planDirtyMsg" style="display:none;color:#475569;font-weight:700;font-size:14px">Zmieniono plan – możesz zapisać.</div>
        </div>
      </form>
    </div>

    <div class="card">
      <h2>Log</h2>
      <pre id="tailLog" style="margin:0">{{log_tail}}</pre>
      <div class="row" style="margin-top:12px">
        <button class="light" type="button" onclick="openLog()">Pełny log</button>
        <button class="light" type="button" onclick="refreshTail()">Odśwież</button>
      </div>
    </div>

  </div>
</details>

<div style="margin:28px 0 12px;text-align:center;color:#64748b;font-size:13px;font-weight:600;letter-spacing:.01em;">
  © 2026 Dominik Bernaszuk · Projekt i wykonanie
</div>

<script>
  window.quickShortDuration = {{ manual_short|int }};
  window.quickLongDuration = {{ manual_long|int }};

  const settingsPanel = document.getElementById("settingsPanel");
  const progressToggle = document.getElementById("toggleProgressBar");
  const progressBar = document.getElementById("statusProgress");
  const progressFill = document.getElementById("statusProgressFill");
  const progressLabel = document.getElementById("progressLabel");

  try{
    const saved = localStorage.getItem("sb_settings_open");
    if(saved === "1") settingsPanel.open = true;
    settingsPanel.addEventListener("toggle", () => {
      localStorage.setItem("sb_settings_open", settingsPanel.open ? "1" : "0");
    });
  }catch(e){}

  if(progressToggle){
    progressToggle.addEventListener("change", async () => {
      await setProgressEnabled(progressToggle.checked);
    });
  }

  function pad(n){ return (n<10?"0":"")+n; }

  function isProgressEnabled(){
    return progressToggle ? !!progressToggle.checked : false;
  }

  async function setProgressEnabled(v){
    if(progressToggle) progressToggle.checked = !!v;
    updateProgressBar();

    try{
      const fd = new FormData();
      fd.set("enabled", v ? "1" : "0");
      const r = await fetch("/ui/progress", {
        method: "POST",
        body: fd,
        headers: { "Accept": "application/json" }
      });
      const data = await r.json();
      if(!data.ok){
        throw new Error(data.error || "save failed");
      }
      if(progressToggle) progressToggle.checked = !!data.progress_bar_enabled;
      updateProgressBar();
      await refreshLogsAfterAction();
    }catch(e){
      openToast("Błąd", "Nie udało się zapisać ustawienia paska postępu.", null, 2200);
    }
  }

  function fmtMMSS(total){
    total = Math.max(0, Math.ceil(total));
    return pad(Math.floor(total/60))+":"+pad(total%60);
  }
  function fmtHoursMinutes(totalSeconds){
    totalSeconds = Math.max(0, Math.floor(totalSeconds));
    const mins = Math.round(totalSeconds / 60);
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    return h + " h " + m + " min";
  }

  function refreshLogo(){
    const img = document.getElementById("schoolLogo");
    const btnDelete = document.getElementById("btnDeleteLogo");
    img.src = "/logo-image?v=" + Date.now();
    img.onload = function(){
      img.style.display = "block";
      btnDelete.disabled = false;
    };
    img.onerror = function(){
      img.style.display = "none";
      btnDelete.disabled = true;
    };
  }

  function resetLogoPreview(){
    const previewImg = document.getElementById("logoPreviewImage");
    const previewEmpty = document.getElementById("logoPreviewEmpty");
    previewImg.src = "";
    previewImg.style.display = "none";
    previewEmpty.style.display = "block";
    previewEmpty.textContent = "Brak podglądu";
  }

  function setLogoPreview(file){
    const previewImg = document.getElementById("logoPreviewImage");
    const previewEmpty = document.getElementById("logoPreviewEmpty");

    if(!file){
      resetLogoPreview();
      return;
    }

    const reader = new FileReader();
    reader.onload = function(e){
      previewImg.src = e.target.result;
      previewImg.style.display = "block";
      previewEmpty.style.display = "none";
    };
    reader.onerror = function(){
      resetLogoPreview();
    };
    reader.readAsDataURL(file);
  }

  function tickClock(){
    const d = new Date();
    document.getElementById("clock").textContent =
      pad(d.getHours())+":"+pad(d.getMinutes())+":"+pad(d.getSeconds());
  }
  setInterval(tickClock, 1000); tickClock();

  let last = null;
  let serverOffsetMs = 0;

  function getClientNowMsPrecise(){
    return performance.timeOrigin + performance.now();
  }

  function getSyncedNowMs(){
    return getClientNowMsPrecise() + serverOffsetMs;
  }

  function getLeftSeconds(){
    if(!last || last.target_ts_ms == null) return null;
    return (last.target_ts_ms - getSyncedNowMs()) / 1000.0;
  }

  async function fetchStatus(){
    const t0 = getClientNowMsPrecise();
    const r = await fetch("/status", { cache: "no-store" });
    const t1 = getClientNowMsPrecise();
    if(!r.ok) return;

    last = await r.json();

    const midpoint = (t0 + t1) / 2.0;
    if(last.server_now_ts_ms != null){
      serverOffsetMs = last.server_now_ts_ms - midpoint;
    }

    const card = document.getElementById("statusCard");
    card.classList.remove("lesson","break","off","outside","closed","prestart");
    card.classList.add(last.state || "outside");

    const counterBlock = document.getElementById("counterBlock");
    const counterTitle = document.getElementById("counterTitle");
    const endLine = document.getElementById("endLine");

    const smallLine = document.getElementById("smallLine");
    if(last.label_small){
      smallLine.style.display = "";
      smallLine.textContent = last.label_small;
    } else {
      smallLine.style.display = "none";
      smallLine.textContent = "";
    }

    const btnOff = document.getElementById("btnTodayOff");
    const btnOn  = document.getElementById("btnTodayOn");
    const activeWindow = (last.state === "lesson" || last.state === "break" || last.state === "prestart");
    const isTodayOff = (last.today_override === "off");
    const canRestoreToday = isTodayOff;

    if(btnOff && btnOn){
      if(activeWindow){
        btnOff.disabled = isTodayOff;
        btnOn.disabled  = !isTodayOff;
      } else if (canRestoreToday) {
        btnOff.disabled = true;
        btnOn.disabled  = false;
      } else {
        btnOff.disabled = true;
        btnOn.disabled  = true;
      }
    }

    document.getElementById("pillProfile").textContent = "Profil: " + (last.profile || "—");

    if(last.state === "lesson" || last.state === "break"){
      endLine.textContent = last.end ? ("Koniec o " + last.end) : "";
    } else {
      endLine.textContent = "";
    }

    document.getElementById("stateLine").textContent = last.label || "—";

    if(last.state === "lesson" || last.state === "break" || last.state === "prestart"){
      counterBlock.style.display = "";
      counterTitle.style.display = (last.state === "prestart") ? "none" : "";
    } else {
      counterBlock.style.display = "none";
      counterTitle.style.display = "";
    }

    tickCounter();
    updateProgressBar();
  }
  setInterval(fetchStatus, 250); fetchStatus();

  async function refreshTimeInfo(){
    try{
      const r = await fetch("/time_info");
      if(!r.ok) return;
      const data = await r.json();
      document.getElementById("timeZonePill").textContent = data.timezone || "—";
      document.getElementById("serverTimePill").textContent = data.server_time_pretty || "—";
      const ntp = document.getElementById("ntpPill");
      ntp.textContent = data.details || "—";
      ntp.classList.remove("status-ok", "status-bad");
      if(data.ntp_synchronized === true){
        ntp.classList.add("status-ok");
      }else if(data.ntp_synchronized === false){
        ntp.classList.add("status-bad");
      }
    }catch(e){}
  }
  setInterval(refreshTimeInfo, 30000);
  refreshTimeInfo();


  function updateProgressBar(){
    const enabled = isProgressEnabled();
    const startMs = last ? last.segment_start_ts_ms : null;
    const targetMs = last ? last.target_ts_ms : null;

    if(!enabled || !last || startMs == null || targetMs == null || targetMs <= startMs){
      if(progressBar) progressBar.style.display = "none";
      if(progressLabel) progressLabel.style.display = "none";
      return;
    }

    const nowMs = getSyncedNowMs();
    const total = targetMs - startMs;
    const elapsed = Math.max(0, nowMs - startMs);
    const pct = Math.max(0, Math.min(100, (elapsed / total) * 100));

    if(progressBar) progressBar.style.display = "block";
    if(progressLabel) progressLabel.style.display = "block";
    
    if(progressFill){
      if(last && last.state === "lesson"){
        progressFill.style.background = "repeating-linear-gradient(45deg,#ffd6d6 0 10px,#ffc2c2 10px 20px)";
      }else{
        progressFill.style.background = "repeating-linear-gradient(45deg,#dff7df 0 10px,#c9f0c9 10px 20px)";
      }
      progressFill.style.width = pct.toFixed(3) + "%";
    }

  }

  function tickCounter(){
    if(!last) return;

    const left = getLeftSeconds();

    if(last.state === "lesson" || last.state === "break" || last.state === "prestart"){
      if(left != null){
        document.getElementById("counter").textContent = fmtMMSS(left);
      }
    } else if(last.state === "outside" || last.state === "closed" || last.state === "off"){
      if(left != null){
        document.getElementById("stateLine").textContent =
          (last.label || "Następny dzwonek") + " (za " + fmtHoursMinutes(left) + ")";
      }
    }

    updateProgressBar();
  }
  setInterval(tickCounter, 50);

  let toastTimer = null;
  function openToast(title, text, bigText, autoCloseMs){
    const dlg = document.getElementById("toastDialog");
    document.getElementById("toastTitle").textContent = title || "Informacja";
    document.getElementById("toastText").textContent = text || "";
    const big = document.getElementById("toastBig");
    if(bigText !== null && bigText !== undefined){
      big.style.display = "";
      big.textContent = bigText;
    } else {
      big.style.display = "none";
    }
    if(dlg.showModal) dlg.showModal();
    if(autoCloseMs && autoCloseMs > 0){
      setTimeout(() => { if(dlg.open) closeToast(); }, autoCloseMs);
    }
  }
  function closeToast(){
    const dlg = document.getElementById("toastDialog");
    if(dlg.close) dlg.close();
    if(toastTimer){ clearInterval(toastTimer); toastTimer = null; }
  }

  async function ringNow(duration){
    openToast("Dzwonienie", "Trwa dzwonek…", fmtMMSS(duration), 0);
    let remain = duration;
    if(toastTimer) clearInterval(toastTimer);
    toastTimer = setInterval(() => {
      remain -= 1;
      if(remain <= 0){
        clearInterval(toastTimer);
        document.getElementById("toastBig").textContent = "KONIEC";
        setTimeout(() => closeToast(), 2000);
        return;
      }
      document.getElementById("toastBig").textContent = fmtMMSS(remain);
    }, 1000);

    try{
      const fd = new FormData();
      fd.set("duration", String(duration));
      const r = await fetch("/ring", { method:"POST", body: fd, headers: { "Accept":"application/json" }});
      const data = await r.json();
      if(!data.ok){
        openToast("Błąd", data.error || "Nie udało się uruchomić dzwonka.", null, 2500);
        return;
      }
      await refreshLogsAfterAction();
    }catch(e){
      openToast("Błąd", "Błąd połączenia.", null, 2500);
    }
  }

  async function postForm(url, formEl){
    const fd = new FormData(formEl);
    const r = await fetch(url, { method:"POST", body: fd, headers: { "Accept":"application/json" }});
    return await r.json();
  }

  function formSnapshot(form){
    const fd = new FormData(form);
    const arr = [];
    for(const pair of fd.entries()){
      arr.push(pair[0] + "=" + String(pair[1]));
    }
    arr.sort();
    return arr.join("&");
  }

  const logoForm = document.getElementById("logoForm");
  const logoInput = document.getElementById("logoInput");
  const btnUploadLogo = document.getElementById("btnUploadLogo");
  const btnDeleteLogo = document.getElementById("btnDeleteLogo");
  const logoDirtyMsg = document.getElementById("logoDirtyMsg");

  function updateLogoDirty(){
    const hasFile = logoInput.files && logoInput.files.length > 0;
    btnUploadLogo.disabled = !hasFile;
    logoDirtyMsg.style.display = hasFile ? "" : "none";
    if(hasFile){
      setLogoPreview(logoInput.files[0]);
    } else {
      resetLogoPreview();
    }
  }
  logoInput.addEventListener("change", updateLogoDirty);

  logoForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if(btnUploadLogo.disabled) return;
    openToast("Proszę czekać…", "Wgrywanie logo…", null, 0);
    try{
      const fd = new FormData(logoForm);
      const r = await fetch("/logo/upload", { method:"POST", body: fd, headers: { "Accept":"application/json" }});
      const data = await r.json();
      if(!data.ok){
        openToast("Błąd", data.error || "Nie udało się wgrać logo.", null, 2500);
        return;
      }
      logoForm.reset();
      updateLogoDirty();
      resetLogoPreview();
      refreshLogo();
      openToast("Zapisano", "Logo zostało wgrane.", null, 1400);
      await refreshLogsAfterAction();
    }catch(err){
      openToast("Błąd", "Błąd połączenia.", null, 2500);
    }
  });

  btnDeleteLogo.addEventListener("click", async () => {
    openToast("Proszę czekać…", "Usuwanie logo…", null, 0);
    try{
      const r = await fetch("/logo/delete", { method:"POST", headers: { "Accept":"application/json" }});
      const data = await r.json();
      if(!data.ok){
        openToast("Błąd", data.error || "Nie udało się usunąć logo.", null, 2500);
        return;
      }
      resetLogoPreview();
      refreshLogo();
      openToast("Usunięto", "Logo zostało usunięte.", null, 1400);
      await refreshLogsAfterAction();
    }catch(err){
      openToast("Błąd", "Błąd połączenia.", null, 2500);
    }
  });

  const bellForm = document.getElementById("bellForm");
  const btnSaveBell = document.getElementById("btnSaveBell");
  const bellDirtyMsg = document.getElementById("bellDirtyMsg");
  let bellInitial = formSnapshot(bellForm);

  function updateBellDirty(){
    const dirty = (formSnapshot(bellForm) !== bellInitial);
    btnSaveBell.disabled = !dirty;
    bellDirtyMsg.style.display = dirty ? "" : "none";
  }
  bellForm.querySelectorAll("input").forEach(el => el.addEventListener("input", updateBellDirty));
  bellForm.querySelectorAll("input").forEach(el => el.addEventListener("change", updateBellDirty));
  updateBellDirty();

  bellForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if(btnSaveBell.disabled) return;
    openToast("Proszę czekać…", "Zapisywanie…", null, 0);
    try{
      const data = await postForm("/save_bell", bellForm);
      if(!data.ok){
        openToast("Błąd", data.error || "Nie udało się zapisać długości.", null, 2500);
        return;
      }
      bellInitial = formSnapshot(bellForm);
      updateBellDirty();
      window.quickShortDuration = parseInt(bellForm.querySelector('[name="manual_short"]').value || "3", 10);
      window.quickLongDuration = parseInt(bellForm.querySelector('[name="manual_long"]').value || "10", 10);
      openToast("Zapisano", "Długości dzwonków zapisane.", null, 1400);
      await refreshLogsAfterAction();
    }catch(err){
      openToast("Błąd", "Błąd połączenia.", null, 2500);
    }
  });

  const hoursForm = document.getElementById("hoursForm");
  const btnSaveHours = document.getElementById("btnSaveHours");
  const hoursDirtyMsg = document.getElementById("hoursDirtyMsg");
  let hoursInitial = formSnapshot(hoursForm);
  function updateHoursDirty(){
    const dirty = (formSnapshot(hoursForm) !== hoursInitial);
    btnSaveHours.disabled = !dirty;
    hoursDirtyMsg.style.display = dirty ? "" : "none";
  }
  hoursForm.querySelectorAll("input").forEach(el => el.addEventListener("input", updateHoursDirty));
  hoursForm.querySelectorAll("input").forEach(el => el.addEventListener("change", updateHoursDirty));
  updateHoursDirty();

  hoursForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if(btnSaveHours.disabled) return;
    openToast("Proszę czekać…", "Zapisywanie…", null, 0);
    try{
      const data = await postForm("/save_hours", hoursForm);
      if(!data.ok){
        openToast("Błąd", data.error || "Nie udało się zapisać.", null, 2500);
        return;
      }
      hoursInitial = formSnapshot(hoursForm);
      updateHoursDirty();
      openToast("Zapisano", "Godziny zapisane.", null, 1400);
      fetchStatus();
      await refreshLogsAfterAction();
    }catch(err){
      openToast("Błąd", "Błąd połączenia.", null, 2500);
    }
  });

  const weeklyForm = document.getElementById("weeklyForm");
  const btnSaveWeekly = document.getElementById("btnSaveWeekly");
  const weeklyDirtyMsg = document.getElementById("weeklyDirtyMsg");
  let weeklyInitial = formSnapshot(weeklyForm);
  function updateWeeklyDirty(){
    const dirty = (formSnapshot(weeklyForm) !== weeklyInitial);
    btnSaveWeekly.disabled = !dirty;
    weeklyDirtyMsg.style.display = dirty ? "" : "none";
  }
  weeklyForm.querySelectorAll("select,input").forEach(el => el.addEventListener("change", updateWeeklyDirty));
  weeklyForm.querySelectorAll("select,input").forEach(el => el.addEventListener("input", updateWeeklyDirty));
  updateWeeklyDirty();

  weeklyForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if(btnSaveWeekly.disabled) return;
    openToast("Proszę czekać…", "Zapisywanie…", null, 0);
    try{
      const data = await postForm("/save_weekly", weeklyForm);
      if(!data.ok){
        openToast("Błąd", data.error || "Nie udało się zapisać tygodnia.", null, 2500);
        return;
      }
      weeklyInitial = formSnapshot(weeklyForm);
      updateWeeklyDirty();
      openToast("Zapisano", "Tydzień zapisany.", null, 1400);
      fetchStatus();
      await refreshLogsAfterAction();
    }catch(err){
      openToast("Błąd", "Błąd połączenia.", null, 2500);
    }
  });

  document.querySelectorAll("form.ajax-today").forEach((f) => {
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = f.querySelector("button");
      if(btn && btn.disabled) return;
      openToast("Proszę czekać…", "Wykonywanie…", null, 0);
      try{
        const data = await postForm(f.action, f);
        if(!data.ok){
          openToast("Błąd", data.error || "Nie udało się wykonać.", null, 2500);
          return;
        }
        openToast(f.dataset.title || "OK", f.dataset.msg || "", null, 1400);
        fetchStatus();
        await refreshLogsAfterAction();
      }catch(err){
        openToast("Błąd", "Błąd połączenia.", null, 2500);
      }
    });
  });

  const planForm = document.getElementById("planForm");
  const btnSavePlan = document.getElementById("btnSavePlan");
  const planDirtyMsg = document.getElementById("planDirtyMsg");
  const planProfileInput = document.getElementById("planProfileInput");
  const profileSelect = document.getElementById("profileSelect");
  const dayStartInput = document.getElementById("dayStartInput");
  const lessonsInput = document.getElementById("lessonsInput");
  const breaksInput = document.getElementById("breaksInput");
  const planRuleMsg = document.getElementById("planRuleMsg");
  const newProfileName = document.getElementById("newProfileName");
  const btnAddProfile = document.getElementById("btnAddProfile");
  const btnDeleteProfile = document.getElementById("btnDeleteProfile");
  const profileManageMsg = document.getElementById("profileManageMsg");
  let planInitial = formSnapshot(planForm);

  function parseCsvInts(s){
    s = (s || "").trim();
    if(!s) return [];
    return s.split(",").map(x => x.trim()).filter(x => x.length>0).map(x => parseInt(x,10)).filter(x => !Number.isNaN(x));
  }
  function validatePlanRule(){
    const lessons = parseCsvInts(lessonsInput.value);
    const br = parseCsvInts(breaksInput.value);
    if(lessons.length === 0){
      planRuleMsg.style.display = "none";
      return true;
    }
    const ok = (br.length === Math.max(0, lessons.length - 1));
    if(!ok){
      planRuleMsg.style.display = "";
      planRuleMsg.textContent = "Błąd: Przerw ma być o 1 mniej niż lekcji. (Lekcji: " + lessons.length + ", Przerw: " + br.length + ")";
      return false;
    }
    planRuleMsg.style.display = "none";
    return true;
  }
  function updatePlanDirty(){
    const dirty = (formSnapshot(planForm) !== planInitial);
    const ruleOk = validatePlanRule();
    btnSavePlan.disabled = !(dirty && ruleOk);
    planDirtyMsg.style.display = dirty ? "" : "none";
  }
  planForm.querySelectorAll("select,input").forEach(el => el.addEventListener("change", updatePlanDirty));
  planForm.querySelectorAll("select,input").forEach(el => el.addEventListener("input", updatePlanDirty));
  updatePlanDirty();

  async function loadProfileIntoPlan(profileName){
    const currentY = window.scrollY;
    try{
      openToast("Proszę czekać…", "Wczytywanie profilu…", null, 0);

      const r = await fetch("/get_timetable?profile=" + encodeURIComponent(profileName));
      const data = await r.json();

      if(!data.ok){
        openToast("Błąd", data.error || "Nie udało się wczytać profilu.", null, 2500);
        return;
      }

      planProfileInput.value = data.profile || profileName;
      dayStartInput.value = data.day_start || "08:30";
      lessonsInput.value = data.lessons_csv || "";
      breaksInput.value = data.breaks_csv || "";

      planInitial = formSnapshot(planForm);
      updatePlanDirty();

      window.scrollTo({ top: currentY, behavior: "auto" });
      openToast("Wczytano", "Profil został wczytany.", null, 900);
    }catch(err){
      openToast("Błąd", "Błąd połączenia.", null, 2500);
    }
  }

  if(profileSelect){
    profileSelect.addEventListener("change", async () => {
      await loadProfileIntoPlan(profileSelect.value);
    });
  }

  function showProfileManageMsg(text, isError){
    if(!profileManageMsg) return;
    profileManageMsg.style.display = "";
    profileManageMsg.style.color = isError ? "#9f1239" : "#475569";
    profileManageMsg.textContent = text;
  }

  function refreshProfileUi(profiles, activeProfile){
    if(!Array.isArray(profiles) || !profiles.length) return;

    if(profileSelect){
      profileSelect.innerHTML = "";
      profiles.forEach((p) => {
        const opt = document.createElement("option");
        opt.value = p;
        opt.textContent = p;
        if(p === activeProfile) opt.selected = true;
        profileSelect.appendChild(opt);
      });
    }

    if(planProfileInput){
      planProfileInput.value = activeProfile;
    }

    document.querySelectorAll('#weeklyForm select[name^="day_"]').forEach((sel) => {
      const current = sel.value;
      const values = ["off", ...profiles];
      sel.innerHTML = "";
      values.forEach((v) => {
        const opt = document.createElement("option");
        opt.value = v;
        opt.textContent = v;
        if(v === current) opt.selected = true;
        sel.appendChild(opt);
      });
      if(!values.includes(current)){
        sel.value = "off";
      }
    });

    weeklyInitial = formSnapshot(weeklyForm);
    updateWeeklyDirty();
  }

  if(btnAddProfile){
    btnAddProfile.addEventListener("click", async () => {
      const name = (newProfileName.value || "").trim();
      if(!name){
        showProfileManageMsg("Podaj nazwę nowego profilu.", true);
        return;
      }
      openToast("Proszę czekać…", "Dodawanie profilu…", null, 0);
      try{
        const fd = new FormData();
        fd.set("name", name);
        const r = await fetch("/profile/add", { method:"POST", body: fd, headers: { "Accept":"application/json" }});
        const data = await r.json();
        if(!data.ok){
          openToast("Błąd", data.error || "Nie udało się dodać profilu.", null, 2500);
          showProfileManageMsg(data.error || "Nie udało się dodać profilu.", true);
          return;
        }

        const active = data.profile || name;
        refreshProfileUi(data.profiles || [], active);
        newProfileName.value = "";
        showProfileManageMsg("Dodano profil.", false);
        await loadProfileIntoPlan(active);
        openToast("Zapisano", "Dodano profil.", null, 1200);
        await refreshLogsAfterAction();
      }catch(err){
        openToast("Błąd", "Błąd połączenia.", null, 2500);
        showProfileManageMsg("Błąd połączenia.", true);
      }
    });
  }

  if(btnDeleteProfile){
    btnDeleteProfile.addEventListener("click", async () => {
      const profile = profileSelect ? profileSelect.value : "";
      if(!profile) return;
      if(!confirm('Usunąć profil: ' + profile + '?')) return;
      openToast("Proszę czekać…", "Usuwanie profilu…", null, 0);
      try{
        const fd = new FormData();
        fd.set("name", profile);
        const r = await fetch("/profile/delete", { method:"POST", body: fd, headers: { "Accept":"application/json" }});
        const data = await r.json();
        if(!data.ok){
          openToast("Błąd", data.error || "Nie udało się usunąć profilu.", null, 2500);
          showProfileManageMsg(data.error || "Nie udało się usunąć profilu.", true);
          return;
        }

        const active = data.active_profile || "";
        refreshProfileUi(data.profiles || [], active);
        showProfileManageMsg("Usunięto profil.", false);
        if(active){
          await loadProfileIntoPlan(active);
        }
        openToast("Usunięto", "Usunięto profil.", null, 1200);
        await refreshLogsAfterAction();
      }catch(err){
        openToast("Błąd", "Błąd połączenia.", null, 2500);
        showProfileManageMsg("Błąd połączenia.", true);
      }
    });
  }

  planForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if(btnSavePlan.disabled){
      if(!validatePlanRule()){
        openToast("Błąd", "Nie można zapisać: Przerw ma być o 1 mniej niż lekcji.", null, 2500);
      }
      return;
    }
    openToast("Proszę czekać…", "Zapisywanie…", null, 0);
    try{
      const data = await postForm("/save_timetable", planForm);
      if(!data.ok){
        openToast("Błąd", data.error || "Nie udało się zapisać planu.", null, 2500);
        return;
      }
      planInitial = formSnapshot(planForm);
      updatePlanDirty();
      openToast("Zapisano", "Plan zapisany.", null, 1400);
      fetchStatus();
      await refreshLogsAfterAction();
    }catch(err){
      openToast("Błąd", "Błąd połączenia.", null, 2500);
    }
  });

  async function reloadCalendar(){
    const box = document.getElementById("calContent");
    const r = await fetch("/calendar?embed=1");
    box.innerHTML = await r.text();
    bindCalendarForms();
  }
  function showCalMsg(text, isError){
    const msg = document.querySelector("#calContent #calMsg");
    if(!msg) return;
    msg.style.display = "";
    msg.style.padding = "10px 12px";
    msg.style.borderRadius = "12px";
    msg.style.border = "1px solid";
    msg.style.marginBottom = "12px";
    msg.style.background = isError ? "#fff1f2" : "#ecfdf5";
    msg.style.borderColor = isError ? "#fecdd3" : "#bbf7d0";
    msg.style.color = isError ? "#9f1239" : "#065f46";
    msg.textContent = text;
  }
  function bindCalendarForms(){
    const addForm = document.querySelector("#calContent #calAddForm");
    if(addForm){
      addForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        try{
          const fd = new FormData(addForm);
          const r = await fetch(addForm.action, { method:"POST", body: fd, headers: { "Accept":"application/json" }});
          const data = await r.json();
          if(!data.ok){
            showCalMsg(data.error || "Błąd zapisu.", true);
            return;
          }
          await reloadCalendar();
          showCalMsg("Dodano zakres.", false);
          fetchStatus();
          await refreshLogsAfterAction();
        }catch(err){
          showCalMsg("Błąd połączenia.", true);
        }
      });
    }
    document.querySelectorAll("#calContent .calDeleteForm").forEach((f) => {
      f.addEventListener("submit", async (e) => {
        e.preventDefault();
        try{
          const fd = new FormData(f);
          const r = await fetch(f.action, { method:"POST", body: fd, headers: { "Accept":"application/json" }});
          const data = await r.json();
          if(!data.ok){
            showCalMsg(data.error || "Nie udało się usunąć.", true);
            return;
          }
          await reloadCalendar();
          showCalMsg("Usunięto zakres.", false);
          fetchStatus();
          await refreshLogsAfterAction();
        }catch(err){
          showCalMsg("Błąd połączenia.", true);
        }
      });
    });
  }
  async function openCalendar(){
    const dlg = document.getElementById("calDialog");
    if(!dlg.showModal){
      window.location.href = "/calendar";
      return;
    }
    dlg.showModal();
    document.getElementById("calContent").textContent = "Ładowanie…";
    await reloadCalendar();
  }
  function closeCalendar(){
    const dlg = document.getElementById("calDialog");
    if(dlg.close) dlg.close();
  }

  async function refreshTail(){
    const r = await fetch("/log/tail");
    if(!r.ok) return;
    document.getElementById("tailLog").textContent = await r.text();
  }

  async function refreshLogsAfterAction(){
    try{
      await refreshTail();
      const dlg = document.getElementById("logDialog");
      if(dlg && dlg.open){
        await refreshFullLog();
      }
    }catch(e){}
  }

  async function openLog(){
    const dlg = document.getElementById("logDialog");
    dlg.showModal();
    document.getElementById("logContent").textContent = "Ładowanie…";
    await refreshFullLog();
  }
  function closeLog(){
    const dlg = document.getElementById("logDialog");
    if(dlg.close) dlg.close();
  }
  async function refreshFullLog(){
    const r = await fetch("/log/full?embed=1");
    document.getElementById("logContent").innerHTML = await r.text();
    bindLogModal();
  }
  function showLogMsg(text, isError){
    const msg = document.querySelector("#logContent #logMsg");
    if(!msg) return;
    msg.style.display = "";
    msg.style.padding = "10px 12px";
    msg.style.borderRadius = "12px";
    msg.style.border = "1px solid";
    msg.style.marginBottom = "12px";
    msg.style.background = isError ? "#fff1f2" : "#ecfdf5";
    msg.style.borderColor = isError ? "#fecdd3" : "#bbf7d0";
    msg.style.color = isError ? "#9f1239" : "#065f46";
    msg.textContent = text;
  }
  async function loadSelectedLog(){
    const sel = document.querySelector("#logContent #logFileSelect");
    const name = sel ? sel.value : "events.log";
    const r = await fetch("/log/read?name=" + encodeURIComponent(name));
    const txt = await r.text();
    const box = document.querySelector("#logContent #fullLogBox");
    if(box) box.textContent = txt || "(pusty)";
  }
  async function logOp(url, payload){
    const fd = new FormData();
    for(const k in payload){ fd.set(k, payload[k]); }
    const r = await fetch(url, { method:"POST", body: fd, headers: { "Accept":"application/json" }});
    return await r.json();
  }
  function bindLogModal(){
    const sel = document.querySelector("#logContent #logFileSelect");
    const btnRefresh = document.querySelector("#logContent #btnLogRefresh");
    const btnDownload = document.querySelector("#logContent #btnLogDownload");
    const btnClearAll = document.querySelector("#logContent #btnLogClearAll");
    const btnDeleteFile = document.querySelector("#logContent #btnLogDeleteFile");

    if(sel) sel.addEventListener("change", loadSelectedLog);
    if(btnRefresh) btnRefresh.addEventListener("click", loadSelectedLog);
    if(btnDownload){
      btnDownload.addEventListener("click", () => {
        const name = sel ? sel.value : "events.log";
        window.location.href = "/log/download?name=" + encodeURIComponent(name);
      });
    }
    if(btnClearAll){
      btnClearAll.addEventListener("click", async () => {
        const ok = confirm("Usunąć wszystkie logi?");
        if(!ok) return;
        const data = await logOp("/log/clear_all", {});
        if(!data.ok){ showLogMsg(data.error || "Błąd.", true); return; }
        showLogMsg("Usunięto wszystkie logi.", false);
        await refreshLogsAfterAction();
      });
    }
    if(btnDeleteFile){
      btnDeleteFile.addEventListener("click", async () => {
        const name = sel ? sel.value : "events.log";
        const data = await logOp("/log/delete_file", { name: name });
        if(!data.ok){ showLogMsg(data.error || "Błąd.", true); return; }
        showLogMsg("Usunięto plik.", false);
        await refreshLogsAfterAction();
      });
    }
    loadSelectedLog();
  }
</script>

</body>
</html>
"""


# ---------------- routes ----------------
@app.get("/health")
def health():
    return jsonify(ok=True)


@app.get("/logo-image")
def logo_image():
    p = get_logo_file()
    if not p:
        abort(404)
    return send_file(p)


@app.post("/logo/upload")
def logo_upload():
    if "logo" not in request.files and "file" not in request.files:
        return jsonify(ok=False, error="Nie wybrano pliku.")

    f = request.files.get("logo") or request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, error="Nie wybrano pliku.")

    original = secure_filename(f.filename)
    ext = os.path.splitext(original)[1].lower()

    if ext not in ALLOWED_LOGO_EXTENSIONS:
        return jsonify(ok=False, error="Dozwolone pliki: PNG, JPG, JPEG, WEBP, SVG.")

    delete_existing_logo()
    target = os.path.join(LOGO_DIR, f"{LOGO_PREFIX}{ext}")

    try:
        f.save(target)
        log_event(f"logo uploaded ({os.path.basename(target)})")
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.post("/logo/delete")
def logo_delete():
    try:
        delete_existing_logo()
        log_event("logo deleted")
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.post("/save_bell")
def save_bell():
    cfg = load_config()

    try:
        manual_short = int(request.form.get("manual_short", "3"))
        manual_long = int(request.form.get("manual_long", "10"))
        button_ring_duration = int(request.form.get("button_ring_duration", "3"))
        duration_lesson_end = int(request.form.get("duration_lesson_end", "3"))
        duration_break_end = int(request.form.get("duration_break_end", "3"))
    except Exception:
        return jsonify(ok=False, error="Wszystkie długości muszą być liczbami.")

    vals = [manual_short, manual_long, button_ring_duration, duration_lesson_end, duration_break_end]
    if any(v < 1 or v > 120 for v in vals):
        return jsonify(ok=False, error="Dozwolony zakres: 1–120 sekund.")

    cfg.setdefault("bell", {})
    cfg["bell"]["manual_short"] = manual_short
    cfg["bell"]["manual_long"] = manual_long
    cfg["bell"]["duration_lesson_end"] = duration_lesson_end
    cfg["bell"]["duration_break_end"] = duration_break_end

    cfg.setdefault("button", {})
    cfg["button"]["ring_duration"] = button_ring_duration

    save_config(cfg)
    log_event("bell durations saved")
    return jsonify(ok=True)


@app.get("/time_info")
def time_info():
    cfg = load_config()
    return jsonify(get_time_info(cfg))


@app.post("/profile/add")
def profile_add():
    cfg = load_config()
    name = sanitize_profile_name(request.form.get("name", ""))
    if not name:
        return jsonify(ok=False, error="Nazwa profilu może zawierać tylko litery, cyfry, myślnik i podkreślenie (max 32 znaki).")

    timetables = cfg.get("timetables") or {}
    if name in timetables:
        return jsonify(ok=False, error="Taki profil już istnieje.")

    timetables[name] = {
        "day_start": "08:30",
        "lessons": [45, 45, 45, 45],
        "breaks": [10, 10, 10],
    }
    cfg["timetables"] = timetables
    save_config(cfg)
    log_event(f"profile added ({name})")
    return jsonify(ok=True, profile=name, profiles=sorted(timetables.keys()))


@app.post("/profile/delete")
def profile_delete():
    cfg = load_config()
    name = (request.form.get("name") or "").strip()
    timetables = cfg.get("timetables") or {}

    if name not in timetables:
        return jsonify(ok=False, error="Nie znaleziono profilu.")

    if len(timetables) <= 1:
        return jsonify(ok=False, error="Nie można usunąć ostatniego profilu.")

    in_use, reason = profile_in_use(cfg, name)
    if in_use:
        return jsonify(ok=False, error=reason)

    del timetables[name]
    cfg["timetables"] = timetables
    save_config(cfg)
    log_event(f"profile deleted ({name})")

    profiles = sorted(timetables.keys())
    active_profile = "normal" if "normal" in profiles else profiles[0]
    return jsonify(ok=True, active_profile=active_profile, profiles=profiles)


@app.get("/get_timetable")
def get_timetable():
    cfg = load_config()
    profiles = sorted((cfg.get("timetables") or {}).keys()) or ["normal"]

    profile = (request.args.get("profile") or "").strip()
    if profile not in profiles:
        return jsonify(ok=False, error="Nieznany profil.")

    tt = (cfg.get("timetables") or {}).get(profile) or {}
    return jsonify(
        ok=True,
        profile=profile,
        day_start=tt.get("day_start", "08:30"),
        lessons_csv=",".join(str(x) for x in (tt.get("lessons") or [])),
        breaks_csv=",".join(str(x) for x in (tt.get("breaks") or [])),
    )


@app.get("/")
def index():
    cfg = load_config()
    rotate_log_if_needed(cfg)

    tz = ZoneInfo(cfg.get("timezone", "Europe/Warsaw"))
    today_prof = resolve_profile(cfg, datetime.now(tz).date())

    weekly = cfg.get("weekly_profile") or {}
    profiles = sorted((cfg.get("timetables") or {}).keys()) or ["normal"]
    active_profile = request.args.get("profile") or ("normal" if "normal" in profiles else profiles[0])

    tt = (cfg.get("timetables") or {}).get(active_profile) or {
        "day_start": "08:30",
        "lessons": [45, 45, 45],
        "breaks": [10, 10]
    }

    day_start = tt.get("day_start", "08:30")
    lessons = tt.get("lessons") or []
    breaks = tt.get("breaks") or []

    _, _, ofs, ots = _school_open_window(cfg)
    bell = cfg.get("bell") or {}
    button = cfg.get("button") or {}
    ti = get_time_info(cfg)

    lp = get_logo_file()
    logo_version = int(os.path.getmtime(lp)) if lp and os.path.exists(lp) else 0

    return render_template_string(
        INDEX_HTML,
        base_style=BASE_STYLE,
        days=DAYS,
        weekly=weekly,
        profiles=profiles,
        profile_choices=(["off"] + profiles),
        active_profile=active_profile,
        today_profile=today_prof,
        day_start=day_start,
        lessons_csv=",".join(str(x) for x in lessons),
        breaks_csv=",".join(str(x) for x in breaks),
        log_tail=tail_log(cfg),
        open_from=ofs,
        open_to=ots,
        logo_exists=logo_exists(),
        logo_version=logo_version,
        manual_short=bell.get("manual_short", 3),
        manual_long=bell.get("manual_long", 10),
        button_ring_duration=button.get("ring_duration", 3),
        duration_lesson_end=bell.get("duration_lesson_end", 3),
        duration_break_end=bell.get("duration_break_end", 3),
        timezone_name=ti["timezone"],
        server_time_pretty=ti["server_time_pretty"],
        time_details=ti["details"],
        progress_bar_enabled=bool(((cfg.get("ui") or {}).get("progress_bar_enabled", False))),
    )


@app.get("/status")
def status():
    cfg = load_config()
    rotate_log_if_needed(cfg)
    return jsonify(compute_status(cfg))


@app.post("/ui/progress")
def save_ui_progress():
    try:
        cfg = load_config()
        ui = cfg.setdefault("ui", {})
        raw = (request.form.get("enabled") or "").strip().lower()
        enabled = raw in ("1", "true", "yes", "on")
        ui["progress_bar_enabled"] = enabled
        save_config(cfg)
        log_event(f"ui progress bar {'enabled' if enabled else 'disabled'}")
        return jsonify(ok=True, progress_bar_enabled=enabled)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.post("/ring")
def ring_now():
    try:
        duration = int(request.form.get("duration", "3"))
    except Exception:
        duration = 3
    ring(duration, "manual")
    return jsonify(ok=True, duration=duration)


@app.post("/today_off")
def today_off():
    cfg = load_config()
    set_today_override(cfg, "off")
    save_config(cfg)
    log_event("today_override=off")
    return jsonify(ok=True)


@app.post("/today_on")
def today_on():
    cfg = load_config()
    set_today_override(cfg, "auto")
    save_config(cfg)
    log_event("today_override=auto")
    return jsonify(ok=True)


@app.post("/save_hours")
def save_hours():
    cfg = load_config()
    open_from = (request.form.get("open_from") or "").strip()
    open_to = (request.form.get("open_to") or "").strip()
    if ":" not in open_from or ":" not in open_to:
        return jsonify(ok=False, error="Zły format. Użyj HH:MM.")
    try:
        ofm = parse_hhmm(open_from)
        otm = parse_hhmm(open_to)
    except Exception:
        return jsonify(ok=False, error="Zły format. Użyj HH:MM.")
    if ofm >= otm:
        return jsonify(ok=False, error="Otwarcie musi być wcześniej niż zamknięcie.")
    cfg.setdefault("school", {})
    cfg["school"]["open_from"] = open_from
    cfg["school"]["open_to"] = open_to
    save_config(cfg)
    log_event("school hours saved")
    return jsonify(ok=True)


@app.post("/save_weekly")
def save_weekly():
    cfg = load_config()
    profiles = set((cfg.get("timetables") or {}).keys())
    allowed = profiles | {"off"}

    weekly = cfg.get("weekly_profile") or {}
    for k, _ in DAYS:
        v = request.form.get(f"day_{k}", weekly.get(k, "off"))
        if v not in allowed:
            v = "off"
        weekly[k] = v

    cfg["weekly_profile"] = weekly
    save_config(cfg)
    log_event("weekly profile saved")
    return jsonify(ok=True)


@app.post("/save_timetable")
def save_timetable():
    cfg = load_config()
    profile = (request.form.get("profile", "normal") or "normal").strip()
    if profile not in (cfg.get("timetables") or {}):
        return jsonify(ok=False, error="Nieznany profil.")

    day_start = (request.form.get("day_start", "08:30") or "08:30").strip()
    lessons_raw = (request.form.get("lessons", "") or "").strip()
    breaks_raw = (request.form.get("breaks", "") or "").strip()

    if ":" not in day_start:
        return jsonify(ok=False, error="Zły format startu (HH:MM).")

    def parse_list(s):
        if not s:
            return []
        out = []
        for p in s.split(","):
            p = p.strip()
            if not p:
                continue
            out.append(int(p))
        return out

    try:
        lessons = parse_list(lessons_raw)
        breaks = parse_list(breaks_raw)
    except Exception:
        return jsonify(ok=False, error="Nieprawidłowe liczby w lekcjach/przerwach.")

    if len(lessons) < 1:
        return jsonify(ok=False, error="Podaj przynajmniej jedną lekcję.")
    if any(x <= 0 or x > 300 for x in lessons + breaks):
        return jsonify(ok=False, error="Minuty muszą być >0 i sensowne.")
    if len(breaks) != max(0, len(lessons) - 1):
        return jsonify(ok=False, error="Błąd: Przerw ma być o 1 mniej niż lekcji.")

    cfg.setdefault("timetables", {})
    cfg["timetables"][profile] = {"day_start": day_start, "lessons": lessons, "breaks": breaks}
    save_config(cfg)
    log_event("timetable saved")
    return jsonify(ok=True)


@app.get("/calendar")
def calendar_view():
    cfg = load_config()
    ranges = normalize_ranges(cfg.get("disabled_ranges") or [])
    cfg["disabled_ranges"] = ranges
    save_config(cfg)

    embed = request.args.get("embed") == "1"
    if embed:
        return render_template_string(CALENDAR_EMBED, ranges=ranges)

    return render_template_string(
        """<!doctype html><html lang="pl"><head><meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width,initial-scale=1"/>
        <title>Kalendarz wyłączeń</title>{{ base_style|safe }}</head><body>
        <div class="row" style="justify-content:space-between;align-items:center">
          <h1 style="margin:0">Kalendarz wyłączeń</h1>
          <a href="/" class="pill" style="text-decoration:none">← Powrót</a>
        </div>
        {{ embed|safe }}
        </body></html>""",
        base_style=BASE_STYLE,
        embed=render_template_string(CALENDAR_EMBED, ranges=ranges),
    )


@app.post("/calendar/add")
def calendar_add():
    cfg = load_config()
    f = (request.form.get("from") or "").strip()
    t = (request.form.get("to") or "").strip()
    label = (request.form.get("label") or "").strip()

    if len(f) != 10 or len(t) != 10 or f[4] != "-" or t[4] != "-":
        return jsonify(ok=False, error="Zły format daty. Użyj YYYY-MM-DD.")
    if f > t:
        return jsonify(ok=False, error="Data 'Od' nie może być po dacie 'Do'.")

    ranges = normalize_ranges(cfg.get("disabled_ranges") or [])
    ok, msg = validate_new_range(ranges, f, t)
    if not ok:
        return jsonify(ok=False, error=msg)

    ranges.append({"from": f, "to": t, "label": label})
    cfg["disabled_ranges"] = normalize_ranges(ranges)
    save_config(cfg)
    log_event("disabled range added")
    return jsonify(ok=True)


@app.post("/calendar/delete")
def calendar_delete():
    cfg = load_config()
    try:
        idx = int(request.form.get("idx", "-1"))
    except Exception:
        idx = -1

    ranges = normalize_ranges(cfg.get("disabled_ranges") or [])
    if 0 <= idx < len(ranges):
        ranges.pop(idx)
        cfg["disabled_ranges"] = normalize_ranges(ranges)
        save_config(cfg)
        log_event("disabled range deleted")
        return jsonify(ok=True)

    return jsonify(ok=False, error="Nie znaleziono zakresu.")


@app.get("/log/tail")
def log_tail_endpoint():
    cfg = load_config()
    rotate_log_if_needed(cfg)
    return Response(tail_log(cfg), mimetype="text/plain; charset=utf-8")


@app.get("/log/full")
def log_full():
    cfg = load_config()
    rotate_log_if_needed(cfg)
    embed = request.args.get("embed") == "1"
    tz = ZoneInfo(cfg.get("timezone", "Europe/Warsaw"))
    today = datetime.now(tz).date().isoformat()
    files = list_log_files()

    if embed:
        return render_template_string(FULL_LOG_EMBED, files=files, today=today)

    return render_template_string(
        """<!doctype html><html lang="pl"><head><meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width,initial-scale=1"/>
        <title>Pełny log</title>{{ base_style|safe }}</head><body>
        <div class="row" style="justify-content:space-between;align-items:center">
          <h1 style="margin:0">Pełny log</h1>
          <a href="/" class="pill" style="text-decoration:none">← Powrót</a>
        </div>
        {{ embed|safe }}
        </body></html>""",
        base_style=BASE_STYLE,
        embed=render_template_string(FULL_LOG_EMBED, files=files, today=today),
    )


@app.get("/log/read")
def log_read():
    name = request.args.get("name", "events.log")
    try:
        p = safe_log_path(name)
        txt = read_log_text(p)
        return Response(txt, mimetype="text/plain; charset=utf-8")
    except Exception as e:
        return Response(f"(błąd: {e})", mimetype="text/plain; charset=utf-8")


@app.get("/log/download")
def log_download():
    name = request.args.get("name", "events.log")
    try:
        p = safe_log_path(name)
        return send_file(p, as_attachment=True, download_name=os.path.basename(p))
    except Exception as e:
        abort(404, str(e))


@app.post("/log/clear_all")
def log_clear_all():
    try:
        truncate_file(LOG_PATH)
        _append_log_raw("LOG CLEARED (all logs)")
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.post("/log/delete_file")
def log_delete_file():
    name = request.form.get("name", "events.log")
    try:
        p = safe_log_path(name)
        if os.path.basename(p) == "events.log":
            truncate_file(p)
            log_event("LOG CLEARED (events.log)")
        else:
            os.remove(p)
            log_event(f"LOG FILE DELETED ({os.path.basename(p)})")
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


def start_bg():
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

    b = threading.Thread(target=button_loop, daemon=True)
    b.start()


start_bg()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080)
