-- Dark Ship Watch: the whole database in one file.
--
--   Part 1  Tables      what is stored: tables, rules on the data, indexes, starting data, views
--   Part 2  Logic       what the database does by itself: checks, triggers, silence detection, weather slowdown
--   Part 3  DBA         logins and permissions, monitoring views, test data for the index experiment
--   Demo    after the "@@DEMO" marker: a demo to run by hand in psql (setup_db.py does not run it)
--
-- Main track is B (Mobile): location_events and latest_position.
-- Bonus tracks: A (Temporal) with vessel_status_history, and C (GIS) with ports, land and the possible areas.
-- The file can be run any number of times without harm. setup_db.py applies it.


-- PART 1: TABLES

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS btree_gist;      -- needed for the EXCLUDE constraint

-- 1.0 Settings: every limit used by the SQL functions lives here
CREATE TABLE IF NOT EXISTS pipeline_config (
    cfg_key      text PRIMARY KEY,
    cfg_value    numeric NOT NULL,
    description  text    NOT NULL
);

INSERT INTO pipeline_config (cfg_key, cfg_value, description) VALUES
    ('bbox_min_lat',          59.0,   'Gulf of Finland area of interest: southern edge (deg)'),
    ('bbox_max_lat',          60.8,   'Gulf of Finland area of interest: northern edge (deg)'),
    ('bbox_min_lon',          21.0,   'Gulf of Finland area of interest: western edge (deg)'),
    ('bbox_max_lon',          30.5,   'Gulf of Finland area of interest: eastern edge (deg)'),
    ('max_speed_knots',       60.0,   'Reported speed above this is physically impossible for a ship -> reject'),
    ('max_jump_knots',        50.0,   'Implied speed between consecutive fixes above this -> IMPOSSIBLE_JUMP'),
    ('min_jump_distance_m',   500.0,  'Ignore the jump test below this distance (GPS jitter on short time deltas)'),
    ('future_tolerance_min',  5.0,    'Timestamps more than this far in the future are rejected'),
    ('gap_threshold_min',     20.0,   'Silence longer than this is a candidate AIS gap'),
    ('gap_lookback_hours',    6.0,    'Open gaps are only raised for vessels last seen within this window (older silence = left coverage)'),
    ('gap_max_hours',         12.0,   'A closed gap longer than this is treated as "left coverage", not dark activity'),
    ('neighbour_radius_m',    15000.0,'Radius used to ask "did nearby ships also go silent?" (receiver-outage test)'),
    ('neighbour_window_min',  15.0,   'Neighbours = ships seen this many minutes before the gap started'),
    ('outage_min_silent',     1.0,    'Minimum silent neighbours needed to call a gap a receiver outage'),
    ('outage_silent_ratio',   0.5,    'Fraction of neighbours that must be silent to call it a receiver outage'),
    ('stationary_speed_knots',0.5,   'Last fix slower than this = ship was berthed/anchored: switching AIS off there is routine'),
    ('edge_margin_deg',       0.15,   'Last fix this close to the area-of-interest edge = vessel simply left coverage'),
    ('port_exit_margin',      0.10,   'Hysteresis: a vessel only EXITs a port after leaving radius * (1 + margin)')
ON CONFLICT (cfg_key) DO NOTHING;

-- 1.1 Collector heartbeat
CREATE TABLE IF NOT EXISTS collector_heartbeat (
    heartbeat_id   bigserial PRIMARY KEY,
    feed           text        NOT NULL CHECK (feed IN ('positions','vessels','sea_state','weather','ports','engine','manual')),
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    duration_ms    integer     CHECK (duration_ms IS NULL OR duration_ms >= 0),
    status         text        NOT NULL DEFAULT 'OK' CHECK (status IN ('OK','PARTIAL','ERROR')),
    rows_fetched   integer     NOT NULL DEFAULT 0 CHECK (rows_fetched  >= 0),
    rows_ingested  integer     NOT NULL DEFAULT 0 CHECK (rows_ingested >= 0),
    rows_rejected  integer     NOT NULL DEFAULT 0 CHECK (rows_rejected >= 0),
    error_count    integer     NOT NULL DEFAULT 0 CHECK (error_count   >= 0),
    message        text
);
CREATE INDEX IF NOT EXISTS idx_heartbeat_feed_time ON collector_heartbeat (feed, started_at DESC);

