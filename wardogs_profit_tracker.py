"""
Wardogs Profit Tracker
Reads WARDOGS profit / loss live from the Steam friends page (in a hidden,
signed-in browser) for you and any friends you pick, writes each amount to
text files for OBS (a separate file per person and/or a shared leaderboard),
and can colour each person's OBS Text source green / red through OBS WebSocket.
"""
import copy
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk
from tkinter import font as tkfont

APP_NAME = "Wardogs Profit Tracker"

# Folder the app lives in (next to the .exe when built, next to the .py otherwise)
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))

def _can_write(folder):
    """True if this app may create files in `folder` (Windows can block it:
    Program Files, or Defender's Controlled folder access on Documents/Desktop)."""
    test = os.path.join(folder, ".wardogs_write_test")
    try:
        os.makedirs(folder, exist_ok=True)
        with open(test, "w") as f:
            f.write("x")
        os.remove(test)
        return True
    except OSError:
        return False


def _in_temp(folder):
    """True for Windows' temp folder, e.g. when the exe is run straight from
    inside a zip: Windows unpacks it there and wipes it afterwards."""
    temp = os.path.normcase(os.path.abspath(os.environ.get("TEMP") or os.environ.get("TMP") or ""))
    here = os.path.normcase(os.path.abspath(folder))
    return bool(temp) and (here == temp or here.startswith(temp + os.sep))


def pick_data_dir(here):
    """Where settings, the Steam sign-in and the log are kept. Normally next to
    the exe; if that folder can't be written to (or is a temp folder that gets
    wiped), %LOCALAPPDATA%\\Wardogs Profit Tracker instead. Settings already
    next to the exe are copied over the first time. Returns (folder, reason,
    ok) — ok is False only if even that folder can't be written to."""
    if not _in_temp(here) and _can_write(here):
        return here, "", True
    reason = ("it's running from a temporary folder (e.g. straight from a zip)" if _in_temp(here)
              else "Windows won't let it save next to the exe")
    fallback = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME)
    try:
        os.makedirs(fallback, exist_ok=True)
        old, new = os.path.join(here, "settings.json"), os.path.join(fallback, "settings.json")
        if os.path.exists(old) and not os.path.exists(new):
            shutil.copyfile(old, new)
    except OSError:
        pass
    return fallback, reason, _can_write(fallback)


DATA_DIR, DATA_DIR_REASON, DATA_DIR_OK = pick_data_dir(HERE)
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
PROFILE_DIR = os.path.join(DATA_DIR, "steam_browser_profile")
LOG_FILE = os.path.join(DATA_DIR, "activity_log.txt")
LOG_FILE_MAX = 1_000_000  # bytes; trimmed to the newest half at startup
LOGIN_URL = "https://steamcommunity.com/login/home/"

# The Steam friends page gets your own status pushed live, within seconds
CHAT_URL = "https://steamcommunity.com/chat"
CHAT_POLL = 2          # seconds between reads of the (already open) page
CHAT_TIMEOUT = 10      # seconds before a read counts as failed
CHAT_RETRY = 60        # after the page fails to restart, try again this much later
MIN_RELOAD_MINUTES = 5  # the optional page refresh is never more often than this
OBS_RETRY = 15         # after OBS can't be reached, wait this long before trying again
REFRESH_CHOICES = (0, 15, 30, 60)  # Advanced › Refresh friends page: Off or every N minutes


def refresh_choice(on, minutes):
    """The Off / 15 / 30 / 60 button for a saved setting; an older saved time
    (any number of minutes) goes to the nearest one."""
    if not on:
        return 0
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = 30
    # on a tie take the longer wait: refreshing less is safer for rate limits
    return min(REFRESH_CHOICES[1:], key=lambda c: (abs(c - minutes), -c))


ME = "Me (signed-in account)"  # the person picker's entry for your own amount
# which name a person goes by in the files: their Steam name or your own label
# (whether it's shown is set per file: "Name" on a separate file, "Names" on
# the leaderboard)
NAME_MODES = {"steam": "Steam name", "custom": "Custom…"}
AVATAR_SIZE = 28       # profile pictures in the window, in pixels
# profile pictures are only ever downloaded from Steam's own servers
AVATAR_HOSTS = ("steamstatic.com", "akamaihd.net", "steamcommunity.com", "steampowered.com")

# Runs in the friends page, for everyone being tracked at once. `me` is the box
# with your own name and status; each friend in the list is a .friend whose
# first text is their name, followed by e.g. "WARDOGS" / "+$32,622 Profit".
# `whos` is a list: "" for you, else a friend's name. For each it returns
# {status, name}, with status null when that friend isn't listed.
#
# Friends are read with textContent, not innerText: innerText is empty for
# friends that aren't drawn (collapsed groups, scrolled out of view, the
# hidden browser), which is most of the list.
#
# Most of the friends list uses scrambled class names that change with Steam
# updates, so entries are read by order instead: the first piece of text is
# the name, the rest is the status. One-letter bits are dropped: the "*"
# nickname mark and the "z z z" away marker.
FRIENDS_JS = r"""(me, whos) => {
    const texts = e => {
        const out = [];
        const walk = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
        for (let t = walk.nextNode(); t; t = walk.nextNode()) {
            const s = t.textContent.trim();
            if (s) out.push(s);
        }
        return out;
    };
    const avatarOf = e => {
        const img = e.querySelector('img.avatar, img');
        const src = img ? (img.currentSrc || img.src || '') : '';
        return src.startsWith('https://') ? src : '';
    };
    // read each friend once: name, status (e.g. "WARDOGS +$32,622 Profit")
    const list = [...document.querySelectorAll('.friend')].map(e => {
        const t = texts(e);
        return {e, name: t[0] || '', key: (t[0] || '').toLowerCase(),
                status: t.slice(1).filter(s => s.length > 1).join(' ')};
    });
    // everyone on the list, for the picker: name -> profile picture
    const seen = new Map();
    for (const f of list) {
        if (f.name && !seen.get(f.name)) seen.set(f.name, avatarOf(f.e));
    }
    const friends = [...seen].sort((a, b) => a[0].localeCompare(b[0]));
    const mine = me.innerText.split('\n').map(x => x.trim()).filter(Boolean);
    const people = whos.map(who => {
        if (!who) return {status: mine.slice(1).join(' '), name: mine[0] || 'Me', avatar: avatarOf(me)};
        const want = who.trim().toLowerCase();
        const hits = list.filter(f => f.key === want);
        // the same friend can be listed twice (favorites + group): prefer one with an amount
        const best = hits.find(f => f.status.includes('$')) || hits[0];
        return best ? {status: best.status, name: best.name, avatar: avatarOf(best.e)}
                    : {status: null, name: who.trim(), avatar: ''};
    });
    return {people, friends, me: {name: mine[0] || '', avatar: avatarOf(me)}};
}"""

DEFAULTS = {
    "reload_on": False,   # refresh the friends page now and then (off: risk of rate limits)
    "reload_minutes": 30,
    # who to track, one entry each: "who" is "" for you, else a friend's name;
    # "name_mode" is which name they go by (see NAME_MODES), "label" the
    # custom one; "own_file" + "out_file" give them a separate file, and
    # "file_name" puts their name in it; "board" puts them on the leaderboard;
    # "obs_source" is the OBS Text source coloured by their amount (OBS color on)
    "people": [{"who": "", "name_mode": "steam", "label": "", "own_file": True,
                "out_file": "", "file_name": False, "board": False,
                "obs_source": "", "overlay": False}],
    # the leaderboard: one file with a line per person ticked "Leaderboard"
    "board_on": False,
    "board_file": "",
    "board_order": "amount",  # "amount" (highest first) or "list" (as listed)
    "board_ranks": True,      # "1. ", "2. " ... in front of each line
    "board_names": True,      # "Name: " in front of each amount
    "space": False,           # "+$3,740" (True: "+ $3,740")
    "tray": True,             # minimising hides the window to a tray icon
    "overlay_port": 8765,     # browser overlays ask 127.0.0.1:<port>/value?person=N
    "board_overlay": False,   # the leaderboard browser overlay (leaderboard.html)
    "board_top": 0,           # show only the top N people (0: everyone)
    "board_top_n": 5,         # the N last picked, kept while All is chosen
    "board_first": "crown",   # the overlay's 1st place: "crown", "number" or "image"
    "board_icon": "",         # the picture for "image" (any png / jpg / gif / webp / svg)
    # Advanced › Browser overlays: the look of both overlay pages
    "overlay_font": "Barlow",   # the overlays' font: one of WEB_FONTS, or any font installed here
    "overlay_size": 18,       # digit height in px (the game's HUD is 18)
    "overlay_color": "#C5C2BF",
    "overlay_pl": False,      # colour the money HUD by profit / loss
    "overlay_pl_board": False,  # colour the leaderboard's amounts by profit / loss
    "overlay_profit_color": "#5FE39B",
    "overlay_loss_color": "#FF8A7A",
    "overlay_bg_mode": "color",       # the box behind the text: "color" or "image"
    "overlay_bg_color": "#000000",
    "overlay_bg_opacity": 45,         # percent (0 = no box, 100 = solid)
    "overlay_bg_image": "",
    "overlay_hud_w": 0,               # money HUD box width / height in px (0: fit the number)
    "overlay_hud_h": 0,
    "overlay_board_w": 0,             # leaderboard panel width (0: fit the names)
    "obs_fit": False,                 # size the OBS Browser sources to fit (over OBS WebSocket)
    "adv_open": False,                # Advanced left open last time
    "keep_last": True,
    "color_on": True,         # colour by profit / loss: the app and each person's OBS source
    "profit_color": "#5FE39B",
    "loss_color": "#FF8A7A",
    "obs_connect": True,      # Connect / Disconnect: use OBS while colouring is on
    "obs_conn_open": True,  # OBS card's Connection fold-out (host/port/password) open
    "obs_host": "localhost",
    "obs_port": 4455,
    "obs_password": "",
}

# ---------------- colors / theme ----------------
BG = "#12161D"
BG_DARK = "#0B0E13"
BG_BAR = "#0E1117"
CARD = "#161B23"
BORDER = "#2C3340"
TEXT = "#E6E9EF"
MUTED = "#9AA3B2"
LABEL = "#B8BFCC"
ACCENT = "#4C8DFF"
STOP = "#C2410C"
START = "#2563EB"


# ---------------- password protection (Windows DPAPI) ----------------
# The OBS password is encrypted with your Windows account before it's saved.
# Only you, logged in to this PC, can decrypt it; copying settings.json to
# another PC or account gives an unreadable blob.

if os.name == "nt":
    import base64
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _UI_FORBIDDEN = 0x01

    def _to_blob(data):
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _from_blob(blob):
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))

    def protect(text):
        if not text:
            return ""
        src, _keep = _to_blob(text.encode("utf-8"))
        out = _Blob()
        if not _crypt32.CryptProtectData(ctypes.byref(src), APP_NAME, None, None, None,
                                         _UI_FORBIDDEN, ctypes.byref(out)):
            return ""
        return base64.b64encode(_from_blob(out)).decode("ascii")

    def unprotect(token):
        if not token:
            return ""
        try:
            src, _keep = _to_blob(base64.b64decode(token))
        except Exception:
            return ""
        out = _Blob()
        if not _crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None,
                                           _UI_FORBIDDEN, ctypes.byref(out)):
            return ""  # different PC/account, or damaged: just ask again
        return _from_blob(out).decode("utf-8", errors="replace")
else:
    # not Windows: never write the password to disk at all
    def protect(text):
        return ""

    def unprotect(token):
        return ""


# ---------------- settings ----------------

def clean_people(people):
    """A tidy list of people from saved settings; always at least one (you)."""
    out = []
    for p in people if isinstance(people, list) else []:
        if isinstance(p, dict):
            mode = p.get("name_mode")
            # older settings: a name was shown when mode was steam/custom (or
            # the even older show_name tick); "off" now means Steam name, hidden
            named = mode in NAME_MODES if "name_mode" in p else bool(p.get("show_name"))
            if mode not in NAME_MODES:
                mode = "steam"
            out.append({"who": str(p.get("who", "")).strip(),
                        "name_mode": mode,
                        "label": str(p.get("label", "")),
                        "own_file": bool(p.get("own_file", False)),
                        "out_file": str(p.get("out_file", "")).strip(),
                        "file_name": bool(p.get("file_name", named)),
                        "board": bool(p.get("board", False)),
                        "obs_source": str(p.get("obs_source", "")).strip(),
                        "overlay": bool(p.get("overlay", False)),
                        "_named": named})  # only used to carry old settings over
    return out or [dict(new_person(), own_file=True, board=False)]


def new_person(who=""):
    """A person added with + Add person: on the leaderboard, no file of their own."""
    return {"who": who, "name_mode": "steam", "label": "", "own_file": False,
            "out_file": "", "file_name": False, "board": True, "obs_source": "",
            "overlay": False}


def migrate_files(saved, s):
    """Settings from before separate files + leaderboard: keep writing exactly
    what was written before. One person -> their separate file; several in one
    file -> that file becomes the leaderboard (list order, no numbers); a file
    per person -> each person's separate file."""
    people = s["people"]
    old_file = str(saved.get("out_file", "")).strip()
    if saved.get("file_per_person") and len(people) > 1:
        for p in people:
            p["own_file"], p["board"] = bool(p["out_file"]), False
    elif len(people) == 1:
        people[0].update(own_file=True, out_file=old_file, board=False)
    else:
        for p in people:
            p["own_file"], p["board"] = False, True
        s.update(board_on=True, board_file=old_file, board_order="list", board_ranks=False,
                 board_names=any(p.get("_named") for p in people))


def amount_value(text):
    """'+ $3,740' -> 3740, '- $6,575' -> -6575 (for sorting the leaderboard)."""
    digits = re.sub(r"[^\d.]", "", text)
    try:
        v = float(digits) if digits else 0.0
    except ValueError:
        v = 0.0
    return -v if text.strip().startswith("-") else v


def leaderboard_rows(people, results, cfg):
    """Everyone ticked "Leaderboard" who has an amount right now, highest
    first (or as listed)."""
    rows = [r for p, r in zip(people, results) if p["board"] and r["text"]]
    if cfg.get("board_order", "amount") == "amount":
        rows.sort(key=lambda r: amount_value(r["text"]), reverse=True)  # stable on ties
    top = cfg.get("board_top", 0)
    return rows[:top] if isinstance(top, int) and top > 0 else rows


def leaderboard_lines(people, results, cfg):
    """The leaderboard file's lines, optionally numbered and named."""
    rows = leaderboard_rows(people, results, cfg)
    ranks, names = cfg.get("board_ranks", True), cfg.get("board_names", True)
    out = []
    for i, r in enumerate(rows, 1):
        line = f"{r['label']}: {r['text']}" if names else r["text"]
        out.append(f"{i}. {line}" if ranks else line)
    return out


def target_files(cfg):
    """Every file the tracker writes to with these settings."""
    paths = [p["out_file"] for p in cfg["people"] if p["own_file"] and p["out_file"]]
    if cfg.get("board_on") and cfg.get("board_file"):
        paths.append(cfg["board_file"])
    return paths


HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")


def fix_types(s):
    """A hand-edited or damaged settings file can hold the wrong kind of value
    (a null file path, a word for a number, "#GGG" for a colour): put each back
    to its default rather than crash the window or OBS colouring later."""
    for key, default in DEFAULTS.items():
        if key == "people":
            continue
        value = s.get(key, default)
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            ok = isinstance(value, int) and not isinstance(value, bool) and value >= (default > 0)
        else:
            ok = isinstance(value, type(default))
        if key.endswith("_color"):
            ok = ok and bool(HEX_COLOR.fullmatch(value))
        if not ok:
            s[key] = copy.deepcopy(default)


