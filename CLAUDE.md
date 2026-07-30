# CLAUDE.md — Henderburgh Arcade

Project context for a fresh session. This file did not exist before
2026-07-30 despite being referenced by name in several module docstrings
(`PRODUCTION.md` too) — those references predate this file; treat any
claim in this doc as reconstructed from the current code and this
session's own history, not carried forward from a prior version of this
file.

## What this is

A 64×64 LED matrix "arcade console" built on an Apollo M-1 WLED-MM panel
(HUB75, ESP32-S3), currently driven by a Mac mini over the network. It
runs classic games, live data "modes" (stock ticker, ISS tracker, flight
tracker, sports scoreboard, news headline ticker, weather + severe
alerts, site guestbook, and an `ambient` rotation tying them together),
video/screen mirroring, and WLED's own ambient lighting effects — all
through one local HTTP server with a web control page and a
phone-optimized remote controller page.

This Mac + WLED-MM setup is the **development rig**, not the final
product. See `PRODUCTION.md` for the sellable-device plan (Raspberry Pi +
HUB75 panel, no Mac, no WLED-MM) — `display.py` already has the seam for
that swap built in (see Architecture below).

## Architecture

**`arcade_server.py`** — the local brain (`ThreadingHTTPServer`, port
7333). Runs the active mode in a background thread, streams frames to the
panel, serves `/` (control page, `arcade.html`) and `/remote` (phone
controller, `remote.html`) plus a JSON API. Start it with:

```
cd wled-m1-arcade
.venv/bin/python arcade_server.py
```

(`.venv` is needed for `mirror` mode — mss + Pillow. Every other mode is
stdlib-only.) In production this runs under launchd as
`com.henderburgh.arcade` (`~/Library/LaunchAgents/com.henderburgh.arcade.plist`);
restart with `launchctl kickstart -k gui/$(id -u)/com.henderburgh.arcade`.

**The mode contract** (`engines.py`) — every mode is a class with:

```python
e = SomeEngine()
e.input(cmd)      # "left"/"right"/"up"/"down"/"rotate"/"drop" from the dpad
e.auto()           # demo/ambient mode: engine steers itself
e.tick()            # advance one step
rgb = e.frame()    # WIDTH*HEIGHT*3 bytes, row-major, top-left origin, returns bytes
e.tick_rate         # seconds between ticks
e.score
```

Registered in `engines.ENGINES` (`name -> class`); `engines.PLAYABLE` is
that set minus `menu`/`boot`. Adding a new mode means: write the engine
class, add it to `ENGINES`, add it to `MenuEngine.NATIVE_GAMES` (label +
accent color) if it should appear on the panel's own menu grid, and add a
small icon case in `MenuEngine._icon`.

**Data modes are pure-I/O / pure-render, split into two files on
purpose** — this is the load-bearing pattern for the whole project:

- A plain module (`market.py`, `satellite.py`, `flights.py`, `sports.py`,
  `news.py`, `weather.py`, `blog.py`) owns ALL network I/O: a
  background-thread poller with a last-good cache, a `FEED` singleton, a
  `get()` that never blocks, and a
  `*_config.json` file (with matching `save_config`/`load_config`) for
  anything an owner should be able to configure without a code edit.
  Feed threads are self-limiting: they start on first `get()` and stop
  once nothing has read for `IDLE_STOP` (120s), so leaving a mode doesn't
  leave a thread polling the internet forever.
- The corresponding `*Engine` class in `engines.py` does **zero I/O**. It
  only calls `FEED.get()` in `tick()` and renders whatever came back in
  `frame()`. This is what keeps a slow or dead upstream API from ever
  stalling the render loop (dropped frames on WLED, stutter on a future
  Pi).

Two rules every feed module enforces, no exceptions:
1. **Never block the render loop.** All I/O happens off the calling
   thread; `get()` returns instantly from cache.
