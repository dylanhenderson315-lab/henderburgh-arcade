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
runs classic games, live data "modes" (clock/dashboard, stock ticker,
ISS tracker, flight tracker, sports scoreboard, news headline ticker,
weather + severe alerts, site guestbook, and an `ambient` rotation tying
them together),
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

- **clock** (`ClockEngine`, 2026-07-31) — **the panel's resting state and
  `DEFAULT_MODE`.** Time (12-hour hero, blinking colon), date, current
  temperature, next ISS pass countdown. Composed from `weather.FEED` and
  `satellite.FEED`; **no `clock.py`** because it has no I/O to isolate
  (`time.localtime()` is a local call) — an empty feed module purely to
  match the pattern would be cargo-cult. Degrades one field at a time and
  the clock itself never depends on a feed.

  **How the three "idle" concepts relate — decided 2026-07-31:**
  - `clock` = resting state. What's on when nothing was chosen.
  - `off` = explicit release to WLED/Home Assistant lighting. **Kept, not
    replaced** — the panel doubles as a house lamp and that handoff is a
    real capability.
  - `ambient` = an active choice to watch live data cycle. **Clock is not
    in `AmbientEngine.SEQUENCE`**: its `has_content()` is always true, so
    it could never be skipped and would eat a dwell slot every lap,
    displacing the data ambient exists to show. It *is* ambient's
    empty-state fallback (a clock beats a "NO DATA YET" screen).

  Both midnight and noon are handled via `strftime("%I")`, not arithmetic:
  `hour % 12` yields 0 for *both* 00:00 and 12:00, which is the classic
  bug. All 10 edge times are covered by the verification described in the
  self-audit section.

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

**Universal scroll control** (`Scroller` + `Browsable` in `engines.py`,
2026-07-31) — **system-wide convention; new sequence modes must follow
it.** Every competitor in this category is push-only; this is what lets
someone actually browse.

    tap left/right   -> step exactly one item, immediately
    hold left/right  -> step once, ~0.35s delay, then ACCELERATING repeat
    release          -> auto-advance stays paused ~4s

Standard key-repeat model (as in holding an arrow key in a text field),
so it needs no instructions. Uses the press/release the phone remote and
control page **already** send (`bindHold` in `remote.html`) — no new
hardware, no new gesture.

**To opt a new mode in:** subclass `Browsable`, call `_init_scroll()` in
`reset()`, implement `_step(direction)`, call `_scroll_tick()` at the top
of `tick()`, and gate auto-advance on `self.browse.auto_ok`. That is the
whole contract.

Two traps, both hit once already:
- **Do not name the scroller `self.scroll`** — `NewsEngine`/`TickerEngine`
  already use that for a marquee pixel offset (a float). It is `self.browse`.
- `bindHold` **also** fires a repeating `input()` every ~110ms while held.
  `input()` is therefore ignored while a press is active, making the
  server tick clock the single source of repeat timing (and the
  acceleration curve independent of client repeat rate).

Applied to ticker, flights, sports, news, blog **and ambient itself**,
where left/right browses the rotation one level up. Verified live: news
counter went `1/15` → tap → `2/15` → 2.5s hold → `9/15`.

**Ambient is a presentation layer, not a rerun** (2026-07-31). Six
"channel idents" sharing one geometry via `draw_ident()`: kicker / hero /
sub. Everything that only matters while *operating* a mode is stripped —
no dots, counters, cursors or nav hints. Each mode supplies
`ambient_frame()` (headline-first), `AMBIENT_STYLE` (its characteristic
entrance, reusing `transitions`), `AMBIENT_ACCENT` (its band colour) and
`ambient_weight()` (drives dwell). Ident accents are **checked pairwise
for distinctness** — two modes sharing a colour defeats the whole
"know it before you read it" idea. Dwell is weighted (live game ~45s,
guestbook ~16s) and clamped to 10–45s.

**Motion system** (`transitions.py`, 2026-07-31) — one shared transition
for the whole product, applied centrally in the render loop so all nine
modes get the same motion rather than each inventing its own.

- **Default is `push_up`** (eased slide). Picked after rendering all
  styles side by side: `fade` has a dead black frame mid-transition;
  `push_up` is continuous motion *and* the cheapest.
- **Cost is the hard constraint.** Everything is byte slicing/concat or
  the cached translate table from `brightness.py` — all C-level passes,
  **no per-pixel Python anywhere**. Measured against a 41.7ms frame
  budget: `push_up` 0.001ms, `fade` 0.005ms, `wipe_right` 0.018ms.
- **There is deliberately no true A-over-B cross-fade.** It needs
  per-pixel arithmetic on two 12KB sources every frame — the one thing
  this module exists to avoid. `fade` goes through black instead. Don't
  "improve" it into a real cross-fade.
- Applied at three levels: `set_mode` (all modes), `AmbientEngine`
  between sub-modes (it advances internally, bypassing `set_mode`), and
  `SportsEngine` between PINNED/TICKER.
- **Two deliberate exemptions:** a severe-alert takeover is *not*
  transitioned (it's an interrupt; easing it in softens the moment meant
  to feel abrupt), and coming from `off` plays the first-light ramp
  instead of a slide (nothing meaningful to slide away from).
- **First light** (`transitions.wake`): the panel blooms up out of black
  over ~0.75s on takeover/startup rather than snapping on fully lit.
  Measured ramp: 0 → 11 → 27 → 68 → 106 → 148 → 164.

