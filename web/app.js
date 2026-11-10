/* Dark Ship Watch - core: shell, KPIs, live map, side panel, incidents.  Other tabs live in panels.js */
const DSW = (() => {
  'use strict';
  const S = {
    tab: 'map', ships: [], gaps: [], summary: null, sel: null, showSyn: true,
    markers: new Map(), regionCache: new Map(), layerOn: {}, refreshing: false, replay: null,
  };
  const ext = { tabs: {} };                         // panels.js registers { id: {label, icon, render(el), refresh(el)} }

  /* helpers */
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pad = n => String(n).padStart(2, '0');
  const fmt = {
    n: (v, d = 0) => v == null ? '–' : Number(v).toLocaleString('en-US', { maximumFractionDigits: d, minimumFractionDigits: d }),
    t: iso => { if (!iso) return '–'; const d = new Date(iso); return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`; },
    ts: iso => { if (!iso) return '–'; const d = new Date(iso); return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`; },
    dur: m => { if (m == null) return '–'; m = Number(m); if (m < 60) return `${Math.round(m)} min`; const h = Math.floor(m / 60); return `${h} h ${Math.round(m - h * 60)} min`; },
    age: s => { if (s == null) return '–'; s = Number(s); if (s < 90) return `${Math.round(s)} s`; if (s < 5400) return `${Math.round(s / 60)} min`; return `${(s / 3600).toFixed(1)} h`; },
  };
  async function api(path, opts) {
    const r = await fetch(path, opts);
    let body = null;
    try { body = await r.json(); } catch (e) { /* non-JSON */ }
    if (!r.ok) throw new Error((body && body.error) || `HTTP ${r.status}`);
    return body;
  }
  const post = (path, data) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-DSW': '1' }, body: JSON.stringify(data || {}) });
  function toast(msg, err = false, ms = 5200) {
    const el = document.createElement('div'); el.className = 'toast' + (err ? ' err' : ''); el.textContent = msg;
    $('#toasts').appendChild(el); setTimeout(() => el.remove(), ms);
  }
  function busy(on, text) { $('#busy').classList.toggle('on', on); if (text) $('#busyText').textContent = text; }
  const badgeFor = c => ({
    DARK_ACTIVITY: ['b-dark', 'Going dark'], IN_PORT: ['b-port', 'In port'], STATIONARY: ['b-still', 'Parked'],
    RECEIVER_OUTAGE: ['b-outage', 'Our receiver was down'], LEFT_AREA: ['b-left', 'Left the area'],
  }[c] || ['b-still', c]);
  const classBadge = c => { const [k, t] = badgeFor(c); return `<span class="badge ${k}">${t}</span>`; };
  const synBadge = s => s ? ' <span class="badge b-syn">DEMO</span>' : '';
  const words = {
    status: { OK: 'Working', PARTIAL: 'Partly working', ERROR: 'Failed' },
    feed: { positions: 'Ship positions', vessels: 'Ship details', weather: 'Wind', sea_state: 'Sea and waves', engine: 'Dark-ship check' },
    reason: c => ({ NULL_MMSI: 'No ship ID', INVALID_MMSI: 'Invalid ship ID', MISSING_FIELDS: 'Missing time or position', INVALID_COORDINATES: 'Impossible position',
      OUT_OF_AREA: 'Outside the Gulf of Finland', UNREALISTIC_SPEED: 'Impossible speed', FUTURE_TIMESTAMP: 'Time in the future', DUPLICATE: 'Already stored', IMPOSSIBLE_JUMP: 'Impossible jump' }[c] || c),
    basis: b => !b ? '' : /within \+\/-/.test(b) ? 'wind and wave readings from around that time' : /fallback/.test(b) ? 'no readings from that time, so the closest in time were used' : /no observations/.test(b) ? 'no weather data, so no slowdown was applied' : b,
  };

  /* icons */
  const ICON = {
    map: '<path d="M3 6l6-2 6 2 6-2v14l-6 2-6-2-6 2z"/><path d="M9 4v14M15 6v14"/>',
    dark: '<circle cx="12" cy="12" r="9"/><path d="M12 12L19 7"/><circle cx="12" cy="12" r="3"/><path d="M12 3v2M3 12h2"/>',
    pipe: '<rect x="3" y="4" width="5" height="5" rx="1"/><rect x="16" y="4" width="5" height="5" rx="1"/><rect x="9.5" y="15" width="5" height="5" rx="1"/><path d="M8 6.5h8M12 9v6"/>',
    temporal: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    ports: '<circle cx="12" cy="5" r="2"/><path d="M12 7v13M6 12H4a8 8 0 0 0 16 0h-2M8 10h8"/>',
    dba: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    guide: '<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>',
    arch: '<rect x="3" y="3" width="18" height="5" rx="1"/><rect x="3" y="10" width="18" height="5" rx="1"/><rect x="3" y="17" width="18" height="4" rx="1"/>',
  };
  const TAB_DEFS = [
    { id: 'map', label: 'Live Map' }, { id: 'guide', label: 'How it works' }, { id: 'dark', label: 'Going Dark' }, { id: 'pipe', label: 'Pipeline' },
    { id: 'temporal', label: 'Temporal' }, { id: 'ports', label: 'Ports' }, { id: 'dba', label: 'DBA Console' }, { id: 'arch', label: 'Architecture' },
  ];

  /* tabs */
  function buildTabs() {
    $('#tabs').innerHTML = TAB_DEFS.map(t => `<button data-tab="${t.id}"><svg viewBox="0 0 24 24">${ICON[t.id === 'dark' ? 'dark' : t.id]}</svg>${t.label}</button>`).join('');
    $$('#tabs button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
  }
  function showTab(id) {
    S.tab = id;
    $('#kpis').style.display = id === 'map' ? '' : 'none';      // the number strip belongs to the map page only
    $$('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === id));
    $$('section.tab').forEach(s => s.classList.toggle('active', s.id === 'tab-' + id));
    if (id === 'map') { setTimeout(() => S.map && S.map.invalidateSize(), 50); return; }
    const t = ext.tabs[id];
    if (t) t.render($('#tab-' + id + ' .page'));
    history.replaceState(null, '', '#' + id);
  }

  /* KPIs / live pill */
  function renderKpis() {
    const s = S.summary; if (!s) return;
    const k = [
      ['Ships active now', fmt.n(s.active_now), `${fmt.n(s.tracked_6h)} seen in the last 6 hours`, 'teal'],
      ['Positions stored', fmt.n(s.fixes_stored), `${fmt.n(s.fixes_24h)} added in the last 24 hours`, ''],
      ['Refused records', fmt.n(s.rejected_total), `${fmt.n(s.rejected_24h)} in the last 24 hours, each with a reason`, 'brass'],
      ['Silent right now', fmt.n(s.dark_open), 'ships that stopped sending and are unexplained', 'alert'],
      ['Silent, came back', fmt.n(s.dark_closed), `${fmt.n(s.gaps_ignored)} more silences had a normal reason`, 'alert'],
      ['Port visits (24 h)', fmt.n(s.port_events_24h), 'arrivals and departures found automatically', 'brass'],
      ['Wind readings', fmt.n(s.weather_obs_2h), 'from the Finnish Meteorological Institute', ''],
      ['Database size', s.db_size, s.synthetic_vessels ? `${s.synthetic_vessels} demo ships included` : 'real data only', 'violet'],
    ];
    $('#kpis').innerHTML = k.map(([l, v, sub, c]) => `<div class="kpi ${c}" title="${esc(l + ': ' + sub)}"><label>${l}</label><b>${v}</b><small>${esc(sub)}</small></div>`).join('');
    const age = s.newest_fix_age_s;
    const dot = $('#liveDot'); dot.className = 'dot' + (age == null || age > 900 ? ' bad' : age > 240 ? ' warn' : '');
    $('#liveText').textContent = age == null ? 'no data yet' : `Live · updated ${fmt.age(age)} ago`;
  }
  function tickClock() { const d = new Date(); $('#clock').textContent = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`; }

  /* map */
  const STATE_LABEL = { underway: 'moving', still: 'stationary', inport: 'in port', stale: 'quiet for a few minutes', silent: 'silent (normal reason)', dark: 'going dark' };
  function initMap() {
    if (typeof L === 'undefined') { $('#map').innerHTML = '<div class="empty" style="padding-top:40vh">The map library could not be loaded (internet needed for Leaflet/tiles).</div>'; return; }
    const map = S.map = L.map('map', { center: [60.0, 25.4], zoom: 8, minZoom: 5, zoomControl: false, attributionControl: true });
    L.control.zoom({ position: 'bottomright' }).addTo(map);
    const esri = 'https://server.arcgisonline.com/ArcGIS/rest/services';
    S.base = {
      // Night chart: OpenStreetMap tiles, darkened with a CSS filter. They exist at every zoom level, including open sea.
      dark: L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 18, className: 'tiles-dark', attribution: '&copy; OpenStreetMap contributors' }),
      // Ocean chart: Esri has real data only up to zoom 10 here, so deeper zoom stretches that level instead of showing blanks.
      ocean: L.layerGroup([
        L.tileLayer(`${esri}/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}`, { maxNativeZoom: 10, maxZoom: 14, attribution: 'Tiles &copy; Esri, GEBCO, NOAA, National Geographic' }),
        L.tileLayer(`${esri}/Ocean/World_Ocean_Reference/MapServer/tile/{z}/{y}/{x}`, { maxNativeZoom: 10, maxZoom: 14 }),
      ]),
    };
    S.base.dark.addTo(map);
    const canvas = L.canvas({ padding: 0.5 });
    S.layers = {
      ships: L.layerGroup(), incidents: L.layerGroup(), regions: L.layerGroup(), circles: L.layerGroup(), ports: L.layerGroup(),
      wind: L.layerGroup(), waves: L.layerGroup(), visited: L.layerGroup(), never: L.layerGroup(),
      seamarks: L.tileLayer('https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png', { maxZoom: 18, attribution: 'Sea marks &copy; OpenSeaMap' }),
      selection: L.layerGroup(), trail: L.layerGroup(),
    };
    S.canvas = canvas;
    $$('#layerPanel [data-layer]').forEach(cb => {
      const name = cb.dataset.layer; S.layerOn[name] = cb.checked;
      cb.addEventListener('change', () => { S.layerOn[name] = cb.checked; applyLayer(name); });
    });
    $$('#layerPanel [name=base]').forEach(r => r.addEventListener('change', () => {
      Object.values(S.base).forEach(l => map.removeLayer(l)); S.base[r.value].addTo(map);
      (S.base[r.value].eachLayer ? S.base[r.value].eachLayer(l => l.bringToBack && l.bringToBack()) : S.base[r.value].bringToBack());
    }));
    $('#optSyn').addEventListener('change', e => { S.showSyn = e.target.checked; refreshAll(); });
    ['ports', 'ships', 'incidents', 'regions', 'selection', 'trail'].forEach(n => S.layers[n].addTo(map));
    S.layerOn.ports = true;
    ['circles', 'wind', 'waves', 'visited', 'never', 'seamarks'].forEach(n => { if (S.layerOn[n]) S.layers[n].addTo(map); });
    map.on('click', () => { /* clicking water clears selection */ if (S.sel) clearSelection(); });
  }
  function applyLayer(name) {
    const g = S.layers[name], on = S.layerOn[name];
    if (on && !S.map.hasLayer(g)) g.addTo(S.map); else if (!on && S.map.hasLayer(g)) S.map.removeLayer(g);
    if (on && name === 'wind' || on && name === 'waves') loadEnvironment();
    if (on && (name === 'visited' || name === 'never')) loadTraffic();
    if (name === 'regions' || name === 'incidents') renderIncidents();
    if (name === 'circles') renderSelection();
  }

  function shipIcon(sh, selected) {
    const rot = (sh.heading != null && sh.heading < 360) ? sh.heading : (sh.course != null ? sh.course : 0);
    const cls = `ship ${sh.state}${sh.is_synthetic ? ' syn' : ''}${selected ? ' sel' : ''}`;
    return { html: `<div class="${cls}" style="position:relative"><svg viewBox="0 0 20 20" style="transform:rotate(${rot}deg)"><path d="M10 1 L16.5 18 L10 14 L3.5 18 Z"/></svg></div>`, key: cls + rot };
  }
  function shipTip(sh) {
    return `<b>${esc(sh.name || 'Ship ' + sh.mmsi)}</b>${sh.is_synthetic ? ' <i>(demo)</i>' : ''}<br>${esc(sh.ship_class)} · ${fmt.n(sh.speed_knots, 1)} knots · ${STATE_LABEL[sh.state] || sh.state}<br><span style="color:#8aa9bf">last heard ${fmt.dur(sh.silent_min)} ago${sh.destination ? ' · → ' + esc(sh.destination) : ''}</span>`;
  }
  function renderShips() {
    if (!S.map) return;
    const seen = new Set();
    for (const sh of S.ships) {
      if (!S.showSyn && sh.is_synthetic) continue;
      seen.add(sh.mmsi);
      const ic = shipIcon(sh, S.sel && S.sel.type === 'ship' && S.sel.id === sh.mmsi);
      let m = S.markers.get(sh.mmsi);
      if (!m) {
        m = L.marker([sh.lat, sh.lon], { icon: L.divIcon({ className: '', html: ic.html, iconSize: [18, 18], iconAnchor: [9, 9] }), riseOnHover: true, zIndexOffset: sh.state === 'dark' ? 800 : 0 });
        m.on('click', e => { L.DomEvent.stopPropagation(e); openShip(sh.mmsi); });
        m.bindTooltip(shipTip(sh), { direction: 'top', offset: [0, -8] });
        m._key = ic.key; S.layers.ships.addLayer(m); S.markers.set(sh.mmsi, m);
      } else {
        m.setLatLng([sh.lat, sh.lon]);
        if (m._key !== ic.key) { m.setIcon(L.divIcon({ className: '', html: ic.html, iconSize: [18, 18], iconAnchor: [9, 9] })); m._key = ic.key; }
        m.setTooltipContent(shipTip(sh));
      }
    }
    for (const [id, m] of S.markers) if (!seen.has(id)) { S.layers.ships.removeLayer(m); S.markers.delete(id); }
  }

  const pinIcon = (cls, size = 16) => L.divIcon({ className: '', html: `<div class="pin ${cls}"></div>`, iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
  const radarIcon = () => L.divIcon({ className: '', html: '<div class="radar-ring"></div>', iconSize: [46, 46], iconAnchor: [23, 23] });
  const labelIcon = (txt, dy = -22) => L.divIcon({ className: '', html: `<div class="pin-label">${esc(txt)}</div>`, iconSize: [0, 0], iconAnchor: [-10, dy * -1 - 8] });

  function renderIncidents() {
    if (!S.map) return;
    S.layers.incidents.clearLayers(); S.layers.regions.clearLayers();
    for (const g of S.gaps) {
      if (!S.showSyn && g.is_synthetic) continue;
      const open = g.status === 'OPEN';
      const p1 = [g.p1_lat, g.p1_lon];
      const tip = `<b>${esc(g.name || 'Ship ' + g.mmsi)}</b>${g.is_synthetic ? ' <i>(demo)</i>' : ''}<br>${open ? 'silent for' : 'was silent for'} ${fmt.dur(g.gap_duration_minutes)}<br>${fmt.t(g.last_seen_time)}${g.reappear_time ? ' → ' + fmt.t(g.reappear_time) : ' → still silent'} UTC`;
      if (open) {
        const r = L.marker(p1, { icon: radarIcon(), interactive: false }); S.layers.incidents.addLayer(r);
        const pin = L.marker(p1, { icon: pinIcon('alert') }).bindTooltip(tip, { direction: 'top' }); pin.on('click', e => { L.DomEvent.stopPropagation(e); openGap(g.gap_id); }); S.layers.incidents.addLayer(pin);
      } else {
        const a = L.marker(p1, { icon: pinIcon('') }).bindTooltip(tip + '<br>last seen here', { direction: 'top' });
        const b = L.marker([g.p2_lat, g.p2_lon], { icon: pinIcon('p2') }).bindTooltip(tip + '<br>came back here', { direction: 'top' });
        const line = L.polyline([p1, [g.p2_lat, g.p2_lon]], { color: '#e5807e', weight: 2, dashArray: '6 7', opacity: 0.85 });
        [a, b, line].forEach(l => { l.on('click', e => { L.DomEvent.stopPropagation(e); openGap(g.gap_id); }); S.layers.incidents.addLayer(l); });
      }
      if (S.layerOn.regions && g.has_region && !(S.sel && S.sel.type === 'gap' && S.sel.id === g.gap_id)) drawFaintRegion(g.gap_id);
    }
  }
  async function drawFaintRegion(id) {
    let d = S.regionCache.get(id);
    if (!d) { try { d = await api('/api/gap/' + id); S.regionCache.set(id, d); } catch (e) { return; } }
    const geo = d.tight || d.lens; if (!geo || !S.layerOn.regions) return;
    L.geoJSON(geo, { style: { color: '#9fbccb', weight: 1, fillColor: '#9fbccb', fillOpacity: 0.1, opacity: 0.6 }, interactive: false }).addTo(S.layers.regions);
  }

  async function loadPorts() {
    const ports = await api('/api/ports' + synQ('?'));
    S.ports = ports; S.layers.ports.clearLayers();
    ports.forEach(p => {
      L.geoJSON(p.zone, { style: { color: '#d5e2e8', weight: 1.2, dashArray: '4 5', fillColor: '#d5e2e8', fillOpacity: 0.07 }, interactive: false }).addTo(S.layers.ports);
      L.marker([p.lat, p.lon], { icon: L.divIcon({ className: '', html: `<div class="portpin">⚓ ${esc(p.name)}</div>`, iconSize: [0, 0], iconAnchor: [-6, 6] }), interactive: false }).addTo(S.layers.ports);
    });
  }
  async function loadEnvironment() {
    if (!S.map || !(S.layerOn.wind || S.layerOn.waves)) return;
    const env = await api('/api/environment');
    S.layers.wind.clearLayers(); S.layers.waves.clearLayers();
    env.wind.forEach(w => {
      const sp = w.wind_speed_ms, col = sp < 5 ? '#c8d9e1' : sp < 10 ? '#a9c5d1' : sp < 15 ? '#9fbccb' : '#7ba7b9';
      const rot = ((w.wind_dir_deg ?? 0) + 180) % 360;                       // arrow points where the wind blows TO
      const ic = L.divIcon({ className: 'windarrow', html: `<svg viewBox="0 0 24 24" style="transform:rotate(${rot}deg)"><path d="M12 2 L17 14 L12 11 L7 14 Z" fill="${col}" stroke="#04121f" stroke-width="1"/><path d="M12 11 V21" stroke="${col}" stroke-width="2"/></svg>`, iconSize: [22, 22], iconAnchor: [11, 11] });
      L.marker([w.lat, w.lon], { icon: ic }).bindTooltip(`<b>Wind ${fmt.n(sp, 1)} m/s</b> (gust ${fmt.n(w.wind_gust_ms, 1)})<br>from ${w.wind_dir_deg ?? '?'}° · ${fmt.t(w.observed_at)} UTC`).addTo(S.layers.wind);
    });
    env.waves.filter(w => w.wave_height_m != null).forEach(w => {
      L.marker([w.lat, w.lon], { icon: L.divIcon({ className: '', html: `<div class="wavelabel">${fmt.n(w.wave_height_m, 1)} m</div>`, iconSize: [0, 0], iconAnchor: [14, 9] }) })
        .bindTooltip(`<b>Significant wave height ${fmt.n(w.wave_height_m, 1)} m</b><br>${esc(w.source)} · ${fmt.t(w.observed_at)} UTC`).addTo(S.layers.waves);
    });
  }
  async function loadTraffic() {
    if (!S.map || !(S.layerOn.visited || S.layerOn.never)) return;
    const d = await api('/api/traffic');
    S.layers.visited.clearLayers(); S.layers.never.clearLayers();
    if (!d.built) { toast('The busy-lanes map is not built yet. Use Demo controls, then Rebuild traffic map.'); return; }
    const c = d.cell, max = Math.max(1, ...d.visited.map(v => v.visits)), logMax = Math.log(max + 1);
    d.visited.forEach(v => L.rectangle([[v.lat, v.lon], [v.lat + c, v.lon + c]], { renderer: S.canvas, stroke: false, fillColor: '#6f96ab', fillOpacity: 0.12 + 0.5 * Math.log(v.visits + 1) / logMax, interactive: false }).addTo(S.layers.visited));
    d.never.forEach(v => L.rectangle([[v.lat, v.lon], [v.lat + c, v.lon + c]], { renderer: S.canvas, stroke: false, fillColor: '#6f8797', fillOpacity: 0.22, interactive: false }).addTo(S.layers.never));
  }

  /* selection */
  function clearSelection() {
    const prev = S.sel; S.sel = null;
    S.layers.selection.clearLayers(); S.layers.trail.clearLayers(); S.layers.circles.clearLayers();
    stopReplay(); if (prev && prev.type === 'ship') renderShips();
    renderOverview();
    if (prev && prev.type === 'gap') renderIncidents();
  }
  function openSide() { $('#side').classList.remove('hidden'); }
  function setSide(title, meta, html) { $('#sideTitle').textContent = title; $('#sideMeta').innerHTML = meta || ''; $('#sideBody').innerHTML = html; openSide(); }

  async function openShip(mmsi, focus = true) {
    showTab('map');
    const prev = S.sel; S.sel = { type: 'ship', id: mmsi };
    S.layers.selection.clearLayers(); S.layers.trail.clearLayers(); S.layers.circles.clearLayers(); stopReplay();
    renderShips(); if (prev && prev.type === 'gap') renderIncidents();
    let d;
    try { d = await api(`/api/ship/${mmsi}?hours=12`); } catch (e) { toast(e.message, true); return; }
    const v = d.vessel; S.shipData = d;
    const pts = d.track.map(p => [p.lat, p.lon]);
    if (pts.length > 1) {
      L.polyline(pts, { color: '#0b2438', weight: 6, opacity: 0.8 }).addTo(S.layers.trail);
      L.polyline(pts, { color: '#9fbccb', weight: 2.5, opacity: 0.95 }).addTo(S.layers.trail);
      d.track.forEach((p, i) => { if (i % 3 === 0) L.circleMarker([p.lat, p.lon], { radius: 2.4, color: '#9fbccb', fillColor: '#04121f', fillOpacity: 1, weight: 1 }).bindTooltip(`${fmt.t(p.event_time)} UTC · ${fmt.n(p.speed_knots, 1)} kn`).addTo(S.layers.trail); });
      if (focus) S.map.fitBounds(L.latLngBounds(pts).pad(0.4), { maxZoom: 11, paddingTopLeft: [400, 20] });
    } else if (v.lat != null && focus) S.map.setView([v.lat, v.lon], 11);
    // gaps of this vessel: show markers
    d.gaps.filter(g => g.is_dark_activity).forEach(g => { /* shown via incidents layer */ });
    renderShipPanel(d);
  }

  function renderShipPanel(d) {
    const v = d.vessel;
    const hist = d.history.map(h => `<tr><td>${esc(h.destination || '–')}</td><td>${fmt.ts(h.eta)}</td><td>${fmt.t(h.valid_from)}</td><td>${h.valid_to ? fmt.t(h.valid_to) : '<span class="badge b-ok">now</span>'}</td></tr>`).join('');
    const gaps = d.gaps.map(g => `<tr class="click" data-gap="${g.gap_id}"><td>${classBadge(g.classification)}</td><td>${fmt.dur(g.gap_duration_minutes)}</td><td>${fmt.t(g.last_seen_time)}→${g.reappear_time ? fmt.t(g.reappear_time) : '…'}</td></tr>`).join('');
    const pev = d.port_events.map(e => `<tr><td><span class="badge ${e.event_type === 'ENTRY' ? 'b-ok' : 'b-still'}">${e.event_type === 'ENTRY' ? 'Arrived' : 'Left'}</span></td><td>${esc(e.port)}</td><td>${fmt.ts(e.event_time)}</td></tr>`).join('');
    const rej = d.rejected.map(r => `<tr><td><span class="badge b-still">${esc(words.reason(r.rejection_reason))}</span></td><td>${esc(r.detail || '')}</td></tr>`).join('');
    setSide(v.name || `Ship ${v.mmsi}`, `Ship ID ${v.mmsi} · ${esc(v.ship_class)}${synBadge(v.is_synthetic)}`, `
      <dl class="kv">
        <dt>Position</dt><dd>${v.lat != null ? `${fmt.n(v.lat, 4)}°N, ${fmt.n(v.lon, 4)}°E` : '–'}</dd>
        <dt>Speed and direction</dt><dd>${fmt.n(v.speed_knots, 1)} knots · heading ${fmt.n(v.course, 0)}°</dd>
        <dt>Last heard</dt><dd>${fmt.ts(v.last_seen)} UTC</dd>
        <dt>Destination</dt><dd>${esc(v.destination || '–')} ${v.eta ? `<span style="color:var(--muted)">arrives ${fmt.ts(v.eta)}</span>` : ''}</dd>
        <dt>Size</dt><dd>${v.length_m ? fmt.n(v.length_m, 0) + ' × ' + fmt.n(v.width_m, 0) + ' m' : '–'} · ${fmt.n(v.draught_m, 1)} m below water</dd>
        <dt>IMO number / call sign</dt><dd>${esc(v.imo || '–')} / ${esc(v.call_sign || '–')}</dd>
      </dl>
      <h4>Last 12 hours · ${d.track.length} positions</h4>
      ${d.track.length > 1 ? `<svg class="spark" viewBox="0 0 340 70" id="speedSpark"></svg>
      <div style="display:flex;gap:8px;align-items:center;margin-top:6px"><button class="btn small" id="replayBtn">▶ Replay</button><input type="range" id="replayRange" min="0" max="${d.track.length - 1}" value="${d.track.length - 1}" style="flex:1;accent-color:#7ba7b9"><span class="mono" id="replayTime" style="color:var(--muted)"></span></div>` : '<div class="empty">No positions stored in the last 12 hours.</div>'}
      <h4>Latest reports from this ship</h4>
      ${d.track.length ? `<table class="t"><tr><th>Time (UTC)</th><th class="num">Speed (knots)</th><th>Position</th></tr>${d.track.slice(-6).reverse().map(p => `<tr><td>${fmt.ts(p.event_time)}</td><td class="num">${fmt.n(p.speed_knots, 1)}</td><td>${fmt.n(p.lat, 4)}, ${fmt.n(p.lon, 4)}</td></tr>`).join('')}</table>` : '<div class="empty">No reports in the last 12 hours.</div>'}
      ${v.is_synthetic ? '' : `<div class="sub" style="margin-top:8px"><a href="https://meri.digitraffic.fi/api/ais/v1/locations?mmsi=${v.mmsi}" target="_blank" rel="noopener">Check this ship at the official source \u2197</a> (compare the position and time)</div>`}
      <h4>Destination history</h4>
      ${hist ? `<table class="t"><tr><th>Destination</th><th>Arrival time</th><th>From</th><th>Until</th></tr>${hist}</table>` : '<div class="empty">No destination reported.</div>'}
      ${gaps ? `<h4>Times it went silent</h4><table class="t"><tr><th>What happened</th><th>Silent for</th><th>When (UTC)</th></tr>${gaps}</table>` : ''}
      ${pev ? `<h4>Port visits</h4><table class="t"><tr><th>What</th><th>Port</th><th>Time</th></tr>${pev}</table>` : ''}
      ${rej ? `<h4>Refused records</h4><table class="t">${rej}</table>` : ''}
      <div style="margin-top:14px"><button class="btn small" id="backOverview">← All ships</button> <button class="btn small" id="toTemporal">See full destination history</button></div>`);
    $$('#sideBody [data-gap]').forEach(r => r.addEventListener('click', () => openGap(+r.dataset.gap)));
    $('#backOverview').addEventListener('click', clearSelection);
    $('#toTemporal').addEventListener('click', () => { DSW.temporalFocus = v.mmsi; showTab('temporal'); });
    if (d.track.length > 1) drawSpark(d.track); setupReplay(d.track);
  }
  function drawSpark(track) {
    const svg = $('#speedSpark'); if (!svg) return;
    const W = 340, H = 70, max = Math.max(5, ...track.map(p => +p.speed_knots || 0));
    const t0 = new Date(track[0].event_time).getTime(), t1 = new Date(track[track.length - 1].event_time).getTime() || t0 + 1;
    const pts = track.map(p => `${(((new Date(p.event_time).getTime() - t0) / Math.max(1, t1 - t0)) * (W - 10) + 5).toFixed(1)},${(H - 8 - ((+p.speed_knots || 0) / max) * (H - 18)).toFixed(1)}`);
    svg.innerHTML = `<defs><linearGradient id="sg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#7ba7b9" stop-opacity=".45"/><stop offset="1" stop-color="#7ba7b9" stop-opacity="0"/></linearGradient></defs>
      <polyline points="5,${H - 8} ${pts.join(' ')} ${W - 5},${H - 8}" fill="url(#sg)" stroke="none"/><polyline points="${pts.join(' ')}" fill="none" stroke="#7ba7b9" stroke-width="1.8"/>
      <text x="5" y="10" fill="#8aa9bf" font-size="10">speed · top ${fmt.n(max, 1)} knots</text>`;
  }
  function setupReplay(track) {
    const btn = $('#replayBtn'), rng = $('#replayRange'); if (!btn) return;
    const mk = L.marker([track[0].lat, track[0].lon], { icon: pinIcon('p2', 18), zIndexOffset: 2000 }).addTo(S.layers.trail);
    const show = i => { const p = track[i]; mk.setLatLng([p.lat, p.lon]); $('#replayTime').textContent = fmt.t(p.event_time) + ' UTC'; rng.value = i; };
    show(track.length - 1);
    rng.addEventListener('input', () => { stopReplay(); show(+rng.value); });
    btn.addEventListener('click', () => {
      if (S.replay) { stopReplay(); return; }
      let i = +rng.value >= track.length - 1 ? 0 : +rng.value; btn.textContent = '❚❚ Pause';
      S.replay = setInterval(() => { show(i); i++; if (i >= track.length) stopReplay(); }, 120);
    });
  }
  function stopReplay() { if (S.replay) { clearInterval(S.replay); S.replay = null; const b = $('#replayBtn'); if (b) b.textContent = '▶ Replay'; } }

  /* incident selection */
  async function openGap(id) {
    showTab('map');
    S.sel = { type: 'gap', id };
    S.layers.selection.clearLayers(); S.layers.trail.clearLayers(); stopReplay(); renderShips();
    let d;
    try { d = await api('/api/gap/' + id); } catch (e) { toast(e.message, true); return; }
    S.regionCache.set(id, d); S.gapData = d; renderIncidents(); renderSelection(true); renderGapPanel(d);
  }
  function renderSelection(fit = false) {
    S.layers.selection.clearLayers(); S.layers.circles.clearLayers();
    if (!S.sel || S.sel.type !== 'gap' || !S.gapData) return;
    const d = S.gapData, open = d.status === 'OPEN';
    const bounds = [];
    if (d.track.length > 1) {
      const pts = d.track.map(p => [p.lat, p.lon]);
      L.polyline(pts, { color: '#e5f0f8', weight: 2, opacity: 0.55 }).addTo(S.layers.selection); bounds.push(...pts);
    }
    if (d.lens) { const g = L.geoJSON(d.lens, { style: { color: '#9fbccb', weight: 1.6, dashArray: '6 6', fillColor: '#9fbccb', fillOpacity: open ? 0.16 : 0.07 }, interactive: false }).addTo(S.layers.selection); bounds.push(g.getBounds().getSouthWest(), g.getBounds().getNorthEast()); }
    if (d.tight && !open) { const g = L.geoJSON(d.tight, { style: { color: '#7ba7b9', weight: 2.2, fillColor: '#7ba7b9', fillOpacity: 0.34 }, interactive: false }).addTo(S.layers.selection); bounds.push(g.getBounds().getSouthWest(), g.getBounds().getNorthEast()); }
    const circleStyle = { color: '#d5e2e8', weight: 1.4, dashArray: '3 6', fill: false, interactive: false };
    if (S.layerOn.circles || fit) {
      [d.circle1, d.circle2].forEach(c => { if (c) L.geoJSON(c, circleStyle).addTo(S.layers.circles); });
    }
    L.marker([d.p1_lat, d.p1_lon], { icon: pinIcon(open ? 'alert' : ''), zIndexOffset: 1500 }).bindTooltip(`last seen ${fmt.t(d.last_seen_time)} UTC`, { permanent: true, direction: 'left', offset: [-8, 0] }).addTo(S.layers.selection);
    bounds.push([d.p1_lat, d.p1_lon]);
    if (!open) { L.marker([d.p2_lat, d.p2_lon], { icon: pinIcon('p2'), zIndexOffset: 1500 }).bindTooltip(`came back ${fmt.t(d.reappear_time)} UTC`, { permanent: true, direction: 'right', offset: [8, 0] }).addTo(S.layers.selection); bounds.push([d.p2_lat, d.p2_lon]); }
    if (fit && bounds.length) S.map.fitBounds(L.latLngBounds(bounds).pad(0.15), { maxZoom: 11, paddingTopLeft: [410, 30] });
  }

  function renderGapPanel(d) {
    const open = d.status === 'OPEN', c = d.conditions || {};
    const dur = Number(d.gap_duration_minutes || 0);
    const inPort = d.classification === 'IN_PORT', still = d.classification === 'STATIONARY', left = d.classification === 'LEFT_AREA', outage = d.classification === 'RECEIVER_OUTAGE';
    const checks = [
      [!inPort, 'It was not in a port', inPort ? 'Its last position was inside a port zone, so silence is normal.' : 'Its last position was outside every port zone.'],
      [!still, 'It was moving when it went quiet', still ? 'It was parked or anchored. Parked ships often switch their transmitter off.' : 'It was travelling at a normal speed.'],
      [!left, 'It did not leave our area', left ? 'It was at the edge of the area we cover, so it probably just sailed out of range.' : 'It was well inside the Gulf of Finland.'],
      [!outage, `Nearby ships kept sending (${d.neighbours_silent ?? 0} of ${d.neighbours_before ?? 0} went quiet)`, outage ? 'Nearby ships went quiet too, so our receiver was probably the problem, not the ship.' : 'Ships within 15 km kept reporting, so our receiver was working. The silence belongs to this ship.'],
    ];
    const chk = checks.map(([ok, t, s]) => `<div class="check"><span class="ic ${ok ? 'ok' : 'no'}">${ok ? '✓' : '✕'}</span><div>${esc(t)}<small>${esc(s)}</small></div></div>`).join('');
    const hours = c.gap_hours ?? (dur / 60);
    const formula = d.v_eff_knots == null ? '<div class="empty">The possible area has not been calculated yet.</div>' : `<div class="formula">
      1. A ${esc((c.ship_class || 'ship').toLowerCase())} ship can sail at about <b>${fmt.n(d.v_max_knots, 1)} knots</b> at most.
      <br>2. Wind (${fmt.n(c.wind_ms, 1)} m/s) and waves (${fmt.n(c.wave_m, 1)} m) slow it, so we multiply by <b>${fmt.n(d.speed_factor, 2)}</b>. That gives <b>${fmt.n(d.v_eff_knots, 1)} knots</b>.
      <br>3. It was silent for <b>${fmt.n(hours, 2)} hours</b>, so it could travel at most <b>${fmt.n(d.reach_radius_km, 1)} km</b>.
      ${open ? '<br>4. The possible area is everything within that distance of the last position, without land. It keeps growing while the ship stays silent.' : `<br>4. The two positions are <b>${fmt.n(d.observed_distance_km, 1)} km</b> apart, so the ship averaged <b>${fmt.n(d.implied_speed_knots, 1)} knots</b>.
      <br>5. The possible area is the sea within reach of both positions, without land: <b>${fmt.n(d.feasible_area_km2, 0)} km²</b>. The tightest possible shape is <b>${fmt.n(d.tight_area_km2, 0)} km²</b>. Land removed: ${fmt.n(d.land_removed_km2, 0)} km².`}</div>
      <div class="note">This shows where the ship <b>could</b> have been, not where it went. The weather slowdown is a rough guess, not marine science. Weather used: ${esc(words.basis(c.basis))}.${d.feasibility_status === 'INFEASIBLE' ? ' <b style="color:#f0aeac">Warning:</b> the ship reappeared farther away than it could have travelled in that time.' : ''}</div>`;
    const barLeft = 8, barW = 100 - 16;
    setSide(d.name || `Ship ${d.mmsi}`, `Ship ID ${d.mmsi} · ${esc(d.ship_class)}${synBadge(d.is_synthetic)}`, `
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">${classBadge(d.classification)} <span class="badge ${open ? 'b-open' : 'b-closed'}">${open ? 'Still silent' : 'Came back'}</span>
        <span style="color:var(--muted);font-size:12.5px">${open ? 'silent for' : 'was silent for'} <b style="color:var(--text)">${fmt.dur(dur)}</b></span></div>
      <div class="timeline-bar"><div class="rail"></div>
        <div class="dark" style="left:${barLeft}%;width:${barW}%"></div>
        <div class="tick" style="left:${barLeft}%">${fmt.t(d.last_seen_time)}</div><div class="tick" style="left:${barLeft + barW}%">${open ? 'now' : fmt.t(d.reappear_time)}</div>
        <div class="tick b" style="left:${barLeft}%">last seen</div><div class="tick b" style="left:${barLeft + barW}%">${open ? 'still silent' : 'came back'}</div></div>
      <h4>${d.is_dark_activity ? 'Why this counts as going dark' : 'Why this does not count as going dark'}</h4>${chk}
      <h4>Where could it have been?</h4>${formula}
      <h4 style="margin-top:16px">On the map</h4>
      <label style="font-size:13px;display:flex;gap:8px;align-items:center"><input type="checkbox" id="gapCircles" ${S.layerOn.circles ? 'checked' : ''} style="accent-color:#d5e2e8"> show the reach circles (how far it could travel from ${open ? 'its last position' : 'each position'})</label>
      <div style="margin-top:14px"><button class="btn small" id="backOverview">← All ships</button> <button class="btn small" id="gapShip">See the ship</button></div>`);
    $('#backOverview').addEventListener('click', clearSelection);
    $('#gapShip').addEventListener('click', () => openShip(d.mmsi));
    $('#gapCircles').addEventListener('change', e => { S.layerOn.circles = e.target.checked; const cb = $('[data-layer=circles]'); if (cb) cb.checked = e.target.checked; applyLayer('circles'); });
  }

  /* overview panel */
  function renderOverview() {
    const dark = S.gaps.filter(g => (S.showSyn || !g.is_synthetic));
    const rows = dark.slice(0, 9).map(g => `<tr class="click" data-gap="${g.gap_id}"><td>${esc(g.name || g.mmsi)}${synBadge(g.is_synthetic)}</td><td><span class="badge ${g.status === 'OPEN' ? 'b-open' : 'b-closed'}">${g.status === 'OPEN' ? 'Still silent' : 'Came back'}</span></td><td class="num">${fmt.dur(g.gap_duration_minutes)}</td></tr>`).join('');
    const counts = S.ships.reduce((a, s) => (a[s.state] = (a[s.state] || 0) + 1, a), {});
    setSide('All ships', `${fmt.n(S.ships.length)} ships on the map · Gulf of Finland`, `
      <div class="search"><input id="shipSearch" placeholder="Find a ship by name or ID…" autocomplete="off"><div class="suggest" id="suggest" style="display:none"></div></div>
      <div class="stat-row"><div><b style="color:var(--teal)">${counts.underway || 0}</b><span>moving</span></div><div><b>${(counts.still || 0) + (counts.stale || 0)}</b><span>parked or quiet</span></div><div><b style="color:var(--brass)">${counts.inport || 0}</b><span>in port</span></div><div><b style="color:var(--alert)">${counts.dark || 0}</b><span>going dark</span></div></div>
      <h4>Ships going dark</h4>
      ${rows ? `<table class="t"><tr><th>Ship</th><th>Status</th><th class="num">Silent for</th></tr>${rows}</table>` : '<div class="empty">No ship is going dark right now.<br>Use <b>Demo controls → Load demo scenario</b> to see an example.</div>'}
      <div class="note">Click a ship to see where it has been. Click a ship under \u201cShips going dark\u201d to see both of its positions and the sea area it could have crossed. A red pulse means silent right now. A dashed ring means a demo ship.</div>`);
    const inp = $('#shipSearch'); let timer;
    inp.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(async () => {
      const q = inp.value.trim(); const box = $('#suggest'); if (q.length < 2) { box.style.display = 'none'; return; }
      const res = await api('/api/vessels/search?q=' + encodeURIComponent(q) + synQ('&'));
      box.innerHTML = res.map(r => `<div data-m="${r.mmsi}"><span>${esc(r.name || 'Unnamed ship')}${synBadge(r.is_synthetic)}</span><span class="mono" style="color:var(--muted)">${r.mmsi}</span></div>`).join('') || '<div>no match</div>';
      box.style.display = 'block'; $$('div[data-m]', box).forEach(d => d.addEventListener('click', () => { box.style.display = 'none'; openShip(+d.dataset.m); }));
    }, 220); });
    $$('#sideBody [data-gap]').forEach(r => r.addEventListener('click', () => openGap(+r.dataset.gap)));
  }

  /* data refresh */
  const synQ = pre => `${pre}synthetic=${S.showSyn ? 1 : 0}`;
  async function refreshAll() {
    if (S.refreshing) return; S.refreshing = true;
    try {
      const [summary, ships, gaps] = await Promise.all([api('/api/summary'), api('/api/ships' + synQ('?')), api('/api/gaps?class=dark' + synQ('&'))]);
      S.summary = summary; S.ships = ships; S.gaps = gaps;
      renderKpis(); renderShips(); renderIncidents();
      if (!S.sel && !(document.activeElement && document.activeElement.id === 'shipSearch')) renderOverview();
      if (S.tab !== 'map') { const t = ext.tabs[S.tab]; if (t && t.refresh) t.refresh($('#tab-' + S.tab + ' .page')); }
      if (S.layerOn.wind || S.layerOn.waves) loadEnvironment();
    } catch (e) { $('#liveDot').className = 'dot bad'; $('#liveText').textContent = 'server unreachable'; console.error(e); }
    finally { S.refreshing = false; }
  }

  /* demo actions */
  const ACTION_LABEL = { seed_demo: 'Loading the demo ships…', collect: 'Fetching the latest live data…', detect: 'Checking for ships that went silent…', traffic_grid: 'Building the busy-lanes map…', reset_demo: 'Removing the demo ships…' };
  async function runAction(name) {
    busy(true, ACTION_LABEL[name] || 'Working…');
    try {
      const r = await post('/api/actions/' + name);
      if (name === 'seed_demo') toast(`Demo ships loaded: ${r.main_batch.accepted} records stored, ${r.main_batch.rejected + r.duplicate_batch.rejected} refused, ${r.gap_sync.opened} silences found, ${r.regions_computed} possible areas drawn.`);
      else if (name === 'collect') toast('Live data updated: ' + Object.entries(r).filter(([k]) => k !== 'elapsed_s').map(([k, v]) => `${words.feed[k] || k}: ${v.ingested} new`).join(' · '));
      else if (name === 'detect') toast(`Check finished: ${r.opened} new silences, ${r.refreshed} re-checked, ${r.regions_computed} possible areas drawn.`);
      else if (name === 'traffic_grid') { toast(`Busy-lanes map built (${r.cells} squares).`); loadTraffic(); }
      else toast('Done.');
      S.regionCache.clear(); await refreshAll();
      if (name === 'seed_demo') {
        showTab('map'); const g = S.gaps.find(x => x.is_synthetic && x.status === 'CLOSED' && x.name === 'SYN DARK STAR') || S.gaps.find(x => x.is_synthetic);
        if (g) openGap(g.gap_id);
      }
      if (name === 'reset_demo') clearSelection();
    } catch (e) { toast(e.message, true); } finally { busy(false); }
  }

  /* boot */
  function boot() {
    buildTabs(); initMap(); tickClock(); setInterval(tickClock, 1000);
    const guide = $('#guide');
    $('#guideBtn').addEventListener('click', () => guide.classList.add('on'));
    $('#guideClose').addEventListener('click', () => guide.classList.remove('on'));
    guide.addEventListener('click', e => { if (e.target === guide) guide.classList.remove('on'); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') guide.classList.remove('on'); });
    $('#lpToggle').addEventListener('click', () => $('#layerPanel').classList.toggle('collapsed'));
    $('#sideClose').addEventListener('click', () => $('#side').classList.add('hidden'));
    $('#sideOpen').addEventListener('click', openSide);
    $('#demoBtn').addEventListener('click', e => { e.stopPropagation(); $('#demoMenu').classList.toggle('open'); });
    document.addEventListener('click', () => $('#demoMenu').classList.remove('open'));
    $$('#demoMenu [data-act]').forEach(b => b.addEventListener('click', () => runAction(b.dataset.act)));
    const start = (location.hash || '#map').slice(1);
    showTab(TAB_DEFS.some(t => t.id === start) ? start : 'map');
    if (S.map) loadPorts().catch(console.error);
    refreshAll(); setInterval(refreshAll, 30000);
    setInterval(() => { if (S.map) loadPorts().catch(() => {}); }, 120000);
  }

  return { boot, S, ext, $, $$, esc, fmt, api, post, toast, busy, classBadge, synBadge, badgeFor, words, showTab, openGap, openShip, refreshAll, runAction, synQ, ICON };
})();
