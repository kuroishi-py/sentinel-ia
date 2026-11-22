# Datasets

Los datos no se versionan: pesan y no corresponde subirlos al repositorio.
Esta carpeta queda vacía a propósito.

## Para el informe

**Credit Card Fraud Detection** (Université Libre de Bruxelles) es la opción más
corta. Son 284.807 transacciones con 0,172% de fraude, un desbalance parecido al
0,5% del caso de SENTINEL-IA. Las features vienen anonimizadas por PCA (`V1` a
`V28`) más `Time` y `Amount`, y la etiqueta es `Class`.

Descárgalo a `data/creditcard.csv` y entrena:

```bash
python -m src.training.train --data data/creditcard.csv --label Class
```

Una advertencia sobre este dataset: como las features están anonimizadas por
PCA, no se puede contar una historia sobre *qué* feature delató el fraude. La
explicación SHAP va a decir «V14 aportó 0,31», que para un analista de Fraud Ops
no significa nada. Si quieren que la parte de interpretabilidad del informe se
sostenga, conviene mapear esas componentes a las features del diseño o usar el
dataset siguiente.

**IEEE-CIS Fraud Detection** es más trabajo pero tiene features con nombre:
monto, tarjeta, dominio de correo, dispositivo, hora. Con ese sí se puede
escribir «el rechazo se explica porque el dispositivo era nuevo y el monto
superaba nueve veces el promedio del usuario», que es lo que la pauta pide
cuando habla de interpretabilidad para la toma de decisiones.

## Para probar el pipeline

Sin descargar nada:

```bash
python -m src.training.train --synthetic
```

Genera 60.000 transacciones con 0,5% de fraude y features con los nombres del
diseño. Sirve para verificar que entrenamiento, API, worker y dashboard
funcionan de punta a punta. **No sirve para el informe:** los datos son
inventados y las métricas no significan nada.

## Archivos que genera el sistema

| Archivo | Qué es |
|---|---|
| `cases.jsonl` | Casos que crea `shap_worker` con su explicación SHAP. Lo lee el dashboard. |