2. **Never invent a number.** If the network is down, keep serving the
   last good data and mark it stale (an "age" the engine can check and
   flag on-screen) rather than showing something false with confidence.
   Same principle drives `backgrounds.py`'s rule about WLED effects
   ("never invent what an effect looks like — every pixel comes from the
   real panel").

**Rendering pipeline** — `display.py` is the one seam between "a mode
computed some pixels" and "pixels reached real hardware":

```
mode.frame() -> arcade_server's render loop (dedup + rate cap)
             -> display.get_renderer() -> WledDDP.send(rgb) [today]
                                        -> BonnetRenderer.send(rgb) [production, not built yet]
```

`WledDDP` (`wled_ddp.py`) streams over WLED's DDP protocol, UDP port
4048. **Do not raise `PANEL_FPS` or remove frame de-duplication** —
flooding WLED with full frames locks the panel hard enough to need a
physical power cycle; this is a hard-won constraint, not a guess.
`BonnetRenderer` is a deliberate `NotImplementedError` stub (not a silent
no-op) for the eventual Raspberry Pi + Adafruit RGB Matrix Bonnet
production path — chain/parallel HUB75 tiling for multi-panel setups is
meant to live entirely inside that renderer, invisible to every mode
above it.

**`backgrounds.py`** — WLED's own firmware effects used as ambient
lighting or as a live-captured underlay behind a game's sprites. Hard
rule: never synthesize what an effect looks like in software; every pixel
either comes from telling the real panel to run the effect, or from a
recorded peek of the real LED buffer via WLED's liveview websocket. The
in-file software generators are dead code for any real display path,
kept only as offline experiment helpers.

**The 3×5 font (`_FONT3x5`/`draw_text3x5` in `engines.py`) is
uppercase-only** and silently drops any character it doesn't have —
no error, no crash, just a quietly wrong string on the panel. This has
caused the *same bug three times* on three different modes (ticker,
flights, sports), always from a live external API returning mixed-case
text. `blog.py` is the fourth and highest-risk site — free-form
user-written posts are the most mixed-case source in the project — and is
uppercased at its I/O boundary for exactly that reason. **Any new mode
that draws API-sourced text must `.upper()` it at
the I/O module boundary** (in the feed module, not the engine) — this is
already the convention for every existing feed. Verify by actually
rendering a frame with real data and reading the pixels; this class of
bug is invisible to a code read.

## What's built

**Games** (all native, headless, stdlib-only): snake, tetris, pong,
breakout, tron, flappy, invaders, life, dodge, 2048, tunnel, powder,
brawler, chase. Plus a TIC-80 fantasy-console cartridge loader
(`tic80_core.py`, `TicCartEngine`) that scans `carts/tic80/*.tic` and
appends them to the menu automatically.

**Data modes**, in build order:
- **ticker** (`market.py`/`TickerEngine`) — crypto (CoinGecko) + stocks
  (Yahoo Finance v8 chart), config-driven watchlist.
- **satellite** (`satellite.py`/`SatelliteEngine`) — ISS live position
  (wheretheiss.at) + next visible pass prediction (polluxlabs), framed as
  an "ISS countdown." Owns `location_config.json`, the project's one
  source of truth for the owner's home coordinates — reused by flights,
  don't duplicate it.
- **flights** (`flights.py`/`FlightEngine`) — nearby ADS-B traffic
  (adsb.lol) + route/airline enrichment (adsbdb), reuses satellite's
  location config. Heading-oriented plane icon (procedurally rotated from
  real `track_deg` via `draw_line`, not a fixed sprite table — deliberately
  not a real airline logo, no IP exposure), color-coded by altitude band
  (chosen over distance-band coloring: distance is already shown as text,
  altitude wasn't color-coded anywhere, and altitude is the more standard
  "what kind of traffic is this" signal on real ATC displays).
- **sports** (`sports.py`/`SportsEngine`, 2026-07-30, expanded same day) —
  NFL/NBA/MLB/NHL/EPL/NCAAF/NCAAB scores via ESPN's free undocumented site
  API. Pinned favorite team (full-screen score, scoring-flash animation,
  win probability when available) + a rotating ticker of every other game,
  each team row tinted with its **real** ESPN `team.color` (with a
  brightness floor so genuinely near-black team colors, which real teams
  do ship, stay visible on the panel's black background — not an invented
  color, the real hue lifted to a visible minimum). Real gaps
  *confirmed-not-assumed*, not guessed: win probability lives in a
  **separate** per-game `summary?event=ID` call, not the bulk scoreboard
  payload; **NHL and EPL's summary endpoints have no win-probability data
  at all**, each checked against a real completed game. The engine shows
  no win% line for those two rather than guessing one. Also: the bare
  scoreboard endpoint with no `dates` param does NOT mean "today" during
  a league's dead period — it jumps to the next scheduled game, which can
  be months out (an NFL game shown in July). Always pass an explicit
  `dates=<today>`; confirmed live this correctly returns zero games for
  an off-season league instead of a bogus future one.
- **news** (`news.py`/`NewsEngine`, 2026-07-30) — source-configurable RSS
  headline ticker, any standard RSS 2.0 feed URL (default: Fox News).
  Headlines only — reads `<item><title>`, never `<description>` or
  `<content:encoded>` even though real feeds carry them. AP's and
  Reuters' commonly-referenced public RSS URLs were tried live and both
  currently redirect to their homepage with nothing parseable — not
  offered as defaults for that reason, documented in `news.py` rather
  than silently omitted.
- **weather** (`weather.py`/`WeatherEngine`, 2026-07-30) — NOAA/NWS
  current conditions + active severe alerts. Reuses `location_config.json`
  (does NOT duplicate a home location). Active alerts **preempt** the
  conditions view rather than taking a turn in a rotation, and pulse;
  severity drives colour so an advisory doesn't cry wolf in tornado red.
  Real NWS behaviours confirmed live, all of them traps: `/points`
  **301-redirects on coordinates with >4 decimal places** and the 301
  body is valid JSON with no usable fields, so it fails *silently*
  (coords are rounded before the request); there is no one-call current
  conditions (walk `/points` → `/gridpoints/.../stations` →
  `/stations/{id}/observations/latest`); **NWS returns metric** (degC,
  km/h) despite being a US agency; fields are frequently null on healthy
  stations (humidity and gust both null on a real clear reading); and
  coverage is US-only, reported honestly as "no NWS coverage".

- **blog** (`blog.py`/`BlogEngine`, 2026-07-30) — **this is a guestbook /
  visitor message board, NOT a blog.** The module and mode are named
  "blog" for historical reasons (it was requested as a blog mode), but the
  data is a public shoutbox: visitors post to the HENDERBURGH site and
  those messages appear on the panel. This is intended behaviour, not a
  bug — confirmed with the owner 2026-07-30. Do not "fix" it into an
  articles feed.
  - Source: the site's **own existing** public endpoint
    `henderburgh.com/api/messages`. No new endpoint was added to
    oura-dashboard; the guestbook logic stays in one place and this is a
    read-only consumer.
  - **There is no title field.** Each entry is `{id, name, text,
    parent_id, timestamp}` — `name` is who posted, `text` is the message
    body. The mode renders name as the heading and text as the body.
  - Replies (`parent_id` set) are filtered out; only top-level messages
    are shown, since "a new message went up" means a new post, not a
    reply to an old one.
  - Entries are **visitor-submitted**, i.e. written by other people, and
    are shown verbatim (uppercased, whitespace-collapsed). Nothing
    bundled or external is ever displayed, so the original
    "no shipped quote/lyric library" copyright intent holds.
  - Presentation is a calm Vestaboard-style idle mode: no scroll, no
    pulse, ~25s dwell — deliberately the quietest mode here. Overlong
    text wraps, then word-truncates, with a hard-split fallback for a
    single unbreakable word.

**`ambient` — the master rotation mode** (`AmbientEngine`). Cycles
flights → ISS → weather → sports → news → blog (guestbook), ~20s each,
skipping any
mode with nothing to show. It **composes real instances** of the other
engines and delegates `tick()/frame()/input()`, so sub-modes look and
behave identically to standalone and any fix lands here free — nothing is
reimplemented. Each engine declares its own **`has_content()`** (lives
with the engine that knows its data shape). **All sub-engines are ticked
every tick, not just the visible one** — load-bearing: `tick()` is what
calls each `FEED.get()`, and an unread feed goes idle and stops polling,
so ticking only the visible one would make every mode come up cold, get
skipped by `has_content()`, and collapse the rotation. It also leaves a
mode immediately if it goes empty mid-dwell.

**Global severe-weather takeover** — an Extreme/Severe NWS alert
preempts **any** mode (game, video, mirror, anything), not just weather.
Implemented in `arcade_server._severe_alert_frame()` applied *after* all
compositing in the render loop. Styling comes from module-level
`draw_alert_frame` + `ALERT_SEVERITY_COLOR`, which `WeatherEngine` also
delegates to, so the two can't drift apart. Only Extreme/Severe qualify
(`GLOBAL_ALERT_SEVERITIES`) — a routine advisory interrupting a game
would train someone to ignore the panel when it finally matters; weather
mode itself still shows every severity. Clears automatically on the next
tick when NWS drops the alert. **Deliberate cost:** this reads
`weather.FEED` every render tick, so weather's poll thread never idles
out (a takeover that only worked while already viewing weather would be
pointless).

**Units are imperial everywhere**, converted at the **render layer**
(`km_to_mi`, `kmh_to_mph`, `kt_to_mph`, `nm_to_mi`, `c_to_f` in
`engines.py`) — feed modules still report whatever their upstream API
actually returns, so the I/O layer stays a faithful mirror of the source
and "what units to display" stays a rendering decision. Note ADS-B is
natively feet (left alone) but knots and *nautical* miles (converted).

**Shared visual system for data modes** — `draw_header`,
`draw_text_centered`, `text_w`, `fit_text`, `draw_divider`, `draw_dots`,
`draw_marquee`, `color_on_dark` in `engines.py`. All six data modes use
the same accent-rule header + title + right tag + stale pip, so they read
as one product; only the accent colour changes per mode. **Use `text_w()`
rather than re-inlining `4*len(s)-1`** — that duplication (~40 sites) is
how a scale=2 string silently overflowed. Games deliberately do NOT use
this system; they have their own full-bleed visual language.

**Other modes**: `mirror` (screen capture -> panel, needs mss+Pillow),
`video`/`stream` naming (see Known issues — `stream.py` is currently
orphaned), `cast` (phone browser -> panel), WLED ambient backgrounds,
`menu` (the panel's own home-screen/game-picker, drawn on the matrix
itself — the phone is just a dpad+buttons controller for it), `boot`
(curtain-parting logo intro).

## Polling load (checked 2026-07-30, no action needed yet)

Every feed only polls while its mode has been read in the last
`IDLE_STOP` (120s), and only one mode renders to the panel at a time — so
steady-state load is whichever single mode is currently selected, with a
brief (≤120s) overlap right after switching modes while the previous
feed's thread hasn't idled out yet. Real per-mode request rates:

- ticker: 1 batched call/60s (crypto) + 1 call per stock symbol/60s.
- satellite: 1 call/12s (position) + 1 call/900s (pass prediction).
- flights: 1 call/15s (position) + up to 4 adsbdb lookups/refresh, capped.
- sports: **7 ESPN calls/20s** (one per configured league) ≈ 0.35 req/s
  while active, plus 1 more call/20s only when the pinned favorite's game
  is actually live (win probability).
- news: 1 call/300s.
- weather: 1 obs call/600s + 1 alerts call/120s (gridpoint cached ~daily).
- audio_sync: zero request cost — a blocking UDP socket, not polling.

- blog: 1 call/300s.

None of this is a real load on the Mac (all network-I/O-bound, sleeping
threads).

**Two changes on 2026-07-30 broke the "only one mode polls at a time"
assumption above — read this before assuming the per-mode numbers are the
whole story:**

1. **`ambient` mode ticks every sub-engine every tick**, so in ambient
   *all six* feeds poll concurrently and continuously, not just the
   visible one. That is required for the rotation to work (see the
   ambient entry above), but it means ambient's real cost is the **sum**
   of flights + satellite + weather + sports + news + blog.
2. **The global severe-weather takeover reads `weather.FEED` on every
   render tick from every mode**, so weather now polls continuously and
   forever, in every mode including `off`-adjacent ones.

**The practical consequence, stated plainly:** ambient is *designed* to
be left running for hours as a "wall of information" — which is exactly
the long-running always-on ESPN scenario previously flagged here as
untested. Sports contributes ~7 undocumented-API calls/20s (~0.35 req/s,
~30k/day) for as long as ambient is up. That has **not** been run long
enough to know whether ESPN throttles or blocks it. Nothing has failed so
far, and no rate limit is documented, so this is a genuine unknown rather
than a known problem — but if sports starts erroring during long ambient
sessions, this is the first place to look, and the cheap fix is a longer
`SCOREBOARD_REFRESH` or skipping leagues with no games today (their
scoreboards are already known-empty from the `dates=` result).

## Known issues / in-progress work

### ⚠ PANEL IS OFFLINE as of end of session 2026-07-30 — likely needs a power cycle

The Apollo M-1 at `192.168.40.24` stopped responding to ping and HTTP
during the audit and had not recovered by the end of the session. This
Mac's own LAN networking is fine (`192.168.40.203` responds), so it is
the panel, not the network. The arcade service is healthy and set to
`off`; it will drive the panel again as soon as the panel is back.

**Most likely cause, and it was my (Claude's) doing:** several audit
scripts did `import arcade_server`, which constructs the module-level
`ARCADE` singleton — a full `Arcade` with its own render thread and its
own `WledDDP` sender. Those ran *concurrently with the launchd service*,
so two or more independent DDP streams hit the panel at once. This file
and `arcade_server.py` both warn that flooding WLED locks the panel hard
enough to need a physical power cycle; that is the documented failure
mode and it matches exactly what happened.

**Recovery:** physically power-cycle the panel (unplug/replug USB-C).

**Rule for future work — this is the actionable part:** never
`import arcade_server` from a test/audit script while the service is
running. Test engines directly (`import engines`, instantiate the engine
class, call `tick()`/`frame()`), which needs no panel and no second
render loop. If an end-to-end test genuinely needs the render loop, stop
the service first (`launchctl bootout gui/$(id -u)/com.henderburgh.arcade`)
so only one DDP sender exists.

### ⚠ ESPN rate exposure if `ambient` runs 24/7 — READ THIS FIRST

**If `ambient` mode is left running around the clock, sports mode alone
issues roughly 17,000 requests per day to ESPN's undocumented, unofficial
API at the current config — and ~30,000/day if all 7 supported leagues
are enabled. ESPN publishes no rate limit and offers no support channel.**

The arithmetic, so it can be checked rather than trusted: `ambient` ticks
every sub-engine on every tick (required — see the ambient entry above),
so sports polls continuously whenever ambient is up. `SCOREBOARD_REFRESH`
is 20s and it makes **one call per configured league per refresh**:

| leagues enabled | calls/day |
|---|---|
| 4 (`DEFAULT_LEAGUES`, and what `sports_config.json` holds today) | **~17,280** |
| 7 (all of `LEAGUE_PATHS` — EPL/NCAAF/NCAAB added) | **~30,240** |

A live pinned game adds one more call per 20s on top. Note the volume
scales linearly with league count, so enabling the three college/soccer
leagues nearly doubles it.

Nothing has failed yet and no limit is documented, so this is a genuine
unknown rather than an observed problem — it has never actually been run
for a full day. **Treat it as untested, not as safe.** Failure would most
likely appear as sports mode erroring or emptying during long ambient
sessions (and, because `has_content()` would then return False, silently
vanishing from the rotation rather than showing an error).

Two cheap mitigations exist and neither is implemented, deliberately —
behaviour was left alone by request on 2026-07-30:
  1. Raise `SCOREBOARD_REFRESH` (60s cuts it to ~10k/day with no
     meaningful loss — scores do not change faster than that).
  2. Skip leagues already known to have no games today. The `dates=`
     response for an off-season league is empty, so those calls are
     provably wasted; on 2026-07-30 only MLB had games (NFL/NBA/NHL all
     off-season), so 3 of the 4 configured leagues were polled every 20s
     purely to be told "no games" — that alone is ~75% of current volume.

This is also the single biggest open risk for the production device,
where it multiplies by unit count — see `PRODUCTION.md`.

**Home Assistant notification pass-through — NOT BUILT, blocked on auth
(2026-07-30).** Requested (doorbell / package / presence events flashing
on the panel). Deliberately **not started**, because the premise that
"the HA client/auth already works" does not hold right now:

- HA is up: `http://192.168.40.203:8123/` returns 200.
- The `HA_TOKEN` in `~/oura-dashboard/.env` is **rejected with 401** on
  every endpoint tried (`/api/`, `/api/states`, `/api/config`). It is a
  183-char JWT, so it's a real token shape — expired or revoked, not
  malformed. `.env.example` holds only a placeholder; no other token
  exists on disk.
- Without `/api/states` there is **no way to see what entities this setup
  actually exposes**, and the request explicitly said to check that.
  Writing a module against guessed entity IDs
  (`binary_sensor.doorbell`, `device_tracker.*`, …) would be inventing
  the integration — the exact thing this project's rules forbid — and it
  would fail silently on a setup whose entities are named differently.

**To unblock:** create a fresh long-lived access token in HA (profile →
Security → Long-lived access tokens) and put it in `~/oura-dashboard/.env`
as `HA_TOKEN=`. Then `GET /api/states` enumerates real entities and this
can be built against what's genuinely there. The panel-side half (a
notification flash/scroll overlay) is straightforward once the event
source is real; the render-loop takeover added for severe weather is the
obvious pattern to reuse for it.

**Audio-reactive visualizer — paused, waiting on the user (as of
2026-07-30).** Plan: WLED-MM's real AudioReactive usermod (the panel has
a physical Rev 6 mic add-on) broadcasts analyzed audio (volume + 16-band
FFT) over UDP multicast (239.0.0.1:11988, "V2" packet format, confirmed
against the real WLED-MM firmware source — see `audio_sync.py`'s
docstring for the exact byte layout) so the arcade service can build a
real EQ/VU visualizer without capturing or fabricating any audio itself
— explicit standing rule from the user: never fake a waveform, same
"never invent what an effect looks like" principle as `backgrounds.py`.

