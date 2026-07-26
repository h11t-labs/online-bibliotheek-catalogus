# Plan: one *book*, several *editions*

**Question.** The library models an e-book and a digital audiobook as two
unrelated items, each with its own PPN and partly its own fields. For a reader
they are one book. Can this catalog present it that way — search a book, find
links to *its* e-book and *its* audiobook — and if so, how?

**Answer.** Yes, and most of the hard part is already built. What is missing is
not a new capability but a single explicit identity: a `work_id`. Today "these
two rows are the same book" is re-derived, slightly differently, in seven places
from a string comparison on `(lower(title), lower(author))`. Making that identity
a stored column, and then a real `works` table, turns a pile of local workarounds
into one modelled fact — and unlocks the things currently declared impossible
(honest format pages, one URL per book, format filters that don't change what a
"result" means).

This document is the plan: what it costs today, what to build, in what order,
what can go wrong, and how to measure that it worked.

---

## 1. What exists today

The two-level model is already implicit:

| Layer | Where it lives |
| --- | --- |
| Edition (one per PPN) | `books` table — the real, faithful mirror of the library |
| Work ("the book") | `books.primary_edition`, plus ad-hoc `(title, author)` joins |

`db.set_primary_editions()` (`src/obc/db.py:311`) stamps one representative
edition per `(lower(title), lower(COALESCE(author,'')))` group — e-book first,
then lowest PPN. Search then collapses editions by filtering
`primary_edition = 1` (`src/obc/web/queries.py:189`), which is fast because it is
a plain indexed boolean.

Everything else re-derives the same grouping by hand:

- `queries.formats_map()` (`:227`) — which formats a work exists in, via
  `WHERE title IN (…)` then grouping on `(title, author)` in Python.
- `queries.editions_map()` (`:246`) — `{format: ppn}` per work, same query again,
  "lower PPN wins" when a work has two editions of one format.
- `queries.book_detail()` (`:405`) — "other editions of this work", a third
  variant, this time as SQL on `lower(title)`/`lower(author)`.
- `queries.browse_summary()` (`:574`) — availability counts as two correlated
  `EXISTS` subqueries over the same key, needing a dedicated functional index
  (`db.py:160`) to stay under a second on the biggest genre page.
- `queries._collapse_editions()` (`:210`) — bolted onto the author, series and
  genre shelves and their counts.
- `similar._norm_key()` (`src/obc/similar.py:83`) — a *fourth* spelling of the
  key, used to drop edition-twins from the recommendation strip at build time.
- `normalize.match_lists()` (`src/obc/normalize.py:158`) — a curated-list slot
  matches exactly one PPN, chosen by whichever edition's ISBN or title-key was
  seen first; the other edition silently loses the ribbon.

## 2. What the workaround costs

Not hypothetical — each of these is visible in the current code or its history.

1. **A format filter turns the work model off.** `search()` skips the collapse
   whenever `format=` is set, on the assumption that "every work then has only
   that one edition". That is false: the catalog holds works with four audiobook
   editions. This is exactly why `/e-books` and `/luisterboeken` were built and
   then **removed again** in #27 — they counted editions as titles and showed the
   same work four times. A whole class of search intent
   ("thrillers luisterboek bibliotheek") has no landing page as a result.
2. **Aggregate pages had to be patched one by one** to agree with the shelf below
   them. `/genre/avontuur` reported 0 audiobooks while its own shelf badged 91,
   because with editions collapsed each work is counted through whichever edition
   represents it. Fixed by adding `EXISTS` subqueries — a workaround whose cost is
   an extra index and a slower page.
3. **A work can be unfindable.** FTS rows are per edition; the
   `primary_edition = 1` filter is applied *after* `MATCH`. Genres, keywords,
   audience and ISBN come from the detail pass, which is per PPN — so if only the
   audiobook edition carries a summary or subject term, a query matching only that
   text returns only that row, which the collapse then throws away. Zero results
   for a book the catalog holds.
