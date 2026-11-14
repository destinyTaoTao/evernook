import { spawn, type ChildProcess } from 'node:child_process';

type State = 'not_started' | 'requested' | 'saved' | 'canceled' | 'outcome_unknown' | 'launch_failed';
type Status = { state: State; saved_count?: number; session_id?: string; pending_event_id?: string; sequence?: number };
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** Only non-secret receipts cross the host boundary; the native window owns all input. */
export class PrivateSessionWindow {
  private child?: ChildProcess;
  private closed = false;
  private result: Status = { state: 'not_started' };
  private readonly config: { python: string; config: string; access: string; entry: string };
  constructor(config: { python: string; config: string; access: string; entry: string }) { this.config = config; }

  status(): Status { return { ...this.result }; }
  blocksOtherWindow(): boolean { return Boolean(this.child) || this.result.state === 'outcome_unknown'; }
  abandon(): void {
    if (this.child || this.result.state !== 'outcome_unknown') throw new Error('evernook:private_window_unavailable');
    this.result = { state: 'not_started' };
  }

  async open(retry = false): Promise<void> {
    if (this.closed || this.child || (!retry && this.result.state === 'outcome_unknown')) throw new Error('evernook:private_window_unavailable');
    const previous = this.result;
    if (retry && (previous.state !== 'outcome_unknown' || !previous.session_id || !previous.pending_event_id
        || !Number.isSafeInteger(previous.sequence))) throw new Error('evernook:private_window_unavailable');
    const args = ['-B', '-m', 'evernook.cli', 'service-private-session', '--config', this.config.config,
      '--access', this.config.access, '--entry', this.config.entry];
    if (retry) args.push('--session-id', previous.session_id!, '--event-id', previous.pending_event_id!, '--sequence', String(previous.sequence));
    this.result = { state: 'requested' };
    let child: ChildProcess;
    try {
      child = spawn(this.config.python, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'] });
    } catch {
      this.result = retry ? previous : { state: 'launch_failed' };
      throw new Error('evernook:private_window_unavailable');
    }
    this.child = child;
    let chunks: Buffer[] = []; let bytes = 0; let oversized = false;
    child.stdout!.on('data', (chunk: Buffer) => {
      bytes += chunk.length;
      if (bytes > 4096) { oversized = true; chunks = []; child.kill(); }
      else if (!oversized) chunks.push(chunk);
    });
    child.stdout!.on('error', () => { oversized = true; chunks = []; child.kill(); });
    child.once('close', code => {
      if (this.child !== child) return;
      this.child = undefined;
      let result: Status = { state: 'outcome_unknown' };
      try {
        if (code !== 0 || oversized) throw new Error();
        const output = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
        if (!output || Object.keys(output).sort().join(',') !== 'contract_version,ok,result'
            || output.contract_version !== 1 || output.ok !== true || !output.result) throw new Error();
        const value = output.result;
        const unknown = value.state === 'outcome_unknown';
        const fields = unknown ? 'pending_event_id,saved_count,sequence,session_id,state' : 'saved_count,session_id,state';
        const base = retry ? previous.sequence! : 0;
        if (Object.keys(value).sort().join(',') !== fields || !['saved', 'canceled', 'outcome_unknown'].includes(value.state)
            || !uuid.test(value.session_id) || !Number.isSafeInteger(value.saved_count) || value.saved_count < 0 || value.saved_count > 100
            || value.saved_count > 100 - base
            || (value.state === 'saved' && value.saved_count < 1) || (value.state === 'canceled' && value.saved_count !== 0)
            || (unknown && (!uuid.test(value.pending_event_id) || !Number.isSafeInteger(value.sequence) || value.sequence < 0 || value.sequence > 99))
            || (unknown && value.sequence !== base + value.saved_count)
            || (retry && unknown && value.saved_count === 0 && value.pending_event_id !== previous.pending_event_id)
            || (retry && value.session_id !== previous.session_id)) throw new Error();
        result = retry && value.state === 'canceled' ? previous : {
          state: value.state, saved_count: value.saved_count, session_id: value.session_id,
          ...(unknown ? { pending_event_id: value.pending_event_id, sequence: value.sequence } : {}),
        };
      } catch { /* A malformed or missing receipt cannot prove which writes succeeded. */ }
      this.result = result;
      chunks = [];
    });
    let spawned = false;
    await new Promise<void>((resolve, reject) => {
      child.once('spawn', () => { spawned = true; resolve(); });
      child.once('error', () => {
        if (this.child === child) {
          this.child = undefined;
          this.result = spawned ? { state: 'outcome_unknown' } : retry ? previous : { state: 'launch_failed' };
        }
        reject(new Error('evernook:private_window_unavailable'));
      });
    });
  }

  close(): void { this.closed = true; this.child?.kill(); }
}
