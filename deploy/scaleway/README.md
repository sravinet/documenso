# Documenso on Scaleway: 48-hour signing sessions

Each signing session is its own Scaleway Project. It lives exactly **48 hours**.
After that, an hourly reaper does four things in order:

1. Removes the container, so nothing more can be signed.
2. Copies the database and every uploaded file into an **object-locked** retention bucket.
3. Checks that copy against a SHA-256 manifest.
4. Only then destroys the Project.

Nothing in this directory changes the Documenso app. Everything runs in `fr-par`.

```
┌ documenso-control ─────────┐   ┌ documenso-retention ──────────────────────┐
│ Pulumi state bucket        │   │ archive bucket, COMPLIANCE lock (N years) │
│ provisioner key's default  │   │ archiver: write objects, never delete     │
└────────────────────────────┘   └──────────────▲────────────────────────────┘
┌ documenso-mail ────────────┐                  │ seal → archive → verify → destroy
│ TEM domain (verified once) │◀─ SMTP ─┐        │  (reaper, hourly)
└────────────────────────────┘         │        │
┌ documenso-<session> (48 h) ──────────┴────────┴──────────────────────────────┐
│ Serverless Container (min 1) ──private network──▶ PostgreSQL 16 (HA, encrypted)│
│   │ app key: this session's uploads bucket only (versioned)                  │
│   └ every key expires at expires_at + 12 h (failsafe if the reaper fails)    │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Where each boundary is enforced

| Boundary | What enforces it | Tested by |
|---|---|---|
| A session lasts exactly 48 h | `window.py` refuses any other `expiresAt` | `test_window.py`, `test_program.py` |
| The app only reaches its own session | App policy `project_ids = [session]`, and no rule anywhere is organization-wide | `test_boundary.py` |
| Nobody can delete an archive | COMPLIANCE object lock on the bucket. The archiver also holds no Delete permission. | `test_boundary.py`, `test_naming_and_grants.py` |
| Nothing is signed after the backup is taken | The reaper sets `phase=sealed` (container removed) before archiving | `test_boundary.py` (no container unless `live`) |
| A session is never destroyed unarchived | `archive.may_destroy`: the manifest must be verified for size, sha256 and lock on every item | `test_archive.py` |
| Credentials don't outlive a failed reaper | Each session API key has `expires_at = expires_at + 12 h` | `test_boundary.py` |
| Permission-set names are real | `permission_sets.json`, a snapshot of Scaleway's reference copied from `captain-assets`, plus a Project-scope check | `test_naming_and_grants.py` |

`test_boundary.py` and `test_program.py` run the real program under Pulumi's mock engine, following `captain-assets/scripts/check_trail_boundary.py`. They check what the program *declares*, not what Scaleway *enforces*. See [Verifying a live session](#verifying-a-live-session).

## One-time setup

Requires `scw` (2.x), `jq`, `pulumi` (3.259.0) and Python 3.12.

### 1. Mint the provisioner key (from an owner or bootstrap profile)

```bash
deploy/scaleway/scripts/bootstrap-provisioner.sh <bootstrap-profile>      # key expires in 7 days
```

This creates:
- the `documenso-control` project;
- the `documenso-provisioner` application and its policy (`ProjectManager`, `IAMApplicationManager`, `IAMPolicyManager`, plus only the products the stacks use);
- a key that expires in 7 days, saved as the `documenso` scw profile;
- the Pulumi state bucket.

The secret key is never printed. A 7-day key covers one session from open to reap. Mint a new one for each session.

### 2. Log in to the state backend and create the venv

```bash
cd deploy/scaleway
export SCW_PROFILE=documenso
export AWS_ACCESS_KEY_ID=$(scw config get access-key) AWS_SECRET_ACCESS_KEY=$(scw config get secret-key)
pulumi login "s3://documenso-pulumi-state-<org8>?endpoint=s3.fr-par.scw.cloud&region=fr-par&s3ForcePathStyle=true"
python3.12 -m venv venv && venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

### 3. Foundation (long-lived)

```bash
pulumi stack init foundation
pulumi config set organizationId <org-id>
pulumi config set retentionBucketName documenso-archive-<org8>
pulumi config set retentionYears <N>          # a legal decision; COMPLIANCE locks cannot be shortened
pulumi config set mailDomain sign.example.com
pulumi config set mailDomainAutoconfig true   # only if the domain is in Scaleway Domains & DNS
pulumi up
```

The first `up` creates everything except the TEM domain. Scaleway refuses a domain (`403 No active offer subscription`) until the mail Project subscribes to an offer, and the Pulumi provider cannot create that subscription. Subscribe once, then run `up` again:

