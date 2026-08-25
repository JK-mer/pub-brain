/* Attention over time (#49): a streamgraph of the top 8 topics + "other",
   with a per-topic drill-down underneath. Hand-rolled like the map.

   Series identity is fixed by total type-weighted output and never re-ranked
   when the measure toggles — color follows the entity, not its rank. */

(() => {
  "use strict";

  const svg = document.getElementById("stream");
  const tip = document.getElementById("tip");
  const legend = document.getElementById("legend");
  const drillBody = document.getElementById("drill-body");
  const drillPick = document.getElementById("drill-pick");
  const stage = document.getElementById("time-root");
  const W = 1000, H = 430, PAD = { l: 10, r: 10, t: 14, b: 26 };
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const TOP = 8;
  // `axis` is the drawn x-axis: the published quarters plus the leading edge
  // (#56). Bands only ever cover `data.quarters` — everything past the dashed
  // rule is expectation, not output.
  const state = { measure: "w", data: null, series: [], drilled: null,
                  axis: [], edge: null };

  const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  fetch(TIME_URLS.data).then((r) => r.json()).then((data) => {
    state.data = data;
    state.axis = data.quarters.slice();
    // Identity is set once, by weighted output (the server's sort order).
    const top = data.topics.slice(0, TOP).map((t, i) => ({ ...t, slot: i + 1 }));
    const rest = data.topics.slice(TOP);
    const other = {
      slug: null, label: "other", slot: "other",
      folded: rest.map((t) => t.label),
      w: sumRows(rest, "w", data.quarters.length),
      words: sumRows(rest, "words", data.quarters.length),
      n: sumRows(rest, "n", data.quarters.length),
    };
    state.series = [...top, other];
    buildLegend();
    buildPicker();
    draw();
    loadEdge();
  });

  /* ---------- the leading edge (#56) ----------
     What the institute is about to say, from hand-entered notes. A second
     fetch on purpose: the streamgraph's own data describes the published
     record and does not know these exist. A failure here must cost the chart
     nothing, so it is caught and dropped. */
  function loadEdge() {
    if (!TIME_URLS.upcoming) return;
    fetch(TIME_URLS.upcoming).then((r) => r.json()).then((edge) => {
      state.edge = edge;
      const last = state.data.quarters[state.data.quarters.length - 1];
      for (const q of quartersBetween(last, edge.edge[1]).slice(1))
        if (!state.axis.includes(q)) state.axis.push(q);
      if (edge.beyond) sayBeyond(edge);
      draw();
    }).catch(() => {});
  }

  /* The axis stops two years out so one far-off note cannot squash the
     chart — so anything past it has to be said, not dropped in silence. */
  function sayBeyond(edge) {
    const note = document.querySelector(".scope-note");
    if (!note) return;
    const span = document.createElement("span");
    span.textContent = ` ${edge.beyond} note${edge.beyond === 1 ? " is" : "s are"}
      expected beyond ${edge.edge[1]} and sit past the right-hand edge.`;
    note.append(span);
  }

  function quartersBetween(lo, hi) {
    const out = [];
    let [y, q] = [+lo.slice(0, 4), +lo.slice(-1)];
    for (let guard = 0; guard < 40; guard++) {
      out.push(`${y}-Q${q}`);
      if (`${y}-Q${q}` === hi) break;
      [y, q] = q === 4 ? [y + 1, 1] : [y, q + 1];
    }
    return out;
  }

  const sumRows = (list, key, len) => Array.from({ length: len },
    (_, i) => list.reduce((a, t) => a + t[key][i], 0));

  /* ---------- geometry ---------- */

  // Share of the plot reserved for the leading edge. Fixed rather than
  // proportional: laid out by quarter count, two quarters of twenty-six get
  // 8% of the width and the marks pile up on the frame — unreadable, and the
  // one part of this chart that is about the future.
  const EDGE_FRAC = 0.16;

  function x(i) {
    // Fractional indices are allowed: the "today" rule sits between two
    // quarters rather than on one.
    const published = state.data.quarters.length;
    const extra = state.axis.length - published;
    const inner = W - PAD.l - PAD.r;
    if (!extra) return PAD.l + (i / Math.max(published - 1, 1)) * inner;
    const wide = inner * (1 - EDGE_FRAC);
    if (i <= published - 1)
      return PAD.l + (i / Math.max(published - 1, 1)) * wide;
    // Inset at both ends so a mark on the last quarter is not half-clipped.
    const t = (i - (published - 1)) / extra;
    return PAD.l + wide + 8 + t * (inner * EDGE_FRAC - 18);
  }

  function stack() {
    // Stack in slot order, "other" at the bottom: palette adjacency is what
    // the CVD validator certified, and it certifies consecutive slots.
    const order = state.series.map((_, i) => i);
    const n = state.data.quarters.length;
    const vals = state.series.map((s) => s[state.measure]);
    const tops = state.series.map(() => new Array(n));
    const bots = state.series.map(() => new Array(n));
    let peak = 0;
    for (let q = 0; q < n; q++) {
      const sum = order.reduce((a, i) => a + vals[i][q], 0);
      let y = -sum / 2;                       // silhouette: centered wiggle
      for (const i of order) {
        bots[i][q] = y; y += vals[i][q]; tops[i][q] = y;
      }
      peak = Math.max(peak, sum);
    }
    const mid = PAD.t + (H - PAD.t - PAD.b) / 2;
    const k = (H - PAD.t - PAD.b) / Math.max(peak, 1);
    return { tops, bots, scale: (v) => mid + v * k };
  }

    /* Catmull-Rom through the quarter points, so the bands flow. */
  function smooth(pts) {
    if (pts.length < 3) return pts.map((p, i) =>
      `${i ? "L" : "M"}${p[0]},${p[1]}`).join("");
    let d = `M${pts[0][0]},${pts[0][1]}`;
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[Math.max(i - 1, 0)], p1 = pts[i],
            p2 = pts[i + 1], p3 = pts[Math.min(i + 2, pts.length - 1)];
      d += `C${p1[0] + (p2[0] - p0[0]) / 9},${p1[1] + (p2[1] - p0[1]) / 9},` +
           `${p2[0] - (p3[0] - p1[0]) / 9},${p2[1] - (p3[1] - p1[1]) / 9},` +
           `${p2[0]},${p2[1]}`;
    }
    return d;
  }

  /* ---------- draw ---------- */

  function elt(tag, attrs) {
    const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    return e;
  }

  function draw() {
    svg.textContent = "";
    const { tops, bots, scale } = stack();
    const qs = state.axis;

    // year gridlines + labels along the bottom
    for (let i = 0; i < qs.length; i++) {
      if (!qs[i].endsWith("Q1")) continue;
      svg.append(elt("line", { class: "grid", x1: x(i), y1: PAD.t,
                               x2: x(i), y2: H - PAD.b }));
      const t = elt("text", { class: "axis", x: Math.max(x(i), 22), y: H - 8 });
      t.textContent = qs[i].slice(0, 4);
      svg.append(t);
    }

    const placed = [];   // label boxes already on the chart
    const labelLayer = elt("g", {});   // labels ride above every band
    state.series.forEach((s, i) => {
      const up = tops[i].map((v, q) => [x(q), scale(v)]);
      const down = bots[i].map((v, q) => [x(q), scale(v)]).reverse();
      const path = elt("path", { class: "band", "data-i": i });
      path.setAttribute("d", smooth(up) + "L" + down.map((p) =>
        `${p[0]},${p[1]}`).join("L") + "Z");
      path.style.fill = s.slot === "other" ? "var(--viz-other)"
                                           : `var(--viz-${s.slot})`;
      path.addEventListener("pointermove", (ev) => hover(ev, s, tops[i], bots[i]));
      path.addEventListener("pointerleave", leave);
      path.addEventListener("click", () => drill(s));
      svg.append(path);

      // Direct label — the relief the palette validator demands for the
      // low-contrast light slots. Widest quarter first; if that box collides
      // with a label already placed, walk down the width ranking.
      // Published quarters only — a band has no width past the today rule.
      const byWidth = state.data.quarters.map((_, q) => q).sort((a, b) =>
        (tops[i][b] - bots[i][b]) - (tops[i][a] - bots[i][a]));
      const half = s.label.length * 3.4;
      for (const q of byWidth.slice(0, 10)) {
        const px = scale(tops[i][q]) - scale(bots[i][q]);
        if (px < 17) break;                    // thinner candidates only follow
        const bx = Math.max(half + 6, Math.min(W - half - 6, x(q)));
        const by = scale((tops[i][q] + bots[i][q]) / 2) + 4;
        const box = { x1: bx - half, x2: bx + half, y1: by - 12, y2: by + 4 };
        if (placed.some((o) => box.x1 < o.x2 && box.x2 > o.x1 &&
                               box.y1 < o.y2 && box.y2 > o.y1)) continue;
        const t = elt("text", { class: "band-label", x: bx, y: by });
        t.textContent = s.label;
        labelLayer.append(t);
        placed.push(box);
        break;
      }
    });
    svg.append(labelLayer);
    drawEdge();
  }

  /* Open notes as outlined marks past a dashed rule. Never filled: fill is
     what published weight looks like on this chart, and none of this is
     published. A note filed under a quarter that has already passed still
     draws, to the left of the rule — that reads as overdue, which it is. */
  function drawEdge() {
    if (!state.edge || !state.edge.notes.length) return;
    const cut = state.data.quarters.length - 0.5;
    if (state.axis.length > state.data.quarters.length) {
      svg.append(elt("line", { class: "today-rule", x1: x(cut), y1: PAD.t,
                               x2: x(cut), y2: H - PAD.b }));
      // Centred over the edge region, not hung off the rule: hung off it, the
      // label ran past the viewBox whenever the edge was one quarter wide.
      const t = elt("text", { class: "axis today-label",
                              x: (x(cut) + W - PAD.r) / 2, y: PAD.t + 9 });
      t.textContent = "coming";
      svg.append(t);
    }
    const lanes = {};
    for (const note of state.edge.notes) {
      const i = state.axis.indexOf(note.expected);
      if (i < 0) continue;                   // undated, or off the axis
      const lane = (lanes[i] = (lanes[i] ?? -1) + 1);
      const mark = elt("circle", { class: "edge-mark", cx: x(i),
                                   cy: PAD.t + 22 + lane * 15, r: 5 });
      mark.addEventListener("pointerenter", (ev) => noteTip(ev, note));
      mark.addEventListener("pointerleave", leave);
      svg.append(mark);
    }
  }

  function noteTip(ev, note) {
    const labels = (note.topics || [])
      .map((s) => (state.data.topics.find((t) => t.slug === s) || {}).label)
      .filter(Boolean);
    tip.innerHTML = `<strong>${esc(note.working_title)}</strong>
      <span>expected ${esc(note.expected)} · not published</span>
      ${labels.length ? `<span>${esc(labels.join(", "))}</span>` : ""}
      ${note.people.length ? `<span>${esc(note.people.join(", "))}</span>` : ""}`;
    tip.hidden = false;
    const box = stage.getBoundingClientRect();
    tip.style.left = Math.min(ev.clientX - box.x + 14,
                              box.width - tip.offsetWidth - 8) + "px";
    tip.style.top = (ev.clientY - box.y - 40) + "px";
  }

  /* ---------- hover ---------- */

  function quarterAt(ev) {
    const pt = new DOMPoint(ev.clientX, ev.clientY)
      .matrixTransform(svg.getScreenCTM().inverse());
    const n = state.axis.length;
    const i = Math.round((pt.x - PAD.l) / (W - PAD.l - PAD.r) * (n - 1));
    // Clamped to the published range: the caller reads series values with it,
    // and there are none past the today rule.
    return Math.max(0, Math.min(state.data.quarters.length - 1, i));
  }

  let cross = null;

  function hover(ev, s, top_, bot_) {
    const q = quarterAt(ev);
    svg.querySelectorAll(".band").forEach((b) =>
      b.classList.toggle("mute", +b.dataset.i !== state.series.indexOf(s)));
    if (!cross) { cross = elt("line", { class: "cross" }); svg.append(cross); }
    cross.setAttribute("x1", x(q)); cross.setAttribute("x2", x(q));
    cross.setAttribute("y1", PAD.t); cross.setAttribute("y2", H - PAD.b);
    const unit = state.measure === "w" ? "weighted" : "words";
    tip.innerHTML = `<strong>${esc(s.label)}</strong>
      <span>${esc(state.data.quarters[q])}</span>
      <span>${s[state.measure][q].toLocaleString("en")} ${unit}
      · ${s.n[q]} piece${s.n[q] === 1 ? "" : "s"}</span>`;
    tip.hidden = false;
    const box = stage.getBoundingClientRect();
    tip.style.left = Math.min(ev.clientX - box.x + 14,
                              box.width - tip.offsetWidth - 8) + "px";
    tip.style.top = (ev.clientY - box.y - 40) + "px";
  }

  function leave() {
    tip.hidden = true;
    if (cross) { cross.remove(); cross = null; }
    svg.querySelectorAll(".band").forEach((b) => b.classList.remove("mute"));
  }

  /* ---------- legend, toggle, drill ---------- */

  function buildLegend() {
    legend.innerHTML = state.series.map((s, i) => `
      <button type="button" class="chip" data-i="${i}">
        <span class="swatch" style="background:${s.slot === "other"
          ? "var(--viz-other)" : `var(--viz-${s.slot})`}"></span>
        ${esc(s.label)}${s.slot === "other" ? ` (${s.folded.length})` : ""}
      </button>`).join("");
    legend.querySelectorAll(".chip").forEach((c) =>
      c.addEventListener("click", () => drill(state.series[+c.dataset.i])));
  }

  function buildPicker() {
    for (const t of state.data.topics) {
      const o = document.createElement("option");
      o.value = t.slug; o.textContent = t.label;
      drillPick.append(o);
    }
    drillPick.addEventListener("change", () => {
      const t = state.data.topics.find((t) => t.slug === drillPick.value);
      if (t) drill(t);
    });
  }

  document.querySelectorAll(".seg button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll(".seg button").forEach((x) =>
        x.classList.toggle("on", x === b));
      state.measure = b.dataset.measure;
      document.getElementById("measure-note").textContent =
        state.measure === "w"
          ? "weighted by type — a Report counts 8, a Comment 1"
          : "summed body-text words — the size of the bet, not the count";
      draw();
      if (state.drilled) drill(state.drilled);
    }));

  function drill(s) {
    state.drilled = s;
    drillPick.value = s.slug || "";
    const vals = s[state.measure];
    const peak = Math.max(...vals, 1);
    const w = 1000, h = 130, pl = 10, pb = 18;
    const pts = vals.map((v, i) => [
      pl + (i / (vals.length - 1)) * (w - 2 * pl),
      h - pb - (v / peak) * (h - pb - 8),
    ]);
    const qs = s.quarters || state.data.quarters;
    const years = qs.map((q, i) => q.endsWith("Q1")
      ? `<text class="axis" x="${Math.max(pts[i][0], 22)}" y="${h - 4}">${q.slice(0, 4)}</text>`
      : "").join("");
    const fill = s.slot === "kw" ? "var(--seal)"
      : s.slot === "other" ? "var(--viz-other)" : `var(--viz-${s.slot})`;
    const stat = s.slot === "kw"
      ? `${s.total} match${s.total === 1 ? "" : "es"}
         (${s.deep ? "anywhere in the text" : "headline & summary"})
         · peak ${peak.toLocaleString("en")}
         ${state.measure === "w" ? "weighted" : "words"} /quarter`
      : `peak ${peak.toLocaleString("en")}
         ${state.measure === "w" ? "weighted" : "words"} /quarter`;
    const catalogLink = s.slot === "kw"
      ? `<a class="button" href="${TIME_URLS.catalog}?q=${encodeURIComponent(s.query)}">open in catalog</a>`
      : s.slug
      ? `<a class="button" href="${TIME_URLS.catalog}?topic=${encodeURIComponent(s.slug)}&filtered=1">open in catalog</a>`
      : `<span class="clear">the ${s.folded.length} topics outside the top 8 — pick one from the list to see it alone</span>`;
    drillBody.innerHTML = `
      <div class="drill-head">
        <strong>${esc(s.label)}</strong>
        <span class="count-mono">${stat}</span>
        ${catalogLink}
      </div>
      <svg viewBox="0 0 ${w} ${h}" class="drill-chart" role="img"
           aria-label="${esc(s.label)} per quarter">
        <path d="${smooth(pts)}L${pts[pts.length - 1][0]},${h - pb}L${pts[0][0]},${h - pb}Z"
              style="fill:${fill}" class="drill-area"></path>
        ${years}
      </svg>`;
  }

  document.getElementById("kw-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const q = document.getElementById("kw").value.trim();
    if (!q) return;
    const deep = document.getElementById("kw-deep").checked ? "1" : "0";
    fetch(`${TIME_URLS.keyword}?q=${encodeURIComponent(q)}&deep=${deep}`)
      .then((r) => r.json())
      .then((d) => {
        if (!d.total) {
          state.drilled = null;
          drillPick.value = "";
          drillBody.innerHTML = `<p class="panel-hint mono">${d.error
            ? esc(d.error)
            : `no matches for “${esc(q)}” ${d.deep ? "anywhere in the text"
               : "in headlines &amp; summaries — try “anywhere in the text”"}`}</p>`;
          return;
        }
        drill({ label: `“${d.query}”`, slot: "kw", query: d.query,
                deep: d.deep, total: d.total, quarters: d.quarters,
                w: d.w, words: d.words, n: d.n });
      });
  });

  document.getElementById("fullscreen").addEventListener("click", () => {
    document.fullscreenElement
      ? document.exitFullscreen()
      : stage.requestFullscreen();
  });
})();
