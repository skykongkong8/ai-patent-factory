import { resolve } from 'node:path';

const root = resolve(import.meta.dir, '..');
const port = process.env.PORT || '4444';
const clientPort = process.env.CLIENT_PORT || '4445';
const env = { ...process.env, PORT: port, CLIENT_PORT: clientPort, VITE_SERVER_PORT: port };
const children = [
  Bun.spawn([process.execPath, 'run', 'src/index.ts'], { cwd: `${root}/server`, env, stdout: 'inherit', stderr: 'inherit' }),
  Bun.spawn([process.execPath, 'run', 'dev'], { cwd: `${root}/client`, env, stdout: 'inherit', stderr: 'inherit' }),
];
function stop() { for (const child of children) child.kill('SIGTERM'); }
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
console.log(`팥콩 팩토리: http://127.0.0.1:${clientPort} — Ctrl+C로 종료`);
const first = await Promise.race(children.map((child) => child.exited));
stop();
await Promise.all(children.map((child) => child.exited));
process.exit(first);