def load_settings():
    s = copy.deepcopy(DEFAULTS)
    saved = {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            raise ValueError("not a settings file")
    except FileNotFoundError:
        saved = {}
    except Exception:
        # unreadable (e.g. half-written by an older version during a crash):
        # keep a copy instead of silently overwriting it with defaults
        saved = {}
        try:
            shutil.copyfile(SETTINGS_FILE, SETTINGS_FILE + ".broken")
        except OSError:
            pass
    try:
        saved = dict(saved)
        enc = saved.pop("obs_password_enc", "")
        # older versions stored it in plain text: read it once, it gets
        # re-saved encrypted below
        plain = saved.pop("obs_password", "")
        s.update(saved)
        s["obs_password"] = unprotect(enc) if enc else plain
    except Exception:
        plain = ""
    s["people"] = clean_people(s.get("people"))
    saved_people = saved.get("people") if isinstance(saved, dict) else None
    if isinstance(saved, dict) and "obs_source" in saved and not any(
            isinstance(p, dict) and "obs_source" in p for p in saved_people or []):
        # older: one OBS source that followed the first person -> theirs now
        s["people"][0]["obs_source"] = str(saved["obs_source"]).strip()
    if isinstance(saved, dict) and saved and "board_on" not in saved:
        migrate_files(saved, s)  # settings from an older version
    if isinstance(saved, dict) and "overlay_pl" in saved and "overlay_pl_board" not in saved:
        s["overlay_pl_board"] = bool(saved["overlay_pl"])  # was one switch for both overlays
    if isinstance(saved, dict) and "obs_on" in saved:
        # older: a separate "OBS color" switch. Now one colour switch does both,
        # and Connect / Disconnect decides whether OBS is used
        s["color_on"] = bool(saved.get("color_on", True)) or bool(saved["obs_on"])
        s["obs_connect"] = bool(saved["obs_on"])
    # drop settings this version doesn't use (old URL, interval, single file...)
    for key in list(s):
        if key not in DEFAULTS and key != "obs_password":
            del s[key]
    for p in s["people"]:
        p.pop("_named", None)
    if s.get("board_order") not in ("amount", "list"):
        s["board_order"] = "amount"
    for key, hi in (("overlay_bg_opacity", 100), ("overlay_hud_w", BOX_MAX), ("overlay_hud_h", BOX_MAX),
                    ("overlay_board_w", BOX_MAX)):
        v = s.get(key)
        s[key] = max(0, min(v, hi)) if isinstance(v, int) and not isinstance(v, bool) else DEFAULTS[key]
    if s.get("overlay_bg_mode") not in ("color", "image"):
        s["overlay_bg_mode"] = "color"
    size = s.get("overlay_size")
    s["overlay_size"] = max(OVERLAY_SIZE_MIN, min(size, OVERLAY_SIZE_MAX)) if isinstance(size, int) else 18
    if s.get("board_first") not in ("crown", "number", "image"):
        s["board_first"] = "crown"
    fix_types(s)
    if s.get("reload_on"):
        # the refresh is Off / 15 / 30 / 60 min now; an older saved time goes to the nearest
        s["reload_minutes"] = refresh_choice(True, s.get("reload_minutes"))
    if plain:
        save_settings(s)  # older versions kept the password in plain text: encrypt it now
    return s


# the window sets "report" so a failed save is said once in the log, not hidden
SAVE_PROBLEM = {"report": None, "warned": False}


def save_settings(s):
    data = dict(s)
    data["obs_password_enc"] = protect(data.pop("obs_password", ""))
    try:
        # write a temp file and swap it in, so a crash mid-save can't leave a
        # half-written settings.json behind
        write_atomic(SETTINGS_FILE, json.dumps(data, indent=2))
        SAVE_PROBLEM["warned"] = False
    except Exception as e:
        if SAVE_PROBLEM["report"] and not SAVE_PROBLEM["warned"]:
            SAVE_PROBLEM["warned"] = True
            SAVE_PROBLEM["report"](f"Couldn't save your settings to {SETTINGS_FILE} ({e}) — "
                                   "they'll be lost when the app closes. Move the exe to a "
                                   "normal folder (not Program Files or inside a zip).")


# ---------------- money ----------------

# sign only counts when it touches the $ (so "Range - $3,740" isn't read as minus)
MONEY = re.compile(r"([+\-−]?)\$\s?(\d[\d,]*(?:\.\d+)?)")


def extract_money(text, space=True):
    """'+ $3,740' / '- $3,740'. A +/- on the amount wins, else Profit/Loss word."""
    m = MONEY.search(text)
    if not m:
        return ""
    symbol, number = m.group(1), m.group(2)
    if symbol == "+":
        sign = "+"
    elif symbol in ("-", "−"):
        sign = "-"
    elif re.search(r"\bloss\b", text, re.IGNORECASE):
        sign = "-"
    else:
        sign = "+"
    return f"{sign}{' ' if space else ''}${number}"


def person_label(person, steam_name):
    """The name this person goes by in the files: your custom label, or their
    Steam name (also when Custom is picked but left empty)."""
    if person.get("name_mode") == "custom" and person.get("label", "").strip():
        return person["label"].strip()
    return steam_name


def file_line(text, label, with_name):
    """A separate file's text: the amount, with 'Name: ' in front if wanted.
    Blank stays blank."""
    return f"{label}: {text}" if text and with_name else text


def fetch_avatar(url, size):
    """Downloads a Steam profile picture and returns it as a small square
    image, or None (not a Steam picture, offline, or Pillow missing)."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if not url.startswith("https://") or not any(host == h or host.endswith("." + h)
                                                 for h in AVATAR_HOSTS):
        return None
    try:
        import io
        import urllib.request
        from PIL import Image
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = r.read(2_000_000)
        return Image.open(io.BytesIO(data)).convert("RGB").resize((size, size), Image.LANCZOS)
    except Exception:
        return None


def tray_image(size=64):
    """The tray icon: a dark rounded square with a green $ (Pillow)."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=(18, 22, 29, 255),
                        outline=(76, 141, 255, 255), width=max(2, size // 22))
    try:
        font = ImageFont.truetype("segoeuib.ttf", int(size * 0.72))
    except OSError:
        font = ImageFont.load_default()
    d.text((size / 2, size / 2), "$", fill=(95, 227, 155, 255), font=font, anchor="mm")
    return img


# ---------------- browser overlay feed ----------------
# The money HUD page (overlay/money_hud.html) is an OBS Browser Source that asks
# this app for a person's number ~10 times a second. Only this PC can reach it
# (127.0.0.1), and it only ever answers the amounts already shown in the app.

HUD_NAME = "money_hud.html"
BOARD_NAME = "leaderboard.html"


def bundled_hud(name=HUD_NAME):
    """An overlay page shipped with the app (inside the exe, or next to the .py)."""
    base = getattr(sys, "_MEIPASS", HERE)
    return os.path.join(base, "overlay", name)


PAGE_VERSION = re.compile(rb"wardogs-page-version:\s*(\d+)")


def page_version(path):
    """The version number in an overlay page (pages from before 1.4 have none: 1)."""
    try:
        with open(path, "rb") as f:
            m = PAGE_VERSION.search(f.read(4096))
        return int(m.group(1)) if m else 1
    except OSError:
        return 0


def install_hud(folder, name=HUD_NAME):
    """Copy an overlay page into the save folder, so OBS can open it from a
    fixed place. Your copy (and any CSS tweaks in it) is kept until the app
    ships a newer version of the page: then the old copy is saved next to it
    as <name>.old.html and replaced."""
    dst = os.path.join(folder, "overlay", name)
    try:
        src = bundled_hud(name)
        if not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
        elif page_version(dst) < page_version(src):
            shutil.copyfile(dst, dst[:-len(".html")] + ".old.html")
            shutil.copyfile(src, dst)
    except OSError:
        pass
    return dst


BOARD_TOP_MAX = 30
OVERLAY_SIZE_MIN, OVERLAY_SIZE_MAX = 10, 72


BOX_MAX = 3840
# fonts the overlays load from Google Fonts (free; OBS needs internet when it
# starts the page). Any font installed on this PC works too.
WEB_FONTS = ("Barlow", "Barlow Condensed", "Barlow Semi Condensed", "Rajdhani", "Oswald", "Teko",
             "Saira", "Saira Condensed", "Chakra Petch", "Exo 2", "Orbitron", "Russo One",
             "Black Ops One", "Bebas Neue", "Anton", "Roboto", "Roboto Condensed", "Roboto Mono",
             "Share Tech Mono", "Inter")
FONT_SEPARATOR = "──── installed on this PC ────"


def overlay_style(cfg, page="hud"):
    """The look the overlay pages apply (sent with every answer, so a change
    shows within a moment)."""
    image = cfg["overlay_bg_mode"] == "image" and icon_version(cfg["overlay_bg_image"]) or ""
    pl = cfg["overlay_pl_board"] if page == "board" else cfg["overlay_pl"]
    font = cfg["overlay_font"].strip() or "Barlow"
    return {"font": font, "webFont": font in WEB_FONTS,
            "size": cfg["overlay_size"], "color": cfg["overlay_color"], "pl": bool(pl),
            "profit": cfg["overlay_profit_color"], "loss": cfg["overlay_loss_color"],
            "bg": cfg["overlay_bg_color"], "opacity": cfg["overlay_bg_opacity"], "image": image,
            "hudW": cfg["overlay_hud_w"], "hudH": cfg["overlay_hud_h"], "boardW": cfg["overlay_board_w"]}


def board_count(cfg):
    """How many rows the leaderboard can show at most (for sizing its source)."""
    n = sum(1 for p in cfg["people"] if p.get("board"))
    top = cfg.get("board_top", 0)
    return max(1, min(n, top) if top else n)


def obs_fit_sizes(cfg):
    """OBS Browser source sizes that fit each overlay: {page name: (w, h)}.
    Generous on width (the pages are anchored top-right, so spare room is
    simply transparent)."""
    import math
    k = cfg["overlay_size"] / 18
    hud = (cfg["overlay_hud_w"] or math.ceil(320 * k), cfg["overlay_hud_h"] or math.ceil(39 * k))
    # panel: padding 10 + 8, heading 15 + 6, 1st row 42, then 32 + 1 divider per row
    rows = board_count(cfg)
    board = (cfg["overlay_board_w"] or math.ceil(460 * k),
             math.ceil((10 + 8 + 15 + 6 + 42 + 33 * (rows - 1)) * k) + 4)
    return {HUD_NAME: hud, BOARD_NAME: board}


def obs_browser_sources(client):
    """[(source name, page)] for every OBS Browser source showing one of our pages."""
    out = []
    for item in client.get_input_list("browser_source").inputs:
        name = item.get("inputName", "")
        try:
            st = client.get_input_settings(name).input_settings
        except Exception as e:
            if obs_request_error(e):
                continue
            raise
        where = (st.get("url", "") if not st.get("is_local_file") else st.get("local_file", "")) or ""
        where = where.split("?")[0].replace("%20", " ")
        for page in (HUD_NAME, BOARD_NAME):
            if where.endswith(page):
                out.append((name, page))
    return out
ICON_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
              ".webp": "image/webp", ".svg": "image/svg+xml"}


def icon_version(path):
    """Changes whenever the picture does, so the overlay loads the new one."""
    try:
        st = os.stat(path)
        return f"{int(st.st_mtime)}-{st.st_size}"
    except (OSError, TypeError, ValueError):
        return ""


def overlay_link(path, person=None):
    """The URL to paste into an OBS Browser Source (Local file unticked)."""
    from urllib.parse import quote
    url = "file:///" + quote(path.replace("\\", "/"), safe="/:")
    return url + (f"?person={person}" if person is not None else "")


LOCAL_ORIGIN = re.compile(r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")


class OverlayServer:
    """Answers GET /value?person=N with {"text": "+$3,740"} from texts() (a
    list, one per person, kept current by the tracker), and serves the HUD
    page at /money_hud.html. 127.0.0.1 only."""

    def __init__(self, port, texts, board=lambda: {"rows": []}, files=lambda: {}, style=lambda page: None):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlparse

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                # the overlay pages are opened from a file (Origin "null"); websites
                # you visit may not read your amounts or pictures from here
                origin = self.headers.get("Origin", "")
                if origin == "null" or LOCAL_ORIGIN.match(origin):
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Access-Control-Allow-Private-Network", "true")
                    self.send_header("Vary", "Origin")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self._send(204, b"", "text/plain")

            def do_GET(self):
                url = urlparse(self.path)
                if url.path == "/value":
                    try:
                        n = int(parse_qs(url.query).get("person", ["1"])[0])
                    except ValueError:
                        n = 1
                    t = texts()
                    text = t[n - 1] if 1 <= n <= len(t) else ""
                    self._send(200, json.dumps({"text": text, "style": style("hud")}).encode(), "application/json")
                elif url.path == "/board":
                    self._send(200, json.dumps(dict(board(), style=style("board"))).encode(), "application/json")
                elif url.path in ("/icon", "/bg"):
                    # your pictures (1st place, background), from wherever they are on disk
                    path = files().get(url.path[1:], "")
                    ctype = ICON_TYPES.get(os.path.splitext(path)[1].lower())
                    try:
                        if not ctype or os.path.getsize(path) > 8 * 1024 * 1024:
                            raise OSError
                        with open(path, "rb") as f:
                            self._send(200, f.read(), ctype)
                    except OSError:
                        self._send(404, b"not found", "text/plain")
                elif url.path in ("/", "/" + HUD_NAME, "/" + BOARD_NAME):
                    try:
                        page = BOARD_NAME if url.path == "/" + BOARD_NAME else HUD_NAME
                        with open(bundled_hud(page), "rb") as f:
                            self._send(200, f.read(), "text/html; charset=utf-8")
                    except OSError:
                        self._send(404, b"not found", "text/plain")
                else:
                    self._send(404, b"not found", "text/plain")

            def log_message(self, *args):
                pass  # quiet: it's asked 10 times a second

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


_write_lock = threading.Lock()  # the tracker and the window both write files


def write_atomic(path, text):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with _write_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        # Windows refuses the replace while OBS has the file open for a moment
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05)


# ---------------- OBS ----------------

def hex_to_obs(hex_color):
    """'#RRGGBB' -> OBS color number (0xAABBGGRR)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (0xFF << 24) | (b << 16) | (g << 8) | r


def obs_client(cfg):
    import obsws_python as obs
    return obs.ReqClient(host=cfg["obs_host"], port=int(cfg["obs_port"]),
                         password=cfg["obs_password"], timeout=3)


def obs_set_color(client, source, hex_color):
    v = hex_to_obs(hex_color)
    # color = Text (GDI+) on Windows; color1/color2 = Text (FreeType 2)
    client.set_input_settings(source, {"color": v, "color1": v, "color2": v}, True)


def obs_request_error(e):
    """True when OBS answered but refused this one request (e.g. no source by
    that name): the connection itself is fine."""
    return type(e).__name__ == "OBSSDKRequestError"


def obs_colors(people, results, cfg):
    """{OBS source: colour} for everyone with a separate file and an OBS
    source set, from their own amount (+ profit colour, - loss colour)."""
    out = {}
    for p, r in zip(people, results):
        src = p.get("obs_source", "").strip()
        if p.get("own_file") and src and r["text"]:
            out[src] = cfg["loss_color"] if r["text"].startswith("-") else cfg["profit_color"]
    return out


# ---------------- browser worker ----------------

# Browsers built on Chromium can be driven directly; others (Firefox) can't.
CHROMIUM_EXES = {"chrome.exe", "msedge.exe", "brave.exe", "vivaldi.exe",
                 "chromium.exe", "opera.exe", "launcher.exe", "browser.exe"}


def default_browser():
    """Returns (name, exe_path) of the Windows default browser, or (None, None)."""
    if os.name != "nt":
        return None, None
    try:
        import winreg
        key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            prog_id = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog_id + r"\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
        m = re.match(r'\s*"([^"]+)"', cmd) or re.match(r"\s*(\S+\.exe)", cmd, re.IGNORECASE)
        exe = os.path.expandvars(m.group(1)) if m else None
        return prog_id, exe
    except Exception:
        return None, None


def launch_browser(pw, headless, log=None):
    """Uses your default browser if it's Chromium-based (Chrome, Edge, Brave,
    Opera, Vivaldi...). Firefox can't be controlled this way, so it falls
    back to Edge, then Chrome."""
    attempts = []
    prog_id, exe = default_browser()
    if exe and os.path.basename(exe).lower() in CHROMIUM_EXES and os.path.exists(exe):
        attempts.append((f"default browser ({os.path.basename(exe)})", {"executable_path": exe}))
    elif prog_id and log:
        log(f"Default browser ({prog_id}) isn't Chromium-based — using Edge/Chrome instead.")
    attempts += [("Edge", {"channel": "msedge"}), ("Chrome", {"channel": "chrome"})]
    last_err = None
    for name, extra in attempts:
        try:
            ctx = pw.chromium.launch_persistent_context(PROFILE_DIR, headless=headless, **extra)
            if log:
                log(f"Using {name}.")
            return ctx
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Couldn't start a browser: {last_err}")


def signed_in(ctx):
    return any(c["name"] == "steamLoginSecure" for c in ctx.cookies("https://steamcommunity.com"))


class Worker(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.stop_event = threading.Event()
        self.force_login = False
        self.ctx = None  # open browser context, closed in cleanup()
        self.obs = None  # open OBS connection, closed in close_obs()
        self.page = None  # the live friends page, while it's working
        self.page_opened = 0

    def log(self, msg, color=None):
        self.app.events.put(("log", time.strftime("%H:%M:%S"), msg, color))

    def emit(self, *ev):
        self.app.events.put(ev)

    def sleep(self, seconds):
        self.stop_event.wait(seconds)

    def login(self, pw):
        self.emit("status", "Waiting for Steam sign-in…", "#FACC15")
        self.log("Opening your browser — sign in to Steam there.")
        ctx = self.ctx = launch_browser(pw, headless=False)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(LOGIN_URL)
            while not signed_in(ctx) and not self.stop_event.is_set():
                time.sleep(2)
            ok = signed_in(ctx)
        except Exception:
            # the sign-in window was closed before signing in
            self.log("Sign-in window closed — click Sign in again to retry.", "#FACC15")
            ok = False
        self.close_ctx()
        if ok:
            self.log("Signed in to Steam.")
        return ok

    def close_ctx(self):
        if self.ctx is not None:
            try:
                self.ctx.close()
            except Exception:
                pass
            self.ctx = None

    def close_obs(self):
        if self.obs is not None:
            try:
                self.obs.disconnect()
            except Exception:
                pass
            self.obs = None

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            self.log(f"Playwright missing: {e}", "#FF8A7A")
            self.emit("stopped", self)
            return
        try:
            with sync_playwright() as pw:
                try:
                    if self.sign_in(pw):
                        self.loop(pw)
                finally:
                    self.close_ctx()
        except Exception as e:
            self.log(f"Stopped: {e}", "#FF8A7A")
        finally:
            self.close_obs()
        self.emit("stopped", self)

    def sign_in(self, pw):
        """Leaves a headless, signed-in browser open in self.ctx; False if
        signing in didn't happen."""
        self.ctx = launch_browser(pw, headless=True, log=self.log)
        if self.force_login or not signed_in(self.ctx):
            self.close_ctx()
            if self.force_login:
                shutil.rmtree(PROFILE_DIR, ignore_errors=True)
            if not self.login(pw):
                return False
            self.ctx = launch_browser(pw, headless=True)
        return True

    # ---- live friends page ----
    def open_chat(self, pw):
        """Opens the Steam friends page once. It keeps a live connection to
        Steam, so your status updates arrive within seconds, no reloading."""
        if self.ctx is None:
            self.ctx = launch_browser(pw, headless=True)
        page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
        page.locator(".currentUserContainer").wait_for(timeout=30000)
        page.wait_for_timeout(2000)  # the status line fills in just after
        self.page, self.page_opened = page, time.time()

    def read_people(self, whos):
        """Status lines on the friends page, e.g. 'WARDOGS -$6,575 Loss', for
        each of `whos` ("" = you, else a friend's name as the list shows it).
        Returns [{status, name, avatar}]; status is None if that friend isn't
        listed. Also keeps self.friends ([name, picture]) and self.me up to
        date for the picker. Playwright's timeout stops a frozen browser from
        stalling us."""
        res = self.page.locator(".currentUserContainer").evaluate(
            FRIENDS_JS, list(whos), timeout=CHAT_TIMEOUT * 1000)
        self.friends, self.me = res["friends"], res["me"]
        return res["people"]

    def restart_chat(self, pw):
        self.page = None
        self.close_ctx()
        self.open_chat(pw)

    def loop(self, pw):
        self.page = None
        chat_fails = 0
        chat_retry_at = 0      # while the friends page is down, when to try it again
        obs_conn = None        # (host, port, password) the OBS connection was made with
        obs_sent = {}          # OBS source -> colour last sent to it
        obs_bad = set()        # sources OBS couldn't colour (warned once)
        obs_warned = False
        obs_retry_at = 0       # after a failed connect, don't try OBS again before this
        written = {}           # path -> text last written there
        bad_paths = set()      # files that failed to write (warned once)
        state = []             # per person: who, last amount, last raw text, warned
        last_logged = 0
        fails = 0
        file_warned = False
        signed_out_warned = False
        self.friends, self.me = [], {}
        sent_friends = None
        self.log("Started.")
        while not self.stop_event.is_set():
            try:
                cfg = copy.deepcopy(self.app.cfg)  # live settings
            except RuntimeError:
                # the window changed them mid-copy (e.g. adding a person): try again
                self.sleep(0.2)
                continue
            people = cfg["people"] = clean_people(cfg.get("people"))
            # without a file it still reads the page (so you can pick people and
            # see amounts), it just has nowhere to write yet
            no_file = not target_files(cfg)
            if no_file and not file_warned:
                self.log("Choose your output text file to send amounts to OBS.", "#FACC15")
                file_warned = True
            elif not no_file:
                file_warned = False
            if self.page is None:
                if time.time() < chat_retry_at:
                    self.sleep(1)
                    continue
                try:
                    self.restart_chat(pw)
                    chat_fails = 0
                    self.log("Reading the live Steam friends page.", "#5FE39B")
                except Exception as e:
                    chat_retry_at = time.time() + CHAT_RETRY
                    self.log(f"Friends page didn't open ({e}) — retrying in {CHAT_RETRY} s.",
                             "#FF8A7A")
                    self.emit("status", "Friends page down — retrying", "#FF8A7A")
                    continue
            try:
                try:
                    reload_every = max(MIN_RELOAD_MINUTES, int(cfg["reload_minutes"])) * 60
                    if cfg["reload_on"] and time.time() - self.page_opened > reload_every:
                        # optional fresh page, in case its connection went stale
                        self.page.reload(wait_until="domcontentloaded", timeout=30000)
                        self.page.locator(".currentUserContainer").wait_for(timeout=30000)
                        self.page.wait_for_timeout(2000)
                        self.page_opened = time.time()
                    found = self.read_people([p["who"] for p in people])
                    landed_on_login = "/login" in self.page.url
                    chat_fails = 0
                    if (self.friends, self.me) != sent_friends:
                        self.emit("friends", [list(f) for f in self.friends], dict(self.me))
                        sent_friends = ([list(f) for f in self.friends], dict(self.me))
                except Exception:
                    chat_fails += 1
                    if chat_fails >= 3:
                        # watchdog: 3 failed reads in a row, so start the browser fresh
                        chat_fails = 0
                        self.page = None
                        self.log("Friends page stopped answering — restarting it.", "#FACC15")
                    raise

                # each person's amount
                state = state[:len(people)]
                results, missing = [], []
                heartbeat = time.time() - last_logged > 300
                for i, (p, f) in enumerate(zip(people, found)):
                    if i == len(state):
                        state.append(None)
                    if state[i] is None or state[i]["who"] != p["who"]:
                        if len(people) > 1 or i or state[i] is not None:
                            self.log(f"Tracking {p['who'] or 'your own account'}.", "#5FE39B")
                        # start fresh: don't carry one person's amount over to another
                        state[i] = {"who": p["who"], "last_text": "", "last_raw": None, "warned": False}
                    st = state[i]
                    raw = f["status"]
                    if raw is None:
                        raw = ""
                        missing.append(p["who"])
                        if not st["warned"]:
                            self.log(f'"{p["who"]}" isn\'t on your friends list right now — they '
                                     "may be offline, or the name is spelled differently.", "#FACC15")
                            st["warned"] = True
                    else:
                        st["warned"] = False
                    text = extract_money(raw, cfg["space"])
                    kept = False
                    if not text and cfg["keep_last"] and st["last_text"]:
                        # reuse the last amount, with the current space setting
                        sign, body = st["last_text"][0], st["last_text"][1:].lstrip()
                        text, kept = sign + (" " if cfg["space"] else "") + body, True
                    if text:
                        st["last_text"] = text
                    label = person_label(p, f["name"])
                    results.append({"text": text, "raw": raw, "kept": kept, "label": label,
                                    "line": file_line(text, label, p["file_name"]),
                                    "name": f["name"], "avatar": f.get("avatar", "")})
                    # log changes (and a heartbeat every 5 min), not every 2 s read
                    if raw != st["last_raw"] or heartbeat:
                        tag = f"[{f['name']}] " if len(people) > 1 else ""
                        note = "(kept last)" if kept else (text or "(blank)")
                        self.log(f'live  {tag}"{raw}" → {note}',
                                 self.app.color_for(text) if text and not kept
                                 and raw != st["last_raw"] else None)
                        st["last_raw"] = raw
                if heartbeat:
                    last_logged = time.time()

                # write: each person's separate file, plus the leaderboard
                board = leaderboard_lines(people, results, cfg)
                by_path = {}
                for p, r in zip(people, results):
                    if p["own_file"] and p["out_file"]:
                        # people sharing a file get a line each in it
                        by_path.setdefault(p["out_file"], []).append(r["line"])
                if cfg["board_on"] and cfg["board_file"]:
                    by_path.setdefault(cfg["board_file"], []).extend(board)
                outputs = {path: "\n".join(lines) for path, lines in by_path.items()}
                for path, content in outputs.items():
                    if written.get(path) == content:
                        continue
                    try:
                        write_atomic(path, content)
                        written[path] = content
                        bad_paths.discard(path)
                    except Exception as e:
                        # one bad file doesn't stop the others; say so once
                        if path not in bad_paths:
                            self.log(f"Couldn't write {path}: {e}", "#FF8A7A")
                            bad_paths.add(path)
                fails = 0
                first = results[0]
                self.emit("value", first["text"], first["raw"], first["kept"])
                self.emit("people", results, board if cfg["board_on"] else None)
                # for the browser overlays: only people with Overlay switched on
                show = cfg["board_on"] and cfg.get("board_overlay")
                self.app.overlay_board = {
                    "rows": [{"name": r["label"], "text": r["text"]}
                             for r in leaderboard_rows(people, results, cfg)] if show else [],
                    "ranks": bool(cfg["board_ranks"]), "names": bool(cfg["board_names"]),
                    "first": cfg["board_first"],
                    "icon": icon_version(cfg["board_icon"]) if cfg["board_first"] == "image" else ""}
                self.app.overlay_texts = [r["text"] if p.get("overlay") else ""
                                          for p, r in zip(people, results)]
                # OBS colour: each person's own Text source, from their own amount,
                # while colouring is on and you haven't pressed Disconnect
                use_obs = cfg["color_on"] and cfg["obs_connect"]
                if not use_obs and obs_conn is not None:
                    # colouring off or disconnected: let go of OBS, resend everything later
                    self.close_obs()
                    obs_conn, obs_sent = None, {}
                if use_obs:
                    conn = (cfg["obs_host"], cfg["obs_port"], cfg["obs_password"])
                    if conn != obs_conn:
                        # host/port/password changed: reconnect and resend straight away
                        self.close_obs()
                        obs_conn, obs_sent, obs_warned, obs_retry_at = conn, {}, False, 0
                    wanted = obs_colors(people, results, cfg)
                    if self.obs is None and time.time() < obs_retry_at:
                        wanted = {}  # OBS was unreachable a moment ago: try again shortly
                    for want_src, want in wanted.items():
                        if obs_sent.get(want_src) == want:
                            continue
                        try:
                            if self.obs is None:
                                self.obs = obs_client(cfg)
                            obs_set_color(self.obs, want_src, want)
                            obs_sent[want_src] = want
                            obs_bad.discard(want_src)
                            obs_warned = False
                            self.emit("obs", "connected")
                            self.log(f'OBS color "{want_src}" → {want}')
                        except Exception as e:
                            if obs_request_error(e):
                                # connected, but this source is the problem (e.g. no
                                # such name in OBS): say so once, keep going with others
                                if want_src not in obs_bad:
                                    self.log(f'OBS: couldn\'t colour "{want_src}" — {e}', "#FF8A7A")
                                    obs_bad.add(want_src)
                                continue
                            self.close_obs()
                            obs_sent = {}
                            obs_retry_at = time.time() + OBS_RETRY
                            self.emit("obs", "error")
                            if not obs_warned:
                                self.log(f"OBS: {e}", "#FF8A7A")
                                obs_warned = True
                            break

                # Steam drops the sign-in cookie (or redirects to login) when the
                # session ends; signed-out pages can show stale text
                if landed_on_login:
                    if not signed_out_warned:
                        self.log("Signed out of Steam — the amount may be stale. "
                                 "Click \"Sign in again\".", "#FACC15")
                        signed_out_warned = True
                    self.emit("status", "Signed out — click Sign in again", "#FACC15")
                elif no_file:
                    signed_out_warned = False
                    self.emit("status", "Live — choose your output text file", "#FACC15")
                elif bad_paths & set(outputs):
                    signed_out_warned = False
                    self.emit("status", "Can't write the output file — see log", "#FF8A7A")
                elif missing:
                    signed_out_warned = False
                    self.emit("status", f"{', '.join(missing)} not found on friends list", "#FACC15")
                else:
                    signed_out_warned = False
                    self.emit("status", "Signed in · live", "#3DDC84")
            except Exception as e:
                fails += 1
                self.log(f"error (attempt {fails}): {e}", "#FF8A7A")
                self.emit("status", "Error — retrying", "#FF8A7A")
            delay = CHAT_POLL if fails == 0 else 5
            self.sleep(delay)
        self.log("Stopped.")