`audio_sync.py` (the UDP listener, pure I/O, same FEED pattern as every
other data module) is written and tested — it correctly listens and
correctly reports zero packets when none arrive, no fabricated fallback.
Blocked on: the panel's mic needed real config fixes (sync mode → Send,
mic type → Generic I2S, a genuine physical power-cycle, not just a
software reboot) which are now all done and confirmed via the panel's own
`/json/si` status endpoint (`Audio Source: I2S digital - quiet`, `Sound
Processing: running`). Despite that, **no UDP packets are reaching the
Mac** even though the panel's own status shows it actively transmitting.
Ruled out: firewall (both relevant Python binaries are `Permitted`),
wrong network interface/multicast route (verified correct via
`route get`), wrong packet format (struct layout verified against real
firmware source, `struct.calcsize` matches `sizeof()` exactly).
**Leading suspect: IGMP snooping on the router/AP silently dropping
multicast traffic** — a well-documented WLED Sound Sync gotcha on
consumer/mesh routers, and not something checkable from either device;
needs the user to check the router's admin UI. Do not resume building
the visualizer engine until real varying packet data is confirmed
flowing — that was an explicit, repeated instruction this session.

**`stream.py` is orphaned** — nothing in the current codebase imports it
(confirmed via grep). Needs a decision: wire it in, or delete it. User
explicitly deferred this to "Thursday or on reset" — not a "do it now"
item, don't touch without being asked.

