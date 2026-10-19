"""Match Strava activity GPS tracks against an offline OSM peak database.

Replaces the summitbag integration: peaks are identified purely by horizontal
distance between track samples and peak coordinates, never by title guessing.

Sport-type rules (enter radius / sparse sample cap / dense sample cap):
  TrailRun, Hike: 100 m / 180 / 1200
  Run, Walk:       50 m / 120 / 900
Other types (Ride, EBikeRide, ...) do not participate.
"""

import math
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PEAK_DIR = ROOT / "peaks"
CHINA_DB = PEAK_DIR / "RunLogChinaPeaks.sqlite"
GLOBAL_DB = PEAK_DIR / "RunLogGlobalPeaks.sqlite"

# sport_type -> (enter_radius_m, sparse_cap, dense_cap)
SPORT_RULES = {
    "TrailRun": (100, 180, 1200),
    "Hike": (100, 180, 1200),
    "Run": (50, 120, 900),
    "Walk": (50, 120, 900),
}
EXIT_RADIUS_M = 1000

# Names that mean "no usable name"; such peaks are never displayed.
BAD_NAMES = frozenset({"", "?????", "未命名山峰", "未命名", "Unnamed", "Unnamed Peak"})

# Lines belonging to the old summitbag integration; removed, never re-added.
SUMMITBAG_LINK = re.compile(
    r"(?:🌐\s*)?(?:https?://)?(?:www\.)?summitbag\.com(?:[/?#]\S*)?", re.I)
YEAR_CLIMB = re.compile(r"年\s*(?:累计\s*)?爬升|年度爬升|total\s*climb", re.I)


