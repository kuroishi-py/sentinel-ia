#!/bin/bash
# User data de la instancia EC2 de SENTINEL-IA (Amazon Linux 2023).
#
# No se usa a mano: deploy/aws_ec2.sh reemplaza los valores __ASI__ y lo pasa
# a run-instances. Al encender, la instancia:
#   1. instala Docker, Compose y git;
#   2. clona el repositorio y, si hay, aplica el parche que dejo aws_ec2.sh en S3;
#   3. escribe .env con la clave de la API, el hash de la contrasena del
#      dashboard y los nombres sslip.io de su IP publica;
#   4. levanta el sistema con deploy/docker-compose.aws.yml y precarga la
#      historia de usuarios para la prueba de carga.
# Todo queda registrado en /var/log/sentinel-ia.log; al final escribe
# /var/log/sentinel-ia-ready.

set -euxo pipefail
exec > >(tee -a /var/log/sentinel-ia.log) 2>&1

REPO_URL="__REPO_URL__"
REPO_REF="__REPO_REF__"
PATCH_URI="__PATCH_URI__"
REGION="__REGION__"
PUBLIC_IP="__PUBLIC_IP__"
API_KEY="__API_KEY__"
DASHBOARD_PASSWORD="__DASHBOARD_PASSWORD__"
# Si alguna URL deja de existir, revisa la ultima version en GitHub.
COMPOSE_VERSION=v2.32.1
BUILDX_VERSION=v0.19.3
APP=/opt/sentinel-ia

dnf install -y docker git
systemctl enable --now docker
usermod -aG docker ec2-user

PLUGINS=/usr/local/lib/docker/cli-plugins
mkdir -p "$PLUGINS"
curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" -o "$PLUGINS/docker-compose"
curl -fsSL "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-amd64" -o "$PLUGINS/docker-buildx"
chmod +x "$PLUGINS/docker-compose" "$PLUGINS/docker-buildx"

git clone --branch "$REPO_REF" "$REPO_URL" "$APP"
cd "$APP"
if [ -n "$PATCH_URI" ]; then
  aws s3 cp "$PATCH_URI" /tmp/sentinel-ia.patch --region "$REGION"
  # git apply acepta tanto un diff como la salida de git format-patch.
  git apply /tmp/sentinel-ia.patch
fi

# IP publica: la elastica que reservo aws_ec2.sh o, si no hubo, la de la instancia.
if [ -z "$PUBLIC_IP" ]; then
  TOKEN=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
  PUBLIC_IP=$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)
fi
IP_DASHED=${PUBLIC_IP//./-}

HASH=$(docker run --rm caddy:2.8-alpine caddy hash-password --plaintext "$DASHBOARD_PASSWORD")

# Las comillas simples del hash evitan que Compose interprete sus "$".
cat > .env <<EOF
API_KEYS=pasarela:${API_KEY}
SENTINEL_API_KEY=${API_KEY}
AUTH_ENABLED=true
SCORER_BACKEND=local
SAGEMAKER_ENDPOINT=
AWS_REGION=${REGION}
SENTINEL_UID=1000
API_HOST=api.${IP_DASHED}.sslip.io
DASHBOARD_HOST=dashboard.${IP_DASHED}.sslip.io
DASHBOARD_USER=fraudops
DASHBOARD_PASSWORD_HASH='${HASH}'
EOF
chmod 600 .env
mkdir -p data reports
chown -R ec2-user:ec2-user "$APP"

docker compose -f docker-compose.yml -f deploy/docker-compose.aws.yml up -d --build

# Precarga: sin historia, cada usuario de la prueba de carga parece una cuenta
# nueva y el modelo manda la mitad a revision (ver src/demo/seed.py).
for _ in $(seq 1 60); do
  if docker compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2)"; then
    break
  fi
  sleep 5
done
docker compose exec -T api python -m src.demo.seed --users 20000

echo "listo $(date -u +%FT%TZ) https://api.${IP_DASHED}.sslip.io" > /var/log/sentinel-ia-ready
