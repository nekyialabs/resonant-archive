"""Integration tests against a real Postgres + pgvector (see pgfixture). Synthetic corpus only. Skipped, loudly, when no
Postgres with pgvector can be started and RESONANT_ARCHIVE_TEST_DSN is not set."""
import json
import os
import unittest

try:
    import psycopg
except ImportError:   # pragma: no cover
    psycopg = None
else:
    from psycopg.conninfo import make_conninfo

from resonant_archive.search import (FakeEmbedder, SearchConfig, SearchError, Searcher, envelope, open_readonly,
                                     render, run_recall)
from resonant_archive.search.engine import extract_anchors
from tests.search import pgfixture as fx

PG = None
DSNS = {}


def _dsn(base, dbname):
    return make_conninfo(base, dbname=dbname)


# On a shared server (RESONANT_ARCHIVE_TEST_DSN) the databases are named per process and dropped afterwards.
def _dbname(name):
    return f'ra_search_{name}_{os.getpid()}'


def setUpModule():
    global PG
    if psycopg is None:
        raise unittest.SkipTest('psycopg 3 not installed')
    try:
        PG = fx.TempPostgres().start()
    except Exception as e:   # pragma: no cover
        raise unittest.SkipTest(f'could not start Postgres: {e}')
    if PG is None:
        raise unittest.SkipTest('no Postgres binaries found and RESONANT_ARCHIVE_TEST_DSN not set')
    with psycopg.connect(PG.dsn, autocommit=True) as admin:
        for name, mem in (('plain', False), ('withmem', True)):
            admin.execute(f'drop database if exists {_dbname(name)} with (force)')
            admin.execute(f'create database {_dbname(name)}')
            DSNS[name] = _dsn(PG.dsn, _dbname(name))
    for name, mem in (('plain', False), ('withmem', True)):
        with psycopg.connect(DSNS[name], autocommit=True) as c:
            fx.load(c, extract_anchors, with_memories=mem)


def tearDownModule():
    if PG and not PG.owned:
        with psycopg.connect(PG.dsn, autocommit=True) as admin:
            for name in DSNS:
                admin.execute(f'drop database if exists {_dbname(name)} with (force)')
    if PG:
        PG.stop()


def unit_embedder(**kw):
    return FakeEmbedder(vectors={'castle geese': fx.axis(0), 'lake': fx.axis(0)}, default=fx.axis(100), **kw)


class Base(unittest.TestCase):
    db = 'plain'
    cfg = SearchConfig(timezone='UTC', recent_source='code')

    @classmethod
    def setUpClass(cls):
        cls.conn = psycopg.connect(DSNS[cls.db], autocommit=True)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def searcher(self, embedder=None, cfg=None):
        return Searcher(self.conn, embedder if embedder is not None else unit_embedder(), cfg or self.cfg)

    @staticmethod
    def ids(res):
        return [h.row['id'] for h in res.hits]


class Hybrid(Base):
    def test_three_signals_fuse(self):
        res = self.searcher().search('castle geese')
        self.assertEqual(set(res.signals), {'fts', 'vec', 'anchor'})
        self.assertFalse(res.vectors_skipped)
        top = self.ids(res)
        self.assertEqual(top[0], 'n1')            # words and meaning agree
        self.assertIn('n2', top[:3])
        self.assertNotIn('r1', top)               # live row (source "code", 1h old) held back
        self.assertEqual(res.excluded_recent, 1)
        text = render(res, self.cfg)
        self.assertIn('fts#', text)
        self.assertIn('excluded: 1 code rows from the last 6h', text)

    def test_include_recent(self):
        res = self.searcher().search('castle geese', include_recent=True)
        self.assertIn('r1', self.ids(res))
        self.assertEqual(res.excluded_recent, 0)

    def test_modes_isolate_signals(self):
        s = self.searcher()
        self.assertEqual(set(s.search('castle', mode='fts').signals), {'fts'})
        self.assertEqual(set(s.search('castle geese', mode='vector').signals), {'vec'})
        self.assertEqual(set(s.search('castle', mode='anchor').signals), {'anchor'})

    def test_fts_phrase_first(self):
        res = self.searcher().search('ride or die', mode='fts')
        self.assertEqual(self.ids(res)[0], 'a2')

    def test_limit(self):
        self.assertEqual(len(self.searcher().search('castle geese', limit=2).hits), 2)
        with self.assertRaises(SearchError):
            self.searcher().search('x', limit=0)

    def test_nothing_found_says_so(self):
        res = self.searcher(FakeEmbedder(down=True)).search('zzzqqq nothing', mode='fts')
        self.assertEqual(res.hits, [])
        self.assertIn('nothing found', render(res, self.cfg))