def haversine_m(lat1, lon1, lat2, lon2):
    """Horizontal distance in meters between two WGS84 points."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = rlat2 - rlat1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(min(1.0, math.sqrt(a)))


def valid_point(point):
    return (isinstance(point, (list, tuple)) and len(point) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) for v in point)
            and -90 <= point[0] <= 90 and -180 <= point[1] <= 180)


def uniform_sample(points, cap):
    """Uniform index sampling, keeping first and last points."""
    n = len(points)
    if n <= cap:
        return list(points)
    if cap <= 1:
        return [points[0]]
    step = (n - 1) / (cap - 1)
    indices = {0, n - 1}
    indices.update(min(n - 1, int(round(i * step))) for i in range(cap))
    return [points[i] for i in sorted(indices)]


def clean_name(name):
    name = (name or "").strip()
    return "" if name in BAD_NAMES else name


def candidate_peaks(sparse, enter_radius_m, db_paths=None):
    """Peaks inside the sample bounding box expanded by the enter radius."""
    lats = [p[0] for p in sparse]
    lons = [p[1] for p in sparse]
    # meters -> degrees (conservative: use latitude for longitude scale)
    lat_pad = enter_radius_m / 111320.0
    lon_pad = enter_radius_m / (111320.0 * max(0.2, math.cos(math.radians(sum(lats) / len(lats)))))
    box = (min(lats) - lat_pad, max(lats) + lat_pad,
           min(lons) - lon_pad, max(lons) + lon_pad)
    paths = db_paths or [p for p in (CHINA_DB, GLOBAL_DB) if p.exists()]
    found = {}
    for path in paths:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            for row in conn.execute(
                    "SELECT id, name, elevation_m, lat, lon FROM osm_peak "
                    "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                    box):
                pid = row[0]
                if pid not in found:
                    found[pid] = {"id": pid, "name": row[1], "elevation_m": row[2],
                                  "lat": row[3], "lon": row[4]}
        finally:
            conn.close()
    return list(found.values())


def count_passes(dense, peak, enter_radius_m):
    """Enter/exit-radius state machine over the dense samples.

    First entry counts 1; only after the track reaches EXIT_RADIUS_M away
    may a re-entry count again.
    """
    count, inside = 0, False
    for lat, lon in dense:
        dist = haversine_m(lat, lon, peak["lat"], peak["lon"])
        if not inside and dist <= enter_radius_m:
            inside = True
            count += 1
        elif inside and dist >= EXIT_RADIUS_M:
            inside = False
    return count


def match_peaks(points, sport_type, db_paths=None):
    """Return [(name, elevation_m_or_None, pass_count)] for matched peaks."""
    rule = SPORT_RULES.get(sport_type)
    if rule is None:
        return []
    enter_radius_m, sparse_cap, dense_cap = rule
    clean = [tuple(p) for p in points if valid_point(p)]
    if len(clean) < 2:
        return []
    sparse = uniform_sample(clean, sparse_cap)
    dense = uniform_sample(clean, dense_cap)
    matched = []
    for peak in candidate_peaks(sparse, enter_radius_m, db_paths):
        name = clean_name(peak["name"])
        if not name:
            continue
        d_min = min(haversine_m(lat, lon, peak["lat"], peak["lon"]) for lat, lon in sparse)
        if d_min > enter_radius_m:
            continue
        # Dense samples must confirm at least one entry into the same radius.
        if not any(haversine_m(lat, lon, peak["lat"], peak["lon"]) <= enter_radius_m
                   for lat, lon in dense):
            continue
        passes = count_passes(dense, peak, enter_radius_m)
        if passes:
            matched.append((name, peak["elevation_m"], passes))
    # Stable order: most passes first, then by name.
    matched.sort(key=lambda m: (-m[2], m[0]))
    return matched


def format_peak_line(matched):
    """One-line peak description, e.g. ⛰️ 大阳台 (587 m) ×2 • ⛰️ 小阳台."""
    parts = []
    for name, elevation, passes in matched:
        text = f"⛰️ {name}"
        if isinstance(elevation, (int, float)) and not isinstance(elevation, bool) \
                and math.isfinite(elevation):
            text += f" ({elevation:g} m)"
        if passes >= 2:
            text += f" ×{passes}"
        parts.append(text)
    return " • ".join(parts)


def is_summitbag_line(text):
    """A line belonging to the retired summitbag integration."""
    stripped = text.strip()
    if not stripped:
        return False
    if SUMMITBAG_LINK.search(stripped) or YEAR_CLIMB.search(stripped):
        return True
    elevation = re.search(r"[（(]\s*\d[\d,.]*\s*(?:m|米)\s*[）)]", stripped)
    return bool(elevation and stripped.startswith(("⛰", "🏔")))


def update_peak_line(description, peak_line):
    """Insert/replace the peak line before the weather block.

    - User notes stay first, untouched.
    - Old summitbag lines (links, year-climb stats, stale peak lines) removed.
    - New peak line goes right before the weather block; without weather it
      goes after the user notes.
    - Weather block and everything else kept verbatim.
    - Idempotent: same peak_line twice does not duplicate.
    """
    from weather_template import weather_spans  # deferred: keeps import light
    description = description or ""
    newline = "\r\n" if "\r\n" in description else "\n"
    trailing_nl = newline if description.endswith(("\n", "\r")) else ""

    # 1) Drop old summitbag lines and any previous peak line we generated.
    kept_lines = []
    for line in description.splitlines(keepends=True):
        text = line.strip()
        if is_summitbag_line(text):
            continue
        if peak_line and text == peak_line.strip():
            continue  # will be re-inserted at the right place
        kept_lines.append(line)
    body = "".join(kept_lines)

    if not peak_line:
        return body.rstrip("\r\n") + trailing_nl

    # 2) Recompute weather position on the filtered text.
    spans = weather_spans(body)
    peak_with_nl = peak_line.strip() + newline
    if not spans:
        return body.rstrip("\r\n") + newline + peak_with_nl.rstrip("\r\n") + trailing_nl

    # 3) Insert right before the weather block.
    weather_start = spans[0][0]
    head, tail = body[:weather_start].rstrip("\r\n"), body[weather_start:]
    text = head + newline + peak_with_nl + tail
    text = re.sub(r"(\r?\n){3,}", newline * 2, text)
    return text.rstrip("\r\n") + trailing_nl if not tail.endswith(("\n", "\r")) else text
