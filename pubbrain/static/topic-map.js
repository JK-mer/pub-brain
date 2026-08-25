/* Topic co-occurrence map (#49). Hand-rolled force layout — 26 nodes need no
   library, and the workbench ships no frameworks on purpose.

   Seeded layout: the same graph settles into the same picture on every visit.
   Spatial memory is a learning feature — "industrial policy sits lower left"
   only works if it still does tomorrow. */

(() => {
  "use strict";

  const svg = document.getElementById("topic-map");
  const panel = document.getElementById("panel-body");
  const stage = document.getElementById("map-root");
  const slider = document.getElementById("threshold");
  const edgeCount = document.getElementById("edge-count");
  const W = 1000, H = 640;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* Deterministic PRNG — the seed is the whole point, see header comment. */
  function mulberry32(a) {
    return () => {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  const state = {
    nodes: [], edges: [], shown: [],
    threshold: +slider.value,
    pinned: null,          // slug pinned by click, panel stays on it
    zoom: { x: 0, y: 0, k: 1 },
    upcoming: null,        // open notes per topic (#56), fetched separately
  };

  const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  /* ---------- data ---------- */

  fetch(MAP_URLS.graph).then((r) => r.json()).then((graph) => {
    const rand = mulberry32(42);
    const maxW = Math.max(...graph.nodes.map((n) => n.weighted));
    // Seeded ring start: spread from the first tick, identical every visit.
    state.nodes = graph.nodes.map((n, i) => ({
      ...n,
      r: 9 + 26 * Math.sqrt(n.weighted / maxW),
      x: W / 2 + Math.cos(i * 2.4) * (170 + rand() * 130),
      y: H / 2 + Math.sin(i * 2.4) * (110 + rand() * 90),
      vx: 0, vy: 0, fixed: false,
    }));
    const bySlug = Object.fromEntries(state.nodes.map((n) => [n.slug, n]));
    state.edges = graph.edges.map((e) => ({
      a: bySlug[e.s1], b: bySlug[e.s2], n: e.n,
    })).filter((e) => e.a && e.b);
    applyThreshold();
    build();
    simulate();
    loadUpcoming();
  });

  /* Topics with something coming (#56). A second fetch: the graph describes
     the published record and does not know notes exist. A dashed ring and a
     count — the same "not yet real" grammar as the coverage view's empty
     glyph — and the node's card lists them. */
  function loadUpcoming() {
    if (!MAP_URLS.upcoming) return;
    fetch(MAP_URLS.upcoming).then((r) => r.json()).then((edge) => {
      state.upcoming = edge;
      for (const n of state.nodes) {
        const count = edge.by_topic[n.slug] || 0;
        if (!count) continue;
        const ring = elt("circle", { class: "coming-ring", r: n.r + 5 });
        const text = elt("text", { class: "coming-count", x: n.r + 9,
                                   y: -(n.r + 5) });
        text.textContent = `+${count}`;
        n.el.append(ring, text);
      }
      if (state.pinned) showTopic(state.pinned);
    }).catch(() => {});
  }

  function applyThreshold() {
    state.threshold = +slider.value;
    state.shown = state.edges.filter((e) => e.n >= state.threshold);
    edgeCount.textContent = `${state.shown.length} edges`;
  }

  /* ---------- force simulation ---------- */

  let alpha = 1;

  function tick() {
    const nodes = state.nodes;
    // pairwise repulsion — n = 26, brute force is nothing
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        let d2 = dx * dx + dy * dy || 1;
        const min = (a.r + b.r + 56);      // room for the labels underneath
        const f = Math.min(12500 / d2, 8) + (d2 < min * min ? 1.4 : 0);
        const d = Math.sqrt(d2);
        dx /= d; dy /= d;
        a.vx -= dx * f; a.vy -= dy * f;
        b.vx += dx * f; b.vy += dy * f;
      }
    }
    // springs along shown edges — stronger ties pull closer
    for (const e of state.shown) {
      const rest = 255 - 6 * Math.min(e.n, 20);
      let dx = e.b.x - e.a.x, dy = e.b.y - e.a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = ((d - rest) / d) * 0.008 * Math.sqrt(e.n);
      e.a.vx += dx * f; e.a.vy += dy * f;
      e.b.vx -= dx * f; e.b.vy -= dy * f;
    }
    // gentle gravity to the center, hard walls at the frame. The x-pull is
    // softer: the stage is wide, so let the graph use the width.
    for (const n of nodes) {
      n.vx += (W / 2 - n.x) * 0.001;
      n.vy += (H / 2 - n.y) * 0.0019;
      if (!n.fixed) {
        n.x += n.vx * alpha; n.y += n.vy * alpha;
      }
      n.vx *= 0.6; n.vy *= 0.6;
      n.x = Math.max(n.r + 4, Math.min(W - n.r - 4, n.x));
      n.y = Math.max(n.r + 4, Math.min(H - n.r - 16, n.y));
    }
  }

  function simulate() {
    if (reduced) {
      for (let i = 0; i < 700; i++) { alpha = Math.max(0.08, alpha * 0.996); tick(); }
      alpha = 0;
      render();
      return;
    }
    const step = () => {
      for (let i = 0; i < 3; i++) tick();
      render();
      alpha *= 0.985;
      if (alpha > 0.02) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function reheat(a = 0.3) {
    const was = alpha;
    alpha = Math.max(alpha, a);
    if (was <= 0.02) simulate();
  }

  /* ---------- svg ---------- */

  const root = elt("g", {});
  const edgeLayer = elt("g", { class: "edges" });
  const nodeLayer = elt("g", { class: "nodes" });
  root.append(edgeLayer, nodeLayer);
  svg.append(root);

  function elt(tag, attrs) {
    const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  }

  function build() {
    for (const e of state.edges) {
      e.el = elt("line", { class: "edge" });
      e.el.addEventListener("click", () => showPair(e));
      edgeLayer.append(e.el);
    }
    for (const n of state.nodes) {
      n.el = elt("g", { class: "node", tabindex: 0, role: "button",
                        "aria-label": n.label });
      n.circle = elt("circle", { r: n.r });
      n.text = elt("text", { y: n.r + 13 });
      n.text.textContent = n.label;
      n.el.append(n.circle, n.text);
      n.el.addEventListener("pointerenter", () => { if (!state.pinned) focus(n); });
      n.el.addEventListener("pointerleave", () => { if (!state.pinned) unfocus(); });
      n.el.addEventListener("focus", () => { if (!state.pinned) focus(n); });
      n.el.addEventListener("blur", () => { if (!state.pinned) unfocus(); });
      n.el.addEventListener("click", (ev) => {
        if (n.dragged) return;                 // a drag is not a click
        ev.stopPropagation();
        state.pinned = state.pinned === n ? null : n;
        state.pinned ? focus(n) : unfocus();
      });
      n.el.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") n.el.dispatchEvent(new Event("click"));
      });
      drag(n);
      nodeLayer.append(n.el);
    }
    svg.addEventListener("click", () => { state.pinned = null; unfocus(); });
  }

  function render() {
    for (const e of state.edges) {
      const on = e.n >= state.threshold;
      e.el.style.display = on ? "" : "none";
      if (!on) continue;
      e.el.setAttribute("x1", e.a.x); e.el.setAttribute("y1", e.a.y);
      e.el.setAttribute("x2", e.b.x); e.el.setAttribute("y2", e.b.y);
      e.el.setAttribute("stroke-width", Math.min(1 + (e.n - state.threshold) * 0.35, 7));
    }
    for (const n of state.nodes)
      n.el.setAttribute("transform", `translate(${n.x},${n.y})`);
  }

  /* ---------- the lens ---------- */

  function neighbors(node) {
    const near = new Set([node]);
    for (const e of state.shown)
      if (e.a === node) near.add(e.b);
      else if (e.b === node) near.add(e.a);
    return near;
  }

  function focus(node) {
    const near = neighbors(node);
    for (const n of state.nodes)
      n.el.classList.toggle("dim", !near.has(n));
    for (const e of state.edges)
      e.el.classList.toggle("lit", e.a === node || e.b === node);
    svg.classList.add("lens");
    showTopic(node);
  }

  function unfocus() {
    svg.classList.remove("lens");
    for (const n of state.nodes) n.el.classList.remove("dim");
    for (const e of state.edges) e.el.classList.remove("lit");
    panel.innerHTML = `<p class="panel-hint">Hover a topic to pull its card.
      Click it to pin; click a link between two topics for the publications
      they share. Drag to arrange, scroll to zoom.</p>`;
  }

  /* ---------- panel cards ---------- */

  function card(head, rows) {
    panel.innerHTML = head + (rows || "");
  }

  function pubRows(pubs) {
    return pubs.map((p) => `
      <li><a href="${MAP_URLS.pub}${p.id}">${esc(p.title)}</a>
        <span class="panel-meta">${esc(p.pub_type)} · ${esc((p.date_published || "").slice(0, 7))}</span>
        ${p.summary_one_liner ? `<span class="panel-one">${esc(p.summary_one_liner)}</span>` : ""}
      </li>`).join("");
  }

  function comingRows(n) {
    const notes = (state.upcoming?.notes || [])
      .filter((note) => note.topics.includes(n.slug));
    if (!notes.length) return "";
    return `<div class="panel-sub">COMING — NOT PUBLISHED</div>
      <ul class="panel-list">${notes.map((note) => `
        <li><a href="${MAP_URLS.upcomingPage}">${esc(note.working_title)}</a>
        <span class="panel-meta">${esc(note.expected || "no quarter yet")}</span>
        </li>`).join("")}</ul>`;
  }

  function showTopic(n) {
    const links = state.shown.filter((e) => e.a === n || e.b === n).length;
    card(`
      <div class="panel-title">${esc(n.label)}</div>
      <dl class="panel-stats">
        <dt>weighted output</dt><dd>${n.weighted}</dd>
        <dt>publications</dt><dd>${n.pubs}</dd>
        <dt>about / touches</dt><dd>${n.about} / ${n.pubs - n.about}</dd>
        <dt>links shown</dt><dd>${links}</dd>
      </dl>
      <a class="button panel-open" href="${MAP_URLS.catalog}?topic=${encodeURIComponent(n.slug)}&filtered=1">open in catalog</a>
      ${comingRows(n)}
      <div class="panel-sub">LATEST, ABOUT THIS TOPIC</div>
      <ul class="panel-list" id="spot"><li class="panel-hint">…</li></ul>`);
    fetch(`${MAP_URLS.topic}?slug=${encodeURIComponent(n.slug)}`)
      .then((r) => r.json())
      .then((d) => {
        const el = document.getElementById("spot");
        if (el) el.innerHTML = pubRows(d.publications) ||
          `<li class="panel-hint">nothing is primarily about this yet</li>`;
      });
  }

  function showPair(e) {
    state.pinned = e.a;      // keep the lens while reading the list
    focus(e.a);
    card(`
      <div class="panel-title">${esc(e.a.label)} × ${esc(e.b.label)}</div>
      <dl class="panel-stats"><dt>shared publications</dt><dd>${e.n}</dd></dl>
      <div class="panel-sub">THE INTERSECTION</div>
      <ul class="panel-list" id="pair"><li class="panel-hint">…</li></ul>`);
    fetch(`${MAP_URLS.pair}?a=${encodeURIComponent(e.a.slug)}&b=${encodeURIComponent(e.b.slug)}`)
      .then((r) => r.json())
      .then((d) => {
        const el = document.getElementById("pair");
        if (el) el.innerHTML = pubRows(d.publications);
      });
  }

  /* ---------- drag, zoom, controls ---------- */

  function drag(n) {
    n.el.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      n.el.setPointerCapture(ev.pointerId);
      n.fixed = true; n.dragged = false;
      const move = (mv) => {
        const p = toMap(mv);
        if (!n.dragged && Math.hypot(p.x - n.x, p.y - n.y) > 3) n.dragged = true;
        if (n.dragged) { n.x = p.x; n.y = p.y; reheat(0.12); render(); }
      };
      const up = (uv) => {
        n.el.releasePointerCapture(uv.pointerId);
        n.fixed = false;
        setTimeout(() => { n.dragged = false; }, 0);
        n.el.removeEventListener("pointermove", move);
        n.el.removeEventListener("pointerup", up);
      };
      n.el.addEventListener("pointermove", move);
      n.el.addEventListener("pointerup", up);
    });
  }

  function toMap(ev) {
    const pt = new DOMPoint(ev.clientX, ev.clientY)
      .matrixTransform(svg.getScreenCTM().inverse());
    return { x: (pt.x - state.zoom.x) / state.zoom.k,
             y: (pt.y - state.zoom.y) / state.zoom.k };
  }

  function applyZoom() {
    root.setAttribute("transform",
      `translate(${state.zoom.x},${state.zoom.y}) scale(${state.zoom.k})`);
  }

  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const pt = new DOMPoint(ev.clientX, ev.clientY)
      .matrixTransform(svg.getScreenCTM().inverse());
    const k = Math.max(0.5, Math.min(4, state.zoom.k * (ev.deltaY < 0 ? 1.12 : 0.89)));
    state.zoom.x = pt.x - ((pt.x - state.zoom.x) / state.zoom.k) * k;
    state.zoom.y = pt.y - ((pt.y - state.zoom.y) / state.zoom.k) * k;
    state.zoom.k = k;
    applyZoom();
  }, { passive: false });

  let pan = null;
  svg.addEventListener("pointerdown", (ev) => {
    if (ev.target !== svg && ev.target !== root) return;
    pan = { x: ev.clientX, y: ev.clientY, zx: state.zoom.x, zy: state.zoom.y };
  });
  svg.addEventListener("pointermove", (ev) => {
    if (!pan) return;
    const ctm = svg.getScreenCTM();
    state.zoom.x = pan.zx + (ev.clientX - pan.x) / ctm.a;
    state.zoom.y = pan.zy + (ev.clientY - pan.y) / ctm.d;
    applyZoom();
  });
  addEventListener("pointerup", () => { pan = null; });

  slider.addEventListener("input", () => {
    applyThreshold();
    if (state.pinned) focus(state.pinned);
    reheat(0.25);
    render();
  });

  document.getElementById("reset-view").addEventListener("click", () => {
    state.zoom = { x: 0, y: 0, k: 1 };
    applyZoom();
  });

  document.getElementById("fullscreen").addEventListener("click", () => {
    document.fullscreenElement
      ? document.exitFullscreen()
      : stage.requestFullscreen();
  });
})();