4. **Two URLs per book.** `/book/{ebook_ppn}` and `/book/{audiobook_ppn}` render
   near-identical pages, each self-canonical (`base.html:13`), both listed in
   `sitemap-books-*.xml` (`app.py:1012`). By the figures in the code's own comments
   — ~68k books of which ~56k are a primary edition (`db.py:331`) — that is ~12k
   URLs whose content is a duplicate of another page, today: split link equity,
   wasted crawl budget on one small VM, and a "cover + title" link on search cards
   that has to *pick* an edition to open (`search.html:279`, "the e-book by
   default"). A looser key than the current exact `(title, author)` match will find
   more pairs, not fewer, so ~12k is the floor.
5. **Metadata is not pooled.** The union of two editions' subjects, keywords,
   ISBNs, audience and age is strictly richer than either. Today the work shows
   only the representative's — and the representative is chosen by format, not by
   completeness.
6. **The library's own cross-edition link is thrown away.** Detail pages carry an
   "Ook beschikbaar als" block whose `<a href>` points straight at the twin
   edition's PPN. `detail.py:39` keeps the *label text* ("Luisterboek (digitaal)")
   and discards the href — the single most authoritative signal available.

## 3. Target model

Keep the two layers, make both explicit.

```
works        one row per book        <- what search, browse, shelves, lists, sitemap use
  ^
  | work_id
  |
books        one row per edition     <- unchanged: the faithful mirror, borrow links, per-format facts
```

**Non-goals.** The scrape stays per PPN: `data/raw/records/{ppn}.json` keeps
mirroring the library one item at a time, because that is what the library is.
The `books` table keeps every column it has. The format facet stays — readers
genuinely search for "luisterboek" — it just changes meaning from *"this row is
an audiobook"* to *"this book is available as an audiobook"*. And the borrow
button stays per edition: you borrow an edition, never a work.

## 4. Work identity

One new module, `src/obc/work.py`, owning the whole question. Two evidence
sources, unioned with a union-find over PPNs:

**(a) Explicit cross-edition links — authoritative.** Extend `parse_detail()` to
capture the PPNs in the "Ook beschikbaar als" hrefs into
`related_ppns: ["460719130"]` (the fixture `tests/fixtures/ebook_460719149.html`
already contains one). Union both directions, but only with PPNs the catalog
actually holds. This is the library asserting the relationship itself: no string
matching, no false positives, and it survives a retitled edition.

**(b) A conservative normalised key — the fallback.**

```python
key = f"{fold(strip_format_noise(title))}|{surname_key(first_author)}|{fold(language)}"
```

Three deliberate choices, all tightenable later:

- `strip_format_noise` drops trailing "(luisterboek)", "- luisterboek",
  "(digitaal)" and the like, which currently *split* works that should merge.
- The **first author's surname** rather than the whole author string, so
  `"Anna Vrij"` and `"A. Vrij ; Jan Stem"` group — reusing `surname_key()`, which
  already handles particles, initials and Nordic transliteration.
- **Language is part of the key.** Two same-titled books in different languages
  are different books to anyone using the language filter, and a merged work would
  have no answer for "what language is this". A missing language (the field is
  junk-filtered, so NULL happens) matches anything *within* an otherwise equal
  key rather than forming its own group.

**Representative / `work_id` = the representative edition's PPN**, chosen exactly
as today (e-book first, then lowest PPN). This is worth more than a synthetic id:
every existing `/book/{ppn}` URL stays meaningful — it either *is* the work URL or
301s to it — and PPNs are issued increasing over time, so a newly licensed edition
gets a higher PPN and cannot steal the representative slot from an existing
e-book. No id churn, no dead links, no new URL space.

**Escape hatch.** `data/raw/work_overrides.json` — `{"merge": [[ppn, ppn], …],
"split": [[ppn, ppn], …]}`, applied last. Same shape as the existing curated
`PUBLISHER_ALIASES` / `AUTHOR_ALIASES` lists: when data is wrong in a way no rule
should guess, curate the exception.

### Auditing the grouping before trusting it

The precision/recall of (b) can be measured **today, with zero extra requests**,
against (a)'s label text which is already stored for every enriched record:

- a record whose `also_available_as` names the other format but whose key found no
  twin → **false split** (a missed merge), countable exactly;
- a record with an audiobook twin in its group but an empty `also_available_as` →
  **false-merge candidate**, worth eyeballing.

Ship this as `obc works --report`: group-size histogram, both counts above, and
the largest / most suspicious groups (differing publisher, year gap > 5, differing
language). Run it against a copy of the production DB — the same discipline #27
used — before any URL behaviour changes.

Where the report shows misses, a targeted `obc scrape --relink` pass re-fetches
detail pages **only** for the candidates it names (records whose label says a twin
exists but no link was captured), not for all 68k titles. That keeps the crawl in
the hours, not days, and it is resumable like every other pass. Note that
`enrich()` skips records that already have an ISBN, so relink needs its own
selector (`related_ppns` key absent) — a two-line change.

## 5. Schema

Free of migration concerns: `normalize` drops and recreates every table into a
temp DB and atomically swaps it in (`db.stream_rebuild`, `normalize.normalize`).
Schema changes cost nothing but a rebuild.

```sql
ALTER-free, since the schema is recreated each run:

books.work_id      TEXT      -- representative PPN of this edition's work
CREATE INDEX idx_books_work ON books(work_id);

CREATE TABLE works (
    work_id        TEXT PRIMARY KEY,   -- = the representative edition's ppn
    title, author, cover_url, slug,    -- from the representative edition
    summary        TEXT,               -- longest non-empty across editions
    language       TEXT,               -- representative's, else any non-null
    publisher      TEXT,
    year           INTEGER,            -- MIN(NULLIF(year, 0)) across editions
    series, series_no, category, audience, age, keywords,
    has_ebook      INTEGER,            -- MAX(format = 'ebook')
    has_audiobook  INTEGER,
    ereader        INTEGER,            -- MAX(ereader) over e-book editions
    ebook_ppn      TEXT,               -- MIN(ppn) per format: replaces editions_map
    audiobook_ppn  TEXT,
    n_editions     INTEGER,
    added_rank     INTEGER             -- MIN(): a new audiobook resurfaces the book
);
CREATE TABLE work_genres  (work_id, genre_id, parent_id);   -- union over editions
CREATE TABLE work_authors (work_id, author_id, position);   -- union over editions
CREATE VIRTUAL TABLE works_fts USING fts5(work_id UNINDEXED, title, author,
    subjects, summary, tokenize = 'unicode61 remove_diacritics 2');
```

Cheap to build and cheap to run:

- Grouping joins the existing `_prepass()` streaming pass — it already reads every
  record file — so no extra I/O. `iter_records()` stamps `work_id` per record;
  union-find over ~68k short strings is a few MB and well under a second.
- `works`, `work_genres`, `work_authors` are `INSERT … SELECT … GROUP BY work_id`
  *after* books are inserted: set-based, constant memory, runs on the small VM.
- The browse indexes get simpler, not more numerous. Today every sort needs a
  `(primary_edition, <sort key>)` composite because the hot query filters a
  boolean over 68k rows (`db.py:146-157`); over `works` every row is a book, so a
  plain `(year DESC)` / `(title COLLATE NOCASE)` index serves it. `works_fts` is
  smaller than `books_fts` by the number of collapsed editions, and the
  `idx_books_title_author_lower` functional index — which exists solely to make
  the `(title, author)` workaround fast — can go.

## 6. Query layer

`queries.py` shrinks. Concretely:

- `search()` selects from `works`; `_build_where()` maps `format=audiobook` to
  `w.has_audiobook = 1` and `ereader=1` to `w.ereader = 1`. **The
  collapse-unless-format-filter branch disappears**, and with it the class of bug
  in §2.1–2.2.
- `formats_map()` / `editions_map()` / `book_detail()["editions"]` collapse into
  reading `has_*`/`*_ppn` off the row already fetched — three queries per page
  gone.
- `browse_summary()` replaces both `EXISTS` subqueries with
  `SUM(has_ebook)` / `SUM(has_audiobook)`, and needs no mirroring of `search()`'s
  collapse rule because there is no rule left to mirror.
- `_collapse_editions()` disappears from the author, series and genre queries;
  they join `work_authors` / `work_genres` instead. `author_title_counts()` and
  `genre_index()` stop needing "count distinct, editions collapsed" gymnastics.
- `suggest()` queries `works_fts`, so a title can no longer appear twice and the
  per-suggestion `editions_map()` round-trip is dropped.

## 7. Web / UI / SEO

- **One URL per book.** `/book/{ppn}` resolves `books.work_id`; a non-representative
  PPN 301s to `/book/{work_id}`. Every shared or indexed audiobook URL keeps
  working. `sitemap-books-*.xml` lists work URLs only — at least ~12k fewer URLs to
  crawl (§2.4), all of them distinct pages.
- **The book page answers the actual complaint.** Shared facts once (title,
  authors, summary, genres, series, lists, "meer zoals dit"), then **one fact block
  per edition** — E-book: pages, size, file type, e-reader yes/no, ISBN, its own
  "Lenen op onlinebibliotheek.nl ↗"; Luisterboek: duration, narrator, size, ISBN,
  its own borrow button. This is the honest answer to *"elk item heeft ook andere
  properties soms"*: don't merge the fields, show them side by side under one
  heading. The existing "Ook als luisterboek →" badge becomes an anchor to the
  block instead of a link to another page.
- **Structured data gets more correct, not less.** One `Book` for the work with a
  `workExample` per edition carrying `bookFormat` + `isbn` + `numberOfPages` /
  `duration` — the pattern schema.org actually documents for this, replacing two
  competing `Book` entities that each claim the same title.
- **`/e-books` and `/luisterboeken` can come back**, honestly counted, closing the
  gap #27 opened when it removed them. Optional and separately shippable.
- **`/stats`** reports both numbers ("N boeken, M edities") instead of quietly
  counting editions as books.
- **Curated lists** match a *work*, so a list slot lands on the book whichever
  edition it was catalogued from, and the dedupe-per-slot rule in `match_lists()`
  becomes dedupe-per-work.
- **`similar.py`** builds vectors per work from the pooled text (better signal,
  fewer documents, faster build) and drops the `_norm_key` post-hoc de-duplication
  loop entirely.

**Deploy window.** A deploy triggers a refresh but serves the old DB until the
swap, so the web layer must tolerate a `works`-less catalog for a few minutes.
Same guard pattern already used for `primary_edition` and `book_genres.parent_id`:
one `_has_works(conn)` helper, falling back to today's path, removed one release
later.

## 8. Rollout

Five PRs, each shippable on its own and each leaving the site working.

| # | Scope | Rough size |
| --- | --- | --- |
| 1 | `obc/work.py` (key + union-find + overrides), `related_ppns` in `detail.py`, `books.work_id`, every existing grouping site switched to `work_id`. No visible change beyond consistency. | ~150 LOC + tests |
| 2 | `works` / `work_genres` / `work_authors` / `works_fts`; search, browse, shelves, facets, summaries, stats move over; format filter becomes a work-level flag; `/e-books` + `/luisterboeken` return. | ~300 LOC |
| 3 | One URL per book: 301s, merged book page with per-edition blocks, `workExample` JSON-LD, sitemap over works, `suggest` over `works_fts`. | ~250 LOC |
| 4 | `similar.py` over works; curated lists match works. | ~100 LOC |
| 5 | `obc works --report`, `obc scrape --relink`, README + this doc updated with measured numbers. | ~150 LOC |

Run PR5's report *before* merging PR3 — the 301s are the one irreversible-ish step
(they teach crawlers a mapping), so the grouping should be measured first.

**Tests.** `tests/sampledata.py` grows three cases the current fixture cannot
express: two audiobook editions of one work (the case that killed
`/luisterboeken`), a title whose format suffix currently splits it, and a record
carrying `related_ppns` so the authoritative path is covered. Then: `/book/002`
301s to `/book/001`; `?format=audiobook` totals count works not editions;
`work_genres` is the union of its editions'; a work whose only summary sits on the
audiobook edition is findable by a word from that summary (§2.3, today a failing
case).

## 9. Decisions to make

Each has a recommendation; none blocks starting on PR1.

1. **`works.year` = oldest edition or representative's?** Recommend `MIN`: it is
   the book's publication year, and per-edition years stay visible on the page.
   Note it shifts "Nieuwste eerst" slightly for works whose editions span years.
2. **Merge same-format duplicates too?** Recommend yes — four audiobook editions
   of one book are four cards today. The risk is merging an abridged or
   re-translated version; the language guard plus the report's "year gap" flag
   should surface those.
3. **Whose cover?** The representative's (the audiobook's own cover stops being
   shown on shelves). Recommend accepting; optionally show it in its edition block.
4. **Keep `primary_edition`?** Recommend dropping it in PR2 once `works` exists —
   it is the same fact, less usefully expressed.

## 10. Risks

- **False merges hide a book** (worse than a false split, which merely duplicates
  it). Mitigated by the conservative key, the language guard, the pre-flight
  report, and the override file.
- **301 churn** if a representative ever changes. Bounded by PPN monotonicity; the
  only real trigger is the representative edition being withdrawn, which today
  already turns that URL into a 404.
- **Relink crawl cost** if run naively over the whole catalog. Hence the targeted
  selector, and hence (b) being a complete fallback rather than a stopgap: the
  feature ships without a single new request.
- **Two shapes in flight** during PR1→PR2. Kept short by merging them in one
  release cycle, with the `_has_works` guard covering the deploy window.

## 11. Verdict

Feasible, and cheaper than it looks: no migrations (the DB is rebuilt every run),
no scrape redesign, no new URL space, and the query layer gets *smaller*. The work
is mostly moving seven ad-hoc groupings onto one modelled identity — which is also
what turns the format facet from a thing we work around into a thing we can
answer.
