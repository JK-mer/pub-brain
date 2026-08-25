/* The record page's hand-edits (#60): every control is a button, its fields
   live in a <dialog>. Native dialog, no library — ESC closes it, the forms
   inside stay ordinary POSTs, and none of the behaviour they carry changes.

   Wired by data attribute rather than by id, because the same picker markup
   now appears in more than one dialog. */
(() => {
  "use strict";

  /* ---------- dialogs ---------- */

  const open = (id) => {
    const dlg = document.getElementById(id);
    if (!dlg) return;
    dlg.showModal();
    // Focus the first real field, not the cancel button.
    const first = dlg.querySelector("input, textarea, select");
    if (first) first.focus();
  };

  document.querySelectorAll("[data-open]").forEach((b) =>
    b.addEventListener("click", () => open(b.dataset.open)));
  document.querySelectorAll("[data-close]").forEach((b) =>
    b.addEventListener("click", () => b.closest("dialog").close()));

  // Clicking the backdrop closes: the click lands on the dialog itself, so
  // anything inside the box is not it.
  document.querySelectorAll("dialog.pop").forEach((dlg) =>
    dlg.addEventListener("click", (ev) => {
      if (ev.target === dlg) dlg.close();
    }));

  /* Destructive actions confirm. On a form the guard belongs to submit; on a
     button inside a form it belongs to the click, or the other buttons in the
     same form would inherit it. */
  document.querySelectorAll("form[data-confirm]").forEach((form) =>
    form.addEventListener("submit", (ev) => {
      if (!confirm(form.dataset.confirm)) ev.preventDefault();
    }));
  document.querySelectorAll("button[data-confirm]").forEach((button) =>
    button.addEventListener("click", (ev) => {
      if (!confirm(button.dataset.confirm)) ev.preventDefault();
    }));

  /* A rejected save comes back as a redirect with ?error=…, so the dialog it
     came from is reopened rather than leaving the message with nothing to act
     on. */
  const failed = new URLSearchParams(location.search).get("open");
  if (failed) open(failed);

  /* ---------- typeaheads ---------- */

  function suggest(box, render, pick) {
    const list = box.parentElement.querySelector(".suggestions");
    const close = () => { list.hidden = true; list.innerHTML = ""; };

    box.addEventListener("input", async function () {
      if (box.value.trim().length < 2) return close();
      const r = await fetch(box.dataset.endpoint + "?q=" +
                            encodeURIComponent(box.value));
      if (!r.ok) return close();
      const items = render(await r.json());
      list.innerHTML = "";
      for (const item of items) {
        const li = document.createElement("li");
        li.textContent = item.label;
        if (item.hint) {
          const em = document.createElement("span");
          em.className = "byline";
          em.textContent = " " + item.hint;
          li.appendChild(em);
        }
        /* mousedown, not click: blur would otherwise close the list first. */
        li.addEventListener("mousedown", () => { pick(item.value); close(); });
        list.appendChild(li);
      }
      list.hidden = items.length === 0;
    });
    box.addEventListener("blur", () => setTimeout(close, 150));
    box.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { close(); e.stopPropagation(); }
      if (e.key === "Enter" && !list.hidden) e.preventDefault();
    });
  }

  /* Credit (#40). The hidden field decides: set means an existing person,
     empty means create one — so a typo mints a second person, and picking
     from the list is what prevents it. */
  document.querySelectorAll("[data-person-search]").forEach((box) => {
    const id = box.closest("form").querySelector("[data-person-id]");
    box.addEventListener("input", () => { id.value = ""; });  /* retyping unpicks */
    suggest(box,
      (d) => d.people.map((p) => ({
        label: p.name, value: p,
        hint: p.title ? p.title + (p.is_current ? "" : " · former") : "",
      })),
      (person) => { box.value = person.name; id.value = person.id; });
  });

  /* Chapter attach (#36) — attaching used to mean knowing an id by heart. */
  document.querySelectorAll("[data-pub-search]").forEach((box) => {
    const form = box.closest("form");
    const id = form.querySelector("[data-pub-id]");
    box.addEventListener("input", () => {
      id.value = "";
      box.setCustomValidity("");
    });
    suggest(box,
      (d) => d.publications.map((p) => ({
        label: p.title, value: p,
        hint: `${p.pub_type} · ${p.date}` +
              (p.chapters ? ` · ${p.chapters} chapters` : ""),
      })),
      (pub) => {
        box.value = pub.title;
        id.value = pub.id;
        box.setCustomValidity("");
      });
    /* Nothing picked means nothing to attach — say so on the box rather than
       400 on submit. */
    form.addEventListener("submit", (ev) => {
      if (!id.value) {
        ev.preventDefault();
        box.setCustomValidity("pick a report from the suggestions");
        box.reportValidity();
      }
    });
  });

  /* ---------- pasting body text (#31) ---------- */

  /* A browser puts both text/plain and text/html on the clipboard, and a
     textarea only ever receives the plain one — which is why pasting from the
     site arrives with headings and links already gone. Take the HTML flavour
     and convert it server-side, so there is one set of rules rather than a
     second implementation here that drifts from text.py. */
  const body = document.getElementById("manual-body");
  const note = document.getElementById("paste-note");
  if (body) body.addEventListener("paste", async function (e) {
    const html = e.clipboardData && e.clipboardData.getData("text/html");
    if (!html) return;                          // plain text: leave it alone
    e.preventDefault();
    note.textContent = "converting pasted HTML…";
    const at = body.selectionStart, to = body.selectionEnd;
    try {
      const r = await fetch(body.dataset.convert, {method: "POST", body: html});
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || r.statusText);
      body.value = body.value.slice(0, at) + data.text + body.value.slice(to);
      body.selectionStart = body.selectionEnd = at + data.text.length;
      note.textContent = data.word_count +
        " words converted — headings, lists and links kept. Edit before saving.";
    } catch (err) {
      /* Never lose the paste: fall back to the plain flavour. */
      const plain = e.clipboardData.getData("text/plain");
      body.value = body.value.slice(0, at) + plain + body.value.slice(to);
      note.textContent = "HTML conversion failed (" + err.message +
        ") — pasted as plain text instead.";
    }
  });
})();
