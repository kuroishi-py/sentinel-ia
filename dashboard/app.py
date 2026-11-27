"""Dashboard de Fraud Ops.

Cubre dos cosas que la pauta de la EP3 pide por separado: el "dashboard
interactivo para visualizacion de resultados" (punto 1.2) y la
"interpretabilidad para toma de decisiones" (punto 2.1), porque muestra la
explicacion SHAP del rechazo junto al caso, en el lenguaje del analista.

Es la version reducida de la fraudops-console de la EP2 (React sobre Nginx).
Streamlit se eligio por tiempo: lo que la evaluacion mira es que la explicacion
este visible y usable, no el framework.

Uso:
    streamlit run dashboard/app.py

Variables:
    API_URL           donde esta decision-api (por omision http://localhost:8000)
    SENTINEL_API_KEY  clave con la que el dashboard llama a /v1/score
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import altair as alt
import pandas as pd
import requests
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.demo import scenarios  # noqa: E402

API_URL = os.environ.get("API_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("SENTINEL_API_KEY", "sentinel-demo-local")
CASES_PATH = ROOT / "data" / "cases.jsonl"
ARTIFACTS = ROOT / "artifacts"

#: Como se lee cada feature, y como se cuenta su valor.
LABELS = {
    "amount_log": ("Monto de la compra", lambda v: f"${math.expm1(v):,.0f}"),
    "amount_vs_user_avg": ("Monto frente al promedio del usuario", lambda v: f"{v:.1f} veces"),
    "tx_count_1h": ("Compras en la ultima hora", lambda v: f"{v:.0f}"),
    "tx_count_24h": ("Compras en las ultimas 24 h", lambda v: f"{v:.0f}"),
    "amount_sum_24h": ("Monto acumulado en 24 h", lambda v: f"${v:,.0f}"),
    "distinct_merchants_24h": ("Comercios distintos en 24 h", lambda v: f"{v:.0f}"),
    "user_age_days": ("Antiguedad del usuario", lambda v: f"{v:.0f} dias"),
    "merchant_risk": ("Riesgo historico del comercio", lambda v: f"{v:.1%}"),
    "is_foreign_country": ("Compra en el extranjero", lambda v: "si" if v else "no"),
    "hour_of_day": ("Hora (UTC)", lambda v: f"{v:.0f} h"),
    "is_new_device": ("Dispositivo nuevo (menos de 24 h)", lambda v: "si" if v else "no"),
}

st.set_page_config(page_title="SENTINEL-IA · Fraud Ops", page_icon="🛡", layout="wide")


# --- datos --------------------------------------------------------------------


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
    frame = pd.DataFrame(rows)
    if not frame.empty and "created_at" in frame:
        frame = frame.sort_values("created_at", ascending=False)
    return frame


def api_get(path: str) -> dict:
    try:
        response = requests.get(f"{API_URL}{path}", timeout=2)
        return response.json() if response.ok else {}
    except (requests.RequestException, ValueError):
        return {}


def score(payload: dict) -> tuple[int, dict]:
    try:
        response = requests.post(
            f"{API_URL}/v1/score", json=payload, headers={"X-API-Key": API_KEY}, timeout=5
        )
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {"detail": response.text}
    except requests.RequestException as exc:
        return 0, {"detail": str(exc)}


def wait_for_case(id_tx: str, seconds: float = 6.0) -> dict | None:
    """La explicacion llega por el bus: se espera a que el worker cree el caso."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        cases = load_cases()
        if not cases.empty:
            match = cases[cases["id_tx"] == id_tx]
            if not match.empty:
                return match.iloc[0].to_dict()
        time.sleep(0.4)
    return None


# --- explicacion ----------------------------------------------------------------


