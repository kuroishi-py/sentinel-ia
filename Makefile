.PHONY: help install train train-ulb api test cov lint e2e up down seed carga carga-capacidad k6 clean

help:
	@echo "install          Crea .venv e instala dependencias de desarrollo"
	@echo "train            Reentrena el modelo de servicio (11 features, historia simulada)"
	@echo "train-ulb        Benchmark con el dataset real de la ULB (baja 150 MB)"
	@echo "api              Levanta decision-api sin Docker (feature store en memoria)"
	@echo "test             Pruebas unitarias e integracion (sin Docker)"
	@echo "cov              Pruebas con reporte de cobertura"
	@echo "lint             Revisa el codigo con ruff"
	@echo "up               Levanta todo con Docker Compose (TLS, API, Redis, RabbitMQ, worker, dashboard)"
	@echo "e2e              Prueba de interoperabilidad de punta a punta (requiere 'make up')"
	@echo "seed             Precarga 20.000 usuarios en Redis para la carga"
	@echo "carga            Locust al pico del caso: 116 tx/s, 3 min"
	@echo "carga-capacidad  Locust a saturacion por 1 min (muestra la cuota)"
	@echo "k6               k6 a 116 tx/s de llegada constante, 3 min"
	@echo "down             Baja Docker Compose y borra volumenes"

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements-dev.txt
	@echo "Listo. Activa con: source .venv/bin/activate"

train:
	python -m src.training.train --simulated --model-version v1.0.0-sim

train-ulb:
	python -m src.training.download_dataset
	python -m src.training.train --data data/creditcard.csv --label Class --drop Time --out artifacts/benchmark_ulb

api:
	FEATURE_STORE=memory PUBLISHER_ENABLED=false API_KEYS=local:clave-local \
	uvicorn src.decision_api.main:app --reload --port 8000

test:
	pytest

cov:
	pytest --cov=src --cov-report=term-missing

lint:
	ruff check src tests load dashboard scripts

up:
	mkdir -p reports data
	docker compose up -d --build
	@echo "API (TLS)  https://localhost:8443/docs"
	@echo "Dashboard  https://localhost:8444"
	@echo "API local  http://localhost:8000/docs"
	@echo "RabbitMQ   http://localhost:15672 (guest/guest)"

e2e:
	docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt reports/caddy-root.crt
	SENTINEL_E2E_URL=https://localhost:8443 pytest -m e2e -v

seed:
	docker compose exec api python -m src.demo.seed --users 20000

carga: seed
	PERFIL=pico docker compose --profile carga run --rm locust

carga-capacidad: seed
	PERFIL=capacidad DURACION=1m docker compose --profile carga run --rm locust

k6: seed
	docker compose --profile carga run --rm k6

down:
	docker compose down -v

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
