# SPORTS_ROADMAP.md — make the sports ticker #1 in market

Owner mandate (2026-09-21, reaffirmed 2026-09-24): the sports ticker
has to be the best on the market, absolutely stunning, and keep
innovating. This is that roadmap.

Same rules that govern the rest of the project apply here:
- **Never invent a number.** Every field on screen must trace to a real
  ESPN payload, real math over real values, or an honest gap.
- **Real render before "done".** Every layout change gets a pixel dump
  against real live game data at both full brightness AND night-dim
  (18%) before it ships.
- **Real live restart.** A source fix isn't done until the launchd
  service picked it up and `/api/frame` shows the change on real stats.

---

## What's built as of 2026-09-24

**Legibility**
- Hero-scale quarter+clock in the empty band above the field strip
  (was scale 1, tiny, buried; now scale 2, the second-most-visible
  fact after the score).
- Ordinal period label (`4TH 6:44`) — the 3x5 Q glyph has a
  below-baseline tail that reads as lowercase `q4` at scale 2;
  ordinals are what broadcasts print and what fans say.
- Real filled 2x2 timeout pips in each team's ESPN color (was
  `"T..." + "..."` rendered as literal 3x5 text, read as glitch).
- Fixed the `N` glyph that had shipped shared top rows with `K` for
  weeks — NYG was rendering as KYG, NHL as KHL, "NOT STARTED" as
  "KOT STARTED".

**Live drama**
- Sustained scoring-play pivot: when ESPN's own `situation.last_play`
  contains TOUCHDOWN/FIELD GOAL/GOAL/HOME RUN/SAFETY/PAT, the hero
  slot swaps the clock for a pulsing gold `TD` / `FG` / `GOAL` / `HR`
  / `SAF` code. Reverts naturally as soon as ESPN swaps last_play to
  the next play — no timer, no fabricated hold.
- Heat glow behind the hero clock during real high-leverage state
  (`is_redzone: True`, or Q2/Q4 under 2:00). Never fires in tandem
  with a score pivot — the pivot's louder.
- Per-competitor score-change pulse: a bright 2px strobe on the right
  edge of the team whose score just changed. Bounded per-event pulse
  map, pruned to live events, first-seen never fires.

**Coverage (already shipping)**
- Per-sport MAIN renderers: baseball, football, basketball, hockey,
  soccer, tennis, MMA (all six team sports have dedicated renderers;
  golf uses a leaderboard renderer, racing uses generic + real caution
  pips + real F1 podium ladder).
- Per-sport DETAIL renderers for all of the above.
- Big-moment detectors (fires the shared `draw_celebration` graphic
  for the pinned favorite): MLB HR, NFL TD, NHL goal, NBA/NCAAB
  clutch, soccer goal, golf lead/eagle/birdie, MMA finish.
- Real hero backdrops: baseball diamond, football turf, basketball
  hardwood, hockey rink, soccer pitch, tennis court, MMA octagon,
  golf/racing use dedicated leaderboard layouts.

---

## Roadmap by tier — where "stunning" comes from next

### TIER 1 — real live drama (highest visual payoff, real data, buildable now)

- **Score-change hero flash** — when a score changes, the whole score
  cutout box strobes bright for ~0.7s (bigger, more obvious than the
  current 2px edge strobe). Real, honest, unmissable.
- **Winning-team crown or bar-brightness tier** — the leading team's
  bar renders 15% brighter than the losing team's. Reads across the
  room without reading numbers.
- **Momentum indicator** — real, honest: the last 3 real scoring
  plays' team attribution → tiny colored pips somewhere at the bottom.
  When a team scores 3 in a row, the last 3 pips are all their color,
  and you can see the run at a glance.
- **Overtime treatment** — currently periods 5+ fall through to `OT`
  label. Real high-drama moments deserve their own hero treatment: OT
  clock in a distinct color, or an "OT" backdrop wash.
- **Two-minute countdown** — during the real final 2:00 of Q4, the
  clock ticks visibly (change every second) with a subtle color
  intensification as time drops.

### TIER 2 — real sport-specific polish

- **NFL possession arrow** — ESPN `situation.possession` is real (an
  event id or an abbreviation); draw a small triangle next to the
  scoring bar of whichever team currently has the ball. Never
  guessed.
- **NFL field-position ball marker with real drive direction** —
  currently the field strip shows a yard marker; add an arrow head
  indicating which endzone the offense is driving toward. Real
  derived from possession + yard_line.
- **NFL 1st down live line** — the yellow "1st down" line every
  broadcast has. Real math from `situation.yard_line +
  situation.distance` when both are real. Only drawn when both are
  present.
- **NBA shot clock** — ESPN's per-game `situation.shotClock` when
  present; draw as a tiny pip row that counts down.
- **NHL empty-net indicator** — real ESPN field, honest presence:
  when true, a small "EN" chip next to the leading team's bar.
- **Baseball pitcher/batter live matchup** — already exists as a
  fetch (`_refresh_matchup`), but only shown on DETAIL. Consider a
  scale-1 mini row on MAIN for pinned favorite games.
- **Soccer stoppage-time badge** — when clock is `90'+X'` render `+X`
  in a bright chip; real derived from the real clock string.

### TIER 3 — real feature additions (bigger scope, needs a plan)

- **NHL goal horn integration** (per [[henderburgh-deferred-polish-ideas]]):
  when the NHL goal detector fires and a real audio-out path is
  available on the Mac mini, play a real horn sound. Open questions:
  which team's horn (generic vs. team-specific), audio-out routing,
  volume limits. NOT built without a real playback path confirmed.
