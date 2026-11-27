// Prueba de carga complementaria con k6: 116 tx/s de llegada constante.
//
// Por que existe ademas de Locust: en Docker Desktop con 2 vCPU, el cliente de
// Locust (Python sobre gevent) le suma decenas de milisegundos a cada solicitud
// cuando hay ~100 conexiones abiertas, aunque el servidor tarde 1,5 ms (lo mide
// el middleware ServerTiming). k6 esta escrito en Go y su ejecutor
// constant-arrival-rate fija la tasa de llegada sin depender de cuanto tarde
// cada respuesta, que es como llega el trafico real de la pasarela.
//
// Usa la misma poblacion que Locust: los usuarios que precarga src.demo.seed.
//
//   docker compose exec api python -m src.demo.seed --users 20000
//   docker compose --profile carga run --rm k6
//
// Deja el resumen en reports/k6_pico.json.

import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const API = __ENV.API_URL || "http://api:8000";
const KEY = __ENV.SENTINEL_API_KEY || "sentinel-demo-local";
const USERS = parseInt(__ENV.USUARIOS_PRECARGADOS || "20000", 10);
const RATE = parseInt(__ENV.TASA || "116", 10);

const degraded = new Counter("decisiones_degradadas");

export const options = {
  scenarios: {
    pico: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: __ENV.DURACION || "3m",
      preAllocatedVUs: 150,
      maxVUs: 400,
    },
  },
  thresholds: {
    // Objetivos de la EP2 (RNF1) medidos desde el cliente.
    http_req_duration: ["p(99)<100"],
    http_req_failed: ["rate<0.01"],
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
};

// Misma formula que src/demo/seed.py.
const pad = (n) => String(n).padStart(6, "0");
const average = (n) => 10000 + ((n * 7919) % 50000);
const B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
const newId = () => "tx-" + Array.from({ length: 26 }, () => B32[Math.floor(Math.random() * 32)]).join("");
const FOREIGN = ["AR", "PE", "BR", "US", "ES"];

export default function () {
  const n = 1 + Math.floor(Math.random() * USERS);
  const amount = Math.round(average(n) * Math.exp(0.5 * (Math.random() + Math.random() + Math.random() - 1.5)));
  const payload = {
    id_tx: newId(),
    user_token: `usr_${pad(n)}`,
    merchant_id: `mrc_${String(1 + Math.floor(Math.random() * 300)).padStart(4, "0")}`,
    amount: amount,
    currency: "CLP",
    country: Math.random() < 0.02 ? FOREIGN[Math.floor(Math.random() * FOREIGN.length)] : "CL",
    device_token: Math.random() < 0.02 ? `dev_nuevo_${newId()}` : `dev_${pad(n)}`,
    mcc: "5411",
  };
  const res = http.post(`${API}/v1/score`, JSON.stringify(payload), {
    headers: { "Content-Type": "application/json", "X-API-Key": KEY },
    timeout: "2s",
  });
  const ok = check(res, { "200": (r) => r.status === 200 });
  if (ok && res.json("degraded")) degraded.add(1);
}

export function handleSummary(data) {
  return {
    "reports/k6_pico.json": JSON.stringify(data, null, 2),
    stdout: textSummary(data),
  };
}

function textSummary(data) {
  const d = data.metrics.http_req_duration.values;
  const f = data.metrics.http_req_failed.values.rate;
  const it = data.metrics.http_reqs.values;
  const fmt = (v) => `${v.toFixed(2)} ms`;
  return [
    "",
    "k6 · SENTINEL-IA · llegada constante",
    `  solicitudes      ${it.count}  (${it.rate.toFixed(1)} req/s)`,
    `  p50 ${fmt(d.med)}  p90 ${fmt(d["p(90)"])}  p95 ${fmt(d["p(95)"])}  p99 ${fmt(d["p(99)"])}  max ${fmt(d.max)}`,
    `  errores          ${(f * 100).toFixed(2)}%`,
    `  degradadas       ${data.metrics.decisiones_degradadas ? data.metrics.decisiones_degradadas.values.count : 0}`,
    `  p99 < 100 ms     ${d["p(99)"] < 100 ? "CUMPLE" : "NO CUMPLE"}`,
    "",
  ].join("\n");
}
