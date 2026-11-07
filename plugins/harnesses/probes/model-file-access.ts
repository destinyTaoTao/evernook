import { isAbsolute, join } from 'node:path';

export type ModelFileAccess = 'library-default' | 'verified-local-path-v1';
export interface ModelFileEnvironment {
  useCustomCache: boolean;
  customCache: { match(request: string): Promise<unknown>; put(...args: unknown[]): Promise<unknown> } | null;
  remoteHost: string;
  remotePathTemplate: string;
}

// Transformers.js exposes cache configuration on a shared environment. Serialize our
// pipeline constructions, including default loads, so a temporary resolver cannot leak.
let loading: Promise<void> = Promise.resolve();

/** Caller must verify the pinned model artifacts before entering this load scope. */
export async function withModelFileAccess<T>(env: ModelFileEnvironment, modelDir: string,
    access: ModelFileAccess, load: () => Promise<T>): Promise<{ value: T; pathHits: number }> {
  const preceding = loading; const finished = Promise.withResolvers<void>(); loading = finished.promise;
  await preceding;
  const oldEnabled = env.useCustomCache; const oldCache = env.customCache;
  let pathHits = 0;
  try {
    if (access === 'verified-local-path-v1') {
      if (!isAbsolute(modelDir)) throw new Error('absolute_model_path_required');
      const file = 'onnx/model_q4.onnx';
      const local = modelDir.replace(/\/$/, '') + '/' + file;
      const remote = env.remoteHost.replace(/\/$/, '') + '/'
        + env.remotePathTemplate.replaceAll('{model}', modelDir).replaceAll('{revision}', 'main').replace(/^\//, '').replace(/\/$/, '') + '/' + file;
      const aliases = new Set([local, remote]);
      env.useCustomCache = true;
      env.customCache = {
        async match(request) {
          if (!aliases.has(request)) return undefined;
          pathHits++;
          return join(modelDir, file);
        },
        async put() { throw new Error('model_path_cache_read_only'); },
      };
    } else if (access !== 'library-default') throw new Error('model_file_access_mismatch');
    return { value: await load(), pathHits };
  } finally {
    env.useCustomCache = oldEnabled; env.customCache = oldCache; finished.resolve();
  }
}
