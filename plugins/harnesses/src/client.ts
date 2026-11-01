import { isAbsolute } from 'node:path';
import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';

export interface MemoryConfig {
  python: string;
  config: string;
  access: string;
  entry: string;
  embeddingConfig?: string;
  cwd?: string;
}

export interface Source { entry: string; session: string; branch: string; event: string }
export interface Event {
  source: Source;
  role: 'user' | 'assistant' | 'tool' | 'system' | 'developer';
  sequence: number;
  occurred_at: string | null;
  completion: 'partial' | 'complete' | 'canceled' | 'failed';
  text: string;
  sensitive?: boolean;
  protected_source?: boolean;
}
export interface Receipt { message_id: string; original_saved: boolean; representation: string; status: string }
export interface CaptureReceipt { message: Receipt; complete: boolean; gaps: string[] }
export interface Attachment {
  attachment_id: string; label: string; media_type: string;
  chunks_base64: string[] | null; expected_size: number; expected_sha256?: string;
  complete: boolean;
}
export interface RecallResult {
  hits: { evidence: { message_id: string; source: Source; role: Event['role']; content_version: number; state_version: number; spans: { start: number; end: number; text: string }[] } }[];
  degraded: string[];
  evidence_state: string;
}

/** After a protected native record, keep only the current safe turn and fixed system context.
 * The host's raw history remains untouched; an unprovable boundary blocks dispatch. */
export function safeHistory<T extends { role: string; content?: unknown }>(messages: T[], safeInput: string,
    protectedTexts: ReadonlySet<string>): { messages: T[]; omitted: number } {
  if (!protectedTexts.size) return { messages, omitted: 0 };
  const text = (content: unknown): string => typeof content === 'string' ? content
    : Array.isArray(content) ? content.filter(block => block?.type === 'text').map(block => block.text).join('') : '';
  const boundary = messages.findLastIndex(message => message.role === 'user' && text(message.content) === safeInput);
  if (boundary < 0) throw new Error('evernook:safe_input_boundary_unavailable');
  const kept = messages.filter((message, index) => index >= boundary || message.role === 'system');
  assertProtectedHistoryExcluded(kept, protectedTexts);
  return { messages: kept, omitted: messages.length - kept.length };
}

export function assertProtectedHistoryExcluded(value: unknown, protectedTexts: ReadonlySet<string>): void {
  if (!protectedTexts.size) return;
  // Sections, tool declarations and provider replay metadata are visible too. Check serialized
  // fields and joined scalar values so split blocks cannot hide a known protected original.
  const joined = (value: unknown): string => typeof value === 'string' ? value
    : value && typeof value === 'object' ? Object.values(value).map(joined).join('') : '';
  const content = joined(value);
  const serialized = JSON.stringify(value) ?? '';
  if ([...protectedTexts].some(value => value && (content.includes(value)
      || serialized.includes(JSON.stringify(value).slice(1, -1))))) throw new Error('evernook:protected_history_not_excluded');
}

/** Trusted native transport. Client identity is loaded by Python from an ACL-checked file. */
export class MemoryClient {
  private client: Client;
  private transport: StdioClientTransport;
  constructor(config: MemoryConfig) {
    if (![config.python, config.config, config.access, ...(config.embeddingConfig ? [config.embeddingConfig] : [])].every(isAbsolute)
        || !config.entry || config.entry.length > 256) throw new Error('evernook:invalid_host_config');
    const args = ['-m', 'evernook.mcp_server', '--config', config.config, '--access', config.access, '--profile', 'trusted-host'];
    if (config.embeddingConfig) args.push('--embedding-config', config.embeddingConfig);
    this.client = new Client({ name: 'evernook-trusted-native', version: '1.0.0' });
    this.transport = new StdioClientTransport({ command: config.python, args, cwd: config.cwd,
      stderr: 'ignore', maxBufferSize: 8 * 1024 * 1024 });
  }
  async connect() {
    try { await this.client.connect(this.transport); }
    catch { await this.close(); throw new Error('evernook:connection_failed'); }
  }
  async call<T>(name: string, args: Record<string, unknown>): Promise<T> {
    try {
      const result = await this.client.callTool({ name: 'evernook_v1_' + name, arguments: args });
      const payload = result.structuredContent as { ok?: boolean; result?: T; error?: { code?: string } } | undefined;
      if (!payload?.ok || result.isError) {
        const code = payload?.error?.code;
        throw new Error('evernook:' + (typeof code === 'string' && /^[a-z_]{1,80}$/.test(code) ? code : 'operation_failed'));
      }
      return payload.result as T;
    } catch (error) {
      if (error instanceof Error && /^evernook:[a-z_]+$/.test(error.message)) throw error;
      throw new Error('evernook:transport_failed');
    }
  }
  remember(event: Event) { return this.call<Receipt>('remember', { event }); }
  stream(event: Event, frames: object[]) { return this.call<CaptureReceipt>('capture_stream', { event, frames }); }
  capture(event: Event, kind: 'message' | 'tool_call' | 'tool_result', attachments: Attachment[] = [], input_id?: string, tool_name?: string) {
    return this.call<CaptureReceipt>('capture', { event, kind, attachments, input_id, tool_name });
  }
  recall(query: string, limit = 8) { return this.call<RecallResult>('recall', { query, limit }); }
  read(message_id: string) { return this.call<{ event: Event; receipt: Receipt & { content_version: number } }>('read', { message_id }); }
  async close() { try { await this.client.close(); } catch { await this.transport.close().catch(() => {}); } }
}
