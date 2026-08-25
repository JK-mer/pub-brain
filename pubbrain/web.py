"""The local web workbench (#19): browse, search, read, review.

Views are thin — every list or lookup goes through `queries`, the same layer
the CLI uses. Two blueprints, `catalog` and `review`, are the seam future
modules (digest, quiz, cheat sheets — #8, #9) plug into as further blueprints.

Single user, no auth. Localhost by default; `--host 0.0.0.0` opens it to the
trusted home LAN (#51) — anything beyond that is #48's tunnel, not this. The
port must stay outside 8780–8799: that range is deliberately exposed to
LLM-driven browsers on this machine.
"""

import re

import requests
from flask import (Blueprint, Flask, abort, current_app, g, redirect,
                   render_template, request, url_for)

from . import (ask, blurb, collect, db, embed, enrich, llm, queries, remote,
               sections, text, topics)

HOST = "127.0.0.1"
PORT = 8901
BROWSER_EXPOSED_PORTS = range(8780, 8800)

catalog = Blueprint("catalog", __name__)
review = Blueprint("review", __name__)
learning = Blueprint("learning", __name__)   # glossary now; digest/quiz/recaps later
insights = Blueprint("insights", __name__, url_prefix="/insights")  # #49
# Its own blueprint, not a corner of `catalog`: notes are not publications and
# nothing else may reach them (#56).
upcoming = Blueprint("upcoming", __name__, url_prefix="/upcoming")


def get_conn():
    if "conn" not in g:
        g.conn = db.connect(current_app.config.get("DB_PATH"))
    return g.conn


