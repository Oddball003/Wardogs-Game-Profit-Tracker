"""
Wardogs Profit Tracker
Reads WARDOGS profit / loss live from the Steam friends page (in a hidden,
signed-in browser) for you and any friends you pick, writes each amount to
text files for OBS (a separate file per person and/or a shared leaderboard),
and can colour each person's OBS Text source green / red through OBS WebSocket.
"""
import copy
import hmac
import json
import os
import queue
import re
import secrets
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
# Twitch name: their linked Twitch account, or their Steam name until they're linked
NAME_MODES = {"steam": "Steam name", "twitch": "Twitch name", "custom": "Custom…"}
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
    // each friend's Steam account number, from the page's own data: it never
    // changes, so it finds them again after a name change (0 if not found)
    const acctOf = e => {
        const k = Object.keys(e).find(k => k.startsWith('__reactFiber'));
        let f = k ? e[k] : null;
        for (let d = 0; f && d < 8; d++, f = f.return) {
            const fr = f.memoizedProps && f.memoizedProps.friend;
            if (fr && fr.m_unAccountID) return Number(fr.m_unAccountID) || 0;
        }
        return 0;
    };
    // read each friend once: name, status (e.g. "WARDOGS +$32,622 Profit")
    const list = [...document.querySelectorAll('.friend')].map(e => {
        const t = texts(e);
        return {e, name: t[0] || '', key: (t[0] || '').toLowerCase(), acct: acctOf(e),
                status: t.slice(1).filter(s => s.length > 1).join(' ')};
    });
    // everyone on the list, for the picker: name -> profile picture
    const seen = new Map();
    for (const f of list) {
        if (f.name && !seen.get(f.name)) seen.set(f.name, avatarOf(f.e));
    }
    const friends = [...seen].sort((a, b) => a[0].localeCompare(b[0]));
    // friends in WARDOGS right now (for !wardogs friends), each once
    const inGame = st => /wardogs/i.test(st) || /\$\s?\d/.test(st);
    const ingame = [], had = new Set();
    for (const f of list) {
        // Steam often shows just the amount ("+$22,581 Profit"), without the game's name
        if (f.name && inGame(f.status) && !had.has(f.key)) { had.add(f.key); ingame.push([f.name, f.status, f.acct]); }
    }
    const mine = me.innerText.split('\n').map(x => x.trim()).filter(Boolean);
    const people = whos.map(w => {
        // a name, or [name, account number] (the number wins when it's known)
        const who = Array.isArray(w) ? w[0] : w, acct = Array.isArray(w) ? Number(w[1]) || 0 : 0;
        if (!who) return {status: mine.slice(1).join(' '), name: mine[0] || 'Me', avatar: avatarOf(me)};
        const want = who.trim().toLowerCase();
        let hits = acct ? list.filter(f => f.acct === acct) : [];
        if (!hits.length) hits = list.filter(f => f.key === want);
        // the same friend can be listed twice (favorites + group): prefer one with an amount
        const best = hits.find(f => f.status.includes('$')) || hits[0];
        return best ? {status: best.status, name: best.name, avatar: avatarOf(best.e), acct: best.acct}
                    : {status: null, name: who.trim(), avatar: ''};
    });
    return {people, friends, ingame, me: {name: mine[0] || '', avatar: avatarOf(me)},
            meStatus: mine.slice(1).join(' ')};
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
    "sb_open": True,                  # Advanced › Streamer.bot unfolded
    "ov_open": True,                  # Advanced › Browser overlays unfolded
    "folders_on": False,              # Advanced › Friend folders
    "folders": [],                    # your own folder names (people list theirs in "folders")
    "folders_closed": [],             # folders folded shut (incl. "Active" / "Inactive")
    "folders_pinned": [],             # your folders whose people stay on the leaderboard after a save
    "overlays_hidden": False,         # hide every overlay (nothing on stream) until shown again
    "overlays_were_on": [],           # which overlays "all off" turned off, for "all on"
    # Streamer.bot: it controls the tracker over 127.0.0.1 with this key ...
    "sb_on": False,
    "api_key": "",
    # ... and the tracker tells it when a game ends (Streamer.bot's HTTP server)
    "sb_send": False,
    "sb_url": "http://127.0.0.1:7474",
    "sb_action": "Wardogs Game Over",
    # saving the leaderboard (end of a game)
    "save_on_game_over": False,
    "board_clear": True,              # (always on now) after saving: everyone but you and the pinned off
    "board_teams": False,             # teams: a badge before each name on the leaderboard overlay
    "team_pics": {},                  # your own badge picture per team ({"lonestar": path, ...})
    "team_colors": {},                # your own colour per team ({"valkyra": "#FF00FF"}); others use TEAMS
    "team_names_tinted": True,        # leaderboard names in their team's colour
    "twitch_names": False,            # people go by their Twitch name (Steam name until linked)
    "board_pin_default": False,       # 📌 Pin by default: anyone put on the leaderboard is pinned
    # Keep last amount, per output (set in the Output editor; first time: from "keep_last")
    "keep_last_hud": True,            # the money HUD overlay
    "keep_last_board": True,          # the leaderboard overlay (and Preview)
    "keep_last_board_file": True,     # the leaderboard's text file
    "keep_last_files": True,          # each person's own text file, the app's number, OBS colouring
    "preview_full": False,            # the Preview shows everyone, whatever How many is
    "save_hotkey_on": True,
    "save_hotkey": "Ctrl+Alt+S",
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
                        "twitch": twitch_login(p.get("twitch", "")),
                        "steam_acct": p["steam_acct"] if isinstance(p.get("steam_acct"), int)
                        and not isinstance(p.get("steam_acct"), bool) and 0 < p["steam_acct"] < 2 ** 32 else 0,
                        "board_keep": bool(p.get("board_keep", False)),
                        "team": p.get("team") if p.get("team") in TEAM_NAMES else "",
                        "twitch_by": str(p.get("twitch_by", ""))[:40] if p.get("twitch") else "",
                        "twitch_at": str(p.get("twitch_at", ""))[:20] if p.get("twitch") else "",
                        "folders": [str(f) for f in p.get("folders", []) if isinstance(f, str) and f.strip()]
                        if isinstance(p.get("folders"), list) else [],
                        "_named": named})  # only used to carry old settings over
    return out or [dict(new_person(), own_file=True, board=False)]


def in_wardogs(status):
    """A Steam status that means they're in WARDOGS: the game's name, or an
    amount on its own ("+$22,581 Profit" — Steam often leaves the name out)."""
    s = str(status or "")
    return "wardogs" in s.lower() or bool(MONEY.search(s))


