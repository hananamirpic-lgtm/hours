#!/usr/bin/env bash
# ============================================================================
# bootstrap.sh - Step 5 in one command, run ON the EC2 box.
#
# Installs Docker + compose, mounts the /data disk, writes infra/poc/.env.prod
# (generating strong secrets for any value you leave blank), logs in to ECR,
# starts the stack, runs migrations, seeds the first admin, and sets the in-app
# public_app_url. Idempotent-ish: safe to re-run; it will not reformat a disk
# that already has a filesystem and will not overwrite an existing .env.prod.
#
# PREREQUISITES (Steps 1-4, already done):
#   - This instance has the IAM role (ECR pull + S3) attached.
#   - Images hours-api:TAG and hours-web:TAG are pushed to ECR.
#
# USAGE (as ec2-user):
#   export ECR_REGISTRY=123456789012.dkr.ecr.il-central-1.amazonaws.com
#   export IMAGE_TAG=v0.1.1
#   export PUBLIC_URL=https://REPLACE_AFTER_CLOUDFRONT.cloudfront.net   # set after Step 6, then re-run the env+restart part
#   export REPO_URL=https://github.com/hananamirpic-lgtm/hours.git
#   export REPO_BRANCH=feature/qr-travel-staffing-employee-i18n
#   export BOOTSTRAP_ADMIN_PASSWORD=choose-a-first-admin-password
#   curl -fsSL "$PUBLIC_URL_NOT_NEEDED" ; ./bootstrap.sh
# ============================================================================
set -euo pipefail

: "${ECR_REGISTRY:?set ECR_REGISTRY, e.g. 123456789012.dkr.ecr.il-central-1.amazonaws.com}"
: "${IMAGE_TAG:?set IMAGE_TAG, e.g. v0.1.1}"
REGION="${AWS_REGION:-il-central-1}"
REPO_URL="${REPO_URL:-https://github.com/hananamirpic-lgtm/hours.git}"
REPO_BRANCH="${REPO_BRANCH:-feature/qr-travel-staffing-employee-i18n}"
APP_DIR="${APP_DIR:-/opt/hours}"
# PUBLIC_URL is optional on the first run (before CloudFront exists). If unset, a
# placeholder is written and you re-run the "reconfigure" section after Step 6.
PUBLIC_URL="${PUBLIC_URL:-https://REPLACE_AFTER_CLOUDFRONT.cloudfront.net}"

gen() { openssl rand -base64 48 | tr -d '\n' | tr '/+' '__' | cut -c1-48; }

echo "==> [1/6] Install Docker, compose, git"
if ! command -v docker >/dev/null; then
  sudo dnf update -y
  sudo dnf install -y docker git jq
  sudo systemctl enable --now docker
  sudo mkdir -p /usr/libexec/docker/cli-plugins
  ARCH=$(uname -m)
  sudo curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${ARCH}" \
    -o /usr/libexec/docker/cli-plugins/docker-compose
  sudo chmod +x /usr/libexec/docker/cli-plugins/docker-compose
  sudo usermod -aG docker "$USER"
  echo "!! Docker group added. If the next docker command says 'permission denied',"
  echo "!! run:  newgrp docker   (or log out and back in) and re-run this script."
fi

echo "==> [2/6] Mount the /data disk"
DATA_DEV=""
for d in /dev/nvme1n1 /dev/xvdb; do [ -b "$d" ] && DATA_DEV="$d" && break; done
if [ -n "$DATA_DEV" ]; then
  if ! sudo blkid "$DATA_DEV" >/dev/null 2>&1; then
    echo "   formatting $DATA_DEV (empty)"
    sudo mkfs -t xfs "$DATA_DEV"
  fi
  sudo mkdir -p /data
  grep -q " /data " /etc/fstab || echo "$DATA_DEV /data xfs defaults,nofail 0 2" | sudo tee -a /etc/fstab
  sudo mount -a
else
  echo "   no second disk found; using / for /data (POC only)"
  sudo mkdir -p /data
fi
sudo mkdir -p /data/postgres /data/redis /data/minio /data/backups /data/deploy /data/restore
sudo chown -R "$USER":"$USER" /data

