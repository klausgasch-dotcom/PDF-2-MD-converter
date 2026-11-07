#!/usr/bin/env python3
"""pdf2md - PDFs in Markdown umwandeln, mit automatischer OCR für eingescannte Seiten.

Für jede PDF-Datei wird eine gleichnamige Markdown-Datei erzeugt
(``Rechnung.pdf`` -> ``Rechnung.md``). Seiten ohne Textebene (Scans) werden
vorher per OCR (OCRmyPDF/Tesseract) mit einer Textebene versehen.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

import pymupdf
import pymupdf4llm

log = logging.getLogger("pdf2md")

# Seiten mit weniger extrahierbaren Zeichen gelten als "eingescannt".
MIN_TEXT_CHARS = 25


def pages_needing_ocr(pdf_path: Path, min_chars: int = MIN_TEXT_CHARS) -> list[int]:
    """Liefert die (0-basierten) Seitennummern, die keine nutzbare Textebene haben."""
    result = []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            text = page.get_text("text").strip()
            if len(text) >= min_chars:
                continue
            # Leere Seiten ohne Bilder/Zeichnungen brauchen keine OCR.
            if page.get_images(full=False) or page.get_drawings():
                result.append(page.number)
    return result


def run_ocr(src: Path, dst: Path, language: str, force: bool) -> None:
    """Erzeugt mit OCRmyPDF eine Kopie von ``src`` mit Textebene in ``dst``."""
    try:
        import ocrmypdf
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "OCR benötigt das Paket 'ocrmypdf' sowie Tesseract und Ghostscript."
        ) from exc

    ocrmypdf.ocr(
        src,
        dst,
        language=language.split("+"),
        force_ocr=force,      # alle Seiten rastern und neu erkennen
        skip_text=not force,  # nur Seiten ohne Text erkennen
        rotate_pages=True,
        deskew=force,         # deskew ist mit skip_text nicht kombinierbar
        output_type="pdf",    # kein PDF/A nötig, nur Zwischenprodukt
        optimize=0,
        progress_bar=False,
        invalidate_digital_signatures=True,
    )


def pdf_to_markdown(pdf_path: Path) -> str:
    """Konvertiert ein PDF (mit Textebene) in Markdown."""
    with pymupdf.open(pdf_path) as doc:
        return pymupdf4llm.to_markdown(doc, use_ocr=False, show_progress=False)


def convert(
    pdf_path: Path,
    out_dir: Path | None = None,
    *,
    language: str = "deu+eng",
    ocr: str = "auto",
    overwrite: bool = False,
) -> Path | None:
    """Wandelt eine PDF-Datei um und gibt den Pfad der Markdown-Datei zurück.

    ``ocr``: ``"auto"`` (nur wenn nötig), ``"force"`` (immer) oder ``"off"`` (nie).
    """
    target_dir = out_dir if out_dir is not None else pdf_path.parent
    md_path = target_dir / f"{pdf_path.stem}.md"
    if md_path.exists() and not overwrite:
        log.warning("Übersprungen (existiert bereits): %s", md_path)
        return None

    ocr_pages: list[int] = []
    if ocr == "auto":
        ocr_pages = pages_needing_ocr(pdf_path)
    do_ocr = ocr == "force" or bool(ocr_pages)

    with tempfile.TemporaryDirectory(prefix="pdf2md-") as tmp:
        source = pdf_path
        if do_ocr:
            if ocr == "force":
                log.info("OCR (erzwungen): %s", pdf_path.name)
            else:
                log.info(
                    "OCR für %d Seite(n) ohne Text: %s",
                    len(ocr_pages),
                    pdf_path.name,
                )
            source = Path(tmp) / "ocr.pdf"
            run_ocr(pdf_path, source, language, force=(ocr == "force"))
        markdown = pdf_to_markdown(source)

    target_dir.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown, encoding="utf-8")
    log.info("Geschrieben: %s", md_path)
    return md_path


def collect_pdfs(inputs: list[Path], recursive: bool) -> list[Path]:
    pdfs: list[Path] = []
    for item in inputs:
        if item.is_dir():
            pattern = "**/*" if recursive else "*"
            pdfs.extend(
                p for p in sorted(item.glob(pattern))
                if p.is_file() and p.suffix.lower() == ".pdf"
            )
        elif item.is_file():
            pdfs.append(item)
        else:
            log.error("Nicht gefunden: %s", item)
    return pdfs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pdf2md",
        description="Konvertiert PDFs in Markdown (mit OCR für eingescannte PDFs). "
        "Die .md-Datei erhält denselben Namen wie das PDF.",
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="PDF-Dateien oder Ordner")
    parser.add_argument(
        "-o", "--output-dir", type=Path,
        help="Zielordner (Standard: neben der jeweiligen PDF-Datei)",
    )
    parser.add_argument(
        "-l", "--lang", default="deu+eng",
        help="Tesseract-Sprachen für OCR, z. B. 'deu', 'eng', 'deu+eng' (Standard)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--force-ocr", action="store_true", help="OCR immer ausführen")
    group.add_argument("--no-ocr", action="store_true", help="OCR nie ausführen")
    parser.add_argument("-r", "--recursive", action="store_true", help="Ordner rekursiv durchsuchen")
    parser.add_argument("-f", "--overwrite", action="store_true", help="Vorhandene .md-Dateien überschreiben")
    parser.add_argument("-q", "--quiet", action="store_true", help="Nur Fehler ausgeben")
    args = parser.parse_args(argv)

    # Fremdbibliotheken (ocrmypdf, pikepdf, ...) nur Fehler ausgeben lassen,
    # eigene Meldungen je nach --quiet.
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
    log.setLevel(logging.ERROR if args.quiet else logging.INFO)

    ocr_mode = "force" if args.force_ocr else "off" if args.no_ocr else "auto"
    pdfs = collect_pdfs(args.inputs, args.recursive)
    if not pdfs:
        log.error("Keine PDF-Dateien gefunden.")
        return 1

    failures = 0
    for pdf in pdfs:
        try:
            convert(pdf, args.output_dir, language=args.lang, ocr=ocr_mode, overwrite=args.overwrite)
        except Exception as exc:  # noqa: BLE001 - Batch soll weiterlaufen
            failures += 1
            log.error("Fehler bei %s: %s", pdf, exc)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
