"""
hangar.py -- "THE HANGAR": a persistent collection of every distinct real
aircraft (by tail number/registration) this device has ever seen on ADS-B,
accumulating over time as a personal record rather than only showing
live/ambient traffic. Same shape as every other feed's log-store in this
project: a plain module owns the I/O (here, a local JSON-lines file
instead of a network call), a lock-protected in-memory cache, get() never
blocks and never invents.

WRITER: flights.FlightFeed's own background poll thread calls
record_sighting() once per real aircraft per refresh cycle -- no new
network call, no new poll cadence, this is pure composition of data
flights.py already fetches every POSITION_REFRESH (15s). times_seen
counts distinct VISITS (a gap of VISIT_GAP_S since last_seen), not
poll ticks -- the same tail lingering in RADIUS_NM for ten minutes
is one visit, not 40. A write to disk still happens when last_seen
moves or a new tail arrives.

IDENTITY -- keyed by REGISTRATION, not the ICAO24 hex. Confirmed live
against a real 238-aircraft sample near ORD (2026-08-03): the ADS-B "r"
field (real registration, e.g. "N8986Q") was present on 235/238 (98.7%).
The ~1-2% of real aircraft broadcasting no registration are simply NOT
recorded here -- a bare hex address is not the human-legible tail number
this feature is about, and inventing a placeholder identity for one would
misrepresent what "seen" means. An honest, small, real gap, not a bug.

RETENTION -- bounded to HANGAR_MAX_ENTRIES from the start, LRU by
last-seen: the least-recently-seen entry is evicted the moment a NEW
distinct aircraft would exceed the cap. Documented explicitly per the ATC
log's own retention-window precedent (see atc.py's LOG_MAX_AGE_SECONDS)
-- an unbounded, ever-growing log the render loop reads is exactly the
resource-growth class of issue this project's most recent audit found and
fixed elsewhere (mma.py/skypass.py's poll loops, arcade_server's POST body
reads). 500 distinct tail numbers is a generous cap for a single home
location's real local+overflight traffic; it exists as a hard ceiling,
not a number expected to bind in practice.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import paneltext

HANGAR_PATH = Path(__file__).parent / "hangar_log.jsonl"
HANGAR_MAX_ENTRIES = 500
# A visit is a distinct appearance, not a poll tick. FlightFeed records
# every aircraft every POSITION_REFRESH (~15s). Counting those as
# times_seen turned a 10-minute overflight into "SEEN 40X". Same tail
# still in the sky (last_seen within this gap) is one visit; a later
# reappearance after the gap is a new one. 30 minutes is longer than a
# local overflight and shorter than a real return trip.
VISIT_GAP_S = 30 * 60


class HangarLog:
    """Reads/writes HANGAR_PATH. Lazy-loaded on first use (not at import
    time -- keeps import side-effect-free like every other module here),
    then kept as an in-memory dict for the life of the process; every
    write is also flushed to disk immediately so a restart doesn't lose
    the collection."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}    # reg -> {reg, type, airline, first_seen, last_seen, times_seen}
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        entries = {}
        if HANGAR_PATH.exists():
            try:
                with HANGAR_PATH.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue    # one corrupt line (a torn write) must not lose the rest
                        if isinstance(e, dict) and e.get("reg"):
                            entries[e["reg"]] = e
            except OSError:
                pass
        self._entries = entries
        self._loaded = True

    def record_sighting(self, reg, ac_type, airline):
        """Called by flights.FlightFeed's background thread, once per
        real aircraft with a real registration per refresh cycle. `reg`
        must already be a real, non-empty, folded registration string --
        the caller filters that, this function trusts it. Real type/
        airline data seen on a LATER sighting fills in a gap from an
        earlier one (e.g. adsbdb's route lookup resolving the airline a
        cycle or two after the aircraft was first logged) rather than
        leaving it stuck missing forever."""
        with self._lock:
            self._ensure_loaded()
            now = time.time()
            e = self._entries.get(reg)
            if e is None:
                self._entries[reg] = {
                    "reg": reg, "type": ac_type or None, "airline": airline or None,
                    "first_seen": now, "last_seen": now, "times_seen": 1,
                }
                self._evict_if_over_cap()
            else:
                if ac_type and not e.get("type"):
                    e["type"] = ac_type
                if airline and not e.get("airline"):
                    e["airline"] = airline
                # Increment only on a new VISIT. last_seen is refreshed
                # every poll while the aircraft stays in range, so a
                # continuous pass keeps the same times_seen. A gap longer
                # than VISIT_GAP_S is a real departure and return.
                if now - float(e.get("last_seen") or 0) >= VISIT_GAP_S:
                    e["times_seen"] = e.get("times_seen", 1) + 1
                e["last_seen"] = now
            self._save()

    def _evict_if_over_cap(self):
        if len(self._entries) <= HANGAR_MAX_ENTRIES:
            return
        oldest_key = min(self._entries, key=lambda k: self._entries[k].get("last_seen", 0))
        del self._entries[oldest_key]

    def _save(self):
        try:
            with HANGAR_PATH.open("w") as f:
                for e in self._entries.values():
                    f.write(json.dumps(e) + "\n")
        except OSError:
            pass    # degrade honestly: an unwritable disk loses persistence, not the session

    def get(self):
        """Every entry, most-recently-seen first. Never blocks (a pure
        in-memory read after the one-time lazy load); never invents an
        entry -- an unreadable/missing/corrupt file is an empty
        collection, not a guess."""
        with self._lock:
            self._ensure_loaded()
            return sorted(self._entries.values(), key=lambda e: e.get("last_seen", 0), reverse=True)


