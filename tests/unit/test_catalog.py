"""Catalog isolation, immutable versions and recoverable publication."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from patent_factory.database import connect_database, connect_read_only_database, ingest, profile_payload
from patent_factory.profile import interview_facts


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temporary.name)
        Path('documents').mkdir()
        self.addCleanup(os.chdir, self.previous)
        self.addCleanup(self.temporary.cleanup)

    def catalog(self, **kwargs):
        from patent_factory.catalog import Catalog
        return Catalog(Path('workspace'), Path('documents'), **kwargs)

    def ready(self, catalog, inventor, *, topic=None, replace=False, facts=None):
        version = catalog.prepare_version('topic' if topic else 'profile', inventor, topic, replace=replace)
        with connect_database(Path(version['database'])) as connection:
            ingest(connection, 'interview', interview_facts(facts or {'expertise': 'public fixture'}))
        return catalog.finish_version(version['version_id'])

    def test_identity_duplicates_are_unambiguous_only_by_id_and_metadata_hides_names(self):
        # A name-keyed catalog would alias distinct people and expose private names.
        with self.catalog(create=True) as catalog:
            first = catalog.create_inventor('Same Person')
            second = catalog.create_inventor('Same Person')
            self.assertNotEqual(first['inventor_id'], second['inventor_id'])
            self.assertEqual(set(catalog.resolve_name('inventor', 'Same Person')), {first['inventor_id'], second['inventor_id']})
            self.assertNotIn('name', json.dumps(catalog.list_inventors()))
            topic = catalog.create_topic(first['inventor_id'], 'Secret project', ['AI', 'AI', 'sensor'])
            self.assertEqual(topic['tag_count'], 2)
            self.assertNotIn('Secret project', json.dumps(topic))
            self.assertEqual(catalog.resolve_name('topic', 'Secret project', first['inventor_id']), [topic['topic_id']])
            with self.assertRaisesRegex(ValueError, 'owner|scope'):
                catalog.entity('topic', topic['topic_id'], second['inventor_id'])
        self.assertEqual(Path('workspace/catalog.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_replace_drops_old_fields_and_enrichment_never_changes_published_database(self):
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            original = self.ready(catalog, inventor, facts={'expertise': 'old', 'old_only': 'remove'})
            original_bytes = Path(original['database']).read_bytes()
            enriched = catalog.prepare_version('profile', inventor)
            with connect_database(Path(enriched['database'])) as connection:
                ingest(connection, 'interview', interview_facts({'added': 'new'}))
            catalog.finish_version(enriched['version_id'])
            replacement = self.ready(catalog, inventor, replace=True, facts={'expertise': 'replacement'})
            with connect_read_only_database(Path(replacement['database'])) as connection:
                self.assertEqual(set(profile_payload(connection)['facts']), {'expertise'})
            self.assertEqual(Path(original['database']).read_bytes(), original_bytes)
            self.assertEqual(catalog.current_version('profile', inventor)['version_id'], replacement['version_id'])
            self.assertNotIn('facts', replacement)
            self.assertEqual(replacement['status'], 'ready')

    def test_compare_and_swap_prevents_concurrent_drafts_overwriting_new_head(self):
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            self.ready(catalog, inventor)
            first = catalog.prepare_version('profile', inventor)
            stale = catalog.prepare_version('profile', inventor)
            for version in (first, stale):
                with connect_database(Path(version['database'])) as connection:
                    ingest(connection, 'interview', interview_facts({'added': version['version_id']}))
            catalog.finish_version(first['version_id'])
            with self.assertRaisesRegex(ValueError, 'active|stale|concurrent'):
                catalog.finish_version(stale['version_id'])
            self.assertEqual(catalog.current_version('profile', inventor)['version_id'], first['version_id'])

    def test_conflict_candidate_never_becomes_active(self):
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            previous = self.ready(catalog, inventor)
            candidate = catalog.prepare_version('profile', inventor)
            with connect_database(Path(candidate['database'])) as connection:
                ingest(connection, 'interview', interview_facts({'expertise': 'conflict'}))
            result = catalog.finish_version(candidate['version_id'])
            self.assertEqual(result['status'], 'conflict_resolution_required')
            self.assertEqual(catalog.current_version('profile', inventor)['version_id'], previous['version_id'])
            with self.assertRaisesRegex(ValueError, 'immutable|published|ready'):
                catalog.prepare_version('profile', inventor, version_id=previous['version_id'])

    def test_export_failure_preserves_head_and_retry_publishes_same_candidate(self):
        # A directory occupying the export path reproduces a real export failure.
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            old = self.ready(catalog, inventor)
            candidate = catalog.prepare_version('profile', inventor, replace=True)
            with connect_database(Path(candidate['database'])) as connection:
                ingest(connection, 'interview', interview_facts({'new': 'ready'}))
            Path(candidate['export']).mkdir()
            with self.assertRaises((ValueError, OSError)):
                catalog.finish_version(candidate['version_id'])
            self.assertEqual(catalog.current_version('profile', inventor)['version_id'], old['version_id'])
            Path(candidate['export']).rmdir()
            retried = catalog.prepare_version('profile', inventor, replace=True, version_id=candidate['version_id'])
            self.assertEqual(retried['version_id'], candidate['version_id'])
            self.assertEqual(catalog.finish_version(candidate['version_id'])['status'], 'ready')
            self.assertEqual(catalog.finish_version(candidate['version_id'])['status'], 'ready')

    def test_snapshots_are_immutable_deduplicated_and_track_original_byte_hash(self):
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            version = catalog.prepare_version('profile', inventor)
            source = Path('documents/background.md')
            source.write_bytes(b'expertise: public fixture\n')
            snapshot = catalog.record_input(version['version_id'], source, 'document')
            self.assertFalse(snapshot.is_absolute())
            self.assertIn(inventor, snapshot.parts)
            self.assertEqual(catalog.record_input(version['version_id'], source, 'document'), snapshot)
            source.write_bytes(b'expertise: edited\n')
            newer = catalog.record_input(version['version_id'], source, 'document')
            self.assertNotEqual(snapshot, newer)
            self.assertEqual(snapshot.read_bytes(), b'expertise: public fixture\n')
            manifest = catalog.input_manifest(version['version_id'])
            self.assertEqual(len(manifest), 2)
            self.assertTrue(all(item['original_path'] == 'documents/background.md' for item in manifest))
            self.assertTrue(all('byte_hash' in item for item in manifest))
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)

    def test_folder_snapshot_preserves_tree_without_recursively_ingesting_managed_area(self):
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            version = catalog.prepare_version('profile', inventor)
            source = Path('documents/incoming')
            (source / 'nested').mkdir(parents=True)
            (source / 'nested/a.md').write_text('one: 1\n')
            (source / 'ignored.bin').write_bytes(b'not a supported document')
            snapshot = catalog.record_input(version['version_id'], source, 'folder')
            self.assertEqual((snapshot / 'nested/a.md').read_text(), 'one: 1\n')
            self.assertFalse((snapshot / 'ignored.bin').exists())
            with self.assertRaisesRegex(ValueError, 'managed|root|overlap'):
                catalog.record_input(version['version_id'], Path('documents'), 'folder')

    def test_snapshot_rejects_cross_inventor_and_cross_topic_sources(self):
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('First')['inventor_id']
            other = catalog.create_inventor('Second')['inventor_id']
            topic1 = catalog.create_topic(inventor, 'First topic', [])['topic_id']
            topic2 = catalog.create_topic(inventor, 'Second topic', [])['topic_id']
            profile = catalog.prepare_version('profile', other)
            source = Path('documents/shared.md')
            source.write_text('one: 1\n')
            snapshot = catalog.record_input(profile['version_id'], source, 'document')
            target = catalog.prepare_version('profile', inventor)
            with self.assertRaisesRegex(ValueError, 'owner|scope'):
                catalog.record_input(target['version_id'], snapshot, 'document')
            first_topic = catalog.prepare_version('topic', inventor, topic1)
            topic_snapshot = catalog.record_input(first_topic['version_id'], source, 'document')
            second_topic = catalog.prepare_version('topic', inventor, topic2)
            with self.assertRaisesRegex(ValueError, 'owner|scope'):
                catalog.record_input(second_topic['version_id'], topic_snapshot, 'document')

    def test_managed_profile_folder_cannot_ingest_its_own_snapshot_parent(self):
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            candidate = catalog.prepare_version('profile', inventor)
            source = Path('documents/source.md')
            source.write_text('expertise: fixture\n')
            snapshot = catalog.record_input(candidate['version_id'], source, 'document')
            managed_parent = snapshot.parent.parent
            with self.assertRaisesRegex(ValueError, 'managed|overlap'):
                catalog.record_input(candidate['version_id'], managed_parent, 'folder')

    def test_legacy_binding_refuses_another_owners_historical_snapshot(self):
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            topic = catalog.create_topic(inventor, 'Topic', [])['topic_id']
            other = catalog.create_inventor('Other')['inventor_id']
            wrong = self.ready(catalog, other)
            with self.assertRaisesRegex(ValueError, 'owner|scope'):
                catalog.bind_run(Path('workspace/runs/legacy'), 'legacy', inventor, topic,
                                 None, None, legacy=True,
                                 legacy_profile_database=wrong['database'],
                                 legacy_profile_export=wrong['export'])
            self.assertIsNone(catalog.run_binding(Path('workspace/runs/legacy')))

    def test_snapshot_rejects_symlinks_oversize_and_path_traversal(self):
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            version = catalog.prepare_version('profile', inventor)
            source = Path('documents/a.md')
            source.write_text('one: 1\n')
            Path('documents/link.md').symlink_to('a.md')
            Path('documents/huge.md').write_bytes(b'x' * 2_000_001)
            for unsafe in (Path('documents/link.md'), Path('documents/huge.md'), Path('documents/../outside.md'), source.resolve()):
                with self.subTest(path=unsafe), self.assertRaises((ValueError, OSError)):
                    catalog.record_input(version['version_id'], unsafe, 'document')

    def test_archive_blocks_new_setup_and_binding_but_preserves_old_binding_lookup(self):
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            topic = catalog.create_topic(inventor, 'Topic', [])['topic_id']
            profile = self.ready(catalog, inventor)
            context = self.ready(catalog, inventor, topic=topic)
            run = Path('workspace/inventors') / inventor / 'topics' / topic / 'runs/fixture'
            binding = catalog.bind_run(run, 'fixture', inventor, topic, profile['version_id'], context['version_id'])
            self.assertEqual(binding['run_id'], 'fixture')
            catalog.archive('inventor', inventor)
            self.assertEqual(catalog.run_binding(run)['inventor_id'], inventor)
            with self.assertRaisesRegex(ValueError, 'archived'):
                catalog.prepare_version('profile', inventor)
            with self.assertRaisesRegex(ValueError, 'archived'):
                catalog.bind_run(run.with_name('second'), 'second', inventor, topic, profile['version_id'], context['version_id'])

    def test_run_binding_replays_exact_scope_and_refuses_mismatched_owner_path_or_version(self):
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            topic = catalog.create_topic(inventor, 'Topic', [])['topic_id']
            other_topic = catalog.create_topic(inventor, 'Other topic', [])['topic_id']
            profile = self.ready(catalog, inventor)
            context = self.ready(catalog, inventor, topic=topic)
            run = Path('workspace/inventors') / inventor / 'topics' / topic / 'runs/fixture'
            catalog.bind_run(run, 'fixture', inventor, topic, profile['version_id'], context['version_id'])
            self.assertEqual(catalog.bind_run(run, 'fixture', inventor, topic, profile['version_id'], context['version_id'])['run_id'], 'fixture')
            with self.assertRaisesRegex(ValueError, 'scope|owner|mismatch'):
                catalog.bind_run(run, 'fixture', inventor, other_topic, profile['version_id'], context['version_id'])
            with self.assertRaisesRegex(ValueError, 'path|managed|scope'):
                catalog.bind_run(Path('workspace/runs/new'), 'new', inventor, topic, profile['version_id'], context['version_id'])
            with self.assertRaisesRegex(ValueError, 'run|mismatch'):
                catalog.run_binding(run, 'another')
            catalog.mark_run_deleted(run, partial=True)
            self.assertEqual(catalog.run_binding(run)['status'], 'deletion_partial')
            catalog.mark_run_deleted(run, partial=False)
            self.assertEqual(catalog.run_binding(run)['status'], 'deleted')
            self.assertEqual(len(catalog.list_runs(inventor_id=inventor)), 1)

    def test_legacy_source_claim_pins_one_owner_and_recovers_candidate_before_publish(self):
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('First')['inventor_id']
            other = catalog.create_inventor('Second')['inventor_id']
            source = Path('workspace/profile.sqlite3')
            with connect_database(source):
                pass
            candidate = catalog.prepare_version('profile', inventor, replace=True, version_id='pv_legacy_fixture')
            claimed = catalog.register_legacy_source(source, inventor, source_hash='a' * 64,
                                                     version_id=candidate['version_id'])
            self.assertEqual(claimed['version_id'], candidate['version_id'])
            self.assertEqual(catalog.legacy_source(source), claimed)
            self.assertEqual(catalog.register_legacy_source(source, inventor, source_hash='a' * 64), claimed)
            with self.assertRaisesRegex(ValueError, 'owner|scope'):
                catalog.register_legacy_source(source, other, source_hash='a' * 64)
            with self.assertRaisesRegex(ValueError, 'hash|changed'):
                catalog.register_legacy_source(source, inventor, source_hash='b' * 64)

    def test_failed_initialization_is_recoverable_by_candidate_id(self):
        from unittest.mock import patch
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            with patch('patent_factory.catalog.connect_database', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    catalog.prepare_version('profile', inventor)
            pending = catalog.list_versions(inventor)[0]
            self.assertEqual(pending['status'], 'preparing')
            recovered = catalog.prepare_version('profile', inventor, version_id=pending['version_id'])
            self.assertEqual(recovered['status'], 'draft')
            with connect_read_only_database(Path(recovered['database'])) as connection:
                self.assertEqual(profile_payload(connection)['state'], 'profile_pending')

    def test_version_lease_refuses_concurrent_resume_and_lease_after_publish(self):
        from concurrent.futures import ThreadPoolExecutor
        from patent_factory.catalog import CatalogError
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            candidate = catalog.prepare_version('profile', inventor)
            def concurrent_resume():
                with self.catalog() as other:
                    with self.assertRaises(CatalogError) as rejected:
                        with other.edit_version(candidate['version_id']):
                            pass
                    return rejected.exception.code
            with catalog.edit_version(candidate['version_id']):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    self.assertEqual(executor.submit(concurrent_resume).result(), 'catalog_version_busy')
                with connect_database(Path(candidate['database'])) as connection:
                    ingest(connection, 'interview', interview_facts({'expertise': 'fixture'}))
                catalog.finish_version(candidate['version_id'])
            with self.assertRaisesRegex(ValueError, 'immutable|published'):
                with catalog.edit_version(candidate['version_id']):
                    pass

    def test_failed_snapshot_write_leaves_no_partial_final_file_and_retry_recovers(self):
        from unittest.mock import patch
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            candidate = catalog.prepare_version('profile', inventor)
            source = Path('documents/background.md')
            source.write_text('expertise: fixture\n')
            with patch('patent_factory.catalog.os.fsync', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    catalog.record_input(candidate['version_id'], source, 'document')
            managed = Path('documents/inventors') / inventor
            self.assertEqual(list(managed.rglob('background.md')), [])
            snapshot = catalog.record_input(candidate['version_id'], source, 'document')
            self.assertEqual(snapshot.read_bytes(), source.read_bytes())

    def test_new_candidate_does_not_adopt_or_overwrite_unregistered_directory(self):
        with self.catalog(create=True) as catalog:
            inventor = catalog.create_inventor('Fixture')['inventor_id']
            directory = Path('workspace/inventors') / inventor / 'profiles/pv_occupied'
            directory.mkdir(parents=True)
            occupied = directory / 'profile.sqlite3'
            with connect_database(occupied) as connection:
                ingest(connection, 'interview', interview_facts({'expertise': 'unregistered'}))
            original = occupied.read_bytes()
            with self.assertRaisesRegex(ValueError, 'exist|registered|occupied'):
                catalog.prepare_version('profile', inventor, version_id='pv_occupied')
            self.assertEqual(occupied.read_bytes(), original)
            self.assertEqual(catalog.list_versions(inventor), [])

    def test_reopening_catalog_refuses_documents_root_override(self):
        from patent_factory.catalog import Catalog, CatalogError
        with self.catalog(create=True):
            pass
        wrong_root = Path('documents/inventors/other')
        wrong_root.mkdir(parents=True)
        for create in (False, True):
            with self.subTest(create=create):
                with self.assertRaises(CatalogError) as rejected:
                    Catalog(Path('workspace'), wrong_root, create=create)
                self.assertEqual(rejected.exception.code, 'catalog_scope_mismatch')
        with self.catalog() as original:
            self.assertEqual(original.list_inventors(), [])

    def test_reopening_catalog_refuses_moved_workspace_root(self):
        from patent_factory.catalog import Catalog, CatalogError
        with self.catalog(create=True):
            pass
        Path('workspace').rename('moved-workspace')
        with self.assertRaises(CatalogError) as rejected:
            Catalog(Path('moved-workspace'), Path('documents'))
        self.assertEqual(rejected.exception.code, 'catalog_scope_mismatch')

    def test_catalog_without_registered_roots_is_explicitly_unsupported(self):
        import sqlite3
        from patent_factory.catalog import CatalogError
        Path('workspace').mkdir()
        with sqlite3.connect('workspace/catalog.sqlite3') as connection:
            connection.execute('PRAGMA user_version=1')
            connection.execute('CREATE TABLE older_catalog_marker(value TEXT)')
        for create in (False, True):
            with self.subTest(create=create):
                with self.assertRaises(CatalogError) as rejected:
                    self.catalog(create=create)
                self.assertEqual(rejected.exception.code, 'catalog_schema')

    def test_invalid_roots_and_symlink_catalog_fail_without_creating_state(self):
        from patent_factory.catalog import Catalog
        with self.assertRaises(ValueError):
            Catalog(Path('../outside'), Path('documents'), create=True)
        Path('workspace').mkdir()
        Path('workspace/catalog.sqlite3').symlink_to('../missing')
        with self.assertRaises(ValueError):
            self.catalog(create=True)


if __name__ == '__main__':
    unittest.main()
