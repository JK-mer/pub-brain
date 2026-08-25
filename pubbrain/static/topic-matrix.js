/* Who knows what (#49): analyst × topic as a heat table.

   The heat is a sequential ramp of the ink token (one hue, light→dark — the
   dataviz rule for magnitude), so both themes come free. Identity is carried
   by the row and column labels, never by color. */

(() => {
  "use strict";

  const table = document.getElementById("matrix");
  const countEl = document.getElementById("matrix-count");
  const showAll = document.getElementById("show-all");
  const stage = document.getElementById("matrix-root");

  const state = { data: null, sortBy: null };   // sortBy: topic slug or null

  const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function load() {
    fetch(`${MATRIX_URLS.data}?all=${showAll.checked ? 1 : 0}`)
      .then((r) => r.json())
      .then((d) => { state.data = d; state.sortBy = null; render(); });
  }
  showAll.addEventListener("change", load);
  load();

  function render() {
    const d = state.data;
    const people = [...d.people];
    if (state.sortBy)
      people.sort((a, b) => (b.cells[state.sortBy] || 0) -
                            (a.cells[state.sortBy] || 0) ||
                            b.mapped_total - a.mapped_total);
    const max = Math.max(1, ...people.flatMap((p) => Object.values(p.cells)));
    countEl.textContent =
      `${people.length} people × ${d.topics.length} topics`;

    table.innerHTML = `
      <thead><tr>
        <th class="who">
          ${state.sortBy ? `<button type="button" class="clear" id="unsort">↺ by total</button>` : ""}
        </th>
        ${d.topics.map((t) => `
          <th class="topic ${state.sortBy === t.slug ? "sorted" : ""}">
            <button type="button" data-slug="${esc(t.slug)}"
                    title="rank people by ${esc(t.label)}">
              <span>${esc(t.label)}</span></button>
          </th>`).join("")}
      </tr></thead>
      <tbody>
        ${people.map((p) => `
          <tr class="${p.credits_total < 5 ? "thin" : ""}">
            <th class="who">
              <a href="${MATRIX_URLS.person}${p.id}">${esc(p.name)}</a>
              <span class="count-mono"
                    title="${p.credits_total} credits in total, ${p.mapped_total} on mapped publications">${p.credits_total}</span>
              ${p.is_current ? "" : `<span class="former">former/ext</span>`}
            </th>
            ${d.topics.map((t) => cell(p, t, max)).join("")}
          </tr>`).join("")}
      </tbody>`;

    table.querySelectorAll("th.topic button").forEach((b) =>
      b.addEventListener("click", () => {
        state.sortBy = b.dataset.slug === state.sortBy ? null : b.dataset.slug;
        render();
      }));
    const un = document.getElementById("unsort");
    if (un) un.addEventListener("click", () => { state.sortBy = null; render(); });
  }

  function cell(p, t, max) {
    const n = p.cells[t.slug] || 0;
    if (!n) return `<td class="zero"></td>`;
    // sqrt ramp: the difference between 0 and 1 matters more than 12 and 13
    const pct = Math.round(12 + 58 * Math.sqrt(n / max));
    return `<td class="hit" style="background:color-mix(in srgb, var(--ink) ${pct}%, transparent)">
      <a href="${MATRIX_URLS.catalog}?person=${p.id}&topic=${encodeURIComponent(t.slug)}&primary=1&filtered=1"
         class="${pct > 38 ? "deep" : ""}"
         title="${esc(p.name)} — ${n} publication${n === 1 ? "" : "s"} primarily about ${esc(t.label)}">${n}</a>
    </td>`;
  }

  document.getElementById("fullscreen").addEventListener("click", () => {
    document.fullscreenElement
      ? document.exitFullscreen()
      : stage.requestFullscreen();
  });
})();
