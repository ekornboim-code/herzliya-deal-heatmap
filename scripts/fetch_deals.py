#!/usr/bin/env python3
"""
One-time dump builder: Herzliya residential deals (last 10 years) with
year-normalized 1-100 scores for total price and price-per-sqm.

Source: odata.org.il CKAN mirror of nadlan deal records (Govmap/nadlan APIs
are SPA-gated from this environment). Deals only carry a street name (no
house number), so each deal is placed along the *real* OSM geometry of its
street (via Overpass), at a random point weighted by segment length, plus a
small perpendicular offset — instead of stacking every deal on one geocoded
street-centroid point. Streets missing from OSM fall back to a single
Nominatim point with a wider jitter radius.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "herzliya-deals.json"
GEOCODE_CACHE_PATH = DATA_DIR / "street-geocode-cache.json"
LINES_CACHE_PATH = DATA_DIR / "street-lines-cache.json"

ODATA_API = "https://www.odata.org.il/api/3/action"
OVERPASS_API = "https://overpass-api.de/api/interpreter"
CITY_NAME = "הרצלייה"
HERZLIYA_BBOX = "32.12,34.78,32.20,34.87"  # south,west,north,east
# The bbox above overlaps neighboring Ra'anana on its east side. Since many
# Hebrew street names repeat across cities (streets named after historical
# figures), bbox-only Overpass/Nominatim queries can pull in a same-named
# street that's actually in Ra'anana. HERZLIYA_RELATION_ID is Herzliya's real
# OSM administrative boundary (relation), used for a precise point-in-polygon
# check so cross-city name collisions get filtered out rather than placed.
HERZLIYA_RELATION_ID = 1382820
BOUNDARY_CACHE_PATH = DATA_DIR / "herzliya-boundary-cache.json"
RESOURCE_IDS = [
    "742a49d3-4ebe-4541-a715-5c8456cd7a65",
    "78d33b90-cb93-478a-ba60-3b519e551505",
]
# Fixed range start; the odata.org.il mirror's Herzliya rows currently max out
# around 2024-09, so a fixed 2014 start (rather than "N years back from now")
# gives a stable, reproducible window instead of silently drifting each run.
START_DATE = datetime(2014, 1, 1)

RESIDENTIAL = {
    "דירה בבית קומות",
    "דירה",
    "דירת גן",
    "דירת גג",
    "דירת גג (פנטהאוז)",
    "דירת נופש",
    "קוטג' דו משפחתי",
    "קוטג' חד משפחתי",
    "קוטג' טורי",
    "בית בודד",
    "חד משפחתי (וילה)",
    "מגורים",
}

SSL_CTX = ssl.create_default_context()
SSL_CTX.set_ciphers("DEFAULT")
USER_AGENT = "nadlan-herzliya-poc/1.0 (local research; contact: local)"


def api_call(action: str, **params):
    url = f"{ODATA_API}/{action}"
    body = json.dumps(params, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=180) as resp:
        return json.load(resp)


def fetch_city_records(resource_id: str) -> list[dict]:
    records: list[dict] = []
    limit = 1000
    offset = 0
    while True:
        data = api_call(
            "datastore_search",
            resource_id=resource_id,
            filters={"city_name": CITY_NAME},
            limit=limit,
            offset=offset,
        )
        batch = data["result"]["records"]
        total = data["result"]["total"]
        records.extend(batch)
        print(f"  {resource_id[:8]}… offset={offset} got={len(batch)} total={total}")
        offset += len(batch)
        if not batch or offset >= total:
            break
        time.sleep(0.2)
    return records


def parse_price(raw) -> float | None:
    if raw is None:
        return None
    s = str(raw).replace(",", "").replace("₪", "").strip()
    if not s:
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    return value if value > 0 else None


def parse_area(raw) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw).replace(",", "").strip())
    except ValueError:
        return None
    return value if value > 0 else None


def parse_date(record: dict) -> datetime | None:
    dt = record.get("dealdatetime") or ""
    if dt:
        try:
            return datetime.fromisoformat(str(dt).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    d = record.get("dealdate") or ""
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(d), fmt)
        except ValueError:
            continue
    return None


def parse_rooms(raw) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except ValueError:
        return None


def load_geocode_cache() -> dict:
    if GEOCODE_CACHE_PATH.exists():
        return json.loads(GEOCODE_CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_geocode_cache(cache: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GEOCODE_CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def geocode_street(
    street: str, cache: dict, boundary: dict | None = None
) -> tuple[float, float] | None:
    key = street.strip()
    if not key:
        return None
    if key in cache:
        val = cache[key]
        return (val["lat"], val["lon"]) if val else None

    query = f"{key}, הרצליה, ישראל"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1, "countrycodes": "il"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=30) as resp:
            results = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"  geocode fail {key!r}: {exc}")
        cache[key] = None
        save_geocode_cache(cache)
        time.sleep(1.1)
        return None

    if not results:
        # fallback English city name
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": f"{key}, Herzliya, Israel", "format": "json", "limit": 1, "countrycodes": "il"}
        )
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=30) as resp:
            results = json.load(resp)

    if results:
        lat = float(results[0]["lat"])
        lon = float(results[0]["lon"])
        # Reject matches outside Herzliya's real boundary — Nominatim can
        # otherwise match a same-named street in a neighboring city (e.g.
        # Ra'anana) since street names repeat heavily across Israeli cities.
        if point_in_boundary(lat, lon, boundary):
            cache[key] = {"lat": lat, "lon": lon}
            save_geocode_cache(cache)
            time.sleep(1.1)
            return lat, lon

    cache[key] = None
    save_geocode_cache(cache)
    time.sleep(1.1)
    return None


def fetch_herzliya_boundary() -> dict | None:
    """Herzliya's real administrative boundary as GeoJSON geometry (Polygon or
    MultiPolygon), via Nominatim's polygon lookup for the known OSM relation.
    Cached to disk since this never changes between runs."""
    if BOUNDARY_CACHE_PATH.exists():
        return json.loads(BOUNDARY_CACHE_PATH.read_text(encoding="utf-8"))

    url = "https://nominatim.openstreetmap.org/lookup?" + urllib.parse.urlencode(
        {"osm_ids": f"R{HERZLIYA_RELATION_ID}", "format": "json", "polygon_geojson": 1}
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=30) as resp:
            results = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"  boundary fetch failed, city-boundary filtering disabled: {exc}")
        return None

    if not results or "geojson" not in results[0]:
        print("  boundary lookup returned no geometry, city-boundary filtering disabled")
        return None

    geom = results[0]["geojson"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BOUNDARY_CACHE_PATH.write_text(json.dumps(geom, ensure_ascii=False), encoding="utf-8")
    return geom


def _ray_cast_ring(lat: float, lon: float, ring: list[list[float]]) -> bool:
    """ring is a list of [lon, lat] pairs (GeoJSON coordinate order)."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            x_at_lat = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_at_lat:
                inside = not inside
        j = i
    return inside


