import { realpathSync } from 'node:fs';
import { isAbsolute, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DatabaseSync } from 'node:sqlite';

export interface Message { id: string; text: string; timestamp: string }

export function externalDirectory(path: string): string {
  if (!isAbsolute(path)) throw new Error('Use an absolute runtime directory outside checkout');
  const canonical = realpathSync(path);
  const checkout = realpathSync(fileURLToPath(new URL('../../../', import.meta.url)));
  const difference = relative(checkout, canonical);
  if (!difference || (!isAbsolute(difference) && difference !== '..' && !difference.startsWith(`..${sep}`))) {
    throw new Error('Runtime directory must be outside checkout');
  }
  return canonical;
}

/** Disposable R0 comparison path: exact evidence plus a tiny derived lexical/vector index. */
export class MinimalStore {
  private db: DatabaseSync;
  constructor(root: string) {
    this.db = new DatabaseSync(join(externalDirectory(root), 'minimal.sqlite'));
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, text TEXT NOT NULL, timestamp TEXT NOT NULL, vector TEXT NOT NULL);
      CREATE VIRTUAL TABLE IF NOT EXISTS lexical USING fts5(id UNINDEXED, terms);`);
  }
  put(message: Message, vector: number[]) {
    const original = this.get(message.id);
    if (original) {
      if (original.text !== message.text || original.timestamp !== message.timestamp) throw new Error('Source conflict');
      return;
    }
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.prepare('INSERT INTO messages VALUES (?, ?, ?, ?)').run(message.id, message.text, message.timestamp, JSON.stringify(vector));
      this.db.prepare('INSERT INTO lexical VALUES (?, ?)').run(message.id, terms(message.text).join(' '));
      this.db.exec('COMMIT');
    } catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  get(id: string): Message | undefined {
    const row = this.db.prepare('SELECT id, text, timestamp FROM messages WHERE id = ?').get(id);
    return row ? { id: String(row.id), text: String(row.text), timestamp: String(row.timestamp) } : undefined;
  }
  all(): Message[] {
    return this.db.prepare('SELECT id, text, timestamp FROM messages ORDER BY id').all()
      .map(row => ({ id: String(row.id), text: String(row.text), timestamp: String(row.timestamp) }));
  }
  search(query: string, queryVector: number[], limit = 10): (Message & { score: number })[] {
    const lexical = this.lexical(query);
    const semantic = this.db.prepare('SELECT id, vector FROM messages').all().map(row => {
      const vector = JSON.parse(String(row.vector)) as number[];
      const dot = vector.reduce((total, value, i) => total + value * (queryVector[i] ?? 0), 0);
      const norm = Math.sqrt(vector.reduce((total, value) => total + value * value, 0) * queryVector.reduce((total, value) => total + value * value, 0));
      return { id: String(row.id), similarity: norm ? dot / norm : 0 };
    }).sort((a, b) => b.similarity - a.similarity).slice(0, 40);
    const scores = new Map<string, number>();
    for (const channel of [lexical, semantic]) channel.forEach((row, rank) => {
      const id = String(row.id);
      scores.set(id, (scores.get(id) ?? 0) + 1 / (60 + rank + 1));
    });
    return [...scores].sort((a, b) => b[1] - a[1]).slice(0, limit)
      .map(([id, score]) => ({ ...this.get(id)!, score }));
  }
  lexical(query: string): { id: string }[] {
    const match = terms(query).map(term => `"${term}"`).join(' OR ');
    return match ? this.db.prepare('SELECT id FROM lexical WHERE terms MATCH ? ORDER BY rank LIMIT 40').all(match).map(row => ({ id: String(row.id) })) : [];
  }
  rebuild(vectors: { id: string; vector: number[] }[]) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.exec('DELETE FROM lexical');
      for (const message of this.all()) this.db.prepare('INSERT INTO lexical VALUES (?, ?)').run(message.id, terms(message.text).join(' '));
      for (const { id, vector } of vectors) this.db.prepare('UPDATE messages SET vector = ? WHERE id = ?').run(JSON.stringify(vector), id);
      this.db.exec('COMMIT');
    } catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  close() { this.db.close(); }
}

function terms(text: string): string[] {
  const chars = Array.from(text.normalize('NFKC').toLowerCase()).filter(char => /[\p{L}\p{N}]/u.test(char));
  return [...new Set(chars.flatMap((_char, i) => [1, 2, 3].map(size => chars.slice(i, i + size).join(''))))];
}
