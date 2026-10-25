# Wardogs Profit Tracker

A small Windows app for streamers playing WARDOGS. It reads your Steam rich
presence, pulls out the money amount (`+ $3,740` / `- $3,740`), and writes it to
a text file that an OBS Text source can show. It can also recolor that OBS text
for profit / loss through OBS WebSocket.

## Build

Needs Python 3 on Windows. Run:

```
build_exe.bat
```

It installs the dependencies (playwright, obsws-python, pyinstaller) and builds
`dist\Wardogs Profit Tracker.exe`. Move the exe anywhere you like; it keeps its
settings and Steam sign-in next to itself.

## Use

1. Start the exe. The first time, a browser opens: sign in to Steam there.
2. Your miniprofile URL is filled in from your account (or paste a Steam ID).
3. Pick an output text file and point an OBS Text source at it ("Read from file").
4. Optional: turn on **OBS color**, enter your OBS WebSocket details
   (OBS › Tools › WebSocket Server Settings) and press **Test**.

Uses your default browser if it's Chromium-based (Chrome, Edge, Brave...),
otherwise Edge or Chrome. The OBS password is stored encrypted with Windows DPAPI.

## Remove

- `remove_old_exe.bat` - deletes the built exe (optionally settings and sign-in).
- `uninstall.bat` - removes the exe, settings, sign-in and optionally the build tools.
