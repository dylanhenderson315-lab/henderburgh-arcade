"""
paneltext.py -- the ONE place external text is made safe to draw.

THIS MODULE EXISTS BECAUSE THE SAME BUG HAS SHIPPED NINE TIMES.

`engines._FONT3x5` covers A-Z, 0-9, space and !$%&'()+,-./:<>?@ -- and
`draw_text3x5` SILENTLY SKIPS anything else. It does not raise and it does
not substitute; the character is simply absent. So a mixed-case or
accented string from a live API renders as a *different, plausible-looking
string*, with nothing in the output to hint that anything was lost. It is
invisible to a code read and easy to miss on the panel.

The running tally, all real, all caught by rendering rather than review:
    1. ticker    -- lowercase symbols vanished
    2. flights   -- "United Airlines" drew as "U A"
    3. sports    -- mixed-case status text
    4. blog      -- free-form visitor messages
    5. sports    -- "@" dropped from the tape, losing home/away
    6. sports    -- "&" dropped from NFL down-and-distance ("3RD & 7")
    7. MMA       -- accented fighter names (Medic, Rebecki, Milosevic)
    8. tennis    -- "(" and ")" dropped, so "7-6(7-5)" became "7-67-5"
    9. golf      -- "Rasmus Hojgaard" drew as "HJGAARD" while he led a
                    live PGA tournament

Number 9 is the reason this file exists rather than another local fix.
mma.py had a correct fold, but it was a *private* one, so sports.py's
universal feed -- written later -- did a plain `.upper()` and reintroduced
the identical bug on a different feed. A per-module fold is not a fix; it
is a fix waiting to be missed by the next module.

**Every feed module must pass externally-sourced display strings through
`panel_text()` at its I/O boundary.** Not in the engine: the engine should
be able to trust that anything reaching it is drawable.
"""
import re
import unicodedata

# Characters that do NOT decompose under NFKD -- a stroke through a letter
# is part of the glyph, not a combining mark, so normalisation alone leaves
# them intact and they get dropped.
_TRANSLIT = {
    "Ø": "O", "ø": "O",      # Hojgaard -- a real live-tournament leader
    "Ł": "L", "ł": "L",      # Blachowicz, Rebecki
    "Đ": "D", "đ": "D",
    "Ħ": "H", "ħ": "H",
    "Æ": "AE", "æ": "AE",
    "Œ": "OE", "œ": "OE",
    "ß": "SS",
    "Þ": "TH", "þ": "TH",
    "Ð": "D", "ð": "D",
    "Ĳ": "IJ", "ĳ": "IJ",
    "Ŀ": "L", "ŀ": "L",
    "’": "'", "‘": "'", "‛": "'",
    "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-", "‐": "-",
    "×": "X", "·": ".", "…": "...",
    "№": "NO", "™": "", "®": "", "©": "",
}

# Must stay in sync with engines._FONT3x5. Verified by a test in
# tools/ and by the render sweeps; a character outside this set is
# replaced rather than dropped.
ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 !$%&'()+,-./:<>?@")


def panel_text(s):
    """Fold any external string into something the 3x5 font can draw.

    Order matters:
      1. explicit map  -- these characters do not decompose
      2. NFKD + strip combining marks -- handles the accented Latin range
      3. uppercase     -- the font has no lowercase at all
      4. hard filter   -- ANY remaining unsupported character becomes a
         space rather than vanishing

    Step 4 is deliberate. A space preserves the shape of the text and is
    visible as "something was here"; silent removal is the exact failure
    this function exists to prevent.
    """
    if s is None:
        return ""
    s = str(s)
    for bad, good in _TRANSLIT.items():
        s = s.replace(bad, good)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.upper()
    s = "".join(c if c in ALLOWED else " " for c in s)
    return re.sub(r"[ \t]+", " ", s).strip()


def undrawable(s):
    """Characters in `s` the font cannot draw. For tests and audits --
    the whole point is that this returns empty for anything that has been
    through panel_text()."""
    return sorted({c for c in str(s or "") if c not in ALLOWED})
