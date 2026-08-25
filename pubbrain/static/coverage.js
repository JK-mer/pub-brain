/* The coverage view's landscape inset (#55): the full map, dimmed, with the
   queried term dropped on it and its neighbours ringed. Read-only on purpose —
   exploring belongs to the landscape page; this figure answers one question:
   does the term land inside covered ground or in the open. */

(() => {
  "use strict";

  const svg = document.getElementById("cov-scape");
  const W = 1000, H = 520, M = 26;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  function elt(tag, attrs) {
    const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  }

  fetch(COV.data).then((r) => r.json()).then((d) => {
    if (!d.fitted) return;
    const ring = new Set(COV.neighbours);
    const pts = d.points.sort((a, b) => b.w - a.w);
    // The term shares the map's extent so its position means what it says.
    const xs = pts.map((p) => p.x).concat([COV.term.x]);
    const ys = pts.map((p) => p.y).concat([COV.term.y]);
    const lo = [Math.min(...xs), Math.min(...ys)];
    const hi = [Math.max(...xs), Math.max(...ys)];
    const sx = (W - 2 * M) / ((hi[0] - lo[0]) || 1);
    const sy = (H - 2 * M) / ((hi[1] - lo[1]) || 1);
    const px = (x) => M + (x - lo[0]) * sx;
    const py = (y) => M + (y - lo[1]) * sy;

    for (const p of pts) {
      svg.append(elt("circle", {
        class: "pt" + (ring.has(p.id) ? " hit" : " dim"),
        cx: px(p.x), cy: py(p.y), r: 1 + 1.8 * Math.sqrt(p.w),
        fill: p.cluster >= 0 ? `var(--viz-${p.cluster + 1})` : "var(--viz-other)",
      }));
    }

    const tx = px(COV.term.x), ty = py(COV.term.y);
    const mark = elt("g", { class: "term-mark" });
    mark.append(elt("circle", { cx: tx, cy: ty, r: 9 }));
    mark.append(elt("line", { x1: tx - 15, y1: ty, x2: tx + 15, y2: ty }));
    mark.append(elt("line", { x1: tx, y1: ty - 15, x2: tx, y2: ty + 15 }));
    const label = elt("text", {
      class: "term-label", y: ty - 20,
      x: Math.min(Math.max(tx, 40), W - 40),
      "text-anchor": tx > W - 120 ? "end" : tx < 120 ? "start" : "middle",
    });
    label.textContent = COV.label;
    mark.append(label);
    svg.append(mark);
  });
})();
