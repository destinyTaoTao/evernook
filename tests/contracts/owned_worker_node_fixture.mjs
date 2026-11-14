import { spawn } from 'node:child_process';

const child = spawn(process.argv[2], ['-u', process.argv[3], 'worker'], {
  windowsHide: true,
  stdio: ['pipe', 'pipe', 'ignore'],
  env: { ...process.env, EVERNOOK_WORKER_PARENT_PID: String(process.pid), EVERNOOK_WORKER_LIFETIME_PIPE: '1' },
});
child.stdin.on('error', () => {});
child.stdout.pipe(process.stdout);
process.stdin.once('data', () => process.argv[4] === 'pipe_data' ? child.stdin.write('x') : child.stdin.end());
setInterval(() => {}, 1000);