**Frontend audit (`arcade.html`, `remote.html`) — also explicitly
deferred** by the user alongside the `stream.py` decision, same "Thursday
or on reset" condition.

## Self-audit 2026-07-30 — what was checked, fixed, and what still feels risky

A deliberate logic-level audit (not just rendered output) was run across
every engine. Five real bugs were found and fixed, each committed
separately. Recording the *method* as much as the results, because the
methods are what found things that code review had missed repeatedly.

### Fixed
1. **Ticker showed a corrupted, sometimes wrong-but-real symbol.**
   `row["sym"][:4]` while 7 chars fit at scale=2 — `MATIC`→`MATI`,
   `GOOGL`→`GOOG` (a *different real ticker*). The scrolling tape below
   showed the full symbol, so one frame displayed two different tickers
   for the same row.
2. **ISS skipped by ambient when only one of its two APIs was up.**
   `has_content()` required a position fix, but PASS and LIVE come from
   two independent APIs that fail separately, and `_refresh_pass`
   swallows its exceptions. Also made view-cycling skip a view whose
   source is down instead of dwelling on a placeholder. Polluxlabs was
   genuinely down at the time, so this was live, not hypothetical.
3. **Sports dwelt on an empty PINNED view** when a favorite was set but
   had no game that day, while real games sat one view away.
