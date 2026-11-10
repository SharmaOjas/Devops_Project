/* Dark Ship Watch - the pages after "Enter the watch": Going Dark, Pipeline, Temporal, Ports, DBA console, Architecture.
   Every visible word is written in plain language. */
(() => {
  'use strict';
  const { $, $$, esc, fmt, api, post, toast, busy, classBadge, synBadge, ext, words } = DSW;

  /* collapsible cards */
  const FOLD = {};
  document.addEventListener('toggle', e => { const d = e.target; if (d.matches && d.matches('details.fold')) FOLD[d.dataset.fold] = d.open; }, true);
  const CLOSED = new Set(['Latest refused records']);
  const card = (title, hint, body) => {
    const key = title.replace(/<[^>]+>/g, '').trim();
    const open = key in FOLD ? FOLD[key] : !CLOSED.has(key);
    return `<details class="card fold" data-fold="${esc(key)}" ${open ? 'open' : ''}><summary><span class="ft">${title}</span>${hint ? `<span class="fs">${hint}</span>` : ''}</summary><div class="fold-body">${body}</div></details>`;
  };
  const head = (t, tip) => `<div class="page-title"><h2>${t}</h2></div>${tip ? `<div class="tip"><i>i</i><span>${tip}</span></div>` : ''}`;
  const barRows = (rows, max) => `<div class="bars">${rows.map(r => `<div class="bar-row"><span>${r.label}</span><div class="track"><div class="fill" style="width:${max ? Math.max(1.5, 100 * r.value / max) : 0}%;--c:${r.color || '#9fbccb'}"></div></div><span class="v">${fmt.n(r.value)}</span></div>`).join('')}</div>`;
  const statusBadge = s => `<span class="badge ${s === 'OK' ? 'b-ok' : s === 'PARTIAL' ? 'b-warn' : 'b-bad'}">${esc(words.status[s] || s)}</span>`;

  /* ============================================================ GOING DARK */
  const CLASS_COLOR = { IN_PORT: '#d5e2e8', STATIONARY: '#8ea6b4', LEFT_AREA: '#9fbccb', RECEIVER_OUTAGE: '#6f96ab', DARK_ACTIVITY: '#e5807e' };
  const CLASS_NAME = { IN_PORT: 'In port (normal)', STATIONARY: 'Parked or anchored (normal)', LEFT_AREA: 'Left our area (normal)', RECEIVER_OUTAGE: 'Our receiver was down (normal)', DARK_ACTIVITY: 'Unexplained: going dark' };
  const gapFilter = { cls: 'dark', status: 'all' };

  ext.tabs.dark = {
    render(el) {
      el.innerHTML = head('Going Dark', 'A ship is "silent" when it stops sending its position. Most silent ships have a normal reason. Drag the slider to change what counts as silent, and click any ship or row to see it on the map.') + `
        <div class="grid g1">
          ${card('Why ships are silent', 'Of all ships that stopped sending, how many have a normal reason. The red bar is the unexplained ones.',
        `<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px"><label style="color:var(--muted)">Count a ship as silent after</label><input type="range" id="thr" min="5" max="180" step="5" value="20" style="flex:1;accent-color:#e5807e"><b id="thrv" style="min-width:64px">20 min</b></div><div id="funnel"></div>`)}
          ${card('Silent and unexplained', 'These ships stopped sending and nothing explains it. Click one to open it.', '<div id="detectList" class="scroll"></div>')}
        </div>
        <div style="height:16px"></div>
        ${card('History of silences', 'Every time a ship went silent, past and present. Click a row to see it on the map.',
          `<div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap" id="gapFilters"></div><div class="scroll" style="max-height:480px" id="gapTable"></div>`)}`;
      const thr = $('#thr', el);
      let t; thr.addEventListener('input', () => { $('#thrv', el).textContent = thr.value + ' min'; clearTimeout(t); t = setTimeout(() => this.load(el), 250); });
      this.load(el);
    },
    refresh(el) { this.load(el); },
    async load(el) {
      const thr = $('#thr', el).value;
      const [f, det, gaps] = await Promise.all([api(`/api/funnel?threshold=${thr}${DSW.synQ('&')}`), api(`/api/detect?threshold=${thr}${DSW.synQ('&')}`), api('/api/gaps?class=all' + DSW.synQ('&'))]);
      const bc = f.by_class, silent = Object.values(bc).reduce((a, b) => a + b, 0), max = f.tracked_6h || 1;
      const rows = [{ label: 'Ships seen in the last 6 hours', value: f.tracked_6h, color: '#7ba7b9' }, { label: `Silent for over ${f.threshold} minutes`, value: silent, color: '#9fbccb' }]
        .concat(['IN_PORT', 'STATIONARY', 'LEFT_AREA', 'RECEIVER_OUTAGE', 'DARK_ACTIVITY'].map(k => ({ label: '&nbsp;&nbsp;– ' + CLASS_NAME[k], value: bc[k] || 0, color: CLASS_COLOR[k] })));
      $('#funnel', el).innerHTML = barRows(rows, max) + `<div class="note"><b>${fmt.n(silent)}</b> ships are silent. <b>${fmt.n(silent - (bc.DARK_ACTIVITY || 0))}</b> of them have a normal explanation. <b style="color:#f0aeac">${bc.DARK_ACTIVITY || 0}</b> are unexplained.</div>`;
      $('#detectList', el).innerHTML = det.length ? `<table class="t"><tr><th>Ship</th><th class="num">Silent for</th><th class="num">Speed (knots)</th><th class="num">Nearby ships</th></tr>${det.map(d => `<tr class="click" data-m="${d.mmsi}"><td>${esc(d.vessel_name || 'Ship ' + d.mmsi)}${synBadge(d.mmsi >= 799000000)}</td><td class="num">${fmt.dur(d.silent_minutes)}</td><td class="num">${fmt.n(d.last_speed_knots, 1)}</td><td class="num">${d.neighbours_before} / ${d.neighbours_silent}</td></tr>`).join('')}</table><div class="sub" style="margin-top:6px">Nearby ships = ships within 15 km just before it went quiet / how many of them also went quiet.</div>` : '<div class="empty">Nothing is going dark at this setting.</div>';
      $$('#detectList tr[data-m]', el).forEach(r => r.addEventListener('click', () => DSW.openShip(+r.dataset.m)));
      const chips = [['all', 'All'], ['dark', 'Going dark'], ['ignored', 'Explained']].map(([k, l]) => `<button class="btn small ${gapFilter.cls === k ? 'primary' : ''}" data-c="${k}">${l}</button>`).join('') +
        '<span style="flex:1"></span>' + [['all', 'All'], ['OPEN', 'Still silent'], ['CLOSED', 'Came back']].map(([k, l]) => `<button class="btn small ${gapFilter.status === k ? 'primary' : ''}" data-s="${k}">${l}</button>`).join('');
      $('#gapFilters', el).innerHTML = chips;
      $$('#gapFilters [data-c]', el).forEach(b => b.addEventListener('click', () => { gapFilter.cls = b.dataset.c; this.load(el); }));
      $$('#gapFilters [data-s]', el).forEach(b => b.addEventListener('click', () => { gapFilter.status = b.dataset.s; this.load(el); }));
      const list = gaps.filter(g => (gapFilter.cls === 'all' || (gapFilter.cls === 'dark') === g.is_dark_activity) && (gapFilter.status === 'all' || g.status === gapFilter.status));
      $('#gapTable', el).innerHTML = list.length ? `<table class="t"><tr><th>#</th><th>Ship</th><th>What happened</th><th>Status</th><th>Last seen</th><th>Came back</th><th class="num">Silent for</th><th class="num">Nearby ships</th><th class="num">Possible area (km²)</th></tr>
        ${list.map(g => `<tr class="click" data-g="${g.gap_id}"><td>${g.gap_id}</td><td>${esc(g.name || 'Ship ' + g.mmsi)}${synBadge(g.is_synthetic)}</td><td>${classBadge(g.classification)}</td><td><span class="badge ${g.status === 'OPEN' ? 'b-open' : 'b-closed'}">${g.status === 'OPEN' ? 'Still silent' : 'Came back'}</span></td><td>${fmt.ts(g.last_seen_time)}</td><td>${g.reappear_time ? fmt.ts(g.reappear_time) : '–'}</td><td class="num">${fmt.dur(g.gap_duration_minutes)}</td><td class="num">${g.neighbours_before ?? '–'} / ${g.neighbours_silent ?? '–'}</td><td class="num">${g.has_region ? fmt.n(g.tight_area_km2 ?? g.feasible_area_km2) : '–'}</td></tr>`).join('')}</table>` : '<div class="empty">Nothing matches.</div>';
      $$('#gapTable tr[data-g]', el).forEach(r => r.addEventListener('click', () => DSW.openGap(+r.dataset.g)));
    },
  };

  /* ============================================================ PIPELINE */
  ext.tabs.pipe = {
    render(el) {
      el.innerHTML = head('Pipeline', 'This is how live data travels. Read the picture from left to right: new data arrives, the database checks every record, good records are stored, and bad ones are kept with the reason they were refused.') + '<div id="pipeBody"><div class="empty">Loading…</div></div>';
      this.refresh(el);
    },
    async refresh(el) {
      const [p, rej] = await Promise.all([api('/api/pipeline'), api('/api/rejections' + DSW.synQ('?'))]);
      const c = p.counts, max = Math.max(1, ...p.throughput.map(t => t.fixes));
      const checks = ['Ship ID missing or invalid', 'Position impossible or outside the Gulf', 'Speed above 60 knots', 'Time is in the future', 'Already stored', 'Impossible jump (over 50 knots)'];
      const flow = `<svg class="flow" viewBox="0 0 1180 250"><defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#9fbccb"/></marker></defs>
        <g class="node"><rect x="10" y="80" width="170" height="90" rx="12"/><text class="t" x="24" y="106">Live sources</text><text class="s" x="24" y="126">Ship positions and details</text><text class="s" x="24" y="142">Wind, waves, sea state</text><text class="s" x="24" y="158">asked every 1 to 30 minutes</text></g>
        <path class="arrow" d="M182 125 H238"/>
        <g class="node brass"><rect x="240" y="70" width="200" height="110" rx="12"/><text class="t" x="256" y="96">Waiting room</text><text class="s" x="256" y="114">new records, not checked yet</text><text class="n" x="256" y="146">${fmt.n(c.staging)}</text><text class="s" x="256" y="166">waiting to be checked</text></g>
        <path class="arrow" d="M442 125 H498"/>
        <g class="node hot"><rect x="500" y="20" width="270" height="210" rx="12"/><text class="t" x="516" y="46">Checking</text><text class="s" x="516" y="64">every record is tested</text>
          ${checks.map((t, i) => `<text class="s" x="528" y="${92 + i * 20}">✓ ${t}</text>`).join('')}
          <text class="s" x="516" y="226" style="fill:#7ba7b9">A ship’s newest position never goes backwards</text></g>
        <path class="arrow" d="M772 70 H850 V45 H880"/><path class="arrow" d="M772 125 H880"/><path class="arrow red" d="M772 185 H850 V205 H880"/>
        <g class="node hot"><rect x="882" y="14" width="290" height="62" rx="10"/><text class="t" x="896" y="38">Movement history</text><text class="n" x="1090" y="42">${fmt.n(c.location_events)}</text><text class="s" x="896" y="60">every stored position</text></g>
        <g class="node hot"><rect x="882" y="94" width="290" height="62" rx="10"/><text class="t" x="896" y="118">Latest positions</text><text class="n" x="1090" y="122">${fmt.n(c.latest_position)}</text><text class="s" x="896" y="140">newest position of each ship</text></g>
        <g class="node bad"><rect x="882" y="174" width="290" height="62" rx="10"/><text class="t" x="896" y="198">Refused</text><text class="n" x="1090" y="202">${fmt.n(c.rejected_events)}</text><text class="s" x="896" y="220">refused records, with the reason</text></g></svg>`;
      const feeds = ['positions', 'vessels', 'weather', 'sea_state', 'engine'].map(f => {
        const name = words.feed[f];
        const h = p.feeds.find(x => x.feed === f);
        if (!h) return `<div class="feed"><div class="head"><span class="dot warn"></span>${name}</div><div class="meta">Has not run yet</div></div>`;
        const dotc = h.status === 'ERROR' ? 'bad' : (h.minutes_ago > 40 ? 'warn' : '');
        return `<div class="feed"><div class="head"><span class="dot ${dotc}"></span>${name} ${statusBadge(h.status)}</div><div class="meta">Last run ${fmt.n(h.minutes_ago, 1)} min ago<br>received <b>${fmt.n(h.rows_fetched)}</b> · stored <b>${fmt.n(h.rows_ingested)}</b> · refused <b>${fmt.n(h.rows_rejected)}</b><br><span style="color:var(--faint)">${esc((h.message || '').slice(0, 140))}</span></div></div>`;
      }).join('');
      const w = 560, h = 120, bw = Math.max(3, w / Math.max(1, p.throughput.length) - 3);
      const chart = p.throughput.length ? `<svg class="spark" viewBox="0 0 ${w} ${h + 22}">${p.throughput.map((t, i) => { const bh = Math.max(2, (t.fixes / max) * (h - 8)); return `<rect x="${i * (w / p.throughput.length) + 1}" y="${h - bh}" width="${bw}" height="${bh}" rx="2" fill="#7ba7b9" opacity=".85"><title>${fmt.t(t.minute)} UTC: ${t.fixes} positions</title></rect>`; }).join('')}<text x="2" y="${h + 16}" fill="#8aa9bf" font-size="11">Last hour: positions stored per minute (highest: ${fmt.n(max)})</text></svg>` : '<div class="empty">Nothing was stored in the last hour. Is the collector running?</div>';
      const reasons = p.reasons;
      const rmax = Math.max(1, ...reasons.map(r => r.live + r.fixtures));
      $('#pipeBody', el).innerHTML = `
        ${card('Where the data comes from', 'Everything here is real and free to use. No account or key is needed.', `<table class="t"><tr><th>What</th><th>Where it comes from</th><th>How often</th></tr>
          <tr><td>Ship positions</td><td>Ships broadcast their position by radio (called AIS). Receivers along the coast pass it to Digitraffic, the Finnish Transport Infrastructure Agency.</td><td>every minute</td></tr>
          <tr><td>Ship details (name, size, destination, arrival time)</td><td>The same Digitraffic service. Crews type the destination and arrival time into the ship\u2019s transmitter.</td><td>every 10 minutes</td></tr>
          <tr><td>Wind</td><td>Weather stations of the Finnish Meteorological Institute (FMI).</td><td>every 10 minutes</td></tr>
          <tr><td>Waves and sea state</td><td>FMI wave buoys and Digitraffic sea-state stations.</td><td>every 30 minutes</td></tr>
          <tr><td>Land map</td><td>Natural Earth, a free world map. Loaded once, used to cut land out of the possible areas.</td><td>once</td></tr>
          <tr><td>Ports</td><td>Eight ports entered by hand (name and centre point).</td><td>once</td></tr></table>`)}
        <div style="height:16px"></div>
        ${card('Data flow', 'The numbers are live counts. The waiting room is usually 0 because each record is checked the moment it arrives.', flow)}
        <div style="height:16px"></div>
        <div class="grid g3" style="grid-template-columns:repeat(5,minmax(0,1fr))">${feeds}</div>
        <div style="height:16px"></div>
        <div class="grid g2">
          ${card('Positions stored', 'How many ship positions were stored each minute.', chart)}
          ${card('Why records were refused', 'The reasons, with how many records each one caught.', `<div class="bars">${reasons.map(r => `<div class="bar-row"><span>${esc(words.reason(r.reason))}</span><div class="track"><div class="fill" style="width:${Math.max(r.live + r.fixtures ? 2 : 0, 100 * (r.live + r.fixtures) / rmax)}%;--c:${r.fixtures && !r.live ? '#8ea6b4' : '#7ba7b9'}"></div></div><span class="v">${r.live}${r.fixtures ? ` + ${r.fixtures}` : ''}</span></div>`).join('')}</div><div class="sub" style="margin-top:8px">Real records <b>+ demo records</b>. A number with a plus sign includes demo records.</div>`)}
        </div>
        <div style="height:16px"></div>
        <div class="grid g1">
          ${card('Latest refused records', 'Nothing is thrown away silently. Every refused record is kept with the reason.', `<div class="scroll"><table class="t"><tr><th>When</th><th>Ship ID</th><th>Why it was refused</th><th>Details</th></tr>${rej.slice(0, 40).map(r => `<tr><td>${fmt.ts(r.rejected_at)}</td><td>${r.mmsi ?? '–'}${r.source !== 'digitraffic' ? ' <span class="badge b-syn">DEMO</span>' : ''}</td><td><span class="badge b-still">${esc(words.reason(r.rejection_reason))}</span></td><td>${esc(r.detail || '')}</td></tr>`).join('') || '<tr><td colspan="4" class="empty">None yet.</td></tr>'}</table></div>`)}
        </div>`;
    },
  };

  /* ============================================================ TEMPORAL */
  ext.tabs.temporal = {
    async render(el) {
      el.innerHTML = head('Temporal', 'Ships change their destination and arrival time. Here you can see every version. Nothing is ever overwritten. Pick a ship below.') + `
        ${card('Pick a ship', 'Ships with the most changes are listed first.', `<div class="search" style="max-width:520px"><input id="tSearch" placeholder="Search by ship name or ID…" autocomplete="off"><div class="suggest" id="tSuggest" style="display:none"></div></div><div id="tPick" style="display:flex;gap:8px;flex-wrap:wrap"></div>`)}
        <div style="height:16px"></div><div id="tBody"></div>`;
      const inp = $('#tSearch', el); let timer;
      inp.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(async () => {
        const q = inp.value.trim(); const box = $('#tSuggest', el); if (q.length < 2) { box.style.display = 'none'; return; }
        const res = await api('/api/vessels/search?q=' + encodeURIComponent(q) + DSW.synQ('&'));
        box.innerHTML = res.map(r => `<div data-m="${r.mmsi}"><span>${esc(r.name || 'Unnamed ship')}${synBadge(r.is_synthetic)}</span><span style="color:var(--muted)">${r.versions} versions</span></div>`).join('');
        box.style.display = 'block'; $$('div[data-m]', box).forEach(d => d.addEventListener('click', () => { box.style.display = 'none'; this.show(el, +d.dataset.m); }));
      }, 220); });
      const top = await api('/api/vessels/search?' + DSW.synQ('').slice(1));
      $('#tPick', el).innerHTML = top.slice(0, 8).map(r => `<button class="btn small" data-m="${r.mmsi}">${esc(r.name || r.mmsi)} · ${r.versions}</button>`).join('');
      $$('#tPick [data-m]', el).forEach(b => b.addEventListener('click', () => this.show(el, +b.dataset.m)));
      const first = DSW.temporalFocus || (top.find(r => r.is_synthetic && r.versions > 1) || top[0] || {}).mmsi;
      DSW.temporalFocus = null;
      if (first) this.show(el, first);
    },
    refresh() { /* history changes rarely; no auto refresh so the chosen ship stays on screen */ },
    async show(el, mmsi) {
      const d = await api('/api/temporal/' + mmsi);
      this.current = mmsi;
      const hist = d.history, now = Date.now();
      if (!hist.length) { $('#tBody', el).innerHTML = card(esc(d.vessel.name || mmsi), '', '<div class="empty">This ship has not reported a destination yet.</div>'); return; }
      const t0 = Math.min(...hist.map(h => new Date(h.valid_from)));
      const t1 = Math.max(now, ...hist.map(h => new Date(h.recorded_at)));
      const W = 1060, L = 24, R = 24, span = (t1 - t0) * 1.04 || 1, x = t => L + ((new Date(t).getTime() - t0) / span) * (W - L - R);
      const rowH = 44, top = 48, H = top + hist.length * rowH + 90;
      const ticks = Array.from({ length: 7 }, (_, i) => t0 + (span * i) / 6);
      let svg = `<svg class="tl-svg" viewBox="0 0 ${W} ${H}">`;
      ticks.forEach(tk => { svg += `<line x1="${x(tk)}" x2="${x(tk)}" y1="${top - 8}" y2="${H - 60}" stroke="#1b3a55" stroke-dasharray="2 5"/><text x="${x(tk)}" y="${top - 12}" text-anchor="middle">${fmt.t(new Date(tk).toISOString())}</text>`; });
      svg += `<text x="${L}" y="12" class="lbl">When each destination was true (UTC)</text>`;
      hist.forEach((h, i) => {
        const y = top + i * rowH, xs = x(h.valid_from), xe = h.valid_to ? x(h.valid_to) : x(now);
        const bw = Math.max(6, xe - xs), label = `to ${esc(h.destination || '(none)')}${h.eta ? ', arriving ' + fmt.t(h.eta) : ''}`, inside = bw > 220;
        svg += `<rect x="${xs}" y="${y}" width="${bw}" height="26" rx="8" fill="${h.is_current ? '#7ba7b9' : '#2c5b7d'}" opacity="${h.is_current ? 0.95 : 0.85}"/>` +
          (inside ? `<text x="${xs + 10}" y="${y + 17}" class="lbl" style="fill:${h.is_current ? '#04202c' : '#e5f0f8'}">${label}</text>`
                  : xs > 300 ? `<text x="${xs - 10}" y="${y + 17}" class="lbl" text-anchor="end">${label}</text>`
                  : `<text x="${xs + bw + 10}" y="${y + 17}" class="lbl">${label}</text>`);
        if (h.is_current) svg += `<path d="M${xe} ${y + 4} l9 9 l-9 9" fill="none" stroke="#7ba7b9" stroke-width="2"/>`;
      });
      const yr = top + hist.length * rowH + 22;
      svg += `<text x="${L}" y="${yr - 8}" class="lbl">When our system learned about it</text><line x1="${L}" x2="${W - R}" y1="${yr + 14}" y2="${yr + 14}" stroke="#274a66"/>`;
      hist.forEach(h => { const xr = x(h.recorded_at); svg += `<path d="M${xr} ${yr + 6} l7 8 l-7 8 l-7 -8z" fill="#d5e2e8"><title>learned ${fmt.ts(h.recorded_at)}</title></path>`; if (h.superseded_at) svg += `<path d="M${x(h.superseded_at)} ${yr + 8} v12" stroke="#d5e2e8" stroke-width="2"/>`; });
      svg += `<text x="${L}" y="${yr + 46}">◆ the moment we learned it &nbsp;&nbsp; | the moment it was replaced (old versions are kept, never deleted)</text></svg>`;
      const mid = new Date((t0 + t1) / 2);
      $('#tBody', el).innerHTML = `
        ${card(`${esc(d.vessel.name || 'Ship ' + d.vessel.mmsi)}${synBadge(d.vessel.is_synthetic)} <span style="color:var(--muted)">${d.vessel.mmsi}</span>`, 'Every time the destination or arrival time changes, a new version is saved and the old one is kept.', svg)}
        <div style="height:16px"></div>
        <div class="grid g1">
          ${card('Look back in time', 'Pick a moment in the past to see what the destination was then.', `
            <label class="sub">Look at this moment (UTC)</label><input type="datetime-local" id="vAt" class="field" value="${mid.toISOString().slice(0, 16)}" style="width:100%;margin-bottom:8px">
            <label class="sub">As we knew it at (optional, UTC)</label><input type="datetime-local" id="kAt" class="field" style="width:100%;margin-bottom:10px">
            <button class="btn primary" id="asofGo">Show</button><div id="asofOut" style="margin-top:12px"></div>`)}
        </div>`;
      $('#asofGo', el).addEventListener('click', async () => {
        const v = $('#vAt', el).value, k = $('#kAt', el).value;
        const res = await api(`/api/asof?mmsi=${mmsi}&valid_at=${encodeURIComponent(v + ':00Z')}${k ? '&known_at=' + encodeURIComponent(k + ':00Z') : ''}`);
        $('#asofOut', el).innerHTML = res.length ? `<div class="formula">Destination: <b>${esc(res[0].destination || '–')}</b><br>Arrival time: ${fmt.ts(res[0].eta)}<br>True from ${fmt.ts(res[0].valid_from)} until ${res[0].valid_to ? fmt.ts(res[0].valid_to) : 'now'}<br>We learned it at ${fmt.ts(res[0].recorded_at)}</div>` : '<div class="empty">We had no record for that moment.</div>';
      });
    },
  };

  /* ============================================================ PORTS */
  ext.tabs.ports = {
    render(el) { el.innerHTML = head('Ports', 'Each card is a 3 km zone around a port. The list below shows ships arriving and leaving, found automatically from their positions.') + '<div id="portBody"><div class="empty">Loading…</div></div>'; this.refresh(el); },
    async refresh(el) {
      const [ports, ev] = await Promise.all([api('/api/ports' + DSW.synQ('?')), api('/api/port_events' + DSW.synQ('?'))]);
      $('#portBody', el).innerHTML = `
        <div class="grid g4">${ports.map(p => `<div class="card" style="padding:16px 20px"><div class="ft">⚓ ${esc(p.name)}</div><div class="fs">${esc(p.country)}</div>
          <div class="stat-row"><div><b>${p.ships_inside}</b><span>in port now</span></div><div><b>${p.entries_24h}</b><span>arrivals in 24 h</span></div></div></div>`).join('')}</div>
        <div style="height:16px"></div>
        ${card('Arrivals and departures', 'A ship counts as arrived when it comes within 3 km of the port, and as left when it moves clearly away.',
          `<div class="scroll" style="max-height:520px"><table class="t"><tr><th>Time (UTC)</th><th>Ship</th><th>Port</th><th>What happened</th><th class="num">Distance from port</th></tr>${ev.map(e => `<tr><td>${fmt.ts(e.event_time)}</td><td>${esc(e.name || 'Ship ' + e.mmsi)}${synBadge(e.is_synthetic)}</td><td>${esc(e.port)}</td><td><span class="badge ${e.event_type === 'ENTRY' ? 'b-ok' : 'b-still'}">${e.event_type === 'ENTRY' ? 'Arrived' : 'Left'}</span></td><td class="num">${fmt.n(e.distance_m)} m</td></tr>`).join('') || '<tr><td colspan="5" class="empty">No arrivals or departures seen yet. A ship has to be seen outside a port and then inside it. The demo scenario shows one.</td></tr>'}</table></div>`)}`;
    },
  };

  /* ============================================================ DBA CONSOLE */
  const lab = { before: {}, after: {} };
  const planHtml = lines => lines.map(l => {
    const e = esc(l);
    if (/Seq Scan/.test(l)) return `<span class="bad">${e}</span>`;
    if (/Index|Bitmap/.test(l)) return `<span class="hl">${e}</span>`;
    if (/Execution Time|Planning Time/.test(l)) return `<span class="hl">${e}</span>`;
    if (/Buffers/.test(l)) return `<span class="dim">${e}</span>`;
    return e;
  }).join('\n');
  const scanWords = types => (types || []).some(t => /Seq/.test(t)) ? 'reads every row' : 'jumps straight to the rows';
  const plainStatement = s => {
    const rules = [[/validate_and_migrate/, 'Run the record-checking step'], [/INSERT INTO staging_ais/, 'Add a new raw record'], [/UPDATE latest_position SET updated_at/, 'Update a ship’s newest position'],
      [/UPDATE latest_position/, 'Change a ship’s newest position'], [/FROM location_events/, 'Read the movement history'], [/DELETE FROM location_events/, 'Delete the movement history'],
      [/DELETE FROM rejected_events/, 'Delete the refused-records log'], [/DROP TABLE ports/, 'Delete the ports table'], [/vessel_status_history/, 'Read the destination history'],
      [/detect_going_dark/, 'Run the going-dark check'], [/v_mon_activity/, 'See who is connected'], [/DELETE FROM ais_gap_events/, 'Delete the going-dark records']];
    for (const [re, text] of rules) if (re.test(s)) return text;
    return 'Run a database command';
  };
  const doing = q => { q = (q || '').trim().toUpperCase(); return q.startsWith('SELECT') || q.startsWith('WITH') ? 'Reading data' : q.startsWith('INSERT') ? 'Adding data' : q.startsWith('UPDATE') ? 'Changing data' : q.startsWith('EXPLAIN') ? 'Timing a search' : q.startsWith('COMMIT') || q === '' ? 'Nothing right now' : 'Working'; };
  const ROLE_NAME = { ingest_role: 'Data collector', analyst_role: 'Viewer', dsw_ingest: 'Collector login', dsw_analyst: 'Viewer login' };
  const ROLE_NOTE = { ingest_role: 'May add data and run the checking step. Cannot read or delete anything else.', analyst_role: 'May only read. Cannot change anything.', dsw_ingest: 'Used by the collector program.', dsw_analyst: 'Used by this website to read data.' };

  function gauge(pct) {
    const r = 58, c = 2 * Math.PI * r, v = Math.max(0, Math.min(100, pct || 0)), col = v > 90 ? '#7ba7b9' : '#8ea6b4';
    return `<svg class="gauge" viewBox="0 0 150 150"><circle cx="75" cy="75" r="${r}" fill="none" stroke="#17324a" stroke-width="12"/><circle cx="75" cy="75" r="${r}" fill="none" stroke="${col}" stroke-width="12" stroke-linecap="round" stroke-dasharray="${(c * v) / 100} ${c}" transform="rotate(-90 75 75)"/><text x="75" y="80" text-anchor="middle" font-size="26" font-weight="600">${fmt.n(v, 1)}%</text><text x="75" y="100" text-anchor="middle" font-size="10" fill="#8aa9bf" style="fill:#8aa9bf">answered from memory</text></svg>`;
  }

  ext.tabs.dba = {
    render(el) {
      el.innerHTML = head('Database health and safety', 'The database also looks after itself. Use the buttons below to switch between health, permissions, two people at once, speed and backups.') + '<div id="dbaBody"><div class="empty">Loading…</div></div>';
      this.state = this.state || { rbac: null, conc: null, plan: null, backup: null };
      this.refresh(el);
    },
    async refresh(el) {
      const o = await api('/api/dba/overview');
      const dbHit = (o.cache.find(c => c.scope === 'database') || {}).hit_pct;
      const st = this.state, bench = o.bench;
      const SUBS = [['monitor', 'Health'], ['security', 'Permissions'], ['concurrency', 'Two at once'], ['indexing', 'Speed'], ['backup', 'Backup']];
      this.sub = this.sub || 'monitor';
      const hasIdx = bench.indexes.filter(i => i !== 'bench_location_events_pkey').length;
      const body = `<div class="seg" id="dbaSeg">${SUBS.map(([k, l]) => `<button type="button" data-sub="${k}" class="${this.sub === k ? 'on' : ''}">${l}</button>`).join('')}</div>
        <section class="dba-sec" data-sub="monitor" ${this.sub === 'monitor' ? '' : 'hidden'}><div class="grid g-1-2">
          ${card('Memory', 'Reading from memory is much faster than reading from disk. Higher is better.', `<div style="display:flex;align-items:center;gap:18px">${gauge(dbHit)}<div class="sub" style="margin:0">Most reads are answered from memory. The table that holds each ship’s newest position is set up so that frequent updates stay cheap.</div></div>`)}
          ${card('Who is connected', 'Every program connected to the database right now.', `<div class="scroll" style="max-height:230px"><table class="t"><tr><th>ID</th><th>User</th><th>Program</th><th>Status</th><th>Doing</th></tr>${o.activity.map(a => `<tr><td>${a.pid}</td><td>${esc(a.usename)}</td><td>${esc(a.application_name)}</td><td>${esc(a.state === 'idle' ? 'waiting' : a.state === 'active' ? 'working' : a.state)}</td><td>${esc(doing(a.query))}</td></tr>`).join('')}</table></div>${o.blocking.length ? `<div class="note" style="border-color:#e5807e">${o.blocking.length} connection(s) are waiting: ${o.blocking.map(b => `#${b.blocked_pid} waits for #${b.blocking_pid}`).join('; ')}</div>` : ''}`)}
        </div>
        </section>
        <section class="dba-sec" data-sub="security" ${this.sub === 'security' ? '' : 'hidden'}><div class="grid g1">
          ${card('Who is allowed to do what', 'The collector may only add data. This website may only read. Press the button to test it for real.',
            `<table class="t"><tr><th>User type</th><th>Can log in</th><th>What it may do</th></tr>${o.roles.map(r => `<tr><td>${esc(ROLE_NAME[r.rolname] || r.rolname)}</td><td>${r.can_login ? 'Yes' : 'No'}</td><td>${esc(ROLE_NOTE[r.rolname] || '')}</td></tr>`).join('')}</table>
             <div style="margin:12px 0"><button class="btn primary" id="rbacGo">Test the permissions</button></div><div id="rbacOut">${st.rbac || ''}</div>`)}
        </div></section>
        <section class="dba-sec" data-sub="concurrency" ${this.sub === 'concurrency' ? '' : 'hidden'}><div class="grid g1">
          ${card('Two people, one ship', 'What happens when several people change the same ship at the same moment. The database makes them take turns so nothing is lost.',
            `<button class="btn primary" id="concGo">Run the test</button><div id="concOut" style="margin-top:12px">${st.conc || ''}</div>`)}
        </div></section>
        <section class="dba-sec" data-sub="indexing" ${this.sub === 'indexing' ? '' : 'hidden'}>
        ${card('Speed test: with and without an index', 'An index works like the index of a book: the database jumps straight to the answer instead of reading everything. Do the steps in order and compare the times.',
          `<div class="stepper"><span class="step-chip ${bench.exists ? 'done' : ''}">${bench.exists ? 'Test table: ' + fmt.n(bench.rows) + ' rows' : 'Test table not created yet'}</span><span class="step-chip ${hasIdx ? 'done' : ''}">${hasIdx ? 'Indexes added' : 'No indexes yet'}</span></div>
           <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" id="bBuild">1 · Create a big test table (400,000 rows)</button><button class="btn primary" id="bRun" ${bench.exists ? '' : 'disabled'}>2 · Time two searches</button><button class="btn brass" id="bIdx" ${bench.exists ? '' : 'disabled'}>3 · Add indexes</button><button class="btn danger" id="bDrop" ${bench.exists ? '' : 'disabled'}>Remove indexes</button></div>
           <div class="sub" style="margin-top:8px">Do 1, then 2, then 3, then 2 again. Each run adds its result to the comparison.</div>
           <div id="bCompare"></div>
           <h4>Look inside a search</h4>
           <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><select id="exPreset" class="field" style="max-width:520px"></select><label style="display:flex;gap:6px;align-items:center;color:var(--muted)"><input type="checkbox" id="exSeq" style="accent-color:#9fbccb"> make it read every row (turn indexes off)</label><button class="btn" id="exGo">Show how it runs</button></div>
           <div class="term" id="exOut" style="margin-top:10px">${st.plan || '<span class="dim">Pick a search and press “Show how it runs”. In the result, “Seq Scan” means it read every row, “Index Scan” means it jumped straight to the data, and “Execution Time” at the bottom is how long it took.</span>'}</div>`)}
        </section>
        <section class="dba-sec" data-sub="backup" ${this.sub === 'backup' ? '' : 'hidden'}>
        ${card('Backup and restore', 'This saves a full copy of the database, then restores the copy into a separate test database and checks that every table has the same number of rows.',
          `<div class="grid g2"><div>${o.backups.length ? `<table class="t"><tr><th>Backup file</th><th class="num">Size (MB)</th><th>Saved at</th></tr>${o.backups.map(b => `<tr><td class="clip" style="max-width:240px">${esc(b.file)}</td><td class="num">${b.mb}</td><td>${esc(b.modified)}</td></tr>`).join('')}</table>` : '<div class="empty">No backups yet.</div>'}<div style="margin-top:12px"><button class="btn primary" id="bkGo">Back up and test the restore</button></div></div><div class="term" id="bkOut">${st.backup || '<span class="dim">The result will appear here.</span>'}</div></div>`)}</section>`;
      $('#dbaBody', el).innerHTML = body;
      this.wire(el);
      this.loadPresets(el);
    },
    wire(el) {
      const st = this.state;
      $$('#dbaSeg button', el).forEach(b => b.addEventListener('click', () => {
        this.sub = b.dataset.sub;
        $$('#dbaSeg button', el).forEach(x => x.classList.toggle('on', x === b));
        $$('.dba-sec', el).forEach(sec => { sec.hidden = sec.dataset.sub !== this.sub; });
      }));
      $('#rbacGo', el).addEventListener('click', async () => {
        busy(true, 'Testing what each user is allowed to do…');
        try {
          const r = await post('/api/actions/rbac');
          st.rbac = `<table class="t"><tr><th>User type</th><th>Tries to</th><th>Should be</th><th>Result</th></tr>${r.rows.map(x => `<tr><td>${esc(ROLE_NAME[x.role_name] || x.role_name)}</td><td>${esc(plainStatement(x.attempted_statement))}</td><td>${x.expected === 'allowed' ? 'Allowed' : 'Blocked'}</td><td><span class="badge ${x.verdict === 'PASS' ? 'b-ok' : 'b-bad'}">${x.outcome === 'allowed' ? 'Allowed' : 'Blocked'} · ${x.verdict === 'PASS' ? 'correct' : 'WRONG'}</span></td></tr>`).join('')}</table>`;
          $('#rbacOut', el).innerHTML = st.rbac;
        } catch (e) { toast(e.message, true); } finally { busy(false); }
      });
      $('#concGo', el).addEventListener('click', async () => {
        busy(true, 'Running the test with three connections…');
        try {
          const r = await post('/api/actions/concurrency');
          st.conc = r.events.map(ev => `<div class="log-line"><span class="ts">${ev.t.toFixed(2)}s</span><div>${esc(ev.message)}${ev.rows && ev.rows.length ? `<table class="t" style="margin-top:6px"><tr>${Object.keys(ev.rows[0]).map(k => `<th>${esc(k)}</th>`).join('')}</tr>${ev.rows.map(row => `<tr>${Object.values(row).map(v => `<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</table>` : ''}</div></div>`).join('');
          $('#concOut', el).innerHTML = st.conc;
        } catch (e) { toast(e.message, true); } finally { busy(false); }
      });
      const bench = async (step, text) => { busy(true, text); try { await post('/api/actions/bench', { step }); await this.refresh(el); } catch (e) { toast(e.message, true); } finally { busy(false); } };
      $('#bBuild', el).addEventListener('click', () => bench('build', 'Creating 400,000 test rows…'));
      $('#bIdx', el).addEventListener('click', () => bench('create_indexes', 'Adding indexes…'));
      $('#bDrop', el).addEventListener('click', () => bench('drop_indexes', 'Removing indexes…'));
      $('#bRun', el).addEventListener('click', async () => {
        busy(true, 'Timing two searches…');
        try {
          const state = (await api('/api/dba/overview')).bench, indexed = state.indexes.some(i => i !== 'bench_location_events_pkey');
          for (const key of ['history', 'spatial']) {
            const r = await api(`/api/dba/explain?preset=bench_${key}`);
            (indexed ? lab.after : lab.before)[key] = r;
            if (key === 'spatial') { st.plan = planHtml(r.plan); $('#exOut', el).innerHTML = st.plan; }
          }
          this.renderCompare(el);
        } catch (e) { toast(e.message, true); } finally { busy(false); }
      });
      $('#exGo', el).addEventListener('click', async () => {
        busy(true, 'Running the search…');
        try {
          const r = await api(`/api/dba/explain?preset=${$('#exPreset', el).value}&force_seq=${$('#exSeq', el).checked ? 1 : 0}`);
          st.plan = `<span class="dim">-- ${esc(r.label)}${r.force_seq ? '  (indexes turned off for this run)' : ''}\n${esc(r.sql)}</span>\n\n` + planHtml(r.plan);
          $('#exOut', el).innerHTML = st.plan;
        } catch (e) { toast(e.message, true); } finally { busy(false); }
      });
      $('#bkGo', el).addEventListener('click', async () => {
        busy(true, 'Saving a backup and testing the restore… (this can take a minute)');
        try { const r = await post('/api/actions/backup'); st.backup = esc(r.output); $('#bkOut', el).textContent = r.output; toast(r.ok ? 'Backup saved and the restore test passed' : 'The backup test reported a problem', !r.ok); await this.refresh(el); } catch (e) { toast(e.message, true); } finally { busy(false); }
      });
      this.renderCompare(el);
    },
    async loadPresets(el) {
      try {
        const r = await api('/api/dba/explain?preset=latest');
        const cur = $('#exPreset', el).value;
        $('#exPreset', el).innerHTML = Object.entries(r.presets).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join('');
        if (cur) $('#exPreset', el).value = cur;
      } catch (e) { /* tables may be empty */ }
    },
    renderCompare(el) {
      const names = { history: 'Find one ship’s movements', spatial: 'Find ships near Helsinki' };
      const cards = ['history', 'spatial'].filter(k => lab.before[k] || lab.after[k]).map(k => {
        const b = lab.before[k], a = lab.after[k], sp = b && a ? b.execution_ms / Math.max(0.001, a.execution_ms) : null;
        return `<div class="c"><div class="sub" style="margin:0 0 6px">${names[k]}</div>
          <div style="display:flex;gap:18px;align-items:flex-end;flex-wrap:wrap"><div><span class="sub">without index</span><br><b style="color:#8ea6b4">${b ? fmt.n(b.execution_ms, 2) + ' ms' : '–'}</b><br><span class="sub">${b ? scanWords(b.scan_types) : ''}</span></div>
          <div style="font-size:22px;color:var(--muted)">→</div>
          <div><span class="sub">with index</span><br><b style="color:#7ba7b9">${a ? fmt.n(a.execution_ms, 2) + ' ms' : '–'}</b><br><span class="sub">${a ? scanWords(a.scan_types) : ''}</span></div>
          ${sp ? `<div><span class="sub">faster by</span><br><b>${fmt.n(sp, 0)}×</b></div>` : ''}</div></div>`;
      }).join('');
      const slot = $('#bCompare', el); if (slot) slot.innerHTML = cards ? `<div class="compare">${cards}</div><div class="sub" style="margin-top:6px">1 ms is one thousandth of a second.</div>` : '';
    },
  };

  /* ============================================================ ARCHITECTURE */
  /* ER diagram in the usual notation: rectangle = entity (table), diamond = relationship, 1 / N / M = cardinality,
     double line = total participation, single line = partial, circle d = disjoint specialization. */
  const W = 220, LH = 16;
  const ENT = {
    rejected: { x: 20, y: 30, kind: 'dep', n: 'REJECTED_EVENT', t: 'rejected_events', a: ['rejected_id (PK)', 'mmsi', 'lat', 'lon', 'rejection_reason (FK)', 'detail', 'rejected_at'] },
    reason: { x: 390, y: 70, kind: 'ind', n: 'REJECTION_REASON', t: 'rejection_reasons', a: ['code (PK)', 'description'] },
    event: { x: 20, y: 270, kind: 'dep', n: 'LOCATION_EVENT', t: 'location_events', a: ['event_id (PK)', 'mmsi (FK)', 'geom', 'speed_knots', 'course', 'heading', 'nav_stat', 'event_time', 'ingested_at'] },
    latest: { x: 20, y: 540, kind: 'dep', n: 'LATEST_POSITION', t: 'latest_position', a: ['mmsi (PK, FK)', 'geom', 'speed_knots', 'course', 'last_seen'] },
    vessel: { x: 390, y: 290, kind: 'ind', n: 'VESSEL', t: 'vessels', a: ['mmsi (PK)', 'imo', 'call_sign', 'name', 'ship_type', 'length_m', 'width_m', 'draught_m', 'destination', 'eta', 'status_ts', 'is_synthetic'] },
    history: { x: 760, y: 230, kind: 'dep', n: 'VESSEL_STATUS_HISTORY', t: 'vessel_status_history', a: ['history_id (PK)', 'mmsi (FK)', 'destination', 'eta', 'valid_from', 'valid_to', 'recorded_at', 'superseded_at', 'is_current'] },
    port: { x: 760, y: 470, kind: 'ind', n: 'PORT', t: 'ports', a: ['port_id (PK)', 'locode', 'name', 'country', 'geom', 'buffer_radius_m'] },
    gap: { x: 390, y: 640, kind: 'sup', n: 'AIS_GAP_EVENT', t: 'ais_gap_events', a: ['gap_id (PK)', 'mmsi (FK)', 'start_port_id (FK)', 'status', 'classification', 'last_seen_time', 'reappear_time', 'start_geom', 'end_geom', 'gap_duration_minutes'] },
  };
  Object.values(ENT).forEach(e => { e.w = W; e.h = 34 + LH + e.a.length * LH + 10; });

  // a relationship: name, centre of the diamond, and the two entity ends [entity, cardinality, total?]
  const REL = [
    { n: 'EXPLAINED BY', c: [315, 115], a: ['rejected', 'N', true], b: ['reason', '1', false] },
    { n: 'RECORDS', c: [315, 340], a: ['vessel', '1', false], b: ['event', 'N', true] },
    { n: 'HAS LATEST', c: [315, 560], a: ['vessel', '1', false], b: ['latest', '1', true] },
    { n: 'HAS VERSIONS', c: [685, 285], a: ['vessel', '1', false], b: ['history', 'N', true] },
    { n: 'VISITS', sub: 'table: port_events', c: [685, 385], a: ['vessel', 'M', false], b: ['port', 'N', false] },
    { n: 'IS NOW IN', sub: 'table: vessel_port_state', c: [685, 480], a: ['vessel', 'N', false], b: ['port', '1', false] },
    { n: 'GOES SILENT', c: [500, 590], a: ['vessel', '1', false], b: ['gap', 'N', true] },
    { n: 'SILENT NEAR', c: [690, 705], a: ['gap', 'N', false], b: ['port', '1', false] },
  ];
  const SUB = [
    { n: 'IN_PORT', note: ['last fix was inside a port'], a: ['start_port_id (FK)'] },
    { n: 'STATIONARY', note: ['last speed was under', '0.5 knots'], a: [] },
    { n: 'LEFT_AREA', note: ['near the edge of the area,', 'or silent over 12 hours'], a: [] },
    { n: 'RECEIVER_OUTAGE', note: ['nearby ships went', 'silent too'], a: ['neighbours_before', 'neighbours_silent'] },
    { n: 'DARK_ACTIVITY', note: ['none of the other four'], a: ['feasibility_status', 'v_max_knots', 'speed_factor', 'reach_radius_km', 'feasible_geom', 'tight_geom'] },
  ];
  const STAND_ALONE = [
    ['staging_ais', 'new records waiting to be checked'], ['location_events_archive', 'old positions moved out'], ['weather_obs', 'wind readings'],
    ['sea_conditions', 'sea state and wave height'], ['land_polygons', 'coastline shapes'], ['land_tiles', 'land cut into small pieces'],
    ['speed_factor_lookup', 'weather slowdown (assumption)'], ['ship_class_speed', 'top speed by ship type (assumption)'],
    ['traffic_grid', 'water that ships have used'], ['pipeline_config', 'limits used by the checks'], ['collector_heartbeat', 'log of every collection run'],
  ];
  const COL = { ind: ['#0b2132', '#9fbccb'], dep: ['#0e2a40', '#7ba7b9'], sup: ['#12344d', '#d5e2e8'], sub: ['#0b2132', '#6f96ab'], rel: ['#16354f', '#9fbccb'] };
  const INK = '#d5e2e8', PAPER = '#08192a', LINE = '#9fbccb', MUTED = '#8ea6b4';

  function erSvg() {
    const tx = (x, y, s, extra = '') => `<text x="${x}" y="${y}" style="font-family:var(--sans);fill:${INK};font-size:${s}px;${extra}">`;
    const centre = b => [b.x + b.w / 2, b.y + b.h / 2];
    // where the line from the box centre towards (px, py) leaves the box
    const edge = (b, px, py) => {
      const [cx, cy] = centre(b), dx = px - cx, dy = py - cy;
      const t = Math.min(dx ? (b.w / 2) / Math.abs(dx) : 1e9, dy ? (b.h / 2) / Math.abs(dy) : 1e9);
      return [cx + dx * t, cy + dy * t];
    };
    const line = (p, q, total) => total
      ? `<path d="M${p[0]} ${p[1]} L${q[0]} ${q[1]}" stroke="${LINE}" stroke-width="5" fill="none"/><path d="M${p[0]} ${p[1]} L${q[0]} ${q[1]}" stroke="${PAPER}" stroke-width="2" fill="none"/>`
      : `<path d="M${p[0]} ${p[1]} L${q[0]} ${q[1]}" stroke="${LINE}" stroke-width="1.4" fill="none"/>`;
    const HW = 60, HH = 27;
    let svg = `<svg viewBox="0 0 1400 1180" class="er" role="img" aria-label="Entity relationship diagram"><rect x="0" y="0" width="1400" height="1180" rx="12" fill="${PAPER}"/>`;

    // relationship lines, cardinality labels and diamonds
    REL.forEach(r => {
      const [cx, cy] = r.c;
      [r.a, r.b].forEach(([key, card, total]) => {
        const b = ENT[key], p = edge(b, cx, cy), [bx, by] = centre(b), dx = bx - cx, dy = by - cy;
        const k = 1 / (Math.abs(dx) / HW + Math.abs(dy) / HH), q = [cx + dx * k, cy + dy * k];
        svg += line(p, q, total);
        const len = Math.hypot(q[0] - p[0], q[1] - p[1]) || 1, ux = (q[0] - p[0]) / len, uy = (q[1] - p[1]) / len;
        svg += tx(p[0] + ux * 16 - uy * 12 - 3, p[1] + uy * 16 + ux * 12 + 4, 13, 'font-weight:700') + card + '</text>';
      });
      svg += `<polygon points="${cx} ${cy - HH} ${cx + HW} ${cy} ${cx} ${cy + HH} ${cx - HW} ${cy}" fill="${COL.rel[0]}" stroke="${COL.rel[1]}" stroke-width="1.4"/>`;
      const words = r.n.split(' ');
      const rows = words.length > 1 && r.n.length > 9 ? [words.slice(0, Math.ceil(words.length / 2)).join(' '), words.slice(Math.ceil(words.length / 2)).join(' ')] : [r.n];
      rows.forEach((w, i) => { svg += tx(cx, cy + 4 + (i - (rows.length - 1) / 2) * 11, 9.5, 'font-weight:700;text-anchor:middle') + w + '</text>'; });
      if (r.sub) svg += tx(cx, cy + HH + 12, 9, 'text-anchor:middle;font-style:italic;fill:#8ea6b4') + r.sub + '</text>';
    });

    // entities
    const box = (e, kind, subtitle) => {
      const [f, s] = COL[kind];
      let g = `<rect x="${e.x}" y="${e.y}" width="${e.w}" height="${e.h}" rx="7" fill="${f}" stroke="${s}" stroke-width="1.4"/>`;
      g += tx(e.x + 10, e.y + 20, 12.5, 'font-weight:700') + e.n + '</text>';
      g += `<path d="M${e.x + 8} ${e.y + 27} H${e.x + e.w - 8}" stroke="${s}" stroke-width=".8" opacity=".6"/>`;
      let y = e.y + 42;
      (e.note || []).forEach(t => { g += tx(e.x + 10, y, 10, 'font-style:italic;fill:' + MUTED) + t + '</text>'; y += 14; });
      if (subtitle) { g += tx(e.x + 10, y, 9.5, 'fill:' + MUTED) + subtitle + '</text>'; y += LH - 1; }
      e.a.forEach(a => { g += tx(e.x + 10, y + LH - 4, 11, /\(PK/.test(a) ? 'font-weight:700' : '') + a + '</text>'; y += LH; });
      return g;
    };
    Object.values(ENT).forEach(e => { svg += box(e, e.kind, 'table: ' + e.t); });

    // specialization of AIS_GAP_EVENT: disjoint, total, decided by the classification column
    const g = ENT.gap, gx = g.x + g.w / 2, circY = g.y + g.h + 52, busY = circY + 46, subY = busY + 30, subH = 160, step = W + 30, x0 = 20;
    svg += line([gx, g.y + g.h], [gx, circY - 15], true);
    svg += tx(gx + 14, g.y + g.h + 20, 10, 'font-style:italic') + 'total: every gap has a class</text>';
    svg += `<circle cx="${gx}" cy="${circY}" r="15" fill="#16354f" stroke="${LINE}" stroke-width="1.4"/>` + tx(gx, circY + 5, 14, 'font-weight:700;text-anchor:middle') + 'd</text>';
    svg += tx(gx + 24, circY - 2, 10, 'font-style:italic') + 'disjoint: a gap has exactly one class,</text>' + tx(gx + 24, circY + 11, 10, 'font-style:italic') + 'decided by the column classification</text>';
    svg += `<path d="M${gx} ${circY + 15} V${busY}" stroke="${LINE}" stroke-width="1.4" fill="none"/><path d="M${x0 + W / 2} ${busY} H${x0 + 4 * step + W / 2}" stroke="${LINE}" stroke-width="1.4" fill="none"/>`;
    SUB.forEach((s, i) => {
      const x = x0 + i * step;
      svg += `<path d="M${x + W / 2} ${busY} V${subY}" stroke="${LINE}" stroke-width="1.4" fill="none"/>`;
      svg += box({ x, y: subY, w: W, h: subH, n: s.n, note: s.note, a: s.a }, 'sub');
    });
    svg += tx(20, subY + subH + 22, 10.5, 'fill:' + MUTED) + 'In the database all five classes share the one table ais_gap_events. Only the columns listed in a box are filled in for that class.</text>';

    // stand-alone tables
    const sx = 1130, sh = 44 + STAND_ALONE.length * 33;
    svg += `<rect x="${sx}" y="30" width="250" height="${sh}" rx="7" fill="#0a1d2d" stroke="#6f8797" stroke-width="1.2" stroke-dasharray="5 4"/>`;
    svg += tx(sx + 10, 50, 12, 'font-weight:700') + 'STAND-ALONE TABLES</text>' + tx(sx + 10, 64, 9.5, 'fill:' + MUTED) + 'no link to another table, joined by place and time</text>';
    STAND_ALONE.forEach(([t, d], i) => { svg += tx(sx + 10, 86 + i * 33, 11, 'font-weight:600') + t + '</text>' + tx(sx + 10, 99 + i * 33, 9.5, 'fill:' + MUTED) + d + '</text>'; });

    // legend
    const ly = 30 + sh + 24;
    svg += `<rect x="${sx}" y="${ly}" width="250" height="262" rx="7" fill="#0a1d2d" stroke="rgba(159,188,203,.25)" stroke-width="1"/>` + tx(sx + 10, ly + 20, 12, 'font-weight:700') + 'HOW TO READ IT</text>';
    const sw = (y, fill, stroke) => `<rect x="${sx + 10}" y="${y}" width="26" height="14" rx="3" fill="${fill}" stroke="${stroke}" stroke-width="1.2"/>`;
    const lt = (y, s) => tx(sx + 46, y + 11, 10.5) + s + '</text>';
    svg += sw(ly + 32, COL.ind[0], COL.ind[1]) + lt(ly + 32, 'table that stands on its own');
    svg += sw(ly + 54, COL.dep[0], COL.dep[1]) + lt(ly + 54, 'table that needs a parent row');
    svg += sw(ly + 76, COL.sup[0], COL.sup[1]) + lt(ly + 76, 'table split into sub-types');
    svg += sw(ly + 98, COL.sub[0], COL.sub[1]) + lt(ly + 98, 'sub-type');
    svg += `<polygon points="${sx + 23} ${ly + 120} ${sx + 36} ${ly + 128} ${sx + 23} ${ly + 136} ${sx + 10} ${ly + 128}" fill="${COL.rel[0]}" stroke="${COL.rel[1]}" stroke-width="1.2"/>` + lt(ly + 120, 'relationship between tables');
    svg += line([sx + 10, ly + 156], [sx + 36, ly + 156], true) + lt(ly + 150, 'double line: total, every row takes part');
    svg += line([sx + 10, ly + 176], [sx + 36, ly + 176], false) + lt(ly + 170, 'single line: partial, optional');
    svg += tx(sx + 10, ly + 205, 12, 'font-weight:700') + '1 N M</text>' + tx(sx + 64, ly + 205, 10.5) + 'how many rows can be linked</text>';
    svg += tx(sx + 10, ly + 222, 10.5) + 'PK primary key (unique row ID)</text>' + tx(sx + 10, ly + 238, 10.5) + 'FK foreign key (points to another table)</text>' + tx(sx + 10, ly + 254, 10.5) + 'd disjoint: only one sub-type per row</text>';
    return svg + '</svg>';
  }

  ext.tabs.arch = {
    render(el) {
      const layer = (x, y, w, h, title, lines) => `<rect class="box" x="${x}" y="${y}" width="${w}" height="${h}" style="stroke:#9fbccb"/><text class="lbl" x="${x + 14}" y="${y + 24}">${title}</text>${lines.map((l, i) => `<text class="dim" x="${x + 14}" y="${y + 44 + i * 16}">${l}</text>`).join('')}`;
      const arch = `<svg viewBox="0 0 1180 330"><defs><marker id="a2" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#9fbccb"/></marker></defs>
        ${layer(10, 20, 210, 130, '1 · Live data', ['Ship positions', 'Ship details', 'Sea state', 'Wind and waves', 'Land map (loaded once)'])}
        ${layer(260, 20, 210, 130, '2 · Collector', ['Asks the sources every minute', 'Tries again if one fails', 'Reconnects to the database', 'Never stores a record twice', 'Can only add data'])}
        ${layer(510, 20, 330, 290, '3 · Database', ['New records wait, then each one is checked', 'Positions: newest one + full history', 'Destination history (never overwritten)', 'Ports, arrivals, land, silences, areas', 'Automatic steps: port visits, silences, history', 'Two kinds of user: one adds data, one reads', 'Speed-ups, health checks and backups', 'Locks stop two people changing one ship at once'])}
        ${layer(880, 20, 290, 130, '4 · Area calculator', ['Usable speed = top speed × weather slowdown', 'Reach = usable speed × time silent', 'Overlap of the two reach circles', 'Land is cut out of the result'])}
        ${layer(880, 180, 290, 130, '5 · This website', ['Only reads data (safe)', 'Admin buttons use a separate login', 'Cannot run free-form commands', 'Map and charts'])}
        ${layer(10, 180, 460, 130, 'An example', ['A ship sends signals, goes quiet for 95 minutes while ships', 'nearby keep reporting, then reappears. The system shows', 'both positions and the sea area it could have crossed,', 'adjusted for wind and waves. It shows what was possible,', 'not what really happened.'])}
        <path d="M222 85 H258" stroke="#9fbccb" stroke-width="1.6" fill="none" marker-end="url(#a2)"/><path d="M472 85 H508" stroke="#9fbccb" stroke-width="1.6" fill="none" marker-end="url(#a2)"/>
        <path d="M842 85 H878" stroke="#9fbccb" stroke-width="1.6" fill="none" marker-end="url(#a2)"/><path d="M842 245 H878" stroke="#9fbccb" stroke-width="1.6" fill="none" marker-end="url(#a2)"/></svg>`;
      el.innerHTML = head('Architecture', 'The top picture shows how data moves through the system. The diagram underneath shows the database tables and how they link together.') + `
        ${card('How the system fits together', '', `<div class="arch">${arch}</div>`)}
        <div style="height:16px"></div>
        ${card('Database tables and links', 'Boxes are tables and diamonds are the links between them. 1, N and M show how many rows can be linked, and a double line means every row must take part.', `<div class="arch">${erSvg()}</div>`)}`;
    },
  };
})();

/* Dark Ship Watch - "How it works": a visual walk through one real record, the collection timetable,
   a live check against the official source, and a dictionary of every database table. Plain language only. */
(() => {
  'use strict';
  const { $, $$, esc, fmt, api, post, toast, busy, ext } = DSW;

  const card = (key, title, hint, body, open = true) =>
    `<details class="card fold" data-fold="${esc(key)}" ${open ? 'open' : ''}><summary><span class="ft">${title}</span>${hint ? `<span class="fs">${hint}</span>` : ''}</summary><div class="fold-body">${body}</div></details>`;
  const head = (t, tip) => `<div class="page-title"><h2>${t}</h2></div><div class="tip"><i>i</i><span>${tip}</span></div>`;
  const when = iso => iso ? fmt.ts(iso) + ' UTC' : '–';
  const dur = s => s == null ? '–' : s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;

  /* ============================================================ the visual walk-through */
  const NODES = ['Ship', 'Coast receivers', 'Digitraffic', 'Collector', 'Checks', 'Database', 'Watchers', 'Your screen'];
  const TITLES = [
    'A ship sends its position', 'Receivers on the coast hear it', 'Digitraffic publishes it', 'Our collector asks for it every minute',
    'The database checks it', 'It is stored', 'Automatic watchers look at it', 'You see it on the screen'];

  function diagram(active) {
    const W = 152;
    let svg = `<svg class="walk" viewBox="0 0 1230 112" role="img" aria-label="The eight steps">`;
    for (let i = 0; i < NODES.length - 1; i++) {
      svg += `<path d="M${20 + i * W + 124} 59 H${20 + (i + 1) * W - 4}" class="wline ${i < active ? 'lit' : ''}" marker-end="url(#wa)"/>`;
    }
    svg += `<defs><marker id="wa" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#8ea6b4"/></marker></defs>`;
    NODES.forEach((n, i) => {
      const cls = i === active ? 'now' : i < active ? 'done' : '';
      svg += `<g class="wnode ${cls}" data-i="${i}" transform="translate(${20 + i * W} 24)"><rect width="124" height="70" rx="14"/><circle cx="22" cy="22" r="11"/><text class="wn" x="22" y="26" text-anchor="middle">${i + 1}</text><text class="wl" x="62" y="54" text-anchor="middle">${n}</text></g>`;
    });
    return svg + '</svg>';
  }

  const kv = rows => `<table class="t kvt">${rows.map(([k, v, m]) => `<tr><td>${k}</td><td><b>${v}</b></td>${m !== undefined ? `<td class="mean">${m}</td>` : ''}</tr>`).join('')}</table>`;

  function stepContent(i, T, S) {
    const s = T.ship, src = T.source, raw = src.raw, pr = raw ? raw.properties : null;
    const who = `<b>${esc(s.name || 'Ship ' + s.mmsi)}</b> (ship ID ${s.mmsi})`;
    switch (i) {
      case 0: return {
        text: `<p>Every ship carries a radio transmitter called <b>AIS</b>. It broadcasts the ship’s ID number, its position from GPS, its speed and direction, and now and then its name, destination and arrival time.</p>
               <p>A moving ship sends a report every few seconds. A ship at anchor sends one every few minutes.</p>`,
        real: `<h5>Today’s example ship</h5>${kv([['Name', esc(s.name || 'unnamed')], ['Ship ID (MMSI)', s.mmsi], ['Type', esc(s.ship_class)], ['Size', s.length_m ? `${fmt.n(s.length_m, 0)} × ${fmt.n(s.width_m, 0)} m` : '–'],
          ['Speed now', `${fmt.n(s.speed_knots, 1)} knots`], ['Destination (typed by the crew)', esc(s.destination || '–')]])}` };
      case 1: return {
        text: `<p>Radio stations along the coasts of Finland and Estonia listen for these signals. Each ship can only be heard while it is within range, roughly 40 to 60 km.</p>
               <p>That is why a ship can look <b>silent</b> in our data even though its transmitter works: it may be out of range, our receiver may be down, or the crew may have switched it off. The Going Dark page tells these cases apart.</p>`,
        real: `<h5>Why a ship can go quiet</h5><ul class="plainlist"><li>It sailed out of radio range</li><li>A receiver on the coast was down</li><li>It is parked and its transmitter is off</li><li>The crew switched it off (the case we look for)</li></ul>` };
      case 2: return {
        text: `<p><b>Digitraffic</b>, run by the Finnish Transport Infrastructure Agency, gathers everything the receivers hear and publishes it free on the internet. This is the <b>official source</b>.</p>
               <p>The box on the right is the real answer it gave a moment ago for this ship.</p>`,
        real: raw ? `<h5>What the official source says right now</h5>${kv([
          ['Ship ID', raw.mmsi, 'which ship'], ['Position', `${raw.coordinates[1]}, ${raw.coordinates[0]}`, 'north, east (the source lists east first)'],
          ['Speed', `${pr.sog} knots`, '102.2 or more would mean unknown'], ['Direction of travel', `${pr.cog}°`, '0 is north, 360 means unknown'],
          ['Bow direction', `${pr.heading}°`, '511 means unknown'], ['Status code', pr.navStat, '0 under way, 1 anchored, 5 moored'],
          ['Time of report', when(new Date(pr.timestampExternal).toISOString()), 'the source sends it as a big number: ' + pr.timestampExternal]])}
          <div class="sub" style="margin-top:6px"><a href="${esc(src.url)}" target="_blank" rel="noopener">Open this record at the official source ↗</a></div>` : `<div class="empty">${esc(src.error || 'Demo ships have no official record.')}</div>` };
      case 3: {
        const f = (S.feeds || []).find(x => x.feed === 'positions') || {};
        return {
          text: `<p>A small program on this computer, the <b>collector</b>, asks Digitraffic for new positions <b>every minute</b>. If the source fails, it waits and tries again.</p>
                 <p>Before storing anything it tidies the data: it turns the time into a readable date, puts latitude before longitude, treats “102.2 knots” as “unknown”, ignores ships outside the Gulf of Finland, and skips positions it already has.</p>`,
          real: `<h5>The last collection run</h5>${kv([['Ran at', when(f.last_run)], ['Next run', `<span data-next="positions">in ${dur(f.next_in_s)}</span>`], ['Records received', fmt.n(f.received)], ['Records stored', fmt.n(f.stored)], ['Records refused', fmt.n(f.refused)]])}
                 <div class="sub" style="margin-top:6px">${esc(f.message || '')}</div>` };
      }
      case 4: return {
        text: `<p>New records go into a <b>waiting room</b> and the database tests each one. A record that fails any test is refused and kept with its reason. Nothing disappears silently.</p>
               <p>Your example record passed every test, which is why it was stored.</p>`,
        real: `<h5>The six tests (your ship passed all of them)</h5><ul class="checklist"><li>Ship ID is present and valid</li><li>Position is possible and inside the Gulf of Finland</li><li>Speed is below 60 knots</li><li>The time is not in the future</li><li>We have not stored this exact report before</li><li>The ship did not “jump” faster than 50 knots since its last position</li></ul>
               <div class="sub" style="margin-top:6px">Refused in the last 24 hours: <b>${fmt.n(T.counts.refused_24h)}</b> records (see the Pipeline page for the reasons).</div>` };
      case 5: return {
        text: `<p>A good record is written to <b>Movement history</b>, the full trail of the ship. The ship’s row in <b>Latest positions</b> is updated, but only if the new report is newer than the one already there.</p>
               <p>Here are the real rows in our database for this ship.</p>`,
        real: `<h5>Newest rows in Movement history</h5><table class="t"><tr><th>Report time (UTC)</th><th class="num">Speed</th><th>Position</th><th>Stored at</th></tr>${T.recent.map(r => `<tr><td>${fmt.ts(r.event_time)}</td><td class="num">${fmt.n(r.speed_knots, 1)}</td><td>${fmt.n(r.lat, 4)}, ${fmt.n(r.lon, 4)}</td><td>${fmt.ts(r.ingested_at)}</td></tr>`).join('')}</table>
               <h5>Latest positions row</h5>${kv([['Last seen', when(s.last_seen)], ['Position', `${fmt.n(s.lat, 5)}, ${fmt.n(s.lon, 5)}`], ['Positions stored for this ship', fmt.n(T.counts.stored_positions)]])}` };
      case 6: return {
        text: `<p>Three watchers work automatically after every stored position:</p>
               <p><b>Port watcher:</b> a ship that comes within 3 km of a port is logged as arrived, and as left when it moves clearly away.</p>
               <p><b>Silence watcher:</b> a ship that stays quiet for 20 minutes is checked. Is it in port, parked, leaving our area, or did the ships around it go quiet too?</p>
               <p><b>History keeper:</b> when a ship changes its destination or arrival time, a new version is saved and the old one is kept.</p>`,
        real: `<h5>For this ship</h5>${kv([['Port visits logged', T.counts.port_visits], ['Times it went silent', T.counts.silences], ['Destination versions saved', T.counts.destination_versions],
          ['Quiet for', `${fmt.n(s.silent_min, 1)} min`, s.silent_min > 20 ? 'longer than 20 minutes: being checked' : 'normal']])}` };
      default: return {
        text: `<p>The website reads the database and draws it: ships on the map, trails, port rings, the silence checks and the possible areas.</p>
               <p>It only reads. It cannot change data, so what you see is exactly what the database holds.</p>`,
        real: `<h5>See it yourself</h5><p class="sub">Open this ship on the map and compare its trail with what you just saw.</p><button class="btn primary" id="gShowShip">Show this ship on the map</button>` };
    }
  }

  /* ============================================================ the page */
  ext.tabs.guide = {
    async render(el) {
      clearTimeout(this.wait); this.playing = false; this.step = 0;
      el.innerHTML = head('How it works', 'Press Next to follow one real ship report from its radio transmitter to your screen. Below you can see when data is collected, check it against the official source, and look inside every database table.') + `
        ${card('walk', 'Follow one real record', 'Eight steps, using a real ship from the live data.', `
          <div id="gDiagram"></div>
          <div class="walk-ctl"><button class="btn small" id="gPrev">← Back</button><button class="btn primary small" id="gNext">Next →</button><button class="btn small" id="gPlay">▶ Play automatically</button><span style="flex:1"></span><button class="btn small" id="gOther">Use another ship</button></div>
          <div class="walk-body"><div><h3 id="gTitle"></h3><div id="gText"></div></div><div class="walk-real" id="gReal"><div class="empty">Loading a real ship…</div></div></div>`)}
        <div style="height:16px"></div>
        ${card('schedule', 'When data is collected', 'Where each kind of data comes from and how often we ask for it.', '<div id="gSchedule"><div class="empty">Loading…</div></div>')}
        <div style="height:16px"></div>
        ${card('verify', 'Check that the data is real', 'Pick a random ship and compare our database with the official source, live.', `
          <button class="btn primary" id="gVerify">Check a random ship now</button>
          <div id="gVerifyOut" style="margin-top:14px"></div>
          <h4>Latest wind readings we stored</h4><div id="gWind" class="scroll" style="max-height:240px"></div>`)}
        <div style="height:16px"></div>
        ${card('tables', 'Every database table', 'What each table holds, what its columns mean, and where it is used. Click a table.', '<div id="gTables"><div class="empty">Loading…</div></div>')}`;
      $('#gPrev', el).addEventListener('click', () => this.userGo(el, this.step - 1));
      $('#gNext', el).addEventListener('click', () => this.userGo(el, this.step + 1));
      $('#gPlay', el).addEventListener('click', () => this.play(el));
      $('#gOther', el).addEventListener('click', () => this.loadTrace(el));
      $('#gVerify', el).addEventListener('click', () => this.verify(el));
      this.el = el;
      await Promise.all([this.loadTrace(el), this.loadSchedule(el), this.loadWind(el), this.loadTables(el)]);
      this.go(el, this.step);
      clearInterval(this.tick);
      this.tick = setInterval(() => this.countdown(), 1000);
    },
    refresh() { /* nothing: the walk-through keeps its place; the timetable refreshes itself */ },

    async loadTrace(el, mmsi) {
      try {
        this.trace = await api('/api/guide/trace' + (mmsi ? '?mmsi=' + mmsi : ''));
        this.go(el, this.step || 0);
      } catch (e) { $('#gReal', el).innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
    },
    go(el, i) {
      this.step = Math.max(0, Math.min(NODES.length - 1, i));
      if (!this.trace) return;
      $('#gDiagram', el).innerHTML = diagram(this.step);
      $$('#gDiagram .wnode', el).forEach(g => g.addEventListener('click', () => this.userGo(el, +g.dataset.i)));
      const c = stepContent(this.step, this.trace, this.sched || {});
      $('#gTitle', el).textContent = `${this.step + 1}. ${TITLES[this.step]}`;
      $('#gText', el).innerHTML = c.text;
      $('#gReal', el).innerHTML = c.real;
      $('#gPrev', el).disabled = this.step === 0; $('#gNext', el).disabled = this.step === NODES.length - 1;
      const btn = $('#gShowShip', el); if (btn) btn.addEventListener('click', () => DSW.openShip(this.trace.ship.mmsi));
    },
    /* automatic play (no sound) */
    runStep(el) {
      if (!this.playing) return;
      this.go(el, this.step);
      this.wait = setTimeout(() => {
        if (!this.playing) return;
        if (this.step >= NODES.length - 1) { this.stopPlay(el); return; }
        this.step += 1; this.runStep(el);
      }, 7000);
    },
    stopPlay(el) {
      this.playing = false; clearTimeout(this.wait);
      const b = $('#gPlay', el); if (b) b.textContent = '▶ Play automatically';
    },
    userGo(el, i) {                                   // Back / Next / clicking a box: restart the timer if playing
      clearTimeout(this.wait); this.go(el, i);
      if (this.playing) this.runStep(el);
    },
    play(el) {
      if (this.playing) { this.stopPlay(el); return; }
      this.playing = true; $('#gPlay', el).textContent = '❚❚ Pause';
      if (this.step >= NODES.length - 1) this.step = 0;
      this.runStep(el);
    },

    async loadSchedule(el) {
      const d = await api('/api/guide/schedule');
      this.sched = { feeds: d.feeds }; this.schedAt = Date.now();
      $('#gSchedule', el).innerHTML = `<table class="t"><tr><th>Data</th><th>Where it comes from</th><th>Asked every</th><th>Last asked</th><th>Next</th><th class="num">Received</th><th class="num">Stored</th><th class="num">Refused</th></tr>
        ${d.feeds.map(f => `<tr><td><b>${esc(f.title)}</b></td><td>${esc(f.source)}${f.url ? `<br><a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.url.replace('https://', ''))} ↗</a>` : ''}</td>
          <td>${dur(f.interval_s)}</td><td>${f.last_run ? dur(f.seconds_since) + ' ago' : 'not yet'}</td><td data-next="${f.feed}" data-s="${f.next_in_s ?? ''}">${f.next_in_s == null ? '–' : 'in ' + dur(f.next_in_s)}</td>
          <td class="num">${fmt.n(f.received)}</td><td class="num">${fmt.n(f.stored)}</td><td class="num">${fmt.n(f.refused)}</td></tr>`).join('')}</table>
        <div style="margin-top:12px"><button class="btn small" id="gFetch">Ask for new data now</button> <span class="sub">Runs the ship-position and wind collection immediately.</span></div>
        <div class="sub" style="margin-top:8px">The land map comes from Natural Earth and the eight ports were typed in by hand. Both were loaded once.</div>`;
      $('#gFetch', el).addEventListener('click', async () => {
        busy(true, 'Fetching the latest live data…');
        try { await post('/api/actions/collect', { feeds: ['positions', 'weather'] }); toast('Live data updated.'); await this.loadSchedule(el); await this.loadWind(el); }
        catch (e) { toast(e.message, true); } finally { busy(false); }
      });
    },
    countdown() {
      if (!this.el || !document.body.contains(this.el) || !this.schedAt) return;
      const elapsed = (Date.now() - this.schedAt) / 1000;
      $$('[data-next]', this.el).forEach(td => {
        const f = (this.sched.feeds || []).find(x => x.feed === td.dataset.next); if (!f || f.next_in_s == null) return;
        const left = f.next_in_s - elapsed;
        td.textContent = left > 1 ? 'in ' + dur(left) : 'due now';
      });
      if (elapsed > 30) this.loadSchedule(this.el).catch(() => {});
    },

    async verify(el) {
      const out = $('#gVerifyOut', el); out.innerHTML = '<div class="empty">Asking the official source…</div>';
      try {
        const t = await api('/api/guide/trace'); const c = t.compare;
        if (!c) { out.innerHTML = `<div class="empty">${esc(t.source.error || 'No comparison possible.')}</div>`; return; }
        out.innerHTML = `<div class="verdict ${c.ok ? 'ok' : 'bad'}">${esc(c.verdict)}</div>
          <div class="sub" style="margin:8px 0">Ship: <b>${esc(t.ship.name || t.ship.mmsi)}</b> (ID ${t.ship.mmsi}). Checked at ${when(t.fetched_at)}.</div>
          <table class="t"><tr><th>What</th><th>Official source (live)</th><th>Our database</th><th>Same?</th></tr>${c.rows.map(r => `<tr><td>${esc(r.field)}</td><td>${esc(r.source ?? '–')}</td><td>${esc(r.ours ?? '–')}</td><td>${r.match === true ? '<span class="badge b-ok">same</span>' : r.match === null ? '<span class="badge b-still">source is newer</span>' : '<span class="badge b-bad">different</span>'}</td></tr>`).join('')}</table>
          <div class="sub" style="margin-top:8px"><a href="${esc(t.source.url)}" target="_blank" rel="noopener">Open this ship at the official source ↗</a>. Ships report every few seconds and we collect once a minute, so a small difference in time is normal.</div>`;
      } catch (e) { out.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
    },
    async loadWind(el) {
      try {
        const env = await api('/api/environment');
        const rows = env.wind.sort((a, b) => new Date(b.observed_at) - new Date(a.observed_at)).slice(0, 8);
        $('#gWind', el).innerHTML = rows.length ? `<table class="t"><tr><th>Measured at (UTC)</th><th>Station position</th><th class="num">Wind (m/s)</th><th class="num">Gust (m/s)</th><th class="num">From (degrees)</th></tr>${rows.map(w => `<tr><td>${fmt.ts(w.observed_at)}</td><td>${fmt.n(w.lat, 3)}, ${fmt.n(w.lon, 3)}</td><td class="num">${fmt.n(w.wind_speed_ms, 1)}</td><td class="num">${fmt.n(w.wind_gust_ms, 1)}</td><td class="num">${w.wind_dir_deg ?? '–'}</td></tr>`).join('')}</table>
          <div class="sub" style="margin-top:6px">Source: Finnish Meteorological Institute open data. Compare with <a href="https://en.ilmatieteenlaitos.fi/observations" target="_blank" rel="noopener">the institute’s own observations page ↗</a>.</div>` : '<div class="empty">No wind readings in the last 3 hours.</div>';
      } catch (e) { /* ignore */ }
    },

    async loadTables(el) {
      const list = await api('/api/dictionary'); this.dict = Object.fromEntries(list.map(t => [t.table, t]));
      const GROUPS = [['Ships and their movements', ['vessels', 'latest_position', 'location_events', 'location_events_archive']], ['Record checks', ['staging_ais', 'rejected_events', 'rejection_reasons']],
        ['History and ports', ['vessel_status_history', 'ports', 'port_events', 'vessel_port_state']], ['Silences', ['ais_gap_events']],
        ['Weather and land', ['weather_obs', 'sea_conditions', 'land_polygons', 'land_tiles']], ['Settings and logs', ['collector_heartbeat', 'pipeline_config', 'speed_factor_lookup', 'ship_class_speed', 'traffic_grid']]];
      $('#gTables', el).innerHTML = `<div class="sub" style="margin-bottom:10px">${list.length} tables. The number is how many rows each one holds right now.</div>` +
        GROUPS.map(([g, names]) => `<div class="tgroup"><h4>${g}</h4><div class="tchips">${names.filter(n => this.dict[n]).map(n => `<button class="tchip" data-t="${n}"><b>${esc(this.dict[n].title)}</b><span>${fmt.n(this.dict[n].rows)} rows</span></button>`).join('')}</div></div>`).join('') +
        '<div id="gTableDetail"></div>';
      $$('#gTables .tchip', el).forEach(b => b.addEventListener('click', () => this.showTable(el, b.dataset.t)));
      this.showTable(el, 'location_events');
    },
    async showTable(el, name) {
      const t = this.dict[name]; if (!t) return;
      $$('#gTables .tchip', el).forEach(b => b.classList.toggle('on', b.dataset.t === name));
      $('#gTableDetail', el).innerHTML = `<div class="tdetail"><h3>${esc(t.title)} <span class="tname">${esc(t.table)}</span></h3><p>${esc(t.what)}</p>
        <div class="usedby"><b>Used by:</b> ${t.used_by.map(u => `<span class="badge b-still">${esc(u)}</span>`).join(' ')}</div>
        <h4>Columns (the facts stored in each row)</h4>
        <div class="scroll" style="max-height:420px"><table class="t"><tr><th>Column</th><th>Kind of value</th><th>Key</th><th>Meaning</th></tr>${t.columns.map(c => `<tr><td><b>${esc(c.name)}</b></td><td>${esc(c.type)}</td><td>${c.key ? `<span class="badge b-ok">${c.key === 'PK' ? 'main key' : 'links to ' + esc(c.links_to)}</span>` : ''}</td><td>${esc(c.meaning)}</td></tr>`).join('')}</table></div>
        <div class="sub" style="margin-top:8px">A <b>main key</b> uniquely identifies a row. A <b>link</b> points at a row in another table, which is how tables are joined together.</div>
        <h4>Newest 3 rows right now (map shapes are shown as text)</h4><div id="gSample" class="scroll"><div class="empty">Loading…</div></div></div>`;
      try {
        const s = await api('/api/dictionary/sample?table=' + name);
        const cols = t.columns.map(c => c.name);
        $('#gSample', el).innerHTML = s.rows.length ? `<table class="t"><tr>${cols.map(c => `<th>${esc(c)}</th>`).join('')}</tr>${s.rows.map(r => `<tr>${cols.map(c => `<td class="clip">${esc(r[c] ?? '–')}</td>`).join('')}</tr>`).join('')}</table>` : '<div class="empty">This table is empty right now.</div>';
      } catch (e) { $('#gSample', el).innerHTML = '<div class="empty">Could not load rows.</div>'; }
    },
  };
})();