@catalog.route("/")
def list_view():
    conn = get_conn()
    q = (request.args.get("q") or "").strip()
    # Repeated params, so a selection survives paging and searching (#26).
    pub_type = [t for t in request.args.getlist("type") if t]
    year = [y for y in request.args.getlist("year") if y]
    # `filtered=1` is the form's own marker. Without it, ticking nothing and
    # never touching the form look identical, and the default could not be
    # overridden with "actually, all types" (#30).
    submitted = request.args.get("filtered") == "1"
    default_types = db.get_setting(conn, db.DEFAULT_TYPES_KEY, []) or []
    using_default = bool(default_types) and not submitted and not pub_type
    if using_default:
        pub_type = list(default_types)
    person_id = request.args.get("person", type=int)
    starred_only = request.args.get("shortlisted") == "1"
    # The glossary's way into the catalog (#22). `primary=1` narrows to the
    # topic the piece is *about* rather than one it touches.
    topic = (request.args.get("topic") or "").strip() or None
    topic_primary = request.args.get("primary") == "1"
    topic_label = topics.labels().get(topic) if topic else None
    # The recurring MERICS formats (#32) — a facet like type and year.
    series = [s for s in request.args.getlist("series") if s]
    page = max(request.args.get("page", default=1, type=int), 1)
    types, years, series_options = queries.filter_options(conn)
    shortlisted = db.shortlisted_ids(conn)
    # "2024–2026" only reads as a range if it actually is one (#26).
    picked = sorted(int(y) for y in year if y.isdigit())
    contiguous_years = bool(picked) and picked == list(
        range(picked[0], picked[-1] + 1))

    if q:
        try:
            hits, notes = queries.hybrid_find(conn, q, limit=30)
        except embed.OllamaUnreachable:
            hits, notes = queries.hybrid_find(conn, q, limit=30, with_vectors=False)
            notes.append("ollama is not running, so paraphrase matching is off")
        if pub_type:   # filters still apply to search results
            hits = [h for h in hits if h["publication"]["pub_type"] in pub_type]
        if year:
            hits = [h for h in hits
                    if (h["publication"]["date_published"] or "")[:4] in year]
        if series:
            hits = [h for h in hits if h["publication"]["series"] in series]
        if starred_only:
            hits = [h for h in hits if h["shortlisted"]]
        if topic:
            keep = db.publications_with_topic(conn, topic, topic_primary)
            hits = [h for h in hits if h["publication"]["id"] in keep]
        people = queries.people_for(conn, [h["publication"]["id"] for h in hits])
        return render_template("list.html", hits=hits, notes=notes, people=people,
                               plain={},
                               q=q, pub_type=pub_type, year=year, series=series,
                               topic=topic, topic_label=topic_label,
                               topic_primary=topic_primary,
                               person_id=person_id, types=types, years=years,
                               series_options=series_options,
                               shortlisted=shortlisted, starred_only=starred_only,
                               contiguous_years=contiguous_years,
                               using_default=using_default,
                               rows=None, total=len(hits), page=1, pages=1)

    offset = (page - 1) * queries.PAGE_SIZE
    rows, total, people = queries.list_publications(
        conn, pub_type=pub_type, year=year, person_id=person_id,
        shortlisted=starred_only, topic=topic, topic_primary=topic_primary,
        series=series, offset=offset)
    pages = max(-(-total // queries.PAGE_SIZE), 1)
    # Opt-in: the blurb is three times the length of a one-liner, so a listing
    # that always showed it would be a different page (#38).
    plain = (blurb.blurbs_for(conn, [r["id"] for r in rows])
             if request.args.get("plain") == "1" else {})
    return render_template("list.html", rows=rows, total=total, people=people,
                           plain=plain,
                           q=q, pub_type=pub_type, year=year, person_id=person_id,
                           topic=topic, topic_label=topic_label,
                           topic_primary=topic_primary,
                           series=series, series_options=series_options,
                           types=types, years=years, page=page, pages=pages,
                           shortlisted=shortlisted, starred_only=starred_only,
                           contiguous_years=contiguous_years,
                           using_default=using_default,
                           hits=None, notes=[])


@catalog.route("/pub/<int:pub_id>")
def pub_view(pub_id):
    conn = get_conn()
    detail = queries.publication_detail(conn, pub_id)
    if not detail:
        abort(404)
    starred = conn.execute("SELECT * FROM shortlist WHERE publication_id = ?",
                           (pub_id,)).fetchone()
    return render_template("pub.html", shortlist=starred,
                           text_source=db.text_source(conn, pub_id),
                           member_of=db.collections_for(conn, pub_id),
                           model_choices=model_choices(),
                           plain=blurb.for_publication(conn, pub_id),
                           all_collections=db.collections(conn), **detail)


@catalog.route("/shortlist")
def shortlist_view():
    """The publications that mattered — the one signal in this catalog that is
    the owner's own rather than scraped or generated (#25)."""
    return render_template("shortlist.html", rows=db.shortlist_rows(get_conn()))


@catalog.route("/pub/<int:pub_id>/shortlist", methods=["POST"])
def shortlist_toggle(pub_id):
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM publications WHERE id = ?",
                        (pub_id,)).fetchone():
        abort(404)
    if request.form.get("action") == "remove":
        db.clear_shortlist(conn, pub_id)
    else:
        db.set_shortlist(conn, pub_id, request.form.get("note", ""))
    conn.commit()
    return redirect(request.form.get("back")
                    or url_for("catalog.pub_view", pub_id=pub_id))


@catalog.route("/pub/<int:pub_id>/text", methods=["POST"])
def save_text(pub_id):
    """Store hand-entered body text (#31).

    Paywalled Briefs, PDF-only reports and work published on a partner's site
    can never be scraped. Saved as `source = 'manual'`, which `extract-text`
    then refuses to overwrite.

    Re-indexes and re-sections immediately: text that search cannot reach looks
    to the user exactly like text that was never saved. Embedding is best
    effort — a missing vector degrades to keyword ranking and `status` reports
    it, where a stale one would be a confident wrong match (#24).
    """
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM publications WHERE id = ?",
                        (pub_id,)).fetchone():
        abort(404)
    body = (request.form.get("body") or "").strip()
    if request.form.get("action") == "remove":
        conn.execute("DELETE FROM publication_text WHERE publication_id = ? "
                     "AND source = 'manual'", (pub_id,))
        conn.execute("DELETE FROM publication_sections WHERE publication_id = ?",
                     (pub_id,))
    elif body:
        db.upsert_text(conn, pub_id, body, len(body.split()), source="manual")
        # Section exactly as `extract-sections` does. Anything less makes a
        # pasted Brief a single argument where a scraped one is ten separate
        # stories, and `independent` is what the section layer keys on (#16).
        rec = conn.execute("SELECT title, pub_type FROM publications WHERE id = ?",
                           (pub_id,)).fetchone()
        found = sections.split(body)
        independent = sections.has_independent_topics(
            rec["pub_type"], rec["title"], found)
        for sec in found:
            sec["is_boilerplate"] = sections.is_boilerplate(sec["heading"])
            sec["independent"] = independent and not sec["is_boilerplate"]
        db.replace_sections(conn, pub_id, found)
    db.reindex_one(conn, pub_id)
    db.rebuild_section_fts(conn)
    conn.execute("DELETE FROM embeddings WHERE publication_id = ?", (pub_id,))
    conn.commit()
    return redirect(url_for("catalog.pub_view", pub_id=pub_id))


@catalog.route("/convert-html", methods=["POST"])
def convert_html():
    """Clipboard HTML -> the same Markdown the scraper produces (#31).

    Converted server-side so there is one implementation of the rules, not a
    second one in JavaScript that drifts from `text.py`.
    """
    try:
        out = text.from_fragment(request.get_data(as_text=True))
    except text.NoBodyText as exc:
        return {"error": str(exc)}, 400
    return {"text": out["text"], "word_count": out["word_count"]}


@catalog.route("/collections")
def collections_view():
    return render_template("collections.html",
                           collections=db.collections(get_conn()))


