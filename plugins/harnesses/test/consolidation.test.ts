import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdir, mkdtemp } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import test from 'node:test';
import { createConsolidation } from '../src/consolidation.ts';
import type { MemoryConfig } from '../src/client.ts';

const python = process.env.EVERNOOK_TEST_PYTHON;
const output = process.env.EVERNOOK_TEST_OUTPUT;
if (!python || !output) throw new Error('Set external synthetic EVERNOOK_TEST_PYTHON and EVERNOOK_TEST_OUTPUT');
const checkout = resolve(import.meta.dirname, '../../..');

test('saved gates, foreground priority and startup catch-up do not rely on an exit callback', { timeout: 180000 }, async t => {
  await mkdir(output, { recursive: true });
  const root = await mkdtemp(join(output, 'consolidation-'));
  const script = "import json,sys; from pathlib import Path; from evernook import initialize,pair_client,MemoryRuntime,Event,Source; from evernook.scheduler import ConsolidationScheduler; c,a=initialize(Path(sys.argv[1]),client_id='owner',entry='owner'); owner=c.parent/'mcp-access-owner.json'; pair_client(c,client_id='owner',entry='owner',access_path=owner); ordinary=c.parent/'mcp-access-host.json'; pair_client(c,client_id='host',entry='host',access_path=ordinary,scopes=('capture','recall')); worker=c.parent/'mcp-access-worker.json'; pair_client(c,client_id='worker',entry='worker',access_path=worker,scopes=('recall','maintain','accept')); r=MemoryRuntime(c); r.submit(a,Event(Source('owner','missed-exit','main','1'),'user',1,None,'complete','合成：重启补跑主题')); ConsolidationScheduler(r).configure(a,paused=True,min_pending=1,idle_seconds=0); print(json.dumps({'config':str(c),'access':str(ordinary),'owner':str(owner),'worker':str(worker)}))";
  const paths = JSON.parse(execFileSync(python, ['-c', script, join(root, 'memory')], { cwd: checkout, encoding: 'utf8' }));
  const config: MemoryConfig = { python, config: paths.config, access: paths.access, entry: 'host', cwd: checkout };
  const background = await createConsolidation(config, { access: paths.worker, pollMs: 100 });
  t.after(() => background.close());
  assert.equal((await background.status()).paused, true);
  assert.equal((await background.status()).processed, 0);
  await background.close();
  execFileSync(python, ['-m', 'evernook.cli', 'scheduler-configure', '--config', paths.config, '--access', paths.owner],
    { cwd: checkout, input: JSON.stringify({ paused: false }), stdio: ['pipe', 'pipe', 'pipe'] });
  const restarted = await createConsolidation(config, { access: paths.worker, pollMs: 100 });
  t.after(() => restarted.close());
  // Hard cancellation can leave the fenced 10s budget + 30s recovery lease intact.
  const deadline = Date.now() + 60000;
  let status = await restarted.status();
  while (status.pending !== 0 && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 100));
    status = await restarted.status();
  }
  assert.equal(status.pending, 0, JSON.stringify(restarted.snapshot()));
  assert.equal(status.covered_messages, 1);
  assert.equal(status.processed, 1);
  await restarted.turnStarted('native-turn');
  execFileSync(python, ['-c', "import sys; from pathlib import Path; from evernook import MemoryRuntime,Event,Source; r=MemoryRuntime(Path(sys.argv[1])); a=r.access_from_file(Path(sys.argv[2])); r.submit(a,Event(Source('owner','during-foreground','main','2'),'user',2,None,'complete','合成：前台期间待处理'))", paths.config, paths.owner], { cwd: checkout });
  await new Promise(resolve => setTimeout(resolve, 300));
  assert.equal((await restarted.status()).pending, 1);
  await restarted.turnEnded('native-turn');
  const settledBy = Date.now() + 60000;
  status = await restarted.status();
  while (status.pending !== 0 && Date.now() < settledBy) {
    await new Promise(resolve => setTimeout(resolve, 100));
    status = await restarted.status();
  }
  assert.equal(status.pending, 0, JSON.stringify(restarted.snapshot()));
  assert.equal(status.processed, 2);
  await restarted.turnStarted('transient-control-error');
  execFileSync(python, ['-c', "import sys; from pathlib import Path; from evernook import MemoryRuntime,Event,Source; r=MemoryRuntime(Path(sys.argv[1])); a=r.access_from_file(Path(sys.argv[2])); r.submit(a,Event(Source('owner','transient-control','main','3'),'user',3,None,'complete','合成：控制短暂失败后自动补跑'))", paths.config, paths.owner], { cwd: checkout });
  const fault = "import sqlite3,sys; from pathlib import Path; c=sqlite3.connect(Path(sys.argv[1]).parent/'memory.sqlite3'); c.execute(sys.argv[2]); c.commit(); c.close()";
  execFileSync(python, ['-c', fault, paths.config, "CREATE TRIGGER synthetic_activity_failure BEFORE UPDATE ON scheduler_foreground_v1 BEGIN SELECT RAISE(ABORT,'synthetic'); END"], { cwd: checkout });
  await restarted.turnEnded('transient-control-error');
  assert.equal(restarted.snapshot().activity_healthy, false);
  execFileSync(python, ['-c', fault, paths.config, 'DROP TRIGGER synthetic_activity_failure'], { cwd: checkout });
  const recoveredBy = Date.now() + 60000;
  status = await restarted.status();
  while (status.pending !== 0 && Date.now() < recoveredBy) {
    await new Promise(resolve => setTimeout(resolve, 100));
    status = await restarted.status();
  }
  assert.equal(status.pending, 0, 'idle must retry failed activity synchronization and resume without another turn');
  assert.equal(status.processed, 3);
  assert.equal(restarted.snapshot().activity_healthy, true);
});
