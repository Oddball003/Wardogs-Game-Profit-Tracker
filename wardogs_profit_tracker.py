"""
Wardogs Profit Tracker
Reads your Steam rich presence (via a signed-in browser), pulls out the
money amount, writes it to a txt file for OBS, and can recolor the OBS
text source for profit / loss through OBS WebSocket.
"""
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
from html.parser import HTMLParser
from tkinter import colorchooser, filedialog, messagebox, ttk

APP_NAME = "Wardogs Profit Tracker"

# Folder the app lives in (next to the .exe when built, next to the .py otherwise)
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(HERE, "settings.json")
PROFILE_DIR = os.path.join(HERE, "steam_browser_profile")
LOG_FILE = os.path.join(HERE, "activity_log.txt")
LOG_FILE_MAX = 1_000_000  # bytes; trimmed to the newest half at startup
LOGIN_URL = "https://steamcommunity.com/login/home/"

MIN_INTERVAL = 10  # seconds - fastest allowed refresh

DEFAULTS = {
    "url": "",
    "interval": 15,
    "space": True,
    "out_file": "",
    "keep_last": True,
    "color_on": True,
    "profit_color": "#5FE39B",
    "loss_color": "#FF8A7A",
    "obs_on": False,
    "obs_source": "Profit Text",
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

def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        enc = saved.pop("obs_password_enc", "")
        # older versions stored it in plain text: read it once, it gets
        # re-saved encrypted below
        plain = saved.pop("obs_password", "")
        s.update(saved)
        s["obs_password"] = unprotect(enc) if enc else plain
        if plain:
            save_settings(s)
    except Exception:
        pass
    return s


def save_settings(s):
    data = dict(s)
    data["obs_password_enc"] = protect(data.pop("obs_password", ""))
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ---------------- page parsing ----------------
VOID = {"img", "br", "hr", "input", "meta", "link", "source", "wbr", "area", "col"}


class MiniProfileParser(HTMLParser):
    WANT = {
        "persona": "name",
        "miniprofile_game_name": "game",
        "rich_presence": "presence",
        "game_state": "state",
    }

    def __init__(self):
        super().__init__()
        self.data = {}
        self.status = ""
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        classes = (dict(attrs).get("class") or "").split()
        key = None
        for c in classes:
            if c in self.WANT:
                key = self.WANT[c]
        if "persona" in classes:
            for st in ("in-game", "online", "offline", "away", "snooze", "busy"):
                if st in classes:
                    self.status = st
        self.stack.append(key)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if self.stack:
            self.stack.pop()

    def handle_data(self, text):
        for key in reversed(self.stack):
            if key:
                self.data[key] = self.data.get(key, "") + text
                break


def parse_presence(html):
    p = MiniProfileParser()
    p.feed(html)
    d = {k: " ".join(v.split()) for k, v in p.data.items()}
    if d.get("presence"):
        return d["presence"]
    return (d.get("state") or p.status or "Offline").title()


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


_write_lock = threading.Lock()  # the worker and the window both write the txt


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


STEAMID64_BASE = 76561197960265728  # SteamID64 = this + account ID


STEAMDB_URL = "https://steamdb.info/calculator/"


def to_miniprofile_url(text):
    """Accepts anything SteamDB shows you and turns it into a miniprofile link:
    a miniprofile link, a SteamID64 (7656...), a /profiles/7656... link,
    a Steam3 ID like [U:1:12345678], or a plain account ID."""
    t = (text or "").strip()
    if not t:
        return t
    if "miniprofile/" in t:
        return t if re.match(r"https?://", t, re.IGNORECASE) else "https://" + t
    m = re.search(r"\[?U:1:(\d+)\]?", t, re.IGNORECASE)
    if m:
        return f"https://steamcommunity.com/miniprofile/{int(m.group(1))}"
    m = re.search(r"(7656\d{13})", t)
    if m:
        return f"https://steamcommunity.com/miniprofile/{int(m.group(1)) - STEAMID64_BASE}"
    if re.fullmatch(r"\d{1,10}", t):
        return f"https://steamcommunity.com/miniprofile/{int(t)}"
    return t  # something else (e.g. a /id/name/ link) - leave it as typed


def miniprofile_url_from_cookies(ctx):
    """Builds https://steamcommunity.com/miniprofile/<account id> for the
    signed-in account. The steamLoginSecure cookie starts with your SteamID64."""
    from urllib.parse import unquote
    for c in ctx.cookies("https://steamcommunity.com"):
        if c["name"] == "steamLoginSecure":
            m = re.match(r"(\d{17})", unquote(c["value"]))
            if m:
                account_id = int(m.group(1)) - STEAMID64_BASE
                if account_id > 0:
                    return f"https://steamcommunity.com/miniprofile/{account_id}", m.group(1)
    return None, None


def signed_in(ctx):
    return any(c["name"] == "steamLoginSecure" for c in ctx.cookies("https://steamcommunity.com"))


def cookie_jar_from_context(ctx):
    """Copies the browser's Steam cookies into a plain Python cookie jar, so
    refreshes don't need the browser (a throttled background browser can
    stall without ever timing out)."""
    from http.cookiejar import Cookie, CookieJar
    jar = CookieJar()
    for c in ctx.cookies():
        if "steam" not in c["domain"]:
            continue
        session = c.get("expires", -1) < 0
        jar.set_cookie(Cookie(
            0, c["name"], c["value"], None, False,
            c["domain"], True, c["domain"].startswith("."),
            c.get("path", "/"), True, c.get("secure", False),
            None if session else int(c["expires"]), session,
            None, None, {"HttpOnly": None} if c.get("httpOnly") else {}))
    return jar


def jar_signed_in(jar):
    jar.clear_expired_cookies()
    return any(c.name == "steamLoginSecure" for c in jar)


class Worker(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.stop_event = threading.Event()
        self.force_login = False
        self.ctx = None  # open browser context, closed in cleanup()
        self.obs = None  # open OBS connection, closed in close_obs()

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
            # the browser is only needed to sign in and read the cookies
            with sync_playwright() as pw:
                try:
                    jar = self.sign_in(pw)
                finally:
                    self.close_ctx()
            if jar is not None:
                self.loop(jar)
        except Exception as e:
            self.log(f"Stopped: {e}", "#FF8A7A")
        finally:
            self.close_obs()
        self.emit("stopped", self)

    def sign_in(self, pw):
        """Returns a cookie jar for the signed-in Steam account, or None."""
        ctx = self.ctx = launch_browser(pw, headless=True, log=self.log)
        need_login = self.force_login or not signed_in(ctx)
        if need_login:
            self.close_ctx()
            if self.force_login:
                shutil.rmtree(PROFILE_DIR, ignore_errors=True)
            if not self.login(pw):
                return None
            ctx = self.ctx = launch_browser(pw, headless=True)
        my_url, sid64 = miniprofile_url_from_cookies(ctx)
        if my_url:
            self.log(f"Signed-in account: SteamID64 {sid64} → {my_url}")
            self.emit("account", my_url)
        return cookie_jar_from_context(ctx)

    def loop(self, jar):
        import urllib.request
        # plain HTTP with the Steam cookies: no browser, hard 20 s timeout
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        headers = {"Cache-Control": "no-cache", "Pragma": "no-cache",
                   "Referer": "https://steamcommunity.com/",
                   "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"}
        obs_conn = None        # (host, port, password) the OBS connection was made with
        obs_color_sent = None  # (source, color) last sent to OBS
        obs_warned = False
        last_text = ""
        fails = 0
        url_warned = None
        signed_out_warned = False
        self.log("Started.")
        while not self.stop_event.is_set():
            cfg = dict(self.app.cfg)  # live settings
            missing = None
            if not cfg["url"]:
                missing = "Steam miniprofile URL"
            elif not cfg["out_file"]:
                missing = "output text file"
            if missing:
                self.emit("status", f"Enter your {missing}", "#FACC15")
                if url_warned != missing:
                    self.log(f"Waiting — set your {missing} above.", "#FACC15")
                    url_warned = missing
                self.sleep(2)
                continue
            url_warned = None
            try:
                url = cfg["url"]
                if not re.match(r"https?://", url, re.IGNORECASE):
                    url = "https://" + url
                url += f"{'&' if '?' in url else '?'}_={int(time.time() * 1000)}"
                # HTTP errors (4xx/5xx) raise and are retried below
                with opener.open(urllib.request.Request(url, headers=headers), timeout=20) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
                    landed_on_login = "/login" in resp.geturl()
                raw = parse_presence(html)
                text = extract_money(raw, cfg["space"])
                kept = False
                if not text and cfg["keep_last"] and last_text:
                    # reuse the last amount, with the current space setting
                    sign, body = last_text[0], last_text[1:].lstrip()
                    text, kept = sign + (" " if cfg["space"] else "") + body, True
                write_atomic(cfg["out_file"], text)
                if text:
                    last_text = text
                fails = 0
                self.emit("value", text, raw, kept)
                note = "(kept last)" if kept else (text or "(blank)")
                self.log(f'refreshed  "{raw}" → {note}',
                         self.app.color_for(text) if text and not kept else None)

                # OBS color
                if cfg["obs_on"] and text:
                    want = cfg["loss_color"] if text.startswith("-") else cfg["profit_color"]
                    conn = (cfg["obs_host"], cfg["obs_port"], cfg["obs_password"])
                    if conn != obs_conn:
                        # host/port/password changed: reconnect and resend
                        self.close_obs()
                        obs_conn, obs_color_sent, obs_warned = conn, None, False
                    if (cfg["obs_source"], want) != obs_color_sent:
                        try:
                            if self.obs is None:
                                self.obs = obs_client(cfg)
                            obs_set_color(self.obs, cfg["obs_source"], want)
                            obs_color_sent = (cfg["obs_source"], want)
                            obs_warned = False
                            self.emit("obs", "connected")
                            self.log(f"OBS color → {want}")
                        except Exception as e:
                            self.close_obs()
                            obs_color_sent = None
                            self.emit("obs", "error")
                            if not obs_warned:
                                self.log(f"OBS: {e}", "#FF8A7A")
                                obs_warned = True

                # Steam drops the sign-in cookie (or redirects to login) when the
                # session ends; signed-out pages can show stale text
                if landed_on_login or not jar_signed_in(jar):
                    if not signed_out_warned:
                        self.log("Signed out of Steam — the amount may be stale. "
                                 "Click \"Sign in again\".", "#FACC15")
                        signed_out_warned = True
                    self.emit("status", "Signed out — click Sign in again", "#FACC15")
                else:
                    signed_out_warned = False
                    self.emit("status", "Signed in · refreshed just now", "#3DDC84")
            except Exception as e:
                fails += 1
                self.log(f"error (attempt {fails}): {e}", "#FF8A7A")
                self.emit("status", "Error — retrying", "#FF8A7A")
            interval = max(MIN_INTERVAL, int(cfg["interval"]))
            # back off on errors (up to 60 s), but never retry faster than normal
            delay = interval if fails == 0 else min(max(60, interval), interval * 2 ** (fails - 1))
            self.sleep(delay)
        self.log("Stopped.")


# ---------------- GUI ----------------

class Switch(tk.Canvas):
    """A small on/off toggle switch tied to a BooleanVar."""
    W, H = 40, 22

    def __init__(self, parent, variable, bg=BG, on_color=ACCENT, off_color="#3A4352"):
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=1)
        self.var = variable
        self.on_color, self.off_color = on_color, off_color
        self.bind("<Button-1>", self.toggle)
        self.bind("<space>", self.toggle)
        self.bind("<Return>", self.toggle)
        self.bind("<FocusIn>", lambda e: self.draw())
        self.bind("<FocusOut>", lambda e: self.draw())
        variable.trace_add("write", lambda *_: self.draw())
        self.draw()

    def toggle(self, _=None):
        self.var.set(not self.var.get())

    def draw(self):
        self.delete("all")
        on = bool(self.var.get())
        w, h, r = self.W, self.H, self.H / 2
        track = self.on_color if on else self.off_color
        # rounded track: two circles + a rectangle
        self.create_oval(0, 0, h, h, fill=track, outline=track)
        self.create_oval(w - h, 0, w, h, fill=track, outline=track)
        self.create_rectangle(r, 0, w - r, h, fill=track, outline=track)
        # knob
        pad = 3
        x = w - h + pad if on else pad
        self.create_oval(x, pad, x + h - 2 * pad, h - pad, fill="#FFFFFF", outline="#FFFFFF")
        try:
            focused = self.focus_get() is self
        except (KeyError, tk.TclError):
            focused = False
        if focused:
            self.create_rectangle(0, 0, w - 1, h - 1, outline="#7FB4FF")


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
        self.my_url = None
        self._style()
        self._build()
        self._load_into_widgets()
        self.after(150, self._pump)
        self.after(1000, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.start()

    # ---- look ----
    def _style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=TEXT, fieldbackground=BG_DARK,
                     bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                     font=("Segoe UI", 10))
        st.configure("TEntry", padding=6, foreground=TEXT, insertcolor=TEXT)
        st.configure("TSpinbox", padding=6, foreground=TEXT, arrowcolor=TEXT,
                     background=BG_DARK, insertcolor=TEXT)
        st.configure("TCheckbutton", background=BG, foreground=LABEL)
        st.map("TCheckbutton", background=[("active", BG)])
        st.configure("Card.TCheckbutton", background=CARD, foreground=LABEL)
        st.map("Card.TCheckbutton", background=[("active", CARD)])
        st.configure("TRadiobutton", background=BG, foreground=LABEL)
        st.map("TRadiobutton", background=[("active", BG)])
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

        def wheel(e):
            # the activity log scrolls itself; the wheel elsewhere scrolls the settings
            if e.widget is self.log_txt:
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

        self._label(form, "Steam miniprofile URL (filled in from your Steam sign-in)").grid(row=0, column=0, columnspan=2, sticky="w")
        self.url_var = tk.StringVar()
        ucol = tk.Frame(form, bg=BG)
        ucol.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 10))
        uf = tk.Frame(ucol, bg=BG)
        uf.pack(fill="x")
        url_entry = ttk.Entry(uf, textvariable=self.url_var)
        url_entry.pack(side="left", fill="x", expand=True)
        # turn a pasted SteamID64 / [U:1:…] / account ID into a miniprofile link
        # gray "Add Steam ID" hint shown while the box is empty
        ph = tk.Label(url_entry, text="Add Steam ID", bg=BG_DARK, fg="#6F7887",
                      font=("Segoe UI", 10), cursor="xterm")
        ph.bind("<Button-1>", lambda e: url_entry.focus_set())

        def update_placeholder(*_):
            try:
                focused = self.focus_get() is url_entry
            except (KeyError, tk.TclError):
                focused = False
            if self.url_var.get().strip() or focused:
                ph.place_forget()
            else:
                ph.place(x=8, rely=0.5, anchor="w")
        self.url_var.trace_add("write", update_placeholder)
        url_entry.bind("<FocusIn>", update_placeholder, add="+")
        url_entry.bind("<FocusOut>", lambda e: (self._normalize_url(), update_placeholder()))
        self.after(100, update_placeholder)
        url_entry.bind("<Return>", lambda e: self._normalize_url())
        url_entry.bind("<<Paste>>", lambda e: self.after(50, self._normalize_url))
        self.my_acct_btn = ttk.Button(uf, text="Use my account", command=self._use_my_account,
                                      state="disabled")
        self.my_acct_btn.pack(side="left", padx=(8, 0))
        hint = tk.Frame(ucol, bg=BG)
        hint.pack(fill="x", pady=(4, 0))
        tk.Label(hint, text="Find Steam ID:", bg=BG, fg="#8A93A3",
                 font=("Segoe UI", 8)).pack(side="left")
        link = tk.Label(hint, text="SteamDB ↗", bg=BG, fg="#7FB4FF",
                        font=("Segoe UI", 8, "underline"), cursor="hand2")
        link.pack(side="left", padx=(4, 0))
        link.bind("<Button-1>", lambda e: __import__("webbrowser").open(STEAMDB_URL))

        self._label(form, "Refresh every (seconds)").grid(row=2, column=0, sticky="w")
        self._label(form, "Space after sign").grid(row=2, column=1, sticky="w", padx=(12, 0))
        self.interval_var = tk.IntVar()
        ttk.Spinbox(form, from_=MIN_INTERVAL, to=300, increment=5, textvariable=self.interval_var,
                    width=8).grid(row=3, column=0, sticky="ew", pady=(4, 10))
        sp = tk.Frame(form, bg=BG)
        sp.grid(row=3, column=1, sticky="w", padx=(12, 0), pady=(4, 10))
        self.space_var = tk.BooleanVar()
        ttk.Radiobutton(sp, text="+ $3,740", value=True, variable=self.space_var).pack(side="left")
        ttk.Radiobutton(sp, text="+$3,740", value=False, variable=self.space_var).pack(side="left", padx=(10, 0))

        self._label(form, "Output text file (for OBS)").grid(row=4, column=0, columnspan=2, sticky="w")
        of = tk.Frame(form, bg=BG)
        of.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(4, 10))
        self.out_var = tk.StringVar()
        ttk.Entry(of, textvariable=self.out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(of, text="Browse…", command=self._browse).pack(side="left", padx=(8, 0))

        # colors
        cr = tk.Frame(form, bg=BG)
        cr.grid(row=6, column=0, columnspan=2, sticky="ew")
        self._label(cr, "Text color").pack(side="left")
        self.color_on_var = tk.BooleanVar()
        ttk.Checkbutton(cr, text="Color by + / −", variable=self.color_on_var).pack(side="right")
        cc = tk.Frame(form, bg=BG)
        cc.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 10))
        cc.columnconfigure(0, weight=1)
        cc.columnconfigure(1, weight=1)
        self.profit_btn = self._color_button(cc, "+ Profit", "profit_color")
        self.profit_btn.grid(row=0, column=0, sticky="ew")
        self.loss_btn = self._color_button(cc, "− Loss", "loss_color")
        self.loss_btn.grid(row=0, column=1, sticky="ew", padx=(12, 0))

        # OBS card
        obs = tk.Frame(body, bg=CARD, highlightbackground="#262C36", highlightthickness=1)
        obs.pack(fill="x", pady=(4, 0))
        inner = tk.Frame(obs, bg=CARD)
        inner.pack(fill="x", padx=14, pady=12)
        inner.columnconfigure(0, weight=2)
        inner.columnconfigure(1, weight=1)
        head = tk.Frame(inner, bg=CARD)
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._label(head, "OBS WebSocket", CARD, TEXT, 10, True).pack(side="left")
        self.obs_pill = tk.Label(head, text="● Off", bg=CARD, fg=MUTED, font=("Segoe UI", 9, "bold"))
        self.obs_pill.pack(side="left", padx=(10, 0))
        self.obs_on_var = tk.BooleanVar()
        Switch(head, self.obs_on_var, bg=CARD).pack(side="right")
        sw_lbl = self._label(head, "OBS color", CARD, LABEL, 9)
        sw_lbl.pack(side="right", padx=(0, 8))
        sw_lbl.bind("<Button-1>", lambda e: self.obs_on_var.set(not self.obs_on_var.get()))

        self._label(inner, "Text source name", CARD).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.obs_source_var = tk.StringVar()
        ttk.Entry(inner, textvariable=self.obs_source_var).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        self._label(inner, "Host", CARD).grid(row=3, column=0, sticky="w")
        self._label(inner, "Port", CARD).grid(row=3, column=1, sticky="w", padx=(12, 0))
        self.obs_host_var = tk.StringVar()
        self.obs_port_var = tk.StringVar()
        ttk.Entry(inner, textvariable=self.obs_host_var).grid(row=4, column=0, sticky="ew", pady=(4, 8))
        ttk.Entry(inner, textvariable=self.obs_port_var).grid(row=4, column=1, sticky="ew", padx=(12, 0), pady=(4, 8))
        self._label(inner, "Password", CARD).grid(row=5, column=0, columnspan=2, sticky="w")
        pw = tk.Frame(inner, bg=CARD)
        pw.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4, 2))
        self.obs_pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(pw, textvariable=self.obs_pass_var, show="•")
        self.pass_entry.pack(side="left", fill="x", expand=True)
        self.show_pass_btn = ttk.Button(pw, text="Show", width=6, command=self._toggle_pass)
        self.show_pass_btn.pack(side="left", padx=(8, 0))
        ttk.Button(pw, text="Test", command=self._test_obs).pack(side="left", padx=(8, 0))
        self._label(inner, "In OBS: Tools › WebSocket Server Settings › Show Connect Info",
                    CARD, "#8A93A3", 8).grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.keep_var = tk.BooleanVar()
        ttk.Checkbutton(body, text="Keep last amount when no money is shown (menus)",
                        variable=self.keep_var).pack(anchor="w", pady=(10, 0))


        # any setting change -> save + apply live
        for v in (self.url_var, self.interval_var, self.space_var, self.out_var, self.color_on_var,
                  self.keep_var, self.obs_on_var, self.obs_source_var, self.obs_host_var,
                  self.obs_port_var, self.obs_pass_var):
            v.trace_add("write", lambda *_: self._sync_cfg())

    def _color_button(self, parent, label, key):
        b = tk.Button(parent, text=label, relief="flat", bd=0, cursor="hand2",
                      font=("Segoe UI", 10, "bold"), padx=10, pady=10,
                      command=lambda: self._pick_color(key))
        return b

    def _paint_color_buttons(self):
        for btn, key, label in ((self.profit_btn, "profit_color", "+ Profit"),
                                (self.loss_btn, "loss_color", "− Loss")):
            c = self.cfg[key]
            btn.configure(text=f"{label}   {c.upper()}", bg=BG_DARK, fg=c,
                          activebackground=BG_DARK, activeforeground=c,
                          highlightbackground=BORDER, highlightthickness=1)

    # ---- settings <-> widgets ----
    def _load_into_widgets(self):
        self._loading = True
        c = dict(self.cfg)
        self.url_var.set(c["url"])
        self.interval_var.set(int(c["interval"]))
        self.space_var.set(bool(c["space"]))
        self.out_var.set(c["out_file"])
        self.color_on_var.set(bool(c["color_on"]))
        self.keep_var.set(bool(c["keep_last"]))
        self.obs_on_var.set(bool(c["obs_on"]))
        self.obs_source_var.set(c["obs_source"])
        self.obs_host_var.set(c["obs_host"])
        self.obs_port_var.set(str(c["obs_port"]))
        self.obs_pass_var.set(c["obs_password"])
        self._loading = False
        self._paint_color_buttons()
        self._set_obs_pill("off" if not c["obs_on"] else "waiting")

    def _sync_cfg(self):
        if getattr(self, "_loading", True):
            return
        c = self.cfg
        try:
            c["interval"] = max(MIN_INTERVAL, int(self.interval_var.get()))
        except (tk.TclError, ValueError):
            pass
        try:
            c["obs_port"] = int(self.obs_port_var.get())
        except ValueError:
            pass
        c["url"] = self.url_var.get().strip()
        space_changed = c["space"] != bool(self.space_var.get())
        c["space"] = bool(self.space_var.get())
        if space_changed and self.current_text and c["out_file"]:
            # update the txt right away instead of waiting for the next refresh
            self.current_text = self.respace(self.current_text)
            try:
                write_atomic(c["out_file"], self.current_text)
            except Exception:
                pass
        c["out_file"] = self.out_var.get().strip()
        c["color_on"] = bool(self.color_on_var.get())
        c["keep_last"] = bool(self.keep_var.get())
        was_on = c["obs_on"]
        c["obs_on"] = bool(self.obs_on_var.get())
        c["obs_source"] = self.obs_source_var.get().strip()
        c["obs_host"] = self.obs_host_var.get().strip() or "localhost"
        c["obs_password"] = self.obs_pass_var.get()
        if was_on != c["obs_on"]:
            self._set_obs_pill("waiting" if c["obs_on"] else "off")
        save_settings(c)
        self._show_value(self.current_text)

    # ---- actions ----
    def _browse(self):
        path = filedialog.asksaveasfilename(title="Output text file", defaultextension=".txt",
                                            initialfile=os.path.basename(self.out_var.get()) or "Game Profit Loss.txt",
                                            filetypes=[("Text file", "*.txt")])
        if path:
            self.out_var.set(path)

    def _pick_color(self, key):
        _, hx = colorchooser.askcolor(color=self.cfg[key], title="Pick a color", parent=self)
        if hx:
            self.cfg[key] = hx.upper()
            save_settings(self.cfg)
            self._paint_color_buttons()
            self._show_value(self.current_text)

    def _normalize_url(self):
        cur = self.url_var.get()
        new = to_miniprofile_url(cur)
        if new != cur.strip():
            self.url_var.set(new)

    def _use_my_account(self):
        if self.my_url:
            self.url_var.set(self.my_url)

    def _toggle_pass(self):
        hidden = self.pass_entry.cget("show") == "•"
        self.pass_entry.configure(show="" if hidden else "•")
        self.show_pass_btn.configure(text="Hide" if hidden else "Show")

    def _test_obs(self):
        self._set_obs_pill("testing")
        cfg = dict(self.cfg)

        def run():
            try:
                cl = obs_client(cfg)
                try:
                    cl.get_input_settings(cfg["obs_source"])
                finally:
                    cl.disconnect()
                self.events.put(("obs", "connected"))
                self.events.put(("log", time.strftime("%H:%M:%S"),
                                 f'OBS test OK — found source "{cfg["obs_source"]}"', "#5FE39B"))
            except Exception as e:
                self.events.put(("obs", "error"))
                self.events.put(("log", time.strftime("%H:%M:%S"), f"OBS test failed: {e}", "#FF8A7A"))
        threading.Thread(target=run, daemon=True).start()

    def _open_txt(self):
        path = self.cfg["out_file"]
        if not path:
            messagebox.showinfo(APP_NAME, "Choose an output text file first (Browse…).", parent=self)
            return
        if not os.path.exists(path):
            write_atomic(path, self.current_text)
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

    def _close(self):
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

    def _set_obs_pill(self, state):
        look = {"connected": ("● Connected", "#5FE39B"), "testing": ("● Testing…", "#FACC15"),
                "error": ("● Not connected", "#FF8A7A"), "waiting": ("● Waiting", MUTED),
                "off": ("● Off", MUTED)}[state]
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
                    self.current_text = text
                    self._show_value(text)
                    self.raw_lbl.configure(text=f'Steam says: "{raw}"')
                    self.last_refresh = time.time()
                elif kind == "status":
                    self.status_lbl.configure(text=ev[1])
                    self.status_dot.configure(fg=ev[2])
                elif kind == "obs":
                    self._set_obs_pill(ev[1])
                elif kind == "account":
                    self.my_url = ev[1]
                    self.my_acct_btn.configure(state="normal")
                    if not self.url_var.get().strip():
                        self.url_var.set(self.my_url)  # fill it in automatically
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
    _instance_lock = open(os.path.join(HERE, ".wardogs.lock"), "w")
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


def focus_existing_window():
    """Brings the already-open window to the front."""
    if os.name != "nt":
        return False
    import ctypes
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
    App().mainloop()