class Anchors(Base):
    def test_exact_path_and_version(self):
        s = self.searcher()
        self.assertEqual(self.ids(s.search('/srv/app/releases/current', mode='anchor'))[0], 'a1')
        self.assertEqual(self.ids(s.search('v2.3.1', mode='anchor')), ['a1'])
        self.assertEqual(self.ids(s.search('build_pipeline', mode='anchor')), ['a1'])

    def test_path_prefix_matches_longer_stored_paths(self):
        res = self.searcher().search('/srv/app/releases', mode='anchor')
        self.assertEqual(set(self.ids(res)), {'a1', 'a2'})


class Filtering(Base):
    def test_sources_read_from_database(self):
        s = self.searcher()
        self.assertEqual(s.sources(), ['code', 'chat-a', 'notes'] if False else sorted(['code', 'chat-a', 'notes']))
        res = s.search('geese', sources=['chat-a'], mode='fts')
        self.assertEqual(self.ids(res), ['b2'])

    def test_unknown_source_names_the_real_ones(self):
        with self.assertRaises(SearchError) as cm:
            self.searcher().search('geese', sources=['gpt'])
        self.assertIn('chat-a', str(cm.exception))

    def test_sources_fall_back_to_distinct_without_a_sources_table(self):
        with psycopg.connect(DSNS['plain'], autocommit=True) as c:
            c.execute('alter table sources rename to sources_off')
            try:
                self.assertEqual(Searcher(self.conn, None, self.cfg).sources(), ['chat-a', 'code', 'notes'])
            finally:
                c.execute('alter table sources_off rename to sources')

    def test_speaker(self):
        res = self.searcher().search('geese', speakers='assistant', mode='fts')
        self.assertEqual(set(self.ids(res)), {'n2', 'b2'})

    def test_date_range_uses_configured_timezone(self):
        # a1 is 2025-03-09 04:30 UTC = 2025-03-08 23:30 in New York
        q = dict(mode='fts', date_from='2025-03-09', date_to='2025-03-09')
        utc = self.searcher().search('deployed', **q)
        self.assertEqual(self.ids(utc), ['a1'])
        ny = self.searcher(cfg=SearchConfig(timezone='America/New_York', recent_source='code')).search('deployed', **q)
        self.assertEqual(ny.hits, [])
        ny2 = self.searcher(cfg=SearchConfig(timezone='America/New_York', recent_source='code')).search(
            'deployed', mode='fts', date_from='2025-03-08', date_to='2025-03-08')
        self.assertEqual(self.ids(ny2), ['a1'])

    def test_hidden_rows(self):
        s = self.searcher()
        self.assertEqual(s.search('exit', mode='fts').hits, [])
        self.assertEqual(self.ids(s.search('exit', mode='fts', hidden=True)), ['a3'])

    def test_recall_echo_never_returned_even_with_hidden(self):
        res = self.searcher().search('castle lake results', mode='fts', hidden=True)
        self.assertNotIn('c3', self.ids(res))

    def test_bad_dates(self):
        with self.assertRaises(SearchError):
            self.searcher().search('x', date_from='2025-04-01', date_to='2025-03-01')
        with self.assertRaises(SearchError):
            self.searcher().search('x', date_from='nope')


class EmbedderDown(Base):
    def test_down_embedder_falls_back_and_says_so(self):
        res = self.searcher(FakeEmbedder(down=True)).search('castle geese')
        self.assertTrue(res.vectors_skipped)
        self.assertEqual(self.ids(res)[0], 'n1')
        self.assertIn('skipped', res.signals['vec'])
        text = render(res, self.cfg)
        self.assertIn('vectors skipped', text)
        self.assertIn('full text and anchors only', text)

    def test_no_embedder_configured(self):
        res = Searcher(self.conn, None, self.cfg).search('castle geese')
        self.assertTrue(res.vectors_skipped)
        self.assertIn('no embedder configured', res.vectors_skipped_reason)

    def test_wrong_dimension_is_skipped_not_crashed(self):
        res = self.searcher(FakeEmbedder(dim=3)).search('castle geese')
        self.assertTrue(res.vectors_skipped)
        self.assertIn('768', res.vectors_skipped_reason)

    def test_vector_mode_fails_loudly_when_down(self):
        with self.assertRaises(SearchError):
            self.searcher(FakeEmbedder(down=True)).search('castle', mode='vector')

    def test_anchor_only_still_works_when_down(self):
        res = self.searcher(FakeEmbedder(down=True)).search('v2.3.1')
        self.assertIn('a1', self.ids(res))


