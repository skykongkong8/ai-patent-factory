import { expect, test } from 'bun:test';
import { mkdtemp, mkdir, writeFile, appendFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

test('synthetic Codex and Claude logs stay scoped across HTTP, live events and reconnects', async () => {
  const temp = await mkdtemp(join(tmpdir(), 'patkong-e2e-'));
  const projectRoot = resolve(import.meta.dir, '../../..');
  const codex = join(temp, 'codex');
  const claude = join(temp, 'claude');
  const day = join(codex, 'sessions', 'demo');
  const claudeProject = join(claude, 'projects', 'demo');
  await mkdir(day, { recursive: true });
  await mkdir(claudeProject, { recursive: true });
  const line = (v: unknown) => JSON.stringify(v) + '\n';
  const timestamp = new Date().toISOString();
  const codexLog = (id: string, cwd?: string, parent?: string) =>
    line({ type: 'session_meta', timestamp, payload: { id, cwd, timestamp, parent_thread_id: parent } }) +
    line({ type: 'event_msg', timestamp, payload: { type: 'user_message', message: `public ${id}` } });
  await writeFile(join(day, 'rollout-ours.jsonl'), codexLog('ours-codex', projectRoot));
  await writeFile(join(day, 'rollout-foreign.jsonl'), codexLog('foreign-codex', temp));
  await writeFile(join(day, 'rollout-orphan.jsonl'), codexLog('orphan', projectRoot, 'unknown'));
  await writeFile(join(day, 'rollout-child.jsonl'), codexLog('ours-child', projectRoot, 'ours-codex'));
  const claudeLine = (id: string, cwd: string) => line({ type: 'user', sessionId: id, cwd, timestamp, message: { role: 'user', content: `public ${id}` } });
  await writeFile(join(claudeProject, 'ours-claude.jsonl'), claudeLine('ours-claude', projectRoot));
  await writeFile(join(claudeProject, 'foreign-claude.jsonl'), claudeLine('foreign-claude', temp));
  const probe = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: () => new Response('') });
  const port = probe.port!;
  probe.stop(true);
  const base = `http://127.0.0.1:${port}`;
  const proc = Bun.spawn([process.execPath, 'run', 'src/index.ts'], {
    cwd: resolve(import.meta.dir, '..'), stdout: 'ignore', stderr: 'pipe',
    env: { ...process.env, PORT: String(port), PATKONG_CODEX_ROOT: codex, PATKONG_CLAUDE_DIRS: claude },
  });
  const sockets: WebSocket[] = [];
  async function until(check: () => boolean | Promise<boolean>) {
    const end = Date.now() + 10000;
    while (Date.now() < end) {
      if (await check()) return;
      await Bun.sleep(30);
    }
    throw new Error('Timed out waiting for observer');
  }
  async function connect(): Promise<Array<Record<string, unknown>>> {
    const events: Array<Record<string, unknown>> = [];
    const ws = new WebSocket(`ws://127.0.0.1:${port}/ws`);
    sockets.push(ws);
    ws.onmessage = (event) => events.push(JSON.parse(String(event.data)));
    await until(() => events.length > 0);
    return events;
  }
  try {
    await until(async () => { try { return (await fetch(`${base}/api/health`)).ok; } catch { return false; } });
    await until(async () => ((await (await fetch(`${base}/api/agents`)).json()) as unknown[]).length === 3);
    const agents = await (await fetch(`${base}/api/agents`)).json() as Array<{ id: string }>;
    expect(agents.map((a) => a.id).sort()).toEqual(['ours-child', 'ours-claude', 'ours-codex']);
    const events = await connect();
    expect(JSON.stringify(events[0])).not.toContain('foreign');
    expect(JSON.stringify(events[0])).not.toContain('orphan');
    expect((await fetch(`${base}/api/agents`, { headers: { origin: 'https://foreign.example' } })).status).toBe(403);
    expect((await fetch(`${base}/api/hooks/install`, { method: 'POST' })).status).toBe(404);
    // Only the target session's appended prompt may appear in live activity.
    const prompt = (message: string) => line({ type: 'event_msg', timestamp: new Date().toISOString(), payload: { type: 'user_message', message } });
    await appendFile(join(day, 'rollout-foreign.jsonl'), prompt('foreign-live-marker'));
    await appendFile(join(day, 'rollout-ours.jsonl'), 'malformed JSON\n' + prompt('ours-live-marker'));
    await until(() => JSON.stringify(events).includes('ours-live-marker'));
    expect(JSON.stringify(events)).not.toContain('foreign-live-marker');
    sockets[0]!.close();
    const reconnected = await connect();
    expect(JSON.stringify(reconnected[0])).toContain('ours-live-marker');
    expect(JSON.stringify(reconnected[0])).not.toContain('foreign');
  } finally {
    for (const ws of sockets) ws.close();
    proc.kill('SIGTERM');
    await proc.exited;
    await rm(temp, { recursive: true, force: true });
  }
}, 20000);
