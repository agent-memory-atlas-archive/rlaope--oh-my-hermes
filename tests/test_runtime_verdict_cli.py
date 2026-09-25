"""`omh runtime verdict show`: a later reader of declared verdicts, never a writer.

The store is `native_completion/v1` (#1782, landed through #1818): `omh_todo
action=record` persists what a model declared about a verification, review or
QA result, and `action=recall` reads it inside a session. This CLI reads the
same store from outside one. What it has to hold:

- the `prepared_not_observed` / `observed` standing a writer claimed survives
  the read unchanged, and every row still says `standing=model_declaration`,
  `observed=false`, as does the payload itself on every status, so nothing
  stored reads back as evidence that work happened;
- "no record" and "a record saying nothing was found" are distinct at the read
  (`store_state` absent/empty/present, `sources` per kind);
- freshness is judged only against a binding the reader stated; without one it
  is `unbound` and no completion judgment is made;
- a store that cannot be read is `malformed` with a non-zero exit, and the read
  never writes or creates anything.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from _cli_harness import run_cli
from omh.plugin_bundle.omh import completion_store as store
from omh.plugin_bundle.omh.tools.todo_tool import omh_todo_handler


class RuntimeVerdictCliTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / 'omh'
        self.hermes = self.root / 'hermes'
        env = patch.dict(os.environ, {'OMH_HOME': str(self.home), 'HERMES_HOME': str(self.hermes)})
        env.start()
        self.addCleanup(env.stop)
        cwd = patch('omh.plugin_bundle.omh.runtime_paths.runtime_cwd', return_value=self.root)
        cwd.start()
        self.addCleanup(cwd.stop)
        self.base = ['--omh-home', str(self.home), '--hermes-home', str(self.hermes),
                     'runtime', 'verdict', 'show']

    def tool(self, action, session='parent', **args):
        return json.loads(omh_todo_handler({'action': action, **args}, session_id=session))

    def checkpoint(self):
        written = self.tool('set', items=[{'text': 'Parser fix', 'state': 'active'},
                                          {'text': 'Regression test'}])
        self.assertEqual(written['status'], 'written', written)
        result = self.tool('checkpoint', accepted=True, revision='rev-a', environment='offline-fixture')
        self.assertEqual(result['status'], 'written', result)
        return result['checkpoint']['checkpoint_id']

    def record(self, key, session='parent', **changes):
        row = {'kind': 'verification', 'item': 1, 'verdict': 'PASS',
               'summary': 'Tests returned exit zero', 'findings': [],
               'claimed_source': 'host_exit', 'claimed_evidence_state': 'observed',
               'references': [], **changes}
        result = self.tool('record', session=session, checkpoint_id=key,
                           revision='rev-a', environment='offline-fixture', result=row)
        self.assertEqual(result['status'], 'written', result)

    def show(self, *extra):
        status, stdout, stderr = run_cli(self.base + list(extra))
        return status, (json.loads(stdout) if stdout else None), stderr

    def test_absent_store_is_absent_not_clean_and_nothing_is_created(self):
        status, payload, stderr = self.show()
        self.assertEqual((status, stderr), (0, ''))
        self.assertEqual(payload['schema_version'], 'native_completion_read/v1')
        self.assertEqual(payload['status'], 'read')
        self.assertEqual(payload['store_state'], 'absent')
        self.assertEqual(payload['checkpoint_count'], 0)
        self.assertEqual(payload['dossiers'], [])
        self.assertIsNone(payload['binding'])
        self.assertEqual(payload['standing'], 'model_declaration')
        self.assertIn('not permission or observed execution', payload['claim_boundary'])
        self.assertFalse(self.home.exists())

    def test_empty_store_is_empty_not_absent(self):
        path = self.home / 'runtime' / 'completion' / 'records.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'schema_version': 'native_completion/v1', 'checkpoints': []}))
        status, payload, _ = self.show()
        self.assertEqual(status, 0)
        self.assertEqual(payload['store_state'], 'empty')
        self.assertEqual(payload['checkpoint_count'], 0)
        self.assertEqual(payload['dossiers'], [])

    # The three tests named after the issue's acceptance lines (#1782).

    def test_a_verdict_a_review_finding_set_and_a_qa_result_written_by_one_session_can_be_read_by_a_later_one_with_their_standing_intact(self):
        key = self.checkpoint()
        self.record(key)
        self.record(key, kind='review', verdict='HOLD', findings=['Missing error check'],
                    claimed_source='independent_review',
                    claimed_evidence_state='prepared_not_observed')
        self.record(key, kind='qa', claimed_source='model',
                    claimed_evidence_state='prepared_not_observed')
        # The declaring session's plan is gone; the dossier is not.
        self.assertEqual(self.tool('clear')['status'], 'cleared')
        status, payload, _ = self.show()
        self.assertEqual(status, 0)
        self.assertEqual(payload['store_state'], 'present')
        self.assertEqual(payload['checkpoint_count'], 1)
        self.assertIsNone(payload['binding'])
        self.assertEqual(payload['standing'], 'model_declaration')
        [dossier] = payload['dossiers']
        self.assertEqual(dossier['freshness'], 'unbound')
        self.assertNotIn('completion', dossier)
        self.assertEqual(dossier['sources'], {'verification': 'declared_no_findings',
                                              'review': 'declared_findings',
                                              'qa': 'declared_no_findings'})
        self.assertLessEqual(set(dossier['sources'].values()), set(store.SOURCE_STATES))
        rows = dossier['checkpoint']['results']
        self.assertEqual([r['kind'] for r in rows], ['verification', 'review', 'qa'])
        self.assertEqual([r['claimed_evidence_state'] for r in rows],
                         ['observed', 'prepared_not_observed', 'prepared_not_observed'])
        self.assertEqual([r['verdict'] for r in rows], ['PASS', 'HOLD', 'PASS'])
        self.assertEqual(rows[1]['findings'], ['Missing error check'])
        for row in rows:
            self.assertEqual(row['standing'], 'model_declaration')
            self.assertIs(row['observed'], False)
            self.assertEqual(row['freshness'], 'unbound')
        # The later session reads the same rows through the tool as well.
        recall = self.tool('recall', session='later', checkpoint_id=key,
                           revision='rev-a', environment='offline-fixture')
        self.assertEqual(recall['status'], 'current', recall)
        self.assertEqual([r['claimed_evidence_state'] for r in recall['checkpoint']['results']],
                         ['observed', 'prepared_not_observed', 'prepared_not_observed'])

    def test_no_record_and_a_record_saying_nothing_was_found_are_distinguishable_at_the_read(self):
        # Five reads a transcript renders alike as "no review findings to
        # cite", kept apart by the store's own vocabulary.
        store_states, source_states = [], []
        _, payload, _ = self.show()
        store_states.append(payload['store_state'])                  # nothing was ever declared
        path = self.home / 'runtime' / 'completion' / 'records.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'schema_version': 'native_completion/v1', 'checkpoints': []}))
        _, payload, _ = self.show()
        store_states.append(payload['store_state'])                  # a store, with nothing in it
        key = self.checkpoint()
        _, payload, _ = self.show()
        store_states.append(payload['store_state'])
        [dossier] = payload['dossiers']
        source_states.append(dossier['sources']['review'])           # a scope, and no review of it
        self.record(key, kind='review', verdict='PASS', findings=[], claimed_source='independent_review')
        _, payload, _ = self.show()
        [dossier] = payload['dossiers']
        source_states.append(dossier['sources']['review'])           # a review that found nothing
        self.record(key, kind='review', verdict='HOLD', findings=['Unchecked return'],
                    claimed_source='independent_review')
        _, payload, _ = self.show()
        [dossier] = payload['dossiers']
        source_states.append(dossier['sources']['review'])           # a review that found something
        _, payload, _ = self.show('--revision', 'rev-b', '--environment', 'offline-fixture')
        [dossier] = payload['dossiers']
        source_states.append(dossier['sources']['review'])           # declared, for another revision
        self.assertEqual(store_states, ['absent', 'empty', 'present'])
        self.assertEqual(source_states, ['absent', 'declared_no_findings', 'declared_findings', 'stale'])
        self.assertEqual(set(store_states), set(store.STORE_STATES))
        self.assertEqual(set(source_states), set(store.SOURCE_STATES))
        # The stale read still carries the rows: the state is said on the
        # source, never by dropping what was declared.
        self.assertEqual(len(dossier['checkpoint']['results']), 2)

    def test_nothing_stored_can_be_read_back_as_evidence_that_work_happened_only_that_it_was_claimed(self):
        key = self.checkpoint()
        # The strongest claim a writer can make: every item PASS, every row
        # claimed observed, from the host's exit, an independent review and CI.
        self.record(key, item=1)
        self.record(key, item=2)
        self.record(key, kind='review', item=1, claimed_source='independent_review')
        self.record(key, kind='qa', item=1, claimed_source='ci')
        status, payload, _ = self.show('--revision', 'rev-a', '--environment', 'offline-fixture')
        self.assertEqual(status, 0)
        self.assertEqual(payload['standing'], 'model_declaration')
        self.assertIn('not permission or observed execution', payload['claim_boundary'])
        [dossier] = payload['dossiers']
        # Complete declarations is the most the read will say; verified, never.
        self.assertIs(dossier['completion']['declarations_complete'], True)
        self.assertEqual(dossier['completion']['status'], 'not_verified')
        self.assertEqual(len(dossier['checkpoint']['results']), 4)
        for row in dossier['checkpoint']['results']:
            self.assertEqual(row['claimed_evidence_state'], 'observed')
            self.assertEqual(row['standing'], 'model_declaration')
            self.assertIs(row['observed'], False)
        self.assertNotIn('"observed": true', json.dumps(payload))
        # The one way to put `observed: true` in the store is to edit the
        # file, and that reads as a malformed store, not as evidence.
        path = self.home / 'runtime' / 'completion' / 'records.json'
        tampered = json.loads(path.read_text())
        tampered['checkpoints'][0]['results'][0]['observed'] = True
        path.write_text(json.dumps(tampered))
        status, payload, _ = self.show('--revision', 'rev-a', '--environment', 'offline-fixture')
        self.assertEqual((status, payload['status']), (1, 'malformed'))
        self.assertEqual(payload['standing'], 'model_declaration')
        self.assertNotIn('dossiers', payload)

    def test_bound_read_judges_freshness_against_the_stated_revision(self):
        key = self.checkpoint()
        self.record(key)
        status, payload, _ = self.show('--revision', 'rev-a', '--environment', 'offline-fixture')
        self.assertEqual(status, 0)
        self.assertEqual(payload['binding'], {'revision': 'rev-a', 'environment': 'offline-fixture'})
        [dossier] = payload['dossiers']
        self.assertEqual(dossier['freshness'], 'current')
        self.assertEqual(dossier['sources']['verification'], 'declared_no_findings')
        self.assertEqual(dossier['completion'], {'status': 'not_verified', 'missing': [2],
                                                 'blockers': [], 'declarations_complete': False})
        self.assertEqual(dossier['checkpoint']['results'][0]['freshness'], 'current')
        # The revision moved: the same row is stale at once, and the store's
        # own vocabulary says so on the source, never by dropping the row.
        status, payload, _ = self.show('--revision', 'rev-b', '--environment', 'offline-fixture')
        self.assertEqual(status, 0)
        [dossier] = payload['dossiers']
        self.assertEqual(dossier['freshness'], 'stale')
        self.assertEqual(dossier['sources']['verification'], 'stale')
        self.assertEqual(dossier['checkpoint']['results'][0]['freshness'], 'stale')
        self.assertEqual(dossier['completion']['missing'], [1, 2])

    def test_bound_read_agrees_with_the_tool_recall_field_for_field(self):
        key = self.checkpoint()
        self.record(key)
        self.record(key, kind='review', verdict='BLOCK', findings=['Unchecked return'])
        recall = self.tool('recall', session='later', checkpoint_id=key,
                           revision='rev-a', environment='offline-fixture')
        self.assertEqual(recall['status'], 'current', recall)
        _, payload, _ = self.show('--revision', 'rev-a', '--environment', 'offline-fixture')
        [dossier] = payload['dossiers']
        self.assertEqual(dossier['checkpoint'], recall['checkpoint'])
        self.assertEqual(dossier['sources'], recall['sources'])
        self.assertEqual(dossier['completion'], recall['completion'])
        self.assertEqual(payload['claim_boundary'], recall['claim_boundary'])

    def test_half_binding_is_refused(self):
        for extra in (['--revision', 'rev-a'], ['--environment', 'offline-fixture']):
            with self.subTest(extra=extra):
                status, payload, stderr = self.show(*extra)
                self.assertEqual(status, 2)
                self.assertIsNone(payload)
                self.assertIn('bind together', stderr)

    def test_session_and_checkpoint_filters_keep_the_store_state(self):
        key = self.checkpoint()
        self.record(key, session='later')
        cases = ((['--session', 'parent'], 1), (['--session', 'later'], 1),
                 (['--session', 'stranger'], 0), (['--checkpoint', key], 1),
                 (['--checkpoint', 'a' * 32], 0))
        for extra, count in cases:
            with self.subTest(extra=extra):
                status, payload, _ = self.show(*extra)
                self.assertEqual(status, 0)
                # A filter that matches nothing is not an absent store.
                self.assertEqual(payload['store_state'], 'present')
                self.assertEqual(payload['checkpoint_count'], 1)
                self.assertEqual(len(payload['dossiers']), count)
        status, payload, stderr = self.show('--checkpoint', 'not-an-id')
        self.assertEqual(status, 2)
        self.assertIsNone(payload)
        self.assertIn('Invalid checkpoint id', stderr)

    def test_malformed_store_exits_nonzero_and_is_left_as_found(self):
        key = self.checkpoint()
        self.record(key)
        path = self.home / 'runtime' / 'completion' / 'records.json'
        tampered = json.loads(path.read_text())
        tampered['checkpoints'][0]['results'][0]['observed'] = True
        for corrupt in ('{broken', json.dumps(tampered)):
            with self.subTest(corrupt=corrupt[:8]):
                path.write_text(corrupt)
                status, payload, _ = self.show()
                self.assertEqual(status, 1)
                self.assertEqual(payload['status'], 'malformed')
                self.assertEqual(payload['standing'], 'model_declaration')
                self.assertNotIn('dossiers', payload)
                self.assertNotIn('store_state', payload)
                self.assertEqual(path.read_text(), corrupt)

    def test_reader_redacts_like_the_tool_and_writes_nothing(self):
        key = self.checkpoint()
        self.record(key)
        path = self.home / 'runtime' / 'completion' / 'records.json'
        secret = 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456'
        data = json.loads(path.read_text())
        data['checkpoints'][0]['results'][0]['summary'] = secret
        path.write_text(json.dumps(data))
        before = path.read_bytes()
        status, payload, _ = self.show()
        self.assertEqual(status, 0)
        self.assertNotIn(secret, json.dumps(payload))
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