def explain(case: dict) -> None:
    """Grafico de aportes y frase legible para un caso."""
    explanation = case.get("explanation")
    if not isinstance(explanation, dict):
        st.info("Este caso no tiene explicacion SHAP (el worker no tenia modelo cargado).")
        return
    contributions: dict = explanation.get("contributions", {})
    values: dict = case.get("features") or {}
    rows = []
    for name, contribution in contributions.items():
        label, fmt = LABELS.get(name, (name, lambda v: f"{v:g}"))
        value = values.get(name)
        rows.append(
            {
                "senal": label,
                "valor": fmt(value) if isinstance(value, int | float) else "",
                "aporte": round(float(contribution), 3),
            }
        )
    frame = pd.DataFrame(rows).sort_values("aporte", key=abs, ascending=False)

    pushing = frame[frame["aporte"] > 0].head(3)
    if not pushing.empty:
        reasons = "; ".join(f"**{r.senal.lower()}** ({r.valor})" for r in pushing.itertuples())
        st.markdown(f"Lo que mas empujo hacia fraude: {reasons}.")
    top = frame.head(8).assign(
        sentido=lambda f: f["aporte"].map(lambda v: "hacia fraude" if v > 0 else "hacia legitima")
    )
    chart = (
        alt.Chart(top)
        .mark_bar()
        .encode(
            x=alt.X("aporte:Q", title="Aporte SHAP (logit)"),
            y=alt.Y("senal:N", sort=alt.EncodingSortField(field="aporte", op="max", order="descending"), title=None,
                    axis=alt.Axis(labelLimit=320)),
            color=alt.Color("sentido:N", scale=alt.Scale(domain=["hacia fraude", "hacia legitima"],
                                                         range=["#c0392b", "#2e86c1"]), legend=alt.Legend(title=None)),
            tooltip=["senal", "valor", "aporte"],
        )
        .properties(height=34 * len(top))
    )
    st.altair_chart(chart, use_container_width=True)
    st.dataframe(frame, hide_index=True, use_container_width=True)
    st.caption(
        f"Aportes SHAP en escala logit (valor base {explanation.get('base_value')}). "
        "Positivo empuja hacia fraude; la suma de base y aportes da el score."
    )


# --- encabezado -------------------------------------------------------------------

st.title("SENTINEL-IA · consola de Fraud Ops")

health = api_get("/health/ready")
stats = api_get("/v1/stats")
metadata = load_json(ARTIFACTS / "metadata.json")

if not health:
    st.error(f"No responde decision-api en {API_URL}. ¿Esta levantada?")

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Modelo en servicio", health.get("model_version") or metadata.get("model_version", "—"))
c2.metric("Decisiones", stats.get("requests_total", 0))
lat = stats.get("latency", {})
c3.metric("p50 interno", f"{lat.get('p50', 0):.2f} ms")
c4.metric("p95 interno", f"{lat.get('p95', 0):.2f} ms")
c5.metric("p99 interno", f"{lat.get('p99', 0):.2f} ms", help="Objetivo de la EP2: < 30 ms")
counters = stats.get("counters", {})
c6.metric("Degradadas", counters.get("degraded", 0))
decisions = stats.get("decisions", {})
if decisions:
    st.caption(
        "Decisiones de este worker: "
        + " · ".join(f"{k} {v}" for k, v in sorted(decisions.items()))
        + f" · cortadas por cuota {counters.get('rate_limited', 0)} · sin clave {counters.get('auth_rejected', 0)}"
        + f" · feature store: {health.get('details', {}).get('feature_store', '—')}"
    )


# --- casos ---------------------------------------------------------------------------

def render_cases() -> None:
    cases = load_cases()
    if cases.empty:
        st.info(
            "Sin casos todavia. Los crea shap-worker por cada revision o rechazo que llega por "
            "RabbitMQ. Prueba la pestana «Probar transacciones»."
        )
    else:
        f1, f2 = st.columns([1, 3])
        choice = f1.radio("Mostrar", ["Todos", "REJECT", "REVIEW"], horizontal=True)
        view = cases if choice == "Todos" else cases[cases["decision"] == choice]
        f2.caption(f"{len(view)} casos · origen: {CASES_PATH.name}, escrito por shap-worker")
        columns = [c for c in ("created_at", "id_tx", "decision", "score", "amount", "merchant_id", "reason_codes") if c in view]
        st.dataframe(view[columns], hide_index=True, use_container_width=True, height=260)

        ids = view["id_tx"].dropna().tolist()
        if ids:
            selected = st.selectbox("Explicacion del caso", ids)
            case = view[view["id_tx"] == selected].iloc[0].to_dict()
            left, right = st.columns([1, 2])
            with left:
                st.metric("Decision", case.get("decision"))
                st.metric("Score", f"{float(case.get('score') or 0):.3f}")
                st.write("**Monto**", f"${float(case.get('amount') or 0):,.0f}")
                st.write("**Comercio**", case.get("merchant_id"))
                st.write("**Razones**", ", ".join(case.get("reason_codes") or []))
                st.write("**Modelo**", case.get("model_version"))
                if case.get("degraded"):
                    st.warning("Decision degradada: falto una feature al momento de decidir.")
            with right:
                explain(case)