4. **Render loop froze for seconds whenever the panel went offline.**
   The DDP socket had no timeout; `sendto()` to an unreachable
   *local-subnet* address blocks on ARP resolution. Every mode froze on
   one frame with no exception and `loop_errors` stuck at 0. One-line
   fix (`settimeout(0.25)`); `socket.timeout` is an `OSError` subclass so
   the existing handler already did the right thing.
5. **`@` was silently dropped from the sports tape** — fifth instance of
   the uppercase-only-font bug class. `"AWAY 3 @ HOME 5"` rendered as
   `"AWAY 3  HOME 5"`, losing the home/away distinction.

### Methods worth reusing (each found something review didn't)
- **Instrument `draw_text3x5` itself** to log any character absent from
  `_FONT3x5`, then drive every mode against live data and every internal
  view/index. This is the only reliable way to catch the recurring glyph
  bug — it is invisible in the code *and* easy to miss on the panel. That
  sweep now reports **zero** dropped characters across all modes.
- **`faulthandler.dump_traceback()` on a stalled process.** Bug 4 was
  first mis-bisected to a recent change because the panel's reachability
  fluctuated between runs; the stack dump identified the real cause
  immediately. Prefer it over bisection when something *hangs* rather
  than errors.
- **Drive engines with only states the FEEDs can actually produce.** An
  earlier harness fed `data=None` and string-typed fields and produced a
  wall of failures that were all fiction — feeds always return a
  well-typed dict. Guarding against unreachable states would have added
  noise, not safety. All seven modes are clean against every *reachable*
  partial state (empty lists, null optional fields, one-of-two-APIs-down).

