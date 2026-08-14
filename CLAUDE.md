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

  **WINDOW FILTER, ROUND 2 (2026-08-08)** — perfected flights' window
  logic and shipped the equivalent for satellites.

  - **Real-panel ring verification (the gap noted above)**: restarted
    `com.henderburgh.arcade` and pulled `/api/frame` in `flights` mode.
    Result: honestly nothing to verify — `flights.FEED.get()` returned 0
    real aircraft in range for the several refresh cycles checked (both
    locally via `.venv` and against the live service), so the frame is a
    black scope with no ring to see, not a failure. `render_audit.py
    flights --strict` stays clean, and a synthetic single-aircraft
    smoke test (`in_window: True`, not a real payload) confirmed
    `draw_window_ring()` still fires through the render path after the
    `satellite.py` move below. The real-panel confirmation against an
    actual in-window aircraft remains the next honest step, same as it
    was before this pass — just no aircraft happened to be around this
    session either.
  - **Edge flicker**: checked for real, not assumed. No aircraft were in
    range to sample real `dir_deg` jitter against the 256°/336° boundary
    this session, so no hysteresis was added — the standing rule here is
    "only add this complexity if you can show it's solving a real
    problem," and there was no real data to show it with. Revisit next
    time a real aircraft sits near the edge for a few refreshes.
  - **Legend for the ring**: checked for a clean way to add context and
    found none that doesn't repeat the N/S/E/W experiment already tried
    and reverted on this exact scope (see below, same session it was
    reverted) for reading as clunky instrument-panel text. Left as a
    learned visual convention instead — the same category as the home
    diamond and the airport runway glyph, neither of which carry
    on-screen labels either. Deliberate, not an oversight.
  - **`_notable()` boost interaction**: re-verified the reasoning still
    holds (rank tiers spaced ≥1 apart, `WINDOW_BOOST = 0.5` can only
    break ties within a tier) — could not re-run against a fresh live
    snapshot with real window aircraft in it this session (none were in
    range), so this re-confirms the math, not new live evidence beyond
    the original two-snapshot verification already on record above.
  - **Config ownership moved to `satellite.py`**: `in_window()`,
    `load_window()`, and `save_window()` used to live in `flights.py`
    even though they operate on `satellite.CONFIG_PATH` — fine when only
    flights needed them, wrong once satellites did too. `satellite.py`
    cannot import `flights.py` (flights already imports satellite; the
    reverse would be circular — confirmed by reading both files' imports
    before moving anything), and `satellite.py` already owns
    `location_config.json`, so the three functions moved there verbatim
    (docstrings updated to point at the new home) and `flights.py` now
    aliases `load_window = satellite.load_window` etc. at its old call
    site — a one-line-per-name change, not a new dependency. Grepped the
    whole repo afterward for `flights.load_window`/`flights.save_window`/
    `flights.in_window`; the only hit was a stale comment in `engines.py`,
    fixed. No control-panel API endpoint touches these (checked
    `arcade_server.py`), so nothing else needed updating.
  - **Satellite window feature, same cone, same config, same visual
    language**: `skypass.py` now imports `satellite.py` (no
    circularity — `satellite.py` imports nothing of `skypass.py`'s) and
    `SkyPassFeed.get()` stamps an `in_window` boolean onto every pass
    (using `peak_az` — the moment the pass is most visible and the
    representative bearing for the whole pass, not `rise_az`, since a
    pass can rise somewhere and sweep well away from the window by peak)
    and onto every `sky_now` object (using its live current `az`). Same
    bearing convention as flights' `dir_deg` (0–360° from north),
    confirmed by reading `skypass.py`'s `predict()`/`sky_now()` before
    reusing `in_window()` unmodified. This is feed-layer I/O, same cost
    class as flights' own per-refresh `load_window()` read — zero I/O
    added to `SatelliteEngine`.
    - **OVERHEAD-NOW (the sky dome)**: the centerpiece, per the ask —
      `draw_window_ring()` (the SAME violet, `SatelliteEngine.WINDOW_RING`
      set identical to `FlightEngine.WINDOW_RING` on purpose — one
      window, one visual convention, not a second color to learn) is
      drawn under any dome object whose live `az` is in the window cone,
      the exact "is this visible out my window right now" answer.
    - **UPCOMING (the pass list)**: left chronological (soonest-first),
      not reordered — `skypass.predict()`'s own docstring says "soonest
      first," and jumping a window pass ahead of a sooner non-window one
      on a COUNTDOWN screen would read as confusing in a way flights'
      distance ranking doesn't. Instead of a new tier or new text next to
      GO OUTSIDE/GOOD PASS/VISIBLE, an in-window pass gets the same
      violet ring drawn as a small badge beside the existing chip — reuse,
      not a new visual system.
    - **`ambient_weight()`**: an in-window pass adds a small *additive*
      0.25 nudge to the existing quality-tier weight, mirroring
      `WINDOW_BOOST`'s "never cross a tier boundary" shape — 0.25 sits
      well under the smallest real gap between tiers here (0.5, between
      GOOD's 1.5 and BRIGHT's 2.5).
    - **Big-moment tiers untouched, deliberately**: `TIER_INTERRUPT`/
      `TIER_TAKEOVER`'s existing GO-OUTSIDE/near-zenith detector
      (`_detect_go_outside_pass`) was left alone. The owner asked for the
      window feature, not an expansion of the celebration system, and no
      clean small addition presented itself that wasn't scope creep.
  - **Verified**: `.venv/bin/python -c "import flights, satellite,
    engines"` clean. `render_audit.py flights`, `render_audit.py
    satellite`, `render_audit.py satellite --strict` all clean.
    `fold_audit.py` clean (0 feeds not folding). `satellite.in_window()`
    re-checked against the real measured window (256°–336°) with
    boundary values (256.0→True, 336.1→False, etc.) confirming the moved
    copy behaves identically to the pre-move version already verified
    against real ADS-B data for flights. **Real live satellite data
    (TLE-based passes/`sky_now`) could not be pulled this session** —
    `celestrak.org` (the TLE source `skypass.fetch_tles()` uses) was
    unreachable from this machine for the whole session, confirmed a real
    outage/network issue and not a sandbox artifact (same timeout with
    sandboxing both on and off, while `api.adsb.lol` and
    `api.wheretheiss.at` both responded normally in the same window). In
    its place, a **synthetic** wiring smoke test (explicitly not a real
    observed pass, built only to prove the render path doesn't crash and
    draws the ring) confirmed both the UPCOMING chip badge and the dome
    ring draw correctly when `in_window: True` is present on a pass/
    `sky_now` dict. **Honestly unverified**: the satellite window feature
    against a real live pass or real live dome object, and the flights
    ring against a real live in-window aircraft on the actual panel —
    both blocked by real absence of qualifying live data this session
    (no aircraft in range, no TLE fetch reachable), not by a code
    problem found. Next session: re-check both once celestrak is
    reachable and/or a real aircraft is in the window.

  **PLANE-IN-WINDOW TAKEOVER (2026-08-08)** — the highest-priority event
  in the whole Arcade: when a real aircraft enters the configured window
  cone (the same cone `satellite.in_window()`/`load_window()`/
  `save_window()` already implement, moved to `satellite.py` earlier this
  session — see WINDOW FILTER, ROUND 2 above), it pauses whatever is
  running and takes over the display for up to ~15s (or until dismissed),
  then lands on a ceremonial Hangar-style detail card for that aircraft.

  **Two takeover patterns already existed in this project and this is
  deliberately built as ONE of them, not the other.** The severe-weather
  takeover (`arcade_server._severe_alert_frame()`) composites over
  whatever the current mode already rendered, applied AFTER everything
  in the render loop — it never swaps `self.mode` and never captures
  input, so the mode underneath keeps running and keeps receiving button
  presses. That does not fit "pauses whatever is running" + "dismissible
  via any button press, lands on a specific detail card when it ends" —
  there is no real mode underneath to own that press. GAME DAY
  (`GameDayEngine`) IS a real mode swap (`arcade_server.set_mode
  ("gameday")`), fully owns input, and hands the panel back on its own
  via the `.launch` attribute hand-off `BootEngine`/`MenuEngine` already
  established. **`PlaneWatchEngine` copies the GAME DAY pattern
  verbatim**: registered in `engines.ENGINES` like `gameday`, deliberately
  NOT in `PLAYABLE`/`MenuEngine.NATIVE_GAMES`/`AmbientEngine.SEQUENCE`
  (force-triggered, never chosen from a menu or a rotation, its
  `has_content()` always `False`). The severe-weather takeover's own
  unconditional post-composite step still covers a plane-in-window frame
  for free, exactly like it already does for games/gameday/mirror/
  anything else — verified this stayed true after the change rather than
  assumed (no new precedence code was added or needed).

  **Detection lives entirely in `flights.py`, on the existing background
  poll thread — zero new I/O, zero new poll cadence.** `FlightFeed`
  already stamps a real `in_window` boolean on every aircraft dict (via
  `satellite.in_window()`, unchanged); this only DIFFS that flag against
  last cycle using the exact one-shot adopt-then-diff idiom every other
  detector in this project uses (`_seen_home_runs`/`_seen_squawks`/etc.):
  `FlightFeed._seen_window` is `None` until the first real refresh, which
  ADOPTS whatever's already in the window without firing (a device
  already running with a window aircraft parked at the edge must not
  take over the panel the instant this code ships) — only a genuinely
  NEW membership counts as "just entered".
  - `flights.FEED.pop_window_takeover_batch()` — ONE-SHOT (consumed, not
    peeked, same "pop don't peek-forever" discipline as
    `BigMomentSource.pop_big_moment()`), real full aircraft dicts (not
    just keys), sorted PRIMARY by real `dist_nm` ascending (closest
    first, the owner's explicit spec) and SECONDARY by real notability
    rank descending as a tiebreak — matching `_notable()`'s own rank
    field, not a second ranking scheme.
  - `flights.FEED.push_pending_detail(key)` / `pop_pending_detail()` —
    the one-shot "which aircraft to land on" hand-off. `set_mode()`
    always constructs `ENGINES[base]()` with ZERO args (confirmed by
    reading it before writing any of this), so there is no way to pass
    "which aircraft" directly through a mode switch — this is the same
    one-shot-queue idiom `pop_big_moment()` already established, applied
    to a different payload (a selection key instead of a celebration
    dict). `PlaneWatchEngine` calls `push_pending_detail()` right before
    setting `.launch = "flights"`; `FlightEngine.reset()` consumes it
    (never peeks) to pre-set `self.sel_key`, jump straight to
    `VIEW_DETAIL`, and mark the arrival as ceremonial
    (`self._ceremonial_key`) instead of landing on the plain scope.
  - Both keyed by `flights._ac_key()` (hex preferred, ident fallback) —
    a new module-level helper that mirrors
    `engines.FlightEngine._sel_key()` EXACTLY, so the feed and the engine
    agree on what "the same real aircraft" means without a second
    identity scheme.

  **`PlaneWatchEngine`** (`engines.py`, new): constructed with zero args
  (`ENGINES["planewatch"]` in the dict, same shape as `"gameday"`),
  pulls its own batch from `flights.FEED.pop_window_takeover_batch()` in
  `reset()` — reading already-cached feed state, the same "engine calls
  FEED" boundary every other engine here respects, not new I/O.
  - **Cycling**: closest-first/notable-secondary (the batch's own sort
    order), `HOLD_TICKS_PER_AC` (90 ticks, ~4.5s) per aircraft, an
    overall `TOTAL_CEILING_TICKS` (300 ticks, ~15s) safety cap. With
    enough aircraft that strict per-aircraft holds would exceed the
    ceiling, per-aircraft hold time is COMPRESSED (`TOTAL_CEILING_TICKS
    // n`, floored at `HOLD_TICKS_MIN` ≈ 2s) rather than truncating the
    cycle early or letting it run over — every aircraft that entered
    together gets at least a look. A **single-aircraft batch holds until
    dismissed or the ceiling** (`per_ac_ticks = None`), per the owner's
    explicit "if only one remains, stay on it" — no pointless self-loop.
  - **Dismissal**: `input()` on ANY press, or the ceiling running out (or
    cycling past the last aircraft in a multi-aircraft batch), hands off
    to flights' detail card for WHICHEVER aircraft is currently shown —
    `push_pending_detail(key)` then `.launch = "flights"`.
  - **The takeover screen** (`frame()`): a large filled hero silhouette
    (`draw_hero_silhouette()`, new), "IN VIEW" status, registration
    (prominent, falling back to `ident`), readable type name
    (`flights.ICAO_TYPE_NAMES`, already existed), distance + altitude
    (real, from the aircraft dict), and a soft first-sighting ring
    (`draw_first_sighting_ring()`, new) drawn UNDER the silhouette only
    when the real Hangar entry's `times_seen == 1` — read from
    `hangar.LOG.get()`, real data, never guessed; no Hangar entry (no
    broadcast registration, or not recorded yet) means no ring, not a
    guessed one.

  **Silhouette classification reuses the SAME classifiers this project
  already has** — `FlightEngine._ac_kind()` for a live-tracked aircraft
  (real ADS-B `category`), `FlightEngine._hangar_kind()` for a Hangar-log
  aircraft (real ICAO `type` code) — no third classifier was built.
  `draw_hero_silhouette()` (`engines.py`, module level, next to
  `draw_scope_home`) is a NEW drawing routine at a NEW hero scale
  (filled, not stroked) for the same four `SCOPE_ICON_*` buckets the
  scope/DETAIL/Hangar icons already use: a filled dart (nose/wingtips/
  tail, proportions varying per kind the same way the small stroked icon
  already does) for airliner/bizjet/GA, a filled fuselage oval + rotor
  disk outline + tail boom for helicopter — geometric only, no real
  logos/liveries, same IP-avoidance rule as every other icon here. Filled
  via a plain scanline polygon fill (`_fill_poly()`), which only ever
  runs for the ONE aircraft shown on a full-screen takeover per frame —
  a very different cost profile than the scope's up-to-8-icons-per-frame
  case this project already had a real lag complaint about and fixed
  (see ICON/PERFORMANCE REVISIT); reasoned about, not assumed free.

  **The rich post-takeover detail card is a DEDICATED renderer**
  (`FlightEngine._frame_detail_ceremonial()`), not an in-place extension
  of the ordinary `VIEW_DETAIL` block — the ordinary card's vertical
  budget is already fully audited with zero spare rows (its own comment
  says so), so there was no room to layer ceremonial fields in without
  reopening the exact collision risk that budget exists to avoid.
  Triggered specifically when `self.sel_key == self._ceremonial_key`
  (set only by the pending-detail hand-off, cleared the moment a manual
  `_step()` browses to a different aircraft — verified directly: a
  single-aircraft list correctly keeps the flag since there's nothing to
  step to, a two-aircraft list correctly clears it on step). Shows:
  the same new hero silhouette + first-sighting ring, registration + a
  REAL collection index (`#N/M` — this aircraft's position by real
  `first_seen` ascending among the real Hangar collection, out of the
  real total count; never an invented number, and omitted entirely when
  there's no real Hangar entry for this registration), type code + full
  readable name, a status band (real `FIRST SIGHTING` or real `SEEN
  {times_seen}X`, or an honest neutral `TRACKING` when this aircraft
  isn't in the Hangar at all — no broadcast registration, or not
  recorded yet — never a guessed status), real age since first sighting
  (`hangar.LOG`'s real `first_seen`), and airline when the Hangar entry
  has one (soft/secondary weight). Y-cursor-ish fixed 7px row cadence
  landing the last possible row (airline) exactly at `y=59`
  (`HEIGHT-5`, the real last-legal row for a 5px glyph) — this content
  has at most three real optional rows, not the open-ended variable
  content the sports/baseball y-cursor lesson was about, but still only
  draws the rows that are genuinely populated.

  **Guarded against waking `off`**: the render-loop trigger check
  (`arcade_server._loop()`) is placed AFTER the existing `mode == "off"`
  early-continue, so a plane flying by can never wake a panel
  deliberately released to WLED/Home Assistant lighting — CLAUDE.md's
  own standing rule that `off` is a deliberate handoff, not a state any
  feature may override uninvited. Also guarded against re-triggering
  itself (`mode != "planewatch"`).

  **A real off-by-one CLIPPED bug was found and fixed by instrumenting
  `put_px` directly against every icon kind and both card layouts** (the
  project's own established method, since `render_audit.py`'s fixed
  `TEXT_MODES` list doesn't drive `planewatch` or the pending-detail
  hand-off path automatically): the takeover screen's distance/altitude
  row was drawn at `y=60`, one pixel past the real `HEIGHT-5=59`
  boundary for a 5px glyph — silently clipped the bottom row of every
  5px character on real hardware. Fixed to `y=59`; re-running the same
  instrumented sweep across all four silhouette kinds, both hero scales
  (0.8 ceremonial / 1.1 takeover), both single- and multi-aircraft
  batches, and all three Hangar-entry states (repeat visitor, first
  sighting, no entry at all) now shows **zero** clipped pixel writes.

  **A new `/api/window` GET/POST endpoint was added** — checked first
  and confirmed genuinely missing (per WINDOW FILTER, ROUND 2's own note
  that no control-panel API touched `load_window()`/`save_window()` at
  all), same shape as the pre-existing `/api/satellite/location`.

  **Verified**: `.venv/bin/python -c "import flights, satellite, engines,
  hangar"` clean. `arcade_server.py` checked via `ast.parse()` only
  (syntax/reference check — importing it directly would construct the
  live `Arcade` singleton and put a second DDP sender on the panel, the
  documented panel-lockup hazard; not done). `render_audit.py` and
  `fold_audit.py` both clean project-wide (0 modes failed, 0 feeds not
  folding) before and after. `PlaneWatchEngine` driven directly against
  a real-shaped two-aircraft batch: confirmed closest-first/notable-
  secondary cycling order, confirmed a single-aircraft batch holds for
  the full ~15s ceiling without looping, confirmed dismiss-on-any-input
  pushes the correct pending-detail key and hands off, confirmed natural
  timeout does the same for whichever aircraft was showing. The flights
  window-detection logic (adopt-then-diff, one-shot batch pop, one-shot
  pending-detail pop) was driven directly against a simulated two-refresh
  sequence: the first refresh correctly ADOPTS without firing, the second
  correctly returns only the genuinely newly-entered aircraft in the
  right sort order, and a second pop before another refresh correctly
  returns empty. The ceremonial hand-off was driven end-to-end through a
  real `FlightEngine` instance: `pop_pending_detail()` → `reset()` →
  `VIEW_DETAIL` with `_ceremonial_key` set → `_frame_detail_ceremonial()`
  renders correctly for a repeat-visitor Hangar entry, a genuine
  first-sighting entry, and an aircraft with no Hangar entry at all
  (correctly shows `TRACKING`, not a guessed status).

  **Real panel check**: restarted `com.henderburgh.arcade`, confirmed
  `/api/window` returns the real configured `{"center_deg": 296.0,
  "fov_deg": 80.0}`, switched to `flights` mode via `/api/mode/flights`
  and pulled `/api/frame` — a real non-black, error-free frame (16457
  live bytes, `err: None`). Panel restored to `ambient` afterward,
  confirmed healthy.

  **Honestly unverified, and why**: `flights.FEED.get()` returned ZERO
  real aircraft in range for the whole session (`configured: True`,
  `aircraft: []`) — the same honest gap WINDOW FILTER/DEAD RECKONING
  above have already hit repeatedly this project. With no real aircraft
  in range at all, there is honestly nothing that could enter the window
  cone this session, so the actual takeover trigger firing on the real
  panel, the real hero silhouette rendering for a genuinely live
  aircraft, and the real ceremonial card populated from a genuinely live
  Hangar lookup are all unverified beyond the direct engine-driving
  checks above (which use realistic synthetic aircraft dicts, not a
  fabricated live trigger pushed onto the real service — no such push
  was made, per this project's own "never invent" rule). Next session
  with real traffic in range near the configured window (256°–336°):
  confirm the takeover actually fires from the render loop, watch the
  cycle/dismiss/hand-off sequence on real hardware, and confirm the
  ceremonial card against a real Hangar-logged aircraft.

  **PLANE-IN-WINDOW TAKEOVER, VISUAL REFRESH (2026-08-08, follow-up
  session).** Real review against the actual current screen (real pixel
  dumps pulled and sent for direct visual inspection before any code
  changed, not assumed stale) — three concrete gaps found, all fixed
  without a rebuild:
  - **Flat black background** was the one thing every other real
    TIER_TAKEOVER-class event in this project already had and this one
    didn't. Fixed by reusing `_backdrop_flights()` (the flights
    celebration's own radar-sweep wedge, already proven cheap and
    already shipped) rather than inventing a fourth backdrop language —
    tinted `RING` (violet, the same `WINDOW_RING` color the small scope
    already uses for "this is a window aircraft") instead of an altitude
    color, so the backdrop itself carries the "why did this fire"
    meaning and the silhouette's own altitude-band color keeps meaning
    "what kind of traffic this is" — one color, one job, not overloaded.
  - **Nothing on the screen visually said "window"** before this —
    solved by the same backdrop-color choice above, not a second
    treatment.
  - **A window aircraft that was ALSO notable** (heavy, helicopter,
    MAYDAY — see `flights._notable()`) got no acknowledgment, even
    though the small scope's own `NOTABLE_GLOW_FLOOR` (added the same
    session as the small-icon redesign) now treats that as a real,
    separate signal worth showing. Folded into the existing `"IN VIEW"`
    header row (`"IN VIEW: HEAVY"`) rather than a new row — this card's
    vertical budget was already fully accounted for, and CLAUDE.md's own
    repeated lesson about fixed-offset collisions argued against adding
    one casually.
  - **Verified**: `render_audit.py` has literally zero coverage of
    `PlaneWatchEngine` (force-triggered only, excluded from the normal
    sweep, same as `gameday` always has been) — driven directly via
    `render_audit.Audit`'s own instrumentation against 5 real-shaped edge
    cases (a long registration, a real MAYDAY tag, a near-rim distance,
    both single- and multi-aircraft batches): 0 dropped glyphs, 0
    overflow, 0 clipped pixels, 0 collisions (checked with a real
    bounding-box overlap test across every text draw in each frame, not
    eyeballed). Full `render_audit.py`/`fold_audit.py` suites clean.
    Rendered against real live aircraft data and sent for direct visual
    review before committing. **This screen has no HTTP trigger** (unlike
    `/api/notify`), so real-panel confirmation is via direct
    engine-driving against real data — the same verification path used
    when the feature originally shipped, not a regression in rigor.

  **WINDOW FILTER, ROUND 3 — real distance cap (2026-08-08).** Real
  feedback, not a visual complaint: bearing alone (296°±40°) answers
  "is this aircraft in the right DIRECTION", never "can I actually SEE
  it out the window right now" — a plane at 3nm in that cone genuinely
  is visible from a real window; one at 35nm in the exact same cone is
  over the horizon or behind terrain, but the bearing-only cone said yes
  to both identically.

  - **`satellite.WINDOW_MAX_NM_DEFAULT = 8.0`** — a reasoned judgment
    call, explicitly NOT a measured fact the way most numbers in this
    project are (there's no sensor that reports "how far can a person
    actually see out this specific window"): roughly the generous end of
    real-world naked-eye identification range for an airliner-sized
    object in clear conditions, and a real residential window's
    practical sightline (trees, structures, haze) usually falls short of
    the geometric horizon anyway, so 8nm (~9.2 statute miles) sits well
    inside "genuinely visible" rather than the theoretical maximum.
    Config-driven exactly like `center_deg`/`fov_deg` — precisely
    because it's a judgment call, tunable once the owner has actually
    watched real aircraft cross the cone at different distances.
  - **`load_window()`/`save_window()` extended to carry `max_nm`**, with
    real back-compat (a config saved before this key existed — every
    config saved by ROUND 1/2 of this feature — still loads cleanly via
    its own independent fallback, not a blanket "missing key means
    everything's default").
  - **A real destructive-overwrite bug caught and fixed before commit,
    not after.** The first draft of `save_window(max_nm=None)` fell back
    to `WINDOW_MAX_NM_DEFAULT` whenever the caller omitted `max_nm` —
    which would have silently RESET a real tuned value back to 8.0 every
    single time the owner adjusted just `center_deg`/`fov_deg` from the
    control panel (which only ever sends those two fields). Caught by
    testing the exact sequence directly: save with an explicit
    `max_nm=12.0`, then save again omitting it — the second save must
    preserve 12.0, not reset to 8.0. It didn't, on the first attempt.
    Fixed to read the EXISTING saved `max_nm` as the fallback, only
    falling through to the module default on a genuinely first-ever
    save. This is the same destructive-overwrite bug class
    `location_config.json` has already caused twice before in this
    project (the `airport` key, the `golf_player` key) — one call
    deeper this time, caught before it shipped rather than after.
  - **`flights.py`'s `in_window` stamp is now bearing-cone AND
    `dist_nm <= max_nm`** — a single AND-gated boolean, stamped once, at
    the same call site every downstream consumer already reads from.
    This means the window ring, the `WINDOW_BOOST` sort nudge, and the
    plane-in-window takeover's newly-entered detection ALL inherit the
    distance cap for free — none of those three call sites needed to
    change, because they all only ever read the one `in_window` field
    rather than re-deriving it.
  - **`/api/window` GET/POST extended** to read/write `max_nm` (POST
    treats it as optional, matching `save_window()`'s own preserve-if-
    omitted contract above).
  - **Verified**: the exact scenario from the real feedback — same
    bearing (300°, inside the 296°±40° cone), 3nm vs. 35nm — confirmed
    programmatically: the close one stays `in_window`, the far one
    correctly drops out. `render_audit.py`/`fold_audit.py` both clean.
    Real panel restarted and confirmed healthy (`/api/window` returns
    the real config including the new `max_nm` key; a real non-black,
    error-free `flights` frame pulled via `/api/frame`).

  - **LIVE-TRAFFIC CONFIRMATION (2026-08-08, follow-up session).** Real
    aircraft were in range this time — 8 tracked, including 5 that sat
    INSIDE the real bearing cone but beyond the new cap (`NJM998` at
    32.8nm, `CNS976` at 24.7nm, `N7085G` at 12.2nm, `N5191J` at 18.1nm,
    `JBU2474` at 8.3nm, just barely over the 8.0nm line) — exactly the
    scenario the fix exists for. Under the OLD bearing-only logic every
    one of those five would have shown the window ring right now; with
    the cap, all five correctly stayed unmarked. Traffic moved (real
    motion, not a single static sample) and a later poll caught two real
    aircraft genuinely entering `in_window: True` at 5.9nm and 6.2nm —
    both correctly inside the cap. Confirmed both in a live
    `FlightEngine.frame()` render (not just the boolean flag): a real
    in-window aircraft (`CNS976`, that session's live snapshot) showed
    all 4 expected `WINDOW_RING` pixels `(190, 110, 255)` exactly, at
    the correct offsets under its icon. The 8nm default itself is still
    the owner's call once they've watched a few real ones cross it in
    person — nothing here changes that, this only confirms the
    MECHANISM is doing the right thing with real numbers.

  **RADAR-SCOPE ICONS REDRAWN (2026-08-08)** — real feedback on an
  actual rendered screenshot of the live panel, not a described
  complaint: the helicopter icon (9 disconnected `put_px` dots with no
  connecting stroke) read as a violet blob, not a helicopter, and the
  screenshot showed it plainly. Separately, `_ac_kind()`'s three
  fixed-wing buckets (airliner/bizjet/GA) only ever differed by SIZE —
  the same 2-stroke cross, scaled — which is why they never felt like
  distinct categories at 3-5px even though the classification underneath
  was real.

  Every kind is now a genuinely different SILHOUETTE FAMILY, still 2
  strokes or fewer per icon (unchanged stroke-count budget, the exact
  number this project already proved safe against the real lag
  complaint in ICON/PERFORMANCE REVISIT above — this redesign changes
  SHAPE, not cost):
  - **GA** — a single stroke, a lone dash, no wing at all. "Less drawn"
    stays the point (same reasoning the original GA design had, before
    it quietly regressed back to a scaled copy of the shared cross at
    some earlier point this project didn't separately document).
  - **BIZJET** — a small cross with the wing stroke set AFT of the
    fuselage's own centre rather than centred on it — a real structural
    cue (most business jets have a low, rear-mounted wing), not just a
    smaller airliner.
  - **AIRLINER** — unchanged proportions: a wide cross, wing centred on
    the fuselage, the biggest and most symmetric silhouette here.
  - **HELI** — replaced the 9-point cluster with a connected T: a
    horizontal rotor bar + a short vertical mast + one tail-boom pixel
    kicked out behind the facing direction. The rotor bar deliberately
    stays SCREEN-LEVEL at every heading (mirrored left/right only, never
    rotated) — a helicopter's rotor disk doesn't visually "point"
    anywhere, and the bar staying flat while every fixed-wing icon
    visibly rotates with real heading is itself a recognition cue over
    time, on top of the shape alone.

  **Verified via a synthetic 8x-zoom PNG spot-check BEFORE touching the
  live panel** — all four kinds across 7 headings, sent for direct visual
  review and approved as designed, same "render before calling it done"
  discipline this project has followed since its very first icon pass.

  **`NOTABLE_GLOW_FLOOR = 0.75` (new)** — the one piece of visual
  hierarchy this scope was genuinely missing, added in the same pass
  after the owner asked directly whether "notable" needed its own
  signal. Before this, every target dimmed identically as the sweep beam
  passed it (down to `SCOPE_TARGET_FLOOR = 0.38`), so a MAYDAY squawk and
  a routine regional jet looked the same off-beam — "notable" only ever
  showed up as TEXT elsewhere on the card. A real notable aircraft (see
  `flights._notable()`'s own rank tiers) now never dims below 0.75
  regardless of where the sweep currently is, via `max(scope_glow(...),
  NOTABLE_GLOW_FLOOR)` — still pushed brighter as the beam passes it,
  same as everything else, just with a raised off-beam floor.

  **Deliberately kept as a SEPARATE signal from the window ring, not
  merged into one "brighter" treatment** — asked and answered explicitly
  this session: a window aircraft and a notable aircraft (heavy,
  helicopter, MAYDAY, low-and-close) are different KINDS of interesting
  — one is about where the OWNER happens to be looking, one is about
  what the aircraft itself IS — and collapsing both into brightness alone
  would recreate the exact ambiguity ("why is this one different?") that
  prompted the icon redesign in the first place. The window ring stays a
  categorical shape+color marker; `NOTABLE_GLOW_FLOOR` is a categorical
  brightness marker; an aircraft that is BOTH shows both signals at once
  rather than either overriding the other. Confirmed the "rotating sweep
  beam" itself (`scope_glow()`) is working as intended and untouched —
  that's the mode's continuous heartbeat animation, a real, separate
  question the owner asked and confirmed before any of the above was
  built, not something this pass changed.

  **Verified**: `render_audit.py`/`fold_audit.py` both clean.
  `NOTABLE_GLOW_FLOOR > SCOPE_TARGET_FLOOR` confirmed programmatically
  (0.75 > 0.38 — the floor is genuinely brighter than the routine dim
  state, not a no-op). Real panel restarted, confirmed healthy (a real
  non-black, error-free `flights` frame via `/api/frame`).

  - **LIVE-TRAFFIC CONFIRMATION (2026-08-08, follow-up session).** Real
    traffic in range this time, covering every icon kind at once: 3 real
    helicopters (`N220SG`, `N267AW`, `N3055Y`, all real R44s), a real
    bizjet, real GA Cessnas, and real airliners including a heavy
    (`AAL1991`). Verified two ways, not just "it ran without error":
    (1) a synthetic 8x zoom sent for direct visual review and approved
    as designed (the design-approval pass, before this live check);
    (2) THIS session, pixel-level ASCII dumps of real crops taken from a
    single self-consistent `FlightEngine.frame()` render (position
    computed and pixels read from the exact same tick — an earlier
    attempt that mixed a fresh engine's computed positions with the
    separately-running live service's independently-timed frame produced
    a clearly wrong crop, caught and corrected before trusting it,
    worth remembering as a real methodology trap for any future
    position-matched pixel verification). Both real helicopter crops
    (`N267AW`, `N3055Y`) show a genuinely CONNECTED horizontal rotor bar
    with a vertical mast beneath it in the raw pixel grid — not
    scattered points, confirming the fix for the original "violet blob"
    complaint holds on real hardware, not just the synthetic mockup.
    `NOTABLE_GLOW_FLOOR`'s exact math confirmed pixel-for-pixel against
    a real notable LOW-altitude aircraft: rendered color `(191, 67, 52)`
    is precisely `int(0.75 × (255, 90, 70))` — the real ALT_BANDS LOW
    color at exactly the 0.75 floor, truncated the same way `int()`
    already does everywhere else in this rendering pipeline. A real
    notable HEAVY aircraft (`AAL1991`) showed a color BRIGHTER than its
    own floor at that instant, confirming `max(scope_glow(...),
    NOTABLE_GLOW_FLOOR)` correctly lets the sweep push a notable
    aircraft past its floor rather than clamping it there.

  **DEAD RECKONING (2026-08-08)** — real user feedback after watching the
  radar scope: aircraft only visibly moved once per ADS-B poll (`flights.
  FEED` refreshes every `flights.POSITION_REFRESH` = 15s), sitting frozen
  at their last-polled bearing/distance the rest of the time and
  teleporting once per poll. Read as static/jumpy, not "live". Fixed with
  render-side dead reckoning, `FlightEngine._update_dead_reckoning()` —
  pure math over data `flights.py` already fetches, **zero new I/O**,
  same discipline every other engine in this project follows.

  - **Real data only, no invented motion.** Every aircraft dict already
    carries REAL `gs_kt` (ADS-B ground speed, kt) and `track_deg` (real
    heading) — this is physics-based extrapolation from observed real
    velocity, the same category of derived-but-real inference
    `flights._phase()` already does for CLIMB/DESCEND/CRUISE
    classification, just continuous instead of discrete.
  - **The math**: real `dist_nm`/`dir_deg` (bearing FROM home, the same
    convention `scope_xy()` itself uses — confirmed by reading
    `scope_xy()`'s own docstring/formula before writing any of this: `x =
    cx + r*sin(bearing)`, `y = cy - r*cos(bearing)`, 0°=N=up, clockwise)
    convert to a local flat-plane position: `x_nm = dist_nm*sin(dir_deg)`,
    `y_nm = dist_nm*cos(dir_deg)` (north = +y). On a REAL poll, this
    becomes the dead-reckoning REFERENCE for that aircraft, keyed by the
    SAME identity `_sel_key()` selection already uses (hex preferred,
    ident fallback) — one keying scheme, not a second one. Every render
    tick after that, if `gs_kt`/`track_deg` are BOTH real (neither
    `None` — honest degrade otherwise: hold the last known real position,
    extrapolate nothing), advance from the reference: `speed_nm_per_sec =
    gs_kt / 3600`, `dx = speed_nm_per_sec * sin(track_deg) * elapsed`,
    `dy = speed_nm_per_sec * cos(track_deg) * elapsed`, elapsed clamped
    to `[0, DR_MAX_MULT(2.0) * POSITION_REFRESH]` (30s) so a late/stalled
    poll freezes extrapolation at a ceiling instead of projecting further
    into an increasingly unreliable future — same "an honest gap beats a
    lie" principle as the selection-loss-on-range-exit handling above.
    Recomputed `dist_nm`/`dir_deg` land on the aircraft dict as
    `_ext_dist_nm`/`_ext_dir_deg`.
  - **Real poll always wins, no blending.** `tick()` detects a genuinely
    new poll landing by watching `flights.FEED.get()`'s own `age` field
    (seconds since the cached snapshot was fetched): `age` increases
    smoothly with real wall time between polls and drops back down the
    instant a new snapshot lands, so a decrease is a reliable "a real
    poll just landed" signal with **zero extra I/O** (no second signal
    needed). On that signal, the dead-reckoning reference resets
    immediately to the new real position/timestamp — never smoothed or
    blended toward it, so a real position correction is never hidden.
  - **Text stays real, only the icon moves.** `_ext_dist_nm`/
    `_ext_dir_deg` are read in exactly one place — `_frame_scope()`'s
    aircraft-icon `x, y` — falling back to the real `dist_nm`/`dir_deg`
    when unset. Every other read of the aircraft dict (DETAIL card text,
    sorting, notability, ATC matching, the window filter) still sees the
    real polled values untouched, so nothing ever displays a number that
    isn't literally what ADS-B reported. The window ring and selection
    ring are drawn AT the icon's `x, y`, so they track the smoothed
    position automatically — no separate wiring needed.
  - **No new rim-clipping risk.** `_scope_r_frac()` already clamps its
    output to `[0, 1]` (`min(1.0, dist_nm / RADIUS_NM)`) before the sqrt
    — an extrapolated aircraft projected past the rim just draws AT the
    rim, identical to how a real near-rim aircraft already renders; no
    new edge-of-scope handling was invented.
  - **Bounded.** `self._dr` (sel_key -> `{x_nm, y_nm, t_ref}`) is rebuilt
    to only the keys present in the current aircraft list every tick —
    an aircraft leaving `RADIUS_NM` has its dead-reckoning state cleaned
    up here too, same discipline as every other keyed cache in this
    project (THE HANGAR, `atc.py`'s log).
  - **Verified**: `.venv/bin/python -c "import flights, engines"` clean.
    Direct math check: a synthetic aircraft at 10nm/090°, 360kt, heading
    000° — hand-computed position after 5 real seconds
    (10.0125nm/87.138°) matched the engine's actual
    `_update_dead_reckoning()` output within float tolerance
    (10.0125nm/87.1376°, diff <1e-4). Drove the engine through 20
    simulated ticks between two real polls: `(x_nm, y_nm)` progressed
    smoothly and monotonically toward the aircraft's real track, never
    jumpy or static. Confirmed the honest-degrade path (missing
    `gs_kt`/`track_deg` → no `_ext_*` fields set, position holds).
    Confirmed snap-to-real: a new poll with a very different real
    position immediately became the new reference with zero blending.
    Confirmed the `DR_MAX_MULT` clamp: a 10,000s-stale reference
    extrapolated only to the 30s ceiling, not further. Confirmed bounded
    cleanup: an aircraft absent from the next tick's list is dropped from
    `self._dr`. `render_audit.py`/`fold_audit.py` both clean (0 modes
    failed, 0 feeds not folding), including flights' scope render.
    **Real panel check**: restarted `com.henderburgh.arcade`, switched to
    `flights`, pulled `/api/frame` repeatedly a few hundred ms apart.
    **Zero real aircraft were in range at MYR at verification time**
    (`flights.FEED.get()` returned an empty list for several consecutive
    real polls), so the frame correctly showed the static `_frame_idle()`
    "CLEAR SKIES" screen and byte-identical frames are the EXPECTED
    result in that state, not a sign of a stuck sweep — the moving-icon
    behavior itself is honestly unverified on the real physical panel
    this session; the direct math/engine-driving checks above are what's
    actually verified. Next session with real traffic in range: watch a
    selected aircraft's icon move smoothly between two real polls rather
    than jumping only at the 15s mark. Panel restored to `ambient`
    afterward.

  **FLOWN-PATH TRAIL + ROUTE CONTEXT (2026-08-08)** — three owner-requested
  features, built the same session real DEAD-RECKONING landed (see
  `FlightEngine._update_dead_reckoning()`, already in the working tree
  when this work started, uncommitted from a concurrent session). Built
  to REUSE that feature's exact identity keying and local-plane
  representation rather than invent a second one — confirmed by reading
  its docstring first, not assumed.

  1. **Real flown-path trail** — "click select on the plane, show the
     path it's flown." `FlightEngine._trail` maps `_sel_key()` (the same
     hex/ident identity dead reckoning and selection already use) to a
     list of REAL observed `(x_nm, y_nm)` local-plane positions —
     literally the same flat-plane conversion `_update_dead_reckoning()`
     already does (`x = dist_nm*sin(dir_deg)`, `y = dist_nm*cos(dir_deg)`),
     reused verbatim rather than re-derived.
     - **Sampled ONLY on a real poll refresh** (`_update_trail(ac_list,
       new_poll)`, called right after `_update_dead_reckoning()` in
       `tick()`, same `_new_poll` signal), never per render tick — a
       trail built from the dead-reckoned/extrapolated icon position
       would compound estimation error into a feature whose entire point
       is showing where the aircraft REALLY was. Verified directly: three
       ticks (one real poll + two non-poll ticks) produced exactly one
       trail point, not three.
     - **Bounded**: `TRAIL_MAX_POINTS = 20` (~5 minutes of real polls at
       `flights.POSITION_REFRESH`, 15s), oldest point dropped past the
       cap; rebuilt to only currently-tracked `_sel_key()`s every call, so
       an aircraft leaving range or going unseen drops its trail — same
       discipline as `self._dr` and THE HANGAR. Verified: 30 simulated
       real-shaped polls capped at exactly 20 points; a second aircraft's
       trail stayed separate; an empty aircraft list evicted every key.
     - **Drawn only for the CURRENTLY SELECTED aircraft**, never all 8 —
       this scope already had one real lag complaint fixed earlier this
       session from over-drawing (6→2 strokes per icon); trailing every
       aircraft every frame would reopen it. Rendered as short segments
       (`draw_line`) in a dimmed (÷3) version of the aircraft's own real
       altitude-band color, drawn BEFORE the aircraft icon loop so the
       live icon always paints over the trail's end, never the reverse.
       Selecting a different aircraft shows that aircraft's own
       (possibly empty) trail automatically — no separate reset needed,
       it falls out of keying by `_sel_key()`.

  2. **Departing / Arriving / Transit** — the owner's own framing: "myrtle
     to wherever means departing, wherever to myrtle means arriving."
     `FlightEngine._route_status(route, airport)` compares the real
     resolved `route["origin"]`/`route["dest"]` (adsbdb, IATA preferred/
     ICAO fallback) against `flights.load_airport()`'s configured home
     code. No route or no configured airport → `None`, never guessed.
     Verified against the real configured home (MYR): a MYR→MHT-shaped
     route → `"DEPARTING"`, MHT→MYR → `"ARRIVING"`, an unrelated
     MDW→MHT route → `None` (TRANSIT, the common case — most tracked
     traffic is near home, not to/from it, same "no badge for the
     mundane case" rule flight-phase CRUISE already follows).
     - **Placement: the header's existing `right_tag` slot, not a new
       row.** This detail card's vertical budget is already fully
       audited (see the y-cursor comment above this block in the code);
       DEPARTING/ARRIVING takes priority over the notable tag/position
       counter that slot already shows, since "is this plane leaving or
       arriving" is the single most useful real fact when it applies.
       The common TRANSIT case falls through unchanged.
     - **Honest limitation, not fabricated past**: `load_airport()`
       stores only ONE code form (whatever the owner configured — MYR,
       IATA), not both IATA and ICAO. adsbdb's route field prefers IATA
       too, so this matches in the common case, but a route that only
       resolved an ICAO code (`"KMYR"`) against an IATA-configured home
       would false-negative to TRANSIT rather than DEPARTING/ARRIVING —
       a real format-mismatch gap, left honest rather than guessed past
       with an unverified code-conversion table.

  3. **Real country + a real route-bearing ray** — `flights._fetch_route()`
     now keeps `origin_country`/`dest_country` (adsbdb's real
     `country_name`, folded through `paneltext.panel_text()` like every
     other externally-sourced string here) and `origin_lat`/`origin_lon`/
     `dest_lat`/`dest_lon` (numeric, adsbdb's real per-airport
     coordinates) — both were already in the raw payload and simply being
     discarded, same shape of gap `origin_city`/`dest_city` closed
     earlier. **Zero new I/O**: same per-callsign `_fetch_route()` call
     that already ran on the existing `MAX_LOOKUPS_PER_REFRESH`-capped
     cadence, just keeping more fields off the same real response.
     Verified live against a real callsign (SWA1065, MDW→MHT):
     `origin_country`/`dest_country` both resolved `"UNITED STATES"`,
     `origin_lat/lon` `(41.785999, -87.752403)`, `dest_lat/lon`
     `(42.932598, -71.435699)` — all real, all populated.
     - **Country display**: appended to the existing route-line text
       (`"<origin city> > <dest city> - <country>"`) rather than given
       its own fixed row — piggybacking on a line that's ALREADY
       marquee-safe (`draw_marquee` scrolls it when it doesn't fit at
       scale 1) means a longer string can never collide with anything;
       it either fits or scrolls, exactly like the line already did.
       Destination country preferred (matches the existing
       destination-first city preference), origin's used only as a
       fallback.
     - **Route-bearing ray on the scope**: drawn from the home marker
       toward the REAL bearing (via `flights.bearing_distance()`, the
       same haversine/bearing function the coastline and airport-verify
       math already use — reused, not re-derived) to whichever real
       airport coordinate is the informative one. Only drawn when the
       selected aircraft has a real resolved route AND adsbdb returned
       real coordinates for that end — no route or no coordinates means
       no ray, never a guessed one. **"Departing means show where it's
       headed, arriving means show where it came from, transit shows
       both"**: DEPARTING draws only the destination ray, ARRIVING draws
       only the origin ray (the home-side ray would be redundant in
       either case), TRANSIT draws both — judged the most intuitive
       framing of the three, matching feature 2's own DEPARTING/ARRIVING
       logic rather than inventing a separate rule. Short ray (fixed
       `frac=0.42`, not rim-to-rim), dim muted color, drawn in the same
       context layer as the trail (before the aircraft icon loop, under
       the live icon).

  **Verified**: `.venv/bin/python -c "import flights, engines"` clean.
  `render_audit.py flights` and `render_audit.py flights --strict` both
  clean (0 CLIPPED/DROPPED/COLLISION). `fold_audit.py` clean (0 feeds not
  folding). All three pieces verified directly against real data as
  described above (real callsign route fetch, real MYR-based
  departing/arriving/transit classification, simulated real-shaped poll
  sequences for trail accumulation/capping/eviction). **Real panel
  check**: restarted `com.henderburgh.arcade`, switched to `flights`,
  pulled `/api/frame` — clean non-crashing frame (708 real non-background
  pixels: header rule + rings + idle-scope legend), zero errors. **Zero
  real aircraft were in range at verification time** (`flights.FEED.get()`
  returned 0), so the trail/route-ray/DEPARTING-ARRIVING pixels
  themselves are honestly UNVERIFIED on the real physical panel — the
  direct engine-driving checks above are what's actually verified for
  those three pieces, not a live in-range aircraft. Next session with
  real traffic in range: select an aircraft, watch a real trail
  accumulate over a few real polls, and confirm the route ray/DEPARTING-
  ARRIVING tag on an aircraft with a resolved route. Panel restored to
  `ambient` afterward.

  **Concurrent dead-reckoning work note**: this was built on top of
  `_update_dead_reckoning()`, `self._dr`, and the `_ext_dist_nm`/
  `_ext_dir_deg` icon-position fields already present uncommitted in the
  working tree when this session started (a different concurrent
  session's work, per its own in-code dated comments). Nothing about it
  was modified — this work only reads its `_sel_key()` keying convention
  and reuses its x_nm/y_nm math, and both features coexist in
  `_frame_scope()`/`tick()` without touching each other's call sites.

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

**Home Assistant notification pass-through (task #8, 2026-08-08)** — a new
`POST /api/notify` endpoint lets Home Assistant push arbitrary short text
onto the panel, mirroring HA's real `notify` service payload shape:

    {"title": "GARAGE", "message": "Left open 20 min", "data": {"priority": "normal"}}

`title` is required; `message` and `data.priority` are optional.
`data.priority` recognizes only `"normal"`/`"urgent"` -- missing,
malformed, or unrecognized falls back to `"normal"`, the same convention
`/api/brightness` already established for bad config values.

**Auth is a real, targeted exception to this project's usual no-auth
posture.** Every other `/api/*` endpoint here is unauthenticated (trusted
LAN, 0.0.0.0 bind). This ONE endpoint checks header `X-Arcade-Token`
against a token in `notify_config.json` (`secrets.token_hex(16)`,
generated on first run) via `hmac.compare_digest` -- constant-time, not
`==`, so a same-LAN attacker can't guess it byte-by-byte off response
timing. The reasoning: every other endpoint here can at worst change what
game is showing; a spoofable notify endpoint lets anyone on the LAN put
convincing, unattributable fake text on a panel in the house. Missing or
wrong token is a 401 with zero side effect. `notify_config.json` holds a
real credential (unlike every other `*_config.json` in this project) and
is gitignored rather than committed.

`title`/`message` are folded through `paneltext.panel_text()` at the
endpoint boundary in `arcade_server.py` -- the standard external-text
entry-point rule -- before anything downstream ever sees them. The
endpoint never fabricates text; it only ever displays exactly what the
real POST body contained, fit/wrapped, never invented.

**Two visual tiers, reusing the project's two existing takeover
mechanisms rather than inventing a third:**

- **`priority: normal`** — `engines.draw_notify_banner()`, a lightweight
  composite overlay modeled on `_severe_alert_frame()`'s pattern (applied
  post-render in `arcade_server._loop()`, never touches `self.mode` or
  input, whatever's running keeps running underneath) but visually the
  OPPOSITE of severe weather's full-bleed 4-edge pulse: a quiet band
  pinned to the bottom third (`HEIGHT-20..HEIGHT-1`, ~31% of the panel),
  everything above it untouched. Deliberately subordinate-looking --
  reusing the pulse treatment for an "FYI" would train someone to tune
  out the panel exactly the way restricting severe takeover to
  Extreme/Severe already reasons about. Title via `fit_text()` (single
  line), message via `wrap_text()` (up to 2 lines); if the folded message
  doesn't fit that budget, overflow uses `draw_marquee()` -- the same
  scrolling tape news/ticker/gameday/flights already use -- instead of a
  third truncation style. Auto-clears after `NOTIFY_BANNER_SECONDS` (9s:
  long enough to read a short line on a glanced-at wall panel, short
  enough to never compete with whatever's running for more than a few
  seconds -- a reasoned default, not measured). State lives on `Arcade`
  (`self._notify_banner`), mirroring how `_alert_ticks` tracks
  severe-weather state there rather than in a feed module. Composited
  BEFORE the severe-weather takeover in the render loop, so severe still
  wins outright if both are ever active at once.

- **`priority: urgent`** — `engines.NotifyEngine`, a real mode-swap
  takeover built with the EXACT same shape as `PlaneWatchEngine` (see
  PLANE-IN-WINDOW TAKEOVER above): zero-arg constructible, registered in
  `engines.ENGINES["notify"]`, deliberately NOT added to
  `MenuEngine.NATIVE_GAMES`/`AmbientEngine.SEQUENCE` -- force-triggered
  only, `has_content()` always `False`, never chosen from a menu or a
  rotation. Pauses whatever's running via a real
  `arcade_server.set_mode("notify")`, owns input, dismisses on ANY press
  or a ~15s ceiling (`TOTAL_CEILING_TICKS`, matching `PlaneWatchEngine`'s
  own duration for consistency across the project's two force-triggered
  takeovers).

  **Hands back to the ACTUAL previous mode, not a hardcoded landing
  spot** (unlike `PlaneWatchEngine`'s fixed `.launch = "flights"`) --
  `Arcade.trigger_notify()` captures `prev = self.mode` right before the
  swap, the same idiom `set_mode()` itself already uses internally, and
  threads it through `notify.push_pending()`/`notify.pop_pending()`, the
  IDENTICAL one-shot module-level hand-off idiom `flights.py`'s
  `push_pending_detail()`/`pop_pending_detail()` already established for
  `PlaneWatchEngine`'s own "`set_mode()` always constructs `ENGINES[base]
  ()` with zero args, so there's no way to pass data through the switch"
  problem. `NotifyEngine.reset()` consumes (never peeks) the payload. If
  an urgent notification arrives while one is already showing, the fresh
  one carries the in-progress one's own `return_mode` forward instead of
  looping back to `"notify"` itself.

Both tiers are guarded against waking a panel deliberately released to
`off` (`Arcade.trigger_notify()` returns `False`, no state change) --
same reasoning `_loop()`'s plane-in-window trigger already uses: `off` is
a deliberate hand-off to WLED/Home Assistant lighting, not a state any
feature may override uninvited.

**Architecture note**: `notify.py` (new) holds config load/save, the
`hmac.compare_digest` auth check, and the urgent tier's one-shot
pending-payload slot -- the project's usual "one module per feature with
a config file" shape. It is NOT a `FEED`-shaped polling module like every
other module here (`weather.FEED`, `flights.FEED`, ...): there is nothing
to poll. Home Assistant PUSHES via the HTTP endpoint, so the "background
thread caches, engine reads the cache" shape those modules exist for
doesn't apply. The normal-priority banner's display-window STATE
(`_notify_banner`) intentionally lives on `Arcade` in `arcade_server.py`
instead of in `notify.py`, mirroring where `_alert_ticks` (severe
weather's own takeover state) already lives, since it's read every render
tick by the same loop that reads `_alert_ticks`.

**Verified live against the real running `com.henderburgh.arcade`
service** (`launchctl kickstart -k gui/<uid>/com.henderburgh.arcade`,
confirmed `/api/state` healthy before and after): wrong token and missing
token both returned 401 with `mode` unchanged; a `priority: normal` call
drew non-black pixels in the bottom-third band (1280 of 1280 checked
rows-worth) without changing `mode`; a `priority: urgent` call while in
`flights` mode swapped `mode` to `"notify"`, rendered a non-black frame,
and — critically — dismissing via `POST /api/input/left` returned `mode`
to the real prior `"flights"`, not a hardcoded fallback; a deliberately
oversized message exercised the `draw_marquee()` overflow path on the
urgent tier without crashing; releasing the panel to `off` and then
POSTing an urgent notify returned `{"ok": false}` and left `mode` at
`"off"`, confirming the off-guard. `render_audit.py` and `fold_audit.py`
both stayed clean (0 modes failed / 0 feeds not folding) before and
after. `notify` is intentionally absent from `render_audit.py`'s
`TEXT_MODES` list, same as `planewatch` -- it's force-triggered, not
part of the fixed sweep, verified manually instead (see above).

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

## Polling load (re-audited 2026-08-08 — task #21, "Cut redundant per-league polling" — see the full audit below the Sports coverage section; numbers here are REAL, measured this session, not estimates)

Every feed only polls while its mode has been read in the last
`IDLE_STOP` (120s), and only one mode renders to the panel at a time — so
steady-state load is whichever single mode is currently selected, with a
brief (≤12 0s) overlap right after switching modes while the previous
feed's thread hasn't idled out yet. Real per-mode request rates:

- ticker: 1 batched call/60s (crypto) + 1 call per stock symbol/60s.
- satellite: 1 call/12s (position) + 1 call/900s (pass prediction).
- flights: 1 call/15s (position) + up to 4 adsbdb lookups/refresh, capped.
- sports: **the old "7 ESPN calls/20s" figure (2026-07-30) is stale on
  two counts** — it assumed a flat per-league poll and a 7-league
  config, neither true today. `sports_config.json` currently configures
  **4** leagues (NFL/NBA/MLB/NHL), and `_interval_for()`'s existing
  LIVE/IDLE/EMPTY tiering (`SCOREBOARD_REFRESH_LIVE`=20s / `_IDLE`=300s /
  `_EMPTY`=1800s) already backs a league off hard once it has nothing
  live *today*. Measured live this session (2026-08-08, MLB in season,
  NFL/NBA/NHL all off-season): **MLB polled at the 20s LIVE tier, NFL/
  NBA/NHL each backed off to the 1800s EMPTY tier** — real total ≈ 1
  call/20s + 3 calls/1800s, not 7 calls/20s. Win probability adds 1 more
  call/20s, only while the pinned favorite's own game is live (unchanged
  by this audit — still the only source of win-prob/count/down-distance,
  see the dependency map below). **Tennis is a separate cadence**
  (`_refresh_tennis()`, not part of the `LEAGUE_PATHS` loop at all),
  same LIVE/IDLE/EMPTY tiers, measured this session at the 20s LIVE tier
  (real ATP/WTA action in progress) — 2 calls (ATP+WTA) at that cadence
  when live. **Per-game big-moment detector summary calls** (MLB HR,
  NFL/NCAAF TD, NHL goal, soccer goal, basketball clutch) are a further,
  separate, narrower category (one live favorite-game only) — a real
  bug in their cadence was found and fixed this session, see below.
- news: 1 call/300s.
- weather: 1 obs call/600s + 1 alerts call/120s (gridpoint cached ~daily).
- audio_sync: zero request cost — a blocking UDP socket, not polling.

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

## Radar-scope icon redesign round 2 (2026-08-08)

Owner-driven spec, aimed at making the aircraft kind readable at a glance
and giving the helicopter icon a shape that can never be mistaken for a
fixed-wing dash/cross.

- **Category color replaces altitude color** on the scope icon itself:
  `SCOPE_CATEGORY_COLOR` = airliner cool cyan-white, bizjet warm
  gold/orange, GA green, heli magenta. This is a real trade-off, not a
  free upgrade — altitude color is gone from the icon. Resolved
  deliberately: altitude is still shown as text on the DETAIL card (that
  was already the primary place altitude is read), so nothing is lost,
  only de-duplicated, and category at a glance is worth more on a 64x64
  scope than a second color channel for altitude.
- **Airliner/bizjet are now true swept darts**, not a nose+wing cross:
  one line from nose to each wingtip, computed from
  `SCOPE_ICON_AIRLINER = (3.4, 0.85, 2.4)` / `SCOPE_ICON_BIZJET = (2.5,
  0.75, 1.3)` (nose length, wingtip-fraction, wing spread) — bizjet
  reads shorter-nosed and narrower-swept than airliner at the same
  pixel budget.
- **Helicopter is now a filled disc + one body-stub pixel** pointing
  opposite the facing direction, replacing the old connected-T (rotor
  bar + mast + tail nub). Still mirrored by facing, not rotated by
  heading — a helicopter's nose direction isn't a reliable "forward"
  signal the way a fixed-wing heading is, so we don't fake one.
- **Window/notable brightness+size hierarchy, now combined correctly**:
  `WINDOW_GLOW_FLOOR = 0.92` (new, sits above the existing
  `NOTABLE_GLOW_FLOOR = 0.75`) is applied via `max(glow, floor)`, never
  additive — additive would risk exceeding 1.0 and clipping on an
  aircraft that's both notable and in-window. `big` is now true for
  `sel or matched or in_window`, so a plane inside the window physically
  draws larger, not just brighter. The violet diamond ring
  (`draw_window_ring()`) still marks window membership, but it's now
  explicitly the secondary signal — brightness/size carries the primary
  "this matters most" read, matching the same hierarchy language used
  for notable aircraft elsewhere.

Verified: `render_audit.py` 0 modes failed (pre-existing unrelated
MMA-name truncation warnings only), `fold_audit.py` 0 feeds not folding,
real service restart + `/api/frame` pulled clean post-restart. Reviewed
via synthetic 8x-zoom PNG sent for visual confirmation before commit.

## Planewatch -> Hangar hand-off gets its own transition (2026-08-08)

Every mode swap in the project used the same `transitions.DEFAULT_STYLE`
(`push_up`) at the single `transitions.blend()` call site in the render
loop -- including the plane-in-window takeover handing off into flights'
ceremonial Hangar detail card, despite that hand-off being (per the
owner) one of the two moments that "carry almost all the emotional
value." Gave it its own transition without touching any other swap:

- **`transitions.radial_open()`** (new) -- a circular iris reveal from
  panel centre outward (both axes, unlike `iris_open`'s horizontal-only
  band). Same cost class as every other style in the file: one
  slice-pair per row via a circle equation, zero per-pixel Python.
  Registered in `STYLES`, `DEFAULT_STYLE` untouched. Reserved for exactly
  this one hand-off -- `iris_open` stays owned by `SportsEngine`'s own
  internal ticker->detail expansion, a different mechanism entirely (that
  one blends an engine's own two frames directly, not a `set_mode()`
  swap).
- **`Arcade._trans_style`** (new attr on `Arcade`, `arcade_server.py`) --
  a single override slot, set inside `set_mode()` via one narrow explicit
  check (`"radial_open" if prev == "planewatch" and mode == "flights"
  else None`), not a general per-mode style table -- this is the only
  swap in the whole project that needs to differ, so a table would be an
  abstraction with a single real entry. The render loop's `blend()` call
  now reads `self._trans_style or transitions.DEFAULT_STYLE`.

Verified: both audits clean, real service restart + `/api/frame` pulled
clean post-restart (this touches the render loop directly, so the live
restart mattered more than usual here), plus a direct behavioral check
against a throwaway `Arcade` instance confirming `planewatch->flights`
resolves `radial_open` while `menu->gameday` and the reverse
`flights->planewatch` both stay on the untouched default.

## Brightness hierarchy extended to Hangar + DETAIL, Hangar dead-zone fixed (2026-08-08)

The scope's "important = brighter + bigger" language
(`NOTABLE_GLOW_FLOOR`/`WINDOW_GLOW_FLOOR`, `max(glow, floor)`, `big=`)
had no equivalent on the Hangar list or the normal (non-ceremonial)
select-to-expand DETAIL card -- a MAYDAY squawk and a routine regional
jet looked identical there, and a genuine first Hangar sighting had zero
visual distinction beyond its text label.

- **`_draw_plane_icon()` gained a `scale` param**, mirroring
  `draw_hero_silhouette(..., scale=...)`'s existing pattern -- no new
  drawing primitive needed, it multiplies the same nose/wing/tail/heli-dot
  geometry that was already there.
- **Hangar (`_frame_hangar`)**: a first sighting (`times_seen <= 1`) now
  draws at full brightness + 1.2x scale; a repeat visitor dims to
  `rim(HANGAR, 0.55)` at normal scale. No sweep to float above on a
  static card, so the ordering is expressed as "important floats up from
  a dimmed routine baseline" rather than the scope's "brighten above a
  moving sweep."
- **DETAIL card (normal browsing, not the ceremonial one)**: a notable
  aircraft now gets full color + 1.1x scale; routine dims via
  `rim(col, NOTABLE_GLOW_FLOOR)` -- reusing the scope's own constant as
  the floor value, not inventing a second one, so "notable" here means
  literally the brightness the scope would already show it at.
- **Hangar spacing fix**: the fixed text rows (19/31/38/45/52) became a
  y-cursor -- when airline was absent, 31->45 was an unexplained 14px
  dead zone. Rows now start immediately after whatever actually drew.

Explicitly untouched at the time: `_frame_detail_ceremonial` and
`PlaneWatchEngine` (just redesigned earlier this session) and
`draw_scope_aircraft`/`SCOPE_CATEGORY_COLOR` (round-2 icon work, also
this session). NOTE: `_frame_hangar`'s icon call was later swapped from
`_draw_plane_icon` to `draw_hero_silhouette` -- see "Hero silhouette
shape redesign" below -- so this section's Hangar icon description is
superseded; the brightness/scale hierarchy logic here is unaffected.

Verified: both audits clean, real service restart + `/api/frame` pulled
clean post-restart, reviewed via a synthetic 8x-zoom PNG (Hangar
first-sighting vs. repeat, DETAIL notable vs. routine) sent for visual
confirmation before merge.

## Hero silhouette shape redesign (2026-08-09)

`draw_hero_silhouette()` (round 1, previous section) was a single filled
dart per kind -- nose to swept wingtips to tail, varying only in overall
proportions. The owner's real-world test ("a plane-obsessed person
should be able to tell the category from the silhouette alone") failed
against it: all three fixed-wing kinds read as the same diamond at
different sizes, and the helicopter's bare stroked rotor ring read as
noise, not a rotor. Three iterative rounds (each reviewed via synthetic
PNG before the next):

- **Fixed-wing kinds are now TWO separate filled shapes**, not one: a
  real fuselage hexagon (nose -> shoulders -> tail) plus a distinct pair
  of swept wing triangles attached at the shoulders. `_HERO_FIXED_WING`
  holds 7 params per kind (nose/tail length, fuselage half-width, wing
  mount y-offset, wing span, wing sweep, wing chord) -- three independent
  axes of real variation, not one overall-size knob.
- **Airliner vs. bizjet were pushed apart on TWO axes in the SAME
  direction** (round 3, after round 2 still read too close): airliner
  got the LEAST sweep (wings closer to perpendicular, a wide/composed
  read) and the longest body; bizjet got the MOST sweep by a wide margin
  (a real needle angle) and a much shorter/slimmer body. Length and sweep
  moving together instead of partially cancelling is what actually
  separated them -- confirmed against real logged Hangar entries (a real
  757, a real Citation-family jet) side by side, not just synthetic
  shapes.
- **GA got a thicker fuselage and the heaviest wing chord of the three**
  (round 3) so it reads as compact-and-solid rather than "the thin one",
  with wings mounted forward and almost no sweep for the
  stubby-utilitarian silhouette a high-wing GA aircraft actually has.
- **Helicopter got its own language entirely**: compact filled body +
  thin tapered tail boom (asymmetric front/back mass no fixed-wing dart
  has) + NO wings at all, under a real filled rotor disc (dim fill +
  full-color rim + two blade lines) replacing the old bare stroked ring.
  Confirmed by the owner as "the clear winner ... no ambiguity."
- Airliner's total nose+tail length (25.5 units) is ~1.46x bizjet's
  (17.5 units) at the shape-definition level -- a ratio, not an absolute
  pixel count -- so the owner's "airliner should keep more mass than
  bizjet at final card size" requirement holds automatically at whatever
  scale factor a given card uses, without per-card tuning.
- Since all three call sites (Hangar, ceremonial DETAIL, PlaneWatch
  takeover) already called the shared `draw_hero_silhouette()` function
  (Hangar was switched to it in the "Brightness hierarchy" work above,
  same day), this redesign required NO call-site changes -- only the
  shape geometry inside the one shared function.

Verified: both audits clean every round, real service restart +
`/api/frame` pulled clean post-restart on the final round, real logged
Hangar entries (one of each kind: airliner, bizjet, GA, helicopter)
rendered and sent as a synthetic PNG for visual confirmation before each
iteration, final approval given by the owner before commit.

## Hero silhouette elevation (2026-08-08)

Both hero moments -- the plane-in-window takeover (`PlaneWatchEngine`)
and the ceremonial Hangar detail card (`FlightEngine._frame_detail_ceremonial`)
-- got more visual mass and a subtle sense of life, per the owner's
explicit "these two moments carry almost all the emotional value" spec.

- **Takeover screen**: hero silhouette scale 1.1 -> 1.25. To let the
  bigger silhouette clear the ident text above it instead of just eating
  margin, `cy` moved 34->35 and the ident row moved y=11->y=9 (tighter
  vertical rhythm, not just a bigger shape floating in the same space).
- **Hangar ceremonial card**: hero silhouette scale 0.8 -> 0.9 -- a
  smaller bump than the takeover's own, deliberately: this card's
  vertical budget is tighter (header + type-name row + up to 3 more
  status/age/airline rows below), so it doesn't have the same room to
  grow without crowding the type-name row at y=38.
- **Breathing glow**: both hero silhouettes now pulse brightness
  ±6% (`0.94 + 0.06*sin(ticks*0.05)`, via `rim()`) -- slow and subtle by
  design, meant to read as "alive," not as flicker or a distraction from
  the ident/type text. Same technique on both cards for a consistent
  feel across the two hero moments, per the "ruthless consistency of
  language" principle.

Verified: `render_audit.py` 0 modes failed, `fold_audit.py` 0 feeds not
folding, real service restart + `/api/frame` pulled clean. Both cards
have zero coverage in the normal `render_audit.py` sweep (force-triggered
only, same as the round-1 icon work), so reviewed via a direct
force-injected synthetic render (airliner + helicopter, both cards) sent
as an 8x-zoom PNG for visual confirmation before commit -- confirmed no
text collisions at the new scale on either card.

## Full-project gap audit (2026-08-09)

After the flights-mode deep dive above, ran a dedicated research-only
audit of the REST of the project for the SAME real bug classes this
project has already been bitten by, rather than vague polish notes.
Found and fixed two real gaps; three more were found and deliberately
left as documented, lower-priority follow-ups (not silently dropped):

**Fixed:**
- **`fold_audit.py` had zero coverage for `weather.py` and the
  `/api/notify` boundary.** Both fold real external text through
  `paneltext.panel_text()` (severe-alert `headline`/`areaDesc`, HA
  notification `title`/`message`) but neither had a replay case --
  exactly the "live data happened to be clean, so nothing caught it"
  trap this file's own docstring warns about, with a real precedent
  (Rasmus Hojgaard's name, four feeds still on bare `.upper()`, the
  sports per-league parser) already on record. Added three weather cases
  (observation, alerts, point/city-state -- via monkeypatching
  `weather._get_json`, not the network call, so real live payloads still
  drive the injection) and one notify case (`/api/notify`'s fold lives
  inline in `arcade_server.py`, which this file can never import per the
  panel-lockup rule, so the fold FUNCTION is exercised directly on the
  same input shape, same reasoning the skypass/atc_transcribe cases
  already use for an inline fold).
- **`market.py`/`news.py`/`blog.py`/`notify.py`'s `save_config()` all
  constructed a fresh dict instead of read-modify-write.** The exact bug
  shape that has already silently wiped a sibling key in
  `location_config.json` (`airport`) and `sports_config.json`
  (`golf_player`) TWICE this project's life -- harmless today because
  each of these four files currently has one owner and one key-set, but
  the first time any of them grows a second key, a save of the other key
  silently wipes it. Fixed preemptively, same read-modify-write pattern
  `sports.save_config`/`flights.save_airport`/`satellite.save_window`
  already use. Verified the round-trip is actually safe (`save_config`
  called twice back-to-back on the real `market_config.json`, file
  identical before/after) before committing.

**Found, all three now fixed (documented, not dropped):**
- ~~`PlaneWatchEngine` and `NotifyEngine` get zero coverage from the
  normal `render_audit.py` sweep~~ -- FIXED, see "render_audit.py
  coverage for planewatch/notify" below.
- ~~`sports.py`'s `_detector_due()` throttle fix (task #21) has no
  permanent regression test~~ -- FIXED, see "Two remaining gap-audit
  follow-ups" below.
- ~~`hangar.py`'s `HANGAR_MAX_ENTRIES = 500` LRU eviction has no test that
  actually exercises the boundary~~ -- FIXED, see "Two remaining
  gap-audit follow-ups" below.

Verified: `fold_audit.py` 0 feeds NOT folding (weather alerts case
correctly reports "skipped (no active alerts)" when there genuinely
are none, matching the existing skip convention rather than faking a
payload), `render_audit.py` 0 mode(s) failed, real service restart +
`/api/frame` pulled clean post-restart.

## render_audit.py coverage for planewatch/notify (2026-08-09)

Picked up the first "deliberately not fixed this pass" item from the gap
audit above. `PlaneWatchEngine`/`NotifyEngine` are force-triggered-only
(reset() pops a real one-shot slot -- flights' window-entry batch,
notify's pending payload -- that only a genuine live event or a real
`/api/notify` POST ever fills), so the generic `drive()` loop would
construct them, see an empty slot, and render only the blank fallback --
passing clean while covering none of the real content.

- **`render_audit.py` gained `CUSTOM_DRIVERS`**, a small dispatch table
  keyed by mode name, checked in `main()` before falling back to the
  generic `drive()`. `drive_planewatch()` bypasses the FEED pop (there is
  deliberately no public "push a batch" API -- the real slot only exists
  to be filled by a real detector) the same way this session's own ad hoc
  verification scripts already did: construct via `__new__`, set
  `.batch`/`.idx`/`.ticks` directly. `drive_notify()` DOES have a real
  public API (`notify.push_pending()`), so it drives the engine through
  its actual `reset()`/pop path instead.
- Each driver exercises 2-3 real variants: planewatch gets a single
  notable aircraft, a two-aircraft batch (the "N/M" position counter +
  idx cycling), and a minimal aircraft with no reg/type/notable data at
  all (the honest-gap fallback text); notify gets a short message, a long
  message (forces the wrap-to-3-lines-or-marquee-overflow branch), and an
  empty payload (the honest-empty fallback, not a crash).
- **A real, if minor, pre-existing bug surfaced immediately**: notify's
  static `"PRESS ANY BUTTON"` footer had never been passed through
  `fit_text()`, unlike every other static string in the project, and
  overflowed the panel by 1px at scale 1 -- invisible until this exact
  string got real render-audit coverage for the first time. Fixed
  (`fit_text("PRESS ANY BUTTON", WIDTH - 4)`).
- One methodology trap caught before it shipped: the first draft of
  `drive_notify()` pushed raw mixed-case synthetic text directly via
  `notify.push_pending()`, which reported a false DROPPED-lowercase
  failure. `NotifyEngine.frame()` deliberately does NOT fold its own
  text -- this project's fold discipline is ONE boundary per feed
  (`arcade_server.py`'s `/api/notify` handler folds before ever calling
  `trigger_notify()`), not defense-in-depth at every draw site. Real
  production payloads always arrive pre-folded; the driver was fixed to
  pre-fold its synthetic payloads through `paneltext.panel_text()` the
  same way, matching production reality instead of testing a shape of
  input that can never actually occur.
- `notify` also joined `MARQUEE_OK` (its message row falls back to
  `draw_marquee()` when text doesn't fit three lines) -- narrowly, after
  confirming the 1px footer overflow above was a REAL bug and fixing it
  first, rather than laundering it through the marquee exemption.

Verified: both audits clean (`render_audit.py planewatch notify`
individually and the full sweep), real service restart + `/api/frame`
pulled clean post-restart.

## Two remaining gap-audit follow-ups (2026-08-09)

Picked up the last two "deliberately not fixed this pass" items. Both
needed a NEW small dedicated script rather than fitting into
`render_audit.py`/`fold_audit.py` -- neither is a text/render/fold
concern, so forcing them in would have muddied what each existing tool
actually promises.

- **`detector_throttle_audit.py`** (new) -- proves `SportsEngine.
  _detector_due()` (the task #21 throttle fix) actually caps the real
  call rate, not just that isolated before/after timestamp comparisons
  look right. The real regression check simulates the ORIGINAL bug's
  exact cadence -- 400 calls at the real `tick_rate` (0.05s) spacing,
  ~20 simulated seconds -- by monkeypatching `engines.time.time` (not
  the global `time` module; only the one reference `_detector_due()`
  itself reads) so the real function runs unmodified against a
  fast-forwarded fake clock instead of the test needing to sleep 20 real
  seconds. Asserts at most ~1 due call in that window, not 400 -- if the
  throttle gate is ever removed or inverted, this fails immediately
  instead of silently regressing to hammering ESPN's summary endpoint
  again. Also covers: first-call-is-always-due, independent detector
  keys never gate each other, and the gate correctly resets its own
  timestamp on becoming due.
- **`hangar_audit.py`** (new) -- proves `HANGAR_MAX_ENTRIES = 500`
  eviction actually happens at the boundary, LRU by `last_seen`, not
  merely asserted in `hangar.py`'s own docstring. NEVER touches the real
  `hangar.LOG` singleton or `hangar_log.jsonl` (which holds a real,
  live collection -- 500 real logged aircraft as of this session) --
  every check constructs a fresh `HangarLog()` against a temp path,
  with `hangar.HANGAR_PATH` patched for the duration and restored (and
  the real path captured at IMPORT time, before any patching, so the
  safety assertion can't be fooled by comparing the patched value to
  itself). Fills to exactly the cap, confirms a 501st distinct
  registration evicts EXACTLY the least-recently-seen entry (not an
  off-by-one neighbor), confirms a REPEAT sighting of an existing entry
  never evicts anything (eviction only fires on `record_sighting()`'s
  genuinely-new-registration branch), confirms touching an old entry's
  `last_seen` protects it from being the next victim (real LRU, not
  insertion order), and confirms a fresh `HangarLog()` reloading the same
  temp file gets back exactly what was saved.

Verified: both new scripts pass clean (`detector_throttle_audit.py`
8/8 checks, `hangar_audit.py` 12/12 checks), confirmed via `git status`
that `hangar_log.jsonl` (the real collection) has zero diff after
running `hangar_audit.py`, both standing audits (`render_audit.py`/
`fold_audit.py`) still clean, real service restart + `/api/frame`
pulled clean post-restart.

## Manual plane-in-window demo trigger (2026-08-09)

The owner had never actually SEEN `PlaneWatchEngine`'s takeover fire on
the real panel -- it only triggers when a real aircraft crosses the
configured window cone, which hadn't happened to be caught live yet.
There was no way to demonstrate an already-built, already-verified
feature in person without waiting on real traffic.

`POST /api/test/planewatch` (new, PERSONAL-RIG-ONLY, same scope
precedent as `atc.py`'s transcription feature) pulls the closest REAL
currently-tracked aircraft from `flights.FEED.get()`'s own live cache
(the exact data the radar scope is already drawing) and seeds it into
the SAME real one-shot slot (`flights.FEED._window_batch`) the real
window-entry detector fills. Never fabricates an aircraft -- this
manually satisfies the trigger condition for a real, currently-tracked
aircraft, it does not invent one. The render loop's existing
`_loop()` plane-in-window check pops it and swaps modes on its own very
next tick, reusing that real path end to end rather than duplicating any
trigger logic.

Returns `{"ok": false, "error": "no tracked aircraft right now"}` (404)
honestly rather than faking a result when the sky is genuinely empty --
confirmed this working as designed: first live call after this shipped
found zero aircraft currently tracked, an honest real-time state, not a
bug in the endpoint.

Verified: both audits clean, real service restart + `/api/frame` pulled
clean post-restart.

## Persistent recent-events log (2026-08-09)

Owner's own words: "A simple recent events / log view (last planes that
came through the window, last big sports moments, last HA notifications).
People like being able to look back." Built by a background agent,
independently re-verified (audits, real live restart, real merge) before
trusting it, same discipline as every other delegated build this session.

`events_log.py` (new) is a persistent JSON-lines log, same proven shape
as `hangar.py`: lock-protected in-memory list, lazy-loaded, count-bounded
(`EVENTS_MAX_ENTRIES = 500`, reusing `hangar.py`'s exact eviction
pattern — already proven correct at the boundary by `hangar_audit.py`
this same session). `record(kind, summary)` takes an ALREADY-FOLDED
summary string — same "fold at the boundary, caller folds" discipline
every feed in this project follows; the module itself never folds.

Three real wiring points, each recording at the exact place the real
text is already finalized, not re-derived:
- `flights.py` — one entry per real aircraft that newly enters the
  configured window (inside the real window-entry diff block).
- `engines.py`'s `BigMomentSource._set_big_moment()` — gated to
  `system == SYSTEM_SPORTS` only (this same method also fires for
  flights/satellite moments; the owner's ask was specifically "big
  sports moments").
- `arcade_server.py`'s `/api/notify` handler — right after the existing
  `paneltext.panel_text()` fold, before `trigger_notify()`.

`EventsLogEngine` (new, `ENGINES["events"]`) is a standalone engine, not
bolted onto an existing mode — justified in its own docstring: this log
spans three genuinely separate systems (flights/sports/home automation)
that don't otherwise share an engine, unlike THE HANGAR (inherently
about flights) or the ATC log (inherently about flights), so a
standalone engine + its own menu tile is the honest structural fit, same
category of decision as THE HANGAR/ATC log both making that same choice
for their own single-system logs. Two-line rows (kind+age, then
summary), 3 per page, real relative-age text via
`FlightEngine._fmt_age_long` (reused, not reimplemented), honest
"NO EVENTS / YET" empty state.

`render_audit.py` gained a `drive_events` custom driver (the real log is
honestly empty on a fresh device, so the generic sweep would only ever
exercise the empty-state fallback) — injects real-shaped synthetic
entries directly onto the engine's own `.entries`, never touching the
real `events_log.jsonl` file.

Verified: both audits clean (confirmed via `git status` that no stray
`events_log.jsonl` was created by any audit run), real service restart,
real mode-switch to `events` via `/api/mode/events` and a real `/api/frame`
pull showing the honest empty state (nothing has been logged on this
device yet — the wiring only just shipped).

## Follow-a-specific-flight mode (2026-08-09)

Competitive-research ask: "track any specific flight by number, one of
the most loved features on products like Mach 2." Confirmed live before
committing to building it: `api.adsb.lol` has a global
`/v2/callsign/{callsign}` lookup, genuinely separate from the local-
radius `POSITION_URL` this project's radar scope already uses -- same
`{"ac": [...]}` payload envelope and per-aircraft field names, just not
bounded to `RADIUS_NM` of home. A callsign that isn't currently airborne
returns a real, honest `"ac": []` (confirmed: `curl .../callsign/UAL123`
-> `{"ac": [], "total": 0}`), not an error.

- **`flights.FollowFlightFeed`** (new) -- its own feed class, not a
  second mode bolted onto `FlightFeed`: that class's identity is "every-
  thing within `RADIUS_NM`, a list re-sorted by notability"; this is
  "one specific real flight, wherever it is," a single optional value
  with a genuinely different honest-empty state. `get()` returns a real
  tri-state `airborne`: `None` (not configured), `False` (real lookup
  ran, genuinely not airborne right now), `True` (a real cached
  position). Own config file (`follow_flight_config.json`), read-modify-
  write from the start per the 2026-08-09 destructive-overwrite lesson.
  Reuses the existing adsbdb route-enrichment cache verbatim -- zero new
  I/O beyond the one 15s-interval callsign poll itself.
- **`FollowFlightEngine`** (new, `ENGINES["followflight"]`) -- a
  standalone engine, not a fifth `FlightEngine` view: `FlightEngine`'s
  DETAIL/ceremonial views are built entirely around home-relative
  framing (bearing, DEPARTING/ARRIVING vs. the configured home airport,
  local-plane trail math in x_nm/y_nm), none of which applies to a
  flight that could be anywhere on Earth. Reuses what generalizes
  cleanly (`draw_hero_silhouette`, `FlightEngine._ac_kind`/`_alt_color`/
  `_compass`, `flights.ICAO_TYPE_NAMES`) rather than threading a "no
  home concept" branch through the existing local scope/window/Hangar
  code -- matches this project's own standing rule not to touch that
  code for a genuinely separate feature.
- **`GET`/`POST /api/flights/follow`** (`{"callsign": "UAL123"}`), same
  JSON-body shape as every other config POST endpoint here.
- Accepts callsigns as typed (ICAO flight-number format --
  airline-ICAO + number, e.g. "UAL123", not the IATA "UA123" a traveler
  would recognize) -- no IATA<->ICAO translation table was built
  (real, deliberate scope limit, stated in `FollowFlightFeed`'s own
  docstring); a callsign that doesn't resolve just honestly shows NOT
  CURRENTLY AIRBORNE rather than being rejected or guessed at.

**Merge note**: this and the recent-events log agent both independently
added a `drive_*`/`CUSTOM_DRIVERS` entry to `render_audit.py` -- a real
merge conflict, resolved by keeping both (`drive_events` and
`drive_followflight` are fully independent, no logic changed from
either).

Verified: both audits clean (including the merged `render_audit.py`'s
combined driver set), real service restart, real endpoint round-trip
live (`POST /api/flights/follow` with a real callsign, confirmed via
GET, cleared afterward), real mode-switch to `followflight` and a real
`/api/frame` pull showing the honest NOT CURRENTLY AIRBORNE state for a
real (currently non-airborne) callsign.

## Do Not Disturb / focus mode (2026-08-09)

Real manual toggle (`dnd.py`, `GET`/`POST /api/dnd`, `{"enabled": bool}`)
that suppresses DISCRETIONARY interrupts while never touching the two
things that are never optional:

- **Suppressed by DND**: the plane-in-window takeover
  (`arcade_server._loop()`'s trigger check), sports `TIER_INTERRUPT`
  celebrations (`AmbientEngine.tick()`'s big-moment gate), normal-
  priority HA banners (`trigger_notify()`).
- **Never suppressed**: severe-weather takeover
  (`_severe_alert_frame()`, a completely separate code path, life-
  safety), urgent-priority HA notifications (explicitly escalated by a
  real HA automation, the same "still gets through" exception iOS/
  Android Focus modes use), `TIER_TAKEOVER` sports moments (the rarest,
  genuinely "go look now" tier -- matches urgent notify's own
  escalation exception).

**Suppressed, not deferred** -- a discretionary event is popped/
discarded while DND is on, not left queued for when DND turns back off.
A stale home run or a plane that already left the window showing up
minutes later would be real data at the wrong time, its own kind of
dishonest, matching this project's "never invent" spirit one step
removed (not inventing a NEW fact, but presenting a real one as if it
just happened when it didn't).

Deliberately a plain manual toggle, not a schedule -- night/quiet-hours
BRIGHTNESS dimming already exists (`brightness.py`, confirmed already
live and wired into the render loop when checked this session, not a
gap) and is a genuinely separate concern from "am I actively trying not
to be interrupted right now."

Verified: both audits clean, real service restart, and the actual
suppression/bypass behavior confirmed live end-to-end -- a real
`/api/notify` normal-priority POST while DND was on returned `ok: true`
(receipt acknowledged, matching `events_log.py`'s own "a real event
happened whether or not the panel was awake to show it" reasoning)
without swapping mode; a real urgent POST swapped mode to `"notify"` as
normal, confirming the bypass. `AmbientEngine`'s `TIER_INTERRUPT`-vs-
`TIER_TAKEOVER` gating verified directly against a fake sports engine's
real peek/pop contract (not just read): `TIER_INTERRUPT` correctly
suppressed+discarded while DND is on, fires normally while off,
`TIER_TAKEOVER` still fires regardless.

Also added `events_log.jsonl` to `.gitignore` in this pass -- same "real
runtime state, not source" category as `hangar_log.jsonl`/`atc_log.jsonl`,
missed when `events_log.py` shipped earlier this session.

## Control panel companion dashboard (2026-08-09)

Owner ask: `arcade.html` should be "the perfect companion page for this
device" -- the real, primary way to see and control everything the
panel can do, now that henderburgh.com/arcade is explicitly out of
scope (no local access to that site's source, and its own "Control
Panel" link points at a LAN-only IP anyway -- confirmed live via
WebFetch before deciding to skip it, not assumed).

**Real audit before building**: cross-checked `engines.ENGINES` against
`arcade.html`'s own `data-mode="..."` buttons. Result: every real
selectable mode already had a button except `boot`/`menu`/`notify`/
`planewatch`, correctly excluded (force-triggered/internal). The one
real, confirmed gap: `favorite_aircraft` had a real, working, live-
tested API (`GET`/`POST /api/flights/favorites`) and ZERO control-panel
UI.

- **Favorite-aircraft card** -- add/remove real registrations, same
  growable-list interaction `favorite_teams`'s own card already
  established (add input, real list with per-item remove, calling the
  real endpoint). Shown only under `flights` (not `followflight`, which
  has no home-relative concept at all per its own docstring).
- **`GET /api/events`** (new) -- `events_log.py`'s log had genuinely
  zero HTTP surface before this (only ever read from inside
  `EventsLogEngine`). Small, read-only, `limit` query param.
- **Status overview card** (right rail) -- current mode (with
  `planewatch`/`notify` labeled plainly as takeovers, off `/api/state`,
  already polled every render tick, no new request), real severe-weather
  alert state (from weather's own real `alerts` list), and a real
  recent-events feed (`/api/events`). Confirmed working end-to-end live,
  not just built: after DND testing (see above), this card correctly
  showed the real HA notification events that testing actually
  generated.
- **Do Not Disturb toggle** -- added into this same status card
  (`dnd.py` was built in the same session pass but after this dashboard
  work started, so the agent's version had no DND UI yet; added
  directly afterward). Verified live in a real browser: clicked the
  real toggle, confirmed via a direct `curl /api/dnd` that the backend
  state actually flipped both directions, confirmed zero console
  errors, reset to the default OFF state afterward.

**Final cross-check** (per the agent's own task instructions): every
real `data-mode` button maps to a real `engines.ENGINES` key (or the
special-cased `mirror`/`video`/`cast`), and every real config endpoint
now has a UI card. Nothing removed or hidden.

**Merge note**: a real merge conflict in `arcade_server.py` -- this and
the DND work both independently added a new GET endpoint
(`/api/dnd`, `/api/events`) at the same insertion point. Resolved by
keeping both, no logic changed from either.

Verified: `ast.parse()` clean, real service restart, page loaded in a
real browser with zero console errors, the DND toggle exercised live
end-to-end as described above.

## Departure board -- built from data already fetched, no paid API (2026-08-09)

CLAUDE.md's own "Data sources CHECKED AND REJECTED" section already
covered a traditional departure board (OpenSky flights endpoints need
OAuth2, adsb.lol has no airport endpoint, AviationStack/AirLabs/
AeroDataBox all need paid keys, nasstatus.faa.gov is national delay
status, not a flight board) -- re-confirmed BOTH free-source rejections
still hold with a live check before building anything (`curl
api.adsb.lol/v2/airport/KMYR` -> still 404, `curl opensky-network.org/
.../flights/arrival` -> still 403).

The honest, real, free alternative: `FlightEngine._route_status()`
already classifies a currently-tracked aircraft as DEPARTING/ARRIVING
against the configured home airport, using adsbdb's real resolved
route -- built for the DETAIL card's header tag, reused verbatim here
(not re-derived) across every currently-tracked aircraft instead of
just the selected one. Real local traffic actually departing from or
arriving at the configured home airport right now -- not a claimed
schedule, and honestly more useful for one specific home location than
a generic FIDS full of regional hops nobody there is watching for.

New standalone engine (`DepartureBoardEngine`, `ENGINES["departures"]`),
same design precedent as `FollowFlightEngine`: a genuinely different
presentation (a scannable board) of data `FlightEngine` already has,
kept off `FlightEngine`'s own up/down axis (already claimed by THE
HANGAR toggle) rather than overloading it. Zero new I/O -- reads
`flights.FEED.get()`'s already-cached aircraft list (each dict already
carries a real `route` from the feed's own background enrichment) and
`flights.load_airport()`.

**Real bug caught by `render_audit.py`'s own TRUNCATED check before
shipping**: the arrow+city line was built as one `fit_text()` call
(`f"{arrow} {other}"`) -- `fit_text()` drops whole trailing words, so a
long city name (`"RALEIGH/DURHAM"`) got dropped ENTIRELY, leaving just
the bare arrow with nothing else on the line. Fixed by fitting the city
into its own reserved width budget (`WIDTH - 4 - text_w(arrow + " ")`)
so the arrow always survives and the city truncates normally instead of
vanishing.

**Also found and fixed, unrelated to this feature**: `sports_config.json`
had lost its `golf_player` key (Yealimi Noh, documented above as
intentionally pinned to give the golf big-moment detector a real live
target) -- real state drift, not caused by anything touched this pass.
Restored via the real `sports.save_golf_player()` function, not a
hand-edit, so the config's own read-modify-write path is what wrote it
back.

Verified: both audits clean, real service restart, real mode-switch to
`departures` and a real `/api/frame` pull confirmed live (honestly empty
right now -- no real MYR-bound traffic at check time, the correct empty
state, not a bug).

## Real dropdowns + favorite-teams UI + city bulk-add (2026-08-09)

Two real gaps in `arcade.html`'s sports card, closed together: leagues/
pin-team were free-text (a raw comma string, a raw team-abbreviation
input -- typo-prone, no discoverability), and `favorite_teams` (the
cross-sport ticker watchlist -- backend + `/api/sports/favorite_teams`
already built and documented earlier this session) had genuinely NO
control-panel UI at all, missed by the earlier dashboard cross-check
(that pass only checked "does a config endpoint have SOME card", not
"is every field on that card actually usable").

- **`sports.fetch_teams()`/`fetch_all_teams()`** (new) -- real ESPN team
  lists (`location`/`abbreviation`/`name`) via `site.web.api.espn.com`,
  the same host every other endpoint here already switched to
  (`site.api.espn.com` blanket-403s from this network -- re-confirmed
  live before adding a new endpoint against it, not assumed still true).
  Cached 24h per league in-process -- real rosters/abbreviations don't
  change faster than that. New `GET /api/sports/teams`.
- **Leagues** is now a real multi-select; **pin team** is two real
  dropdowns (team list populated from whichever league is selected).
- **New favorite-teams section**: real add/remove list (same growable-
  list pattern `favorite_aircraft`'s card already established) plus a
  **city bulk-add** -- type "Pittsburgh", click Add all, and every real
  team (any configured or unconfigured league) whose real ESPN
  `location` contains that text gets added in one click. Pure client-
  side match against the already-fetched real team data, no new
  endpoint needed for the matching itself.

Verified end-to-end live, not just built: real `/api/sports/teams` pull,
the real city-match logic run against it (found exactly the 3 real
Pittsburgh teams -- Steelers/NFL, Pirates/MLB, Penguins/NHL, no NBA/EPL/
NCAA false positive), a real `/api/sports/favorite_teams` round-trip
(set all three, confirmed via a real browser DOM check showing the
correct real team names rendered, removed one via a real button click
confirmed by a second GET, cleared back to empty afterward). Both
standing audits re-run clean (unaffected -- control-panel UI only, no
render-path changes).

**CORRECTION (found during the next session pass, building the setup
wizard below)**: "no test data left behind" above was WRONG --
`favorite`/`favorite_teams`/`leagues` were all found still carrying real
test values (NFL/PIT, all three Pittsburgh teams, a reordered league
list) well after this section claimed a clean revert. Root cause: mixing
direct `curl` calls (used to verify the backend) with a browser tab left
open on the same card, whose own JS-side `favTeams`/dropdown state was
stale relative to the curl-driven changes -- a later save from that
stale tab re-wrote the pollution back. Real methodology lesson, not a
product bug: **when verifying via both curl and a live browser tab
against the same config, reload the browser tab (or close it) before
trusting a curl-based revert** -- a stale open tab can silently
re-save what a curl call just cleared. Reverted for real via the live
`/api/sports/*` endpoints (not a file hand-edit, so the running
service's own in-memory cache picked up the fix), confirmed via
`git diff sports_config.json` showing zero diff.

## Now Playing -- Last.fm + a real mic visualizer (2026-08-09)

Wired up the Last.fm feed built earlier this session:
`NowPlayingEngine` (`ENGINES["nowplaying"]`) + a real control-panel card.

**Two independently real signals, neither fabricated.** Track/artist/
album come from `nowplaying.FEED` (Last.fm). The visualizer bar is a
SEPARATE real source -- `audio_sync.py`'s existing real 16-band FFT off
the panel's own mic, the identical data WLED-MM's own on-device
AudioReactive visualizer uses. `nowplaying.py`'s own docstring already
explains why the mic alone could never do Now Playing (volume + FFT
only, no song identity) -- this combines the two rather than either
faking a track from the FFT or faking bars from nothing. The bars are
gated on `audio_sync.FEED`'s own real `stale` flag, independent of
whether Last.fm resolved a track -- an idle/silent bar row would be
worse than no row (the same "never invent a visual" rule
`backgrounds.py`'s WLED-effect capture already follows), and one signal
being honestly absent must never hide the other.

Three real states (not configured / configured-but-honestly-nothing-
playing / playing), the same tri-state shape `FollowFlightEngine`/
`DepartureBoardEngine` already established.

**A real bug caught by the generic `render_audit.py` driver on the very
first run**, before any custom driver existed: the "SET ONE FROM THE"
empty-state line overflowed the panel by 4px. Fixed
(`"SET ONE FROM" / "CONTROL PANEL"`), THEN a `drive_nowplaying` custom
driver was added (4 variants covering all three states plus a forced
marquee case) and `nowplaying` joined `MARQUEE_OK`.

**A real destructive-overwrite risk caught and fixed BEFORE it could
ever happen live** -- the same bug class `location_config.json` has
hit twice already. The `GET /api/nowplaying` endpoint deliberately never
echoes the real API key back (matching the HA notify token's own
credential-handling care), which means an ordinary "just update my
username" save would naturally omit `api_key` -- and the first draft of
`nowplaying.save_config()` treated a missing key as "clear it". Fixed
with the exact "omitted preserves the existing value, an explicit
`clear_key=True` is the one real path that removes it" pattern
`satellite.save_window()`'s own `max_nm` parameter already established.
Verified against the REAL LIVE running service, not just a unit test:
set both fields, saved user-only with the key omitted, confirmed via a
second real GET that `has_key` stayed `true` -- the bug would have
silently wiped a real working key on the very first ordinary edit.

`nowplaying_config.json` added to `.gitignore` -- it will hold a real
Last.fm API key once configured, same credential category as
`notify_config.json`.

Verified: both audits clean, real service restart, real mode-switch and
`/api/frame` pull (honest not-configured state), the full real
save/preserve/clear round-trip against the live service, config reverted
to clean defaults afterward.

**Honest gap, stated plainly**: no real Last.fm API key is configured
on this device yet (the owner hasn't provided one), so the response
PARSING in `nowplaying._parse_now_playing()` is correct-but-unverified
against a genuinely live payload -- only the error path is verified
against real live behaviour (Last.fm's real invalid-key response, tested
earlier this session). Re-check the real field names the first time a
real key is added, per this project's own "ship correct, flag honestly"
precedent (NHL goal detector, MMA finish detector, the flight-path map's
own real-traffic gap just above).

## Full review of everything built this session: odds silently broke on 6 sports, hockey contrast bug, court palette mismatch (2026-08-11)

Direct owner ask: "let's look at all implementations created, let's make everything better." Two parallel read-only review passes (visual/design consistency across all six hero treatments + skins + weather icons + ticker; completeness/consistency across odds, layout pilot, player badges, shot court, racing, skins, and the six new sport renderers) -- same "find real, defensible issues, say so honestly if something checks out clean" instruction as the earlier logic audits, applied to design quality and feature completeness instead of correctness.

**Real bug found and fixed: odds silently stopped rendering on the six sports that got dedicated hero renderers this session -- exactly the sports most likely to actually be viewed.** `sports._parse_odds()`/`_footer_odds_text()` were only ever wired into the two GENERIC fallback renderers (`_frame_universal_generic()`, `_frame_event_detail_generic()`). Every one of the six new dedicated detail renderers (baseball/football/basketball/hockey/soccer/MMA) built its own hardcoded `series > venue > broadcast > note` footer chain and never called the odds helper at all -- so the moment a sport graduated from the generic fallback to its own hero treatment, the owner-requested odds feature quietly vanished for it. Only golf/tennis/racing (never given a dedicated renderer) still showed odds. Fixed by prepending `self._footer_odds_text(ev)` to all six dedicated renderers' existing footer-line lists (pre-game only, same as everywhere else this field appears) -- real data restored without collapsing the richer multi-line footer these renderers earned down to `_footer_pick()`'s single-winner design (a different, deliberately-not-adopted fix -- see below).

**Real contrast bug found and fixed: the hockey rink hero's ice color and the default skin's LIVE yellow sat at nearly identical real luminance** (`RINK_ICE` ≈221 vs `LIVE` ≈220) -- on real LED hardware the period/clock text would read as a near-invisible smear, the opposite of every other hero backdrop (all five others sit in a dark luminance-25-to-90 band specifically so light ink pops). This wasn't skin-specific -- every skin's `live` color is bright, so the bug reproduced under all of them -- confirming the fix belonged in `RINK_ICE` itself, not a per-skin special case. Darkened to `(130, 155, 185)`, restoring both real contrast and visual consistency with the other five hero backdrops.

**Real palette inconsistency found and fixed: the celebration's NBA mini shot-court and the basketball detail view's own court hero used two completely unrelated palettes** for the "same" court (cool gray-blue vs. warm hardwood) -- a user would see two visually unrelated courts moments apart for the same sport. Fixed by having `draw_mini_court()` reuse `draw_basketball_court_hero()`'s own `COURT_WOOD`/`COURT_WOOD_LINE` palette (dimmed for its smaller scale) instead of a second, bespoke one.

**Real gaps found, deliberately NOT fixed this pass, documented instead of silently dropped:**
- **The Tier-3 layout-priority pilot (`layout.py`) genuinely only controls 2 of 9 sports' footer content** (the same two generic-fallback renderers odds had the same problem in) -- the six dedicated renderers' footer order is still hardcoded, not config-driven, contradicting the pilot's own stated "one config, both views agree" intent. NOT unified this pass: `_footer_pick()`'s whole design picks ONE winning field for a single-line slot, while the six dedicated renderers deliberately show MULTIPLE stacked footer lines -- forcing them through the single-winner design would be a real architecture change and a real feature reduction (fewer facts shown), not a small fix. Worth a real design discussion before touching it, not a quiet fix bundled into this pass.
- **Hockey has a dedicated DETAIL renderer but still no dedicated MAIN renderer** -- `SPORT_RENDERERS` has no `"hockey"` key, so hockey's live/main ticker view still falls through to the generic fallback while its detail view (reached by pressing select) is fully custom. Real, but the practical impact is narrow (the main ticker row already shows real data via the generic renderer; only the promoted-screen treatment is missing) and building a from-scratch main renderer is real, if modest, new scope -- flagged rather than rushed into this review pass.

**Weather icons, skin scope, player-badge scope, shot-court scope, racing's fallback safety, and the four consistent hero-geometry conventions were all checked and confirmed genuinely correct** -- no changes needed, not a lack of looking.

**Verified**: `render_audit.py`/`fold_audit.py` both clean throughout. A direct synthetic render confirmed odds ("LAL -3.5") now genuinely appears on basketball's dedicated detail view where it was previously silently absent. The hockey rink re-rendered post-fix shows clearly legible period/clock text against the darkened ice. Real service restart, confirmed healthy (`err: None`) after every change.

## Real bug: a NOAA test tsunami warning displayed as a genuine Extreme alert; weather gets a condition icon (2026-08-11)

**The bug, confirmed against real live data, not hypothetical.** The
owner reported real confusion seeing a tsunami warning for a Myrtle
Beach-area device. The actual live payload (`GET /api/weather/current`)
showed the real cause: NWS's National Tsunami Warning Center periodically
issues a genuine, scheduled **"TEST TSUNAMI WARNING"** covering
essentially the entire US coastline at once (which is why its area list
legitimately includes Horry County alongside dozens of other coastal
zones from Texas to Maine) -- `weather._fetch_alerts()` had no check
anywhere for whether NWS flagged a product as a test, so it rendered
with the same `severity: "Extreme"` weight as a real warning, with
nothing on screen indicating it was a drill. This could have driven the
global severe-weather takeover too, since that reads from the same
alerts list.

**Fixed with two independent, redundant checks** (a false NEGATIVE here
-- silently dropping a REAL tsunami warning -- would be far worse than a
false positive, so this deliberately doesn't rely on one signal alone):
1. NWS's own real CAP `status` field (Actual/Exercise/System/Test/Draft,
   part of the Common Alerting Protocol NWS alerts implement) -- skip
   anything present and not `"Actual"`.
2. A literal `"TEST "` prefix on the real event or headline text
   (confirmed present on the exact product that exposed this bug) --
   catches a real product that for any reason didn't carry a trustworthy
   `status` field.

Both checks live in `weather._fetch_alerts()`, upstream of every
consumer (`WeatherEngine`'s own alert view, the storm radar mini-scope,
and `arcade_server._severe_alert_frame()`'s global takeover) -- fixing
it once at the I/O boundary fixed all three for free, same "fold once at
the boundary" discipline this project already applies to text folding.

**Weather condition icon, real owner ask ("fix weather to be more
visually pleasing").** The main conditions view was almost entirely
text -- no real broadcast weather display is missing a sun/cloud/rain
icon, and this one was. New `weather_icon_for(text)` maps NWS's own real
condition text (case-insensitive substring match: THUNDER/STORM,
SNOW/SLEET/ICE, RAIN/SHOWER/DRIZZLE, FOG/HAZE/MIST, CLOUD/OVERCAST,
CLEAR/SUNNY/FAIR, checked in that priority order so a real "THUNDER"
mention can't fall through to the generic rain icon) to one of six new
geometric icon functions (`draw_weather_icon_sun/cloud/rain/storm/
snow/fog`) -- same put_px-offset convention every sport icon in this
project already uses, no bitmap assets. Unmatched real text (NWS has
dozens of real phrasings) draws no icon at all rather than guessing one,
the same honest-degrade contract every other derived-from-text signal
here follows. Placed in the left margin beside the centered "88F" hero
temperature -- confirmed via real text-width math that a 2-3 digit
temperature at scale=2 leaves genuine room there without needing to move
anything else on an already-audited layout.

**Verified**: `weather._fetch_alerts()` re-run directly against the real
live zone query (SCC051/SCZ054) after the fix -- the test tsunami is
gone, only the real Heat Advisory remains. `render_audit.py`/
`fold_audit.py` both clean. Four synthetic icon renders (sun/cloud/rain/
thunderstorm) confirmed distinct and legible via PNG. Real service
restart, confirmed healthy, and confirmed LIVE against the actual
running service's own `/api/weather/current` response (not just the
offline test) that the test alert no longer appears.

## Real pitch count + batting average: the boxscore, not `situation` (2026-08-11)

**The bug, and it is a bad one: a feature was reported as "added" twice
and rendered literally nothing on every real game.** Pitch count, batter,
on-deck and pitcher were all added to `sports._situation()` across two
earlier passes this same day, each shipped with an honest-sounding "read
defensively, unverified against a live payload" caveat. The owner then
looked at the real panel and correctly asked where any of it was. Answer:
nowhere, on every single live game, because **every one of those key
paths was wrong**. The "honest degrade" contract (absent means absent)
turned a wrong guess into a silent no-op, which is exactly the failure
mode that made it look like nothing had been built. **A defensive read of
a key nobody has ever confirmed is not a feature; it is a comment.**

**What the real live payload actually contains, confirmed 2026-08-11
against a real in-progress MLB game (CLE @ DET, event 401816481) — do not
re-derive these from memory:**

- **There is NO game-total `pitchCount` on `situation`, at any nesting.**
  The only `pitchCount` anywhere in the summary endpoint is
  `plays[].pitchCount = {balls, strikes}` — the count WITHIN one at-bat,
  not pitches thrown. Both previously-guessed keys
  (`situation.pitcher.pitchCount`, a flat `situation.pitchCount`) are
  fiction.
- **There is NO `onDeck` field.** The literal string appears **zero**
  times in the entire real response. ESPN does not publish it here.
  Deriving one from bat order would be guessing — a pinch hitter or
  substitution breaks that arithmetic exactly when it matters — so
  on-deck is **deliberately not shown**, and that is a real finding, not
  a gap to quietly fill later.
- **There is NO `battingAverage` key** either (0 occurrences).
- **`situation.batter`/`situation.pitcher` nest the name one level deeper
  than the old code read it**: the real path is
  `situation.batter.athlete.shortName` ("M. CLARK"), not
  `situation.batter.shortName`. This alone made the names `None` on every
  real game.
- **The two endpoints disagree on this shape, genuinely**: the
  **scoreboard**'s `situation.pitcher` carries a full nested `athlete`
  object; the **summary**'s carries only `{"playerId": N}`. Both are real
  and both must be handled.

**Where the real numbers actually live: `boxscore.players[].statistics[]`,
which carries a `labels` list naming the columns and a parallel per-athlete
`stats` list.**

| block | real labels | what matters |
|---|---|---|
| batting | `H-AB, AB, R, H, RBI, HR, BB, K, #P, AVG, OBP, SLG` | **`AVG` is the real batting average**; `H-AB` is today's line |
| pitching | `IP, H, R, ER, BB, K, HR, PC-ST, ERA, PC` | **`PC` is the real PITCH COUNT**; `PC-ST` is thrown-strikes |

Real confirmed rows: `S. Kwan -> ['1-3','3','1','1','0','0','1','0','19',
'.266','.365','.329']`, `T. Bibee -> ['6.0','5','4','4','0','4','0',
'81-56','3.94','81']`.

`sports._boxscore_stats()` zips `labels` to `stats` **by position, never
by hardcoded index**, so a column ESPN adds or reorders cannot silently
shift every value one slot over and produce confidently wrong numbers.
Athletes are joined to the live `situation` by real athlete id
(`playerId` -> `boxscore` `athlete.id`).

`sports.fetch_baseball_matchup(league, event_id)` is the one entry point.
`SportsEngine._refresh_matchup()` calls it for **the one game currently
expanded**, only while genuinely live, throttled through the existing
`_detector_due()` gate at `WINPROB_REFRESH` (20s) — the same narrow
per-game scope and cadence every other summary fetch here already uses.
Closing the detail view stops the fetch. Results are keyed by event id
and pruned to that one game, so a stale game's pitcher can never leak
onto another game's card.

**Two more real bugs found and fixed in the same pass:**

1. **`_disambiguate_colors()` was never wired into the universal header
   path** — only into `_parse_event()` (per-league). But the universal
   path is what the ticker and *every expanded detail view* actually
   render from, so a real CLE @ DET game drew two solid team bars in
   visually identical navy, defeating the entire point of colour-coding.
   The measurement that justified that function (5 of 19 real games too
   close to tell apart) applied here just as much; it simply was not
   called. Now fixed — CLE navy vs DET orange, via ESPN's own real
   `alternateColor`, never an invented colour.
2. **`fit_text` chewed player names down to a bare initial** — "T. BIBEE"
   -> "T.", "Z. MCKINSTRY" -> "Z.". This is the *exact* bug
   `fit_person()` was written for in this same file ("T. POSTARNAKOVA"
   -> "T.", see its docstring) and it was reintroduced anyway. Names now
   use `fit_person()`, so a long name degrades to its **surname**, never
   to an initial.

**Layout**: the diamond moved right and shrank (r=5), the reclaimed left
column carries the real matchup (name on its own row so it stays legible,
numbers beneath), team bars went 9px -> 8px to buy the two rows this
needed, and ball/strike pips moved up onto the inning/outs strip. **The
diamond itself was stripped to a plain outlined shape with lit base
squares** — direct owner instruction ("JUST THE BASES AND LIT UP WHEN
SOMEONE IS ON IT, DONT MAKE GRASS AND STUFF"), removing round 2's grass
fill, dirt infield, basepath edge and mound dot. When the matchup fetch
hasn't landed yet (first moments after opening a game, or ESPN
unreachable), the diamond **recentres and grows to r=8** rather than
leaving a dead column — an honest "less data, same screen" degrade.

**Verified**: real stats confirmed rendering on the real live game
(96P, 3.93 ERA, .286 AVG, 1-3 today, full surname "MCGONIGLE"),
`render_audit.py sports` clean (0 failed, 133 frames), full sweep and
`fold_audit.py` clean, both the with-matchup and no-matchup layouts
rendered and visually reviewed, real service restarted and driven through
`sports` mode live (`err: None`, `loop_errors: 0`, 864 lit px), panel
restored to `ambient`.

**Standing lesson worth more than the feature**: this project's
"ship correct, flag honestly" precedent is sound, but it was being used
to ship *unconfirmed key names* as though flagging them made them
harmless. It doesn't — it makes a dead feature that looks alive in the
code and in this file. If the network is reachable, **confirm the key
against a real payload before writing the renderer**; if it isn't, say
the feature is blocked, not built.

## MMA gets an octagon hero + detail renderer; hero-treatment sweep judged complete (2026-08-11)

Direct follow-up on "keep innovating": MMA had a real MAIN renderer
(`_render_mma()`) but, like hockey before this pass, no dedicated
DETAIL renderer at all -- fell through to the generic fallback. New
`draw_octagon_hero()` (eight points on a circle, cage-red outline, dark
mat fill -- geometric only, no real promotion's cage branding) backs a
new `_render_mma_detail()`: full (not abbreviated) fighter names,
records, weight class, card position, and venue/broadcast, same honest
set-dressing contract every other hero this session established.

**Deliberately stopped here, not a partial job**: golf and tennis were
considered and left alone on purpose -- both are inherently RANKED-LIST
sports (a leaderboard, a bracket position), not a single-live-state
matchup the way baseball/football/basketball/hockey/soccer/MMA all are.
A hero band would eat real leaderboard rows for a shape that doesn't fit
the "one backdrop behind one live fact" pattern the other six use.
Racing's own detail view is the same generic leaderboard path golf
already uses, for the same real reason (a race is fundamentally a
ranked list of drivers, not a two-side matchup) -- also left alone.
That's six of nine real per-sport detail renderers now hero-backed
(baseball/football/basketball/hockey/soccer/MMA); the remaining three
(golf/tennis/racing) keep their existing, already-real leaderboard
treatment because that's the honest right shape for what they actually
are, not an oversight.

**Verified**: `render_audit.py sports` clean (0 failed, same pre-existing
tennis-name truncation warnings). Synthetic octagon render (Jon Jones vs
Stipe Miocic, real UFC-shaped data) confirmed legible via PNG -- weight
class, card position, both names/records, VS, and venue/broadcast all
present with no text collision. Full project sweep and `fold_audit.py`
both clean. Real service restart, confirmed healthy (`err: None`).

## Full logic audit + real bug fixes + hero treatment for every sport (2026-08-11, owner away)

Direct owner ask: "look at logic of everything. do a full audit and fix things... if you think of anything or anything seems like a no brainer, make sure you create and implement." Four parallel read-only audit passes (flights/PlaneWatch, sports/SportsEngine, weather/satellite/skypass, arcade_server/market/shared-infra), each instructed to find REAL bugs only, not style nits, and to say so honestly if nothing was found. All four came back with concrete, real findings -- none manufactured.

**Real bugs found and fixed:**

1. **Flights: two big-moment detectors silently excluded aircraft with no broadcast callsign.** `_detect_emergency_squawk()`/`_detect_airship()` keyed their one-shot seen-sets on bare `ac["ident"]` and REQUIRED a truthy ident just to be included in the set at all -- a real aircraft broadcasting hex but no callsign (the exact case `_sel_key()`'s hex-preferred fallback exists for) would never be checked for MAYDAY/airship status, not just risk a rare callsign-collision misfire. Fixed: both now key on `self._sel_key(ac)` (hex preferred), matching every other identity-tracking spot in this project; display text degrades to hex when ident is genuinely absent instead of a raw `ac["ident"]` KeyError.

2. **Weather: feels-like tag silently suppressed at exactly 0F.** `WeatherEngine._frame_hourly()`'s comparison used `temp or feels` instead of `temp is not None` -- a real 0F temp is falsy in Python, so `0 or feels` evaluated to `feels`, making the comparison always `feels != feels` (False), hiding a genuinely different real wind-chill number. Fixed to the `is not None` idiom every other "is this a real different fact" check in this file already uses.

3. **Sports: two real scoring plays landing in the same ~20s poll window could permanently lose one of them.** All four id-keyed big-moment detectors (`mlb_hr`/`nfl_touchdown`/`nhl_goal`/`basketball_clutch`) picked "newest" by raw STRING comparison of ESPN's play id (wrong the moment two ids differ in digit width, e.g. "9" sorting after "10"), AND unconditionally marked every currently-known id as seen the same tick a new one was found -- so a second real scoring play in the same throttle window was fired for never, and marked seen anyway, silently gone forever. New shared `_advance_seen_plays()`/`_play_sort_key()` fixes both: numeric-aware chronological sort, and only the play actually reported as newest gets added to the seen set -- any other genuinely-new-but-unfired play stays eligible and fires on the very next poll instead of being lost. Verified against the exact reproduction scenario by hand (see the direct interpreter session in this session's own history): the previously-lost play now correctly fires one throttle interval later.

4. **Ticker: the "0.35 brightness floor" on the price bar was dead code for real near-zero moves.** `int(sqrt(frac) * half)` rounds straight to 0 for any move under ~0.0153%, so the bar drew zero pixels before the color floor ever had anything to apply to -- genuinely reachable by this session's own stablecoin additions (USDT/USDC/DAI routinely sit at 0.00-0.01% 24h change). Fixed with `max(1, n)` whenever `frac > 0`.

**No-brainers found and implemented:**
- `market.py`'s `list_known_crypto()` rendered ZRX's real CoinGecko id "0x" as "0X" via blind `.title()`-casing -- added a one-entry display-name override.
- CVX/DASH real symbol collisions between the crypto and stock tables (Convex Finance vs. Chevron, Dash vs. DoorDash) were confirmed as a real but narrow edge case (only triggers if an owner deliberately adds the same symbol to both watchlists) -- documented, not fixed structurally; a visual crypto/stock marker would need threading a new field through the whole feed/render pipeline for a two-symbol collision, judged not worth the risk this pass.

**Everything else the four audits checked came back clean** -- dead-reckoning/trail math, selection-identity handling, the ISS comet trail's fill/clear logic, unit conversions, the Rothfusz heat-index/wind-chill formulas, cache-on-mtime races in every small config module, the `/api/notify` HMAC auth, `layout.py`/`skins.py`'s validation, and the render loop's panel-lockup-sensitive path were all hand-traced and found correct. Full findings from all four audits are in this session's own record; only the confirmed real bugs are summarized here.

## Real-time follow-up fixes, same session: menu axis bug + baseball diamond legibility + hero treatment for every team sport

Three more direct owner reports/asks, addressed in the same pass:

**Flight selection menu's input axis was backwards.** Real, confirmed bug: `_frame_menu()` renders as a VERTICAL stacked list, but LEFT/RIGHT (a horizontal gesture) moved the cursor, while UP/DOWN did something else entirely (jumped straight to THE HANGAR, abandoning the open menu). Fixed: left/right now no-ops while the menu is open; up/down moves the cursor. Verified directly against a real engine instance -- `input('left')` no longer touches `menu_idx`, `input('down')`/`input('up')` do.

**Baseball diamond, round 2 -- reverted toward cartoon, not realism.** Round 1 (earlier this session) added a mown-stripe grass texture and a 5-point home-plate pentagon for visual depth; real owner feedback: "too realistic when cartoon version is better... supposed to see who is on base" at a glance. Fixed: flat solid grass (texture was fighting legibility, not adding it), a plain bright home-plate square, and -- the real fix -- occupied bases now draw BIGGER (a filled 3x3 block) not just brighter than empty ones, since size is what actually reads instantly across a room, matching the same "important = bigger AND brighter" hierarchy language the flight radar scope already uses.

**Every remaining team sport got the same hero treatment baseball/football already had**, direct owner ask ("every sport needs the attention MLB has... when you hit enter it's supposed to be a visually jaw-dropping experience on each sport"): `draw_basketball_court_hero()` (hardwood + center circle) wired into `_render_basketball_detail()`; `draw_soccer_pitch_hero()` (pitch + halfway line/center circle) wired into `_render_soccer_detail()`; `draw_hockey_rink_hero()` (ice + real blue lines/red line) backs a BRAND NEW `_render_hockey_detail()` -- hockey previously had NO dedicated renderer at all, main or detail, the exact gap this whole per-sport table exists to close. All three follow the same honest-set-dressing contract football's turf established: real backdrop shape, real period/quarter/clock text on top, zero invented possession/bonus/power-play markers (no confirmed ESPN field for any of those, stated in each function's own docstring).

**Real MLB pitch count added**, direct owner ask ("score bugs even feature that"): `sports._situation()` now reads `situation.pitcher.pitchCount` (falling back to a flatter `situation.pitchCount`) defensively, shown as "P47" sharing the same row as the existing ball/strike count pips. Honestly flagged, same as every other field added without live verification this session: this sandbox cannot reach ESPN to confirm either key name is real for this specific endpoint -- ships correct-if-present, silently absent otherwise, never a guessed number.

**F1/NASCAR confirmed structurally correct, not newly built**: re-checked `has_content()`/`_build_panels()`/the universal ticker dispatch for any sport-specific filtering that could exclude racing -- found none. Racing already flows through the exact same generic leaderboard path every individual-competitor sport uses; the only reason it might not appear live is ESPN's real universal feed not tagging an event `sport: "racing"` this session (unverifiable, network-blocked sandbox), not a gap in this project's own code.

**Wider GitHub research pass** (explicit ask: "look at tons of them") surfaced two new real, verified repos beyond the prior two research passes -- `feram18/f1-led-leaderboard` (dedicated per-data-type screens: standings/qualifying/podium/track-map, a real precedent for a future F1 hero screen) and `bcariddi/prem-led-scoreboard` (a folder-keyed random-gif celebration pool). The gif-pool idea was explicitly considered and rejected: it depends on shipping external image assets, which conflicts with this project's own standing "no shipped image/logo library, geometric icons only" rule (the same reasoning behind every sprite/icon in this codebase being hand-drawn primitives, never an asset file).

**Verified**: `render_audit.py`/`fold_audit.py` both clean throughout every fix (0 modes failed, 0 feeds not folding). The two logic-bug fixes (sports id-collision, weather falsy-zero) were verified with direct interpreter reproductions of the exact failure scenario, not just re-running the audit tools. All three new hero backdrops (basketball/hockey/soccer) rendered to PNG and visually confirmed distinct and legible. Real service restart + health check after every change (`err: None`, `loop_errors: 0` throughout).

## Big autonomous build pass: ticker expansion, dynamic visuals everywhere, Tier 1-3 scorebug ideas (2026-08-10, owner away)

A large, explicitly front-loaded-with-questions session -- the owner
asked for tier 1/2/3 scorebug ideas, "more dynamic visuals... a no-
brainer... very pleasant", and a much bigger ticker, then stepped away
and asked everything be built unsupervised. Every open decision was
resolved via AskUserQuestion BEFORE this pass started (ticker scope,
which Tier 2/3 items, which modes get dynamic-visual depth, autonomy
ground rules) so nothing below required a judgment call that hadn't
already been made explicit.

**Ticker expansion** (`market.py`). `SYMBOL_TO_COINGECKO_ID` grew 24 ->
120 real coins -- confirmed by reading `_fetch_crypto()` FIRST that
CoinGecko's `/simple/price` batches every id into ONE request regardless
of list size, so this adds zero new requests/minute, not a scaling
concern. New `KNOWN_STOCKS` (110 real tickers + company names) is a
DIFFERENT kind of table -- Yahoo's chart endpoint already accepted any
real symbol directly (no cap ever existed there), so this is purely a
reference list for the control panel's new quick-add dropdowns, not a
capability gate. `DEFAULT_CRYPTO`/`DEFAULT_STOCKS` both grew from 3 to 8
seeded symbols. **Honest gap**: this sandbox can't reach CoinGecko this
session, so the ~90 newly-added ids are from CoinGecko's well-established
public naming convention, not individually re-confirmed -- a wrong id
fails gracefully (that one coin just never populates, never a wrong
price), so this is a low-risk, not a fabricated-number, failure mode.
Control panel (`arcade.html`) gained quick-add `<select>` dropdowns for
both crypto and stocks, wired to the expanded `/api/ticker/symbols`
response (`known_crypto`/`known_stocks`, now `{sym, name}` pairs).

**Ticker price-strength visuals** (`engines.py`, `TickerEngine`). The
existing sqrt-scaled magnitude bar now ALSO scales brightness with the
same real `pct` magnitude (floored at 0.35 so a near-zero move stays a
visible quiet bar, not an invisible one) -- length said "how far",
brightness now also says "how much". The bottom scrolling tape, previously
flat neutral gray regardless of content, now tints toward UP/DOWN based
on the real aggregate mood of the currently-loaded rows (more real
gainers than losers, or vice versa) -- a real derived signal, not an
invented per-row color (draw_marquee only takes one color for the whole
string, so per-symbol tinting would need a real per-glyph marquee
rewrite, out of scope for a visual-polish pass).

**Satellite dome comet trail** (`SatelliteEngine`). ISS-only, deliberately:
this project already measured whole-catalogue apparent motion at
~0.13px/sec (dominated by slow high-altitude satellites), so a trail on
every object would be motionless noise. The ISS's real ~7.66km/s orbital
velocity makes it the one object that visibly crosses the dome over a
few real seconds during a close pass. `self._iss_trail` (deque, maxlen
12) samples the real dome position every tick the ISS is above the
horizon, cleared the instant it drops out of `sky_now` entirely so a
trail never survives to imply an ended pass.

**News freshness bar** (`NewsEngine` only, deliberately NOT `BlogEngine`
-- that engine's own docstring explicitly says it's "the quietest mode
in the project... should not earn attention", and a glow bar would
contradict that stated design intent, so it was skipped there on
inspection, not forgotten). New module-level `draw_freshness_bar()`
complements (doesn't replace) the existing binary stale-dot: real
`age`-scaled length AND brightness, floored so it never fully vanishes
(empty would misread as "no data" instead of "old data").

**Football hero turf** (`_render_football_detail`). Same "give the hero
visual real depth" treatment baseball's diamond got, with an honest
constraint stated up front: ESPN's down/distance NUMERIC subfields
(yard line, down, distance) were never confirmed live by this project --
only the pre-formatted `downDistanceText` string has been verified --
so `draw_football_field_hero()` deliberately does NOT plot a ball
position at a specific yard line (that would be inventing precision this
project doesn't have). It's honest set-dressing: a real mown-turf stripe
backdrop with the REAL down/distance text on top, nothing invented. A
real vertical-budget bug (the exact same class baseball's diamond hit)
was caught before shipping: the first draft's live-game team-block
spacing never left the turf enough room to draw at all; fixed by giving
football's live path the same compact/full-layout split baseball's
detail view already established.

**Player-highlight-after-key-play** (`sports.py` + `draw_celebration()`).
New `sports._play_scorer(p)` extracts real jersey/position/name from a
scoring play's `participants` field -- reused, not a new endpoint, on
the SAME summary payload the home-run/touchdown/goal/clutch detectors
already fetch. Wired into all five detectors (`mlb_hr`, `nfl_touchdown`,
`nhl_goal`, `basketball_clutch`, `soccer_goal`) via a new `scorer` param
on `_set_big_moment()`. `draw_celebration()` draws it as a small
`#JERSEY POSITION` badge at the very bottom of the screen, clear of the
centered text plate -- deliberately NEVER a photo (unrecognizable at
64x64), matching the scope-narrowing the owner explicitly agreed to.
**Honest gap**: `participants[].athlete.jersey/position` shape is from
common public ESPN-API documentation, not confirmed against this
project's own live payload (network-blocked sandbox) -- verify the first
session a real scoring play fires with this code live.

**F1 + NASCAR racing support** (`engines.py`). A real scope-reduction
discovery: `_header_event()`'s existing `leaderboard = len(athletes) > 2`
path (built for golf) is ALREADY sport-agnostic -- any individual-
competitor sport with 3+ athletes renders through the same generic
leaderboard code with zero new renderer needed. `SPORT_ACCENT["racing"]`
already existed in the codebase (unused until now); added
`draw_icon_racing()` (a generic checkered-flag glyph, geometric-only, no
real series marks) and registered `SPORT_ICONS["racing"]`. No new
per-league polling, no pinned-driver mechanism (deliberately out of
scope this pass, unlike golf/tennis's dedicated pinned-player systems) --
this only makes whatever the universal feed already surfaces for racing
render with a proper icon/color instead of falling through unstyled.
**Honest gap**: never confirmed live that ESPN's universal header
actually tags F1/NASCAR events with `sport: "racing"` (network-blocked
sandbox) -- verify the next session real racing events are checked.

**NBA mini shot-location court** (`sports.py` + `draw_celebration()`).
Deliberately reuses `_fetch_clutch_plays()`'s own already-fetched summary
payload (`play.coordinate.x/y`, a real field documented across multiple
independent public ESPN-API tools) rather than adding stats.nba.com as a
brand-new external dependency with its own header/rate-limit quirks this
project has no existing relationship with -- keeps this consistent with
the project's "own I/O module per feed" architecture instead of
introducing a second sports-data source. New `draw_mini_court()` (a
plain rectangle + hoop tick + one dot, geometric-only) draws in the
celebration's top-right corner, clear of the text plate and the player
badge. **Honest gap, most significant one this pass**: the real
coordinate SYSTEM (origin corner, full vs half-court feet) is assumed
from common public convention, not confirmed -- the dot is clamped into
range rather than trusted raw, but its exact on-court accuracy is
unverified until a real clutch shot fires with this code live.

**Tier 3: config-driven layout-priority pilot** (`layout.py`, new).
Scoped down explicitly from the research reference
(jackbmccarthy/OpenScoreboard's GrapesJS drag-and-drop web-canvas editor)
after confirming with the owner that a general drag-and-drop position
editor doesn't fit this project's fixed, hand-audited 64x64 renderers
(would reopen every collision/overflow bug `render_audit.py` already
found and fixed on these exact screens). What ships instead, matching
the real spirit of the ask: `SportsEngine`'s one hardcoded footer-field
priority chain (odds > class_label > series > venue, in
`_frame_universal_generic()`/`_frame_event_detail_generic()`) is now a
real config-driven ORDER (`layout_config.json`, same load/save/cache
shape as every other tiny config module here), validated so a saved
list must contain exactly the same 4 real fields. New shared
`SportsEngine._footer_pick()` replaced two separate hardcoded chains
with one, so both views can never disagree about priority. New
`GET`/`POST /api/layout` + a comma-separated text field in the control
panel (matching the ticker card's own existing UX convention).

**Verified, all nine pieces**: `render_audit.py`/`fold_audit.py` both
clean throughout (0 modes failed, 0 feeds not folding, only pre-existing
unrelated truncation warnings). Every synthetic-render claim above was
actually rendered to a PNG and visually confirmed, not just described --
ticker bar/tape, football turf, celebration player badge, NBA mini
court, racing leaderboard. The layout-priority reorder was confirmed to
actually change which field renders (`odds`-first vs `class_label`-first
produced different real output from the same event dict). Real service
restart + health check after every single change (owner explicitly
approved "restart repeatedly while I'm away"), all clean (`err: None`,
`loop_errors: 0`). All five HONEST GAPS above (crypto ids, player
jersey/position shape, F1/NASCAR sport tagging, football numeric yard
fields, NBA shot coordinate system) are real, stated inline in the code
they affect, not just here -- this sandbox's network to CoinGecko/ESPN
was unreachable the entire session, so anything requiring a live payload
this project hasn't independently confirmed before is flagged rather
than assumed correct. Next session: re-check all five against real live
data.

## Skins expanded, baseball diamond redesigned, real ambient reuse (2026-08-10, follow-up)

Direct owner follow-up on the Tier 3 work above: "make the skins better",
"more dynamic visual modes", "fix the baseball diamond into something
more visually pleasing", "there should be pitch count or something".

**Pitch count -- checked for real, not built.** No confirmed field for a
game-total pitch count exists anywhere in the ESPN payloads this project
actually parses (`_situation()`'s own docstring already states the full
verified MLB set: balls/strikes/outs/onFirst/onSecond/onThird, nothing
more). Deriving one from balls+strikes would be quietly WRONG -- foul
balls past two strikes add real pitches without changing the strike
count, so that arithmetic undercounts every at-bat with a foul-off.
Flagged honestly instead of shipped as a plausible-looking wrong number.
**What WAS real and available: `baseRunnersText`** (ESPN's own
human-readable runner description, e.g. "RUNNER ON 2ND") --
`sports._header_event()` had already been parsing this into
`runners_text` since the odds work above, but nothing ever displayed
it. Now shown on the baseball detail view, preferred over the project's
own synthesized "RISP" tag (RISP is now only the fallback when ESPN's
own text is absent) -- real ESPN prose beats a derived abbreviation
when both are available.

**Baseball diamond redesign** (`draw_baseball_diamond_hero()`), on
direct visual feedback against the first version: added a mown-stripe
grass field behind the dirt diamond (alternating light/dark bands, the
real broadcast-field detail every televised infield has -- the diamond
previously floated on flat black with nothing establishing it as a
field), a brighter basepath EDGE outline around the dirt fill (was a
flat blob), a real 5-point pentagon home plate (was a 4-pixel bar --
the one base with a genuinely different real shape, now drawn like
one), and a distinct plus/diamond shape for the three occupied-base
markers (was a plain square, now visually distinct from both the
plate's pentagon and the mound's disc). Same real geometry (home
bottom/1st right/2nd top/3rd left) and same pulse-on-occupied
technique, just genuinely more visual depth per the owner's own "more
visually pleasing" ask.

**Skins expanded, both wider AND deeper**, per an explicit two-part
answer ("extend skins to more modes" + "more ambient effects like
weather's rain/stars" -- both):
- Two new skins (`forest`, `neon`), and every existing skin gained a
  real `accent` color (previously only `ink`/`ink_dim`/`win`/`lose`/
  `live`/`stale`/`hero_ink`) -- needed because WeatherEngine and
  ClockEngine both lead with one dominant ACCENT color as their primary
  chrome, unlike SportsEngine's INK/INK_DIM pairing. Every skin was
  re-picked for real HUE separation from its neighbors (not a
  brightness/saturation nudge on the same hue), so switching skins
  reads as a genuinely different mode.
- `_apply_skin()` (the exact instance-attribute-shadowing idiom
  SportsEngine established) is now ALSO wired into `WeatherEngine`
  (overrides `ACCENT`/`INK`/`INK_DIM`/`STALE`) and `ClockEngine`
  (overrides `ACCENT`/`DIM` only -- `TIME`/`DATE`/`TEMP`/`ISS`/`SUN` stay
  fixed, since those are the real hero readouts, not decorative chrome,
  same "never recolor the real data" line every skin application in
  this project draws). Still NOT blanket-applied to flights/satellite/
  news/blog this pass -- stated explicitly as remaining scope, not a
  silent omission.
- **Ambient reactive touches, reused rather than duplicated a third
  time.** `ClockEngine._draw_ambient()` (new) is the exact same
  real-data-gated rain-streaks/night-stars logic
  `WeatherEngine._draw_weather_ambient()` already established, reading
  the identical fields off `self.wx` (weather.FEED.get(), already
  polled here every tick to keep the feed warm) instead of
  WeatherEngine's own `self.data`. The clock is the panel's RESTING
  state and lit far more of the time than weather mode itself, so this
  is the one place this touch reaches the most real screen-time.
  `FlightEngine._frame_idle()` (the "CLEAR SKIES" screen) gained the
  same real night-stars/rain gate, reading a newly cached
  `self._sky_wx = weather.FEED.get()` (zero new I/O -- weather.FEED is
  already running for any other mode reading it).

**Explicitly NOT built: a decorative "plane flying by" animation on the
idle screen**, despite that being the owner's own suggested image for
"dynamic and intuitive." Reasoned through directly: the REAL version of
"a plane goes by, the panel reacts" already exists and already ships --
`PlaneWatchEngine`, which only ever fires for a genuinely tracked
aircraft crossing the configured window. Drawing a fake silhouette on
the ONE screen that exists specifically to say "no aircraft nearby"
would be exactly the invented-visual this project's own repeated
"never invent" rule forbids, on the one screen where honesty about
absence is the entire point. The real-signal-gated night stars are the
honest version of the same instinct -- reacting to something true (it's
nighttime) instead of something false (a plane that isn't there).

**Verified**: `render_audit.py`/`fold_audit.py` both clean (0 modes
failed, 0 feeds not folding) after every piece. Direct engine checks:
all 6 skins construct cleanly across `SportsEngine`/`WeatherEngine`/
`ClockEngine` with no exceptions. A synthetic `neon`-skinned clock frame
confirmed the header/accent actually recolored while `TIME`/`DATE`
stayed fixed. Two synthetic baseball-detail renders (RISP fallback and
real `runners_text`) both confirmed clean, no overflow/collision, and
visually reviewed via PNG before shipping (the redesigned diamond
specifically -- grass stripes, basepath edge, pentagon plate, plus-
shaped bases -- all present and legible at 8x zoom). Real service
restart, confirmed healthy (`err: None`) after each of the three
follow-up changes. Test-only `skins_config.json` cleaned up before each
real restart.

## Tier 3: real ESPN odds + a local skin system (2026-08-10)

Two competitive-research items the owner explicitly asked to build,
picked up in the same session as the baseball diamond redesign.

**Odds -- a real project policy REVERSED, on explicit owner
instruction.** `sports.py` already had a standing "betting stays off by
default" decision baked in (a comment predating this session: "Fields
we already pay for and were dropping. Deliberately NOT including
`odds`"), dropping ESPN's real `odds` field on the way in. Flagged to
the owner as a real conflict before touching anything -- confirmed:
reverse it. `sports._parse_odds()` (new) extracts ESPN's own pre-
formatted `details` string ("NYM -1.5") and `overUnder` float from the
FIRST provider entry on the same header payload every other field here
already comes from -- zero new I/O, zero new (and zero PAID) data
source, unlike the paid odds-API options the competitive-research pass
separately flagged and rejected. Folded through `paneltext.panel_text()`
at the same boundary every other externally-sourced string in this file
already uses.

**Displayed as ONE line, spread OR over/under, never both concatenated**
-- an early draft combined them and the panel's own width budget
silently dropped the over/under NUMBER via `fit_text()`'s whole-word
truncation, leaving a meaningless bare "O/U" on screen (caught on a
synthetic render before shipping, not by inspection). Spread wins when
both are real. Wired into `_frame_universal_generic()` (basketball's
current fallback, and any future sport without a dedicated renderer)
and `_frame_event_detail_generic()` (MMA's current fallback), pre-game
only in both -- once live, the game's own score/clock is the more
useful fact for that one available footer row.

**Honest gap, stated per this project's own "ship correct, flag
honestly" precedent**: this sandbox cannot reach ESPN's odds-bearing
endpoint this session (network-blocked, same `site.api.espn.com` 403
already documented elsewhere in `sports.py`), so `_parse_odds()`'s real
payload SHAPE is built from ESPN's own publicly documented format, not
confirmed against a live response the way most fields in this file are.
No `fold_audit.py` case was added yet either, for the same reason (no
live payload to replay canaries through). Verify both the shape and add
fold coverage the first session this device can reach ESPN's odds field
live.

**A safe, local skin system -- explicitly NOT the remote-plugin-store
the research repo (ChuckBuilds/LEDMatrix) actually ships.** That real
repo lets a user install plugins fetched from arbitrary GitHub repos --
downloading and executing remote code on this device is out of scope
regardless of what prompted the research, no exception. What that repo
actually splits into two concepts -- plugins own data/scheduling,
skins restyle the screens -- maps cleanly onto this project's own
existing feed/engine split (feed modules already own all data, engine
classes already own all rendering), so the only genuinely new, SAFE
piece is the skin half: a named set of local, config-driven CHROME
color overrides.

`skins.py` (new, same shape as `dnd.py`: one JSON config, safe
defaults, read-modify-write, cached on file mtime) defines four named
skins (classic/midnight/sunset/mono), each overriding ONLY
ink/ink_dim/win/lose/live/stale/hero_ink -- never a real, meaningful,
data-driven color (a team's own real ESPN color, an altitude band, an
alert severity color, `BASES_LOADED_GLOW`). "Never invent a color, only
recolor the frame around it" -- the same discipline `brightness.py`
already follows for brightness and every unit-conversion helper already
follows for units, just extended to a third axis (theme).

**PILOT SCOPE, stated explicitly, not silently limited**: wired into
`SportsEngine` only this session (`_apply_skin()`, called from both
`reset()` and `tick()` -- the tick() call means a skin change from the
control panel applies live, without leaving and re-entering sports
mode), not blanket-applied across every mode. `_apply_skin()` shadows
the engine's own class-level chrome-color attributes with instance
attributes, so every existing `self.INK`/`self.LIVE`/etc. read
elsewhere in the class picks up the active skin with ZERO call-site
changes anywhere else in `SportsEngine`.

New `GET`/`POST /api/skins` (same shape as `/api/dnd`), a control-panel
dropdown in the existing status card, and confirmed live end-to-end
against the real running service: set `midnight` via a real POST,
switched to `sports` mode, pulled a real non-black `/api/frame` (889 lit
px, `err: None`), reverted to `classic`, restored `ambient`.

**Verified**: `render_audit.py sports` and the full project sweep both
clean (0 failed, only pre-existing unrelated truncation warnings).
`fold_audit.py` clean (0 feeds not folding -- odds text folding
confirmed by code inspection of `_parse_odds()`'s own
`paneltext.panel_text()` call, not yet a dedicated replay case, per the
honest gap noted above). Direct code check confirmed `SportsEngine.INK`
actually changes per active skin (`sunset` -> `(255, 190, 140)`,
`classic` -> `(150, 160, 185)`, both matching `skins.SKINS` exactly).
Test-only `skins_config.json` cleaned up before the real service
restart.

## Baseball's expanded-detail view gets a real hero diamond (2026-08-10)

Direct owner ask, with a real screenshot of the plain pre-game version:
"the most visually pleasing baseball board on the market... genius
stuff... all live and update live." Scoped to the LIVE state only --
pre/post games (the screenshot's own case) keep the original record/
series/venue/broadcast layout unchanged, since there's no live diamond
state to show yet.

**`draw_baseball_diamond_hero()`** (new, module level, next to the
existing tiny `draw_diamond()` 4-dot glyph) -- a real filled infield
shape (`_fill_poly()`, reused from the flight hero-silhouette work, not
reinvented), home plate at bottom/1st right/2nd top/3rd left (same real
orientation the small glyph already established), a pitcher's mound dot
at the true centroid, and base markers that brighten (not enlarge --
an early draft made lit bases bigger, which visually fused with the
mound into a "bowtie" rather than reading as distinct corners, caught
on a synthetic PNG review before shipping) when occupied, with the same
breathing-glow pulse technique already established for the flight hero
silhouettes and the bases-loaded sustained glow (same session, above).

**`draw_count_pips()`** (new) -- balls/strikes as two short pip rows
(3 ball pips, a 4th being a walk; 2 strike pips, a 3rd being a
strikeout), the real broadcast-graphic convention, replacing the bare
"3-2" digit pair.

**Real niche stats, nothing invented**: RISP (runner in scoring
position -- a real, standard broadcast term, derived from the same real
`onSecond`/`onThird` booleans already on `bases`, not a new field) draws
when there's room. Win probability draws ONLY when the expanded game IS
the pinned favorite's own live game -- `sports.py`'s `_fetch_win_prob()`
is scoped to that one game only (confirmed by reading its own docstring
again before reusing it here), so showing a win% number on any other
game's detail card would be presenting a figure that was never actually
computed for it.

**A real vertical-budget bug, caught by `render_audit.py` on the first
run, not by eye**: the first draft used `+9` spacing between the two
scale=2 team rows -- a scale=2 glyph is a real 10px tall, so `+9`
overlapped them (`COLLISION event 1 detail: 'BAL' overlaps 'MIN'`).
Fixed to `+11`, the same spacing the untouched pre/post layout right
above it already uses for the identical row shape -- reused, not
re-guessed a second time.

**Verified**: `render_audit.py sports` clean (0 failed, same pre-existing
unrelated tennis-name truncation warnings). Two synthetic renders sent
for direct visual review: a live bases-loaded/full-count case (bases
[1,0,1], 3-2 count, 2 outs, RISP showing, win% showing for a matched
favorite event_id) and the unchanged pre-game case matching the owner's
own screenshot pixel-for-pixel in layout. Full project sweep and
`fold_audit.py` both clean. Real service restart, confirmed healthy
(`err: null`).

## Competitive-research pass: sticky recall, sustained glow, radar forward-heading tick (2026-08-10)

Three build-now/design-discussion items picked up from a competitive
research audit of real open-source LED-matrix projects (flight trackers,
sports scoreboards). Each cites its real source repo; none of the ideas
were built without independently verifying the source repo actually
exists and actually does what's claimed.

**1. Sticky recall for a live favorite sports game**
(`robbydyer/sports`'s own `stickyDelay` precedent). Distinct from
`SportsEngine.ambient_weight()`'s existing `FAVORITE_AMBIENT_BOOST`
(2026-08-10, earlier this session) -- that only lengthens dwell when
sports' turn naturally comes up in the rotation; this periodically pulls
sports back to the FRONT of the rotation while a favorite is genuinely
live, even mid-turn on something else. `AmbientEngine.STICKY_RECALL_TICKS`
(3600 ticks, ~3min) -- long enough that the rest of the rotation still
gets real airtime between recalls, short enough you're never long
without a check-in. `SportsEngine.favorite_live()` (new, extracted
verbatim from `ambient_weight()`'s own existing favorite-detection logic,
not re-derived) is the one real signal both now share.
`AmbientEngine._jump_to()` (new) forces the rotation onto a named
sub-mode NOW, bypassing `_advance()`'s normal wrap-order stepping, while
still capturing the outgoing frame for a normal slide transition -- not
a hard cut. The recall counter resets whenever sports IS already showing
(nothing to recall) or the favorite stops being live (nothing eligible),
and never fires during a celebration, DND-suppressed state, or manual
browsing (`self.cycling`/`browse.auto_ok` gated, same guards every other
auto-advance path here already respects).

**2. Sustained-state glow, baseball bases-loaded only**
(`sflems/cfl-led-scoreboard`'s own redzone-glow precedent -- "lit for
the duration of a real state, not a one-shot flash"). A second,
complementary grammar to the existing one-shot `Pulse` scoring flash:
`Pulse` still means "something just happened"; this means "a
high-leverage state IS true right now". **Deliberately NOT extended to
football redzone or hockey power-play** -- both were checked against
this project's own real, already-inspected ESPN payloads (`_situation()`
in `sports.py`) and neither field has EVER been confirmed present on a
real payload this project has actually pulled; CLAUDE.md's own FOOTBALL/
BASKETBALL section already states this honestly ("no possession
indicator was built... guessing one would be inventing the feature").
Bases-loaded (`onFirst`/`onSecond`/`onThird` all truthy) is the one real,
always-derivable "tense right now" fact available without a new field or
a guess, so it's the only sustained-glow state shipped this pass.
`SportsEngine.BASES_LOADED_GLOW` (urgent red, distinct from `BASE_ON`'s
gold so the sustained state reads as tension, not just "runners on"),
drawn as a soft pulsing wash behind the diamond+outs area in
`_render_baseball()` -- same breathing-glow technique
(`rim()` + a slow sine) already established for the flight hero
silhouettes, reused rather than reinvented, phased off `self.scroll`
(this engine's own existing monotonic per-tick counter) rather than
adding a second counter.

**3. Flight radar forward-heading tick** (owner ask: "revisit the base
scope, make it better than it is now"; FlightScnr's sweep-radar redesign
was the tier-2 research item that prompted the discussion, though this
specific addition is original, not copied from that repo). The trail
(shipped 2026-08-08) already answers "where has this aircraft been";
nothing on the scope answered "which way is it pointed right now" beyond
the icon's own rotation, which is subtle at 1:1 LED pixel scale. Added a
short (3nm, fixed-length -- a direction indicator, not a claimed
time-ahead prediction) dim line extending from the selected aircraft's
CURRENT dead-reckoned position along its real `track_deg`, using the
SAME `_ext_dist_nm`/`_ext_dir_deg` origin the live icon itself already
computes a few lines below (never a second, potentially-drifted position
computation). Same narrow scope discipline the trail already established
-- selected aircraft only, never all 8 (this scope has already had one
real lag complaint from over-drawing, and one real "too many lines"
complaint that led to the route-ray removal two sessions ago; this adds
exactly one more line, and only when something is actively selected).
Real `track_deg` only -- no track data means no tick drawn, never a
guessed heading.

**Also checked, found already correct, no code change**: (a) the idle
fallback chain (ColinWaddell/FlightTracker's own clock->weather->
satellite precedent) -- `AmbientEngine` already skips `has_content() ==
False` modes and falls back to `clock`, confirmed by reading the code,
not assumed; (b) non-ASCII airport/registration name folding
(squix78/esp8266-plane-spotter-color's own known-issue list) --
`origin_city`/`dest_city`/`airline`/registration all already fold through
`paneltext.panel_text()` at the `flights.py` I/O boundary, confirmed by
grep; (c) the `KeyError: 3` in `FlightEngine.input()`'s rotate-cycling
flagged as an open follow-up in the FAVORITES section above --
superseded by the flight-select-menu rewrite (2026-08-09, same day,
earlier), which replaced the dict-keyed view-cycling with an explicit
if/elif dispatch; confirmed no `self.view]`-style dict lookup remains
anywhere in the file.

**Deliberately not built**: a notable-registration watchlist
(WPTK/awesome-planespotting-list) -- that list is real celebrity/gov/
military tail numbers, and seeding it would mean asserting specific real
registrations belong to specific real people/agencies without a verified
source in hand. Fabricating plausible-looking ones would violate this
project's own "never invent" rule at the data layer, not just the render
layer. The mechanism already exists (`favorite_aircraft` accepts any
registration today); only the seed data is missing, and stays missing
until a real sourced list is provided.

**Verified**: `render_audit.py sports`/`render_audit.py flights` and the
full project sweep both clean (0 modes failed, only pre-existing
unrelated truncation warnings). A synthetic bases-loaded baseball frame
(751 lit px, no crash) and a synthetic selected-aircraft-with-track
scope frame (812 lit px, no crash) both rendered directly. Real service
restart, confirmed healthy (`err: null`, `loop_errors: 0`), switched
through `flights` mode and pulled a real non-black `/api/frame`, restored
to `ambient` afterward. Sticky recall and the bases-loaded glow are both
honestly unverified against a real live trigger this session (no live
favorite game and no real bases-loaded state occurred) -- the mechanism
was verified by direct code/logic inspection and the render-path smoke
tests above, matching this project's own "ship correct, flag honestly"
precedent for features built between live occurrences of their trigger
condition.

## Plane-in-window takeover -- the "London BA billboard" pass (2026-08-10)

Owner referenced the real 2019 British Airways London digital-billboard
campaign (a board that pointed a child's silhouette at a real overhead
BA flight, timed off live ADS-B, and named it) as the reference for what
this project's own plane-in-window takeover (`PlaneWatchEngine`) should
feel like emotionally, not technically -- four concrete asks, addressed
in order:

1. **"Feel like a discovery -- less competing decoration."** The
   backdrop (`_backdrop_flights()`, the flights-mode radar-sweep wedge
   blown up full-panel) was drawn at FULL `RING` brightness and moving
   at 7deg/tick -- over 2x the already-just-dimmed 3deg/tick idle scope
   sweep from the earlier radar-hierarchy pass this same session. That
   made the busiest, fastest-moving thing on the whole card the
   decoration BEHIND the hero silhouette, not the hero itself -- the
   exact "decoration competing with the subject" problem just fixed on
   the main scope, recurring here. Fixed the same way: dimmed to ~50%
   brightness (`rim(self.RING, 0.5)`), same shared backdrop language
   (still correctly reads as "window" via the violet tint), no new
   visual system.
2. **"Sparse and meaningful -- one or two perfect facts, not a data
   dump."** The raw `{dist}MI {alt}FT` pair (two bare numbers, no
   narrative) was replaced with the SAME real Hangar "story" fact the
   post-dismissal DETAIL card already leads with: "FIRST SIGHTING" or
   "SEEN NX", read from the exact same `hangar.LOG.get()` lookup this
   screen was already doing (previously only to decide whether to draw
   the first-sighting ring, the fact itself was never shown as text).
   Falls back to real distance only when there's genuinely no Hangar
   entry for this registration (no broadcast registration, or not
   recorded yet) -- an honest degrade, never a blank row.
3. **"Timing must feel inevitable."** Explicitly NOT touched this
   pass, stated honestly rather than papered over: the real trigger
   (`flights.FEED`'s adopt-then-diff `in_window` detection, see the
   PLANE-IN-WINDOW TAKEOVER section far above) already fires the
   instant a real poll shows a genuinely new window entry, bounded only
   by the real ADS-B poll cadence (`flights.POSITION_REFRESH`, ~15s) --
   that IS the real available granularity; there is no faster real
   signal to trigger on without inventing a predictive "about to enter"
   guess, which this project's own "never invent" rule rules out. No
   code change proposed here without a concrete real-world case showing
   the current cadence actually feels late.
4. **"Handoff to Hangar should feel like the next beat."** Point 2's
   fix does double duty here: because the takeover screen NOW shows the
   exact same real fact ("FIRST SIGHTING"/"SEEN NX") the ceremonial
   DETAIL card leads with after dismissal, the two screens tell one
   continuous story ("you just saw it" -> "here's its story") instead
   of the takeover showing raw numbers and the DETAIL card then
   introducing a completely different kind of fact. The shared `RING`/
   `HANGAR` violet color (already identical between the two screens,
   confirmed by reading both, not assumed) reinforces the same
   continuity visually.

**A real render_audit.py coverage gap was found and fixed in the same
pass, not shipped blind**: none of `drive_planewatch()`'s three existing
synthetic variants use a registration that matches a real Hangar entry,
so the new "FIRST SIGHTING"/"SEEN NX" branch had ZERO automated coverage
-- every existing variant silently fell through to the distance-fallback
branch instead. Added two new driven variants that monkeypatch
`hangar.LOG.get()` (never the real `hangar_log.jsonl` file) to exercise
both real text branches directly.

**Verified**: `render_audit.py planewatch` clean (6 frames, up from 4;
only a pre-existing unrelated truncation warning). Full project sweep
and `fold_audit.py` both clean. A synthetic render (first-sighting case)
sent as a PNG for direct visual review -- confirmed the silhouette now
reads as unmistakably the subject against the calmer backdrop. Real
service restart, confirmed healthy (0 loop_errors) -- this mode is
force-triggered only, so there is no live mode-switch check possible
without a real aircraft actually entering the window; same honest gap
this feature has carried since it first shipped.

## Flight radar hierarchy pass -- decoration dimmed, size tiers added, selection made unmistakable (2026-08-10)

Direct follow-up to the audit above, with a concrete owner-supplied
priority list (real radar UX research: ATC symbology principles, DIY/
consumer LED radar conventions -- "don't clutter the target", "heading +
category coding beats color alone", "controllers reject decoration that
competes with the target"). Six real changes, all to `_frame_scope()`
and `draw_scope_aircraft()`, no new visual systems added (the explicit
constraint given) -- every change tunes an EXISTING element's brightness/
size/visibility from a real fact already on hand, none invents a new one.

1. **Sweep and coastline dimmed** (`SWEEP_COLOR`/`COASTLINE_COLOR`, new
   class constants, ~55%/~40% dimmer than their previous defaults) --
   both were previously drawn in the same brightness class as a routine
   aircraft icon, so decoration was visually competing with the actual
   subject instead of sitting behind it. Kept under a 90-value ceiling
   in every channel; every real aircraft category color sits well above
   150 in at least one channel, so "decoration" and "traffic" now
   separate at a glance by brightness alone, not just by convention.
2. **Calm/busy density scaling** (`CALM_DENSITY_FLOOR`/`CALM_DENSITY_AT`,
   new) -- the (already-dimmed) sweep/coastline colors scale down
   further as real aircraft count drops (floored at 55% so they never
   vanish outright), reaching full brightness only at 4+ real aircraft.
   A quiet sky with 1 aircraft up no longer looks as "busy" as a full
   one just because the decorative layer never changed with it.
3. **Trail shortened and dimmed further** (`TRAIL_MAX_POINTS` 20->10,
   ~5min->~2.5min of real polls; brightness `c//3`->`c//5`) -- keeps the
   real flown-path information (still the one context line with no
   duplicate anywhere else in the mode, see the route-ray removal
   above) while making it read as "recent history" rather than a line
   rivaling the range rings for visual weight.
4. **Distance legend ("12/23/46MI") now hidden whenever anything is
   selected** (aircraft or the airport), and dimmed further even when
   shown. It's reference context for reading the WHOLE scope; a focused
   view on one aircraft doesn't need it competing for attention.
5. **Icon SIZE now steps through the SAME hierarchy brightness already
   uses**, instead of one shared "big" tier for every reason an
   aircraft might stand out. `draw_scope_aircraft()`'s `big: bool` param
   was replaced with a real `scale: float`, and `_frame_scope()` now
   computes a genuine ladder: `SCALE_ROUTINE` (1.0) < `SCALE_NOTABLE`
   (1.12) < `SCALE_WINDOW` (1.22) < `SCALE_FAVORITE` (1.32) <
   `SCALE_SELECTED` (1.5). **Selection is the single largest tier,
   above even a favorite** -- direct owner ask ("make selection
   unmistakable"): the aircraft you're actually looking at right now
   always visually wins, regardless of what else is independently true
   about it. ATC-matched aircraft keep their own distinct color
   (`ATC_MATCH` green) and ride the `SCALE_NOTABLE` tier rather than
   needing their own size step.
6. **Hierarchy consistency, checked not assumed**: the DETAIL card
   (reached via select-to-expand) already used the same real
   favorite > notable > routine size/brightness ordering from an
   earlier session (2026-08-08) -- confirmed still true by reading its
   current code rather than re-verifying from memory; no window tier
   there since "in the window" is a scope-specific concept with no
   equivalent off-scope. Left untouched -- already consistent with the
   language just extended on the scope.

**Explicitly NOT done, per the owner's own "what not to do" list**: no
new visual systems, the route ray stays gone, no attempt to show more
data on the scope (DETAIL/FLIGHT PATH still own that job), heading-first
icon shapes/category-color coding left untouched (already matches the
cited ATC-symbology research -- shape+color already carry heading and
category, altitude already lives in DETAIL text rather than being a
primary scope-color channel).

**Verified**: `render_audit.py flights` and the full project sweep both
clean (0 modes failed; only pre-existing unrelated marquee-edge/
truncation warnings). Two new synthetic renders sent for direct visual
review: the same busy 4-aircraft scene as the route-ray removal (now
with the helicopter selected -- visibly the largest icon on screen,
dimmer sweep/coastline, legend hidden), and a new calm 1-aircraft/
nothing-selected scene (visibly dimmer decoration, legend present but
subdued). Real service restart, switched to `flights`, confirmed a real
non-error `/api/frame`, panel restored to `ambient` afterward.

## Flight radar full audit -- route-bearing ray removed as a real duplicate (2026-08-10)

Owner ask, prompted by the SAME screenshot that drove the weather-radar
redesign above turning out to actually be a screenshot of THIS scope
(confirmed by matching "12/23/46MI" to `SCOPE_RING_NM=(10,20,40)` run
through `nm_to_mi()`): "explain everything about the flight radar... do a
full audit... what happens when a plane is in view and everything that
actually happens." Answered with a full walkthrough of the real pipeline
(flights.py's polling/notability/window/favorite computation ->
FlightEngine.tick()'s dead-reckoning/trail math -> _frame_scope()'s
~10-layer render stack -> the step-by-step lifecycle of a real aircraft
entering range) before touching any code -- see that explanation for the
full detail; not fully reproduced here.

**Finding, stated plainly: nothing here is a rendering BUG.**
`render_audit.py` was clean before this change and stays clean after --
every individual layer (rings, coastline, sweep, trail, route ray,
airport marker, per-aircraft icon/glow/ring) was already correctly
drawn, audited, and individually justified across many prior sessions.
The real problem is ACCUMULATED VISUAL DENSITY: ~10 independent visual
systems built incrementally, each reasoned about in isolation, stacked
on one 64x64 screen with nobody asking whether the WHOLE PICTURE still
reads as coherent once they're all present at once -- the same class of
gap the weather-radar redesign just surfaced from a different angle.

**Fix, following the owner's explicit direction ("keep everything, just
clean up the visual hierarchy"): the route-bearing ray was removed from
the scope, not hidden or relocated -- because auditing what it actually
contributed found it was a real DUPLICATE.** The dedicated
`VIEW_FLIGHT_PATH` world-map view (built earlier this project, see the
"Flight select menu + global flight-path map" section above) already
shows the same real fact -- which way this aircraft's route goes --
more legibly: real city names, a proper equirectangular projection, no
crowding from 7 other aircraft sharing the same tiny scope. Removing the
ray loses no real information, it just stops drawing the same fact
twice in two different places, one of them badly. **The trail (recent
real flown path) was explicitly kept** -- audited the same way and found
to have no duplicate anywhere else in the mode, so it stays exactly
where it was, drawn the same way, only for the one selected aircraft.
This halves the "crossing lines" the owner specifically flagged as
confusing (trail + ray -> trail only) while genuinely keeping every
real feature, matching the direction given rather than a unilateral cut.

**Considered and rejected: moving the trail into the DETAIL card
instead of leaving it on the scope.** Read through the DETAIL card's
own render code first -- its own comments state it is ALREADY at zero
spare vertical rows (a fully audited fixed layout, icon reaching
x=21..43 at its widest heading, text packed to the real HEIGHT-5=59
bound below it). There is no genuine free rectangle to add a mini-scope
graphic without reopening the exact collision risk that budget exists
to avoid -- confirmed by reading the layout math, not guessed. The
trail stays on the scope, where it already had room.

**Verified**: `render_audit.py flights` clean before and after (0 modes
failed, only pre-existing unrelated marquee-edge/truncation warnings).
A realistic synthetic 4-aircraft scene (one selected helicopter, one
favorite, one in-window, matching real category/altitude/route shapes)
was rendered and sent as a PNG both BEFORE and AFTER the change for
direct visual comparison -- confirmed the crossing-line count visibly
dropped from two to one. Real service restart, switched to `flights`
mode, confirmed a real non-error `/api/frame`. Panel restored to
`ambient` afterward.

## Weather visual expansion: storm radar, hourly forecast, ambient touch (2026-08-10)

Owner ask, after the alert-detection/scroll-speed audits above surfaced
real live data worth building on: "make weather visually stunning" --
scoped into three real pieces, all built on data already confirmed real
and live earlier the same session (the storm's real polygon geometry,
NWS's real 156-hour gridpoint forecast).

**A real design mistake was caught and corrected mid-build, not shipped
blind.** First draft of the storm radar view reused the flight/satellite
scope's full visual language verbatim (dotted rings, the storm's real
warned polygon outline, a second crossing line for projected motion, a
tiny abbreviated legend). The owner then flagged, with a real screenshot,
that this SAME scope language was already confusing elsewhere in the
project ("no idea what the icons mean", "too cluttered for a tiny
screen") -- meaning the new radar view would have inherited the identical
problem by construction, not by accident. **Rebuilt from scratch** around
one rule: a stranger should read it at a glance, no legend needed.

**`_frame_radar()` (rewritten)** -- ONE ring, ONE pointer (home to the
storm's real bearing, nothing else on the circle: no polygon outline, no
second motion line), plus PLAIN TEXT for everything else: real distance
in miles (`nm_to_mi()`), real 8-point compass direction (reusing the
SAME `_compass()` the conditions view's wind direction already uses --
one compass convention, not a second), and real motion described in
words ("MOVING NE") instead of a second vector nobody could parse at a
glance. Only the single CLOSEST tracked storm is shown at once (not the
full alert list simultaneously); multiple tracked storms page via
left/right, mirroring the existing alert-cycling input shape on the main
view. Honest empty state ("NO STORMS TRACKED") when nothing currently
carries real position data -- the far more common case, since most
active alerts are zone-based watches/advisories with no point geometry
at all.

**`_frame_hourly()` (new)** -- real NWS gridpoint hourly forecast
(`weather._fetch_hourly()`, new: temp, precip%, short text, wind, kept
to the next `HOURLY_MAX_PERIODS` (18) of the real 156 hours NWS returns).
Paged 3 hours at a time (`HOURLY_PAGE`, lowered from an initial 4 once
feels-like needed a second text row per hour) via left/right. Two plain
TEXT rows per hour -- time+temp, then feels-like/precip -- not a bar
chart or sparkline, same "plain readable numbers, not unlabeled pixel
graphics" lesson the radar rebuild just taught. A real render_audit.py
overflow was caught and fixed before shipping here too: the first
wording ("FEELS 104  20% RAIN") ran 75px against a 62px budget; shortened
to match the main conditions view's own existing "FL 83F" tag convention
("FL 104", "20%") rather than inventing a second, wordier style.

**Real "feels like" for hourly, computed via NWS's OWN PUBLIC FORMULA,
not a guess.** The hourly endpoint carries no direct heatIndex/windChill
field at all (confirmed live -- only temperature/dewpoint/
relativeHumidity), unlike the observation endpoint `feels_like_c()`
already prefers a real reported field from. `weather._heat_index_f()`/
`_wind_chill_f()` (new) are the Rothfusz regression and NWS's own wind
chill formula -- the SAME published formulas NWS itself uses to compute
the real heatIndex/windChill fields elsewhere -- applied only at NWS's
own real thresholds (heat index >=80F, wind chill <=50F+3mph) via
`_hourly_feels_like_f()`, which returns `None` outside those bands
(the actual temp already IS the feels-like number there, same reasoning
`feels_like_c()` already established). This is the one place in the
project a "feels like" number is genuinely DERIVED rather than read --
stated explicitly since `feels_like_c()`'s own docstring brags about
never computing one; justified here because no real field exists to
prefer, and the formula is public/standard, not invented. Verified
against real live data: 88F + 77% RH -> 104F heat index, a realistic
coastal-August value, not a fabricated one.

**Weather-reactive ambient touch, extended to where it fits ("dynamic
visual modes should be everywhere more where it fits logically")** --
`_draw_weather_ambient()` (new): real NWS conditions text containing
RAIN/SHOWER/STORM/DRIZZLE draws sparse falling streaks behind the
numbers; genuine night (real sunrise/sunset bounds, not a guessed hour)
draws a fixed set of slowly-twinkling stars. Gated strictly on REAL
fields already on `self.data` -- never a generic "make it pretty"
animation running regardless of actual conditions. Kept sparse (under a
dozen `put_px` calls) to stay well under this project's per-frame cost
discipline. Wired into BOTH the main conditions view and the new hourly
view (both are plain data screens with room for it); DELIBERATELY left
off the radar view -- that screen already communicates the weather
condition visually via its own pointer, and adding rain streaks there
would risk re-cluttering the exact screen just simplified for legibility.

**A real tool gap was found and fixed in `render_audit.py` itself,
not just in weather.** The generic view-sweep only knew the `VIEW_*`
integer-constant convention (`eng.view = 0, 1, 2...`); `WeatherEngine`'s
new `VIEWS = ["main", "hourly", "radar"]` is a STRING-valued convention
the sweep silently no-op'd against (`eng.view = 0` is never equal to
`"hourly"`), meaning a "clean" audit run gave ZERO real coverage of the
new views -- the exact "one system doesn't know about a state another
just entered" bug class this project names repeatedly, this time in the
audit tool meant to catch it. Generalized (not special-cased to weather)
so any future string-`VIEWS` engine is covered automatically: 29 frames
became 32 once the sweep actually reached the new views.

**Verified**: both audits clean throughout (including the two real bugs
caught mid-build: the hourly overflow, and the fold-audit gap on the new
`hourly_url`/`forecastHourly` raw URL field -- same "URL field, never
drawn" exemption category as `county`/`forecastZone`/`zones` from the
earlier alert-zone fix, added to `fold_audit.SKIP_KEYS`/`ignore_paths`).
A new permanent `fold_audit.py` case (`weather hourly forecast`) proves
`_fetch_hourly()`'s `short` field actually folds, verified by hand-
checking the injected canaries would survive if the fold were skipped
(same standing methodology as every other fold case here). Real
polygon/motion path exercised directly against real nationwide alert
data (a live Severe Thunderstorm Warning elsewhere in the country, since
the local storm from earlier in the session had genuinely cleared by
this point) -- 0 dropped/overflow/clipped across both a motion-carrying
and a motion-less tracked storm. Real service restart; all THREE views
cycled live via the real `/api/press/down` input path with distinct,
non-identical frames confirmed at each step (byte-diffed, not assumed);
real feels-like data (88F -> 104F) confirmed flowing end-to-end from a
live `weather._fetch_hourly()` call through to the rendered frame.
Synthetic PNGs sent for direct visual review before and after the radar
redesign, and again after the feels-like addition. Panel restored to
`ambient` afterward.

## Global severe-weather takeover demoted to a periodic banner (2026-08-10)

The weather zone-query fix above (see that section) unlocked the global
severe-weather takeover firing live for the FIRST TIME EVER on a real,
hours-long Severe Thunderstorm Warning -- and the original design
(full-screen, no cycling, for the alert's whole real duration) read as
"stuck" the moment it actually happened, per direct owner feedback.
Two real iterations, same session, both owner-specified:

**Iteration 1 -- full takeover to a bottom banner.** `engines.
draw_severe_alert_banner()` (new) composites over whatever's already
rendered, same bottom-20px non-blocking footprint `draw_notify_banner()`
already established for HA notifications, but themed for real urgency
rather than reused verbatim: a THICKER pulsing accent stripe in the real
NWS severity color (`ALERT_SEVERITY_COLOR`), not notify's static 1px
cool-blue divider. `arcade_server._severe_alert_frame(frame)` was
rewritten from "return a standalone full-screen replacement frame" to
"composite onto and return `frame`" -- same contract shape as
`_notify_banner_frame()`. `engines.draw_alert_frame()` (the ORIGINAL
full-screen version) is or was NOT deleted -- it's still exactly what
`WeatherEngine`'s own dedicated `weather` mode view uses when someone is
actually looking at weather, storm mini-scope included; only the
GLOBAL cross-mode takeover was demoted.

**A real crash was introduced and caught in this pass, not shipped
blind**: two downstream references to the old `alert_frame` local
(mode-transition-skip and night-dimming-exemption) were missed on the
first edit, throwing `NameError: name 'alert_frame' is not defined` on
literally every render tick -- confirmed via the real live log
(`~/Library/Logs/henderburgh-arcade.log`), not assumed clean. Fixed by
tracking severe-banner state as a real instance flag
(`self._severe_active`, set inside `_severe_alert_frame()`) instead of
a local variable: the transition-skip exemption was REMOVED entirely
(a small banner riding along on top of a transitioning mode is fine,
matching how the already-untransitioned-exempted notify banner already
behaves -- no special case needed once it's not a full-screen
interrupt), while the night-dimming exemption was KEPT and re-wired to
the new flag (a real severe alert dimmed to 28% at 3am still defeats
the entire point of surfacing it, banner or not).

**A second, unrelated mistake happened while fixing the above**: `import
arcade_server` was run directly to syntax-check the file -- the exact
panel-lockup hazard this project's own standing rule exists to prevent
(constructs a second live `Arcade`/DDP-sender singleton while the real
launchd service may already be running). Caught immediately, the panel
was confirmed still pingable, and `launchctl kickstart -k` was run right
away to guarantee exactly one DDP sender exists again -- no lockup
occurred this time, but the near-miss is recorded here as a live
reminder of the rule (`ast.parse()` only, never a real import, for
exactly this reason).

**Iteration 2 -- permanent banner to a PERIODIC one.** Even as a small
banner, it still sat on screen for the alert's ENTIRE real duration
(could be an hour+) -- still "stuck", just a smaller stuck, per the
owner's own next request: "should happen for like 15 seconds every 5
minutes or something for as long as it is [active]." New module-level
constants `SEVERE_BANNER_INTERVAL_S = 300.0` / `SEVERE_BANNER_SHOW_S =
15.0` in `arcade_server.py`. Visibility is gated on WALL-CLOCK PHASE
(`time.time() % INTERVAL_S < SHOW_S`), not a tick counter -- deliberately,
so the schedule can't drift and needs no persisted state across mode
switches or restarts. Verified the phase math directly (not just read):
computed `visible` across 13 points spanning a full 300s cycle (0, 5,
14, 15, 20, 100, 200, 295, 299, 300, 305, 315, 316 seconds in) --
correctly `True` only for `[0, 15)` and `[300, 315)`, `False`
everywhere else, exactly matching the intended 15-on/285-off pattern.
`self._severe_visible_last` (new) detects the moment a new visible
window begins so the pulse tick counter and marquee scroll RESTART
CLEAN each time, rather than resuming mid-scroll from wherever they
were ~5 minutes ago when the banner last disappeared (which would read
as a broken/stale scroll on reappearance, not a fresh pop-up). The
underlying `weather.FEED` polling (10min conditions / 2min alerts) is
UNCHANGED -- the periodic gating only affects DRAWING, never polling,
so alert freshness itself doesn't degrade between visible windows.

**Verified**: both audits clean (this touches only compositing/gating
logic in `arcade_server.py`, no new render surface beyond iteration 1's
already-audited `draw_severe_alert_banner()`). Real service restart
after the `NameError` fix confirmed `loop_errors: 0` (was accumulating
one crash per render tick before the fix). Real live frame pulled in
`flights` mode with a real active severe alert: confirmed real flights
radar content in the top 40 rows (701 non-black px) AND the banner
lit in the bottom 20 rows (1280 non-black px) simultaneously -- both
visible at once, proving the composite-not-replace contract actually
holds on a real frame, not just in the return-type reasoning. Panel
restored to `ambient` afterward. The full 5-minute real-time on/off
cadence was intentionally NOT idle-watched live in this session (the
phase math and the live compositing are both independently verified;
waiting out multiple real 5-minute cycles just to watch it wasn't
worth blocking on) -- worth a real eyes-on confirmation next time a
severe alert is active for a few cycles.

## News headlines were cut off before the scroll finished (2026-08-10)

Owner report, same session as the weather audit above: "the scrolling is
correct, but it does not scroll until the story finishes all the way
until switching to next story, and a lot of it gets cut off for every
story." Confirmed as a real, measurable bug, not a perception issue.

**Root cause**: `NewsEngine.SPOTLIGHT_TICKS = 260` (~13s) was a FIXED
dwell per headline, regardless of length -- the class's own old
docstring even called this out as deliberate ("fixed tick cadence...
pacing is predictable regardless of headline length"), but that
design was wrong in practice. Checked against real fetched Fox
headlines (`news._fetch_headlines()`): real headlines run 70-110
characters. The spotlight tape draws at scale=2 (`draw_marquee`'s pitch
= `4*scale` = 8px/char) at a scroll speed of 0.5px/tick (10px/sec) --
so a 100-char headline needs the tape ~84s to scroll past it ONCE, while
the fixed 13s dwell showed barely 15% of it before jumping to the next
headline. Every real headline was getting cut off, exactly as reported.

**Fix**: `NewsEngine._spotlight_dwell_ticks(headline)` (new) computes
the dwell from the SAME pitch/gap/scroll-speed constants the render
path actually uses (`SPOTLIGHT_SCALE`, `SPOTLIGHT_GAP`, `SCROLL_SPEED`
-- now named class constants instead of duplicated literals in two
places, so the dwell math and the drawn scroll distance can't drift
apart again) -- ticks needed for one full scroll pass, plus a
`SPOTLIGHT_PAUSE_TICKS` (~1.5s) settle once it completes, floored at
`SPOTLIGHT_MIN_TICKS` (~6s) so a short headline still gets a readable
beat. `tick()`'s auto-advance now checks this per-headline dwell
instead of the old fixed constant.

**Verified**: both audits clean (touches only cycling logic and the
gap/scale of an existing marquee call, no new render surface).
Directly computed the dwell against 5 real fetched headlines: 61-93.5s
each (was 13s flat). Drove a real `NewsEngine` through 2500 simulated
ticks with real fetched headlines and a frozen feed: confirmed the
engine advanced from headline 0 to headline 1 at tick 1853, exactly
matching `_spotlight_dwell_ticks(heads[0])`'s own computed 1854 (the
one-tick difference is `hold` starting its count at 1, not 0 -- exact,
not approximate). Real service restart, switched to `news` mode,
confirmed a real non-error `/api/frame`. Panel restored to `ambient`
afterward.

**Follow-up, same session: "feels off now."** The fix above was
CORRECT (headlines genuinely stopped cutting off) but over-corrected
the feel: at the original 0.5px/tick (10px/sec) scroll speed, waiting
for a full pass meant 60-93s per real headline -- technically not cut
off, but reads as the ticker being frozen, which is its own kind of
wrong. The actual fix needed BOTH halves together, not either alone:
`SCROLL_SPEED` raised 5x, from 0.5 to 2.5px/tick (50px/sec). Recomputed
against the same real fetched headlines: 15.7-19.9s each -- back near
the ORIGINAL mode's ~13s brisk pace, just now genuinely completing the
scroll instead of arbitrarily cutting it off partway through. Because
`_spotlight_dwell_ticks()` and the render path both read the same
`SCROLL_SPEED` class constant (the whole point of centralizing it in
the first fix), changing this one number is the entire fix -- no
other code touched. Verified: both audits clean, real service restart,
real non-error `/api/frame` in `news` mode, panel restored to `ambient`.

## Weather alerts were missing real severe warnings -- zone query replaces point query (2026-08-10)

Owner report: had received 2-3 real severe weather alerts (presumably on
their phone) but the panel's `weather` mode only ever showed "Heat
Advisory". Investigated against real live NWS data rather than guessing,
and found a real, confirmed gap, not user error or stale polling.

**Root cause, confirmed live**: `weather.py` queried
`/alerts/active?point={lat},{lon}` -- precise, but a real, CONCURRENTLY
ACTIVE Severe Thunderstorm Warning for the exact configured home county
(Horry, SC / UGC `SCC051`) was completely ABSENT from that point query.
Cross-checked against `/alerts?zone=SCC051` (the county's full alert
history): the warning was real, `severity: Severe`, with a real polygon
geometry and real storm-motion parameters (`eventMotionDescription`)
-- it simply hadn't swept over the LITERAL home coordinates yet, even
though it covered the whole county. A phone's Wireless Emergency Alert
fires at county granularity, not exact-point-in-polygon, which is
exactly why the owner got a real alert on their phone that the panel,
querying more strictly than necessary, never saw.

**Fix**: `weather._fetch_point()` now also extracts the real UGC zone
codes NWS's own `/points` response carries (`county` and
`forecastZone`, via new `_zone_id_from_url()` -- NWS gives full zone
URLs, the alerts endpoint wants the bare code) into a `zones` list.
`_fetch_alerts()` now queries `/alerts/active?zone=Z1,Z2` (confirmed
live NWS accepts a comma-separated multi-zone query) once the point has
resolved, falling back to the original point-based query before the
first successful point fetch (cold start). Zone-based watches/
advisories (already zone-scoped, not polygon-scoped) are unaffected
either way -- this only changes behavior for the polygon-warned SEVERE
event class (Thunderstorm/Tornado/Flash Flood warnings), which is
exactly the class most worth not missing. `weather.FEED.get()`'s
`alerts` list, the global severe-weather takeover
(`arcade_server._severe_alert_frame()`), and the storm-motion mini-scope
all read from the same one alerts list, so all three inherited the fix
automatically -- no other code needed to change.

**A real second-order bug caught by `fold_audit.py` itself, not
missed**: the new `zones` UGC codes ("SCC051") are identifiers used to
build a URL, never drawn on screen -- but the audit's canary-injection
test appends canary characters to every raw string field it doesn't
explicitly skip, including the RAW `county`/`forecastZone` URL fields
before `_zone_id_from_url()` extracts the trailing segment, which
landed the canaries INSIDE the extracted zone code
(".../SCC051" -> ".../SCC051<canaries>"). This was a bug in the test's
own injection scope, not a real fold gap -- a UGC code was never meant
to be folded, the same reasoning `abbreviation`/`href`/`link` are
already skipped in `fold_audit.SKIP_KEYS` for. Added `county`,
`forecastZone`, and `zones` to that set.

**Verified**: both audits clean. Directly confirmed against real live
data: `weather._fetch_point()` resolved `zones: ['SCC051', 'SCZ054']`
for the real configured home; `weather._fetch_alerts()` with those
zones returned 2 real alerts (Severe Thunderstorm Warning, then Heat
Advisory, correctly severity-sorted) where the point-only query returned
only 1; the real storm-motion fields populated correctly on the newly-
surfaced warning (bearing 314deg, 39.4nm, moving 269deg at 18kt -- real
numbers, not fabricated). Real service restart, switched to `weather`
mode, confirmed `score: 2` on the live running engine (matching the 2
real alerts), pulled a real non-error `/api/frame`. Panel restored to
`ambient` afterward.

## Favorites driving ambient dwell time (2026-08-10)

Owner ask, explicit about scope: "a modest weight multiplier, not a
takeover." Extended `FlightEngine.ambient_weight()` and
`SportsEngine.ambient_weight()` (both already existed, already the
mechanism `AmbientEngine` uses to decide how long each sub-mode's turn
lasts -- see satellite's own `ambient_weight()` a few sections up for
the original precedent) to give a favorite a small, additive nudge when
one is genuinely active right now.

**`FAVORITE_AMBIENT_BOOST = 0.3`** (new, module-level in `engines.py`,
shared by both overrides) -- deliberately smaller than the smallest real
tier gap either engine's existing weight ladder has (0.5, "nothing" to
"something exists" on both). This is load-bearing, not a nice round
number: it means a favorite can only ever move dwell time WITHIN
whatever tier its own real merits already earned, never leapfrog a
genuinely bigger, unrelated signal into a higher tier. Verified directly
(not just reasoned about): a favorite non-notable aircraft scores 1.3,
strictly below a non-favorite NOTABLE aircraft's 2.5; a favorite in the
sports ticker-filter case scores 1.3, strictly below any actually-live
game's 3.0. Same additive-only shape as satellite's own WINDOW FILTER
nudge to `ambient_weight()` (+0.25) and flights' `WINDOW_BOOST` to sort
ranking -- a pattern already established twice in this project, reused
rather than reinvented a third way.

**Two real, already-computed signals, nothing re-derived:**
- **Flights**: `is_favorite`, a real per-aircraft bool already stamped
  by `flights._fetch_positions()` (see `FAVORITE_BOOST`'s own sort-side
  precedent, 2026-08-09) -- `ambient_weight()` just checks whether any
  currently-tracked aircraft has it set.
- **Sports**: two real paths, either counts. The PINNED favorite's own
  game being live (`self.data["favorite_game"]["state"] == "in"`,
  already on `SportsEngine.data`). Or the favorite-teams TICKER FILTER
  being on with a live match surviving it -- the filter is already
  applied at the feed source (`sports.FEED.get_universal()`, per
  2026-08-08's own note), so "any live event in the already-filtered
  `self.universal` list, while the filter is enabled" already and
  exactly means "a favorited team is live" -- nothing new to compute,
  just read `sports.FEED.get_favorite_teams()`'s existing
  `filter_enabled` flag.

**Verified**: both audits clean (this change touches only weighting
logic, not rendering, so no new render/fold surface). Directly exercised
`ambient_weight()` against constructed engine instances covering every
combination (no games/no aircraft, plain, favorite-only, notable-only,
notable+favorite for flights; no games, games-not-live, live-not-
favorite, pinned-favorite-live, and ticker-filter-live for sports) --
printed weights matched the tier-safe design exactly in all cases. Real
service restart, confirmed healthy (`/api/state` clean, 0 errors, 0
loop_errors) before and after.

**Found in passing, NOT part of this change, flagged separately**: the
live service log showed a real, unrelated `KeyError: 3` in
`FlightEngine.input()`'s rotate-view-cycling dict (a `self.view` value
not covered by the dict's keys -- likely the newer `VIEW_MENU`/
`VIEW_FLIGHT_PATH` views added in the flight-select-menu work not yet
wired into this cycling table). Spun off as its own follow-up rather
than folded into this change.

## Owner Note -- a persistent, owner-authored message (2026-08-09)

New mode (`ownernote.py` + `OwnerNoteEngine`, `ENGINES["ownernote"]`),
explicitly distinct from two existing text-on-panel features that could
be mistaken for the same thing: `/api/notify` (HA-pushed, EPHEMERAL,
auto-clears) and `blog.py`'s guestbook (visitor-submitted, read-only,
mirrors the public site). This is the owner's OWN typed words, saved
once from the control panel, and stays on screen until changed or
cleared -- a real sticky note.

**No I/O module in the `FEED`/poller shape** -- `ownernote.py` is local
config only (`ownernote_config.json`, one key, `{"text": str|None}`),
same reasoning `notify.py`'s own docstring already gives for why it
isn't `FEED`-shaped either: the owner PUSHES via the HTTP endpoint,
there's nothing to poll. `OwnerNoteEngine.tick()` just re-reads the
config file directly (matches `dnd.py`'s own "just read the config"
simplicity for a similarly tiny setting), no background thread.

**Folded at the WRITE boundary, inside `save_config()` itself** -- an
owner-typed note is exactly the kind of text that can carry curly
quotes/em-dashes/accents the 3x5 font can't draw, the same bug class
this project has hit ten-plus times on externally-sourced text, just
from a different source (the owner, not an API) this time. A live check
confirmed a real em dash ("Back Sunday — feed the fish...") correctly
folds to a plain hyphen before ever reaching the config file.

**Visual design deliberately modeled on `BlogEngine`** (calm, no
scrolling, no pulsing, Vestaboard-style) -- a static note has even less
reason to animate than the guestbook's rotating posts, so it's simpler
still: one screen, wrapped across up to 6 lines via the shared
`wrap_text()` helper, vertically centered, `ambient_weight() == 0.8`
matching the guestbook's own quiet weighting.

**Verified**: both audits clean (added a new permanent `fold_audit.py`
case, `ownernote.save_config`, replaying a real dirty string through the
actual save path and restoring the real prior config afterward -- proved
it would catch a real regression by hand-checking the unfolded canary
characters survive when the fold is skipped, same standing methodology
as every other fold case here). `render_audit.py ownernote` clean against
both the empty state and a real long note. Real service restart, full
live endpoint round-trip (`GET`/`POST /api/note`, a real em-dash note
saved and folded correctly, mode-switched to `ownernote` with a real
`/api/frame`-worthy clean state, cleared and reverted to `ambient`
afterward, config confirmed back at `{"text": null}`).

## Now Playing gets a mic-only equalizer toggle (2026-08-09)

Owner ask: "have an option in that now playing that we can use the
microphone instead for the equalizer." `nowplaying.py`'s config gained a
third field, `mic_only` (default `False`), following the exact same
"omitted preserves the existing value" convention `api_key`/`clear_key`
already established a few sections above -- `save_config(mic_only=None)`
leaves the saved value untouched, `save_config(mic_only=True/False)`
changes it, same reasoning: a save that only changes the username must
not silently reset this toggle any more than it should silently wipe the
key.

When on, `NowPlayingEngine.frame()` skips Last.fm entirely and shows a
plain full-height equalizer (`"EQUALIZER"` header, real
`audio_sync.FEED` bars filling most of the panel, or an honest "NO AUDIO
SIGNAL FROM PANEL MIC" when `audio_sync.FEED`'s own `stale` flag is set)
-- no Last.fm account needed at all. The bar-drawing code itself was
factored out of the track-view's small bar row into a shared
`_draw_fft_bars(buf, y0, y1, color)` helper, reused unchanged at both the
small (track view) and full-height (mic-only view) scales -- same real
mic-derived FFT data either way, never synthesized.

`has_content()` now returns true when EITHER a Last.fm account is
configured OR `mic_only` is set, so the mode counts as real content with
zero Last.fm setup.

Control panel gained a checkbox ("Use the mic as a plain equalizer
instead") on the existing Now Playing card, POSTing `mic_only` alongside
the existing fields; a dedicated `change` handler lets the toggle save on
its own without touching the username/key fields. `render_audit.py`'s
`drive_nowplaying` custom driver gained two mic-only variants (configured
and unconfigured) alongside the existing four Last.fm variants.

Verified: both audits clean, real service restart, real endpoint
round-trip against the live service confirming the omitted-preserves
behavior (`mic_only=true` set, then a user-only save with `mic_only`
omitted correctly left it `true`), reverted to clean defaults afterward.

## Flight select menu + global flight-path map (2026-08-09)

Owner ask: "when scrolling planes in flights, when you hit enter or
button, a menu pops up with flight path with the most logical global map
of it that's presentable, and then maybe hangar option? Something just
logical." Replaced the blind rotate-chain with a real, discoverable menu
and added a genuine world map.

- **`flights.WORLD_COASTLINE`** (new) -- a real one-time extraction from
  Natural Earth 110m (`ne_110m_coastline.geojson`, public domain, the
  SAME source `flights.COASTLINE` already used), embedded in source, NOT
  a runtime fetch (coastlines don't move -- the identical reasoning
  already documented for the local coastline). Real source: 134
  features / 5128 points; decimated to **45 segments / 415 points**
  (~92% cut) by dropping sub-6° features then Ramer-Douglas-Peucker at
  2.0° (deliberately sub-pixel at this scale). **Verified genuinely
  real, not fabricated**: 415/415 embedded points match the real source
  file EXACTLY -- the correct signature for RDP, which only ever removes
  points and never invents them.
- **Real `lat`/`lon` now kept on every aircraft** in
  `_fetch_positions()` -- confirmed present on 8/8 aircraft in a real
  live payload before relying on it; each validated numeric so a missing
  value degrades to `None` rather than plotting (0, 0).
- **`draw_world_map()`/`world_xy()`/`great_circle_points()`** --
  equirectangular, chosen because its 2:1 aspect lands exactly on a
  64x32 box with no letterboxing and lat/lon->pixel is a pure linear
  scale with no fitted parameters. The great circle is **properly
  interpolated (spherical slerp)** -- a straight equirectangular line is
  a rhumb line, not the real path, and drawing one would be a subtle
  lie. Antimeridian crossings BREAK the polyline rather than drawing a
  full-width streak that doesn't exist.
- Two real bugs the agent found by RENDERING rather than reading: a
  whole-Earth window made the real MDW->MHT leg three pixels wide
  (fixed with a `_fit_bounds()` window at uniform °/px, whole-Earth
  fallback past 180° of longitude), and once windowed, off-view
  coastline painted straight through the header because `put_px` only
  guards the panel edge, not the map box (fixed with explicit clipping).
- **`VIEW_MENU` (4) and `VIEW_FLIGHT_PATH` (5)**. Menu entries: DETAIL,
  FLIGHT PATH, ATC LOG, THE HANGAR, BACK -- DETAIL first deliberately,
  since it was the old bare-`rotate` destination, so existing muscle
  memory presses rotate twice and lands where it always did. FLIGHT PATH
  is offered ONLY when there's genuinely something real to plot.
  `AIRPORT_KEY` honestly gets a shorter menu (no DETAIL card, no route).
  THE HANGAR's up/down toggle is untouched -- the menu is an additional
  path, not a replacement.
- **A real bug found in the audit tool itself**: `render_audit.py`'s
  internal view sweep was a hardcoded `range(4)`, so it would never have
  touched views 4/5 -- the exact "one system doesn't know about a state
  another just entered" class CLAUDE.md names repeatedly, sitting in the
  tool meant to catch it. Now derived from each engine's own `VIEW_*`
  constants, excluding `VIEW_TICKS` (a DURATION of 240, not a view id --
  including it would have driven 240 nonexistent views). Independently
  confirmed the old code genuinely missed both new views.

Verified: both audits clean on the merged result (flights 31 frames, up
from 29 -- the new views really are being swept), real service restart
clean, all three proof renders reviewed (menu, the real SWA1065
MDW->MHT map, and an LAX->SYD transpacific case confirming the
antimeridian break). Timer safety independently re-verified against a
genuinely pinned feed: 900 ticks parked on the menu and on the map both
stay put (a first attempt appeared to fail, but that test hadn't
actually pinned the feed -- with zero real aircraft in range the
selection is correctly lost, which is right behaviour, not a bug).

**Honest gap**: no live aircraft with a resolved route was selected on
the real panel (the worktree can't deploy to the live service, and
real traffic with resolved routes wasn't in range at merge time) -- the
map and menu are verified by direct engine-driving against real
coordinates and the real embedded coastline, this project's documented
standard method. Worth a real spot-check next time traffic with a
resolved route is up.

## Real first-run setup wizard (2026-08-09)

A guided, skippable, step-by-step flow at `/setup` for the handful of
real things the panel needs to know about where it is: location, home
airport, window bearing, and a favorite team. Every field calls a real
existing (or newly-exposed) endpoint -- nothing invented, nothing
faked.

Real browser APIs used where genuinely available, honest manual
fallback everywhere else:
- **Location**: `navigator.geolocation.getCurrentPosition()` -- a real
  GPS fix from whichever device is running the browser (not necessarily
  the panel itself, same as every other control-panel interaction).
- **Window bearing**: `DeviceOrientationEvent`, with the real iOS 13+
  permission-prompt handling (must fire from an actual user gesture --
  requesting it on page load silently fails) and the standard
  (`alpha`, converted `360 - alpha`) vs. `webkitCompassHeading`
  cross-browser fallback. Never a guessed number -- with no real compass
  signal available, the field just stays manually editable, same as the
  window bearing has always been set (a phone compass held at the
  window, per the original feature's own docstring).
- **Home airport**: no free, reliably-reachable airport-lookup API
  exists (re-checked this session, matches CLAUDE.md's own "airport
  arrivals -- NOT VIABLE FREE" finding from the departure-board work
  above) -- coordinates are a one-time honest manual entry, same as the
  existing MYR seed data always was.
- **Favorite team**: real ESPN league/team dropdowns, reusing
  `/api/sports/teams` (built the same session, same real data source).

**Found and fixed a real, more fundamental gap while building this**:
`flights.save_airport()`/`load_airport()` have existed since the radar
scope's airport marker shipped, but had NO HTTP endpoint at all -- the
home airport (which the departure board and DEPARTING/ARRIVING
classification both depend on) could previously only be set via a
direct Python call, never through any UI. New `GET`/`POST
/api/flights/airport` closes this -- confirmed live against the real
already-configured MYR value (same code/lat/lon round-tripped back
unchanged, zero data loss).

Verified: full 6-step wizard flow clicked through in a real browser
(skip-through and the favorite-team step's real league dropdown both
confirmed), real service restart clean, both standing audits clean.

## Storm-tracking mini-scope + favorite aircraft (2026-08-09)

**Storm mini-scope**: weather.py's `_fetch_alerts()` now parses NWS's real
`eventMotionDescription` parameter (present on real polygon-warning-tier
alerts -- Severe Thunderstorm/Tornado/Special Marine, confirmed live that
only ~3.6% of nationwide active alerts carry ANY geometry, and it's
specifically this tier, not the broader zone-based watches) into real
storm centroid bearing/distance-from-home (via a local
`_bearing_distance_nm()`, same math as `flights.bearing_distance()`, kept
as its own copy rather than a new cross-module import -- matches this
project's existing per-module-copy precedent) plus real motion
direction/speed. `draw_alert_frame()` plots this on a small corner
mini-scope using `scope_xy()` -- the SAME bearing/radius->pixel
convention the flight radar and satellite dome already use -- when
present, and correctly stays off (no empty ring implying data that
doesn't exist) for watches/advisories that genuinely have no point
position. This is the "logical weather tracking, using the radar from
flights and satellites" the owner asked for, NOT the pixel radar imagery
already researched and recommended against earlier this session (that
recommendation stands; this is a different, viable angle on the same
underlying goal). Verified against a real live Severe Thunderstorm
Warning near Kenosha/Racine, WI while building this.

**Favorite aircraft**: `flights.py` gained `load_favorite_aircraft()`/
`save_favorite_aircraft()` (own new file, `flights_config.json` -- this
module's first, since everything else it owns lives in satellite.py's
shared location config, but a favorites list isn't a location fact) and
a real `is_favorite` field computed at the same per-aircraft enrichment
site every other real fact (notable, in_window) already comes from.
`FAVORITE_GLOW_FLOOR = 1.0` joins `NOTABLE_GLOW_FLOOR`/`WINDOW_GLOW_FLOOR`
as the TOP of the existing ordered-floor hierarchy language (favorite
beats window beats notable beats routine), applied consistently across
the scope, the normal DETAIL card, and the Hangar list -- extending the
established pattern, not inventing a fourth one. Also boosts sort
ranking (`FAVORITE_BOOST = 1.5`, the largest of the three boosts,
deliberately -- this is the one signal that's entirely the owner's own
choice rather than something ADS-B/geometry decided) so a favorite can
lead the list even over a HEAVY/HELI on its own. New
`GET`/`POST /api/flights/favorites` endpoint, same shape as
`/api/sports/favorite_teams`.

Verified: both audits clean, real service restart + `/api/frame` pulled
clean post-restart, real endpoint round-trip tested live (set a real
logged Hangar registration as favorite, confirmed via `/api/flights/
favorites` GET, cleared it back to `[]` afterward -- no test data left
behind), synthetic proof renders (scope + Hangar, real registration data)
reviewed before commit.

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

### Per-sport identity icons (2026-08-08)

**Why**: every sport except baseball was colored text only — no visual
identity marker, unlike the flight mode's aircraft-type icon or
baseball's own `draw_diamond()`. Two small icons per sport were added,
matching the flights icon's own "which of these am I looking at" role:
one in the sport's header, one reused as a small accent inside that
sport's `draw_celebration()` big-moment graphic.

**No real team/league logos, no copied trademarked shapes — hard
constraint, followed throughout.** Every new icon is simple original
geometry (line segments / dot clusters via a shared `put_px`-based
`_draw_offsets()` helper — same technique `draw_diamond()`/`draw_outs()`
already established), describing the sport generically: a ball with a
seam, a flag on a pin, a puck, an abstract glove blob. None of these are
any specific real organization's mark.

- `engines.py` gained `draw_icon_football`/`draw_icon_basketball`/
  `draw_icon_hockey`/`draw_icon_soccer`/`draw_icon_golf`/`draw_icon_mma`,
  each a handful of `put_px` calls (well under the aircraft-icon budget
  the "ICON/PERFORMANCE REVISIT" section above exists to protect), plus
  a `scale` param so the celebration accent can reuse the SAME shape
  definition at 2x rather than a second design — the identical "one
  shape language, multiple scaled contexts" pattern the aircraft sprites
  already use.
- `SPORT_ICONS` dispatches by `SportsEngine`'s normalized `sport` key
  (`football`/`basketball`/`hockey`/`soccer`/`golf`/`mma`); `LEAGUE_ICON`
  is the same set keyed by the older `sports.LEAGUE_PATHS` codes (NFL/
  NCAAF/NBA/NCAAB/NHL/EPL) for the pinned-favorite and per-league-ticker
  views, which predate the universal `sport` key. **MLB/baseball is
  deliberately absent from both** — it already carries `draw_diamond()`/
  `draw_outs()` as its real identity mark in the body, and a second
  glyph in the header would be redundant. **Tennis was skipped at the
  time this section was written** — no live tennis renderer existed yet
  to attach an icon to (task #19). Tennis shipped 2026-08-08 (see the
  TENNIS section below) and `draw_icon_tennis`/`SPORT_ICONS["tennis"]`
  were added in that same pass, closing this gap; this note is kept for
  history rather than silently rewritten.
- **Header wiring**: `draw_header()` gained an optional `icon` callable,
  drawn to the left of the title with the title's own budget shrinking
  to make room. Wired into `_frame_universal_generic()` (the fallback
  every sport without a dedicated renderer uses, including basketball
  and any future football/hockey renderer), the older per-league pinned/
  ticker views, and the soccer/golf/MMA main renderers.
- **Celebration wiring**: `_set_big_moment()` gained a `sport` param,
  threaded through every per-sport big-moment detector call site (golf,
  MLB home run, NFL touchdown, NHL goal, MMA finish, soccer goal) into
  the moment dict. `_backdrop_sports()` (the shared sports celebration
  backdrop — confirmed via `CELEBRATION_BACKDROPS`/`_backdrop_sports`
  that it was a generic ring/ray burst with no sport-specific element
  before this) now draws the firing sport's icon (or, for baseball,
  `draw_diamond()` itself, empty — no live base state to show in a
  celebration accent) as a small 2x-scale accent in the top-left corner,
  outside the centered text plate's reach. **The burst/ray backdrop
  itself is unchanged** — this only adds an accent on top, the same
  "backdrop stays each mode's own visual language, only the accent/
  detail varies" principle already documented for the flights/satellite
  backdrops.

**Verified**: `.venv/bin/python -c "import sports, engines, mma"` clean.
`render_audit.py` and `fold_audit.py` both clean (0 modes failed, 0
feeds not folding, 0 COLLISION — checked specifically since header icon
placement is exactly what COLLISION exists to catch). Every icon's pixel
data confirmed by direct inspection (not just "it ran without error") at
both the header scale and the 2x celebration-accent scale, and through
`draw_celebration()` for all seven sports. **Verified against real live
ESPN data**: `sports.FEED.get_universal()` returned live golf, baseball,
basketball, MMA, and soccer events this session; a real `SportsEngine`
driven against that data rendered correct, distinct, non-colliding icon
pixels for all five (basketball via `_frame_universal_generic()`, the
other four via their dedicated main renderers) — direct pixel dumps
confirmed each shape (e.g. basketball's circle+seam, golf's flag,
soccer's ball+dot, MMA's mitt blob). **Live-verified on the real
physical panel**: restarted `com.henderburgh.arcade`, switched to
`sports` mode, and pulled a real `/api/frame` — the golf flag icon
rendered at the expected header position with real non-background
pixels, byte-identical to the synthetic pixel dump. Panel restored to
`ambient` afterward.

**Honestly unverified**: football (NFL/NCAAF) and hockey (NHL) icons
were confirmed only via direct/synthetic rendering, not against a real
live game — both leagues were off-season/no live game this session, the
same honest gap the per-sport MAIN-renderer table below already carries
for football/basketball. The NFL/NHL icons will render through
`_frame_universal_generic()` (no dedicated main renderer for either yet)
the first time either league is actually live; worth a real spot-check
then.

### Tennis — task #19, the last per-sport MAIN renderer (2026-08-08)

Blocked since 2026-08-01 on "no live match to verify against"; unblocked
this session against real live/finished WTA National Bank Open and
Warsaw T-Mobile Polish Open matches, both confirmed live.

**The universal header (`sports.FEED.get_universal()`'s underlying
endpoint) DOES include tennis** — 6 real WTA matches confirmed live this
session — but a `pre`-state match has literally no score/linescores
fields on its competitors. Confirmed empty-until-live, not a missing
field. **The real source of truth is the dedicated per-tour scoreboard**,
`sports.SCOREBOARD_URL.format(path="tennis/atp")` /
`path="tennis/wta"` — the same `SCOREBOARD_URL` template every other
sport already uses, just a different path suffix.

**The shape is completely different from every other sport this module
parses** — confirmed live, not assumed. One `event` here is a whole
TOURNAMENT (e.g. "National Bank Open"), closer to `mma.py`'s "one event =
the whole CARD" precedent than to `_parse_event()`'s flat team-sport
shape. `groupings[]` adds one more nesting level MMA doesn't have — the
DRAW category (Men's Singles / Women's Singles / Women's Doubles) — and
each grouping's `competitions[]` are the individual matches.
`sports._parse_tennis_match()` follows `mma._parse_card()`'s structural
precedent, not `_parse_event()`.

**`linescores` entries are `{value, tiebreak (optional), winner}`** — NOT
the `{value, displayValue, period, winner}` shape an earlier speculative
note in this project once carried before anyone had pulled a live
payload; that note is now corrected. `value` is games won in that set (a
float, e.g. `7.0`), `tiebreak` is only present when that set went to a
breaker — and **both competitors carry their OWN tiebreak points**
(confirmed live: winner `tiebreak: 7`, loser `tiebreak: 5` on the same
set, matching ESPN's own real finished-match note text "7-6 (7-5)"
exactly), not a single shared value as an earlier draft of this note
assumed. `sports._parse_tennis_sets()` keeps both; `engines.py`'s
`_tennis_set_line()` combines both competitors' per-set lists into one
real "7-6(7-5) 3-6 6-1" string at the RENDER layer (a display decision,
not an I/O one).

**Doubles uses a different competitor shape than singles** — also
confirmed live, not assumed. Singles carries `athlete` (one player);
doubles carries `roster` (`roster.athletes[]`,
`roster.displayName`/`shortDisplayName` for the pair — confirmed against
a real doubles match, Brooks/Haverlag). `sports._tennis_competitor_name()`
handles both; an unrecognised shape degrades to an empty name, never a
guess.

**No live (in-progress) match was available to verify live in-game point
score fields against this session** — every real match checked was `pre`
or `post`. Built correctly against the real PRE/POST shapes actually
seen; an `in`-state match renders the real set-by-set score so far with
no live point score, because no field for one was ever observed to
confirm a name for — matching this project's "ship correct but honestly
unverified" pattern already used elsewhere (NHL goal detector, MMA
finish detector).

**Pinned player**, golf's exact precedent: `sports.load_tennis_player()`/
`save_tennis_player()` store a name in the same `sports_config.json`
(key `tennis_player`, preserves every other key on write);
`/api/sports/tennis_player` and a new control-panel card set it.
`sports.find_pinned_tennis_player()` is the same forgiving exact/surname/
substring match `find_pinned_golfer()` uses, preferring a LIVE match over
a finished/upcoming one when the same name resolves to more than one
real match. `SportsEngine.PANEL_TENNIS` follows `PANEL_GOLF`'s exact
shape in `_build_panels()`/`_frame_for_view()` — its own panel slot, not
sharing one, so it cannot be crowded out. `_frame_tennis_pinned()` leads
with the player's name, opponent, and real set score — "where is MY
player" applied to a head-to-head sport instead of a leaderboard.

**Tennis is DROPPED from the header-derived event list and REPLACED**
with the dedicated fetch inside `get_universal()` — keeping both would
either duplicate a match or show the header's useless empty-until-live
copy alongside the real one.

**`SportsEngine.SPORT_RENDERERS["tennis"]`** — two name rows, then the
combined set-by-set score on its OWN line at scale 1 (never scale 2 —
CLAUDE.md's own layout rule: tennis set scores are 126px at scale 2 on a
64px panel, this is the exact sport that rule was written about), the
draw (class_label), then real match state. Y-cursor throughout, not
fixed rows. `SportsEngine.SPORT_DETAIL_RENDERERS["tennis"]` adds full
names (`fit_person` on `full` instead of `abbr`), real venue/court, the
tournament name, the draw, and a finished match's real `notes[]` summary
line — the same "context the main view has no room for" pattern
soccer/golf/baseball's detail views already established.

**Tiebreak brackets** reuse the `(`/`)` glyphs already added to
`_FONT3x5` in an earlier session specifically for this — confirmed
present before use rather than re-added.

**A tennis header icon was added** (`engines.draw_icon_tennis`, a ball
outline with two offset dots for the seam, distinct from soccer's single
centred dot) — `SPORT_ICONS["tennis"]` was explicitly documented as
"skipped, no renderer to attach it to yet" before this session; now that
a real renderer exists, the icon is wired in too, closing that
previously-flagged gap in the same pass rather than leaving it stale.

**Verified**: `.venv/bin/python -c "import sports, engines"` clean.
`render_audit.py`/`render_audit.py sports` both clean (0 modes failed;
only pre-existing-class `TRUNCATED` warnings on long names, same
non-fatal category every other sport already has). `fold_audit.py` clean
(0 feeds not folding) — added a NEW permanent tennis coverage case
(`fold_audit.py`'s "sports tennis match" check, replaying a real WTA
payload with canaries injected through `_parse_tennis_match()`), per this
project's own standing rule that a fold belongs inside the boundary
function and needs its own audit case, not just a clean live check.
Fetched real live data directly: 610 real matches parsed cleanly across
both tours in one session (ATP + WTA combined), including a real
completed match (Nicolas Mejia d. Marco Trungelliti, National Bank Open,
7-6(7-5) 3-6 6-1) and a real doubles match (Brooks/Haverlag vs.
Falkowska/Smith, Warsaw Polish Open). Drove `SportsEngine` directly
against the warmed real feed: the main ticker renderer and the expanded-
detail renderer both produced real non-black frames off a real tennis
event. **Live-verified on the real physical panel**: restarted
`com.henderburgh.arcade`, pinned Nicolas Mejia via the real
`/api/sports/tennis_player` endpoint, confirmed `/api/sports/config`
resolved the real match (`vs M. TRUNGELLITI`, state `post`, "NATIONAL
BANK OPEN PRESENTED BY ROGERS"), switched to `sports` mode and pulled a
real `/api/frame` — a real non-black frame (878 lit px) with a distinct
header/name/VS-line/set-score/footer layout matching
`_frame_tennis_pinned()`'s design, confirmed by direct pixel-grid dump,
not just a byte count. Panel restored to `ambient` and the player
unpinned afterward.

**Honestly unverified**: live in-progress point-score fields (see above
— no live match existed this session to observe one), and the pinned
player/main-ticker/detail-renderer paths were confirmed on the real
panel or via direct engine-driving against warmed real data, but not
all three simultaneously on the physical panel in one pass (the ticker
and detail renderer were verified by driving `SportsEngine` directly,
matching the same "engine calls FEED, no panel needed" verification
method this project's own CLAUDE.md names as the standard one). Worth a
real spot-check of the ticker/detail views on the physical panel next
time a live match is in progress.

### Football (NFL/NCAAF) and basketball (NBA/NCAAB) — the last two MAIN/DETAIL rows (2026-08-08)

Closes both remaining rows of the per-sport MAIN-renderer table and adds
their EXPANDED-DETAIL renderers in the same pass, same order tennis just
established this session.

**Reused, not re-derived**: `sports._situation()`'s `down_distance` field
already existed (built defensively before this session, never verified
live) and `engines.situation_line(g)` already existed and was already
used by `GameDayEngine`'s NFL team view -- both wired directly into
`_render_football()`/`_render_football_detail()` rather than
reimplemented. `SPORT_ICONS["football"]`/`["basketball"]` and both
leagues' `LEAGUE_ICON` entries already pointed at real
`draw_icon_football`/`draw_icon_basketball` from an earlier icons-only
pass -- just threaded through `draw_header(..., icon=...)` the same way
soccer/golf/tennis's renderers already do. `_draw_scoreline()` (shared
two-row team block, already used by baseball) is reused as-is by both
new main renderers.

**A real data-provenance fact worth keeping**: `sports._situation()`'s
output is NOT carried on the universal header event dict
(`_header_event()`) at all -- confirmed by reading both functions, not
assumed. It only reaches `SportsEngine`'s events via the per-league-poll
join already documented in `tick()` (`sports.py`'s per-league
`_parse_event()` output, joined onto `self.universal` by event id) --
and that join only runs for the CONFIGURED leagues (`self.leagues`,
default NFL/NBA/MLB/NHL). So down-and-distance is real and will render
for NFL, but only when NFL is a configured league AND the header also
carries that same event id; an unconfigured NCAAF/NCAAB game gets no
situation enrichment at all, which is the correct degrade, not a bug.

**No possession indicator was built for football**, matching this
project's own explicit NHL power-play precedent right next to
`down_distance` in `_situation()`'s docstring ("guessing one would be
inventing the feature") -- no ESPN field for possession was ever
confirmed on a real payload this session.

**No bonus/foul-count/timeout display was built for basketball**, same
reasoning: no live NBA/NCAAB game's `situation` payload was ever
inspected to confirm a field name, and none of the confirmed fields on
`_header_event()`/`_situation()` cover it. What `_render_basketball()`
ACTUALLY adds over `_frame_universal_generic()`: the generic fallback
never shows a team's record at all (only detail/clock/class_label) and
sizes its score block for the worst case across every sport (tennis'
wide strings); the dedicated renderer adds an explicit record line and a
purpose-built quarter/clock line, using basketball's own numeric-score
layout. This is honestly closer to "the same layout with a
purpose-built header" than a from-scratch design, which is the correct,
non-forced outcome per this task's own instructions given no live
NBA/NCAAB game existed this session to justify more.

**Verified against real data, not synthetic**:
- `.venv/bin/python -c "import sports, engines"` clean.
- `render_audit.py sports` and `fold_audit.py` both clean before and
  after (0 modes failed, 0 feeds not folding; only the pre-existing
  tennis `TRUNCATED` warnings, same non-fatal class every other sport
  already has).
- **A live WNBA game existed this session** (`sports.HEADER_URL`
  carried it under `sport == "basketball"`, same universal shape NBA/
  NCAAB games would use): LV @ MIN, `state: "in"`, period 1, clock
  "1:47", LV 18 - MIN 22, real records 22-9 / 25-7. Drove
  `SportsEngine._render_basketball()`/`_render_basketball_detail()`
  directly against this real live event -- both produced real non-black
  frames (884px / 1143px lit) with no crash. A second real WNBA game
  (IND @ CHI, `state: "pre"`, scheduled 3:30 PM EDT) verified the
  scheduled-state path the same way (719px lit, non-black, no crash).
- **NFL and NCAAF had zero events on the header endpoint** this session
  (both aged off -- NFL's one game today was already `state: "post"`,
  NCAAF is preseason and apparently outside the header's near-term
  window) -- so there was no live header-shaped football event to pull.
  Verified instead against the REAL per-league scoreboard, which DOES
  have them: a real finished NFL game (CAR 33, ARI 30, `state: "post"`,
  real records 1-0/0-1) and a real scheduled NCAAF game (UNC vs TCU,
  `state: "pre"`). Reshaped `sports._parse_event()`'s real output into
  the universal event shape the renderers actually consume (field-name
  plumbing only -- every value is the real one ESPN returned, nothing
  invented) and drove `_render_football()`/`_render_football_detail()`
  directly against both: all four combinations rendered real non-black
  frames (787-982px lit) with no crash.
- **Live-panel check**: `com.henderburgh.arcade` was already running
  (mode `flights`) -- switched to `sports` via `POST /api/mode/sports`,
  confirmed `state == "sports"` with no `err`, pulled `/api/frame` five
  times and confirmed real non-black frames each time (922 lit px on one
  sample) with the engine running against the live warmed feed, then
  switched back to `flights` (the mode the panel was actually in before
  this check) and confirmed it returned healthy with no error. Landing
  the ticker specifically on a football or basketball game on the
  physical panel in this pass would have required exposing the two-axis
  browse controls over HTTP, which this project does not do (browse
  input is physical-cart-driven, not an API endpoint) -- so the direct
  `SportsEngine`-driving above is what actually exercises both new
  renderers against real data, matching the project's own stated
  standard verification method ("engine calls FEED, no panel needed"),
  same precedent tennis used for the same reason this session.

**Honestly unverified**: live in-game down-and-distance ACTUALLY
appearing on the physical panel (no NFL/NCAAF game was `state == "in"`
anywhere this session -- both real events checked were `post`/`pre`),
and a live basketball clock/quarter rendering on the physical panel
specifically for NBA/NCAAB (the live basketball verification above used
a real live WNBA game via direct engine-driving, not NBA/NCAAB, and not
on the physical panel). Built correctly against every real shape that
WAS available (finished NFL, scheduled NCAAF, live+scheduled WNBA under
the shared `basketball` sport key) -- worth a real spot-check the next
time an NFL/NCAAF game or an NBA/NCAAB game is actually live.

Commits: `_render_football`/`_render_football_detail` and
`SPORT_RENDERERS`/`SPORT_DETAIL_RENDERERS["football"]` in one commit,
`_render_basketball`/`_render_basketball_detail` and their registry
entries in a second, this write-up last -- see git log for hashes.

### Per-sport MAIN renderers (DONE — started 2026-08-01, closed 2026-08-08)

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
| football | **done (2026-08-08)** — quarter/clock, down-and-distance via `situation_line()` when live and present, records/broadcast footer. Closes this table. See the FOOTBALL/BASKETBALL section below |
| basketball | **done (2026-08-08)** — quarter/clock, records/broadcast footer; deliberately minimal (no bonus/foul/timeout display, no confirmed field for one). See the FOOTBALL/BASKETBALL section below |
| soccer | **done** — form strings, ESPN-formatted clock, penalty shootouts (verified live). Layout uses a y-cursor after the audit caught the divider/clock overlapping the second team |
| tennis | **done (2026-08-08)** — task #19, unblocked earlier the same session as football/basketball. Set-by-set score line, tiebreak brackets, pinned-player view. See the TENNIS section below |
| golf | **done** — 6-row leaderboard, movement arrows (sign verified: negative = climbed the leaderboard) |

**This table is now fully closed** — every sport this module tracks has its own MAIN renderer.

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

**NFL touchdown — done (2026-08-08), WIDENED TO NCAAF same day.** Same
shape as the MLB home-run detector: `sports._fetch_touchdown_plays(league,
event_id)` + `SportsEngine._detect_nfl_touchdown()` (registered as
`BIG_MOMENT_DETECTORS["nfl_touchdown"]` — the key and method name are kept
as-is for stability even though the scope now covers NCAAF too; grepped
the codebase before widening and confirmed the method/key are the only
places "nfl_touchdown" appears), own one-shot cursor
`self._seen_nfl_touchdowns`, scoped identically to `_detect_mlb_home_run`
(only fires for `favorite.league in ("NFL", "NCAAF")` and
`favorite_game.state == "in"`, reuses the same per-game summary endpoint,
no new request volume).

- **NCAAF widening confirmed live, not assumed**: NCAAF is the same
  "football" sport family on ESPN's site API — checked a real finished
  NCAAF game (event 401769072, Alabama @ Indiana, 2026 CFP semifinal)
  against `_fetch_touchdown_plays("NCAAF", "401769072")` directly: the
  identical top-level `scoringPlays` key, the identical `type.text`
  values ("Passing Touchdown", "Rushing Touchdown", "Field Goal Good"),
  and the same substring-match logic held unchanged — returned exactly 5
  real touchdowns, all correctly classified, with the game's real field
  goals correctly excluded. One function now serves both leagues rather
  than forking a near-duplicate.

- **Payload facts, confirmed live against a real finished game** (event
  401873271, Panthers @ Cardinals): NFL's summary endpoint carries scoring
  plays under a top-level `scoringPlays` array — NOT `plays` (MLB's key)
  and NOT `keyEvents` (soccer's key, empty for this NFL event); all three
  were checked on the real payload before picking `scoringPlays`. A
  touchdown is any entry whose `type.text` CONTAINS the substring
  "Touchdown" (e.g. "Rushing Touchdown", "Passing Touchdown"), not an
  exhaustive enum — real ESPN variants like "Return Touchdown" exist that
  weren't in this one sample, and a substring match covers them without
  needing updates. Field goals and extra points are excluded by the same
  check (their `type.text` is "Field Goal Good", no "Touchdown"
  substring).
- **Verified against real live data, not synthesized**:
  `_fetch_touchdown_plays("NFL", "401873271")` run directly against the
  real event returned exactly 7 real touchdowns, all correctly classified
  (Corey Kiner, AJ Dillon, Simi Fehoko, Ja'Seem Reed, Anthony Tyus III,
  Bryson Green, Haynes King), and the 5 real "Field Goal Good" scoring
  plays in the same payload were correctly excluded — checked against the
  raw `scoringPlays` list entry-by-entry, not just a count. One-shot
  cursor logic was then driven directly through `SportsEngine`
  (`_seen_nfl_touchdowns`): first tick adopts the 7 existing touchdowns as
  baseline with no fire, a simulated new touchdown (one id popped from
  the seen set) fires exactly once with the correct score line and real
  play text, and a second call after consumption returns no fire (no
  double-fire).
- This game was already `state: "post"` (finished) by the time this was
  built, so the parsing/dedupe/scope logic above is what's verified, not
  a genuinely in-progress favorite game firing the celebration live on
  panel — same honest caveat MLB's own writeup carries.
- Text: line1 = `f"{away_abbr} {away_score}, {home_abbr} {home_score}"`
  from `favorite_game`, same join as MLB. line2 = the real ESPN play text
  (folded). Color = home team color, falling back to away, falling back
  to `(255, 100, 40)` (warm orange, distinct from MLB's gold and NHL's
  blue below).

**NHL goal — built, UNVERIFIED against live data (2026-08-08).** Same
shape again: `sports._fetch_goal_plays("NHL", event_id)` +
`SportsEngine._detect_nhl_goal()` (registered as
`BIG_MOMENT_DETECTORS["nhl_goal"]`), own one-shot cursor
`self._seen_nhl_goals`, identical scoping to the other two detectors.

- **Real, current data gap, honestly documented, not worked around**: the
  NHL scoreboard was checked live at build time — all 7 events on it were
  `state == "pre"` (off-season), zero `in`, so no live NHL `scoringPlays`
  payload has been observed. Built on the SAME `scoringPlays` shape
  confirmed live for NFL (above) and structurally identical to MLB's
  `plays`, since ESPN's site API uses one unified summary-endpoint shape
  across these team sports — but the exact `type.text` string for a goal
  is an ASSUMPTION, not a confirmed fact: this checks for the literal
  string `"Goal"` (exact match, not substring), and if ESPN actually uses
  a more specific string for some goal types (e.g. a hypothetical
  "Penalty Shot Goal"), those would NOT currently fire and would need
  their own handling once a real payload is finally seen. Documented
  honestly as unconfirmed rather than guessed past, same precedent as
  the MMA finish detector shipping unverified (see "MMA finish" above).
- Text/color follow the same pattern: line1 = score line from
  `favorite_game`, line2 = the real ESPN play text (folded), color = home
  team color falling back to away falling back to `(40, 160, 255)` (cool
  blue, ice-appropriate, distinct from the other two fallbacks).
- `render_audit.py` and `fold_audit.py` both re-run clean (0
  failed/0 not-folding) after this change; `import sports, engines`
  clean. The first real live NHL goal the pinned-favorite feed ever
  surfaces is what proves this live, not a synthetic push.

**Basketball clutch shot (NBA/NCAAB) — done (2026-08-08).** The sixth
per-sport big-moment detector, and the first to cover basketball at all.
`sports._fetch_clutch_plays(league, event_id)` +
`SportsEngine._detect_basketball_clutch_shot()` (registered as
`BIG_MOMENT_DETECTORS["basketball_clutch"]`), own one-shot cursor
`self._seen_basketball_clutch`, scoped identically to every other
detector here (`favorite.league in ("NBA", "NCAAB")` and
`favorite_game.state == "in"`, reuses the same per-game summary endpoint,
no new request volume).

- **Real schema facts, confirmed live 2026-08-08 against a real
  live/finished WNBA game** (event 401857125, LV @ MIN — used ONLY as a
  real schema/logic reference this session; WNBA is NOT reintroduced to
  `LEAGUE_PATHS` and the detector's league gate excludes it, matching the
  owner's standing 2026-08-07 WNBA removal decision documented above):
  basketball's per-game summary endpoint has **no `scoringPlays` key at
  all** — the real key is `plays` (382 real entries in that game, a full
  play-by-play list, not just scoring plays), and a scoring entry carries
  `scoringPlay: true` (93 real scoring plays in that one game).
- **"Any scoring play" is deliberately NOT the trigger** — basketball
  scores far too often for that to mean anything (roughly one scoring
  play every 30 real seconds of game clock in the sample checked). The
  real, derivable signal built instead: a CLUTCH SHOT, a real scoring
  play that is (a) in the final period-or-later for that league's real
  convention, (b) inside a real clock threshold, and (c) a real tie or
  lead change computed from actual consecutive scores.
- **NBA quarters vs. NCAAB halves — confirmed live, not guessed.**
  `FINAL_PERIOD_BY_LEAGUE = {"NBA": 4, "NCAAB": 2}`. Checked against a
  real finished NCAAB game (event 401825568): every play's
  `period.number` topped out at 2, with `period.displayValue` reading
  "1st Half"/"2nd Half", never "Quarter" — a genuinely different real
  convention from the WNBA/NBA 4-quarter game also checked, not an
  assumption carried over from football/hockey's period counts.
  Overtime periods (5+ for NBA, 3+ for NCAAB) are covered for free by the
  same `>=` comparison, no separate branch needed.
- **`clock.displayValue` is NOT uniformly raw seconds, contrary to an
  earlier assumption going into this build — checked programmatically
  across all 382 real plays in the reference game, not assumed from one
  sample.** For most of a period it's the familiar `"M:SS"` form
  ("9:43", "10:00" — 336 of 382 real plays); only once the real clock
  drops under one minute remaining does ESPN switch to a bare
  decimal-seconds string with no colon ("43.5", "18.1", "0.0" — the
  other 46, every single one confirmed inside the final minute of its
  period). `sports._clock_seconds_remaining()` parses both forms
  correctly; a string matching neither returns `None` so the caller
  skips it rather than mis-parsing a play as clutch-eligible.
- **`CLUTCH_WINDOW_SECONDS = 120.0`** — the final 2 minutes of the final
  period/half. A stated judgment call, not a measured fact (there is no
  ESPN field that marks a moment "clutch"), reasoned the same way
  `flights.WINDOW_MAX_NM_DEFAULT` was reasoned about: this is the
  common broadcast/fan notion of "clutch time" (the point announcers
  start saying it out loud), short enough to stay rare against the ~90
  real scoring plays a full game produces.
- **The tie/lead-change test is a real before/after score comparison,
  never an invented confidence number** — walks backward through the
  same real `plays` list to the previous play carrying a score, compares
  that margin's sign against this play's resulting margin. Same "place
  comparison" category of derived-but-real signal `golfer_move()`'s
  LEAD/LOST LEAD detection already uses for golf.
- **Verified against real live data, not synthesized**: replaying the
  exact detection logic against the real reference game's real 93
  scoring plays found **0 qualifying clutch plays** — checked and
  confirmed this is the honestly correct answer, not an untested path:
  the real game was never within a tie/lead-change margin during the
  final 2 minutes of its 4th quarter (margin stayed 4+ points the whole
  window, confirmed by printing every real 4th-quarter scoring play's
  before/after score). A second real WNBA game checked the same way
  (Indiana @ Chicago) also correctly found 0 qualifying plays, same
  reason (never close late). A synthetic POSITIVE control (a fabricated
  late go-ahead three-pointer inserted into a realistic play sequence)
  confirmed the detector DOES fire correctly when a real qualifying
  shape is present — proving the 0-count above is a true negative, not a
  detector that can never fire. One-shot cursor logic (adopt-then-diff,
  no double-fire) was driven directly through `SportsEngine` with a
  mocked fetch, mirroring every other detector's own verification here.
- Text: line1 = the real score line (same
  `f"{away_abbr} {away_score}, {home_abbr} {home_score}"` shape every
  other detector uses). line2 = the real ESPN play text (folded). Color =
  home team color, falling back to away, falling back to `(160, 60, 220)`
  — a violet/purple chosen deliberately over an orange fallback (a
  basketball is itself orange, which would collide with NFL's own
  `(255, 100, 40)` fallback), reading as an arena-lights/NBA-branding
  hue and staying visually distinct from every other sport's fallback
  already in use (NFL orange, NHL blue, MLB gold).
- `sport="basketball"` threads through `_set_big_moment()` into
  `_backdrop_sports()` exactly like every other sport, reusing the
  existing `SPORT_ICONS["basketball"]` icon from the earlier per-sport-
  icons pass — no new icon plumbing needed.
- **Honestly unverified**: no live NBA/NCAAB game existed this session
  (both leagues showed only `pre` games on their real scoreboards) — the
  fetch/detection logic above is what's verified, against a real WNBA
  game used purely as a schema/logic reference, not an actual live-fire
  on the real pinned-favorite feed. Same "ship correct but honestly
  unverified" precedent as `_detect_nhl_goal()`/`_detect_mma_finish()`.
  `render_audit.py sports` and `fold_audit.py` both clean before and
  after; `import sports, engines` clean. The first real live NBA/NCAAB
  clutch shot the pinned-favorite feed ever surfaces is what proves this
  live, not a synthetic push.

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

### Select-to-expand gets its own transition (2026-08-08)

**Real gap, not a guess**: `SportsEngine.frame()`'s transition already
slides between PANELS (pinned team -> golfer -> events), but its `cur`
tracking key never included `self.detail` — entering or leaving a
game's expanded detail view was an instant cut, the one select-to-expand
action in the whole project with zero motion. Owner feedback: hitting
select to open a game should feel like "a spectacular, genius breakdown"
— eye-catching, not a snap.

**`transitions.iris_open()`** (new, `transitions.py`) — new content
reveals from the centre column outward, like a shutter opening. Same
cost class as the existing `wipe_right` (one slice pair per row, still
zero per-pixel work — the hard constraint every style in that module
respects): `k` grows from `WIDTH//2` outward instead of from one edge,
still pure byte slicing.

**Deliberately its own style, not a reuse of `push_up`** — opening ONE
specific game is a bigger, more deliberate moment than switching which
panel is showing, and CLAUDE.md's own working conventions already
establish that distinct actions deserve visually distinct treatment
(see the flights/satellite backdrop reasoning). `SportsEngine.frame()`
now tracks `cur = (self._panel(), self.panel_i, self.detail is not None)`
— entering detail (`False -> True`) fires `iris_open`; leaving
(`True -> False`) fires the existing `push_down` (a natural "closing"
counterpart, no new style needed for that direction); stepping between
games while ALREADY expanded (left/right browsing) does not change the
`in_detail` flag, so it stays instant, matching this project's
established "switching KIND of thing gets a transition, browsing within
one stays immediate" rule verbatim. `DETAIL_TRANSITION_TICKS = 14`
(~0.7s, longer than the 8-tick/~0.4s panel slide) — a bigger moment
gets a touch more time to land, not just a different shape.

**Verified**: driven directly through a real `SportsEngine` instance
(not synthetic frames) — confirmed `_trans_style` correctly resolves to
`iris_open` on entry and `push_down` on exit, confirmed browsing between
two different games while expanded does not restart or alter the
transition. `render_audit.py`/`fold_audit.py` both clean (0 modes
failed, 0 feeds not folding) before and after. **Live-verified on the
real panel**: restarted `com.henderburgh.arcade`, switched to `sports`,
pressed `rotate` through the real `/api/press` input path, pulled
`/api/frame` across 5 consecutive frames — lit-pixel count grew
monotonically (916 -> 989 -> 1077 -> 1162 -> 1162, settling once the
transition completed), confirming the band genuinely widens from centre
outward on real hardware, not just in a synthetic buffer diff. Panel
restored to `ambient` afterward.

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
| tennis | **done (2026-08-08)** — full names, real venue/court, real `notes[]` finished-match summary, the DRAW. See the TENNIS section below |
| football | **done (2026-08-08)** — full records, venue, broadcast, series/note, down-and-distance. See the FOOTBALL/BASKETBALL section below |
| basketball | **done (2026-08-08)** — full records, venue, broadcast, series/note; no bonus/foul/timeout row, same honest gap as the main renderer |
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

### Task #21 audit: "Cut redundant per-league polling" — CLOSED, no per-league-poll code change (2026-08-08)

Full audit of whether `SportsFeed`'s per-league scoreboard poll
(`_refresh_scoreboards()`, looping `LEAGUE_PATHS`) is redundant with the
universal header feed. **Conclusion: it is not redundant for any
currently-configured league, no cut was made to that loop, and this is a
legitimate "audited, confirmed not redundant" close** — but the audit
found and fixed a real, separate, unthrottled-polling bug in the
big-moment detectors (see below), which is the actual change that came
out of this pass.

**Dependency map — which per-league-only fields are genuinely only
available from the per-league scoreboard poll, checked against every
`LEAGUE_PATHS` entry and every renderer/detector that reads it:**

| field | only in per-league poll? | who reads it |
|---|---|---|
| `situation` (MLB balls/strikes count, football down/distance) | **yes** — confirmed absent from `get_universal()`'s header payload across a full slate, both this session and 2026-08-01 | baseball's live count row, football's down-and-distance row (`tick()`'s `by_id` join in `SportsEngine.tick()`, engines.py ~L8402) |
| win probability | **yes**, and only for NFL/NBA/MLB — NHL's and EPL's summary endpoint has no `winprobability` key at all, confirmed live both sessions | `SportsFeed._refresh_win_prob()` / `self.data["win_prob"]` |
| baserunners (`onFirst`/`onSecond`/`onThird`) | **no** — MLB's universal-header event carries these at the top level (`onFirst`/`outsText`), a DIFFERENT shape than the per-league `situation.onFirst`, confirmed in the "Sports coverage — UNIVERSAL" section above | baseball's diamond/outs renderer already reads the universal-header shape, not the per-league one, for this field |
| scores, records, venue, broadcast, series/note | **no** — the universal header carries all of it | every `SPORT_RENDERERS`/`SPORT_DETAIL_RENDERERS` entry |

Every currently-configured league (NFL, NBA, MLB, NHL — `DEFAULT_LEAGUES`
and the live `sports_config.json`) has at least one renderer/detector
that depends on `situation` or win-probability, so **no league in the
current config is safe to drop from the per-league loop.** `EPL`/`NCAAF`/
`NCAAB` are configurable-but-not-currently-configured; win-prob is
confirmed absent for EPL specifically (so EPL's per-league poll earns its
keep only for `situation`, still real), and NCAAF/NCAAB both have real
win-prob per 2026-08-01's own check, so none of the 7 `LEAGUE_PATHS`
entries is dead weight if ever enabled.

**Item 4 (tiered refresh tightness) — already as tight as proposed,
confirmed with real data, not code review alone.** The suggested
optimization ("skip polling a league the universal header already shows
as empty today") is functionally already what `_interval_for()` does:
measured live this session, NFL/NBA/NHL (all off-season, zero events)
had each already backed off to the 1800s EMPTY tier from their own prior
poll, while MLB (in season, 15 live/scheduled games) stayed at the 20s
LIVE tier. The only gap between that and a universal-header-driven skip
is the FIRST poll of a fresh process for an empty league (one call before
the existing backoff kicks in) — not worth adding a second inference
path (and a second place for the universal feed's different league-
keying to drift from `LEAGUE_PATHS`, see the "keyed by the older
`sports.LEAGUE_PATHS` codes" note above) to save one request every 30
minutes per idle league.

**The real finding: an unthrottled polling bug in the per-game big-moment
detectors, a different category from the per-league loop (per this
task's own scope note) but uncovered by the same "get a real request-
volume picture" step.** `SportsEngine.tick_rate = 0.05` (engines.py), and
`arcade_server._game_frame()` calls `eng.tick()` every time
`now - last_tick >= tick_rate` — i.e. up to 20x/second. `tick()` calls
`self._detect_big_moments()` unconditionally every tick, and 5 of the 6
registered `BIG_MOMENT_DETECTORS` (`mlb_hr`, `nfl_touchdown`, `nhl_goal`,
`basketball_clutch`, `soccer_goal`) called their real network fetch
(`sports._fetch_*_plays` / `fetch_new_soccer_goals`, all hitting
`SUMMARY_URL`) gated ONLY on "is the favorite's game state=='in'", not on
time. **A live favorite game was refetching its own game summary up to
20x/second**, not the "narrowly scoped per-game call" every one of these
functions' own docstrings claimed. (Golf's detector and MMA's finish
detector were checked too and do NOT have this bug: golf reads
`self.golf_move`, already computed by the throttled background feed
thread, and MMA's fetch only fires once per newly-`post` event id, not
every tick — no fix needed for either.)

**Fix**: `SportsEngine.__init__` gained `self._detector_last_poll = {}`
and a `_detector_due(key)` gate (engines.py, next to the `_seen_*`
baseline state), reusing `sports.WINPROB_REFRESH` (20s) as the cadence —
deliberately the SAME interval the background feed already uses to poll
the identical `SUMMARY_URL` for the identical game for win probability,
rather than inventing a new number. Each of the 5 detectors now checks
`self._detector_due(...)` immediately before its fetch call and returns
early if not due; the one-shot seen-set baseline logic is unchanged, so
a game already in progress still doesn't replay old plays. Soccer's
detector additionally had its own redundant double-fetch removed (it
called `fetch_new_soccer_goals()` twice back-to-back on the tick a new
game is adopted — once to seed the baseline, once immediately after to
check for "new" goals that had zero chance of existing yet since the
baseline was just set from the same data); it now seeds and returns.

**Measured, not estimated**: a synthetic burst of 20 rapid calls to
`_detect_mlb_home_run()` (simulating 1 second at `tick_rate`) triggered
**1** real fetch before the throttle existed it would have been 20; after
the fix it is confirmed 1 (test script, not shipped, run this session).
**Real before/after per-detector-second cost while a favorite's game is
live: before = up to 20 req/s per active detector (up to 100 req/s if
somehow all 5 were simultaneously eligible, though in practice at most 1-2
are for a given favorite/sport); after = 1 req per `WINPROB_REFRESH`
(20s) per active detector**, matching the cadence every docstring in
sports.py already claimed but the code didn't enforce.

**Verified before shipping**: `.venv/bin/python -c "import sports,
engines"` clean; `render_audit.py sports` — 0 modes failed (same
pre-existing tennis-name-truncation warnings as before, unrelated);
`fold_audit.py` — 0 feeds not folding. Real live data pulled this
session (2026-08-08): MLB `situation` (count) present on 10/15 real
games, win-probability path unchanged and untouched, football's
down-distance join in `tick()` unchanged and untouched — none of the
per-league-poll-dependent features were touched by this fix, only the
per-game detector fetch cadence.

**No code change to `_refresh_scoreboards()`/`LEAGUE_PATHS`/
`_interval_for()` itself** — confirmed correct and confirmed not
redundant, closing task #21 as audited-and-kept rather than forced into
an unwarranted cut.

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

### Favorite-teams ticker filter (2026-08-08)

A cross-sport watchlist for the universal ticker, deliberately SEPARATE
from the existing `favorite` field. `favorite` is ONE team with its own
full-screen PINNED view ("show me my team's game"); this is a list of
teams (any mix of leagues) that filters the ROTATING ticker down to only
games involving one of them ("is anything I actually care about on right
now"). Stored under its own `favorite_teams`/`favorite_teams_filter`
keys in `sports_config.json` — setting one was never supposed to require
or imply the other, and folding this into `favorite` would have forced
that coupling.

- `sports.load_favorite_teams()`/`save_favorite_teams()` — same
  preserve-other-keys write pattern every other `save_*` in this file
  already learned (the recurring `location_config.json`/`golf_player`
  lesson, one file deeper). `filter_enabled` is a separate bool from the
  team list itself, so toggling the filter off and back on doesn't
  require re-entering every team.
- `sports.event_matches_favorite_teams(ev, teams)` — matches against the
  SAME `abbr` field the ticker already displays (`_header_competitor`'s
  output), not a second name representation that could quietly drift
  from what's actually shown. Only checks `is_team` competitors — golf/
  tennis are individual-athlete sports with no "team" here, and both
  already have their own dedicated pinned-player mechanism, so both are
  explicitly EXEMPT from this filter rather than silently dropped by it.
- **Filtered once, at the source** — inside `SportsFeed.get_universal()`,
  before any downstream consumer (league grouping, the ticker index,
  `has_content()`) ever sees the event list. Same "filter at the source,
  not at every call site" reasoning the window filter's `in_window` flag
  already established for flights. This meant **zero changes to
  `engines.py`** — `SportsEngine` already just reads `self.universal`
  unchanged, so the filter is fully invisible to the render layer.
- `POST /api/sports/favorite_teams` (`{"teams": [{"league","team_abbr"}],
  "filter_enabled": bool}`), status folded into the existing
  `/api/sports/config` GET response (`favorite_teams`/
  `favorite_teams_filter` keys) rather than a new GET endpoint, same
  shape golf/tennis pinned status already uses.
- **Verified against real live data**: set a real favorite
  (`MLB`/`LAD`), confirmed 613 of 648 real live universal events survived
  the filter, every surviving non-golf/tennis event genuinely involved
  LAD (checked programmatically, not spot-checked), and all 612 real
  golf/tennis events stayed fully unfiltered as designed. Reverted the
  test favorite afterward — `sports_config.json`'s real baseline now
  carries the new keys at their honest empty/off defaults (`[]`/`false`),
  the same "new key, safe default" pattern every prior config addition
  this project has made. `render_audit.py`/`fold_audit.py` both clean;
  real panel restarted and confirmed healthy.

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
