import { buildContainerEnvVars, SIGNING_CERT_ENV_KEY } from './config';
import { encodeBase64 } from './encoding';

/**
 * Container env for a fresh start. The signing certificate comes from the
 * secret when set, otherwise from R2 (for certificates over the 5 KB secret limit).
 */
export const resolveContainerEnvVars = async (env: Env): Promise<Record<string, string>> => {
  const envVars = buildContainerEnvVars(env);

  if (envVars[SIGNING_CERT_ENV_KEY]) {
    return envVars;
  }

  const certKey = env.SIGNING_CERT_R2_KEY;

  if (!certKey) {
    return envVars;
  }

  const certObject = await env.SIGNING_CERT_BUCKET.get(certKey);

  if (!certObject) {
    console.warn(`Signing certificate "${certKey}" not found in R2; document sealing will be unavailable.`);
    return envVars;
  }

  envVars[SIGNING_CERT_ENV_KEY] = encodeBase64(new Uint8Array(await certObject.arrayBuffer()));

  return envVars;
};
