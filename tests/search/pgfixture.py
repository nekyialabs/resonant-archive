"""A throwaway real Postgres + pgvector for the integration tests, and a synthetic corpus. Synthetic data only.

Uses RESONANT_ARCHIVE_TEST_DSN if set (an admin DSN on a server with pgvector, the same one tests/conftest.py uses; the
older RESONANT_TEST_DSN still works). Otherwise starts a private
cluster with the local `initdb` / `pg_ctl` (found next to `pg_config`, in /usr/lib/postgresql/*/bin, or on PATH) on a
unix socket in a temp dir, and stops it afterwards. Returns None when neither is available (tests then skip).
"""
from __future__ import annotations

import datetime
import glob
import os
import shutil
import subprocess
import tempfile

DIM = 768
UTC = datetime.timezone.utc

# The schema the search engine is written against is the real one: every shipped migration, in order, so this
# fixture can never drift from resonant_archive/migrations/ (docs/SCHEMA.md is the contract).
def _schema() -> str:
    from resonant_archive import db
    return ("create extension if not exists vector;\ncreate extension if not exists pg_trgm;\n"
            + "\n".join(m.sql for m in db.migrations()))


DDL = _schema()


def _bin_dirs():
    dirs = sorted(glob.glob('/usr/lib/postgresql/*/bin'), reverse=True)
    pg_config = shutil.which('pg_config')
    if pg_config:
        try:
            dirs.insert(0, subprocess.check_output([pg_config, '--bindir'], text=True).strip())
        except Exception:
            pass
    return dirs


def _find(tool):
    for d in _bin_dirs():
        p = os.path.join(d, tool)
        if os.path.exists(p):
            return p
    return shutil.which(tool)


