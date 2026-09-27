export const DOCUMENSO_PORT = 3000;

export const SIGNING_CERT_ENV_KEY = 'NEXT_PRIVATE_SIGNING_LOCAL_FILE_CONTENTS';

export const WARM_INSTANCE_INDEX = 0;

const DEFAULT_INSTANCE_COUNT = 1;

const MAX_INSTANCE_COUNT = 50;

/**
 * Vars that only configure this Worker and must not leak into the Documenso process.
 */
const WORKER_ONLY_KEYS: ReadonlySet<string> = new Set(['CONTAINER_INSTANCE_COUNT', 'SIGNING_CERT_R2_KEY']);

/**
 * Build the process environment for the Documenso container from the Worker's
 * vars and secrets. Bindings (objects) and Worker-only vars are dropped.
 */
export const buildContainerEnvVars = (env: object): Record<string, string> => {
  const envVars: Record<string, string> = {};

  for (const [key, value] of Object.entries(env)) {
    if (typeof value !== 'string') {
      continue;
    }

    if (WORKER_ONLY_KEYS.has(key)) {
      continue;
    }

    envVars[key] = value;
  }

  envVars.PORT = String(DOCUMENSO_PORT);

  return envVars;
};

export const parseInstanceCount = (raw: string | undefined): number => {
  if (raw === undefined || raw.trim() === '') {
    return DEFAULT_INSTANCE_COUNT;
  }

  const parsed = Number(raw);

  if (!Number.isInteger(parsed) || parsed < 1 || parsed > MAX_INSTANCE_COUNT) {
    throw new RangeError(
      `CONTAINER_INSTANCE_COUNT must be an integer between 1 and ${MAX_INSTANCE_COUNT}, received "${raw}".`,
    );
  }

  return parsed;
};

export const instanceName = (index: number): string => `instance-${index}`;

export const pickInstanceIndex = (instanceCount: number, random: number): number => {
  if (random < 0 || random >= 1) {
    throw new RangeError(`random must be in [0, 1), received ${random}.`);
  }

  return Math.floor(random * instanceCount);
};

/**
 * Rewrite the incoming request so the Node server sees the real client IP,
 * protocol and host (Documenso records the IP in document audit logs).
 */
export const withForwardedHeaders = (request: Request): Request => {
  const url = new URL(request.url);
  const headers = new Headers(request.headers);

  const clientIp = request.headers.get('cf-connecting-ip');

  if (clientIp) {
    headers.set('x-forwarded-for', clientIp);
    headers.set('x-real-ip', clientIp);
  } else {
    headers.delete('x-forwarded-for');
    headers.delete('x-real-ip');
  }

  headers.set('x-forwarded-proto', url.protocol.replace(':', ''));
  headers.set('x-forwarded-host', url.host);

  return new Request(request, { headers });
};
