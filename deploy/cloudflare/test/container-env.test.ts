import { env } from 'cloudflare:workers';
import { describe, expect, it } from 'vitest';
import { resolveContainerEnvVars } from '../src/container-env';
import { encodeBase64 } from '../src/encoding';

const CERT_BYTES = new Uint8Array(8_192).map((_, index) => (index * 7) % 256);

describe('resolveContainerEnvVars', () => {
  it('loads a certificate larger than the secret limit from R2 as base64', async () => {
    await env.SIGNING_CERT_BUCKET.put(env.SIGNING_CERT_R2_KEY, CERT_BYTES);

    const envVars = await resolveContainerEnvVars(env);

    expect(envVars.NEXT_PRIVATE_SIGNING_LOCAL_FILE_CONTENTS).toBe(encodeBase64(CERT_BYTES));
    expect(envVars.NEXT_PRIVATE_INTERNAL_WEBAPP_URL).toBe('http://localhost:3000');
    expect(envVars.PORT).toBe('3000');
    expect(envVars.SIGNING_CERT_R2_KEY).toBeUndefined();
  });

  it('prefers the certificate secret over R2', async () => {
    await env.SIGNING_CERT_BUCKET.put(env.SIGNING_CERT_R2_KEY, CERT_BYTES);
    const envWithSecret: Env = Object.assign({}, env, {
      NEXT_PRIVATE_SIGNING_LOCAL_FILE_CONTENTS: 'c2VjcmV0',
    });

    const envVars = await resolveContainerEnvVars(envWithSecret);

    expect(envVars.NEXT_PRIVATE_SIGNING_LOCAL_FILE_CONTENTS).toBe('c2VjcmV0');
  });

  it('starts without a certificate when none is stored', async () => {
    await env.SIGNING_CERT_BUCKET.delete(env.SIGNING_CERT_R2_KEY);

    const envVars = await resolveContainerEnvVars(env);

    expect(envVars.NEXT_PRIVATE_SIGNING_LOCAL_FILE_CONTENTS).toBeUndefined();
  });
});
