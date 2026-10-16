"""Resolve inventor/topic ownership before invoking any workflow or network verb.

The catalog selects paths; existing JSON CLI handlers remain the stage authorities.
There is deliberately no process-global or on-disk 'active inventor'.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Callable, Any

from .paths import contained_input, private_root, private_contained_directory


class ScopeError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def configure_parser(commands: argparse._SubParsersAction) -> None:
    for kind in ('inventor', 'topic'):
        parent = commands.add_parser(kind, help=f'manage local {kind} identities')
        children = parent.add_subparsers(dest=f'{kind}_command', required=True)
        for action in ('create', 'list', 'status', 'archive'):
            command = children.add_parser(action)
            if action == 'create':
                command.add_argument('--name', required=True)
            if action == 'list':
                command.add_argument('--name', help='exact name match; return IDs only')
            if kind == 'topic' and action == 'create':
                command.add_argument('--tag', action='append', default=[])
        if kind == 'topic':
            for mode in ('document', 'folder', 'interview'):
                command = children.add_parser(mode, help=f'version topic context from {mode}')
                if mode == 'interview':
                    command.add_argument('--responses')
                else:
                    command.add_argument('source', type=Path)
    legacy = commands.add_parser('legacy', help='inspect or explicitly assign unclassified data')
    operations = legacy.add_subparsers(dest='legacy_command', required=True)
    for action in ('inspect', 'adopt'):
        command = operations.add_parser(action)
        command.add_argument('--run', type=Path)
        command.add_argument('--run-id')
        if action == 'adopt':
            command.add_argument('--profile', action='store_true')

    def visit(parser):
        nested = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        if nested:
            for action in nested:
                for child in action.choices.values():
                    visit(child)
            return
        if parser.prog.endswith(' init'):
            return
        options = parser._option_string_actions
        # KIPRIS has a pre-existing --inventor search expansion; preserve it.
        if parser.prog.endswith('research kipris'):
            options['--inventor'].dest = 'inventor_terms'
            parser.add_argument('--inventor-term', dest='inventor_terms', action='append')
        parser.add_argument('--inventor-id', dest='inventor', help='explicit inventor owner ID')
        for name in ('inventor', 'topic'):
            if f'--{name}' not in options:
                parser.add_argument(f'--{name}', help=f'explicit {name} ID (or run ownership assertion)')
        for name, default in (('workspace-root', 'workspace'), ('documents-root', 'documents')):
            if f'--{name}' not in options:
                parser.add_argument(f'--{name}', type=Path, default=Path(default))
        is_profile_input = 'profile ' in parser.prog and not 'conflict-' in parser.prog
        is_topic_input = any(parser.prog.endswith('topic ' + m) for m in ('document', 'folder', 'interview'))
        if is_profile_input or is_topic_input:
            parser.add_argument('--replace', action='store_true', help='start an empty new version')
            parser.add_argument('--candidate-version', help='resume the exact draft version')
        if 'profile conflict-' in parser.prog:
            parser.add_argument('--candidate-version', help='exact managed draft version')
        # run paths may be resolved from an explicit, cataloged run ID.
        if '--run' in options:
            options['--run'].required = False
        if '--run-id' in options:
            options['--run-id'].required = False
        is_stage = any(part in parser.prog.split()[1:] for part in (
            'research', 'ideate', 'shortlist', 'audit', 'scaffold', 'gate',
            'draft', 'review', 'validate', 'share',
        ))
        if is_stage:
            if '--run' not in options:
                parser.add_argument('--run', type=Path, help='managed run path')
            if '--run-id' not in options:
                parser.add_argument('--run-id', help='managed run ID')
    for parser in commands.choices.values():
        visit(parser)


def _require(value, code):
    if not value:
        raise ScopeError(code, code)
    return value


def _under(path: Path, root: Path) -> bool:
    if path.is_absolute() or '..' in path.parts:
        return False
    return path == root or root in path.parents


def _assert_path(value, expected, label):
    if value is not None and Path(value) != Path(expected):
        raise ScopeError('scope_mismatch', f'{label} differs from the selected scope')


def _version_paths(args, version):
    _assert_path(getattr(args, 'database', None), version['database'], 'profile database')
    _assert_path(getattr(args, 'profile', None), version['export'], 'profile export')
    args.database, args.profile = Path(version['database']), Path(version['export'])


def _metadata(binding):
    return {key: binding[key] for key in (
        'inventor_id', 'topic_id', 'profile_version_id', 'topic_version_id',
    ) if key in binding}


def assert_document_scope(args, value: Path) -> None:
    binding = getattr(args, '_managed_binding', None)
    if binding is None:
        return
    scope = args.documents_root / 'inventors'
    if _under(value, scope):
        own = scope / binding['inventor_id'] / 'topics' / binding['topic_id']
        if not _under(value, own):
            raise ScopeError('scope_mismatch', 'document belongs to another inventor or topic')


def _guard_catalog_target(value: Path, root: Path) -> None:
    if value.is_absolute() or '..' in value.parts:
        raise ScopeError('unsafe_path', 'absolute paths and parent traversal are not allowed; use repository-relative paths')
    for parent in (value, *value.parents):
        if (parent / 'catalog.sqlite3').exists() and parent != root:
            raise ScopeError('scope_mismatch', 'managed paths require their registered workspace root')
    if not (root / 'catalog.sqlite3').exists():
        parts = value.parts
        if any(part == 'inventors' and index + 1 < len(parts) and parts[index + 1].startswith('iv_')
               for index, part in enumerate(parts)):
            raise ScopeError('scope_mismatch', 'managed paths require their registered workspace root')


def _management(args, catalog):
    kind = args.command
    action = getattr(args, kind + '_command')
    if action == 'create':
        if kind == 'inventor':
            return catalog.create_inventor(args.name)
        return catalog.create_topic(_require(args.inventor, 'inventor_required'), args.name, args.tag)
    if action == 'list':
        if args.name:
            return {'status': 'reported', 'matches': catalog.resolve_name(kind, args.name, inventor_id=args.inventor)}
        items = catalog.list_inventors() if kind == 'inventor' else catalog.list_topics(_require(args.inventor, 'inventor_required'))
        return {'status': 'reported', 'items': items}
    identifier = _require(getattr(args, kind), kind + '_required')
    result = catalog.entity(kind, identifier, inventor_id=args.inventor if kind == 'topic' else None)
    if action == 'archive':
        return catalog.archive(kind, identifier)
    if kind == 'inventor':
        result['runs'] = catalog.list_runs(inventor_id=identifier)
        result['versions'] = catalog.list_versions(identifier)
    else:
        result['runs'] = catalog.list_runs(inventor_id=args.inventor, topic_id=identifier)
        result['versions'] = catalog.list_versions(_require(args.inventor, 'inventor_required'), identifier)
    return {'status': 'reported', **result}


def _profile(args, catalog, delegate):
    inventor = _require(args.inventor, 'inventor_required')
    topic = args.topic if args.command == 'topic' else None
    conflict = args.command == 'profile' and args.profile_command.startswith('conflict-')
    if args.command == 'profile' and args.topic and not conflict:
        raise ScopeError('scope_mismatch', 'common profile setup does not accept a topic')
    if args.command == 'topic':
        _require(topic, 'topic_required')
    if conflict:
        version = catalog.version(_require(args.candidate_version, 'candidate_version_required'))
        topic = version.get('topic_id')
        if version['inventor_id'] != inventor or args.topic != topic:
            raise ScopeError('scope_mismatch', 'draft belongs to another inventor or topic')
    else:
        version = catalog.prepare_version(
            'topic' if topic else 'profile', inventor, topic,
            replace=args.replace, version_id=args.candidate_version,
        )
    if conflict and args.profile_command == 'conflict-inspect':
        return _profile_locked(args, catalog, delegate, version, conflict, topic)
    with catalog.edit_version(version['version_id']):
        version = catalog.version(version['version_id'])
        return _profile_locked(args, catalog, delegate, version, conflict, topic)


def _profile_locked(args, catalog, delegate, version, conflict, topic):
    _version_paths(args, version)
    inspect = conflict and args.profile_command == 'conflict-inspect'
    if version['status'] == 'ready' and not inspect:
        raise ScopeError('immutable_version', 'published versions cannot be edited')
    if conflict and not inspect:
        from .provenance import strict_json_loads
        source = contained_input(args.input, private_root(args.workspace_root, 'workspace root'), 'decision')
        if source.stat().st_size > args.byte_budget:
            raise ScopeError('input_too_large', 'decision input exceeds byte budget')
        request = strict_json_loads(source.read_text(encoding='utf-8'))
        if request.get('candidate_version_id') != version['version_id'] or request.get('inventor_id') != args.inventor:
            raise ScopeError('scope_mismatch', 'decision requires exact inventor and candidate version')
        args._managed_conflict = True
    elif not inspect:
        if getattr(args, 'source', None) is not None:
            args.source = catalog.record_input(version['version_id'], args.source, getattr(args, 'profile_command', None) or args.topic_command)
        elif getattr(args, 'responses', None) not in (None, '-'):
            args.responses = str(catalog.record_input(version['version_id'], Path(args.responses), 'interview'))
        else:
            raise ScopeError('responses_file_required', 'managed interviews require a response file')
    original = args.command
    if original == 'topic':
        args.command, args.profile_command = 'profile', args.topic_command
    try:
        payload, code = delegate(args)
    finally:
        args.command = original
    if not inspect:
        published = catalog.finish_version(version['version_id'])
        payload.update({**published, 'status': payload['status']})
        if topic and payload['status'] == 'profile_ready':
            payload['status'] = 'topic_ready'
    else:
        payload.update({**version, 'status': payload['status']})
    payload['candidate_version_id'] = version['version_id']
    payload['command'] = original + '.' + (args.topic_command if original == 'topic' else args.profile_command)
    return payload, code


def _bind_start(args, catalog):
    from .contexts import prepare_effective_profile
    inventor = _require(args.inventor, 'inventor_required')
    topic = _require(args.topic, 'topic_required')
    catalog.entity('topic', topic, inventor_id=inventor)
    if args.run_id is None:
        args.run_id = 'run_' + uuid.uuid4().hex[:20]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', args.run_id):
        raise ScopeError('invalid_run_id', 'managed run ID must be a path-safe identifier')
    expected = args.workspace_root / 'inventors' / inventor / 'topics' / topic / 'runs' / args.run_id
    _assert_path(args.run, expected, 'run path')
    snapshot = expected / 'context'
    export, database = snapshot / 'profile.json', snapshot / 'profile.sqlite3'
    _assert_path(args.profile, export, 'profile export')
    _assert_path(args.profile_database, database, 'profile database')
    args.run = expected
    existing = catalog.run_binding(expected, args.run_id)
    if existing:
        if existing['status'] != 'ready':
            raise ScopeError('run_unavailable', 'deleted or deleting run IDs cannot be reused')
        binding = existing
    else:
        profile = catalog.current_version('profile', inventor)
        context = catalog.current_version('topic', inventor, topic)
        binding = catalog.bind_run(expected, args.run_id, inventor, topic, profile['version_id'], context['version_id'])
    profile = catalog.version(binding['profile_version_id'])
    context = catalog.version(binding['topic_version_id'])
    root = private_root(args.workspace_root, 'workspace root')
    private_contained_directory(expected, root, 'managed run', create=True)
    private_contained_directory(snapshot, root, 'pinned profile', create=True)
    args._prepared_profile = prepare_effective_profile(
        Path(profile['database']), Path(context['database']), database, export, _metadata(binding),
    )
    args.profile, args.profile_database = export, database
    args._managed_binding = binding
    return binding


def _resolve_run(args, catalog):
    if getattr(args, 'run', None) is None and getattr(args, 'run_id', None):
        matches = [x for x in catalog.list_runs(inventor_id=args.inventor, topic_id=args.topic) if x['run_id'] == args.run_id]
        if len(matches) != 1:
            raise ScopeError('run_id_ambiguous', 'supply a cataloged run ID or explicit run path')
        args.run = Path(matches[0].get('run_path', matches[0].get('run')))
    if not getattr(args, 'run', None):
        raise ScopeError('run_required', 'a run path or cataloged run ID is required')
    binding = catalog.run_binding(args.run, getattr(args, 'run_id', None))
    read_only = args.command == 'run' and args.run_command in ('status', 'show')
    if binding is None:
        if read_only and not (args.inventor or args.topic):
            return None
        raise ScopeError('legacy_assignment_required', 'assign legacy run ownership before mutation')
    for option, key in (('inventor', 'inventor_id'), ('topic', 'topic_id')):
        value = getattr(args, option, None)
        if value and value != binding[key]:
            raise ScopeError('scope_mismatch', f'{option} does not own this run')
    args.run_id = binding['run_id']
    if binding['status'] != 'ready' and args.command != 'delete-run':
        raise ScopeError('run_unavailable', 'run deletion is pending or complete')
    if binding.get('legacy'):
        profile_db = binding.get('legacy_profile_database')
        profile_export = binding.get('legacy_profile_export')
    else:
        profile_db = str(args.run / 'context' / 'profile.sqlite3')
        profile_export = str(args.run / 'context' / 'profile.json')
    if profile_db:
        _assert_path(getattr(args, 'profile_database', None), profile_db, 'profile database')
        _assert_path(getattr(args, 'profile', None), profile_export, 'profile export')
        args.profile_database, args.profile = Path(profile_db), Path(profile_export)
        if args.command != 'delete-run':
            from .contexts import resolve_pinned_profile
            from .database import connect_read_only_database
            from .legacy import read_legacy_database
            from .provenance import strict_json_loads
            workspace = private_root(args.workspace_root, 'workspace root')
            database_path = contained_input(args.run / 'factory.sqlite3', workspace, 'run database')
            pinned_path = contained_input(Path(profile_db), workspace, 'pinned profile')
            with read_legacy_database(database_path) as connection, connect_read_only_database(pinned_path) as profile_connection:
                resolve_pinned_profile(connection, profile_connection, args.run_id)
                row = connection.execute(
                    "SELECT ar.content_json FROM artifact_revisions ar JOIN current_artifacts ca "
                    "ON ca.revision_id=ar.revision_id WHERE ca.run_id=? AND ca.kind='profile_context' AND ar.stale=0",
                    (args.run_id,),
                ).fetchone()
                if row:
                    content = strict_json_loads(row[0])
                    if content.get('version') == 'profile-context-v2' and content.get('binding') != _metadata(binding):
                        raise ScopeError('scope_mismatch', 'catalog and run context ownership differ')
    args._managed_binding = binding
    # Every request/artifact path belongs to this run, even when it is a v1 adoption.
    for option in ('input', 'out', 'validate', 'query_input', 'feature_input', 'seal'):
        value = getattr(args, option, None)
        output_root = args.run / 'requests'
        if option == 'out' and args.command == 'research' and args.research_command == 'normalize-web':
            output_root = (args.documents_root / 'inventors' / binding['inventor_id'] / 'topics'
                           / binding['topic_id'] / 'runs' / args.run_id)
        if value is not None and not _under(value, output_root):
            raise ScopeError('scope_mismatch', f'{option} must be beneath the selected run')
    # Research inputs can be explicit unclassified documents or this topic's inputs;
    # data already managed by a different owner is never silently reused.
    for option in ('source', 'fixture_manifest', 'fixture_response', 'fixture_account'):
        value = getattr(args, option, None)
        if value is not None:
            assert_document_scope(args, value)
    if getattr(args, 'fixture_manifest', None) is not None:
        from .provenance import strict_json_loads
        source = contained_input(args.fixture_manifest, private_root(args.documents_root, 'documents root'), 'fixture manifest')
        if source.stat().st_size > args.byte_budget:
            raise ScopeError('input_too_large', 'fixture manifest exceeds byte budget')
        manifest = strict_json_loads(source.read_text(encoding='utf-8'))
        if isinstance(manifest, dict) and isinstance(manifest.get('responses'), list):
            for entry in manifest['responses']:
                if isinstance(entry, dict) and isinstance(entry.get('source'), str):
                    assert_document_scope(args, Path(entry['source']))
    return binding


def execute(args, delegate: Callable) -> tuple[dict[str, Any], int]:
    """Preflight and dispatch, keeping all managed filesystem changes in the core."""
    from .catalog import Catalog
    root = getattr(args, 'workspace_root', Path('workspace'))
    if args.command == 'audit' and args.audit_command == 'retrieve' and args.live and args.fixture_manifest is not None:
        raise ScopeError('invalid_input', 'audit retrieve --live does not take --fixture-manifest')
    for option in ('workspace_root', 'documents_root', 'run'):
        value = getattr(args, option, None)
        if value is not None and (value.is_absolute() or '..' in value.parts):
            raise ScopeError('unsafe_path', 'absolute paths and parent traversal are not allowed; use repository-relative paths')
    # Selecting a different root must never turn a managed target into a legacy
    # write. Inspect ancestor markers only; do not enumerate private directories.
    for option, value in vars(args).items():
        if option == 'responses' and isinstance(value, str) and value != '-':
            value = Path(value)
        if not isinstance(value, Path):
            continue
        _guard_catalog_target(value, root)
    # A public/unclassified manifest can still name managed private snapshots.
    # Check its bounded nested paths before the legacy compatibility dispatch.
    if getattr(args, 'fixture_manifest', None) is not None:
        from .provenance import strict_json_loads
        source = contained_input(args.fixture_manifest, private_root(args.documents_root, 'documents root'), 'fixture manifest')
        if source.stat().st_size > args.byte_budget:
            raise ScopeError('input_too_large', 'fixture manifest exceeds byte budget')
        manifest = strict_json_loads(source.read_text(encoding='utf-8'))
        if isinstance(manifest, dict) and isinstance(manifest.get('responses'), list):
            for entry in manifest['responses']:
                if isinstance(entry, dict) and isinstance(entry.get('source'), str):
                    _guard_catalog_target(Path(entry['source']), root)
    if args.command == 'legacy' and args.legacy_command == 'inspect':
        from .legacy import inspect_legacy
        return inspect_legacy(root, run=args.run, run_id=args.run_id), 0
    present = (root / 'catalog.sqlite3').exists()
    requested = args.command in ('inventor', 'topic', 'legacy') or bool(getattr(args, 'inventor', None))
    if not present and not requested:
        run_optional = ((args.command == 'scaffold' and args.scaffold_command in ('report', 'feature-map'))
                        or (args.command == 'research' and args.research_command == 'normalize-web'))
        if hasattr(args, 'run') and not run_optional:
            if not args.run or (args.command not in ('delete-run', 'validate') and not getattr(args, 'run_id', None)):
                from .cli import InvalidArgumentsError
                raise InvalidArgumentsError()
        if args.command == 'ideate' and (args.profile is None or args.profile_database is None):
            from .cli import InvalidArgumentsError
            raise InvalidArgumentsError()
        return delegate(args)
    create_catalog = args.command == 'inventor' and args.inventor_command == 'create'
    with Catalog(root, args.documents_root, create=create_catalog and not present) as catalog:
        if args.command in ('inventor', 'topic') and getattr(args, args.command + '_command') in ('create', 'list', 'status', 'archive'):
            return _management(args, catalog), 0
        if args.command == 'legacy':
            from .legacy import inspect_legacy, adopt_legacy
            if args.legacy_command == 'inspect':
                return inspect_legacy(root, run=args.run, run_id=args.run_id), 0
            return adopt_legacy(catalog, inventor_id=_require(args.inventor, 'inventor_required'), topic_id=args.topic, run=args.run, run_id=args.run_id, profile=args.profile), 0
        if args.command in ('profile', 'topic'):
            return _profile(args, catalog, delegate)
        if args.command == 'run' and args.run_command == 'start':
            binding = _bind_start(args, catalog)
        else:
            binding = _resolve_run(args, catalog)
        if args.command == 'delete-run' and binding:
            catalog.mark_run_deleting(args.run)
            if args.run.exists():
                payload, code = delegate(args)
            else:
                payload, code = ({
                    'command': 'delete-run', 'status': 'deleted', 'removed': [], 'failures': [],
                    'root': str(args.run.relative_to(root)), 'reconciled': True,
                }, 0)
            payload.update(_metadata(binding))
            payload['run'] = str(args.run)
            try:
                catalog.mark_run_deleted(args.run, partial=code != 0)
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                # Preserve the actual deletion receipt even if its secondary
                # index is unavailable. An exact retry reconciles an absent run.
                payload['catalog_update'] = {'status': 'pending', 'retryable': True}
                return payload, 11
            payload['catalog_update'] = {'status': 'updated'}
            return payload, code
        payload, code = delegate(args)
        if binding:
            payload.update(_metadata(binding))
            payload['run'] = str(args.run)
        return payload, code
