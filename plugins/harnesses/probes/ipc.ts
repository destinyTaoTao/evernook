import { readFile, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { MemoryClient } from '../src/client.ts';
import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';

const [config, access, embeddingConfig, contextConfig, python, output] = process.argv.slice(2);
const client = new MemoryClient({ config, access, embeddingConfig, contextConfig, python, entry: 'synthetic-scale',
  cwd: resolve(import.meta.dirname, '../../..') });
const rows = [];
const echoRows = [];
for (const cache of ['fresh_process', 'warm']) {
  for (let repetition = 0; repetition < (cache === 'fresh_process' ? 10 : 1); repetition++) {
    const peer = new Client({ name: 'evernook-transport-diagnostic', version: '1.0.0' });
    const created = performance.now();
    try {
      await peer.connect(new StdioClientTransport({ command: python,
        args: [resolve(import.meta.dirname, '../../../probes/admission/ipc_echo.py')], stderr: 'ignore' }));
      const echoConnectMs = performance.now() - created;
      for (let sample = 0; sample < (cache === 'warm' ? 30 : 1); sample++) {
        const at = performance.now();
        const response = await peer.callTool({ name: 'synthetic_echo', arguments: { text: '合成IPC' } });
        if ((response.structuredContent as { text?: string })?.text !== '合成响应'.repeat(1024)) throw new Error('echo_mismatch');
        echoRows.push({ cache, connect_ms: echoConnectMs, roundtrip_ms: performance.now() - at });
      }
    } finally { await peer.close(); }
  }
}
const fixture = JSON.parse(await readFile(resolve(dirname(config), '../manifest.json'), 'utf8'));
const recentQuery = '合成条目' + String(fixture.expected_messages - 1).padStart(7, '0');
const boot = performance.now();
try {
  await client.connect();
  const connectMs = performance.now() - boot;
  const at = performance.now();
  const status = await client.call<{ context: { model_id: string } }>('status', {});
  const statusMs = performance.now() - at;
  for (const query of [recentQuery, '合成条目0000001', '外星深海音乐会许可证', '记录']) {
    const started = performance.now();
    const bundle = await client.context(query, status.context.model_id, 4096, '2026-11-04T00:00:00Z');
    rows.push({ query, roundtrip_ms: performance.now() - started, bundle });
    await writeFile(output, JSON.stringify({ connect_ms: connectMs, status_roundtrip_ms: statusMs, rows, transport_only_echo: echoRows,
      echo_text_utf8_bytes: Buffer.byteLength('合成响应'.repeat(1024), 'utf8'),
      scope: 'Echo baseline: same MCP transport, synthetic 12288-byte UTF-8 text duplicated in content and structured fields, no Core/auth. Actual memory roundtrips include IPC, auth/audit and Core work; no subtraction of independent samples.' }) + '\n');
  }
} finally { await client.close(); }
