import { realpathSync } from 'node:fs';
import { isAbsolute, relative, sep } from 'node:path';
import type { ProviderHandlers, SessionStartPayload, SessionEventsPayload } from './types';

/** Session membership uses its initial cwd, never a tool's per-call workdir. */
export function isProjectPath(cwd: string | undefined, root: string): boolean {
  if (cwd === undefined || !isAbsolute(cwd)) return false;
  try {
    const part = relative(realpathSync(root), realpathSync(cwd));
    return part === '' || (part !== '..' && !part.startsWith(`..${sep}`) && !isAbsolute(part));
  } catch {
    return false;
  }
}

type Payload = SessionStartPayload | SessionEventsPayload;

/** Filter both discovery and incremental events before the state manager sees them. */
export function projectHandlers(root: string, target: ProviderHandlers): ProviderHandlers {
  const membership = new Map<string, boolean>();
  const pending = new Map<string, { payload: Payload; start: boolean }>();
  const key = (p: Payload) => `${p.source}:${p.configDir}:${p.sessionId}`;
  const parentKey = (p: Payload) => `${p.source}:${p.configDir}:${p.parentId}`;

  function deliver(payload: Payload, start: boolean): void | Promise<void> {
    return start ? target.onSessionStart(payload) : target.onSessionEvents(payload);
  }

  function handle(payload: Payload, start: boolean): void | Promise<void> {
    const id = key(payload);
    if (!membership.has(id)) {
      if (payload.isSubagent || payload.parentId !== undefined) {
        if (payload.parentId === undefined || payload.parentId === payload.sessionId) return;
        const parent = membership.get(parentKey(payload));
        if (parent === undefined) {
          // Keep only the latest delivery per unresolved child, with a bounded queue.
          if (pending.size >= 1024) pending.delete(pending.keys().next().value!);
          pending.set(id, { payload, start });
          return;
        }
        membership.set(id, parent);
      } else {
        if (payload.sessionCwd === undefined) return;
        membership.set(id, isProjectPath(payload.sessionCwd, root));
      }
    }
    if (!membership.get(id)) return;
    const result = deliver(payload, start);
    // Parents may be discovered after children. Deliver the parent first.
    const flush = () => {
      for (const [childId, entry] of pending) {
        if (parentKey(entry.payload) !== id) continue;
        pending.delete(childId);
        void handle(entry.payload, entry.start);
      }
    };
    if (result instanceof Promise) return result.then(flush);
    flush();
  }

  return {
    onSessionStart: (payload) => handle(payload, true),
    onSessionEvents: (payload) => { void handle(payload, false); },
  };
}
