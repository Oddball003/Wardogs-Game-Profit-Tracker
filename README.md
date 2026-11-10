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

## What it can do

- **Your amount, live** — in a text file for an OBS Text source, and / or a
  browser overlay that looks like the game's own money HUD.
- **Friends too** — track anyone on your Steam friends list, each with their
  own file or overlay.
- **A leaderboard** — everyone you pick, sorted by profit, as an overlay with a
  gold crown for 1st and rows that slide as places change (and / or a text
  file). Show WARDOGS teams with the factions' emblems and team colours.
- **An Output editor** — change how the overlays and text files look with a
  live preview and test players: font, size, colours, background, the
  leaderboard's layout, and more.
- **End-of-game tools** — one button (or an in-game hotkey) resets the
  leaderboard for the next game, and can save the results as a text file and
  a picture first.
- **Streamer.bot and Twitch chat** — let Streamer.bot control the tracker, and
  let viewers join the leaderboard and pick a team from chat.
- **OBS colours** — turn each person's OBS Text source green or red with their
  profit or loss.

## Download

Grab `Wardogs.Profit.Tracker.exe` from the
[latest release](../../releases/latest), put it in a normal folder (for example
a "Wardogs" folder on your desktop) and run it. Nothing else to install:
everything it needs is inside the exe, and it uses Microsoft Edge (part of
Windows 10 / 11) in the background.

Windows may show a SmartScreen warning because the exe isn't signed: click
**More info → Run anyway**.

## Quick start

1. Start the exe. The first time, a browser window opens: sign in to Steam
   there and keep **Remember me** ticked, so you stay signed in.
2. Your card is first in the list. Pick how to show your amount:
   - **📺 Overlay**, then click the **Overlay link** and paste it into an OBS
     **Browser** source (Local file unticked, 400 × 39), or
   - **📄 Save to file**, pick a file, and point an OBS **Text** source at it
     (**Read from file**).
3. Play. New amounts show up within a few seconds of Steam sending them.

## The window

**People** — whose amounts to show: you, and any friends (**+ Add person**,
then click the name to search your friends list). Friends are tracked by their
Steam account, so a name change doesn't lose them. Each card has:

- **Name**: their Steam name, their linked **Twitch name**, or a **Custom…** label.
- **📄 Save to file**: their own text file (its **Name** chip puts their name in
  front: `Name: +$3,740`), and with OBS colouring on, the OBS Text source to
  colour.
- **🏆 Leaderboard**, with **📌** to pin them: pinned players stay on after a
  reset, and only show while they're in a game.
- **📺 Overlay**: their own money HUD.
- **🛡 Team** (while Teams is on) and **🔗 Link** for their Twitch account (while
  Streamer.bot is on).

**Leaderboard** — switch it on to use it.

- **📺 Browser overlay** and **👁 Preview**: the preview opens a small window
  with the live leaderboard as it looks on stream, with a **☰ Full list**
  toggle to see everyone.
- **🧹 Reset**: everyone off except you and anyone pinned, amounts back to zero,
  ready for the next game. Nothing resets by itself.
- **⌨**: the reset hotkey (**Ctrl+Alt+S** by default, works even in-game). Click
  it to turn it on / off or pick other keys.
- **💾 Save results on reset**: Reset first saves a dated text file of the
  standings and a picture of the leaderboard in **Saved leaderboards** next to
  the app (**Open folder**).
- **📌 Pin by default**: everyone you put on the leaderboard is pinned.

**Live output** — what's being written and shown right now, and
**✏ Edit outputs** to open the Output editor.

**Advanced** (opens as you left it):

- **Minimize to tray** (on by default) and **Start with Windows** (off by default).
- **Friend folders**: sorts your people into **Active** (playing) and
  **Inactive**, plus your own folders (📁 on a card), with a search box. Pin a
  folder to keep everyone in it on the leaderboard.
