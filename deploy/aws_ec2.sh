#!/bin/bash
# Crea la instancia EC2 de SENTINEL-IA desde AWS CloudShell.
#
#   bash deploy/aws_ec2.sh [parche.patch]
#
# Crea (o reutiliza) un grupo de seguridad con 80 y 443 abiertos y el 22 solo
# para la IP de CloudShell, un par de llaves, una IP elastica y una instancia
# t3.medium con Amazon Linux 2023 y el perfil LabInstanceProfile. La instancia
# se configura sola con deploy/ec2_user_data.sh (unos 10 minutos).
#
# Si se pasa un parche (cambios que todavia no estan en GitHub), se sube a S3 y
# la instancia lo aplica sobre la rama indicada en REPO_REF.
#
# Variables opcionales: AWS_REGION (us-east-1), INSTANCE_TYPE (t3.medium),
# REPO_URL, REPO_REF (main), INSTANCE_PROFILE (LabInstanceProfile).
#
# Deja las URLs y las credenciales en ~/sentinel-ia-credenciales.txt.

set -euo pipefail

REGION=${AWS_REGION:-us-east-1}
NAME=sentinel-ia
TYPE=${INSTANCE_TYPE:-t3.medium}
REPO_URL=${REPO_URL:-https://github.com/kuroishi-py/sentinel-ia.git}
REPO_REF=${REPO_REF:-main}
PROFILE=${INSTANCE_PROFILE:-LabInstanceProfile}
PATCH_FILE=${1:-}
HERE=$(cd "$(dirname "$0")" && pwd)
KEY_FILE="$HOME/.ssh/${NAME}.pem"
CREDS="$HOME/sentinel-ia-credenciales.txt"
export AWS_DEFAULT_REGION=$REGION

say() { printf '\n== %s\n' "$*"; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
say "Cuenta $ACCOUNT, region $REGION"

# --- Parche en S3 ----------------------------------------------------------
PATCH_URI=""
if [ -n "$PATCH_FILE" ]; then
  BUCKET="${NAME}-${ACCOUNT}-${REGION}"
  if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    if [ "$REGION" = "us-east-1" ]; then
      aws s3api create-bucket --bucket "$BUCKET" >/dev/null
    else
      aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
    fi
  fi
  PATCH_URI="s3://${BUCKET}/deploy/$(basename "$PATCH_FILE")"
  aws s3 cp "$PATCH_FILE" "$PATCH_URI" >/dev/null
  say "Parche subido a $PATCH_URI"
fi

# --- Red ---------------------------------------------------------------------
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 describe-security-groups --filters Name=group-name,Values="${NAME}-sg" Name=vpc-id,Values="$VPC" \
  --query 'SecurityGroups[0].GroupId' --output text)
if [ "$SG" = "None" ]; then
  SG=$(aws ec2 create-security-group --group-name "${NAME}-sg" --vpc-id "$VPC" \
    --description "SENTINEL-IA: HTTPS publico, SSH solo desde CloudShell" --query GroupId --output text)
  # 80 lo necesita Let's Encrypt para validar el certificado; Caddy redirige a 443.
  aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 80 --cidr 0.0.0.0/0 >/dev/null
  aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 443 --cidr 0.0.0.0/0 >/dev/null
fi
MY_IP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 22 --cidr "${MY_IP}/32" >/dev/null 2>&1 || true
say "Grupo de seguridad $SG (SSH solo desde $MY_IP)"

# --- Llave SSH --------------------------------------------------------------
if [ ! -f "$KEY_FILE" ]; then
  mkdir -p "$HOME/.ssh"
  aws ec2 delete-key-pair --key-name "$NAME" >/dev/null 2>&1 || true
  aws ec2 create-key-pair --key-name "$NAME" --query KeyMaterial --output text > "$KEY_FILE"
  chmod 400 "$KEY_FILE"
fi

# --- IP elastica (la URL no cambia si el laboratorio reinicia la instancia) ---
ALLOC=""
PUBLIC_IP=""
if OUT=$(aws ec2 allocate-address --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$NAME}]" \
  --query '[AllocationId,PublicIp]' --output text 2>/dev/null); then
  read -r ALLOC PUBLIC_IP <<<"$OUT"
  say "IP elastica $PUBLIC_IP"
else
  say "Sin IP elastica (no permitida): se usara la IP publica de la instancia, que cambia si se reinicia"
fi

# --- Credenciales -----------------------------------------------------------
API_KEY=$(openssl rand -hex 24)
DASHBOARD_PASSWORD=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-16)

# --- Instancia --------------------------------------------------------------
AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)
USER_DATA=$(mktemp)
sed -e "s|__REPO_URL__|${REPO_URL}|" -e "s|__REPO_REF__|${REPO_REF}|" -e "s|__PATCH_URI__|${PATCH_URI}|" \
  -e "s|__REGION__|${REGION}|" -e "s|__PUBLIC_IP__|${PUBLIC_IP}|" -e "s|__API_KEY__|${API_KEY}|" \
  -e "s|__DASHBOARD_PASSWORD__|${DASHBOARD_PASSWORD}|" "$HERE/ec2_user_data.sh" > "$USER_DATA"

INSTANCE=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$TYPE" --key-name "$NAME" \
  --security-group-ids "$SG" --iam-instance-profile Name="$PROFILE" \
  --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2,HttpEndpoint=enabled \
  --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":20,"VolumeType":"gp3"}}]' \
  --user-data "file://$USER_DATA" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
  --query 'Instances[0].InstanceId' --output text)
rm -f "$USER_DATA"
say "Instancia $INSTANCE lanzada; esperando que arranque"
aws ec2 wait instance-running --instance-ids "$INSTANCE"

if [ -n "$ALLOC" ]; then
  aws ec2 associate-address --instance-id "$INSTANCE" --allocation-id "$ALLOC" >/dev/null
else
  PUBLIC_IP=$(aws ec2 describe-instances --instance-ids "$INSTANCE" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
fi
IP_DASHED=${PUBLIC_IP//./-}

umask 077
cat > "$CREDS" <<EOF
SENTINEL-IA en AWS ($(date -u +%FT%TZ))
Instancia:   $INSTANCE ($TYPE, $REGION)
IP publica:  $PUBLIC_IP
API:         https://api.${IP_DASHED}.sslip.io/docs
Dashboard:   https://dashboard.${IP_DASHED}.sslip.io
Usuario del dashboard:     fraudops
Contrasena del dashboard:  $DASHBOARD_PASSWORD
API key (X-API-Key):       $API_KEY
SSH:  ssh -i $KEY_FILE ec2-user@$PUBLIC_IP
Avance: ssh ... 'tail -f /var/log/sentinel-ia.log'   (listo cuando exista /var/log/sentinel-ia-ready)
EOF
say "Listo. La instancia termina de configurarse sola en unos 10 minutos."
cat "$CREDS"
