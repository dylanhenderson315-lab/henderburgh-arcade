"""
ownernote.py -- a persistent, owner-authored note/message, distinct from
every other text-on-the-panel feature this project already has:

  * `/api/notify` (notify.py) is HA-PUSHED and EPHEMERAL -- a banner or
    takeover that auto-clears after a fixed window, and Home Assistant
    is the author.
  * `blog.py` mirrors the PUBLIC guestbook -- visitor-submitted, not the
    owner's own words, and read-only from this project's side.
  * This is the owner's OWN words, typed once from the control panel,
    and stays on screen until the owner changes or clears it -- a real
    sticky note, not a notification.

No network I/O at all -- this is local config, same shape as
`dnd.py`/`notify.py`'s own config halves, not a `FEED`-shaped poller
(there is nothing to poll; the owner PUSHES via the HTTP endpoint, the
identical reasoning notify.py's own module docstring already gives for
why it isn't FEED-shaped either).

The text is entirely owner-typed, so it goes through
`paneltext.panel_text()` at the save boundary -- same "fold at the
write boundary" rule as ATC transcription's `fold_transcript()`, not
deferred to render time.
"""
import json
from pathlib import Path

import paneltext

CONFIG_PATH = Path(__file__).parent / "ownernote_config.json"


def load_config():
    """{"text": str|None}. None is the honest default -- no note set,
    never a placeholder message."""
    if not CONFIG_PATH.exists():
        return {"text": None}
    try:
        raw = json.loads(CONFIG_PATH.read_text())
        if isinstance(raw, dict):
            v = raw.get("text")
            return {"text": (str(v).strip() or None) if v else None}
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        pass
    return {"text": None}


def save_config(text):
    """Folds the owner's own text through panel_text() HERE, at the
    write boundary -- an owner-typed note is exactly the kind of text
    that can carry curly quotes/accents/emoji the 3x5 font can't draw,
    the same class of bug this project has hit ten-plus times on
    externally-sourced text. An empty/whitespace-only save clears the
    note (`text: None`), the one real "remove it" path -- there is no
    separate clear_key-style flag needed since, unlike a credential,
    there's no risk of an unrelated save silently wiping this (this
    file has exactly one key)."""
    folded = paneltext.panel_text(text) if text else None
    data = {"text": folded or None}
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    return data