### Also verified clean (no action taken)
- **Cursor safety when a feed's list shrinks under a running engine** —
  a real event (planes leave, games end, headlines roll off). Every
  engine both normalises its cursor in `tick()` *and* indexes with
  `% len()` in `frame()`, so a stale cursor can neither `IndexError` nor
  show the wrong item. Shrink-to-one and shrink-to-empty were both
  exercised on sports, flights, news, blog and weather. Note the belt
  *and* braces matter: an early version of this test called `frame()`
  without `tick()` and appeared to find a bug — the real loop always
  ticks first, so that was a test artifact.
- **Every reachable partial-data state across all seven modes** — empty
  lists, null optional fields (real ADS-B and NWS both emit these), one
  of two APIs down, pregame null scores, missing ESPN team colours,
  posts with empty name or body. No crashes, no overflow.
- **`has_content()` now implemented on all seven data engines**,
  including ticker (which ambient does not use yet — see the commit; it
  would have been an AttributeError waiting to happen).

### Checked and believed correct, but worth a second opinion
- **`has_content()` on every engine** was verified against real feed key
  names (a wrong key would silently drop a mode from `ambient` forever).
  All correct. The subtler risk remains the class fixed in bugs 2 and 3:
  *an engine with multiple views whose `has_content()` only reflects
  one*. Flights, news and blog are single-view so they're safe today,
  but any future multi-view mode inherits this trap.
