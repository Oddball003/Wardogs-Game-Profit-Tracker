## What's new in v1.5.0

### New: Output editor
**✏ Edit outputs** (in the Live output panel) opens a visual editor with a live preview of your overlays and text files, using test players, and the settings right beside it. Change the font, size, colours, background and box size; choose how the leaderboard looks (order, places, names, teams, how many, 1st place); pick the text files and the money format; set Keep last amount for each output on its own; and connect OBS. Every change saves straight away and shows on stream within a moment.

### New: Streamer.bot support
Streamer.bot can now run the tracker for you, from hiding the overlays on stream to changing colours and managing who's on the leaderboard. Your viewers can get in on it too: they can check who's playing, join the leaderboard and pick a team straight from Twitch chat. When a game ends, the tracker hands Streamer.bot the result and the final standings, so it can post them in chat or kick off any action you like. Every command is listed in **Advanced › Streamer.bot** with its own Copy button, and [STREAMERBOT.md](https://github.com/Oddball003/Wardogs-Game-Profit-Tracker/blob/main/STREAMERBOT.md) has the full guide.

### New: reset the leaderboard, and save your results
- **🧹 Reset** gets the leaderboard ready for the next game: everyone comes off except you and anyone you've pinned (📌), and the amounts go back to zero.
- With **💾 Save results on reset** on, it first saves a dated text file of the standings and a picture of the leaderboard, in a **Saved leaderboards** folder next to the app.
- Reset from the button, the tray icon, Streamer.bot, or a **hotkey that works even in-game** (Ctrl+Alt+S by default, or pick your own).
- **📌 Pin by default** pins everyone you put on the leaderboard. Pinned players only show while they're in a game.

### New: teams
- Show each player's WARDOGS team on the leaderboard, with the faction's own emblem for **Lonestar**, **Manticore** or **Valkyra**, and their name in the team's colour.
- Set a player's team on their card, or let viewers pick their own when they join from chat.

### New: leaderboard preview
- **👁 Preview** opens a small window with your live leaderboard exactly as it looks on stream, with a **☰ Full list** toggle to see everyone.

### New: friend folders
- **Advanced › Friend folders** sorts your people into **Active** (playing) and **Inactive**, plus folders of your own, with a search box. Pin a folder to keep everyone in it on the leaderboard.

### New: Twitch names
- A person can go by their linked **Twitch name** instead of their Steam name. **Advanced › Twitch names by default** switches everyone over at once.

### New: hide the overlays on stream
- **Advanced › Hide overlays on stream** makes the overlays show nothing until you turn it back off.

### Changed
- Friends are now remembered by their Steam account, so they're still tracked after they change their Steam name.
- The main window is simpler: the overlay look, the leaderboard's display options, the text file choice, the money format, Keep last amount and OBS WebSocket all moved into the Output editor.
- **Separate file** on each person's card is now called **Save to file**.

### Fixed
- Edge could sign the tracker's Steam browser in to your Microsoft account and turn on sync. It can't anymore. If yours was already signed in, delete the `steam_browser_profile` folder and sign in to Steam again from the tracker.

### Upgrading
Replace the old `Wardogs Profit Tracker.exe` with this one, and keep `settings.json` and the `steam_browser_profile` folder next to it. Your settings carry over, and the overlay pages update by themselves (your old copies are kept as `.old.html`).

Windows may show a SmartScreen warning because the exe isn't signed: click **More info → Run anyway**.
