"""The search engine: hybrid full text + vector + exact anchors, fused by rank (RRF), plus around / overview /
associate, and (only when a memories table has rows) drill.

Plain functions and one class, no CLI, no MCP. WP8's CLI and MCP server call `Searcher`; `format.py` turns results
into the compact text a model reads, or into JSON.

    searcher = Searcher(conn, embedder, SearchConfig(timezone='Europe/Paris'))
    result = searcher.search('castle geese', sources=['notes'], date_from='2025-03-01', limit=10)
    print(format.render(result, searcher.cfg))

`conn` is a psycopg 3 connection. For read-only use open it with `open_readonly(dsn)`.
"""
from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field

from .core import (Filters, MODES, SearchConfig, SearchError, check_sources, clean_list, diversify, flat,
                   fts_weight, parse_day, rrf, day_bounds, LIMIT_MAX)
from .embedder import QueryEmbedder, vector_literal
from .sql import Store, build_where

READ_ONLY_OPTIONS = '-c default_transaction_read_only=on -c statement_timeout=10s'


def open_readonly(dsn: str, timeout: str = '10s'):
    """A read-only autocommit psycopg 3 connection with a statement timeout."""
    try:
        import psycopg
    except ImportError:
        raise SearchError('psycopg 3 is not installed (pip install "psycopg[binary]")', 'db')
    try:
        from ..config import connect_kwargs
        return psycopg.connect(dsn, autocommit=True,
                               options=f'-c default_transaction_read_only=on -c statement_timeout={timeout}',
                               **connect_kwargs(dsn))
    except Exception as e:
        first = str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__
        raise SearchError(f'cannot reach the archive database ({first})', 'db')


# ---------------------------------------------------------------- anchors (query side)

def extract_anchors(text: str) -> list:
    """[(token, kind)] cut from a query. Uses the archive's own extractor (resonant_archive.anchors) so query tokens
    are cut exactly as stored tokens were; if that module is not installed, a small built-in cutter handles urls,
    paths, versions, identifiers and quoted phrases."""
    try:
        from resonant_archive.anchors import extract   # type: ignore
    except ImportError:
        return _fallback_extract(text)
    return sorted(extract(text))


def _fallback_extract(text: str) -> list:
    import re
    out = set()
    for m in re.finditer(r'https?://[^\s<>"\'`)\]}|]+', text or ''):
        out.add((m.group(0).rstrip('.,;:!?\'"*_'), 'url'))
    rest = re.sub(r'https?://\S+', ' ', text or '')
    for m in re.finditer(r'(?<![\w.:/-])(?:~|[A-Za-z]:)?(?:/[\w.@+%-]+){2,}/?', rest):
        out.add((m.group(0).rstrip('.,;:!?\'"*_)'), 'path'))
    for m in re.finditer(r'(?<![\w.])(?:v\d+\.\d+(?:\.\d+)*|\d+\.\d+\.\d+)(?![\w]|\.\d)', rest):
        out.add((m.group(0), 'version'))
    for m in re.finditer(r'\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b|\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+\b', rest):
        if len(m.group(0)) > 6:
            out.add((m.group(0), 'ident'))
    for q in (r'"([^"\n]{3,60})"', r"'([^'\n]{3,60})'", r'`([^`\n]{3,60})`'):
        for m in re.finditer(q, text or ''):
            out.add((m.group(1).strip(), 'quote'))
    return sorted(out)


# ---------------------------------------------------------------- results

@dataclass
class Hit:
    rank: int
    row: dict                          # id, source, conv_id, conv_title, ts, seq, speaker, context, hidden, tags, text, len
    ranks: dict = field(default_factory=dict)    # {signal: rank within that signal}
    extra: dict = field(default_factory=dict)    # e.g. {'cosine': 0.83}


@dataclass
class SearchResult:
    query: str
    mode: str
    hits: list                         # [Hit], best first
    fused: int                         # candidates after fusion (before limit)
    signals: dict                      # {'fts': 12, 'vec': 'skipped: ...', 'anchor': 0}
    vectors_skipped: bool
    vectors_skipped_reason: str | None
    notes: list
    pending_embeddings: int
    excluded_recent: int
    filters: Filters
    elapsed_ms: int


@dataclass
class AroundResult:
    target: dict
    rows: list                         # in seq order
    lo: int
    hi: int
    conv_min: int
    conv_max: int
    conv_total: int
    show_hidden: bool


@dataclass
class OverviewResult:
    date_from: datetime.date
    date_to: datetime.date
    sources: list | None
    by: list                           # [(source, speaker, visible, hidden)]
    contexts: list                     # [(context, n)]
    busiest: list                      # [(source, conv_id, title, t0, t1, n)]
    nconv: int
    memories: list | None              # None = no memories table / no rows / not asked; else [memory dict]
    memories_mode: str                 # include | only | none