# --- probador ---------------------------------------------------------------------------

def render_try(auto: str | None = None) -> None:
    """Escenarios de demostracion. ``auto`` los dispara sin boton (?escenario=ato)."""
    risk = load_json(ARTIFACTS / "merchant_risk.json").get("merchants", {})
    risky = max(risk, key=risk.get) if risk else "mrc_0001"

    st.write(
        "Cada escenario crea un usuario nuevo, le arma un mes de historia normal (fechada hacia "
        "atras con el campo `timestamp`) y despues dispara el patron. Asi las features de velocidad, "
        "el promedio del usuario y la antiguedad del dispositivo tienen de donde salir."
    )
    presets = {
        "normal": ("Compra normal", lambda u: [scenarios.normal_purchase(u)]),
        "ato": ("Toma de cuenta", lambda u: scenarios.account_takeover(u, risky)),
        "tarjeta": ("Prueba de tarjeta", lambda u: scenarios.card_testing(u)),
    }
    b1, b2, b3 = st.columns(3)
    run = presets.get(auto or "")
    if b1.button("Compra normal", use_container_width=True):
        run = presets["normal"]
    if b2.button("Toma de cuenta", use_container_width=True):
        run = presets["ato"]
    if b3.button("Prueba de tarjeta", use_container_width=True):
        run = presets["tarjeta"]

    if run:
        title, build = run
        user = scenarios.new_user()
        for payload in scenarios.normal_history(user):
            score(payload)
        results = []
        for payload in build(user):
            code, body = score(payload)
            results.append(
                {
                    "id_tx": payload["id_tx"],
                    "monto": payload["amount"],
                    "pais": payload["country"],
                    "dispositivo": payload["device_token"].rsplit("_", 1)[-1],
                    "http": code,
                    "decision": body.get("decision", body.get("detail")),
                    "score": body.get("score"),
                    "latencia_ms": body.get("latency_ms"),
                }
            )
        st.subheader(title)
        st.dataframe(pd.DataFrame(results), hide_index=True, use_container_width=True)
        last = results[-1]
        if last["decision"] in {"REVIEW", "REJECT"}:
            with st.spinner("Esperando la explicacion de shap-worker (camino asincrono)..."):
                case = wait_for_case(last["id_tx"])
            if case:
                st.success("Caso creado por shap-worker a partir del evento en RabbitMQ.")
                explain(case)
            else:
                st.warning("La explicacion no llego. ¿Estan corriendo RabbitMQ y shap-worker?")

    with st.expander("Transaccion manual"), st.form("manual"):
        m1, m2, m3 = st.columns(3)
        user_token = m1.text_input("user_token", "usr_demo_manual")
        merchant_id = m2.text_input("merchant_id", "mrc_0042")
        amount = m3.number_input("Monto (CLP)", min_value=1.0, value=19_990.0, step=1_000.0)
        m4, m5, _ = st.columns(3)
        country = m4.text_input("Pais", "CL")
        device = m5.text_input("device_token", "dev_manual")
        if st.form_submit_button("Evaluar"):
            code, body = score(
                {
                    "id_tx": scenarios._id(),
                    "user_token": user_token,
                    "merchant_id": merchant_id,
                    "amount": float(amount),
                    "currency": "CLP",
                    "country": country,
                    "device_token": device,
                }
            )
            (st.json if code == 200 else st.error)(body)

# --- modelo ------------------------------------------------------------------------------