LOG = HangarLog()


# ---- type book ----------------------------------------------------------
# Qualitative airframe facts a spotter actually wants. Seeded from ICAO
# type designators already in THIS hangar (plus the well-known families
# those codes belong to). Every field is a published class fact
# (engine layout, typical role, typical seating) -- NEVER a price.
# Used-aircraft money moves weekly; inventing a sticker would lie.
# Missing stays None and the glass omits the row.
#
# seats is a TYPICAL published cabin, not this tail's interior. Airline
# configs vary too much -- airliners get a class (NARROWBODY) and no
# seat number.

def _tb(name, engines=None, role=None, seats=None):
    return {"name": name, "engines": engines, "role": role, "seats": seats}


TYPEBOOK = {
    # helicopters
    "R44":  _tb("ROBINSON R44", "1 PISTON", "PRIVATE", "4"),
    "AS65": _tb("AS365 DAUPHIN", "2 TURBINE", "HELI", "8-13"),
    "EC35": _tb("EC135", "2 TURBINE", "HELI", "7-8"),
    "B407": _tb("BELL 407", "1 TURBINE", "HELI", "7"),
    # GA piston
    "C150": _tb("CESSNA 150", "1 PISTON", "TRAINER", "2"),
    "C172": _tb("CESSNA 172", "1 PISTON", "TRAINER", "4"),
    "C206": _tb("CESSNA 206", "1 PISTON", "UTILITY", "6"),
    "T206": _tb("CESSNA T206", "1 PISTON", "UTILITY", "6"),
    "C82S": _tb("CESSNA 182", "1 PISTON", "PERSONAL", "4"),
    "C414": _tb("CESSNA 414", "2 PISTON", "CHARTER", "6-8"),
    "SR20": _tb("CIRRUS SR20", "1 PISTON", "PERSONAL", "4"),
    "SR22": _tb("CIRRUS SR22", "1 PISTON", "PERSONAL", "5"),
    "S22T": _tb("CIRRUS SR22T", "1 PISTON", "PERSONAL", "5"),
    "P28A": _tb("PIPER CHEROKEE", "1 PISTON", "TRAINER", "4"),
    "P28R": _tb("PIPER ARROW", "1 PISTON", "PERSONAL", "4"),
    "P32R": _tb("PIPER LANCE", "1 PISTON", "PERSONAL", "6"),
    "PA32": _tb("PIPER SARATOGA", "1 PISTON", "PERSONAL", "6"),
    "PA24": _tb("PIPER COMANCHE", "1 PISTON", "PERSONAL", "4"),
    "PA44": _tb("PIPER SEMINOLE", "2 PISTON", "TRAINER", "4"),
    "PA31": _tb("PIPER NAVAJO", "2 PISTON", "CHARTER", "6-8"),
    "PA46": _tb("PIPER MALIBU", "1 PISTON", "PERSONAL", "6"),
    "BE33": _tb("BONANZA 33", "1 PISTON", "PERSONAL", "4"),
    "BE35": _tb("BONANZA 35", "1 PISTON", "PERSONAL", "4"),
    "BE36": _tb("BONANZA 36", "1 PISTON", "PERSONAL", "6"),
    "BE55": _tb("BARON 55", "2 PISTON", "PERSONAL", "6"),
    "BE58": _tb("BARON 58", "2 PISTON", "PERSONAL", "6"),
    "DA40": _tb("DIAMOND DA40", "1 PISTON", "TRAINER", "4"),
    "COL3": _tb("COLUMBIA 300", "1 PISTON", "PERSONAL", "4"),
    "COL4": _tb("CESSNA TTX", "1 PISTON", "PERSONAL", "4"),
    "M20P": _tb("MOONEY M20", "1 PISTON", "PERSONAL", "4"),
    "AA1":  _tb("GRUMMAN TRAINER", "1 PISTON", "TRAINER", "2"),
    "AA5":  _tb("GRUMMAN TIGER", "1 PISTON", "PERSONAL", "4"),
    "RV12": _tb("VANS RV-12", "1 PISTON", "HOMEBUILT", "2"),
    "TAMP": _tb("TB9 TAMPICO", "1 PISTON", "TRAINER", "4"),
    # turboprops
    "PC12": _tb("PILATUS PC-12", "1 TURBOPROP", "CORPORATE", "6-9"),
    "TBM7": _tb("TBM 700", "1 TURBOPROP", "PERSONAL", "6"),
    "TBM9": _tb("TBM 900", "1 TURBOPROP", "PERSONAL", "6"),
    "P46T": _tb("PIPER MERIDIAN", "1 TURBOPROP", "PERSONAL", "6"),
    "BE9L": _tb("KING AIR 90", "2 TURBOPROP", "CORPORATE", "6-8"),
    "BE20": _tb("KING AIR 200", "2 TURBOPROP", "CORPORATE", "8-9"),
    "BE30": _tb("KING AIR 300", "2 TURBOPROP", "CORPORATE", "8-10"),
    "B350": _tb("KING AIR 350", "2 TURBOPROP", "CORPORATE", "8-11"),
    "AT73": _tb("ATR 72", "2 TURBOPROP", "REGIONAL"),
    "C30J": _tb("C-130J", "4 TURBOPROP", "MILITARY"),
    "TEX2": _tb("T-6 TEXAN II", "1 TURBOPROP", "MILITARY", "2"),
    "SH36": _tb("SHORTS 360", "2 TURBOPROP", "CARGO"),
    # light / mid / large jets
    "SF50": _tb("VISION JET", "1 JET", "PERSONAL", "5-7"),
    "HDJT": _tb("HONDAJET", "2 JET", "CORPORATE", "5-6"),
    "PC24": _tb("PILATUS PC-24", "2 JET", "CORPORATE", "6-8"),
    "C25A": _tb("CITATION CJ1", "2 JET", "CORPORATE", "5-6"),
    "C25B": _tb("CITATION CJ2", "2 JET", "CORPORATE", "6"),
    "C25C": _tb("CITATION CJ3", "2 JET", "CORPORATE", "6-7"),
    "C525": _tb("CITATION JET", "2 JET", "CORPORATE", "5-6"),
    "C550": _tb("CITATION II", "2 JET", "CORPORATE"),
    "C56X": _tb("CITATION EXCEL", "2 JET", "CORPORATE"),
    "C680": _tb("CITATION SOVEREIGN", "2 JET", "CORPORATE"),
    "C68A": _tb("CITATION LATITUDE", "2 JET", "CORPORATE"),
    "C700": _tb("CITATION LONGITUDE", "2 JET", "CORPORATE"),
    "C750": _tb("CITATION X", "2 JET", "CORPORATE"),
    "E50P": _tb("PHENOM 100", "2 JET", "CORPORATE", "4-6"),
    "E55P": _tb("PHENOM 300", "2 JET", "CORPORATE", "6-8"),
    "E545": _tb("PRAETOR 500", "2 JET", "CORPORATE"),
    "E35L": _tb("LEGACY 600", "2 JET", "CORPORATE"),
    "CL30": _tb("CHALLENGER 300", "2 JET", "CORPORATE"),
    "CL35": _tb("CHALLENGER 350", "2 JET", "CORPORATE"),
    "CL60": _tb("CHALLENGER 600", "2 JET", "CORPORATE"),
    "GLF4": _tb("GULFSTREAM G450", "2 JET", "CORPORATE"),
    "GLF5": _tb("GULFSTREAM G550", "2 JET", "CORPORATE"),
    "GLF6": _tb("GULFSTREAM G650", "2 JET", "CORPORATE"),
    "GLEX": _tb("GLOBAL EXPRESS", "2 JET", "CORPORATE"),
    "GL5T": _tb("GLOBAL 5000", "2 JET", "CORPORATE"),
    "G280": _tb("GULFSTREAM G280", "2 JET", "CORPORATE"),
    "FA7X": _tb("FALCON 7X", "3 JET", "CORPORATE"),
    "F2TH": _tb("FALCON 2000", "2 JET", "CORPORATE"),
    "F900": _tb("FALCON 900", "3 JET", "CORPORATE"),
    "FA10": _tb("FALCON 10", "2 JET", "CORPORATE"),
    "H25B": _tb("HAWKER 800", "2 JET", "CORPORATE"),
    "LJ45": _tb("LEARJET 45", "2 JET", "CORPORATE"),
    "LJ55": _tb("LEARJET 55", "2 JET", "CORPORATE"),
    "LJ60": _tb("LEARJET 60", "2 JET", "CORPORATE"),
    "BE40": _tb("BEECHJET 400", "2 JET", "CORPORATE"),
    "T38":  _tb("T-38 TALON", "2 JET", "MILITARY", "2"),
    "C17":  _tb("C-17", "4 JET", "MILITARY"),
    "C5M":  _tb("C-5M GALAXY", "4 JET", "HEAVY LIFT"),
    # airliners -- no seat count (cabin is an airline choice)
    "A319": _tb("A319", "2 JET", "NARROWBODY"),
    "A320": _tb("A320", "2 JET", "NARROWBODY"),
    "A321": _tb("A321", "2 JET", "NARROWBODY"),
    "A20N": _tb("A320NEO", "2 JET", "NARROWBODY"),
    "A21N": _tb("A321NEO", "2 JET", "NARROWBODY"),
    "A333": _tb("A330-300", "2 JET", "WIDEBODY"),
    "A35K": _tb("A350-1000", "2 JET", "WIDEBODY"),
    "BCS1": _tb("A220-100", "2 JET", "NARROWBODY"),
    "BCS3": _tb("A220-300", "2 JET", "NARROWBODY"),
    "B712": _tb("717-200", "2 JET", "REGIONAL"),
    "B737": _tb("737-700", "2 JET", "NARROWBODY"),
    "B738": _tb("737-800", "2 JET", "NARROWBODY"),
    "B739": _tb("737-900", "2 JET", "NARROWBODY"),
    "B38M": _tb("737 MAX 8", "2 JET", "NARROWBODY"),
    "B39M": _tb("737 MAX 9", "2 JET", "NARROWBODY"),
    "B752": _tb("757-200", "2 JET", "NARROWBODY"),
    "B763": _tb("767-300", "2 JET", "WIDEBODY"),
    "B764": _tb("767-400", "2 JET", "WIDEBODY"),
    "B788": _tb("787-8", "2 JET", "WIDEBODY"),
    "B789": _tb("787-9", "2 JET", "WIDEBODY"),
    "CRJ2": _tb("CRJ200", "2 JET", "REGIONAL"),
    "CRJ7": _tb("CRJ700", "2 JET", "REGIONAL"),
    "CRJ9": _tb("CRJ900", "2 JET", "REGIONAL"),
    "E145": _tb("ERJ-145", "2 JET", "REGIONAL"),
    "E45X": _tb("ERJ-145XR", "2 JET", "REGIONAL"),
    "E170": _tb("E170", "2 JET", "REGIONAL"),
    "E75L": _tb("E175", "2 JET", "REGIONAL"),
    "E75S": _tb("E175", "2 JET", "REGIONAL"),
    "E190": _tb("E190", "2 JET", "REGIONAL"),
}