echo "==> [3/6] Get the code"
if [ ! -d "$APP_DIR/.git" ]; then
  sudo mkdir -p "$APP_DIR" && sudo chown "$USER":"$USER" "$APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
git fetch origin
git checkout "$REPO_BRANCH"
git pull --ff-only origin "$REPO_BRANCH" || true

echo "==> [4/6] Write infra/poc/.env.prod (generating any missing secrets)"
ENV_FILE=infra/poc/.env.prod
if [ -f "$ENV_FILE" ]; then
  echo "   $ENV_FILE already exists; leaving it as-is. Delete it first to regenerate."
else
  JWT="${JWT_SECRET_KEY:-$(gen)}"
  ENC="${ENCRYPTION_KEY:-$(gen)}"
  DBPW="${POSTGRES_PASSWORD:-$(gen)}"
  MINIO_AK="${S3_ACCESS_KEY_ID:-hoursminio}"
  MINIO_SK="${S3_SECRET_ACCESS_KEY:-$(gen)}"
  ADMINPW="${BOOTSTRAP_ADMIN_PASSWORD:-$(gen)}"
  install -m 600 /dev/null "$ENV_FILE"
  cat > "$ENV_FILE" <<ENVEOF
ENVIRONMENT=production
LOG_LEVEL=INFO
APP_TIMEZONE=Asia/Jerusalem
CORS_ALLOWED_ORIGINS=${PUBLIC_URL}
REQUIRE_2FA_ENROLMENT=true
JWT_SECRET_KEY=${JWT}
ENCRYPTION_KEY=${ENC}
POSTGRES_DB=hours
POSTGRES_USER=hours
POSTGRES_PASSWORD=${DBPW}
DATABASE_URL=postgresql+psycopg://hours:${DBPW}@postgres:5432/hours
REDIS_URL=redis://redis:6379/0
S3_ENDPOINT_URL=http://minio:9000
S3_PUBLIC_ENDPOINT_URL=${PUBLIC_URL}
S3_ACCESS_KEY_ID=${MINIO_AK}
S3_SECRET_ACCESS_KEY=${MINIO_SK}
S3_BUCKET_DOCUMENTS=hours-documents
S3_REGION=${REGION}
SMTP_HOST=email-smtp.${REGION}.amazonaws.com
SMTP_PORT=587
SMTP_FROM_ADDRESS=no-reply@example.com
SCHEDULER_ENABLED=true
HSTS_ENABLED=true
RATE_LIMIT_ENABLED=true
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=${ADMINPW}
BOOTSTRAP_ADMIN_LANGUAGE=he
API_IMAGE=${ECR_REGISTRY}/hours-api:${IMAGE_TAG}
WEB_IMAGE=${ECR_REGISTRY}/hours-web:${IMAGE_TAG}
ENVEOF
  echo "   Wrote $ENV_FILE. First-admin password:"
  echo "     $ADMINPW"
  echo "   (change it in-app after first login, then remove it from the file)"
fi

echo "==> [5/6] ECR login, start, migrate, seed"
set -a; . infra/poc/.env.prod; set +a
export API_IMAGE WEB_IMAGE
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY"
CF="-f docker-compose.yml -f infra/poc/docker-compose.prod.yml --env-file infra/poc/.env.prod"
docker compose $CF up -d
docker compose $CF run --rm api alembic upgrade head
docker compose $CF run --rm api python -m app.seed

echo "==> [6/6] Set public_app_url and record version"
docker compose $CF exec -T postgres \
  psql -U hours -d hours -c "UPDATE settings SET value='${PUBLIC_URL}' WHERE key='public_app_url';" || \
  echo "   (could not set public_app_url yet; set it in the Settings screen after Step 6)"
echo "${IMAGE_TAG}" > /data/deploy/CURRENT_VERSION

echo "==> Done. Local health check:"
curl -fsS http://localhost/api/health && echo "" && echo "OK - app is up on the box."
echo ""
echo "Next: create CloudFront (infra/poc/cloudfront.yaml), then re-run the reconfigure step"
echo "to point CORS_ALLOWED_ORIGINS, S3_PUBLIC_ENDPOINT_URL and public_app_url at the CloudFront URL."