# ---------------- GUI ----------------

class Chip(tk.Frame):
    """An on/off chip tied to a BooleanVar: outlined, with an icon and text
    that light up blue when on. Click (or Space/Enter when focused) toggles."""
    ON_BG, OFF_BG, OFF_LINE = "#16213A", BG_DARK, "#3A4352"

    def __init__(self, parent, icon, text, variable):
        super().__init__(parent, bg=self.OFF_LINE, cursor="hand2", takefocus=1)
        self.var = variable
        self.inner = tk.Frame(self, bg=self.OFF_BG)
        self.inner.pack(padx=1, pady=1)
        self.icon = tk.Label(self.inner, text=icon, font=("Segoe UI Symbol", 10), cursor="hand2")
        self.icon.pack(side="left", padx=(9, 3), pady=2)
        self.text = tk.Label(self.inner, text=text, font=("Segoe UI", 10), cursor="hand2")
        self.text.pack(side="left", padx=(0, 11), pady=2)
        for w in (self, self.inner, self.icon, self.text):
            w.bind("<Button-1>", self.toggle)
        self.bind("<space>", self.toggle)
        self.bind("<Return>", self.toggle)
        variable.trace_add("write", lambda *_: self.draw())
        self.draw()

    def toggle(self, _=None):
        self.var.set(not self.var.get())

    def draw(self):
        on = bool(self.var.get())
        bg = self.ON_BG if on else self.OFF_BG
        self.configure(bg=ACCENT if on else self.OFF_LINE)
        self.inner.configure(bg=bg)
        self.icon.configure(bg=bg, fg=ACCENT if on else MUTED)
        self.text.configure(bg=bg, fg=TEXT if on else MUTED)


class Switch(tk.Canvas):
    """A small on/off toggle switch tied to a BooleanVar: a smooth rounded
    track with a white knob that slides across and shows a tick (on) or a
    cross (off). Drawn with Pillow at 4x and scaled down, so the edges are
    clean (Tk's own circles are jagged)."""
    W, H = 46, 24
    SS = 4               # supersampling
    STEPS = 6            # animation frames for a slide
    FRAME_MS = 16
    _cache = {}

    def __init__(self, parent, variable, bg=BG, on_color=ACCENT, off_color="#3A4352"):
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=1)
        self.var = variable
        self.on_color, self.off_color = on_color, off_color
        self.pos = 1.0 if variable.get() else 0.0   # 0 = off (left), 1 = on (right)
        self._anim = None
        self._img = None
        self.bind("<Button-1>", self.toggle)
        self.bind("<space>", self.toggle)
        self.bind("<Return>", self.toggle)
        self.bind("<FocusIn>", lambda e: self.draw())
        self.bind("<FocusOut>", lambda e: self.draw())
        variable.trace_add("write", lambda *_: self._slide())
        self.draw()

    def toggle(self, _=None):
        self.var.set(not self.var.get())

    def _slide(self):
        target = 1.0 if self.var.get() else 0.0
        if self._anim is not None:
            self.after_cancel(self._anim)
            self._anim = None
        if not self.winfo_viewable():
            self.pos = target       # hidden: no point animating
            self.draw()
            return

        def step():
            d = 1.0 / self.STEPS
            self.pos = min(target, self.pos + d) if target > self.pos else max(target, self.pos - d)
            self.draw()
            self._anim = self.after(self.FRAME_MS, step) if self.pos != target else None
        step()

    @staticmethod
    def _mix(a, b, t):
        a, b = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
        return tuple(round(x + (y - x) * t) for x, y in zip(a, b))

    def _image(self, pos, focused):
        key = (round(pos, 3), self.on_color, self.off_color, focused)
        img = self._cache.get(key)
        if img is not None:
            return img
        from PIL import Image, ImageDraw
        k = self.SS
        w, h = self.W * k, self.H * k
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        track = self._mix(self.off_color, self.on_color, pos)
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=track)
        if focused:
            d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, outline=(127, 180, 255), width=k)
        pad, knob = 3 * k, h - 6 * k
        x = pad + (w - 2 * pad - knob) * pos
        d.ellipse((x, pad, x + knob, pad + knob), fill=(255, 255, 255))
        # tick when on, cross when off, in the track's colour
        cx, cy, r, lw = x + knob / 2, pad + knob / 2, knob * 0.22, round(1.8 * k)
        if pos >= 0.5:
            d.line([(cx - r, cy + r * 0.05), (cx - r * 0.25, cy + r * 0.8), (cx + r * 1.05, cy - r * 0.75)],
                   fill=track, width=lw, joint="curve")
        else:
            d.line([(cx - r * 0.8, cy - r * 0.8), (cx + r * 0.8, cy + r * 0.8)], fill=track, width=lw)
            d.line([(cx - r * 0.8, cy + r * 0.8), (cx + r * 0.8, cy - r * 0.8)], fill=track, width=lw)
        img = im.resize((self.W, self.H), Image.LANCZOS)
        self._cache[key] = img
        return img

    def draw(self):
        try:
            focused = self.focus_get() is self
        except (KeyError, tk.TclError):
            focused = False
        try:
            from PIL import ImageTk
            self._img = ImageTk.PhotoImage(self._image(self.pos, focused), master=self)
        except Exception:
            self._img = None
        self.delete("all")
        if self._img is not None:
            self.create_image(0, 0, image=self._img, anchor="nw")
            return
        # no Pillow: plain Tk drawing
        on = self.pos >= 0.5
        w, h, r = self.W, self.H, self.H / 2
        track = self.on_color if on else self.off_color
        self.create_oval(0, 0, h, h, fill=track, outline=track)
        self.create_oval(w - h, 0, w, h, fill=track, outline=track)
        self.create_rectangle(r, 0, w - r, h, fill=track, outline=track)
        x = w - h + 3 if on else 3
        self.create_oval(x, 3, x + h - 6, h - 3, fill="#FFFFFF", outline="#FFFFFF")
        if focused:
            self.create_rectangle(0, 0, w - 1, h - 1, outline="#7FB4FF")


