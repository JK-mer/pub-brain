/* The embedding landscape (#49). Coordinates come precomputed and cached —
   this file only draws them, so the same picture greets every visit and a
   place, once learned, stays learned.

   Hover is nearest-point, not per-mark: at ~1,100 points the marks are small
   and a generous hit radius beats 1,100 tiny targets (dataviz rule: hit
   target bigger than the mark). Color is the cluster of the primary topic —
   six validated hues; the tooltip names the exact topic. */

(() => {
  "use strict";

  const svg = document.getElementById("scape");
  const stage = document.querySelector(".scape-stage");
  const rootEl = document.getElementById("scape-root");
  const tip = document.getElementById("scape-tip");
  const legend = document.getElementById("scape-legend");
  const findBox = document.getElementById("scape-find");
  const findCount = document.getElementById("find-count");
  const W = 1000, H = 640, M = 26;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function elt(tag, attrs) {
    const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  }

  const state = {
    points: [], clusters: [],
    muted: new Set(),          // cluster indices toggled off via the legend
    hot: null,                 // the point under the cursor
    hits: [],                  // find matches
    zoom: { x: 0, y: 0, k: 1 },
  };

  const root = elt("g", {});
  svg.append(root);

  /* ---------- data ---------- */

  fetch(SCAPE_URLS.data).then((r) => r.json()).then((d) => {
    if (!d.fitted) {
      document.getElementById("scape-empty").hidden = false;
      findBox.disabled = true;
      return;
    }
    state.clusters = d.clusters;
    // Big marks go down first so small ones stay hoverable on top of them.
    state.points = d.points.sort((a, b) => b.w - a.w);
    const xs = state.points.map((p) => p.x), ys = state.points.map((p) => p.y);
    const lo = [Math.min(...xs), Math.min(...ys)];
    const hi = [Math.max(...xs), Math.max(...ys)];
    const sx = (W - 2 * M) / ((hi[0] - lo[0]) || 1);
    const sy = (H - 2 * M) / ((hi[1] - lo[1]) || 1);
    for (const p of state.points) {
      p.px = M + (p.x - lo[0]) * sx;
      p.py = M + (p.y - lo[1]) * sy;
      p.r = 1 + 2.6 * Math.sqrt(p.w);
      p.el = elt("circle", {
        class: "pt", cx: p.px, cy: p.py, r: p.r,
        fill: p.cluster >= 0 ? `var(--viz-${p.cluster + 1})` : "var(--viz-other)",
      });
      root.append(p.el);
    }
    buildLegend();
  });

  /* ---------- legend: six clusters, click to mute ---------- */

  function buildLegend() {
    const counts = state.clusters.map(() => 0);
    let loose = 0;
    for (const p of state.points)
      p.cluster >= 0 ? counts[p.cluster]++ : loose++;
    state.clusters.forEach((name, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.setAttribute("aria-pressed", "false");
      b.innerHTML = `<span class="swatch" style="background:var(--viz-${i + 1})"></span>
        ${esc(name)} <span class="count-mono">${counts[i]}</span>`;
      b.addEventListener("click", () => {
        state.muted.has(i) ? state.muted.delete(i) : state.muted.add(i);
        b.classList.toggle("off", state.muted.has(i));
        b.setAttribute("aria-pressed", state.muted.has(i));
        applyMute();
      });
      legend.append(b);
    });
    if (loose) {
      const s = document.createElement("span");
      s.className = "chip quiet";
      s.innerHTML = `<span class="swatch" style="background:var(--viz-other)"></span>
        awaiting topics <span class="count-mono">${loose}</span>`;
      legend.append(s);
    }
  }

  function applyMute() {
    for (const p of state.points)
      p.el.classList.toggle("mute",
        p.cluster >= 0 && state.muted.has(p.cluster));
  }

  /* ---------- nearest-point hover, tooltip, click-through ---------- */

  function toMap(ev) {
    const pt = new DOMPoint(ev.clientX, ev.clientY)
      .matrixTransform(svg.getScreenCTM().inverse());
    return { x: (pt.x - state.zoom.x) / state.zoom.k,
             y: (pt.y - state.zoom.y) / state.zoom.k };
  }

  function nearest(ev) {
    const m = toMap(ev);
    const reach = 14 / state.zoom.k;      // generous, and constant on screen
    let best = null, bestD = Infinity;
    for (const p of state.points) {
      if (p.cluster >= 0 && state.muted.has(p.cluster)) continue;
      const d = Math.hypot(p.px - m.x, p.py - m.y) - p.r;
      if (d < bestD && d < reach) { best = p; bestD = d; }
    }
    return best;
  }

  svg.addEventListener("mousemove", (ev) => {
    if (pan) return;
    const p = nearest(ev);
    if (p !== state.hot) {
      state.hot?.el.classList.remove("hot");
      state.hot = p;
      p?.el.classList.add("hot");
      svg.style.cursor = p ? "pointer" : "grab";
    }
    p ? showTip(p, ev) : (tip.hidden = true);
  });
  svg.addEventListener("mouseleave", () => {
    state.hot?.el.classList.remove("hot");
    state.hot = null;
    tip.hidden = true;
  });
  svg.addEventListener("click", () => {
    if (state.hot && !panned) location.href = SCAPE_URLS.pub + state.hot.id;
  });

  function showTip(p, ev) {
    tip.innerHTML = `<strong>${esc(p.title)}</strong>
      <span>${esc(p.type)} · ${esc(p.date || "no date")}</span>
      <span>${esc(p.topic || "no topic mapped")}</span>
      ${p.one_liner ? `<span class="tip-one">${esc(p.one_liner)}</span>` : ""}`;
    tip.hidden = false;
    const box = stage.getBoundingClientRect();
    const x = ev.clientX - box.left, y = ev.clientY - box.top;
    tip.style.left = Math.min(x + 14, box.width - tip.offsetWidth - 8) + "px";
    tip.style.top = Math.min(y + 14, box.height - tip.offsetHeight - 8) + "px";
  }

  /* ---------- find: the survey beam ---------- */

  function find(q) {
    for (const p of state.hits) p.el.classList.remove("hit");
    state.hits = [];
    const term = q.trim().toLowerCase();
    if (!term) { findCount.textContent = ""; return; }
    for (const p of state.points) {
      if ((p.title + " " + (p.one_liner || "") + " " + (p.topic || ""))
          .toLowerCase().includes(term)) {
        p.el.classList.add("hit");
        state.hits.push(p);
      }
    }
    findCount.textContent = `${state.hits.length} found`;
  }
  findBox.addEventListener("input", () => find(findBox.value));
  findBox.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { findBox.value = ""; find(""); }
  });

  /* ---------- zoom, pan, controls — the map's manners ---------- */

  function applyZoom() {
    root.setAttribute("transform",
      `translate(${state.zoom.x},${state.zoom.y}) scale(${state.zoom.k})`);
  }

  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const pt = new DOMPoint(ev.clientX, ev.clientY)
      .matrixTransform(svg.getScreenCTM().inverse());
    const k = Math.max(0.6, Math.min(9, state.zoom.k * (ev.deltaY < 0 ? 1.14 : 0.88)));
    state.zoom.x = pt.x - ((pt.x - state.zoom.x) / state.zoom.k) * k;
    state.zoom.y = pt.y - ((pt.y - state.zoom.y) / state.zoom.k) * k;
    state.zoom.k = k;
    applyZoom();
  }, { passive: false });

  let pan = null, panned = false;
  svg.addEventListener("pointerdown", (ev) => {
    pan = { x: ev.clientX, y: ev.clientY, zx: state.zoom.x, zy: state.zoom.y };
    panned = false;
  });
  svg.addEventListener("pointermove", (ev) => {
    if (!pan) return;
    if (Math.hypot(ev.clientX - pan.x, ev.clientY - pan.y) > 3) panned = true;
    if (!panned) return;
    const ctm = svg.getScreenCTM();
    state.zoom.x = pan.zx + (ev.clientX - pan.x) / ctm.a;
    state.zoom.y = pan.zy + (ev.clientY - pan.y) / ctm.d;
    tip.hidden = true;
    applyZoom();
  });
  addEventListener("pointerup", () => {
    pan = null;
    setTimeout(() => { panned = false; }, 0);
  });

  document.getElementById("reset-view").addEventListener("click", () => {
    state.zoom = { x: 0, y: 0, k: 1 };
    applyZoom();
  });

  document.getElementById("fullscreen").addEventListener("click", () => {
    document.fullscreenElement
      ? document.exitFullscreen()
      : rootEl.requestFullscreen();
  });
})();