# First flight / EIS year -- stable published dates only.
TYPE_YEAR = {
    "C150": "1958", "C172": "1956", "C206": "1962", "T206": "1962",
    "C82S": "1956", "C414": "1968",
    "SR20": "1999", "SR22": "2001", "S22T": "2006", "SF50": "2016",
    "P28A": "1960", "P28R": "1967", "PA32": "1965", "PA24": "1957",
    "PA44": "1978", "PA31": "1967", "PA46": "1983", "P46T": "2000",
    "BE33": "1959", "BE35": "1945", "BE36": "1968", "BE55": "1960",
    "BE58": "1969", "BE9L": "1964", "BE20": "1972", "BE30": "1984",
    "B350": "1990", "BE40": "1985",
    "DA40": "2000", "M20P": "1955", "RV12": "2008",
    "PC12": "1994", "PC24": "2017", "TBM7": "1990", "TBM9": "2014",
    "R44": "1993", "EC35": "1994", "B407": "1995", "AS65": "1975",
    "HDJT": "2015", "C25A": "2002", "C25B": "2000", "C25C": "2004",
    "C550": "1978", "C56X": "1996", "C680": "2004", "C68A": "2014",
    "C700": "2019", "C750": "1996",
    "E50P": "2008", "E55P": "2009", "E545": "2018", "E35L": "2002",
    "CL30": "2001", "CL35": "2014", "CL60": "1980",
    "GLF4": "1995", "GLF5": "2003", "GLF6": "2009", "GLEX": "1996",
    "GL5T": "1998", "G280": "2009",
    "FA7X": "2005", "F2TH": "1993", "F900": "1984", "FA10": "1970",
    "H25B": "1983", "LJ45": "1995", "LJ55": "1979", "LJ60": "1990",
    "T38": "1959", "TEX2": "2000", "C17": "1991", "C5M": "2006",
    "C30J": "1996", "AT73": "1988", "SH36": "1981",
    "A319": "1995", "A320": "1988", "A321": "1993",
    "A20N": "2016", "A21N": "2017", "A333": "1992", "A35K": "2016",
    "BCS1": "2013", "BCS3": "2015",
    "B712": "1998", "B737": "1997", "B738": "1997", "B739": "2000",
    "B38M": "2017", "B39M": "2018",
    "B752": "1982", "B763": "1986", "B764": "2000",
    "B788": "2009", "B789": "2013",
    "CRJ2": "1991", "CRJ7": "1999", "CRJ9": "2001",
    "E145": "1995", "E45X": "2002", "E170": "2002",
    "E75L": "2004", "E75S": "2004", "E190": "2004",
}

