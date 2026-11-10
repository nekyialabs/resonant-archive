"""All SQL for the embedding backfill lives here, and only here.

Written against migration 0001 (docs/SCHEMA.md is the contract):

    messages(id text, text, hidden boolean, embedding vector(768), embed_status, embedded_hash text, embedded_at)

What the backfill does with that schema:
  * work = visible (``hidden`` false), non-blank rows whose ``embedding`` is NULL or whose ``embedded_hash`` is not
    the sha1 of the current ``text``. Hidden rows are inserted ``skipped`` by the importers and are never embedded.
  * after a vector is stored the row is stamped ``embed_status = 'done'``, ``embedded_hash``, ``embedded_at``.
  * a visible row whose text is blank is moved from ``pending`` to ``skipped`` so it does not count as outstanding.
  * the HNSW index already exists (``messages_embedding``, created by 0001); ``build_index`` only creates one when
    none is present on ``messages.embedding``.

Message text is never logged.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Callable, Sequence

from .nomic import FULL_DIM

log = logging.getLogger("resonant_archive.embed")

TABLE = "messages"
REQUIRED_COLUMNS = ("id", "text", "hidden", "embedding", "embed_status", "embedded_hash", "embedded_at")


class SchemaProblem(RuntimeError):
    """The target table is not shaped the way the backfill expects."""


class PgBackfillStore:
    """Postgres side of the backfill. Takes an open psycopg 3 connection (autocommit off)."""

    def __init__(self, conn, table: str = TABLE) -> None:
        from psycopg import sql

        self._sql = sql
        self.conn = conn
        self.table = table
        self.T = sql.Identifier(table)
        self._staged = False

    # -- preconditions ------------------------------------------------------
    def check_ready(self) -> None:
        sql = self._sql
        enc = self.conn.execute("SHOW server_encoding").fetchone()[0].upper()
        if enc not in ("UTF8", "UTF-8"):
            self.conn.rollback()
            raise SchemaProblem("server_encoding is not UTF8: md5(text) would not match Python's UTF-8 hash")
        rows = self.conn.execute(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), t.typname "
            "FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid "
            "WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped",
            (sql.Identifier(self.table).as_string(self.conn),),
        ).fetchall()
        self.conn.commit()
        cols = {n: (fmt, typ) for n, fmt, typ in rows}
        for need in REQUIRED_COLUMNS:
            if need not in cols:
                raise SchemaProblem(f"table {self.table!r} lacks column {need!r}")
        if cols["embedding"][0] != f"vector({FULL_DIM})":
            raise SchemaProblem(f"embedding column is {cols['embedding'][0]}, expected vector({FULL_DIM})")
        for col, want in (("id", "text"), ("embedded_hash", "text")):
            if cols[col][0] != want:
                raise SchemaProblem(f"{col} column is {cols[col][0]}, expected {want}")

    def try_lock(self) -> bool:
        """Session-level advisory lock so two backfills never run at once."""
        got = self.conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))",
                                (f"resonant-archive:embed:{self.table}",)).fetchone()[0]
        self.conn.commit()
        return bool(got)

    # -- reads --------------------------------------------------------------
    def scan_pending(self, hash_fn: Callable[[str], str]) -> tuple[list[tuple], int]:
        """Return ([(id, chars)] shortest first, blank_count) for visible rows needing a vector.

        Pending = embedding IS NULL, or embedded_hash differs from hash_fn(text). Done with a
        server-side cursor; the text itself never leaves this function.
        """
        sql = self._sql
        out: list[tuple] = []
        blank = 0
        with self.conn.cursor(name="resonant_embed_scan") as cur:
            cur.itersize = 2000
            cur.execute(sql.SQL("SELECT id, embedded_hash, embedding IS NULL, text FROM {} "
                                "WHERE hidden IS FALSE").format(self.T))
            for rid, stored, missing, txt in cur:
                if not txt or not txt.strip():
                    blank += 1
                    continue
                if not missing and stored == hash_fn(txt):
                    continue
                out.append((rid, len(txt)))
        self.conn.commit()
        out.sort(key=lambda r: (r[1], str(r[0])))
        return out, blank

    def mark_blank_skipped(self) -> int:
        """Visible rows with blank text can never get a vector: move them out of ``pending``."""
        cur = self.conn.execute(self._sql.SQL(
            "UPDATE {} SET embed_status = 'skipped' WHERE hidden IS FALSE AND embed_status = 'pending' "
            "AND btrim(text) = ''").format(self.T))
        n = cur.rowcount
        self.conn.commit()
        return n

    def fetch_texts(self, ids: Sequence) -> list[tuple]:
        sql = self._sql
        rows = self.conn.execute(
            sql.SQL("SELECT id, text FROM {} WHERE id = ANY(%s) AND hidden IS FALSE").format(self.T), (list(ids),)
        ).fetchall()
        return [(rid, txt) for rid, txt in rows if txt and txt.strip()]

    # -- writes -------------------------------------------------------------
    def write_vectors(self, rows: Sequence[tuple], hash_fn: Callable[[str], str]) -> int:
        """Store ``[(id, text, vector)]``. Returns rows actually updated.

        Each UPDATE re-checks md5(text) against the md5 of the text that was embedded, so a row
        edited mid-run is left pending rather than stamped with a stale vector.
        """
        sql = self._sql
        if not self._staged:
            self.conn.execute(sql.SQL(
                "CREATE TEMP TABLE IF NOT EXISTS embed_stage (id text, h text, m text, e vector({})) "
                "ON COMMIT DELETE ROWS").format(sql.SQL(str(FULL_DIM))))
            self._staged = True
        update = sql.SQL("UPDATE {} t SET embedding = s.e, embedded_hash = s.h, embed_status = 'done', "
                         "embedded_at = now() FROM embed_stage s WHERE t.id = s.id AND md5(t.text) = s.m"
                         ).format(self.T)
        with self.conn.cursor() as cur:
            with cur.copy("COPY embed_stage (id, h, m, e) FROM STDIN") as cp:
                for rid, txt, vec in rows:
                    cp.write_row((rid, hash_fn(txt), hashlib.md5(txt.encode("utf-8")).hexdigest(),
                                  "[" + ",".join(map("{:.9g}".format, vec)) + "]"))
            cur.execute(update)
            updated = cur.rowcount
        self.conn.commit()
        return updated

    def rollback(self) -> None:
        self.conn.rollback()

    def drop_index(self) -> int:
        """Drop every HNSW index on ``embedding`` (for a rebuild around a bulk load). Returns how many were dropped."""
        sql = self._sql
        names = [r[0] for r in self.conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND tablename = %s "
            "AND indexdef ILIKE '%%USING hnsw (embedding %%'", (self.table,)).fetchall()]
        for name in names:
            self.conn.execute(sql.SQL("DROP INDEX {}").format(sql.Identifier(name)))
        self.conn.commit()
        if names:
            log.info("dropped HNSW index %s before the bulk load; it is rebuilt afterwards", ", ".join(names))
        return len(names)

    def build_index(self, maintenance_work_mem: str = "1GB") -> bool:
        """Create an HNSW index on ``embedding`` if none exists. Returns True if one was built now.

        Migration 0001 already creates ``messages_embedding``, so on a normal install this only reports that.
        """
        sql = self._sql
        exists = self.conn.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND tablename = %s "
            "AND indexdef ILIKE '%%USING hnsw (embedding %%'", (self.table,)).fetchone()
        self.conn.commit()
        if exists:
            log.info("HNSW index on %s.embedding already present (kept current by updates)", self.table)
            return False
        name = f"{self.table}_embedding"
        log.info("building HNSW index %s (vector_cosine_ops), maintenance_work_mem=%s", name, maintenance_work_mem)
        t0 = time.time()
        self.conn.execute(sql.SQL("SET maintenance_work_mem = {}").format(sql.Literal(maintenance_work_mem)))
        self.conn.execute("SET max_parallel_maintenance_workers = 2")
        self.conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} USING hnsw (embedding vector_cosine_ops)")
                          .format(sql.Identifier(name), self.T))
        self.conn.commit()
        log.info("index ready in %.0fs", time.time() - t0)
        return True


def connect(dsn: str):
    """Open a psycopg 3 connection for the backfill (autocommit off)."""
    import psycopg

    from ..config import connect_kwargs

    conn = psycopg.connect(dsn, autocommit=False, **connect_kwargs(dsn))
    conn.add_notice_handler(lambda d: log.info("postgres notice: %s", d.message_primary))
    return conn
