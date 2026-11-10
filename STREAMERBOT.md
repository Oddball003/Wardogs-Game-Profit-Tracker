# Streamer.bot

Streamer.bot can control the tracker (hide the overlays, change colours, add
people to the leaderboard, save it…), and the tracker can tell Streamer.bot when
a game ends so it can post the result in chat or run anything else.

Everything stays on your PC (`127.0.0.1`).

## Streamer.bot controls the tracker

1. In the tracker: **Advanced › Streamer.bot › Let Streamer.bot control the
   tracker** on.
2. Every command is listed there: click **Copy** next to one to copy its link,
   or **Copy commands** for all of them (paste them into a text file to keep).
   Each command is a link with your key in it (the window shows it as ••••). Treat the key like a password: anyone with it
   could control the tracker from your PC. **New key** replaces it, and the old
   links stop working.
3. In Streamer.bot, make an action (for example on a hotkey, a Stream Deck
   button or a chat command) with the sub-action **Core › Network › Fetch URL**,
   and paste one of the links.

The links look like `http://127.0.0.1:8765/api/<command>?key=<your key>`. Add
more options with `&`, for example `…/api/board/add?key=…&name=Chopper`.

| Command | What it does |
| --- | --- |
| `status` | What the tracker is showing right now (JSON: people, amounts, leaderboard, hidden) |
| `hide` / `show` / `hide/toggle` | Hide every overlay so nothing shows on stream (see-through), or show them again |
| `overlay/off` / `overlay/on` / `overlay/toggle` | Every overlay off or on (on brings back the ones that were on). Add `&name=Chopper` for one person, `&name=leaderboard` for the leaderboard overlay |
| `board/on` / `board/off` / `board/toggle` | The leaderboard on or off |
| `board/add` / `board/remove` | `&name=Chopper`: put someone on the leaderboard or take them off. A new name is added to the tracker |
| `color` | Any of `&text=` `&profit=` `&loss=` `&bg=` (overlay colours, like `5FE39B`), `&opacity=0–100`, `&pl=hud\|board\|both\|off` (colour by profit / loss), `&app_profit=` `&app_loss=` (the app and OBS Text sources) |
| `team/set` | `&name=Chopper&team=valkyra`: put someone on Lonestar, Manticore or Valkyra (`none` clears it) |
| `refresh` | Refresh the Steam friends page once |
| `save` | Save the results (text + picture), then reset: everyone off except you and anyone pinned, amounts back to zero |
| `board/end` | Reset quietly: everyone off except you and anyone pinned (📌 on their card or folder), amounts back to zero, nothing saved |
| `gameover` | End the game now: send the result to Streamer.bot (if that's on) |

Names match the Steam name or the label you gave someone (any case); `me`
means you. The answer is JSON with `"ok": true`, or `"ok": false` and an
`"error"` saying what's wrong.

## The tracker tells Streamer.bot when a game ends

1. In Streamer.bot: **Servers/Clients › HTTP Server**, tick **Auto Start** and
   press **Start** (the address is usually `http://127.0.0.1:7474`).
2. Make an action called **Wardogs Game Over** (or any name, then type the same
   name in the tracker).
3. In the tracker: **Advanced › Streamer.bot › Send game results to
   Streamer.bot** on, check the address and the action name, and press
   **Test**.

A game counts as over when your amount has been gone for 15 seconds (back in the
menus). The action gets these arguments:

| Argument | Example |
| --- | --- |
| `%who%` | `Oddball` |
| `%amount%` | `+$18,400` |
| `%amountValue%` | `18400` |
| `%result%` | `profit`, `loss` or `even` |
| `%message%` | `Oddball finished the game +$18,400 in profit` |
| `%leaderboard%` | `1. Oddball +$18,400 \| 2. Chopper +$12,750` |
| `%players%` | `2` |
| `%place1%`, `%place1Amount%`, `%place2%`… | `Oddball`, `+$18,400`, `Chopper`… |
| `%test%` | `True` when it came from the **Test** button |

For example, a **Twitch › Chat › Send Message to Channel** sub-action with
`%message%` posts the result in chat.

## Twitch chat commands

Viewers who play with you can join the leaderboard from chat. First a mod (or
you) links their Twitch account to their player. You can also type it in the
**Twitch** box on their card in the tracker; it shows while Streamer.bot
control is on.

Make one Streamer.bot **command** and **action** for each. In each action, use
**Core › Network › Fetch URL** with the link below (from **Copy commands**) and
tick **Parse as JSON** off. Set its **Variable name** to `reply`, then add
**Twitch › Chat › Send Whisper** to `%user%` with the message `%reply%`.

| Command | Fetch URL (after `…/api/`) | Who |
| --- | --- | --- |
| `!wardogs friends` | `twitch/friends?key=…&user=%userName%&format=text` | Everyone |
| `!wardogs tracker link` | `twitch/link?key=…&user=%userName%&input=%rawInput%&format=text` | **Moderators + Broadcaster only**: set that in the command's permissions |
| `!join wardogs leaderboard` | `twitch/join?key=…&user=%userName%&input=%rawInput%&format=text` | Everyone |
| `!wardogs team` | `twitch/team?key=…&user=%userName%&input=%rawInput%&format=text` | Everyone |

How it goes:

1. A viewer types `!wardogs friends`. They get a whisper like
   `In WARDOGS now: 1. Oddball +$5,000 (@you) · 2. RotorBird +$18,400 · 3. Spandy Pandy -$300`.
   That's everyone in WARDOGS right now: you and your Steam friends, tracked
   or not.
2. A mod types `!wardogs tracker link 2 @RotorFan`, which links player 2 to
   that Twitch account. A player you don't track yet is added to the tracker
   (not on the leaderboard). Without `@viewer`, it links the person typing it.
   You can also use the player's name instead of a number. A Twitch account
   belongs to one player at a time.
3. The viewer types `!join wardogs leaderboard`, and they're on the leaderboard.
   With **🛡 Teams** on they can add their team: `!join wardogs leaderboard valkyra`.
   Later, `!wardogs team lonestar` switches team (`none` clears it). Short
   names work: `lone`, `mant`, `valk`.

Twitch only lets accounts with a verified phone number send whispers, and it
limits how many new people one account can whisper each day. If whispers don't
arrive, use **Send Message to Channel** with `%reply%` instead.

## Resetting and saving the leaderboard

**🧹 Reset** in the Leaderboard panel, the tray icon's menu, the hotkey
(**Ctrl+Alt+S** by default, works even in-game) and the `save` command get the
leaderboard ready for the next game: everyone comes off it except you and
anyone pinned (**📌** next to their Leaderboard chip, or on their folder;
pinning **Active** keeps whoever is still in a game), and everyone's amount
goes back to zero. Viewers can rejoin with `!join wardogs leaderboard`.

With **💾 Save results on reset** on (and always for the `save` command), it
first saves:

- a dated text file with the standings, and
- a picture of the leaderboard overlay with a see-through background,

in **Saved leaderboards** next to the app (**Open folder** opens it). To reset
without saving anything, use the `board/end` command. Nothing resets by itself
when a game ends.