# One extra thesis only when it says more than engines+role.
TYPE_USE = {
    "PC12": "REGIONAL PRIVATE AND CORPORATE",
    "PC24": "JET FOR SHORT STRIPS",
    "SF50": "SINGLE ENGINE PERSONAL JET",
    "HDJT": "OVER WING LIGHT JET",
    "C172": "THE STANDARD TRAINER",
    "SR22": "PERSONAL CROSS COUNTRY",
    "R44": "PRIVATE FOUR SEAT HELI",
    "C5M": "USAF HEAVY LIFT",
    "C17": "USAF STRATEGIC AIRLIFT",
    "C30J": "TACTICAL TRANSPORT",
    "T38": "USAF SUPERSONIC TRAINER",
    "TEX2": "USAF PRIMARY TRAINER",
    "AT73": "REGIONAL TURBOPROP",
    "GLEX": "ULTRA LONG RANGE",
    "GLF5": "LONG RANGE BIZJET",
    "GLF6": "ULTRA LONG RANGE",
    "C750": "FASTEST CIVILIAN CITATION",
}


def type_facts(icao_type):
    """Folded type card, or {} if we have nothing honest to say."""
    t = (icao_type or "").strip().upper()
    raw = TYPEBOOK.get(t) or {}
    name = paneltext.panel_text(raw.get("name")) or None
    engines = paneltext.panel_text(raw.get("engines")) or None
    role = paneltext.panel_text(raw.get("role")) or None
    seats = paneltext.panel_text(raw.get("seats")) or None
    year = paneltext.panel_text(TYPE_YEAR.get(t)) or None
    use = paneltext.panel_text(TYPE_USE.get(t)) or None
    if not any((name, engines, role, seats, year, use)):
        return {}
    return {
        "name": name,
        "engines": engines,
        "role": role,
        "seats": seats,
        "year": year,
        "use": use,
    }