- **Ambient ticking all six sub-engines every tick** is deliberate and
  documented, but it means ambient's cost is the sum of all feeds and it
  is the reason for the ESPN exposure below. Worth confirming the
  trade-off is still wanted.
- **The `changed`/`released` half of the alert-takeover audit is timing
  sensitive.** A cold mode draws a near-static LOADING screen, so
  sampling too soon after `set_mode` makes a healthy render look frozen —
  this produced false failures twice. Any future test needs a generous
  settle or a poll-until-changed loop.
- **`fit_text` truncation is silent by design.** It degrades gracefully,
  but nothing warns when a string is being shortened, so an
  over-long label can quietly lose meaning (the empty-ambient message
  `"WAITING FOR DATA"` → `"WAITING FOR"` was caught only by looking at
  the pixels). A debug mode that logs every truncation might be worth it.
- **Scrolling marquees intentionally draw characters partially
  off-panel** at both edges. The bounds checker flags these; they are
  correct. Don't "fix" them.

## Working conventions worth knowing before touching this codebase

- **Verify by rendering, not by reading.** Every mode in this project has
  had at least one real bug that a code read would not catch (missing
  font glyphs, inverted arrows, mixed-case text silently vanishing,
  vertically-overlapping text at scale=2). The established discipline is:
  render an actual frame with real data, save it as a PNG, and look at
  it — every new mode and every nontrivial layout change should go
  through this before being called done.
- **Never invent a number or a pixel.** This shows up as: feeds serving
  stale-but-honest cached data instead of fabricating current data;
  `backgrounds.py` refusing to synthesize WLED effects in software;
  the audio-sync work refusing to fake a waveform. If real data isn't
  available, say so on-screen (a stale-dot, a "NO DATA" message) rather
  than approximate it.
- **Config-driven, not hardcoded**, for anything owner-specific: symbols,
  home location, sports leagues/favorite team. Every one of these follows
  the same shape — a JSON file next to the module, `load_*()`/`save_*()`
  functions with safe defaults on first run, and matching GET/POST
  endpoints in `arcade_server.py` so the control panel and phone remote
  can change it without a code edit or restart.
- **One feature per commit, matching an established pattern where one
  exists.** Recent history: each new data mode ("Flights tracker: new
  mode, same pattern as ticker/satellite") explicitly calls out which
  prior mode it copied the shape from.
- The user runs this session-by-session and steps away for days at a
  time; leave in-progress work in a clearly-described, non-broken state
  (as above) rather than mid-refactor.
