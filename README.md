# Wardogs Game Profit Tracker

A vibe coded Python program to track your current game profit or loss in WARDOGS
and show it on stream.

It reads your status from the Steam friends page with a web browser (your default
browser if it's Chrome-based, otherwise Edge), pulls out the money amount
(`+$3,740` / `-$3,740`) and writes it to a text file that an OBS Text source can
show. It can also connect to OBS and change the text color depending on whether
you're in profit or loss.

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
4. Optional, to colour OBS too: enter your OBS WebSocket details
   (OBS › Tools › WebSocket Server Settings › Show Connect Info), type each
   person's OBS Text source name on their card (under their separate file) and
   press **Test**. With **Color by profit / loss** on, it connects by itself.

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
- **Leaderboard file** — one file with a line for everyone on the leaderboard.
  **Highest first** (re-sorts as amounts change) or **List order**, plus
  **Number places** and **Names**: `1. Name: +$3,740`. People with no amount
  right now (in menus, offline) are left off.
- **Advanced › `+$3,740` / `+ $3,740`** — with or without a space after the
  +/- (no space by default).
- **Advanced › Refresh friends page** — **Off** (default), or every 15, 30 or
  60 minutes. The page updates live on its own; refreshing it often can get you
  rate limited by Steam, so only pick a time if the number stops updating.
- **Text color › Color by profit / loss** — one switch for colouring the
  amount in the app and each person's OBS source (green / red, colours
  pickable). Off: plain white, and OBS isn't touched.
- **OBS WebSocket** — connection details (fold away with **Connection**),
  **Test**, and **Connect / Disconnect**. Turning colouring on connects
  automatically if someone has an OBS source; **Disconnect** stops using OBS
  until you press **Connect**. OBS is only contacted when someone has an OBS
  source filled in.
- **Keep last amount** — keeps showing the last amount in menus instead of going blank.

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