class PersonPicker(tk.Toplevel):
    """A drop-down under a person's box: a search field and your friends list
    with profile pictures. Click a name or press Enter to pick; typing a name
    that isn't listed (a friend who's offline) and pressing Enter uses it."""
    HEIGHT = 300

    def __init__(self, app, anchor, on_pick):
        super().__init__(app)
        if app.picker is not None:
            app.picker.close()
        app.picker = self
        self.app, self.on_pick = app, on_pick
        self.overrideredirect(True)
        # a borderless window can open *behind* the main one on Windows, where
        # clicks land on the settings underneath: keep it on top while open
        self.attributes("-topmost", True)
        self.configure(bg=BORDER)
        x, y = anchor.winfo_rootx(), anchor.winfo_rooty() + anchor.winfo_height() + 2
        self.geometry(f"{max(anchor.winfo_width(), 280)}x{self.HEIGHT}+{x}+{y}")
        box = tk.Frame(self, bg=BG_DARK)
        box.pack(fill="both", expand=True, padx=1, pady=1)
        self.search = tk.StringVar()
        entry = ttk.Entry(box, textvariable=self.search)
        entry.pack(fill="x", padx=6, pady=6)
        self.canvas = tk.Canvas(box, bg=BG_DARK, highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(box, orient="vertical", command=self.canvas.yview,
                            style="Log.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y", padx=(0, 4), pady=(0, 6))
        self.canvas.pack(fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        self.list = tk.Frame(self.canvas, bg=BG_DARK)
        win = self.canvas.create_window((0, 0), window=self.list, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(win, width=e.width))
        self.list.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.search.trace_add("write", lambda *_: self.fill())
        entry.bind("<Return>", lambda e: self.pick_first())
        entry.bind("<Escape>", lambda e: self.close())
        self.bind("<FocusOut>", lambda e: self.after(150, self._close_if_unfocused))
        self.rows = []
        self.fill()
        self.lift()
        entry.focus_force()

    def _matches(self):
        q = self.search.get().strip().lower()
        everyone = [("", ME)] + [(n, n) for n, _ in self.app.friends]
        return [(who, label) for who, label in everyone if q in label.lower()]

    def fill(self):
        for w in self.list.winfo_children():
            w.destroy()
        self.rows = []
        matches = self._matches()
        typed = self.search.get().strip()
        if typed and not any(label.lower() == typed.lower() for _, label in matches):
            matches.append((typed, f'Use "{typed}"'))
        if not self.app.friends and not typed:
            running = self.app.worker is not None and self.app.worker.is_alive()
            tk.Label(self.list, text="Loading your friends list — it appears here in a few seconds."
                     if running else "Press Start to load your friends list.",
                     bg=BG_DARK, fg=MUTED, font=("Segoe UI", 8), wraplength=240,
                     justify="left").pack(anchor="w", padx=4, pady=(0, 4))
        for who, label in matches:
            row = tk.Label(self.list, text=f"  {label}", image=self.app.avatar_for(who),
                           compound="left", anchor="w", bg=BG_DARK, fg=TEXT,
                           font=("Segoe UI", 10), padx=4, pady=2, cursor="hand2")
            row.pack(fill="x")
            row.bind("<Enter>", lambda e, r=row: r.configure(bg="#1E2633"))
            row.bind("<Leave>", lambda e, r=row: r.configure(bg=BG_DARK))
            row.bind("<Button-1>", lambda e, w=who: self.pick(w))
            self.rows.append((who, row))
        self.canvas.yview_moveto(0)

    def repaint_avatars(self):
        for who, row in self.rows:
            row.configure(image=self.app.avatar_for(who))

    def on_wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def pick_first(self):
        if self.rows:
            self.pick(self.rows[0][0])

    def pick(self, who):
        self.on_pick(who)
        self.close()

    def _close_if_unfocused(self):
        try:
            f = self.focus_get()
        except Exception:
            f = None
        if f is None or f.winfo_toplevel() is not self:
            self.close()

    def close(self):
        if self.app.picker is self:
            self.app.picker = None
        try:
            self.destroy()
        except tk.TclError:
            pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.configure(bg=BG)
        self.geometry("980x820")
        self.minsize(820, 300)
        self.cfg = load_settings()
        self.events = queue.Queue()
        self.worker = None
        self.last_refresh = None
        self.current_text = ""
        self.current_people = []   # latest per-person results from the tracker
        self.current_board = None  # latest leaderboard lines (None when it's off)
        self.friends = []          # [name, picture url] from the friends list
        self.picker = None         # the open person search popup, if any
        self.avatar_urls = {}      # lower-case name ("" = you) -> picture url
        self.avatar_imgs = {}      # picture url -> image (None while downloading)
        self.avatar_queue = queue.Queue()
        self.blank_avatar = tk.PhotoImage(width=AVATAR_SIZE, height=AVATAR_SIZE)
        self.blank_avatar.put("#2A3140", to=(0, 0, AVATAR_SIZE, AVATAR_SIZE))
        threading.Thread(target=self._avatar_loader, daemon=True).start()
        self._style()
        self._build()
        self._load_into_widgets()
        SAVE_PROBLEM["report"] = lambda msg: self.events.put(
            ("log", time.strftime("%H:%M:%S"), msg, "#FF8A7A"))
        if DATA_DIR_REASON:
            # couldn't keep things next to the exe: say where they are instead
            self.events.put(("log", time.strftime("%H:%M:%S"),
                             f"Settings and Steam sign-in are saved in {DATA_DIR} — {DATA_DIR_REASON}.",
                             "#FACC15"))
        if not DATA_DIR_OK:
            save_settings(self.cfg)  # can't save anywhere: this reports it in the log
        self.after(150, self._pump)
        self.after(1000, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._close)
        # the search popup is pinned on top, so close it when the window moves,
        # resizes or is minimised rather than leave it floating on its own
        self.bind("<Configure>", self._close_picker_on_move, add="+")
        self.bind("<Unmap>", self._close_picker_on_move, add="+")
        # minimise to the tray (with "Minimize to tray" on)
        self.tray = None
        self.bind("<Unmap>", self._maybe_to_tray, add="+")
        self._show_signal = make_show_signal()
        # browser overlays: the HUD page in the save folder + the number feed
        self.overlay_texts = []
        self.overlay_board = {"rows": []}
        self.board_path = install_hud(DATA_DIR, BOARD_NAME)
        self.hud_path = install_hud(DATA_DIR)
        self.overlay = None
        self._update_overlay_server()
        self.start()

    # ---- look ----
    def _style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=TEXT, fieldbackground=BG_DARK,
                     bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                     font=("Segoe UI", 10))
        st.configure("TEntry", padding=6, foreground=TEXT, insertcolor=TEXT)
        st.configure("TCombobox", padding=6, foreground=TEXT, arrowcolor=TEXT,
                     background=BG_DARK, insertcolor=TEXT)
        st.map("TCombobox", fieldbackground=[("readonly", BG_DARK)],
               background=[("active", "#252D39")], arrowcolor=[("disabled", MUTED)])
        # the mouse wheel scrolls the settings; it shouldn't also flip a
        # dropdown when the pointer happens to be over one
        for ev in ("<MouseWheel>", "<Shift-MouseWheel>"):
            self.unbind_class("TCombobox", ev)
        # the drop-down list itself is a plain Tk listbox
        self.option_add("*TCombobox*Listbox.background", BG_DARK)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))
        st.configure("TCheckbutton", background=BG, foreground=LABEL)
        st.map("TCheckbutton", background=[("active", BG)])
        st.configure("TButton", background="#1C222C", foreground=TEXT, padding=(12, 8),
                     bordercolor=BORDER, focuscolor=ACCENT)
        st.map("TButton", background=[("active", "#252D39")])
        # slim dark scrollbars: no arrows, just a rounded-looking thumb
        thumb_layout = [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})]
        for name, trough in (("Log.Vertical.TScrollbar", BG_DARK), ("Page.Vertical.TScrollbar", BG)):
            st.layout(name, thumb_layout)
            st.configure(name, troughcolor=trough, background="#2A3140", bordercolor=trough,
                         lightcolor="#2A3140", darkcolor="#2A3140", gripcount=0,
                         relief="flat", borderwidth=0, width=8, arrowsize=8)
            st.map(name, background=[("pressed", "#4A5466"), ("active", "#3A4352")],
                   lightcolor=[("pressed", "#4A5466"), ("active", "#3A4352")],
                   darkcolor=[("pressed", "#4A5466"), ("active", "#3A4352")])
        st.configure("Run.TButton", background=STOP, foreground="white",
                     font=("Segoe UI", 10, "bold"), padding=(18, 8), bordercolor=STOP)
        st.map("Run.TButton", background=[("active", STOP)])

    def _label(self, parent, text, bg=BG, fg=LABEL, size=9, bold=False):
        return tk.Label(parent, text=text, bg=bg, fg=fg,
                        font=("Segoe UI", size, "bold" if bold else "normal"), anchor="w")

    def _section(self, parent, text, bg=BG):
        return tk.Label(parent, text=text.upper(), bg=bg, fg=MUTED,
                        font=("Segoe UI", 8, "bold"), anchor="w")

    # ---- layout ----
    def _build(self):
        # footer first, so it always keeps its space at the bottom
        foot = tk.Frame(self, bg=BG_BAR)
        foot.pack(fill="x", side="bottom")
        fi = tk.Frame(foot, bg=BG_BAR)
        fi.pack(fill="x", padx=20, pady=12)
        ttk.Button(fi, text="Sign in again", command=self._relogin).pack(side="left")
        self.run_btn = ttk.Button(fi, text="Stop", style="Run.TButton", command=self._toggle_run)
        self.run_btn.pack(side="right")
        ttk.Button(fi, text="Open txt", command=self._open_txt).pack(side="right", padx=(0, 8))
        ttk.Button(fi, text="Show log", command=self._open_log).pack(side="right", padx=(0, 8))

        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True)

        # side panel on the right: live output + activity
        side = tk.Frame(main, bg=BG, width=380)
        side.pack(side="right", fill="y", padx=(0, 20), pady=16)
        side.pack_propagate(False)

        # live output card (top of the side panel)
        live = tk.Frame(side, bg=BG_DARK, highlightbackground="#262C36", highlightthickness=1)
        live.pack(fill="x")
        self._section(live, "Live output", BG_DARK).pack(fill="x", padx=16, pady=(12, 0))
        st_row = tk.Frame(live, bg=BG_DARK)
        st_row.pack(fill="x", padx=16, pady=(4, 0))
        self.status_dot = tk.Label(st_row, text="●", bg=BG_DARK, fg="#FACC15", font=("Segoe UI", 9))
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_lbl = tk.Label(st_row, text="Starting…", bg=BG_DARK, fg=MUTED,
                                   font=("Segoe UI", 9), anchor="w")
        self.status_lbl.pack(side="left", fill="x")
        self.money_lbl = tk.Label(live, text="—", bg=BG_DARK, fg=TEXT,
                                  font=("Consolas", 40, "bold"), anchor="w")
        self.money_lbl.pack(fill="x", padx=14, pady=(6, 0))
        # what the files get: everyone's amount, and the leaderboard
        self.obs_line_lbl = tk.Label(live, text="", bg=BG_DARK, fg=TEXT, font=("Segoe UI", 10),
                                     anchor="w", justify="left", wraplength=340)
        self.raw_lbl = tk.Label(live, text='Steam says: ""', bg=BG_DARK, fg=MUTED,
                                font=("Consolas", 9), anchor="w", justify="left", wraplength=340)
        self.raw_lbl.pack(fill="x", padx=16, pady=(2, 12))

        self._section(side, "Activity").pack(anchor="w", pady=(14, 6))
        logf = tk.Frame(side, bg=BG_DARK, highlightbackground="#262C36", highlightthickness=1)
        logf.pack(fill="both", expand=True)
        log_bar = ttk.Scrollbar(logf, orient="vertical", style="Log.Vertical.TScrollbar")
        log_bar.pack(side="right", fill="y", padx=(0, 3), pady=4)
        self.log_txt = tk.Text(logf, bg=BG_DARK, fg=MUTED, font=("Consolas", 9), relief="flat",
                               wrap="word", padx=10, pady=8, state="disabled",
                               insertbackground=TEXT, yscrollcommand=log_bar.set)
        self.log_txt.pack(side="left", fill="both", expand=True)
        log_bar.configure(command=self.log_txt.yview)
        self.log_txt.tag_configure("time", foreground="#6F7887")
        tk.Frame(main, bg="#222833", width=1).pack(side="right", fill="y", pady=16, padx=(0, 20))

        # settings scroll when the window is small
        holder = tk.Frame(main, bg=BG)
        holder.pack(side="left", fill="both", expand=True)
        canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview,
                            style="Page.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        outer = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=outer, anchor="nw")

        def refit(_=None):
            canvas.itemconfigure(win, width=canvas.winfo_width())
            need = outer.winfo_reqheight()
            have = canvas.winfo_height()
            # stretch to fill when there's room, scroll when there isn't
            canvas.itemconfigure(win, height=max(need, have))
            canvas.configure(scrollregion=(0, 0, canvas.winfo_width(), max(need, have)))
            if need > have:
                if not bar.winfo_ismapped():
                    bar.pack(side="right", fill="y", padx=(0, 4), pady=6)
            elif bar.winfo_ismapped():
                bar.pack_forget()
                canvas.yview_moveto(0)

        canvas.bind("<Configure>", refit)
        outer.bind("<Configure>", refit)

        # the settings have a set height inside the scroll area, so when they
        # grow (Advanced opened, a person added, a file box shown) nothing tells
        # the scroll area: watch the height they need and refit when it changes
        last_need = [0]

        def check_height():
            need = outer.winfo_reqheight()
            if need != last_need[0]:
                last_need[0] = need
                refit()

        def watch_height():
            check_height()
            self.after(250, watch_height)
        self.after(250, watch_height)
        # called right after something opens, closes or is added, so the new
        # part gets room at once instead of up to a quarter-second later
        def refit_now():
            outer.update_idletasks()  # let Tk work out the new height first
            check_height()
        self._refit_soon = lambda: self.after_idle(refit_now)

        def wheel(e):
            # the activity log scrolls itself; the wheel elsewhere scrolls the settings
            if e.widget is self.log_txt:
                return
            try:
                top = e.widget.winfo_toplevel()
            except Exception:
                return
            if top is not self:
                # the person search popup scrolls its own list
                if hasattr(top, "on_wheel"):
                    top.on_wheel(e)
                return
            if bar.winfo_ismapped():
                canvas.yview_scroll(int(-e.delta / 120), "units")
        self.bind_all("<MouseWheel>", wheel)

        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=(16, 14))

        # settings
        form = tk.Frame(body, bg=BG)
        form.pack(fill="x")
        form.columnconfigure(0, weight=1)
        form.columnconfigure(1, weight=1)

        # people: whose amounts to show (you and/or friends)
        ppl = tk.Frame(form, bg=BG)
        ppl.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self._label(ppl, "People").pack(anchor="w")
        self.people_box = tk.Frame(ppl, bg=BG)
        self.people_box.pack(fill="x", pady=(4, 0))
        ctl = tk.Frame(ppl, bg=BG)
        ctl.pack(fill="x")
        ttk.Button(ctl, text="+ Add person", command=self._add_person).pack(side="left")
        hint = self._label(ppl, "Click a person to search your friends list (it fills in once the "
                                "tracker is running). A friend's amount shows while they're in WARDOGS.",
                           fg="#8A93A3", size=8)
        hint.configure(wraplength=480, justify="left")
        hint.pack(anchor="w", pady=(4, 0))

        # the leaderboard: one shared file for everyone ticked "Leaderboard"
        SH = "#141C2A"
        lb = tk.Frame(form, bg=SH, highlightbackground="#2C4A7A", highlightthickness=1)
        lb.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        li = tk.Frame(lb, bg=SH)
        li.pack(fill="x", padx=12, pady=10)
        head = tk.Frame(li, bg=SH)
        head.pack(fill="x")
        self._label(head, "Leaderboard", SH, TEXT, 10, True).pack(side="left")
        self.board_on_var = tk.BooleanVar()
        Switch(head, self.board_on_var, bg=SH).pack(side="right")
        self.board_body = tk.Frame(li, bg=SH)
        self.board_body.pack(fill="x")
        bf = tk.Frame(self.board_body, bg=SH)
        bf.pack(fill="x", pady=(8, 0))
        self.board_file_var = tk.StringVar()
        ttk.Entry(bf, textvariable=self.board_file_var).pack(side="left", fill="x", expand=True)
        ttk.Button(bf, text="Browse…", command=self._browse_board).pack(side="left", padx=(8, 0))
        orow = tk.Frame(self.board_body, bg=SH)
        orow.pack(fill="x", pady=(8, 0))
        self._label(orow, "Order", SH).pack(side="left")
        self.order_var = tk.StringVar()
        seg, self.order_btns = self._segmented(
            orow, (("amount", "Highest first"), ("list", "List order")), self.order_var)
        seg.pack(side="left", padx=(10, 0))
        self.ranks_var = tk.BooleanVar()
        self.board_names_var = tk.BooleanVar()
        crow = tk.Frame(self.board_body, bg=SH)
        crow.pack(fill="x", pady=(8, 0))
        self._label(crow, "Show", SH).pack(side="left")
        Chip(crow, "#", "Number places", self.ranks_var).pack(side="left", padx=(12, 0))
        Chip(crow, "👤", "Names", self.board_names_var).pack(side="left", padx=(8, 0))
        # how many people: everyone, or just the top N
        trow = tk.Frame(self.board_body, bg=SH)
        trow.pack(fill="x", pady=(8, 0))
        self._label(trow, "How many", SH).pack(side="left")
        self.board_top_var = tk.StringVar()   # "all" or "top"
        seg, self.top_btns = self._segmented(trow, (("all", "All"), ("top", "Top")), self.board_top_var)
        seg.pack(side="left", padx=(12, 0))
        # − N +: how many, lit only while Top is chosen (clicking it picks Top)
        self.board_top_n_var = tk.IntVar()
        step = tk.Frame(trow, bg=BORDER)
        step.pack(side="left", padx=(8, 0))
        self.top_minus = tk.Label(step, text="−", font=("Segoe UI", 10), padx=10, pady=4, cursor="hand2")
        self.top_num = tk.Label(step, font=("Segoe UI", 10), width=3, pady=4)
        self.top_plus = tk.Label(step, text="+", font=("Segoe UI", 10), padx=10, pady=4, cursor="hand2")
        for w_ in (self.top_minus, self.top_num, self.top_plus):
            w_.pack(side="left", padx=1, pady=1)
        self.top_minus.bind("<Button-1>", lambda e: self._step_top(-1))
        self.top_plus.bind("<Button-1>", lambda e: self._step_top(1))
        self.top_people = self._label(trow, "", SH, "#8A93A3", 8)
        self.top_people.pack(side="left", padx=(8, 0))
        for v in (self.board_top_var, self.board_top_n_var):
            v.trace_add("write", lambda *_: self._paint_top())
        # the leaderboard browser overlay
        brow = tk.Frame(self.board_body, bg=SH)
        brow.pack(fill="x", pady=(8, 0))
        self._label(brow, "Overlay", SH).pack(side="left")
        self.board_overlay_var = tk.BooleanVar()
        Chip(brow, "📺", "Browser overlay", self.board_overlay_var).pack(side="left", padx=(12, 0))
        # shown while the overlay is on: its link, and what 1st place gets
        self.board_link_row = tk.Frame(self.board_body, bg=SH)
        lrow = tk.Frame(self.board_link_row, bg=SH)
        lrow.pack(fill="x")
        self._label(lrow, "Overlay link", SH, LABEL, 9).pack(side="left")
        self.board_link = tk.Label(lrow, text=f"…/overlay/{BOARD_NAME}", bg=SH, fg=ACCENT,
                                   cursor="hand2", font=("Segoe UI", 9, "underline"), anchor="w")
        self.board_link.pack(side="left", padx=(8, 0))
        self.board_link.bind("<Button-1>", lambda e: self._copy_board_link())
        frow = tk.Frame(self.board_link_row, bg=SH)
        frow.pack(fill="x", pady=(8, 0))
        self._label(frow, "1st place", SH).pack(side="left")
        self.board_first_var = tk.StringVar()
        seg, self.first_btns = self._segmented(
            frow, (("crown", "👑 Crown"), ("number", "# Number"), ("image", "🖼 Custom")), self.board_first_var)
        seg.pack(side="left", padx=(10, 0))
        self.icon_row = tk.Frame(self.board_link_row, bg=SH)
        self.icon_name = self._label(self.icon_row, "", SH, LABEL, 9)
        self.icon_name.pack(side="left", fill="x", expand=True)
        ttk.Button(self.icon_row, text="Choose image…", command=self._browse_icon).pack(side="right")
        self._board_link_after = brow
        hint = self._label(self.board_body, "Everyone with Leaderboard on, a line each — as a text file "
                                            "(choose one above, or leave it empty) and / or a browser "
                                            "overlay. People with no amount right now are left off.",
                           SH, "#8A93A3", 8)
        hint.configure(wraplength=470, justify="left")
        hint.pack(anchor="w", pady=(6, 0))



        # Advanced (closed by default): refreshing the friends page
        self.adv_head = tk.Label(body, text="▸  Advanced", bg=BG, fg=TEXT, cursor="hand2",
                                 font=("Segoe UI", 10, "bold"), anchor="w")
        self.adv_head.pack(anchor="w", pady=(14, 0))
        self.adv_head.bind("<Button-1>", lambda e: self._show_advanced(not self.adv_open))
        AB = "#11151C"
        self.adv_body = tk.Frame(body, bg=AB, highlightbackground="#232A35", highlightthickness=1)
        ab = tk.Frame(self.adv_body, bg=AB)
        ab.pack(fill="x", padx=12, pady=10)
        # keep showing the last amount while the game shows none (menus)
        kr = tk.Frame(ab, bg=AB)
        kr.pack(fill="x")
        self._label(kr, "Keep last amount", AB, TEXT).pack(side="left")
        self.keep_var = tk.BooleanVar()
        Switch(kr, self.keep_var, bg=AB).pack(side="right")
        keep_hint = self._label(ab, "While the game shows no money (menus, loading) the overlays and "
                                    "files keep the last amount instead of going blank.", AB, "#8A93A3", 8)
        keep_hint.configure(wraplength=470, justify="left")
        keep_hint.pack(anchor="w", pady=(4, 10))
        # +$3,740 or + $3,740 — the buttons show both, so no label
        self.space_var = tk.BooleanVar()
        seg, self.space_btns = self._segmented(
            ab, ((False, "+$3,740"), (True, "+ $3,740")), self.space_var)
        seg.pack(anchor="w", pady=(0, 10))
        rr = tk.Frame(ab, bg=AB)
        rr.pack(fill="x")
        self._label(rr, "Refresh friends page", AB, TEXT).pack(side="left")
        self.refresh_var = tk.IntVar()  # minutes; 0 = off
        seg, self.refresh_btns = self._segmented(
            rr, [(v, f"{v} min" if v else "Off") for v in REFRESH_CHOICES], self.refresh_var)
        seg.pack(side="left", padx=(12, 0))
        warn = self._label(ab, "Off is best — the page updates live. Only pick a time if the number "
                               "stops updating; refreshing often can get you rate limited by Steam.",
                           AB, "#E0A458", 8)
        warn.configure(wraplength=470, justify="left")
        warn.pack(anchor="w", pady=(6, 0))
        tr = tk.Frame(ab, bg=AB)
        tr.pack(fill="x", pady=(10, 0))
        self._label(tr, "Minimize to tray", AB, TEXT).pack(side="left")
        self.tray_var = tk.BooleanVar()
        Switch(tr, self.tray_var, bg=AB).pack(side="right")
        tray_hint = self._label(ab, "Minimising hides the window to a tray icon by the clock — click it "
                                    "to bring the window back, right-click for Start / Stop and Quit.",
                                AB, "#8A93A3", 8)
        tray_hint.configure(wraplength=470, justify="left")
        tray_hint.pack(anchor="w", pady=(4, 0))
        sr0 = tk.Frame(ab, bg=AB)
        sr0.pack(fill="x", pady=(10, 0))
        self._label(sr0, "Start with Windows", AB, TEXT).pack(side="left")
        self.startup_var = tk.BooleanVar(value=bool(startup_entry()))
        Switch(sr0, self.startup_var, bg=AB).pack(side="right")
        self.startup_var.trace_add("write", lambda *_: self._set_startup())
        st_hint = self._label(ab, "Opens when you sign in to Windows and starts tracking — straight to "
                                  "the tray when Minimize to tray is on.", AB, "#8A93A3", 8)
        st_hint.configure(wraplength=470, justify="left")
        st_hint.pack(anchor="w", pady=(4, 0))
        # colors
        cr = tk.Frame(ab, bg=AB)
        cr.pack(fill="x", pady=(14, 0))
        self.color_head = self._label(cr, "App / OBS text color", AB)
        self.color_head.pack(side="left")
        # switch, like OBS color: off dims the heading and both colour buttons
        self.color_on_var = tk.BooleanVar()
        Switch(cr, self.color_on_var, bg=AB).pack(side="right")
        cl = self._label(cr, "Color by profit / loss", AB, LABEL, 9)
        cl.configure(cursor="hand2")
        cl.pack(side="right", padx=(0, 8))
        cl.bind("<Button-1>", lambda e: self.color_on_var.set(not self.color_on_var.get()))
        cc = tk.Frame(ab, bg=AB)
        cc.pack(fill="x", pady=(4, 0))
        cc.columnconfigure(0, weight=1)
        cc.columnconfigure(1, weight=1)
        self.profit_btn = self._color_button(cc, "+ Profit", "profit_color")
        self.profit_btn.grid(row=0, column=0, sticky="ew")
        self.loss_btn = self._color_button(cc, "− Loss", "loss_color")
        self.loss_btn.grid(row=0, column=1, sticky="ew", padx=(12, 0))

        # OBS card
        obs = tk.Frame(ab, bg=CARD, highlightbackground="#262C36", highlightthickness=1)
        obs.pack(fill="x", pady=(12, 0))
        inner = tk.Frame(obs, bg=CARD)
        inner.pack(fill="x", padx=14, pady=12)
        inner.columnconfigure(0, weight=2)
        inner.columnconfigure(1, weight=1)
        head = tk.Frame(inner, bg=CARD)
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._label(head, "OBS WebSocket", CARD, TEXT, 10, True).pack(side="left")
        self.obs_pill = tk.Label(head, text="● Not used", bg=CARD, fg=MUTED, font=("Segoe UI", 9, "bold"))
        self.obs_pill.pack(side="left", padx=(10, 0))

        # connection: no switch of its own — "Color by profit / loss" turns
        # colouring on, Connect / Disconnect decides whether OBS is used
        fields = tk.Frame(inner, bg=CARD)
        fields.grid(row=1, column=0, columnspan=2, sticky="ew")
        fields.columnconfigure(0, weight=2)
        fields.columnconfigure(1, weight=1)
        src_hint = self._label(fields, "With Color by profit / loss on, each person's OBS Text source "
                                       "(set on their card, under their separate file) turns green or red "
                                       "from their own amount.", CARD, "#8A93A3", 8)
        src_hint.configure(wraplength=430, justify="left")
        src_hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 8))
        self._label(fields, "Host", CARD).grid(row=3, column=0, sticky="w")
        self._label(fields, "Port", CARD).grid(row=3, column=1, sticky="w", padx=(12, 0))
        self.obs_host_var = tk.StringVar()
        self.obs_port_var = tk.StringVar()
        ttk.Entry(fields, textvariable=self.obs_host_var).grid(row=4, column=0, sticky="ew", pady=(4, 8))
        ttk.Entry(fields, textvariable=self.obs_port_var).grid(row=4, column=1, sticky="ew", padx=(12, 0), pady=(4, 8))
        self._label(fields, "Password", CARD).grid(row=5, column=0, columnspan=2, sticky="w")
        pw = tk.Frame(fields, bg=CARD)
        pw.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4, 2))
        self.obs_pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(pw, textvariable=self.obs_pass_var, show="•")
        self.pass_entry.pack(side="left", fill="x", expand=True)
        self.show_pass_btn = ttk.Button(pw, text="Show", width=6, command=self._toggle_pass)
        self.show_pass_btn.pack(side="left", padx=(8, 0))
        # Test and Connect / Disconnect get their own row so nothing is squashed
        act = tk.Frame(fields, bg=CARD)
        act.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(act, text="Test", command=self._test_obs).pack(side="left")
        self.connect_btn = ttk.Button(act, text="Disconnect", width=11, command=self._toggle_connect)
        self.connect_btn.pack(side="left", padx=(8, 0))
        self._label(fields, "In OBS: Tools › WebSocket Server Settings › Show Connect Info",
                    CARD, "#8A93A3", 8).grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 0))
        # "Connection" fold-out: hides host / port / password / Test / Connect
        # (e.g. so the password box isn't on screen while streaming)
        self.conn_head = tk.Label(fields, text="", bg=CARD, fg=LABEL, cursor="hand2",
                                  font=("Segoe UI", 9, "bold"), anchor="w")
        self.conn_head.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.conn_head.bind("<Button-1>", lambda e: self._show_connection(not self.cfg["obs_conn_open"]))
        self.conn_widgets = [w for w in fields.grid_slaves() if int(w.grid_info()["row"]) >= 3]

        # Browser overlays: text size and colour for the money HUD and leaderboard pages
        self._label(ab, "Browser overlays", AB, TEXT).pack(anchor="w", pady=(14, 0))
        fr = tk.Frame(ab, bg=AB)
        fr.pack(fill="x", pady=(6, 0))
        self._label(fr, "Font", AB).pack(side="left")
        self.ov_font_var = tk.StringVar()
        try:
            installed = sorted({f for f in tkfont.families(self) if not f.startswith("@")} - set(WEB_FONTS),
                               key=str.lower)
        except tk.TclError:
            installed = []
        self.font_box = ttk.Combobox(fr, textvariable=self.ov_font_var, state="readonly", height=18,
                                     values=list(WEB_FONTS) + [FONT_SEPARATOR] + installed, width=30)
        self.font_box.pack(side="left", padx=(12, 0))
        self.font_box.bind("<<ComboboxSelected>>", lambda e: self._pick_font())
        self.font_note = self._label(fr, "", AB, "#8A93A3", 8)
        self.font_note.pack(side="left", padx=(8, 0))
        sr = tk.Frame(ab, bg=AB)
        sr.pack(fill="x", pady=(8, 0))
        self._label(sr, "Text size", AB).pack(side="left")
        self.ov_size_var = tk.IntVar()
        step = tk.Frame(sr, bg=BORDER)
        step.pack(side="left", padx=(12, 0))
        self.size_minus = tk.Label(step, text="−", font=("Segoe UI", 10), padx=10, pady=4, cursor="hand2")
        self.size_num = tk.Label(step, font=("Segoe UI", 10), width=3, pady=4, bg=BG_DARK, fg=TEXT)
        self.size_plus = tk.Label(step, text="+", font=("Segoe UI", 10), padx=10, pady=4, cursor="hand2")
        for w_ in (self.size_minus, self.size_num, self.size_plus):
            w_.pack(side="left", padx=1, pady=1)
        self.size_minus.bind("<Button-1>", lambda e: self._step_size(-1))
        self.size_plus.bind("<Button-1>", lambda e: self._step_size(1))
        self.size_reset = self._label(sr, "", AB, "#8A93A3", 8)
        self.size_reset.pack(side="left", padx=(8, 0))
        self.size_reset.bind("<Button-1>", lambda e: self.ov_size_var.set(DEFAULTS["overlay_size"]))
        self.ov_size_var.trace_add("write", lambda *_: self._paint_size())
        cr2 = tk.Frame(ab, bg=AB)
        cr2.pack(fill="x", pady=(8, 0))
        self._label(cr2, "Text color", AB).pack(side="left")
        self.ov_color_btn = self._small_color_button(cr2, "overlay_color")
        self.ov_color_btn.pack(side="left", padx=(12, 0))
        pr = tk.Frame(ab, bg=AB)
        pr.pack(fill="x", pady=(8, 0))
        self._label(pr, "Color by profit / loss", AB).pack(side="left")
        self.ov_pl_var = tk.BooleanVar()
        self.ov_pl_board_var = tk.BooleanVar()
        Chip(pr, "$", "Money HUD", self.ov_pl_var).pack(side="left", padx=(12, 0))
        Chip(pr, "🏆", "Leaderboard", self.ov_pl_board_var).pack(side="left", padx=(8, 0))
        pc = tk.Frame(ab, bg=AB)
        pc.pack(fill="x", pady=(6, 0))
        self.ov_profit_btn = self._small_color_button(pc, "overlay_profit_color")
        self.ov_profit_btn.pack(side="left")
        self.ov_loss_btn = self._small_color_button(pc, "overlay_loss_color")
        self.ov_loss_btn.pack(side="left", padx=(8, 0))
        # background: a colour at some opacity, or your own picture
        bgr = tk.Frame(ab, bg=AB)
        bgr.pack(fill="x", pady=(10, 0))
        self._label(bgr, "Background", AB).pack(side="left")
        self.ov_bg_mode_var = tk.StringVar()
        seg, self.bg_mode_btns = self._segmented(bgr, (("color", "Color"), ("image", "Image")), self.ov_bg_mode_var)
        seg.pack(side="left", padx=(12, 0))
        self.ov_bg_btn = self._small_color_button(bgr, "overlay_bg_color")
        self.ov_bg_btn.pack(side="left", padx=(8, 0))
        self.bg_image_row = tk.Frame(ab, bg=AB)
        self.bg_image_name = self._label(self.bg_image_row, "", AB, LABEL, 9)
        self.bg_image_name.pack(side="left", fill="x", expand=True)
        ttk.Button(self.bg_image_row, text="Choose image…", command=self._browse_bg).pack(side="right")
        self._bg_image_after = bgr
        opr = tk.Frame(ab, bg=AB)
        opr.pack(fill="x", pady=(8, 0))
        self._opacity_row = opr
        self._label(opr, "Opacity", AB).pack(side="left")
        self.ov_opacity_var = tk.IntVar()
        self._stepper(opr, self.ov_opacity_var, 0, 100, 5, AB, unit="%").pack(side="left", padx=(12, 0))
        # box size: Auto fits the text
        bxr = tk.Frame(ab, bg=AB)
        bxr.pack(fill="x", pady=(10, 0))
        self._label(bxr, "Money HUD box", AB).pack(side="left")
        self.ov_hud_w_var, self.ov_hud_h_var, self.ov_board_w_var = tk.IntVar(), tk.IntVar(), tk.IntVar()
        self._label(bxr, "W", AB, "#8A93A3", 9).pack(side="left", padx=(12, 4))
        self._stepper(bxr, self.ov_hud_w_var, 20, BOX_MAX, 10, AB, auto=True).pack(side="left")
        self._label(bxr, "H", AB, "#8A93A3", 9).pack(side="left", padx=(10, 4))
        self._stepper(bxr, self.ov_hud_h_var, 10, BOX_MAX, 1, AB, auto=True).pack(side="left")
        lbr = tk.Frame(ab, bg=AB)
        lbr.pack(fill="x", pady=(8, 0))
        self._label(lbr, "Leaderboard box", AB).pack(side="left")
        self._label(lbr, "W", AB, "#8A93A3", 9).pack(side="left", padx=(12, 4))
        self._stepper(lbr, self.ov_board_w_var, 100, BOX_MAX, 10, AB, auto=True).pack(side="left")
        self._label(lbr, "(height follows the rows)", AB, "#8A93A3", 8).pack(side="left", padx=(10, 0))
        # OBS: size the Browser sources to fit
        ofr = tk.Frame(ab, bg=AB)
        ofr.pack(fill="x", pady=(10, 0))
        self._label(ofr, "Size OBS sources to fit", AB).pack(side="left")
        self.obs_fit_var = tk.BooleanVar()
        Switch(ofr, self.obs_fit_var, bg=AB).pack(side="right")
        ofb = tk.Frame(ab, bg=AB)
        ofb.pack(fill="x", pady=(6, 0))
        self.obs_fit_info = self._label(ofb, "", AB, "#8A93A3", 8)
        self.obs_fit_info.configure(wraplength=330, justify="left")
        self.obs_fit_info.pack(side="left", fill="x", expand=True)
        ttk.Button(ofb, text="Resize in OBS now", command=lambda: self._fit_obs(True)).pack(side="right")
        ov_hint = self._label(ab, "For the 📺 overlays in OBS (money HUD and leaderboard); changes show "
                                  "within a moment. Color by profit / loss: pick which overlays get the Profit / "
                                  "Loss colours — names and $0 stay the text colour.",
                              AB, "#8A93A3", 8)
        ov_hint.configure(wraplength=470, justify="left")
        ov_hint.pack(anchor="w", pady=(6, 0))
        self.adv_open = False

        # any setting change -> save + apply live
        for v in (self.board_on_var, self.board_overlay_var, self.board_top_var, self.board_top_n_var,
                  self.board_first_var,
                  self.board_file_var, self.order_var, self.ranks_var,
                  self.board_names_var, self.space_var, self.refresh_var, self.color_on_var,
                  self.tray_var, self.ov_font_var, self.ov_size_var, self.ov_pl_var, self.ov_pl_board_var, self.ov_bg_mode_var,
                  self.ov_opacity_var, self.ov_hud_w_var, self.ov_hud_h_var, self.ov_board_w_var,
                  self.obs_fit_var,
                  self.keep_var, self.obs_host_var,
                  self.obs_port_var, self.obs_pass_var):
            v.trace_add("write", lambda *_: self._sync_cfg())

    def _segmented(self, parent, choices, var):
        """A joined row of buttons, the chosen one lit blue (Order, +$ spacing,
        Refresh). choices = [(value, text)]; clicking one sets `var`, and the
        buttons repaint themselves whenever `var` changes."""
        seg = tk.Frame(parent, bg=BORDER)  # the border shows through as a 1px outline
        btns = {}
        for value, text in choices:
            b = tk.Label(seg, text=text, font=("Segoe UI", 10), padx=12, pady=4, cursor="hand2")
            b.pack(side="left", padx=1, pady=1)
            b.bind("<Button-1>", lambda e, v=value: var.set(v))
            btns[value] = b

        def paint(*_):
            try:
                current = var.get()
            except tk.TclError:
                current = None
            for value, b in btns.items():
                on = value == current
                b.configure(bg=ACCENT if on else BG_DARK, fg="white" if on else MUTED)
        var.trace_add("write", paint)
        paint()
        return seg, btns

    def _stepper(self, parent, var, lo, hi, step, bg, auto=False, unit=""):
        """− N +, tied to an IntVar. Hold a button to keep stepping. With auto,
        0 shows "Auto" (stepping below lo goes there, + from Auto starts at lo)."""
        f = tk.Frame(parent, bg=BORDER)
        minus = tk.Label(f, text="−", font=("Segoe UI", 10), padx=8, pady=3, cursor="hand2")
        num = tk.Label(f, font=("Segoe UI", 10), width=5, pady=3, bg=BG_DARK, fg=TEXT)
        plus = tk.Label(f, text="+", font=("Segoe UI", 10), padx=8, pady=3, cursor="hand2")
        for w_ in (minus, num, plus):
            w_.pack(side="left", padx=1, pady=1)

        def get():
            try:
                return int(var.get())
            except (tk.TclError, ValueError):
                return 0 if auto else lo

        def move(d):
            n = get()
            if auto and n == 0:
                n = lo if d > 0 else 0
            else:
                n += d * step
                n = 0 if auto and n < lo else max(lo, min(n, hi))
            var.set(n)

        def paint(*_):
            n = get()
            num.configure(text="Auto" if auto and n == 0 else f"{n}{unit}")
            minus.configure(bg=BG_DARK, fg=ACCENT if (n > (0 if auto else lo)) else "#4A5262")
            plus.configure(bg=BG_DARK, fg=ACCENT if n < hi else "#4A5262")

        for b, d in ((minus, -1), (plus, 1)):
            state = {}

            def press(e, d=d, state=state):
                move(d)
                def again(delay):
                    move(d)
                    state["job"] = f.after(delay, again, 40)
                state["job"] = f.after(400, again, 60)

            def release(e, state=state):
                if state.get("job"):
                    f.after_cancel(state.pop("job"))
            b.bind("<ButtonPress-1>", press)
            b.bind("<ButtonRelease-1>", release)
            b.bind("<Leave>", release)
        var.trace_add("write", paint)
        f.after_idle(paint)
        f.step = move  # for tests
        return f

    def _browse_bg(self):
        path = filedialog.askopenfilename(
            title="Background picture for the browser overlays",
            filetypes=[("Pictures", " ".join("*" + e for e in ICON_TYPES)), ("All files", "*.*")])
        self._bg_asking = False
        if path:
            self.cfg["overlay_bg_image"] = path
            save_settings(self.cfg)
        elif not self.cfg["overlay_bg_image"]:
            self.ov_bg_mode_var.set("color")  # cancelled with nothing chosen: back to a colour
        self._paint_bg()

    def _paint_bg(self):
        c = self.cfg
        image = c["overlay_bg_mode"] == "image"
        if image:
            pic = c["overlay_bg_image"]
            self.bg_image_name.configure(text=os.path.basename(pic) if pic else "No image chosen",
                                         fg=LABEL if pic else "#FACC15")
            self.bg_image_row.pack(fill="x", pady=(6, 0), after=self._bg_image_after)
            self.ov_bg_btn.pack_forget()
        else:
            self.bg_image_row.pack_forget()
            if not self.ov_bg_btn.winfo_ismapped():
                self.ov_bg_btn.pack(side="left", padx=(8, 0))
        self._refit_soon()

    def _paint_fit(self):
        c = self.cfg
        sizes = obs_fit_sizes(c)
        hud, board = sizes[HUD_NAME], sizes[BOARD_NAME]
        text = f"Money HUD {hud[0]} × {hud[1]}  ·  Leaderboard {board[0]} × {board[1]}"
        if c["obs_fit"] and not c["obs_connect"]:
            text += "\nOBS is disconnected — press Connect in OBS WebSocket."
        elif c["obs_fit"]:
            text += "\nKept in step automatically while OBS is connected."
        self.obs_fit_info.configure(text=text)

    def _fit_obs_soon(self):
        """Resize the OBS sources shortly after the last change (not on every click)."""
        if getattr(self, "_fit_job", None):
            self.after_cancel(self._fit_job)
        self._fit_job = self.after(900, self._fit_obs)

    def _fit_obs(self, asked=False):
        """Set every OBS Browser source showing one of our pages to its fitting
        size. asked: from the button, so say what happened even if nothing changed."""
        self._fit_job = None
        c = self.cfg
        if not asked and not (c["obs_fit"] and c["obs_connect"]):
            return
        sizes, last = obs_fit_sizes(c), getattr(self, "_fit_done", None)
        if not asked and sizes == last:
            return
        cfg = copy.deepcopy(c)

        def say(msg, color):
            self.events.put(("log", time.strftime("%H:%M:%S"), msg, color))

        def run():
            try:
                cl = obs_client(cfg)
            except Exception as e:
                say(f"OBS: couldn't connect to resize the browser sources — {e}", "#FF8A7A")
                return
            try:
                found = obs_browser_sources(cl)
                for name, page in found:
                    w, h = sizes[page]
                    cl.set_input_settings(name, {"width": w, "height": h}, True)
            except Exception as e:
                say(f"OBS: resizing the browser sources failed — {e}", "#FF8A7A")
                return
            finally:
                try:
                    cl.disconnect()
                except Exception:
                    pass
            self._fit_done = sizes
            if found:
                say("OBS: sized " + ", ".join(f'"{n}" {sizes[pg][0]}×{sizes[pg][1]}' for n, pg in found),
                    "#5FE39B")
            else:
                say("OBS: no Browser source showing money_hud.html or leaderboard.html — add one "
                    "with the Overlay link first.", "#FACC15")
        threading.Thread(target=run, daemon=True).start()

    def _small_color_button(self, parent, key):
        return tk.Button(parent, relief="flat", bd=0, cursor="hand2", font=("Segoe UI", 9, "bold"),
                         padx=10, pady=4, command=lambda: self._pick_color(key))

    def _paint_overlay_colors(self):
        c = self.cfg
        self.ov_bg_btn.configure(text=f"{c['overlay_bg_color'].upper()}", bg=BG_DARK,
                                 fg=c["overlay_bg_color"] if c["overlay_bg_color"].upper() != "#000000" else LABEL,
                                 activebackground=BG_DARK, highlightbackground=BORDER, highlightthickness=1)
        for btn, key, label in ((self.ov_color_btn, "overlay_color", "Text"),
                                (self.ov_profit_btn, "overlay_profit_color", "+ Profit"),
                                (self.ov_loss_btn, "overlay_loss_color", "− Loss")):
            on = key == "overlay_color" or c["overlay_pl"] or c["overlay_pl_board"]
            fg = c[key] if on else "#4A5262"   # profit / loss dimmed while off (still clickable)
            btn.configure(text=f"{label}   {c[key].upper()}", bg=BG_DARK, fg=fg,
                          activebackground=BG_DARK, activeforeground=fg,
                          highlightbackground=BORDER, highlightthickness=1)

    def _pick_font(self):
        if self.ov_font_var.get() == FONT_SEPARATOR:  # the divider isn't a font
            self.ov_font_var.set(self.cfg["overlay_font"])
        self._paint_font()

    def _paint_font(self):
        f = self.ov_font_var.get()
        self.font_note.configure(text="game font" if f == "Barlow" else
                                 "from Google Fonts" if f in WEB_FONTS else "installed font")

    def _step_size(self, d):
        n = self.ov_size_var.get() + d
        self.ov_size_var.set(max(OVERLAY_SIZE_MIN, min(n, OVERLAY_SIZE_MAX)))

    def _paint_size(self):
        try:
            n = self.ov_size_var.get()
        except tk.TclError:
            return
        self.size_num.configure(text=str(n))
        for b, ok in ((self.size_minus, n > OVERLAY_SIZE_MIN), (self.size_plus, n < OVERLAY_SIZE_MAX)):
            b.configure(bg=BG_DARK, fg=ACCENT if ok else "#4A5262")
        default = n == DEFAULTS["overlay_size"]
        self.size_reset.configure(text="px  (game size)" if default else "px  ·  reset",
                                  cursor="" if default else "hand2",
                                  fg="#8A93A3" if default else ACCENT)

    def _color_button(self, parent, label, key):
        b = tk.Button(parent, text=label, relief="flat", bd=0, cursor="hand2",
                      font=("Segoe UI", 10, "bold"), padx=10, pady=10,
                      command=lambda: self._pick_color(key))
        return b

    def _paint_color_buttons(self):
        on = bool(self.cfg["color_on"])
        self.color_head.configure(fg=LABEL if on else "#5C6575")
        for btn, key, label in ((self.profit_btn, "profit_color", "+ Profit"),
                                (self.loss_btn, "loss_color", "− Loss")):
            # dimmed while colouring is off (still clickable to pick a colour)
            c = self.cfg[key] if on else "#4A5262"
            btn.configure(text=f"{label}   {self.cfg[key].upper()}", bg=BG_DARK, fg=c,
                          activebackground=BG_DARK, activeforeground=c,
                          highlightbackground=BORDER, highlightthickness=1)

    # ---- settings <-> widgets ----
    def _load_into_widgets(self):
        self._loading = True
        c = dict(self.cfg)
        self.board_on_var.set(bool(c["board_on"]))
        self.board_file_var.set(c["board_file"])
        self.order_var.set(c["board_order"])
        self.ranks_var.set(bool(c["board_ranks"]))
        self.board_names_var.set(bool(c["board_names"]))
        self.board_overlay_var.set(bool(c["board_overlay"]))
        self.board_top_n_var.set(min(c["board_top"] or c["board_top_n"], BOARD_TOP_MAX))
        self.board_top_var.set("top" if c["board_top"] > 0 else "all")
        self.board_first_var.set(c["board_first"])
        self.space_var.set(bool(c["space"]))
        self.tray_var.set(bool(c["tray"]))
        self.ov_font_var.set(c["overlay_font"])
        self._paint_font()
        self.ov_size_var.set(c["overlay_size"])
        self.ov_pl_var.set(bool(c["overlay_pl"]))
        self.ov_pl_board_var.set(bool(c["overlay_pl_board"]))
        self.ov_bg_mode_var.set(c["overlay_bg_mode"])
        self.ov_opacity_var.set(c["overlay_bg_opacity"])
        self.ov_hud_w_var.set(c["overlay_hud_w"])
        self.ov_hud_h_var.set(c["overlay_hud_h"])
        self.ov_board_w_var.set(c["overlay_board_w"])
        self.obs_fit_var.set(bool(c["obs_fit"]))
        self._paint_overlay_colors()
        self._paint_bg()
        self._paint_fit()
        self.refresh_var.set(refresh_choice(c["reload_on"], c["reload_minutes"]))
        self.color_on_var.set(bool(c["color_on"]))
        self.keep_var.set(bool(c["keep_last"]))
        self.obs_host_var.set(c["obs_host"])
        self.obs_port_var.set(str(c["obs_port"]))
        self.obs_pass_var.set(c["obs_password"])
        self._loading = False
        self._paint_color_buttons()
        self._paint_connect()
        self._paint_board()
        self._show_connection(c["obs_conn_open"], save=False)
        # Advanced opens as you left it (and always while a refresh is switched on)
        self._show_advanced(bool(c["adv_open"] or c["reload_on"]), save=False)
        self._build_people()

    # ---- people ----
    def _build_people(self):
        """(Re)draws one card per person: picture + name (click to search),
        which name they go by, remove, Separate file / Leaderboard, and their
        file (+ OBS source with OBS color on)."""
        for w in self.people_box.winfo_children():
            w.destroy()
        self.person_rows = []
        # drop the panel's lines for people who are no longer in the list
        self.current_people = self.current_people[:len(self.cfg["people"])]
        self._show_value(self.current_text)
        people = self.cfg["people"]
        for i, p in enumerate(people):
            card = tk.Frame(self.people_box, bg=CARD, highlightbackground="#262C36", highlightthickness=1)
            card.pack(fill="x", pady=(0, 6))
            row = tk.Frame(card, bg=CARD)
            row.pack(fill="x", padx=10, pady=8)
            line = tk.Frame(row, bg=CARD)
            line.pack(fill="x")
            btn = tk.Label(line, bg=BG_DARK, fg=TEXT, font=("Segoe UI", 10), anchor="w",
                           compound="left", padx=6, pady=3, cursor="hand2",
                           highlightthickness=1, highlightbackground=BORDER)
            btn.pack(side="left", fill="x", expand=True)
            btn.bind("<Button-1>", lambda e, i=i: self._pick_person(i))
            # which name they go by: Steam name / Custom… (+ a box for your label)
            mode = tk.StringVar(value=NAME_MODES[p["name_mode"]])
            box = ttk.Combobox(line, textvariable=mode, values=list(NAME_MODES.values()),
                               state="readonly", width=11)
            box.pack(side="left", padx=(8, 0))
            label = tk.StringVar(value=p["label"])
            label_entry = ttk.Entry(line, textvariable=label, width=10)
            label.trace_add("write", lambda *_, i=i, v=label: self._set_person(i, label=v.get()))
            mode.trace_add("write", lambda *_, i=i, v=mode, e=label_entry, b=box:
                           self._set_name_mode(i, v.get(), e, b))
            box.bind("<<ComboboxSelected>>", lambda e: e.widget.selection_clear())
            rm = ttk.Button(line, text="✕", width=3, command=lambda i=i: self._remove_person(i))
            rm.pack(side="left", padx=(8, 0))
            if p["name_mode"] == "custom":
                label_entry.pack(side="left", padx=(6, 0), before=rm)
            if len(people) == 1:
                rm.state(["disabled"])
            # where their amount goes: a separate file and/or the leaderboard
            chips = tk.Frame(row, bg=CARD)
            chips.pack(fill="x", pady=(8, 0))
            own = tk.BooleanVar(value=p["own_file"])
            on_board = tk.BooleanVar(value=p["board"])
            Chip(chips, "📄", "Separate file", own).pack(side="left")
            Chip(chips, "🏆", "Leaderboard", on_board).pack(side="left", padx=(8, 0))
            ov = tk.BooleanVar(value=p["overlay"])
            Chip(chips, "📺", "Overlay", ov).pack(side="left", padx=(8, 0))
            # when Overlay is on: their link, click to copy
            lr = tk.Frame(row, bg=CARD)
            self._label(lr, "Overlay link", CARD, LABEL, 9).pack(side="left")
            link = tk.Label(lr, text=self._short_link(i), bg=CARD, fg=ACCENT, cursor="hand2",
                            font=("Segoe UI", 9, "underline"), anchor="w")
            link.pack(side="left", padx=(8, 0))
            link.bind("<Button-1>", lambda e, i=i, l=link: self._copy_overlay_link(i, l))
            if p["overlay"]:
                lr.pack(fill="x", pady=(6, 0))
            ov.trace_add("write", lambda *_, i=i, v=ov, f=lr, c=chips: self._set_overlay(i, v.get(), f, c))
            # their own file block: the file, and (with OBS color on) their OBS source
            ff = tk.Frame(row, bg=CARD)
            fl = tk.Frame(ff, bg=CARD)
            fl.pack(fill="x")
            fvar = tk.StringVar(value=p["out_file"])
            fvar.trace_add("write", lambda *_, i=i, v=fvar: self._set_person(i, out_file=v.get().strip()))
            ttk.Entry(fl, textvariable=fvar).pack(side="left", fill="x", expand=True)
            ttk.Button(fl, text="Browse…",
                       command=lambda i=i, v=fvar: self._browse_person(i, v)).pack(side="left", padx=(8, 0))
            # their name in front of the amount in this file ("Name: + $3,740")
            fname = tk.BooleanVar(value=p["file_name"])
            fname.trace_add("write", lambda *_, i=i, v=fname: self._set_person(i, file_name=bool(v.get())))
            Chip(fl, "👤", "Name", fname).pack(side="left", padx=(8, 0))
            src = tk.StringVar(value=p["obs_source"])
            src.trace_add("write", lambda *_, i=i, v=src: self._set_person(i, obs_source=v.get().strip()))
            if self.cfg["color_on"]:
                sr = tk.Frame(ff, bg=CARD)
                sr.pack(fill="x", pady=(6, 0))
                self._label(sr, "OBS source", CARD, LABEL, 9).pack(side="left")
                ttk.Entry(sr, textvariable=src).pack(side="left", fill="x", expand=True, padx=(8, 0))
            if p["own_file"]:
                ff.pack(fill="x", pady=(6, 0))
            own.trace_add("write", lambda *_, i=i, v=own, f=ff: self._set_own_file(i, v.get(), f))
            on_board.trace_add("write", lambda *_, i=i, v=on_board: self._set_person(i, board=bool(v.get())))
            self.person_rows.append({"btn": btn, "mode": mode, "label": label, "file": fvar,
                                     "own": own, "board": on_board, "fname": fname, "src": src,
                                     "overlay": ov, "rm": rm})
        self._paint_people()
        self._refit_soon()

    def _set_own_file(self, i, on, file_row):
        self._set_person(i, own_file=bool(on))
        if on:
            file_row.pack(fill="x", pady=(6, 0))
        else:
            file_row.pack_forget()
        self._refit_soon()

    def _paint_board(self):
        on = bool(self.cfg["board_on"])
        if on:
            self.board_body.pack(fill="x")
        else:
            self.board_body.pack_forget()
        if self.cfg["board_overlay"]:
            self.board_link_row.pack(fill="x", pady=(6, 0), after=self._board_link_after)
        else:
            self.board_link_row.pack_forget()
        if self.cfg["board_first"] == "image":
            icon = self.cfg["board_icon"]
            self.icon_name.configure(text=os.path.basename(icon) if icon else "No image chosen — the crown "
                                     "is used until you choose one", fg=LABEL if icon else "#FACC15")
            self.icon_row.pack(fill="x", pady=(6, 0))
        else:
            self.icon_row.pack_forget()
        if getattr(self, "overlay_board", None) is not None:
            self._update_overlay_server()
        self._refit_soon()

    def _show_connection(self, open_it, save=True):
        self.cfg["obs_conn_open"] = bool(open_it)
        self.conn_head.configure(text=("▾" if open_it else "▸") + "  Connection"
                                 + ("" if open_it else "  (hidden)"))
        for w in self.conn_widgets:
            if open_it:
                w.grid()
            else:
                w.grid_remove()
        if save:
            save_settings(self.cfg)
        self._refit_soon()

    def _show_advanced(self, open_it, save=True):
        self.adv_open = open_it
        if save and self.cfg.get("adv_open") != bool(open_it):
            self.cfg["adv_open"] = bool(open_it)
            save_settings(self.cfg)
        self.adv_head.configure(text=("▾" if open_it else "▸") + "  Advanced")
        if open_it:
            self.adv_body.pack(fill="x", pady=(6, 0), after=self.adv_head)
        else:
            self.adv_body.pack_forget()
        self._refit_soon()

    def _step_top(self, d):
        """− / +: change how many, and switch to Top if All was chosen."""
        if self.board_top_var.get() != "top":
            self.board_top_var.set("top")
        else:
            n = self.board_top_n_var.get() + d
            self.board_top_n_var.set(max(1, min(n, BOARD_TOP_MAX)))

    def _paint_top(self):
        try:
            on, n = self.board_top_var.get() == "top", self.board_top_n_var.get()
        except tk.TclError:
            return
        self.top_num.configure(text=str(n), bg=BG_DARK, fg=TEXT if on else "#4A5262")
        for b, ok in ((self.top_minus, n > 1), (self.top_plus, n < BOARD_TOP_MAX)):
            b.configure(bg=BG_DARK, fg=(ACCENT if ok else "#4A5262") if on else "#4A5262")
        self.top_people.configure(text=("person" if n == 1 else "people") if on else "everyone")

    def _browse_icon(self):
        path = filedialog.askopenfilename(
            title="1st place picture for the leaderboard overlay",
            filetypes=[("Pictures", " ".join("*" + e for e in ICON_TYPES)), ("All files", "*.*")])
        self._icon_asking = False
        if path:
            self.cfg["board_icon"] = path
            save_settings(self.cfg)
        elif not self.cfg["board_icon"]:
            self.board_first_var.set("crown")  # cancelled with nothing chosen: back to the crown
        self._paint_board()

    def _browse_board(self):
        path = filedialog.asksaveasfilename(title="Leaderboard text file", defaultextension=".txt",
                                            initialfile=os.path.basename(self.board_file_var.get())
                                            or "Leaderboard.txt",
                                            filetypes=[("Text file", "*.txt")])
        if path:
            self.board_file_var.set(path)

    def _paint_people(self):
        for p, r in zip(self.cfg["people"], getattr(self, "person_rows", [])):
            r["btn"].configure(text=f"  {p['who'] or ME}", image=self.avatar_for(p["who"]))

    def _set_person(self, i, **changes):
        if getattr(self, "_loading", True) or i >= len(self.cfg["people"]):
            return
        self.cfg["people"][i].update(changes)
        save_settings(self.cfg)
        if "board" in changes:   # the leaderboard's fitting height changes
            self._paint_fit()
            self._fit_obs_soon()

    def _close_picker_on_move(self, e):
        if e.widget is self and self.picker is not None:
            geo = (self.winfo_rootx(), self.winfo_rooty(), self.winfo_width(), self.winfo_height())
            if geo != getattr(self, "_last_geo", geo) or e.type == tk.EventType.Unmap:
                self.picker.close()
            self._last_geo = geo
        elif e.widget is self:
            self._last_geo = (self.winfo_rootx(), self.winfo_rooty(),
                              self.winfo_width(), self.winfo_height())

    def _set_name_mode(self, i, text, label_entry, box):
        mode = next((k for k, v in NAME_MODES.items() if v == text), "steam")
        self._set_person(i, name_mode=mode)
        if mode == "custom":
            label_entry.pack(side="left", padx=(6, 0), after=box)
            label_entry.focus_set()
        else:
            label_entry.pack_forget()
        self._refit_soon()

    def _pick_person(self, i):
        if i >= len(self.person_rows):
            return
        def chosen(who):
            self.cfg["people"][i]["who"] = who
            save_settings(self.cfg)
            self._paint_people()
        PersonPicker(self, self.person_rows[i]["btn"], chosen)

    def _add_person(self):
        self.cfg["people"].append(new_person())
        save_settings(self.cfg)
        self._build_people()
        # go straight to choosing who it is
        self.after(50, lambda: self._pick_person(len(self.cfg["people"]) - 1))

    def _remove_person(self, i):
        if len(self.cfg["people"]) > 1:
            self.cfg["people"].pop(i)
            save_settings(self.cfg)
            self._build_people()

    def _browse_person(self, i, var):
        who = self.cfg["people"][i]["who"]
        path = filedialog.asksaveasfilename(title=f"Text file for {who or 'you'}", defaultextension=".txt",
                                            initialfile=os.path.basename(var.get()) or f"{who or 'My'} profit.txt",
                                            filetypes=[("Text file", "*.txt")])
        if path:
            var.set(path)

    # ---- profile pictures ----
    def avatar_for(self, who):
        """The small profile picture for `who` ("" = you); a grey square until
        it has downloaded, or if there isn't one."""
        url = self.avatar_urls.get(who.strip().lower(), "")
        img = self.avatar_imgs.get(url) if url else None
        if url and url not in self.avatar_imgs:
            self.avatar_imgs[url] = None  # downloading
            self.avatar_queue.put(url)
        return img or self.blank_avatar

    def _avatar_loader(self):
        # one download at a time, in the background
        while True:
            url = self.avatar_queue.get()
            self.events.put(("avatar", url, fetch_avatar(url, AVATAR_SIZE)))

    def _sync_cfg(self):
        if getattr(self, "_loading", True):
            return
        c = self.cfg
        try:
            c["obs_port"] = int(self.obs_port_var.get())
        except ValueError:
            pass
        minutes = int(self.refresh_var.get())
        c["reload_on"] = minutes > 0
        if minutes:
            c["reload_minutes"] = minutes
        # the tracker rewrites the file(s) with these within a couple of seconds
        c["space"] = bool(self.space_var.get())
        c["tray"] = bool(self.tray_var.get())
        font = self.ov_font_var.get().strip()
        if font and font != FONT_SEPARATOR:
            c["overlay_font"] = font
        try:
            c["overlay_size"] = max(OVERLAY_SIZE_MIN, min(int(self.ov_size_var.get()), OVERLAY_SIZE_MAX))
        except (tk.TclError, ValueError):
            pass
        c["overlay_pl"] = bool(self.ov_pl_var.get())
        c["overlay_pl_board"] = bool(self.ov_pl_board_var.get())
        for key, var, hi in (("overlay_bg_opacity", self.ov_opacity_var, 100),
                             ("overlay_hud_w", self.ov_hud_w_var, BOX_MAX),
                             ("overlay_hud_h", self.ov_hud_h_var, BOX_MAX),
                             ("overlay_board_w", self.ov_board_w_var, BOX_MAX)):
            try:
                c[key] = max(0, min(int(var.get()), hi))
            except (tk.TclError, ValueError):
                pass
        mode = self.ov_bg_mode_var.get()
        c["overlay_bg_mode"] = mode if mode in ("color", "image") else "color"
        if mode == "image" and not c["overlay_bg_image"] and not getattr(self, "_bg_asking", False):
            self._bg_asking = True
            self.after_idle(self._browse_bg)  # Image with no picture yet: pick one
        fit_was = c["obs_fit"]
        c["obs_fit"] = bool(self.obs_fit_var.get())
        if c["obs_fit"] and not fit_was:
            self._fit_done = None  # just switched on: size them now
        self._paint_overlay_colors()
        self._paint_bg()
        self._paint_fit()
        self._fit_obs_soon()
        c["board_on"] = bool(self.board_on_var.get())
        c["board_file"] = self.board_file_var.get().strip()
        c["board_order"] = self.order_var.get() if self.order_var.get() in ("amount", "list") else "amount"
        c["board_ranks"] = bool(self.ranks_var.get())
        c["board_names"] = bool(self.board_names_var.get())
        c["board_overlay"] = bool(self.board_overlay_var.get())
        n = max(1, min(int(self.board_top_n_var.get() or 5), BOARD_TOP_MAX))
        c["board_top_n"] = n
        c["board_top"] = n if self.board_top_var.get() == "top" else 0
        first = self.board_first_var.get()
        c["board_first"] = first if first in ("crown", "number", "image") else "crown"
        if c["board_first"] == "image" and not c["board_icon"] and not getattr(self, "_icon_asking", False):
            self._icon_asking = True
            self.after_idle(self._browse_icon)  # Custom with no picture yet: pick one
        self._paint_board()
        was_on = c["color_on"]
        c["color_on"] = bool(self.color_on_var.get())
        self._paint_color_buttons()
        c["keep_last"] = bool(self.keep_var.get())
        c["obs_host"] = self.obs_host_var.get().strip() or "localhost"
        c["obs_password"] = self.obs_pass_var.get()
        if was_on != c["color_on"]:
            if c["color_on"] and self._obs_sources():
                # switched on with OBS set up before: connect and check straight away
                c["obs_connect"] = True
                save_settings(c)
                self._test_obs()
            self._paint_connect(keep_pill=c["color_on"] and c["obs_connect"] and self._obs_sources())
            self._build_people()  # show / hide each person's OBS source box
        self._save_soon()
        self._show_value(self.current_text)

    def _save_soon(self):
        """Save now, or if a save just happened, once more shortly (so holding
        − / + doesn't rewrite settings.json many times a second)."""
        now = time.monotonic()
        if getattr(self, "_save_job", None):
            return  # a save is already on its way and will have this change
        wait = 0.3 - (now - getattr(self, "_saved_at", 0.0))
        if wait <= 0:
            self._saved_at = now
            save_settings(self.cfg)
        else:
            def later():
                self._save_job = None
                self._saved_at = time.monotonic()
                save_settings(self.cfg)
            self._save_job = self.after(int(wait * 1000) + 1, later)

    # ---- actions ----
    def _pick_color(self, key):
        _, hx = colorchooser.askcolor(color=self.cfg[key], title="Pick a color", parent=self)
        if hx:
            self.cfg[key] = hx.upper()
            save_settings(self.cfg)
            self._paint_color_buttons()
            self._paint_overlay_colors()
            self._show_value(self.current_text)

    def _toggle_pass(self):
        hidden = self.pass_entry.cget("show") == "•"
        self.pass_entry.configure(show="" if hidden else "•")
        self.show_pass_btn.configure(text="Hide" if hidden else "Show")

    def _obs_sources(self):
        """Every OBS source that would be coloured: people with a separate
        file and an OBS source filled in."""
        return sorted({p["obs_source"].strip() for p in self.cfg["people"]
                       if p["own_file"] and p["obs_source"].strip()})

    def _paint_connect(self, keep_pill=False):
        """Connect / Disconnect button text, and the status when nothing's
        been tried yet (keep_pill: a check is under way, leave its status)."""
        c = self.cfg
        self.connect_btn.configure(text="Disconnect" if c["obs_connect"] else "Connect")
        if keep_pill:
            return
        if not c["obs_connect"]:
            self._set_obs_pill("disconnected")
        elif not c["color_on"] or not self._obs_sources():
            self._set_obs_pill("notused")
        else:
            self._set_obs_pill("waiting")

    def _toggle_connect(self):
        c = self.cfg
        c["obs_connect"] = not c["obs_connect"]
        save_settings(c)
        stamp = time.strftime("%H:%M:%S")
        if c["obs_connect"]:
            self._paint_connect(keep_pill=True)
            self._add_log(stamp, "OBS: connecting…", None)
            self._test_obs()  # connect now and say whether it worked
            self._fit_done = None
            self._fit_obs_soon()
        else:
            self._paint_connect()
            self._add_log(stamp, "OBS: disconnected — press Connect to use it again.", "#FACC15")

    def _test_obs(self):
        self._set_obs_pill("testing")
        cfg = copy.deepcopy(self.cfg)
        sources = self._obs_sources()

        def say(msg, color):
            self.events.put(("log", time.strftime("%H:%M:%S"), msg, color))

        def run():
            try:
                cl = obs_client(cfg)
            except Exception as e:
                self.events.put(("obs", "error", "test"))
                say(f"OBS test failed — couldn't connect: {e}", "#FF8A7A")
                return
            found, missing = [], []
            try:
                for src in sources:
                    try:
                        cl.get_input_settings(src)
                        found.append(src)
                    except Exception as e:
                        if not obs_request_error(e):
                            raise  # the connection itself failed, not this name
                        missing.append(src)
            except Exception as e:
                self.events.put(("obs", "error", "test"))
                say(f"OBS test failed — the connection dropped: {e}", "#FF8A7A")
                return
            finally:
                try:
                    cl.disconnect()
                except Exception:
                    pass
            # the connection worked even if a name is wrong: the log says which one
            self.events.put(("obs", "connected", "test"))
            if not sources:
                say("OBS connected — now set an OBS source on someone's card (under their "
                    "separate file).", "#FACC15")
            if found:
                say("OBS test OK — found " + ", ".join(f'"{s}"' for s in found), "#5FE39B")
            if missing:
                say("OBS has no Text source called " + ", ".join(f'"{s}"' for s in missing)
                    + " — check the spelling (it must match OBS exactly).", "#FF8A7A")
        threading.Thread(target=run, daemon=True).start()

    def _open_txt(self):
        # the first file the tracker writes: a separate file, else the leaderboard
        files = target_files(self.cfg)
        path = files[0] if files else ""
        if not path:
            messagebox.showinfo(APP_NAME, "Turn on Separate file for someone, or the leaderboard, "
                                          "and choose a file first (Browse…).", parent=self)
            return
        if not os.path.exists(path):
            write_atomic(path, "")
        self._open_file(path)

    def _open_log(self):
        if not os.path.exists(LOG_FILE):
            messagebox.showinfo(APP_NAME, "No log yet — it starts filling once the tracker runs.",
                                parent=self)
            return
        self._open_file(LOG_FILE)

    def _open_file(self, path):
        try:
            os.startfile(path)  # Windows
        except AttributeError:
            subprocess.Popen(["xdg-open", path])

    def start(self, force_login=False):
        if self.worker and self.worker.is_alive():
            return
        self.worker = Worker(self)
        self.worker.force_login = force_login
        self.worker.start()
        self.run_btn.configure(text="Stop")
        ttk.Style(self).configure("Run.TButton", background=STOP, bordercolor=STOP)
        ttk.Style(self).map("Run.TButton", background=[("active", STOP)])

    def stop(self):
        if self.worker:
            self.worker.stop_event.set()

    def _toggle_run(self):
        if self.worker and self.worker.is_alive():
            self.stop()
            self.run_btn.configure(text="Stopping…")
        else:
            self.start()

    def _relogin(self):
        if not messagebox.askyesno(APP_NAME, "Sign out and sign in to Steam again?", parent=self):
            return
        self.stop()

        def wait_then_start():
            if self.worker and self.worker.is_alive():
                self.after(300, wait_then_start)
            else:
                self.start(force_login=True)
        wait_then_start()

    # ---- start with Windows ----
    def _set_startup(self):
        on = bool(self.startup_var.get())
        if on == bool(startup_entry()):
            return
        try:
            set_startup(on)
        except OSError as e:
            self._add_log(time.strftime("%H:%M:%S"), f"Couldn't change Start with Windows: {e}", "#FF8A7A")
            self.after_idle(lambda: self.startup_var.set(bool(startup_entry())))
            return
        self._add_log(time.strftime("%H:%M:%S"),
                      "Starts with Windows from now on." if on else "Won't start with Windows any more.",
                      "#5FE39B" if on else None)

    def _fix_startup_path(self):
        """The exe was moved since Start with Windows was switched on: point
        the entry at where it is now."""
        if not getattr(sys, "frozen", False):
            return  # running the .py: leave the exe's entry alone
        entry = startup_entry()
        if entry and entry != startup_command():
            try:
                set_startup(True)
            except OSError:
                pass

    # ---- tray ----
    def _maybe_to_tray(self, e):
        if e.widget is self and self.cfg.get("tray") and self.state() == "iconic":
            self.after(0, self._hide_to_tray)

    def _hide_to_tray(self):
        """Hide the window and show the tray icon. Needs pystray (in the exe);
        without it the window just stays minimised."""
        if self.tray is not None:
            return
        try:
            import pystray
            post = lambda what: self.events.put(("tray", what))
            running = lambda item: bool(self.worker and self.worker.is_alive())
            menu = pystray.Menu(
                pystray.MenuItem("Show", lambda: post("show"), default=True),
                pystray.MenuItem(lambda item: "Stop tracking" if running(item) else "Start tracking",
                                 lambda: post("toggle")),
                pystray.MenuItem("Quit", lambda: post("quit")))
            self.tray = pystray.Icon("wardogs_profit_tracker", tray_image(), APP_NAME, menu)
            self.tray.run_detached()
        except Exception as e:
            self.tray = None
            self._add_log(time.strftime("%H:%M:%S"), f"Couldn't make a tray icon ({e}) — "
                                                     "the window stays minimised instead.", "#FACC15")
            return
        self.withdraw()

    def _show_from_tray(self):
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def _short_link(self, i):
        """What the link looks like on the card (the full one is copied)."""
        return f"…/overlay/{HUD_NAME}?person={i + 1}"

    def _copy_overlay_link(self, i, label=None):
        link = overlay_link(self.hud_path, i + 1)
        self.clipboard_clear()
        self.clipboard_append(link)
        who = self.cfg["people"][i]["who"] or "you"
        self._add_log(time.strftime("%H:%M:%S"),
                      f"Copied the overlay link for {who} — in OBS add a Browser source, untick "
                      f"Local file and paste it as the URL: {link}", "#5FE39B")
        if label is not None:
            # show it worked, then put the link back
            label.configure(text="✓ Copied — paste it as an OBS Browser source URL", fg="#5FE39B")
            self.after(2000, lambda: label.winfo_exists() and
                       label.configure(text=self._short_link(i), fg=ACCENT))

    def _copy_board_link(self):
        link = overlay_link(self.board_path)
        self.clipboard_clear()
        self.clipboard_append(link)
        self._add_log(time.strftime("%H:%M:%S"),
                      f"Copied the leaderboard overlay link — in OBS add a Browser source, untick "
                      f"Local file and paste it as the URL: {link}", "#5FE39B")
        self.board_link.configure(text="✓ Copied — paste it as an OBS Browser source URL", fg="#5FE39B")
        self.after(2000, lambda: self.board_link.winfo_exists() and
                   self.board_link.configure(text=f"…/overlay/{BOARD_NAME}", fg=ACCENT))

    def _set_overlay(self, i, on, link_row, chips):
        self._set_person(i, overlay=bool(on))
        if on:
            link_row.pack(fill="x", pady=(6, 0), after=chips)
        else:
            link_row.pack_forget()
        self._update_overlay_server()
        self._refit_soon()

    def _update_overlay_server(self):
        """Run the number feed only while someone has Overlay on."""
        wanted = (any(p.get("overlay") for p in self.cfg["people"])
                  or (self.cfg["board_on"] and self.cfg["board_overlay"]))
        if wanted and self.overlay is None:
            try:
                self.overlay = OverlayServer(int(self.cfg["overlay_port"]), lambda: self.overlay_texts,
                                             lambda: self.overlay_board,
                                             lambda: {"icon": self.cfg["board_icon"],
                                                      "bg": self.cfg["overlay_bg_image"]},
                                             lambda page: overlay_style(self.cfg, page))
            except OSError as e:
                self.events.put(("log", time.strftime("%H:%M:%S"),
                                 f"Overlay feed couldn't start on port {self.cfg['overlay_port']} "
                                 f"({e}) — is another program using it?", "#FACC15"))
        elif not wanted and self.overlay is not None:
            self.overlay.close()
            self.overlay = None

    def _close(self):
        if getattr(self, "_save_job", None):
            self.after_cancel(self._save_job)  # saved below anyway
            self._save_job = None
        if getattr(self, "overlay", None) is not None:
            self.overlay.close()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        self.stop()
        save_settings(self.cfg)
        # hide now, but give the worker a few seconds to close the browser
        # so nothing is left running in the background
        self.withdraw()
        deadline = time.time() + 8

        def finish():
            if self.worker and self.worker.is_alive() and time.time() < deadline:
                self.after(100, finish)
            else:
                self.destroy()
        finish()

    # ---- display ----
    def color_for(self, text):
        if not self.cfg["color_on"] or not text:
            return TEXT
        return self.cfg["loss_color"] if text.startswith("-") else self.cfg["profit_color"]

    def respace(self, text):
        """Apply the current space setting to '+ $3,740' / '+$3,740'."""
        if text and text[0] in "+-":
            return text[0] + (" " if self.cfg["space"] else "") + text[1:].lstrip()
        return text

    def _show_value(self, text):
        text = self.respace(text)
        self.money_lbl.configure(text=text or "—", fg=self.color_for(text))
        results = self.current_people
        board = self.current_board if self.cfg.get("board_on") else None
        if len(results) > 1 or board is not None:
            # everyone, named in the app even when their name isn't in the files
            rows = [f"{r.get('label') or r['name']}:  {r['text'] or '—'}" for r in results]
            text_out = "Everyone:\n" + "\n".join(rows)
            if board is not None:
                text_out += "\n\nLeaderboard file:\n" + ("\n".join(board) or "(nobody with an amount yet)")
            self.obs_line_lbl.configure(text=text_out)
            self.obs_line_lbl.pack(fill="x", padx=16, pady=(2, 0), before=self.raw_lbl)
            return
        line = results[0]["line"] if results else text
        if line and line != text:
            self.obs_line_lbl.configure(text=f"OBS shows: {line}")
            self.obs_line_lbl.pack(fill="x", padx=16, pady=(2, 0), before=self.raw_lbl)
        else:
            self.obs_line_lbl.pack_forget()

    def _set_obs_pill(self, state):
        look = {"connected": ("● Connected", "#5FE39B"), "testing": ("● Testing…", "#FACC15"),
                "error": ("● Not connected", "#FF8A7A"), "waiting": ("● Waiting", MUTED),
                "disconnected": ("● Disconnected", MUTED), "notused": ("● Not used", MUTED)}[state]
        self.obs_pill.configure(text=look[0], fg=look[1])

    def _add_log(self, ts, msg, color):
        t = self.log_txt
        # only follow new lines if you're already looking at the bottom
        at_bottom = t.yview()[1] >= 0.999
        t.configure(state="normal")
        tag = None
        if color:
            tag = "c" + color.lstrip("#")
            t.tag_configure(tag, foreground=color)
        if t.index("end-1c") != "1.0":
            t.insert("end", "\n")
        t.insert("end", ts + "  ", "time")
        t.insert("end", msg, tag or ())
        # keep the log short: drop the oldest lines from the top
        lines = int(t.index("end-1c").split(".")[0])
        if lines > 300:
            t.delete("1.0", f"{lines - 300 + 1}.0")
        t.configure(state="disabled")
        if at_bottom:
            t.see("end")
        # also keep it on disk, so gaps and errors can be checked later
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d')} {ts}  {msg}\n")
        except Exception:
            pass

    def _pump(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self._add_log(ev[1], ev[2], ev[3])
                elif kind == "value":
                    _, text, raw, kept = ev
                    self.current_text = text  # drawn by the "people" update right after
                    self.raw_lbl.configure(text=f'Steam says: "{raw}"')
                    self.last_refresh = time.time()
                elif kind == "status":
                    self.status_lbl.configure(text=ev[1])
                    self.status_dot.configure(fg=ev[2])
                elif kind == "obs":
                    # Test / Connect results always show; the tracker's only while it's
                    # actually using OBS (a late one mustn't undo "Not used" / "Disconnected")
                    if len(ev) > 2 or (self.cfg["obs_connect"] and self.cfg["color_on"]):
                        self._set_obs_pill(ev[1])
                elif kind == "people":
                    self.current_people, self.current_board = ev[1], ev[2]
                    self._show_value(self.current_text)
                elif kind == "friends":
                    _, friends, me = ev
                    self.friends = friends
                    self.avatar_urls = {name.lower(): url for name, url in friends}
                    self.avatar_urls[""] = me.get("avatar", "")
                    self._paint_people()
                    if self.picker is not None:
                        self.picker.fill()
                elif kind == "avatar":
                    _, url, pic = ev
                    if pic is None:
                        # didn't download (offline?): allow another try in a minute
                        self.after(60000, lambda u=url: self.avatar_imgs.pop(u, None)
                                   if self.avatar_imgs.get(u) is None else None)
                    else:
                        try:
                            from PIL import ImageTk
                            self.avatar_imgs[url] = ImageTk.PhotoImage(pic)
                        except Exception:
                            pass
                        self._paint_people()
                        if self.picker is not None:
                            self.picker.repaint_avatars()
                elif kind == "tray":
                    if ev[1] == "show":
                        self._show_from_tray()
                    elif ev[1] == "toggle":
                        self._toggle_run()
                        if self.tray is not None:
                            self.tray.update_menu()
                    elif ev[1] == "quit":
                        self._close()
                elif kind == "stopped":
                    if ev[1] is not self.worker:
                        continue  # an older run finishing after a new one started
                    self.run_btn.configure(text="Start")
                    ttk.Style(self).configure("Run.TButton", background=START, bordercolor=START)
                    ttk.Style(self).map("Run.TButton", background=[("active", START)])
                    self.status_lbl.configure(text="Stopped")
                    self.status_dot.configure(fg=MUTED)
        except queue.Empty:
            pass
        self.after(150, self._pump)

    def _tick(self):
        if show_signal_set(self._show_signal):
            self._show_from_tray()
        if self.last_refresh and self.worker and self.worker.is_alive() \
                and self.status_lbl.cget("text").startswith("Signed in"):
            secs = int(time.time() - self.last_refresh)
            self.status_lbl.configure(text=f"Signed in · refreshed {secs}s ago")
        self.after(1000, self._tick)


_instance_lock = None  # kept alive for as long as the app runs


def already_running():
    """True if another copy of the app is open. Uses a Windows named mutex,
    which Windows releases by itself when the app closes (even on a crash)."""
    global _instance_lock
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        ERROR_ALREADY_EXISTS = 183
        # use_last_error makes ctypes save the error code right after the call
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        _instance_lock = k32.CreateMutexW(None, False, "Local\\WardogsProfitTracker_SingleInstance")
        return ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    # other systems: an exclusive lock on a file
    import fcntl
    _instance_lock = open(os.path.join(DATA_DIR, ".wardogs.lock"), "w")
    try:
        fcntl.flock(_instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True


def trim_log_file():
    """Keeps activity_log.txt from growing forever: drops the oldest half."""
    try:
        if os.path.getsize(LOG_FILE) > LOG_FILE_MAX:
            with open(LOG_FILE, "rb") as f:
                f.seek(-LOG_FILE_MAX // 2, os.SEEK_END)
                tail = f.read()
            tail = tail[tail.find(b"\n") + 1:]  # start on a whole line
            with open(LOG_FILE, "wb") as f:
                f.write(tail)
    except OSError:
        pass


def disable_power_throttling():
    """Asks Windows not to slow this app down while it's in the background
    (EcoQoS / power throttling), so refreshes keep coming on time."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class _Throttling(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]

        EXECUTION_SPEED, IGNORE_TIMER_RESOLUTION = 0x1, 0x4
        ProcessPowerThrottling = 4
        # control these two, state off = never throttle
        info = _Throttling(1, EXECUTION_SPEED | IGNORE_TIMER_RESOLUTION, 0)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wintypes.DWORD]
        k32.SetProcessInformation(k32.GetCurrentProcess(), ProcessPowerThrottling,
                                  ctypes.byref(info), ctypes.sizeof(info))
    except Exception:
        pass  # older Windows: nothing to turn off


SHOW_EVENT = "Local\\WardogsProfitTracker_Show"


# ---------------- start with Windows ----------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_NAME = APP_NAME          # the value's name under RUN_KEY
STARTUP_ARG = "--startup"        # how the app knows Windows started it


def startup_command():
    """What Windows runs at sign-in: this exe (or pythonw + this script)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {STARTUP_ARG}'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return f'"{pyw if os.path.exists(pyw) else exe}" "{os.path.abspath(__file__)}" {STARTUP_ARG}'


def startup_entry():
    """The command registered to run at sign-in, or "" when there's none."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return str(winreg.QueryValueEx(k, STARTUP_NAME)[0])
    except (OSError, ImportError):
        return ""


def set_startup(on):
    """Add or remove the sign-in entry (this user only, no admin needed)."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, STARTUP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(k, STARTUP_NAME)
            except FileNotFoundError:
                pass


def make_show_signal():
    """A named Windows event a second copy of the exe sets to say "show the
    window" (works even while it's hidden in the tray)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        return k32.CreateEventW(None, False, False, SHOW_EVENT)  # auto-reset, not set
    except Exception:
        return None


def show_signal_set(handle):
    """True once each time a second copy asked to show the window."""
    if not handle:
        return False
    global _WAIT
    if _WAIT is None:
        import ctypes
        from ctypes import wintypes
        _WAIT = ctypes.WinDLL("kernel32", use_last_error=True).WaitForSingleObject
        _WAIT.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    return _WAIT(handle, 0) == 0  # WAIT_OBJECT_0


_WAIT = None


def focus_existing_window():
    """Brings the already-open window to the front (also out of the tray)."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenEventW.restype = wintypes.HANDLE
    k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    EVENT_MODIFY_STATE = 0x0002
    ev = k32.OpenEventW(EVENT_MODIFY_STATE, False, SHOW_EVENT)
    if ev:
        k32.SetEvent(wintypes.HANDLE(ev))  # the running copy shows itself within a second
        k32.CloseHandle(wintypes.HANDLE(ev))
        return True
    u32 = ctypes.windll.user32
    hwnd = u32.FindWindowW(None, APP_NAME)
    if not hwnd:
        return False
    SW_RESTORE = 9
    u32.ShowWindow(hwnd, SW_RESTORE)
    u32.SetForegroundWindow(hwnd)
    return True


if __name__ == "__main__":
    if already_running():
        if not focus_existing_window():
            r = tk.Tk()
            r.withdraw()
            messagebox.showinfo(APP_NAME, f"{APP_NAME} is already open.")
            r.destroy()
        sys.exit(0)
    disable_power_throttling()
    trim_log_file()
    app = App()
    app._fix_startup_path()
    if STARTUP_ARG in sys.argv[1:]:
        # started by Windows at sign-in: out of the way (tracking starts as usual)
        app.after(300, app._hide_to_tray if app.cfg.get("tray") else app.iconify)
    app.mainloop()
