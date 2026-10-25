# Wardogs-Game-Profit-Tracker

A Vibe Coded Python program to track your current game profits or loss

This Uses steam mini profile to grab the profit with a web browser it will use your default if its chrome based if not it will use edge
it also allows you to connect to obs and change the color of text based on if you are in profit or loss
downsides you need to login to steam for to not get limited but due to this it auto grabs the mini profile link and can view the number even if your profile is private
this is 100% vibe coded i probably cant support this long term if some wants to take this over thats ok with me
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

- `uninstall.bat` - removes the exe, settings, sign-in and optionally the build tools.