- **Twitch names by default**: everyone goes by their linked Twitch name
  (their Steam name until they're linked).
- **Hide overlays on stream**: the overlays show nothing until you turn it off.
- **Refresh friends page**: off by default, since the page updates live; only
  pick a time if the number stops updating.
- **Streamer.bot**: see below.

## Output editor

**✏ Edit outputs** opens a window with a live preview on the left and the
settings on the right. Tabs at the top switch between the **🏆 Leaderboard**,
the **$ Money HUD**, a person's **📄 Text file** and the **📄 Leaderboard txt**,
and only the settings for that output are shown. Every change saves straight
away and shows on stream within a moment.

- **Overlays**:
  - Font (Barlow, the game's, HUD-style Google fonts, or any font on your PC),
    size and colour.
  - Profit / loss colours.
  - Background: a colour with opacity, or a picture.
  - Box size.
- **Leaderboard**:
  - Order (highest first, or as listed), places, names and how many.
  - 1st place: crown, number or your own picture.
  - Teams with their emblems or your own badges, team colours, and names in
    team colour.
- **Text files**: the leaderboard's text file, and the money format
  (`+$3,740` or `+ $3,740`).
- **Keep last amount**: for each output on its own, keep showing the last
  amount in menus instead of going blank.
- **OBS WebSocket**:
  - **Color by profit / loss** for the app and each person's OBS Text source.
  - Connection details, folded away so the password isn't on screen.
  - Test and Connect / Disconnect.
  - **Size OBS sources to fit**, which resizes the overlays' Browser sources
    in OBS for you.

## Overlays in OBS

Both overlays read the tracker directly, so they don't need a text file. Add an
OBS **Browser** source with **Local file unticked** and the overlay's link as
the URL:

- **Money HUD**: turn on **📺 Overlay** on a card and click its **Overlay
  link**. Size 400 × 39.
- **Leaderboard**: turn on **📺 Browser overlay** in the Leaderboard panel and
  click its **Overlay link**. The panel is anchored top-right.

Full steps and the finer details: [`overlay/OBS_SETUP.md`](overlay/OBS_SETUP.md).

## Streamer.bot

Turn on **Advanced › Streamer.bot › Let Streamer.bot control the tracker**. Every
command is listed there with a **Copy** button; paste the link into a
Streamer.bot **Fetch URL** sub-action. Commands can hide the overlays, change
colours, add people to the leaderboard, set teams, refresh, reset and more.
The tracker can also send each game's result to a Streamer.bot action, so it
can post it in chat.

Viewers can use Twitch chat commands: `!join wardogs leaderboard [team]`,
`!wardogs team <team>`, `!wardogs friends` (whispers who's playing) and, for
mods, `!wardogs tracker link`. Setup and every command:
[`STREAMERBOT.md`](STREAMERBOT.md).

## Privacy and files

- Settings (`settings.json`), your Steam sign-in and the activity log are kept
  next to the exe. If Windows won't allow that (Program Files, run from inside
  a zip, or Defender's "Controlled folder access"), they go in
  `%LOCALAPPDATA%\Wardogs Profit Tracker` instead, and the log says so.
- The tracker answers overlays and Streamer.bot on `127.0.0.1:8765`: this PC
  only, and only while something needs it. Websites you visit can't read it.
  Streamer.bot needs your private key.
- The OBS password is stored encrypted with your Windows account (DPAPI).
- The tracker's browsers never sign in to your Microsoft account.
- **Show log** opens `activity_log.txt`, which records every change with a
  time stamp. It's handy if something looks stuck.

## Build it yourself

Needs Python 3 on Windows. Run:

```
build_exe.bat
```

It installs the dependencies (playwright, obsws-python, pillow, pystray,
pyinstaller) and builds `dist\Wardogs Profit Tracker.exe`.

## Remove

Run `uninstall.bat`. It removes the exe, settings, Steam sign-in and log (next
to the exe and in `%LOCALAPPDATA%\Wardogs Profit Tracker`), offers to delete the
text files it wrote, and optionally the build tools.

## Downloads

[![Latest release](https://img.shields.io/github/v/release/Oddball003/Wardogs-Game-Profit-Tracker?label=latest)](https://github.com/Oddball003/Wardogs-Game-Profit-Tracker/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/Oddball003/Wardogs-Game-Profit-Tracker/total)](https://github.com/Oddball003/Wardogs-Game-Profit-Tracker/releases)

- **[Download the latest version](https://github.com/Oddball003/Wardogs-Game-Profit-Tracker/releases/latest/download/Wardogs.Profit.Tracker.exe)** (Wardogs.Profit.Tracker.exe)
- [Latest release notes](https://github.com/Oddball003/Wardogs-Game-Profit-Tracker/releases/latest)
- [All releases](https://github.com/Oddball003/Wardogs-Game-Profit-Tracker/releases)

## Credits

The Lonestar, Valkyra and Manticore faction emblems in `overlay/teams/` are
artwork from WARDOGS by BULKHEAD, published by Team17. They're included
only to show players' teams while playing and streaming the game. This
tracker is a free fan project, not affiliated with or endorsed by BULKHEAD
or Team17.
