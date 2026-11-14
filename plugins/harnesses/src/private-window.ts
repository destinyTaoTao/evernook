import { spawn, type ChildProcess } from 'node:child_process';
import { randomUUID } from 'node:crypto';

type State = 'not_started' | 'requested' | 'saved' | 'canceled' | 'outcome_unknown' | 'launch_failed';
type Status = { state: State; session_id?: string; event_id?: string };

/** Start only the fixed local capture command; content never enters the host. */
export class PrivateWindow {
  private child?: ChildProcess;
  private closed = false;
  private result: Status = { state: 'not_started' };
  private readonly config: { python: string; config: string; access: string; entry: string };

  constructor(config: { python: string; config: string; access: string; entry: string }) { this.config = config; }

  status(): Status { return { ...this.result }; }

  abandon(): void {
    if (this.child || this.result.state !== 'outcome_unknown') throw new Error('evernook:private_window_unavailable');
    this.result = { state: 'not_started' };
  }

  async open(retry = false): Promise<void> {
    if (this.closed || this.child) throw new Error('evernook:private_window_unavailable');
    if (retry && this.result.state !== 'outcome_unknown') throw new Error('evernook:private_window_unavailable');
    if (!retry && this.result.state === 'outcome_unknown') throw new Error('evernook:private_window_unavailable');
    const session = retry ? this.result.session_id! : randomUUID();
    const event = retry ? this.result.event_id! : randomUUID();
    this.result = { state: 'requested', session_id: session, event_id: event };
    let child: ChildProcess;
    try {
      child = spawn(this.config.python, ['-B', '-m', 'evernook.cli', 'service-private-window',
        '--config', this.config.config, '--access', this.config.access, '--entry', this.config.entry,
        '--session-id', session, '--event-id', event],
      { windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'] });
    } catch {
      this.result = retry ? { state: 'outcome_unknown', session_id: session, event_id: event } : { state: 'launch_failed' };
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
      let state: State = 'outcome_unknown';
      try {
        if (code !== 0 || oversized) throw new Error();
        const output = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
        if (!output || Object.keys(output).sort().join(',') !== 'contract_version,ok,result'
            || output.contract_version !== 1 || output.ok !== true || !output.result
            || Object.keys(output.result).sort().join(',') !== 'event_id,session_id,state'
            || output.result.session_id !== session || output.result.event_id !== event
            || !['saved', 'canceled', 'outcome_unknown'].includes(output.result.state)) throw new Error();
        state = retry && output.result.state === 'canceled' ? 'outcome_unknown' : output.result.state;
      } catch { /* An unavailable or malformed receipt never proves a save or cancellation. */ }
      this.result = { state, session_id: session, event_id: event };
      chunks = [];
    });
    let spawned = false;
    await new Promise<void>((resolve, reject) => {
      child.once('spawn', () => { spawned = true; resolve(); });
      child.once('error', () => { if (this.child === child) { this.child = undefined;
        this.result = spawned || retry ? { state: 'outcome_unknown', session_id: session, event_id: event } : { state: 'launch_failed' }; }
        reject(new Error('evernook:private_window_unavailable')); });
    });
  }

  close(): void {
    this.closed = true;
    this.child?.kill();
  }
}
