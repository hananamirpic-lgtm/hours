# POC Deployment Runbook - Single EC2 + CloudFront (Tel Aviv)

This is the **low-cost proof-of-concept** deployment for ~10 users. It runs the whole
system on one small EC2 instance with `docker compose`, fronted by CloudFront, in the
**Israel (Tel Aviv) `il-central-1`** region. Target cost: roughly **$18-25/month**.

> This is a deliberately simpler topology than the production CloudFormation stack in
> [`../cloud/`](../cloud/) and [`../README.md`](../README.md) (ALB + managed RDS + ECS).
> Use THIS runbook for the POC; use that stack when the POC graduates to production.

It assumes **no prior AWS experience**. Follow the phases in order.

---

## 0. What you are building

```
  Internet / phones ─── HTTPS ──▶ CloudFront ─── HTTPS/HTTP ──▶ EC2 (t4g.small)
                                                                   │ Caddy (reverse proxy)
                                                                   │  ├─ web  (React/nginx)
                                                                   │  ├─ api  (FastAPI)
                                                                   │  ├─ postgres ┐
                                                                   │  ├─ redis    │ data on /data (EBS)
                                                                   │  └─ minio ───┘
                                                                   ▼
                                                          S3 bucket (backups)
```

**Cost drivers avoided on purpose:** no load balancer, no managed RDS, no ElastiCache,
no NAT gateway. Those are the usual budget killers; for 10 users the single box is fine.

**Known tradeoff:** one box is not highly available - if the instance or its disk fails,
the app is down until you restore from backup. Acceptable for a POC; the backups below
make data loss very unlikely.

---

## 1. One-time account setup

1. Create the AWS account. Sign in as **root** only to do the next two steps.
2. Create an **IAM admin user** with a password and **MFA**, and use that from now on.
   (Root is for break-glass only.)
3. Set a **budget alert**: Billing -> Budgets -> Create budget -> Monthly cost -> e.g. $40
   -> email yourself. This is your safety net against a surprise bill.
4. Top-right region selector -> choose **Israel (Tel Aviv) il-central-1**. Do everything
   in this region EXCEPT the one CloudFront certificate note in Phase 5.
5. Install the AWS CLI on your laptop and run `aws configure` with the IAM user's keys and
   region `il-central-1`.

---

## 2. Create the image registry (ECR)

You ship versions as Docker images in a private registry.

1. Console -> **ECR** -> Create two **private** repositories: `hours-api` and `hours-web`.
2. Note your account id; your registry is
   `<ACCOUNT_ID>.dkr.ecr.il-central-1.amazonaws.com`.

**Build and push images** (from your laptop, in the repo root). Build **multi-arch** so the
image works whether the instance is Graviton (arm64) or x86 (amd64):

```bash
ACCOUNT=<ACCOUNT_ID>
REGION=il-central-1
REG=${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com
VERSION=v0.1.0

aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REG

docker buildx create --use 2>/dev/null || true
docker buildx build --platform linux/amd64,linux/arm64 \
  -t $REG/hours-api:$VERSION --target prod ./backend --push
docker buildx build --platform linux/amd64,linux/arm64 \
  -t $REG/hours-web:$VERSION --target prod ./frontend --push
```

---

## 3. Create the backups bucket

1. Console -> **S3** -> Create bucket, name e.g. `hours-poc-backups-<ACCOUNT_ID>`, region
   **il-central-1**, **Block all public access = ON**, **Versioning = ON**.
2. Add a **lifecycle rule** to expire objects after 30 days (keeps backup cost near zero).

---

## 4. Launch the EC2 instance

1. Console -> **EC2** -> Launch instance.
   - Name: `hours-poc`
   - AMI: **Amazon Linux 2023**
   - Instance type: **t4g.small** (Arm/Graviton, cheapest). If t4g is not offered in
     il-central-1, use **t3.small** (x86) - either works because the images are multi-arch.
   - Key pair: create/download one (for SSH).
   - Network: default VPC, **Auto-assign public IP = Enable**.
   - Storage: keep the 8 GB root; **Add a second EBS volume, 20 GB, gp3** (this becomes `/data`).
2. **Security group** (firewall):
   - SSH (22): **My IP** only.
   - HTTP (80) and HTTPS (443): temporarily **Anywhere** for setup; you lock 80 to
     CloudFront IP ranges in Phase 5.
3. **IAM role for the instance**: create an IAM role with these managed/inline permissions
   and attach it to the instance (so the box can pull images and write backups without keys):
   - `AmazonEC2ContainerRegistryReadOnly` (pull from ECR)
   - S3 read/write limited to your backups bucket
   - (optional) `AmazonSSMManagedInstanceCore` for browser-based shell access