@catalog.route("/collection/<slug>")
def collection_view(slug):
    conn = get_conn()
    coll = conn.execute("SELECT * FROM collections WHERE slug = ?", (slug,)).fetchone()
    if coll is None:
        abort(404)
    return render_template("collection.html", collection=coll,
                           members=db.collection_members(conn, slug))


def _slugify(name):
    """A project the owner invents has no URL to take a slug from."""
    out = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return out[:60] or None


@catalog.route("/collections", methods=["POST"])
def create_collection():
    """Start a project from the workbench (#37).

    The URL is optional and that is the point: a project the owner invents has
    no merics.org page, so `collect.from_page` has nothing to read for it and
    it is manual-only by construction. The page says so rather than leaving him
    waiting for it to fill itself.
    """
    conn = get_conn()
    name = (request.form.get("name") or "").strip()
    slug = _slugify(request.form.get("slug") or name)
    if not name or not slug:
        return redirect(url_for("catalog.collections_view",
                                error="a project needs a name"))
    if conn.execute("SELECT 1 FROM collections WHERE slug = ?", (slug,)).fetchone():
        return redirect(url_for("catalog.collections_view",
                                error=f"{slug} already exists"))
    db.upsert_collection(conn, slug, name,
                         (request.form.get("url") or "").strip() or None)
    conn.commit()
    return redirect(url_for("catalog.collection_view", slug=slug))


@catalog.route("/collection/<slug>/edit", methods=["POST"])
def edit_collection(slug):
    """Rename or delete. Renaming changes `name` only — `slug` is the identity
    `publication_collections` stores (#37)."""
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM collections WHERE slug = ?", (slug,)).fetchone():
        abort(404)
    if request.form.get("action") == "delete":
        n = db.delete_collection(conn, slug)
        conn.commit()
        return redirect(url_for("catalog.collections_view",
                                removed=f"{slug} and {n} membership"
                                        f"{'' if n == 1 else 's'}"))
    name = (request.form.get("name") or "").strip()
    if name:
        db.rename_collection(conn, slug, name)
        conn.commit()
    return redirect(url_for("catalog.collection_view", slug=slug))


@catalog.route("/pub/<int:pub_id>/collection", methods=["POST"])
def collection_toggle(pub_id):
    """Attach or detach by hand. A manual membership outranks detection and is
    never undone by it (#32).

    A name given instead of a slug starts a project here (#37), so beginning one
    does not mean leaving the record you are looking at.
    """
    conn = get_conn()
    slug = request.form.get("slug", "")
    new_name = (request.form.get("new_project") or "").strip()
    if new_name and not slug:
        slug = _slugify(new_name)
        if not slug:
            abort(400)
        # A name that slugifies onto an existing project attaches to it. It must
        # NOT re-register: `upsert_collection` sets url and blurb from its
        # arguments, so re-registering with neither blanks both, and a typo
        # onto "ETNC" would silently cost that project its page URL.
        if not conn.execute("SELECT 1 FROM collections WHERE slug = ?",
                            (slug,)).fetchone():
            db.upsert_collection(conn, slug, new_name)
    if not conn.execute("SELECT 1 FROM collections WHERE slug = ?", (slug,)).fetchone():
        abort(404)
    if request.form.get("action") == "remove":
        db.remove_from_collection(conn, pub_id, slug)
    else:
        db.add_to_collection(conn, pub_id, slug, source="manual")
    conn.commit()
    return redirect(url_for("catalog.pub_view", pub_id=pub_id))


@catalog.route("/pub/<int:pub_id>/chapter-of", methods=["POST"])
def attach_chapter(pub_id):
    """Hang this publication off a parent report (#36)."""
    conn = get_conn()
    parent = request.form.get("parent_id", type=int)
    position = request.form.get("position", type=int)
    if not parent:
        abort(400)
    try:
        db.attach_chapter(conn, pub_id, parent, position)
    except ValueError as exc:
        conn.rollback()
        return redirect(url_for("catalog.pub_view", pub_id=pub_id,
                                error=str(exc), open="dlg-chapter"))
    conn.commit()
    return redirect(url_for("catalog.pub_view", pub_id=pub_id))


@catalog.route("/pub/<int:pub_id>/detach-chapter", methods=["POST"])
def detach_chapter(pub_id):
    conn = get_conn()
    db.detach_chapter(conn, pub_id)
    conn.commit()
    return redirect(url_for("catalog.pub_view", pub_id=pub_id))


