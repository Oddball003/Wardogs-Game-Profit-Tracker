# Wardogs Game Profit Tracker

A vibe coded Python program to track your current game profit or loss in WARDOGS
and show it on stream.

It reads your status from the Steam friends page with a web browser (your default
browser if it's Chrome-based, otherwise Edge), pulls out the money amount
(`+ $3,740` / `- $3,740`) and writes it to a text file that an OBS Text source can
show. It can also connect to OBS and change the text color depending on whether
you're in profit or loss.

You need to sign in to Steam once, so it can read your own status. That also means
it works even if your profile is private.

This is 100% vibe coded. I probably can't support it long term — if someone wants
to take it over, that's OK with me.

## Download

Grab `Wardogs.Profit.Tracker.exe` from the
[latest release](../../releases/latest), put it in any folder and run it.
Settings and your Steam sign-in are saved next to the exe.

Windows may show a SmartScreen warning because the exe isn't signed: click
**More info → Run anyway**.

## Use

1. Start the exe. The first time, a browser opens: sign in to Steam there.
2. Pick an output text file (**Browse…**) and point an OBS Text source at it
   (**Read from file**).
3. Play. New amounts show up within a few seconds of Steam sending them.
4. Optional: turn on **OBS color**, enter your OBS WebSocket details
   (OBS › Tools › WebSocket Server Settings › Show Connect Info) and press **Test**.

## Settings

- **Space after sign** — `+ $3,740` or `+$3,740`.
- **Text before / after amount** — optional, e.g. `Profit: ` gives
  `Profit: + $3,740`. Spaces count. Nothing is added while the amount is blank.
- **Refresh the Steam friends page** — off by default. The page updates live on
  its own; refreshing it often can get you rate limited by Steam, so only turn it
  on if the number stops updating.
- **Text color** — profit / loss colors, used in the app and for OBS color.
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

It installs the dependencies (playwright, obsws-python, pyinstaller) and builds
`dist\Wardogs Profit Tracker.exe`.

## Remove

Run `uninstall.bat`. It removes the exe, settings, Steam sign-in and log, and
optionally the build tools.
