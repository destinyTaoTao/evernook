// Diagnostic preload for the owned embedding worker; never consumes its streams.
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';

const report = process.env.EVERNOOK_EMBEDDING_READ_TRACE;
const record = value => fs.appendFileSync(report, JSON.stringify(value) + '\n');
record({ event: 'worker_started' });
const original = fs.createReadStream;
fs.createReadStream = function (path, ...options) {
  const stream = original.call(this, path, ...options);
  if (String(path).endsWith('.onnx')) {
    stream.once('close', () => record({ event: 'graph_stream_closed', bytes_read: stream.bytesRead }));
  }
  return stream;
};
syncBuiltinESMExports();