@catalog.route("/pub/<int:pub_id>/summary", methods=["POST"])
def write_summary(pub_id):
    """Write the summary by hand (#46).

    The one field the tool rests on was the one field only a model could
    produce. Routed through `enrich.write_summary` so it goes in as an ordinary
    enrichment row and picks up the two consequences a promotion has: the
    one-liner vector is rebuilt, and the topics go back on `map-topics`'
    worklist because they were read off the summary that just changed.
    """
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM publications WHERE id = ?",
                        (pub_id,)).fetchone():
        abort(404)
    _, problems = enrich.write_summary(
        conn, pub_id, request.form.get("one_liner"), request.form.get("short"))
    if problems:
        conn.rollback()
        # `open` reopens the dialog it came from (#60) — the message is useless
        # beside a closed popup.
        return redirect(url_for("catalog.pub_view", pub_id=pub_id,
                                error="; ".join(problems), open="dlg-write"))
    conn.commit()
    return redirect(url_for("catalog.pub_view", pub_id=pub_id,
                            note="Saved. Topics are re-derived from the summary, "
                                 "so run map-topics when you next have quota."))


@catalog.route("/pub/<int:pub_id>/credit", methods=["POST"])
def credit(pub_id):
    """Add or remove a credit by hand (#40).

    The byline is often in the prose, the PDF cover or the title and in no
    field the parser can read, so this is the only path for 236 records that
    credit nobody. Stored as `source = 'manual'`, which re-parse leaves alone.

    Picking an existing person is the normal case; `name` creates one only when
    nothing was picked, so submitting the form twice cannot mint a duplicate.
    """
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM publications WHERE id = ?",
                        (pub_id,)).fetchone():
        abort(404)
    role = request.form.get("role", "author")
    person_id = request.form.get("person_id", type=int)
    if request.form.get("action") == "remove":
        db.uncredit_person(conn, pub_id, person_id, role)
    else:
        name = (request.form.get("name") or "").strip()
        if not person_id and name:
            person_id = db.add_person(conn, name)
        if not person_id:
            abort(400)
        try:
            db.credit_person(conn, pub_id, person_id, role)
        except ValueError:
            conn.rollback()
            abort(400)
    conn.commit()
    return redirect(url_for("catalog.pub_view", pub_id=pub_id))


@catalog.route("/people/search")
def people_search():
    """Typeahead for the credit form — id and name only."""
    term = request.args.get("q", "")
    if len(term.strip()) < 2:
        return {"people": []}
    return {"people": [
        {"id": r["id"], "name": r["name"], "title": r["title"],
         "affiliation": r["affiliation"], "is_current": r["is_current"]}
        for r in db.find_people(get_conn(), term)]}


@catalog.route("/pubs/search")
def pubs_search():
    """Typeahead for the chapter-attach picker (#36) — attaching used to mean
    knowing a publication id by heart."""
    term = request.args.get("q", "")
    if len(term.strip()) < 2:
        return {"publications": []}
    return {"publications": [
        {"id": r["id"], "title": r["title"], "pub_type": r["pub_type"],
         "date": (r["date_published"] or "")[:7], "chapters": r["chapters"]}
        for r in db.find_parents(get_conn(), term)]}


@catalog.route("/people")
def people_view():
    conn = get_conn()
    show_all = request.args.get("all") == "1"
    people = queries.list_people(conn, current_only=not show_all)
    total = len(queries.list_people(conn, current_only=False))
    return render_template("people.html", people=people, total=total,
                           show_all=show_all, hidden=total - len(people))


@catalog.route("/person/<int:person_id>")
def person_view(person_id):
    """One person's credits, filterable by keyword and type (#61)."""
    page = queries.person_page(
        get_conn(), person_id, q=request.args.get("q"),
        pub_type=[t for t in request.args.getlist("type") if t])
    if not page:
        abort(404)
    return render_template("person.html", **page)


@review.route("/flags")
def flags_view():
    conn = get_conn()
    return render_template("flags.html", rows=queries.flagged(conn),
                           stats=queries.review_stats(conn))


@review.route("/pub/<int:pub_id>/flag", methods=["POST"])
def flag(pub_id):
    """Trust is the default; this records the exception. Posting again
    updates the note; 'clear' removes the flag."""
    conn = get_conn()
    # A verdict names the summary row it reviewed, not the publication (#18) —
    # with candidates in play, "the flag on publication 42" is ambiguous.
    subject = db.primary_enrichment_id(conn, pub_id)
    if subject is None:
        abort(404)
    if request.form.get("action") == "clear":
        conn.execute("DELETE FROM reviews WHERE scope = 'enrichment' AND subject_id = ?",
                     (subject,))
    else:
        db.upsert_review(conn, "enrichment", subject, "flagged",
                         request.form.get("note", "").strip())
    conn.commit()
    return redirect(url_for("catalog.pub_view", pub_id=pub_id))


@review.route("/backlog")
def backlog_view():
    """Everything unfinished, with links (#33). A work queue, not a dashboard:
    the owner can explain almost any of these given a URL, so every row carries
    one."""
    show_parked = request.args.get("parked") == "1"
    return render_template("backlog.html", show_parked=show_parked,
                           **queries.backlog(get_conn(), show_parked=show_parked))