4. Launch, then note the instance's **public IP / public DNS**.

---

## 5. Install and configure on the box

SSH in: `ssh -i your-key.pem ec2-user@<PUBLIC_DNS>`

```bash
# --- Docker + compose plugin ---
sudo dnf update -y
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
# log out and back in so the group applies, then:

# --- mount the 20 GB data volume at /data ---
lsblk                                   # find the new device, e.g. /dev/nvme1n1
sudo mkfs -t xfs /dev/nvme1n1           # ONLY if it is empty/new
sudo mkdir -p /data
echo "/dev/nvme1n1 /data xfs defaults,nofail 0 2" | sudo tee -a /etc/fstab
sudo mount -a
sudo mkdir -p /data/postgres /data/redis /data/minio /data/backups /data/deploy
sudo chown -R ec2-user:ec2-user /data

# --- get the code ---
sudo mkdir -p /opt/hours && sudo chown ec2-user:ec2-user /opt/hours
git clone <YOUR_REPO_URL> /opt/hours
cd /opt/hours

# --- production env file ---
cp infra/poc/.env.prod.example infra/poc/.env.prod
chmod 600 infra/poc/.env.prod
# Edit infra/poc/.env.prod and fill in EVERY REPLACE_ME (see the comments in that file).
# Generate secrets with:  openssl rand -base64 48
nano infra/poc/.env.prod
```

**First start:**

```bash
export ACCOUNT=<ACCOUNT_ID> REGION=il-central-1
export REG=${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com
export API_IMAGE=$REG/hours-api:v0.1.0 WEB_IMAGE=$REG/hours-web:v0.1.0

aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REG

docker compose -f docker-compose.yml -f infra/poc/docker-compose.prod.yml \
  --env-file infra/poc/.env.prod up -d

# migrate the database and create the first admin
docker compose -f docker-compose.yml -f infra/poc/docker-compose.prod.yml \
  --env-file infra/poc/.env.prod run --rm api alembic upgrade head
docker compose -f docker-compose.yml -f infra/poc/docker-compose.prod.yml \
  --env-file infra/poc/.env.prod run --rm api python -m app.seed

# verify locally on the box
curl -fsS http://localhost/api/health
```

Record the running version so upgrades know the rollback target:

```bash
echo v0.1.0 > /data/deploy/CURRENT_VERSION
```

---

## 6. Put CloudFront in front (public HTTPS, no domain yet)

CloudFront gives you a free `https://xxxx.cloudfront.net` URL and HTTPS without a domain.

1. Console -> **CloudFront** -> Create distribution.
2. **Origin domain**: your EC2 public DNS.
   - No custom domain yet -> set **Origin protocol = HTTP only**, port 80. (Caddy serves the
     app on :80; CloudFront adds the public HTTPS.)
3. **Viewer protocol policy**: Redirect HTTP to HTTPS.
4. **Cache behavior**:
   - Default (the SPA): cache normally.
   - Add a behavior for path pattern **`/api/*`**: **CachingDisabled**, and an origin request
     policy that **forwards the `Authorization` header, all cookies, and all query strings**
     (the API is dynamic and authenticated). CloudFront's managed policy
     `AllViewerExceptHostHeader` works well for this.
5. Create. Wait ~5-10 minutes for **Deployed**, then note the
   **Distribution domain name** `xxxx.cloudfront.net`.
6. **Lock the origin**: edit the EC2 security group so **port 80 is allowed only from
   CloudFront's IP ranges** (use the AWS-managed prefix list `com.amazonaws.global.cloudfront.origin-facing`),
   not from Anywhere. Keep 443 closed unless you later add a domain + Caddy TLS.

> **Note on the cert (for later):** when you add a custom domain, its ACM certificate must be
> created in **us-east-1 (N. Virginia)** - CloudFront only reads certs from that region,
> regardless of where the origin is. The `Caddyfile` has the domain block ready to uncomment.

---

## 7. Point the app at its public URL

Two settings must use the CloudFront URL, or QR codes and CORS will break:

1. In `infra/poc/.env.prod` set
   `CORS_ALLOWED_ORIGINS=https://xxxx.cloudfront.net`, then restart:
   ```bash
   cd /opt/hours
   docker compose -f docker-compose.yml -f infra/poc/docker-compose.prod.yml \
     --env-file infra/poc/.env.prod up -d api
   ```
2. Log in to the admin portal at `https://xxxx.cloudfront.net`, go to **Settings**, and set
   the **public app URL** to `https://xxxx.cloudfront.net`. This is what site QR codes encode,
   so QR downloads work and a scanned code opens the public site. (Until this is set, QR
   generation refuses with `public_app_url_not_configured`.)

---

