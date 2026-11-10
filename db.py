"""Settings and database helpers shared by every other file.

Every setting can be overridden with a DSW_* environment variable, so the same code works with the
portable database (port 5433, no password) or any other PostgreSQL server that has PostGIS.
"""
from __future__ import annotations

import logging
import os
import random
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import psycopg2
import psycopg2.extras

BASE_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Database connection.
DB_HOST = _env("DSW_DB_HOST", "localhost")
DB_PORT = int(_env("DSW_DB_PORT", "5433"))
DB_NAME = _env("DSW_DB_NAME", "darkshipwatch")

# Three logins show least privilege (they are created in database.sql, part 3):
# owner can do everything, ingest can only add data, analyst can only read.
DB_ROLES = {
    "owner": {"user": _env("DSW_DB_USER", "postgres"), "password": _env("DSW_DB_PASSWORD", "")},
    "ingest": {"user": _env("DSW_INGEST_USER", "dsw_ingest"), "password": _env("DSW_INGEST_PASSWORD", "dsw_ingest_pw")},
    "analyst": {"user": _env("DSW_ANALYST_USER", "dsw_analyst"), "password": _env("DSW_ANALYST_PASSWORD", "dsw_analyst_pw")},
}
# Setting DSW_RBAC=0 makes everything connect as the owner.
USE_RBAC = _env("DSW_RBAC", "1") not in ("0", "false", "False", "no")

DB_CONNECT_RETRIES = 6
DB_CONNECT_BASE_DELAY_S = 1.0          # wait 1, 2, 4, 8 ... seconds between tries

# Area we watch: positions outside this box are dropped before storing.
BBOX = {"min_lat": 59.0, "max_lat": 60.8, "min_lon": 21.0, "max_lon": 30.5}

# Free data sources, none of them needs an API key.
USER_AGENT = "DarkShipWatch/1.0 (academic ADBMS project)"
HTTP_HEADERS = {"User-Agent": USER_AGENT, "Digitraffic-User": "DarkShipWatch/1.0", "Accept-Encoding": "gzip"}

DIGITRAFFIC_BASE = "https://meri.digitraffic.fi"
API = {
    "locations": f"{DIGITRAFFIC_BASE}/api/ais/v1/locations",      # position of every ship
    "vessels": f"{DIGITRAFFIC_BASE}/api/ais/v1/vessels",          # name, destination, ETA and so on
    "sea_state": f"{DIGITRAFFIC_BASE}/api/sse/v1/measurements",   # sea state estimates
    "ports": f"{DIGITRAFFIC_BASE}/api/port-call/v1/ports",
    "fmi_wfs": "https://opendata.fmi.fi/wfs",                     # Finnish Meteorological Institute
}
FMI_WEATHER_QUERY = "fmi::observations::weather::simple"          # wind speed, gust, direction
FMI_WAVE_QUERY = "fmi::observations::wave::simple"                # wave height

# How often each feed is polled, in seconds.
POLL_INTERVALS = {"positions": 60, "vessels": 600, "weather": 600, "sea_state": 1800, "engine": 300}
HTTP_TIMEOUT_S = (10, 60)               # connect, read
HTTP_RETRIES = 4
HTTP_BACKOFF_BASE_S = 2.0               # wait 2, 4, 8, 16 seconds between retries

# Limits used by the SQL functions. setup_db.py copies them into the pipeline_config table.
THRESHOLDS = {
    "bbox_min_lat": BBOX["min_lat"],
    "bbox_max_lat": BBOX["max_lat"],
    "bbox_min_lon": BBOX["min_lon"],
    "bbox_max_lon": BBOX["max_lon"],
    "max_speed_knots": 60.0,            # a reported speed above this is rejected
    "max_jump_knots": 50.0,             # a jump between two fixes faster than this is rejected
    "min_jump_distance_m": 500.0,
    "gap_threshold_min": 20.0,          # silence longer than this counts as a gap
    "gap_lookback_hours": 6.0,          # older open gaps mean the ship left the area
    "gap_max_hours": 12.0,              # a closed gap longer than this is not dark activity
    "neighbour_radius_m": 15000.0,      # radius for the receiver outage test
}

