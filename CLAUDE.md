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
runs 14 classic games, live data "modes" (clock/dashboard, stock ticker,
ISS tracker, flight tracker, sports scoreboard, news headline ticker,
weather + severe alerts, site guestbook, and an `ambient` rotation tying
them together), a `gameday` event-takeover mode,
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
that set minus `menu`/`boot`. **Adding a new mode means all of:** write the
engine class, add it to `ENGINES`, add it to `MenuEngine.NATIVE_GAMES`
(label + accent color) and a small icon case in `MenuEngine._icon` for the
panel's own menu grid, **and add a button to `arcade.html`** (plus a config
card if it has config). That last step was missed for seven consecutive
modes — see the web-surfaces section for the cross-check that catches it.

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

**ALL external display text goes through `paneltext.panel_text()`** — see
that module's docstring for the full nine-instance tally of this bug. The
fold used to live privately inside `mma.py`, which is exactly how
`sports.py`'s universal feed reintroduced it later (a live PGA leader,
"Hojgaard", rendered as "HJGAARD"). **A per-module fold is not a fix; it
is a fix waiting to be missed by the next module.** Unsupported characters
become a space rather than vanishing, so a loss is visible.

**The 3×5 font (`_FONT3x5`/`draw_text3x5` in `engines.py`) is
uppercase-only** (54 glyphs: `A-Z`, `0-9`, space and ``!$%&'()+,-./:<>?@``)
and **silently drops any character it doesn't have** — no error, no crash,
just a quietly wrong string that still looks plausible.

**`.upper()` IS NOT ENOUGH AND NEVER WAS.** That was the guidance here for
a long time and it is wrong: `.upper()` happily preserves curly quotes,
en-dashes, and accented letters, all of which the font then drops. Use
**`paneltext.panel_text()`** at the feed module's I/O boundary — it folds
diacritics, maps the letters that don't decompose (Ł/Ø/Đ), uppercases, and
turns anything still unsupported into a **space** so a loss is visible
rather than silent.

As of 2026-08-01 every feed that renders external text uses it: `sports`
(**both** the universal and the per-league parser — the per-league one was
missed on the first migration), `mma`, `news`, `blog`, `flights`,
`weather`. (`market.py` deliberately does not — its `.upper()` calls are on
user-typed ticker symbols, not API text, and folding them could corrupt a
symbol. `sports.py`'s remaining `.upper()` calls are on config values
matched against `LEAGUE_PATHS` keys, which must NOT be folded.)

**Enforced by `fold_audit.py`** — do not rely on reading the code for this;
it has been read and pronounced complete twice while still being wrong.

**Run `render_audit.py` instead of trusting a code read** — see below.

## What's built

**Games** (all native, headless, stdlib-only): snake, tetris, pong,
breakout, tron, flappy, invaders, life, dodge, 2048, tunnel, powder,
brawler, chase. Plus a TIC-80 fantasy-console cartridge loader
(`tic80_core.py`, `TicCartEngine`) that scans `carts/tic80/*.tic` and
appends them to the menu automatically.

**Data modes**, in build order:
- **ticker** (`market.py`/`TickerEngine`) — crypto (CoinGecko) + stocks
  (Yahoo Finance v8 chart), config-driven watchlist.
- **satellite** (`satellite.py` + `skypass.py` / `SatelliteEngine`) —
  **UNIFIED 2026-08-02**, see the dedicated section below for the full
  before/after. Was three views (ISS PASS / ISS LIVE / SKY) sharing an
  engine but not a design; now two (UPCOMING / OVERHEAD-NOW) over ONE
  pass list that includes the ISS as an entry. Owns
  `location_config.json`, the project's one source of truth for the
  owner's home coordinates — reused by flights, don't duplicate it.
- **flights** (`flights.py`/`FlightEngine`) — nearby ADS-B traffic
  (adsb.lol) + route/airline enrichment (adsbdb), reuses satellite's
  location config. Heading-oriented plane icon (procedurally rotated from
  real `track_deg` via `draw_line`, not a fixed sprite table — deliberately
  not a real airline logo, no IP exposure), color-coded by altitude band
  (chosen over distance-band coloring: distance is already shown as text,
  altitude wasn't color-coded anywhere, and altitude is the more standard
  "what kind of traffic is this" signal on real ATC displays).

  **Flight phase — CLIMB / DESCEND / CRUISE** (`flights._phase()`,
  2026-08-02). Verified against real ORD traffic (MYR had zero aircraft
  in range at build time; ORD confirmed the payload shape and value
  ranges, the classification applies wherever aircraft are tracked).
  - `baro_rate`/`geom_rate`: **never both populated on the same real
    aircraft** — one carries a value, the other is null. `baro_rate`
    preferred, `geom_rate` the fallback; neither present → phase is
    `None`, not guessed.
  - **Threshold is ±300fpm, not "any nonzero value"** — a real level
    aircraft rarely reports exactly 0 (one showed -64fpm while
    unambiguously not descending). Real sustained climb/descent observed
    at 1500–3700fpm. Level-below-cruise stays `None`: genuinely
    ambiguous (pattern? holding? transition?).
  - **Cruise floor is 18,000ft — the real FAA Class A airspace floor**,
    not invented. At or above it, altitude alone settles CRUISE
    regardless of rate.
  - **Feeds notability narrowly, not as a new tier**: LOW (already
    alt≤3000ft + dist≤12nm) escalates from rank 2 to rank 3 — same as a
    heavy or helicopter — only when phase confirms active climb/descend,
    not merely coincidental lowness. Phase does **not** get its own
    ranking or reorder the list on its own; near any 40nm radius enough
    aircraft are always transitioning that using phase as a primary sort
    key would make ordering noisy rather than useful.
  - Visual: `draw_trend_arrow` (promoted to module level, shared with
    baseball's half-inning indicator — one arrow convention, not two) at
    x=3 in the icon's vertical band, the one horizontal margin that stays
    clear of the plane icon's rotation footprint at every heading.
    Nothing drawn for CRUISE or `None` — same "no badge for the mundane
    case" rule the notable tag already follows.
  - **Partial live check 2026-08-02**: one real aircraft (VIR74W,
    39,000ft) appeared near MYR at session start — correctly classified
    CRUISE (altitude alone, per the FAA floor rule) with no arrow drawn,
    confirmed on the real panel (score=1, zero errors). Still an honest
    gap: no CLIMB or DESCEND case has occurred at MYR itself yet, so the
    arrow's real-panel appearance remains verified only against captured
    ORD data, not local traffic. The LOW+phase escalation is likewise
    still unverified live. Check again next session.
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

**`gameday` — the EVENT / TAKEOVER mode** (`GameDayEngine` + `mma.py` +
`gameday_config.json`, 2026-08-01). **A different CATEGORY of mode, and new
modes should pick a side deliberately.** Every data mode above is a GLANCE
mode: it shares the panel, gets a slice of attention, and ambient strips it
to one fact. GAME DAY assumes the opposite — opt-in, one event, nothing else
competing — so it is allowed to be maximally detailed and dramatic. It is
**not** in `AmbientEngine.SEQUENCE` (a rotation cannot contain a takeover;
its `has_content()` returns False), and it **hands the panel back on its
own** when the event ends, reusing the existing `.launch` hand-off that
`BootEngine`/`MenuEngine` already use — no new mechanism in the render loop.
The severe-weather takeover still outranks it, for free, since that
composites afterwards.

Two targets via `gameday_config.json`: `ufc` (next/current card from
`mma.FEED`) or `team` (the pinned favourite from `sports.FEED`, with the
frame escalating on how CLOSE **and** how LATE the game is — multiplied, not
averaged, and only while genuinely live).

**`mma.py` — everything verified against real payloads, not assumed:**
- **Structure is NOT like the team sports.** One `event` = the whole CARD;
  `competitions[]` = the individual FIGHTS, **prelims first, main event
  last**. A 5-round fight is how a main event is identified — there is no
  `isMainEvent` flag.
- **One request returns the entire card**, so a live poll is one call per
  20s total, not one per fight. This matters given the ESPN volume risk.
- **There is NO `method` field.** Finish method is only recoverable from the
  play-by-play `details` list, keyed on **stable type IDs** (20 submission,
  21 KO/TKO, 22 decision) because ESPN's own text for 21 is the mangled
  token `"Kotko"`. Returns `None` rather than guessing when absent.
- **`displayClock` is time ELAPSED in the final round**, not remaining — the
  payload proves it, since every decision reads exactly `5:00` at its final
  scheduled round.
- **There is no working MMA summary endpoint** (`/mma/ufc/summary` 404s).
- `?dates=YYYYMMDD` only works for a date a card actually exists on, and card
  dates are **UTC** (a Saturday-night US card is often the next UTC day).
  The league `calendar` list is the authoritative schedule; its `$ref` links
  point at `sports.core.api.espn.pvt`, which does not resolve.
- **Fight statistics** (sig. strikes, takedowns, control time) live on a
  different host, `sports.core.api.espn.com`, as a **per-fighter**
  sub-resource. That is **two calls with no batched form**, so they are
  fetched ONLY for the fight currently on screen and only once it is live or
  finished — never for the whole card. Same discipline as sports.py's win
  probability.
- **This is the highest-risk feed in the project for the glyph bug.** One
  real card contained Medić, Spasić, Milošević, Rębecki, Čepo and Todorović.
  Untreated, "MEDIĆ" renders as "MEDI" — a wrong name, silently.
  `mma.panel_text()` folds diacritics (NFKD + an explicit map for the
  letters that don't decompose, like Ł/Ø/Đ) at the I/O boundary.

Views rotate UPCOMING → STATS → CARD (drill-down, then pull back). A finish
**preempts everything** and holds ~22s. Results only fire for fights that
finish **while watching** — loading a card already 9 fights deep must not
replay nine finishes (same first-value rule as `Pulse`).

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

**Satellite modes — UNIFIED 2026-08-02** (`satellite.py` + `skypass.py` /
`SatelliteEngine`). Was three views built over several sessions (ISS PASS,
ISS LIVE, SKY — SKY added 2026-08-01 as a third, additive view alongside
the two ISS ones). **They shared an engine but not a design**, and were
collapsed into one coherent system. Full reasoning below; this is the
current, correct picture — anything describing three satellite views or
an "ISS-only" pass predictor is stale.

**Why unify rather than level one up to the other.** SKY's live-pass arc
(real rise/peak azimuth, real progress along the pass) was already
better, more honest code than ISS LIVE's decorative orbit ring — the ring
was never a real ground-track projection. But ISS PASS's chip-style
urgency treatment ("GO OUTSIDE" filled in colour, reading before any text
resolves) was better than SKY's plain coloured text. Effort had landed
unevenly across a 2×2 grid (ISS/SKY × waiting/live), not split cleanly
between "ISS gets two views" and "everything else gets one" — so leveling
either system up to the other would have been wrong in half the cases.

**Two states now: UPCOMING and OVERHEAD-NOW.**

- **ONE list, `skypass.FEED`, which already includes the ISS.** It is
  genuinely one of the ~157 naked-eye objects in CelesTrak's `visual`
  catalogue, so it needed no special injection — it is simply an entry,
  sorted the same chronological way as everything else. On a night it
  isn't visible (the normal case — verified zero visible ISS passes over
  this location for multiple consecutive days, both before and after the
  unification), it is just absent, same as any quiet object, rather than
  a screen padded with stale ISS trivia because the mode had nothing
  better to show.
- **ONE arc renderer** (`_draw_pass_arc`) for OVERHEAD-NOW, used by every
  object including the ISS — SKY's version, kept because it was the more
  accurate of the two live treatments.
- **ONE chip treatment** (`_draw_chip`) for UPCOMING, extended from ISS's
  version to every object. GO OUTSIDE / GOOD PASS / VISIBLE now applies
  system-wide; SKY's old plain-text quality line is gone.
- **ONE accent colour** for the whole mode (`ACCENT`, was a gold/blue
  split with no shared meaning).
- **The ISS keeps exactly one real distinction**: continuous live
  telemetry (altitude/speed/sunlit from `satellite.py`'s wheretheiss.at
  poller) that nothing else in the catalogue has. It appears as a SLOT
  inside the shared layout only when the current list entry IS the ISS —
  altitude+sunlit while waiting, speed while overhead — the same pattern
  as MLB's diamond appearing inside the shared sports renderer, not a
  second screen. A small accent tint on its own name is the only other
  visual distinction it gets.

**`satellite.py`'s own ISS-only pass predictor (polluxlabs) is RETIRED.**
It had already been cross-validated against `skypass.py`'s SGP4
predictions before the cut — rise times agreed within 3–14s (scan step is
20s, so resolution-limited) and peak elevation within 0.1–0.4°, *and* both
independently found zero visible ISS passes over the same three-day
window. Maintaining two pipelines that already proved they agree was pure
duplication. `satellite.py` keeps **only** the continuous live-position
poller (wheretheiss.at) — that data has no equivalent anywhere else, which
is why it stayed. `ClockEngine`'s "next ISS pass" countdown now reads the
ISS entry out of `skypass`'s unified list, matched by **NORAD catalog
number 25544**, not by name string — a CelesTrak display-name formatting
change cannot silently break the match the way a string comparison could.

**`ambient_weight()` now considers the best pass across the WHOLE list**,
not the ISS alone — the ISS being invisible for days must not suppress
real dwell time for a genuinely bright pass from something else.
**Confirmed against real data 2026-08-02, and it changes real behaviour
today**: with the ISS genuinely invisible right now, the OLD logic (ISS
quality only) would return the 1.0 default weight → ~400 ticks (~20s)
dwell. The new logic correctly credits the best available non-ISS pass
(rank 3 in the live catalogue) → weight 2.5 → clamped to the 900-tick
ceiling (~45s). Verified end-to-end through `AmbientEngine._dwell_for()`
with the real satellite sub-engine, not just the weight function in
isolation — `has_content()` true, in `SEQUENCE`, dwell computation
matches the manual formula exactly.

**`SatelliteEngine` is now a real `Browsable` subclass** (tap-to-step,
hold-to-accelerate through the pass list) instead of hand-rolled
left/right — matches the system-wide scroll-control convention, and
`/api/state`'s `browse` reporting (see keyboard section) picks it up
automatically with no special-casing needed.

- Catalogue is CelesTrak's **`visual` GROUP** (157 objects) — naked-eye
  observable, curated upstream, so no invented magnitude cutoff.
- **"Visible" = satellite sunlit AND observer in darkness.** Both are
  computed. Overhead at noon is invisible; in Earth's shadow at midnight
  equally so.
- **Requires `sgp4`** — the first non-stdlib dependency outside
  mirror/video (`requirements.txt`). The launchd service already runs
  `.venv/bin/python`, so this is fine, but it **degrades honestly**:
  `HAVE_SGP4 == False` shows "PREDICTOR UNAVAILABLE" rather than guessing.
  A hand-rolled propagator was rejected — a subtly wrong SGP4 produces
  confidently wrong times.
- **Layout bug from `render_audit.py`, not review**: the countdown row
  started 1px after a scale-2 name ended and collided on longer names
  ("TERRA" overlapped "1H 50M"). Fixed with a y-cursor. Also widened the
  name-fit budget from `WIDTH-6` to `WIDTH-4` — the tighter one truncated
  exactly-15-character names like "SPACEMOBILE-001" for no real reason.
- **OVERHEAD-NOW verified live 2026-08-02** — a real pass (multiple
  objects overhead simultaneously around 23:01 local) was caught at the
  start of the next session. Confirmed programmatically, not by eye: the
  arc marker's x position swept monotonically 19->56 over 43 real
  seconds while y dipped toward the horizon as the pass ended, then the
  view correctly transitioned back to UPCOMING within the next poll once
  the pass genuinely ended. Real orbital timing driving real on-panel
  motion, not a loop or a static frame.
- **Real finding from that same capture, not yet fixed**: two objects
  (OAO 3, peak 75.8°, and SEASAT 1, peak 19.8°) went overhead in the same
  tick. The "newly overhead -> jump cursor" logic in `tick()` iterates
  the pass list and keeps whichever match it finds LAST, which is
  arbitrary tie-breaking, not a deliberate choice -- the lower, less
  impressive pass could win over a dramatically better one purely by
  list order. Worth a small fix: prefer the higher `quality_rank` (or
  peak elevation) when multiple passes become overhead in the same tick,
  rather than "whichever the loop saw last".

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

**Ambient shows the REAL mode screens** (revised 2026-08-01). An earlier
pass gave each mode a separate "channel ident" layout (`draw_ident`,
per-mode `ambient_frame()`); **that was removed deliberately and should not
come back.** The manual screens are the designed ones, and maintaining two
layouts per mode meant every change had to be made twice or they drifted —
with the ambient copy being the one nobody sees while working on a mode.

`AmbientEngine._render_current()` is now literally `self.current.frame()`.
Ambient is a rotation **controller**: it owns WHICH mode shows, for HOW
LONG, the entrance between modes, and browsing — **not what any mode looks
like.** Kept from that pass: weighted dwell (`ambient_weight()`, live game
~45s vs guestbook ~16s, clamped 10–45s), the per-mode entrance transition
(`AMBIENT_STYLE`), and the shared scroll control.

Verified pixel-identical: 25 rendered frames per mode against an
independently-ticked instance of the same mode, all six modes, 150/150
identical. **Freeze `cycling` when testing this** — ambient legitimately
skips a mode whose `has_content()` is false (flights with zero aircraft),
which looks like a rendering mismatch if the rotation is allowed to move
under the comparison.

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

**The two web surfaces** (`arcade.html` control panel, `remote.html` phone
remote) — audited and brought current 2026-08-01. Before that, **seven modes
had no button at all** and **every one of the seven config endpoints had a
working API and no UI**, so setting a favourite team meant a raw `curl`.

- Modes are **grouped**, not one flat list: Games (14, with the auto-play
  demos folded into a `<details>`), Data & ambient, Event, Media. **GAME DAY
  is styled as a takeover**, in its own crimson matching
  `engines.GAMEDAY_ACCENT`, because it behaves differently from a peer mode.
- Config cards for ticker, location, sports, news, guestbook and Game Day
  appear with the mode they belong to; **night dimming lives in the rail**
  because it is global. The **location card is shared** by ISS/flights/
  weather rather than duplicated — there is one `location_config.json`.
- **When adding a mode, add its button AND its config card**, then re-run the
  cross-check that every `data-mode` maps to a real engine and every
  selectable engine has a button. That check found all seven gaps.
- **Field shapes must be read from `arcade_server.py`, not assumed.** Three
  were wrong on the first pass: `/api/ticker/symbols` takes **separate
  `crypto` and `stocks` lists** (crypto is validated against CoinGecko,
  stocks are not), and news uses **`feed_url`** on both GET and POST.

**Keyboard control** (`arcade.html`) — reuses the `Scroller` model rather
than inventing a second one. `keydown` → `/api/press`, `keyup` →
`/api/release`, so tap-to-step and hold-to-accelerate behave identically to
the phone remote and the acceleration curve stays server-side.
- **`e.repeat` is guarded**: the OS key-repeat must never re-fire `press`, or
  a held key double-steps. Verified: a held key with six OS repeats still
  sends exactly one press and one release.
- **`INPUT`/`SELECT`/`TEXTAREA` are excluded**, or config fields become
  unusable. Verified: arrows, space, P and Escape typed into a field send
  nothing and leave focus intact.
- The legend is **derived from the engine classes**: only modes that actually
  subclass `Browsable` advertise "hold accelerates". `satellite`/`weather`/
  `clock` cycle views without acceleration and say so.

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
- gameday (UFC): **1 call** per poll for the WHOLE card — 20s while a fight
  is live, 120s if a card exists today, 1800s otherwise. Fight statistics
  add 2 calls (one per fighter) but only for the single fight on screen,
  and only while it is live or just finished.

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

## Data sources that were CHECKED AND REJECTED (2026-08-01)

Verified against the real endpoints, not assumed. Recorded so nobody
re-litigates them from memory or builds against a source that will bill.

### Airport arrivals/departures board — NOT VIABLE FREE

A per-flight board (airline, flight number, city pair, scheduled/estimated
time, gate status) has **no free source**. Probed live:

| source | result |
|---|---|
| OpenSky `/flights/arrival` | **403** — anonymous access to the flights endpoints was withdrawn; needs OAuth2 now |
| adsb.lol `/v2/airport/KMYR` | **404** — it has no airport endpoint (it is position-only, which is what `flights.py` already uses) |
| AviationStack | **401** — key required; free tier is 100 req/month and a paid ladder |
| AirLabs | **401** — key required |
| AeroDataBox | **401** — key required |
| FAA ASWS | host no longer resolves (service retired) |

**What IS free and real:** `nasstatus.faa.gov/api/airport-status-information`
— no key, XML, live. But it is national **delay/closure** info only, and
it lists **only airports currently affected** (MYR was absent, correctly,
because it had no delays). That is a genuine "is my airport delayed" feed,
**not** a flight board. Scheduled airline data comes from Cirium/OAG/
FlightAware, all paid.

### Commute / traffic — NOT VIABLE FREE

The green/yellow/red indicator requires *live traffic*, and every source
with it is paid. Probed live: TomTom **401**, HERE **401**, 511SC **404**
(both paths), Waze partner feed **404**. OSRM's demo server works with no
key and returns a route and duration — but it is **free-flow routing with
no live traffic**, so the indicator would be permanently green and the
number would never reflect an actual jam. Building on it would look like
the feature while being decorative, which is worse than not having it.

Per PRODUCTION.md's no-recurring-per-unit-cost rule, both are **skipped
rather than approximated**.

## Deliberately deferred (decided against, NOT missed)

Things that were considered, priced, and consciously left out. They are
recorded here so a future pass does not "discover" them as oversights and
build them badly.

### Long-haul / international flights as a "notable" category

`flights._notable()` flags heavy jets, helicopters, airships, unusually
high and unusually low traffic, and emergency squawks — all confirmed
actually occurring in a real 213-aircraft sample before shipping.

Long-haul or international was considered for the same list and
**deliberately left out**: judging it means resolving the origin and
destination airport of every aircraft to coordinates, which needs a
second dataset (an airport-coordinates table) plus a per-flight lookup on
a path that currently does none. That is a real cost on a hot path, and
the cheap approximations (guessing from altitude, callsign prefix, or
registration country) are wrong often enough to be worse than not
labelling at all — a mislabelled "INTL" is more damaging than a missing
one, because the whole point of the notable tag is that it is trustworthy.

**Revisit only alongside an airport dataset**, not by inference. Until
then this is a deferred enhancement, not a gap in the notable criteria.

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

### Sports coverage — UNIVERSAL as of 2026-08-01

Sports is no longer 7 configured leagues. `sports.FEED.get_universal()`
reads **one** endpoint — `site.api.espn.com/apis/v2/scoreboard/header`,
the one behind espn.com's own scoreboard bar — which returned **43 events
across 11 leagues in 7 sports in a single request**: golf (PGA + LPGA),
MLB, WNBA, three soccer competitions, PFL, PLL lacrosse, ATP and WTA.

**Do NOT replace this with per-league polling.** ESPN publishes **338
sport/league slugs** (enumerated from `sports.core.api.espn.com/v2/sports`);
covering them individually is exactly the volume risk below. This is one
request no matter how many leagues are live — it *reduced* the sports
request rate while multiplying coverage.

**Absence is free and that is the design.** ESPN omits a league with
nothing on, so there is no "no games today" state to filter. Never add an
empty-league placeholder back.

**Payload shapes are NOT uniform across sports** — verified against a real
payload, and this is the trap:
- MLB puts baserunners at the **event top level** (`onFirst`/`outsText`),
  *not* nested in `situation` the way the per-league endpoint does.
- `status` here is a plain **string** and `summary` is display text
  ("Final", "FT", "Round 3 - In Progress"); the per-league API nests both
  under `status.type`.
- Golf/tennis competitors are **athletes** with `place` / `score` ("-10") /
  `status.thru`, not teams with numeric scores. Golf arrives as a real
  25-deep leaderboard; the dedicated golf endpoint has the **full 147-player
  field** if more depth is ever needed.
- Tennis carries `linescores`, `tournamentSeed` and completed-match
  `notes[]`; soccer carries `form`; MMA carries `cardSegment`,
  `matchNumber` and a weight class in `competitionType`.

**Golf — pinned player** (2026-08-01). `sports.load_golf_player()` /
`save_golf_player()` store a name in the same `sports_config.json`;
`/api/sports/golf_player` and the control panel set it. The pinned view
leads with **position / score to par / through-hole**, because golf's
question is "where is MY player", not "what is the score".

- **Notable moves only** — eagle, birdie, bogey, taking or losing the lead
  — flashed through the shared `Pulse`. Routine holes deliberately do not
  flash.
- **Detection lives in the FEED, not the engine.** Only the feed sees
  consecutive polls (which is what a "move" is defined against), and the
  engine is recreated on every mode switch, so a baseline kept there would
  make the pin flash on arrival. Pinning a new player clears the baseline.
- **Score to par is a STRING and `E` means even par** — `_par_value()`
  handles it; `int()` on it raises, and treating `E` as missing compares
  wrongly.
- **Name matching is forgiving on purpose**: the board shows
  "R. HOJGAARD", a person types "Rasmus Hojgaard" or "hojgaard". Exact,
  surname, and substring all match.
- `save_config()` **preserves keys it does not own**, so setting a
  favourite team cannot wipe the pinned golfer.
- **Trap that actually bit:** `tick()` forces `view = 1` whenever the
  configured leagues have any game, so the golfer view (at `view == 0`)
  was *unreachable* until a pinned golfer was made to count as real
  view-0 content. It rendered perfectly in isolation and never appeared
  on the panel — only an end-to-end pixel match against the real panel
  caught it.

### Per-sport renderers (IN PROGRESS — started 2026-08-01)

**Why**: one generic renderer had to satisfy every sport at once, so every
layout decision was made for the WORST CASE across seven of them. The tell
that it was under-serving: the one-renderer rule was **already broken
twice** (golf is a leaderboard, tennis has string scores too wide for the
slot). Those two broke loudly enough to force an exception; the rest were
quietly flattened into two rows of `ABBREV + score`.

**Contract**: `SportsEngine.SPORT_RENDERERS` maps `sport` → a method taking
`(self, buf, ev)` that draws the WHOLE frame including its own header and
returns None. The caller owns blank/fill. **Anything unclaimed falls back
to `_frame_universal_generic()`, unchanged** — adding a renderer is purely
additive and cannot regress another sport.

| sport | status |
|---|---|
| baseball | **done** — diamond/outs/count/half-inning on the main row |
| mma | **done** — weight class primary, records per fighter, card position |
| football | not started — **NFL/NCAAF are off-season, no live data to verify against** |
| basketball | not started — NBA off-season; verify against **WNBA**. No live WNBA game as of 2026-08-01 evening (both today's games already finished) |
| soccer | **done** — form strings, ESPN-formatted clock, penalty shootouts (verified live). Layout uses a y-cursor after the audit caught the divider/clock overlapping the second team |
| tennis | not started — no live match as of 2026-08-01 evening. **Confirmed real**: header events carry `linescores`, one entry per SET with `value`/`displayValue`/`period`/`winner` (checked against a real finished match, J. Pegula d. D. Shnaider 7-5 6-4) — this is the field the set-by-set grid needs, but it has not been rendered against a LIVE match, only a finished one glimpsed while checking the shape |
| golf | **done** — 6-row leaderboard, movement arrows (sign verified: negative = climbed the leaderboard) |

**Baseball payload facts** (verified, do not re-derive):
- `onFirst`/`onSecond`/`onThird` are **athlete IDs, not booleans** (0 =
  empty). Truthiness is the right test, for a non-obvious reason.
- **The count is NOT in the header.** No balls/strikes fields exist in it
  at all, checked across a full 15-game slate. It comes from the
  per-league scoreboard's `situation`, joined by event id in `tick()`.
  Event ids match **exactly** across the two feeds (15/15).
- **Therefore the per-league poll is NOT redundant** for configured
  leagues — it is the only source of the count and win probability. Any
  "cut the redundant polling" work must be narrower than deleting it.
- **Baseball's live layout was later verified against 4 real live MLB
  games** (2026-08-01, evening slate) -- exact pixel match on the real
  panel. The earlier synthesized-state sweep is now backed by a live
  check; no gap remains.

### Two-axis navigation (2026-08-01)

LEFT/RIGHT walks **games within a league**; UP/DOWN walks **leagues**.
Both axes are driven by the SAME `Scroller`, so tap/hold/accelerate feels
identical — `Browsable.VERTICAL_BROWSE = True` opts a mode in and it
implements `_step_v()`. Auto-advance pauses while **either** axis is active.

**Grouped by league, not sport**, deliberately: ESPN nests
`sports → leagues → events` so sport is the native outer key, but people
name leagues ("is the NWSL game on?"), and grouping by sport would merge
ATP with WTA and PGA with LPGA and lump three soccer competitions
together. **League order follows the feed's own order** — stable on
purpose; sorting by "has a live game" would move leagues under the
viewer's fingers.

Discoverability is one affordance per axis: a **league rail** down the
right edge (pip per league, current lit and widened) for vertical, and the
header's **league-relative** N/M counter for horizontal.

### Panels replaced the contested view slot (2026-08-01)

`view` used to be `0=PINNED / 1=TICKER` where **slot 0 was contested** —
favourite team and pinned golfer both wanted it and whichever lost became
*unreachable*, and `tick()` force-set `view=1` on top of that. That is what
hid the golfer view. **Every panel with data is now its own entry in
`self.panels` and gets its own turn**, so nothing shares a slot and a
future pinned thing cannot hide an existing one.

### Auto-cycle shows only STARTED games

A board that spends its time on things that have not happened yet is a
schedule, not a scoreboard. Today that was **40 of 54 events scheduled**;
the auto-cycle visits only live and finished ones, cutting a lap from ~4
minutes to ~1. **Manual browsing still reaches scheduled games.** Falls
back to everything if nothing has started.

### fit_person() vs fit_text()

`fit_text` drops whole trailing words — right for a headline, **wrong for a
name**: "T. POSTARNAKOVA" became "T.", keeping the least informative part.
`fit_person()` tries the **surname** before cutting anything. Use it for
any human name.

**Select-to-expand**: `rotate` expands the event under the cursor into a
full-detail view, `drop` (or `rotate` again) returns to the ticker at the
same position. Browsing while expanded stays expanded, and auto-advance is
suspended there. The detail view shares GAME DAY's `draw_event_frame()` —
one "this is a whole event" language, not a third style.

**Two layout rules this exposed, worth keeping:**
- **String scores must not be drawn at scale 2.** Tennis set scores are
  126px at scale 2 on a 64px panel; they drop to scale 1 on their own line
  and are never truncated (a clipped set score is *wrong*, not smaller).
- **The detail view lays out with a Y-CURSOR, not fixed rows.** What each
  sport provides varies, and fixed offsets collided the moment content
  changed — the second competitor's record landed on the venue line in
  both tennis and MMA.

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
6b. **Tennis tiebreak brackets were silently dropped** — the EIGHTH
   instance. `"7-6(7-5)"` rendered as `"7-67-5"`, a different and
   plausible-looking score. `(` and `)` added to `_FONT3x5`.
6. **`&` was silently dropped from NFL down-and-distance** — the SEVENTH
   instance, and it had already shipped. ESPN's `downDistanceText` is
   literally `"3RD & 7"`, so the sports mode rendered `"3RD  7"`. The glyph
   is now in `_FONT3x5`; the data was correct and the font was short.
   Found while building GAME DAY's team view, which reuses the same
   `situation_line()`.
7. **GAME DAY's stats view had overlapping text** — the "FIGHT STATS"
   kicker occupies rows 6-10 and the fighter names were drawn at y=8.
   Invisible to a code read; caught by rendering the frame and looking.

### The two audits — run BOTH; they catch different things

`render_audit.py` sees only the data flowing **right now**. `fold_audit.py`
proves the fold is **applied at all**. A feed can pass the first and fail
the second all day, and that is precisely the latent state every glyph bug
has shipped from.

### `fold_audit.py` — run after touching ANY feed parser

    .venv/bin/python fold_audit.py

Replays each feed's own real payload with five known-undrawable characters
injected into the display strings (each one has already caused a real bug
here), then asserts nothing undrawable reaches the output. Control tokens
(`pre`/`in`/`post`, ids, dates) are skipped on purpose — parsers compare
against them and none is drawn, so polluting them breaks the parse instead
of testing the fold.

**It found three unfolded boundaries on its first run**, all of which had
passed a live check hours earlier *because that day's data was ASCII*:
sports' per-league `record`/`score`/`display_clock`, mma's `clock`, and a
news boundary that lived in the caller rather than in the function that
looked like the boundary.

**The lesson worth keeping: "I checked it live and it was clean" is not
evidence a field is folded.** It is only evidence about today's data.

**A fold belongs INSIDE the function that looks like the boundary**
(`news._clean_title`, `blog._clean`, `flights._ident`). When it sits in the
caller instead, an audit aimed at the obvious place passes while the real
boundary is somewhere else.

### `render_audit.py` — run this before calling any layout done

    .venv/bin/python render_audit.py            # every text mode, real data
    .venv/bin/python render_audit.py sports     # one mode
    .venv/bin/python render_audit.py --strict   # fail on truncation too

Makes permanent the instrumentation that caught every one of the ten glyph
bugs. Checks four things: **DROPPED** (no glyph), **OVERFLOW** (leaves the
panel), **TRUNCATED** (reported, not failed — abbreviating a headline is
legitimate), **COLLISION** (two text draws sharing pixels).

**It found three real bugs on its first run**, all in code shipped hours
earlier: a soccer layout drawing the divider and clock through the second
team's score row, a golf name budget cutting "E. HENSELEIT" to "HENSEL",
and the tenth glyph instance (news dropping a curly quote, which is what
exposed that four feeds had never been migrated to `paneltext`).

Marquee modes legitimately draw off-edge to loop seamlessly and are
exempted via `MARQUEE_OK` — `ambient` is in that set because it composes
real instances of the marquee modes.

**COLLISION is the check worth caring about most.** Fixed row offsets are
correct until content varies — a longer record, a form line, a team with a
longer name — and then two elements silently overlap. Prefer a **y-cursor**
over fixed offsets in any renderer whose content varies.

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
- **`fit_text` truncation is silent by design** — this is now covered by
  `render_audit.py`, which reports every truncation (see below). Resolved.
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
