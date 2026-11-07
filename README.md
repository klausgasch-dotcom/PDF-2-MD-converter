# pdf2md – PDF zu Markdown (mit OCR)

Tool, das PDFs einliest und den Inhalt als Markdown ausgibt – als **Weboberfläche/HTTP-API**
(Docker, z. B. auf einem Raspberry Pi 5 oder bei einem Webhoster) und als **Kommandozeilen-Tool**.

- **Gleicher Dateiname:** `Rechnung.pdf` → `Rechnung.md`
- **Automatische OCR:** Seiten ohne Textebene (eingescannte PDFs) werden erkannt und
  vorher mit [OCRmyPDF](https://ocrmypdf.readthedocs.io/) / Tesseract texterkannt.
  Seiten, die bereits Text enthalten, bleiben unverändert (auch gemischte PDFs funktionieren).
- **Markdown-Struktur:** Überschriften, Listen, Tabellen usw. über
  [PyMuPDF4LLM](https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/).
- Einzelne Dateien oder ganze Ordner (optional rekursiv).

## Web-Version (Docker)

Die Weboberfläche (`app.py`) bietet Upload per Drag & Drop. Eine PDF-Datei liefert eine
gleichnamige `.md`-Datei zurück, mehrere Dateien eine ZIP-Datei mit gleichnamigen `.md`-Dateien.

Das Docker-Image ist Multi-Arch (**linux/amd64** und **linux/arm64**) – dasselbe Image läuft
beim Webhoster und auf dem **Raspberry Pi 5** (64-Bit Raspberry Pi OS).

### Starten

```bash
cp .env.example .env        # Benutzer/Passwort eintragen!
docker compose up -d        # -> http://<host>:8000
```

Ohne Compose:

```bash
docker run -d -p 8000:8000 -e PDF2MD_USER=admin -e PDF2MD_PASSWORD=geheim \
  --restart unless-stopped ghcr.io/klausgasch-dotcom/pdf-2-md-converter:latest
```

Das Image wird von GitHub Actions (`.github/workflows/docker.yml`) bei jedem Push auf `main`
für beide Architekturen gebaut und nach `ghcr.io` veröffentlicht. Ist das Paket auf GitHub
privat, vorher `docker login ghcr.io` (mit einem Token mit `read:packages`) ausführen –
oder das Paket in den GitHub-Paketeinstellungen auf *public* stellen. Alternativ direkt auf
dem Zielrechner bauen: `docker compose up -d --build`.

### Konfiguration (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `PDF2MD_USER`, `PDF2MD_PASSWORD` | leer | Aktiviert HTTP-Basic-Auth. **Bei öffentlichem Hosting unbedingt setzen.** |
| `PDF2MD_MAX_UPLOAD_MB` | `50` | Maximale Größe pro PDF |
| `PDF2MD_MAX_FILES` | `20` | Maximale Anzahl Dateien pro Anfrage |
| `PDF2MD_MAX_PARALLEL` | `1` | Gleichzeitige Konvertierungen (OCR ist CPU-intensiv; Pi 5: 1–2) |
| `PDF2MD_DEFAULT_LANG` | `deu+eng` | Vorausgewählte OCR-Sprache |
| `PORT` | `8000` | Port im Container (wird von vielen Hostern automatisch gesetzt) |

`/healthz` liefert den Status für Load-Balancer/Monitoring (ohne Login).

### Hosting im Web (Übergangsphase)

Geeignet ist jeder Anbieter, der **Docker-Container** ausführt, z. B. ein kleiner VPS
(Hetzner, netcup, …) mit `docker compose`, oder Container-Plattformen wie Render,
Railway, Fly.io oder Google Cloud Run (Dockerfile im Repo angeben, `PORT` wird übernommen).
Reine Serverless-/Static-Hoster (z. B. Vercel, Netlify) eignen sich **nicht**, da Tesseract
und Ghostscript als Systemprogramme benötigt werden und OCR länger dauern kann als deren
Zeitlimits. Empfehlung: mindestens 1 GB RAM; HTTPS über den Hoster oder einen
Reverse-Proxy (Caddy, Traefik, nginx) davor. Bei einem Reverse-Proxy das Upload-Limit
anpassen (nginx: `client_max_body_size 50m;`) und Timeouts großzügig wählen (OCR großer
Scans kann Minuten dauern).

### Umzug auf den Raspberry Pi 5

1. Raspberry Pi OS **64-Bit** installieren, Docker installieren: `curl -fsSL https://get.docker.com | sh`
2. `docker-compose.yml` und `.env` auf den Pi kopieren
3. `docker compose up -d` – es wird automatisch das arm64-Image gezogen

Hinweise: Das Image ist ca. 1,5 GB groß (OCR- und Layout-Erkennung). Der Pi 5 schafft
OCR gut, braucht für eine gescannte Seite aber einige Sekunden; `PDF2MD_MAX_PARALLEL=1`
belassen. Für Zugriff von außen einen Reverse-Proxy mit HTTPS oder einen Tunnel
(z. B. Cloudflare Tunnel, Tailscale) verwenden, statt Port 8000 direkt freizugeben.

### HTTP-API

```bash
curl -u admin:geheim -F "files=@Brief.pdf" -OJ http://localhost:8000/api/convert          # -> Brief.md
curl -u admin:geheim -F "files=@a.pdf" -F "files=@b.pdf" -F lang=deu -F ocr=auto \
     -OJ http://localhost:8000/api/convert                                                # -> markdown.zip
```

Parameter: `files` (eine oder mehrere PDFs), `lang` (z. B. `deu+eng`), `ocr` (`auto` | `force` | `off`).
Interaktive API-Doku: `/api/docs`.

### Lokal ohne Docker starten

```bash
pip install -r requirements-web.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Kommandozeilen-Tool

### Installation

#### 1. Systemprogramme (für OCR)

| System | Befehl |
|---|---|
| Debian/Ubuntu | `sudo apt install tesseract-ocr tesseract-ocr-deu ghostscript` |
| macOS (Homebrew) | `brew install tesseract tesseract-lang ghostscript` |
| Windows | [Tesseract-Installer (UB Mannheim)](https://github.com/UB-Mannheim/tesseract/wiki) inkl. Sprachpaket *Deutsch*, sowie [Ghostscript](https://ghostscript.com/releases/gsdnld.html); beide müssen im `PATH` liegen |

Ohne diese Programme funktionieren digitale PDFs trotzdem, nur die OCR nicht.

#### 2. Python-Paket (Python ≥ 3.9)

```bash
pip install .
# oder ohne Installation:
pip install -r requirements.txt
```

### Verwendung

```bash
pdf2md Dokument.pdf                 # erzeugt Dokument.md neben dem PDF
pdf2md *.pdf -o markdown/           # alle PDFs, Ausgabe in Ordner markdown/
pdf2md Scans/ -r                    # Ordner rekursiv verarbeiten
pdf2md Scan.pdf --force-ocr -f      # OCR erzwingen, vorhandene .md überschreiben
pdf2md Report.pdf -l eng            # OCR-Sprache Englisch
```

Ohne Installation: `python pdf2md.py …`

| Option | Bedeutung |
|---|---|
| `-o, --output-dir DIR` | Zielordner (Standard: neben der jeweiligen PDF-Datei) |
| `-l, --lang LANGS` | Tesseract-Sprachen, z. B. `deu`, `eng`, `deu+eng` (Standard) |
| `--force-ocr` | OCR auf allen Seiten, auch wenn Text vorhanden ist (z. B. bei schlechter vorhandener Textebene) |
| `--no-ocr` | Nie OCR ausführen |
| `-r, --recursive` | Unterordner mit durchsuchen |
| `-f, --overwrite` | Vorhandene `.md`-Dateien überschreiben (sonst werden sie übersprungen) |
| `-q, --quiet` | Nur Fehler ausgeben |

Der Exit-Code ist `1`, wenn mindestens eine Datei fehlgeschlagen ist.

### Als Python-Bibliothek

```python
from pathlib import Path
import pdf2md

pdf2md.convert(Path("Scan.pdf"), language="deu", ocr="auto")  # -> Path("Scan.md")
```

## Wie wird „eingescannt“ erkannt?

Eine Seite gilt als eingescannt, wenn sie weniger als 25 extrahierbare Zeichen
enthält, aber Bilder oder Grafiken. Ist mindestens eine solche Seite vorhanden,
läuft OCRmyPDF mit `--skip-text` (nur diese Seiten werden erkannt). Danach wird
das PDF mit Textebene nach Markdown konvertiert. Das Original-PDF wird nicht verändert.

## Tests

```bash
pip install -r requirements-web.txt pytest httpx
python -m pytest tests
```