@review.route("/pub/<int:pub_id>/park", methods=["POST"])
def park(pub_id):
    """Take a record off the backlog without changing it (#33) — for records
    that are already correct, like a graphics-only piece with 34 words."""
    conn = get_conn()
    action = request.form.get("action")
    if action == "unpark":
        db.unpark_backlog(conn, pub_id)
    else:
        db.park_backlog(conn, pub_id, request.form.get("note", ""),
                        verdict="todo" if action == "todo" else "parked")
    conn.commit()
    return redirect(request.referrer or url_for("review.backlog_view"))


@review.route("/url-disposition", methods=["POST"])
def url_disposition():
    """The owner's call on a root-level URL (#10), made in the workbench rather
    than relayed through chat where it would be recorded nowhere."""
    conn = get_conn()
    db.set_url_disposition(conn, request.form["url"],
                           request.form.get("disposition", "todo"),
                           request.form.get("note"))
    conn.commit()
    return redirect(request.referrer or url_for("review.backlog_view"))


@review.route("/url-dispositions", methods=["POST"])
def url_dispositions():
    """Confirm many root-level URLs at once (#10). 170 of them one button at a
    time is not a workflow — the proposals are pre-ticked and the owner
    corrects the exceptions."""
    conn = get_conn()
    disposition = request.form.get("disposition", "exclude")
    urls = request.form.getlist("url")
    note = (request.form.get("bulk_note") or "").strip() or None
    for url in urls:
        db.set_url_disposition(conn, url, disposition, note)
    conn.commit()
    return redirect(url_for("review.backlog_view", done=len(urls)))


@review.route("/url-row", methods=["POST"])
def url_row():
    """Act on one URL from its own row (#33): exclude it, or hand it over with
    a note. The only two verbs the owner uses.

    The rows live inside the bulk form — nested forms are invalid HTML — so
    each note input is named for its own URL and the button carries which row
    was pressed.
    """
    conn = get_conn()
    url = request.form.get("exclude") or request.form.get("claude")
    if not url:
        abort(400)
    disposition = "exclude" if request.form.get("exclude") else "todo"
    db.set_url_disposition(conn, url, disposition,
                           request.form.get(f"note__{url}"))
    conn.commit()
    return redirect((request.referrer or url_for("review.backlog_view")) + "#homeless")


@review.route("/settings")
def settings_view():
    """What is actually in effect. Read-only for now (#26): provider and model
    are per-call arguments today and there is nowhere to persist a choice, so
    showing a control that silently forgets would be worse than showing none.
    """
    providers = [{
        "name": name,
        "default_model": cfg["default_model"],
        "base_url": cfg["base_url"],
        "is_default": name == llm.DEFAULT_PROVIDER,
        "key_present": llm.has_api_key(name),
        # The shortlist the picker offers, and how many the provider carries
        # in total — that gap once hid a configured model entirely (#39), and
        # showing both is what stops the shortlist reading as the whole truth.
        "models": llm.offered(name),
        "catalogue": llm.catalogue_size(name),
    } for name, cfg in sorted(llm.PROVIDERS.items())]
    conn = get_conn()
    types, _years, _series = queries.filter_options(conn)
    return render_template("settings.html", providers=providers,
                           embed_model=embed.MODEL,
                           types=types,
                           default_types=db.get_setting(
                               conn, db.DEFAULT_TYPES_KEY, []) or [],
                           saved=request.args.get("saved") == "1",
                           status=queries.status_report(conn))


@review.route("/settings/default-view", methods=["POST"])
def save_default_view():
    """Persist which types the catalog opens with (#30). Saving none clears
    the default rather than storing an empty filter that would hide the whole
    catalog."""
    conn = get_conn()
    db.set_setting(conn, db.DEFAULT_TYPES_KEY,
                   [t for t in request.form.getlist("type") if t])
    conn.commit()
    return redirect(url_for("review.settings_view", saved=1))


@review.route("/pub/<int:pub_id>/promote/<int:enrichment_id>", methods=["POST"])
def promote(pub_id, enrichment_id):
    """Make a candidate summary the one the tool actually uses (#18)."""
    conn = get_conn()
    try:
        enrich.promote(conn, pub_id, enrichment_id)
    except ValueError:
        conn.rollback()
        abort(404)
    conn.commit()
    return redirect(url_for("catalog.pub_view", pub_id=pub_id))


@review.route("/pub/<int:pub_id>/dismiss/<int:enrichment_id>", methods=["POST"])
def dismiss(pub_id, enrichment_id):
    """Discard a candidate in favour of the summary already in place (#18)."""
    conn = get_conn()
    try:
        if db.dismiss_enrichment(conn, enrichment_id) != pub_id:
            raise ValueError("summary belongs to another publication")
    except ValueError:
        conn.rollback()
        abort(404)
    conn.commit()
    return redirect(url_for("catalog.pub_view", pub_id=pub_id))


