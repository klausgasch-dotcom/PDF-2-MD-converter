# pdf2md – PDF zu Markdown (mit OCR)

Kommandozeilen-Tool, das PDFs einliest und den Inhalt als Markdown ausgibt.

- **Gleicher Dateiname:** `Rechnung.pdf` → `Rechnung.md`
- **Automatische OCR:** Seiten ohne Textebene (eingescannte PDFs) werden erkannt und
  vorher mit [OCRmyPDF](https://ocrmypdf.readthedocs.io/) / Tesseract texterkannt.
  Seiten, die bereits Text enthalten, bleiben unverändert (auch gemischte PDFs funktionieren).
- **Markdown-Struktur:** Überschriften, Listen, Tabellen usw. über
  [PyMuPDF4LLM](https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/).
- Einzelne Dateien oder ganze Ordner (optional rekursiv).

## Installation

### 1. Systemprogramme (für OCR)

| System | Befehl |
|---|---|
| Debian/Ubuntu | `sudo apt install tesseract-ocr tesseract-ocr-deu ghostscript` |
| macOS (Homebrew) | `brew install tesseract tesseract-lang ghostscript` |
| Windows | [Tesseract-Installer (UB Mannheim)](https://github.com/UB-Mannheim/tesseract/wiki) inkl. Sprachpaket *Deutsch*, sowie [Ghostscript](https://ghostscript.com/releases/gsdnld.html); beide müssen im `PATH` liegen |

Ohne diese Programme funktionieren digitale PDFs trotzdem, nur die OCR nicht.

### 2. Python-Paket (Python ≥ 3.9)

```bash
pip install .
# oder ohne Installation:
pip install -r requirements.txt
```

## Verwendung

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

## Als Python-Bibliothek

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
pip install pytest
python -m pytest tests
```
