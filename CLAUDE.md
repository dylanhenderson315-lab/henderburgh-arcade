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
a unified satellite tracker for the ISS and every other visible bright
satellite, flight tracker, sports scoreboard, news headline ticker,
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
that module's docstring for the full ten-instance tally of this bug (plus
why a per-module fold still wasn't enough — that's instance 10, news). The
fold used to live privately inside `mma.py`, which is exactly how
`sports.py`'s universal feed reintroduced it later (a live PGA leader,
"Hojgaard", rendered as "HJGAARD"). **A per-module fold is not a fix; it
is a fix waiting to be missed by the next module.** Unsupported characters
become a space rather than vanishing, so a loss is visible. **Enforced by
`fold_audit.py`, not just this rule** — see the audits section.

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
  **UNIFIED 2026-08-01**, see the dedicated section below for the full
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

  **Real city names + readable aircraft type (2026-08-02)**, prompted by
  a commercial reference product (a flight-info LED display) the owner
  wanted this mode to read more like. Two real fields were already
  available and simply being discarded:
  - `adsbdb`'s route response includes `origin.municipality`/
    `destination.municipality` (confirmed live: RDU → "Raleigh/Durham",
    LGA → "New York") alongside the airport codes already in use —
    `flights._fetch_route()` now keeps `origin_city`/`dest_city`, folded
    through `paneltext.panel_text()` at the I/O boundary like every other
    externally-sourced string here (airline names get the same fold now
    too, closing a gap where they were only `.upper()`'d, the exact bug
    class in `paneltext.py`'s tally instance 2).
  - `flights.ICAO_TYPE_NAMES` — a static ICAO-type-designator → readable
    name lookup (`"B738"` → `"737-800"`), same category of reference data
    as the compass-direction table, not invented per-flight. Populated
    from real codes seen in a live 40nm sample near ORD (A21N/B39M/A321/
    A20N/BCS3/B77L/B737/B772/B744) plus other common airliner types. A
    code missing from the table falls back to the bare code, never a
    guessed name.
  - The route line now prefers `"<origin city> > <dest city>"`, falling
    back to the airport-code pair when either municipality is missing,
    through the same `fit_text()` truncation every other row already
    uses — verified against a real adsbdb payload (DAL1362, RDU→LGA) and
    an intentionally-long pair (DFW→MSP full city names) to confirm
    graceful truncation with zero overflow/collision
    (`render_audit.py flights` clean both times). Known tradeoff, same as
    every other truncated field in this project: a city pair too long to
    fit loses the destination city entirely rather than clipping mid-word
    — acceptable, not a bug.
  - Row layout/positions are UNCHANGED from the already-audited baseline
    — only the CONTENT of the type and route slots got smarter, not the
    layout math, to avoid reopening the collision risk `render_audit.py`
    exists to catch.

  **RADAR SCOPE — shared system, TWO DIFFERENT PROJECTIONS (2026-08-02).**
  One visual language (`draw_scope_*` / `scope_xy` / `scope_glow` in
  `engines.py`) used by BOTH the flights and satellite modes: home at
  centre, dotted range rings, rotating sweep with a fading trail, targets
  that brighten as the beam passes. **What is shared is the DRAWING; the
  projection math is deliberately NOT shared**, because these answer
  different questions and one formula would make one of them wrong:
  - **flights — GROUND radar**: bearing + ground DISTANCE. Centre = home
    on the ground, rim = `RADIUS_NM` (40nm) out.
  - **satellite — SKY DOME**: bearing + ELEVATION ANGLE. Centre = zenith
    (straight up), rim = the horizon. Standard all-sky convention, and
    the natural full-sky generalisation of the existing single-pass arc.

  **The flight scope's range scale is SQRT, and that is a measured fix,
  not a style choice.** Against real live traffic near MYR, 6 of 9 real
  objects (8 aircraft + the airport) landed inside a **6px radius** on a
  linear 40nm scale — an unreadable blob at centre with the outer half of
  the scope empty — because most interesting traffic near a home location
  is approach traffic inside ~6nm. Sqrt puts **zero** of those 9 in that
  blob while preserving exact distance ORDER, and the rings are labelled
  with their **true nm values** (`10/20/40NM` on screen) so the
  compression is stated rather than hidden. The sky dome stays **linear
  in elevation** — the correct convention there, and it needs no such
  correction since elevation is already bounded 0–90°.

  **Targets never fade to nothing** behind the sweep (`SCOPE_TARGET_FLOOR`
  = 0.38). The object is really up there for the whole rotation, so
  vanishing for most of the cycle would be the display lying for the sake
  of the effect — the sweep is decoration over continuously-known data,
  not a sensor that only learns about a target when the beam hits it.

  **Home airport marker** is config-driven (`flights.load_airport()` /
  `save_airport()`), stored in `location_config.json` because an airport
  is a LOCATION fact and that file is the one source of truth for where
  the owner is. **Not auto-detected**: resolving "nearest airport" needs a
  12MB airport database to answer a question the owner answers once, same
  config-driven pattern as the pinned golfer. Seeded with MYR's real
  coordinates (33.679699, −78.928299) from OurAirports (public domain);
  verified 3.47nm at bearing 196° from the configured home, cross-checked
  against an independent haversine calculation.
  - **`satellite.save_location()` now PRESERVES keys it does not own** —
    it previously rebuilt the whole document, which would have silently
    wiped the `airport` key the first time the home pin moved. Exactly
    the lesson `sports.save_config()` already learned about `golf_player`.

  **The satellite dome is a THIRD, ADDITIVE view** — the settled
  UPCOMING/OVERHEAD-NOW pair is untouched and still selects itself from
  whether a pass is happening. An **actual overhead pass outranks the dome
  and pins the view to it**: that is the go-outside moment the mode exists
  for, and the new scope must never be able to interrupt it.

  **`has_content()` UPDATED 2026-08-02** to also count real objects
  genuinely visible right now, independent of whether the predictor has
  anything QUEUED next — a clear night with several bright objects
  currently crossing the sky but nothing predicted soon previously could
  never surface the dome in ambient at all, withholding one of the best
  visuals in the project exactly when it would land. Uses `sky_now()`'s
  `visible` flag (elevation + sunlit + observer darkness, the same
  three-part test `predict()` already uses), **never the raw above-
  horizon list** — confirmed live that 8–14 objects sit above the horizon
  in broad daylight with `visible` correctly `False` for all of them; the
  raw list would have made the dome claim content nearly around the
  clock. Verified against real orbital data at a real nighttime instant
  (not fabricated): 2 real objects visible with zero queued passes ->
  `has_content()` now `True`, was `False` before this change.

  **A second bug was found and fixed by actually rendering the new
  scenario, not by inspection**: `tick()`'s view stayed pinned to
  VIEW_PASSES (nothing to draw for an empty pass list) even once
  `has_content()` said yes, so `frame()` would have shown "NO VISIBLE
  PASSES" instead of the dome it just claimed to have. `tick()` now
  forces `VIEW_SCOPE` when passes are empty but something is genuinely
  visible. Dome target coloring also switched from `sunlit` to `visible`
  for the same daylight-false-positive reason — a sunlit object at noon
  now renders dim, not bright, matching what a person outside would
  actually see. The ISS keeps its badge tint and a larger mark
  regardless.

  **Orientation — tried text, shipped a real landmark instead
  (2026-08-02, same session).** First attempt: N/S/E/W single-letter
  labels on the crosshair, plus an alternating footer legend explaining
  the diamond/plus marks ("<>=HOME +=MYR"). Both were built, verified
  clean, and then reverted on direct feedback — they read as clunky
  instrument-panel text against the sweep/rings aesthetic this view is
  going for, and didn't actually make the marks self-explanatory.
  **`flights.COASTLINE`** replaced both: real geography, not decoration —
  extracted once from Natural Earth's public-domain 10m coastline dataset,
  clipped to the contiguous run within 55nm of the configured home,
  verified against the raw source before embedding (nearest real point is
  2.77nm at bearing 131.7° SE, consistent with the actual Grand Strand
  shoreline curving away from a few miles inland). **A live Overpass API
  coastline query was tried FIRST and reliably times out server-side** — a
  known limitation of that API for `natural=coastline` queries specifically
  (coastline ways are expensive), not a transient failure, which is why
  this is a one-time static extraction rather than a feed. Same reasoning
  as MYR's coordinates being a one-time lookup rather than a live source:
  coastlines don't move. Drawn as connected line segments through the
  exact same bearing/distance → `scope_xy()` pipeline every other scope
  element uses, so it tracks correctly if the configured home ever moves.
  **Static and location-specific on purpose** — if the home ever relocates
  somewhere the Atlantic isn't nearby, this needs manual re-extraction,
  same expectation as re-picking the home airport after a move.

  **Controls — the two modes now use DIFFERENT bindings, noted honestly
  rather than glossed over**:
  - **Flights** (updated 2026-08-02): `rotate` — the hardware's actual
    select button, same convention `SportsEngine`'s select-to-expand
    already documented — opens the aircraft currently under the
    left/right browse cursor into the full detail card; `drop` closes
    back to the scope, or toggles auto-advance when nothing is expanded.
    Replaced the original `up`/`down` toggle below, which flipped views
    with no connection to which aircraft was highlighted — the whole
    point of "select this ONE" is lost if the binding can't express which
    one. **Auto-advance now suspends while a detail view was reached by
    manual select** (`self._auto_detail` flag), not just by the periodic
    spotlight rotation — without this, the existing auto-cycle would
    silently walk the view away from an aircraft someone deliberately
    opened a few seconds later, the identical "browsing while expanded
    stays expanded" rule sports already follows.
  - **Satellite**: still `up`/`down` toggles scope ↔ detail (unchanged).
    Both engines are `VERTICAL_BROWSE = False`, so `Browsable._axis()`
    leaves up/down unclaimed and free to mean this — no new input
    plumbing was needed for either binding.
  - **Real pre-existing bug found and fixed while exercising the new
    flights binding** with `render_audit.py`'s collision detector driven
    across every real aircraft (a check the mode's normal step-loop never
    exercises, since it never calls `input("rotate")`): the inline
    aircraft-type text on the detail card's stats row was centred across
    the FULL PANEL WIDTH while the fit-check gating it validated against
    the GAP between the two side stats — a real collision (`P46T` vs
    `"24MI NE"`) passed the fit check and still visually overlapped,
    because `left="-"`/`right="24MI NE"` skews the true gap well off
    panel-centre. Now centred within the actual gap the check measured.

  **ATC transcription log — PERSONAL-RIG-ONLY, phase 1 (transcription
  process) done and verified live 2026-08-02.** Concept: selecting an
  aircraft on the scope and pressing select twice (past the detail card)
  opens a timestamped log of real ATC transmissions. Feasibility was
  investigated FIRST, no code, against five specific questions before any
  commitment — full writeup and the answers are in the session this was
  built in; the short version below is what shaped the build.

  - **PERSONAL-RIG-ONLY, not a maybe.** LiveATC.net's own current terms
    (confirmed live): *"Audio streams may not be used in any third-party
    products."* Transcribing to text doesn't launder that clause — see
    `PRODUCTION.md`'s matching exclusion note, which this doc and that one
    must stay in sync on. Same category of restriction as the flight
    tracker's plane icon deliberately not being a real airline logo, just
    a data-source restriction instead of an IP one.
  - **A genuinely separate OS PROCESS** (`atc_transcribe.py`, run
    standalone — `.venv/bin/python atc_transcribe.py`), never imported by
    `arcade_server.py` or `engines.py`. Confirmed reasoning, not just
    caution: `mlx-whisper`'s real compute is Metal/native (releases the
    GIL, same as numpy), so a thread was *probably* fine, but proving that
    under real concurrent render-loop load wasn't worth the risk when
    process isolation removes the question entirely — same discipline as
    the existing hard rule against `import arcade_server` from a script,
    applied in the opposite direction.
  - **Real stream, found by testing, not scraping**: LiveATC's website is
    Cloudflare-protected and can't be scraped for a feed list, but their
    actual audio mounts are served separately and weren't protected.
    `s1-bos.liveatc.net/kmyr` confirmed live, continuous, real MYR ground
    frequency audio.
  - **Real performance, measured on this Mac mini (M4 Pro)**: a genuine
    71.68s MYR clip transcribed in 1.76s — a **40.8× realtime factor**
    with `mlx-community/whisper-small.en-mlx`. `CHUNK_SECONDS = 20` (see
    `atc.py`) leaves enormous headroom before the next chunk is due.
  - **A real bug from the FIRST live run, not a hypothetical**: `curl
    --max-time` exits **28** (`CURLE_OPERATION_TIMEDOUT`) when it correctly
    cuts a continuous stream off after the requested duration — the
    ENTIRE POINT of using `--max-time` against a stream that never ends on
    its own — but the original code treated any non-zero exit as a hard
    failure, so every single fetch was silently discarded despite
    returning real, valid audio. Fixed: exit codes `(0, 28)` are both
    treated as success; only an actually-empty/tiny file counts as a real
    failure. Caught immediately by watching the FIRST live run rather than
    assuming a clean unit test proved the real pipeline worked.
  - **A second real quirk, also from the live run**: Whisper occasionally
    returns a bare `"!"` or similar stray punctuation on near-silent
    audio instead of `""`. Not a crash, just noise — filtered by requiring
    at least one alphanumeric character in the result before it counts as
    a real transmission.
  - **Verified stable against the real live stream**, not a sample file:
    ~90s of continuous real operation, 3 real transcript chunks, zero
    crashes, e.g. `"...wind at 200 at 16 gusts 22, the altimeter 2 at 9 or
    8, 3, 2 clouds, 2001 are scattered at 5500, broken at 8,000"` — a real,
    coherent ATIS-style weather readout. Process CPU held at ~0.1%
    baseline between chunks.
  - **Log format**: `atc_log.jsonl` (gitignored — runtime state, not
    source), one JSON line per non-empty chunk, `{"ts": <real wall-clock
    chunk-START time>, "text": ..., "duration": 20}`. The engine-side
    reader (not yet built — phase 2) only ever reads this file; it never
    talks to the worker process directly, matching every other feed's
    "background writes, engine reads a snapshot" shape, just with a file
    standing in for an in-memory `FEED` object because the writer is a
    separate process.
  - **PHASE 2 — done and live-verified 2026-08-02.** `atc.AtcLogFeed`
    reads the worker's log with the same shape every other `FEED` in this
    project uses. `FlightEngine` gained a THIRD view (`rotate` now cycles
    SCOPE → DETAIL → ATC LOG → SCOPE, not a two-state toggle), showing the
    GENERAL airport log — deliberately not filtered to the selected
    aircraft, since per-aircraft correlation is phase 3 and, per the
    feasibility research's own finding, will only ever be a
    confidence-gated highlight, never a silent filter.
    - **"NO CUTOFFS, EVERYTHING MUST BE SEEN" — real requirement, not a
      nicety.** A real transmission runs 100-400+ characters (one real
      MYR transmission wrapped to 30 lines). `self.atc_pages` holds
      EVERY page for the current entry (recomputed only when the entry's
      timestamp actually changes), `tick()` auto-advances a page every
      ~4.5s, and left/right overrides manually with wraparound — all
      three verified directly (not just by rendering once), including
      that a fresh transmission always resets to page 1 rather than
      wherever the cursor was.
    - **"LAST: Xm Ys" vs "Xs AGO"** distinguishes a stale last-known
      transmission from a live one — never a blank screen just because
      the frequency went quiet, matching "show the last one, be honest
      about when" exactly as asked.
    - **THIS PROJECT'S #1 DOCUMENTED BUG CLASS, shipped again in code
      written this same session.** Whisper's raw output is natural
      mixed-case English; the 3×5 font is uppercase-only. Unfolded, "A
      bunch of them" rendered as literally just "A" — every lowercase
      letter silently dropped, found by looking at an actual rendered
      frame. Fixed by folding through `paneltext.panel_text()` at the
      WRITE boundary (`atc_transcribe.fold_transcript()`, extracted as a
      pure function specifically so it needs no live model to test).
      Given this project's own history with this exact bug class, added
      **permanent `fold_audit.py` coverage** for it too — verified the
      coverage would have caught the original bug by running it against
      the unfixed code first, not just added and trusted.
    - **A second real bug, ALSO found by rendering, not assumed clean**:
      the original `"LAST TX Xm Ys AGO"` caption (19 chars, 75px) overran
      the 60px budget and silently dropped `"AGO"`. Shortened to
      `"LAST: Xm Ys"` (13 chars, 51px real margin).
    - **A THIRD real bug, found ONLY by watching the live panel over
      several real minutes — the kind of bug no standalone test could
      have caught.** `AtcLogFeed.get()`'s 2s re-read throttle compared
      "now" against a single `_last_read` timestamp that `get()` itself
      bumped on EVERY call. On an engine ticking every 0.05s, `get()` is
      called every 0.05s too, so the elapsed time being checked could
      never accumulate past ~0.05s — the comparison timestamp was reset
      by the very call checking it. `_refresh()` never ran again after
      the very first read. The live panel showed the SAME first-ever log
      entry for 14+ real minutes while the file kept growing underneath
      it, invisible to every earlier standalone test (a short-lived
      script calling `get()` only a few times, seconds apart, never
      exercises the rapid-call pattern that exposes this). Fixed the way
      `flights.FlightFeed` already does it correctly: a SEPARATE
      `_last_refresh_try`, touched only by the refresh path, never by the
      read path. Verified two ways: a direct simulation of the exact real
      tick rate against a file that changes mid-run, AND a real ~10-
      minute live-panel session confirming the caption tracked the
      genuinely latest transmission throughout, not just once.
    - **render_audit.py's flights coverage jumped from 13 to 29 frames**
      after this — confirms the rotate-driven step-loop (added last
      session) now also exercises all THREE view states across every
      real aircraft automatically, not just two.
  - **PHASE 3 — done and verified against real live data, 2026-08-02.**
    `atc.match_callsign()` matches AIRLINE NAME + FLIGHT NUMBER only,
    exact-match required against the REAL currently-tracked aircraft list
    — never GA tail numbers, per the feasibility research's own finding
    that those cannot be reliably reconstructed from ASR text.
    `atc.AIRLINE_ICAO` is real, static reference data (FAA radio
    callsigns → ICAO codes), same category as `flights.ICAO_TYPE_NAMES`.
    **Built and tuned against real captured MYR transcript text before
    ever touching the engine**: confirmed exact matches (`DAL2327`,
    `FFT4117`) against real currently-tracked aircraft, and confirmed
    correct REJECTION of same-airline-different-flight-number candidates
    (`"SOUTHWEST 1437"` said repeatedly on real audio while only
    `SWA2587` — a different flight — was actually in range that
    session). A match draws a bright-green `"MATCH: <ident>"` tag in the
    log (`ATC_MATCH`, visually unmistakable from the general amber) and
    the SAME color on the matched aircraft's own scope marker — the
    correlation is visible from either direction, matching the original
    pitch. **General log is ALWAYS the unfiltered default** regardless of
    match status — nothing about this layer can ever hide a real
    transmission, only add a highlight on top.
    - **A real bug found by checking against LIVE data, not the
      synthetic test that had already passed.** Matching was originally
      gated to the exact same "entry changed" trigger as page
      computation — a one-shot attempt at the moment a transmission
      arrives. But `flights.FEED`'s background thread may not have
      completed its first fetch yet on that exact tick, so a genuinely
      correct real match (`"SOUTHWEST 1437"` → `SWA1437`, both real) was
      silently missed because the aircraft list was still empty at that
      specific instant — invisible to a synthetic test that pre-seeds
      both the transcript and the aircraft list together, only exposed
      by driving the real, asynchronous startup race. Fixed by retrying
      every tick while unmatched (cheap: one regex pass over a short
      string) instead of a single attempt, with the page budget
      recomputed correctly even when the match lands on a LATER tick
      than the transcript itself did. Verified by forcing the exact
      race (ticking immediately after `reset()`, before the feed has
      warmed up) and confirming the match eventually lands once real
      data arrives, not just in the case where both happened to be
      ready at the same instant.

  **SELECTION IDENTITY FIX + PART 2 (airport selection, per-aircraft
  conversation), 2026-08-02.** Two related pieces of a larger radar/ATC
  overhaul, both done and verified.

  - **`FlightEngine.sel_key` replaced `self.cur` as a bare list index.**
    `flights.FEED` re-sorts its aircraft list by notability/distance on
    every refresh, so an index survived across ticks but the aircraft AT
    that index did not — a real, confirmed correctness bug: a selection
    could silently start pointing at a different real aircraft the
    moment the list reordered underneath it. `sel_key` is a stable
    identity (`hex`/ICAO24 preferred, `ident` fallback — new `hex` field
    added to `flights._fetch_positions()`'s output for exactly this),
    resolved to a position fresh every tick/step/frame via
    `_find_by_key()`. **An aircraft leaving `RADIUS_NM` now explicitly
    clears the selection and falls back to the scope view** rather than
    silently re-pointing at whatever now occupies the old slot — same
    "an honest gap beats a lie" principle as everywhere else in this
    project. Verified with a direct simulation of a live reorder
    (selection correctly followed the aircraft) and a selected-aircraft-
    leaves-range case (selection cleanly drops), plus full
    `render_audit`/`fold_audit` passes and a live panel run.

  - **Speaker attribution (controller vs. pilot per transcript line) was
    investigated and DROPPED, not built** — checked against real
    accumulated `atc_log.jsonl` data before writing any rule, same
    discipline as the phase 3 callsign-matching research. Finding: each
    real 20s Whisper chunk routinely contains BOTH the controller and one
    or more aircraft, interleaved, with real cross-talk, and Whisper
    gives no diarization or utterance boundaries — e.g. one real captured
    chunk reads `"ALRIGHT, LEAVING 3046 LEFT ON ALPHA, TAXI TO RAMP AND
    SAY GATE.! 10-8-2, I'M TALKING SOUTHWEST, THEY'RE STOPPING AT
    1-2,000..."`, a ground instruction, an aircraft readback, and a
    second aircraft's transmission all run together in one chunk with no
    boundary markers. A whole-chunk speaker label would misattribute
    mixed content most of the time. **Not honestly derivable from this
    data — flagged to the owner rather than approximated with a heuristic
    that would be wrong more often than right.**

  - **What IS honestly derivable, and what got built instead: two real
    views over one log, keyed off identity-based selection.** Selecting
    an aircraft and opening the log shows its "conversation" — every
    entry in the retained log (up to `atc.LOG_MAX_AGE_SECONDS`) where
    `atc.match_callsign()` names THAT aircraft, newest first, paginated
    across the whole filtered set with each page carrying its OWN source
    entry's real timestamp (so an older transmission in a multi-message
    conversation correctly reads as older once paged to, not all
    captioned with the newest one's age). This is real filtering of real
    transcript chunks, not reconstructed dialogue — still whole-chunk
    granularity, honestly so. Selecting nothing, or the airport, shows
    the unfiltered general frequency log exactly as phase 2 built it
    (just the single newest transmission), plus the phase 3 confidence-
    gated match highlight — which only draws in the general view now,
    since inside a per-aircraft filtered view the header already names
    that aircraft and a repeated "MATCH: X" tag would be redundant.
  - **The airport is now a real, steppable selection target**
    (`FlightEngine.AIRPORT_KEY`, a sentinel in the same `sel_key`
    namespace as a real aircraft's hex/ident — never collides with one),
    appended after every aircraft in the left/right step cycle, not just
    a marker drawn on the scope. It gets its own two-state `rotate`
    (SCOPE ↔ ATC LOG) rather than the three-stop aircraft cycle, since it
    has no per-aircraft DETAIL card to show. Exempted from the
    "still in the aircraft list" selection-loss check (it's never in
    that list by construction) but still clears if the airport itself
    gets unconfigured out from under the selection.
  - **A real bug found by `render_audit.py`'s existing step-loop against
    real live aircraft + real logged transcript data together**, not by
    inspection: `"NO TRANSMISSIONS YET"` (17 chars / 66px) overflowed the
    64px panel by 2px the moment the step-loop's automatic
    rotate/left-right driving happened to land on a real selected
    aircraft with genuinely zero matching real transmissions in the
    current log — a state that only exists once real aircraft AND a real
    log both exist, so it could not have shown up against synthetic data
    alone. Fixed by splitting across three short lines, matching the
    general view's existing "NO ATC DATA / YET" pattern.
  - **Verified**: `render_audit.py`/`fold_audit.py` both clean after
    both pieces; live panel exercised through the real `/api/press`
    input path (rotate ×4, left ×1) with zero errors and a real
    non-black rendered frame confirmed via direct pixel dump.

  **PART 3 — radar scope visual overhaul, 2026-08-02.** Real,
  heading-oriented aircraft icons replace the plain dot every scope
  target used to draw; the airport gets a runway glyph instead of a
  generic plus.

  - **Icon classification is real ADS-B data, not decoration.**
    `FlightEngine._ac_kind()` reads `category` (real ADS-B emitter
    category — added to `flights._fetch_positions()`'s output dict;
    it was already being read for `_notable()` but discarded before
    reaching the engine) to pick one of four shapes:
    - **Helicopter** (category A7) — a small rotor-disk cluster
      (screen-relative, NOT heading-oriented — a hovering rotor reads
      the same from any angle) plus a heading-oriented tail-boom stub.
      The one shape that owes nothing to the dart family, deliberately,
      so it reads unmistakably even at 1px.
    - **Airliner** (A3/A4/A5) — a wide heading-oriented dart.
    - **Business jet** — same dart, narrower spread, chosen ONLY when
      the aircraft's real ICAO type designator (`type`) matches
      `flights.BIZJET_TYPES`, a new reference table (Citation/
      Gulfstream/Learjet/Falcon/Phenom/Challenger/Hawker/PC-24 real
      designators). **Honest gap, stated in the table's own docstring**:
      unlike `ICAO_TYPE_NAMES` (populated from a real locally-observed
      sample), this list is built from general real-world type-
      designator knowledge — no bizjet happened to be in range during
      this build to confirm against. Business jets and light GA share
      the SAME emitter category (A1/A2), so `type` is the only real
      signal that can split them, and `type` isn't always broadcast —
      when it's missing or not a known bizjet code, it falls back to
      plain GA rather than guessing.
    - **GA** (A1/A2, not a known bizjet type) — a plain heading-oriented
      dash, no wings at all. Deliberately less ink, not a shrunk copy
      of the airliner dart — "small" reads as *less drawn*, not smaller
      copies of a bigger shape.
    - No/unknown category defaults to the airliner dart, not a guess —
    A3 (large) is the majority real category in this project's own
    213-aircraft sample, so it's the statistically honest fallback.
  - **`draw_scope_airport()`** — two end-cap ticks joined by a short
    bar, an "I"-shaped runway-strip glyph. **Deliberately NOT oriented
    to the airport's real runway heading**: `flights.load_airport()`/
    `location_config.json` store only lat/lon/name, no runway bearing.
    MYR's real runway is genuinely 18/36 (confirmed live in a captured
    transmission this same session — `"RUNWAY 18V ALPHA"`), but
    hardcoding that one real airport's heading would be silently WRONG
    the moment the configured home airport changes to a different one —
    a canonical vertical glyph is an honest generic "this is a runway"
    mark, not a claimed real bearing this project doesn't actually have
    on file.
  - **Honest constraint on how distinctly four shapes actually read at
    this pixel density**, stated in `draw_scope_aircraft()`'s own
    docstring rather than oversold: with up to 8 real aircraft inside a
    23px scope radius, the airliner/bizjet/GA family is a REAL,
    deliberate difference in the pixel offsets, but at 1:1 LED pixel
    scale it will often read as "a small pointed shape" to a human eye
    rather than unmistakably different classes. The helicopter and the
    plain GA dash are the two shapes that stay unmistakable regardless
    of scale (one has no heading-oriented wings at all, the other is a
    rotor disk, not a dart) — confirmed by rendering a synthetic frame
    with all four kinds present and saving it to PNG: the helicopter's
    rotor cluster and the airport's runway glyph are both instantly
    readable at a glance; the airliner/bizjet/GA distinction is real in
    the code but visually subtle.
  - **Verified**: `render_audit.py`'s instrumented `put_px` driven
    directly against synthetic-but-realistic aircraft covering all four
    icon kinds, a near-rim position (39.9nm, right at the edge of the
    23px radius), an unknown-heading helicopter, and every selection
    state (each aircraft in turn, the airport) — zero clipped pixels,
    zero exceptions. Full `render_audit.py`/`fold_audit.py` suites clean
    (0 modes failed, 0 feeds not folding). Live panel exercised through
    the real input path with a real non-black rendered frame confirmed
    by pixel dump — **no real traffic happened to be in range at check
    time**, so the icon SHAPES themselves were confirmed via the direct
    synthetic render + saved PNG, not the live hardware pass; the live
    check confirms the code path runs cleanly on real hardware, not what
    it looks like there. Worth a real visual spot-check next time actual
    live traffic of a mixed category is in range.


  **ICON/PERFORMANCE REVISIT, 2026-08-03.** Real user feedback after
  Part 3 shipped: reported lag/choppiness plus icons that looked
  "forced and over processed". The icon geometry was genuinely heavier
  than it needed to be (a 6-`draw_line`-per-aircraft top-down dart --
  thickened fuselage, swept wing lines, a tailplane) for what a few LED
  pixels can resolve; simplified to 2 strokes (one fuselage line, one
  wingspan line) per aircraft, and the DETAIL card's icon dropped a
  redundant parallel fuselage line added in the same pass. Real,
  measurable per-frame cost reduction, not just a visual simplification.
  Same session, further explicit feedback: **helicopters stay a fixed
  2D SIDE-VIEW sprite** (mirrored left/right by heading's sign, never
  rotated -- a side view can't face any other way), **everything else
  is TOP-DOWN**, rotated to real heading. `draw_scope_aircraft()` and
  `_draw_plane_icon()` (DETAIL card) both now branch on `kind` for
  this split, sharing the same simplified 2-stroke top-down geometry.
  - **Scope enlarged, own bigger radius**: `FlightEngine.FLT_CX/CY/R`
    (32, 32, 26) override the shared module-level `SCOPE_CX/CY/R`
    (which `SatelliteEngine`'s dome still uses untouched) via the
    `cx`/`cy`/`radius` params every `draw_scope_*` primitive already
    accepted -- no shared-primitive changes needed, just passing the
    override through every call site in `_frame_scope()`.
  - **Ring legend now prints MILES**, not nautical miles -- this
    project converts to imperial at the render layer everywhere else
    (`km_to_mi`/`kmh_to_mph`/`nm_to_mi`), the scope legend was the one
    holdout. The ring GEOMETRY itself is still computed from the real
    nm values (that's what `RADIUS_NM` and ADS-B `dist_nm` actually
    are); only the printed label changed, via `nm_to_mi()`, rounded, on
    one compact line so the bigger circle keeps its bottom margin.
  - Verified: zero clipped pixels confirmed directly via
    `render_audit.py`'s instrumented `put_px` against the same
    synthetic multi-kind aircraft set as Part 3 (including the near-rim
    case), plus a rendered PNG spot-check of the bigger scope with all
    icon kinds present. Full suite clean (0 modes failed).

  **DESTINATION LEGIBILITY, 2026-08-03.** Real user feedback: the
  DETAIL card's route line fell back to raw 3-letter airport codes
  ("RDU>LGA") too often, and a code isn't legible to someone who
  doesn't already know airport codes. Reordered the fallback chain to
  prioritize the DESTINATION city name, spelled out -- if the full
  origin+dest pair doesn't fit, the ORIGIN city is dropped first
  (`"> RALEIGH/DURHAM"`), never the destination; raw codes are now the
  true last resort, only when adsbdb gave no city name at all. Further
  feedback: rather than ever truncating a long destination name, it now
  **scrolls** (`draw_marquee`, same primitive news/ticker/gameday
  already use) when it doesn't fit at `WIDTH-4` -- `self.route_scroll`
  (deliberately NOT named `self.scroll`, which `Browsable`/other
  marquee modes already claim -- see this doc's own documented trap
  about that exact collision), reset to the start whenever the
  underlying text changes (a different aircraft, or a freshly-resolved
  route) so it never jumps mid-scroll. `render_audit.py`'s `MARQUEE_OK`
  exemption set gained `"flights"` for this -- same legitimate off-panel
  edge-drawing every other marquee mode is already exempted for.

  **THE HANGAR, 2026-08-03.** A NEW feature (not part of the Part
  1-3 overhaul) -- inspired by a competitor product (FlightPortrait): a
  persistent, accumulating collection of every distinct real aircraft
  this device has ever seen, by tail number, building up over time
  rather than only showing live/ambient traffic.
  - **Verified feasible BEFORE building, against real live data**: a
    real 238-aircraft ADS-B sample near ORD showed real registration
    (`"r"` field) present on 235/238 (98.7%), real type on 227/238
    (95.4%) -- reliable enough to build on. `hangar.py` (new module,
    same shape as every other feed/log store here) keys entries by
    REGISTRATION, not the ICAO24 hex -- a bare hex isn't the human-
    legible tail number this feature is about, and the ~1-2% of real
    aircraft with no broadcast registration are simply not recorded,
    an honest gap rather than an invented placeholder identity.
  - **Bounded from the start**, per the immediately-preceding audit's
    own lesson on unbounded resources: `HANGAR_MAX_ENTRIES = 500`,
    LRU-by-last-seen eviction, documented in `hangar.py`'s own
    docstring with the same rigor as `atc.py`'s `LOG_MAX_AGE_SECONDS`.
  - **Writer is `flights.py`'s own background poll thread** -- zero new
    network calls, zero new poll cadence, pure composition of data
    that cycle already fetched. A disk write only happens when
    something actually CHANGED (a new distinct aircraft, or a real
    update to a repeat visitor's type/airline/times_seen), not
    unconditionally every ~15s refresh -- most cycles see zero new
    distinct aircraft.
  - **A FOURTH view, deliberately NOT a fourth stop on the rotate
    cycle.** It's whole-device history, orthogonal to whatever's
    currently selected -- same relationship `SatelliteEngine`'s sky
    dome has to its pass list, so it reuses that exact idiom: up/down
    toggles it (`FlightEngine` is not `VERTICAL_BROWSE`, so the axis
    was unclaimed -- no new input plumbing needed). Paging reuses the
    same left/right `Browsable` machinery ATC log's pagination already
    established.
  - **Dispatches BEFORE the "no aircraft right now" idle check in
    `frame()`** -- deliberately. The collection has nothing to do with
    whether anything is in the sky this instant (arguably most useful
    exactly when it's empty), and letting an empty sky silently block
    a real, populated view would be exactly the "one system doesn't
    know about a state another just entered" bug class this doc names
    repeatedly. Checked directly, not folded into `has_content()`.
  - **Verified end-to-end against real live data, not synthesized**:
    ran the real fetch->record pipeline against a live ADS-B sample,
    confirmed registration/type/airline all populate correctly.
    `render_audit.py`'s instrumented `put_px` driven directly against
    synthetic edge cases (no type, no airline, a 3-day-old entry, an
    oversized registration/airline string) -- zero clipped/overflow/
    collision. New permanent `fold_audit.py` coverage for the `reg`
    field (verified directly via `fold_audit.check()`, since the
    script's own first live call -- ESPN -- is still 403ing as of this
    session, an external condition unrelated to this feature, already
    flagged and not worked around). **Live-verified on the real panel,
    not staged**: the live device's own background poller had already
    recorded 11 distinct real aircraft at MYR by the time of the check
    (including a real Robinson R44 helicopter and three real business
    jets -- Hawker, Learjet, Citation), toggle in/out and paging both
    confirmed through the real `/api/press` input path, and a
    `times_seen` count that genuinely incremented (5X->6X) BETWEEN two
    checks a few seconds apart, caught live by the device's own poller
    mid-verification -- not a fabricated before/after.

  **THE HANGAR gets a real sprite icon, 2026-08-07.** Until this
  session, every Hangar entry was text-only -- no aircraft icon, unlike
  the radar scope and the flight DETAIL card. This closes that gap,
  building on a design already researched and approved by the owner
  across three explicit decisions before any code was written:

  1. **Unknown/unmatched Hangar type codes silently render the generic
     GA icon** -- not a distinct "unknown" mark. Hangar's real
     population already skews GA/light, so this is the honest
     statistical default, matching the live scope's own existing
     "default to majority bucket when unclassifiable" convention
     (`FlightEngine._ac_kind()`'s own no-category fallback).
  2. **A NEW type→bucket lookup table, seeded ONLY from the 198 real
     type codes already in `hangar_log.jsonl` on disk** -- not general
     aviation knowledge. This closes the exact honesty gap
     `BIZJET_TYPES` admits to in its own docstring (built from general
     knowledge, never confirmed against a real local sighting).
     `flights.HANGAR_HELI_TYPES` (2 real codes: R44, AS65) and
     `flights.HANGAR_AIRLINER_TYPES` (real narrowbody/widebody/regional
     jets plus two large military transports -- C17, C30J -- which have
     no dedicated bucket in this project's deliberately-unexpanded
     4-bucket set but are honestly closer to "large heavy transport"
     than GA) are new. Five real bizjet codes not previously in
     `BIZJET_TYPES` were folded into that existing table instead of a
     separate one (GLEX, FA10, HDJT, E35L, E545 -- Bombardier Global
     Express, Falcon 10, HondaJet, Embraer Legacy 600/450, all confirmed
     against real Hangar registrations). **Two real seen codes were
     deliberately left unmapped** because neither could be positively
     identified: `GA6C` (no confident real-world match) and `HUNT` (a
     real Hawker Hunter jet warbird, but fits none of the four buckets
     honestly) -- both correctly fall through to GA per decision #1
     rather than being guessed into a wrong bucket.
  3. **The sprite reuses the flight DETAIL card's 3-stroke budget**
     (`FlightEngine._draw_plane_icon()`, unmodified), not the radar
     scope's tighter 2-stroke one -- safe because the Hangar is browsed
     one aircraft at a time (`_frame_hangar()` already only ever draws
     the single entry at `hangar_idx`), the identical "only one sprite
     draws per frame" reasoning that already lets the DETAIL card be
     thicker than the scope. `FlightEngine._hangar_kind(type_code)` is
     the new classifier (parallel to `_ac_kind()`, but keyed on the
     real ICAO `type` string alone -- a persisted Hangar entry has no
     live ADS-B `category` field to read). The sprite is drawn with
     `heading_deg=None`, reusing `_draw_plane_icon()`'s EXISTING fixed
     "up" orientation + dim uncertainty ring (confirmed present and
     already used by the DETAIL card for heading-unknown aircraft
     before relying on it here -- no new static-bitmap code path was
     written).
  - **Layout**: the icon sits at `(WIDTH//2, 19)` between the header and
    the existing text rows, which shifted down (type 19→31, airline
    27→38, seen 40→45, age 52→52 unchanged) to make room without
    colliding with the icon's ~9px reach.
  - **Radar scope (`draw_scope_aircraft()`) and the flight DETAIL card's
    OWN call site (`_frame_detail`'s heading-oriented use) are
    completely unmodified** -- this only adds a new call site inside
    `_frame_hangar()`.
  - **Verified**: `render_audit.py`/`fold_audit.py` both clean before
    and after (0 mode failures, 0 unfolded feeds). Direct pass over all
    223 real Hangar entries currently on disk (`hangar.LOG.get()`) with
    `render_audit.Audit`'s own instrumentation: 0 collisions, 0 clipped
    pixels, 0 overflow, 0 dropped glyphs. Real classification spot-check
    against named real entries: `N293NV`/A320 → AIRLINER, `N3055Y`/R44 →
    HELI, `N803SD`/C25B → BIZJET, `N434CD`/SR22 → GA, `N641BW`/GA6C →
    GA (unmatched fallback), `N346AX`/HUNT → GA (unmatched fallback).
    Bucket distribution across the real live collection: 136 AIRLINER,
    47 GA, 35 BIZJET, 5 HELI. **Live-verified on the real physical
    panel**: `com.henderburgh.arcade` restarted to pick up the code,
    switched to `flights` mode and paged through the Hangar view via
    the real `/api/press/up` and `/api/press/right` input path, and a
    direct `/api/frame` pixel dump confirmed a real drawn sprite (a
    fuselage line, wingspan line, tailplane, and the dim heading-unknown
    ring) at two different real Hangar entries, not just header/text --
    e.g. one dump showed the ring/cross pattern centered at
    `(32, 19)` exactly matching `_draw_plane_icon()`'s expected reach,
    with 533-607 real non-black pixels on frame, zero errors. Panel
    restored to `ambient` afterward.

  **PERFORMANCE — measured before building, not assumed:**
  | workload | cost | vs 50ms frame budget |
  |---|---|---|
  | `skypass.sky_now()` — all 157 objects, ONE instant | **0.32 ms** | 0.6% |
  | scope render (rings + sweep + targets) | **0.60 ms** | 1.2% |
  | existing `predict()` — 36h pass scan | 1488 ms | background, 15-min timer |

  All 157 TLEs propagate cleanly (0 failures). **`sky_now()` and
  `predict()` are completely different workloads and must not be confused
  when judging whether live tracking is affordable**: `predict()` scans 36
  hours at 30s steps (~678,000 propagations), `sky_now()` does one per
  satellite — a 4,651× difference. Continuous all-object tracking is
  cheap; pass prediction is what is expensive.
  - **Cadence is decoupled from the frame rate on purpose**
    (`SKY_NOW_REFRESH = 1.0s`, computed on the FEED's background thread,
    never in the engine). Measured worst-case apparent motion of a real
    visible object is **0.13 px/sec** on the 64px dome, so even a 5s
    cadence stays sub-pixel — recomputing every frame would be 20× the
    work for a result nobody can see. The feed loop's sleep dropped 5s → 1s
    to match; the expensive work is gated by its own timers (`TLE_REFRESH`
    12h, `PASS_REFRESH` 15min) so it does **not** run any more often.
  - The sweep angle advances per-frame (one float add) so the scope stays
    live-looking even while the underlying positions update on the slower
    cadence.

  **Verified on the real panel** by direct pixel dump of the actual sent
  frame (not a comparison against a separately-polled instance): flights
  scope with 8 real aircraft + the MYR marker, sky dome with 7 real
  catalogued objects, and the up/down toggle confirmed returning to the
  untouched UPCOMING view. Zero errors, zero loop errors in both.

  **Flight phase — CLIMB / DESCEND / CRUISE** (`flights._phase()`,
  2026-08-01). Verified against real ORD traffic (MYR had zero aircraft
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
  - **Partial live check 2026-08-01**: one real aircraft (VIR74W,
    39,000ft) appeared near MYR at session start — correctly classified
    CRUISE (altitude alone, per the FAA floor rule) with no arrow drawn,
    confirmed on the real panel (score=1, zero errors). Still an honest
    gap: no CLIMB or DESCEND case has occurred at MYR itself yet, so the
    arrow's real-panel appearance remains verified only against captured
    ORD data, not local traffic. The LOW+phase escalation is likewise
    still unverified live. Check again next session.

  **WINDOW FILTER (2026-08-07)** — prioritize/flag aircraft currently
  visible out one specific real window overlooking the golf course,
  measured with a phone compass held at the window: centre bearing
  296°, 80° total FOV (256°→336°).

  - **Confirmed field, not assumed**: the bearing every scope projection
    in this project already uses (`scope_xy(brg, ...)`, the compass tag
    on the DETAIL card) is `dir_deg` — sourced from ADS-B's `dir` field
    at `flights._fetch_positions()`, the real bearing FROM home TO the
    aircraft. This is NOT `track_deg` (the aircraft's own heading,
    ADS-B's `track` field, used only for rotating the icon). Checked by
    grepping every existing `dir_deg` use in `engines.py` before writing
    any window code, per the owner's explicit instruction to confirm
    rather than assume.
  - **Config lives in the shared `location_config.json`**, not a new
    file — a window's bearing is a LOCATION fact tied to where the
    panel/owner is, the identical reasoning that already put `airport`
    there. `flights.load_window()`/`save_window()` mirror
    `load_airport()`/`save_airport()` exactly, including the
    merge-preserve write (verified directly: `save_window(296, 80)`
    followed by reading the file back showed `lat`/`lon`/`label`/
    `airport` all untouched, `window` added as a sibling key — the same
    check this file's own recurring-bug note demanded before touching
    this config a third time). Seeded with the real measured values
    (296°/80°) rather than left to a code default nobody has to look up.
  - **Angular test is the given formula, unmodified**:
    `flights.in_window()` — `abs(((dir_deg - center + 180) % 360) - 180)
    <= fov/2`.
  - **Soft-priority boost, additive on top of the existing notable+
    distance ranking, not a replacement.** `WINDOW_BOOST = 0.5`, added
    to an aircraft's notable rank before sorting. Justified by the
    ranking's own shape rather than picked arbitrarily: `_notable()`
    ranks are integers spaced **at least 1 apart** (0 routine, 2/3/4/5
    for real criteria), so a 0.5 boost can never cross a rank tier
    boundary — it can only break ties within one. Verified against two
    independent real live `flights.FEED.get()` snapshots on 2026-08-07
    (window = 256°–336°); the second one (VIR36VL notable-not-window
    rank 3.0, TIV685 notable-not-window rank 2.0, N610CT/FFT1785/JBU483
    window-routine rank 0.5, N157JR/SWA4100/N773TA routine-not-window
    rank 0.0) rendered in exactly that order, with N157JR — closer at
    8.8nm — correctly ranking BELOW the window-boosted aircraft despite
    being nearer, while TIV685 (a real notable aircraft, not even in the
    window) still outranked every window-boosted routine aircraft. Full
    reasoning and both snapshots are recorded as a comment on
    `WINDOW_BOOST` in `flights.py`, not just here.
  - **`in_window` is a real boolean on every aircraft dict**, computed
    in `flights.py` (zero I/O added to `FlightEngine` — the window
    config read happens on the feed's own ~15s refresh cadence, the same
    cost class as `satellite.py`'s own periodic config re-check).
  - **Visual treatment shipped, deliberately simple**:
    `engines.draw_window_ring()` draws a small 4-point diamond ring
    (violet, `FlightEngine.WINDOW_RING`, chosen distinct from every
    `ALT_BANDS` icon color, `ATC_MATCH` green, and the `(255,255,255)`
    "selected" white) under any in-window aircraft's scope icon. A
    fixed-radius ring rather than a distance/heading-oriented design —
    at this pixel density a bigger design system would be guessing at
    something the owner hasn't asked for; flagged as the ceiling of
    "simple" rather than silently expanded.
  - **Verified**: `render_audit.py flights` and `render_audit.py flights
    --strict` both clean (0 CLIPPED/DROPPED/COLLISION; the only
    TRUNCATED warnings are pre-existing and unrelated to this change).
    `fold_audit.py` clean (0 feeds not folding — this feature adds no
    new externally-sourced text). `FlightEngine` driven directly against
    real live `flights.FEED` data for 40 real ticks with zero exceptions
    and a non-black rendered frame, confirming `in_window` flows end to
    end onto real aircraft. **Not yet verified on the real physical
    panel** — this was built in an isolated git worktree and the live
    `com.henderburgh.arcade` launchd service runs the separate main
    checkout, which this worktree cannot deploy into or restart; the
    real-panel pixel-dump check (`/api/frame`) is the next step once
    this merges to main.
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

**Satellite modes — UNIFIED 2026-08-01** (`satellite.py` + `skypass.py` /
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
**Confirmed against real data 2026-08-01, and it changes real behaviour
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
- **OVERHEAD-NOW verified live 2026-08-01** — a real pass (multiple
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

### Per-sport MAIN renderers (IN PROGRESS — started 2026-08-01)

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
additive and cannot regress another sport. **This is a separate dispatch
table from `SPORT_DETAIL_RENDERERS` below** — a sport can have a main
renderer, a detail renderer, both, or neither; don't conflate the two
when adding one.

| sport | status |
|---|---|
| baseball | **done** — diamond/outs/count/half-inning on the main row |
| mma | **done** — weight class primary, records per fighter, card position |
| football | not started — **NFL/NCAAF are off-season, no live data to verify against** |
| basketball | not started — NBA off-season; verify against **WNBA**. Still no live WNBA game as of 2026-08-01 (checked again — both games each day so far have already finished by the time this was checked) |
| soccer | **done** — form strings, ESPN-formatted clock, penalty shootouts (verified live). Layout uses a y-cursor after the audit caught the divider/clock overlapping the second team |
| tennis | not started — still no live match as of 2026-08-01. **Confirmed real**: header events carry `linescores`, one entry per SET with `value`/`displayValue`/`period`/`winner` (checked against a real finished match, J. Pegula d. D. Shnaider 7-5 6-4) — this is the field the set-by-set grid needs, but it has not been rendered against a LIVE match, only a finished one glimpsed while checking the shape |
| golf | **done** — 6-row leaderboard, movement arrows (sign verified: negative = climbed the leaderboard) |

### Big-moment celebration — SHARED INFRASTRUCTURE done, per-sport detection IN PROGRESS (started 2026-08-02)

**What**: a full-panel graphic that interrupts `ambient` when something
genuinely notable happens in a live game the sports engine is tracking —
home run, goal, MMA finish, buzzer-beater, etc. Built as two pieces,
deliberately in this order, because every per-sport detector plugs into
the same graphic and interrupt mechanism — building those twice per sport
in parallel would have been exactly the kind of collision the shared-first
ordering exists to avoid.

**1. `draw_celebration()` (`engines.py`, module level, next to
`draw_alert_frame`)** — original design, not a broadcast recreation: an
impact-frame white flash on the first two ticks, rotating sunburst rays,
three staggered expanding rings, and a centered text block (kind/line1/
line2) on a dimmed plate for legibility. No league logo, no referee
signal, nothing trademarked. `CELEBRATION_TICKS = 90` (~4.5s at ambient's
0.05s tick rate). Verified by rendering frames to PNG at t=1/20/45/89 and
by running it through `render_audit.py`'s instrumented `put_px`/
`draw_text3x5` directly (0 dropped chars, 0 overflow, decorative ring/ray
overshoot off-panel is expected and harmless — same class of exemption as
marquee edge-drawing).

**2. The contract every sport plugs into** — `pop_big_moment()` /
`_set_big_moment()` on `SportsEngine`, a one-slot queue (a second moment
before the first is popped overwrites it rather than queuing — only the
most recent real event matters by the time anyone reads it). A moment is
`{"kind", "line1", "line2", "color"}`, all three text fields already
`paneltext.panel_text()`-folded by whichever per-sport detector calls
`_set_big_moment()`. **Detection itself is deliberately NOT part of this
shared layer** — same split as `SPORT_RENDERERS`: a sport opts in from
inside its own tick/parse path, an unclaimed sport contributes nothing.

**3. `AmbientEngine`'s interrupt** — checked every tick regardless of
which sub-mode is showing (a home run interrupts the news ticker exactly
as readily as a quiet sports screen). Severe weather still outranks it
for free: `arcade_server` composites the global alert takeover *after*
`AmbientEngine.frame()` runs, so an alert covers a celebration exactly
like it covers anything else — no new precedence code needed. Dwell
timing **pauses** during a celebration (`self.hold` does not advance)
so it can't eat into whichever sub-mode's turn was in progress. **Does
NOT fire for manual sports browsing** — `draw_celebration()` is only
ever called from `AmbientEngine.frame()`, never from `SportsEngine`
itself; confirmed by grep, not just intent.

Verified: fires once on `_set_big_moment()`, `hold` stays paused for the
full 90-tick hold, clears itself and resumes normal dwell after, second
call after consumption returns `None` (no double-fire). Both
`render_audit.py` and `fold_audit.py` re-run clean across the whole
project after this change (0 modes failed, 0 feeds not folding).

**Not yet live-verified against real hardware, deliberately**: there is
no real detector wired in yet, so pushing a fabricated celebration to the
live panel would be exactly the kind of synthesized-data check the
project's "never invent" rule exists to prevent. The first real per-sport
trigger (MLB, most likely) is what proves this live, not a synthetic
push — verify it then, not now.

**All five per-sport detectors done (2026-08-02), built in parallel then
merged.** Real infrastructure lesson worth keeping: the parallel-agent
worktrees turned out to share ONE physical directory rather than being
truly isolated, discovered when the first four agents correctly refused
to build against missing shared infrastructure that had only been
committed locally, not yet pushed to `origin`. After pushing, all five
correctly avoided destructive git commands and isolated their own diffs
from each other's uncommitted work when they found it — but one commit
(WNBA) still got silently dropped mid-chain by another agent's
from-scratch file reconstruction technique, caught only by diffing the
final merged state's `BIG_MOMENT_DETECTORS` registry against the full set
of five expected keys, not by trusting any individual agent's "done and
committed" report. Recovered with `git cherry-pick` (not a hand-applied
patch — that approach silently dropped a different sport's code on the
first attempt) and reconciled onto `main`. **Lesson for next time: verify
a merged multi-agent result by checking the actual combined artifact
(here, the dispatch dict's keys) against the expected count, not by
summing individual completion reports.**

Golf's `golf_player` is currently pinned to **Yealimi Noh** (real current
leader, -7, Round 4 of the AIG Women's Open in progress as of this
write-up) specifically to give the golf detector a real live game to fire
against — Scottie Scheffler, the previous pin, wasn't in any live
tournament. Left in place rather than reverted; the first real EAGLE/
BIRDIE/LEAD move is what proves the full pipeline live, same as MLB/
soccer/WNBA still need their own first live game.

**Per-sport detectors, in the audited priority order (MLB → soccer →
WNBA → golf → MMA; tennis/football blocked, no data)** — see the
sports-coverage section above for what each sport's live payload actually
exposes (MLB's `scoringPlay`+`alternativeType.text`, soccer's
`keyEvents`, WNBA's `plays[]`, golf's existing `golfer_move()`, MMA's
type-ID reconstruction). Cost discipline: any new per-game polling for
scoring-play detail must reuse the SAME narrow scope win-probability
already uses — only the pinned favorite's own live game, never every game
in a league — or it reopens the ESPN request-volume risk this project has
already had to mitigate twice.

- **soccer — done (2026-08-02).** `sports.fetch_new_soccer_goals(league,
  event_id, seen)` polls one soccer match's `keyEvents` (the per-game
  summary endpoint, same one `_fetch_win_prob()` already uses) and
  returns newly-seen goals; `SportsEngine._detect_soccer_goal()` calls it
  ONLY for the pinned favorite's own game and ONLY while `state == "in"`
  — never for every soccer match in the universal feed. A goal is any
  keyEvent with `scoringPlay: True`, not a `type.text` whitelist —
  verified live against a real 2026-08-02 MLS/NWSL slate (8 real
  matches, 30+ real goals, zero false positives among `scoringPlay=True`
  entries); ESPN's own `type.text` varies with HOW it was scored, real
  values seen: `"Goal"`, `"Goal - Header"`, `"Goal - Volley"`,
  `"Goal - Free-kick"`, `"Penalty - Scored"`, `"Own Goal"` — a whitelist
  would need updating for every finishing variant ESPN adds, `scoringPlay`
  doesn't. Each `keyEvent` DOES carry a stable string `id` (confirmed
  live, unlike MLB's `plays`), used directly as the per-game dedupe key
  — same per-game "seen" idiom `GameDayEngine._seen_done` uses for MMA
  finishes: adopt whatever's already there on the first read for a given
  game, don't replay it, then only report what's genuinely new; the seen
  set resets whenever the watched `event_id` changes. Text is folded with
  `paneltext.panel_text()` inside `sports.py` at the I/O boundary, not in
  `engines.py`. Verified against real data end-to-end, not synthetic: ran
  `fetch_new_soccer_goals("MLS_TEST", "761697", seen)` (a test-only
  `LEAGUE_PATHS` entry pointed at `soccer/usa.1`) against the real CF
  Montreal 2–2 New England Revolution match already used to prove the
  shared infrastructure — it correctly returned exactly the 4 real goals
  (Carles Gil's penalty, Dor Turgeman, Brayan Vera, Prince Owusu's
  header) in order, and a second call against the same `seen` set
  correctly returned zero (dedupe holds). `render_audit.py sports` and
  `fold_audit.py` both re-run clean after this change (0 modes failed, 0
  feeds not folding). Not yet fired against a real live pinned-favorite
  soccer game on the actual panel — that needs a live MLS/EPL game with
  the owner's team pinned and live, which wasn't available at build
  time; the parsing/dedupe/scope logic above is what's verified, the
  full on-panel celebration trigger is the next thing to confirm live.

**MLB home run — done (2026-08-02).** First real detector plugged into
the seam, `sports._fetch_home_run_plays(league, event_id)` +
`SportsEngine._detect_mlb_home_run()` (registered as
`BIG_MOMENT_DETECTORS["mlb_hr"]`).

- **Scope matches `_fetch_win_prob()` exactly**: only fetched when
  `self.data["favorite"]["league"] == "MLB"` and
  `self.data["favorite_game"]["state"] == "in"` — never polled for the
  whole universal feed or any non-favorite game. Reuses the same
  per-game `summary?event=ID` endpoint `_fetch_win_prob()` already calls
  (no new endpoint), so this adds zero request volume beyond what a live
  favorite game already costs when win probability is showing.
- **Payload facts, confirmed live** (not assumed): each scoring play
  carries `scoringPlay: bool` and `alternativeType.text`; a real home run
  reads the literal string `"Home Run"` (e.g. `"Sanoja homered to left
  (377 feet), Stowers scored."`). Other real scoring plays seen the same
  day — `"Single"`, `"Double"`, `"Sacrifice Fly"` — are correctly
  excluded; only `"Home Run"` fires the celebration.
- **Folding happens in `sports.py`, not `engines.py`** — same discipline
  as every other feed: `_fetch_home_run_plays()` runs
  `paneltext.panel_text()` on the play text before returning it, and
  `engines.py` never calls `panel_text()` directly (confirmed by grep —
  it has no `paneltext` import at all).
- **Seen-play tracking is `SportsEngine._seen_home_runs`**, same one-shot
  idiom as `GameDayEngine._seen_done` for MMA finishes: `None` until the
  first read, which *adopts* the current set of home-run play ids without
  firing (a game already 3 home runs deep when the mode opens must not
  replay them), then only ids not already in the set are new.
- **Text**: line1 = `"{AWAY} {away_score}, {HOME} {home_score}"` built
  from already-folded team abbreviations and int scores (both ASCII-safe,
  no fold needed at the join site); line2 = the real ESPN play text
  (already folded, e.g. `"TAYLOR HOMERED TO LEFT (410 FEET), SEMIEN
  SCORED."`), left to `draw_celebration()`'s own `fit_text()` if it runs
  long rather than hand-truncated here. Color = the home team's real
  ESPN color (`fg["home"]["color"]`), falling back to away, then the
  shared neutral gold.
- **Verified against real data, not synthesized**: `_fetch_home_run_plays`
  run directly against 12 real finished 2026-08-01 MLB games (MIA@NYM,
  MIN@SEA, PIT@CIN, TEX@HOU, ARI@CLE, WSH@ATL, NYY@CHC, KC@COL, SF@SD,
  BOS@LAD, MIL@LAA, DET@ATH) — found 35 real home runs total, all
  correctly filtered from the surrounding non-HR scoring plays (singles,
  doubles, sac flies) and all folded clean. Then `SportsEngine` was
  constructed directly (no `arcade_server`, no panel) with
  `favorite_game` pointed at MIA@NYM (`event_id` `401816349`,
  `state: "in"`) and driven through three ticks: first tick adopts the 3
  existing home runs as baseline with no fire; second tick (unchanged
  data) does not re-fire; a simulated new home run (one id removed from
  `_seen_home_runs`) fires exactly once with the real folded play text
  and a correct score line, and popping a second time returns `None`
  (no double-fire). `render_audit.py sports` and `fold_audit.py` both
  clean (0 failed / 0 not-folding) after this change.
- **No live game to verify against at build time** — today's slate
  (2026-08-02) was still all `pre` when this was built, so the one-shot
  logic above was proven against real finished-game data plus a
  hand-driven engine, not a genuinely live favorite game. Worth a real
  end-to-end check next time a favorite MLB game is actually live.

**Golf — done (2026-08-02).** The smallest of the five: `golfer_move()`
already runs in the feed and `tick()` already reads its result into
`self.golf_move` every poll, right next to `golf_pulse.note()` which
drives the existing quiet flash. `_detect_golf_big_moment()` (registered
as `BIG_MOMENT_DETECTORS["golf_move"]`) reads that same field — no new
ESPN parsing, no new polling.

- **Judgment call: only EAGLE/BIRDIE/LEAD fire the big celebration.**
  `golfer_move()` can also return BOGEY or LOST LEAD, and both are
  deliberately excluded — a full-panel celebratory burst over a bogey or
  a lost lead would be tonally backwards (there's nothing to celebrate),
  and the existing `Pulse` flash already surfaces those two
  appropriately without implying good news. Same "no badge for the
  negative/mundane case" reasoning already used elsewhere (flight phase
  CRUISE, routine golf holes not flashing at all).
- **One-shot firing uses its own tracking variable**
  (`self._last_golf_big_moment`), not `golf_pulse` itself — `golf_pulse`
  is a `Pulse` instance whose `.t`/`.on` timing already belongs to the
  quiet flash; re-keying it here for a second purpose would make the two
  features silently fight over the same flash clock. The idiom (compare
  against the last-seen value, act only on a real change) is copied from
  `golf_pulse.note()` one line above it in `tick()`, just with an
  independent piece of state. This matters because `GOLF_MOVE_TTL` (20s
  in `sports.py`) means the same move value is read from the feed for
  many ticks in a row — firing on every tick instead of once would leave
  the celebration graphic stuck on screen for as long as the feed kept
  reporting the move.
- Text: line1 is the pinned golfer's name (`golf_pinned["abbr"]` or
  `["full"]`, already `panel_text()`-folded upstream — confirmed via
  `_frame_golf_pinned()`, which draws it unfolded a second time), line2
  is the move kind plus current score-to-par (e.g. `"EAGLE -10"`).
- Color: neutral warm gold `(255, 200, 40)` — golf has no team color,
  same fallback `draw_celebration()`'s own docstring names for golf/MMA.
- **Verified**: `render_audit.py sports` and `fold_audit.py` both clean
  (0 failed / 0 not-folding) after this change. One-shot logic was
  unit-tested directly (not via a fabricated live panel push) by calling
  `_detect_golf_big_moment()` repeatedly with `self.golf_move` set to
  every value the feed is documented to produce (`golfer_move()`'s own
  docstring: EAGLE/BIRDIE/LEAD/BOGEY/LOST LEAD/None) — confirmed it
  fires exactly once per new move, does not re-fire while the feed keeps
  reporting the same move, and BOGEY/LOST LEAD never fire regardless of
  repetition.

**MMA finish — built, UNVERIFIED against live data (2026-08-02).**
`sports._fetch_mma_finish_method(league_slug, event_id)` +
`SportsEngine._detect_mma_finish()` (registered as
`BIG_MOMENT_DETECTORS["mma_finish"]`).

- **Do not confuse this with GameDayEngine's existing `_finish_round`/
  `_seen_done` mechanism.** That is a different, older, already-working
  system driving GAME DAY's dedicated UFC-card RESULT takeover from
  `mma.FEED` (the dedicated card feed). This is the new, separate thing:
  firing the shared `draw_celebration()` graphic while `ambient` is
  showing, off whatever MMA/PFL event `sports.FEED.get_universal()`
  happens to surface -- a completely different feed pathway from
  `mma.FEED`, confirmed non-interchangeable in an earlier session.
  Neither `GameDayEngine` nor `mma.py`'s existing finish handling was
  touched.
- **Reuses the already-verified type-ID facts from mma.py** (20 =
  submission, 21 = KO/TKO, 22 = decision -- `mma.METHOD_BY_ID`/
  `mma.METHOD_BY_TEXT`), not re-derived. One-shot per event id, same
  idiom as `GameDayEngine._seen_done`/`_seen_home_runs`: the first read
  adopts whatever is already `state == "post"` without firing, only a
  newly-post id fires.
- **Real, current data gap, honestly documented, not worked around**: as
  of this build `sports.FEED.get_universal()` has ZERO mma/PFL events
  (checked live), and `mma.FEED` (GAME DAY's own dedicated card feed)
  also has no next card. Per this project's "never invent" rule, no
  synthetic event was fabricated to force a test.
- **Two specific open unknowns in `_fetch_mma_finish_method()`, both
  genuine blockers, not guessed past**: (1) whether a per-event summary
  endpoint (`.../mma/{slug}/summary?event=ID`, same URL shape
  `SUMMARY_URL` already uses for every team sport) even exists for a
  universal-feed event id -- mma.py's own docstring already established
  the BARE `.../mma/ufc/summary` (no event id) 404s, but the per-event
  form was never tested because no live/recent MMA event exists to build
  the URL from; (2) the league slug used to build that URL is a guess --
  the universal header event only exposes the already-uppercased,
  `panel_text()`-folded league display name ("UFC", "PFL"), not ESPN's
  raw path slug, so this lowercases the display name rather than the
  real slug (`"ufc"`) `mma.py`'s own `SCOREBOARD_URL` is built on.
- **Degrades honestly if the fetch fails**: `_fetch_mma_finish_method()`
  returns `None` on absolutely anything unexpected (wrong shape, no
  `details`/`plays` list, 404, timeout, malformed json) rather than a
  guessed method, and the detector still fires the celebration with
  `kind = "RESULT"` in that case -- a fight ending is real and worth
  celebrating even when the HOW is unknown, same "degrade one field at a
  time" discipline as every other feed here.
- **Cost discipline**: the fetch only ever runs once per newly-observed
  finish (a one-shot transition, not continuous polling), matching the
  narrow per-event scope `mlb_hr`/soccer-goal detectors already use.
- **Verified**: `render_audit.py sports` -- 0 modes failed.
  `fold_audit.py` -- 0 feeds not folding. Confirmed the detector is a
  true no-op against REAL current data: ticked a real `SportsEngine`
  against the live universal feed (45 real events, zero MMA/PFL among
  them), `pop_big_moment()` returned `None`, `_seen_mma_done` correctly
  adopted an empty set, zero exceptions.
- **NOT verified end-to-end against a real finish, and cannot be from
  this session** -- no live/recent MMA event exists in either feed. The
  code is ready and additive; the first real UFC/PFL finish the
  universal feed ever surfaces is what proves this live, not a synthetic
  push.

**WNBA big-moment detection — REMOVED (2026-08-07), owner preference, not
a bug.** A WNBA big-play detector was built 2026-08-02 (per-game
play-by-play polling, a late-clock threshold calibrated against two real
finished games, one-shot seen/new tracking) but was never reachable in
practice, since WNBA was never added to `LEAGUE_PATHS`, so it could never
be set as the pinned favorite team. The owner does not want WNBA
specifically covered by this project; rather than build a WNBA-specific
replacement, all of that detector's plumbing was deleted outright:
`sports.py`'s `WNBA` entry in `LEAGUE_PATHS`, `_WNBA_PATH`,
`WNBA_BIG_PLAY_CLOCK_SECONDS`, `_clock_seconds()`,
`_fetch_wnba_big_plays()`, `SportsFeed._refresh_wnba_big_play()` and its
call site in `_loop()`, the `_wnba_*` instance fields, and
`pop_wnba_big_play()`; and `engines.py`'s `_detect_wnba_big_play` plus its
`BIG_MOMENT_DETECTORS["wnba_big_play"]` registration. `render_audit.py`
and a direct `import sports, engines` both stayed clean after the
removal — no dead references remain.

The owner's actual ask is broader coverage of tennis, golf, baseball,
NFL, NHL, and other prime-time sports through the existing pinnable-
favorite/big-moment system, not a WNBA substitute. Of those: golf and
baseball already have their own big-moment detectors (see above); NFL
and NHL are already pinnable via `LEAGUE_PATHS`/`DEFAULT_LEAGUES`, just
without a dedicated big-moment detector yet (not built here — a real,
separate piece of work, not in scope for this removal); a real tennis
renderer remains explicitly deferred, tracked separately (task #19).

### The celebration system GENERALIZED across all four modes (2026-08-07)

**Why**: sports had a genuinely sophisticated interrupt (five detectors,
priority-ordered under severe weather). Flights and satellite had
nothing — a MAYDAY squawk and a routine helicopter got the identical
quiet notable-tag treatment, and a GO-OUTSIDE-grade satellite pass
couldn't interrupt ambient at all despite being the entire reason that
mode exists. Named as the real inconsistency in a full four-system deep
dive, then fixed deliberately, design-first (the owner asked for the
tier scheme and per-system visuals before any code, specifically
because the earlier ambient "channel ident" experiment had shipped,
been wrong, and been reverted — see the ambient section above).

**ONE mechanism, not four.** `BigMomentSource` (`engines.py`, module
level, next to `draw_celebration`) is mixed into `SportsEngine`,
`FlightEngine`, and `SatelliteEngine`. Sports' own private
`pop_big_moment()`/`_set_big_moment()` were deleted and replaced by the
shared version — same adopt-then-diff detector idiom every existing
sports detector already used, not a second mechanism.

**THREE intensity tiers** — the actual missing piece, not the queue
plumbing:
- `TIER_FLASH` — does **not** reach `AmbientEngine` at all. Routed to
  its own `_flash` field, drawn as a compact in-mode banner
  (`draw_flash_banner`) only by the mode that fired it. Load-bearing:
  without it, every candidate is forced into interrupt-or-nothing,
  which is exactly the pressure that inflates a top tier until it means
  nothing. With it, the project can be generous about noticing and
  stingy about interrupting.
- `TIER_INTERRUPT` — full-panel, `CELEBRATION_TICKS` (90, ~4.5s).
  Exactly what every sports detector already did — zero regression.
- `TIER_TAKEOVER` — full-panel, 120 ticks (~6s), and the **only** tier
  allowed to pre-empt a celebration already playing. Reserved for
  genuinely rare "go look now" events. Deliberately three tiers, not
  four — a fourth invites everything to settle into the middle.

**One renderer, one text hierarchy, one set of timing beats — only the
BACKDROP varies per system** (`CELEBRATION_BACKDROPS`), reusing each
mode's own existing visual language rather than inventing a fourth
product: sports keeps its original ring/ray burst unchanged
(`_backdrop_sports`); flights gets a full-panel radar-sweep wedge
(`_backdrop_flights`, faster than the scope's own idle sweep — this is
an event, not ambience); satellite gets a horizon-to-horizon arc with a
travelling marker (`_backdrop_satellite`, reusing `_draw_pass_arc`'s own
language). Color stays owned by the event (a real team color, a real
urgency color), not the system — the backdrop alone is what makes a
MAYDAY and a home run obviously belong to different things while still
obviously being the same device speaking.

**Queue correctness across four systems — two real design bugs fixed
before they could ship, not two bugs found after:**
- `AmbientEngine.tick()` now **peeks** every engine's pending moment
  (`peek_big_moment()`, non-consuming) and selects the **highest tier**,
  not first-found-in-dict-order — the old code's `for e in
  self.engines.values(): ... break` meant a trivial moment could
  pre-empt a critical one purely by iteration position. The loser stays
  queued in its own engine (not popped, not discarded) rather than
  being silently lost.
- `_set_big_moment()`'s overwrite is now **tier-gated**: a lesser moment
  arriving while a bigger one is still pending in the same engine's
  slot is dropped, not applied. `TIER_TAKEOVER` may pre-empt an
  in-flight celebration; `TIER_INTERRUPT`-vs-`TIER_INTERRUPT` and
  `TIER_TAKEOVER`-vs-`TIER_TAKEOVER` both keep the original
  never-interrupt-what's-already-playing rule.

**A real pre-existing bug found in the process, unrelated to anything
being added.** `render_audit.py`'s instrumented `put_px` had never
actually been run against `draw_celebration()` before — nothing in the
normal audit sweep forces a celebration to fire, so this code path had
zero coverage since the feature shipped. The **original** ring-radius
math (`((t * 1.6 + k * 11) % 34) + 3`, unchanged by anything in this
generalization) reached ~37px from a (32, 30) centre — well past the
real panel edge in every direction, on every single sports celebration
that has ever fired. Fixed with `_MAX_BURST_R`, computed from the real
panel bounds rather than a guessed constant; verified 0 clipped pixels
across all 3 backdrops × both interrupt tiers × every tick of their
hold, via `render_audit.Audit`'s own instrumentation run directly
against `draw_celebration()`.

**Flights detectors (3)**, keyed off `flights._notable()`'s own real
classification — one source of truth, not re-derived:
- **Emergency squawk** (rank 5 — HIJACK/NORADIO/MAYDAY from a real
  ADS-B squawk code) → `TIER_TAKEOVER`. The rarest, least ambiguous
  "look up" this mode can produce. **Cannot be fabricated to test** — a
  real emergency squawk can't be manufactured on demand — so this is
  honestly flagged as never-fired-live, same treatment `mma_finish`
  already established; verified instead via the detector's own logic
  against a real-shaped payload.
- **Airship** (category B2 — confirmed exactly ONE real instance in
  this project's own 213-aircraft sample) → `TIER_INTERRUPT`.
  Deliberately NOT promoted: helicopters (6/213), heavies (11/213), or
  any phase-transition state — all common enough near any airport that
  interrupting on them would stop being special within days.
- **First-ever aircraft TYPE, via THE HANGAR** → `TIER_FLASH`.
  Deliberately keyed on `type`, not registration — 11 distinct
  registrations were logged in the Hangar's first few real minutes of
  operation, which would have made a registration-keyed flash fire
  constantly during exactly the period a new device is still building
  its collection. A new type code is rare and self-limiting, naturally
  approaching zero as the collection matures. Verified live against
  real detector logic: adopts silently on the first tick (a device
  that's been running doesn't flash on its whole existing collection),
  fires correctly the moment a genuinely new type is recorded.

**Satellite detector (1, two tiers)**, reusing `tick()`'s own existing
newly-overhead-pass detection (`_overhead_ids` diffing) rather than
re-deriving it:
- A **GO-OUTSIDE-grade pass** (`skypass.quality_rank` rank 3, the same
  ≥60° `ELEV_EXCELLENT` threshold the UPCOMING chip already uses)
  beginning right now → `TIER_INTERRUPT`.
- A **near-zenith pass** (`GO_OUTSIDE_TAKEOVER_EL = 80°`, sitting well
  above the existing chip's own top tier) → `TIER_TAKEOVER`.
- No `TIER_FLASH` here, deliberately — a lower-grade pass beginning is
  already fully served by the existing in-mode chip/arc treatment;
  flashing on top of a screen already showing the pass would be
  redundant, not useful.
- **The ISS is deliberately NOT special-cased** — unifying it into one
  ordinary catalogue entry was the entire point of the 2026-08-01
  satellite rework (see that section above), and re-privileging it here
  would undo it. It fires on its own real merits like everything else.
- Honestly flagged: no qualifying real pass occurred during this
  session's build — unverified live, same as the flights TAKEOVER/
  INTERRUPT detectors.

**Verified**: unit tests for tier-gated overwrite and highest-tier-wins
queue selection (both pass — a `TIER_FLASH` cannot clobber a pending
`TIER_INTERRUPT`, a `TIER_TAKEOVER` correctly overwrites a pending
`TIER_INTERRUPT`, and `AmbientEngine`'s selection picks the higher-tier
moment regardless of which engine's dict-iteration position it's in,
leaving the loser queued rather than dropped). All three flights
detectors run against real-shaped live-traffic payloads. Full
`AmbientEngine` integration path exercised end-to-end: a sub-engine
fires → ambient picks it up the same tick → renders with zero clipped
pixels → clears correctly after its own tier-specific hold length.
`render_audit.py` clean (0 modes failed) project-wide, including the
celebration path for the first time ever. Live panel verified
error-free across ambient/flights/satellite modes via direct pixel
dump — no real qualifying flights or satellite event occurred during
the session to trigger a live TIER_INTERRUPT/TIER_TAKEOVER, honestly
flagged rather than worked around with a fabricated trigger.

**WNBA was briefly added to `LEAGUE_PATHS` in this same pass, then
REMOVED again later the same day (see the WNBA removal note above) —
this paragraph is stale and kept only for the session history.** It had
been added to make `_detect_wnba_big_play` reachable
(`load_config()`/`set_favorite()` both reject any league outside
`LEAGUE_PATHS`), verified mechanically (`set_favorite("WNBA", ...)`
succeeded and persisted) but never verified against a real live WNBA
game (ESPN was 403ing this network for the session's duration). On
direct owner instruction later the same day, WNBA-specific coverage was
dropped from the project entirely rather than pursued further — see
"WNBA big-moment detection — REMOVED" above for what was actually
deleted and why.

**MMA expanded-detail renderer was explicitly SKIPPED this session**,
not attempted — it was scoped to real live MMA/PFL events only, "don't
build against synthesized card data," and ESPN's outage meant there was
no way to even check whether one existed. Still open; build it the
first session ESPN is reachable and a real card is live.

### Per-sport EXPANDED-DETAIL renderers (started 2026-08-01)

**Why, and why it's a SEPARATE follow-up rather than part of the main-
renderer work above**: select-to-expand (`rotate`) still used ONE generic
detail view for every sport, and it went from "fine" to "a visible
inconsistency" the moment the main renderers above shipped — for baseball
specifically, the generic detail view showed LESS live state (bases/outs,
no count) than baseball's own compact main row already did, which is
backwards for a view whose whole point is "more detail, not less".

**Contract**: `SportsEngine.SPORT_DETAIL_RENDERERS`, same shape as
`SPORT_RENDERERS` one level deeper — a sport claims its own expanded
renderer or falls back to `_frame_event_detail_generic()`. Verified as a
byte-identical no-op across all 45 live events before anything was added,
same discipline as the main-renderer seam.

| sport | status |
|---|---|
| baseball | **done** — same diamond/outs/count/arrow language as the main row, plus room the main row doesn't have: both teams' full records, series status, venue, broadcast |
| soccer | **done** — full record with points ("7-5-5, 26 PTS", no room on the compact row), venue, broadcast, series, note, shootout at full size. Verified live against two real MLS matches |
| golf | **done** — full tournament name, round status, venue, broadcast (the deep leaderboard the main view already provides isn't repeated). Verified against the real finished Rocket Classic |
| mma / others | not started — main renderer exists for MMA but no card was available to verify against at build time (zero events, no live/upcoming card in the window checked) |

**Two real bugs found building baseball's detail view, both from
verification, neither visible by eye:**
- The footer (series/venue/broadcast) never appeared on ANY live game.
  Not because anything overflowed — the cursor advanced by more (+12)
  than the live-state row actually draws (~+6-7), pushing y past the
  footer guard on every single live game. Found by hand-deriving the
  y-budget, since `render_audit.py` at the time had no way to see it (see
  below).
- The guard itself was off by one: `HEIGHT-6` rejected a legal `y=59` for
  a 5px glyph (the true bound is `HEIGHT-5`).

**This is what led to instrumenting `put_px` in `render_audit.py`** — see
that section. Every non-text graphical primitive (diamonds, outs pips,
trend arrows, arcs, event-frame borders) goes through `put_px`, which is
bounds-checked and silently drops out-of-range writes, the identical
failure shape as a dropped glyph but on a part of the tool that had never
been watching for it.

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
6. **`&` was silently dropped from NFL down-and-distance** — the SEVENTH
   instance, and it had already shipped. ESPN's `downDistanceText` is
   literally `"3RD & 7"`, so the sports mode rendered `"3RD  7"`. The glyph
   is now in `_FONT3x5`; the data was correct and the font was short.
   Found while building GAME DAY's team view, which reuses the same
   `situation_line()`.
7. **Tennis tiebreak brackets were silently dropped** — the EIGHTH
   instance. `"7-6(7-5)"` rendered as `"7-67-5"`, a different and
   plausible-looking score. `(` and `)` added to `_FONT3x5`.
8. **GAME DAY's stats view had overlapping text** — the "FIGHT STATS"
   kicker occupies rows 6-10 and the fighter names were drawn at y=8.
   Invisible to a code read; caught by rendering the frame and looking.

**This list stops being maintained here at instance 8.** Instances 9
(golf/Hojgaard) and 10 (news) are in `paneltext.py`'s own tally, and
every bug found since — the soccer divider/clock collision, golf's name
truncation, baseball's footer/cursor-budget bug, the satellite
simultaneous-overhead tie-break — is recorded where it was fixed (the
per-sport renderer sections above, the satellite section, the two-audits
section) rather than duplicated into an ever-growing single list here.
Check those sections, not just this one, for the full picture.

### "One system doesn't know about a state another just entered" — a standing risk class, not a one-off

Same weight as the glyph-drop pattern in `paneltext.py`'s tally: this has
now failed **four separate times**, independently, in four different
subsystems. It deserves the same "assume it will happen again, test for
it specifically" treatment rather than being treated as four unrelated
bugs that happened to rhyme. (Named more broadly than the original
"has_content() vs render path" framing after instance 4 turned out to be
the same root cause one layer further down — not a capability check at
all, but two pieces of *runtime* state.)

The shape every time: something correctly determines a fact (real content
exists, a user just made a deliberate choice) — but a SECOND,
separately-maintained piece of state decides what actually happens next,
and nothing keeps the two in sync. The first piece is right every time;
the second was never told.

1. **The pinned-golfer view** (2026-08-01). `tick()` force-set `view = 1`
   whenever the configured leagues had any game — a leftover contested-slot
   design, not something aware that a pinned golfer had real content sitting
   at `view == 0`. The golfer view rendered perfectly in isolation and never
   once appeared on the panel. Caught only by an end-to-end pixel match
   against the real hardware, not by reading the code or testing the view
   function directly.
2. **The keyboard legend** (2026-08-01). `arcade.html` kept its own list of
   "browsable modes" in the UI, in parallel with the engine classes that are
   the actual source of truth. When sports gained a second browse axis, the
   engine knew; the parallel list didn't, so the legend kept advertising
   left/right only and up/down — a real, working binding — was completely
   undiscoverable. Caught by manual testing in a real browser, counting
   actual outbound input requests, not by reading the legend code.
3. **The satellite dome's `has_content()`** (2026-08-02). Fixing
   `has_content()` to count real objects visible right now — not just
   queued passes — was step one. Step two, found only by actually rendering
   that exact scenario: `tick()`'s view stayed pinned to `VIEW_PASSES`, which
   has nothing to draw for an empty pass list, so the mode would have
   reported real content to `AmbientEngine` and then shown "NO VISIBLE
   PASSES" the moment it was selected. `has_content()` was completely
   correct and the bug was invisible from reading it.
4. **Flights' auto-cycle vs. a manual selection** (2026-08-02, same
   session, building the fix for #3's own sibling feature). The pre-existing
   spotlight rotation (scope → detail → next aircraft → … → scope) had no
   way to know a human had just pressed `rotate` to open ONE specific
   aircraft — it would have silently walked the view away a few seconds
   later on its normal timer, the exact "browsing while expanded stays
   expanded" rule sports had already had to learn. Fixed with an explicit
   `_auto_detail` flag distinguishing "the timer put us here" from "a person
   did". Caught by design review before it ever shipped, not by rendering —
   the first instance of this class caught **before** going live rather
   than after, because the pattern was already named and being watched for.

**The common root**: a fact computed or decided in one place (`has_content()`,
a UI's mode registry, a view-selection flag, an auto-advance timer) that
something else was supposed to stay synchronized with, but nothing enforces
the sync — so it holds by construction until the day a new feature changes
one side and not the other. Same failure shape as the glyph-drop bug (a fold
applied in one place, trusted everywhere, missed the one place it wasn't),
just one layer up: a fact known in one place, assumed everywhere, missed the
one place that needed to check it.

**What this means going forward**: whenever a change touches `has_content()`,
a mode's view-selection logic, an auto-advance/timer interacting with a
manual action, or anything that gates what happens next based on a fact
decided elsewhere — **render or exercise the specific scenario the change
claims to enable, end to end, not just the function that reports the fact
truthfully.** Instance 4 shows this can now be caught at design time simply
by asking "does anything else in this engine act on a timer, and would it
know this state just changed?" — worth asking explicitly on every new
view/mode-selection feature, not just discovered by accident.
**`render_audit.py`'s step-loop now drives `input("rotate")`/`input("drop")`
at every browse position automatically** (added 2026-08-02, after instance
4's sibling bug slipped through a clean run) — see that tool's own
docstring for the proof it actually closes the gap, not just claims to.

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
looked like the boundary. Extended 2026-08-01 to cover `skypass.py`'s
TLE-name fold, added when the satellite modes were unified.

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
bugs. Checks five things: **DROPPED** (no glyph), **OVERFLOW** (a
`draw_text3x5` box leaves the panel), **CLIPPED** (a non-text graphical
primitive — `put_px` directly — leaves the panel; see below),
**TRUNCATED** (reported, not failed — abbreviating a headline is
legitimate), **COLLISION** (two text draws sharing pixels).

**It found three real bugs on its first run**, all in code shipped hours
earlier: a soccer layout drawing the divider and clock through the second
team's score row, a golf name budget cutting "E. HENSELEIT" to "HENSEL",
and the tenth glyph instance (news dropping a curly quote, which is what
exposed that four feeds had never been migrated to `paneltext`).

**CLIPPED was added 2026-08-01**, and it closes a real blind spot: every
non-text graphical primitive (`draw_diamond`, `draw_outs`,
`draw_trend_arrow`, the pass arc, `draw_event_frame`'s border) goes
through `put_px`, which is bounds-checked and SILENTLY drops an
out-of-range write — the identical failure shape as a dropped glyph, on a
part of the renderer the tool had never been watching. Found while
building baseball's expanded detail view (see that section) — the real
bug there turned out NOT to be clipping (CLIPPED correctly reported none),
but the investigation is what exposed the gap in the tool itself.

**`drive()` was also extended 2026-08-01 to exercise select-to-expand**
(`eng.detail = ...`) for every event, not just the ticker row — it
previously never set `.detail` at all, so a per-sport expanded-detail
renderer could ship with a real bug and a clean audit run would say
nothing about it.

Marquee modes legitimately draw off-edge to loop seamlessly and are
exempted via `MARQUEE_OK` — `ambient` is in that set because it composes
real instances of the marquee modes. The same exemption applies to
CLIPPED, for the same reason.

**COLLISION is the check worth caring about most.** Fixed row offsets are
correct until content varies — a longer record, a form line, a team with a
longer name — and then two elements silently overlap. Prefer a **y-cursor**
over fixed offsets in any renderer whose content varies. **CLIPPED is the
same lesson one level down**: a cursor that advances by MORE than its
content actually draws doesn't clip anything itself, but it starves
whatever comes after it of room that was never really needed — check the
real ink extent of a glyph, not a guessed advance amount.

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
