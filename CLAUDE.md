# Wardogs Profit Tracker

Windows desktop app (Python + tkinter) that reads the user's Steam rich presence
for the game WARDOGS, pulls out the money amount, and writes it to a text file
that an OBS Text source reads. It can also recolor that OBS text source for
profit/loss through OBS WebSocket.

## Files
- `wardogs_profit_tracker.py` — the whole app (single file).
- `build_exe.bat` — installs deps (playwright, obsws-python, pyinstaller) and builds
  `dist\Wardogs Profit Tracker.exe` with PyInstaller (`--onefile --windowed --collect-all playwright`).
- `remove_old_exe.bat` — closes the app and deletes the old exe (optionally settings/sign-in).
- `uninstall.bat` — removes exe, settings.json, steam_browser_profile, optional txt/pip packages.

## How it works
- Data source: `https://steamcommunity.com/miniprofile/<account id>` (HTML).
  Rich presence text is in the element with class `rich_presence`.
- Must be fetched **signed in to Steam** — signed-out requests got stale text
  ("In Main Menus") while the signed-in browser updated fine. That's why Playwright is used.
- Playwright persistent context in `steam_browser_profile/` next to the exe. First run opens a
  visible browser for Steam sign-in; afterwards headless. Sign-in detected by the
  `steamLoginSecure` cookie.
- Each refresh uses `context.request.get(...)` (not `page.goto`) — lighter, still uses cookies.
- Browser choice: Windows default browser if Chromium-based (read from registry UserChoice),
  else Edge (`channel="msedge"`), else Chrome. Firefox can't be driven, so it falls back.
- Miniprofile URL is auto-filled from the signed-in account: SteamID64 is the start of the
  `steamLoginSecure` cookie; account id = SteamID64 − 76561197960265728.
  The URL box also converts pasted SteamID64, /profiles/ links, [U:1:x], and plain account IDs.

## Output rules (user-specified — keep these)
- Output is only the money, always with a sign, e.g. `+ $3,740` / `- $3,740`.
- A `+`/`-` touching the `$` in Steam's text wins; otherwise "Profit" → `+`, "Loss" → `-`;
  neither → `+`. A dash used as a separator (`Range - $3,740`) is NOT a minus.
- Space after the sign is a setting (on: `+ $3,740`, off: `+$3,740`) and applies live.
- No money in the text → blank, unless "Keep last amount" is on.
- Text file is written atomically (tmp + os.replace) so OBS never reads half a file.

## Settings / behavior
- `settings.json` next to the exe. OBS password is NEVER stored in plain text: saved as
  `obs_password_enc` using Windows DPAPI (CryptProtectData). Old plain values get migrated.
- URL and output file start empty by default (no personal defaults). Empty URL shows gray
  "Add Steam ID" placeholder; under it "Find Steam ID: SteamDB ↗" → https://steamdb.info/calculator/
- Default output file name suggested by Browse: `Game Profit Loss.txt`.
- Refresh interval default 15 s, minimum 10 s (`MIN_INTERVAL`). Backs off on errors up to 60 s.
- OBS color: on/off switch labeled "OBS color"; sets `color`, `color1`, `color2`
  (GDI+ and FreeType text sources) as 0xAABBGGRR via `SetInputSettings`, only when the sign changes.
- Single instance: Windows named mutex `Local\WardogsProfitTracker_SingleInstance`;
  a second launch focuses the existing window.

## UI layout
- Window title "Wardogs Profit Tracker", dark theme, ~980×820.
- Left (scrollable): Steam URL + "Use my account", refresh interval, space toggle, output file,
  profit/loss color pickers + "Color by + / −", OBS WebSocket card (switch, source, host, port,
  password with show/hide, Test), "Keep last amount".
- Right side panel: Live output (status, big colored amount, raw Steam text) above Activity log.
- Activity log: newest at the bottom, auto-scrolls only if already at bottom, max 300 lines.
- Footer (always visible): Sign in again · Open txt · Start/Stop.
- Slim dark scrollbars (no arrows).

## Status
Logic was tested on Linux with stand-ins (fake Steam page / fake OBS). Not yet verified on
Windows: the actual window, the PyInstaller build, real Steam sign-in, real OBS, registry
default-browser lookup, DPAPI. Run `build_exe.bat` and test these first.

## Don't
- Don't hard-code anyone's Steam ID, URL, API key or passwords.
- Don't use the Steam Web API for this — it has no rich presence.
