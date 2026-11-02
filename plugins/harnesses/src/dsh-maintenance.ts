import type { Context } from '@deepseek-ai/cordis';
import type { Agent } from '@deepseek-ai/dsh-agent';
import { defineTool } from '@deepseek-ai/dsh-tools';
import type { installMemory } from './dsh.ts';
import type { MemoryConfig } from './client.ts';
import { createMaintenance, type MaintenanceConfig, type MaintenanceProposal } from './maintenance.ts';
export type { MaintenanceConfig, MaintenanceProposal, MaintenanceReview, MaintenanceOutcome } from './maintenance.ts';

/** Native dsh user capture is the trusted seam; model tools only create reviews. */
export async function installDshMaintenance(ctx: Context, config: MemoryConfig,
    memory: Pick<Awaited<ReturnType<typeof installMemory>>, 'capture' | 'status'>, maintenance: MaintenanceConfig) {
  const service = await createMaintenance(config, maintenance);
  const dispose = ctx.on('system-prompt/assemble', async (_assembly, context, next) => {
    const result = await next();
    if (context.agent) {
      await memory.capture(context.agent.session);
      const receipt = memory.status(context.agent.session.id).latest_input;
      if (receipt?.original_saved) await service.confirm(String(context.agent.session.id), receipt.message_id, context.signal);
    }
    return result;
  });
  let disposeTool = () => {};
  try {
    disposeTool = ctx.tools.register(defineTool({ name: 'evernook_memory_propose',
      description: 'Propose an exact memory maintenance review. This tool never confirms or applies it. The trusted host shows the review and requires a later native user confirmation.',
      parameters: { proposal: { type: 'string', required: true, description: 'JSON proposal: pin/suppress with message_id; correct_source with message_id/replacement_id; purge with source_ids; profile with tag_id/action and optional revise draft; fact_correction with subject_id/predicate/run_id/candidate_id.' } },
      output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
      async execute(args, exec) {
        if (!exec.agent || args.proposal.length > 131072) throw new Error('evernook:invalid_maintenance_proposal');
        let proposal: MaintenanceProposal;
        try { proposal = JSON.parse(args.proposal) as MaintenanceProposal; }
        catch { throw new Error('evernook:invalid_maintenance_proposal'); }
        const review = await service.propose(String(exec.agent.session.id), proposal, exec.signal);
        return JSON.stringify({ proposal_id: review.proposal_id, operation: review.operation.kind, state: 'pending_user_confirmation' });
      } }));
  } catch (error) { dispose(); await service.close(); throw error; }
  return { ...service,
    propose(agent: Agent, proposal: MaintenanceProposal, signal?: AbortSignal) {
      return service.propose(String(agent.session.id), proposal, signal);
    },
    async close() { disposeTool(); dispose(); await service.close(); },
  };
}
