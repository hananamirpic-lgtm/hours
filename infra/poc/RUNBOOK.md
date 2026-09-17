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
6. health-checks, and **rolls back automatically if the new version is unhealthy**.

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

## When the POC graduates to production

Move to the managed stack in [`../cloud/`](../cloud/): RDS (managed backups/PITR/Multi-AZ),
ECS/ALB (HA, rolling deploys), the release pipeline, and secrets in Secrets Manager. The app
code and images are unchanged - only the surrounding infrastructure grows up.