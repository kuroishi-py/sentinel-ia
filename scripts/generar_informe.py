"""Genera el PDF del informe tecnico a partir de docs/informe/EP3_informe_tecnico.md.

El markdown es la fuente; el PDF se regenera cada vez que cambian las cifras o
alguien completa una seccion. Usa Chrome, Chromium o Edge en modo headless (por
el protocolo DevTools) para imprimir con numero de pagina.

La portada lleva datos del equipo que no se versionan porque el repositorio es
publico. Si existe docs/informe/portada.local.json, cada par "texto": "valor"
reemplaza ese texto en el informe. Ejemplo:

    {
      "[COMPLETAR: integrantes]": "Nombre Apellido, ...",
      "[COMPLETAR: docente y seccion]": "Nombre del docente · seccion 001D",
      "[COMPLETAR: fecha de entrega]": "4 de diciembre de 2026"
    }

Uso:
    pip install markdown websockets          # ya vienen en requirements-dev.txt
    python scripts/generar_informe.py [--salida ruta.pdf] [--navegador ruta]

Por omision deja docs/informe/EP3_Equipo7_Martinez_Farenden_PenayLillo_Pavon.pdf
(los PDF no se versionan) e imprime cuantas paginas tiene y cuantos
[COMPLETAR] quedan.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
FUENTE = ROOT / "docs" / "informe" / "EP3_informe_tecnico.md"
PORTADA = ROOT / "docs" / "informe" / "portada.local.json"
SALIDA = ROOT / "docs" / "informe" / "EP3_Equipo7_Martinez_Farenden_PenayLillo_Pavon.pdf"

NAVEGADORES = [
    os.environ.get("NAVEGADOR", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
]

CSS = """
@page { size: A4; margin: 16mm 15mm 16mm 15mm; }
body { font-family: Arial, Helvetica, sans-serif; font-size: 9.4pt; line-height: 1.36; color: #1b1b1b; }
h1 { font-size: 17pt; margin: 0 0 4px 0; }
h1 + p { margin-top: 0; color: #444; }
h2 { font-size: 12.5pt; margin: 14px 0 5px 0; border-bottom: 1px solid #bbb; padding-bottom: 2px; page-break-after: avoid; }
h3 { font-size: 10.5pt; margin: 10px 0 4px 0; page-break-after: avoid; }
p { margin: 4px 0 6px 0; text-align: justify; }
ul, ol { margin: 3px 0 6px 0; padding-left: 20px; }
li { margin: 1px 0; }
table { border-collapse: collapse; width: 100%; margin: 5px 0 8px 0; font-size: 8.6pt; page-break-inside: avoid; }
th, td { border: 1px solid #c8c8c8; padding: 2.5px 5px; vertical-align: top; text-align: left; }
th { background: #eef1f3; }
code { font-family: Consolas, "Courier New", monospace; font-size: 8.6pt; background: #f3f3f3; padding: 0 2px; }
figure { margin: 6px 0 10px 0; text-align: center; page-break-inside: avoid; }
figure img { max-width: 100%; max-height: 92mm; border: 1px solid #ddd; }
figure.pagina img { max-height: 236mm; border: none; }
figcaption { font-size: 8.4pt; color: #444; margin-top: 3px; font-style: italic; }
.completar { background: #fff3cd; }
.referencias p { padding-left: 24px; text-indent: -24px; text-align: left; }
"""


def find_browser(explicit: str | None) -> str:
    for candidate in [explicit or "", *NAVEGADORES]:
        if not candidate:
            continue
        path = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if path:
            return path
    raise SystemExit("No encontre Chrome, Chromium ni Edge. Indica uno con --navegador o NAVEGADOR=...")


LIST_ITEM = re.compile(r"^(\s*)([-*]|\d+\.)\s")


def blank_before_lists(text: str) -> str:
    """Python-Markdown necesita una linea en blanco antes de una lista; GitHub no."""
    out: list[str] = []
    for line in text.splitlines():
        prev = out[-1] if out else ""
        if LIST_ITEM.match(line) and prev.strip() and not LIST_ITEM.match(prev) and not prev.startswith("|"):
            out.append("")
        out.append(line)
    return "\n".join(out)


def to_html(text: str) -> str:
    # Comentarios de autor: no van al PDF.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = blank_before_lists(text)
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    # Imagenes como figuras con su leyenda.
    body = re.sub(
        r'<p><img alt="([^"]*)" src="([^"]+)" ?/?></p>',
        # Las imagenes *_rot.png son diagramas girados que ocupan una pagina.
        lambda m: (
            f'<figure{" class=pagina" if m.group(2).endswith("_rot.png") else ""}>'
            f'<img src="{m.group(2)}" alt=""><figcaption>{m.group(1)}</figcaption></figure>'
        ),
        body,
    )
    # Lo que falta completar se ve resaltado.
    body = re.sub(r"(\[COMPLETAR:[^\]]*\])", r'<span class="completar">\1</span>', body)
    body = body.replace("<h2>Referencias</h2>", '<h2>Referencias</h2><div class="referencias">', 1)
    if '<div class="referencias">' in body:
        body = body.replace("<h2>Declaración de uso de IA generativa</h2>", "</div><h2>Declaración de uso de IA generativa</h2>", 1)
    return f"<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'><title>Informe EP3</title><style>{CSS}</style></head><body>{body}</body></html>"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def print_pdf(browser: str, page: Path, out: Path) -> bytes:
    from websockets.sync.client import connect

    port = free_port()
    profile = tempfile.mkdtemp(prefix="informe-")
    proc = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu", f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(100):
            try:
                targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1).read())
                target = next(t for t in targets if t.get("type") == "page")
                break
            except Exception:  # noqa: BLE001 - el navegador todavia arranca
                time.sleep(0.2)
        if target is None:
            raise SystemExit("El navegador no abrio el puerto de depuracion")
        with connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
            counter = 0

            def call(method: str, **params):
                nonlocal counter
                counter += 1
                ws.send(json.dumps({"id": counter, "method": method, "params": params}))
                while True:
                    msg = json.loads(ws.recv())
                    if msg.get("id") == counter:
                        if "error" in msg:
                            raise SystemExit(f"{method}: {msg['error']}")
                        return msg.get("result", {})

            call("Page.enable")
            call("Page.navigate", url=page.as_uri())
            time.sleep(2.5)
            footer = (
                "<div style='font-size:7.5px;width:100%;text-align:center;color:#666;font-family:Arial'>"
                "SENTINEL-IA · EP3 ITY1102 · <span class='pageNumber'></span> / <span class='totalPages'></span></div>"
            )
            result = call(
                "Page.printToPDF",
                printBackground=True,
                preferCSSPageSize=True,
                displayHeaderFooter=True,
                headerTemplate="<span></span>",
                footerTemplate=footer,
            )
        data = base64.b64decode(result["data"])
        out.write_bytes(data)
        return data
    finally:
        proc.terminate()
        shutil.rmtree(profile, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="PDF del informe tecnico de la EP3")
    parser.add_argument("--salida", default=str(SALIDA))
    parser.add_argument("--navegador", default=None)
    args = parser.parse_args()

    text = FUENTE.read_text(encoding="utf-8")
    if PORTADA.exists():
        for key, value in json.loads(PORTADA.read_text(encoding="utf-8")).items():
            text = text.replace(key, html.escape(value))

    # El HTML se escribe junto al markdown para que las rutas img/... resuelvan.
    page = FUENTE.with_suffix(".tmp.html")
    page.write_text(to_html(text), encoding="utf-8")
    out = Path(args.salida).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = print_pdf(find_browser(args.navegador), page, out)
    finally:
        page.unlink(missing_ok=True)

    pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
    pending = len(re.findall(r"\[COMPLETAR:", re.sub(r"<!--.*?-->", "", text, flags=re.S)))
    print(f"{out} · {pages} paginas · {pending} [COMPLETAR] pendientes")


if __name__ == "__main__":
    main()