class TempPostgres:
    def __init__(self):
        self.dir = None
        self.dsn = None
        self.owned = False

    def start(self):
        env = os.environ.get('RESONANT_ARCHIVE_TEST_DSN') or os.environ.get('RESONANT_TEST_DSN')
        if env:
            self.dsn = env
            return self
        initdb, pg_ctl = _find('initdb'), _find('pg_ctl')
        if not initdb or not pg_ctl:
            return None
        self.dir = tempfile.mkdtemp(prefix='ra-test-pg-')
        data = os.path.join(self.dir, 'data')
        sock = os.path.join(self.dir, 'sock')
        os.mkdir(sock)
        subprocess.run([initdb, '-D', data, '-A', 'trust', '-U', 'test', '--no-sync'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        opts = f"-c listen_addresses='' -c unix_socket_directories='{sock}' -c fsync=off -c port=54398"
        subprocess.run([pg_ctl, '-D', data, '-o', opts, '-l', os.path.join(self.dir, 'log'), '-w', 'start'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.owned = True
        self.pg_ctl, self.data = pg_ctl, data
        self.dsn = f'host={sock} port=54398 user=test dbname=postgres'
        return self

    def stop(self):
        if self.owned:
            subprocess.run([self.pg_ctl, '-D', self.data, '-m', 'immediate', 'stop'], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)


def axis(i: int, w: float = 1.0, j: int | None = None, w2: float = 0.0):
    """A 768-dim vector with weight on axis i (and optionally j)."""
    v = [0.0] * DIM
    v[i] = w
    if j is not None:
        v[j] = w2
    return v


def lit(v):
    return '[' + ','.join(repr(float(x)) for x in v) + ']'


NOW = datetime.datetime.now(UTC).replace(microsecond=0)


def d(y, m, dd, h=12, mi=0):
    return datetime.datetime(y, m, dd, h, mi, tzinfo=UTC)


# (id, source, conv, title, ts, speaker, text, hidden, tags, embedding-or-None)
def corpus():
    c1, c2, c3, c4 = 'conv-notes-1', 'conv-chat-1', 'conv-chat-2', 'conv-code-1'
    rows = [
        ('n1', 'notes', c1, 'Lake trip', d(2025, 3, 2, 10), 'user', 'We saw the castle by the lake and fed the geese.', False, [], axis(0)),
        ('n2', 'notes', c1, 'Lake trip', d(2025, 3, 2, 10, 5), 'assistant', 'The geese were loud near the old jetty.', False, [], axis(0, 1.0, 1, 0.3)),
        ('n3', 'notes', c1, 'Lake trip', d(2025, 3, 2, 10, 9), 'user', 'Packing list: bread, camera, rain jacket.', False, ['edited'], axis(2)),
        ('a1', 'chat-a', c2, 'Deploy notes', d(2025, 3, 9, 4, 30), 'user', 'Deployed build_pipeline v2.3.1 from /srv/app/releases/current today.', False, [], axis(3)),
        ('a2', 'chat-a', c2, 'Deploy notes', d(2025, 3, 9, 4, 40), 'assistant', 'Rollback plan is in /srv/app/releases/previous/README.md, see "ride-or-die" runbook.', False, [], axis(3, 1.0, 4, 0.2)),
        ('a3', 'chat-a', c2, 'Deploy notes', d(2025, 3, 9, 4, 41), 'tool', 'exit 0 ok ok ok', True, [], None),
        ('a4', 'chat-a', c2, 'Deploy notes', d(2025, 3, 9, 4, 50), 'user', 'Thanks. Next the migration for the invoices table.', False, [], axis(5)),
        ('b1', 'chat-a', c3, 'Cooking', d(2025, 11, 2, 1, 30), 'user', 'Slow braise: onions, thyme, and a long simmer.', False, [], axis(6)),
        ('b2', 'chat-a', c3, 'Cooking', d(2025, 11, 2, 1, 35), 'assistant', 'Add the thyme late; the geese recipe wants butter.', False, [], axis(0, 0.6, 6, 0.6)),
        ('c1', 'code', c4, 'Refactor', d(2025, 5, 5, 9), 'user', 'Rename parseConfigFile to load_config and update imports.', False, [], axis(7)),
        ('c2', 'code', c4, 'Refactor', d(2025, 5, 5, 9, 2), 'assistant', 'Done: renamed parseConfigFile across 4 files.', False, [], axis(7, 1.0, 8, 0.2)),
        ('c3', 'code', c4, 'Refactor', d(2025, 5, 5, 9, 3), 'tool', 'mcp__chronicle__recall castle by the lake results', True, [], None),
        # a live-conversation row: source "code", 1 hour old
        ('r1', 'code', 'conv-code-live', 'Live', NOW - datetime.timedelta(hours=1), 'user', 'castle geese live conversation right now', False, [], axis(0, 1.0, 9, 0.1)),
        ('e1', 'notes', 'conv-notes-empty-vec', 'No vector yet', d(2025, 3, 3, 8), 'user', 'castle notes without a vector yet', False, [], None),
    ]
    return rows


def load(conn, extract, with_memories=False, with_sources_table=True):
    with conn.cursor() as cur:
        cur.execute(DDL)
        seq = {}
        for (mid, src, conv, title, ts, sp, text, hid, tags, emb) in corpus():
            seq[conv] = seq.get(conv, -1) + 1
            cur.execute("""insert into messages (id, source, conv_id, conv_title, ts, speaker, context, text, hidden, tags,
                              seq, embedding) values (%s,%s,%s,%s,%s,%s,'default',%s,%s,%s,%s,%s::vector)""",
                        (mid, src, conv, title, ts, sp, text, hid, tags, seq[conv], lit(emb) if emb else None))
            if not hid:
                for tok, kind in extract(text):
                    cur.execute('insert into anchors (token, kind, msg_id, ts) values (%s,%s,%s,%s)', (tok, kind, mid, ts))
        cur.execute("update messages set embed_status = 'done' where embedding is not null")
        if with_sources_table:
            cur.execute("""insert into sources (importer, unit_key, label, content_hash, n_messages)
                           select 'test', source, source, 'x', count(*) from messages group by source""")
        if with_memories:
            cur.execute("insert into messages (id, source, conv_id, conv_title, ts, speaker, context, text, hidden, seq) "
                        "values ('code:uu-7', 'code', 'conv-code-1', 'Refactor', %s, 'assistant', 'default', 'Tests pass.', false, 3)",
                        (d(2025, 5, 5, 9, 4),))
            cur.execute("""insert into memories (id, source, sid, ts, t_from, t_to, from_uuid, to_uuid, text) values
                           ('m1','code','conv-code-1',%s,%s,%s,'c1','c2','Renamed the config loader.'),
                           ('m2','chat-a','conv-chat-1',%s,%s,%s,'a1','missing-id','Deployed the pipeline.'),
                           ('m3','code','conv-code-1',%s,null,null,'c2','uu-7','Finished the rename.')""",
                        (d(2025, 5, 6), d(2025, 5, 5, 9), d(2025, 5, 5, 9, 3), d(2025, 3, 10), d(2025, 3, 9, 4, 30),
                         d(2025, 3, 9, 4, 50), d(2025, 5, 7)))
