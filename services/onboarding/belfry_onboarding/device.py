"""A short label for the device that opened a session ("Safari on iPhone"), from its User-Agent.

Stored after the app's name in the signer's connection name ("Chat · Safari on iPhone") so the
member can tell their sessions apart; never more than a browser and a system.
"""

from __future__ import annotations

import re

SEP = " · "

_BROWSERS = (
    (r"EdgA?/|Edg/", "Edge"),
    (r"OPR/|Opera", "Opera"),
    (r"SamsungBrowser/", "Samsung Internet"),
    (r"Firefox/|FxiOS/", "Firefox"),
    (r"CriOS/|Chrome/|Chromium/", "Chrome"),
    (r"Safari/", "Safari"),
)
_SYSTEMS = (
    (r"iPhone", "iPhone"),
    (r"iPad", "iPad"),
    (r"Android", "Android"),
    (r"Windows", "Windows"),
    (r"Mac OS X|Macintosh", "Mac"),
    (r"CrOS", "ChromeOS"),
    (r"Linux", "Linux"),
)


def label(ua: str | None) -> str:
    ua = (ua or "")[:400]
    browser = next((n for p, n in _BROWSERS if re.search(p, ua)), "")
    system = next((n for p, n in _SYSTEMS if re.search(p, ua)), "")
    if browser and system:
        return f"{browser} on {system}"
    return browser or system


def name(app: str, ua: str | None) -> str:
    d = label(ua)
    return f"{app}{SEP}{d}" if d else app


def split(full: str | None) -> tuple[str, str]:
    """(app, device) from a stored connection name; device is "" for older names."""
    full = full or ""
    app, sep, d = full.rpartition(SEP)
    if sep and d in _KNOWN:
        return app, d
    return full, ""


_KNOWN = (
    {f"{b} on {s}" for _, b in _BROWSERS for _, s in _SYSTEMS}
    | {n for _, n in _BROWSERS}
    | {n for _, n in _SYSTEMS}
)