def point_in_boundary(lat: float, lon: float, boundary: dict | None) -> bool:
    """True if (lat, lon) is inside the given GeoJSON Polygon/MultiPolygon.
    If boundary is None (fetch failed), returns True so filtering degrades
    gracefully to "no filtering" rather than dropping everything."""
    if boundary is None:
        return True
    gtype = boundary.get("type")
    coords = boundary.get("coordinates")
    if gtype == "Polygon":
        polygons = [coords]
    elif gtype == "MultiPolygon":
        polygons = coords
    else:
        return True

    for rings in polygons:
        if not rings:
            continue
        if not _ray_cast_ring(lat, lon, rings[0]):
            continue
        if any(_ray_cast_ring(lat, lon, hole) for hole in rings[1:]):
            continue
        return True
    return False


def jitter(lat: float, lon: float, seed: str, meters: float = 90.0) -> tuple[float, float]:
    """Deterministic offset for the (rare) fallback point-only streets."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    u = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    v = int.from_bytes(digest[4:8], "big") / 0xFFFFFFFF
    angle = u * 2 * math.pi
    radius = math.sqrt(v) * meters
    dlat = (radius * math.cos(angle)) / 111_320
    dlon = (radius * math.sin(angle)) / (111_320 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def seed_floats(seed: str, n: int) -> list[float]:
    """Deterministic pseudo-random floats in [0, 1) derived from a seed string."""
    out = []
    i = 0
    while len(out) < n:
        digest = hashlib.sha256(f"{seed}:{i}".encode("utf-8")).digest()
        out.append(int.from_bytes(digest[:8], "big") / 0xFFFFFFFFFFFFFFFF)
        i += 1
    return out


def haversine_m(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    lat1, lon1 = p1
    lat2, lon2 = p2
    r = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def escape_overpass(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def overpass_query(query: str, max_retries: int = 5) -> dict:
    body = ("data=" + urllib.parse.quote(query)).encode("utf-8")
    req = urllib.request.Request(
        OVERPASS_API,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, context=SSL_CTX, timeout=90) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 504) and attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                print(f"  Overpass {exc.code}, retrying in {wait}s…")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("Overpass query failed after retries")


def load_lines_cache() -> dict:
    if LINES_CACHE_PATH.exists():
        return json.loads(LINES_CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_lines_cache(cache: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LINES_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def clip_polyline_to_boundary(
    coords: list[list[float]], boundary: dict | None
) -> list[list[list[float]]]:
    """Split coords ([[lat, lon], ...]) into contiguous sub-polylines that lie
    inside the boundary, dropping the portions outside it. A single OSM way
    can legitimately run across a municipal border (same street continuing
    into Ra'anana, Ramat HaSharon, etc.) without being split at the border in
    OSM — checking only one representative point (e.g. the midpoint) and then
    keeping the *whole* way would let placement land on the foreign portion
    too, so every point must be checked and the way clipped accordingly."""
    if boundary is None:
        return [coords] if len(coords) >= 2 else []

    runs: list[list[list[float]]] = []
    current: list[list[float]] = []
    for pt in coords:
        if point_in_boundary(pt[0], pt[1], boundary):
            current.append(pt)
        else:
            if len(current) >= 2:
                runs.append(current)
            current = []
    if len(current) >= 2:
        runs.append(current)
    return runs


def fetch_street_lines(streets: list[str], cache: dict, boundary: dict | None = None) -> None:
    """Populate cache[street] with a list of polylines ([[lat, lon], ...]) from
    OSM way geometry, batching many streets per Overpass request. cache[street]
    is set to [] when the street has no matching highway way in OSM. A batch
    that fails after retries is skipped (left unset) so those streets fall
    back to point-geocoding instead of aborting the whole run.

    The Overpass bbox query overlaps neighboring cities (e.g. Ra'anana,
    Ramat HaSharon), so every returned way is clipped to only the portion(s)
    that actually fall inside Herzliya's real boundary (see
    clip_polyline_to_boundary) — a same-named street, or a single way that
    continues across the border, would otherwise leak placements into the
    neighboring city."""
    pending = [s for s in streets if s not in cache]
    if not pending:
        return

    batch_size = 20
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        clauses = "\n".join(
            f'  way["highway"]["name"="{escape_overpass(s)}"]({HERZLIYA_BBOX});' for s in batch
        )
        query = f"[out:json][timeout:60];\n(\n{clauses}\n);\nout geom;"
        print(f"  Overpass batch [{start + 1}-{start + len(batch)}/{len(pending)}]")
        try:
            data = overpass_query(query)
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            print(f"  batch failed, will use point-fallback for these streets: {exc}")
            time.sleep(3)
            continue

        found: dict[str, list[list[list[float]]]] = defaultdict(list)
        dropped_points = 0
        for el in data.get("elements", []):
            name = el.get("tags", {}).get("name")
            geom = el.get("geometry")
            if not name or not geom or len(geom) < 2:
                continue
            coords = [[pt["lat"], pt["lon"]] for pt in geom]
            clipped_runs = clip_polyline_to_boundary(coords, boundary)
            dropped_points += len(coords) - sum(len(r) for r in clipped_runs)
            found[name].extend(clipped_runs)

        if dropped_points:
            print(f"    clipped {dropped_points} point(s) outside Herzliya boundary")
        for s in batch:
            cache[s] = found.get(s, [])
        save_lines_cache(cache)
        time.sleep(2.0)


def prune_lines_cache_by_boundary(cache: dict, boundary: dict | None) -> int:
    """Re-clip an already-cached lines dict against the boundary (cheap,
    local, no network) — cleans up entries fetched before boundary/clip
    filtering existed, or if boundary was unavailable on a previous run."""
    if boundary is None:
        return 0
    removed = 0
    for street, polylines in list(cache.items()):
        kept: list[list[list[float]]] = []
        for coords in polylines:
            clipped_runs = clip_polyline_to_boundary(coords, boundary)
            removed += len(coords) - sum(len(r) for r in clipped_runs)
            kept.extend(clipped_runs)
        cache[street] = kept
    if removed:
        save_lines_cache(cache)
    return removed


ADDRESS_CACHE_PATH = DATA_DIR / "street-address-cache.json"


def load_address_cache() -> dict:
    if ADDRESS_CACHE_PATH.exists():
        return json.loads(ADDRESS_CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_address_cache(cache: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ADDRESS_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def fetch_street_addresses(
    streets: list[str], cache: dict, boundary: dict | None = None
) -> None:
    """Populate cache[street] with OSM addr:housenumber points ([{lat, lon,
    house}]) for that street name, so deals can be snapped to the *nearest*
    real tagged address as an approximation (the source deal records have no
    house number at all). cache[street] is [] when OSM has no tagged
    addresses for that street name. Points outside Herzliya's real boundary
    are dropped (see fetch_street_lines for why the bbox alone isn't enough)."""
    pending = [s for s in streets if s not in cache]
    if not pending:
        return

    batch_size = 15  # each street contributes 2 clauses (node + way)
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        clauses = "\n".join(
            f'  node["addr:housenumber"]["addr:street"="{escape_overpass(s)}"]({HERZLIYA_BBOX});\n'
            f'  way["addr:housenumber"]["addr:street"="{escape_overpass(s)}"]({HERZLIYA_BBOX});'
            for s in batch
        )
        query = f"[out:json][timeout:60];\n(\n{clauses}\n);\nout center;"
        print(f"  Overpass address batch [{start + 1}-{start + len(batch)}/{len(pending)}]")
        try:
            data = overpass_query(query)
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            print(f"  address batch failed, streets will have no approx house number: {exc}")
            time.sleep(3)
            continue

        found: dict[str, list[dict]] = defaultdict(list)
        dropped_other_city = 0
        for el in data.get("elements", []):
            tags = el.get("tags", {})
            street_name = tags.get("addr:street")
            house = tags.get("addr:housenumber")
            if not street_name or not house:
                continue
            if el.get("type") == "node":
                lat, lon = el.get("lat"), el.get("lon")
            else:
                center = el.get("center") or {}
                lat, lon = center.get("lat"), center.get("lon")
            if lat is None or lon is None:
                continue
            if not point_in_boundary(lat, lon, boundary):
                dropped_other_city += 1
                continue
            found[street_name].append({"lat": lat, "lon": lon, "house": house})

        if dropped_other_city:
            print(f"    dropped {dropped_other_city} address point(s) outside Herzliya boundary")
        for s in batch:
            cache[s] = found.get(s, [])
        save_address_cache(cache)
        time.sleep(2.0)


def prune_address_cache_by_boundary(cache: dict, boundary: dict | None) -> int:
    """Re-filter an already-cached address dict against the boundary (cheap,
    local, no network)."""
    if boundary is None:
        return 0
    removed = 0
    for street, points in list(cache.items()):
        kept = [p for p in points if point_in_boundary(p["lat"], p["lon"], boundary)]
        removed += len(points) - len(kept)
        cache[street] = kept
    if removed:
        save_address_cache(cache)
    return removed


def nearest_house_number(
    addr_points: list[dict], lat: float, lon: float, max_dist_m: float = 120.0
) -> str | None:
    """Nearest tagged OSM house number to (lat, lon), or None if the closest
    one is farther than max_dist_m (i.e. too sparse/unreliable to show)."""
    if not addr_points:
        return None
    best_dist = math.inf
    best_house = None
    for pt in addr_points:
        dist = haversine_m((lat, lon), (pt["lat"], pt["lon"]))
        if dist < best_dist:
            best_dist = dist
            best_house = pt["house"]
    return best_house if best_dist <= max_dist_m else None


ROUTE2_CACHE_PATH = DATA_DIR / "route2-geometry-cache.json"
ROUTE20_CACHE_PATH = DATA_DIR / "route20-geometry-cache.json"


def fetch_route_lines(
    ref: str, cache_path: Path, label: str
) -> list[list[list[float]]] | None:
    """Fetch a numbered route's (e.g. כביש 2, כביש 20) real geometry through
    the Herzliya bbox, used to classify deals by which side of the road
    they're on (e.g. Herzliya Pituach is west of Route 2; a separate
    expensive strip sits west of Route 20)."""
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    query = (
        "[out:json][timeout:60];\n"
        f'way["highway"]["ref"~"^{ref}($|;| )"]({HERZLIYA_BBOX});\n'
        "out geom;"
    )
    try:
        data = overpass_query(query)
    except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
        print(f"  {label} fetch failed, skipping this exclusion: {exc}")
        return None

    lines = []
    for el in data.get("elements", []):
        geom = el.get("geometry")
        if geom and len(geom) >= 2:
            lines.append([[pt["lat"], pt["lon"]] for pt in geom])
    if not lines:
        return None

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
    return lines


def is_west_of_road(lat: float, lon: float, road_lines: list[list[list[float]]]) -> bool:
    """True if (lat, lon) is on the sea side (west, i.e. lower longitude) of
    the nearest segment of the given road geometry.

    Uses latitude-interpolated road longitude (direction-invariant: doesn't
    matter which way each OSM way segment was drawn) rather than a
    cross-product sign, since these roads run roughly north-south here and
    are each stitched from many independently-directed way fragments.
    """
    best_dist = math.inf
    best_segment = None
    for coords in road_lines:
        for a, b in zip(coords, coords[1:]):
            lat1, lon1 = a
            lat2, lon2 = b
            mlat = math.cos(math.radians((lat1 + lat2) / 2))
            ax, ay = lon1 * mlat, lat1
            bx, by = lon2 * mlat, lat2
            px, py = lon * mlat, lat
            dx, dy = bx - ax, by - ay
            seg_len2 = dx * dx + dy * dy
            if seg_len2 == 0:
                continue
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))
            cx, cy = ax + t * dx, ay + t * dy
            dist = math.hypot(px - cx, py - cy)
            if dist < best_dist:
                best_dist = dist
                best_segment = (lat1, lon1, lat2, lon2)

    if best_segment is None:
        return False

    lat1, lon1, lat2, lon2 = best_segment
    if lat2 == lat1:
        road_lon = (lon1 + lon2) / 2
    else:
        frac = (lat - lat1) / (lat2 - lat1)
        road_lon = lon1 + frac * (lon2 - lon1)
    return lon < road_lon


