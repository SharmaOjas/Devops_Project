/* Dark Ship Watch - opening page: animated top-down ocean + ship, editorial sections fed by live data */
const INTRO = (() => {
  'use strict';
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const intro = () => $('#intro');
  const S = { running: true, mx: 0, my: 0, tx: 0, ty: 0, inView: { hero: true, device: false }, data: {}, scene: null, entered: false };
  const rand = (a, b) => a + Math.random() * (b - a);
  const ease = t => 1 - Math.pow(1 - t, 3);

  /* ================================================================ hero scene */
  function startOcean(canvas) {
    const ctx = canvas.getContext('2d');
    const low = document.createElement('canvas'), lctx = low.getContext('2d');
    const shipCv = document.createElement('canvas'), shadowCv = document.createElement('canvas');
    let W = 0, H = 0, dpr = 1, lw = 0, lh = 0, img = null, STEP = 7, sc = 1;
    const foam = [], ripples = [], plankton = [];
    const t0 = performance.now();
    let last = t0, lastRipple = 0, acc = 0, rafId = 0;

    /* ---- ship art (rendered once per resize) ---- */
    function hullPath(c, s = 1, ys = 1) {
      c.beginPath(); c.moveTo(-500 * s, 0);
      c.bezierCurveTo(-430 * s, -22 * ys, -330 * s, -62 * ys, -230 * s, -78 * ys); c.lineTo(430 * s, -78 * ys);
      c.quadraticCurveTo(500 * s, -78 * ys, 500 * s, -50 * ys); c.lineTo(500 * s, 50 * ys); c.quadraticCurveTo(500 * s, 78 * ys, 430 * s, 78 * ys);
      c.lineTo(-230 * s, 78 * ys); c.bezierCurveTo(-330 * s, 62 * ys, -430 * s, 22 * ys, -500 * s, 0); c.closePath();
    }
    function renderShip(k) {
      const w = Math.ceil(1100 * k), h = Math.ceil(280 * k);
      shipCv.width = shadowCv.width = w; shipCv.height = shadowCv.height = h;
      const c = shipCv.getContext('2d'); c.setTransform(k, 0, 0, k, w / 2, h / 2);
      // hull (red-brown boot-topping visible as the outer rim)
      let g = c.createLinearGradient(0, -80, 0, 80); g.addColorStop(0, '#8b3a31'); g.addColorStop(.5, '#6c2923'); g.addColorStop(1, '#8b3a31');
      hullPath(c); c.fillStyle = g; c.fill(); c.lineWidth = 3; c.strokeStyle = '#1d0b0a'; c.stroke();
      // deck
      g = c.createLinearGradient(0, -70, 0, 70); g.addColorStop(0, '#8aa1ab'); g.addColorStop(.5, '#cbd8dc'); g.addColorStop(1, '#8aa1ab');
      hullPath(c, .975, .86); c.fillStyle = g; c.fill(); c.lineWidth = 1.6; c.strokeStyle = 'rgba(255,255,255,.55)'; c.stroke();
      c.save(); hullPath(c, .975, .86); c.clip();
      // containers
      const pal = ['#a64b3c', '#2a7f8e', '#c9a24b', '#24455f', '#7b8d96', '#b8553f', '#3b8f7d', '#d6d0c2', '#5a6d8c'];
      let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
      for (let bay = 0; bay < 14; bay++) {
        const x0 = -318 + bay * 42;
        for (let col = 0; col < 5; col++) {
          if (rnd() < 0.07) continue;
          const y0 = -62 + col * 25, color = pal[Math.floor(rnd() * pal.length)];
          const cg = c.createLinearGradient(x0, 0, x0 + 38, 0); cg.addColorStop(0, color); cg.addColorStop(1, shade(color, -22));
          c.fillStyle = cg; c.fillRect(x0, y0, 38, 24);
          c.fillStyle = 'rgba(255,255,255,.2)'; c.fillRect(x0, y0, 38, 3);
          c.fillStyle = 'rgba(0,0,0,.18)'; for (let r = 1; r < 5; r++) c.fillRect(x0 + r * 7.5, y0 + 3, 1, 21);
          c.strokeStyle = 'rgba(0,0,0,.45)'; c.lineWidth = .8; c.strokeRect(x0, y0, 38, 24);
        }
      }
      // forecastle: winches + deck lines
      c.fillStyle = '#3a4a52'; [[-408, -16], [-408, 16], [-380, 0]].forEach(([x, y]) => { c.beginPath(); c.arc(x, y, 7, 0, 7); c.fill(); });
      c.strokeStyle = 'rgba(0,0,0,.25)'; c.lineWidth = 1; c.beginPath(); c.moveTo(-470, 0); c.lineTo(-330, 0); c.stroke();
      c.restore();
      // accommodation block + funnel + mast
      c.fillStyle = '#dfe7ea'; c.strokeStyle = 'rgba(0,0,0,.5)'; c.lineWidth = 1.2; c.fillRect(326, -58, 96, 116); c.strokeRect(326, -58, 96, 116);
      c.fillStyle = '#f4f8f9'; c.fillRect(342, -44, 62, 88); c.strokeRect(342, -44, 62, 88);
      c.fillStyle = '#23404e'; c.fillRect(330, -52, 90, 7); c.fillRect(330, 45, 90, 7);
      c.fillStyle = '#b23a34'; c.fillRect(384, -20, 28, 40); c.fillStyle = '#f4f8f9'; c.fillRect(384, -5, 28, 10);
      c.fillStyle = '#1b2a33'; c.beginPath(); c.arc(356, 0, 6, 0, 7); c.fill(); c.strokeStyle = '#1b2a33'; c.beginPath(); c.moveTo(350, 0); c.lineTo(362, 0); c.stroke();
      // soft shadow silhouette
      const s = shadowCv.getContext('2d'); s.setTransform(k, 0, 0, k, w / 2, h / 2); hullPath(s); s.fillStyle = 'rgba(0,10,20,.85)'; s.fill();
    }
    function shade(hex, amt) {
      const n = parseInt(hex.slice(1), 16); const f = v => Math.max(0, Math.min(255, v + amt));
      return `rgb(${f(n >> 16)},${f((n >> 8) & 255)},${f(n & 255)})`;
    }

    function resize() {
      const r = canvas.getBoundingClientRect(); if (!r.width) return;
      dpr = Math.min(window.devicePixelRatio || 1, 1.5); W = r.width; H = r.height;
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
      STEP = W < 700 ? 10 : 7; lw = Math.ceil(W / STEP); lh = Math.ceil(H / STEP); low.width = lw; low.height = lh; img = lctx.createImageData(lw, lh);
      sc = Math.max(.5, Math.min(W / 1250, H / 720));
      renderShip(sc * dpr);
      plankton.length = 0;
      for (let i = 0; i < (W < 700 ? 40 : 95); i++) plankton.push({ x: rand(0, W), y: rand(0, H), r: rand(.6, 2.6), vy: rand(-12, -3), vx: rand(2, 9), tw: rand(0, 6), z: rand(.2, 1) });
    }

    function water(t) {
      const d = img.data, flow = t * 5.5; let p = 0;
      for (let j = 0; j < lh; j++) {
        const v = j * 0.078;
        for (let i = 0; i < lw; i++) {
          const u = (i - flow) * 0.078;
          const a = Math.sin(u * 1.35 + Math.sin(v * 0.8 + t * 0.5) * 1.7 + t * 0.3);
          const b = Math.sin(v * 1.9 - u * 0.5 + Math.sin(u * 0.6 - t * 0.4) * 1.5 - t * 0.5);
          const c = Math.sin(u * 0.7 + v * 1.1 + t * 0.7);
          const base = 0.5 + 0.5 * (a + b + c) / 3;
          const ridge = Math.pow(1 - Math.abs(a * 0.5 + b * 0.5), 9) * 0.32;
          d[p++] = 3 + 14 * base + 110 * ridge; d[p++] = 30 + 66 * base + 160 * ridge; d[p++] = 52 + 74 * base + 170 * ridge; d[p++] = 255;
        }
      }
      lctx.putImageData(img, 0, 0);
      ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(low, 0, 0, W, H);
    }

    function hullHalfWidth(x) {                      // local x in ship units -> half beam (approx.)
      if (x < -230) return 78 * Math.pow(Math.max(0, (x + 500) / 270), 0.62);
      return x > 430 ? 70 : 78;
    }

    function frame(now) {
      rafId = requestAnimationFrame(frame);
      if (!S.running || !S.inView.hero || document.hidden || !W) { last = now; return; }
      const dt = Math.min(0.05, (now - last) / 1000); last = now; const t = (now - t0) / 1000;
      S.mx += (S.tx - S.mx) * 0.05; S.my += (S.ty - S.my) * 0.05;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      water(t);

      // ship placement
      const enter = ease(Math.min(1, t / 5.5));
      const cx = W * 0.865 + (1 - enter) * W * 0.5 - S.mx * 20, cy = H * 0.56 + Math.sin(t * 0.8) * 3.2 - S.my * 11;
      const rot = 0.06 + Math.sin(t * 0.45) * 0.006, cr = Math.cos(rot), sr = Math.sin(rot), k = sc;
      const toScreen = (lx, ly) => [cx + (lx * cr - ly * sr) * k, cy + (lx * sr + ly * cr) * k];

      // bow-wave wedge (water pushed aside)
      ctx.save(); ctx.translate(cx, cy); ctx.rotate(rot); ctx.scale(k, k);
      [-1, 1].forEach(sd => {
        const g = ctx.createLinearGradient(-520, 0, 80, sd * 190); g.addColorStop(0, 'rgba(235,250,255,.34)'); g.addColorStop(1, 'rgba(235,250,255,0)');
        ctx.beginPath(); ctx.moveTo(-505, 0); ctx.quadraticCurveTo(-330, sd * 80, -40, sd * 215); ctx.lineTo(60, sd * 150); ctx.quadraticCurveTo(-250, sd * 66, -505, 0); ctx.fillStyle = g; ctx.fill();
      });
      ctx.restore();

      // foam spawn (screen space) - flows right, away from the bow
      acc += dt * 190;
      while (acc > 1) {
        acc -= 1;
        const sd = Math.random() < .5 ? -1 : 1, lx = rand(-492, 340), hw = hullHalfWidth(lx) + 3;
        const [x, y] = toScreen(lx, sd * hw);
        foam.push({ x, y, vx: rand(120, 190) * k, vy: sd * rand(2, 26) * k + Math.sin(rot) * 40, r: rand(3, 8) * k, life: 0, max: rand(2.6, 4.6) });
        if (Math.random() < .28) { const [bx, by] = toScreen(-496, rand(-8, 8)); foam.push({ x: bx, y: by, vx: rand(90, 170) * k, vy: rand(-60, 60) * k, r: rand(4, 11) * k, life: 0, max: rand(1.4, 2.8) }); }
      }
      for (let i = foam.length - 1; i >= 0; i--) { const f = foam[i]; f.life += dt; if (f.life > f.max) { foam.splice(i, 1); continue; } f.x += f.vx * dt; f.y += f.vy * dt; f.vy *= 0.992; }
      ctx.save(); ctx.globalCompositeOperation = 'screen';
      for (const f of foam) { const u = f.life / f.max, a = (1 - u) * (u < .08 ? u / .08 : 1) * 0.72; ctx.fillStyle = `rgba(226,246,252,${a.toFixed(3)})`; ctx.beginPath(); ctx.arc(f.x, f.y, f.r * (1 + u * 3.2), 0, 7); ctx.fill(); }
      ctx.restore();

      // shadow + ship
      ctx.save(); ctx.translate(cx, cy); ctx.rotate(rot);
      ctx.filter = 'blur(16px)'; ctx.globalAlpha = .55; ctx.drawImage(shadowCv, -shadowCv.width / (2 * dpr) + 20 * k, -shadowCv.height / (2 * dpr) + 34 * k, shadowCv.width / dpr, shadowCv.height / dpr);
      ctx.filter = 'blur(1.4px)'; ctx.globalAlpha = 1; ctx.drawImage(shipCv, -shipCv.width / (2 * dpr), -shipCv.height / (2 * dpr), shipCv.width / dpr, shipCv.height / dpr);
      ctx.filter = 'none'; ctx.restore();

      // AIS reticle on the deck
      const [rx, ry] = toScreen(-110, 0), pulse = (t % 3.2) / 3.2;
      ctx.save(); ctx.translate(rx, ry); ctx.strokeStyle = 'rgba(127,216,238,.9)'; ctx.lineWidth = 1.2; ctx.setLineDash([4, 6]); ctx.rotate(t * 0.5);
      ctx.beginPath(); ctx.arc(0, 0, 34 * Math.max(.8, k), 0, 7); ctx.stroke(); ctx.setLineDash([]); ctx.rotate(-t * 0.5);
      ctx.beginPath(); ctx.moveTo(-8, 0); ctx.lineTo(8, 0); ctx.moveTo(0, -8); ctx.lineTo(0, 8); ctx.stroke();
      ctx.strokeStyle = `rgba(127,216,238,${(1 - pulse) * .7})`; ctx.beginPath(); ctx.arc(0, 0, 34 + pulse * 90, 0, 7); ctx.stroke(); ctx.restore();
      const co = $('#callout'); if (co) { co.style.left = (rx + 78) + 'px'; co.style.top = (ry - 150) + 'px'; if (t > 1.5) co.classList.add('on'); }

      // plankton / bubbles
      for (const p of plankton) {
        p.x += (p.vx * p.z - S.mx * 3) * dt; p.y += p.vy * p.z * dt; p.tw += dt * 2;
        if (p.y < -10) { p.y = H + 10; p.x = rand(0, W); } if (p.x > W + 10) p.x = -10;
        const a = (0.25 + 0.35 * Math.sin(p.tw) ** 2) * p.z;
        ctx.fillStyle = `rgba(190,238,248,${a.toFixed(3)})`; ctx.beginPath(); ctx.arc(p.x - S.mx * 14 * p.z, p.y - S.my * 8 * p.z, p.r * p.z + .3, 0, 7); ctx.fill();
      }

      // cursor ripples
      for (let i = ripples.length - 1; i >= 0; i--) {
        const r = ripples[i], u = (now - r.t) / 1900; if (u > 1) { ripples.splice(i, 1); continue; }
        ctx.strokeStyle = `rgba(210,242,250,${((1 - u) * .45).toFixed(3)})`; ctx.lineWidth = 1.4; ctx.beginPath(); ctx.ellipse(r.x, r.y, 8 + u * 110, (8 + u * 110) * .42, 0, 0, 7); ctx.stroke();
      }
    }

    // input
    const hero = canvas.parentElement;
    hero.addEventListener('mousemove', e => {
      const r = hero.getBoundingClientRect(); const x = e.clientX - r.left, y = e.clientY - r.top;
      S.tx = (x / r.width - .5) * 2; S.ty = (y / r.height - .5) * 2;
      const now = performance.now(); if (now - lastRipple > 90) { lastRipple = now; ripples.push({ x, y, t: now }); }
    });
    hero.addEventListener('mouseleave', () => { S.tx = 0; S.ty = 0; });
    hero.addEventListener('click', e => { const r = hero.getBoundingClientRect(); for (let i = 0; i < 3; i++) setTimeout(() => ripples.push({ x: e.clientX - r.left, y: e.clientY - r.top, t: performance.now() }), i * 180); });
    window.addEventListener('resize', resize);
    resize(); rafId = requestAnimationFrame(frame);
    return { resize, stop() { cancelAnimationFrame(rafId); } };
  }

  /* ================================================================ map helpers */
  function* polygons(g) {
    if (!g) return;
    if (g.type === 'Polygon') yield g.coordinates;
    else if (g.type === 'MultiPolygon') for (const p of g.coordinates) yield p;
    else if (g.type === 'GeometryCollection') for (const x of g.geometries) yield* polygons(x);
  }
  function makeProj(b, w, h, pad = 0) {            // b = [lon0, lat0, lon1, lat1]; equirectangular with cos(60°)
    const c = Math.cos(60 * Math.PI / 180), gw = (b[2] - b[0]) * c, gh = b[3] - b[1];
    const k = Math.min((w - pad * 2) / gw, (h - pad * 2) / gh), ox = (w - gw * k) / 2, oy = (h - gh * k) / 2;
    const f = (lon, lat) => [ox + (lon - b[0]) * c * k, oy + (b[3] - lat) * k]; f.k = k; return f;
  }
  function landPath(c, geom, proj) {
    c.beginPath();
    for (const poly of polygons(geom)) for (const ring of poly) ring.forEach(([lo, la], i) => { const [x, y] = proj(lo, la); i ? c.lineTo(x, y) : c.moveTo(x, y); });
  }
  const STATE_COL = { underway: '#46c6cf', still: '#7d93a8', inport: '#e6d3a8', stale: '#47607a', silent: '#47607a', dark: '#ff4d5e' };

  function hiDpi(canvas, cssW, cssH) {
    const dpr = Math.min(window.devicePixelRatio || 1, 2); canvas.style.height = cssH + 'px';
    canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr);
    const c = canvas.getContext('2d'); c.setTransform(dpr, 0, 0, dpr, 0, 0); return { c, dpr };
  }

  /* ================================================================ Epic-style paper map */
  function drawGulf() {
    const cv = $('#gulfCanvas'), d = S.data; if (!cv || !d.land) return;
    const cssW = cv.parentElement.clientWidth || 900, cssH = Math.round(cssW / 2.5);
    const { c, dpr } = hiDpi(cv, cssW, cssH), bounds = [21.15, 58.98, 29.95, 60.8], proj = makeProj(bounds, cssW, cssH, 6);
    c.clearRect(0, 0, cssW, cssH);
    // land masks: sharp (to skip land) and blurred (to size the dots near the coast)
    const mk = blur => { const o = document.createElement('canvas'); o.width = cssW; o.height = cssH; const x = o.getContext('2d'); if (blur) x.filter = `blur(${blur}px)`; landPath(x, d.land, proj); x.fillStyle = '#000'; x.fill('evenodd'); return x.getImageData(0, 0, cssW, cssH).data; };
    const sharp = mk(0), soft = mk(16), sp = 8.5;
    const at = (m, x, y) => m[((Math.min(cssH - 1, Math.max(0, Math.round(y))) * cssW) + Math.min(cssW - 1, Math.max(0, Math.round(x)))) * 4 + 3];
    for (let row = 0, y = 4; y < cssH; y += sp * 0.866, row++) for (let x = 4 + (row % 2 ? sp / 2 : 0); x < cssW; x += sp) {
      if (at(sharp, x, y) > 24) continue;
      const near = at(soft, x, y) / 255, r = 0.9 + 2.6 * Math.pow(Math.min(1, near * 1.6), 1.4);
      c.fillStyle = `rgba(120,184,218,${(0.7 + 0.3 * near).toFixed(2)})`; c.beginPath(); c.arc(x, y, r, 0, 7); c.fill();
    }
    // land (olive-sand, brown coast) with a soft watercolour edge
    c.save(); landPath(c, d.land, proj); c.fillStyle = '#d7ddb6'; c.fill('evenodd'); c.clip('evenodd');
    c.filter = 'blur(9px)'; landPath(c, d.land, proj); c.strokeStyle = 'rgba(160,172,112,.55)'; c.lineWidth = 18; c.stroke(); c.restore();
    landPath(c, d.land, proj); c.strokeStyle = 'rgba(140,122,91,.85)'; c.lineWidth = 1.1; c.stroke();
    // names
    const label = (txt, lon, lat, size, col, italic) => { const [x, y] = proj(lon, lat); c.font = `${italic ? 'italic ' : ''}${size}px "Cormorant Garamond", Georgia, serif`; c.fillStyle = col; c.textAlign = 'center'; c.letterSpacing = italic ? '1px' : '8px'; c.fillText(txt, x, y); c.letterSpacing = '0px'; };
    label('FINLAND', 25.4, 60.62, 21, '#8c7a5b'); label('ESTONIA', 25.6, 59.27, 21, '#8c7a5b'); label('RUSSIA', 29.0, 60.55, 17, '#8c7a5b');
    label('Gulf of Finland', 25.3, 59.93, 26, '#4f9cc4', true);
    // live ships (tiny navy dots, red for dark)
    (d.ships || []).filter(s => !s.is_synthetic).forEach(s => {
      const [x, y] = proj(s.lon, s.lat); if (x < 0 || y < 0 || x > cssW || y > cssH) return;
      c.fillStyle = s.state === 'dark' ? '#e0263d' : 'rgba(11,53,80,.72)'; c.beginPath(); c.arc(x, y, s.state === 'dark' ? 3.4 : 1.7, 0, 7); c.fill();
    });
    // ports: ring + leader line + label
    const off = { FIHEL: [-80, -58], EETLL: [-64, 46], FIKTK: [30, -54], FITKU: [44, -52], FIHKO: [22, 52], FINLI: [40, 72] };
    (d.ports || []).filter(p => off[p.locode]).forEach(p => {
      const [x, y] = proj(p.lon, p.lat), [dx, dy] = off[p.locode], lx = x + dx, ly = y + dy;
      c.strokeStyle = 'rgba(140,122,91,.8)'; c.lineWidth = 1; c.beginPath(); c.moveTo(x, y); c.lineTo(lx, ly); c.stroke();
      c.fillStyle = '#f6f2ea'; c.strokeStyle = '#8c7a5b'; c.lineWidth = 2; c.beginPath(); c.arc(x, y, 8.5, 0, 7); c.fill(); c.stroke();
      c.fillStyle = '#8c7a5b'; c.beginPath(); c.arc(x, y, 4, 0, 7); c.fill();
      c.textAlign = dx < 0 ? 'right' : 'left'; c.fillStyle = '#7a6847'; c.font = '600 14px Inter, sans-serif'; c.fillText(p.name, lx + (dx < 0 ? -6 : 6), ly + 4);
      c.font = '11px Inter, sans-serif'; c.fillStyle = '#9a8a68'; c.fillText(`${p.ships_inside} in port · ${p.entries_24h} arrivals/24 h`, lx + (dx < 0 ? -6 : 6), ly + 19);
    });
    c.textAlign = 'left';
  }

  /* ================================================================ tracker mock-up */
  let deviceBase = null, deviceMeta = null;
  function buildDevice() {
    const cv = $('#deviceCanvas'), d = S.data; if (!cv || !d.land || !d.ships) return;
    const box = cv.parentElement, cssW = box.clientWidth || 1000, cssH = box.clientHeight || 500;
    const { c, dpr } = hiDpi(cv, cssW, cssH); cv.style.height = '100%';
    const proj = makeProj([22.35, 59.3, 28.9, 60.78], cssW, cssH, 8);
    const base = document.createElement('canvas'); base.width = cssW * dpr; base.height = cssH * dpr; const b = base.getContext('2d'); b.setTransform(dpr, 0, 0, dpr, 0, 0);
    const g = b.createLinearGradient(0, 0, 0, cssH); g.addColorStop(0, '#07334d'); g.addColorStop(1, '#031624'); b.fillStyle = g; b.fillRect(0, 0, cssW, cssH);
    b.strokeStyle = 'rgba(127,216,238,.07)'; b.lineWidth = 1;
    for (let lo = 22; lo <= 29; lo++) { const [x] = proj(lo, 60); b.beginPath(); b.moveTo(x, 0); b.lineTo(x, cssH); b.stroke(); }
    for (let la = 59; la <= 61; la += .5) { const [, y] = proj(23, la); b.beginPath(); b.moveTo(0, y); b.lineTo(cssW, y); b.stroke(); }
    landPath(b, d.land, proj); b.fillStyle = '#0c3a4d'; b.fill('evenodd'); b.strokeStyle = 'rgba(127,216,238,.55)'; b.lineWidth = 1.2; b.shadowColor = 'rgba(70,198,207,.6)'; b.shadowBlur = 8; b.stroke(); b.shadowBlur = 0;
    (d.ports || []).forEach(p => { const [x, y] = proj(p.lon, p.lat); b.strokeStyle = 'rgba(230,211,168,.8)'; b.setLineDash([3, 4]); b.beginPath(); b.arc(x, y, 11, 0, 7); b.stroke(); b.setLineDash([]); b.fillStyle = '#e6d3a8'; b.font = '600 10px Montserrat, sans-serif'; b.fillText(p.name.toUpperCase(), x + 15, y + 3); });
    const darkOnes = [];
    d.ships.forEach(s => {
      const [x, y] = proj(s.lon, s.lat); if (x < 0 || y < 0 || x > cssW || y > cssH) return;
      if (s.state === 'dark') { darkOnes.push({ x, y, s }); return; }
      b.fillStyle = STATE_COL[s.state] || '#46c6cf'; b.shadowColor = b.fillStyle; b.shadowBlur = 5; b.beginPath(); b.arc(x, y, 2.3, 0, 7); b.fill();
    });
    b.shadowBlur = 0;
    deviceBase = base; deviceMeta = { cssW, cssH, dpr, darkOnes };
    const mid = cssW / 2; const target = darkOnes.filter(o => o.x > 130 && o.x < cssW - 130 && o.y > 70).sort((a, z) => ((a.s.is_synthetic ? 1e4 : 0) + Math.abs(a.x - mid)) - ((z.s.is_synthetic ? 1e4 : 0) + Math.abs(z.x - mid)))[0];
    const pop = $('#popCard');
    if (target) {
      const gap = (d.gaps || []).find(g => g.mmsi === target.s.mmsi);
      pop.innerHTML = `<b>${esc(target.s.name || 'MMSI ' + target.s.mmsi)}${target.s.is_synthetic ? ' (demo)' : ''}</b><span>silent ${fmtDur(target.s.silent_min)} · ${esc(target.s.ship_class)}</span><br><em>GOING DARK</em>`;
      pop.style.left = target.x + 'px'; pop.style.top = target.y + 'px'; pop.style.display = 'block'; deviceMeta.target = target;
    } else pop.style.display = 'none';
    const list = $('#pingList');
    const recent = d.ships.filter(s => !s.is_synthetic && s.state !== 'stale' && s.state !== 'silent').sort((a, z) => a.silent_min - z.silent_min).slice(0, 3);
    const rows = [];
    if (target) rows.push(`<div class="row"><i class="red"></i><div><b>${esc(target.s.name || target.s.mmsi)}</b><span>going dark · ${fmtDur(target.s.silent_min)}</span></div></div>`);
    recent.forEach(s => rows.push(`<div class="row"><i></i><div><b>${esc(s.name || 'MMSI ' + s.mmsi)}</b><span>${s.speed_knots != null ? (+s.speed_knots).toFixed(1) : '–'} kn · ${fmtDur(s.silent_min)} ago</span></div></div>`));
    list.innerHTML = rows.join('');
  }
  function drawDevice(now) {
    requestAnimationFrame(drawDevice);
    const cv = $('#deviceCanvas'); if (!S.inView.device || !deviceBase || !cv || document.hidden) return;
    const { cssW, cssH, dpr, darkOnes } = deviceMeta, c = cv.getContext('2d'); c.setTransform(1, 0, 0, 1, 0, 0); c.drawImage(deviceBase, 0, 0); c.setTransform(dpr, 0, 0, dpr, 0, 0);
    const t = now / 1000;
    // slow radar sweep from the middle of the Gulf
    const ox = cssW * .52, oy = cssH * .55, ang = t * .6;
    c.save(); c.translate(ox, oy); c.rotate(ang); const sg = c.createLinearGradient(0, 0, cssW * .7, 0); sg.addColorStop(0, 'rgba(70,198,207,.16)'); sg.addColorStop(1, 'rgba(70,198,207,0)');
    c.fillStyle = sg; c.beginPath(); c.moveTo(0, 0); c.arc(0, 0, cssW * .7, -.22, 0); c.closePath(); c.fill(); c.restore();
    darkOnes.forEach(({ x, y }, i) => {
      const u = ((t + i * .4) % 2.2) / 2.2; c.strokeStyle = `rgba(255,77,94,${(1 - u) * .8})`; c.lineWidth = 1.5; c.beginPath(); c.arc(x, y, 5 + u * 26, 0, 7); c.stroke();
      c.fillStyle = '#ff4d5e'; c.shadowColor = '#ff4d5e'; c.shadowBlur = 12; c.beginPath(); c.arc(x, y, 4, 0, 7); c.fill(); c.shadowBlur = 0;
    });
  }

  /* ================================================================ data + helpers */
  const esc = v => String(v ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  const fmtDur = m => { m = Number(m); if (!isFinite(m)) return '–'; if (m < 60) return Math.round(m) + ' min'; return Math.floor(m / 60) + ' h ' + Math.round(m % 60) + ' min'; };
  const fmtN = n => Number(n ?? 0).toLocaleString('en-US');

  function countUp(el, to, ms = 1700) {
    const from = +el.dataset.v || 0; el.dataset.v = to; const t0 = performance.now();
    const step = now => { const u = Math.min(1, (now - t0) / ms), e = ease(u); el.textContent = fmtN(Math.round(from + (to - from) * e)); if (u < 1) requestAnimationFrame(step); };
    requestAnimationFrame(step);
  }
  function renderNumbers(s) {
    const items = [['active_now', 'Ships on the water now', 'underway or reporting'], ['fixes_stored', 'Positions stored', 'every one checked'], ['rejected_total', 'Records refused', 'always with a reason'], ['dark', 'Ships that went dark', 'unexplained silences', 'red']];
    const wrap = $('#bigNums');
    if (!wrap.children.length) wrap.innerHTML = items.map(([k, l, sm, cls]) => `<div class="bn reveal ${cls || ''}"><b data-k="${k}">0</b><span>${l}</span><small>${sm}</small></div>`).join('');
    $$('b[data-k]', wrap).forEach(el => { const v = el.dataset.k === 'dark' ? (s.dark_open + s.dark_closed) : s[el.dataset.k]; if (S.numbersSeen) el.textContent = fmtN(v), el.dataset.v = v; else el.dataset.target = v; });
    observeReveal();
  }
  function renderHeroLive(s) {
    $('#heroLive').innerHTML = `<span><i class="live-dot"></i>Live</span><span><b>${fmtN(s.active_now)}</b>ships on the water</span><span class="${s.dark_open ? 'red' : ''}"><b>${fmtN(s.dark_open)}</b>silent right now</span><span><b>${fmtN(s.fixes_stored)}</b>positions stored</span>`;
  }

  async function loadData() {
    try {
      const [summary, ships, gaps, ports, land] = await Promise.all([DSW.api('/api/summary'), DSW.api('/api/ships?synthetic=1'), DSW.api('/api/gaps?class=dark&synthetic=1'), DSW.api('/api/ports?synthetic=0'), S.data.land ? Promise.resolve(S.data.land) : DSW.api('/api/land')]);
      Object.assign(S.data, { summary, ships, gaps, ports, land });
      renderHeroLive(summary); renderNumbers(summary); drawGulf(); buildDevice();
    } catch (e) { console.warn('intro data', e); $('#heroLive').innerHTML = '<span>Live data unavailable</span>'; }
  }

  /* ================================================================ behaviour */
  let io = null;
  function observeReveal() {
    if (!io) io = new IntersectionObserver(es => es.forEach(e => {
      if (!e.isIntersecting) return; e.target.classList.add('in'); io.unobserve(e.target);
      if (e.target.id === 'bigNums' || e.target.classList.contains('bn')) {
        $$('#bigNums b[data-k]').forEach(b => { if (!b.dataset.counted) { b.dataset.counted = 1; countUp(b, +b.dataset.target || 0); } }); S.numbersSeen = true;
      }
    }), { root: intro(), threshold: 0.18 });
    $$('.reveal:not(.in)').forEach(el => io.observe(el));
  }

  function go(name) { const sec = $(`[data-sec="${name}"]`); if (sec) intro().scrollTo({ top: name === 'top' ? 0 : sec.offsetTop - 10, behavior: 'smooth' }); }

  function enter(tab, instant) {
    if (S.entered && instant !== true) return;
    S.entered = true; const el = intro();
    document.body.classList.remove('intro-on');
    const target = tab || 'map';
    DSW.showTab(target);
    if (DSW.S.map) setTimeout(() => DSW.S.map.invalidateSize(), 80);
    DSW.refreshAll();
    if (instant === true) { el.classList.add('gone'); S.running = false; return; }
    el.classList.add('leaving'); S.running = false;
    setTimeout(() => el.classList.add('gone'), 1000);
  }
  function show() {
    const el = intro(); S.entered = false; S.running = true;
    el.classList.remove('gone'); void el.offsetWidth; el.classList.remove('leaving'); el.scrollTop = 0;
    document.body.classList.add('intro-on'); history.replaceState(null, '', location.pathname);
    loadData();
  }

  function boot() {
    const el = intro(); if (!el) return;
    document.body.classList.add('intro-on');
    $$('[data-go]').forEach(a => a.addEventListener('click', e => { e.preventDefault(); go(a.dataset.go); }));
    $$('[data-enter]').forEach(a => a.addEventListener('click', e => { e.preventDefault(); enter(a.dataset.enter); }));
    $('#brandHome').addEventListener('click', show);
    el.addEventListener('scroll', () => {
      $('#inNav').classList.toggle('solid', el.scrollTop > 60);
      const h = $('.in-hero').offsetHeight; S.inView.hero = el.scrollTop < h * 1.05;
    }, { passive: true });
    const dev = $('.device'); if (dev) new IntersectionObserver(es => { S.inView.device = es[0].isIntersecting; }, { root: el, threshold: 0.05 }).observe(dev);
    // tiny decorative dots for the GIS tile
    const gd = $('#gisdots'); if (gd) { let h = ''; for (let y = 20, r = 0; y < 330; y += 14, r++) for (let x = (r % 2 ? 10 : 3); x < 400; x += 14) { const k = Math.max(0, 1 - Math.abs(y - (250 - x * .25)) / 90); if (k > .05) h += `<circle cx="${x}" cy="${y}" r="${(0.6 + 2.6 * k).toFixed(1)}"/>`; } gd.innerHTML = h; }
    observeReveal();
    S.scene = startOcean($('#oceanCanvas'));
    requestAnimationFrame(drawDevice);
    loadData(); setInterval(() => { if (!S.entered) loadData(); }, 60000);
    window.addEventListener('resize', () => { drawGulf(); buildDevice(); });
    if (location.hash && location.hash.length > 1) enter(location.hash.slice(1), true);   // deep link: skip the opening page
  }

  return { boot, enter, show };
})();