def render_model() -> None:
    served = load_json(ARTIFACTS / "metrics.json")
    bench = load_json(ARTIFACTS / "benchmark_ulb" / "metrics.json")
    bench_meta = load_json(ARTIFACTS / "benchmark_ulb" / "metadata.json")

    st.write(
        "Dos modelos, a proposito (issue #1). El **de servicio** usa las 11 features del diseno "
        "y es el que responde `/v1/score` y se explica arriba; se entreno con una historia simulada "
        "cuyas features calcula el mismo codigo que la API. El **benchmark** usa el dataset real de "
        "la ULB (componentes PCA): mide el algoritmo sobre fraude real, pero no se puede servir."
    )

    def summary(metrics: dict) -> dict:
        main = metrics.get("at_reject_threshold", {})
        review = metrics.get("at_review_threshold", {})
        return {
            "Precision (0,80)": main.get("precision"),
            "Recall (0,80)": main.get("recall"),
            "F1 (0,80)": main.get("f1"),
            "Recall (0,45)": review.get("recall"),
            "PR-AUC": main.get("pr_auc"),
            "ROC-AUC": main.get("roc_auc"),
            "Accuracy": main.get("accuracy"),
            "FP sobre legitimas (0,80)": main.get("false_positive_rate_on_legit"),
        }

    table = {"Servicio (simulado, 11 features)": summary(served)}
    if bench:
        table[f"Benchmark ULB ({bench_meta.get('rows', 0):,} tx reales)"] = summary(bench)
    base = served.get("baseline_rules")
    if base:
        table["Motor de reglas (mismos datos simulados)"] = {
            "Precision (0,80)": base.get("precision"),
            "Recall (0,80)": base.get("recall"),
            "FP sobre legitimas (0,80)": base.get("false_positive_rate_on_legit"),
        }
    st.dataframe(pd.DataFrame(table).style.format(precision=4, na_rep="—"), use_container_width=True)
    st.caption("Accuracy se muestra solo para dejar claro por que no sirve: con 0,5% de fraude, aprobar todo da 99,5%.")

    g1, g2 = st.columns(2)
    with g1:
        st.write("**Importancia global (promedio de |SHAP|)**")
        importance = served.get("global_importance_mean_abs_shap", {})
        if importance:
            frame = pd.DataFrame(
                {"senal": [LABELS.get(k, (k,))[0] for k in importance], "importancia": list(importance.values())}
            )
            st.altair_chart(
                alt.Chart(frame)
                .mark_bar(color="#5d6d7e")
                .encode(
                    x=alt.X("importancia:Q", title="Promedio de |SHAP|"),
                    y=alt.Y("senal:N", sort="-x", title=None, axis=alt.Axis(labelLimit=320)),
                )
                .properties(height=30 * len(frame)),
                use_container_width=True,
            )
    with g2:
        st.write("**Recall por tipo de fraude (umbral de revision)**")
        scen = served.get("recall_by_scenario_at_review", {})
        if scen:
            st.dataframe(
                pd.DataFrame(scen).T.rename(columns={"frauds": "fraudes", "recall_at_review": "recall"}),
                use_container_width=True,
            )
        cm = served.get("at_reject_threshold", {}).get("confusion_matrix", {})
        if cm:
            st.write("**Matriz de confusion (umbral 0,80)**")
            st.dataframe(
                pd.DataFrame(
                    [[cm.get("tn", 0), cm.get("fp", 0)], [cm.get("fn", 0), cm.get("tp", 0)]],
                    index=["Real: legitima", "Real: fraude"],
                    columns=["Predijo legitima", "Predijo fraude"],
                ),
                use_container_width=True,
            )

    with st.expander("Barrido de umbrales del modelo de servicio"):
        sweep = served.get("sweep", [])
        if sweep:
            st.dataframe(
                pd.DataFrame(sweep)[["threshold", "precision", "recall", "f1", "false_positive_rate_on_legit"]],
                hide_index=True,
                use_container_width=True,
            )


# --- composicion -----------------------------------------------------------------------
# ?vista=casos|probar|modelo abre una seccion sola (enlaces directos y capturas
# para el informe); ?escenario=normal|ato|tarjeta dispara un escenario al abrir.

vista = st.query_params.get("vista")
if vista == "casos":
    render_cases()
elif vista == "probar":
    render_try(st.query_params.get("escenario"))
elif vista == "modelo":
    render_model()
else:
    tab_cases, tab_try, tab_model = st.tabs(["Casos abiertos", "Probar transacciones", "Calidad del modelo"])
    with tab_cases:
        render_cases()
    with tab_try:
        render_try()
    with tab_model:
        render_model()
