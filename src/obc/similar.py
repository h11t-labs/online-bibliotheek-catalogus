"""Content-based "meer boeken zoals dit" via TF-IDF cosine similarity.

This is an *offline precompute*: it reads the finished catalog, builds one TF-IDF
vector per **work** from its text signal (summary + genres + keywords + title +
author), finds each work's nearest neighbours by cosine similarity, and stores the
top-K into a ``work_similar`` table. The web app then only does a cheap indexed
lookup per book page — no ML at request time.

Vectorising works rather than editions means better signal from the pooled text,
fewer documents, and no post-hoc de-duplication: an e-book and its audiobook are
one document, so there is no way for the strip to recommend a book's own twin.

Why TF-IDF and not collaborative filtering: we have rich *content* per title
(99% of books carry a ~600-char summary, 96% carry genres) but **no behavioural
data** (borrows/ratings), so "mensen die dit leenden…" is out of reach. TF-IDF over
the text we already scrape gives a solid "voelt als hetzelfde boek" signal for free.

scikit-learn is an *optional* dependency (see ``[project.optional-dependencies]``):
only this precompute imports it. The web layer never does, so ``obc serve`` stays
lean. Install with ``uv sync --extra recommend`` and run ``obc similar``.

Field weighting: categorical signal (genre/keyword/author) is prefixed into its own
token namespace (``g·spanning``) so it can't collide with summary words, and
repeated to weight it above free text. Genres are the strongest topical signal, so
they get the most repetition.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from . import db
from .log import logger

# Compact Dutch stop-word list — enough to stop function words from dominating the
# TF-IDF vocabulary. (idf already down-weights ubiquitous words, but dropping them
# keeps the vocabulary — and the sparse matrix — smaller and the vectors cleaner.)
_STOP_WORDS = (
    "de het een en van te dat die in op aan met voor is als maar om ook door naar of "
    "zijn was wordt worden werd hij zij ze we wij ik jij je u men er hier daar dan "
    "nog wel niet geen zo toen nu al deze dit zulke over onder tussen bij uit tot na "
    "zonder tegen per want omdat terwijl toch echter zoals meer meest zeer heel erg "
    "hun haar ons onze jullie mijn wie wat waar waarom hoe welke welk "
    "heeft hebben had hadden kan kunnen kon konden zal zullen zou zouden moet moeten "
    "mag mogen wil willen weer eens even nooit altijd vaak soms iemand iets niets alles "
    "elk elke ieder iedere beide zowel dus alleen samen zelf zich")
_STOP = frozenset(_STOP_WORDS.split())

# summary words shorter than this carry little topical signal
_MIN_LEN = 3
_WORD_RE = re.compile(r"[a-zà-ÿ0-9]+", re.IGNORECASE)


def _words(text: str | None) -> list[str]:
    """Lowercase word tokens from free text, minus stop-words and very short ones."""
    if not text:
        return []
    return [w for w in _WORD_RE.findall(text.lower())
            if len(w) >= _MIN_LEN and w not in _STOP]


def _doc_tokens(row: sqlite3.Row, genres: list[str], *,
                w_genre: int = 3, w_keyword: int = 2, w_author: int = 1,
                w_title: int = 2) -> list[str]:
    """The weighted token bag for one work.

    Categorical fields live in their own token namespace (``g·``/``k·``/``a·``) so a
    genre "Oorlog" never matches the summary word "oorlog"; repetition sets the
    weight (higher = stronger pull towards works sharing that value).
    """
    toks: list[str] = []
    for g in genres:
        gk = re.sub(r"\s+", "_", g.strip().lower())
        toks += [f"g·{gk}"] * w_genre
    for k in _words(row["keywords"]):
        toks += [f"k·{k}"] * w_keyword
    if row["author"]:
        ak = re.sub(r"\s+", "_", row["author"].strip().lower())
        toks += [f"a·{ak}"] * w_author
    toks += _words(row["title"]) * w_title
    toks += _words(row["summary"])
    return toks


def _load_docs(conn: sqlite3.Connection,
               w_author: int = 1) -> tuple[list[str], list[list[str]]]:
    """Return ``(work_ids, token_bags)`` for every work, in one pass.

    Genres are gathered via a single grouped query and joined in memory, so this is
    two queries total regardless of catalog size. ``w_author`` sets the author-token
    weight (0 drops it — the 'lsa' method uses 0 for more author diversity).
    """
    genres_by_work: dict[str, list[str]] = {}
    for r in conn.execute(
            "SELECT wg.work_id, g.name AS name "
            "FROM work_genres wg JOIN genres g ON g.id = wg.genre_id"):
        genres_by_work.setdefault(r["work_id"], []).append(r["name"])

    work_ids: list[str] = []
    bags: list[list[str]] = []
    for row in conn.execute(
            "SELECT work_id, title, author, summary, keywords FROM works"):
        work_ids.append(row["work_id"])
        bags.append(_doc_tokens(row, genres_by_work.get(row["work_id"], []),
                                w_author=w_author))
    return work_ids, bags


# Recommender used by the site:
#   lsa — genre-rich TF-IDF with the author token dropped, reduced with Truncated SVD
#         then cosine. More author-diverse and less word-literal than plain TF-IDF, and
#         best on the held-out audience metric in the experiments (scratchpad/
#         rec_experiments.py). The (unused) plain-TF-IDF baseline was dropped after that
#         comparison. The ``method`` column stays so another recommender can be added.
METHODS = ("lsa",)
_MIN_SCORE = {"lsa": 0.10}   # cosine score below which a neighbour is dropped
_AUTHOR_WEIGHT = {"lsa": 0}  # 0 = drop the author token (more author diversity)


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create ``work_similar`` (keyed by method so both recommenders coexist), and
    migrate a pre-``method`` table by dropping it (it is always rebuildable).

    ``book_similar`` goes with it: a rebuild into a fresh temp DB can never carry
    the old table over, but a local dev DB built before the works model can, and a
    stale table of edition PPNs would be a confusing thing to find."""
    conn.execute("DROP TABLE IF EXISTS book_similar")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(work_similar)")]
    if cols and "method" not in cols:
        conn.execute("DROP TABLE work_similar")
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS work_similar ("
        "  work_id       TEXT NOT NULL,"
        "  method        TEXT NOT NULL,"
        "  rank          INTEGER NOT NULL,"
        "  other_work_id TEXT NOT NULL,"
        "  score         REAL,"
        "  PRIMARY KEY (work_id, method, rank));"
        "CREATE INDEX IF NOT EXISTS idx_similar_work ON work_similar(work_id, method);")
    conn.commit()


