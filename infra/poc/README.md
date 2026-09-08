# POC deployment (single EC2 + CloudFront, Tel Aviv)

The low-cost proof-of-concept deployment for ~10 users: the whole system on one small
EC2 instance running `docker compose`, fronted by CloudFront, in `il-central-1`.
Target cost ~$18-25/month.

This is intentionally simpler and cheaper than the production stack in [`../cloud/`](../cloud/)
(ALB + managed RDS + ECS). Use this for the POC; move to that stack for production.

## Files

| File | Purpose |
|---|---|
| [`RUNBOOK.md`](RUNBOOK.md) | Step-by-step, beginner-friendly: account -> EC2 -> ECR -> CloudFront -> cutover -> upgrades. Start here. |
| [`docker-compose.prod.yml`](docker-compose.prod.yml) | Production overlay of the root compose file (prod images, Caddy TLS/proxy, SES, data on `/data`, log rotation). |
| [`caddy/Caddyfile`](caddy/Caddyfile) | Reverse proxy: `/api/*` -> api, everything else -> web. Ready for a custom domain later. |
| [`.env.prod.example`](.env.prod.example) | Every environment variable the app needs, with the AWS-specific ones called out. Copy to `.env.prod` on the box (git-ignored). |
| [`scripts/deploy.sh`](scripts/deploy.sh) | One-command upgrade: back up -> pull new ECR tag -> switch api/web -> migrate -> health-check -> auto-rollback. |
| [`scripts/rollback.sh`](scripts/rollback.sh) | Return to the previous version; `--restore-db` also restores the pre-upgrade database. |
| [`scripts/backup.sh`](scripts/backup.sh) | `pg_dump` + MinIO data to the S3 backup bucket. Run pre-upgrade (by deploy.sh) and nightly (cron). |
| [`scripts/bootstrap.sh`](scripts/bootstrap.sh) | Step 5 in one command, run ON the EC2 box: installs Docker, mounts `/data`, clones the repo, writes `.env.prod` (generating any missing secrets), starts the stack, migrates, and seeds the first admin. |
| [`cloudfront.yaml`](cloudfront.yaml) | CloudFormation for Step 6 only: the CloudFront distribution pointing at your existing EC2 (`OriginDomainName` parameter). Steps 1-5 are done outside it. |

## Quick reference (on the EC2 box)

```bash
# upgrade to a new version
./infra/poc/scripts/deploy.sh vX.Y.Z

# roll back (code only; DB kept)
./infra/poc/scripts/rollback.sh

# manual backup
./infra/poc/scripts/backup.sh manual
```

See [`RUNBOOK.md`](RUNBOOK.md) for the environment variables these scripts expect.