# Settings for the possible-area calculation.
KNOT_M_PER_S = 1852.0 / 3600.0          # one knot in metres per second
CIRCLE_SEGMENTS_PER_QUARTER = 48        # smoothness of the reach circles
ELLIPSE_POINTS = 144

# Land map (Natural Earth 10m land, public domain).
NATURAL_EARTH_LAND_URL = "https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_land.zip"
LAND_CLIP_BOX = (19.0, 57.5, 32.0, 61.5)  # lon_min, lat_min, lon_max, lat_max

# Web app.
WEB_HOST = _env("DSW_WEB_HOST", "127.0.0.1")
WEB_PORT = int(_env("DSW_WEB_PORT", "8000"))

# Demo ships use this MMSI range (799 is not a real country code) and are flagged is_synthetic.
SYNTHETIC_MMSI_MIN = 799000000
SYNTHETIC_MMSI_MAX = 799999999

log = logging.getLogger("dsw.db")


def retry(fn: Callable[[], Any], attempts: int, base_delay: float, exceptions: tuple, what: str = "operation"):
    """Run fn(). If it fails, wait base_delay * 2**n (plus a little jitter) and try again."""
    last: Exception | None = None
    for n in range(attempts):
        try:
            return fn()
        except exceptions as exc:
            last = exc
            if n == attempts - 1:
                break
            delay = base_delay * (2 ** n) + random.uniform(0, 0.5)
            log.warning("%s failed (%s); retry %d/%d in %.1fs", what, exc, n + 1, attempts - 1, delay)
            time.sleep(delay)
    assert last is not None
    raise last


def connect(role: str = "owner", autocommit: bool = False, app_name: str = "darkshipwatch"):
    """Open a connection as 'owner', 'ingest' or 'analyst', retrying with backoff if the server is busy."""
    effective = role if USE_RBAC else "owner"
    creds = DB_ROLES[effective]

    def _open():
        kwargs = dict(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=creds["user"],
                      connect_timeout=10, application_name=f"{app_name}:{effective}", options="-c timezone=UTC")
        if creds["password"]:
            kwargs["password"] = creds["password"]
        conn = psycopg2.connect(**kwargs)
        conn.autocommit = autocommit
        return conn

    return retry(_open, DB_CONNECT_RETRIES, DB_CONNECT_BASE_DELAY_S,
                 (psycopg2.OperationalError,), what=f"connect as {creds['user']}")


def is_alive(conn) -> bool:
    try:
        if conn is None or conn.closed:
            return False
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.rollback()
        return True
    except psycopg2.Error:
        return False


def query(conn, sql: str, params: Sequence | dict | None = None) -> list[dict]:
    """Run a SELECT and return the rows as dicts."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
    return rows


def scalar(conn, sql: str, params: Sequence | dict | None = None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def execute(conn, sql: str, params: Sequence | dict | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def _fmt(value: Any, max_width: int) -> str:
    if value is None:
        text = "-"
    elif isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, datetime):
        text = value.strftime("%Y-%m-%d %H:%M:%S")
    elif isinstance(value, date):
        text = value.isoformat()
    elif isinstance(value, (float, Decimal)):
        text = f"{float(value):,.2f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= max_width else text[: max_width - 3] + "..."


def format_table(rows: Iterable[dict], columns: Sequence[str] | None = None, max_width: int = 44) -> str:
    rows = list(rows)
    if not rows:
        return "  (no rows)"
    columns = list(columns or rows[0].keys())
    cells = [[_fmt(r.get(c), max_width) for c in columns] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(columns)]
    line = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    out = [line, "| " + " | ".join(c.ljust(w) for c, w in zip(columns, widths)) + " |", line]
    out += ["| " + " | ".join(v.ljust(w) for v, w in zip(row, widths)) + " |" for row in cells]
    out.append(line)
    return "\n".join(out)


def print_table(rows: Iterable[dict], columns: Sequence[str] | None = None, max_width: int = 44, indent: str = "") -> None:
    for ln in format_table(rows, columns, max_width).splitlines():
        print(indent + ln)
