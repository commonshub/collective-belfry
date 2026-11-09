"""Places for members' profiles: a city to pick, or one the belfry learnt.

``GET /api/places?q=<text>&limit=8`` (belfry.<domain>/api/v1/places, no sign-in: public geodata)
answers the cities whose name starts with the text (accents and case folded), then those with a
word starting with it, the most populated first. The list is GeoNames' cities15000 (CC BY 4.0,
about 34,000 cities above 15,000 people), trimmed and gzipped in ``data/`` (750 KB), read once.

``POST /api/places`` (NIP-98, an active member, not a guest) ``{"name", "country", "near"}`` adds
a place GeoNames doesn't know: its point is the one of the nearby city ``near`` (an approximate
location on purpose), kept in the register (param ``belfry.places``). The same name and country
again answers the place already there.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services

DATA = Path(__file__).parent / "data" / "cities15000.tsv.gz"
ATTRIBUTION = "Places: GeoNames (CC BY 4.0)"
PARAM = "belfry.places"
MAX_LIMIT = 20
MAX_NAME = 80

_lock = threading.Lock()
# (fold(name), fold(ascii), place) per city; place = the answer's shape plus "pop"
_cities: list[tuple[str, str, dict[str, Any]]] | None = None
_by_id: dict[str, dict[str, Any]] = {}
_countries: set[str] = set()


def fold(text: str) -> str:
    """Lower case without accents: 'Liège' and 'liege' match."""
    norm = unicodedata.normalize("NFKD", text)
    return "".join(c for c in norm if not unicodedata.combining(c)).casefold().strip()


def _load() -> list[tuple[str, str, dict[str, Any]]]:
    global _cities
    with _lock:
        if _cities is None:
            rows: list[tuple[str, str, dict[str, Any]]] = []
            for line in gzip.decompress(DATA.read_bytes()).decode("utf-8").splitlines():
                gid, name, ascii_name, cc, region, lat, lon, pop = line.split("\t")
                place = {
                    "id": f"gn:{gid}",
                    "name": name,
                    "region": region,
                    "country": cc,
                    "lat": float(lat),
                    "lon": float(lon),
                    "pop": int(pop or 0),
                }
                rows.append((fold(name), fold(ascii_name), place))
                _by_id[place["id"]] = place
                _countries.add(cc)
            _cities = rows
    return _cities


def countries() -> set[str]:
    _load()
    return _countries


def city(place_id: str) -> dict[str, Any] | None:
    _load()
    return _by_id.get(place_id)


def _public(place: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in place.items() if k not in ("pop", "added_by", "at")}
    out["lat"], out["lon"] = round(float(out["lat"]), 2), round(float(out["lon"]), 2)
    return out


def added(svc: Services) -> list[dict[str, Any]]:
    """The places members added (empty without the belfry's own register)."""
    param = getattr(svc.odoo, "param", None)
    if param is None:
        return []
    try:
        rows = json.loads(param(PARAM, "[]") or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict)]


def search(svc: Services, q: str, limit: int = 8) -> list[dict[str, Any]]:
    q = fold(q)
    limit = max(1, min(MAX_LIMIT, limit))
    if len(q) < 2:
        return []
    starts: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    rows = [(fold(p["name"]), fold(p["name"]), {**p, "pop": 0}) for p in added(svc)]
    for name, ascii_name, place in [*rows, *_load()]:
        if name.startswith(q) or ascii_name.startswith(q):
            starts.append(place)
        elif any(w.startswith(q) for w in (name + " " + ascii_name).replace("-", " ").split()):
            words.append(place)

    def rank(p: dict[str, Any]) -> int:
        return -p["pop"]

    found = sorted(starts, key=rank) + sorted(words, key=rank)
    return [_public(p) for p in found[:limit]]


def add(svc: Services, pubkey: str, name: str, country: str, near: str) -> dict[str, Any]:
    from .holder_invites import ApiError

    name = " ".join(name.split())
    if not name or len(name) > MAX_NAME or any(unicodedata.category(c)[0] == "C" for c in name):
        raise ApiError(422, "invalid", f"name: 1 to {MAX_NAME} characters.")
    country = country.strip().upper()
    if country not in countries():
        raise ApiError(422, "invalid", "country: an ISO 3166 code such as BE.")
    base = city(near) or next((p for p in added(svc) if p.get("id") == near), None)
    if base is None:
        raise ApiError(422, "invalid", "near: the id of a city from the list.")
    with _lock:
        rows = added(svc)
        for r in rows:
            if fold(r["name"]) == fold(name) and r["country"] == country:
                return _public(r)
        n = max([int(str(r["id"]).removeprefix("b:")) for r in rows] or [0]) + 1
        row = {
            "id": f"b:{n}",
            "name": name,
            "region": base.get("region", "") if base["country"] == country else "",
            "country": country,
            "lat": round(float(base["lat"]), 2),
            "lon": round(float(base["lon"]), 2),
            "near": base["name"],
            "added_by": pubkey,
            "at": int(time.time()),
        }
        svc.odoo.set_param(PARAM, json.dumps([*rows, row]))
    return _public(row)


def member_key(svc: Services, pubkey: str) -> dict[str, Any] | None:
    """The active, non-guest pass of this key (with its partner), else None."""
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
        ["tier", "partner_id"],
    )
    # a channel's guests, people or agents, have tier "guest" (guests.py)
    return next((r for r in rows if r.get("tier") != "guest"), None)


def register(app: FastAPI) -> None:
    from .holder_invites import ApiError, authenticate

    @app.get("/api/places")
    async def places(request: Request, q: str = "", limit: int = 8) -> JSONResponse:
        s = request.app.state.services
        found = await run_in_threadpool(search, s, q[:80], limit)
        return JSONResponse(
            {"places": found, "attribution": ATTRIBUTION},
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.post("/api/places")
    async def add_place(request: Request) -> JSONResponse:
        from . import invite

        s = request.app.state.services
        body = await request.body()
        pubkey = authenticate(
            s,
            request.app.state.api_seen,
            request.headers.get("authorization"),
            "POST",
            "/api/places",
            body,
        )
        if not await run_in_threadpool(member_key, s, pubkey):
            raise ApiError(403, "not_member", "Only members add places.")
        if not invite.own(s):
            raise ApiError(409, "unsupported", "Adding places needs the belfry's own register.")
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        place = await run_in_threadpool(
            add,
            s,
            pubkey,
            str(data.get("name") or ""),
            str(data.get("country") or ""),
            str(data.get("near") or ""),
        )
        return JSONResponse({"place": place}, headers={"Cache-Control": "no-store"})
