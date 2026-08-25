/* The filter-bar facets, shared by the catalog listing and the person page
   (#61) — one implementation, because two copies of a five-line helper is how
   one of them quietly stops matching the other. */

/* Ticking every box and ticking none both mean "all" to the server, so `none`
   clears rather than submitting an impossible empty filter. */
function facetSet(btn, on) {
  btn.closest(".options").querySelectorAll("input[type=checkbox]")
     .forEach((cb) => { cb.checked = on; });
}

/* One panel at a time: they are absolutely positioned and adjacent, so two
   open at once means the second covers the first's options. */
document.querySelectorAll("details.facet").forEach((d) =>
  d.addEventListener("toggle", () => {
    if (d.open) document.querySelectorAll("details.facet[open]")
                        .forEach((o) => { if (o !== d) o.open = false; });
  }));
