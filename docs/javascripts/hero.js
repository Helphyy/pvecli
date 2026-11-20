// The drawing behind the home page (overrides/home.html): a fleet of
// clusters around the workstation that runs pvecli. Each cluster is a ring
// of nodes linked in full mesh, each node carries its guests, and the API
// calls travel from the centre to the clusters along strands.
//
// Kept light: the fleet does not turn, so it is drawn once on a layer of its
// own; each frame only copies that layer and draws the calls in flight,
// and nothing is drawn while the tab is hidden.

(function () {
  "use strict";

  const canvas = document.querySelector(".pv-hero__canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Seeded random numbers: the same fleet on every visit
  let seed = 649;
  const rand = () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const range = (a, b) => a + (b - a) * rand();

  const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
  const scale = (a, s) => [a[0] * s, a[1] * s, a[2] * s];
  const cross = (a, b) => [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0],
  ];
  const norm = (a) => scale(a, 1 / Math.hypot(a[0], a[1], a[2]));

  // The fleet
  const CLUSTERS = 16;
  const STEPS = 64;
  const clusters = [];
  for (let i = 0; i < CLUSTERS; i++) {
    // Spread the clusters on a sphere (golden angle), with some jitter
    const y = 1 - (2 * (i + 0.5)) / CLUSTERS;
    const r = Math.sqrt(1 - y * y);
    const phi = i * 2.399963 + range(-0.15, 0.15);
    const center = scale([Math.cos(phi) * r, y * 0.85, Math.sin(phi) * r], range(0.8, 1.25));
    const axis = norm(center);
    const u = norm(cross(axis, Math.abs(axis[1]) < 0.9 ? [0, 1, 0] : [1, 0, 0]));
    const v = cross(axis, u);
    const ring = range(0.07, 0.12);
    const hue = range(20, 40);

    const nodes = [];
    const k = 3 + Math.floor(rand() * 4);
    for (let j = 0; j < k; j++) {
      const a = (j / k) * Math.PI * 2 + range(-0.2, 0.2);
      const pos = add(center, add(scale(u, Math.cos(a) * ring), scale(v, Math.sin(a) * ring)));
      const guests = [];
      const g = 2 + Math.floor(rand() * 3);
      for (let n = 0; n < g; n++) {
        guests.push({
          r: range(0.02, 0.04),
          a: range(0, Math.PI * 2),
          tilt: range(-1.2, 1.2),
        });
      }
      nodes.push({ pos, guests });
    }

    // Two strands from the centre to the cluster
    const strands = [];
    for (let s = 0; s < 2; s++) {
      const bend = add(
        scale(center, range(0.35, 0.6)),
        add(scale(u, range(-0.3, 0.3)), scale(v, range(-0.3, 0.3))),
      );
      const pts = [];
      for (let n = 0; n <= STEPS; n++) {
        const t = n / STEPS, b = 2 * (1 - t) * t, c = t * t;
        pts.push([
          b * bend[0] + c * center[0],
          b * bend[1] + c * center[1],
          b * bend[2] + c * center[2],
        ]);
      }
      strands.push(pts);
    }
    clusters.push({
      center, nodes, strands, hue,
      color: `hsl(${hue}, 100%, 55%)`,
      guest: `hsl(${hue + 10}, 100%, 75%)`,
    });
  }

  // A glow drawn once, then stamped with drawImage
  const sprite = document.createElement("canvas");
  sprite.width = sprite.height = 64;
  {
    const s = sprite.getContext("2d");
    const g = s.createRadialGradient(32, 32, 0, 32, 32, 32);
    g.addColorStop(0, "hsla(30, 100%, 70%, 1)");
    g.addColorStop(1, "hsla(30, 100%, 50%, 0)");
    s.fillStyle = g;
    s.fillRect(0, 0, 64, 64);
  }
  function glow(x, y, r, alpha) {
    ctx.globalAlpha = alpha;
    ctx.drawImage(sprite, x - r, y - r, r * 2, r * 2);
  }

  // Calls in flight: along a strand, then out to every node of the cluster
  const pulses = [];
  let lastPulse = 0;

  // Camera
  let width = 0, height = 0, cx = 0, cy = 0, unit = 0, ratio = 1;
  const rotY = 0.6, rotX = 0.35;
  const cY = Math.cos(rotY), sY = Math.sin(rotY);
  const cX = Math.cos(rotX), sX = Math.sin(rotX);
  function project(p) {
    const x1 = p[0] * cY + p[2] * sY;
    const z1 = -p[0] * sY + p[2] * cY;
    const y2 = p[1] * cX - z1 * sX;
    const z2 = p[1] * sX + z1 * cX;
    const f = 3.2 / (3.2 - z2);
    return [cx + x1 * f * unit, cy - y2 * f * unit, z2];
  }
  // Nearer is brighter
  const depth = (z) => 0.3 + 0.7 * Math.max(0, Math.min(1, (z + 1.3) / 2.6));

  // A point along a projected strand, between its samples
  const TRAIL = 10;
  function at(pts, pos) {
    const i = Math.min(Math.floor(pos), pts.length - 2);
    const f = pos - i;
    const a = pts[i], b = pts[i + 1];
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
  }

  function polyline(c, pts) {
    c.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) c.lineTo(pts[i][0], pts[i][1]);
  }

  // The still fleet, drawn once per size
  const layer = document.createElement("canvas");
  function drawFleet() {
    layer.width = canvas.width;
    layer.height = canvas.height;
    const l = layer.getContext("2d");
    l.setTransform(ratio, 0, 0, ratio, 0, 0);
    l.fillStyle = "#0b0c0f";
    l.fillRect(0, 0, width, height);
    l.globalCompositeOperation = "lighter";
    l.lineJoin = "round";
    l.lineCap = "round";
    for (const c of clusters) {
      const center = project(c.center);
      const a = depth(center[2]);
      c.projected = c.strands.map((s) => s.map(project));
      l.strokeStyle = c.color;
      l.lineWidth = 0.7;
      l.globalAlpha = 0.22 * a;
      l.beginPath();
      for (const pts of c.projected) polyline(l, pts);
      l.stroke();

      c.points = c.nodes.map((n) => project(n.pos));
      l.globalAlpha = 0.35 * a;
      l.lineWidth = 0.8;
      l.beginPath();
      const nodes = c.points;
      for (let i = 0; i < nodes.length; i++) {
        l.moveTo(center[0], center[1]);
        l.lineTo(nodes[i][0], nodes[i][1]);
        for (let j = i + 1; j < nodes.length; j++) {
          l.moveTo(nodes[i][0], nodes[i][1]);
          l.lineTo(nodes[j][0], nodes[j][1]);
        }
      }
      l.stroke();

      l.fillStyle = c.guest;
      l.globalAlpha = 0.6 * a;
      l.beginPath();
      for (const n of c.nodes) {
        for (const g of n.guests) {
          const p = project(add(n.pos, [
            Math.cos(g.a) * g.r,
            Math.sin(g.a) * g.r * Math.sin(g.tilt),
            Math.sin(g.a) * g.r * Math.cos(g.tilt),
          ]));
          l.rect(p[0] - 0.8, p[1] - 0.8, 1.6, 1.6);
        }
      }
      l.fill();

      for (const p of nodes) {
        l.globalAlpha = 0.4 * a;
        l.drawImage(sprite, p[0] - 6, p[1] - 6, 12, 12);
      }
      l.fillStyle = "#fff";
      l.globalAlpha = 0.8 * a;
      l.beginPath();
      for (const p of nodes) l.rect(p[0] - 1.2, p[1] - 1.2, 2.4, 2.4);
      l.fill();
    }
    const o = project([0, 0, 0]);
    l.globalAlpha = 0.6;
    l.drawImage(sprite, o[0] - 22, o[1] - 22, 44, 44);
    l.globalAlpha = 1;
    l.fillStyle = "#fff";
    l.beginPath();
    l.arc(o[0], o[1], 2.6, 0, Math.PI * 2);
    l.fill();
  }

  function resize() {
    ratio = Math.min(window.devicePixelRatio || 1, 1.5);
    width = canvas.clientWidth;
    height = canvas.clientHeight;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    const narrow = width < 760;
    cx = narrow ? width * 0.5 : width * 0.72;
    cy = narrow ? height * 0.32 : height * 0.48;
    unit = Math.min(width, height) * (narrow ? 0.36 : 0.42);
    drawFleet();
  }

  let running = false;
  function start() {
    if (!running) { running = true; requestAnimationFrame(loop); }
  }
  function loop(now) {
    if (document.hidden) { running = false; return; }
    requestAnimationFrame(loop);
    frame(now);
  }

  function frame(now) {
    const t = now / 1000;

    ctx.globalCompositeOperation = "copy";
    ctx.globalAlpha = 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.drawImage(layer, 0, 0);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    if (still) return;
    ctx.globalCompositeOperation = "lighter";

    // New calls
    if (now - lastPulse > 140) {
      lastPulse = now;
      pulses.push({ n: Math.floor(rand() * clusters.length), s: Math.floor(rand() * 2), born: t });
    }
    for (let i = pulses.length - 1; i >= 0; i--) {
      const p = pulses[i];
      const c = clusters[p.n];
      const travel = (t - p.born) / 1.8;
      if (travel < 1) {
        // Along the strand, at sub-segment precision, with a trail that fades
        const pts = c.projected[p.s];
        const e = travel * travel * (3 - 2 * travel);
        const head = at(pts, e * STEPS);
        const tailPos = Math.max(0, e * STEPS - TRAIL);
        const trail = [at(pts, tailPos)];
        for (let k = Math.ceil(tailPos); k < e * STEPS; k++) trail.push(pts[k]);
        trail.push(head);
        const tail = trail[0];
        const g = ctx.createLinearGradient(tail[0], tail[1], head[0], head[1]);
        g.addColorStop(0, "hsla(35, 100%, 70%, 0)");
        g.addColorStop(1, c.guest);
        ctx.strokeStyle = g;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.globalAlpha = 0.95;
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        polyline(ctx, trail);
        ctx.stroke();
        glow(head[0], head[1], 6, 0.9);
      } else if (travel < 1.35) {
        // Fanned out to the nodes
        const k = (travel - 1) / 0.35;
        const o = project(c.center);
        for (const q of c.points) {
          glow(o[0] + (q[0] - o[0]) * k, o[1] + (q[1] - o[1]) * k, 5, 0.8 * (1 - k));
        }
      } else {
        pulses.splice(i, 1);
      }
    }
  }

  let resizing = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizing);
    resizing = setTimeout(() => { resize(); frame(performance.now()); }, 100);
  });
  if (!still) {
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) start();
    });
  }

  resize();
  frame(0);
  if (!still) start();
})();
