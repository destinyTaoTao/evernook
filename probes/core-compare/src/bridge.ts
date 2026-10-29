import { createInterface } from 'node:readline';
import { MinimalStore } from './minimal.ts';

// Deliberately narrow experiment: measure a real candidate/read path across Python -> Node stdio.
const store = new MinimalStore(process.argv[2]);
process.stdout.write(JSON.stringify({ ready: true }) + '\n');
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  const input = JSON.parse(line) as { id: number; query: string; vector: number[] };
  const start = performance.now();
  const hits = store.search(input.query, input.vector, 10);
  const pathMs = performance.now() - start;
  process.stdout.write(JSON.stringify({ id: input.id, pathMs, hits }) + '\n');
}
store.close();
