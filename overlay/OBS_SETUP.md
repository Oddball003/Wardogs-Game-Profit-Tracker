# Browser overlays — OBS setup

Two pages, both styled like the game's money HUD:

- `money_hud.html` — one person's amount (below).
- `leaderboard.html` — everyone on the leaderboard ([further down](#leaderboard-overlay)).

Neither needs a text file: they read the tracker directly.

## Money HUD

`money_hud.html` shows one person's live profit styled like the game's money HUD
(Barlow, #C5C2BF, 18 px digits, 39 px box at 45% black, anchored on its right edge).
It gets the number from Wardogs Profit Tracker, so the tracker must be running.

## Add it

1. Start Wardogs Profit Tracker.
2. On the person's card, turn on **📺 Overlay**, then click the **Overlay link**
   under it. The link is copied, e.g.
   `file:///C:/…/overlay/money_hud.html?person=1`.
   (The page lives in the tracker's save folder — next to the exe, or
   `%LOCALAPPDATA%\Wardogs Profit Tracker` — and is never overwritten.)
3. In OBS: **Sources → + → Browser**, name it e.g. *Money HUD*, then:
   - **Local file:** **untick** (the link above already points at the file, and
     only the URL box keeps the `?person=1` part).
   - **URL:** paste the link.
   - **Width:** `400` (any width wider than your biggest number) — **Height:** `39`.
   - **Custom CSS:** leave the default.
   - **Shutdown source when not visible:** **untick**.
   - **Refresh browser when scene becomes active:** **untick**.
4. Keep the source at **100% scale** (pixels then match 1080p exactly) and drag it
   so its **right edge** sits where the HUD's right edge should be — the box grows
   to the left as the number gets longer.

It keeps working after OBS or the tracker restarts: the page loads from disk, asks
the tracker every 100 ms, and keeps the last number on screen while the tracker is
closed. When there's no amount (menus), the box disappears.

## Fine-tune the look

The quick ones are in the tracker under **Advanced › Browser overlays**: **Font**
(Google fonts load from the internet; installed fonts are used as they are —
digits come out as tall as Barlow's in any font), **Text
size**, **Text color** and **Color by profit / loss** (with its own Profit and
Loss colours; turn it on for the **Money HUD**, the **Leaderboard**, or both). They apply to both overlays and show within a moment; the box,
padding and leaderboard rows grow with the text size. Also there:

- **Background** — **Color** (pick one) with **Opacity** (0 % = no box), or
  **Image**: your own picture (png, jpg, gif, webp, svg) filling the box, faded
  by the same Opacity. The text itself is never faded.
- **Money HUD box W / H** and **Leaderboard box W** — **Auto** fits the text;
  hold − / + to step quickly. A wider HUD box keeps the number on the right.
- **Size OBS sources to fit** — with OBS WebSocket connected, the tracker sets
  the **Width / Height** of every Browser source showing `money_hud.html` or
  `leaderboard.html` (found by its link — no names to type), and keeps them in
  step when the text size, box size or number of leaderboard people changes.
  **Resize in OBS now** does it once. Sizes are shown under the switch.

These override `--digit-height`, `--color`, `--bg-rgb`, `--box-opacity`,
`--box-height` and the box widths in the file.

For the rest, open the copy of `money_hud.html` in the save folder with Notepad and change the
variables at the top (`--digit-height`, `--weight`, `--stroke`, `--color`,
`--tracking`, `--box-opacity`, `--box-height`, `--pad-left`, `--pad-right`,
`--nudge-y`). Then in OBS: right-click the source → **Refresh**.

To check the look in a normal browser, open the file and add `?preview=1` to the
address: sample numbers over a dark background.

Barlow is loaded from Google Fonts, so OBS needs internet when it starts the page.

## Leaderboard overlay

1. In the tracker's **Leaderboard** panel, turn on **📺 Browser overlay** and click
   the **Overlay link** to copy it (`file:///C:/…/overlay/leaderboard.html`).
2. In OBS add a **Browser** source the same way as above (**Local file** unticked,
   link as the URL). Size: **Width** `500`, **Height** `260` — add about 32 px of
   height for each person past six.
3. The panel is anchored top-right: place the source's **top-right corner** where
   the panel should sit.

It follows the tracker's Leaderboard settings — who's on it, **Highest first** /
**List order**, **How many**, **Number places**, **Names**. When places change
the rows slide. Nobody with an amount: the panel disappears.

**1st place** (under the Overlay link) picks what the leader gets:

- **👑 Crown** (default) — a gold crown.
- **# Number** — a plain `1`, like everyone else's place.
- **🖼 Custom** — your own picture (png, jpg, gif, webp or svg, up to 8 MB).
  Pick it with **Choose image…**; it stays where it is on your PC and the
  tracker hands it to the overlay. Swap the file and the overlay picks up the
  new one. If it can't be loaded, the crown is shown instead.

Options after the link: `?title=TEAM` changes the heading (`?title=` hides it),
`?plus=1` shows `+$` on profit. Look settings are at the top of the file
(`--row-height`, `--icon-size`, `--icon-radius`, `--gold`, `--slide-ms`, …).
`?preview=1` in a normal browser shows sample rows that shuffle.

## Remove the old setup (once the HUD looks right)

1. Delete the old **Text (GDI+)** source that showed the number (select it → Delete).
   Its **shader filter** goes with it.
2. **Tools → Scripts:** select the **letter-spacing Lua script** → **−** to remove it.
3. The hidden **"Profit Input"** text source is no longer needed for the HUD (the
   page reads the tracker directly). Delete it unless something else uses it — the
   tracker keeps writing its text file either way.
4. If that person's card in the tracker has an **OBS source** set to the old text
   source, clear it.