class Around(Base):
    def test_neighbours_and_hidden_collapse(self):
        res = self.searcher().around('a2', before=1, after=2)
        text = render(res, self.cfg)
        self.assertIn('▶ a2', text)
        self.assertIn('[tool/system: 15 chars]', text)
        self.assertNotIn('exit 0', text)
        self.assertIn('exit 0', render(self.searcher().around('a2', before=1, after=2, hidden=True), self.cfg))

    def test_unknown_id(self):
        with self.assertRaises(SearchError):
            self.searcher().around('nope')

    def test_edges_say_start_and_end(self):
        text = render(self.searcher().around('n1', before=5, after=5), self.cfg)
        self.assertIn('from its start', text)
        self.assertIn('to its end', text)


class Overview(Base):
    def test_counts_without_memories(self):
        s = self.searcher()
        res = s.overview('2025-03-01', '2025-03-31')
        self.assertIsNone(res.memories)
        text = render(res, self.cfg)
        self.assertIn('notes: 4', text.replace('notes: 4 —', 'notes: 4'))
        self.assertIn('chat-a', text)
        self.assertNotIn('memories', text.lower())
        self.assertNotIn('code:', text)
        self.assertIn('(UTC)', text)

    def test_source_filter_and_unknown(self):
        text = render(self.searcher().overview('2025-01-01', '2025-12-31', sources='code'), self.cfg)
        self.assertNotIn('notes:', text)
        with self.assertRaises(SearchError):
            self.searcher().overview('2025-01-01', '2025-12-31', sources='gpt')

    def test_actions_hide_drill(self):
        self.assertEqual(self.searcher().available_actions(), ['search', 'around', 'overview', 'associate'])
        with self.assertRaises(SearchError):
            self.searcher().drill('#0')


class Associate(Base):
    def test_kin_outside_the_conversation(self):
        res = self.searcher().associate('n1', k=4)
        ids = self.ids(res)
        self.assertEqual(ids[0], 'b2')
        self.assertNotIn('n2', ids)       # same conversation
        self.assertNotIn('r1', ids)       # live row held back
        self.assertEqual(res.excluded_recent, 1)
        self.assertIn('cos 0.707', render(res, self.cfg))
        self.assertIn('r1', self.ids(self.searcher().associate('n1', k=4, include_recent=True)))

    def test_no_embedding_is_an_error(self):
        with self.assertRaises(SearchError) as cm:
            self.searcher().associate('e1')
        self.assertIn('no embedding', str(cm.exception))
        with self.assertRaises(SearchError):
            self.searcher().associate('a3')


class Safety(Base):
    def test_injection_shaped_query_is_just_text(self):
        evil = "x'); drop table messages; --"
        for mode in ('hybrid', 'fts', 'anchor'):
            res = self.searcher().search(evil, mode=mode)
            self.assertEqual(res.signals.get('fts', 0), 0)
            self.assertEqual(res.signals.get('anchor', 0), 0)
        self.assertEqual(self.conn.execute('select count(*) from messages').fetchone()[0], len(fx.corpus()))

    def test_injection_shaped_source_is_rejected(self):
        with self.assertRaises(SearchError):
            self.searcher().search('x', sources=["notes' or '1'='1"])

    def test_readonly_connection_cannot_write(self):
        ro = open_readonly(DSNS['plain'])
        try:
            self.assertTrue(Searcher(ro, unit_embedder(), self.cfg).search('castle geese').hits)
            with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
                ro.execute("delete from messages where id = 'n1'")
        finally:
            ro.close()