## 8. Email (Amazon SES, Tel Aviv)

SES is available in `il-central-1`. New accounts start in the **sandbox** (can only send to
verified addresses), which is fine for a known 10-user POC.

1. Console -> **SES** (region il-central-1) -> verify your sender address/domain.
2. Verify each of the 10 users' email addresses (sandbox), OR request production access.
3. SES -> **SMTP settings** -> **Create SMTP credentials**. Put them where the app's mail
   client expects them and set `SMTP_HOST=email-smtp.il-central-1.amazonaws.com`,
   `SMTP_PORT=587` in `.env.prod`.

---

## 9. Upgrading to a new version (simple, with automatic backup + rollback)

On your laptop: build and push the new version to ECR (Phase 2 commands with a new
`VERSION=vX.Y.Z`). Then on the box, ONE command:

```bash
cd /opt/hours
export ECR_REGISTRY=<ACCOUNT_ID>.dkr.ecr.il-central-1.amazonaws.com
export BACKUP_BUCKET=s3://hours-poc-backups-<ACCOUNT_ID>
export AWS_REGION=il-central-1
# load DB creds so backup/restore can reach Postgres
set -a; . infra/poc/.env.prod; set +a

./infra/poc/scripts/deploy.sh vX.Y.Z
```

`deploy.sh` automatically:
1. **backs up** the database and object storage to S3 first (satisfies "back up the old
   version on every upgrade");
2. records the current version as the rollback target;
3. pulls the new images from ECR;
4. switches only `api` and `web` - **postgres/redis/minio keep running on `/data`, so the
   database is preserved and the change is transparent to users**;
5. runs `alembic upgrade head`;
6. health-checks, and **rolls back automatically if the new version is unhealthy**;
7. runs the post-deploy checks (sign-in, a read, the ticket API for the current flag state). A failed
   check is named and the script exits 1 but does **not** roll back (see Phase 12).

### Rolling back manually

```bash
cd /opt/hours
export ECR_REGISTRY=<ACCOUNT_ID>.dkr.ecr.il-central-1.amazonaws.com
export BACKUP_BUCKET=s3://hours-poc-backups-<ACCOUNT_ID> AWS_REGION=il-central-1
set -a; . infra/poc/.env.prod; set +a

./infra/poc/scripts/rollback.sh                 # switch code back to the previous version
# only if a schema change forces it:
./infra/poc/scripts/rollback.sh --restore-db    # also restore the pre-upgrade database
```

Most upgrades are forward-compatible, so a plain `rollback.sh` (code only, DB untouched) is
enough and the DB keeps all its data. `--restore-db` is the escape hatch for an incompatible
schema change.

---

## 10. Backups and minimal maintenance

**Nightly backup** - add a cron job on the box:

```bash
crontab -e
# run at 02:30 Israel time every night:
30 2 * * *  cd /opt/hours && export BACKUP_BUCKET=s3://hours-poc-backups-<ACCOUNT_ID> AWS_REGION=il-central-1 && set -a && . infra/poc/.env.prod && set +a && ./infra/poc/scripts/backup.sh nightly >> /data/backups/cron.log 2>&1
```

**Whole-box recovery** - schedule a nightly **EBS snapshot** of the `/data` volume:
EC2 -> Lifecycle Manager -> create a snapshot policy on the data volume, daily, retain 7.

**Test a restore ONCE now** (an untested backup is not a backup): run `backup.sh`, then on a
throwaway DB run `pg_restore` from the dump and confirm the data is there.

**Ongoing maintenance is minimal:**
- Amazon Linux 2023 applies security patches automatically.
- All containers `restart: unless-stopped` (survive reboots and crashes).
- Docker logs are rotated (10 MB x 5) so they cannot fill the disk.
- Check the budget alert email and the CloudWatch instance status check monthly.
- Reboot the box after a kernel update roughly monthly: `sudo reboot` (containers come back).

---

## 11. Checklist recap (priorities 1-7)

| Requirement | How this deployment meets it |
|---|---|
| 1. Lowest cost | Single t4g.small + gp3 + CloudFront/S3, no ALB/RDS/ElastiCache/NAT. ~$18-25/mo. |
| 2. Simple transition | One box, your existing compose file, this runbook, no IaC required. |
| 3. Simple upgrades | `./deploy.sh vX.Y.Z` - one command. |
| 4. Backup + rollback each upgrade | `deploy.sh` backs up first; `rollback.sh` returns to the prior version. |
| 5. DB preserved, transparent | Postgres on `/data`; only api/web are replaced on upgrade. |
| 6. Minimal maintenance | Auto-patched OS, auto-restart containers, rotated logs, nightly backup, monthly glance. |
| 7. Other gotchas | CloudFront cert must be us-east-1; SES sandbox; set public_app_url + CORS; lock :80 to CloudFront; WeasyPrint native libs in the prod image; test a restore. |

---

## 12. Service tickets: enablement, checks and back-out

The service-ticket system (customer portal at `/service`, management screens at `/tickets`,
API under `/api/service/`) ships inside the normal release (Phase 9) but stays **off** until
you turn it on. One flag, `SERVICE_TICKETS_ENABLED`, controls it. The migration
`0011_service_tickets` and `0012_staff_opened_tickets` run on every deploy whatever the flag says, so
turning the flag on later needs **no migration**. This phase covers only what is extra for this feature.

### 12.1 Settings

The 13 `SERVICE_*` settings live in `infra/poc/.env.prod` (template: `infra/poc/.env.prod.example`;
the dev equivalents are in `docker-compose.yml` and `.env.example`).

| Setting | Default | Note |
|---|---|---|
| `SERVICE_TICKETS_ENABLED` | `false` | On only for the word `true` (spaces and letter case ignored). `1`, `yes`, empty or unset mean off. Read once at API start. |
| `SERVICE_ACCESSIBILITY_CONTACT` | empty | **Must be supplied before turning the flag on** (phone or email shown on the accessibility statement). A flag-on `deploy.sh` stops at its pre-flight check while this is empty. If the value has spaces, wrap it in double quotes so `. infra/poc/.env.prod` (Phases 9 and 10) still works. |
| `SERVICE_CUSTOMER_TOKEN_TTL_SECONDS` | `28800` | Customer sign-in lifetime (8 hours). |
| `SERVICE_SIGNIN_RATE_MAX` / `_WINDOW_SECONDS` | `10` / `60` | Sign-ins per address. |
| `SERVICE_SUBMIT_RATE_MAX` / `_WINDOW_SECONDS` | `10` / `3600` | Ticket submissions per customer. |
| `SERVICE_REQUEST_RATE_MAX` / `_WINDOW_SECONDS` | `120` / `60` | Requests per customer. |
| `SERVICE_SIGNIN_LOCK_ATTEMPTS` / `_LOCK_SECONDS` | `5` / `900` | Per-phone sign-in lock. |
| `SERVICE_CLIENT_IP_HEADER` | `CloudFront-Viewer-Address` | Trusted only from a peer inside the CIDR list below. |
| `SERVICE_TRUSTED_PROXY_CIDRS` | private ranges (see the file) | Caddy on the Docker network. |

A rate or lifetime value that is not a positive integer falls back to its default. There is no new
secret: the customer-token key is derived from `JWT_SECRET_KEY`.

**Always run compose by hand with `--env-file infra/poc/.env.prod`.** The base `docker-compose.yml`
lists each `SERVICE_*` variable under `api.environment`, and those entries win over the `env_file`
of the prod overlay. Without `--env-file`, compose falls back to the defaults, so the flag is
forced to `false` and the contact is empty, with no error.

Two more traps when you run compose by hand on the box:

- **A shell that sourced `.env.prod` keeps the old values.** Phase 9 runs `set -a; . infra/poc/.env.prod; set +a`,
  and shell variables beat `--env-file`. After editing the flag, use a **new SSH session** (or
  `unset` every `SERVICE_*` variable), or the old value wins.
- **Set the image tags first.** `deploy.sh` exports `API_IMAGE` and `WEB_IMAGE` for its own run only;
  `.env.prod` still holds the tag written at bootstrap. Export the running version before any
  `up`, or compose recreates `api` on the old image:

  ```bash
  cd /opt/hours
  export ECR_REGISTRY=<ACCOUNT_ID>.dkr.ecr.il-central-1.amazonaws.com
  export API_IMAGE=$ECR_REGISTRY/hours-api:$(cat /data/deploy/CURRENT_VERSION)
  export WEB_IMAGE=$ECR_REGISTRY/hours-web:$(cat /data/deploy/CURRENT_VERSION)
  ```

### 12.2 First-time enablement checklist

Do these in order. Keep the flag **off** until step 8.

1. **Security review done** on the final implementation (the `builder-security` skill named by the
   repository security rule). Do it before the CloudFront update and before the first flag-on deploy.
2. **Port 80 locked to CloudFront** and verified (12.3).
3. **Accessibility contact supplied**: `SERVICE_ACCESSIBILITY_CONTACT` is set in `.env.prod` (12.1).
4. **Smoke user created** and `/data/deploy/smoke.env` written (12.5). Do this before the first
   deploy that includes the post-deploy checks, or `hours_signin` fails.
5. **Release shipped with the flag off, CloudFront updated first** (12.4, 12.6).
6. **Forged-header check passed** (12.7).
7. **Contact confirmed**: `grep '^SERVICE_ACCESSIBILITY_CONTACT=' infra/poc/.env.prod` shows a value.
8. **Flag flipped** (12.8).
9. **Flag-on checks pass** (12.6, manual block).
10. **Manual accessibility pass** done with `docs/service-tickets-accessibility-checklist.md`
    (the automated scan `npm run test:a11y` first, then the checklist with one desktop and one mobile
    screen reader, on a test environment running the build you accept). Do it before acceptance and
    before you give customers the address.

### 12.3 Precondition: port 80 reachable only from CloudFront

Caddy trusts `CloudFront-Viewer-Address` to rate-limit by the visitor's address. If port 80 is open to
the internet, a caller can reach the origin directly, skip CloudFront's HTTPS, and forge that header
to dodge the per-address limit. (The per-phone lock and per-customer limits still hold.) Phase 6
step 6 sets the rule; confirm it before the first flag-on deploy:

1. EC2 -> Security Groups -> the instance's group -> **Inbound rules**. Port 80 must have exactly one
   source: the prefix list `com.amazonaws.global.cloudfront.origin-facing`. There must be **no**
   `0.0.0.0/0` or `::/0` rule on port 80, and port 443 stays closed.
2. Optional read-only CLI check (look for `PrefixListIds` and empty `IpRanges`/`Ipv6Ranges` on port 80):

   ```bash
   aws ec2 describe-security-groups --region il-central-1 --group-ids <SG_ID> --query 'SecurityGroups[0].IpPermissions'
   ```
3. From a machine **outside** the allowed ranges (your laptop), the origin must not answer:
   `curl -m 10 http://<EC2_PUBLIC_DNS>/api/health` must time out or fail, not return JSON.

### 12.4 CloudFront, Caddy and the order of steps

Two edge changes ship with this feature. Neither is applied by `deploy.sh`; you do both by hand.

- **CloudFront:** one new behavior for `/api/service/*`, placed **before** `/api/*` (CloudFront matches in
  list order): allowed methods GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE, cache policy
  `CachingDisabled`, origin request policy `AllViewerAndCloudFrontHeaders-2022-06` (this one forwards
  `CloudFront-Viewer-Address`), compress on, redirect HTTP to HTTPS. `/api/*` is unchanged and still never caches.
  The SPA deep links `/service/*` and `/tickets/*` use the default behavior and need no change.
- **Caddy:** an exact-path block for `/api/service/tickets` and another for `/api/service/staff/tickets`,
  each with a 13 MiB request-body cap (above the app's own 12 MiB limit), placed before `handle /api/*`.
  They are in `infra/poc/caddy/Caddyfile`.

Order:

1. On the box, update the checkout to the release (`cd /opt/hours && git pull --ff-only`).
   `deploy.sh`, `docker-compose.yml` and the Caddyfile come from the checkout, not from the images.
   Build and push the images from your laptop as in Phase 2.
2. **Update the CloudFront stack** with the new `infra/poc/cloudfront.yaml` (or add the same behavior by
   hand if you created the distribution in the console). Keep the existing `OriginDomainName` value:

   ```bash
   aws cloudformation update-stack --stack-name <CLOUDFRONT_STACK> \
     --template-body file://infra/poc/cloudfront.yaml \
     --parameters ParameterKey=OriginDomainName,UsePreviousValue=true
   aws cloudformation wait stack-update-complete --stack-name <CLOUDFRONT_STACK>
   aws cloudfront get-distribution --id <DISTRIBUTION_ID> --query Distribution.Status   # wait for "Deployed"
   ```
3. **Load the new Caddy config.** Caddy keeps its old config until it is reloaded, and a plain `deploy.sh`
   does not recreate it. Recreate the container (a few seconds of interruption). Do not rely on
   `caddy reload` here: `git pull` replaces the file and the container can still show the old one.

   ```bash
   cd /opt/hours
   docker compose -f docker-compose.yml -f infra/poc/docker-compose.prod.yml \
     --env-file infra/poc/.env.prod up -d --force-recreate --no-deps caddy
   ```
4. Run `deploy.sh vX.Y.Z` (12.6) with `SERVICE_TICKETS_ENABLED=false` in `.env.prod`.
5. Only after the checks in 12.6 and 12.7 pass, flip the flag (12.8).

### 12.5 Smoke user for the post-deploy checks

The `hours_signin` check needs a real Hours sign-in. The bootstrap admin cannot be used (2FA is
mandatory for it), so use a dedicated low-privilege user. Create it on the running system **before**
the first deploy that includes the checks:

1. In the admin portal -> **Users**, create a user, for example `deploy-smoke`, with role
   **Site manager**, **no sites assigned**, and a temporary password.
2. Sign in once as that user in a browser and set your own password on the forced change-password screen.
   This clears `must_change_password`; with it set, the check cannot pass. Do **not** enrol 2FA for
   this user: the check sends no code, and an account with 2FA on answers `totp_required`.
3. On the box, write the credentials as the user that runs `deploy.sh` (the file is outside the repository):

   ```bash
   read -rsp 'Smoke password: ' SMOKE_PW; echo
   ( umask 077; printf 'SMOKE_USERNAME=%s\nSMOKE_PASSWORD=%s\n' deploy-smoke "$SMOKE_PW" > /data/deploy/smoke.env )
   unset SMOKE_PW
   chmod 600 /data/deploy/smoke.env
   ls -l /data/deploy/smoke.env        # -rw------- , not under /opt/hours
   ```

The file has exactly two lines, `SMOKE_USERNAME=...` and `SMOKE_PASSWORD=...`. `deploy.sh` reads it as
data, never prints it, never puts it on a command line, and warns if the mode is not 600. If the
password is changed, the user is deactivated, or the role changes, update the user or the file, or
`hours_signin` fails on the next deploy.

### 12.6 Post-deploy checks (`deploy.sh`)

After the existing health check, `deploy.sh` runs `post_deploy_checks` against `http://localhost`
(Caddy on the box), each with `curl --max-time 30`. A timeout counts as a failure. Every result is printed.

| Check | Runs when | Passes when |
|---|---|---|
| `hours_signin` | always | the smoke user signs in at `/api/auth/login`: 200 and an `access_token` |
| `hours_read` | always | `/api/auth/me` with that token: 200 and a `username` |
| `ticket_api_off` | flag off | `/api/service/auth/me` returns 404 |
| `ticket_api_on` | flag on | the same request returns neither 404 nor 5xx (401 expected) |
| `ticket_body_path` | flag on | a 2 MiB `POST /api/service/tickets` with no token returns 401 (proves Caddy and the API take the body) |
| `accessibility_contact` | flag on | `/api/features` has a non-empty `accessibility_contact` |

The flag state is read from `.env.prod` with the application's rule (trim, lowercase, equals `true`).

- **Pre-flight.** A flag-on deploy fails at once while `SERVICE_ACCESSIBILITY_CONTACT` is empty in the
  env file, before the backup or any change.
- **On a failed check** the script names it, prints the pre-upgrade backup name and **exits 1**. There
  is **no automatic rollback**: rollback stays tied to the health check only, because `--restore-db`
  discards data written after the backup. You decide: turn the flag off, `rollback.sh`, or
  `rollback.sh --restore-db` (12.9).
- **Do not re-run `deploy.sh` with the version that is already running** just to repeat the checks. It
  records that version as the rollback target, so `rollback.sh` would have nowhere to go back to.
- The script's own test: `bash infra/poc/scripts/tests/test_post_deploy_checks.sh` (Git Bash or WSL).

`deploy.sh` runs the flag-on checks only when the flag is already on at deploy time. After a manual
flip (12.8), run the same three checks by hand:

```bash
curl -s -o /dev/null -w '%{http_code}\n' --max-time 30 http://localhost/api/service/auth/me   # expect 401
head -c 2097152 /dev/zero | tr '\0' 'a' | curl -s -o /dev/null -w '%{http_code}\n' --max-time 30 \
  -H 'Content-Type: application/octet-stream' -H 'Expect:' --data-binary @- \
  http://localhost/api/service/tickets                                                       # expect 401
curl -fsS --max-time 30 http://localhost/api/features     # service_tickets true, accessibility_contact not empty
```

With the flag off, the first request must return 404 instead.

### 12.7 Forged-header check (once, after the CloudFront update)

This proves that a `CloudFront-Viewer-Address` value sent by a client is replaced by the real one
before it reaches the API. It works with the flag on or off (Caddy logs the request either way).
Run it after the CloudFront update is **Deployed** and the Caddy config is loaded (12.4).

1. **Turn on a temporary access log in Caddy.** Add `log { format json }` as the first line inside the
   `:80 { ... }` block of `infra/poc/caddy/Caddyfile`, and recreate Caddy:

   ```bash
   cd /opt/hours
   cp -p infra/poc/caddy/Caddyfile /tmp/Caddyfile.orig
   nano infra/poc/caddy/Caddyfile
   docker compose -f docker-compose.yml -f infra/poc/docker-compose.prod.yml \
     --env-file infra/poc/.env.prod up -d --force-recreate --no-deps caddy
   ```
2. **From your laptop**, send one normal and one forged request through CloudFront, and note your real
   public address (`curl https://checkip.amazonaws.com`):

   ```bash
   curl -sS -o /dev/null https://xxxx.cloudfront.net/api/service/auth/me
   curl -sS -o /dev/null -H 'CloudFront-Viewer-Address: 203.0.113.99:1234' https://xxxx.cloudfront.net/api/service/auth/me
   ```
3. **On the box**, read the logged header (Caddy may print the name as `Cloudfront-Viewer-Address`):

   ```bash
   docker compose -f docker-compose.yml -f infra/poc/docker-compose.prod.yml \
     --env-file infra/poc/.env.prod logs --since 5m caddy | grep -i 'cloudfront-viewer-address'
   ```
4. **Pass:** the first request logs your real address (`ip:port`, or `[ipv6]:port`), and the second logs
   that same real address, **not** `203.0.113.99`.
   **Fail:** the forged value appears, or the header is missing from both. Keep the flag **off**, and
   re-check 12.3 and the `/api/service/*` behavior and its origin request policy in 12.4. With the
   forged value accepted, the per-address limit can be dodged. With the header missing, the per-address
   limit is skipped and the API logs a warning.
5. **Turn the log off again** and remove the copy. The log records visitor addresses and request headers,
   so it must not stay on:

   ```bash
   cp -p /tmp/Caddyfile.orig infra/poc/caddy/Caddyfile && rm /tmp/Caddyfile.orig
   git status --short infra/poc/caddy/Caddyfile        # no output = back to the committed file
   docker compose -f docker-compose.yml -f infra/poc/docker-compose.prod.yml \
     --env-file infra/poc/.env.prod up -d --force-recreate --no-deps caddy
   ```

### 12.8 The flag: turning the ticket system on and off

The flag is read once, when the API starts, so a change takes effect only after the API is recreated.

1. Open a **new SSH session** and set the image tags (12.1).
2. Edit `infra/poc/.env.prod` and set `SERVICE_TICKETS_ENABLED=true` (or `false`). To turn it on,
   `SERVICE_ACCESSIBILITY_CONTACT` must already hold a value.
3. Recreate the API:

   ```bash
   cd /opt/hours
   docker compose -f docker-compose.yml -f infra/poc/docker-compose.prod.yml \
     --env-file infra/poc/.env.prod up -d --force-recreate api
   ```
4. Confirm the container sees it and the API agrees:

   ```bash
   docker compose -f docker-compose.yml -f infra/poc/docker-compose.prod.yml \
     --env-file infra/poc/.env.prod exec -T api printenv SERVICE_TICKETS_ENABLED
   curl -fsS http://localhost/api/features       # {"service_tickets": true, ...} or false
   ```
5. After turning it **on**, run the manual flag-on checks in 12.6.

Off means: every `/api/service/` request answers 404, `/service` and `/tickets` show a not-available
page, and the Hours console shows no ticket entry. A signed-in technician sees a message that the
ticket system is unavailable. The tables, photos, technician users and customers all stay, and
nothing is deleted. Turning the flag back on restores everything as it was.

### 12.9 Back-out: four paths

Use the lightest path that fixes the problem.

| # | Path | Use when | What it does |
|---|---|---|---|
| 1 | Flag off (12.8) | Normal back-out. Anything wrong with tickets. | Hides the feature. Keeps all data. No migration. |
| 2 | `rollback.sh` | The release is bad and **no technician user exists** (F11). | Code only. Previous image tags. Database untouched. |
| 3 | `rollback.sh --restore-db` | Code must go back **and** the database must return to its pre-upgrade state (F11, F12). | Switches code back and restores the pre-upgrade dump. Newer data is lost. |
| 4 | `alembic downgrade 0010_operations_admin_role` | A failed release needs the ticket tables gone. | Drops the ticket tables only. For a failed release, not for turning the feature off. |

Environment for paths 2 and 3: the same as "Rolling back manually" in Phase 9.

**Path 2, code only:**

```bash
./infra/poc/scripts/rollback.sh
```

**Path 3, code and data:**

```bash
./infra/poc/scripts/rollback.sh --restore-db
```

**Path 4, schema removal.** Take a backup first (`./infra/poc/scripts/backup.sh pre-downgrade`), then turn the
flag **off** and recreate the API (12.8) so nothing is querying the tables when they disappear. Run the
downgrade with the **new** image, the one that contains revision `0012_staff_opened_tickets` (an older image
cannot find the revision):

```bash
cd /opt/hours
docker compose -f docker-compose.yml -f infra/poc/docker-compose.prod.yml \
  --env-file infra/poc/.env.prod run --rm api alembic downgrade 0010_operations_admin_role
```

What the downgrade (revisions `0012_staff_opened_tickets` then `0011_service_tickets`, going back to `0010_operations_admin_role`) does and does not do:

- It **drops the `svc_` tables only**: `svc_customers`, `svc_categories`, `svc_tickets`,
  `svc_history_entries`, `svc_settings`, `svc_field_labels`, `svc_signin_failures`, and the ticket-number
  sequence. It **deletes every customer and ticket**.
- It **never touches the Hours tables or their data**: no existing table, column, index, constraint or row changes.
- It keeps the `technician` value in the user-role type (PostgreSQL cannot remove a value without
  rebuilding the type) and keeps every photo object under `service-tickets/` in the bucket.
- Never use a target lower than `0010_operations_admin_role` (and never `base`): lower targets undo
  Hours schema changes. A later `deploy.sh` runs `alembic upgrade head` and creates the ticket tables again, empty.

Two findings from the design explain the limits of paths 2 and 3. They are reproduced here so the
decision is made with the same facts:

> **F11. Code rollback below this release breaks the Users list once a technician exists.**
> SQLAlchemy's `Enum(UserRole)` raises `LookupError` on an unknown stored label. R25.9 correctly keeps
> the value after downgrade.
> Use the flag as the normal back-out (R21). If code must go back, use `rollback.sh --restore-db`.

> **F12. `rollback.sh --restore-db` leaves `svc_` tables behind** (`pg_restore --clean` only drops
> objects present in the dump) and does not restore object storage. Next deploy re-runs `0011` and `0012` against
> existing tables.
> `0011` is idempotent per table and seeds only tables it creates (R19.17); `0012` adds its column only when it is missing. Orphan photos stay in
> the bucket under the prefix and are harmless.

To see whether path 2 is safe (F11), count technician users before rolling code back. A result above 0
means: use the flag (path 1) or `--restore-db` (path 3), not plain `rollback.sh`:

```bash
docker compose -f docker-compose.yml -f infra/poc/docker-compose.prod.yml --env-file infra/poc/.env.prod \
  exec -T postgres psql -U "${POSTGRES_USER:-hours}" -d "${POSTGRES_DB:-hours}" -tAc "select count(*) from users where role = 'technician'"
```

Remember that `--restore-db` discards everything written to the restored tables after the pre-upgrade
backup. The backup name is in `/data/deploy/LAST_PRE_UPGRADE_BACKUP`. Photos written after that backup are
not restored or removed (F12).

### 12.10 Orphan photo sweep

A crash between writing a photo and committing its ticket leaves one object in the bucket that no ticket
references. The sweep lists the `service-tickets/` prefix in the Hours bucket (`S3_BUCKET_DOCUMENTS`) and
reports the objects that are old enough and not referenced by any `svc_tickets.photo_key`. It is **manual on
purpose**: no cron job, nothing runs it for you.

```bash
cd /opt/hours
# 1. Report only (nothing is deleted):
docker compose -f docker-compose.yml -f infra/poc/docker-compose.prod.yml --env-file infra/poc/.env.prod \
  run --rm api python -m app.service_tickets.maintenance sweep-orphans --older-than 1h

# 2. Read the ORPHAN lines. If they are what you expect, delete them:
docker compose -f docker-compose.yml -f infra/poc/docker-compose.prod.yml --env-file infra/poc/.env.prod \
  run --rm api python -m app.service_tickets.maintenance sweep-orphans --older-than 1h --delete
```

- `--older-than` is a number and a unit: `30m`, `1h`, `2d`. The minimum is 5 minutes and the maximum 365
  days, so a ticket being created right now keeps its photo. Prefer `1h` or more.
- Output lines are `ORPHAN <key> <time>`, then `DELETED <key>` or `FAILED <key>` with `--delete`, then a
  `summary:` line with counters. Only keys under `service-tickets/` are ever listed or deleted. No ticket data is printed.
- Exit codes: 0 done, 2 bad arguments, 3 the object store could not be listed, 4 the database could not be read
  (nothing deleted for that page), 5 some deletes failed (run it again).
- **When to run it:** after an API crash or restart during busy ticket submission, and as occasional
  housekeeping. Always run the report-only form first.
- **Be careful after the tables were removed and recreated** (path 4 followed by a new deploy). The
  new `svc_tickets` is empty, so every photo looks unreferenced and `--delete` would remove all of them.
  Do not use `--delete` in that case unless you mean to discard those photos. While the tables are
  missing, the sweep stops with exit code 4 and deletes nothing.

---

## When the POC graduates to production

Move to the managed stack in [`../cloud/`](../cloud/): RDS (managed backups/PITR/Multi-AZ),
ECS/ALB (HA, rolling deploys), the release pipeline, and secrets in Secrets Manager. The app
code and images are unchanged - only the surrounding infrastructure grows up.