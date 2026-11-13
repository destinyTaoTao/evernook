// Actual pinned transport + agent loop + Core against a loopback synthetic Messages endpoint.
// Never uses a real account, model, credential or user workspace.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFileSync } from 'node:child_process';
import { resolve, isAbsolute, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { loop, follow } from '../host/test/loop.mjs';
import { fixture, load } from '../host/test/runtime.mjs';

const entry = process.env.EVERNOOK_DSH_ADAPTER_ENTRY;
const python = process.env.EVERNOOK_TEST_PYTHON;
assert.ok(entry && isAbsolute(entry) && python && isAbsolute(python));
const { installMemory } = await import(pathToFileURL(entry));
const { DeepSeekAdapter, resolveAdapterOptions } = await load('@deepseek-ai/dsh-llm-deepseek');
const signature = 'synthetic-native-thinking-signature';
const reasoning = '合成传输推理🙂：核对临时工具。';
const requests = []; const failures = [];
let effects = 0; let host; let handle; let memory;
const server = createServer(async (request, response) => {
  try {
    assert.equal(request.url, '/v1/messages'); assert.equal(request.method, 'POST');
    const chunks = []; let bytes = 0;
    for await (const chunk of request) { bytes += chunk.length; assert.ok(bytes <= 262144); chunks.push(chunk); }
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    assert.equal(body.model, 'local-test'); assert.equal(body.stream, true);
    requests.push(body); assert.ok(requests.length <= 2);
    if (requests.length === 2) {
      const assistant = body.messages.find(message => message.role === 'assistant');
      const thinking = assistant?.content.find(block => block.type === 'thinking');
      assert.equal(thinking?.thinking, reasoning); assert.equal(thinking?.signature, signature);
      assert.ok(body.messages.some(message => message.content.some(block => block.type === 'tool_result' && block.tool_use_id === 'synthetic-wire-call')));
      assert.equal(effects, 1);
    }
    response.writeHead(200, { 'Content-Type': 'text/event-stream' });
    const send = value => response.write(`event: ${value.type}\ndata: ${JSON.stringify(value)}\n\n`);
    send({ type: 'message_start', message: { id: 'synthetic-response-' + requests.length, usage: { input_tokens: 1, output_tokens: 0 } } });
    if (requests.length === 1) {
      send({ type: 'content_block_start', index: 0, content_block: { type: 'thinking', thinking: '' } });
      send({ type: 'content_block_delta', index: 0, delta: { type: 'thinking_delta', thinking: reasoning } });
      send({ type: 'content_block_delta', index: 0, delta: { type: 'signature_delta', signature } });
      send({ type: 'content_block_stop', index: 0 });
      send({ type: 'content_block_start', index: 1, content_block: { type: 'tool_use', id: 'synthetic-wire-call', name: 'synthetic-tool', input: {} } });
      send({ type: 'content_block_delta', index: 1, delta: { type: 'input_json_delta', partial_json: '{}' } });
      send({ type: 'content_block_stop', index: 1 });
    } else {
      send({ type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } });
      send({ type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: '合成协议链路完成。' } });
      send({ type: 'content_block_stop', index: 0 });
    }
    send({ type: 'message_delta', delta: { stop_reason: requests.length === 1 ? 'tool_use' : 'end_turn' }, usage: { output_tokens: 1 } });
    send({ type: 'message_stop' }); response.end();
  } catch (error) { failures.push(String(error)); response.writeHead(500); response.end('synthetic protocol assertion failed'); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
try {
  const address = server.address(); assert.ok(address && typeof address === 'object');
  const options = resolveAdapterOptions({ baseURL: `http://127.0.0.1:${address.port}`, thinking: 'enabled',
    reasoningEffort: 'high', maxTokens: 512, streamIdleTimeoutMs: 5000, models: [{ id: 'local-test' }] }, {});
  const adapter = new DeepSeekAdapter({ options: () => options, resolveAuth: async () => ({ headers: {} }),
    resolveUserId: () => 'synthetic-anonymous-id', prepareExtensions: async () => ({ fields: {}, accept: async () => {} }) });
  host = await loop(adapter);
  const paths = JSON.parse(execFileSync(python, ['-c',
    "import json,sys; from pathlib import Path; from evernook import initialize,pair_client; c,a=initialize(Path(sys.argv[1]),client_id='synthetic-owner',entry='owner'); p=c.parent/'mcp-access-dsh.json'; pair_client(c,client_id='synthetic-dsh',entry='dsh',access_path=p,scopes=('capture','recall')); print(json.dumps({'config':str(c),'access':str(p)}))",
    join(host.root, 'memory')], { cwd: resolve(import.meta.dirname, '../../..'), encoding: 'utf8' }));
  memory = await installMemory(host.ctx, { python, ...paths, entry: 'dsh', streamMode: 'durable_tools', toolPublication: true });
  handle = await host.create();
  host.ctx.tools.register(fixture('synthetic-tool', async () => {
    const saved = JSON.parse(execFileSync(python, ['-c',
      "import json,sys; from pathlib import Path; from evernook import MemoryRuntime; r=MemoryRuntime(Path(sys.argv[1])); a=r.access_from_file(Path(sys.argv[2])); rows=r.list_messages(a,limit=100); ids=[v.receipt.message_id for v in rows.messages if v.event.source.session==sys.argv[3] and v.event.source.event.endswith(':frames')]; assert len(ids)==1; c=r.read_capture(a,ids[0]); frames=json.loads(r.read_attachment(a,ids[0],'raw-tool-stream-v1')); print(json.dumps({'complete':c.complete,'thinking':any(f.get('type')=='reasoning-delta' for f in frames),'signature':sys.argv[4] in json.dumps(frames)}))",
      paths.config, paths.access, String(handle.agent.session.id), signature], { encoding: 'utf8' }));
    assert.deepEqual(saved, { complete: true, thinking: true, signature: true });
    effects++; return [{ type: 'text', text: '合成工具回执。' }];
  }));
  follow(handle);
  await Promise.race([handle.agent.whenIdle(), new Promise((_, reject) => { const timer = setTimeout(() => reject(new Error('synthetic deadline exceeded')), 20000); timer.unref(); })]);
  assert.deepEqual(failures, []); assert.deepEqual(host.errors, []);
  assert.equal(requests.length, 2); assert.equal(effects, 1);
  const attempts = await memory.streams(handle.agent.session.id);
  const pages = await Promise.all(attempts.streams.map(source => memory.readStream(source)));
  assert.ok(pages.some(page => page.text === '合成协议链路完成。' && page.complete));
  console.log(JSON.stringify({ synthetic: true, actual_deepseek_transport: true, requests: 2, tool_effects: 1,
    complete_before_tool: true, native_thinking_replay: true, passed: true }));
} finally {
  await handle?.dispose(); await memory?.close(); await host?.close();
  server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
}
