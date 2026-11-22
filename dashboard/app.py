"""Dashboard de Fraud Ops.

Cubre dos cosas que la pauta de la EP3 pide por separado: el "dashboard
interactivo para visualizacion de resultados" (punto 1.2) y la
"interpretabilidad para toma de decisiones" (punto 2.1), porque muestra la
explicacion SHAP del rechazo junto al caso.

Es la version reducida de la fraudops-console de la EP2 (React sobre Nginx).
Streamlit se eligio por tiempo: lo que la evaluacion mira es que la explicacion
este visible y usable, no el framework.

Uso:
    streamlit run dashboard/app.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

API_URL = st.secrets.get("api_url", "http://localhost:8000") if hasattr(st, "secrets") else "http://localhost:8000"
CASES_PATH = Path("data/cases.jsonl")
METRICS_PATH = Path("artifacts/metrics.json")
METADATA_PATH = Path("artifacts/metadata.json")

st.set_page_config(page_title="SENTINEL-IA · Fraud Ops", page_icon="🛡", layout="wide")


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def load_cases() -> pd.DataFrame:
    if not CASES_PATH.exists():
        return pd.DataFrame()
    rows = []
    for line in CASES_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return pd.DataFrame(rows)


def fetch_service_metrics() -> dict:
    """Lee /metrics de la API y extrae los contadores que interesan."""
    try:
        response = requests.get(f"{API_URL}/metrics", timeout=2)
        response.raise_for_status()
    except requests.RequestException:
        return {}
    out: dict[str, float] = {}
    for line in response.text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) == 2:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                continue
    return out


st.title("SENTINEL-IA · consola de Fraud Ops")

metadata = load_json(METADATA_PATH)
model_metrics = load_json(METRICS_PATH)
service = fetch_service_metrics()

# --- estado del sistema ----------------------------------------------------
col1, col2, col3, col4 = st.columns(4)
col1.metric("Modelo en produccion", metadata.get("model_version", "sin modelo"))
col2.metric("Decisiones atendidas", int(service.get("sentinel_requests_total", 0)))
col3.metric("Degradadas", int(service.get("sentinel_degraded_total", 0)))
col4.metric("Rechazadas por cuota", int(service.get("sentinel_rate_limited_total", 0)))

if not service:
    st.warning(f"No se pudo leer /metrics en {API_URL}. ¿Esta corriendo decision-api?")

st.divider()

# --- metricas del modelo ---------------------------------------------------
st.subheader("Calidad del modelo")
main = model_metrics.get("at_reject_threshold", {})
if main:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Precision", f"{main.get('precision', 0):.3f}")
    m2.metric("Recall", f"{main.get('recall', 0):.3f}")
    m3.metric("F1", f"{main.get('f1', 0):.3f}")
    m4.metric("PR-AUC", f"{main.get('pr_auc', 0):.3f}")

    fpr = main.get("false_positive_rate_on_legit", 0)
    st.caption(
        f"Falsos positivos sobre transacciones legitimas: {fpr:.2%}. "
        f"El motor de reglas que se reemplaza esta en 40%."
    )

    cm = main.get("confusion_matrix", {})
    if cm:
        st.dataframe(
            pd.DataFrame(
                [[cm.get("tn", 0), cm.get("fp", 0)], [cm.get("fn", 0), cm.get("tp", 0)]],
                index=["Real: legitima", "Real: fraude"],
                columns=["Predijo legitima", "Predijo fraude"],
            ),
            use_container_width=True,
        )

    with st.expander("Barrido de umbrales"):
        sweep = model_metrics.get("sweep", [])
        if sweep:
            st.dataframe(
                pd.DataFrame(sweep)[["threshold", "precision", "recall", "f1", "false_positive_rate_on_legit"]],
                use_container_width=True,
            )
else:
    st.info("Sin metricas todavia. Corre: python -m src.training.train --synthetic")

st.divider()

# --- casos y explicaciones -------------------------------------------------
st.subheader("Casos abiertos")
cases = load_cases()

if cases.empty:
    st.info(
        "Sin casos. Levanta RabbitMQ y el worker, y manda transacciones a /v1/score "
        "para que se generen rechazos y revisiones."
    )
else:
    only_explained = st.checkbox("Solo casos con explicacion", value=False)
    view = cases[cases["explanation"].notna()] if only_explained else cases

    columns = [c for c in ("id_tx", "decision", "score", "amount", "merchant_id", "reason_codes", "created_at") if c in view.columns]
    st.dataframe(view[columns], use_container_width=True, height=260)

    ids = view["id_tx"].dropna().tolist()
    if ids:
        selected = st.selectbox("Ver la explicacion de un caso", ids)
        case = view[view["id_tx"] == selected].iloc[0].to_dict()

        left, right = st.columns([1, 2])
        with left:
            st.write("**Decision**", case.get("decision"))
            st.write("**Score**", case.get("score"))
            st.write("**Modelo**", case.get("model_version"))
            st.write("**Razones**", ", ".join(case.get("reason_codes") or []))
            if case.get("degraded"):
                st.warning("Decision degradada: falto una feature al momento de decidir.")

        with right:
            explanation = case.get("explanation")
            if not explanation:
                st.info("Este caso no tiene explicacion SHAP.")
            else:
                contributions = explanation.get("contributions", {})
                frame = (
                    pd.DataFrame({"feature": list(contributions), "aporte": list(contributions.values())})
                    .sort_values("aporte", key=abs, ascending=False)
                    .head(10)
                    .set_index("feature")
                )
                st.write("**Aporte de cada feature al score**")
                st.bar_chart(frame)
                st.caption(
                    f"Valor base del modelo: {explanation.get('base_value')}. "
                    f"Un aporte positivo empuja hacia fraude."
                )

st.divider()

# --- probador manual -------------------------------------------------------
st.subheader("Probar una transaccion")
with st.form("probar"):
    c1, c2, c3 = st.columns(3)
    id_tx = c1.text_input("id_tx", "tx-manual-001")
    user_token = c2.text_input("user_token", "usr_demo_0001")
    merchant_id = c3.text_input("merchant_id", "mrc_0042")
    c4, c5, c6 = st.columns(3)
    amount = c4.number_input("Monto", min_value=1.0, value=19990.0, step=1000.0)
    currency = c5.text_input("Moneda", "CLP")
    country = c6.text_input("Pais", "CL")
    submitted = st.form_submit_button("Evaluar")

if submitted:
    try:
        response = requests.post(
            f"{API_URL}/v1/score",
            json={
                "id_tx": id_tx,
                "user_token": user_token,
                "merchant_id": merchant_id,
                "amount": float(amount),
                "currency": currency,
                "country": country,
            },
            timeout=5,
        )
        if response.status_code == 200:
            body = response.json()
            decision = body["decision"]
            color = {"APPROVE": "normal", "REVIEW": "off", "REJECT": "inverse"}.get(decision, "normal")
            d1, d2, d3 = st.columns(3)
            d1.metric("Decision", decision, delta_color=color)
            d2.metric("Score", f"{body['score']:.4f}")
            d3.metric("Latencia", f"{body['latency_ms']:.1f} ms")
            st.json(body)
        else:
            st.error(f"{response.status_code}: {response.text}")
    except requests.RequestException as exc:
        st.error(f"No se pudo llamar a la API: {exc}")
