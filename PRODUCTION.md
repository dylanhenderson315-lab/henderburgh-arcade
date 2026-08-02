# PRODUCTION.md — sellable-device plan

## IMPORTANT — how this document was reconstructed

I (Claude) do not have access to any conversation history beyond the
current session. This file was referenced by name in several code
docstrings (`market.py`, `satellite.py`, `flights.py`, `engines.py`) as
if it already existed and already contained a real plan — it did not
actually exist anywhere in the repo or its git history until now. That
means whatever pricing, BOM figures, competitor comparisons, and
compliance decisions were discussed when those docstrings were written
are **not available to me**, and I have deliberately not invented
plausible-sounding numbers to fill the gaps — that would violate this
project's own core rule ("never invent a number") applied to its own
planning doc.

What follows is split clearly into two kinds of content:
- **Evidenced** — a real decision I can point to a specific line of code
  for, because the code only makes sense if that decision was already
  made.
- **⚠ UNKNOWN — needs your input** — something a real production plan
  needs (pricing, BOM cost, competitor names, compliance) that I have no
  source for at all. I have not filled these in with guesses.

Please treat this as a skeleton to correct and fill in, not a finished
plan.

---

## Product concept (evidenced)

A single sellable device: the same arcade software this repo builds,
running standalone on dedicated hardware instead of a Mac + a separate
WLED-MM panel. `display.py` documents this explicitly as the intended
end state — "later, on the production device (Raspberry Pi driving a
HUB75 panel through an Adafruit Bonnet), with only the renderer swapped."
Every mode (`engines.py`) is already hardware-agnostic pure Python for
exactly this reason: the same `frame()` output drives WLED-over-DDP today
and will drive the Bonnet-over-GPIO renderer later, unchanged.

**Bonnet renderer is not built yet** — `display.BonnetRenderer` is a
deliberate `NotImplementedError` stub, not a partial implementation.
There is currently no Pi hardware in the loop being tested against. This
is real, current status, not a historical note: **the software is
further along than the hardware path.**

## Single SKU, config-driven personalization (evidenced)

Every owner-specific setting — market watchlist, home location (for
ISS/flights), sports leagues and favorite team — is config-driven via a
JSON file + API endpoints, explicitly so "a single Mac install shouldn't
need a code edit" (`market.py`) and because "the production device needs
per-owner watchlists" / "a real owner location" (`market.py`,
`satellite.py`). This only makes sense as a design decision if the plan
is **one product, personalized per-owner through a setup flow**, not
per-customer custom builds. `market.load_config()`'s comment about "the
production device's setup flow" implies a first-boot configuration
experience is planned, though its actual shape (app? web wizard? QR
code?) is not documented anywhere I have access to.

⚠ **UNKNOWN — needs your input:** is this genuinely locked to one SKU, or
were multiple SKUs (e.g. different panel sizes as distinct purchasable
products, vs. one product with a size *option*) ever discussed?

## Panel size as the scalability axis (evidenced)

`display.BonnetRenderer`'s docstring is explicit that chain/parallel
HUB75 tiling — i.e., driving multiple physical panels as one larger
logical display — belongs entirely inside the renderer, "invisible to
every mode above it." Every mode already targets a flat 64×64 canvas but
is written to degrade gracefully at smaller sizes — `TickerEngine`'s own
comment: "On a 32x32 production panel the spotlight alone still works...
smaller panels show less at once, never fewer features." `sports.py`'s
`SportsEngine` docstring extends the same principle to a hypothetical
*larger* array: "one thing at a time on a single panel, more
simultaneously if this project ever grows to more panels."

Read together, this is real evidence of an intended **panel-size product
axis**: the same software scales down to a small single panel (fewer
pixels, same feature set, one thing at a time) and up to a tiled
multi-panel array (room for pinned content *and* a simultaneous ticker,
etc.) without any mode-level code changes — only the renderer and
`WIDTH`/`HEIGHT` change. This reads like the natural "real upsell" lever
for a product line (bigger display, same features, higher price), but I
have no evidence of it ever being stated as the actual pricing/upsell
strategy — that framing is mine, inferred from the architecture, not
something I can attribute to a past conversation.

⚠ **UNKNOWN — needs your input:** was "panel size is the upsell, feature
set stays constant" ever actually said explicitly, or is this purely an
architectural affordance that hasn't been turned into a pricing decision
yet?

## Cost discipline (evidenced, partial)