def collection_index(entries):
    """Local rarity from THIS hangar. Never world-fleet numbers."""
    types = {}
    for e in entries or []:
        t = (e.get("type") or "").strip().upper()
        if t:
            types[t] = types.get(t, 0) + 1
    return types, len(entries or [])


def rarity_line(type_code, type_counts, total):
    """How uncommon this type is over THIS house. None if unknown."""
    t = (type_code or "").strip().upper()
    n = type_counts.get(t) if t else None
    if not n or not total:
        return None
    if n == 1:
        return "ONLY ONE HERE"
    if n <= 3:
        return "RARE HERE  %d" % n
    if n >= 20:
        return "COMMON  %d" % n
    return "%d %s" % (n, t)


def revisit_rhythm(first_seen, last_seen, times_seen):
    """How often this tail tends to come back, as real arithmetic over
    the two real timestamps and visit count this project already
    stores -- (last_seen - first_seen) / (times_seen - 1). None below
    3 real visits: a 2-visit average is a single real gap dressed up as
    a rhythm, not a reliable pattern, and this project doesn't present
    a guess-shaped number as a real fact. Never a fabricated interval
    -- purely derived from data already on disk, same category as
    bird_ordinal()/rarity_line() above."""
    try:
        times = int(times_seen)
        fs, ls = float(first_seen), float(last_seen)
    except (TypeError, ValueError):
        return None
    if times < 3 or ls <= fs:
        return None
    avg_days = (ls - fs) / (86400.0 * (times - 1))
    if avg_days < 1:
        return "EVERY <1D"
    if avg_days < 21:
        return "EVERY ~%dD" % round(avg_days)
    return "EVERY ~%dW" % round(avg_days / 7)


