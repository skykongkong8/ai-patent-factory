import { mkdtemp, mkdir, rm, appendFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

// Public synthetic logs only. No real CLI session directories are watched in demo mode.
const root = resolve(import.meta.dir, '../..');
const temp = await mkdtemp(join(tmpdir(), 'patkong-demo-'));
const codex = join(temp, 'codex');
const claude = join(temp, 'claude');
const day = join(codex, 'sessions', 'demo');
const project = join(claude, 'projects', 'demo');
await mkdir(day, { recursive: true });
await mkdir(project, { recursive: true });
const line = (value: unknown) => JSON.stringify(value) + '\n';
const now = () => new Date().toISOString();
const codexFile = join(day, 'rollout-demo.jsonl');
const claudeFile = join(project, 'demo-claude.jsonl');
await writeFile(codexFile, line({ type: 'session_meta', timestamp: now(), payload: { id: 'demo-codex', cwd: root, timestamp: now() } })
  + line({ type: 'event_msg', timestamp: now(), payload: { type: 'user_message', message: '공개 데모: 자료를 살펴보고 발명 아이디어를 정리해주세요.' } }));
await writeFile(claudeFile, line({ type: 'user', sessionId: 'demo-claude', cwd: root, timestamp: now(), message: { role: 'user', content: '공개 데모: 팥콩 작업 내용을 검토해주세요.' } }));
const subdir = join(project, 'demo-claude', 'subagents');
await mkdir(subdir, { recursive: true });
const childFile = join(subdir, 'agent-demo.jsonl');
await writeFile(childFile, line({ type: 'user', sessionId: 'demo-claude', cwd: root, timestamp: now(), message: { role: 'user', content: '공개 예시의 자료 검토' } }));
await writeFile(childFile.replace('.jsonl', '.meta.json'), JSON.stringify({ agentType: 'Explore', description: '자료 확인' }));
let turn = 0;
const tools = ['Read', 'Edit', 'Bash', 'Grep', 'Skill', 'Agent'];
const timer = setInterval(async () => {
  const tool = tools[turn++ % tools.length]!;
  await appendFile(codexFile, line({ type: 'event_msg', timestamp: now(), payload: { type: 'exec_command_end', call_id: `demo-${turn}`, command: ['bash', '-c', turn % 2 ? 'rg public-example README.md' : 'python3 -m patent_factory --help'], cwd: root, exit_code: 0 } })
    + line({ type: 'event_msg', timestamp: now(), payload: { type: 'token_count', info: { total_token_usage: { input_tokens: 14000 + turn * 10, output_tokens: 1200 }, last_token_usage: { input_tokens: 14000, output_tokens: 100 }, model_context_window: 128000 } } }));
  const content = [{ type: 'tool_use', id: `tool-${turn}`, name: tool, input: { file_path: `${root}/README.md`, command: 'python3 -m patent_factory --help', skill: 'patent-research', description: '공개 데모 검토' } }];
  await appendFile(claudeFile, line({ type: 'assistant', sessionId: 'demo-claude', cwd: root, timestamp: now(), message: { role: 'assistant', content, usage: { input_tokens: 18000 + turn * 10, output_tokens: 500 }, model: 'claude-sonnet-4-6' } }));
  await appendFile(childFile, line({ type: 'assistant', sessionId: 'demo-claude', cwd: root, timestamp: now(), message: { role: 'assistant', content: [{ type: 'tool_use', id: `child-${turn}`, name: 'Read', input: { file_path: `${root}/README.md` } }] } }));
}, 4000);
const child = Bun.spawn([process.execPath, 'run', 'scripts/start.ts'], {
  cwd: resolve(import.meta.dir, '..'), stdout: 'inherit', stderr: 'inherit',
  env: { ...process.env, PATKONG_CODEX_ROOT: codex, PATKONG_CLAUDE_DIRS: claude },
});
const stop = () => { clearInterval(timer); child.kill('SIGTERM'); };
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
try { process.exitCode = await child.exited; }
finally { clearInterval(timer); await rm(temp, { recursive: true, force: true }); }
