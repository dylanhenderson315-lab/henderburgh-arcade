"""One list of modes. Menu, ambient, remote, and the web pad all read this.

ENGINES in engines.py is still the class registry -- this file is identity
and placement only. A name that is on the shelf, in WORLD, or on the
phone must appear here once. Takeovers stay listed so they cannot be
forgotten, but they are never menu or ambient slots.
"""
from __future__ import annotations


def _m(name, label, kind, color, *, menu=False, sequence=False,
       arcade=False, gallery=False, note=""):
    return {
        "name": name, "label": label, "kind": kind, "color": color,
        "menu": menu, "sequence": sequence, "arcade": arcade,
        "gallery": gallery, "note": note,
    }


# Shelf order matches the panel menu. Sequence order is WORLD's walk.
MODES = (
    _m("snake", "SNAKE", "game", (51, 255, 176), menu=True, arcade=True),
    _m("tetris", "TETRIS", "game", (0, 200, 255), menu=True, arcade=True),
    _m("pong", "PONG", "game", (143, 255, 199), menu=True, arcade=True),
    _m("breakout", "BREAKOUT", "game", (255, 90, 120), menu=True, arcade=True),
    _m("tron", "TRON", "game", (0, 255, 242), menu=True, arcade=True, gallery=True),
    _m("flappy", "FLAPPY", "game", (255, 226, 60), menu=True, arcade=True),
    _m("invaders", "INVADERS", "game", (255, 51, 200), menu=True, arcade=True),
    _m("dodge", "DODGE", "game", (255, 210, 60), menu=True, arcade=True),
    _m("2048", "2048", "game", (176, 96, 255), menu=True, arcade=True),
    _m("life", "LIFE", "game", (125, 255, 176), menu=True, arcade=True, gallery=True),
    _m("brawler", "BRAWLER", "game", (80, 235, 130), menu=True, arcade=True),
    _m("chase", "CHASE", "game", (255, 226, 60), menu=True, arcade=True),
    _m("tunnel", "TUNNEL", "game", (120, 110, 255), menu=True, arcade=True, gallery=True),
    _m("powder", "POWDER", "game", (230, 190, 90), menu=True, arcade=True, gallery=True),
    _m("ticker", "TICKER", "data", (60, 230, 110), menu=True, sequence=True,
       note="Left/right steps symbols. HOLD parks the tape."),
    _m("satellite", "ISS", "data", (255, 226, 60), menu=True, sequence=True,
       note="Left/right swaps pass list and live sky. HOLD parks."),
    _m("flights", "FLIGHTS", "data", (120, 200, 255), menu=True, sequence=True,
       note="Left/right walks aircraft. HOLD parks the rotation."),
    _m("followflight", "FOLLOW", "data", (255, 200, 90), menu=True, sequence=True,
       note="ICAO callsign only (UAL123, not UA123). Ambient only when airborne."),
    _m("departures", "BOARD", "data", (120, 200, 255), menu=True, sequence=True,
       note="Live to/from home. Not a scheduled board. Left/right pages."),
    _m("nowplaying", "MUSIC", "data", (255, 90, 200), menu=True, sequence=True,
       note="Bass mass + air. Left/right walks palettes. Last.fm names the track."),
    _m("ownernote", "TODAY", "data", (255, 214, 110), menu=True, sequence=True,
       note="Today's work checklist. One task per line, x to check. Edit on this phone."),
    _m("sports", "SPORTS", "data", (255, 140, 40), menu=True, sequence=True,
       note="Every live game, in DETAIL. Left/right walks the slate."),
    _m("news", "NEWS", "data", (255, 226, 60), menu=True, sequence=True,
       note="Left/right walks headlines. HOLD parks."),
    _m("weather", "WEATHER", "data", (90, 190, 255), menu=True, sequence=True,
       note="Up/down: conditions, hourly, radar. Ambient: hourly, or radar if storms."),
    _m("home", "HOME", "data", (255, 186, 90), menu=True, sequence=True,
       note="House radar. What's on. Rotate for house events."),
    _m("clock", "CLOCK", "data", (120, 200, 255), menu=True, sequence=True,
       note="FACE flips analog and digital."),
    _m("blog", "BLOG", "data", (176, 96, 255), menu=True, sequence=True,
       note="Left/right walks posts. HOLD parks."),
    _m("events", "EVENTS", "data", (255, 190, 90), menu=True, sequence=True,
       note="Left/right pages the log."),
    _m("ambient", "AMBIENT", "director", (176, 96, 255), menu=True,
       note="Up/down channel. Left/right steps. HOLD pauses."),
    _m("gameday", "GAME DAY", "takeover", (235, 45, 65), menu=True,
       note="UFC card takeover. Power or MENU leaves it."),
    _m("planewatch", "PLANE IN WINDOW", "takeover", (120, 200, 255)),
    _m("notify", "NOTIFY", "takeover", (176, 96, 255)),
    _m("menu", "MENU", "internal", (150, 160, 185)),
    _m("boot", "BOOT", "internal", (255, 226, 60)),
)

_BY_NAME = {m["name"]: m for m in MODES}


def get(name):
    return _BY_NAME.get(name)


def names():
    return tuple(m["name"] for m in MODES)


def menu_tiles():
    """(name, label, rgb) in shelf order."""
    return [(m["name"], m["label"], m["color"]) for m in MODES if m["menu"]]


# WORLD walk — sky first, not menu-shelf order.
SEQUENCE = (
    "flights", "satellite", "weather", "home", "sports", "nowplaying",
    "followflight", "departures", "ticker", "news", "events",
    "blog", "ownernote", "clock",
)


def sequence():
    return SEQUENCE


# ARCADE channel — gallery faces first, then the rest.
ARCADE = (
    "life", "tunnel", "tron", "powder", "snake", "pong", "breakout",
    "chase", "flappy", "invaders", "dodge", "tetris", "2048", "brawler",
)
GALLERY = ("life", "tunnel", "tron", "powder")


def arcade():
    return ARCADE


def gallery():
    return GALLERY


def mixable():
    seen = []
    for n in sequence() + gallery():
        if n not in seen:
            seen.append(n)
    return tuple(seen)


def labels():
    return {m["name"]: m["label"] for m in MODES}


def missing_from(engine_names):
    """Catalog names that have no engine class. Empty if honest."""
    have = set(engine_names)
    return [n for n in names() if n not in have]


def public_json():
    out = []
    for m in MODES:
        if m["kind"] == "internal":
            continue
        r, g, b = m["color"]
        out.append({
            "name": m["name"],
            "label": m["label"],
            "kind": m["kind"],
            "menu": m["menu"],
            "sequence": m["sequence"],
            "arcade": m["arcade"],
            "gallery": m["gallery"],
            "color": "#%02x%02x%02x" % (r, g, b),
            "note": m["note"],
        })
    return {
        "modes": out,
        "sequence": list(sequence()),
        "arcade": list(arcade()),
        "gallery": list(gallery()),
        "mixable": list(mixable()),
        "menu": [t[0] for t in menu_tiles()],
        "labels": labels(),
    }
