import { spawn, type ChildProcess } from 'node:child_process';

/** Start only the fixed local capture command; content never enters the host. */
export class PrivateWindow {
  private child?: ChildProcess;
  private closed = false;
  private readonly config: { python: string; config: string; access: string; entry: string };

  constructor(config: { python: string; config: string; access: string; entry: string }) { this.config = config; }

  async open(): Promise<void> {
    if (this.closed || this.child) throw new Error('evernook:private_window_unavailable');
    const child = spawn(this.config.python, ['-B', '-m', 'evernook.cli', 'service-private-window',
      '--config', this.config.config, '--access', this.config.access, '--entry', this.config.entry],
    { windowsHide: true, stdio: 'ignore' });
    this.child = child;
    child.once('close', () => { if (this.child === child) this.child = undefined; });
    await new Promise<void>((resolve, reject) => {
      child.once('spawn', () => resolve());
      child.once('error', () => { if (this.child === child) this.child = undefined; reject(new Error('evernook:private_window_unavailable')); });
    });
  }

  close(): void {
    this.closed = true;
    this.child?.kill();
  }
}