def place_on_street(lines: list[list[list[float]]], seed: str) -> tuple[float, float] | None:
    """Pick a point along real street geometry, weighted by segment length,
    with a small perpendicular offset so dots sit beside the road, not on it."""
    segments = []  # (coords, cumulative_lengths, total_length)
    for coords in lines:
        cum = [0.0]
        for a, b in zip(coords, coords[1:]):
            cum.append(cum[-1] + haversine_m(tuple(a), tuple(b)))
        if cum[-1] > 0:
            segments.append((coords, cum, cum[-1]))
    if not segments:
        return None

    r_seg, r_frac, r_side, r_offset = seed_floats(seed, 4)

    total = sum(s[2] for s in segments)
    target = r_seg * total
    acc = 0.0
    chosen = segments[0]
    for seg in segments:
        if acc + seg[2] >= target or seg is segments[-1]:
            chosen = seg
            break
        acc += seg[2]

    coords, cum, total_len = chosen
    target_len = r_frac * total_len
    idx = 0
    for i in range(len(cum) - 1):
        if cum[i] <= target_len <= cum[i + 1]:
            idx = i
            break
    seg_len = cum[idx + 1] - cum[idx] or 1.0
    t = (target_len - cum[idx]) / seg_len
    lat1, lon1 = coords[idx]
    lat2, lon2 = coords[idx + 1]
    lat = lat1 + (lat2 - lat1) * t
    lon = lon1 + (lon2 - lon1) * t

    # Perpendicular offset (~4-14m) so dots sit beside the street, alternating sides.
    bearing = math.atan2(lon2 - lon1, lat2 - lat1)
    perp = bearing + math.pi / 2
    side = 1.0 if r_side >= 0.5 else -1.0
    offset_m = 4.0 + r_offset * 10.0
    dlat = (side * offset_m * math.cos(perp)) / 111_320
    dlon = (side * offset_m * math.sin(perp)) / (111_320 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def percentile_scores(values: list[float]) -> list[int]:
    """Map values to 1-100 via average percentile rank (ties share score)."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [50]
    order = sorted(range(n), key=lambda i: values[i])
    scores = [0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        # average rank of the tie group, 0-based
        avg_rank = (i + j) / 2.0
        pct = avg_rank / (n - 1)
        score = int(round(1 + pct * 99))
        score = max(1, min(100, score))
        for k in range(i, j + 1):
            scores[order[k]] = score
        i = j + 1
    return scores


def year_normalize(deals: list[dict]) -> None:
    by_year: dict[int, list[int]] = defaultdict(list)
    for idx, deal in enumerate(deals):
        by_year[deal["year"]].append(idx)

    for year, indices in by_year.items():
        totals = [deals[i]["price"] for i in indices]
        ppsms = [deals[i]["pricePerSqm"] for i in indices]
        total_scores = percentile_scores(totals)
        ppsm_scores = percentile_scores(ppsms)
        for local_i, deal_i in enumerate(indices):
            deals[deal_i]["scoreTotal"] = total_scores[local_i]
            deals[deal_i]["scorePerSqm"] = ppsm_scores[local_i]


def build_address(record: dict) -> str:
    street = (record.get("street") or "").strip()
    city = (record.get("city_name") or CITY_NAME).strip()
    display = (record.get("displayaddress") or record.get("fulladdress") or "").strip()
    if display:
        return display
    if street:
        return f"{street}, {city}"
    return city


def is_residential(desc) -> bool:
    if desc is None:
        return False
    return str(desc).strip() in RESIDENTIAL


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = START_DATE
    print(f"Fetching {CITY_NAME} deals since {cutoff.date()}…")

    raw_records: list[dict] = []
    for rid in RESOURCE_IDS:
        raw_records.extend(fetch_city_records(rid))
    print(f"Raw city records: {len(raw_records)}")

    # Dedupe by keyvalue (prefer newer row_num if present)
    by_key: dict[str, dict] = {}
    for rec in raw_records:
        key = str(rec.get("keyvalue") or rec.get("_id"))
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = rec
            continue
        try:
            if int(rec.get("row_num") or 0) >= int(prev.get("row_num") or 0):
                by_key[key] = rec
        except ValueError:
            by_key[key] = rec
    print(f"Unique deals: {len(by_key)}")

    candidates = []
    streets_needed: set[str] = set()
    for rec in by_key.values():
        if not is_residential(rec.get("dealnaturedescription")):
            continue
        deal_dt = parse_date(rec)
        if deal_dt is None or deal_dt < cutoff:
            continue
        price = parse_price(rec.get("dealamount"))
        area = parse_area(rec.get("dealnature"))
        street = (rec.get("street") or "").strip()
        if price is None or area is None or not street:
            continue
        candidates.append((rec, deal_dt, price, area, street))
        streets_needed.add(street)

    print(f"Residential + dated + priced candidates: {len(candidates)}")
    print(f"Unique streets to place: {len(streets_needed)}")

    print("Fetching Herzliya's real administrative boundary…")
    boundary = fetch_herzliya_boundary()
    if boundary:
        print(f"  boundary type: {boundary['type']}")
    else:
        print("  boundary unavailable — cross-city name collisions won't be filtered this run")

    print("Fetching real street geometry from Overpass…")
    lines_cache = load_lines_cache()
    pruned_lines = prune_lines_cache_by_boundary(lines_cache, boundary)
    if pruned_lines:
        print(f"  clipped {pruned_lines} cached point(s) outside Herzliya (stale/other-city)")
    fetch_street_lines(sorted(streets_needed), lines_cache, boundary)
    streets_with_lines = sum(1 for s in streets_needed if lines_cache.get(s))
    print(f"Streets with OSM geometry: {streets_with_lines}/{len(streets_needed)}")

    # Fallback point-geocode only for streets Overpass couldn't find.
    fallback_streets = sorted(s for s in streets_needed if not lines_cache.get(s))
    geo_cache = load_geocode_cache()
    pruned_geo = 0
    for street, val in list(geo_cache.items()):
        if val and not point_in_boundary(val["lat"], val["lon"], boundary):
            geo_cache[street] = None
            pruned_geo += 1
    if pruned_geo:
        save_geocode_cache(geo_cache)
        print(f"  pruned {pruned_geo} cached fallback point(s) outside Herzliya (stale/other-city)")
    fallback_coords: dict[str, tuple[float, float]] = {}
    if fallback_streets:
        print(f"Falling back to point geocoding for {len(fallback_streets)} streets…")
    for i, street in enumerate(fallback_streets):
        if street in geo_cache and geo_cache[street]:
            fallback_coords[street] = (geo_cache[street]["lat"], geo_cache[street]["lon"])
            continue
        if street in geo_cache and geo_cache[street] is None:
            continue
        print(f"  geocoding [{i + 1}/{len(fallback_streets)}] {street}")
        coords = geocode_street(street, geo_cache, boundary)
        if coords:
            fallback_coords[street] = coords

    print("Fetching OSM tagged house numbers (for approximate addresses)…")
    address_cache = load_address_cache()
    pruned_addr = prune_address_cache_by_boundary(address_cache, boundary)
    if pruned_addr:
        print(f"  pruned {pruned_addr} cached address point(s) outside Herzliya (stale/other-city)")
    fetch_street_addresses(sorted(streets_needed), address_cache, boundary)
    streets_with_addresses = sum(1 for s in streets_needed if address_cache.get(s))
    print(f"Streets with OSM address points: {streets_with_addresses}/{len(streets_needed)}")

    print("Fetching Route 2 geometry (to exclude Herzliya Pituach)…")
    route2_lines = fetch_route_lines("2", ROUTE2_CACHE_PATH, "Route 2")
    if route2_lines:
        print(f"  Route 2: {len(route2_lines)} way segments")
    else:
        print("  Route 2 geometry unavailable — Pituach exclusion disabled for this run")

    print("Fetching Route 20 geometry (to exclude the western strip beyond it)…")
    route20_lines = fetch_route_lines("20", ROUTE20_CACHE_PATH, "Route 20")
    if route20_lines:
        print(f"  Route 20: {len(route20_lines)} way segments")
    else:
        print("  Route 20 geometry unavailable — that exclusion disabled for this run")

    deals: list[dict] = []
    skipped_geo = 0
    skipped_pituach = 0
    skipped_west_of_20 = 0
    skipped_wrong_city = 0
    placed_on_line = 0
    placed_fallback = 0
    houses_matched = 0
    for rec, deal_dt, price, area, street in candidates:
        deal_id = str(rec.get("keyvalue") or rec.get("_id"))
        lines = lines_cache.get(street)
        placed = place_on_street(lines, deal_id) if lines else None
        if placed:
            lat, lon = placed
            placed_on_line += 1
        elif street in fallback_coords:
            lat, lon = jitter(*fallback_coords[street], deal_id)
            placed_fallback += 1
        else:
            skipped_geo += 1
            continue

        # Final safety net: a perpendicular offset or fallback jitter near the
        # city edge could in principle push a point just outside the real
        # boundary even though its source line/point was inside.
        if not point_in_boundary(lat, lon, boundary):
            skipped_wrong_city += 1
            continue

        if route2_lines and is_west_of_road(lat, lon, route2_lines):
            skipped_pituach += 1
            continue

        if route20_lines and is_west_of_road(lat, lon, route20_lines):
            skipped_west_of_20 += 1
            continue

        gush = str(rec.get("gush") or "")
        parts = re.split(r"[-/]", gush)
        house_approx = nearest_house_number(address_cache.get(street), lat, lon)
        if house_approx:
            houses_matched += 1
        deals.append(
            {
                "id": deal_id,
                "date": deal_dt.date().isoformat(),
                "year": deal_dt.year,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "address": build_address(rec),
                "street": street,
                "houseNumberApprox": house_approx,
                "price": int(round(price)),
                "areaSqm": round(area, 1),
                "pricePerSqm": int(round(price / area)),
                "rooms": parse_rooms(rec.get("assetroomno")),
                "floor": (rec.get("floorno") or None),
                "propertyType": rec.get("dealnaturedescription"),
                "gush": parts[0] if parts else None,
                "helka": parts[1] if len(parts) > 1 else None,
            }
        )

    print(
        f"Placed deals: {len(deals)} (on street geometry: {placed_on_line}, "
        f"fallback point: {placed_fallback}, skipped no-geo: {skipped_geo}, "
        f"skipped wrong-city: {skipped_wrong_city}, "
        f"approx house number matched: {houses_matched}/{len(deals)}, "
        f"excluded Herzliya Pituach: {skipped_pituach}, "
        f"excluded west of Route 20: {skipped_west_of_20})"
    )
    if not deals:
        raise SystemExit("No deals left after filtering/geocoding")

    year_normalize(deals)
    deals.sort(key=lambda d: d["date"], reverse=True)

    years = sorted({d["year"] for d in deals})
    payload = {
        "meta": {
            "city": "הרצליה",
            "citySourceName": CITY_NAME,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "years": [years[0], years[-1]],
            "dealCount": len(deals),
            "source": "odata.org.il nadlan mirror + OSM/Overpass street geometry",
            "excludedHerzliyaPituach": skipped_pituach,
            "excludedWestOfRoute20": skipped_west_of_20,
            "excludedWrongCity": skipped_wrong_city,
            "notes": (
                "Deals have no house number, so each is placed at a random point "
                "along its street's real OSM geometry (weighted by segment length) "
                "with a small perpendicular offset; streets missing from OSM fall "
                "back to a single geocoded point with wider jitter. All placement "
                "sources are filtered against Herzliya's real administrative "
                "boundary (not just a bounding box) to reject same-named streets "
                "that actually belong to a neighboring city like Ra'anana. Herzliya "
                "Pituach (west of Route 2) and the separate expensive strip west of "
                "Route 20 are both excluded entirely so their high prices don't skew "
                "the year-normalized percentile scores. Scores "
                "are percentile ranks 1-100 within each calendar year. The "
                "underlying registry data currently lags roughly a year or more "
                "behind the present, so the most recent 1-2 years are typically "
                "thin or absent. houseNumberApprox is the nearest real OSM-tagged "
                "house number on the same street (within 120m) — it is NOT the "
                "deal's actual registered address, just a plausible nearby number "
                "for display purposes."
            ),
        },
        "deals": deals,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({OUT_PATH.stat().st_size // 1024} KB, {len(deals)} deals)")

    # Spot-check one year
    sample_year = years[-1]
    year_deals = [d for d in deals if d["year"] == sample_year]
    by_total = sorted(year_deals, key=lambda d: d["price"])
    print(
        f"Spot-check {sample_year}: cheapest scoreTotal={by_total[0]['scoreTotal']} "
        f"price={by_total[0]['price']}; dearest scoreTotal={by_total[-1]['scoreTotal']} "
        f"price={by_total[-1]['price']}"
    )


if __name__ == "__main__":
    main()
