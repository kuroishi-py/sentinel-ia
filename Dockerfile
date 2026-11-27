# Dockerfile de decision-api.
#
# Sigue el Anexo A de la EP2: multi-etapa, usuario no root y dependencias
# instaladas desde un requirements fijo. La raiz de solo lectura la aplica el
# orquestador (read_only en docker-compose.yml, readonlyRootFilesystem en ECS).
#
# El modelo NO va dentro de la imagen: se monta (local) o se descarga al
# iniciar segun la version aprobada (EP2 D10). Asi cambiar de modelo no obliga
# a reconstruir ni a redesplegar la imagen.
#
# Para ECS Fargate en ARM64 (Graviton, ~20% mas barato segun EP2 seccion 7.4):
#   docker buildx build --platform linux/arm64 -t sentinel-ia/decision-api:1.0.0 .

# --- etapa de construccion -------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# gcc se necesita para compilar algunas ruedas y no debe quedar en la imagen
# final: por eso esta etapa es aparte.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc g++ \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# --- etapa final -----------------------------------------------------------
FROM python:3.12-slim

LABEL org.opencontainers.image.title="SENTINEL-IA decision-api" \
      org.opencontainers.image.description="Scoring de fraude en tiempo real" \
      org.opencontainers.image.version="1.0.0"

# OMP_NUM_THREADS=1: un hilo de OpenMP por proceso. La concurrencia la dan
# los workers de gunicorn; sin esto, cada prediccion de XGBoost abre un hilo
# por nucleo y los procesos se pelean la CPU (ver scorer.py).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=1 \
    PATH="/opt/venv/bin:$PATH"

# Usuario sin privilegios: la EP2 lo exige por PCI-DSS y minimo privilegio.
RUN groupadd --gid 10001 sentinel \
 && useradd --uid 10001 --gid sentinel --no-create-home --shell /usr/sbin/nologin sentinel

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=sentinel:sentinel src/ ./src/

# El modelo se monta o se descarga en runtime; el directorio existe vacio.
RUN mkdir -p /app/artifacts && chown sentinel:sentinel /app/artifacts

USER sentinel
EXPOSE 8000

# /health/ready solo responde 200 cuando el modelo ya esta cargado, asi que
# sirve igual como health check del contenedor y del ALB (EP2 seccion 7.3).
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2).status==200 else 1)"

# 2 workers por tarea de 2 GB: la EP2 dimensiona ~0,45 GB por proceso con mas
# del 50% de holgura (seccion 7.4).
CMD ["gunicorn", "src.decision_api.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "30", \
     "--worker-tmp-dir", "/tmp", \
     "--access-logfile", "-"]