```bash
scw tem offers update project-id=$(pulumi stack output mail_project_id) name=essential region=fr-par
pulumi up
```

`essential` has no commitment and includes 300 emails a month; `scale` has a 30-day commitment.

The lock configuration can also fail on the first `up`, when it is applied before the new bucket is ready. The second `up` applies it.

TEM domain verification **can take up to 48 hours**, so do this well before the first session. For a domain outside Scaleway DNS, add the SPF, DKIM, DMARC and MX records the console shows.

Save the archiver key for the reaper:

```bash
pulumi stack output archiver_access_key
pulumi stack output --show-secrets archiver_secret_key
```

## Running a session

```bash
pulumi stack init session-<id>
pulumi config set organizationId <org-id>
pulumi config set foundationStack <org>/documenso/foundation
pulumi config set hostname sign.example.com
pulumi config set dnsZone example.com                 # optional: creates the CNAME
pulumi config set image docker.io/documenso/documenso:<version>
pulumi config set mailFromAddress no-reply@sign.example.com
base64 -i cert.p12 | pulumi config set --secret signingCertificate
pulumi config set --secret signingPassphrase

./session_ctl.py open <id>      # starts the 48 h clock; refuses to reopen a session
pulumi up -s session-<id>
```

For a custom image, open with `--phase prepare`, then:
1. push the image to the `registry_endpoint` output;
2. set `image`;
3. run `./session_ctl.py phase <id> live`, then `pulumi up`.

### Tuning (all optional)

| Config | Default | Notes |
|---|---|---|
| `cpuLimit` | `2000` (mvCPU) | Serverless Containers accept 70 to 6000 |
| `memoryGib` | `4` | Up to 12 GB. The valid CPU/memory pairs are not published, so check the first `pulumi up`. |
| `maxScale` | `3` | `minScale` is fixed at 1, because Documenso's cron poller runs in-process |
| `dbNodeType` | `DB-PRO2-XXS` | HA and encryption at rest are always on |
| `dbVolumeGib` | `10` | `sbs_5k` volume |

## Ending a session

The reaper workflow (`.github/workflows/scaleway-session-reaper.yml`) runs hourly once the repository variable `DOCUMENSO_REAPER_ENABLED` is `true`. It needs:

| Kind | Name |
|---|---|
| Secret | `SCW_PROVISIONER_ACCESS_KEY` |
| Secret | `SCW_PROVISIONER_SECRET_KEY` |
| Secret | `PULUMI_CONFIG_PASSPHRASE` |
| Secret | `DOCUMENSO_ARCHIVER_ACCESS_KEY` |
| Secret | `DOCUMENSO_ARCHIVER_SECRET_KEY` |
| Variable | `SCW_ORGANIZATION_ID` |
| Variable | `PULUMI_BACKEND_URL` |
| Variable | `CI_RUNNER_DEFAULT` (optional; the blade fleet) |

To run it by hand:

```bash
ARCHIVER_ACCESS_KEY=… ARCHIVER_SECRET_KEY=… ./session_ctl.py reap <id>
./session_ctl.py reap <id> --force-before-expiry --confirm <id>   # end early
```

`reap` runs these steps, and is safe to re-run after any failure:

1. Set `phase=sealed` and run `pulumi up`. This removes the container, its domain and its DNS record. Data stays.
2. Take a logical backup through the RDB API (`POST /backups`, wait for `ready`, `POST /backups/{id}/export`) and download it from `download_url`.
3. Copy every version in the uploads bucket, current and noncurrent, into `sessions/<id>/…`. Each file is downloaded with the key scoped to the session project and re-uploaded with the key scoped to retention, because Object Storage can't copy across projects in one call. Each upload carries Content-MD5 and a `sha256` metadata field.
4. Write `manifest.json` last, then HEAD every item and check its size, sha256 and object-lock retention.
5. Only if that check finds no problems: `pulumi destroy`, then `pulumi stack rm`.

If step 4 finds anything, the session is **not** destroyed, and the workflow fails and names it.

## Verifying a live session

The tests can't show that Scaleway enforces these rules. After the first real session, check:

- **App key:** reading the retention bucket with it returns `AccessDenied`.
- **Archiver key:** `DeleteObject` on an archived item fails with the object lock.
- **Database:** has no public endpoint (`scw rdb instance get` shows only the private network).
- **Key expiry:** after `expires_at + 12 h`, the app key is rejected.

## What was not verified

- Nothing here has been run against a real Scaleway account yet: no `pulumi preview` or `up`.
- The provisioner's IAM application and policy are in `bootstrap-provisioner.sh`, but nobody has run it.
- The RDB export is Scaleway's own PostgreSQL format. It is archived as-is and has not been test-restored.
