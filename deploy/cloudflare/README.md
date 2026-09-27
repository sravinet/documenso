# Documenso on Cloudflare

Runs the unmodified Documenso Docker image (`docker/Dockerfile`) on
[Cloudflare Containers](https://developers.cloudflare.com/containers/), fronted by a Worker.

```
client ──▶ Worker (src/index.ts) ──▶ DocumensoContainer (Durable Object) ──▶ Node server :3000
                 │                                                       │
                 └── cron */5: keeps instance-0 warm                     ├──▶ Postgres (external)
                                                                         └──▶ R2 via S3 API (uploads)
```

Documenso relies on native Node modules (skia-canvas, sharp, bcrypt) and an in-process
cron poller, so it runs in a container rather than directly on the Workers runtime.

## What the Worker does

- Load-balances across `CONTAINER_INSTANCE_COUNT` container instances (the app is stateless; sessions live in Postgres).
- Forwards every string var and secret to the container as an environment variable
  (except the Worker-only `CONTAINER_INSTANCE_COUNT` and `SIGNING_CERT_R2_KEY`).
- Sets `x-forwarded-for` / `x-real-ip` from `CF-Connecting-IP` so audit logs record the signer's IP. Client-supplied values are overwritten.
- Waits up to 3 minutes on cold start, because the image runs `prisma migrate deploy` before it listens.
- Pings `/api/health` on `instance-0` every 5 minutes so background jobs (reminders, expirations) keep running.
  This keeps one instance running all the time, so it is billed continuously.

## Prerequisites

- Workers Paid plan (required for Containers).
- Docker running locally. `wrangler deploy` builds the image for `linux/amd64`.
  The build uses up to 8 GB of Node heap, so give your Docker VM at least 10 GB of memory (for example `colima start --memory 12`).
- A Postgres database reachable from the internet (Neon, Supabase, RDS, …).
  Containers connect to it directly; Hyperdrive is not used.

## Setup

```bash
cd deploy/cloudflare
pnpm install
```

### 1. Storage (R2)

```bash
pnpm exec wrangler r2 bucket create documenso-uploads
```

Create an R2 API token with read/write access to that bucket, then set:

| Secret | Value |
| --- | --- |
| `NEXT_PRIVATE_UPLOAD_ENDPOINT` | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `NEXT_PRIVATE_UPLOAD_BUCKET` | `documenso-uploads` |
| `NEXT_PRIVATE_UPLOAD_ACCESS_KEY_ID` | R2 token access key |
| `NEXT_PRIVATE_UPLOAD_SECRET_ACCESS_KEY` | R2 token secret |

Browsers upload directly to R2 with presigned URLs, so add a CORS rule on the bucket that allows
`PUT` and `GET` from your `NEXT_PUBLIC_WEBAPP_URL` origin.

### 2. Signing certificate

If the base64 `.p12` is under 5 KB, store it as a secret:

```bash
base64 -i cert.p12 | pnpm exec wrangler secret put NEXT_PRIVATE_SIGNING_LOCAL_FILE_CONTENTS
```

Otherwise upload it to the certificate bucket declared in `wrangler.jsonc`.
The Worker reads it at container start and passes it as `NEXT_PRIVATE_SIGNING_LOCAL_FILE_CONTENTS`:

```bash
pnpm exec wrangler r2 bucket create documenso-signing-cert
pnpm exec wrangler r2 object put documenso-signing-cert/cert.p12 --file ./cert.p12 --remote
```

In both cases, set `NEXT_PRIVATE_SIGNING_PASSPHRASE`.

### 3. Remaining secrets

Set each one with `pnpm exec wrangler secret put <NAME>`:

- `NEXT_PUBLIC_WEBAPP_URL`: the public URL, for example `https://sign.example.com`
- `NEXTAUTH_SECRET`, `NEXT_PRIVATE_ENCRYPTION_KEY`, `NEXT_PRIVATE_ENCRYPTION_SECONDARY_KEY`: random strings of 32+ characters
- `NEXT_PRIVATE_DATABASE_URL`, `NEXT_PRIVATE_DIRECT_DATABASE_URL`
- Email: `NEXT_PRIVATE_SMTP_TRANSPORT` plus that transport's settings. HTTP transports (`resend`, `mailchannels`) avoid any SMTP port restrictions.

Any other Documenso variable (see `.env.example` at the repo root) can be added as a var or secret in the same way.

### 4. Deploy

```bash
pnpm run deploy
```

Then add a custom domain for the Worker, and make sure it matches `NEXT_PUBLIC_WEBAPP_URL`.

The first request after a deploy (or after an instance sleeps) waits for the container to boot and run migrations.

## Development

```bash
pnpm test          # unit + integration tests (vitest-pool-workers)
pnpm typecheck     # wrangler types --check && tsc
pnpm cf-typegen    # regenerate worker-configuration.d.ts after editing wrangler.jsonc
```

## Tuning

- `instance_type` (in `wrangler.jsonc`): `standard-1` (1/2 vCPU, 4 GiB). Raise it if sealing large PDFs is slow.
- `CONTAINER_INSTANCE_COUNT`: must stay at or below `max_instances`.