class Recall(Base):
    def test_text_output(self):
        code, out = run_recall(['search', 'castle geese', '--limit', '3'], self.conn, unit_embedder(), self.cfg)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('recall archive "castle geese"'))

    def test_json_contract(self):
        code, out = run_recall(['search', 'castle geese', '--json', '--source', 'notes'], self.conn, unit_embedder(), self.cfg)
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertTrue(d['ok'])
        self.assertEqual(d['action'], 'search')
        self.assertIn('recall archive', d['text'])
        data = d['data']
        self.assertEqual(data['hits'][0]['id'], 'n1')
        self.assertEqual(data['hits'][0]['source'], 'notes')
        self.assertFalse(data['vectors_skipped'])
        self.assertEqual(data['filters']['sources'], ['notes'])
        self.assertRegex(data['hits'][0]['ts'], r'^2025-03-02T10:00:00')

    def test_json_reports_skipped_vectors(self):
        code, out = run_recall(['--json', 'search', 'castle geese'], self.conn, FakeEmbedder(down=True), self.cfg)
        d = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(d['data']['vectors_skipped'])
        self.assertIn('vectors skipped', d['text'])

    def test_json_errors_are_json(self):
        for argv, kind in ((['search', 'q', '--source', 'gpt', '--json'], 'usage'), (['around', 'nope', '--json'], 'usage'),
                           (['search', 'q', '--limit', '99', '--json'], 'usage'), (['drill', '#0', '--json'], 'usage')):
            code, out = run_recall(argv, self.conn, unit_embedder(), self.cfg)
            d = json.loads(out)
            self.assertEqual((code, d['ok'], d['kind']), (2, False, kind), argv)

    def test_text_error(self):
        code, out = run_recall(['around', 'nope'], self.conn, unit_embedder(), self.cfg)
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith('recall failed: no message with id nope'))

    def test_every_action_has_json(self):
        for argv in (['around', 'n1'], ['overview', '--from', '2025-03-01', '--to', '2025-03-31'], ['associate', 'n1']):
            code, out = run_recall(argv + ['--json'], self.conn, unit_embedder(), self.cfg)
            self.assertEqual(code, 0, argv)
            d = json.loads(out)
            self.assertTrue(d['ok'] and d['data'])


class WithMemories(Base):
    db = 'withmem'

    def test_actions_include_drill(self):
        self.assertEqual(self.searcher().available_actions(), ['search', 'around', 'overview', 'associate', 'drill'])

    def test_overview_lists_memories(self):
        text = render(self.searcher().overview('2025-01-01', '2025-12-31'), self.cfg)
        self.assertIn('memories (3', text)
        self.assertIn('#0 2025-03-09', text)       # indexes run by time
        self.assertIn('#1 2025-05-05', text)
        self.assertIn('#2 2025-05-07', text)     # null t_from falls back to the memory's own ts
        only = render(self.searcher().overview('2025-05-01', '2025-05-31', memories='only'), self.cfg)
        self.assertIn('Renamed the config loader', only)
        self.assertNotIn('visible messages', only)
        none = render(self.searcher().overview('2025-01-01', '2025-12-31', memories='none'), self.cfg)
        self.assertNotIn('Renamed', none)

    def test_drill_by_index_and_by_message_id(self):
        s = self.searcher()
        text = render(s.drill('#1'), self.cfg)
        self.assertIn('drill #1', text)
        self.assertIn('c1 ·', text)
        self.assertIn('c2 ·', text)
        self.assertEqual(s.drill('c1').memory['id'], 'm1')

    def test_drill_finds_prefixed_message_ids(self):
        res = self.searcher().drill('uu-7')          # message id is 'code:uu-7'
        self.assertEqual(res.memory['id'], 'm3')
        self.assertEqual([r['id'] for r in res.rows], ['c2', 'c3', 'code:uu-7'])

    def test_drill_with_missing_endpoint_uses_time_span(self):
        res = self.searcher().drill('#0')
        self.assertTrue(any('not in the archive' in n for n in res.notes))
        self.assertTrue(res.rows)

    def test_drill_unknown(self):
        with self.assertRaises(SearchError):
            self.searcher().drill('#9')

    def test_recall_drill_json(self):
        code, out = run_recall(['drill', '#1', '--json'], self.conn, unit_embedder(), self.cfg)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['data']['memory']['id'], 'm1')


if __name__ == '__main__':
    unittest.main()