def _picked_model(form):
    """(provider, model) from the form's `provider:model` value (#39).

    One control rather than two, because the pair has to agree: a provider
    picker plus a model picker lets you ask one provider for a model only
    another has, and the failure arrives as an opaque upstream 400.
    """
    raw = (form.get("model") or "").strip()
    if ":" not in raw:
        return form.get("provider") or llm.DEFAULT_PROVIDER, None
    provider, model = raw.split(":", 1)
    if provider not in llm.PROVIDERS:
        return llm.DEFAULT_PROVIDER, None
    return provider, model or None


def model_choices():
    """{provider: [model, …]} for the regenerate picker — the shortlist, not
    the provider's whole catalogue (see `llm.PROVIDERS`)."""
    return {name: llm.offered(name) for name in sorted(llm.PROVIDERS)}


@review.route("/pub/<int:pub_id>/regenerate", methods=["POST"])
def regenerate(pub_id):
    """Rewrite a summary that was flagged as misleading (#24).

    Synchronous on purpose: one call takes a few seconds and a job queue for a
    single-user tool is machinery with nothing to manage.
    """
    conn = get_conn()
    provider, model = _picked_model(request.form)
    try:
        result = enrich.regenerate(conn, pub_id, provider=provider, model=model)
    except (ValueError, enrich.Invalid, llm.NoApiKey) as exc:
        # Nothing was written: a failed rewrite leaves the old summary and its
        # flag in place, which is the recoverable state.
        conn.rollback()
        return render_template("regenerated.html", pub_id=pub_id, error=str(exc),
                               detail=queries.publication_detail(conn, pub_id)), 502
    conn.commit()
    return render_template("regenerated.html", pub_id=pub_id, error=None,
                           detail=queries.publication_detail(conn, pub_id),
                           **result)


@learning.route("/glossary")
def glossary_view():
    """The vocabulary, and the way into the catalog through it (#22).

    Two counts per topic, because one would mislead: `primary` is what the
    piece is about, the total also counts topics it merely touches. Both are
    over *summarized* publications — the 212 podcasts and the paywalled items
    have no summary to map, so no topic reaches them.
    """
    conn = get_conn()
    return render_template("glossary.html", clusters=topics.load(),
                           counts=db.topic_counts(conn),
                           primary=db.topic_counts(conn, primary_only=True),
                           mapped=conn.execute(
                               "SELECT COUNT(DISTINCT publication_id) "
                               "FROM publication_topics").fetchone()[0],
                           summarized=conn.execute(
                               "SELECT COUNT(*) FROM primary_enrichment"
                           ).fetchone()[0],
                           catalog=conn.execute(
                               "SELECT COUNT(*) FROM publications").fetchone()[0])


@learning.route("/ask", methods=["GET"])
def ask_view():
    """Answer a question from retrieved excerpts, with citations (#24).

    An answer is only as good as the search behind it, so the retrieved records
    are always shown — including the ones the answer ignored.
    """
    question = (request.args.get("q") or "").strip()
    if not question:
        return render_template("ask.html", result=None, question="")
    conn = get_conn()
    try:
        result = ask.answer(conn, question,
                            provider=request.args.get("provider")
                            or llm.DEFAULT_PROVIDER)
    except embed.OllamaUnreachable:
        result = ask.answer(conn, question, with_vectors=False)
        result["notes"].append("ollama is not running, so retrieval was keyword "
                               "only — the answer rests on a weaker search")
    except (llm.NoApiKey, requests.RequestException) as exc:
        return render_template("ask.html", result=None, question=question,
                               error=str(exc)), 502
    return render_template("ask.html", result=result, question=question)


def _quarter_choices(conn, keep=None):
    """The next eight quarters, plus whichever one a note already carries.

    Without `keep`, editing a note whose quarter has passed would silently
    re-file it: the stored value is not in the list, so the select submits
    something else.
    """
    year, quarter = int(db.now()[:4]), (int(db.now()[5:7]) + 2) // 3
    choices = []
    for _ in range(8):
        choices.append(f"{year}-Q{quarter}")
        year, quarter = (year + 1, 1) if quarter == 4 else (year, quarter + 1)
    if keep and keep not in choices:
        choices.append(keep)
    return sorted(choices)


@upcoming.route("/")
def upcoming_view():
    """The notebook of what is coming (#56).

    Hand-entered, hand-linked, and deliberately outside every other query in
    this workbench: nothing here is a publication, and nothing here reaches
    search, the FTS index, an LLM or the MCP server.
    """
    conn = get_conn()
    board = queries.upcoming_notes(conn)
    editing = request.args.get("edit", type=int)
    return render_template(
        "upcoming.html", clusters=topics.load(), max_topics=db.UPCOMING_MAX_TOPICS,
        quarters=_quarter_choices(conn), editing=editing,
        quarters_for=lambda n: _quarter_choices(conn, n["expected"]), **board)