def bird_ordinal(entries, reg):
    """This tail's place in the collection by first_seen, oldest first."""
    if not reg:
        return None, len(entries or [])
    by_first = sorted(entries or [], key=lambda e: e.get("first_seen") or 0)
    for i, e in enumerate(by_first):
        if e.get("reg") == reg:
            return i + 1, len(by_first)
    return None, len(by_first)


def _age_txt(ts, now):
    if not ts:
        return None
    try:
        secs = max(0, int(now - float(ts)))
    except (TypeError, ValueError):
        return None
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return "%dD %dH" % (d, h)
    if h:
        return "%dH %02dM" % (h, m)
    if m:
        return "%dM %02dS" % (m, s)
    return "%dS" % s


def dossier(entry, entries, sheet=None, now=None):
    """Pinned identity + paged fact rows. Missing stays off.

    The glass pins the tail and the type name. Everything else is a
    page of at most four rows so 64px never stacks a wall of plates.
    """
    e = entry or {}
    sheet = sheet or {}
    now = time.time() if now is None else now
    facts = type_facts(e.get("type"))
    icao = paneltext.panel_text((e.get("type") or "").strip()) or None
    name = (facts.get("name")
            or icao
            or "TYPE UNKNOWN")
    counts, total = collection_index(entries)
    pages = []

    air = []
    if facts.get("engines"):
        air.append(facts["engines"])
    if facts.get("seats"):
        air.append("SEATS %s" % facts["seats"])
    if facts.get("use"):
        air.append(facts["use"])
    if facts.get("role") and facts["role"] not in (facts.get("use") or ""):
        air.append(facts["role"])
    if facts.get("year"):
        air.append("SINCE %s" % facts["year"])
    if air:
        pages.append(air[:4])
        leftover_year = facts.get("year") and len(air) > 4
    else:
        leftover_year = bool(facts.get("year"))

    here = []
    times = e.get("times_seen") or 1
    try:
        times = int(times)
    except (TypeError, ValueError):
        times = 1
    here.append("FIRST SIGHTING" if times <= 1 else "SEEN %dX" % times)
    rare = rarity_line(icao, counts, total)
    if rare:
        here.append(rare)
    ordinal, n_all = bird_ordinal(entries, e.get("reg"))
    if ordinal and n_all:
        here.append("BIRD %d/%d" % (ordinal, n_all))
    if here:
        pages.append(here[:4])

    tail = []
    owner = sheet.get("owner")
    maker = sheet.get("manufacturer")
    variant = sheet.get("variant")
    country = sheet.get("country")
    airline = paneltext.panel_text(e.get("airline")) or None
    if owner:
        tail.append(owner)
    if maker:
        tail.append(maker)
    if variant and variant != name:
        tail.append(variant)
    if country:
        tail.append(country)
    if airline and airline != owner and len(tail) < 4:
        tail.append(airline)
    if tail:
        pages.append(tail[:4])

    log = []
    first = _age_txt(e.get("first_seen"), now)
    last = _age_txt(e.get("last_seen"), now)
    if first:
        log.append("FIRST %s" % first)
    if last:
        log.append("LAST %s" % last)
    rhythm = revisit_rhythm(e.get("first_seen"), e.get("last_seen"), times)
    if rhythm:
        log.append(rhythm)
    if leftover_year:
        log.append("SINCE %s" % facts["year"])
    if icao:
        log.append(icao)
    if log:
        pages.append(log[:4])

    if not pages:
        pages.append(["NO FACTS YET"])

    return {
        "name": name,
        "icao": icao,
        "first_sighting": times <= 1,
        "pages": pages,
    }


