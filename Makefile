.PHONY: help install train api test cov lint load up down clean

help:
	@echo "install  Crea .venv e instala dependencias de desarrollo"
	@echo "train    Entrena un modelo con datos sinteticos"
	@echo "api      Levanta decision-api en modo recarga"
	@echo "test     Corre los 17 casos de prueba"
	@echo "cov      Pruebas con reporte de cobertura"
	@echo "lint     Revisa el codigo con ruff"
	@echo "load     Prueba de carga al pico del caso (116 tx/s, 3 min)"
	@echo "up       Levanta todo con Docker Compose"
	@echo "down     Baja Docker Compose y borra volumenes"

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements-dev.txt
	@echo "Listo. Activa con: source .venv/bin/activate"

train:
	python -m src.training.train --synthetic

api:
	uvicorn src.decision_api.main:app --reload --port 8000

test:
	pytest

cov:
	pytest --cov=src --cov-report=term-missing

lint:
	ruff check src tests load dashboard

load:
	mkdir -p reports
	locust -f load/locustfile.py --host http://localhost:8000 \
	       --headless -u 116 -r 20 -t 3m --csv reports/carga

up:
	docker compose up -d
	@echo "API        http://localhost:8000/docs"
	@echo "Dashboard  http://localhost:8501"
	@echo "RabbitMQ   http://localhost:15672 (guest/guest)"

down:
	docker compose down -v

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
