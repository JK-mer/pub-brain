/* Pickers for the upcoming notes (#56). Same grammar as the credit picker in
   pub.html — suggest as you type, a real <ul> rather than a <datalist> — but
   wired by data attribute instead of by id, because the page repeats the form
   once per note. */
(function () {
  function suggest(box, endpoint, render, pick) {
    const list = box.parentElement.querySelector('.suggestions');
    const close = () => { list.hidden = true; list.innerHTML = ''; };

    box.addEventListener('input', async function () {
      if (box.value.trim().length < 2) return close();
      const r = await fetch(endpoint + '?q=' + encodeURIComponent(box.value));
      if (!r.ok) return close();
      const items = render(await r.json());
      list.innerHTML = '';
      for (const item of items) {
        const li = document.createElement('li');
        li.textContent = item.label;
        if (item.hint) {
          const em = document.createElement('span');
          em.className = 'byline';
          em.textContent = ' ' + item.hint;
          li.appendChild(em);
        }
        /* mousedown, not click: blur would otherwise close the list first. */
        li.addEventListener('mousedown', () => { pick(item.value); close(); });
        list.appendChild(li);
      }
      list.hidden = items.length === 0;
    });
    box.addEventListener('blur', () => setTimeout(close, 150));
    box.addEventListener('keydown', e => {
      if (e.key === 'Escape') close();
      /* Enter in a typeahead means "pick", never "submit the whole form". */
      if (e.key === 'Enter' && !list.hidden) e.preventDefault();
    });
  }

  document.querySelectorAll('.people-picker').forEach(function (picker) {
    const box = picker.querySelector('[data-person-search]');
    const chips = picker.querySelector('[data-chips]');
    const has = id => !!chips.querySelector(`input[value="${id}"]`);

    suggest(box, picker.dataset.endpoint,
      data => data.people.map(p => ({
        label: p.name, value: p,
        hint: p.title ? p.title + (p.is_current ? '' : ' · former') : '',
      })),
      function (person) {
        box.value = '';
        if (has(person.id)) return;            /* twice is once */
        const li = document.createElement('li');
        li.textContent = person.name;
        const field = document.createElement('input');
        field.type = 'hidden';
        field.name = 'person_id';
        field.value = person.id;
        const drop = document.createElement('button');
        drop.type = 'button';
        drop.className = 'drop';
        drop.textContent = '×';
        drop.addEventListener('click', () => li.remove());
        li.append(field, drop);
        chips.appendChild(li);
      });
  });

  document.querySelectorAll('.link-form').forEach(function (form) {
    const box = form.querySelector('[data-pub-search]');
    const id = form.querySelector('[data-pub-id]');
    box.addEventListener('input', () => { id.value = ''; });  /* retyping unpicks */
    suggest(box, form.dataset.endpoint,
      data => data.publications.map(p => ({
        label: p.title, value: p, hint: p.pub_type + ' · ' + (p.date || 'no date'),
      })),
      function (pub) { box.value = pub.title; id.value = pub.id; });
  });

  /* A chip rendered server-side needs the same remove button behaviour. */
  document.querySelectorAll('[data-drop]').forEach(function (drop) {
    drop.addEventListener('click', () => drop.parentElement.remove());
  });
})();