def _note_form(form):
    return {
        "working_title": form.get("working_title"),
        "note": form.get("note"),
        "expected": form.get("expected"),
        # Three ranked selects rather than a multi-select: position is entry
        # order, and a blank slot is how a topic is removed.
        "topic_slugs": form.getlist("topic"),
        "person_ids": form.getlist("person_id"),
    }


@upcoming.route("/new", methods=["POST"])
def create_note():
    conn = get_conn()
    try:
        db.add_upcoming_note(conn, **_note_form(request.form))
    except ValueError as exc:
        conn.rollback()
        return redirect(url_for("upcoming.upcoming_view", error=str(exc)))
    conn.commit()
    return redirect(url_for("upcoming.upcoming_view"))


@upcoming.route("/<int:note_id>/edit", methods=["POST"])
def edit_note(note_id):
    conn = get_conn()
    if db.upcoming_note(conn, note_id) is None:
        abort(404)
    try:
        db.edit_upcoming_note(conn, note_id, **_note_form(request.form))
    except ValueError as exc:
        conn.rollback()
        return redirect(url_for("upcoming.upcoming_view", error=str(exc),
                                edit=note_id))
    conn.commit()
    return redirect(url_for("upcoming.upcoming_view"))


@upcoming.route("/<int:note_id>/shelve", methods=["POST"])
def shelve_note(note_id):
    conn = get_conn()
    try:
        db.shelve_upcoming_note(conn, note_id, request.form.get("reason"))
    except ValueError as exc:
        conn.rollback()
        return redirect(url_for("upcoming.upcoming_view", error=str(exc)))
    conn.commit()
    return redirect(url_for("upcoming.upcoming_view"))


@upcoming.route("/<int:note_id>/link", methods=["POST"])
def link_note(note_id):
    """It landed as this publication — chosen by hand, always (#56)."""
    conn = get_conn()
    try:
        db.link_upcoming_note(conn, note_id,
                              request.form.get("publication_id", type=int) or 0)
    except ValueError as exc:
        conn.rollback()
        return redirect(url_for("upcoming.upcoming_view", error=str(exc)))
    conn.commit()
    return redirect(url_for("upcoming.upcoming_view"))


@upcoming.route("/<int:note_id>/reopen", methods=["POST"])
def reopen_note(note_id):
    conn = get_conn()
    db.reopen_upcoming_note(conn, note_id)
    conn.commit()
    return redirect(url_for("upcoming.upcoming_view"))


@insights.route("/")
def insights_hub():
    """The Insights hub (#49): one tile per view, each carrying a live stat.

    Podcasts are excluded from every Insights view, and this page is where
    that is said — once, uniformly, instead of a footnote per chart.
    """
    conn = get_conn()
    graph = queries.topic_graph(conn)
    podcasts = conn.execute(
        "SELECT COUNT(*) FROM publications WHERE pub_type = 'Podcast' "
        "AND parent_id IS NULL").fetchone()[0]
    mapped = conn.execute(
        "SELECT COUNT(DISTINCT publication_id) FROM publication_topics "
        "JOIN publications p ON p.id = publication_id "
        "WHERE p.parent_id IS NULL").fetchone()[0]
    quarters = len(queries.topic_time(conn)["quarters"])
    current_people = len(queries.who_knows_what(conn)["people"])
    placed = conn.execute("SELECT COUNT(*) FROM landscape_coords").fetchone()[0]
    return render_template(
        "insights.html", topics_n=len(graph["nodes"]),
        edges_n=sum(1 for e in graph["edges"] if e["n"] >= 5),
        mapped=mapped, podcasts=podcasts, quarters=quarters,
        current_people=current_people, placed=placed,
        caveats_n=len(queries.coverage_caveats(conn)))


@insights.route("/map")
def topic_map_view():
    return render_template("insights_map.html")


@insights.route("/time")
def topic_time_view():
    return render_template("insights_time.html")


@insights.route("/data/topic-time.json")
def topic_time_data():
    return queries.topic_time(get_conn())


@insights.route("/landscape")
def landscape_view():
    return render_template("insights_landscape.html")


@insights.route("/data/landscape.json")
def landscape_data():
    """Coordinates come from the cache; new publications are slotted in on
    the way past (cheap, moves nobody — #49). The first full fit is a CLI
    act (`pubbrain landscape`), never a side effect of a page load."""
    from . import landscape
    conn = get_conn()
    if landscape.place_new(conn):
        conn.commit()
    return queries.landscape(conn)


@insights.route("/coverage")
def coverage_page():
    """The coverage view (#55): what the catalog does and does not hold for a
    term, verdict counted rather than generated, caveats always attached."""
    term = (request.args.get("term") or "").strip()
    result = queries.coverage_view(get_conn(), term) if term else None
    return render_template("insights_coverage.html", term=term, result=result)


@insights.route("/data/coverage.json")
def coverage_data():
    term = (request.args.get("term") or "").strip()
    if not term:
        return {"error": "term required"}, 400
    return queries.coverage_view(get_conn(), term)