One concrete, real pricing-adjacent decision is in `flights.py`:
FlightAware's AeroAPI was explicitly rejected in favor of the free,
keyless adsb.lol + adsbdb combination, with the reasoning spelled out —
AeroAPI "has a real $100/month minimum past its free tier and doesn't
scale per-unit-sold for the production device." This is real evidence of
a standing design principle: **recurring per-unit API costs are treated
as a hard constraint against shipping a feature**, not just a nice-to-have
to minimize. Every other data source in this codebase (CoinGecko, Yahoo
Finance chart endpoint, wheretheiss.at, polluxlabs, ESPN's site API) is
similarly free and keyless — consistent with, but not proof of, the same
policy being applied deliberately every time, rather than these
particular APIs just happening to be free.

### ⚠ Free-API dependency risk at unit scale (real, quantified, unresolved)

"Free and keyless" is not the same as "free of risk at scale", and the
sports feature is the concrete case. Per the polling analysis in
`CLAUDE.md`, a single unit left running the `ambient` rotation 24/7
issues roughly **17,000 ESPN requests/day** (~30,000 with all 7 leagues
enabled) to an **undocumented, unofficial API with no published rate
limit, no terms of use covering this, and no support channel**.

That is per unit. At even a modest realistic volume, the aggregate is
substantial and originates from many residential IPs running identical
request patterns against an endpoint ESPN never published for this.
Three distinct exposures, none currently mitigated:

1. **Technical** — ESPN may throttle or block; the failure mode is
   sports silently dropping out of the rotation.
2. **Terms/legal** — using an undocumented internal endpoint commercially
   is a different posture from doing so personally. Worth a real answer
   before shipping, alongside the FCC/GPL items below.
3. **Support cost** — if ESPN changes or closes the endpoint, every unit
   in the field loses the feature simultaneously, with no vendor
   relationship through which to see it coming.

Cheap mitigations exist (longer refresh, skipping off-season leagues —
both detailed in `CLAUDE.md`) and neither is implemented yet. This does
not change the "no recurring per-unit cost" advantage, which still holds;
it means the *reliability* of that advantage is unverified for the one
data source that is scraped rather than offered.

⚠ **UNKNOWN — needs your input, all of the following are unfilled:**
- Target retail price (any figure at all)
- Actual BOM: which Pi model, which Bonnet, which HUB75 panel size(s)
  and $/panel, PSU, enclosure — nothing costed anywhere in this repo
- Margin target / manufacturing plan (one-off assembly vs. contract
  manufacturing)
- Any named competitor products this was meant to be positioned against
- Compliance requirements (FCC Part 15 for a Pi+radio device sold in the
  US, UL/ETL for the power supply, CE if EU sales are in scope, CPSC/CE
  toy-safety-adjacent rules if it's ever marketed at kids given the game
  library) — **none of this is addressed anywhere in the current code or
  session history I have access to, and it is a real pre-sale blocker,
  not a nice-to-have.**

## Features explicitly EXCLUDED from the sellable device (not oversights)

**ATC transcription log** (`atc.py`/`atc_transcribe.py`, added 2026-08-02)
transcribes LiveATC.net's public MYR audio stream locally with Whisper for
a personal-rig-only radar-scope feature. This is deliberately **not**
part of the sellable-device scope, and should not be added to it without
first resolving the constraint below — this is a note that a decision
was already made, not a TODO to build it in.

Reasoning: LiveATC.net's own current terms (confirmed live, 2026-08-02)
state plainly **"Audio streams may not be used in any third-party
products."** Transcribing to text rather than replaying the audio doesn't
obviously escape that clause — it is still a product feature derived from
their stream. For one owner's personal Mac mini this is a low-risk,
non-commercial personal use; for a manufactured unit sold to other
people, that unit unambiguously *is* a third-party product, and shipping
this feature on it would be a direct violation of a real, current, named
term of service — not a hypothetical or an abstract IP concern the way
the flight tracker's plane icon (deliberately not a real airline logo) is.
Revisit only if either (a) the home location moves somewhere LiveATC
coverage doesn't reach, making the question moot, or (b) a licensing
conversation with LiveATC.net actually happens — guessing past this
clause is not an option.

## What IS concretely true about the hardware path today

- **Dev/prototype rig (in use right now):** Mac mini running
  `arcade_server.py`, streaming over WLED's DDP protocol (UDP 4048) to a
  third-party Apollo M-1 panel (WLED-MM firmware on ESP32-S3, 64×64
  HUB75, with a Rev 6 I2S digital mic add-on). This is explicitly *not*
  the production hardware — it's the fastest path to a real display to
  build and verify software against.
- **Planned production rig:** Raspberry Pi driving a HUB75 panel directly
  through an Adafruit RGB Matrix Bonnet, using hzeller's
  `rpi-rgb-led-matrix` Python bindings (named specifically in
  `BonnetRenderer`'s docstring as the intended library). No WLED, no ESP32,
  no third-party firmware in the production path — the Pi owns the panel
  directly.
- **Not yet decided/built:** how (or whether) the production device gets
  its own microphone for audio-reactive visuals — the current audio-sync
  work (see `CLAUDE.md`'s Known Issues) is built against the Apollo M-1's
  WLED-MM mic specifically, over a protocol (WLED's UDP Sound Sync) that
  is a WLED-MM feature, not something the Bonnet-direct production path
  would have by default. This is a real open question the current
  architecture doesn't answer, not an oversight in this doc.

## Suggested next step

This file needs a real pass from you filling in the ⚠ UNKNOWN sections —
particularly compliance, since that can gate a launch timeline more than
almost anything else here. I'd rather hand this back honestly incomplete
than backfill it with numbers that sound plausible but aren't real.
