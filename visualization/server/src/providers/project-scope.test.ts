import { describe, expect, test } from 'bun:test';
import { tmpdir } from 'node:os';
import { mkdtempSync, mkdirSync, rmSync, symlinkSync } from 'node:fs';
import { join } from 'node:path';
import { projectHandlers, isProjectPath } from './project-scope';
import type { SessionStartPayload, SessionEventsPayload } from './types';

const root = mkdtempSync(join(tmpdir(), 'patkong-scope-'));
mkdirSync(join(root, 'subdir'));
process.on('exit', () => rmSync(root, { recursive: true, force: true }));

function session(id: string, cwd?: string, parentId?: string): SessionStartPayload {
  return {
    source: 'codex', sessionId: id, configDir: '/synthetic/codex',
    sessionCwd: cwd, parentId, isSubagent: parentId !== undefined,
    events: [],
  };
}

describe('project scope before state, API, and WebSocket publication', () => {
  test('matches path boundaries and rejects relative paths and symlink escapes', () => {
    expect(isProjectPath(root, root)).toBe(true);
    expect(isProjectPath(join(root, 'subdir'), root)).toBe(true);
    expect(isProjectPath(`${root}-other`, root)).toBe(false);
    expect(isProjectPath('subdir', root)).toBe(false);
    expect(isProjectPath(undefined, root)).toBe(false);
    symlinkSync(tmpdir(), join(root, 'outside'));
    expect(isProjectPath(join(root, 'outside'), root)).toBe(false);
  });

  test('does not deliver foreign or unknown sessions, including subsequent updates', async () => {
    const seen: string[] = [];
    const handlers = projectHandlers(root, {
      onSessionStart: (p) => { seen.push(p.sessionId); },
      onSessionEvents: (p) => { seen.push(p.sessionId); },
    });
    await handlers.onSessionStart(session('ours', root));
    await handlers.onSessionStart(session('foreign', tmpdir()));
    await handlers.onSessionStart(session('unknown'));
    handlers.onSessionEvents(session('foreign', root));
    handlers.onSessionEvents(session('ours', tmpdir()));
    expect(seen).toEqual(['ours', 'ours']);
  });

  test('child membership requires its verified parent and supports discovery before parent', async () => {
    const seen: string[] = [];
    const handlers = projectHandlers(root, {
      onSessionStart: (p) => { seen.push(p.sessionId); },
      onSessionEvents: (p) => { seen.push(p.sessionId); },
    });
    await handlers.onSessionStart(session('orphan', root, 'unknown'));
    await handlers.onSessionStart(session('child', undefined, 'parent'));
    await handlers.onSessionStart(session('parent', root));
    expect(seen).toEqual(['parent', 'child']);
    await handlers.onSessionStart(session('foreign-parent', tmpdir()));
    await handlers.onSessionStart(session('foreign-child', root, 'foreign-parent'));
    expect(seen).toEqual(['parent', 'child']);
  });

  test('resumed sessions can first arrive as updates', () => {
    const seen: SessionEventsPayload[] = [];
    const handlers = projectHandlers(root, {
      onSessionStart: () => {}, onSessionEvents: (p) => { seen.push(p); },
    });
    handlers.onSessionEvents(session('resumed', root));
    expect(seen.map((p) => p.sessionId)).toEqual(['resumed']);
  });
});
