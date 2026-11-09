# Wardogs Game Profit Tracker

A vibe coded Python program to track your current game profit or loss in WARDOGS
and show it on stream.

It reads your status from the Steam friends page with a web browser (your default
browser if it's Chrome-based, otherwise Edge), pulls out the money amount
(`+$3,740` / `-$3,740`) and writes it to a text file that an OBS Text source can
show. It can also show it as a live browser overlay styled like the game's own
money HUD (one per person, plus a leaderboard), and connect to OBS to change the
text color depending on whether you're in profit or loss.

You need to sign in to Steam once, so it can read your own status. That also means
it works even if your profile is private.

This is 100% vibe coded. I probably can't support it long term — if someone wants
to take it over, that's OK with me.

## Download

Grab `Wardogs.Profit.Tracker.exe` from the
[latest release](../../releases/latest), put it in a normal folder (e.g. a
"Wardogs" folder on your desktop or in Downloads) and run it. Nothing else to
install — Python and everything it needs are inside the exe; it uses Microsoft
Edge (part of Windows 10/11) in the background.

Settings and your Steam sign-in are saved next to the exe. If Windows won't
allow that (Program Files, run straight from inside a zip, or Defender's
"Controlled folder access"), they're saved in `%LOCALAPPDATA%\Wardogs Profit Tracker`
instead, and the log says so. When signing in to Steam, keep **Remember me**
ticked so you stay signed in.

Windows may show a SmartScreen warning because the exe isn't signed: click
**More info → Run anyway**.

## Use

1. Start the exe. The first time, a browser opens: sign in to Steam there.
2. Pick a text file for your amount (**Separate file** › **Browse…**) and point
   an OBS Text source at it (**Read from file**).
3. Play. New amounts show up within a few seconds of Steam sending them.
4. Optional, a live overlay that looks like the game's money HUD: turn on
   **📺 Overlay** on your card, click the **Overlay link** and paste it into an
   OBS **Browser** source (Local file unticked). No text file needed.
5. Optional, to colour an OBS Text source too: open **Advanced › OBS WebSocket**,
   enter your details (OBS › Tools › WebSocket Server Settings › Show Connect
   Info), type each person's OBS Text source name on their card (under their
   separate file) and press **Test**. With **Color by profit / loss** on, it
   connects by itself.

## Settings

- **People** — whose amounts to show: you (default) and/or friends. Click a
  person to search your Steam friends list, with profile pictures (it fills in
  once the tracker is running); you can also type a name. A friend's amount
  shows while they're in WARDOGS. **+ Add person** tracks several at once.
  Each person has:
  - **Name**: **Steam name**, or **Custom…** to type your own label. It's used
    wherever their name is shown — their separate file and the leaderboard.
  - **Separate file** — their own text file. Its **Name** chip (off by
    default) puts their name in front: `Name: +$3,740`.
  - **Leaderboard** — puts them in the shared leaderboard file. Both can be on
    at once (e.g. your own file *and* the leaderboard).
  - **OBS source** (with Color by profit / loss on) — the OBS Text source showing their
    separate file. It turns green or red from that person's own amount, so
    everyone's source is coloured on its own. The leaderboard isn't coloured
    (one OBS text source can only be one colour).
- **Leaderboard** — everyone on the leaderboard, as a text file (a line each)
  and / or a **📺 Browser overlay**. **Highest first** (re-sorts as amounts
  change) or **List order**, plus **Number places** and **Names**:
  `1. Name: +$3,740`. **How many** keeps it to the first few (**All**, or **Top** and − / + for 1–30).
  People with no amount right now (in menus, offline) are left off.
- **📺 Overlay** (on a person's card) — a live browser overlay for that person;
  see below.
- **Advanced › Minimize to tray** — minimising hides the window to a tray icon
  (Show / Start-Stop tracking / Quit). On by default; starting the app again
  brings the window back.
- **Advanced › Start with Windows** (off by default) — opens when you sign in
  to Windows and starts tracking, straight to the tray when Minimize to tray is
  on. Turn it off there (or uninstall) to remove it.
- **Advanced › Browser overlays** — the 📺 overlays' **Font** (Barlow, the game's,
  by default; HUD-style Google fonts like Oswald, Rajdhani, Teko, Bebas Neue, or any
  font installed on your PC), **Text size** (− / +,
  18 px is the game's own), **Text color**, and **Color by profit / loss** (chips for
  **Money HUD** and **Leaderboard**, each on its own) with its own Profit and
  Loss colours (names and $0 keep the text colour);
  **Background** — a colour with **Opacity**, or your own **Image**; **box
  sizes** (Auto fits the text); and **Size OBS sources to fit**, which sets the
  Width / Height of every OBS Browser source showing an overlay (over OBS
  WebSocket — it finds them by their link, no names to type). Changes show in
  OBS within a moment.
- **Advanced › `+$3,740` / `+ $3,740`** — with or without a space after the
  +/- (no space by default).
- **Advanced › Refresh friends page** — **Off** (default), or every 15, 30 or
  60 minutes. The page updates live on its own; refreshing it often can get you
  rate limited by Steam, so only pick a time if the number stops updating.
- **Advanced › App / OBS text color › Color by profit / loss** — one switch for colouring the
  amount in the app and each person's OBS source (green / red, colours
  pickable). Off: plain white, and OBS isn't touched.
- **Advanced › OBS WebSocket** — connection details (fold away with **Connection**),
  **Test**, and **Connect / Disconnect**. Turning colouring on connects
  automatically if someone has an OBS source; **Disconnect** stops using OBS
  until you press **Connect**. OBS is only contacted when someone has an OBS
  source filled in.
- **Advanced › Keep last amount** (switch, on by default) — keeps showing the last
  amount in menus instead of going blank. Advanced opens as you last left it.

### Money HUD overlay (OBS Browser Source)

`overlay/money_hud.html` shows a person's live profit styled like the game's
money HUD, updating within ~100 ms. Turn on **📺 Overlay** on a person's card and
click the **Overlay link** to copy it, then add an OBS **Browser** source with
**Local file unticked** and the link as the URL (400 × 39).

`overlay/leaderboard.html` is the same look for the whole leaderboard: 1st place
gets a gold crown (or **1st place › Custom** for your own picture, or **Number**
for a plain `1`), and rows slide when places change. Turn on **📺 Browser
overlay** in the Leaderboard panel and copy its link the same way.

Neither needs a text file. Full steps and the style settings:
`overlay/OBS_SETUP.md`. The tracker answers on `127.0.0.1:8765` (this PC only),
and only while an overlay is turned on.

**Show log** opens `activity_log.txt` (next to the exe), which records every change
with a time stamp — handy if something looks stuck.

The OBS password is stored encrypted with your Windows account (DPAPI); it can't be
read on another PC.

## Build it yourself

Needs Python 3 on Windows. Run:

```
build_exe.bat
```

It installs the dependencies (playwright, obsws-python, pillow, pyinstaller) and builds
`dist\Wardogs Profit Tracker.exe`.

## Remove

Run `uninstall.bat`. It removes the exe, settings, Steam sign-in and log (next
to the exe and in `%LOCALAPPDATA%\Wardogs Profit Tracker`), offers to delete the
text files it wrote, and optionally the build tools.
