import { expect, test } from 'bun:test';
import { mkdtemp, mkdir, writeFile, appendFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ClaudeProvider } from './claude-provider';

test('Claude discovery and later growth consume the same complete-line boundary exactly once', async () => {
  const root = await mkdtemp(join(tmpdir(), 'patkong-claude-prefix-'));
  const project = join(root, 'projects', 'public');
  await mkdir(project, { recursive: true });
  const file = join(project, 'prefix-session.jsonl');
  const timestamp = new Date().toISOString();
  const user = JSON.stringify({ type: 'user', sessionId: 'prefix-session', cwd: '/public', timestamp, message: { role: 'user', content: 'public test' } });
  const tool = JSON.stringify({ type: 'assistant', sessionId: 'prefix-session', cwd: '/public', timestamp, message: { role: 'assistant', content: [{ type: 'tool_use', id: 'once', name: 'Read', input: { file_path: '/public/README.md' } }] } });
  await writeFile(file, user + '\n' + tool); // Valid JSON, but the last line is still uncommitted.
  const provider = new ClaudeProvider({ claudeDirs: [root], pollIntervalMs: 15 });
  let discoveries = 0;
  const ids: string[] = [];
  const capture = (p: { events: Array<{ toolCalls: Array<{ id: string }> }> }) => {
    for (const event of p.events) for (const tc of event.toolCalls) ids.push(tc.id);
  };
  const until = async (ready: () => boolean) => {
    const end = Date.now() + 2000;
    while (!ready() && Date.now() < end) await Bun.sleep(10);
    expect(ready()).toBe(true);
  };
  try {
    await provider.start({ onSessionStart: (p) => { discoveries++; capture(p); }, onSessionEvents: capture });
    await until(() => discoveries === 1);
    expect(ids).toEqual([]);
    await appendFile(file, '\n');
    await until(() => ids.length >= 1);
    expect(ids).toEqual(['once']);
  } finally {
    provider.stop();
    await rm(root, { recursive: true, force: true });
  }
});