- **Real per-game "storyline" line** — one sentence generated ONLY
  from real facts on the payload (leading scorer, most yards, etc.).
  Requires plumbing more from `boxscore.teams[].statistics[]` and
  `boxscore.players[].statistics[]`. Real risk: overpromising a
  storyline with a slack field. Only ship when the sentence is
  purely reference-data + real numbers, never narrative.
- **Real live win-probability curve** — the current `_fetch_win_prob`
  returns a single float; the summary endpoint's `winprobability`
  array has a full per-play series. Sparkline treatment. Real data,
  real math.
- **Pinned-favorite always-on badge** — when the pinned favorite is
  live, ANY ticker event that isn't that game shows a tiny pip in a
  corner reminding the owner "your team is playing right now." Real
  gate: `favorite_game.state == "in"` AND current ticker event is
  not the favorite event.
- **Ambient sports "channel"** — a new director mode
  (see `ambient.py`) that prioritizes sports and keeps a live
  favorite game recalled to the front more aggressively. Sticky
  recall already exists (`STICKY_RECALL_TICKS`); the missing piece
  is a channel-specific weight boost.
- **Real replay tape** — when a big-moment celebration fires, hold
  the score frame for ~3s to "sit with" the moment before returning
  to normal ticker rhythm. Currently the celebration is fully-
  covering and doesn't preserve context.

### TIER 4 — 2nd-panel unlock (needs the second panel the owner is planning)

The owner mentioned needing another (bigger) panel to keep upgrading.
The software is already resolution-aware (see PRODUCTION.md's panel
scalability section). Real features that unlock at more pixels:

- **Two-game split** — pinned favorite on left half, ticker on right.
  A wider panel gives room for both without either being cramped.
- **Real bigger scores** — a wider panel means the score font can go
  scale 3 without collision. Currently the 64px width caps it.
- **Real per-team score history line** — one row per team with a
  compact drive-by-drive strip. Only fits at ≥96px wide.
- **Multi-league dashboard** — one row per league (NFL / MLB / NHL /
  soccer) with a live game count and highlight. Fits a tall panel.
- **Real leader board carousel** — sports with a real leader (golf,
  tennis, racing) get their own permanent slot instead of sharing
  the events panel. Fits on a wider display.

### TIER 5 — dreams (unblocked by conversations we haven't had yet)

- **Real fantasy integration** — with a real Yahoo/ESPN fantasy
  API key, the pinned favorite becomes "your fantasy lineup" and
  the panel shows real live projected points. Never invented, all
  real. Requires the owner to authorize a real fantasy platform.
- **Real bet-slip tracker** — real DraftKings/FanDuel bet API
  integration for the owner's real active bets. Same "real only"
  rule. Requires real API access.
- **Real "watch party" mode** — when 2+ favorite teams play at
  once, a takeover mode that shows both games side-by-side (needs
  a wider panel, see TIER 4).

---

## Honest gaps to close before adding more features

1. **Live NFL panel verification** — the panel was unplugged for
   weeks. Every fix from 2026-09-21 forward has been render-tested
   against real ESPN data via `/api/frame`, not eyes-on the real
   physical panel. Owner needs to plug it in and confirm the hero
   clock reads across the room, the timeout pips read as pips, the
   score strobe is visible not distracting.
2. **NBA/NCAAB live-game rendering** — never seen a real live NBA
   game rendered on the panel (both leagues were off-season for
   most build sessions). NBA season starts October; verify hero
   treatments then.
3. **Soccer live verification** — MLS/NWSL live tested but not
   EPL/LaLiga which have different clock formats (`90'+4'`).
4. **Golf real leaderboard eyes-on** — golf detail view is untested
   against a real major-championship weekend with 25+ real players
   on the leaderboard.

---

## The verification path, standardized

Every sports change from now on:

1. `sports.FEED.get_universal()` on a real live game, real slate.
2. Render MAIN and DETAIL via `eng._frame_for_view()` (NOT
   `eng.frame()` — that hits the 14-tick iris transition and can
   look identical for the first ~10 frames).
3. Save side-by-side PNG at 5-8x zoom.
4. Save a copy dimmed to `night_brightness` (0.18) — the panel
   spends most viewed hours there.
5. Run `render_audit.py sports` + `fold_audit.py` — must be clean.
6. Restart the live service (`launchctl kickstart -k
   gui/$(id -u)/com.henderburgh.arcade`), confirm `err: null` AND
   `stats.sent` incrementing AND `stats.loop_errors` flat.
7. Pull `/api/frame` in sports mode, confirm real non-black frame.
8. `git checkout -- ownernote_config.json` before committing.

---

## Standing anti-patterns (things "innovation" would break)

- **Don't invent a possession/momentum/anything from side-effects.**
  A number computed from other numbers on the payload is derived,
  not invented. A guess based on "usually" is invented.
- **Don't shrink the hero clock to fit a badge.** The clock is now
  the second-most-important fact by design. If a feature wants space,
  it takes it from a lower-priority row.
- **Don't add per-play polling to the ticker.** The per-game
  detectors are throttled at `WINPROB_REFRESH` (20s) for a real
  reason — see the 2026-08-08 polling audit.
- **Don't paint over real team colors.** Skins re-tint chrome, never
  data. Team colors are real data.
- **Don't add features that only work with a paid API** — the whole
  "no recurring per-unit cost" discipline (see PRODUCTION.md) rules
  out AeroAPI, TomTom, and the paid odds APIs. Same rule applies
  to any future sports feature: free-and-real, or don't ship.