# ---- per-tail sheet (adsbdb) -------------------------------------------
# Same host flights.py already uses for routes. One tail at a time --
# whichever hangar card is on screen -- never the whole 500.
AIRCRAFT_URL = "https://api.adsbdb.com/v0/aircraft/{reg}"
_UA = "Mozilla/5.0 (HenderburghArcade)"
SHEET_IDLE = 120.0
SHEET_OK_TTL = 86400.0     # owner/type do not change mid-day
SHEET_MISS_TTL = 86400.0   # 404 is a real miss, do not hammer
TIMEOUT = 8.0


def _parse_sheet(payload):
    ac = ((payload or {}).get("response") or {}).get("aircraft") or {}
    if not isinstance(ac, dict) or not ac:
        return None
    owner = paneltext.panel_text(ac.get("registered_owner")) or None
    maker = paneltext.panel_text(ac.get("manufacturer")) or None
    variant = paneltext.panel_text(ac.get("type")) or None
    country = paneltext.panel_text(ac.get("registered_owner_country_iso_name")
                                   or ac.get("registered_owner_country_name")) or None
    icao = paneltext.panel_text(ac.get("icao_type")) or None
    if not any((owner, maker, variant)):
        return None
    return {
        "owner": owner,
        "manufacturer": maker,
        "variant": variant,
        "country": country,
        "icao": icao,
    }


class HangarSheets:
    """Off-thread adsbdb aircraft sheet for the tail on screen."""

    def __init__(self):
        self._lock = threading.Lock()
        self._want = None
        self._last_read = 0.0
        self._cache = {}       # reg -> parsed or {}
        self._updated = {}     # reg -> epoch
        self._thread = None
        self._err = None

    def want(self, reg):
        reg = (reg or "").strip().upper()
        if not reg:
            return
        with self._lock:
            self._want = reg
            self._last_read = time.time()
        self._ensure()

    def get(self, reg):
        reg = (reg or "").strip().upper()
        with self._lock:
            self._last_read = time.time()
            rec = self._cache.get(reg)
            return dict(rec) if rec else {}

    def _ensure(self):
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
        if not alive:
            t = threading.Thread(target=self._run, daemon=True)
            with self._lock:
                self._thread = t
            t.start()

    def _due(self, reg):
        last = self._updated.get(reg, 0.0)
        rec = self._cache.get(reg)
        ttl = SHEET_MISS_TTL if rec == {} else SHEET_OK_TTL
        return time.time() - last >= ttl

    def _poll(self, reg):
        url = AIRCRAFT_URL.format(reg=urllib.parse.quote(reg, safe=""))
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                parsed = _parse_sheet(json.load(r))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                parsed = {}
            else:
                raise
        with self._lock:
            self._cache[reg] = parsed or {}
            self._updated[reg] = time.time()
            self._err = None
            if len(self._cache) > 8:
                oldest = min(self._updated, key=self._updated.get)
                if oldest != reg:
                    self._cache.pop(oldest, None)
                    self._updated.pop(oldest, None)

    def _run(self):
        while True:
            with self._lock:
                idle = time.time() - self._last_read > SHEET_IDLE
                want = self._want
                due = bool(want) and self._due(want)
            if idle:
                with self._lock:
                    self._thread = None
                return
            if due:
                try:
                    self._poll(want)
                except Exception as e:                 # noqa: BLE001
                    with self._lock:
                        self._err = type(e).__name__
                    time.sleep(30.0)
            time.sleep(1.0)


SHEETS = HangarSheets()