@dataclass
class AssociateResult:
    origin: dict
    hits: list                         # [Hit] with extra['cosine']
    candidates: int
    skipped_conv: int
    skipped_source: int
    k: int
    exact_scan: bool
    excluded_recent: int
    filters: Filters


@dataclass
class DrillResult:
    memory: dict
    rows: list
    notes: list
    show_hidden: bool


# ---------------------------------------------------------------- the engine

class Searcher:
    def __init__(self, conn, embedder: QueryEmbedder | None = None, cfg: SearchConfig | None = None):
        self.cfg = cfg or SearchConfig()
        self.embedder = embedder
        self.store = Store(conn, self.cfg)
        self._sources: list | None = None

    # -- catalogue
    def sources(self) -> list:
        """Source labels, read from the database each time a Searcher is first used (never a fixed list)."""
        if self._sources is None:
            self._sources = self.store.known_sources()
        return self._sources

    def memories_available(self) -> bool:
        return self.store.has_memories()

    def available_actions(self) -> list:
        acts = ['search', 'around', 'overview', 'associate']
        return acts + ['drill'] if self.memories_available() else acts

    def _filters(self, sources, speakers, contexts, date_from, date_to, hidden, include_recent) -> Filters:
        f = Filters(sources=clean_list(sources, 'source'), speakers=clean_list(speakers, 'speaker'),
                    contexts=clean_list(contexts, 'context'),
                    date_from=parse_day(date_from) if date_from else None,
                    date_to=parse_day(date_to) if date_to else None,
                    hidden=bool(hidden), include_recent=bool(include_recent)).check()
        if f.sources:
            check_sources(f.sources, self.sources())
        return f

    # -- search
    def search(self, query: str, *, mode: str = 'hybrid', sources=None, speakers=None, contexts=None,
               date_from=None, date_to=None, hidden: bool = False, include_recent: bool = False,
               limit: int = 10) -> SearchResult:
        if not query or not query.strip():
            raise SearchError('search needs a query')
        if mode not in MODES:
            raise SearchError(f'mode: {mode} not one of {", ".join(MODES)}')
        if not 1 <= int(limit) <= LIMIT_MAX:
            raise SearchError(f'limit must be 1-{LIMIT_MAX}')
        f = self._filters(sources, speakers, contexts, date_from, date_to, hidden, include_recent)
        where, params = build_where(f, self.cfg)
        lists: dict = {}
        sig_status: dict = {}
        notes: list = []
        skipped_reason = None
        t0 = time.monotonic()
        st = self.store

        if mode in ('hybrid', 'fts'):
            lists['fts'] = st.fts_ids(query, where, params)
        if mode in ('hybrid', 'vector'):
            try:
                vec = self._embed(query)
                rows, exact = st.vec_ids(vec, where, params, self.cfg.signal_top)
                lists['vec'] = [r[0] for r in rows]
                if exact:
                    notes.append('vec used an exact scan (filters starved the index)')
            except SearchError as e:
                if mode == 'vector':
                    raise
                skipped_reason = str(e)
                sig_status['vec'] = f'skipped: {e}'
        if mode in ('hybrid', 'anchor'):
            found = extract_anchors(query)
            toks = sorted({t for t, _ in found})
            lists['anchor'] = st.anchor_ids(flat(query), toks, st.prefix_patterns(found), where, params)
            if toks:
                notes.append(f'anchor tokens from query: {len(toks)}')

        weights = {'vec': 1.0, 'anchor': 1.0, 'fts': fts_weight(query, self.cfg)}
        fused = rrf(lists, k=self.cfg.rrf_k, weights=weights)
        shown = fused[:int(limit)]
        rows = st.fetch_rows([i for i, _, _ in shown], 20000)
        hits = [Hit(n, rows[i], ranks) for n, (i, _, ranks) in enumerate(shown, 1) if i in rows]
        signals = {s: len(lists[s]) for s in lists}
        signals.update(sig_status)
        return SearchResult(query=query, mode=mode, hits=hits, fused=len(fused), signals=signals,
                            vectors_skipped='vec' in sig_status, vectors_skipped_reason=skipped_reason,
                            notes=notes, pending_embeddings=st.pending_embeddings(),
                            excluded_recent=st.count_recent_excluded(f), filters=f,
                            elapsed_ms=int((time.monotonic() - t0) * 1000))

    def _embed(self, text: str) -> str:
        if self.embedder is None:
            raise SearchError('no embedder configured', 'embedder')
        try:
            vec = self.embedder.embed_query(text)
        except SearchError:
            raise
        except Exception as e:   # EmbedderUnavailable, a timeout, a bad reply: all mean "no vector this time"
            raise SearchError(f'embedder unavailable ({type(e).__name__}: {str(e)[:120]})', 'embedder')
        if len(vec) != self.cfg.embed_dim:
            raise SearchError(f'embedder returned {len(vec)} dimensions, the archive uses {self.cfg.embed_dim}', 'embedder')
        return vector_literal(vec)

    # -- around
    def around(self, message_id: str, *, before: int = 4, after: int = 4, hidden: bool = False) -> AroundResult:
        for name, v in (('before', before), ('after', after)):
            if not 0 <= int(v) <= 30:
                raise SearchError(f'{name} must be 0-30')
        t = self.store.fetch_rows([message_id]).get(message_id)
        if not t:
            raise SearchError(f'no message with id {message_id[:120]} in the archive (ids come from search hits)')
        lo, hi = self.store.seq_bounds(t['conv_id'], t['seq'], int(before), int(after), hidden, message_id)
        rows = self.store.seq_slice(t['conv_id'], lo, hi)
        cmin, cmax, total = self.store.conv_extent(t['conv_id'])
        return AroundResult(t, rows, lo, hi, cmin, cmax, total, hidden)

    # -- overview
    def overview(self, date_from, date_to, *, sources=None, memories: str = 'include') -> OverviewResult:
        if memories not in ('include', 'only', 'none'):
            raise SearchError('memories must be include, only or none')
        d_from, d_to = parse_day(date_from), parse_day(date_to)
        Filters(date_from=d_from, date_to=d_to).check()
        srcs = clean_list(sources, 'source')
        if srcs:
            check_sources(srcs, self.sources())
        lo, hi = day_bounds(d_from, d_to, self.cfg.tz)
        counts = dict(by=[], contexts=[], busiest=[], nconv=0)
        if memories != 'only':
            counts = self.store.overview_counts(lo, hi, srcs)
        mems = None
        if memories != 'none' and self.memories_available():
            mems = self.store.memories_in_range(lo, hi)
        return OverviewResult(d_from, d_to, srcs, counts['by'], counts['contexts'], counts['busiest'], counts['nconv'],
                              mems, memories)

    # -- associate
    def associate(self, message_id: str, *, k: int = 8, sources=None, speakers=None, contexts=None,
                  date_from=None, date_to=None, hidden: bool = False, include_recent: bool = False) -> AssociateResult:
        if not 1 <= int(k) <= 20:
            raise SearchError('k must be 1-20')
        src = self.store.embedding_of(message_id)
        if not src:
            raise SearchError(f'no message with id {message_id[:120]} in the archive')
        if src[1] is None:
            raise SearchError(f'{message_id} has no embedding (hidden, blank, or still pending): nothing to associate from')
        f = self._filters(sources, speakers, contexts, date_from, date_to, hidden, include_recent)
        where, params = build_where(f, self.cfg)
        rows, exact = self.store.vec_ids(src[1], where, params, self.cfg.associate_fetch, exclude_conv=src[2])
        meta = self.store.fetch_rows([r[0] for r in rows])
        cand = [dict(meta[i], d=d) for i, d in rows if i in meta]
        kept, sk_conv, sk_src = diversify(cand, src[3], int(k))
        origin = self.store.fetch_rows([message_id])[message_id]
        hits = [Hit(n, r, {}, {'cosine': 1 - r['d']}) for n, r in enumerate(kept, 1)]
        excl = self.store.count_recent_excluded(f, ' and m.conv_id <> %(xc)s and m.embedding is not null', {'xc': src[2]})
        return AssociateResult(origin, hits, len(cand), sk_conv, sk_src, int(k), exact, excl, f)

    # -- drill (only with a memories table that has rows)
    def drill(self, key: str, *, hidden: bool = False) -> DrillResult:
        if not self.memories_available():
            raise SearchError('drill needs memories, and this archive has none (import a chronicle mirror first)')
        mems = self.store.all_memories()
        m = find_memory(key, mems)
        conv = str(m['conv_id'])
        f = self.store.endpoint_row(conv, m['from_uuid'])
        t = self.store.endpoint_row(conv, m['to_uuid'], last=True)
        notes: list = []
        if f and t:
            lo, hi = f['seq'], t['seq']
        else:
            lo2, hi2 = self.store.time_span_seqs(conv, m['t_from'], m['t_to'])
            lo, hi = (f['seq'] if f else lo2), (t['seq'] if t else hi2)
            missing = ' and '.join(x for x, y in (('from', f), ('to', t)) if not y)
            notes.append(f"[{missing} message not in the archive; taken from the memory's time span]")
            if lo is None or hi is None:
                raise SearchError(f'memory #{m["index"]}: none of its messages are in the archive yet (conversation {conv[:12]})')
        return DrillResult(m, self.store.seq_slice(conv, lo, hi), notes, hidden)


def find_memory(key: str, mems: list) -> dict:
    k = str(key).strip().lstrip('#')
    if k.isdigit():
        i = int(k)
        if i < len(mems):
            return mems[i]
        raise SearchError(f'no memory #{i}: there are {len(mems)} (#0-#{len(mems) - 1})')
    for m in mems:
        for end in (m['from_uuid'], m['to_uuid']):
            if end and (k == end or k.endswith(':' + end)):
                return m
    raise SearchError(f'no memory with from/to id {k[:80]} (use the #index overview shows)')