**`Pulse`** (`engines.py`) — the shared "something just changed" flash.
Sports' scoring flash set the bar; this generalises it so news (new
headline), blog (new post), flights (nearest aircraft changed) and
weather (temperature changed, rounded first so float jitter can't fire
it) all mark new content identically. Never flashes on the **first**
value seen — otherwise every mode flashes on arrival and you learn to
ignore it. Blinks rather than holding solid: a static highlight reads as
a colour choice, a blinking one reads as an event.

**Team colour disambiguation** (`sports._disambiguate_colors`) — measured
across all seven leagues, **5 of 19 real games** had primary team colours
close enough to render as one colour (NFL SEA vs NE were byte-identical,
both navies lifted to the same value by the brightness floor). Falls back
to ESPN's own `alternateColor`, never an invented colour; if nothing
separates, primaries are left alone since the abbreviations still
disambiguate. Re-measured after: 0 of 19.

**Time-based night dimming** (`brightness.py`, 2026-07-31) — applied in
the render loop to whatever is about to be sent, so it covers **every**
mode uniformly (a game at 3am dims exactly like the clock). Necessary
because the panel now rests on the clock and is therefore lit 24h.

- **At the render layer, not WLED's `bri`** — renderer-agnostic (the
  future Bonnet path gets it free), no network round-trip, deterministic
  and testable without hardware. Same reasoning as unit conversion.
- **No ambient light sensor on this rig.** PRODUCTION.md assumes one for
  the sellable device; the Apollo M-1 exposes none. When a sensor exists,
  only `level_at()` changes — everything above takes a 0..1 scalar.
- **Smooth fade** (default 30 min), because it cost almost nothing: one
  lerp, plus a cached 256-byte table applied with `bytes.translate()`,
  which runs in C over the whole 12KB frame. `fade_minutes: 0` gives an
  instant switch.
- **Severe alerts are exempt** — the global takeover exists to grab
  attention, and a tornado warning dimmed to 28% at 3am defeats it at
  exactly the hour it matters. Verified: 51.8 vs 168.3 mean luminance.
- Config via `/api/brightness` (`enabled`, `day_brightness`,
  `night_brightness`, `night_start`, `night_end`, `fade_minutes`).
  Malformed values fall back to defaults rather than raising.
- **The midnight wrap is the trap here**: a naive `start <= t < end` is
  false for *every* minute of a 22:00→07:00 window. All arithmetic is
  "minutes since night began, modulo 1440", so wrapping needs no special
  case. Covered by tests along with fade=0, disabled, `start == end`,
  malformed times, fade longer than the window, and non-wrapping windows.

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

### Panel lockup hazard — RESOLVED 2026-07-30, but the rule stands

The Apollo M-1 stopped responding to ping and HTTP during the audit on
2026-07-30. It was power-cycled and came back healthy (uptime reset,
RSSI -55, service streaming with 0 send errors). **No action outstanding.**

Cause, recorded because the rule it produced still matters: several audit
scripts did `import arcade_server`, which constructs the module-level
`ARCADE` singleton — a full `Arcade` with its own render thread and its
own `WledDDP` sender. Those ran *concurrently with the launchd service*,
so two or more independent DDP streams hit the panel at once. Both this
file and `arcade_server.py` warn that flooding WLED locks the panel hard
enough to need a physical power cycle; that is exactly what happened.

**Rule: never `import arcade_server` from a test/audit script while the
service is running.** Test engines directly (`import engines`,
instantiate the engine class, call `tick()`/`frame()`) — that needs no
panel and no second render loop, and it is how every verification in this
project should be done. If an end-to-end test genuinely needs the render
loop, stop the service first
(`launchctl bootout gui/$(id -u)/com.henderburgh.arcade`) so exactly one
DDP sender exists.

Related and already fixed: the render loop used to freeze for seconds
whenever the panel went away (no DDP socket timeout) — see the self-audit
section below.

### ESPN request volume — MITIGATED 2026-07-31 (was the top open risk)

`ambient` is designed to run for hours, which made a flat 20s poll per
league ~17,280 requests/day against an **undocumented, unofficial API
with no published rate limit, no terms covering this use, and no support
channel**. Now mitigated in `sports.py`:

- Poll interval is derived per league from what it last returned:
  **live game → 20s** (unchanged, this is where it matters), **games
  today but none live → 300s**, **no games at all → 1800s**.
- **Exponential backoff on failure** (30s doubling to a 600s cap, reset
  on success). Retrying a throttled endpoint every 20s forever is exactly
  what earns a block from an API you cannot ask permission from.

Measured against real leagues, not estimated: on 2026-07-31 NFL/NBA/NHL
were off-season (EMPTY tier) while MLB had a live game (LIVE tier) →
**17,280 → 4,464 req/day, a 3.9x cut with no loss of responsiveness on
the league actually playing.** Volume still scales with league count, so
enabling all 7 raises it.

**Accepted tradeoff:** a game kicking off is noticed up to 5 minutes
late, after which that league flips to the LIVE tier and updates every
20s.

**Still true and still unverified:** nothing has been run for a full
24h, and ESPN's actual tolerance remains unknown. If sports starts
erroring or emptying during long ambient sessions this is still the first
place to look — but it now backs off instead of hammering. For the
production device this multiplies by unit count; see `PRODUCTION.md`.

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
