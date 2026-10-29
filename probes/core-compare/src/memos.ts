import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { bootstrapMemoryCore, initTestLogger, resolveConfig, type ResolvedHome } from '@memtensor/memos-local-plugin';
import { externalDirectory } from './minimal.ts';

/** Unmodified upstream facade; all knobs are explicit experimental overrides. */
export async function openMemos(root: string, model: string | undefined) {
  const homeRoot = externalDirectory(root);
  const home: ResolvedHome = {
    root: homeRoot, configFile: join(homeRoot, 'config.yaml'), dataDir: join(homeRoot, 'data'),
    dbFile: join(homeRoot, 'data', 'memos.db'), skillsDir: join(homeRoot, 'skills'),
    logsDir: join(homeRoot, 'logs'), daemonDir: join(homeRoot, 'daemon'),
  };
  for (const directory of [home.dataDir, home.skillsDir, home.logsDir, home.daemonDir]) mkdirSync(directory, { recursive: true });
  const config = resolveConfig({
    embedding: { provider: 'local', ...(model ? { model } : {}), cache: { enabled: false } },
    llm: { provider: 'local_only', fallbackToHost: false, maxRetries: 0 },
    telemetry: { enabled: false }, hub: { enabled: false },
    algorithm: {
      capture: { embedTraces: Boolean(model), synthReflections: false, alphaScoring: false },
      reward: { llmScoring: false }, l2Induction: { useLlm: false }, l3Abstraction: { useLlm: false },
      decisionRepair: { useLlm: false },
      retrieval: { llmFilterEnabled: false, includeLowValue: true, vectorScanMaxAgeMs: 0 },
    },
  });
  initTestLogger();
  const core = await bootstrapMemoryCore({ agent: 'evernook-r0', home, config, autoRecovery: false, telemetry: null, pkgVersion: '2.0.16-beta.1' });
  return { core, home, config };
}