def build_similar(conn: sqlite3.Connection, *, method: str = "lsa", k: int = 24,
                  min_score: float | None = None, lsa_dim: int = 300,
                  batch: int = 64) -> int:
    """Compute top-``k`` cosine neighbours per work for one ``method`` and refill its
    rows in ``work_similar``.

    ``'lsa'`` drops the author token and reduces the genre-rich TF-IDF space with
    Truncated SVD before cosine. Returns the number of works that got ≥1 neighbour.
    Raises ``ImportError`` if scikit-learn is absent.
    """
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; expected one of {METHODS}")
    try:
        import numpy as np
        from scipy import sparse
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.preprocessing import normalize
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError(
            "book recommendations need scikit-learn. Install the optional extra:\n"
            "    uv sync --extra recommend") from exc

    if min_score is None:
        min_score = _MIN_SCORE[method]
    work_ids, bags = _load_docs(conn, w_author=_AUTHOR_WEIGHT[method])
    n = len(work_ids)
    logger.info(f"[{method}] TF-IDF over {n} works…")

    # analyzer=identity: we already produced the exact token list per doc, so the
    # vectorizer must not re-tokenize/lowercase. min_df=2 drops hapax terms (a word
    # in one book can't link two books anyway) and shrinks the vocabulary.
    vec = TfidfVectorizer(analyzer=lambda toks: toks, min_df=2, sublinear_tf=True,
                          norm="l2", dtype=np.float32)
    x = vec.fit_transform(bags)  # (n, vocab), L2-normalised rows -> cosine = dot
    logger.info(f"[{method}] vocabulary: {len(vec.vocabulary_)} terms; nnz={x.nnz}")
    # The per-doc token lists dominate RSS (one str object per token, ~64k docs);
    # once vectorised they are dead weight. Drop them before the SVD peak.
    bags.clear()
    del bags

    if method == "lsa":
        # TruncatedSVD needs n_components < min(n_samples, n_features)
        dim = max(1, min(lsa_dim, x.shape[1] - 1, x.shape[0] - 1))
        svd = TruncatedSVD(n_components=dim, random_state=0)
        # copy=False on both steps: the SVD output is already a fresh array, so
        # re-typing and L2-normalising it in place avoids two (n, dim) copies.
        feats = svd.fit_transform(x).astype(np.float32, copy=False)
        feats = normalize(feats, copy=False)  # dense, L2-normed
        logger.info(f"[lsa] reduced to {dim} dims "
                    f"(explained variance {svd.explained_variance_ratio_.sum():.2f})")
        del x, svd  # the sparse TF-IDF is not needed once reduced
    else:
        feats = x  # sparse

    _ensure_table(conn)
    cur = conn.cursor()
    cur.execute("DELETE FROM work_similar WHERE method = ?", (method,))

    is_sparse = sparse.issparse(feats)
    # For the dense case keep .T as a *view*: BLAS handles a transposed operand, and
    # a contiguous copy would duplicate the whole (n, dim) matrix.
    ft = feats.T.tocsr() if is_sparse else feats.T
    written = 0
    # Exactly k candidates, no over-fetch: nothing is skipped any more. The old
    # pool existed to survive dropping edition twins from the candidate list, and
    # at this grain a work has no twin to drop. min_score only ever truncates the
    # list, so a short one is the honest answer rather than something to pad.
    pool = max(1, min(k, n - 1))
    for start in range(0, n, batch):
        stop = min(start + batch, n)
        block = feats[start:stop] @ ft
        sims = block.toarray() if is_sparse else np.asarray(block)  # (bsz, n) float32
        for i in range(stop - start):
            gi = start + i
            row_sims = sims[i]
            row_sims[gi] = -1.0  # never recommend the work itself
            cand = np.argpartition(row_sims, -pool)[-pool:]
            cand = cand[np.argsort(row_sims[cand])[::-1]]
            picked = 0
            for j in cand:
                s = float(row_sims[j])
                if s < min_score:
                    break
                cur.execute(
                    "INSERT INTO work_similar(work_id, method, rank, other_work_id, score) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (work_ids[gi], method, picked, work_ids[j], round(s, 4)))
                picked += 1
                if picked >= k:
                    break
            if picked:
                written += 1
        if start and start % (batch * 20) == 0:
            logger.info(f"  [{method}] neighbours: {stop}/{n}")
    conn.commit()
    logger.info(f"[{method}] filled: {written}/{n} works have ≥1 recommendation")
    return written


def main(db_path: str | Path = db.DEFAULT_DB, *, k: int = 24,
         lsa_dim: int = 300) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for m in METHODS:
            build_similar(conn, method=m, k=k, lsa_dim=lsa_dim)
        # drop rows from any recommender no longer built (e.g. the retired 'tfidf')
        qs = ",".join("?" * len(METHODS))
        conn.execute(f"DELETE FROM work_similar WHERE method NOT IN ({qs})", METHODS)
        conn.commit()
    finally:
        conn.close()