@insights.route("/matrix")
def matrix_view():
    return render_template("insights_matrix.html")


@insights.route("/data/matrix.json")
def matrix_data():
    return queries.who_knows_what(
        get_conn(), current_only=request.args.get("all") != "1")


@insights.route("/data/keyword-time.json")
def keyword_time_data():
    q = (request.args.get("q") or "").strip()
    return queries.keyword_time(get_conn(), q,
                                deep=request.args.get("deep") == "1")


@insights.route("/data/topic-graph.json")
def topic_graph_data():
    return queries.topic_graph(get_conn())


@insights.route("/data/topic.json")
def topic_spotlight_data():
    slug = (request.args.get("slug") or "").strip()
    rows = queries.topic_spotlight(get_conn(), slug)
    return {"publications": [dict(r) for r in rows]}


@insights.route("/data/upcoming-edge.json")
def upcoming_edge_data():
    """Open notes for the two Insights views that surface them (#56).

    A second fetch rather than a field on `topic-time.json` / `topic-graph.json`:
    those describe the published record and must stay identical whether or not
    notes exist, which is the invariant the tripwire tests hold.

    It sits on the `insights` blueprint, so unlike `/upcoming` it survives a
    remote deployment unless it says otherwise — which is why it says so here
    (#48). Both callers treat a failed fetch as "no notes" and draw the
    published record alone.
    """
    if not remote.serves_upcoming():
        abort(404)
    return queries.upcoming_edge(get_conn())


@insights.route("/data/topic-pair.json")
def topic_pair_data():
    a = (request.args.get("a") or "").strip()
    b = (request.args.get("b") or "").strip()
    rows = queries.topic_pair(get_conn(), a, b)
    return {"publications": [dict(r) for r in rows]}


def create_app(db_path=None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path      # None -> paths.DB_PATH
    app.register_blueprint(catalog)
    app.register_blueprint(review)
    app.register_blueprint(learning)
    app.register_blueprint(insights)
    # The upcoming layer is hand-entered notes about unpublished work (#56).
    # It is not registered at all in remote mode, so a tunnel-facing instance
    # has no route to it rather than a route that refuses (#48).
    if remote.serves_upcoming():
        app.register_blueprint(upcoming)

    @app.before_request
    def require_token():
        """Second lock behind Cloudflare Access (#48). Absent a configured
        token this is a no-op, which is the workstation and LAN case.

        A browser cannot set an Authorization header by hand, so a token in
        the query string is accepted once and moved into a cookie — the URL
        carrying it is not kept in history beyond that redirect.
        """
        expected = remote.web_token()
        if not expected:
            return None
        if request.endpoint == "static":
            return None
        presented = (remote.bearer_from(request.headers.get("Authorization"))
                     or request.cookies.get(remote.COOKIE))
        if remote.token_ok(presented, expected):
            return None
        from_url = (request.args.get("token") or "").strip()
        if remote.token_ok(from_url, expected):
            clean = {k: v for k, v in request.args.items(multi=True) if k != "token"}
            response = redirect(url_for(request.endpoint or "catalog.list_view",
                                        **{**(request.view_args or {}), **clean}))
            response.set_cookie(remote.COOKIE, from_url, httponly=True,
                                samesite="Lax", secure=True, max_age=30 * 86400)
            return response
        return "not authorised", 401

    @app.teardown_appcontext
    def close_conn(_exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def registry_counts():
        conn = get_conn()
        # Counts like every other surface: chapters reached through their
        # parent (#36), merged duplicates through their survivor (#47).
        row = conn.execute(
            """SELECT (SELECT COUNT(*) FROM publications
                       WHERE parent_id IS NULL) p,
                      (SELECT COUNT(*) FROM people
                       WHERE merged_into IS NULL) pe,
                      (SELECT COUNT(*) FROM primary_enrichment) e,
                      (SELECT COUNT(*) FROM reviews WHERE scope = 'enrichment') r
            """).fetchone()
        return {"registry": {"publications": row["p"], "people": row["pe"],
                             "enriched": row["e"], "reviewed": row["r"]},
                "serves_upcoming": remote.serves_upcoming()}

    @app.template_filter("people_line")
    def people_line(people):
        return ", ".join(p["name"] + (" (host)" if p["role"] == "host"
                                      else " (guest)" if p["role"] == "guest" else "")
                         for p in people or [])

    return app


def run(port=PORT, host=None, debug=False) -> None:
    # The port check binds the data's fate, not the interface's: 8780–8799 is
    # exposed to LLM-driven browsers whatever address it listens on (#51).
    if port in BROWSER_EXPOSED_PORTS:
        raise SystemExit(
            f"port {port} is in 8780–8799, which is exposed to LLM-driven "
            "browsers on this machine — pick one outside that range")
    problems = remote.check_config()
    if problems:
        raise SystemExit("; ".join(problems))
    create_app().run(host=host or HOST, port=port, debug=debug)
