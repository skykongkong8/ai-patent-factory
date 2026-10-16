"""Local inventor/topic catalog, immutable profile versions and input snapshots.

The catalog indexes ownership; facts remain authoritative in each version's
profile database. Every public result is metadata-only.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from .database import (connect_database, connect_read_only_database,
                       consistent_snapshot, immediate_transaction, profile_payload,
                       utc_now)
from .paths import (contained_input, contained_output, owner_only_file,
                    private_contained_directory, private_root)
from .profile import ALLOWED_SUFFIXES, MAX_DOCUMENT_BYTES, atomic_write_profile
from .provenance import canonical_json, digest, normalize

CATALOG_VERSION = 1
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
_SCHEMA = """
CREATE TABLE catalog_config (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 workspace_relative TEXT NOT NULL, documents_relative TEXT NOT NULL
);
CREATE TABLE inventors (
 inventor_id TEXT PRIMARY KEY, name TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
 profile_version_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE topics (
 topic_id TEXT PRIMARY KEY, inventor_id TEXT NOT NULL REFERENCES inventors(inventor_id),
 name TEXT NOT NULL, tags_json TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
 topic_version_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE versions (
 version_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('profile','topic')),
 inventor_id TEXT NOT NULL REFERENCES inventors(inventor_id),
 topic_id TEXT REFERENCES topics(topic_id), parent_version_id TEXT REFERENCES versions(version_id),
 replace_mode INTEGER NOT NULL, status TEXT NOT NULL, directory TEXT NOT NULL UNIQUE,
 database_path TEXT NOT NULL UNIQUE, export_path TEXT NOT NULL UNIQUE,
 profile_revision TEXT, profile_hash TEXT, created_at TEXT NOT NULL,
 CHECK((kind='profile' AND topic_id IS NULL) OR (kind='topic' AND topic_id IS NOT NULL))
);
CREATE TABLE legacy_sources (
 source_path TEXT PRIMARY KEY, source_hash TEXT NOT NULL,
 inventor_id TEXT NOT NULL REFERENCES inventors(inventor_id),
 version_id TEXT REFERENCES versions(version_id), created_at TEXT NOT NULL
);
CREATE TABLE input_snapshots (
 input_id TEXT PRIMARY KEY, inventor_id TEXT NOT NULL REFERENCES inventors(inventor_id),
 topic_id TEXT REFERENCES topics(topic_id), mode TEXT NOT NULL,
 original_path TEXT NOT NULL, snapshot_path TEXT NOT NULL UNIQUE,
 manifest_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE version_inputs (
 version_id TEXT NOT NULL REFERENCES versions(version_id),
 input_id TEXT NOT NULL REFERENCES input_snapshots(input_id), PRIMARY KEY(version_id,input_id)
);
CREATE TABLE run_bindings (
 run_id TEXT PRIMARY KEY, run_path TEXT NOT NULL UNIQUE,
 inventor_id TEXT NOT NULL REFERENCES inventors(inventor_id),
 topic_id TEXT NOT NULL REFERENCES topics(topic_id),
 profile_version_id TEXT REFERENCES versions(version_id),
 topic_version_id TEXT REFERENCES versions(version_id),
 legacy INTEGER NOT NULL DEFAULT 0, legacy_profile_database TEXT, legacy_profile_export TEXT,
 status TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


class CatalogError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or value in {'.', '..'}:
        raise CatalogError('catalog_invalid_id', f'{label}: path-safe identifier required')
    return value


def _text(value: str, label: str) -> str:
    value = normalize(value)
    if not isinstance(value, str) or not value or len(value) > 500:
        raise CatalogError('catalog_invalid_input', f'{label}: non-empty text of at most 500 characters required')
    return value


def _relative(path: Path) -> str:
    return path.relative_to(Path.cwd()).as_posix()


def exists(workspace_root: Path) -> bool:
    """Check existence through the same non-symlink root policy as opening."""
    relative = Path(workspace_root)
    if relative.is_absolute() or '..' in relative.parts:
        raise CatalogError('catalog_unsafe_path', 'catalog root must be repository-relative')
    if not relative.exists():
        return False
    root = private_root(relative, 'workspace root')
    path = contained_output(relative / 'catalog.sqlite3', root, 'catalog')
    return path.exists()


class Catalog:
    def __init__(self, workspace_root: Path, documents_root: Path = Path('documents'), *, create: bool = False):
        self.workspace_relative = Path(workspace_root)
        self.documents_relative = Path(documents_root)
        self.workspace_root = private_root(self.workspace_relative, 'workspace root', create=create)
        self.documents_root = private_root(self.documents_relative, 'documents root', create=create)
        self.workspace_relative = Path(_relative(self.workspace_root))
        self.documents_relative = Path(_relative(self.documents_root))
        if self.workspace_root == self.documents_root or self.workspace_root in self.documents_root.parents or self.documents_root in self.workspace_root.parents:
            raise CatalogError('catalog_unsafe_path', 'workspace and documents roots must be separate')
        self.path = contained_output(self.workspace_relative / 'catalog.sqlite3', self.workspace_root, 'catalog')
        if not self.path.exists():
            if not create:
                raise CatalogError('catalog_missing', 'inventor catalog is not initialized')
            # Exclusive creation prevents a concurrent initializer from replacing state.
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        owner_only_file(self.path)
        if create:
            connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
            try:
                with immediate_transaction(connection):
                    version = connection.execute('PRAGMA user_version').fetchone()[0]
                    if version == 0:
                        if connection.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone():
                            raise CatalogError('catalog_schema', 'unrecognized catalog database')
                        for statement in _SCHEMA.split(';'):
                            if statement.strip():
                                connection.execute(statement)
                        connection.execute('INSERT INTO catalog_config VALUES(1,?,?)',
                                           (self.workspace_relative.as_posix(), self.documents_relative.as_posix()))
                        connection.execute(f'PRAGMA user_version={CATALOG_VERSION}')
                    elif version != CATALOG_VERSION:
                        raise CatalogError('catalog_schema', 'unsupported catalog schema')
            finally:
                connection.close()
        self.connection = self._connect(readonly=True)
        self._held_locks: dict[str, tuple[int, int]] = {}

    def _connect(self, *, readonly: bool) -> sqlite3.Connection:
        contained_input(self.workspace_relative / 'catalog.sqlite3', self.workspace_root, 'catalog')
        mode = 'ro' if readonly else 'rw'
        connection = sqlite3.connect(f'file:{quote(str(self.path))}?mode={mode}', uri=True,
                                     isolation_level=None, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        if readonly:
            connection.execute('PRAGMA query_only=ON')
        try:
            if connection.execute('PRAGMA user_version').fetchone()[0] != CATALOG_VERSION:
                raise CatalogError('catalog_schema', 'unsupported catalog schema')
            try:
                roots = connection.execute(
                    'SELECT workspace_relative,documents_relative FROM catalog_config WHERE singleton=1'
                ).fetchone()
            except sqlite3.DatabaseError:
                raise CatalogError('catalog_schema', 'catalog schema has no registered roots') from None
            if roots is None:
                raise CatalogError('catalog_schema', 'catalog schema has no registered roots')
            if tuple(roots) != (self.workspace_relative.as_posix(), self.documents_relative.as_posix()):
                raise CatalogError('catalog_scope_mismatch', 'configured roots do not match registered catalog scope')
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect(readonly=False)
        try:
            with immediate_transaction(connection):
                yield connection
        finally:
            connection.close()

    def __enter__(self) -> 'Catalog':
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _entity_metadata(kind: str, row: sqlite3.Row) -> dict[str, Any]:
        result = {key: row[key] for key in ('inventor_id', 'created_at')}
        result.update(archived=bool(row['archived']), status='archived' if row['archived'] else 'active')
        if kind == 'inventor':
            result['profile_version_id'] = row['profile_version_id']
        else:
            result.update(topic_id=row['topic_id'], topic_version_id=row['topic_version_id'],
                          tag_count=len(json.loads(row['tags_json'])))
        return result

    def _entity_row(self, connection: sqlite3.Connection, kind: str, identifier: str,
                    inventor_id: str | None = None) -> sqlite3.Row:
        if kind not in {'inventor', 'topic'}:
            raise CatalogError('catalog_invalid_kind', 'entity kind must be inventor or topic')
        _identifier(identifier, kind)
        table, key = ('inventors', 'inventor_id') if kind == 'inventor' else ('topics', 'topic_id')
        row = connection.execute(f'SELECT * FROM {table} WHERE {key}=?', (identifier,)).fetchone()
        if row is None:
            raise CatalogError('catalog_entity_missing', f'{kind} is not registered')
        if inventor_id is not None and row['inventor_id'] != inventor_id:
            raise CatalogError('catalog_scope_mismatch', 'topic owner scope mismatch')
        return row

    def entity(self, kind: str, identifier: str, inventor_id: str | None = None) -> dict[str, Any]:
        return self._entity_metadata(kind, self._entity_row(self.connection, kind, identifier, inventor_id))

    def _active_owner(self, connection: sqlite3.Connection, inventor_id: str, topic_id: str | None) -> None:
        inventor = self._entity_row(connection, 'inventor', inventor_id)
        topic = self._entity_row(connection, 'topic', topic_id, inventor_id) if topic_id else None
        if inventor['archived'] or (topic is not None and topic['archived']):
            raise CatalogError('catalog_archived', 'archived inventor or topic cannot start new work')

    def create_inventor(self, name: str) -> dict[str, Any]:
        name = _text(name, 'inventor name')
        identifier = 'iv_' + uuid.uuid4().hex[:24]
        with self._write() as connection:
            connection.execute('INSERT INTO inventors(inventor_id,name,created_at) VALUES(?,?,?)',
                               (identifier, name, utc_now()))
        return self.entity('inventor', identifier)

    def create_topic(self, inventor_id: str, name: str, tags: list[str]) -> dict[str, Any]:
        name = _text(name, 'topic name')
        if not isinstance(tags, list) or len(tags) > 100:
            raise CatalogError('catalog_invalid_input', 'topic tags must be a bounded array')
        tags = sorted({_text(tag, 'topic tag') for tag in tags})
        identifier = 'tp_' + uuid.uuid4().hex[:24]
        with self._write() as connection:
            self._active_owner(connection, inventor_id, None)
            connection.execute('INSERT INTO topics(topic_id,inventor_id,name,tags_json,created_at) VALUES(?,?,?,?,?)',
                               (identifier, inventor_id, name, canonical_json(tags), utc_now()))
        return self.entity('topic', identifier)

    def list_inventors(self) -> list[dict[str, Any]]:
        return [self._entity_metadata('inventor', row) for row in self.connection.execute('SELECT * FROM inventors ORDER BY inventor_id')]

    def list_topics(self, inventor_id: str) -> list[dict[str, Any]]:
        self._entity_row(self.connection, 'inventor', inventor_id)
        return [self._entity_metadata('topic', row) for row in self.connection.execute('SELECT * FROM topics WHERE inventor_id=? ORDER BY topic_id', (inventor_id,))]

    def resolve_name(self, kind: str, name: str, inventor_id: str | None = None) -> list[str]:
        name = _text(name, 'name')
        if kind == 'inventor':
            rows = self.connection.execute('SELECT inventor_id FROM inventors WHERE name=? ORDER BY inventor_id', (name,))
        elif kind == 'topic':
            self._entity_row(self.connection, 'inventor', inventor_id)
            rows = self.connection.execute('SELECT topic_id FROM topics WHERE inventor_id=? AND name=? ORDER BY topic_id', (inventor_id, name))
        else:
            raise CatalogError('catalog_invalid_kind', 'entity kind must be inventor or topic')
        return [row[0] for row in rows]

    def archive(self, kind: str, identifier: str) -> dict[str, Any]:
        with self._write() as connection:
            self._entity_row(connection, kind, identifier)
            table, key = ('inventors', 'inventor_id') if kind == 'inventor' else ('topics', 'topic_id')
            connection.execute(f'UPDATE {table} SET archived=1 WHERE {key}=?', (identifier,))
        return self.entity(kind, identifier)

    def _owner(self, connection: sqlite3.Connection, kind: str, inventor_id: str,
               topic_id: str | None) -> tuple[str, str, str]:
        if kind == 'profile' and topic_id is None:
            self._entity_row(connection, 'inventor', inventor_id)
            return 'inventors', 'inventor_id', 'profile_version_id'
        if kind == 'topic' and topic_id is not None:
            self._entity_row(connection, 'topic', topic_id, inventor_id)
            return 'topics', 'topic_id', 'topic_version_id'
        raise CatalogError('catalog_scope_mismatch', 'profile/topic owner scope mismatch')

    def _head(self, connection: sqlite3.Connection, kind: str, inventor_id: str, topic_id: str | None) -> str | None:
        table, key, head = self._owner(connection, kind, inventor_id, topic_id)
        return connection.execute(f'SELECT {head} FROM {table} WHERE {key}=?', (topic_id or inventor_id,)).fetchone()[0]

    @staticmethod
    def _version_metadata(row: sqlite3.Row) -> dict[str, Any]:
        result = {key: row[key] for key in ('version_id', 'kind', 'inventor_id', 'topic_id',
                  'parent_version_id', 'status', 'directory', 'profile_revision', 'profile_hash', 'created_at')}
        result.update(database=row['database_path'], export=row['export_path'], replace=bool(row['replace_mode']))
        return result

    def version(self, version_id: str) -> dict[str, Any]:
        _identifier(version_id, 'version')
        row = self.connection.execute('SELECT * FROM versions WHERE version_id=?', (version_id,)).fetchone()
        if row is None:
            raise CatalogError('catalog_version_missing', 'profile/topic version is unavailable')
        return self._version_metadata(row)

    def current_version(self, kind: str, inventor_id: str, topic_id: str | None = None) -> dict[str, Any]:
        identifier = self._head(self.connection, kind, inventor_id, topic_id)
        if identifier is None:
            raise CatalogError('catalog_version_missing', 'owner has no active profile/topic version')
        return self.version(identifier)

    def list_versions(self, inventor_id: str, topic_id: str | None = None) -> list[dict[str, Any]]:
        self._entity_row(self.connection, 'inventor', inventor_id)
        if topic_id is not None:
            self._entity_row(self.connection, 'topic', topic_id, inventor_id)
        return [self._version_metadata(row) for row in self.connection.execute(
            'SELECT * FROM versions WHERE inventor_id=? AND topic_id IS ? ORDER BY created_at,version_id',
            (inventor_id, topic_id))]

    @contextmanager
    def edit_version(self, version_id: str) -> Iterator[dict[str, Any]]:
        """Lease one draft across ingestion and activation, including retries.

        Reentrant within the owning thread so callers can wrap an entire setup;
        another thread/process receives a retryable refusal without waiting.
        """
        version = self.version(version_id)
        if version['status'] == 'ready':
            raise CatalogError('catalog_version_immutable', 'published ready version is immutable')
        held = self._held_locks.get(version_id)
        if held is not None and held[0] == threading.get_ident():
            yield version
            return
        private_contained_directory(Path(version['directory']), self.workspace_root,
                                    'version directory', create=True)
        lock_path = contained_output(Path(version['directory']) / '.edit.lock',
                                     self.workspace_root, 'version edit lock')
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            owner_only_file(lock_path)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CatalogError('catalog_version_busy', 'candidate version is being edited; retry the operation') from None
            self._held_locks[version_id] = (threading.get_ident(), descriptor)
            try:
                version = self.version(version_id)
                if version['status'] == 'ready':
                    raise CatalogError('catalog_version_immutable', 'published ready version is immutable')
                yield version
            finally:
                self._held_locks.pop(version_id, None)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def prepare_version(self, kind: str, inventor_id: str, topic_id: str | None = None, *,
                        replace: bool = False, version_id: str | None = None) -> dict[str, Any]:
        with self._write() as connection:
            self._owner(connection, kind, inventor_id, topic_id)
            self._active_owner(connection, inventor_id, topic_id)
            row = None
            if version_id is not None:
                _identifier(version_id, 'version')
                if not version_id.startswith('pv_' if kind == 'profile' else 'tv_'):
                    raise CatalogError('catalog_invalid_id', 'candidate identifier prefix does not match kind')
                row = connection.execute('SELECT * FROM versions WHERE version_id=?', (version_id,)).fetchone()
                if row is not None:
                    if (row['kind'], row['inventor_id'], row['topic_id'], bool(row['replace_mode'])) != (kind, inventor_id, topic_id, replace):
                        raise CatalogError('catalog_scope_mismatch', 'candidate version owner or replacement scope mismatch')
                    if row['status'] == 'ready':
                        raise CatalogError('catalog_version_immutable', 'published ready version is immutable')
            if row is None:
                version_id = version_id or ('pv_' if kind == 'profile' else 'tv_') + uuid.uuid4().hex[:24]
                base = self.workspace_relative / 'inventors' / inventor_id
                directory = base / 'profiles' / version_id if kind == 'profile' else base / 'topics' / topic_id / 'contexts' / version_id
                if directory.exists() or directory.is_symlink():
                    raise CatalogError('catalog_path_occupied', 'unregistered version directory already exists')
                connection.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (version_id, kind, inventor_id, topic_id,
                                    self._head(connection, kind, inventor_id, topic_id), int(replace),
                                    'preparing', directory.as_posix(), (directory / 'profile.sqlite3').as_posix(),
                                    (directory / 'profile.json').as_posix(), None, None, utc_now()))
                row = connection.execute('SELECT * FROM versions WHERE version_id=?', (version_id,)).fetchone()
        if row['status'] == 'preparing':
            with self.edit_version(version_id):
                # Refresh after acquiring the lease: a concurrent recovery may
                # already have initialized this candidate.
                with self._write() as connection:
                    row = connection.execute('SELECT * FROM versions WHERE version_id=?', (version_id,)).fetchone()
                    if row['status'] == 'preparing':
                        version = self._version_metadata(row)
                        database = contained_output(Path(version['database']), self.workspace_root, 'version database')
                        destination = connect_database(database)
                        try:
                            if row['parent_version_id'] and not replace:
                                parent = self.version(row['parent_version_id'])
                                source = connect_read_only_database(contained_input(Path(parent['database']), self.workspace_root, 'parent version database'))
                                try:
                                    source.backup(destination)
                                finally:
                                    source.close()
                        finally:
                            destination.close()
                        connection.execute("UPDATE versions SET status='draft' WHERE version_id=?", (version_id,))
        return self.version(version_id)

    def finish_version(self, version_id: str) -> dict[str, Any]:
        version = self.version(version_id)
        if version['status'] == 'ready':
            return version
        with self.edit_version(version_id):
            return self._finish_version(version_id)

    def _finish_version(self, version_id: str) -> dict[str, Any]:
        version = self.version(version_id)
        database = contained_input(Path(version['database']), self.workspace_root, 'version database')
        connection = connect_read_only_database(database)
        try:
            with consistent_snapshot(connection):
                payload = profile_payload(connection)
        finally:
            connection.close()
        with self._write() as catalog:
            self._active_owner(catalog, version['inventor_id'], version['topic_id'])
            fresh = catalog.execute('SELECT * FROM versions WHERE version_id=?', (version_id,)).fetchone()
            if fresh['status'] == 'ready':
                return self._version_metadata(fresh)
            if payload['state'] != 'profile_ready':
                catalog.execute('UPDATE versions SET status=?,profile_revision=? WHERE version_id=?',
                                (payload['state'], payload['profile_revision'], version_id))
            else:
                head = self._head(catalog, version['kind'], version['inventor_id'], version['topic_id'])
                if head != version['parent_version_id']:
                    raise CatalogError('catalog_stale_head', 'active version changed while candidate was prepared')
                export = contained_output(Path(version['export']), self.workspace_root, 'version export')
                atomic_write_profile(export, payload)
                catalog.execute("UPDATE versions SET status='ready',profile_revision=?,profile_hash=? WHERE version_id=?",
                                (payload['profile_revision'], digest(payload), version_id))
                table, key, active = self._owner(catalog, version['kind'], version['inventor_id'], version['topic_id'])
                catalog.execute(f'UPDATE {table} SET {active}=? WHERE {key}=?',
                                (version_id, version['topic_id'] or version['inventor_id']))
        return self.version(version_id)

    def _source_owner(self, source: Path, version: dict[str, Any], *, directory: bool) -> None:
        parts = source.relative_to(self.documents_root).parts
        if directory and (not parts or (parts[0] == 'inventors' and len(parts) < 3)):
            raise CatalogError('catalog_input_scope', 'folder overlaps managed input root')
        if not parts or parts[0] != 'inventors':
            return
        if len(parts) < 3 or parts[1] != version['inventor_id']:
            raise CatalogError('catalog_input_scope', 'managed source owner scope mismatch')
        if parts[2] == 'profile':
            if directory and len(parts) == 3:
                raise CatalogError('catalog_input_scope', 'folder overlaps managed input snapshot parent')
            return
        if len(parts) < 5 or parts[2] != 'topics' or parts[3] != version['topic_id']:
            raise CatalogError('catalog_input_scope', 'managed source topic owner scope mismatch')

    @staticmethod
    def _read_document(path: Path) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise CatalogError('catalog_input_invalid', 'input requires a regular file')
            if metadata.st_size > MAX_DOCUMENT_BYTES:
                raise CatalogError('catalog_input_invalid', 'input document too large')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                content = stream.read(MAX_DOCUMENT_BYTES + 1)
            if len(content) > MAX_DOCUMENT_BYTES:
                raise CatalogError('catalog_input_invalid', 'input document too large')
            return content
        finally:
            os.close(descriptor)

    def record_input(self, version_id: str, source: Path, mode: str) -> Path:
        with self.edit_version(version_id):
            return self._record_input(version_id, source, mode)

    def _record_input(self, version_id: str, source: Path, mode: str) -> Path:
        version = self.version(version_id)
        if version['status'] == 'ready':
            raise CatalogError('catalog_version_immutable', 'published version input is immutable')
        self._active_owner(self.connection, version['inventor_id'], version['topic_id'])
        if mode not in {'document', 'folder', 'interview'}:
            raise CatalogError('catalog_invalid_input', 'unsupported input mode')
        source = contained_input(Path(source), self.documents_root, 'profile input', directory=mode == 'folder')
        self._source_owner(source, version, directory=mode == 'folder')
        files: list[tuple[str, Path, bytes]] = []
        def collect(folder: Path) -> None:
            for child in sorted(folder.iterdir(), key=lambda path: path.name):
                child_mode = child.lstat().st_mode
                if stat.S_ISLNK(child_mode):
                    raise CatalogError('catalog_input_invalid', 'input rejected: symbolic link')
                if stat.S_ISDIR(child_mode):
                    collect(child)
                elif not stat.S_ISREG(child_mode):
                    raise CatalogError('catalog_input_invalid', 'input rejected: nonregular path')
                elif child.suffix.lower() in ALLOWED_SUFFIXES:
                    files.append((child.relative_to(source).as_posix(), child, self._read_document(child)))
        if mode == 'folder':
            collect(source)
        else:
            if source.suffix.lower() not in ALLOWED_SUFFIXES:
                raise CatalogError('catalog_input_invalid', 'unsupported input document')
            files.append((source.name, source, self._read_document(source)))
        manifest = [{'original_path': _relative(path), 'relative_path': relative,
                     'byte_hash': hashlib.sha256(content).hexdigest(), 'byte_size': len(content)}
                    for relative, path, content in files]
        input_id = 'ip_' + digest({'inventor_id': version['inventor_id'], 'topic_id': version['topic_id'],
                                   'mode': mode, 'source': _relative(source), 'manifest': manifest})[:24]
        base = self.documents_relative / 'inventors' / version['inventor_id']
        directory = base / 'profile' / input_id if version['kind'] == 'profile' else base / 'topics' / version['topic_id'] / input_id
        snapshot = directory if mode == 'folder' else directory / source.name
        for item in manifest:
            item['snapshot_path'] = (directory / item['relative_path']).as_posix()
        with self._write() as connection:
            connection.execute('INSERT OR IGNORE INTO input_snapshots VALUES(?,?,?,?,?,?,?,?,?)',
                               (input_id, version['inventor_id'], version['topic_id'], mode,
                                _relative(source), snapshot.as_posix(), canonical_json(manifest), 'preparing', utc_now()))
        private_contained_directory(directory, self.documents_root, 'input snapshot', create=True)
        for relative, _, content in files:
            destination = directory / relative
            private_contained_directory(destination.parent, self.documents_root, 'input snapshot directory', create=True)
            absolute = contained_output(destination, self.documents_root, 'input snapshot file')
            descriptor, temporary = tempfile.mkstemp(prefix='.snapshot-', dir=absolute.parent)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, absolute, follow_symlinks=False)
                except FileExistsError:
                    if self._read_document(absolute) != content:
                        raise CatalogError('catalog_snapshot_mismatch', 'immutable input snapshot hash mismatch')
            finally:
                os.unlink(temporary)
            owner_only_file(absolute)
        with self._write() as connection:
            row = connection.execute('SELECT status FROM versions WHERE version_id=?', (version_id,)).fetchone()
            if row['status'] == 'ready':
                raise CatalogError('catalog_version_immutable', 'published version input is immutable')
            connection.execute("UPDATE input_snapshots SET status='ready' WHERE input_id=?", (input_id,))
            connection.execute('INSERT OR IGNORE INTO version_inputs VALUES(?,?)', (version_id, input_id))
        return snapshot

    def input_manifest(self, version_id: str) -> list[dict[str, Any]]:
        self.version(version_id)
        result = []
        for row in self.connection.execute('SELECT s.* FROM input_snapshots s JOIN version_inputs v USING(input_id) WHERE v.version_id=? ORDER BY s.input_id', (version_id,)):
            for entry in json.loads(row['manifest_json']):
                result.append(dict(entry, input_id=row['input_id'], mode=row['mode']))
        return result

    def legacy_source(self, source: Path) -> dict[str, Any] | None:
        source = contained_input(Path(source), self.workspace_root, 'legacy source database')
        row = self.connection.execute('SELECT * FROM legacy_sources WHERE source_path=?', (_relative(source),)).fetchone()
        return dict(row) if row else None

    def register_legacy_source(self, source: Path, inventor_id: str, *, source_hash: str,
                               version_id: str | None = None) -> dict[str, Any]:
        source = contained_input(Path(source), self.workspace_root, 'legacy source database')
        if not isinstance(source_hash, str) or not re.fullmatch('[0-9a-f]{64}', source_hash):
            raise CatalogError('catalog_invalid_input', 'legacy source hash must be SHA-256')
        if version_id is not None:
            version = self.version(version_id)
            if version['kind'] != 'profile' or version['inventor_id'] != inventor_id:
                raise CatalogError('catalog_scope_mismatch', 'legacy source candidate owner scope mismatch')
        with self._write() as connection:
            self._active_owner(connection, inventor_id, None)
            row = connection.execute('SELECT * FROM legacy_sources WHERE source_path=?', (_relative(source),)).fetchone()
            if row:
                if row['inventor_id'] != inventor_id:
                    raise CatalogError('catalog_scope_mismatch', 'legacy source owner scope mismatch')
                if row['source_hash'] != source_hash:
                    raise CatalogError('catalog_legacy_changed', 'legacy source hash changed after adoption claim')
                if version_id is not None:
                    if row['version_id'] is not None and row['version_id'] != version_id:
                        raise CatalogError('catalog_scope_mismatch', 'legacy source candidate scope mismatch')
                    connection.execute('UPDATE legacy_sources SET version_id=? WHERE source_path=?', (version_id, _relative(source)))
            else:
                connection.execute('INSERT INTO legacy_sources VALUES(?,?,?,?,?)',
                                   (_relative(source), source_hash, inventor_id, version_id, utc_now()))
        return self.legacy_source(Path(_relative(source)))

    def _run_path(self, path: Path) -> Path:
        # Binding precedes bootstrap, so only containment and existing chain are checked.
        path = Path(path)
        if path.is_absolute() or '..' in path.parts:
            raise CatalogError('catalog_unsafe_path', 'run path must be repository-relative')
        current = Path.cwd()
        for part in path.parts:
            current /= part
            if current.is_symlink():
                raise CatalogError('catalog_unsafe_path', 'run path rejected: symbolic link')
        absolute = (Path.cwd() / path).resolve(strict=False)
        if absolute == self.workspace_root or self.workspace_root not in absolute.parents:
            raise CatalogError('catalog_unsafe_path', 'run path outside configured workspace')
        return Path(_relative(absolute))

    @staticmethod
    def _binding_metadata(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result['legacy'] = bool(row['legacy'])
        return result

    def bind_run(self, run_path: Path, run_id: str, inventor_id: str, topic_id: str,
                 profile_version_id: str | None, topic_version_id: str | None, *, legacy: bool = False,
                 legacy_profile_database: str | None = None, legacy_profile_export: str | None = None) -> dict[str, Any]:
        run_id = _identifier(run_id, 'run')
        path = self._run_path(run_path)
        expected = self.workspace_relative / 'inventors' / inventor_id / 'topics' / topic_id / 'runs' / run_id
        if not legacy and path != expected:
            raise CatalogError('catalog_scope_mismatch', 'managed run path does not match owner scope')
        if not legacy and (legacy_profile_database is not None or legacy_profile_export is not None):
            raise CatalogError('catalog_scope_mismatch', 'legacy profile paths require legacy binding')
        for version_id, kind in ((profile_version_id, 'profile'), (topic_version_id, 'topic')):
            if version_id is None and legacy:
                continue
            version = self.version(version_id)
            if version['status'] != 'ready' or version['kind'] != kind or version['inventor_id'] != inventor_id or version['topic_id'] != (topic_id if kind == 'topic' else None):
                raise CatalogError('catalog_scope_mismatch', 'run pinned version owner scope mismatch')
        historical_root = self.workspace_root / 'inventors' / inventor_id / 'topics' / topic_id / 'legacy-contexts'
        for value, label in ((legacy_profile_database, 'legacy profile database'), (legacy_profile_export, 'legacy profile export')):
            if value is not None:
                resolved = contained_input(Path(value), self.workspace_root, label)
                if historical_root not in resolved.parents:
                    raise CatalogError('catalog_scope_mismatch', 'historical profile path owner scope mismatch')
        scope = (path.as_posix(), inventor_id, topic_id, profile_version_id, topic_version_id,
                 int(legacy), legacy_profile_database, legacy_profile_export)
        with self._write() as connection:
            self._entity_row(connection, 'topic', topic_id, inventor_id)
            existing = connection.execute('SELECT * FROM run_bindings WHERE run_id=? OR run_path=?', (run_id, path.as_posix())).fetchone()
            if existing is not None:
                actual = tuple(existing[key] for key in ('run_path', 'inventor_id', 'topic_id', 'profile_version_id', 'topic_version_id', 'legacy', 'legacy_profile_database', 'legacy_profile_export'))
                if actual != scope or existing['run_id'] != run_id:
                    raise CatalogError('catalog_scope_mismatch', 'run binding scope mismatch')
                return self._binding_metadata(existing)
            self._active_owner(connection, inventor_id, topic_id)
            connection.execute('INSERT INTO run_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (run_id, *scope, 'ready', utc_now()))
        return self.run_binding(path, run_id)

    def run_binding(self, run_path: Path, run_id: str | None = None) -> dict[str, Any] | None:
        path = self._run_path(run_path)
        row = self.connection.execute('SELECT * FROM run_bindings WHERE run_path=?', (path.as_posix(),)).fetchone()
        if row is None:
            return None
        if run_id is not None and run_id != row['run_id']:
            raise CatalogError('catalog_run_mismatch', 'requested run ID does not match registered run')
        return self._binding_metadata(row)

    def list_runs(self, inventor_id: str | None = None, topic_id: str | None = None) -> list[dict[str, Any]]:
        clauses, parameters = [], []
        if inventor_id is not None:
            self._entity_row(self.connection, 'inventor', inventor_id)
            clauses.append('inventor_id=?')
            parameters.append(inventor_id)
        if topic_id is not None:
            self._entity_row(self.connection, 'topic', topic_id, inventor_id)
            clauses.append('topic_id=?')
            parameters.append(topic_id)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        return [self._binding_metadata(row) for row in self.connection.execute('SELECT * FROM run_bindings' + where + ' ORDER BY run_id', parameters)]

    def mark_run_deleting(self, run_path: Path) -> None:
        """Persist intent before destructive core cleanup, so IDs cannot resurrect."""
        path = self._run_path(run_path)
        with self._write() as connection:
            connection.execute("UPDATE run_bindings SET status='deleting' WHERE run_path=?", (path.as_posix(),))

    def mark_run_deleted(self, run_path: Path, partial: bool) -> None:
        path = self._run_path(run_path)
        with self._write() as connection:
            connection.execute('UPDATE run_bindings SET status=? WHERE run_path=?',
                               ('deletion_partial' if partial else 'deleted', path.as_posix()))
