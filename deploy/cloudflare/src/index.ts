import { Container } from '@cloudflare/containers';

import {
  buildContainerEnvVars,
  DOCUMENSO_PORT,
  instanceName,
  parseInstanceCount,
  pickInstanceIndex,
  WARM_INSTANCE_INDEX,
  withForwardedHeaders,
} from './config';
import { resolveContainerEnvVars } from './container-env';

// Documenso runs `prisma migrate deploy` before listening, so a cold start
// takes far longer than the library's 20s default.
const STARTUP_TIMEOUT_MS = 180_000;

export class DocumensoContainer extends Container<Env> {
  override defaultPort = DOCUMENSO_PORT;

  // Longer than the cron interval so the warm instance never idles out.
  override sleepAfter = '15m';

  override pingEndpoint = 'localhost/api/health';

  constructor(ctx: DurableObjectState<object>, env: Env) {
    super(ctx, env);
    this.envVars = buildContainerEnvVars(env);
  }

  private pendingStart: Promise<void> | null = null;

  override async fetch(request: Request): Promise<Response> {
    if (!this.ctx.container?.running) {
      await this.startOnce();
    }

    return await super.fetch(request);
  }

  // Concurrent cold-start requests share one start instead of racing.
  private startOnce(): Promise<void> {
    this.pendingStart ??= this.startWithResolvedEnv().finally(() => {
      this.pendingStart = null;
    });

    return this.pendingStart;
  }

  private async startWithResolvedEnv(): Promise<void> {
    await this.startAndWaitForPorts({
      ports: DOCUMENSO_PORT,
      startOptions: { envVars: await resolveContainerEnvVars(this.env) },
      cancellationOptions: { portReadyTimeoutMS: STARTUP_TIMEOUT_MS },
    });
  }
}

const getInstance = (env: Env, index: number): DurableObjectStub<DocumensoContainer> =>
  env.DOCUMENSO.get(env.DOCUMENSO.idFromName(instanceName(index)));

export default {
  async fetch(request, env): Promise<Response> {
    const instanceCount = parseInstanceCount(env.CONTAINER_INSTANCE_COUNT);
    const instance = getInstance(env, pickInstanceIndex(instanceCount, Math.random()));

    return await instance.fetch(withForwardedHeaders(request));
  },

  async scheduled(_controller, env): Promise<void> {
    const response = await getInstance(env, WARM_INSTANCE_INDEX).fetch(
      new Request(`http://localhost:${DOCUMENSO_PORT}/api/health`),
    );

    if (!response.ok) {
      throw new Error(`Documenso health check failed with status ${response.status}.`);
    }
  },
} satisfies ExportedHandler<Env>;
