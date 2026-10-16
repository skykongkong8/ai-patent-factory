"""Public synthetic fixtures exercise scope selection through the real CLI."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from patent_factory.cli import main


class ManagedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.previous)
        self.addCleanup(self.temp.cleanup)
        self.call('init')
        Path('documents/base.md').write_text('name: Example\nexpertise: sensors\ntechnical_domain: electronics\nproject_summary: reduce drift\n')
        Path('documents/topic.md').write_text('project_summary: measure low current\nconstraints: offline\n')

    def call(self, *args, code=0):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            actual = main(list(map(str, args)))
        value = json.loads(output.getvalue())
        self.assertEqual(actual, code, value)
        self.assertEqual(value['schema_version'], 'cli-result-v1')
        return value

    def inventor(self):
        return self.call('inventor', 'create', '--name', 'Example')['inventor_id']

    def configured(self):
        inventor = self.inventor()
        profile = self.call('profile', 'document', 'documents/base.md', '--inventor', inventor)
        topic = self.call('topic', 'create', '--inventor', inventor, '--name', 'Low current', '--tag', 'sensors')['topic_id']
        context = self.call('topic', 'document', 'documents/topic.md', '--inventor', inventor, '--topic', topic)
        return inventor, topic, profile, context

    def test_replace_preserves_prior_version_and_pins_run(self):
        inventor, topic, profile, context = self.configured()
        run = self.call('run', 'start', '--inventor', inventor, '--topic', topic, '--run-id', 'first')
        self.assertEqual(run['next_state'], 'research_ready')
        before = self.call('run', 'status', '--run', run['run'], '--run-id', 'first')
        Path('documents/new.md').write_text('name: Example\nexpertise: optics\n')
        replacement = self.call('profile', 'document', 'documents/new.md', '--inventor', inventor, '--replace')
        self.assertNotEqual(profile['version_id'], replacement['version_id'])
        after = self.call('run', 'status', '--run', run['run'], '--run-id', 'first')
        self.assertEqual(before['artifacts'], after['artifacts'])
        self.assertEqual(after['profile_version_id'], profile['version_id'])
        self.assertEqual(after['topic_version_id'], context['version_id'])
        self.assertTrue(Path(profile['export']).is_file())

    def test_cross_inventor_preflight_refuses_before_research(self):
        inventor, topic, _, _ = self.configured()
        run = self.call('run', 'start', '--inventor', inventor, '--topic', topic, '--run-id', 'first')
        other = self.inventor()
        result = self.call('research', 'serpapi', '--run', run['run'], '--run-id', 'first', '--query', 'current sensor', '--inventor', other, code=2)
        self.assertEqual(result['failure_code'], 'scope_mismatch')

    def test_catalog_blocks_unassigned_mutations_but_allows_legacy_status(self):
        self.inventor()
        result = self.call('profile', 'document', 'documents/base.md', code=2)
        self.assertEqual(result['failure_code'], 'inventor_required')

    def test_same_name_requires_explicit_identity_and_listing_has_no_values(self):
        first, second = self.inventor(), self.inventor()
        self.assertNotEqual(first, second)
        result = self.call('inventor', 'list', '--name', 'Example')
        self.assertEqual(set(result['matches']), {first, second})
        self.assertNotIn('Example', json.dumps(result))

    def test_wrong_topic_run_path_does_not_create_files(self):
        inventor, topic, _, _ = self.configured()
        other = self.inventor()
        denied = Path('workspace/inventors') / other / 'stolen'
        result = self.call('run', 'start', '--inventor', inventor, '--topic', topic, '--run', denied, '--run-id', 'first', code=2)
        self.assertEqual(result['failure_code'], 'scope_mismatch')
        self.assertFalse(denied.exists())

    def test_legacy_inspect_does_not_initialize_catalog(self):
        result = self.call('legacy', 'inspect')
        self.assertEqual(result['status'], 'reported')
        self.assertFalse(Path('workspace/catalog.sqlite3').exists())

    def test_new_run_profile_override_refused_before_registration(self):
        inventor, topic, _, _ = self.configured()
        self.call('run', 'start', '--inventor', inventor, '--topic', topic,
                  '--run-id', 'override', '--profile', 'workspace/profile.json', code=2)
        listed = self.call('inventor', 'status', '--inventor', inventor)
        self.assertEqual(listed['runs'], [])

    def test_same_owner_requests_cannot_target_a_sibling_run(self):
        inventor, topic, _, _ = self.configured()
        first = self.call('run', 'start', '--inventor', inventor, '--topic', topic, '--run-id', 'first')
        second = self.call('run', 'start', '--inventor', inventor, '--topic', topic, '--run-id', 'second')
        out = Path(second['run']) / 'requests' / 'candidate.json'
        result = self.call('scaffold', 'candidate', '--run', first['run'], '--out', out, code=2)
        self.assertEqual(result['failure_code'], 'scope_mismatch')
        self.assertFalse(out.exists())

    def test_scaffold_cannot_overwrite_run_database(self):
        inventor, topic, _, _ = self.configured()
        run = self.call('run', 'start', '--inventor', inventor, '--topic', topic, '--run-id', 'first')
        database = Path(run['run']) / 'factory.sqlite3'
        before = database.read_bytes()
        result = self.call('scaffold', 'report', '--run-id', 'first', '--out', database, code=2)
        self.assertEqual(result['failure_code'], 'scope_mismatch')
        self.assertEqual(before, database.read_bytes())

    def test_nested_root_cannot_edit_a_published_profile(self):
        inventor, topic, profile, _ = self.configured()
        before = Path(profile['database']).read_bytes()
        Path('documents/extra.md').write_text('another_skill: testing\n')
        result = self.call('profile', 'document', 'documents/extra.md',
                           '--workspace-root', Path('workspace/inventors') / inventor,
                           '--database', profile['database'], '--profile', profile['export'], code=2)
        self.assertEqual(result['failure_code'], 'scope_mismatch')
        self.assertEqual(before, Path(profile['database']).read_bytes())

    def test_root_override_cannot_ingest_managed_interview_responses(self):
        inventor, _, _, _ = self.configured()
        source = Path('documents/inventors') / inventor / 'profile' / 'responses.json'
        source.write_text(json.dumps({'name': 'Public example'}))
        Path('other-workspace').mkdir()
        result = self.call('profile', 'interview', '--responses', source,
                           '--workspace-root', 'other-workspace', code=2)
        self.assertEqual(result['failure_code'], 'scope_mismatch')
        self.assertFalse(Path('other-workspace/profile.sqlite3').exists())

    def test_independent_sibling_workspace_can_use_legacy_fixtures(self):
        self.inventor()
        isolated = Path('test-fixture')
        isolated.mkdir()
        result = self.call('profile', 'document', 'documents/base.md', '--workspace-root', isolated)
        self.assertEqual(result['status'], 'profile_ready')
        self.assertTrue((isolated / 'profile.sqlite3').exists())

    def test_nested_root_cannot_advance_an_unassigned_legacy_run(self):
        self.call('profile', 'document', 'documents/base.md')
        self.call('run', 'start', '--run', 'workspace/runs/legacy', '--run-id', 'legacy')
        self.inventor()
        fixture = Path(__file__).resolve().parents[1] / 'fixtures/kipris/word-search-v1.xml'
        Path('documents/fixture.xml').write_bytes(fixture.read_bytes())
        result = self.call('research', 'fixture', 'documents/fixture.xml', '--query', 'sensor',
                           '--run', 'workspace/runs/legacy', '--run-id', 'legacy',
                           '--workspace-root', 'workspace/runs', code=2)
        self.assertEqual(result['failure_code'], 'scope_mismatch')

    def test_delete_preserves_receipt_and_retry_repairs_catalog(self):
        inventor, topic, _, _ = self.configured()
        run = self.call('run', 'start', '--inventor', inventor, '--topic', topic, '--run-id', 'first')
        with patch('patent_factory.catalog.Catalog.mark_run_deleted', side_effect=OSError('fixture index unavailable')):
            result = self.call('delete-run', '--run', run['run'], code=11)
        self.assertEqual(result['status'], 'deleted')
        self.assertTrue(result['removed'])
        self.assertEqual(result['catalog_update']['status'], 'pending')
        self.assertFalse(Path(run['run']).exists())
        retried = self.call('delete-run', '--run', run['run'])
        self.assertEqual(retried['status'], 'deleted')
        self.assertTrue(retried['reconciled'])
        self.assertEqual(self.call('inventor', 'status', '--inventor', inventor)['runs'][0]['status'], 'deleted')

    def test_deleted_run_id_cannot_be_silently_recreated(self):
        inventor, topic, _, _ = self.configured()
        run = self.call('run', 'start', '--inventor', inventor, '--topic', topic, '--run-id', 'first')
        self.call('delete-run', '--run', run['run'])
        self.call('run', 'start', '--inventor', inventor, '--topic', topic, '--run-id', 'first', code=2)
        self.assertFalse(Path(run['run']).exists())

    def test_normalize_web_uses_topic_run_document_directory(self):
        inventor, topic, _, _ = self.configured()
        run = self.call('run', 'start', '--inventor', inventor, '--topic', topic, '--run-id', 'first')
        Path('documents/web.json').write_text(json.dumps({'schema_version': 'web-rows-v1', 'rows': [{
            'title': 'Public example', 'url': 'https://arxiv.org/abs/1234.56789',
            'identifier': '1234.56789', 'abstract': 'A public abstract',
            'limitations': ['public metadata only'], 'language': 'en',
        }]}))
        out = Path('documents/inventors') / inventor / 'topics' / topic / 'runs/first/normalized.json'
        # Valid ownership must reach the normalizer, irrespective of source format validation.
        result = self.call('research', 'normalize-web', 'documents/web.json', '--run', run['run'],
                           '--out', out, '--allow-host', 'arxiv.org', '--source-type', 'arxiv')
        self.assertTrue(out.exists(), result)

    def test_conflict_decision_is_version_bound_and_applies_entire_batch(self):
        inventor, topic, original, _ = self.configured()
        Path('documents/enrich.md').write_text('expertise: optics\nnew_skill: calibration\n')
        pending = self.call('profile', 'document', 'documents/enrich.md', '--inventor', inventor, code=3)
        self.assertEqual(pending['status'], 'conflict_resolution_required')
        version = pending['candidate_version_id']
        inspected = self.call('profile', 'conflict-inspect', '--inventor', inventor,
                              '--candidate-version', version, '--batch-id', pending['batch_id'])
        decision = {
            'schema_version': 'profile-conflict-decision-v1', 'action': 'choose_source',
            'actor': 'fixture-inventor', 'reason': 'use the newer public fixture',
            'batch_id': pending['batch_id'], 'subject_hash': inspected['subject_hash'],
            'choices': [{'conflict_id': c['conflict_id'], 'selected': 'incoming'} for c in inspected['conflicts']],
            'inventor_id': inventor, 'candidate_version_id': 'wrong-version',
        }
        request = Path(pending['directory']) / 'decision.json'
        request.write_text(json.dumps(decision))
        denied = self.call('profile', 'conflict-decide', '--inventor', inventor,
                          '--candidate-version', version, '--batch-id', pending['batch_id'], '--input', request, code=2)
        self.assertEqual(denied['failure_code'], 'scope_mismatch')
        decision['candidate_version_id'] = version
        request.write_text(json.dumps(decision))
        applied = self.call('profile', 'conflict-decide', '--inventor', inventor,
                           '--candidate-version', version, '--batch-id', pending['batch_id'], '--input', request)
        self.assertEqual(applied['status'], 'profile_ready')
        facts = json.loads(Path(applied['export']).read_text())['facts']
        self.assertEqual(facts['new_skill']['value'], 'calibration')
        self.assertEqual(facts['expertise']['value'], 'optics')
        old = json.loads(Path(original['export']).read_text())['facts']
        self.assertEqual(old['expertise']['value'], 'sensors')
        inspected_again = self.call('profile', 'conflict-inspect', '--inventor', inventor,
                                    '--candidate-version', version, '--batch-id', pending['batch_id'])
        self.assertEqual(inspected_again['status'], 'decided')

    def test_legacy_inspect_rejects_absolute_user_paths(self):
        result = self.call('legacy', 'inspect', '--workspace-root', Path('workspace').absolute(), code=2)
        self.assertEqual(result['failure_code'], 'unsafe_path')

    def test_corrupt_legacy_database_still_emits_one_json_result(self):
        broken = Path('workspace/broken')
        broken.mkdir()
        (broken / 'factory.sqlite3').write_bytes(b'not a sqlite database')
        result = self.call('run', 'status', '--run', broken, '--run-id', 'broken', code=2)
        self.assertEqual(result['status'], 'error')

    def test_kipris_query_inventor_is_separate_from_owner(self):
        from patent_factory.cli import build_parser
        args = build_parser().parse_args(['research', 'kipris', '--query', 'sensor',
                                         '--inventor', 'Public Inventor', '--inventor-id', 'iv_example'])
        self.assertEqual(args.inventor, 'iv_example')
        self.assertEqual(args.inventor_terms, ['Public Inventor'])

    def test_archive_blocks_new_run_but_allows_existing_resume(self):
        inventor, topic, _, _ = self.configured()
        run = self.call('run', 'start', '--inventor', inventor, '--topic', topic, '--run-id', 'first')
        self.call('topic', 'archive', '--inventor', inventor, '--topic', topic)
        self.call('run', 'status', '--run', run['run'])
        self.call('run', 'start', '--inventor', inventor, '--topic', topic, '--run-id', 'second', code=2)

    def test_audit_nested_fixture_cannot_read_another_inventor(self):
        inventor, topic, _, _ = self.configured()
        run = self.call('run', 'start', '--inventor', inventor, '--topic', topic, '--run-id', 'first')
        other = self.inventor()
        source = Path('documents/inventors') / other / 'profile' / 'reference.xml'
        source.parent.mkdir(parents=True)
        source.write_text('<root/>')
        manifest = Path('documents/manifest.json')
        manifest.write_text(json.dumps({'schema_version': 'audit-fixture-manifest-v1', 'responses': [{
            'finalist_id': 'example', 'page': 1, 'source': str(source), 'term': 'example',
        }]}))
        query = Path(run['run']) / 'requests' / 'query.json'
        query.parent.mkdir(exist_ok=True)
        query.write_text('{}')
        result = self.call('audit', 'retrieve', '--run', run['run'], '--query-input', query,
                           '--fixture-manifest', manifest, code=2)
        self.assertEqual(result['failure_code'], 'scope_mismatch')


if __name__ == '__main__':
    unittest.main()