def crown_image(size=16, color=(0xD4, 0xAF, 0x37)):
    """The leaderboard overlay's gold crown (same shape as its SVG), as a picture."""
    from PIL import Image, ImageDraw
    k = 8
    im = Image.new("RGBA", (24 * k, 24 * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    pts = [(2.6, 18.4), (21.4, 18.4), (23, 6.8), (17.5, 11.2), (12, 3.6), (6.5, 11.2), (1, 6.8)]
    d.polygon([(x * k, y * k) for x, y in pts], fill=color)
    d.rounded_rectangle((2.6 * k, 19.6 * k, 21.4 * k, 22 * k), radius=int(0.6 * k), fill=color)
    return im.resize((size, size), Image.LANCZOS)


def browser_left_open(profile):
    """True if a browser window from before still uses this profile (the tracker
    was closed without closing it, e.g. ended in Task Manager). A new window
    would only be handed to that one and our process would end at once."""
    lock = os.path.join(profile, "lockfile")
    if not os.path.exists(lock):
        return False
    try:
        os.remove(lock)   # left behind by a browser that's gone: harmless
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def close_browser_using(profile):
    """Close the browser windows that use this profile (only ours: the path is ours)."""
    env = dict(os.environ, WD_PROFILE=profile)
    script = ("$p = $env:WD_PROFILE; Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" | "
              "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($p) } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                       capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass
    for _ in range(20):   # let it let go of the profile
        if not browser_left_open(profile):
            return
        time.sleep(0.1)


def bring_to_front(title):
    """Show the (largest) window with this title in front; False if there's none."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    best = [None, 0]

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def each(h, _):
        buf = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(h, buf, 256)
        if buf.value == title and u.IsWindowVisible(h):
            r = wintypes.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > best[1]:
                best[:] = [h, area]
        return True
    u.EnumWindows(each, 0)
    if not best[0]:
        return False
    u.ShowWindow(best[0], 9)   # restore if minimised
    u.SetForegroundWindow(best[0])
    return True


def preview_browser():
    """Edge (or Chrome) to show the leaderboard preview in its own small window."""
    import winreg
    for name in ("msedge.exe", "chrome.exe"):
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, "Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + name) as k:
                    exe = winreg.QueryValueEx(k, "")[0]
                if exe and os.path.exists(exe):
                    return exe
            except OSError:
                pass
    for exe in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(exe):
            return exe
    return ""


def twitch_login(text):
    """'https://www.twitch.tv/RotorBird/', '@RotorBird' or 'RotorBird' -> 'rotorbird'
    ("" when it isn't a Twitch name)."""
    t = str(text or "").strip().lower()
    t = re.sub(r"^(https?://)?(www\.|m\.)?twitch\.tv/", "", t).lstrip("@").split("/")[0].split("?")[0]
    return t if re.fullmatch(r"[a-z0-9_]{1,25}", t) else ""


def new_person(who="", cfg=None):
    """A person added with + Add person: on the leaderboard, no file of their own
    (going by their Twitch name when Advanced › Twitch names by default is on)."""
    mode = "twitch" if (cfg or {}).get("twitch_names") else "steam"
    pin = bool((cfg or {}).get("board_pin_default"))   # 📌 Pin by default (they start on the leaderboard)
    return {"who": who, "name_mode": mode, "label": "", "own_file": False,
            "out_file": "", "file_name": False, "board": True, "obs_source": "",
            "overlay": False, "folders": [], "twitch": "", "twitch_by": "", "twitch_at": "",
            "steam_acct": 0, "board_keep": pin, "team": ""}


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


def board_pinned(p, cfg):
    """Pinned to the leaderboard (📌 on their card, or in a pinned folder) — not you."""
    if not p.get("who"):
        return False
    if p.get("board_keep"):
        return True
    if not cfg.get("folders_on"):
        return False
    pinned = cfg.get("folders_pinned", [])
    return "Active" in pinned or any(f in pinned for f in p.get("folders", []))


def leaderboard_rows(people, results, cfg, which="board_text"):
    """Everyone ticked "Leaderboard" who has an amount right now, highest
    first (or as listed). Pinned players only while they're in a game (not
    their kept amount from before). which: the amount to use — the overlay's
    ("board_text") or the text file's ("board_file_text"), each with its own
    Keep last amount (results without them just use "text")."""
    rows = []
    for p, r in zip(people, results):
        text = r.get(which, r["text"])
        if p["board"] and text and not (board_pinned(p, cfg) and not r.get("live", True)):
            rows.append(r if text == r["text"] else dict(r, text=text))
    if cfg.get("board_order", "amount") == "amount":
        rows.sort(key=lambda r: amount_value(r["text"]), reverse=True)  # stable on ties
    top = cfg.get("board_top", 0)
    return rows[:top] if isinstance(top, int) and top > 0 else rows


def leaderboard_lines(people, results, cfg):
    """The leaderboard file's lines, optionally numbered and named."""
    rows = leaderboard_rows(people, results, cfg, "board_file_text")
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
    # folders: names kept tidy, people only in folders that exist
    names = []
    for f in s.get("folders", []) if isinstance(s.get("folders"), list) else []:
        if not isinstance(f, str):
            continue
        f = f.strip()
        if f and f.lower() not in [n.lower() for n in names] + [a.lower() for a in AUTO_FOLDERS]:
            names.append(f)
    s["folders"] = names
    for person in s["people"]:
        person["folders"] = [f for f in names if f in person.get("folders", [])]
    pics = s.get("team_pics") if isinstance(s.get("team_pics"), dict) else {}
    s["team_pics"] = {t: v for t, v in pics.items() if t in TEAM_NAMES and isinstance(v, str) and v}
    for k in KEEP_LAST_KEYS:   # new per-output switches start as the old single one was
        if k not in saved:
            s[k] = bool(s.get("keep_last", True))
    tcs = s.get("team_colors") if isinstance(s.get("team_colors"), dict) else {}
    s["team_colors"] = {t: v.upper() for t, v in tcs.items()
                        if t in TEAM_NAMES and isinstance(v, str) and HEX_COLOR.fullmatch(v)}
    if not isinstance(s.get("folders_closed"), list):
        s["folders_closed"] = []
    pinned = s.get("folders_pinned") if isinstance(s.get("folders_pinned"), list) else []
    s["folders_pinned"] = [f for f in ["Active"] + names if f in pinned]
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
    """The name this person goes by in the files: your custom label, their
    Twitch name, or their Steam name (also when Custom is left empty, or
    Twitch name is picked but they aren't linked)."""
    mode = person.get("name_mode")
    if mode == "custom" and person.get("label", "").strip():
        return person["label"].strip()
    if mode == "twitch" and person.get("twitch"):
        return person["twitch"]
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
EDITOR_NAME = "editor.html"   # the Output editor (served by the tracker, never put in OBS)


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
# WARDOGS' three factions. Badges are the faction emblems (overlay/teams, BULKHEAD /
# Team17's artwork), or your own picture; a shield in the team colour if neither loads.
TEAMS = (("lonestar", "Lonestar", "#3A4BE6"), ("manticore", "Manticore", "#34A835"),
         ("valkyra", "Valkyra", "#D62B24"))   # the game's team colours (from the faction emblems)
TEAM_NAMES = {t: n for t, n, _ in TEAMS}


def parse_team(text):
    """'valk', 'Valkyra', 'LONESTAR' -> the team's id; 'none' / 'clear' -> "";
    None when it isn't a team (or could be more than one)."""
    t = str(text or "").strip().lower()
    if t in ("none", "clear", "off", "no", "-"):
        return ""
    hits = [tid for tid, name, _ in TEAMS if len(t) >= 2 and (tid.startswith(t) or t.startswith(tid))]
    return hits[0] if len(hits) == 1 else None


TEAM_DEFAULT_COLORS = {t: c for t, _, c in TEAMS}
KEEP_LAST_KEYS = ("keep_last_hud", "keep_last_board", "keep_last_board_file", "keep_last_files")

# the Output editor's test players: (name, team, where they hover, how far they swing)
DEMO_PLAYERS = (("Your Chopper Pilot", "lonestar", 16000, 5000), ("PilotAndy", "valkyra", 14000, 6000),
                ("Spandy Pandy", "lonestar", 12500, 4000), ("RotorBird", "valkyra", 11000, 7000),
                ("Oddball", "lonestar", 9000, 6500), ("Tailspin", "manticore", 7000, 5000),
                ("noobiewithboobies", "manticore", 4500, 5500), ("Freaky Otter", "manticore", 2500, 6000),
                ("Pronothin", "valkyra", 500, 4500), ("husky", "lonestar", -1500, 4000))
DEMO_ME = 4   # the money HUD in the editor shows Oddball's amount


def demo_texts(now=None, space=False):
    """The test players' amounts right now: each drifts up and down on its own
    (about a 20 s cycle), so places keep swapping and some go into a loss."""
    import math
    t = time.time() if now is None else now
    out = []
    for k, (_, _, base, swing) in enumerate(DEMO_PLAYERS):
        v = int(round((base + swing * math.sin(t / (3.0 + k * 0.37) + k * 1.7)) / 10.0)) * 10
        out.append(("-" if v < 0 else "+") + (" " if space else "") + f"${abs(v):,}")
    return out


def _demo_people(cfg, now=None):
    texts = demo_texts(now, cfg.get("space", False))
    people = [{"who": name, "board": True} for name, *_ in DEMO_PLAYERS]
    results = [{"text": texts[k], "label": name, "team": team} for k, (name, team, _, _) in enumerate(DEMO_PLAYERS)]
    return people, results


def demo_board(cfg, now=None):
    """The leaderboard overlay with the test players, using your settings (order,
    How many, names, places, teams, 1st place, colours)."""
    people, results = _demo_people(cfg, now)
    return board_payload(leaderboard_rows(people, results, cfg), cfg)


def demo_file(cfg, now=None):
    """What the leaderboard text file would say for the test players."""
    people, results = _demo_people(cfg, now)
    return leaderboard_lines(people, results, cfg)


def team_color(team, cfg=None):
    """A team's colour: the one you picked, else the game's."""
    mine = (cfg or {}).get("team_colors", {}).get(team, "")
    return mine if HEX_COLOR.fullmatch(mine or "") else TEAM_DEFAULT_COLORS.get(team, "#5C6575")


def team_badge(team, size=18, cfg=None):
    """The shield badge for a team, as a picture (the overlay draws the same one)."""
    from PIL import Image, ImageDraw, ImageFont
    color = team_color(team, cfg)
    k = 8
    im = Image.new("RGBA", (24 * k, 24 * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    shield = [(12, 1.5), (21, 4.5), (21, 11), (19.6, 15.6), (16.6, 19.4), (12, 22.5),
              (7.4, 19.4), (4.4, 15.6), (3, 11), (3, 4.5)]
    d.polygon([(x * k, y * k) for x, y in shield], fill=color)
    try:
        font = ImageFont.truetype("segoeuib.ttf", 13 * k)
    except OSError:
        font = ImageFont.load_default()
    d.text((12 * k, 12.2 * k), TEAM_NAMES.get(team, "?")[0], fill=(17, 21, 28, 255), font=font, anchor="mm")
    return im.resize((size, size), Image.LANCZOS)


def team_pic(team, cfg):
    """A team's badge picture: yours if you set one, else the faction emblem
    that ships with the app ("" if neither is there)."""
    mine = cfg.get("team_pics", {}).get(team, "")
    if mine and _recently(("exists", mine), lambda: os.path.exists(mine)):
        return mine
    emblem = os.path.join(getattr(sys, "_MEIPASS", HERE), "overlay", "teams", f"{team}.webp")
    return emblem if _recently(("exists", emblem), lambda: os.path.exists(emblem), ttl=60) else ""


def data_url(path):
    """A picture as a data: URL, so a page can show it without asking the tracker."""
    import base64
    ctype = ICON_TYPES.get(os.path.splitext(path or "")[1].lower())
    try:
        if not ctype or os.path.getsize(path) > 8 * 1024 * 1024:
            return ""
        with open(path, "rb") as f:
            return f"data:{ctype};base64," + base64.b64encode(f.read()).decode()
    except OSError:
        return ""


def board_payload(rows, cfg):
    """What the leaderboard overlay shows, from leaderboard rows."""
    teams = bool(cfg.get("board_teams"))
    pics = {t: team_pic(t, cfg) for t, _, _ in TEAMS} if teams else {}
    return {"rows": [{"name": r["label"], "text": r["text"], "team": r.get("team", "") if teams else ""}
                     for r in rows],
            "ranks": bool(cfg["board_ranks"]), "names": bool(cfg["board_names"]), "first": cfg["board_first"],
            "icon": icon_version(cfg["board_icon"]) if cfg["board_first"] == "image" else "",
            "teams": teams, "teamPics": {t: icon_version(pics.get(t, "")) for t, _, _ in TEAMS if pics.get(t)},
            "teamColors": {t: team_color(t, cfg) for t, _, _ in TEAMS} if teams else {},
            "teamNames": teams and bool(cfg.get("team_names_tinted", True))}
AUTO_FOLDERS = ("Active", "Inactive")   # kept up to date by the tracker
OVERLAY_SIZE_MIN, OVERLAY_SIZE_MAX = 10, 72


BOX_MAX = 3840
# fonts the overlays load from Google Fonts (free; OBS needs internet when it
# starts the page). Any font installed on this PC works too.
WEB_FONTS = ("Barlow", "Barlow Condensed", "Barlow Semi Condensed", "Rajdhani", "Oswald", "Teko",
             "Saira", "Saira Condensed", "Chakra Petch", "Exo 2", "Orbitron", "Russo One",
             "Black Ops One", "Bebas Neue", "Anton", "Roboto", "Roboto Condensed", "Roboto Mono",
             "Share Tech Mono", "Inter")
FONT_SEPARATOR = "──── installed on this PC ────"
STEAM64_BASE = 76561197960265728   # Steam ID = this + the account number


def overlay_style(cfg, page="hud"):
    """The look the overlay pages apply (sent with every answer, so a change
    shows within a moment)."""
    image = cfg["overlay_bg_mode"] == "image" and icon_version(cfg["overlay_bg_image"]) or ""
    pl = cfg["overlay_pl_board"] if page == "board" else cfg["overlay_pl"]
    font = cfg["overlay_font"].strip() or "Barlow"
    return {"font": font, "webFont": font in WEB_FONTS, "hidden": bool(cfg.get("overlays_hidden")),
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


_recent = {}   # (what, path) -> (when, answer): file checks reused for a moment


def _recently(key, work, ttl=0.5):
    """The overlays ask ~10 times a second each: look at the disk at most
    every `ttl` seconds per file instead of on every answer."""
    now = time.monotonic()
    hit = _recent.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    value = work()
    _recent[key] = (now, value)
    return value


def icon_version(path):
    """Changes whenever the picture does, so the overlay loads the new one."""
    def stat():
        try:
            st = os.stat(path)
            return f"{int(st.st_mtime)}-{st.st_size}"
        except (OSError, TypeError, ValueError):
            return ""
    return _recently(("ver", path), stat) if isinstance(path, str) and path else stat()


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

    def __init__(self, port, texts, board=lambda: {"rows": []}, files=lambda: {}, style=lambda page: None,
                 api=lambda cmd, params: (404, {"ok": False, "error": "not available"}),
                 demo=lambda kind: None, editor=None):
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

            def do_POST(self):
                self.do_GET()

            def do_GET(self):
                url = urlparse(self.path)
                if editor is not None and (url.path == "/editor" or url.path.startswith("/editor/")):
                    # the Output editor (a page this server hands out, on this PC only)
                    body = b""
                    if self.command == "POST":
                        try:
                            n = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
                        except ValueError:
                            n = 0
                        body = self.rfile.read(n) if n > 0 else b""
                    params = {k: v[0] for k, v in parse_qs(url.query).items()}
                    try:
                        code, ctype, out = editor(self.command, url.path, params, self.headers, body)
                    except Exception as e:
                        code, ctype, out = 500, "application/json", json.dumps({"ok": False, "error": str(e)}).encode()
                    self._send(code, out, ctype)
                    return
                if url.path.startswith("/api/") or url.path == "/api":
                    # Streamer.bot (or anything else on this PC that has the key)
                    params = {k: v[0] for k, v in parse_qs(url.query).items()}
                    as_text = params.pop("format", "").lower() == "text"
                    try:
                        code, out = api(url.path[5:].strip("/").lower(), params)
                    except Exception as e:
                        code, out = 500, {"ok": False, "error": str(e)}
                    if as_text:
                        # &format=text: a line ready to whisper / post (always 200,
                        # so Streamer.bot keeps it in its variable)
                        msg = out.get("text") or out.get("error") or ("Done." if out.get("ok") else "")
                        self._send(200, msg.encode("utf-8"), "text/plain; charset=utf-8")
                    else:
                        self._send(code, json.dumps(out).encode(), "application/json")
                elif url.path == "/value":
                    try:
                        n = int(parse_qs(url.query).get("person", ["1"])[0])
                    except ValueError:
                        n = 1
                    if parse_qs(url.query).get("demo", [""])[0] == "1":
                        text = demo("hud") or ""      # the Output editor's test amount
                    else:
                        t = texts()
                        text = t[n - 1] if 1 <= n <= len(t) else ""
                    self._send(200, json.dumps({"text": text, "style": style("hud")}).encode(), "application/json")
                elif url.path == "/board":
                    q = parse_qs(url.query)
                    full = q.get("full", [""])[0] == "1"
                    if q.get("demo", [""])[0] == "1":
                        data = demo("board") or {"rows": []}   # the Output editor's test players
                    else:
                        data = board(True) if full else board()
                    self._send(200, json.dumps(dict(data, style=style("board"))).encode(), "application/json")
                elif url.path in ("/icon", "/bg") or url.path.startswith("/team/"):
                    # your pictures (1st place, background, team badges), from wherever they are
                    path = files().get(url.path[1:].replace("/", "_"), "")
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


# Edge signs every new profile in to the Windows Microsoft account and turns on
# sync unless this feature is off. Our profiles are only for Steam / the preview.
EDGE_NO_SIGNIN = "msImplicitSignin"
# Playwright's own list, in case its copy can't be read (see playwright_disabled_features)
PLAYWRIGHT_FEATURES = ("AvoidUnnecessaryBeforeUnloadCheckSync", "DestroyProfileOnBrowserClose",
                       "DialMediaRouteProvider", "GlobalMediaControls", "HttpsUpgrades", "LensOverlay",
                       "MediaRouter", "PaintHolding", "ThirdPartyStoragePartitioning",
                       "BlockOriginHeaderModificationOnRedirect", "AvoidCorsURLLoaderRestartOnRedirect",
                       "Translate", "AutoDeElevate", "OptimizationHints", "NetworkTimeServiceQuerying",
                       "AimEnabled", "msForceBrowserSignIn", "msEdgeUpdateLaunchServicesPreferredVersion")


def playwright_disabled_features():
    """The features Playwright switches off itself. A second --disable-features
    replaces the first (the browser doesn't merge them), so ours repeats these."""
    try:
        import playwright
        lib = os.path.join(os.path.dirname(playwright.__file__), "driver", "package", "lib")
        for name in ("coreBundle.js", os.path.join("server", "chromium", "chromiumSwitches.js")):
            path = os.path.join(lib, name)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    m = re.search(r"disabledFeatures = \[(.*?)\]", f.read(), re.S)
                found = re.findall(r'^\s*"(\w+)"', m.group(1), re.M) if m else []
                if found:
                    return found
    except Exception:
        pass
    return list(PLAYWRIGHT_FEATURES)


def edge_signed_in(profile_dir):
    """True if Edge signed this profile in to a Microsoft account."""
    try:
        with open(os.path.join(profile_dir, "Default", "Preferences"), encoding="utf-8") as f:
            return any(a.get("email") for a in json.load(f).get("account_info") or [])
    except Exception:
        return False


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
    features = ",".join(playwright_disabled_features() + [EDGE_NO_SIGNIN])
    for name, extra in attempts:
        try:
            ctx = pw.chromium.launch_persistent_context(PROFILE_DIR, headless=headless,
                                                        args=[f"--disable-features={features}"], **extra)
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
        self.reload_now = False  # Streamer.bot asked for a one-time refresh
        self.zero_now = False    # 🧹 Reset: forget everyone's kept amount

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
        accts = list(getattr(self, "accts", []))
        arg = [[w, accts[k] if k < len(accts) else 0] for k, w in enumerate(whos)]
        res = self.page.locator(".currentUserContainer").evaluate(
            FRIENDS_JS, arg, timeout=CHAT_TIMEOUT * 1000)
        self.friends, self.me = res["friends"], res["me"]
        # you first (when you're in WARDOGS), then friends in WARDOGS
        me = [[res["me"]["name"] or "You", res.get("meStatus", ""), 0]] \
            if in_wardogs(res.get("meStatus", "")) else []
        self.ingame = me + [list(x) for x in res.get("ingame", [])]
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
        self.ingame, sent_ingame = [], None
        sent_acct = {}
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
                    if self.reload_now or (cfg["reload_on"] and time.time() - self.page_opened > reload_every):
                        if self.reload_now:
                            self.log("Refreshing the friends page (asked once).")
                        self.reload_now = False
                        # optional fresh page, in case its connection went stale
                        self.page.reload(wait_until="domcontentloaded", timeout=30000)
                        self.page.locator(".currentUserContainer").wait_for(timeout=30000)
                        self.page.wait_for_timeout(2000)
                        self.page_opened = time.time()
                    self.accts = [p.get("steam_acct", 0) for p in people]
                    found = self.read_people([p["who"] for p in people])
                    # a friend's account number, first seen or after a name change: tell the window
                    for k, (p, f) in enumerate(zip(people, found)):
                        acct = f.get("acct") or 0
                        if p["who"] and acct and (acct != p.get("steam_acct") or
                                                  f["name"].lower() != p["who"].lower()):
                            if sent_acct.get(k) != (p["who"], acct, f["name"]):
                                sent_acct[k] = (p["who"], acct, f["name"])
                                self.emit("acct", k, p["who"], acct, f["name"])
                    landed_on_login = "/login" in self.page.url
                    chat_fails = 0
                    if (self.friends, self.me) != sent_friends:
                        self.emit("friends", [list(f) for f in self.friends], dict(self.me))
                        sent_friends = ([list(f) for f in self.friends], dict(self.me))
                    if self.ingame != sent_ingame:
                        self.emit("ingame", [list(x) for x in self.ingame])
                        sent_ingame = [list(x) for x in self.ingame]
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
                if self.zero_now:   # 🧹 Reset: amounts back to zero (live money comes back on its own)
                    self.zero_now = False
                    for s_ in state:
                        if s_ is not None:
                            s_["last_text"] = ""
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
                    # end of a game: your amount was showing, then gone for a while
                    if i == 0:
                        if text:
                            st["in_game"], st["gone_at"], st["final"] = True, None, text
                        elif st.get("in_game"):
                            st["gone_at"] = st.get("gone_at") or time.time()
                            if time.time() - st["gone_at"] >= GAME_OVER_SECS:
                                st["in_game"] = False
                                self.emit("game_over", st["final"])
                    # Keep last amount is per output: the HUD, the leaderboard overlay,
                    # the leaderboard's text file, and people's own files (+ the app)
                    live = text
                    last = ""
                    if not live and st["last_text"]:
                        # the last amount, with the current space setting
                        sign, body = st["last_text"][0], st["last_text"][1:].lstrip()
                        last = sign + (" " if cfg["space"] else "") + body
                    pick = lambda key: live or (last if cfg.get(key, cfg["keep_last"]) else "")
                    text = pick("keep_last_files")
                    kept = bool(text) and not live
                    if live:
                        st["last_text"] = live
                    label = person_label(p, f["name"])
                    results.append({"text": text, "raw": raw, "kept": kept, "label": label,
                                    "hud_text": pick("keep_last_hud"), "board_text": pick("keep_last_board"),
                                    "board_file_text": pick("keep_last_board_file"),
                                    "live": in_wardogs(raw),   # in a game right now (pinned players need it)
                                    "last": st["last_text"], "team": p.get("team", ""),
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
                show = cfg["board_on"] and (cfg.get("board_overlay") or getattr(self.app, "preview_on", False))
                self.app.overlay_board = board_payload(leaderboard_rows(people, results, cfg) if show else [], cfg)
                # the Preview's Full list (its own toggle): everyone, whatever How many is
                if getattr(self.app, "preview_on", False):
                    self.app.overlay_board_full = board_payload(
                        leaderboard_rows(people, results, dict(cfg, board_top=0)) if show else [], cfg)
                else:
                    self.app.overlay_board_full = None   # only built while the Preview shows it
                self.app.overlay_texts = [r.get("hud_text", r["text"]) if p.get("overlay") else ""
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
        self.geometry("1100x820")
        self.minsize(940, 300)
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
        if not self.cfg.get("api_key"):
            self.cfg["api_key"] = secrets.token_urlsafe(12)
            save_settings(self.cfg)
        self.hotkey = None
        self._update_hotkey()
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
        lh = tk.Frame(live, bg=BG_DARK)
        lh.pack(fill="x", padx=16, pady=(12, 0))
        self._section(lh, "Live output", BG_DARK).pack(side="left")
        # the Output editor: how the overlays and text files look, with a live preview
        ttk.Button(lh, text="✏ Edit outputs", command=self._open_editor).pack(side="right")
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
        hint.configure(wraplength=590, justify="left")
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
        # Order / Show / How many / 1st place are set in the Output editor now (Full
        # list in the Preview window, the hotkey in its popup); their controls stay
        # here, never shown, so everything else works as before
        moved = self._board_moved = tk.Frame(self.board_body, bg=SH)
        orow = tk.Frame(moved, bg=SH)
        orow.pack(fill="x", pady=(8, 0))
        self._label(orow, "Order", SH).pack(side="left")
        self.order_var = tk.StringVar()
        seg, self.order_btns = self._segmented(
            orow, (("amount", "Highest first"), ("list", "List order")), self.order_var)
        seg.pack(side="left", padx=(10, 0))
        self.ranks_var = tk.BooleanVar()
        self.board_names_var = tk.BooleanVar()
        crow = tk.Frame(moved, bg=SH)
        crow.pack(fill="x", pady=(8, 0))
        self._label(crow, "Show", SH).pack(side="left")
        Chip(crow, "#", "Number places", self.ranks_var).pack(side="left", padx=(12, 0))
        Chip(crow, "👤", "Names", self.board_names_var).pack(side="left", padx=(8, 0))
        self.board_teams_var = tk.BooleanVar()
        Chip(crow, "🛡", "Teams", self.board_teams_var).pack(side="left", padx=(8, 0))
        # 🎨 Team colors: only while Teams is on
        self.team_colors_btn = tk.Label(crow, text="🎨 Team colors", bg=BG_DARK, fg=ACCENT, cursor="hand2",
                                        font=("Segoe UI", 9), padx=8, pady=4, highlightthickness=1,
                                        highlightbackground="#3A4352")
        self.team_colors_btn.bind("<Button-1>", lambda e: self._team_colors_popup())
        self.board_teams_var.trace_add("write", lambda *_: self._show_team_colors_btn())
        # how many people: everyone, or just the top N
        trow = tk.Frame(moved, bg=SH)
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
        # a live look at the leaderboard overlay, in its own small window
        self.preview_var = tk.BooleanVar(value=False)
        Chip(brow, "👁", "Preview", self.preview_var).pack(side="left", padx=(8, 0))
        self.preview_full_var = tk.BooleanVar()   # Full list: a toggle in the Preview window now
        Chip(moved, "☰", "Full list", self.preview_full_var).pack(side="left")
        self.preview_var.trace_add("write", lambda *_: self._set_preview(bool(self.preview_var.get())))
        # (how it all looks: ✏ Edit outputs in the Live output panel)
        # shown while the overlay is on: its link, and what 1st place gets
        self.board_link_row = tk.Frame(self.board_body, bg=SH)
        lrow = tk.Frame(self.board_link_row, bg=SH)
        lrow.pack(fill="x")
        self._label(lrow, "Overlay link", SH, LABEL, 9).pack(side="left")
        self.board_link = tk.Label(lrow, text=f"…/overlay/{BOARD_NAME}", bg=SH, fg=ACCENT,
                                   cursor="hand2", font=("Segoe UI", 9, "underline"), anchor="w")
        self.board_link.pack(side="left", padx=(8, 0))
        self.board_link.bind("<Button-1>", lambda e: self._copy_board_link())
        frow = tk.Frame(moved, bg=SH)
        frow.pack(fill="x", pady=(8, 0))
        self._label(frow, "1st place", SH).pack(side="left")
        self.board_first_var = tk.StringVar()
        seg, self.first_btns = self._segmented(
            frow, (("crown", "👑 Crown"), ("number", "# Number"), ("image", "🖼 Custom")), self.board_first_var)
        try:   # the same gold crown the overlay draws
            from PIL import ImageTk
            self._crown_img = ImageTk.PhotoImage(crown_image(15), master=self)
            self.first_btns["crown"].configure(text=" Crown", image=self._crown_img, compound="left")
        except Exception:
            pass
        seg.pack(side="left", padx=(10, 0))
        self.icon_row = tk.Frame(moved, bg=SH)
        self.icon_name = self._label(self.icon_row, "", SH, LABEL, 9)
        self.icon_name.pack(side="left", fill="x", expand=True)
        ttk.Button(self.icon_row, text="Choose image…", command=self._browse_icon).pack(side="right")
        self._board_link_after = brow
        # 🧹 Reset: save the results first (if Save results on reset is on), then
        # unpinned players off and everyone's amount back to zero
        erow = tk.Frame(self.board_body, bg=SH)
        erow.pack(fill="x", pady=(10, 0))
        ttk.Button(erow, text="🧹 Reset", command=lambda: self._reset_board("button")).pack(side="left")
        # the save hotkey: one button showing it, its settings in a popup
        self.hk_btn = tk.Label(erow, bg=BG_DARK, fg=ACCENT, cursor="hand2", font=("Segoe UI", 9), padx=8, pady=4,
                               highlightthickness=1, highlightbackground="#3A4352")
        self.hk_btn.pack(side="left", padx=(8, 0))
        self.hk_btn.bind("<Button-1>", lambda e: self._hotkey_popup())
        of = tk.Label(erow, text="Open folder", bg=SH, fg=ACCENT, cursor="hand2", font=("Segoe UI", 9, "underline"))
        of.pack(side="left", padx=(10, 0))
        of.bind("<Button-1>", lambda e: self._open_saved())
        e2 = tk.Frame(self.board_body, bg=SH)
        e2.pack(fill="x", pady=(8, 0))
        self.save_auto_var = tk.BooleanVar()
        Chip(e2, "💾", "Save results on reset", self.save_auto_var).pack(side="left")
        # 📌 Pin by default: anyone newly put on the leaderboard is pinned
        self.pin_default_var = tk.BooleanVar()
        Chip(e2, "📌", "Pin by default", self.pin_default_var).pack(side="left", padx=(8, 0))
        self.board_clear_var = tk.BooleanVar(value=True)   # always on now (kept for old settings)
        self.hk_on_var = tk.BooleanVar()
        self.hk_ctrl, self.hk_alt, self.hk_shift = tk.BooleanVar(), tk.BooleanVar(), tk.BooleanVar()
        self.hk_key_var = tk.StringVar()
        # the text file is chosen in the Output editor now (its control stays, never shown)
        bf = tk.Frame(moved, bg=SH)
        bf.pack(fill="x", pady=(10, 0))
        self._label(bf, "Text file", SH).pack(side="left")
        self.board_file_var = tk.StringVar()
        ttk.Entry(bf, textvariable=self.board_file_var).pack(side="left", fill="x", expand=True, padx=(12, 0))
        ttk.Button(bf, text="Browse…", command=self._browse_board).pack(side="left", padx=(8, 0))
        hint = self._label(self.board_body, "Everyone with Leaderboard on, as a browser overlay and / or a "
                                            "text file (chosen in Edit outputs). 🧹 Reset saves the results "
                                            "first if Save results on reset is on, then takes everyone off "
                                            "except you and anyone pinned (📌) and sets the amounts back to "
                                            "zero. Pinned players only show while they're in a game.",
                           SH, "#8A93A3", 8)
        hint.configure(wraplength=580, justify="left")
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
        # Keep last amount is set per output in the Output editor now (this switch
        # stays, never shown, for older settings)
        kr = tk.Frame(ab, bg=AB)
        self.keep_var = tk.BooleanVar()
        Switch(kr, self.keep_var, bg=AB)
        # +$3,740 or + $3,740 — set in the Output editor now (this control stays, never shown)
        self.space_var = tk.BooleanVar()
        seg, self.space_btns = self._segmented(
            ab, ((False, "+$3,740"), (True, "+ $3,740")), self.space_var)
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
        warn.configure(wraplength=580, justify="left")
        warn.pack(anchor="w", pady=(6, 0))
        tr = tk.Frame(ab, bg=AB)
        tr.pack(fill="x", pady=(10, 0))
        self._label(tr, "Minimize to tray", AB, TEXT).pack(side="left")
        self.tray_var = tk.BooleanVar()
        Switch(tr, self.tray_var, bg=AB).pack(side="right")
        tray_hint = self._label(ab, "Minimising hides the window to a tray icon by the clock — click it "
                                    "to bring the window back, right-click for Start / Stop and Quit.",
                                AB, "#8A93A3", 8)
        tray_hint.configure(wraplength=580, justify="left")
        tray_hint.pack(anchor="w", pady=(4, 0))
        sr0 = tk.Frame(ab, bg=AB)
        sr0.pack(fill="x", pady=(10, 0))
        self._label(sr0, "Start with Windows", AB, TEXT).pack(side="left")
        self.startup_var = tk.BooleanVar(value=bool(startup_entry()))
        Switch(sr0, self.startup_var, bg=AB).pack(side="right")
        self.startup_var.trace_add("write", lambda *_: self._set_startup())
        st_hint = self._label(ab, "Opens when you sign in to Windows and starts tracking — straight to "
                                  "the tray when Minimize to tray is on.", AB, "#8A93A3", 8)
        st_hint.configure(wraplength=580, justify="left")
        st_hint.pack(anchor="w", pady=(4, 0))
        fo = tk.Frame(ab, bg=AB)
        fo.pack(fill="x", pady=(10, 0))
        self._label(fo, "Friend folders", AB, TEXT).pack(side="left")
        self.folders_on_var = tk.BooleanVar()
        Switch(fo, self.folders_on_var, bg=AB).pack(side="right")
        fo_hint = self._label(ab, "Sorts the people list into folders: Active (in WARDOGS with a file, the "
                                  "leaderboard or an overlay on), Inactive (everyone else) and your own "
                                  "folders, with a search box.", AB, "#8A93A3", 8)
        fo_hint.configure(wraplength=580, justify="left")
        fo_hint.pack(anchor="w", pady=(4, 0))
        tn = tk.Frame(ab, bg=AB)
        tn.pack(fill="x", pady=(10, 0))
        self._label(tn, "Twitch names by default", AB, TEXT).pack(side="left")
        self.twitch_names_var = tk.BooleanVar(value=bool(self.cfg["twitch_names"]))
        Switch(tn, self.twitch_names_var, bg=AB).pack(side="right")
        self.twitch_names_var.trace_add("write", lambda *_: self._set_twitch_names())
        tn_hint = self._label(ab, "Everyone on Steam name switches to Twitch name: the leaderboard and files "
                                  "show their linked Twitch account, or their Steam name until they're linked. "
                                  "You can still change anyone on their card.", AB, "#8A93A3", 8)
        tn_hint.configure(wraplength=580, justify="left")
        tn_hint.pack(anchor="w", pady=(4, 0))
        hr = tk.Frame(ab, bg=AB)
        hr.pack(fill="x", pady=(10, 0))
        self._label(hr, "Hide overlays on stream", AB, TEXT).pack(side="left")
        self.ov_hidden_var = tk.BooleanVar()
        Switch(hr, self.ov_hidden_var, bg=AB).pack(side="right")
        hd_hint = self._label(ab, "The overlays show nothing (see-through) until you turn it off — also "
                                  "from Streamer.bot.", AB, "#8A93A3", 8)
        hd_hint.configure(wraplength=580, justify="left")
        hd_hint.pack(anchor="w", pady=(4, 0))
        # OBS card: in the Output editor now (this one stays, never shown, so
        # the colouring / connection code works as before)
        obs = tk.Frame(ab, bg=CARD, highlightbackground="#262C36", highlightthickness=1)
        inner = tk.Frame(obs, bg=CARD)
        inner.pack(fill="x", padx=14, pady=12)
        inner.columnconfigure(0, weight=2)
        inner.columnconfigure(1, weight=1)
        head = tk.Frame(inner, bg=CARD)
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._label(head, "OBS WebSocket", CARD, TEXT, 10, True).pack(side="left")
        self.obs_pill = tk.Label(head, text="● Not used", bg=CARD, fg=MUTED, font=("Segoe UI", 9, "bold"))
        self.obs_pill.pack(side="left", padx=(10, 0))

        # colors (the app's amounts and each person's OBS Text source)
        colors = tk.Frame(inner, bg=CARD)
        colors.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        cr = tk.Frame(colors, bg=CARD)
        cr.pack(fill="x")
        self.color_head = self._label(cr, "App / OBS text color", CARD)
        self.color_head.pack(side="left")
        # switch: off dims the heading and both colour buttons
        self.color_on_var = tk.BooleanVar()
        Switch(cr, self.color_on_var, bg=CARD).pack(side="right")
        cl = self._label(cr, "Color by profit / loss", CARD, LABEL, 9)
        cl.configure(cursor="hand2")
        cl.pack(side="right", padx=(0, 8))
        cl.bind("<Button-1>", lambda e: self.color_on_var.set(not self.color_on_var.get()))
        cc = tk.Frame(colors, bg=CARD)
        cc.pack(fill="x", pady=(4, 0))
        cc.columnconfigure(0, weight=1)
        cc.columnconfigure(1, weight=1)
        self.profit_btn = self._color_button(cc, "+ Profit", "profit_color")
        self.profit_btn.grid(row=0, column=0, sticky="ew")
        self.loss_btn = self._color_button(cc, "− Loss", "loss_color")
        self.loss_btn.grid(row=0, column=1, sticky="ew", padx=(12, 0))

        # connection: no switch of its own — "Color by profit / loss" turns
        # colouring on, Connect / Disconnect decides whether OBS is used
        fields = tk.Frame(inner, bg=CARD)
        fields.grid(row=2, column=0, columnspan=2, sticky="ew")
        fields.columnconfigure(0, weight=2)
        fields.columnconfigure(1, weight=1)
        src_hint = self._label(fields, "With Color by profit / loss on, each person's OBS Text source "
                                       "(set on their card, under Save to file) turns green or red "
                                       "from their own amount.", CARD, "#8A93A3", 8)
        src_hint.configure(wraplength=540, justify="left")
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

        # Streamer.bot: it controls the tracker, and hears when a game ends
        sbc = tk.Frame(ab, bg=CARD, highlightbackground="#262C36", highlightthickness=1)
        sbc.pack(fill="x", pady=(12, 0))
        sbi = tk.Frame(sbc, bg=CARD)
        sbi.pack(fill="x", padx=14, pady=12)
        self.sb_head = tk.Label(sbi, bg=CARD, fg=TEXT, cursor="hand2", font=("Segoe UI", 10, "bold"), anchor="w")
        self.sb_head.pack(anchor="w")
        self.sb_head.bind("<Button-1>", lambda e: self._fold("sb_open"))
        sb_body = self.sb_body = tk.Frame(sbi, bg=CARD)
        sb_body.pack(fill="x")
        r1 = tk.Frame(sb_body, bg=CARD)
        r1.pack(fill="x", pady=(8, 0))
        self._label(r1, "Let Streamer.bot control the tracker", CARD).pack(side="left")
        self.sb_on_var = tk.BooleanVar()
        Switch(r1, self.sb_on_var, bg=CARD).pack(side="right")
        r1b = tk.Frame(sb_body, bg=CARD)
        r1b.pack(fill="x", pady=(6, 0))
        ttk.Button(r1b, text="Copy commands", command=self._copy_sb_commands).pack(side="left")
        nk = tk.Label(r1b, text="New key", bg=CARD, fg=ACCENT, cursor="hand2", font=("Segoe UI", 9, "underline"))
        nk.pack(side="left", padx=(10, 0))
        nk.bind("<Button-1>", lambda e: self._new_api_key())
        self.sb_cmd_note = self._label(sb_body, "", CARD, "#8A93A3", 8)
        self.sb_cmd_note.configure(wraplength=540, justify="left")
        self.sb_cmd_note.pack(anchor="w", pady=(4, 0))
        self._build_sb_command_list(sb_body)
        r2 = tk.Frame(sb_body, bg=CARD)
        r2.pack(fill="x", pady=(12, 0))
        self._label(r2, "Send game results to Streamer.bot", CARD).pack(side="left")
        self.sb_send_var = tk.BooleanVar()
        Switch(r2, self.sb_send_var, bg=CARD).pack(side="right")
        sf = tk.Frame(sb_body, bg=CARD)
        sf.pack(fill="x", pady=(6, 0))
        sf.columnconfigure(0, weight=1)
        sf.columnconfigure(1, weight=1)
        self._label(sf, "HTTP Server", CARD).grid(row=0, column=0, sticky="w")
        self._label(sf, "Action", CARD).grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.sb_url_var, self.sb_action_var = tk.StringVar(), tk.StringVar()
        ttk.Entry(sf, textvariable=self.sb_url_var).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Entry(sf, textvariable=self.sb_action_var).grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=(4, 0))
        r3 = tk.Frame(sb_body, bg=CARD)
        r3.pack(fill="x", pady=(8, 0))
        ttk.Button(r3, text="Test", command=self._test_sb).pack(side="left")
        sb_hint = self._label(sb_body, "In Streamer.bot: Servers/Clients › HTTP Server › Auto Start + Start. "
                                   "Its action gets %who%, %amount%, %result% (profit / loss / even), "
                                   "%message%, %leaderboard%, %place1%, %place1Amount%… A game counts as "
                                   "over when your amount has been gone for 15 s (back in the menus).",
                              CARD, "#8A93A3", 8)
        sb_hint.configure(wraplength=540, justify="left")
        sb_hint.pack(anchor="w", pady=(6, 0))
        # (the Browser overlays section is gone: Hide overlays on stream is with the
        # switches above, the look is in the Output editor). Its controls stay,
        # never shown, so Streamer.bot and the rest work as before
        self.ov_head = tk.Label(ab, bg=AB)
        ov_body = self.ov_body = tk.Frame(ab, bg=AB)
        look = self._ov_moved = tk.Frame(ov_body, bg=AB)
        fr = tk.Frame(look, bg=AB)
        fr.pack(fill="x", pady=(6, 0))
        self._label(fr, "Font", AB).pack(side="left")
        self.ov_font_var = tk.StringVar()
        try:
            installed = sorted({f for f in tkfont.families(self) if not f.startswith("@")} - set(WEB_FONTS),
                               key=str.lower)
        except tk.TclError:
            installed = []
        self.installed_fonts = installed   # the Output editor lists them too
        self.font_box = ttk.Combobox(fr, textvariable=self.ov_font_var, state="readonly", height=18,
                                     values=list(WEB_FONTS) + [FONT_SEPARATOR] + installed, width=30)
        self.font_box.pack(side="left", padx=(12, 0))
        self.font_box.bind("<<ComboboxSelected>>", lambda e: self._pick_font())
        self.font_note = self._label(fr, "", AB, "#8A93A3", 8)
        self.font_note.pack(side="left", padx=(8, 0))
        sr = tk.Frame(look, bg=AB)
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
        cr2 = tk.Frame(look, bg=AB)
        cr2.pack(fill="x", pady=(8, 0))
        self._label(cr2, "Text color", AB).pack(side="left")
        self.ov_color_btn = self._small_color_button(cr2, "overlay_color")
        self.ov_color_btn.pack(side="left", padx=(12, 0))
        pr = tk.Frame(look, bg=AB)
        pr.pack(fill="x", pady=(8, 0))
        self._label(pr, "Color by profit / loss", AB).pack(side="left")
        self.ov_pl_var = tk.BooleanVar()
        self.ov_pl_board_var = tk.BooleanVar()
        Chip(pr, "$", "Money HUD", self.ov_pl_var).pack(side="left", padx=(12, 0))
        Chip(pr, "🏆", "Leaderboard", self.ov_pl_board_var).pack(side="left", padx=(8, 0))
        pc = tk.Frame(look, bg=AB)
        pc.pack(fill="x", pady=(6, 0))
        self.ov_profit_btn = self._small_color_button(pc, "overlay_profit_color")
        self.ov_profit_btn.pack(side="left")
        self.ov_loss_btn = self._small_color_button(pc, "overlay_loss_color")
        self.ov_loss_btn.pack(side="left", padx=(8, 0))
        # background: a colour at some opacity, or your own picture
        bgr = tk.Frame(look, bg=AB)
        bgr.pack(fill="x", pady=(10, 0))
        self._label(bgr, "Background", AB).pack(side="left")
        self.ov_bg_mode_var = tk.StringVar()
        seg, self.bg_mode_btns = self._segmented(bgr, (("color", "Color"), ("image", "Image")), self.ov_bg_mode_var)
        seg.pack(side="left", padx=(12, 0))
        self.ov_bg_btn = self._small_color_button(bgr, "overlay_bg_color")
        self.ov_bg_btn.pack(side="left", padx=(8, 0))
        self.bg_image_row = tk.Frame(look, bg=AB)
        self.bg_image_name = self._label(self.bg_image_row, "", AB, LABEL, 9)
        self.bg_image_name.pack(side="left", fill="x", expand=True)
        ttk.Button(self.bg_image_row, text="Choose image…", command=self._browse_bg).pack(side="right")
        self._bg_image_after = bgr
        opr = tk.Frame(look, bg=AB)
        opr.pack(fill="x", pady=(8, 0))
        self._opacity_row = opr
        self._label(opr, "Opacity", AB).pack(side="left")
        self.ov_opacity_var = tk.IntVar()
        self._stepper(opr, self.ov_opacity_var, 0, 100, 5, AB, unit="%").pack(side="left", padx=(12, 0))
        # team badges: our shields, or your own picture per team
        tpr = tk.Frame(look, bg=AB)
        tpr.pack(fill="x", pady=(10, 0))
        self._label(tpr, "Team badges", AB).pack(side="left")
        self.team_pic_btns = {}
        for t, name, _ in TEAMS:
            b = tk.Label(tpr, bg=BG_DARK, fg=TEXT, font=("Segoe UI", 9), padx=6, pady=3, cursor="hand2",
                         highlightthickness=1, highlightbackground=BORDER)
            b.pack(side="left", padx=(8, 0))
            b.bind("<Button-1>", lambda e, t=t: self._pick_team_pic(t))
            x = tk.Label(tpr, text="✕", bg=AB, fg=MUTED, cursor="hand2", font=("Segoe UI Symbol", 8))
            x.bind("<Button-1>", lambda e, t=t: self._clear_team_pic(t))
            self.team_pic_btns[t] = (b, x)
        self.after_idle(self._paint_team_pics)
        # box size: Auto fits the text
        bxr = tk.Frame(look, bg=AB)
        bxr.pack(fill="x", pady=(10, 0))
        self._label(bxr, "Money HUD box", AB).pack(side="left")
        self.ov_hud_w_var, self.ov_hud_h_var, self.ov_board_w_var = tk.IntVar(), tk.IntVar(), tk.IntVar()
        self._label(bxr, "W", AB, "#8A93A3", 9).pack(side="left", padx=(12, 4))
        self._stepper(bxr, self.ov_hud_w_var, 20, BOX_MAX, 10, AB, auto=True).pack(side="left")
        self._label(bxr, "H", AB, "#8A93A3", 9).pack(side="left", padx=(10, 4))
        self._stepper(bxr, self.ov_hud_h_var, 10, BOX_MAX, 1, AB, auto=True).pack(side="left")
        lbr = tk.Frame(look, bg=AB)
        lbr.pack(fill="x", pady=(8, 0))
        self._label(lbr, "Leaderboard box", AB).pack(side="left")
        self._label(lbr, "W", AB, "#8A93A3", 9).pack(side="left", padx=(12, 4))
        self._stepper(lbr, self.ov_board_w_var, 100, BOX_MAX, 10, AB, auto=True).pack(side="left")
        self._label(lbr, "(height follows the rows)", AB, "#8A93A3", 8).pack(side="left", padx=(10, 0))
        # OBS: size the Browser sources to fit
        ofr = tk.Frame(look, bg=AB)   # (in the Output editor's OBS section now)
        ofr.pack(fill="x", pady=(10, 0))
        self._label(ofr, "Size OBS sources to fit", AB).pack(side="left")
        self.obs_fit_var = tk.BooleanVar()
        Switch(ofr, self.obs_fit_var, bg=AB).pack(side="right")
        ofb = tk.Frame(look, bg=AB)
        ofb.pack(fill="x", pady=(6, 0))
        self.obs_fit_info = self._label(ofb, "", AB, "#8A93A3", 8)
        self.obs_fit_info.configure(wraplength=330, justify="left")
        self.obs_fit_info.pack(side="left", fill="x", expand=True)
        ttk.Button(ofb, text="Resize in OBS now", command=lambda: self._fit_obs(True)).pack(side="right")
        ov_hint = self._label(ov_body, "For the 📺 overlays in OBS (money HUD and leaderboard). Edit outputs "
                                  "opens a window with a live preview (with test players) and every setting "
                                  "beside it; changes save straight away and show on stream within a moment.",
                              AB, "#8A93A3", 8)
        ov_hint.configure(wraplength=580, justify="left")
        ov_hint.pack(anchor="w", pady=(6, 0))
        self.adv_open = False

        # any setting change -> save + apply live
        for v in (self.board_on_var, self.board_overlay_var, self.board_top_var, self.board_top_n_var,
                  self.board_first_var,
                  self.board_file_var, self.order_var, self.ranks_var,
                  self.board_names_var, self.space_var, self.refresh_var, self.color_on_var,
                  self.tray_var, self.ov_font_var, self.ov_size_var, self.ov_pl_var, self.ov_pl_board_var, self.ov_bg_mode_var,
                  self.ov_opacity_var, self.ov_hud_w_var, self.ov_hud_h_var, self.ov_board_w_var,
                  self.obs_fit_var, self.ov_hidden_var, self.folders_on_var, self.sb_on_var, self.sb_send_var,
                  self.sb_url_var, self.sb_action_var, self.save_auto_var, self.board_clear_var, self.pin_default_var, self.hk_on_var, self.board_teams_var, self.preview_full_var,
                  self.hk_ctrl, self.hk_alt, self.hk_shift, self.hk_key_var,
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
        self.ov_hidden_var.set(bool(c["overlays_hidden"]))
        self.folders_on_var.set(bool(c["folders_on"]))
        self.sb_on_var.set(bool(c["sb_on"]))
        self.sb_send_var.set(bool(c["sb_send"]))
        self.sb_url_var.set(c["sb_url"])
        self.sb_action_var.set(c["sb_action"])
        self.save_auto_var.set(bool(c["save_on_game_over"]))
        self.board_clear_var.set(True)
        self.pin_default_var.set(bool(c["board_pin_default"]))
        self.board_teams_var.set(bool(c["board_teams"]))
        self.twitch_names_var.set(bool(c["twitch_names"]))
        self.preview_full_var.set(bool(c["preview_full"]))
        self.hk_on_var.set(bool(c["save_hotkey_on"]))
        mods = [m.lower() for m in c["save_hotkey"].split("+")[:-1]]
        self.hk_ctrl.set("ctrl" in mods or "control" in mods)
        self.hk_alt.set("alt" in mods)
        self.hk_shift.set("shift" in mods)
        self.hk_key_var.set(c["save_hotkey"].split("+")[-1].strip().upper() or "S")
        self._paint_sb()
        self._paint_hotkey()
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
        self._paint_folds()
        # Advanced opens as you left it (and always while a refresh is switched on)
        self._show_advanced(bool(c["adv_open"] or c["reload_on"]), save=False)
        self._build_people()

    # ---- people ----
    def _build_people(self):
        """(Re)draws one card per person: picture + name (click to search),
        which name they go by, remove, Save to file / Leaderboard, and their
        file (+ OBS source with OBS color on)."""
        for w in self.people_box.winfo_children():
            w.destroy()
        self.person_rows = []
        # drop the panel's lines for people who are no longer in the list
        self.current_people = self.current_people[:len(self.cfg["people"])]
        self._show_value(self.current_text)
        people = self.cfg["people"]
        folders = bool(self.cfg["folders_on"])
        if folders:
            self._build_folder_frames()  # before the cards, so the cards draw on top of them
        self.person_cards = []
        for i, p in enumerate(people):
            card = tk.Frame(self.people_box, bg=CARD, highlightbackground="#262C36", highlightthickness=1)
            self.person_cards.append(card)
            if not folders:
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
            team_btn = None
            if self.cfg.get("board_teams"):
                team_btn = tk.Label(line, bg=BG_DARK, fg=MUTED, font=("Segoe UI", 9), padx=6, pady=3,
                                    cursor="hand2", compound="left", highlightthickness=1,
                                    highlightbackground=BORDER)
                team_btn.pack(side="left", padx=(8, 0))
                team_btn.bind("<Button-1>", lambda e, i=i: self._team_menu(i, e.widget))
                self._paint_team_btn(team_btn, p.get("team", ""))
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
            Chip(chips, "📄", "Save to file", own).pack(side="left")
            board_chip = Chip(chips, "🏆", "Leaderboard", on_board)
            board_chip.pack(side="left", padx=(8, 0))
            keep = tk.BooleanVar(value=p.get("board_keep", False))
            if p["who"]:   # you always stay; friends can be pinned to stay (shown while on the leaderboard)
                pin = tk.Label(chips, text="📌", font=("Segoe UI Emoji", 9), padx=5, pady=3, cursor="hand2",
                               highlightthickness=1)
                pin.bind("<Button-1>", lambda e, v=keep: v.set(not v.get()))
                self._paint_pin(pin, keep.get())
                if p["board"]:
                    pin.pack(side="left", padx=(2, 0), after=board_chip)
                keep.trace_add("write", lambda *_, i=i, v=keep, b=pin: (
                    self._set_person(i, board_keep=bool(v.get())), self._paint_pin(b, v.get())))
                on_board.trace_add("write", lambda *_, v=on_board, b=pin, c=board_chip: (
                    b.pack(side="left", padx=(2, 0), after=c) if v.get() else b.pack_forget()))
                # 📌 Pin by default: put on the leaderboard -> pinned straight away
                on_board.trace_add("write", lambda *_, v=on_board, k=keep: (
                    k.set(True) if v.get() and self.cfg.get("board_pin_default") and not k.get() else None))
            # same row: the overlay, their Twitch link, and their folders
            ov = tk.BooleanVar(value=p["overlay"])
            Chip(chips, "📺", "Overlay", ov).pack(side="left", padx=(8, 0))
            twitch_btn = None
            if self.cfg.get("sb_on"):
                twitch_btn = tk.Label(chips, font=("Segoe UI", 10), padx=11, pady=2, cursor="hand2",
                                      highlightthickness=1)
                twitch_btn.pack(side="left", padx=(8, 0))
                twitch_btn.bind("<Button-1>", lambda e, i=i: self._twitch_popup(i, e.widget))
                self._paint_twitch_btn(twitch_btn, p)
            if folders:
                fb = tk.Label(chips, text="📁", bg=BG_DARK, fg=MUTED, font=("Segoe UI Symbol", 10), padx=8, pady=2,
                              cursor="hand2", highlightthickness=1, highlightbackground="#3A4352")
                fb.pack(side="left", padx=(8, 0))
                fb.bind("<Button-1>", lambda e, i=i: self._folder_menu(i, e.widget))
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
                                     "overlay": ov, "rm": rm, "twitch": twitch_btn, "keep": keep,
                                     "team": team_btn})
        self._paint_people()
        if folders:
            self._folder_sig = None
            self._layout_folders()
        self._refit_soon()

    # ---- friend folders ----
    def _build_folder_frames(self):
        """The search box, + New folder, and one folder (header + body) each."""
        c = self.cfg
        if not hasattr(self, "folder_search"):
            self.folder_search = tk.StringVar()
            self.folder_search.trace_add("write", lambda *_: self._layout_folders())
        top = tk.Frame(self.people_box, bg=BG)
        top.pack(fill="x", pady=(0, 6))
        sb = tk.Frame(top, bg=BG_DARK, highlightthickness=1, highlightbackground=BORDER)
        sb.pack(side="left", fill="x", expand=True)
        tk.Label(sb, text="🔎", bg=BG_DARK, fg=MUTED, font=("Segoe UI Symbol", 9)).pack(side="left", padx=(6, 0))
        self.folder_search_entry = tk.Entry(sb, textvariable=self.folder_search, bg=BG_DARK, fg=TEXT, bd=0,
                                            insertbackground=TEXT, font=("Segoe UI", 10))
        self.folder_search_entry.pack(side="left", fill="x", expand=True, padx=6, pady=4)
        ttk.Button(top, text="+ New folder", command=self._new_folder).pack(side="left", padx=(8, 0))
        self.folder_frames = {}
        for name in list(AUTO_FOLDERS) + c["folders"]:
            box = tk.Frame(self.people_box, bg=BG)
            head = tk.Frame(box, bg=BG)
            head.pack(fill="x")
            title = tk.Label(head, bg=BG, fg=TEXT, font=("Segoe UI", 10, "bold"), cursor="hand2", anchor="w")
            title.pack(side="left")
            title.bind("<Button-1>", lambda e, n=name: self._toggle_folder(n))
            if name != "Inactive":
                # 📌: everyone in this folder stays on the leaderboard after a save
                # (Active: whoever is in it at that moment)
                fp = tk.Label(head, text="📌", bg=BG, cursor="hand2", font=("Segoe UI Emoji", 9), padx=4,
                              highlightthickness=1)
                fp.pack(side="left", padx=(8, 0))
                fp.bind("<Button-1>", lambda e, n=name: self._pin_folder(n))
                self._paint_pin(fp, name in c["folders_pinned"])
            if name not in AUTO_FOLDERS:
                for sym, cmd in (("✎", self._rename_folder), ("✕", self._delete_folder)):
                    b = tk.Label(head, text=sym, bg=BG, fg=MUTED, cursor="hand2", font=("Segoe UI Symbol", 9))
                    b.pack(side="left", padx=(8, 0))
                    b.bind("<Button-1>", lambda e, n=name, f=cmd: f(n))
            else:
                tk.Label(head, text="automatic", bg=BG, fg="#5C6575", font=("Segoe UI", 8)).pack(side="left",
                                                                                              padx=(8, 0))
            body = tk.Frame(box, bg=BG)
            empty = tk.Label(box, bg=BG, fg="#5C6575", font=("Segoe UI", 8), anchor="w")
            self.folder_frames[name] = {"box": box, "title": title, "body": body, "empty": empty, "rows": []}

    def _is_active(self, i):
        """In WARDOGS right now, with a file, the leaderboard or an overlay on."""
        p = self.cfg["people"][i]
        res = getattr(self, "current_people", []) or []
        raw = str(res[i].get("raw", "")) if i < len(res) else ""
        return in_wardogs(raw) and bool(p["own_file"] or p["board"] or p["overlay"])

    def _person_name(self, i):
        p, res = self.cfg["people"][i], getattr(self, "current_people", []) or []
        r = res[i] if i < len(res) else {}
        return r.get("label") or p["label"] or p["who"] or ME

    def _folder_members(self):
        people = self.cfg["people"]
        act = [i for i in range(len(people)) if self._is_active(i)]
        out = {"Active": act, "Inactive": [i for i in range(len(people)) if i not in act]}
        for f in self.cfg["folders"]:
            out[f] = [i for i, p in enumerate(people) if f in p.get("folders", [])]
        return out

    def _layout_folders(self):
        """Puts each card in Active or Inactive, your folders' rows in place,
        and hides what the search doesn't match."""
        if not self.cfg["folders_on"] or not getattr(self, "folder_frames", None):
            return
        members = self._folder_members()
        q = self.folder_search.get().strip().lower()
        closed = set(self.cfg["folders_closed"])
        names = {i: " ".join({self._person_name(i), self.cfg["people"][i]["who"],
                              self.cfg["people"][i]["label"]}).lower() for i in range(len(self.cfg["people"]))}
        placed = set()
        for name, fr in self.folder_frames.items():
            mem = members.get(name, [])
            shown = mem if (not q or q in name.lower()) else [i for i in mem if q in names[i]]
            visible = not q or q in name.lower() or bool(shown)
            if not visible:
                fr["box"].pack_forget()
            else:
                fr["box"].pack(fill="x", pady=(0, 8))
            is_open = name not in closed or bool(q)
            fr["title"].configure(text=("▾ " if is_open else "▸ ") + f"{name}  ·  {len(mem)}")
            for w in fr["rows"]:
                w.destroy()
            fr["rows"] = []
            if is_open and visible:
                fr["body"].pack(fill="x", pady=(4, 0))
            else:
                fr["body"].pack_forget()
            fr["empty"].pack_forget()
            if is_open and visible and not shown:
                fr["empty"].configure(text="No one here yet — use 📁 on someone's card." if name not in AUTO_FOLDERS
                                      else "No one right now.")
                fr["empty"].pack(fill="x", padx=4, pady=(2, 0))
            if name in AUTO_FOLDERS:
                for i in shown:
                    if is_open and visible:
                        card = self.person_cards[i]
                        card.pack(in_=fr["body"], fill="x", pady=(0, 6))
                        card.lift()
                        placed.add(i)
            elif is_open and visible:
                for i in shown:
                    fr["rows"].append(self._folder_row(fr["body"], name, i))
        for i, card in enumerate(self.person_cards):
            if i not in placed:
                card.pack_forget()
        self._folder_sig = self._folder_signature()
        self._refit_soon()

    def _folder_row(self, parent, folder, i):
        """A compact line for someone in one of your folders: name, amount,
        playing or not, Show (jumps to their card) and ✕ (out of this folder)."""
        res = getattr(self, "current_people", []) or []
        r = res[i] if i < len(res) else {}
        row = tk.Frame(parent, bg=CARD, highlightbackground="#262C36", highlightthickness=1)
        row.pack(fill="x", pady=(0, 4))
        tk.Label(row, text=self._person_name(i), image=self.avatar_for(self.cfg["people"][i]["who"]),
                 compound="left", bg=CARD, fg=TEXT, font=("Segoe UI", 10), padx=6, anchor="w").pack(
            side="left", pady=4)
        text = r.get("text", "")
        tk.Label(row, text=text or "—", bg=CARD, fg=self.color_for(text) if text else MUTED,
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(8, 0))
        tk.Label(row, text="active" if self._is_active(i) else "inactive", bg=CARD, fg="#8A93A3",
                 font=("Segoe UI", 8)).pack(side="left", padx=(8, 0))
        x = tk.Label(row, text="✕", bg=CARD, fg=MUTED, cursor="hand2", font=("Segoe UI Symbol", 9))
        x.pack(side="right", padx=(6, 8))
        x.bind("<Button-1>", lambda e: self._set_folder(i, folder, False))
        sh = tk.Label(row, text="Show", bg=CARD, fg=ACCENT, cursor="hand2", font=("Segoe UI", 9, "underline"))
        sh.pack(side="right")
        sh.bind("<Button-1>", lambda e: self._show_card(i))
        return row

    def _folder_signature(self):
        """What the folders show; when it changes they're laid out again."""
        res = getattr(self, "current_people", []) or []
        return (tuple(self._is_active(i) for i in range(len(self.cfg["people"]))),
                tuple(str(r.get("text", "")) for r in res), tuple(self._person_name(i) for i in
                                                                   range(len(self.cfg["people"]))))

    def _folders_tick(self):
        """After new amounts or a chip change: move people if their folder changed."""
        if self.cfg["folders_on"] and getattr(self, "folder_frames", None) and \
                self._folder_signature() != getattr(self, "_folder_sig", None):
            self._layout_folders()

    def _pin_folder(self, name):
        pinned = self.cfg["folders_pinned"]
        if name in pinned:
            pinned.remove(name)
        else:
            pinned.append(name)
        save_settings(self.cfg)
        self._add_log(time.strftime("%H:%M:%S"), f'Folder "{name}" ' + (
            "pinned: its people stay on the leaderboard after a save." if name in pinned else "unpinned."), None)
        self._build_people()

    def _kept_by(self, p, i=None):
        """Why someone stays on the leaderboard after a save ("" if they don't)."""
        if not p["who"]:
            return "you"
        if p.get("board_keep"):
            return "📌"
        if not self.cfg.get("folders_on"):
            return ""   # folders off: their pins aren't shown, so they don't count
        pinned = self.cfg.get("folders_pinned", [])
        if "Active" in pinned and i is not None and self._is_active(i):
            return "📌 Active"
        hit = [f for f in p.get("folders", []) if f in pinned]
        return f'📌 {hit[0]}' if hit else ""

    # ---- leaderboard preview window ----
    def _set_preview(self, on):
        if on and getattr(self, "preview_proc", None) is None:
            exe = preview_browser()
            if not exe:
                self._add_log(time.strftime("%H:%M:%S"), "Preview needs Microsoft Edge or Chrome.", "#FACC15")
                self.after_idle(lambda: self.preview_var.set(False))
                return
            self.preview_on = True
            self._update_overlay_server()
            # Full list is a toggle in the Preview window itself (it remembers it)
            url = overlay_link(self.board_path) + f"?stage=1&server=http://127.0.0.1:{self.cfg['overlay_port']}"
            profile = os.path.join(DATA_DIR, "preview_browser")
            if browser_left_open(profile):   # a preview from before: ours wouldn't open
                close_browser_using(profile)
            if edge_signed_in(profile):   # an older version let Edge sign it in: start it afresh
                shutil.rmtree(profile, ignore_errors=True)
            try:
                self.preview_proc = subprocess.Popen(
                    [exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=560,340",
                     "--no-first-run", "--no-default-browser-check", "--disable-sync",
                     f"--disable-features={EDGE_NO_SIGNIN}",
                     # Edge otherwise restarts itself on a new profile, so the process we
                     # started ends while the window stays open (the chip would switch off)
                     "--edge-skip-compat-layer-relaunch"],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as e:
                self.preview_on, self.preview_proc = False, None
                self._add_log(time.strftime("%H:%M:%S"), f"Preview didn't open: {e}", "#FF8A7A")
                self.after_idle(lambda: self.preview_var.set(False))
                return
            if not self.cfg["board_on"]:
                self._add_log(time.strftime("%H:%M:%S"), "Preview: the leaderboard is off, so it's empty "
                                                         "until you switch it on.", "#FACC15")
            self.after(1000, self._watch_preview)
        elif not on and getattr(self, "preview_proc", None) is not None:
            self._close_preview()

    def _close_preview(self):
        proc, self.preview_proc, self.preview_on = getattr(self, "preview_proc", None), None, False
        if proc is not None and proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._update_overlay_server()

    def _watch_preview(self):
        """You closed the preview window yourself: switch the chip off."""
        proc = getattr(self, "preview_proc", None)
        if proc is None:
            return
        if proc.poll() is not None:
            self.preview_proc, self.preview_on = None, False
            self.preview_var.set(False)
            self._update_overlay_server()
        else:
            self.after(1000, self._watch_preview)

    # ---- Output editor: a page with a live preview of the overlays and their settings ----
    def _open_editor(self):
        proc = getattr(self, "editor_proc", None)
        if proc is not None and proc.poll() is None:
            if not bring_to_front("Output editor"):
                self._add_log(time.strftime("%H:%M:%S"), "The output editor is already open.", None)
            return
        exe = preview_browser()
        if not exe:
            self._add_log(time.strftime("%H:%M:%S"), "The output editor needs Microsoft Edge or Chrome.", "#FACC15")
            return
        self.editor_on = True
        self._update_overlay_server()
        if self.overlay is None:
            self.editor_on = False
            return
        if not getattr(self, "editor_token", ""):
            self.editor_token = secrets.token_urlsafe(18)
        url = f"http://127.0.0.1:{self.cfg['overlay_port']}/editor?t={self.editor_token}"
        profile = os.path.join(DATA_DIR, "editor_browser")
        if browser_left_open(profile):   # an editor from before (its page can't save any more)
            close_browser_using(profile)
        if edge_signed_in(profile):
            shutil.rmtree(profile, ignore_errors=True)
        try:
            self.editor_proc = subprocess.Popen(
                [exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1240,820",
                 "--no-first-run", "--no-default-browser-check", "--disable-sync",
                 f"--disable-features={EDGE_NO_SIGNIN}", "--edge-skip-compat-layer-relaunch"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            self.editor_on, self.editor_proc = False, None
            self._update_overlay_server()
            self._add_log(time.strftime("%H:%M:%S"), f"The output editor didn't open: {e}", "#FF8A7A")
            return
        self.after(1000, self._watch_editor)

    def _watch_editor(self):
        proc = getattr(self, "editor_proc", None)
        if proc is None:
            return
        if proc.poll() is not None:   # you closed it
            self.editor_proc, self.editor_on = None, False
            self._update_overlay_server()
        else:
            self.after(1000, self._watch_editor)

    def _close_editor(self):
        proc, self.editor_proc, self.editor_on = getattr(self, "editor_proc", None), None, False
        if proc is not None and proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _editor_http(self, method, path, q, headers, body):
        """The editor page and its calls (on the feed's thread). Everything but the
        page itself needs the token the tracker put in the editor's address."""
        js = lambda code, obj: (code, "application/json", json.dumps(obj).encode())
        if path == "/editor":
            try:
                with open(bundled_hud(EDITOR_NAME), "rb") as f:
                    return 200, "text/html; charset=utf-8", f.read()
            except OSError:
                return 404, "text/plain", b"not found"
        token = getattr(self, "editor_token", "")
        if not token or not hmac.compare_digest(str(headers.get("X-Editor-Token", "")), token):
            return js(403, {"ok": False, "error": "open the editor from the tracker"})
        if path == "/editor/state":
            return js(200, self._editor_state())
        if path == "/editor/textfile":   # the test players as a text file would show them
            if q.get("kind") == "person":   # a person's own file: the amount, and with the Name chip
                amount = demo_texts(space=self.cfg["space"])[DEMO_ME]
                return js(200, {"ok": True, "lines": [amount, "", file_line(amount, DEMO_PLAYERS[DEMO_ME][0], True)]})
            return js(200, {"ok": True, "lines": demo_file(self.cfg)})
        if path in ("/editor/set", "/editor/pick") and method == "POST":
            if path == "/editor/set":
                try:
                    arg = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    return js(400, {"ok": False, "error": "not JSON"})
                if not isinstance(arg, dict):
                    return js(400, {"ok": False, "error": "send an object"})
            else:
                arg = q.get("what", "")
            box = {"done": threading.Event(), "reply": {"ok": False, "error": "the tracker is busy"}}
            self.events.put(("editor", "pick" if path == "/editor/pick" else "set", arg, box))
            box["done"].wait(600 if path == "/editor/pick" else 5)
            return js(200 if box["reply"].get("ok") else 400, box["reply"])
        return js(404, {"ok": False, "error": "not found"})

    def _editor_state(self):
        c = self.cfg
        pic = lambda p: os.path.basename(p) if p else ""
        return {"ok": True,
                "fonts": list(WEB_FONTS), "installed": list(getattr(self, "installed_fonts", [])),
                "sizeMin": OVERLAY_SIZE_MIN, "sizeMax": OVERLAY_SIZE_MAX, "sizeGame": DEFAULTS["overlay_size"],
                "boxMax": BOX_MAX, "topMax": BOARD_TOP_MAX, "hudPerson": 1,
                "teams": [{"id": t, "name": n, "default": dc, "color": team_color(t, c),
                           "changed": t in c["team_colors"], "pic": pic(c["team_pics"].get(t, ""))}
                          for t, n, dc in TEAMS],
                "values": {k: c[k] for k in (
                    "overlay_font", "overlay_size", "overlay_color", "overlay_pl", "overlay_pl_board",
                    "overlay_profit_color", "overlay_loss_color", "overlay_bg_mode", "overlay_bg_color",
                    "overlay_bg_opacity", "overlay_hud_w", "overlay_hud_h", "overlay_board_w",
                    "board_ranks", "board_names", "board_teams", "team_names_tinted", "board_first",
                    "board_top", "board_top_n", "board_order")},
                "bgImage": pic(c["overlay_bg_image"]), "icon": pic(c["board_icon"]),
                # Text file, spacing and OBS WebSocket (moved here from the main window)
                "boardFile": c["board_file"], "boardFileName": pic(c["board_file"]), "space": bool(c["space"]),
                "keep": {k: bool(c.get(k, c["keep_last"])) for k in KEEP_LAST_KEYS},
                "obs": {"colorOn": bool(c["color_on"]), "profit": c["profit_color"], "loss": c["loss_color"],
                        "connect": bool(c["obs_connect"]), "host": c["obs_host"], "port": c["obs_port"],
                        "passwordSet": bool(c["obs_password"]),   # the password itself never goes to the page
                        "status": list(getattr(self, "_obs_status", ("● Not used", MUTED))),
                        "fit": bool(c["obs_fit"]), "fitText": self._fit_text()}}

    def _fit_text(self):
        sizes = obs_fit_sizes(self.cfg)
        hud, board = sizes[HUD_NAME], sizes[BOARD_NAME]
        return f"Money HUD {hud[0]} × {hud[1]}  ·  Leaderboard {board[0]} × {board[1]}"

    def _editor_set(self, data):
        """Apply what the editor changed, through the same controls the window
        uses (so everything is checked and saved the usual way)."""
        c = self.cfg
        hexc = lambda v: "#" + str(v).strip().lstrip("#").upper()
        need = None
        for key, v in data.items():
            if key in ("overlay_color", "overlay_profit_color", "overlay_loss_color", "overlay_bg_color"):
                if not HEX_COLOR.fullmatch(hexc(v)):
                    return {"ok": False, "error": f"{v} isn't a colour"}
                c[key] = hexc(v)
                self._save_soon()   # dragging a colour picker: not a file write per step
                self._paint_overlay_colors()
            elif key == "overlay_font":
                f = str(v).strip()[:80]
                if f and f != FONT_SEPARATOR:
                    self.ov_font_var.set(f)
                    self._paint_font()
            elif key in ("overlay_size", "overlay_bg_opacity", "overlay_hud_w", "overlay_hud_h", "overlay_board_w"):
                lo, hi = {"overlay_size": (OVERLAY_SIZE_MIN, OVERLAY_SIZE_MAX), "overlay_bg_opacity": (0, 100)}.get(
                    key, (0, BOX_MAX))
                n = max(lo, min(int(float(v)), hi))
                {"overlay_size": self.ov_size_var, "overlay_bg_opacity": self.ov_opacity_var,
                 "overlay_hud_w": self.ov_hud_w_var, "overlay_hud_h": self.ov_hud_h_var,
                 "overlay_board_w": self.ov_board_w_var}[key].set(n)
            elif key in ("overlay_pl", "overlay_pl_board", "board_ranks", "board_names", "board_teams"):
                {"overlay_pl": self.ov_pl_var, "overlay_pl_board": self.ov_pl_board_var,
                 "board_ranks": self.ranks_var, "board_names": self.board_names_var,
                 "board_teams": self.board_teams_var}[key].set(bool(v))
            elif key == "team_names_tinted":
                c["team_names_tinted"] = bool(v)
                self._save_soon()
            elif key == "overlay_bg_mode":
                if v == "image" and not c["overlay_bg_image"]:
                    need = "bg"            # no picture yet: the editor asks for one
                elif v in ("color", "image"):
                    self.ov_bg_mode_var.set(v)
            elif key == "board_first":
                if v == "image" and not c["board_icon"]:
                    need = "icon"
                elif v in ("crown", "number", "image"):
                    self.board_first_var.set(v)
            elif key == "board_order" and v in ("amount", "list"):
                self.order_var.set(v)
            elif key == "board_top":
                n = int(float(v))
                if n > 0:
                    self.board_top_n_var.set(max(1, min(n, BOARD_TOP_MAX)))
                self.board_top_var.set("top" if n > 0 else "all")
            elif key == "team_color" and isinstance(v, dict):
                t = v.get("team")
                if t in TEAM_NAMES:
                    hx = hexc(v.get("color", "")) if v.get("color") else ""
                    if hx and not HEX_COLOR.fullmatch(hx):
                        return {"ok": False, "error": "not a colour"}
                    self._set_team_color(t, hx)
            elif key == "team_pic_clear" and v in TEAM_NAMES:
                self._clear_team_pic(v)
            # Text file, spacing and OBS WebSocket
            elif key == "board_file_clear":
                self.board_file_var.set("")
            elif key in KEEP_LAST_KEYS:   # Keep last amount, per output
                c[key] = bool(v)
                self._save_soon()
                self._show_value(self.current_text)
            elif key == "space":
                self.space_var.set(bool(v))
            elif key == "color_on":
                self.color_on_var.set(bool(v))
            elif key in ("profit_color", "loss_color"):
                if not HEX_COLOR.fullmatch(hexc(v)):
                    return {"ok": False, "error": f"{v} isn't a colour"}
                c[key] = hexc(v)
                self._save_soon()
                self._paint_color_buttons()
                self._show_value(self.current_text)
            elif key == "obs_host":
                self.obs_host_var.set(str(v).strip()[:200])
            elif key == "obs_port":
                p_ = int(float(v))
                if not 1 <= p_ <= 65535:
                    return {"ok": False, "error": "the port is 1–65535"}
                self.obs_port_var.set(str(p_))
            elif key == "obs_password":
                self.obs_pass_var.set(str(v)[:200])
            elif key == "obs_fit":
                self.obs_fit_var.set(bool(v))
            elif key == "obs_test":
                self._test_obs()
            elif key == "obs_connect_toggle":
                self._toggle_connect()
            elif key == "obs_fit_now":
                self._fit_obs(True)
        return dict(self._editor_state(), need=need)

    def _editor_pick(self, what):
        """A picture for the editor (background, 1st place, a team badge): a
        file dialog in front of the editor window."""
        if what == "board_file":   # the leaderboard's text file: a save-as dialog
            holder = tk.Toplevel(self)
            holder.withdraw()
            holder.attributes("-topmost", True)
            try:
                path = filedialog.asksaveasfilename(
                    parent=holder, title="Leaderboard text file", defaultextension=".txt",
                    initialfile=os.path.basename(self.board_file_var.get()) or "Leaderboard.txt",
                    filetypes=[("Text file", "*.txt")])
            finally:
                holder.destroy()
            if path:
                self.board_file_var.set(path)
            return dict(self._editor_state(), picked=bool(path))
        if what not in ("bg", "icon") and not (what.startswith("team_") and what[5:] in TEAM_NAMES):
            return {"ok": False, "error": "nothing to pick"}
        title = {"bg": "Background picture for the overlays", "icon": "1st place picture for the leaderboard"}.get(
            what, f"Badge picture for {TEAM_NAMES.get(what[5:], '')}")
        holder = tk.Toplevel(self)   # keeps the dialog on top of the editor
        holder.withdraw()
        holder.attributes("-topmost", True)
        try:
            path = filedialog.askopenfilename(
                parent=holder, title=title,
                filetypes=[("Pictures", " ".join("*" + e for e in ICON_TYPES)), ("All files", "*.*")])
        finally:
            holder.destroy()
        if path:
            if what == "bg":
                self.cfg["overlay_bg_image"] = path
                save_settings(self.cfg)
                self.ov_bg_mode_var.set("image")
                self._paint_bg()
            elif what == "icon":
                self.cfg["board_icon"] = path
                save_settings(self.cfg)
                self.board_first_var.set("image")
                self._paint_board()
            else:
                self.cfg["team_pics"][what[5:]] = path
                save_settings(self.cfg)
                self._build_people()
                self._paint_team_pics()
        return dict(self._editor_state(), picked=bool(path))

    def _toggle_folder(self, name):
        closed = self.cfg["folders_closed"]
        if name in closed:
            closed.remove(name)
        else:
            closed.append(name)
        save_settings(self.cfg)
        self._layout_folders()

    def _folder_name_ok(self, name, old=None):
        name = (name or "").strip()
        taken = [f.lower() for f in self.cfg["folders"] if f != old] + [a.lower() for a in AUTO_FOLDERS]
        if not name:
            return ""
        if name.lower() in taken:
            messagebox.showinfo(APP_NAME, f'There\'s already a folder called "{name}".', parent=self)
            return ""
        return name[:40]

    def _new_folder(self, for_person=None):
        from tkinter import simpledialog
        name = self._folder_name_ok(simpledialog.askstring(APP_NAME, "Name for the new folder:", parent=self))
        if not name:
            return
        self.cfg["folders"].append(name)
        if for_person is not None:
            self.cfg["people"][for_person].setdefault("folders", []).append(name)
        save_settings(self.cfg)
        self._build_people()

    def _rename_folder(self, old):
        from tkinter import simpledialog
        name = self._folder_name_ok(simpledialog.askstring(APP_NAME, f'Rename "{old}" to:', initialvalue=old,
                                                           parent=self), old)
        if not name or name == old:
            return
        c = self.cfg
        c["folders"] = [name if f == old else f for f in c["folders"]]
        for person in c["people"]:
            person["folders"] = [name if f == old else f for f in person.get("folders", [])]
        c["folders_closed"] = [name if f == old else f for f in c["folders_closed"]]
        c["folders_pinned"] = [name if f == old else f for f in c["folders_pinned"]]
        save_settings(c)
        self._build_people()

    def _delete_folder(self, name):
        if not messagebox.askyesno(APP_NAME, f'Delete the folder "{name}"? The people in it stay in the '
                                             "tracker.", parent=self):
            return
        c = self.cfg
        c["folders"] = [f for f in c["folders"] if f != name]
        for person in c["people"]:
            person["folders"] = [f for f in person.get("folders", []) if f != name]
        c["folders_closed"] = [f for f in c["folders_closed"] if f != name]
        c["folders_pinned"] = [f for f in c["folders_pinned"] if f != name]
        save_settings(c)
        self._build_people()

    def _set_folder(self, i, folder, on):
        mine = self.cfg["people"][i].setdefault("folders", [])
        if on and folder not in mine:
            mine.append(folder)
        elif not on and folder in mine:
            mine.remove(folder)
        save_settings(self.cfg)
        self._layout_folders()

    def _folder_menu(self, i, widget):
        """📁 on a card: tick the folders this person is in, or make a new one."""
        m = tk.Menu(self, tearoff=0)
        self._menu_vars = []
        for f in self.cfg["folders"]:
            v = tk.BooleanVar(value=f in self.cfg["people"][i].get("folders", []))
            self._menu_vars.append(v)
            m.add_checkbutton(label=f, variable=v, command=lambda f=f, v=v: self._set_folder(i, f, v.get()))
        if self.cfg["folders"]:
            m.add_separator()
        m.add_command(label="New folder…", command=lambda: self._new_folder(for_person=i))
        m.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())

    def _show_card(self, i):
        """Jump to someone's full card (opening its folder) and flash it."""
        auto = "Active" if self._is_active(i) else "Inactive"
        if auto in self.cfg["folders_closed"]:
            self.cfg["folders_closed"].remove(auto)
        self.folder_search.set("")
        self._layout_folders()
        card = self.person_cards[i]
        self.update_idletasks()
        canvas = card
        while canvas is not None and not isinstance(canvas, tk.Canvas):
            canvas = canvas.master
        if canvas is not None:
            box = canvas.bbox("all")
            content = canvas.winfo_children()[0] if canvas.winfo_children() else None
            if box and content is not None:
                y = card.winfo_rooty() - content.winfo_rooty()
                canvas.yview_moveto(max(0, y - 20) / max(1, box[3]))
        card.configure(highlightbackground=ACCENT, highlightthickness=2)
        self.after(1500, lambda: card.winfo_exists() and card.configure(highlightbackground="#262C36",
                                                                         highlightthickness=1))

    def _team_img(self, team, size=16):
        """The team's badge for the app: its emblem (or your picture), else the shield."""
        pic = team_pic(team, self.cfg)
        cache = self.__dict__.setdefault("_team_imgs", {})
        # the colour only matters for the shield (no picture): otherwise a new
        # picture would be made for every colour tried
        key = (team, size, pic, icon_version(pic), "" if pic else team_color(team, self.cfg))
        if key not in cache:
            from PIL import Image, ImageTk
            try:
                im = Image.open(pic).convert("RGBA")
                im.thumbnail((size, size), Image.LANCZOS)
            except Exception:
                im = team_badge(team, size, self.cfg)
            cache[key] = ImageTk.PhotoImage(im, master=self)
        return cache[key]

    def _paint_team_btn(self, btn, team):
        try:
            img = self._team_img(team) if team in TEAM_NAMES else ""
        except Exception:
            img = ""
        btn.configure(text=(" " + TEAM_NAMES[team]) if team in TEAM_NAMES else "＋ Team", image=img,
                      fg=team_color(team, self.cfg) if team in TEAM_NAMES else MUTED)

    def _set_team(self, i, team, by=None):
        """Person i's team ("" = none). by: who changed it, for the log."""
        if i >= len(self.cfg["people"]) or self.cfg["people"][i].get("team", "") == team:
            return
        self.cfg["people"][i]["team"] = team
        save_settings(self.cfg)
        row = self.person_rows[i] if i < len(getattr(self, "person_rows", [])) else {}
        if row.get("team") is not None:
            self._paint_team_btn(row["team"], team)
        if by:
            self._add_log(time.strftime("%H:%M:%S"), f"{self._person_name(i)} is now on "
                          f"{TEAM_NAMES.get(team, 'no team')} ({by})", None)

    def _team_menu(self, i, widget):
        m = tk.Menu(self, tearoff=0)
        cur = self.cfg["people"][i].get("team", "")
        self._team_menu_var = tk.StringVar(value=cur)   # marks the current team
        for t, name, _ in TEAMS:
            m.add_radiobutton(label="  " + name, image=self._team_img(t), compound="left", value=t,
                              variable=self._team_menu_var, command=lambda t=t: self._set_team(i, t))
        m.add_separator()
        m.add_command(label="No team", command=lambda: self._set_team(i, ""))
        m.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())

    def _show_team_colors_btn(self):
        if self.board_teams_var.get():
            self.team_colors_btn.pack(side="left", padx=(8, 0))
        else:
            self.team_colors_btn.pack_forget()
            pop = getattr(self, "team_colors_pop", None)
            if pop is not None and pop.winfo_exists():
                pop.destroy()

    def _team_colors_popup(self):
        """A colour per team (names on the leaderboard, the shield), and Names in team color."""
        old = getattr(self, "team_colors_pop", None)
        if old is not None and old.winfo_exists():
            old.destroy()
            return   # the button toggles it
        btn = self.team_colors_btn
        pop = self.team_colors_pop = tk.Toplevel(self, bg=BORDER)
        pop.withdraw()  # placed once it's built (a borderless window placed early jumps to 0,0)
        pop.overrideredirect(True)
        pop.transient(self)
        inner = tk.Frame(pop, bg=CARD)
        inner.pack(padx=1, pady=1)
        body = tk.Frame(inner, bg=CARD)
        body.pack(padx=12, pady=10)
        close = lambda e=None: pop.winfo_exists() and pop.destroy()
        pop.bind("<Escape>", close)
        self._label(body, "Team colors", CARD, TEXT, 10, True).pack(anchor="w")
        grid = tk.Frame(body, bg=CARD)
        grid.pack(fill="x", pady=(8, 0))
        self.team_color_btns = {}
        for r, (t, name, _) in enumerate(TEAMS):
            tk.Label(grid, image=self._team_img(t, 18), bg=CARD).grid(row=r, column=0, pady=3)
            self._label(grid, name, CARD, TEXT, 9).grid(row=r, column=1, sticky="w", padx=(6, 12))
            b = tk.Button(grid, relief="flat", bd=0, cursor="hand2", font=("Segoe UI", 9, "bold"), width=10,
                          bg=BG_DARK, activebackground=BG_DARK, highlightthickness=1,
                          highlightbackground=BORDER, command=lambda t=t: self._pick_team_color(t))
            b.grid(row=r, column=2, sticky="w")
            reset = tk.Label(grid, text="Reset", bg=CARD, fg=ACCENT, cursor="hand2",
                             font=("Segoe UI", 9, "underline"))
            reset.grid(row=r, column=3, sticky="w", padx=(8, 0))
            reset.bind("<Button-1>", lambda e, t=t: self._set_team_color(t, ""))
            self.team_color_btns[t] = (b, reset)
        nr = tk.Frame(body, bg=CARD)
        nr.pack(fill="x", pady=(10, 0))
        self._label(nr, "Names in team color", CARD, TEXT, 9).pack(side="left")
        self.team_names_var = tk.BooleanVar(value=bool(self.cfg["team_names_tinted"]))
        Switch(nr, self.team_names_var, bg=CARD).pack(side="right", padx=(12, 0))

        def names_changed(*_):
            self.cfg["team_names_tinted"] = bool(self.team_names_var.get())
            save_settings(self.cfg)
        self.team_names_var.trace_add("write", names_changed)
        self._label(body, "Set each player's team with the team button on their card.",
                    CARD, "#8A93A3", 8).pack(anchor="w", pady=(6, 0))
        ttk.Button(body, text="Close", command=close).pack(anchor="w", pady=(8, 0))
        self._paint_team_colors()
        pop.update_idletasks()
        x = btn.winfo_rootx()
        y = btn.winfo_rooty() + btn.winfo_height() + 4
        where = f"+{min(x, self.winfo_rootx() + self.winfo_width() - pop.winfo_reqwidth())}+{y}"
        pop.geometry(where)
        pop.deiconify()
        pop.update_idletasks()
        pop.geometry(where)

    def _paint_team_colors(self):
        for t, (b, reset) in getattr(self, "team_color_btns", {}).items():
            if not b.winfo_exists():
                continue
            c = team_color(t, self.cfg)
            b.configure(text=c.upper(), fg=c, activeforeground=c)
            if t in self.cfg["team_colors"]:
                reset.grid()
            else:
                reset.grid_remove()

    def _pick_team_color(self, team):
        pop = getattr(self, "team_colors_pop", None)
        _, hx = colorchooser.askcolor(color=team_color(team, self.cfg), title=f"{TEAM_NAMES[team]} colour",
                                      parent=pop if pop is not None and pop.winfo_exists() else self)
        if hx:
            self._set_team_color(team, hx)

    def _set_team_color(self, team, hx):
        """Team's colour ("" = back to the game's)."""
        hx = (hx or "").upper()
        if hx and HEX_COLOR.fullmatch(hx) and hx != TEAM_DEFAULT_COLORS[team]:
            self.cfg["team_colors"][team] = hx
        else:
            self.cfg["team_colors"].pop(team, None)
        self._save_soon()
        self._paint_team_colors()
        for k, row in enumerate(getattr(self, "person_rows", [])):   # card team buttons in the new colour
            if row.get("team") is not None and row["team"].winfo_exists() and k < len(self.cfg["people"]):
                self._paint_team_btn(row["team"], self.cfg["people"][k].get("team", ""))

    def _pick_team_pic(self, team):
        path = filedialog.askopenfilename(
            title=f"Badge picture for {TEAM_NAMES[team]}",
            filetypes=[("Pictures", " ".join("*" + e for e in ICON_TYPES)), ("All files", "*.*")])
        if path:
            self.cfg["team_pics"][team] = path
            save_settings(self.cfg)
            self._build_people()   # card buttons show the new picture
        self._paint_team_pics()

    def _clear_team_pic(self, team):
        self.cfg["team_pics"].pop(team, None)
        save_settings(self.cfg)
        self._paint_team_pics()
        self._build_people()   # card buttons back to the emblem

    def _paint_team_pics(self):
        for t, (btn, x) in getattr(self, "team_pic_btns", {}).items():
            pic = self.cfg["team_pics"].get(t, "")
            btn.configure(text=f"{TEAM_NAMES[t]}: " + (os.path.basename(pic)[:14] if pic else "emblem"))
            if pic:
                x.pack(side="left", padx=(2, 8))
            else:
                x.pack_forget()

    def _paint_pin(self, pin, on):
        """📌: lit while this friend stays on the leaderboard after a save."""
        if on:
            pin.configure(bg="#16213A", fg=ACCENT, highlightbackground=ACCENT)
        else:
            pin.configure(bg=BG_DARK, fg=MUTED, highlightbackground="#3A4352")

    def _paint_twitch_btn(self, btn, p):
        if p.get("twitch"):
            btn.configure(text="✓ Linked", bg="#12301F", fg="#5FE39B", highlightbackground="#2E7D4F")
        else:
            btn.configure(text="🔗 Link", bg=BG_DARK, fg=ACCENT, highlightbackground="#3A4352")

    def _link_twitch(self, i, login, by):
        """Link person i to a Twitch login ("" unlinks). One player per account."""
        c = self.cfg
        for k, other in enumerate(c["people"]):
            if login and k != i and other.get("twitch") == login:
                other.update(twitch="", twitch_by="", twitch_at="")
        c["people"][i].update(twitch=login, twitch_by=by if login else "",
                              twitch_at=time.strftime("%Y-%m-%d %H:%M") if login else "")
        save_settings(c)
        for k, row in enumerate(getattr(self, "person_rows", [])):
            if row.get("twitch") is not None and k < len(c["people"]):
                self._paint_twitch_btn(row["twitch"], c["people"][k])

    def _set_twitch(self, i, var):
        """From the Link box: a name or twitch.tv link. False if it isn't one."""
        raw = var.get().strip()
        login = twitch_login(raw)
        if not login:
            self._add_log(time.strftime("%H:%M:%S"), f'"{raw}" isn\'t a Twitch name or link.', "#FACC15")
            return False
        var.set(login)
        if login != self.cfg["people"][i].get("twitch", ""):
            self._link_twitch(i, login, "you (in the app)")
            self._add_log(time.strftime("%H:%M:%S"), f"Twitch: {self._person_name(i)} linked to @{login}", "#5FE39B")
        return True

    def _twitch_popup(self, i, btn):
        """Link: type a Twitch name. Linked: who it is, who linked it and when,
        open their channel, or Unlink."""
        if getattr(self, "twitch_pop", None) is not None and self.twitch_pop.winfo_exists():
            self.twitch_pop.destroy()
        p = self.cfg["people"][i]
        pop = self.twitch_pop = tk.Toplevel(self, bg=BORDER)
        pop.withdraw()  # placed once it's built (a borderless window placed early jumps to 0,0)
        pop.overrideredirect(True)
        pop.transient(self)
        inner = tk.Frame(pop, bg=CARD)
        inner.pack(padx=1, pady=1)
        body = tk.Frame(inner, bg=CARD)
        body.pack(padx=12, pady=10)
        close = lambda e=None: pop.winfo_exists() and pop.destroy()
        pop.bind("<Escape>", close)
        name = self._person_name(i)
        if p.get("twitch"):
            login = p["twitch"]
            self._label(body, f"{name} is linked", CARD, TEXT, 10, True).pack(anchor="w")
            # both hidden (handy on stream): click to show; the Twitch user can then be changed
            grid = tk.Frame(body, bg=CARD)
            grid.pack(fill="x", pady=(6, 0))
            HIDDEN = "••••••••••  (click to show)"
            self._label(grid, "Twitch", CARD, LABEL, 9).grid(row=0, column=0, sticky="w")
            tw_cell = tk.Frame(grid, bg=CARD)
            tw_cell.grid(row=0, column=1, sticky="w", padx=(10, 0), pady=2)
            tw_hidden = tk.Label(tw_cell, text=HIDDEN, bg=CARD, fg=ACCENT, cursor="hand2", font=("Segoe UI", 9))
            tw_hidden.pack(side="left")
            self._label(grid, "Steam ID", CARD, LABEL, 9).grid(row=1, column=0, sticky="w")
            st_cell = tk.Frame(grid, bg=CARD)
            st_cell.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=2)
            acct = p.get("steam_acct", 0)
            steam_id = str(STEAM64_BASE + acct) if acct else ""
            st_hidden = tk.Label(st_cell, text=HIDDEN if steam_id else "not found yet (once they're seen online)",
                                 bg=CARD, fg=ACCENT if steam_id else "#5C6575", cursor="hand2" if steam_id else "",
                                 font=("Segoe UI", 9))
            st_hidden.pack(side="left")
            note = self._label(body, "", CARD, "#FACC15", 8)

            def show_twitch(_=None):
                tw_hidden.destroy()
                v = tk.StringVar(value=login)
                e = ttk.Entry(tw_cell, textvariable=v, width=22)
                e.pack(side="left")
                e.focus_set()
                e.select_range(0, "end")

                def save(_=None):
                    new = twitch_login(v.get())
                    if not new:
                        note.configure(text="That isn't a Twitch name or link.")
                        note.pack(anchor="w")
                        return
                    if new != login:
                        self._link_twitch(i, new, "you (in the app)")
                        self._add_log(time.strftime("%H:%M:%S"), f"Twitch: {name} changed from @{login} to @{new}",
                                      "#5FE39B")
                    close()
                ttk.Button(tw_cell, text="Save", width=6, command=save).pack(side="left", padx=(6, 0))
                chn = tk.Label(tw_cell, text="Open", bg=CARD, fg=ACCENT, cursor="hand2",
                               font=("Segoe UI", 9, "underline"))
                chn.pack(side="left", padx=(6, 0))
                chn.bind("<Button-1>", lambda ev: self._open_url(f"https://www.twitch.tv/{login}"))
                e.bind("<Return>", save)
                self.twitch_change, self.twitch_change_save = v, save

            def show_steam(_=None):
                st_hidden.destroy()
                v = tk.StringVar(value=steam_id)
                e = tk.Entry(st_cell, textvariable=v, width=20, state="readonly", relief="flat",
                             readonlybackground=BG_DARK, fg=TEXT, font=("Segoe UI", 9))
                e.pack(side="left")   # read-only, but you can select and copy it
            tw_hidden.bind("<Button-1>", show_twitch)
            if steam_id:
                st_hidden.bind("<Button-1>", show_steam)
            self.twitch_show, self.steam_show = show_twitch, show_steam
            if p.get("twitch_by") or p.get("twitch_at"):
                when = " · ".join(x for x in (p.get("twitch_at", ""), f"by {p['twitch_by']}"
                                              if p.get("twitch_by") else "") if x)
                self._label(body, f"Linked {when}", CARD, "#8A93A3", 8).pack(anchor="w", pady=(4, 0))
            self._label(body, "They join with !join wardogs leaderboard", CARD, "#5C6575", 8).pack(
                anchor="w", pady=(2, 0))
            bt = tk.Frame(body, bg=CARD)
            bt.pack(fill="x", pady=(10, 0))

            def unlink():
                self._link_twitch(i, "", "")
                self._add_log(time.strftime("%H:%M:%S"), f"Twitch: {name} unlinked from @{login}", "#FACC15")
                close()
            self.twitch_unlink = ttk.Button(bt, text="Unlink", command=unlink)
            self.twitch_unlink.pack(side="left")
            ttk.Button(bt, text="Close", command=close).pack(side="left", padx=(8, 0))
        else:
            self._label(body, f"Link {name} to a Twitch account", CARD, TEXT, 10, True).pack(anchor="w")
            self._label(body, "Their Twitch name, or a twitch.tv link", CARD, "#8A93A3", 8).pack(anchor="w")
            v = tk.StringVar()
            e = ttk.Entry(body, textvariable=v, width=28)
            e.pack(fill="x", pady=(6, 0))
            note = self._label(body, "", CARD, "#FACC15", 8)
            note.pack(anchor="w")
            bt = tk.Frame(body, bg=CARD)
            bt.pack(fill="x", pady=(8, 0))

            def link(_=None):
                if self._set_twitch(i, v):
                    close()
                else:
                    note.configure(text="That isn't a Twitch name or link.")
            self.twitch_entry, self.twitch_link = v, link
            ttk.Button(bt, text="Link", command=link).pack(side="left")
            ttk.Button(bt, text="Cancel", command=close).pack(side="left", padx=(8, 0))
            e.bind("<Return>", link)
            pop.after(50, e.focus_set)
        pop.update_idletasks()
        x = btn.winfo_rootx()
        y = btn.winfo_rooty() + btn.winfo_height() + 4
        where = f"+{min(x, self.winfo_rootx() + self.winfo_width() - pop.winfo_reqwidth())}+{y}"
        pop.geometry(where)
        pop.deiconify()
        pop.update_idletasks()
        pop.geometry(where)
        # closes when you click anywhere else in the window
        pop.bind("<FocusOut>", lambda e: pop.after(150, lambda: pop.winfo_exists() and
                                                    pop.focus_get() is None and close()))
        if p.get("twitch"):
            pop.after(50, pop.focus_set)

    def _open_url(self, url):
        import webbrowser
        webbrowser.open(url)

    def _got_acct(self, i, who, acct, name):
        """The tracker found friend i's Steam account number. Keep it, and if
        they've changed their Steam name, follow them to the new one."""
        people = self.cfg["people"]
        if i >= len(people) or people[i]["who"] != who:
            return  # changed in the window meanwhile
        p = people[i]
        p["steam_acct"] = acct
        if name and name.lower() != who.lower():
            p["who"] = name
            self._add_log(time.strftime("%H:%M:%S"), f'"{who}" changed their Steam name to "{name}" — '
                                                     "still tracking them.", "#5FE39B")
            self.after_idle(self._build_people)
        save_settings(self.cfg)

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

    def _fold(self, key):
        """Streamer.bot / Browser overlays: fold away or open (remembered)."""
        self.cfg[key] = not self.cfg[key]
        save_settings(self.cfg)
        self._paint_folds()

    def _paint_folds(self):
        for key, head, body, title in (("sb_open", self.sb_head, self.sb_body, "Streamer.bot"),):
            on = bool(self.cfg[key])
            head.configure(text=("▾  " if on else "▸  ") + title + ("" if on else "  (hidden)"))
            if on:
                body.pack(fill="x", after=head)
            else:
                body.pack_forget()
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
        if self.cfg.get("folders_on") and {"own_file", "board", "overlay", "label"} & set(changes):
            self.after_idle(self._folders_tick)
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

    def _set_twitch_names(self):
        """Advanced › Twitch names by default: everyone on Steam name moves to
        Twitch name (and back when it's turned off); new people start on it."""
        on = bool(self.twitch_names_var.get())
        if getattr(self, "_loading", False) or on == self.cfg["twitch_names"]:
            return
        self.cfg["twitch_names"] = on
        old, new = ("steam", "twitch") if on else ("twitch", "steam")
        for p in self.cfg["people"]:
            if p.get("name_mode") == old:
                p["name_mode"] = new
        save_settings(self.cfg)
        self._build_people()

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
        self.cfg["people"].append(new_person(cfg=self.cfg))
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
        c["overlays_hidden"] = bool(self.ov_hidden_var.get())
        if bool(self.folders_on_var.get()) != c["folders_on"]:
            c["folders_on"] = bool(self.folders_on_var.get())
            self.after_idle(self._build_people)
        c["sb_send"] = bool(self.sb_send_var.get())
        c["sb_url"] = self.sb_url_var.get().strip() or DEFAULTS["sb_url"]
        c["sb_action"] = self.sb_action_var.get().strip() or DEFAULTS["sb_action"]
        c["save_on_game_over"] = bool(self.save_auto_var.get())
        c["board_clear"] = True   # saving always resets the leaderboard now
        c["board_pin_default"] = bool(self.pin_default_var.get())
        if bool(self.board_teams_var.get()) != c["board_teams"]:
            c["board_teams"] = bool(self.board_teams_var.get())
            self.after_idle(self._build_people)   # show / hide the team buttons
        c["preview_full"] = bool(self.preview_full_var.get())   # unused now: the Preview window has its own toggle
        hk_was = (c["save_hotkey_on"], c["save_hotkey"])
        c["save_hotkey_on"] = bool(self.hk_on_var.get())
        mods = [n for v, n in ((self.hk_ctrl, "Ctrl"), (self.hk_alt, "Alt"), (self.hk_shift, "Shift")) if v.get()]
        c["save_hotkey"] = "+".join(mods + [self.hk_key_var.get() or "S"])
        if (c["save_hotkey_on"], c["save_hotkey"]) != hk_was:
            self._update_hotkey()
        sb_was = c["sb_on"]
        c["sb_on"] = bool(self.sb_on_var.get())
        if c["sb_on"] != sb_was:
            self._update_overlay_server()
            self.after_idle(self._build_people)  # show / hide the Twitch boxes
        self._paint_sb()
        self._paint_hotkey()
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
                say("OBS connected — now set an OBS source on someone's card (under Save to "
                    "file).", "#FACC15")
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
            messagebox.showinfo(APP_NAME, "Turn on Save to file for someone, or the leaderboard, "
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

    # ---- Streamer.bot: commands in ----
    def _api_request(self, cmd, params):
        """On the feed's thread: check the key, then let the window do it."""
        if not self.cfg.get("sb_on"):
            return 403, {"ok": False, "error": "Streamer.bot control is off in the tracker (Advanced › Streamer.bot)"}
        if not hmac.compare_digest(str(params.pop("key", "")), str(self.cfg.get("api_key", ""))):
            return 403, {"ok": False, "error": "wrong or missing key — copy the commands from the tracker again"}
        box = {"done": threading.Event(), "reply": (504, {"ok": False, "error": "the tracker is busy"})}
        self.events.put(("api", cmd, params, box))
        box["done"].wait(5)
        return box["reply"]

    def _find_person(self, name):
        n = str(name).strip().lower()
        people, res = self.cfg["people"], getattr(self, "current_people", []) or []
        for i, p in enumerate(people):
            r = res[i] if i < len(res) else {}
            names = {p["who"].lower(), p["label"].lower(), str(r.get("name", "")).lower(),
                     str(r.get("label", "")).lower()} - {""}
            if n in names or (i == 0 and not p["who"] and n in ("me", "you", "self")):
                return i
        return None

    def _api(self, cmd, q):
        """A Streamer.bot command, on the window's thread. Returns (status, reply)."""
        c, ok = self.cfg, lambda **kw: (200, dict(ok=True, **kw))
        bad = lambda code, msg: (code, {"ok": False, "error": msg})
        stamp = time.strftime("%H:%M:%S")
        act = cmd.rsplit("/", 1)[-1]

        def flip(current):
            return {"on": True, "off": False, "toggle": not current}[act]

        if cmd == "status":
            res = getattr(self, "current_people", []) or []
            return ok(running=bool(self.worker and self.worker.is_alive()), hidden=c["overlays_hidden"],
                      leaderboard=c["board_on"], people=[
                          {"name": (res[i]["label"] if i < len(res) else p["who"]) or p["who"] or "you",
                           "amount": res[i]["text"] if i < len(res) else "", "leaderboard": p["board"],
                           "overlay": p["overlay"]} for i, p in enumerate(c["people"])])
        if cmd in ("hide", "show", "hide/toggle"):
            v = True if cmd == "hide" else False if cmd == "show" else not c["overlays_hidden"]
            self.ov_hidden_var.set(v)
            self._add_log(stamp, "Streamer.bot: overlays " + ("hidden" if v else "shown"), None)
            return ok(hidden=v)
        if cmd in ("board/on", "board/off", "board/toggle"):
            v = flip(c["board_on"])
            self.board_on_var.set(v)
            self._add_log(stamp, f"Streamer.bot: leaderboard {'on' if v else 'off'}", None)
            return ok(leaderboard=v)
        if cmd in ("board/add", "board/remove"):
            name = q.get("name", "").strip()
            if not name:
                return bad(400, "add &name=… (a Steam name, or the label you gave them)")
            i = self._find_person(name)
            if cmd == "board/remove":
                if i is None:
                    return bad(404, f'no one called "{name}" in the tracker')
                self.person_rows[i]["board"].set(False)
            elif i is None:
                c["people"].append(new_person(name, c))  # new people go on the leaderboard
                save_settings(c)
                self._build_people()
            else:
                self.person_rows[i]["board"].set(True)
            self._add_log(stamp, f"Streamer.bot: {name} {'off' if cmd.endswith('remove') else 'on'} "
                                 f"the leaderboard", None)
            return ok(name=name, leaderboard=cmd == "board/add")
        if cmd in ("overlay/on", "overlay/off", "overlay/toggle"):
            name = q.get("name", "").strip()
            if name.lower() in ("leaderboard", "board"):
                v = flip(c["board_overlay"])
                self.board_overlay_var.set(v)
                return ok(name="leaderboard", overlay=v)
            if name:
                i = self._find_person(name)
                if i is None:
                    return bad(404, f'no one called "{name}" in the tracker')
                v = flip(c["people"][i]["overlay"])
                self.person_rows[i]["overlay"].set(v)
                return ok(name=name, overlay=v)
            # every overlay: off remembers which were on, on brings those back
            on_now = [("person", p["who"]) for p in c["people"] if p["overlay"]] + \
                     ([("board", "")] if c["board_overlay"] else [])
            v = flip(bool(on_now))
            if not v:
                if on_now:
                    c["overlays_were_on"] = on_now
                for row in self.person_rows:
                    row["overlay"].set(False)
                self.board_overlay_var.set(False)
            else:
                back = [tuple(k) for k in c["overlays_were_on"] if isinstance(k, (list, tuple)) and len(k) == 2] \
                    or [("person", p["who"]) for p in c["people"]] + [("board", "")]
                for kind, who in back:
                    if kind == "board":
                        self.board_overlay_var.set(True)
                        continue
                    i = next((k for k, p in enumerate(c["people"]) if p["who"] == who), None)
                    if i is not None and i < len(self.person_rows):
                        self.person_rows[i]["overlay"].set(True)
            save_settings(c)
            self._add_log(stamp, f"Streamer.bot: overlays {'on' if v else 'off'}", None)
            return ok(overlays=v)
        if cmd == "color":
            keys = {"text": "overlay_color", "profit": "overlay_profit_color", "loss": "overlay_loss_color",
                    "bg": "overlay_bg_color", "app_profit": "profit_color", "app_loss": "loss_color"}
            changed = {}
            for k, key in keys.items():
                if k in q:
                    v = "#" + q[k].strip().lstrip("#").upper()
                    if not HEX_COLOR.fullmatch(v):
                        return bad(400, f"{k}={q[k]} isn't a colour like 5FE39B")
                    c[key] = changed[k] = v
            if "opacity" in q:
                try:
                    self.ov_opacity_var.set(max(0, min(int(float(q["opacity"])), 100)))
                    changed["opacity"] = self.ov_opacity_var.get()
                except ValueError:
                    return bad(400, "opacity is 0–100")
            if "pl" in q:
                pl = q["pl"].lower()
                if pl not in ("hud", "board", "both", "off", "none"):
                    return bad(400, "pl is hud, board, both or off")
                self.ov_pl_var.set(pl in ("hud", "both"))
                self.ov_pl_board_var.set(pl in ("board", "both"))
                changed["pl"] = pl
            if not changed:
                return bad(400, "add a colour: text, profit, loss, bg, opacity, pl, app_profit or app_loss")
            save_settings(c)
            self._paint_color_buttons()
            self._paint_overlay_colors()
            self._show_value(self.current_text)
            self._add_log(stamp, "Streamer.bot: colours " + ", ".join(f"{k} {v}" for k, v in changed.items()), None)
            return ok(**changed)
        if cmd.startswith("twitch/"):
            return self._api_twitch(cmd[7:], q)
        if cmd == "team/set":
            name, team = q.get("name", "").strip(), parse_team(q.get("team", "none"))
            i = self._find_person(name) if name else None
            if i is None:
                return bad(404, f'no one called "{name}" in the tracker')
            if team is None:
                return bad(400, "team is " + ", ".join(t for t, _, _ in TEAMS) + " or none")
            self._set_team(i, team, "Streamer.bot")
            return ok(name=name, team=TEAM_NAMES.get(team, ""))
        if cmd == "refresh":
            if not (self.worker and self.worker.is_alive()):
                return bad(409, "the tracker isn't running")
            self.worker.reload_now = True
            return ok(refresh=True)
        if cmd in ("board/end", "board/clear"):
            # silent: ready for the next game, nothing saved
            before = sum(1 for p in c["people"] if p["board"])
            self._reset_board("Streamer.bot", save=False)
            after = sum(1 for p in c["people"] if p["board"])
            return ok(cleared=before - after, kept=after)
        if cmd == "save":
            out = self._reset_board("Streamer.bot", save=True)
            return ok(saved=out) if out else bad(409, "nothing to save — no one has an amount (the board was reset)")
        if cmd == "gameover":
            res = getattr(self, "current_people", []) or []
            text = (res[0].get("text") or res[0].get("last", "")) if res else ""
            if not text:
                return bad(409, "no amount to end the game with")
            self._game_over(text)
            return ok(amount=text)
        return bad(404, f'unknown command "{cmd}" — try: ' + ", ".join(n for n, _ in SB_COMMANDS))

    def _ingame_list(self):
        """Who's in WARDOGS right now: [(name, amount, twitch)] — you and your
        Steam friends (whether you track them or not)."""
        out = []
        for item in getattr(self, "ingame_friends", []) or []:
            name, status = item[0], item[1]
            i = self._find_person(name)
            if i is None and name == (getattr(self, "current_people", None) or [{}])[0].get("name"):
                i = 0
            tw = self.cfg["people"][i].get("twitch", "") if i is not None else ""
            out.append((name, extract_money(status, self.cfg["space"]), tw))
        return out

    def _api_twitch(self, what, q):
        """Twitch chat, through Streamer.bot. Answers carry "text" to whisper."""
        c = self.cfg
        user = twitch_login(q.get("user", ""))
        stamp = time.strftime("%H:%M:%S")
        say = lambda text, **kw: (200, dict(ok=True, text=text, **kw))
        no = lambda text: (200, {"ok": False, "text": text})
        if what in ("join", "team"):
            if not user:
                return no("Add &user=%userName% to the link.")
            i = next((k for k, p in enumerate(c["people"]) if p.get("twitch") == user), None)
            if i is None:
                return no(f"@{user} isn't linked to a WARDOGS player yet — ask a mod to link you "
                          "(!wardogs friends shows who's playing).")
            name = self._person_name(i)
            # a team after the command: !join wardogs leaderboard valkyra / !wardogs team lonestar
            words = q.get("input", "").split()
            team = parse_team(words[-1]) if words else None
            if what == "team":
                if team is None:
                    return no("Pick a team: " + ", ".join(n for _, n, _ in TEAMS) + " (or none).")
                self._set_team(i, team, f"@{user}")
                return say(f"{name} is now on {TEAM_NAMES[team]}." if team else f"{name} has no team now.",
                           name=name, team=TEAM_NAMES.get(team, ""))
            if team is not None:
                self._set_team(i, team, f"@{user}")
            if not c["people"][i]["board"]:
                self.person_rows[i]["board"].set(True)
                self._add_log(stamp, f"Twitch: {user} joined the leaderboard as {name}", "#5FE39B")
            on_team = f" for {TEAM_NAMES[c['people'][i]['team']]}" if c["people"][i].get("team") else ""
            if not c["board_on"]:
                return say(f"You're on the leaderboard as {name}{on_team} (it's switched off right now).", name=name)
            return say(f"You're on the WARDOGS leaderboard as {name}{on_team}.", name=name)
        if what == "friends":
            rows = self._ingame_list()
            if user:
                now = time.time()
                self._twitch_lists = {u: v for u, v in getattr(self, "_twitch_lists", {}).items()
                                      if now - v[0] < 900}
                self._twitch_lists[user] = (now, [r[0] for r in rows])
            if not rows:
                return say("No one is in WARDOGS right now.", players=[])
            parts = [f"{n}. {name}{' ' + amt if amt else ''}{' (@' + tw + ')' if tw else ''}"
                     for n, (name, amt, tw) in enumerate(rows, 1)]
            text = "In WARDOGS now: " + " · ".join(parts)
            if len(text) > 480:  # Twitch whispers stop at 500 characters
                keep = []
                for part in parts:
                    if len("In WARDOGS now: " + " · ".join(keep + [part])) > 440:
                        break
                    keep.append(part)
                text = "In WARDOGS now: " + " · ".join(keep) + f" · …and {len(parts) - len(keep)} more"
            return say(text + " — mods: !wardogs tracker link <number> @viewer",
                       players=[{"n": n, "name": r[0], "amount": r[1], "twitch": r[2]}
                                for n, r in enumerate(rows, 1)])
        if what == "link":
            # mods / broadcaster only: Streamer.bot's command permissions decide who
            words = q.get("input", "").replace(",", " ").split()
            if not words:
                return no("Use: !wardogs tracker link <number from !wardogs friends> @viewer")
            pick, who = words[0], twitch_login(words[1]) if len(words) > 1 else user
            if not who:
                return no("Add the viewer: !wardogs tracker link <number> @viewer")
            saved = getattr(self, "_twitch_lists", {}).get(user)
            names = saved[1] if saved and time.time() - saved[0] < 900 else [r[0] for r in self._ingame_list()]
            if pick.isdigit():
                if not 1 <= int(pick) <= len(names):
                    return no(f"There's no {pick} on the list — send !wardogs friends again.")
                name = names[int(pick) - 1]
            else:
                name = pick
            i = self._find_person(name)
            acct = next((x[2] for x in getattr(self, "ingame_friends", []) or []
                         if len(x) > 2 and x[0] == name and x[2]), 0)
            if i is None and acct:
                i = next((k for k, p in enumerate(c["people"]) if p.get("steam_acct") == acct), None)
            if i is None:
                c["people"].append(dict(new_person(name, c), board=False, steam_acct=acct))
                i = len(c["people"]) - 1
                self._link_twitch(i, who, user or "Twitch chat")
                self._build_people()
            else:
                self._link_twitch(i, who, user or "Twitch chat")
            self._add_log(stamp, f"Twitch: {name} linked to @{who}" + (f" by {user}" if user and user != who else ""),
                          "#5FE39B")
            return say(f"Linked {name} to @{who}. They can type !join wardogs leaderboard now.", name=name,
                       twitch=who)
        return (404, {"ok": False, "error": f'unknown Twitch command "{what}"'})

    def _sb_base(self):
        return f"http://127.0.0.1:{self.cfg['overlay_port']}/api/"

    def _copy_sb_commands(self):
        key = self.cfg["api_key"]
        text = ["Wardogs Profit Tracker — Streamer.bot commands",
                "In Streamer.bot add a sub-action: Core › Network › Fetch URL, and paste one of these.",
                "Keep the key private (it's like a password); New key in the tracker replaces it.", ""]
        for name, what in SB_COMMANDS:
            text.append(f"{self._sb_base()}{name}?key={key}")
            text.append(f"    {what}")
        self.clipboard_clear()
        self.clipboard_append("\n".join(text))
        self._add_log(time.strftime("%H:%M:%S"), "Copied the Streamer.bot commands — paste them into a "
                                                 "text file to keep (they include your key).", "#5FE39B")
        if not self.cfg["sb_on"]:
            self._add_log(time.strftime("%H:%M:%S"), "Turn on Let Streamer.bot control the tracker for "
                                                     "them to work.", "#FACC15")

    def _build_sb_command_list(self, parent):
        """Every Streamer.bot command, grouped, each with Copy (its full link with
        your key). The key itself isn't shown. Scrolls in its own box."""
        box = tk.Frame(parent, bg=BORDER)
        box.pack(fill="x", pady=(8, 0))
        cv = tk.Canvas(box, bg=BG_DARK, highlightthickness=0, bd=0, height=230)
        bar = ttk.Scrollbar(box, orient="vertical", command=cv.yview, style="Log.Vertical.TScrollbar")
        cv.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y", padx=(0, 1), pady=1)
        cv.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        inner = tk.Frame(cv, bg=BG_DARK)
        win = cv.create_window((0, 0), window=inner, anchor="nw")
        cv.bind("<Configure>", lambda e: cv.itemconfigure(win, width=e.width))
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        # the wheel scrolls this list while the mouse is over it (not the whole window)
        def wheel(e):
            cv.yview_scroll(int(-e.delta / 120), "units")
            return "break"
        def group(name):
            if name.startswith("twitch/"):
                return "Twitch chat"
            if name in ("hide", "show", "hide/toggle") or name.startswith("overlay/"):
                return "Overlays"
            if name.startswith("board/") or name == "team/set":
                return "Leaderboard"
            if name == "color":
                return "Colours"
            return "Game"
        order = ("Overlays", "Leaderboard", "Colours", "Game", "Twitch chat")
        for g in order:
            cmds = [(n, d) for n, d in SB_COMMANDS if group(n) == g]
            if not cmds:
                continue
            self._label(inner, g, BG_DARK, TEXT, 9, True).pack(anchor="w", padx=8, pady=(8, 2))
            for name, what in cmds:
                row = tk.Frame(inner, bg=BG_DARK)
                row.pack(fill="x", padx=8, pady=2)
                tk.Label(row, text=name, bg=BG_DARK, fg=ACCENT, font=("Consolas", 9), width=15,
                         anchor="w").pack(side="left", anchor="n")
                cp = tk.Label(row, text="Copy", bg=BG_DARK, fg=ACCENT, cursor="hand2",
                              font=("Segoe UI", 9, "underline"))
                cp.pack(side="right", anchor="n", padx=(8, 0))
                cp.bind("<Button-1>", lambda e, n=name: self._copy_sb_command(n))
                d = tk.Label(row, text=what, bg=BG_DARK, fg="#8A93A3", font=("Segoe UI", 8),
                             justify="left", anchor="w", wraplength=330)
                d.pack(side="left", fill="x", expand=True)
        self._label(inner, f"Each link is {self._sb_base()}<command>?key=•••• — Copy puts the full "
                           "link (with your key) on the clipboard.", BG_DARK, "#5C6575", 8).pack(
            anchor="w", padx=8, pady=(8, 8))
        for w_ in [cv, inner] + [c for r in inner.winfo_children() for c in [r] + r.winfo_children()]:
            w_.bind("<MouseWheel>", wheel)

    def _copy_sb_command(self, name):
        """One command's full link (with your key), ready for Fetch URL."""
        self.clipboard_clear()
        self.clipboard_append(f"{self._sb_base()}{name}?key={self.cfg['api_key']}")
        self._add_log(time.strftime("%H:%M:%S"), f"Copied the Streamer.bot command “{name}” (it includes "
                                                 "your key).", "#5FE39B")
        if not self.cfg["sb_on"]:
            self._add_log(time.strftime("%H:%M:%S"), "Turn on Let Streamer.bot control the tracker for "
                                                     "it to work.", "#FACC15")

    def _new_api_key(self):
        self.cfg["api_key"] = secrets.token_urlsafe(12)
        save_settings(self.cfg)
        self._add_log(time.strftime("%H:%M:%S"), "New Streamer.bot key — the old commands stop working; "
                                                 "Copy commands again.", "#FACC15")

    def _paint_sb(self):
        c = self.cfg
        self.sb_cmd_note.configure(
            text=(f"Commands are links on {self._sb_base()}… — Streamer.bot opens them with Fetch URL. "
                  "Copy commands puts the full list (with your key) on the clipboard.") if c["sb_on"] else
            "Off: nothing can control the tracker. Turn on to hide overlays, change colours, add people… "
            "from Streamer.bot.")

    def _test_sb(self):
        cfg = copy.deepcopy(self.cfg)
        rows = standings(cfg["people"], getattr(self, "current_people", []) or [], cfg) or \
            [{"label": "RotorBird", "text": "+$18,400"}, {"label": "Chopper", "text": "+$12,750"}]
        args = game_args(rows[0]["label"], rows[0]["text"], rows, test=True)

        def run():
            err = sb_send_action(cfg, args)
            self.events.put(("log", time.strftime("%H:%M:%S"),
                             f'Streamer.bot test OK — ran "{cfg["sb_action"]}"' if not err else
                             f"Streamer.bot test failed: {err}", "#5FE39B" if not err else "#FF8A7A"))
        threading.Thread(target=run, daemon=True).start()

    # ---- end of game ----
    def _game_over(self, text):
        c = self.cfg
        res = getattr(self, "current_people", []) or []
        label = (res[0]["label"] if res else "") or "You"
        self._add_log(time.strftime("%H:%M:%S"), f"Game over: {label} {text}", self.color_for(text))
        # nothing is saved or reset by itself now: 🧹 Reset does that (saving first if you want)
        if c["sb_send"]:
            cfg = copy.deepcopy(c)
            args = game_args(label, text, standings(cfg["people"], res, cfg))

            def run():
                err = sb_send_action(cfg, args)
                self.events.put(("log", time.strftime("%H:%M:%S"),
                                 f'Sent the result to Streamer.bot ("{cfg["sb_action"]}").' if not err else
                                 f"Couldn't send the result to Streamer.bot: {err}",
                                 "#5FE39B" if not err else "#FF8A7A"))
            threading.Thread(target=run, daemon=True).start()

    def _save_leaderboard(self, why):
        """Saves the standings as a dated .txt now and a .png of the overlay
        shortly after. Returns the .txt path, or "" when there's nothing to save."""
        stamp = time.strftime("%H:%M:%S")
        if time.monotonic() - getattr(self, "_last_save", -10) < 2:
            self._add_log(stamp, f"Already saved a moment ago — ignored the extra save ({why}).", None)
            return getattr(self, "_last_save_path", "")
        c = copy.deepcopy(self.cfg)
        rows = standings(c["people"], getattr(self, "current_people", []) or [], c)
        if not rows:
            self._add_log(stamp, "Nothing to save — no one on the leaderboard has an amount yet.", "#FACC15")
            return ""
        now = time.localtime()
        base = os.path.join(saved_dir(), "Leaderboard " + time.strftime("%Y-%m-%d %H-%M-%S", now))
        try:
            os.makedirs(saved_dir(), exist_ok=True)
            write_atomic(base + ".txt", standings_text(rows, now))
        except OSError as e:
            self._add_log(stamp, f"Couldn't save the leaderboard: {e}", "#FF8A7A")
            return ""
        self._last_save, self._last_save_path = time.monotonic(), base + ".txt"
        self._add_log(stamp, f"Saved the leaderboard ({why}): {base}.txt", "#5FE39B")
        board = dict(board_payload(rows, c), style=dict(overlay_style(c, "board"), hidden=False))
        # the picture is drawn without the tracker's feed (it may be off): its images go along
        board["teamPics"] = {t: url for t in board["teamPics"] if (url := data_url(team_pic(t, c)))}
        if c["board_first"] == "image":
            board["iconUrl"] = data_url(c["board_icon"])
        if c["overlay_bg_mode"] == "image":
            board["style"]["imageUrl"] = data_url(c["overlay_bg_image"])
        page = self.board_path

        def draw():
            try:
                snapshot_png(page, board, base + ".png")
                self.events.put(("log", time.strftime("%H:%M:%S"), f"… and its picture: {base}.png", "#5FE39B"))
            except Exception as e:
                self.events.put(("log", time.strftime("%H:%M:%S"),
                                 f"The leaderboard picture didn't save ({e}) — the text file did.", "#FACC15"))
        threading.Thread(target=draw, daemon=True).start()
        return base + ".txt"

    def _reset_board(self, why, save=None):
        """🧹 Reset (button, hotkey, tray, Streamer.bot): save the results first if
        Save results on reset is on (or save=True), then ready the leaderboard for
        the next game — unpinned players off, everyone's kept amount back to zero.
        Returns the saved .txt path ("" if nothing was saved)."""
        path = ""
        if self.cfg["save_on_game_over"] if save is None else save:
            path = self._save_leaderboard(why)
        self._clear_board()
        self._zero_amounts()
        return path

    def _zero_amounts(self):
        """Everyone's amount back to zero: the kept last amounts go, so the board
        fills again as the next game's money comes in (live money shows again)."""
        if self.worker is not None:
            self.worker.zero_now = True
        res = getattr(self, "current_people", None) or []
        for r in res:
            if r.get("kept"):
                r["text"], r["kept"] = "", False
            r["last"] = ""
        self._add_log(time.strftime("%H:%M:%S"), "Leaderboard reset: amounts back to zero.", None)

    def _clear_board(self):
        """Ready for the next game: everyone off the leaderboard except you and
        anyone pinned (📌 on their card, or a pinned folder)."""
        kept, cleared = [], []
        for i, p in enumerate(self.cfg["people"]):
            if not p["board"]:
                continue
            if self._kept_by(p, i):
                kept.append(self._person_name(i))
            elif i < len(self.person_rows):
                self.person_rows[i]["board"].set(False)
                cleared.append(self._person_name(i))
        if cleared:
            self._add_log(time.strftime("%H:%M:%S"), f"Leaderboard cleared for the next game: took off "
                          f"{', '.join(cleared)}" + (f" (kept {', '.join(kept)})" if kept else ""), None)

    def _open_saved(self):
        os.makedirs(saved_dir(), exist_ok=True)
        self._open_file(saved_dir())

    def _update_hotkey(self):
        if getattr(self, "hotkey", None) is not None:
            self.hotkey.stop()
            self.hotkey = None
        self._hotkey_error = ""
        c = self.cfg
        if c["save_hotkey_on"] and os.name == "nt":
            def failed(msg):
                self._hotkey_error = msg
                self.events.put(("log", time.strftime("%H:%M:%S"), f"Reset hotkey: {msg}.", "#FACC15"))
            self.hotkey = GlobalHotkey(c["save_hotkey"], lambda: self.events.put(("hotkey",)), failed)
        if hasattr(self, "hk_btn"):
            self._paint_hotkey()

    def _paint_hotkey(self):
        c = self.cfg
        problem = False
        if not c["save_hotkey_on"]:
            text = "Hotkey off — reset with the button, the tray icon or Streamer.bot."
        elif getattr(self, "_hotkey_error", ""):
            err = self._hotkey_error   # often names the keys already ("Ctrl+Alt+S is already used…")
            text = (err if c["save_hotkey"] in err else f"{c['save_hotkey']}: {err}") + " — pick another key."
            problem = True
        elif not parse_hotkey(c["save_hotkey"]):
            text, problem = f"{c['save_hotkey']}: a letter or number needs Ctrl, Alt or Shift too.", True
        else:
            text = (f"{c['save_hotkey']} resets the leaderboard from anywhere, even in-game (saving the "
                    "results first if Save results on reset is on).")
        if c["save_hotkey_on"]:
            self.hk_btn.configure(text=f"⌨ {c['save_hotkey']}" + ("  ⚠" if problem else ""),
                                  fg="#FACC15" if problem else ACCENT,
                                  highlightbackground="#FACC15" if problem else ACCENT, bg="#16213A")
        else:
            self.hk_btn.configure(text="⌨ Hotkey off", fg=MUTED, highlightbackground="#3A4352", bg=BG_DARK)
        if getattr(self, "hk_note", None) is not None:
            self.hk_note.configure(text=text + f"  Results are saved in “{SAVE_DIR_NAME}” next to the app.")

    def _hotkey_popup(self):
        """⌨: the save hotkey's settings, under its button. Built once and reused
        (its switch and chips follow the same settings as before)."""
        pop = getattr(self, "hk_pop", None)
        if pop is not None and pop.winfo_viewable():
            pop.withdraw()   # the button toggles it
            return
        if pop is None:
            pop = self.hk_pop = tk.Toplevel(self, bg=BORDER)
            pop.withdraw()  # placed once it's built (a borderless window placed early jumps to 0,0)
            pop.overrideredirect(True)
            pop.transient(self)
            inner = tk.Frame(pop, bg=CARD)
            inner.pack(padx=1, pady=1)
            body = tk.Frame(inner, bg=CARD)
            body.pack(padx=12, pady=10)
            head = tk.Frame(body, bg=CARD)
            head.pack(fill="x")
            self._label(head, "Reset hotkey", CARD, TEXT, 10, True).pack(side="left")
            Switch(head, self.hk_on_var, bg=CARD).pack(side="right", padx=(24, 0))
            keys = tk.Frame(body, bg=CARD)
            keys.pack(fill="x", pady=(10, 0))
            for var, name in ((self.hk_ctrl, "Ctrl"), (self.hk_alt, "Alt"), (self.hk_shift, "Shift")):
                Chip(keys, "", name, var).pack(side="left", padx=(0, 6))
            ttk.Combobox(keys, textvariable=self.hk_key_var, values=HOTKEY_KEYS, state="readonly",
                         width=4).pack(side="left")
            self.hk_note = self._label(body, "", CARD, "#8A93A3", 8)
            self.hk_note.configure(wraplength=330, justify="left")
            self.hk_note.pack(anchor="w", pady=(8, 0))
            ttk.Button(body, text="Close", command=pop.withdraw).pack(anchor="w", pady=(8, 0))
            pop.bind("<Escape>", lambda e: pop.withdraw())
            # closes when you click anywhere else in the window
            pop.bind("<FocusOut>", lambda e: pop.after(150, lambda: pop.winfo_exists() and pop.winfo_viewable()
                                                        and pop.focus_get() is None and pop.withdraw()))
        self._paint_hotkey()
        pop.update_idletasks()
        btn = self.hk_btn
        x = btn.winfo_rootx()
        y = btn.winfo_rooty() + btn.winfo_height() + 4
        where = f"+{min(x, self.winfo_rootx() + self.winfo_width() - pop.winfo_reqwidth())}+{y}"
        pop.geometry(where)
        pop.deiconify()
        pop.update_idletasks()
        pop.geometry(where)
        pop.after(50, pop.focus_set)

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
                pystray.MenuItem("Reset leaderboard", lambda: post("save")),
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
        """Run the local feed only while an overlay is on or Streamer.bot may
        control the tracker."""
        wanted = (any(p.get("overlay") for p in self.cfg["people"])
                  or (self.cfg["board_on"] and self.cfg["board_overlay"]) or self.cfg["sb_on"]
                  or getattr(self, "preview_on", False) or getattr(self, "editor_on", False))
        if wanted and self.overlay is None:
            try:
                self.overlay = OverlayServer(int(self.cfg["overlay_port"]), lambda: self.overlay_texts,
                                             lambda full=False: (getattr(self, "overlay_board_full", None)
                                                                 or self.overlay_board) if full
                                             else self.overlay_board,
                                             lambda: dict({"icon": self.cfg["board_icon"],
                                                           "bg": self.cfg["overlay_bg_image"]},
                                                          **{f"team_{t}": team_pic(t, self.cfg)
                                                             for t, _, _ in TEAMS}),
                                             lambda page: overlay_style(self.cfg, page),
                                             self._api_request,
                                             lambda kind: (demo_board(self.cfg) if kind == "board"
                                                           else demo_texts(space=self.cfg["space"])[DEMO_ME]),
                                             self._editor_http)
            except OSError as e:
                self.events.put(("log", time.strftime("%H:%M:%S"),
                                 f"Overlay feed couldn't start on port {self.cfg['overlay_port']} "
                                 f"({e}) — is another program using it?", "#FACC15"))
        elif not wanted and self.overlay is not None:
            self.overlay.close()
            self.overlay = None

    def _close(self):
        if getattr(self, "preview_proc", None) is not None:
            self._close_preview()
        if getattr(self, "editor_proc", None) is not None:
            self._close_editor()
        if getattr(self, "hotkey", None) is not None:
            self.hotkey.stop()
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
        self._obs_status = look   # the Output editor shows it too
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
                    self._folders_tick()
                    self._show_value(self.current_text)
                elif kind == "ingame":
                    self.ingame_friends = ev[1]
                elif kind == "acct":
                    self._got_acct(*ev[1:])
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
                    elif ev[1] == "save":
                        self._reset_board("tray")
                    elif ev[1] == "quit":
                        self._close()
                elif kind == "api":
                    _, cmd, params, box = ev
                    try:
                        box["reply"] = self._api(cmd, params)
                    except Exception as e:
                        box["reply"] = (500, {"ok": False, "error": str(e)})
                    box["done"].set()
                elif kind == "editor":
                    _, what, arg, box = ev

                    def run(what=what, arg=arg, box=box):
                        try:
                            box["reply"] = (self._editor_pick(arg) if what == "pick" else self._editor_set(arg))
                        except Exception as e:
                            box["reply"] = {"ok": False, "error": str(e)}
                        box["done"].set()
                    # a file dialog waits for you: not inside the event pump
                    self.after(1, run) if what == "pick" else run()
                elif kind == "hotkey":
                    self._reset_board("hotkey")
                elif kind == "game_over":
                    self._game_over(ev[1])
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


# ---------------- end of game: save the leaderboard ----------------

SAVE_DIR_NAME = "Saved leaderboards"
GAME_OVER_SECS = 15    # your amount gone this long after a game: the game is over


def saved_dir():
    return os.path.join(DATA_DIR, SAVE_DIR_NAME)


def standings(people, results, cfg):
    """The leaderboard to save: everyone ticked Leaderboard (else everyone)
    with an amount now or a last amount from the game, in the leaderboard's
    order, cut to How many."""
    rows = []
    ticked = any(p.get("board") for p in people)
    for p, r in zip(people, results):
        text = r.get("text") or r.get("last", "")   # (saved results keep pinned players' last amount)
        if text and (p.get("board") or not ticked):
            rows.append({"label": r.get("label", ""), "text": text,
                         "team": p.get("team", "") if cfg.get("board_teams") else ""})
    if cfg.get("board_order", "amount") == "amount":
        rows.sort(key=lambda r: amount_value(r["text"]), reverse=True)
    top = cfg.get("board_top", 0)
    return rows[:top] if top else rows


def standings_text(rows, when):
    width = max([len(r["label"]) for r in rows] + [4])
    lines = [f"WARDOGS leaderboard — {time.strftime('%Y-%m-%d %H:%M', when)}", ""]
    lines += [f"{i}. {r['label']:<{width}}  {r['text']}" + (f"  [{TEAM_NAMES[r['team']]}]"
              if r.get("team") in TEAM_NAMES else "") for i, r in enumerate(rows, 1)]
    return "\n".join(lines) + "\n"


def snapshot_png(page_path, board, out):
    """Draws the leaderboard overlay page in a hidden browser and saves just the
    panel as a PNG with a see-through background."""
    from urllib.parse import quote
    from playwright.sync_api import sync_playwright
    url = overlay_link(page_path) + "?snapshot=1#" + quote(json.dumps(board))
    with sync_playwright() as pw:
        last = None
        for extra in ({"channel": "msedge"}, {"channel": "chrome"}):
            try:
                br = pw.chromium.launch(headless=True, **extra)
                break
            except Exception as e:
                last, br = e, None
        if br is None:
            raise RuntimeError(f"no browser to draw it with ({last})")
        try:
            page = br.new_page(viewport={"width": 1600, "height": 1200}, device_scale_factor=2)
            page.goto(url)
            page.wait_for_function("document.body.dataset.ready === '1'", timeout=15000)
            page.locator("#panel").screenshot(path=out, omit_background=True)
        finally:
            br.close()


# ---------------- Streamer.bot ----------------

def sb_send_action(cfg, args):
    """Runs the Streamer.bot action through its HTTP server (Servers/Clients ›
    HTTP Server). Returns "" when it worked, else what went wrong."""
    import urllib.error
    import urllib.request
    url = cfg["sb_url"].strip().rstrip("/") or "http://127.0.0.1:7474"
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    body = json.dumps({"action": {"name": cfg["sb_action"].strip()}, "args": args}).encode()
    req = urllib.request.Request(url + "/DoAction", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            return "" if 200 <= r.status < 300 else f"Streamer.bot answered {r.status}"
    except urllib.error.HTTPError as e:
        return f"Streamer.bot answered {e.code} — check the action name"
    except Exception as e:
        return f"couldn't reach Streamer.bot at {url} ({getattr(e, 'reason', e)}) — is its HTTP Server on?"


def game_args(label, text, rows, test=False):
    """The arguments a Streamer.bot action gets when a game ends."""
    v = amount_value(text)
    result = "profit" if v > 0 else "loss" if v < 0 else "even"
    words = {"profit": f"finished the game {text} in profit", "loss": f"finished the game {text} down",
             "even": "finished the game even"}
    args = {"who": label, "amount": text, "amountValue": int(v) if v == int(v) else v, "result": result,
            "message": f"{label} {words[result]}", "players": len(rows), "test": test,
            "leaderboard": " | ".join(f"{i}. {r['label']} {r['text']}" for i, r in enumerate(rows, 1))}
    for i, r in enumerate(rows, 1):
        args[f"place{i}"], args[f"place{i}Amount"] = r["label"], r["text"]
        args[f"place{i}Team"] = TEAM_NAMES.get(r.get("team", ""), "")
    return args


SB_COMMANDS = [
    ("status", "what the tracker is showing (JSON)"),
    ("hide", "hide every overlay — nothing on stream"),
    ("show", "show the overlays again"),
    ("hide/toggle", "hide or show"),
    ("overlay/off", "turn every overlay off (…&name=Chopper or &name=leaderboard for one)"),
    ("overlay/on", "turn them back on (same &name=)"),
    ("overlay/toggle", "on or off (same &name=)"),
    ("board/on", "leaderboard on"),
    ("board/off", "leaderboard off"),
    ("board/toggle", "leaderboard on or off"),
    ("board/add", "&name=Chopper — put someone on the leaderboard (adds them if new)"),
    ("board/remove", "&name=Chopper — take someone off the leaderboard"),
    ("color", "&text=C5C2BF &profit=5FE39B &loss=FF8A7A &bg=000000 &opacity=45 "
              "&pl=hud|board|both|off &app_profit= &app_loss= (any of them)"),
    ("refresh", "refresh the Steam friends page once"),
    ("save", "save the results (text + picture), then reset: everyone off except you and anyone pinned, "
             "amounts back to zero"),
    ("board/end", "reset quietly: everyone off except you and anyone pinned, amounts back to zero, nothing saved"),
    ("gameover", "end the game now: send the result to Streamer.bot (if that's on)"),
    ("team/set", "&name=Chopper&team=valkyra — put someone on a team (lonestar, manticore, valkyra, none)"),
    ("twitch/join", "&user=%userName%&input=%rawInput%&format=text — !join wardogs leaderboard [team]: puts "
                    "the viewer on the leaderboard (their Twitch must be linked), on that team if given"),
    ("twitch/team", "&user=%userName%&input=%rawInput%&format=text — !wardogs team valkyra: the viewer "
                    "changes their team"),
    ("twitch/friends", "&user=%userName%&format=text — !wardogs friends: who's in WARDOGS now, numbered, "
                       "to whisper back"),
    ("twitch/link", "&user=%userName%&input=%rawInput%&format=text — !wardogs tracker link 2 @viewer "
                    "(mods / broadcaster only: set that in Streamer.bot)"),
]


# ---------------- global hotkey ----------------

HOTKEY_KEYS = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + [str(d) for d in range(10)] + \
              [f"F{n}" for n in range(1, 13)]


def parse_hotkey(spec):
    """'Ctrl+Alt+S' -> (modifiers, virtual key), or None if it isn't usable
    (a letter or digit needs Ctrl, Alt or Shift)."""
    parts = [x.strip() for x in str(spec).split("+") if x.strip()]
    if not parts:
        return None
    mods = 0
    for m in parts[:-1]:
        bit = {"ctrl": 2, "control": 2, "alt": 1, "shift": 4, "win": 8}.get(m.lower())
        if bit is None:
            return None
        mods |= bit
    key = parts[-1].upper()
    if key in [f"F{n}" for n in range(1, 13)]:
        vk = 0x70 + int(key[1:]) - 1
    elif len(key) == 1 and (key.isdigit() or "A" <= key <= "Z"):
        if not mods:
            return None
        vk = ord(key)
    else:
        return None
    return mods, vk


class GlobalHotkey:
    """A Windows hotkey that works in any program (even in-game). callback()
    runs on this helper's thread: keep it to a queue.put()."""

    def __init__(self, spec, callback, on_error):
        self.spec, self.callback, self.on_error = spec, callback, on_error
        self.tid = None
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(2)

    def _run(self):
        import ctypes
        from ctypes import wintypes
        u32, k32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32")
        self.tid = k32.GetCurrentThreadId()
        parsed = parse_hotkey(self.spec)
        if not parsed or not u32.RegisterHotKey(None, 1, parsed[0] | 0x4000, parsed[1]):  # MOD_NOREPEAT
            self.ready.set()
            self.on_error(f"{self.spec} is already used by another program" if parsed
                          else f"{self.spec} isn't a usable hotkey")
            return
        self.ready.set()
        msg = wintypes.MSG()
        try:
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:  # WM_HOTKEY
                    self.callback()
        finally:
            u32.UnregisterHotKey(None, 1)

    def stop(self):
        if self.tid:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self.tid, 0x0012, 0, 0)  # WM_QUIT
            self.tid = None


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
