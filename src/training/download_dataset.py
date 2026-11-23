"""Descarga el dataset de fraude desde OpenML.

Se usa OpenML y no Kaggle a proposito: Kaggle exige credenciales y un token por
persona, lo que hace que el equipo no pueda reproducir la descarga con un solo
comando. OpenML sirve el mismo dataset de la ULB sin autenticacion, con
licencia publica y cita formal, que es lo que se necesita para las referencias
APA del informe.

Dataset: creditcard (OpenML id 1597)
    284.807 transacciones de tarjetas europeas de septiembre de 2013
    492 fraudes, 0,172% del total
    30 features: Time, Amount y V1 a V28 (componentes principales)
    Etiqueta: Class (0 legitima, 1 fraude)

Cita (Dal Pozzolo et al., 2015):
    Dal Pozzolo, A., Caelen, O., Johnson, R. A., y Bontempi, G. (2015).
    Calibrating probability with undersampling for unbalanced classification.
    En 2015 IEEE Symposium Series on Computational Intelligence (pp. 159-166).
    IEEE. https://doi.org/10.1109/SSCI.2015.33

Importante sobre las features: V1 a V28 son componentes principales, no
variables con significado. Eso sirve para medir la calidad del modelo, pero no
para explicarle a un analista por que se rechazo una transaccion. Ver
data/README.md.

Uso:
    python -m src.training.download_dataset
    python -m src.training.download_dataset --format arff   # si el parquet falla
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("download")

OPENML_ID = 1597
PARQUET_URL = f"https://data.openml.org/datasets/0000/{OPENML_ID}/dataset_{OPENML_ID}.pq"
ARFF_URL = "https://api.openml.org/data/v1/download/1673544/creditcard.arff"

DATA_DIR = Path("data")
TARGET_CSV = DATA_DIR / "creditcard.csv"

#: Lo que se espera encontrar. Si el dataset cambia, mejor fallar que entrenar
#: en silencio con datos distintos a los que el informe declara.
EXPECTED_ROWS = 284_807
EXPECTED_FRAUD = 492


def _remote_size(url: str) -> int | None:
    """Tamano total segun el servidor, o None si no lo informa."""
    try:
        import requests

        head = requests.head(url, timeout=30, allow_redirects=True)
        return int(head.headers["Content-Length"])
    except Exception:  # noqa: BLE001
        return None


def download(url: str, destination: Path, attempts: int = 5) -> None:
    """Descarga con reintentos y reanudacion.

    Son 73 MB: una conexion que se corta a mitad de camino es lo normal, no la
    excepcion. Si el archivo parcial ya existe, se pide solo el resto con una
    cabecera Range en vez de volver a empezar.
    """
    import requests

    total = _remote_size(url)
    logger.info(
        "Descargando %s%s", url, f" ({total / 1e6:.1f} MB)" if total else " (tamano desconocido)"
    )

    for attempt in range(1, attempts + 1):
        done = destination.stat().st_size if destination.exists() else 0
        if total is not None and done >= total:
            break

        headers = {"Range": f"bytes={done}-"} if done else {}
        mode = "ab" if done else "wb"
        if done:
            logger.info("Reanudando desde %.1f MB (intento %d/%d)", done / 1e6, attempt, attempts)

        try:
            with requests.get(url, stream=True, timeout=60, headers=headers) as response:
                # 416 significa que ya estaba completo.
                if response.status_code == 416:
                    break
                response.raise_for_status()
                # Si el servidor ignora Range y responde 200, hay que reescribir
                # desde cero para no duplicar bytes.
                if done and response.status_code == 200:
                    logger.info("El servidor no acepta Range; se descarga de nuevo completo")
                    done, mode = 0, "wb"

                with destination.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        done += len(chunk)
                        if total:
                            sys.stderr.write(
                                f"\r  {100 * done / total:5.1f}%  "
                                f"{done / 1e6:6.1f} / {total / 1e6:.1f} MB"
                            )
                            sys.stderr.flush()
            sys.stderr.write("\n")

            if total is None or destination.stat().st_size >= total:
                return
            logger.warning(
                "Incompleto: %.1f de %.1f MB", destination.stat().st_size / 1e6, total / 1e6
            )
        except Exception as exc:  # noqa: BLE001 - se reintenta con lo ya descargado
            sys.stderr.write("\n")
            logger.warning("Intento %d/%d fallo: %s", attempt, attempts, exc)

        if attempt < attempts:
            time.sleep(2 * attempt)

    size = destination.stat().st_size if destination.exists() else 0
    if total is not None and size < total:
        raise SystemExit(
            f"No se pudo completar la descarga ({size / 1e6:.1f} de {total / 1e6:.1f} MB) "
            f"despues de {attempts} intentos. El archivo parcial queda en {destination}: "
            f"vuelve a correr el comando y continua desde ahi."
        )


def from_parquet(tmp_dir: Path):
    import pandas as pd

    raw = tmp_dir / "creditcard.pq"
    download(PARQUET_URL, raw)
    logger.info("Leyendo el parquet")
    return pd.read_parquet(raw)


def from_arff(tmp_dir: Path):
    import pandas as pd
    from scipy.io import arff

    raw = tmp_dir / "creditcard.arff"
    download(ARFF_URL, raw)
    logger.info("Parseando el ARFF (toma un rato, son 150 MB de texto)")
    data, _ = arff.loadarff(str(raw))
    frame = pd.DataFrame(data)
    # El ARFF trae la etiqueta como bytes: b'0' y b'1'.
    for column in frame.columns:
        if frame[column].dtype == object:
            frame[column] = frame[column].str.decode("utf-8")
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(description="Descarga el dataset de fraude desde OpenML")
    parser.add_argument("--format", choices=("parquet", "arff"), default="parquet")
    parser.add_argument("--force", action="store_true", help="Vuelve a descargar si ya existe")
    args = parser.parse_args()

    if TARGET_CSV.exists() and not args.force:
        size_mb = TARGET_CSV.stat().st_size / 1e6
        logger.info("%s ya existe (%.1f MB). Usa --force para rebajarlo.", TARGET_CSV, size_mb)
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = DATA_DIR / ".tmp"
    tmp_dir.mkdir(exist_ok=True)

    try:
        frame = from_parquet(tmp_dir) if args.format == "parquet" else from_arff(tmp_dir)
    finally:
        pass

    # La etiqueta puede venir como texto segun el formato.
    if "Class" not in frame.columns:
        raise SystemExit(f"No hay columna 'Class'. Columnas: {list(frame.columns)[:10]}")
    frame["Class"] = frame["Class"].astype(int)

    rows = len(frame)
    fraud = int(frame["Class"].sum())
    logger.info("Filas: %d | fraudes: %d (%.3f%%)", rows, fraud, 100 * fraud / rows)

    if rows != EXPECTED_ROWS or fraud != EXPECTED_FRAUD:
        logger.warning(
            "El dataset no coincide con lo esperado (%d filas, %d fraudes). "
            "Revisa que el informe declare estos numeros y no los anteriores.",
            EXPECTED_ROWS,
            EXPECTED_FRAUD,
        )

    logger.info("Escribiendo %s", TARGET_CSV)
    frame.to_csv(TARGET_CSV, index=False)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    logger.info("Listo: %.1f MB", TARGET_CSV.stat().st_size / 1e6)
    logger.info("Ahora: python -m src.training.train --data %s --label Class", TARGET_CSV)


if __name__ == "__main__":
    main()