-- 1.2 Vessels. Destination and ETA changes are kept in vessel_status_history
CREATE TABLE IF NOT EXISTS vessels (
    mmsi         bigint PRIMARY KEY CHECK (mmsi BETWEEN 1 AND 999999999),
    imo          bigint,
    call_sign    text,
    name         text,
    ship_type    smallint CHECK (ship_type IS NULL OR ship_type BETWEEN 0 AND 99),
    length_m     numeric(7,1) CHECK (length_m  IS NULL OR length_m  >= 0),
    width_m      numeric(6,1) CHECK (width_m   IS NULL OR width_m   >= 0),
    draught_m    numeric(5,1) CHECK (draught_m IS NULL OR draught_m >= 0),
    destination  text,                      -- latest value, older ones are in vessel_status_history
    eta          timestamptz,               -- latest reported value
    status_ts    timestamptz,               -- time of the message that carried destination and eta
    is_synthetic boolean     NOT NULL DEFAULT false,   -- true only for the labelled demo ships
    first_seen   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_vessels_synthetic ON vessels (is_synthetic) WHERE is_synthetic;

-- 1.3 Incoming data: staging is untrusted, location_events and rejected_events are the checked result
CREATE TABLE IF NOT EXISTS staging_ais (               -- no constraints on purpose, it must accept dirty data
    staging_id   bigserial PRIMARY KEY,
    mmsi         bigint,
    lat          double precision,
    lon          double precision,
    sog          double precision,
    cog          double precision,
    heading      integer,
    nav_stat     integer,
    event_time   timestamptz,
    source       text        NOT NULL DEFAULT 'digitraffic',
    ingested_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_staging_event_time ON staging_ais (event_time);

CREATE TABLE IF NOT EXISTS rejection_reasons (
    code         text PRIMARY KEY,
    description  text NOT NULL
);
INSERT INTO rejection_reasons (code, description) VALUES
    ('NULL_MMSI',           'MMSI missing'),
    ('INVALID_MMSI',        'MMSI outside the 200000000-799999999 ship range'),
    ('MISSING_FIELDS',      'Timestamp or coordinates missing'),
    ('INVALID_COORDINATES', 'Latitude outside -90..90 or longitude outside -180..180'),
    ('OUT_OF_AREA',         'Position outside the Gulf of Finland bounding box'),
    ('UNREALISTIC_SPEED',   'Reported speed over ground negative or above the physical limit'),
    ('FUTURE_TIMESTAMP',    'Timestamp further in the future than the clock-skew tolerance'),
    ('DUPLICATE',           'A fix for this MMSI at this exact time is already stored'),
    ('IMPOSSIBLE_JUMP',     'Implied speed from the previous fix exceeds the kinematic limit')
ON CONFLICT (code) DO NOTHING;

CREATE TABLE IF NOT EXISTS rejected_events (
    rejected_id      bigserial PRIMARY KEY,
    mmsi             bigint,
    lat              double precision,
    lon              double precision,
    sog              double precision,
    cog              double precision,
    heading          integer,
    nav_stat         integer,
    event_time       timestamptz,
    source           text NOT NULL DEFAULT 'digitraffic',
    rejection_reason text NOT NULL REFERENCES rejection_reasons (code),
    detail           text,
    rejected_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rejected_at     ON rejected_events (rejected_at DESC);
CREATE INDEX IF NOT EXISTS idx_rejected_reason ON rejected_events (rejection_reason);
CREATE INDEX IF NOT EXISTS idx_rejected_mmsi   ON rejected_events (mmsi) WHERE mmsi IS NOT NULL;

-- 1.4 Track B (Mobile): movement history and latest position
CREATE TABLE IF NOT EXISTS location_events (
    event_id     bigserial PRIMARY KEY,
    mmsi         bigint      NOT NULL REFERENCES vessels (mmsi) ON DELETE CASCADE,
    geom         geometry(Point, 4326) NOT NULL,
    speed_knots  numeric(5,1) CHECK (speed_knots IS NULL OR speed_knots >= 0),
    course       numeric(5,1) CHECK (course  IS NULL OR course  BETWEEN 0 AND 360),
    heading      smallint     CHECK (heading IS NULL OR heading BETWEEN 0 AND 511),
    nav_stat     smallint,
    event_time   timestamptz NOT NULL,
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_location_mmsi_time UNIQUE (mmsi, event_time),
    CONSTRAINT ck_location_latlon CHECK (ST_Y(geom) BETWEEN -90 AND 90 AND ST_X(geom) BETWEEN -180 AND 180)
);
CREATE INDEX IF NOT EXISTS idx_location_event_time ON location_events (event_time);
CREATE INDEX IF NOT EXISTS gist_location_geom      ON location_events USING gist (geom);
CREATE INDEX IF NOT EXISTS gist_location_geog      ON location_events USING gist ((geom::geography));

CREATE TABLE IF NOT EXISTS latest_position (           -- one row per ship, updated all the time
    mmsi         bigint PRIMARY KEY REFERENCES vessels (mmsi) ON DELETE CASCADE,
    geom         geometry(Point, 4326) NOT NULL,
    speed_knots  numeric(5,1) CHECK (speed_knots IS NULL OR speed_knots >= 0),
    course       numeric(5,1),
    heading      smallint,
    nav_stat     smallint,
    last_seen    timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
-- This table is updated very often, so leave free space in each page and vacuum early.
ALTER TABLE latest_position SET (fillfactor = 70, autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02);
CREATE INDEX IF NOT EXISTS idx_latest_last_seen ON latest_position (last_seen);
CREATE INDEX IF NOT EXISTS gist_latest_geom     ON latest_position USING gist (geom);
CREATE INDEX IF NOT EXISTS gist_latest_geog     ON latest_position USING gist ((geom::geography));

CREATE TABLE IF NOT EXISTS location_events_archive (   -- old fixes are moved here by archive_old_locations()
    LIKE location_events INCLUDING DEFAULTS
);
CREATE INDEX IF NOT EXISTS idx_archive_mmsi_time ON location_events_archive (mmsi, event_time);

-- 1.5 Track C (GIS): ports, port events and land
CREATE TABLE IF NOT EXISTS ports (
    port_id           serial PRIMARY KEY,
    locode            text UNIQUE NOT NULL,
    name              text NOT NULL,
    country           text NOT NULL,
    geom              geometry(Point, 4326) NOT NULL,
    buffer_radius_m   integer NOT NULL DEFAULT 3000 CHECK (buffer_radius_m > 0),
    source            text NOT NULL DEFAULT 'hand-entered'
);
CREATE INDEX IF NOT EXISTS gist_ports_geom ON ports USING gist (geom);
CREATE INDEX IF NOT EXISTS gist_ports_geog ON ports USING gist ((geom::geography));

-- The port data source has no coordinates for Helsinki or Hanko, so all ports are entered by hand
-- (approximate harbour centres).
INSERT INTO ports (locode, name, country, geom, buffer_radius_m, source) VALUES
    ('FIHEL',     'Helsinki',            'Finland', ST_SetSRID(ST_MakePoint(24.9360, 60.1610), 4326), 3000, 'hand-entered'),
    ('FITKU',     'Turku',               'Finland', ST_SetSRID(ST_MakePoint(22.2200, 60.4350), 4326), 3000, 'hand-entered'),
    ('EETLL',     'Tallinn',             'Estonia', ST_SetSRID(ST_MakePoint(24.7690, 59.4470), 4326), 3000, 'hand-entered'),
    ('FIKTK',     'Kotka',               'Finland', ST_SetSRID(ST_MakePoint(26.9100, 60.4600), 4326), 3000, 'hand-entered'),
    ('FIHKO',     'Hanko',               'Finland', ST_SetSRID(ST_MakePoint(22.9560, 59.8180), 4326), 3000, 'hand-entered'),
    ('FINLI',     'Naantali',            'Finland', ST_SetSRID(ST_MakePoint(22.0150, 60.4620), 4326), 3000, 'hand-entered'),
    ('FIHEL-VUO', 'Helsinki Vuosaari',   'Finland', ST_SetSRID(ST_MakePoint(25.1450, 60.2090), 4326), 3000, 'hand-entered'),
    ('EETLL-MUU', 'Tallinn Muuga',       'Estonia', ST_SetSRID(ST_MakePoint(24.9700, 59.4980), 4326), 3000, 'hand-entered')
ON CONFLICT (locode) DO NOTHING;

CREATE TABLE IF NOT EXISTS vessel_port_state (         -- which port a ship is inside right now, used to find entries and exits
    mmsi             bigint PRIMARY KEY REFERENCES vessels (mmsi) ON DELETE CASCADE,
    port_id          integer REFERENCES ports (port_id),
    since            timestamptz,
    last_event_time  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS port_events (
    event_id     bigserial PRIMARY KEY,
    mmsi         bigint      NOT NULL REFERENCES vessels (mmsi) ON DELETE CASCADE,
    port_id      integer     NOT NULL REFERENCES ports (port_id),
    event_type   text        NOT NULL CHECK (event_type IN ('ENTRY','EXIT')),
    event_time   timestamptz NOT NULL,
    distance_m   numeric(9,1),
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_port_event UNIQUE (mmsi, port_id, event_type, event_time)
);
CREATE INDEX IF NOT EXISTS idx_port_events_time ON port_events (event_time DESC);

CREATE TABLE IF NOT EXISTS land_polygons (             -- land that is cut out of the possible areas
    land_id   serial PRIMARY KEY,
    name      text,
    source    text NOT NULL,
    geom      geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS gist_land_geom ON land_polygons USING gist (geom);

CREATE TABLE IF NOT EXISTS land_tiles (                -- land split into small pieces so that spatial queries stay fast
    tile_id   serial PRIMARY KEY,
    geom      geometry(Polygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS gist_land_tiles_geom ON land_tiles USING gist (geom);

-- 1.6 Track A (Temporal): destination and ETA history.
-- Valid time (valid_from to valid_to) is when it was true at sea.
-- Recorded time (recorded_at to superseded_at) is when the database knew about it.
CREATE TABLE IF NOT EXISTS vessel_status_history (
    history_id    bigserial PRIMARY KEY,
    mmsi          bigint      NOT NULL REFERENCES vessels (mmsi) ON DELETE CASCADE,
    destination   text,
    eta           timestamptz,
    valid_from    timestamptz NOT NULL,
    valid_to      timestamptz,                               -- empty means still valid
    recorded_at   timestamptz NOT NULL DEFAULT now(),        -- when the row was created
    superseded_at timestamptz,                               -- when the row was closed
    is_current    boolean     NOT NULL DEFAULT true,
    CONSTRAINT ck_status_valid_range  CHECK (valid_to IS NULL OR valid_to > valid_from),
    CONSTRAINT ck_status_current_flag CHECK (is_current = (valid_to IS NULL)),
    -- the database refuses two overlapping versions for one ship
    CONSTRAINT ex_status_no_overlap EXCLUDE USING gist (mmsi WITH =, tstzrange(valid_from, valid_to) WITH &&)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_status_one_current ON vessel_status_history (mmsi) WHERE is_current;
CREATE INDEX IF NOT EXISTS idx_status_mmsi_from ON vessel_status_history (mmsi, valid_from DESC);

-- 1.7 Weather and sea state, and the slowdown tables
CREATE TABLE IF NOT EXISTS sea_conditions (
    obs_id          bigserial PRIMARY KEY,
    source          text        NOT NULL CHECK (source IN ('DIGITRAFFIC_SSE','FMI_WAVE')),
    site_id         text        NOT NULL,
    site_name       text,
    geom            geometry(Point, 4326) NOT NULL,
    observed_at     timestamptz NOT NULL,
    sea_state       text,                                    -- CALM, LIGHT, MODERATE and so on
    wave_height_m   numeric(5,2) CHECK (wave_height_m IS NULL OR wave_height_m >= 0),
    wave_dir_deg    smallint     CHECK (wave_dir_deg  IS NULL OR wave_dir_deg BETWEEN 0 AND 360),
    trend           text,
    collected_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_sea_obs UNIQUE (source, site_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_sea_observed ON sea_conditions (observed_at DESC);
CREATE INDEX IF NOT EXISTS gist_sea_geom    ON sea_conditions USING gist (geom);

CREATE TABLE IF NOT EXISTS weather_obs (
    obs_id         bigserial PRIMARY KEY,
    station_id     text        NOT NULL,                     -- the weather service gives only the position as "lat,lon"
    geom           geometry(Point, 4326) NOT NULL,
    observed_at    timestamptz NOT NULL,
    wind_speed_ms  numeric(5,1) CHECK (wind_speed_ms IS NULL OR wind_speed_ms >= 0),
    wind_gust_ms   numeric(5,1) CHECK (wind_gust_ms  IS NULL OR wind_gust_ms  >= 0),
    wind_dir_deg   smallint     CHECK (wind_dir_deg  IS NULL OR wind_dir_deg BETWEEN 0 AND 360),
    collected_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_weather_obs UNIQUE (station_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_weather_observed ON weather_obs (observed_at DESC);
CREATE INDEX IF NOT EXISTS gist_weather_geom    ON weather_obs USING gist (geom);

-- The next two tables are my own assumptions, not maritime science. They are simple guesses that can be
-- edited, and every possible area records which values it used.
CREATE TABLE IF NOT EXISTS speed_factor_lookup (
    band_id        serial PRIMARY KEY,
    condition_type text    NOT NULL CHECK (condition_type IN ('WIND_MS','WAVE_M','SEA_STATE')),
    label          text    NOT NULL,
    lower_bound    numeric,                                  -- a band means lower <= x < upper
    upper_bound    numeric,
    speed_factor   numeric(3,2) NOT NULL CHECK (speed_factor > 0 AND speed_factor <= 1),
    is_assumption  boolean NOT NULL DEFAULT true,
    CONSTRAINT uq_speed_factor UNIQUE (condition_type, label)
);
INSERT INTO speed_factor_lookup (condition_type, label, lower_bound, upper_bound, speed_factor) VALUES
    ('WIND_MS','calm',          0,   5, 1.00),
    ('WIND_MS','breeze',        5,  10, 0.95),
    ('WIND_MS','fresh',        10,  15, 0.85),
    ('WIND_MS','strong',       15,  20, 0.70),
    ('WIND_MS','gale',         20,  25, 0.55),
    ('WIND_MS','storm',        25, 999, 0.40),
    ('WAVE_M', 'smooth',        0, 0.5, 1.00),
    ('WAVE_M', 'slight',      0.5, 1.5, 0.95),
    ('WAVE_M', 'moderate',    1.5, 2.5, 0.85),
    ('WAVE_M', 'rough',       2.5, 4.0, 0.70),
    ('WAVE_M', 'very rough',  4.0, 99,  0.50)
ON CONFLICT (condition_type, label) DO NOTHING;
INSERT INTO speed_factor_lookup (condition_type, label, speed_factor) VALUES
    ('SEA_STATE','CALM',      1.00),
    ('SEA_STATE','LIGHT',     0.95),
    ('SEA_STATE','MODERATE',  0.85),
    ('SEA_STATE','ROUGH',     0.65),
    ('SEA_STATE','VERY_ROUGH',0.50)
ON CONFLICT (condition_type, label) DO NOTHING;

CREATE TABLE IF NOT EXISTS ship_class_speed (          -- assumed top speed for each ship type (knots)
    class_id     serial PRIMARY KEY,
    label        text NOT NULL UNIQUE,
    type_min     smallint NOT NULL,
    type_max     smallint NOT NULL,
    v_max_knots  numeric(4,1) NOT NULL CHECK (v_max_knots > 0)
);
INSERT INTO ship_class_speed (label, type_min, type_max, v_max_knots) VALUES
    ('Unknown',            0,  0, 18.0),
    ('Wing-in-ground',    20, 29, 60.0),
    ('Fishing',           30, 30, 12.0),
    ('Towing / dredging', 31, 34, 12.0),
    ('Military',          35, 35, 30.0),
    ('Sailing',           36, 36,  8.0),
    ('Pleasure craft',    37, 37, 25.0),
    ('High-speed craft',  40, 49, 35.0),
    ('Pilot / SAR',       50, 51, 25.0),
    ('Tug / tender',      52, 53, 13.0),
    ('Law enforcement',   54, 55, 30.0),
    ('Passenger',         60, 69, 25.0),
    ('Cargo',             70, 79, 16.0),
    ('Tanker',            80, 89, 15.0),
    ('Other',             90, 99, 18.0)
ON CONFLICT (label) DO NOTHING;

-- 1.8 Going dark: silences. Open means still silent, closed means the ship came back.
CREATE TABLE IF NOT EXISTS ais_gap_events (
    gap_id                  bigserial PRIMARY KEY,
    mmsi                    bigint      NOT NULL REFERENCES vessels (mmsi) ON DELETE CASCADE,
    last_seen_time          timestamptz NOT NULL,           -- last fix before the silence
    reappear_time           timestamptz,                    -- first fix after the silence, empty while open
    gap_duration_minutes    numeric(10,1),                  -- final if closed, a snapshot if open
    start_geom              geometry(Point, 4326) NOT NULL, -- position before
    end_geom                geometry(Point, 4326),          -- position after
    status                  text        NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED')),
    classification          text        NOT NULL DEFAULT 'DARK_ACTIVITY'
                            CHECK (classification IN ('DARK_ACTIVITY','IN_PORT','STATIONARY','RECEIVER_OUTAGE','LEFT_AREA')),
    is_dark_activity        boolean     NOT NULL DEFAULT true,
    start_port_id           integer REFERENCES ports (port_id),
    neighbours_before       integer,
    neighbours_silent       integer,
    detected_at             timestamptz NOT NULL DEFAULT now(),
    closed_at               timestamptz,
    -- possible area (written by collector.py)
    feasibility_status      text        NOT NULL DEFAULT 'PENDING' CHECK (feasibility_status IN ('PENDING','OK','PROJECTED','INFEASIBLE')),
    v_max_knots             numeric(5,1),
    speed_factor            numeric(4,2),
    v_eff_knots             numeric(5,1),
    reach_radius_km         numeric(9,2),
    observed_distance_km    numeric(9,2),
    implied_speed_knots     numeric(6,1),
    feasible_geom           geometry(MultiPolygon, 4326),   -- overlap of the two reach circles, minus land
    tight_geom              geometry(MultiPolygon, 4326),   -- tightest area (an ellipse between the two positions), minus land
    feasible_area_km2       numeric(12,1),
    tight_area_km2          numeric(12,1),
    land_removed_km2        numeric(12,1),
    visited_area_km2        numeric(12,1),                  -- part of the tight area that real ships have used
    conditions              jsonb,
    feasibility_computed_at timestamptz,
    CONSTRAINT ck_gap_times CHECK (reappear_time IS NULL OR reappear_time > last_seen_time),
    CONSTRAINT ck_gap_status CHECK ((status = 'OPEN' AND reappear_time IS NULL) OR (status = 'CLOSED' AND reappear_time IS NOT NULL)),
    CONSTRAINT uq_gap_vessel_start UNIQUE (mmsi, last_seen_time)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_open_gap ON ais_gap_events (mmsi) WHERE status = 'OPEN';
CREATE INDEX IF NOT EXISTS idx_gap_status     ON ais_gap_events (status, classification);
CREATE INDEX IF NOT EXISTS idx_gap_detected   ON ais_gap_events (detected_at DESC);
CREATE INDEX IF NOT EXISTS gist_gap_start     ON ais_gap_events USING gist (start_geom);
CREATE INDEX IF NOT EXISTS gist_gap_feasible  ON ais_gap_events USING gist (feasible_geom);

-- 1.9 Optional layer of water that no ship has visited
CREATE TABLE IF NOT EXISTS traffic_grid (
    cell_id        serial PRIMARY KEY,
    geom           geometry(Polygon, 4326) NOT NULL,
    visits         integer     NOT NULL DEFAULT 0,
    distinct_ships integer     NOT NULL DEFAULT 0,
    is_land        boolean     NOT NULL DEFAULT false,
    never_visited  boolean     NOT NULL DEFAULT true,
    refreshed_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS gist_traffic_grid_geom ON traffic_grid USING gist (geom);

-- 1.10 Rough fallback coastline, used only until setup_db.py --land loads the real land
CREATE OR REPLACE FUNCTION seed_fallback_land() RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    n integer := 0;
BEGIN
    IF EXISTS (SELECT 1 FROM land_polygons) THEN
        RETURN 0;                                               -- land is already there
    END IF;
    INSERT INTO land_polygons (name, source, geom) VALUES
    ('Finland / Russia north shore', 'FALLBACK_COARSE', ST_Multi(ST_GeomFromText(
        'POLYGON((21.00 60.62,21.30 60.78,21.45 60.82,21.60 60.60,21.85 60.52,22.05 60.43,22.30 60.40,22.55 60.15,22.80 59.95,'
        '22.90 59.82,23.20 59.90,23.45 59.98,23.90 60.00,24.30 59.95,24.45 59.92,24.60 60.10,24.95 60.12,25.35 60.20,25.70 60.30,'
        '26.10 60.36,26.45 60.40,26.95 60.42,27.20 60.50,27.55 60.50,27.95 60.52,28.40 60.45,28.60 60.36,28.80 60.30,29.10 60.20,'
        '29.50 60.10,29.75 60.03,30.00 60.05,30.15 59.99,30.25 59.93,31.00 59.90,31.00 61.20,21.00 61.20,21.00 60.62))', 4326))),
    ('Estonia / Ingria south shore', 'FALLBACK_COARSE', ST_Multi(ST_GeomFromText(
        'POLYGON((21.00 58.00,21.00 58.98,22.45 58.98,22.70 59.05,22.95 59.10,23.50 59.22,23.80 59.28,23.95 59.40,24.10 59.38,'
        '24.40 59.42,24.55 59.48,24.80 59.47,25.00 59.52,25.40 59.62,25.75 59.70,26.00 59.62,26.50 59.55,27.10 59.45,27.70 59.42,'
        '28.00 59.47,28.25 59.68,28.60 59.78,29.20 59.88,29.78 59.92,30.00 59.89,31.00 59.88,31.00 58.00,21.00 58.00))', 4326))),
    ('Naissaar', 'FALLBACK_COARSE', ST_Multi(ST_GeomFromText(
        'POLYGON((24.45 59.52,24.55 59.52,24.55 59.60,24.45 59.60,24.45 59.52))', 4326))),
    ('Suursaari (Hogland)', 'FALLBACK_COARSE', ST_Multi(ST_GeomFromText(
        'POLYGON((26.85 60.03,27.10 60.03,27.10 60.10,26.85 60.10,26.85 60.03))', 4326))),
    ('Kotlin', 'FALLBACK_COARSE', ST_Multi(ST_GeomFromText(
        'POLYGON((29.65 59.97,29.90 59.99,29.90 60.02,29.65 60.00,29.65 59.97))', 4326)));
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;

SELECT seed_fallback_land();

-- 1.11 Views
-- each ship with its latest position and how long it has been silent
CREATE OR REPLACE VIEW v_vessel_overview AS
SELECT v.mmsi, v.name, v.ship_type, v.length_m, v.width_m, v.draught_m, v.destination, v.eta, v.is_synthetic,
       lp.geom, lp.speed_knots, lp.course, lp.heading, lp.last_seen,
       EXTRACT(EPOCH FROM (now() - lp.last_seen)) / 60.0 AS silent_minutes
FROM vessels v
LEFT JOIN latest_position lp USING (mmsi);

-- how many records were refused for each reason
CREATE OR REPLACE VIEW v_rejection_summary AS
SELECT rejection_reason, count(*) AS rejected_rows, min(rejected_at) AS first_at, max(rejected_at) AS last_at
FROM rejected_events
GROUP BY rejection_reason;

-- every silence together with the ship it belongs to and its possible area figures
CREATE OR REPLACE VIEW v_dark_incidents AS
SELECT g.gap_id, g.mmsi, v.name, g.status, g.classification, g.is_dark_activity,
       g.last_seen_time, g.reappear_time, g.gap_duration_minutes,
       g.neighbours_before, g.neighbours_silent,
       g.v_max_knots, g.speed_factor, g.v_eff_knots, g.reach_radius_km,
       g.feasible_area_km2, g.tight_area_km2, g.feasibility_status, v.is_synthetic
FROM ais_gap_events g
JOIN vessels v USING (mmsi);


-- PART 2: LOGIC

-- 2.0 Small helper functions
CREATE OR REPLACE FUNCTION fn_cfg(p_key text) RETURNS numeric
LANGUAGE sql STABLE AS $$
    -- Here we read one setting value from the config table.
    SELECT cfg_value FROM pipeline_config WHERE cfg_key = p_key
$$;

-- distance in metres between two points, measured on the earth's curved surface
CREATE OR REPLACE FUNCTION fn_distance_m(a geometry, b geometry) RETURNS double precision
LANGUAGE sql IMMUTABLE AS $$
    SELECT ST_Distance(a::geography, b::geography)
$$;

-- assumed top speed (knots) for a ship type code, with the Unknown class as the fallback
CREATE OR REPLACE FUNCTION fn_ship_vmax(p_type smallint) RETURNS numeric
LANGUAGE sql STABLE AS $$
    -- This looks up the top speed for the type code, and uses 18 knots if nothing matches.
    SELECT COALESCE(
        (SELECT v_max_knots FROM ship_class_speed WHERE COALESCE(p_type, 0) BETWEEN type_min AND type_max ORDER BY type_min DESC LIMIT 1),
        18.0)
$$;

-- the name of the ship class for a ship type code
CREATE OR REPLACE FUNCTION fn_ship_class(p_type smallint) RETURNS text
LANGUAGE sql STABLE AS $$
    -- This looks up the class name for the type code, or says Unknown.
    SELECT COALESCE(
        (SELECT label FROM ship_class_speed WHERE COALESCE(p_type, 0) BETWEEN type_min AND type_max ORDER BY type_min DESC LIMIT 1),
        'Unknown')
$$;

-- true when a point is close to the edge of the area we watch, so the ship is probably leaving
CREATE OR REPLACE FUNCTION fn_near_edge(p_geom geometry) RETURNS boolean
LANGUAGE sql STABLE AS $$
    -- This checks if the point is inside the edge margin on any of the four sides.
    SELECT ST_Y(p_geom) < fn_cfg('bbox_min_lat') + fn_cfg('edge_margin_deg')
        OR ST_Y(p_geom) > fn_cfg('bbox_max_lat') - fn_cfg('edge_margin_deg')
        OR ST_X(p_geom) < fn_cfg('bbox_min_lon') + 2 * fn_cfg('edge_margin_deg')   -- at 60N one degree of longitude is only half as long as one of latitude
        OR ST_X(p_geom) > fn_cfg('bbox_max_lon') - 2 * fn_cfg('edge_margin_deg')
$$;

-- 2.1 Checking incoming records: staging_ais goes in, location_events and latest_position come out,
-- and anything that fails goes to rejected_events with a reason.
-- One call is one all-or-nothing unit. If anything fails, the staged rows are untouched and can be retried.
-- Rows are claimed with FOR UPDATE SKIP LOCKED, so two checkers never take the same row.
-- A ship's latest_position row is locked while its new fix is compared with the previous one.
-- SECURITY DEFINER lets the limited ingest login call this without touching the checked tables directly.
CREATE OR REPLACE FUNCTION validate_and_migrate_ais(p_batch_size integer DEFAULT 100000)
RETURNS TABLE (processed integer, accepted integer, rejected integer, duplicates integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    r              staging_ais%ROWTYPE;
    lp             latest_position%ROWTYPE;
    v_reason       text;
    v_detail       text;
    v_pt           geometry;
    v_prev_geom    geometry;
    v_prev_time    timestamptz;
    v_dt_s         double precision;
    v_dist_m       double precision;
    v_knots        double precision;
    v_rows         integer;
    n_proc         integer := 0;
    n_acc          integer := 0;
    n_rej          integer := 0;
    n_dup          integer := 0;
    c_min_lat      numeric := fn_cfg('bbox_min_lat');
    c_max_lat      numeric := fn_cfg('bbox_max_lat');
    c_min_lon      numeric := fn_cfg('bbox_min_lon');
    c_max_lon      numeric := fn_cfg('bbox_max_lon');
    c_max_speed    numeric := fn_cfg('max_speed_knots');
    c_max_jump     numeric := fn_cfg('max_jump_knots');
    c_min_dist     numeric := fn_cfg('min_jump_distance_m');
    c_future       numeric := fn_cfg('future_tolerance_min');
BEGIN
    -- This goes through the staged rows one by one, oldest first. Rows locked by another checker are skipped.
    FOR r IN
        SELECT * FROM staging_ais
        ORDER BY event_time NULLS FIRST, staging_id
        LIMIT p_batch_size
        FOR UPDATE SKIP LOCKED
    LOOP
        n_proc     := n_proc + 1;
        v_reason   := NULL;
        v_detail   := NULL;
        v_prev_geom := NULL;
        v_prev_time := NULL;

        -- simple rule checks first, they are the cheapest
        IF r.mmsi IS NULL THEN
            v_reason := 'NULL_MMSI';
        ELSIF r.mmsi NOT BETWEEN 200000000 AND 799999999 THEN
            v_reason := 'INVALID_MMSI';
            v_detail := 'mmsi=' || r.mmsi;
        ELSIF r.event_time IS NULL OR r.lat IS NULL OR r.lon IS NULL THEN
            v_reason := 'MISSING_FIELDS';
        ELSIF r.lat NOT BETWEEN -90 AND 90 OR r.lon NOT BETWEEN -180 AND 180 THEN
            v_reason := 'INVALID_COORDINATES';
            v_detail := format('lat=%s lon=%s', r.lat, r.lon);
        ELSIF r.lat NOT BETWEEN c_min_lat AND c_max_lat OR r.lon NOT BETWEEN c_min_lon AND c_max_lon THEN
            v_reason := 'OUT_OF_AREA';
            v_detail := format('lat=%s lon=%s outside [%s..%s, %s..%s]', r.lat, r.lon, c_min_lat, c_max_lat, c_min_lon, c_max_lon);
        ELSIF r.sog IS NOT NULL AND (r.sog < 0 OR r.sog > c_max_speed) THEN
            v_reason := 'UNREALISTIC_SPEED';
            v_detail := format('sog=%s kn (limit %s)', r.sog, c_max_speed);
        ELSIF r.event_time > now() + make_interval(mins => c_future::integer) THEN
            v_reason := 'FUTURE_TIMESTAMP';
            v_detail := 'event_time=' || r.event_time;
        END IF;

        -- duplicate and speed checks, these need data from the database
        IF v_reason IS NULL THEN
            v_pt := ST_SetSRID(ST_MakePoint(r.lon, r.lat), 4326);

            -- This looks for a fix already saved for the same ship and time.
            PERFORM 1 FROM location_events WHERE mmsi = r.mmsi AND event_time = r.event_time;
            IF FOUND THEN
                v_reason := 'DUPLICATE';
                v_detail := 'fix already stored for this mmsi/time';
            ELSE
                -- This gets the ship's latest saved position.
                SELECT * INTO lp FROM latest_position WHERE mmsi = r.mmsi FOR UPDATE;   -- lock this ship's row so two updates cannot clash
                IF FOUND THEN
                    IF r.event_time > lp.last_seen THEN
                        v_prev_geom := lp.geom;
                        v_prev_time := lp.last_seen;
                    ELSE   -- a late fix: compare it with the fix that really came before it
                        -- This finds the fix that came just before this late one.
                        SELECT le.geom, le.event_time INTO v_prev_geom, v_prev_time
                        FROM location_events le
                        WHERE le.mmsi = r.mmsi AND le.event_time < r.event_time
                        ORDER BY le.event_time DESC LIMIT 1;
                    END IF;
                END IF;

                IF v_prev_geom IS NOT NULL THEN
                    v_dt_s   := EXTRACT(EPOCH FROM (r.event_time - v_prev_time));
                    v_dist_m := ST_Distance(v_pt::geography, v_prev_geom::geography);
                    v_knots  := (v_dist_m / 1852.0) / (GREATEST(v_dt_s, 1.0) / 3600.0);
                    IF v_dist_m > c_min_dist AND v_knots > c_max_jump THEN
                        v_reason := 'IMPOSSIBLE_JUMP';
                        v_detail := format('%s m in %s s = %s kn (limit %s kn)',
                                           round(v_dist_m::numeric, 0), round(v_dt_s::numeric, 0), round(v_knots::numeric, 1), c_max_jump);
                    END IF;
                END IF;
            END IF;
        END IF;

        -- store the fix or reject it
        IF v_reason IS NOT NULL THEN
            -- Here we save the bad row in rejected_events with the reason.
            INSERT INTO rejected_events (mmsi, lat, lon, sog, cog, heading, nav_stat, event_time, source, rejection_reason, detail)
            VALUES (r.mmsi, r.lat, r.lon, r.sog, r.cog, r.heading, r.nav_stat, r.event_time, r.source, v_reason, v_detail);
            n_rej := n_rej + 1;
            IF v_reason = 'DUPLICATE' THEN n_dup := n_dup + 1; END IF;
        ELSE
            INSERT INTO vessels (mmsi) VALUES (r.mmsi) ON CONFLICT (mmsi) DO NOTHING;     -- placeholder until the ship details arrive

            -- This saves the good fix in location_events. If it is already there it does nothing.
            INSERT INTO location_events (mmsi, geom, speed_knots, course, heading, nav_stat, event_time)
            VALUES (r.mmsi, v_pt,
                    CASE WHEN r.sog IS NULL THEN NULL ELSE round(r.sog::numeric, 1) END,
                    CASE WHEN r.cog BETWEEN 0 AND 360 THEN round(r.cog::numeric, 1) END,
                    CASE WHEN r.heading BETWEEN 0 AND 511 THEN r.heading::smallint END,
                    r.nav_stat::smallint,
                    r.event_time)
            ON CONFLICT (mmsi, event_time) DO NOTHING;
            GET DIAGNOSTICS v_rows = ROW_COUNT;

            IF v_rows = 0 THEN                                   -- another process inserted the same fix first
                -- Here we record it as a duplicate because another process saved it first.
                INSERT INTO rejected_events (mmsi, lat, lon, sog, cog, heading, nav_stat, event_time, source, rejection_reason, detail)
                VALUES (r.mmsi, r.lat, r.lon, r.sog, r.cog, r.heading, r.nav_stat, r.event_time, r.source, 'DUPLICATE', 'concurrent insert');
                n_rej := n_rej + 1;
                n_dup := n_dup + 1;
            ELSE
                n_acc := n_acc + 1;
                -- This adds the ship's latest position, or updates it if the ship already has one.
                INSERT INTO latest_position (mmsi, geom, speed_knots, course, heading, nav_stat, last_seen)
                VALUES (r.mmsi, v_pt,
                        CASE WHEN r.sog IS NULL THEN NULL ELSE round(r.sog::numeric, 1) END,
                        CASE WHEN r.cog BETWEEN 0 AND 360 THEN round(r.cog::numeric, 1) END,
                        CASE WHEN r.heading BETWEEN 0 AND 511 THEN r.heading::smallint END,
                        r.nav_stat::smallint,
                        r.event_time)
                ON CONFLICT (mmsi) DO UPDATE
                   SET geom = EXCLUDED.geom, speed_knots = EXCLUDED.speed_knots, course = EXCLUDED.course,
                       heading = EXCLUDED.heading, nav_stat = EXCLUDED.nav_stat,
                       last_seen = EXCLUDED.last_seen, updated_at = now()
                 WHERE EXCLUDED.last_seen > latest_position.last_seen;      -- only move forward in time
            END IF;
        END IF;

        -- This row is finished, so we remove it from staging.
        DELETE FROM staging_ais WHERE staging_id = r.staging_id;
    END LOOP;

    processed := n_proc; accepted := n_acc; rejected := n_rej; duplicates := n_dup;
    RETURN NEXT;
END;
$$;

COMMENT ON FUNCTION validate_and_migrate_ais(integer) IS
    'Atomic staging->trusted migration: rejects bad rows with a reason, inserts good ones, advances latest_position only if newer.';

-- 2.2 Track A (Temporal): destination and ETA history, one new row for every change.
-- Valid time is valid_from to valid_to (when the plan applied).
-- Recorded time is recorded_at to superseded_at (when this database knew about it).
CREATE OR REPLACE FUNCTION track_vessel_status_scd2() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER                    -- the limited collector login updates vessels, and the history is written on its behalf
SET search_path = public, pg_temp
AS $$
DECLARE
    cur  vessel_status_history%ROWTYPE;
    ts   timestamptz := COALESCE(NEW.status_ts, now());
BEGIN
    -- This gets the ship's current history row and locks it.
    SELECT * INTO cur FROM vessel_status_history WHERE mmsi = NEW.mmsi AND is_current FOR UPDATE;

    IF NOT FOUND THEN
        -- The ship has no history yet, so we add its first row.
        INSERT INTO vessel_status_history (mmsi, destination, eta, valid_from)
        VALUES (NEW.mmsi, NEW.destination, NEW.eta, ts);

    ELSIF cur.destination IS DISTINCT FROM NEW.destination OR cur.eta IS DISTINCT FROM NEW.eta THEN
        IF ts <= cur.valid_from THEN
            RETURN NEW;                       -- an old message that arrived late: ignore it, never rewrite the past
        END IF;
        -- This closes the old history row because the destination or ETA changed.
        UPDATE vessel_status_history
           SET valid_to = ts, is_current = false, superseded_at = now()
         WHERE history_id = cur.history_id;
        -- Then we add a new row with the new destination and ETA.
        INSERT INTO vessel_status_history (mmsi, destination, eta, valid_from)
        VALUES (NEW.mmsi, NEW.destination, NEW.eta, ts);
    END IF;
    RETURN NEW;
END;
$$;

-- Triggers: save a history row when a ship is added with a destination, or when its destination or ETA changes
DROP TRIGGER IF EXISTS trg_vessel_status_ins ON vessels;
CREATE TRIGGER trg_vessel_status_ins
    AFTER INSERT ON vessels
    FOR EACH ROW WHEN (NEW.destination IS NOT NULL OR NEW.eta IS NOT NULL)
    EXECUTE FUNCTION track_vessel_status_scd2();

DROP TRIGGER IF EXISTS trg_vessel_status_upd ON vessels;
CREATE TRIGGER trg_vessel_status_upd
    AFTER UPDATE OF destination, eta ON vessels
    FOR EACH ROW WHEN (OLD.destination IS DISTINCT FROM NEW.destination OR OLD.eta IS DISTINCT FROM NEW.eta)
    EXECUTE FUNCTION track_vessel_status_scd2();

-- "as of" query: what was true at sea at p_valid_at, according to what the database knew at p_known_at
CREATE OR REPLACE FUNCTION fn_vessel_status_as_of(p_mmsi bigint, p_valid_at timestamptz, p_known_at timestamptz DEFAULT now())
RETURNS TABLE (history_id bigint, destination text, eta timestamptz, valid_from timestamptz, valid_to timestamptz,
               recorded_at timestamptz, superseded_at timestamptz)
LANGUAGE sql STABLE AS $$
    -- This gets the history rows that were true and known at the given times.
    SELECT h.history_id, h.destination, h.eta, h.valid_from, h.valid_to, h.recorded_at, h.superseded_at
    FROM vessel_status_history h
    WHERE h.mmsi = p_mmsi
      AND h.valid_from  <= p_valid_at
      AND h.recorded_at <= p_known_at
      AND (   h.valid_to IS NULL
           OR h.valid_to > p_valid_at
           OR (h.superseded_at IS NOT NULL AND h.superseded_at > p_known_at))   -- the row was still open at that time
$$;

-- 2.3 Track C (GIS): port visits. A fix inside a port's radius (3 km) is an ENTRY.
-- An EXIT only counts once the ship is beyond radius * 1.1, so a ship drifting on the edge does not
-- create a stream of events. A ship's very first fix only sets its state, so no arrival is invented.
CREATE OR REPLACE FUNCTION check_port_proximity() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    st        vessel_port_state%ROWTYPE;
    cur_port  ports%ROWTYPE;
    hit       RECORD;
    v_known   boolean;
    v_port    integer;
    v_since   timestamptz;
    v_dist    numeric;
    c_margin  numeric := fn_cfg('port_exit_margin');
BEGIN
    -- This gets the ship's port state and locks it.
    SELECT * INTO st FROM vessel_port_state WHERE mmsi = NEW.mmsi FOR UPDATE;
    v_known := FOUND;
    IF v_known AND NEW.event_time <= st.last_event_time THEN
        RETURN NULL;                                           -- a late fix: the port state only moves forward
    END IF;

    v_port  := st.port_id;
    v_since := st.since;

    IF v_known AND v_port IS NOT NULL THEN
        -- This gets the port the ship was last in.
        SELECT * INTO cur_port FROM ports WHERE port_id = v_port;
        v_dist := ST_Distance(NEW.geom::geography, cur_port.geom::geography);
        IF v_dist > cur_port.buffer_radius_m * (1 + c_margin) THEN
            -- Here we save an EXIT event because the ship is far enough from the port.
            INSERT INTO port_events (mmsi, port_id, event_type, event_time, distance_m)
            VALUES (NEW.mmsi, v_port, 'EXIT', NEW.event_time, round(v_dist, 1))
            ON CONFLICT DO NOTHING;
            v_port  := NULL;
            v_since := NULL;
        END IF;
    END IF;

    IF v_port IS NULL THEN
        -- This finds the nearest port whose radius contains the new position.
        SELECT p.port_id, ST_Distance(p.geom::geography, NEW.geom::geography) AS dist
          INTO hit
          FROM ports p
         WHERE ST_DWithin(p.geom::geography, NEW.geom::geography, p.buffer_radius_m)
         ORDER BY dist
         LIMIT 1;
        IF FOUND THEN
            IF v_known THEN
                -- Here we save an ENTRY event for that port.
                INSERT INTO port_events (mmsi, port_id, event_type, event_time, distance_m)
                VALUES (NEW.mmsi, hit.port_id, 'ENTRY', NEW.event_time, round(hit.dist::numeric, 1))
                ON CONFLICT DO NOTHING;
            END IF;
            v_port  := hit.port_id;
            v_since := NEW.event_time;
        END IF;
    END IF;

    -- This saves the ship's new port state, or updates it if one already exists.
    INSERT INTO vessel_port_state (mmsi, port_id, since, last_event_time)
    VALUES (NEW.mmsi, v_port, v_since, NEW.event_time)
    ON CONFLICT (mmsi) DO UPDATE
       SET port_id = EXCLUDED.port_id, since = EXCLUDED.since, last_event_time = EXCLUDED.last_event_time;
    RETURN NULL;
END;
$$;

-- Trigger: after every stored position, check whether the ship entered or left a port
DROP TRIGGER IF EXISTS trg_location_port ON location_events;
CREATE TRIGGER trg_location_port
    AFTER INSERT ON location_events
    FOR EACH ROW EXECUTE FUNCTION check_port_proximity();

-- 2.4 Going dark: sorting silences, finding silent ships, and keeping the gap records up to date.
-- This decides what a silence most likely is:
--   IN_PORT          the last fix was inside a port
--   STATIONARY       the ship was already almost still, so it was parked and switched AIS off
--   LEFT_AREA        the ship was at the edge of our area, or the closed gap lasted over 12 hours
--   RECEIVER_OUTAGE  nearby ships that were active also went quiet, so our receiver is the problem
--   DARK_ACTIVITY    everything else: a moving ship went silent while its neighbours kept reporting
CREATE OR REPLACE FUNCTION fn_classify_gap(p_mmsi bigint, p_t1 timestamptz, p_p1 geometry, p_p2 geometry, p_t2 timestamptz,
                                           p_speed numeric DEFAULT NULL)
RETURNS TABLE (classification text, port_id integer, neighbours_before integer, neighbours_silent integer)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_port    integer;
    v_nb      integer := 0;
    v_sil     integer := 0;
    c_thr     numeric := fn_cfg('gap_threshold_min');
    c_radius  numeric := fn_cfg('neighbour_radius_m');
    c_window  numeric := fn_cfg('neighbour_window_min');
    v_syn     boolean := COALESCE((SELECT is_synthetic FROM vessels WHERE mmsi = p_mmsi), false);
BEGIN
    -- 1. inside a port?
    SELECT p.port_id INTO v_port
      FROM ports p
     WHERE ST_DWithin(p.geom::geography, p_p1::geography, p.buffer_radius_m)
     ORDER BY ST_Distance(p.geom::geography, p_p1::geography)
     LIMIT 1;
    IF v_port IS NOT NULL THEN
        RETURN QUERY SELECT 'IN_PORT'::text, v_port, 0, 0;
        RETURN;
    END IF;

    -- 2. already not moving when the signal stopped?
    IF p_speed IS NOT NULL AND p_speed < fn_cfg('stationary_speed_knots') THEN
        RETURN QUERY SELECT 'STATIONARY'::text, NULL::integer, 0, 0;
        RETURN;
    END IF;

    -- 3. simply left (or entered) the area we watch?
    IF fn_near_edge(p_p1)
       OR (p_p2 IS NOT NULL AND fn_near_edge(p_p2))
       OR (p_t2 IS NOT NULL AND EXTRACT(EPOCH FROM (p_t2 - p_t1)) / 3600.0 > fn_cfg('gap_max_hours')) THEN
        RETURN QUERY SELECT 'LEFT_AREA'::text, NULL::integer, 0, 0;
        RETURN;
    END IF;

    -- 4. did the neighbours go silent too? A neighbour is another ship seen nearby just before the silence began
    SELECT count(*)::integer,
           count(*) FILTER (WHERE q.silent)::integer
      INTO v_nb, v_sil
      FROM (
            SELECT n.mmsi,
                   NOT EXISTS (SELECT 1 FROM location_events x
                                WHERE x.mmsi = n.mmsi
                                  AND x.event_time >  p_t1
                                  AND x.event_time <= p_t1 + make_interval(mins => c_thr::integer)) AS silent
              FROM (SELECT DISTINCT le.mmsi
                      FROM location_events le
                      JOIN vessels nv ON nv.mmsi = le.mmsi AND nv.is_synthetic = v_syn   -- demo ships only see demo ships
                     WHERE le.event_time BETWEEN p_t1 - make_interval(mins => c_window::integer) AND p_t1
                       AND le.mmsi <> p_mmsi
                       AND ST_DWithin(le.geom::geography, p_p1::geography, c_radius)) n
           ) q;

    IF v_sil >= fn_cfg('outage_min_silent') AND v_sil >= fn_cfg('outage_silent_ratio') * v_nb THEN
        RETURN QUERY SELECT 'RECEIVER_OUTAGE'::text, NULL::integer, v_nb, v_sil;
    ELSE
        RETURN QUERY SELECT 'DARK_ACTIVITY'::text, NULL::integer, v_nb, v_sil;
    END IF;
END;
$$;

-- Every ship that has been silent longer than the limit, with the verdict and the evidence.
CREATE OR REPLACE FUNCTION classify_silent_vessels(p_threshold_minutes integer DEFAULT NULL, p_lookback_hours integer DEFAULT NULL)
RETURNS TABLE (mmsi bigint, vessel_name text, last_seen timestamptz, silent_minutes numeric, last_geom geometry,
               last_speed_knots numeric, classification text, port_id integer, port_name text,
               neighbours_before integer, neighbours_silent integer)
LANGUAGE sql STABLE AS $$
    -- This takes each ship's last position, classifies its silence and keeps the ones silent past the limit.
    SELECT lp.mmsi, v.name, lp.last_seen,
           round((EXTRACT(EPOCH FROM (now() - lp.last_seen)) / 60.0)::numeric, 1),
           lp.geom, lp.speed_knots, c.classification, c.port_id, p.name, c.neighbours_before, c.neighbours_silent
      FROM latest_position lp
      JOIN vessels v ON v.mmsi = lp.mmsi
     CROSS JOIN LATERAL fn_classify_gap(lp.mmsi, lp.last_seen, lp.geom, NULL, NULL, lp.speed_knots) c
      LEFT JOIN ports p ON p.port_id = c.port_id
     WHERE lp.last_seen <  now() - make_interval(mins  => COALESCE(p_threshold_minutes, fn_cfg('gap_threshold_min')::integer))
       AND lp.last_seen >= now() - make_interval(hours => COALESCE(p_lookback_hours,    fn_cfg('gap_lookback_hours')::integer))
     ORDER BY lp.last_seen
$$;

-- The going dark detector: ships silent past the limit that are not in a port, not at the edge of the
-- area, and whose neighbours did not also go silent.
CREATE OR REPLACE FUNCTION detect_going_dark_vessels(threshold_minutes integer DEFAULT 20)
RETURNS TABLE (mmsi bigint, vessel_name text, last_seen timestamptz, silent_minutes numeric, last_geom geometry,
               last_speed_knots numeric, classification text, port_id integer, port_name text,
               neighbours_before integer, neighbours_silent integer)
LANGUAGE sql STABLE AS $$
    -- This keeps only the silences that were classified as dark activity.
    SELECT * FROM classify_silent_vessels(threshold_minutes) c WHERE c.classification = 'DARK_ACTIVITY'
$$;

-- Save ships that are silent right now as OPEN gaps and keep their verdicts fresh. Runs every collection cycle.
CREATE OR REPLACE FUNCTION sync_open_gaps(p_threshold_minutes integer DEFAULT NULL)
RETURNS TABLE (opened integer, refreshed integer, expired integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    n_open integer; n_ref integer; n_exp integer;
BEGIN
    -- This adds an OPEN gap for each ship that is silent now. Gaps that already exist are skipped.
    INSERT INTO ais_gap_events (mmsi, last_seen_time, gap_duration_minutes, start_geom, status, classification,
                                is_dark_activity, start_port_id, neighbours_before, neighbours_silent)
    SELECT c.mmsi, c.last_seen, c.silent_minutes, c.last_geom, 'OPEN', c.classification,
           c.classification = 'DARK_ACTIVITY', c.port_id, c.neighbours_before, c.neighbours_silent
      FROM classify_silent_vessels(p_threshold_minutes) c
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n_open = ROW_COUNT;

    -- This refreshes the duration and verdict of the gaps that are still open.
    UPDATE ais_gap_events g
       SET gap_duration_minutes = c.silent_minutes,
           classification       = c.classification,
           is_dark_activity     = c.classification = 'DARK_ACTIVITY',
           start_port_id        = c.port_id,
           neighbours_before    = c.neighbours_before,
           neighbours_silent    = c.neighbours_silent
      FROM classify_silent_vessels(p_threshold_minutes) c
     WHERE g.status = 'OPEN' AND g.mmsi = c.mmsi AND g.last_seen_time = c.last_seen;
    GET DIAGNOSTICS n_ref = ROW_COUNT;

    -- silent for longer than the look-back time: assume the ship left, so stop calling it dark
    UPDATE ais_gap_events
       SET classification = 'LEFT_AREA', is_dark_activity = false,
           gap_duration_minutes = round((EXTRACT(EPOCH FROM (now() - last_seen_time)) / 60.0)::numeric, 1),
           feasible_geom = NULL, tight_geom = NULL, feasible_area_km2 = NULL, tight_area_km2 = NULL,
           feasibility_status = 'PENDING'
     WHERE status = 'OPEN' AND classification <> 'LEFT_AREA'
       AND last_seen_time < now() - make_interval(hours => fn_cfg('gap_lookback_hours')::integer);
    GET DIAGNOSTICS n_exp = ROW_COUNT;

    opened := n_open; refreshed := n_ref; expired := n_exp;
    RETURN NEXT;
END;
$$;

-- When a silent ship transmits again, close its gap (or record a closed gap directly) and classify it again
-- with the full evidence. The possible area is set back to PENDING so the engine works it out again.
CREATE OR REPLACE FUNCTION trg_location_gap() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    prev     RECORD;
    cls      RECORD;
    gap_min  numeric;
    v_rows   integer;
BEGIN
    -- This checks if the ship already has a later fix, which would make this one old.
    IF EXISTS (SELECT 1 FROM location_events WHERE mmsi = NEW.mmsi AND event_time > NEW.event_time) THEN
        RETURN NULL;                                            -- a late fix inside existing history: no new gap
    END IF;

    -- This gets the fix that came just before the new one.
    SELECT le.geom, le.event_time, le.speed_knots INTO prev
      FROM location_events le
     WHERE le.mmsi = NEW.mmsi AND le.event_time < NEW.event_time
     ORDER BY le.event_time DESC LIMIT 1;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    gap_min := EXTRACT(EPOCH FROM (NEW.event_time - prev.event_time)) / 60.0;
    IF gap_min <= fn_cfg('gap_threshold_min') THEN
        RETURN NULL;
    END IF;

    -- This classifies the silence using both the old and the new position.
    SELECT * INTO cls FROM fn_classify_gap(NEW.mmsi, prev.event_time, prev.geom, NEW.geom, NEW.event_time, prev.speed_knots);

    -- This closes the ship's open gap with the new position and verdict.
    UPDATE ais_gap_events
       SET reappear_time = NEW.event_time, end_geom = NEW.geom, gap_duration_minutes = round(gap_min, 1),
           status = 'CLOSED', closed_at = now(),
           classification = cls.classification, is_dark_activity = (cls.classification = 'DARK_ACTIVITY'),
           start_port_id = cls.port_id, neighbours_before = cls.neighbours_before, neighbours_silent = cls.neighbours_silent,
           feasibility_status = 'PENDING', feasible_geom = NULL, tight_geom = NULL, feasibility_computed_at = NULL
     WHERE mmsi = NEW.mmsi AND status = 'OPEN';
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    IF v_rows = 0 THEN
        -- There was no open gap, so we add a closed gap directly.
        INSERT INTO ais_gap_events (mmsi, last_seen_time, reappear_time, gap_duration_minutes, start_geom, end_geom, status,
                                    classification, is_dark_activity, start_port_id, neighbours_before, neighbours_silent, closed_at)
        VALUES (NEW.mmsi, prev.event_time, NEW.event_time, round(gap_min, 1), prev.geom, NEW.geom, 'CLOSED',
                cls.classification, cls.classification = 'DARK_ACTIVITY', cls.port_id, cls.neighbours_before, cls.neighbours_silent, now())
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN NULL;
END;
$$;

-- Trigger: after every stored position, close the ship's open silence if it has one
DROP TRIGGER IF EXISTS trg_location_gap ON location_events;
CREATE TRIGGER trg_location_gap
    AFTER INSERT ON location_events
    FOR EACH ROW EXECUTE FUNCTION trg_location_gap();

-- 2.5 Weather slowdown: wind and waves give a speed factor (the lookup table is my own assumption, see section 1.7)
CREATE OR REPLACE FUNCTION get_speed_factor(p_geom geometry, p_at timestamptz DEFAULT now(),
                                            p_radius_km numeric DEFAULT 200, p_window_min integer DEFAULT 90)
RETURNS TABLE (wind_ms numeric, wind_station_km numeric, wind_obs_time timestamptz,
               wave_m numeric, wave_station_km numeric, wave_obs_time timestamptz,
               sea_state text, sea_obs_time timestamptz,
               wind_factor numeric, wave_factor numeric, sea_factor numeric,
               speed_factor numeric, basis text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_win   interval := make_interval(mins => p_window_min);
    v_rad   numeric  := p_radius_km * 1000;
    v_live_wind boolean := true;
    v_live_wave boolean := true;
BEGIN
    -- wind: the nearest station with a reading close to that time
    SELECT w.wind_speed_ms, round((ST_Distance(w.geom::geography, p_geom::geography) / 1000)::numeric, 1), w.observed_at
      INTO wind_ms, wind_station_km, wind_obs_time
      FROM weather_obs w
     WHERE w.wind_speed_ms IS NOT NULL
       AND w.observed_at BETWEEN p_at - v_win AND p_at + v_win
       AND ST_DWithin(w.geom::geography, p_geom::geography, v_rad)
     ORDER BY ST_Distance(w.geom::geography, p_geom::geography), abs(EXTRACT(EPOCH FROM (w.observed_at - p_at)))
     LIMIT 1;
    IF NOT FOUND THEN                       -- otherwise the reading closest in time (for an old gap, from before the collector ran)
        v_live_wind := false;
        SELECT w.wind_speed_ms, round((ST_Distance(w.geom::geography, p_geom::geography) / 1000)::numeric, 1), w.observed_at
          INTO wind_ms, wind_station_km, wind_obs_time
          FROM weather_obs w
         WHERE w.wind_speed_ms IS NOT NULL AND ST_DWithin(w.geom::geography, p_geom::geography, v_rad)
         ORDER BY abs(EXTRACT(EPOCH FROM (w.observed_at - p_at))), ST_Distance(w.geom::geography, p_geom::geography)
         LIMIT 1;
    END IF;

    -- This does the same for waves: the nearest station with a reading close to that time.
    SELECT s.wave_height_m, round((ST_Distance(s.geom::geography, p_geom::geography) / 1000)::numeric, 1), s.observed_at
      INTO wave_m, wave_station_km, wave_obs_time
      FROM sea_conditions s
     WHERE s.wave_height_m IS NOT NULL
       AND s.observed_at BETWEEN p_at - v_win AND p_at + v_win
       AND ST_DWithin(s.geom::geography, p_geom::geography, v_rad)
     ORDER BY ST_Distance(s.geom::geography, p_geom::geography), abs(EXTRACT(EPOCH FROM (s.observed_at - p_at)))
     LIMIT 1;
    IF NOT FOUND THEN
        v_live_wave := false;
        -- Nothing close in time, so we take the wave reading closest in time.
        SELECT s.wave_height_m, round((ST_Distance(s.geom::geography, p_geom::geography) / 1000)::numeric, 1), s.observed_at
          INTO wave_m, wave_station_km, wave_obs_time
          FROM sea_conditions s
         WHERE s.wave_height_m IS NOT NULL AND ST_DWithin(s.geom::geography, p_geom::geography, v_rad)
         ORDER BY abs(EXTRACT(EPOCH FROM (s.observed_at - p_at))), ST_Distance(s.geom::geography, p_geom::geography)
         LIMIT 1;
    END IF;

    -- the sea state estimate is only used when it is fresh, because most of its sites are out of date
    SELECT s.sea_state, s.observed_at INTO sea_state, sea_obs_time
      FROM sea_conditions s
     WHERE s.sea_state IS NOT NULL
       AND s.observed_at BETWEEN p_at - interval '6 hours' AND p_at + interval '6 hours'
       AND ST_DWithin(s.geom::geography, p_geom::geography, v_rad)
     ORDER BY ST_Distance(s.geom::geography, p_geom::geography)
     LIMIT 1;

    -- These three get the speed factor for the wind, the waves and the sea state from the lookup table.
    SELECT f.speed_factor INTO wind_factor FROM speed_factor_lookup f
     WHERE f.condition_type = 'WIND_MS' AND wind_ms >= f.lower_bound AND wind_ms < f.upper_bound;
    SELECT f.speed_factor INTO wave_factor FROM speed_factor_lookup f
     WHERE f.condition_type = 'WAVE_M' AND wave_m >= f.lower_bound AND wave_m < f.upper_bound;
    SELECT f.speed_factor INTO sea_factor FROM speed_factor_lookup f
     WHERE f.condition_type = 'SEA_STATE' AND f.label = upper(sea_state);

    speed_factor := LEAST(COALESCE(wind_factor, 1.0), COALESCE(wave_factor, 1.0), COALESCE(sea_factor, 1.0));
    basis := CASE
        WHEN wind_ms IS NULL AND wave_m IS NULL AND sea_state IS NULL THEN 'no observations: factor defaults to 1.00'
        WHEN v_live_wind AND (v_live_wave OR wave_m IS NULL) THEN 'observations within +/-' || p_window_min || ' min of the gap midpoint'
        ELSE 'fallback: observation closest in time (none inside the gap window)'
    END;
    RETURN NEXT;
END;
$$;

-- Turn the result of a cut or overlap into a clean MultiPolygon (or empty if there is nothing left)
CREATE OR REPLACE FUNCTION fn_clean_polygons(g geometry) RETURNS geometry
LANGUAGE sql IMMUTABLE AS $$
    -- This turns the shape into a valid MultiPolygon, or NULL if it is empty.
    SELECT CASE WHEN q.m IS NULL OR ST_IsEmpty(q.m) THEN NULL ELSE ST_SetSRID(q.m, 4326) END
      FROM (SELECT CASE WHEN g IS NULL OR ST_IsEmpty(g) THEN NULL
                        ELSE ST_Multi(ST_CollectionExtract(ST_MakeValid(g), 3)) END AS m) q
$$;

-- 2.6 Upkeep: land tiles, the never-visited water grid, and archiving
CREATE OR REPLACE FUNCTION rebuild_land_tiles() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE n integer;
BEGIN
    -- This empties the table first so it can be rebuilt.
    TRUNCATE land_tiles RESTART IDENTITY;
    -- This cuts the land polygons into small tiles and saves them.
    INSERT INTO land_tiles (geom)
    SELECT (ST_Dump(ST_CollectionExtract(ST_MakeValid(ST_Subdivide(lp.geom, 256)), 3))).geom
      FROM land_polygons lp;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;

DO $$
BEGIN
    -- We only build the tiles when the table is empty.
    IF NOT EXISTS (SELECT 1 FROM land_tiles) THEN
        PERFORM rebuild_land_tiles();
    END IF;
END
$$;

-- Divide the sea into square cells. A cell that no ship has ever crossed is "never visited".
CREATE OR REPLACE FUNCTION rebuild_traffic_grid(p_cell_deg numeric DEFAULT 0.05) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE n integer;
BEGIN
    -- This clears the old grid.
    TRUNCATE traffic_grid RESTART IDENTITY;
    -- This creates the square cells over the area we watch.
    INSERT INTO traffic_grid (geom)
    SELECT sg.geom
      FROM ST_SquareGrid(p_cell_deg::float8,
                         ST_MakeEnvelope(fn_cfg('bbox_min_lon')::float8, fn_cfg('bbox_min_lat')::float8,
                                         fn_cfg('bbox_max_lon')::float8, fn_cfg('bbox_max_lat')::float8, 4326)) sg;
    GET DIAGNOSTICS n = ROW_COUNT;

    -- This marks the cells whose centre is on land.
    UPDATE traffic_grid g SET is_land = true
     WHERE EXISTS (SELECT 1 FROM land_tiles t WHERE t.geom && g.geom AND ST_Intersects(t.geom, ST_Centroid(g.geom)));

    -- This counts the visits and ships in each cell, using real ships only.
    UPDATE traffic_grid g
       SET visits = v.visits, distinct_ships = v.ships, never_visited = false
      FROM (SELECT tg.cell_id, count(*)::integer AS visits, count(DISTINCT le.mmsi)::integer AS ships
              FROM traffic_grid tg
              JOIN location_events le ON le.geom && tg.geom AND ST_Intersects(le.geom, tg.geom)
              JOIN vessels sv ON sv.mmsi = le.mmsi AND NOT sv.is_synthetic        -- real ships only
             GROUP BY tg.cell_id) v
     WHERE g.cell_id = v.cell_id;

    -- This marks the cells with no visits that are not land as never visited.
    UPDATE traffic_grid SET never_visited = (visits = 0 AND NOT is_land), refreshed_at = now();
    RETURN n;
END;
$$;

-- Archiving for the big movement table: fixes older than p_days move to location_events_archive.
-- The newest fix of every ship always stays.
CREATE OR REPLACE FUNCTION archive_old_locations(p_days integer DEFAULT 30) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE n integer;
BEGIN
    -- This deletes old fixes (but keeps each ship's newest one) and copies them into the archive table.
    WITH moved AS (
        DELETE FROM location_events le
         WHERE le.event_time < now() - make_interval(days => p_days)
           AND NOT EXISTS (SELECT 1 FROM latest_position lp WHERE lp.mmsi = le.mmsi AND lp.last_seen = le.event_time)
        RETURNING le.event_id, le.mmsi, le.geom, le.speed_knots, le.course, le.heading, le.nav_stat, le.event_time, le.ingested_at
    )
    INSERT INTO location_events_archive (event_id, mmsi, geom, speed_knots, course, heading, nav_stat, event_time, ingested_at)
    SELECT event_id, mmsi, geom, speed_knots, course, heading, nav_stat, event_time, ingested_at FROM moved;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;


-- PART 3: DBA

-- 3.1 Logins and permissions, giving each one only what it needs.
-- ingest_role is for the collector: it can add data and run the checking function, nothing else.
-- It cannot read the movement history, delete anything or change the schema.
-- analyst_role can only read, and the website uses it.
-- The demo passwords are below. Change them for a real deployment.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingest_role')  THEN CREATE ROLE ingest_role  NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analyst_role') THEN CREATE ROLE analyst_role NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dsw_ingest') THEN
        CREATE ROLE dsw_ingest  LOGIN PASSWORD 'dsw_ingest_pw'  IN ROLE ingest_role  CONNECTION LIMIT 10;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dsw_analyst') THEN
        CREATE ROLE dsw_analyst LOGIN PASSWORD 'dsw_analyst_pw' IN ROLE analyst_role CONNECTION LIMIT 20;
    END IF;
END
$$;

-- let the DBA switch into both roles (the permission test needs this)
DO $$
BEGIN
    EXECUTE format('GRANT ingest_role, analyst_role TO %I', current_user);
EXCEPTION WHEN OTHERS THEN
    NULL;
END
$$;

-- nothing is open to everyone by default
REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION validate_and_migrate_ais(integer), sync_open_gaps(integer), rebuild_land_tiles(),
                           rebuild_traffic_grid(numeric), archive_old_locations(integer) FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO ingest_role, analyst_role;

-- ingest_role
GRANT INSERT ON staging_ais, collector_heartbeat, sea_conditions, weather_obs TO ingest_role;
GRANT SELECT, INSERT, UPDATE ON vessels TO ingest_role;              -- ship details are added here, and this fires the history trigger
GRANT SELECT (mmsi, last_seen) ON latest_position TO ingest_role;    -- so the collector can skip fixes that have not changed
GRANT UPDATE (geom, speed_knots, course, heading, nav_stat, last_seen, updated_at) ON latest_position TO ingest_role;
GRANT USAGE ON SEQUENCE staging_ais_staging_id_seq, collector_heartbeat_heartbeat_id_seq,
                        sea_conditions_obs_id_seq, weather_obs_obs_id_seq TO ingest_role;
GRANT EXECUTE ON FUNCTION validate_and_migrate_ais(integer), sync_open_gaps(integer) TO ingest_role;

-- analyst_role
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst_role;         -- includes pipeline_config, so results can be explained
GRANT pg_read_all_stats TO analyst_role;                             -- lets the monitoring views see other connections

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO analyst_role;

-- 3.2 Monitoring views: connections, locks, cache hit ratio, table health and index use
-- This shows the other connections in this database and how long they have been running.
CREATE OR REPLACE VIEW v_mon_activity AS
SELECT pid, usename, application_name, client_addr, state, wait_event_type, wait_event,
       round(EXTRACT(EPOCH FROM (now() - xact_start))::numeric, 1)   AS xact_age_s,
       round(EXTRACT(EPOCH FROM (now() - query_start))::numeric, 1)  AS query_age_s,
       left(regexp_replace(query, '\s+', ' ', 'g'), 160)             AS query
  FROM pg_stat_activity
 WHERE datname = current_database() AND pid <> pg_backend_pid() AND backend_type = 'client backend';

-- connections that are waiting for another connection to finish
CREATE OR REPLACE VIEW v_mon_blocking AS
SELECT blocked.pid                          AS blocked_pid,
       blocked.usename                      AS blocked_user,
       blocking.pid                         AS blocking_pid,
       blocking.usename                     AS blocking_user,
       round(EXTRACT(EPOCH FROM (now() - blocked.query_start))::numeric, 1) AS waiting_s,
       left(regexp_replace(blocked.query,  '\s+', ' ', 'g'), 100) AS blocked_query,
       left(regexp_replace(blocking.query, '\s+', ' ', 'g'), 100) AS blocking_query
  FROM pg_stat_activity blocked
  JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS b(pid) ON true
  JOIN pg_stat_activity blocking ON blocking.pid = b.pid
 WHERE blocked.datname = current_database();

-- queries that have been running for a long time
CREATE OR REPLACE VIEW v_mon_long_running AS
SELECT pid, usename, state, round(EXTRACT(EPOCH FROM (now() - query_start))::numeric, 1) AS running_s,
       left(regexp_replace(query, '\s+', ' ', 'g'), 160) AS query
  FROM pg_stat_activity
 WHERE datname = current_database() AND state = 'active' AND pid <> pg_backend_pid()
   AND now() - query_start > interval '5 seconds'
 ORDER BY query_start;

-- how often data is found in memory instead of read from disk
CREATE OR REPLACE VIEW v_mon_cache_hit AS
SELECT 'database'::text AS scope, datname::text AS object,
       blks_hit, blks_read, round(100.0 * blks_hit / NULLIF(blks_hit + blks_read, 0), 2) AS hit_pct
  FROM pg_stat_database WHERE datname = current_database()
UNION ALL
SELECT 'table', relname::text, heap_blks_hit, heap_blks_read,
       round(100.0 * heap_blks_hit / NULLIF(heap_blks_hit + heap_blks_read, 0), 2)
  FROM pg_statio_user_tables
 WHERE heap_blks_hit + heap_blks_read > 0;

-- size of each table, dead rows and when it was last cleaned
CREATE OR REPLACE VIEW v_mon_table_health AS
SELECT s.relname AS table_name, s.n_live_tup AS live_rows, s.n_dead_tup AS dead_rows,
       round(100.0 * s.n_dead_tup / NULLIF(s.n_live_tup + s.n_dead_tup, 0), 1) AS dead_pct,
       s.seq_scan, s.idx_scan, s.n_tup_hot_upd AS hot_updates,
       pg_size_pretty(pg_total_relation_size(s.relid)) AS total_size,
       pg_total_relation_size(s.relid) AS total_bytes,
       COALESCE(s.last_autovacuum, s.last_vacuum) AS last_vacuum,
       COALESCE(s.last_autoanalyze, s.last_analyze) AS last_analyze
  FROM pg_stat_user_tables s
 WHERE s.relname <> 'spatial_ref_sys';              -- a PostGIS table that is not ours

-- how often each index is used, so unused ones can be found
CREATE OR REPLACE VIEW v_mon_index_usage AS
SELECT s.relname AS table_name, s.indexrelname AS index_name, s.idx_scan, s.idx_tup_read,
       pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size,
       pg_relation_size(s.indexrelid) AS index_bytes,
       am.amname AS index_type
  FROM pg_stat_user_indexes s
  JOIN pg_class c  ON c.oid = s.indexrelid
  JOIN pg_am    am ON am.oid = c.relam
 WHERE s.relname <> 'spatial_ref_sys';

-- locks that are held or requested right now
CREATE OR REPLACE VIEW v_mon_locks AS
SELECT l.locktype, l.mode, l.granted, l.pid, a.usename, c.relname AS relation,
       left(regexp_replace(a.query, '\s+', ' ', 'g'), 100) AS query
  FROM pg_locks l
  JOIN pg_stat_activity a ON a.pid = l.pid
  LEFT JOIN pg_class c ON c.oid = l.relation
 WHERE a.datname = current_database() AND l.pid <> pg_backend_pid()
   AND (c.relname IS NULL OR c.relnamespace = 'public'::regnamespace);

-- the latest run of each feed, to see that collection is still working
CREATE OR REPLACE VIEW v_collector_health AS
SELECT DISTINCT ON (feed)
       feed, status, started_at, finished_at, duration_ms, rows_fetched, rows_ingested, rows_rejected, error_count, message,
       round(EXTRACT(EPOCH FROM (now() - started_at)) / 60.0, 1) AS minutes_ago
  FROM collector_heartbeat
 ORDER BY feed, started_at DESC;

-- 3.3 Permission proof: really tries allowed and forbidden statements as each role, then undoes them
CREATE OR REPLACE FUNCTION dba_rbac_proof()
RETURNS TABLE (role_name text, attempted_statement text, expected text, outcome text, verdict text)
LANGUAGE plpgsql AS $$
DECLARE
    t        RECORD;
    v_out    text;
BEGIN
    FOR t IN
        -- This is the list of statements to try, with the role and the result we expect.
        SELECT * FROM (VALUES
            ('ingest_role',  'INSERT INTO staging_ais (mmsi, lat, lon, event_time) VALUES (230000001, 60.0, 25.0, now())', 'allowed'),
            ('ingest_role',  'SELECT * FROM validate_and_migrate_ais(0)',                                                   'allowed'),
            ('ingest_role',  'UPDATE latest_position SET updated_at = now() WHERE false',                                   'allowed'),
            ('ingest_role',  'SELECT count(*) FROM location_events',                                                        'denied'),
            ('ingest_role',  'DELETE FROM location_events',                                                                 'denied'),
            ('ingest_role',  'DELETE FROM rejected_events',                                                                 'denied'),
            ('ingest_role',  'DROP TABLE ports',                                                                            'denied'),
            ('analyst_role', 'SELECT count(*) FROM location_events',                                                        'allowed'),
            ('analyst_role', 'SELECT count(*) FROM vessel_status_history',                                                  'allowed'),
            ('analyst_role', 'SELECT count(*) FROM detect_going_dark_vessels(20)',                                          'allowed'),
            ('analyst_role', 'SELECT count(*) FROM v_mon_activity',                                                         'allowed'),
            ('analyst_role', 'INSERT INTO staging_ais (mmsi) VALUES (1)',                                                   'denied'),
            ('analyst_role', 'UPDATE latest_position SET last_seen = now()',                                                'denied'),
            ('analyst_role', 'DELETE FROM ais_gap_events',                                                                  'denied'),
            ('analyst_role', 'SELECT * FROM validate_and_migrate_ais(0)',                                                   'denied')
        ) AS x(r, stmt, exp)
    LOOP
        v_out := NULL;
        BEGIN
            -- Here we switch to the role, run the statement and then undo it.
            EXECUTE format('SET LOCAL ROLE %I', t.r);
            EXECUTE t.stmt;
            v_out := 'allowed';
            RAISE EXCEPTION 'probe finished' USING ERRCODE = 'RB001';      -- undo whatever the test did
        EXCEPTION
            WHEN SQLSTATE 'RB001' THEN NULL;
            WHEN insufficient_privilege THEN v_out := 'denied';
            WHEN OTHERS THEN v_out := 'error: ' || SQLERRM;
        END;
        role_name := t.r; attempted_statement := t.stmt; expected := t.exp; outcome := v_out;
        verdict := CASE WHEN v_out = t.exp THEN 'PASS' ELSE 'FAIL' END;
        RETURN NEXT;
    END LOOP;
END;
$$;
REVOKE EXECUTE ON FUNCTION dba_rbac_proof() FROM PUBLIC;

-- 3.4 Index test data: a big copy of the movement table with no indexes, so the before and after
-- times are measured on this machine (the live tables are still small).
CREATE OR REPLACE FUNCTION dba_build_benchmark(p_rows integer DEFAULT 400000) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE n bigint;
BEGIN
    DROP TABLE IF EXISTS bench_location_events;
    CREATE TABLE bench_location_events (
        event_id    bigint,
        mmsi        bigint,
        geom        geometry(Point, 4326),
        speed_knots numeric(5,1),
        event_time  timestamptz
    );
    -- This fills the table with fake rows using random points in the sea area.
    INSERT INTO bench_location_events
    SELECT g,
           230000000 + (g % 2500),
           ST_SetSRID(ST_MakePoint(21.0 + random() * 9.5, 59.0 + random() * 1.8), 4326),
           round((random() * 22)::numeric, 1),
           now() - (g * interval '7 seconds')
      FROM generate_series(1, p_rows) g;
    ANALYZE bench_location_events;
    -- This counts the rows so the function can return the number.
    SELECT count(*) INTO n FROM bench_location_events;
    RETURN n;
END;
$$;

-- @@DEMO
-- Everything below is a demo. setup_db.py does not run it.

\echo '=== A. RBAC PROOF: each role tries allowed and forbidden statements ==='
-- This runs the permission test. Each login tries things it is allowed to do and things it is not
-- allowed to do, and the result shows if the database blocked or allowed them correctly.
SELECT role_name, attempted_statement, expected, outcome, verdict FROM dba_rbac_proof();

\echo '=== B. INDEXING: EXPLAIN (ANALYZE, BUFFERS) before and after ==='
-- This makes a big test table with 400,000 rows and no indexes, so we can see how slow it is first.
SELECT dba_build_benchmark(400000) AS benchmark_rows;

\echo '--- B1 BEFORE: movement history of one ship (no index) ---'
-- This gets the last 3 days of positions of one ship, newest first.
-- Without an index the database has to read every row in the table to find them.
EXPLAIN (ANALYZE, BUFFERS)
SELECT event_id, event_time, speed_knots FROM bench_location_events
 WHERE mmsi = 230000042 AND event_time > now() - interval '3 days' ORDER BY event_time DESC;

\echo '--- B2 BEFORE: ships within 10 km of Helsinki (no spatial index) ---'
-- This counts the positions that are within 10 km of Helsinki.
-- Without a spatial index it has to measure the distance for every single row.
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) FROM bench_location_events
 WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(24.94, 60.17), 4326)::geography, 10000);

-- These two lines add the indexes: one for finding a ship's history fast and one for finding
-- positions near a place fast. ANALYZE then updates the table statistics so the planner uses them.
CREATE INDEX IF NOT EXISTS idx_bench_mmsi_time ON bench_location_events (mmsi, event_time DESC);
CREATE INDEX IF NOT EXISTS gist_bench_geom     ON bench_location_events USING gist ((geom::geography));
ANALYZE bench_location_events;

\echo '--- B1 AFTER: B-tree on (mmsi, event_time DESC) ---'
-- This is the same ship history query as before, run again now that the index exists.
-- It should jump straight to that ship's rows and be much faster.
EXPLAIN (ANALYZE, BUFFERS)
SELECT event_id, event_time, speed_knots FROM bench_location_events
 WHERE mmsi = 230000042 AND event_time > now() - interval '3 days' ORDER BY event_time DESC;

\echo '--- B2 AFTER: GiST on the geography expression ---'
-- This is the same Helsinki distance query as before, run again with the spatial index.
-- Now it only looks at positions that are close to Helsinki instead of the whole table.
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) FROM bench_location_events
 WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(24.94, 60.17), 4326)::geography, 10000);

\echo '=== C. CONCURRENCY: row-level locking on a vessel''s state ==='
-- C1. All or nothing: a whole batch is saved together or not at all.
-- This adds one test position, runs the checking function, shows the ship is there, and then does
-- ROLLBACK. The last count shows 0, which proves that nothing was kept after the rollback.
BEGIN;
    INSERT INTO staging_ais (mmsi, lat, lon, sog, event_time, source)
    VALUES (799000090, 60.0, 25.0, 10, now() - interval '1 minute', 'dba-demo');
    SELECT * FROM validate_and_migrate_ais();
    SELECT mmsi, last_seen FROM latest_position WHERE mmsi = 799000090;      -- visible inside the transaction
ROLLBACK;                                                                    -- and gone after it
SELECT count(*) AS rows_left_after_rollback FROM latest_position WHERE mmsi = 799000090;

-- C2. Two updates to the same ship at the same time. Open two psql sessions:
--   SESSION 1                                              SESSION 2
--   BEGIN;
--   SELECT mmsi, last_seen FROM latest_position
--    WHERE mmsi = 230000001 FOR UPDATE;      -- row is locked
--                                                          BEGIN;
--                                                          SELECT ... WHERE mmsi = 230000001 FOR UPDATE NOWAIT;
--                                                          -- error: could not get the lock on the row
--                                                          SELECT ... WHERE mmsi = 230000001 FOR UPDATE SKIP LOCKED;
--                                                          -- returns 0 rows, locked rows are skipped
--                                                          UPDATE latest_position SET speed_knots = 9 WHERE mmsi = 230000001;
--                                                          -- waits here
--   (from a third session you can see it with:  SELECT * FROM v_mon_blocking;)
--   COMMIT;                                              -- session 2 now continues
--
-- simulate_demo.py runs the same test with real connections and prints the result.

\echo '=== D. MONITORING ==='
\echo '--- D1 sessions (pg_stat_activity) ---'
-- This shows who is connected to the database right now and what each connection is doing.
SELECT pid, usename, application_name, state, wait_event_type, wait_event, query_age_s, query FROM v_mon_activity;
\echo '--- D2 lock waits ---'
-- This shows if any connection is stuck waiting for another one. It is empty when nothing is blocked.
SELECT * FROM v_mon_blocking;
\echo '--- D3 buffer cache hit ratio ---'
-- This shows how often data was found in memory instead of being read from disk. Higher is better.
SELECT * FROM v_mon_cache_hit WHERE scope = 'database' OR hit_pct IS NOT NULL ORDER BY scope, object LIMIT 12;
\echo '--- D4 table health (dead tuples, vacuum, HOT updates) ---'
-- This lists the biggest tables with how many old dead rows they have and when vacuum last cleaned them.
SELECT table_name, live_rows, dead_rows, dead_pct, hot_updates, total_size, last_vacuum FROM v_mon_table_health ORDER BY total_bytes DESC LIMIT 10;
\echo '--- D5 index usage ---'
-- This shows how many times each index was used, so we can spot indexes that are never used.
SELECT table_name, index_name, index_type, idx_scan, index_size FROM v_mon_index_usage ORDER BY idx_scan DESC LIMIT 12;
\echo '--- D6 collector heartbeat ---'
-- This shows the last run of each data feed: did it work, how long ago, and how many rows it got.
SELECT feed, status, minutes_ago, rows_fetched, rows_ingested, rows_rejected, message FROM v_collector_health;

\echo '=== E. BACKUP & RECOVERY: run  ./backup_restore.sh  (pg_dump -Fc, pg_restore --list, restore into a scratch DB, row-count comparison) ==='
