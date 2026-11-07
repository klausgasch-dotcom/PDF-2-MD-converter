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
    log.debug("Geschrieben: %s", md_path)
    return md_path


def collect_pdfs(inputs: list[Path], recursive: bool) -> list[tuple[Path, Path]]:
    """Sammelt PDFs als (Datei, relativer Unterordner)-Paare.

    Der Unterordner (relativ zum angegebenen Eingabeordner) wird bei ``-o``
    nachgebildet, damit gleichnamige PDFs aus verschiedenen Ordnern sich nicht
    gegenseitig überschreiben.
    """
    pdfs: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for item in inputs:
        if item.is_dir():
            pattern = "**/*" if recursive else "*"
            found = [
                (p, p.parent.relative_to(item))
                for p in sorted(item.glob(pattern))
                if p.is_file() and p.suffix.lower() == ".pdf"
            ]
        elif item.is_file():
            found = [(item, Path())]
        else:
            log.error("Nicht gefunden: %s", item)
            continue
        for pdf, rel in found:
            key = pdf.resolve()
            if key not in seen:
                seen.add(key)
                pdfs.append((pdf, rel))
    return pdfs


def _convert_job(pdf: Path, out_dir: Path | None, language: str, ocr: str, overwrite: bool):
    """Wrapper für den Batch: liefert (Status, Ergebnis/Fehlermeldung)."""
    try:
        md = convert(pdf, out_dir, language=language, ocr=ocr, overwrite=overwrite)
    except Exception as exc:  # noqa: BLE001 - Batch soll weiterlaufen
        return "fehler", f"{type(exc).__name__}: {exc}"
    return ("ok", md) if md is not None else ("übersprungen", None)


def _init_worker(level: int) -> None:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
    log.setLevel(level)


def run_batch(
    pdfs: list[tuple[Path, Path]],
    out_dir: Path | None,
    *,
    language: str = "deu+eng",
    ocr: str = "auto",
    overwrite: bool = False,
    jobs: int = 1,
) -> dict[str, int]:
    """Verarbeitet viele PDFs; Fehler einzelner Dateien brechen den Batch nicht ab."""
    total = len(pdfs)
    counts = {"ok": 0, "übersprungen": 0, "fehler": 0}
    failed: list[tuple[Path, str]] = []

    def target(rel: Path) -> Path | None:
        return out_dir / rel if out_dir is not None else None

    def report(n: int, pdf: Path, status: str, detail) -> None:
        counts[status] += 1
        if status == "fehler":
            failed.append((pdf, detail))
            log.error("[%d/%d] Fehler bei %s: %s", n, total, pdf, detail)
        elif status == "ok":
            log.info("[%d/%d] %s -> %s", n, total, pdf, detail)

    if jobs <= 1 or total <= 1:
        for n, (pdf, rel) in enumerate(pdfs, 1):
            report(n, pdf, *_convert_job(pdf, target(rel), language, ocr, overwrite))
    else:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor, as_completed

        with ProcessPoolExecutor(
            max_workers=jobs,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_init_worker,
            initargs=(log.level,),
        ) as pool:
            futures = {
                pool.submit(_convert_job, pdf, target(rel), language, ocr, overwrite): pdf
                for pdf, rel in pdfs
            }
            for n, fut in enumerate(as_completed(futures), 1):
                report(n, futures[fut], *fut.result())

    if total > 1:
        log.info(
            "Fertig: %d umgewandelt, %d übersprungen, %d Fehler (von %d)",
            counts["ok"], counts["übersprungen"], counts["fehler"], total,
        )
        for pdf, msg in failed:
            log.info("  Fehlgeschlagen: %s (%s)", pdf, msg)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pdf2md",
        description="Konvertiert PDFs in Markdown (mit OCR für eingescannte PDFs). "
        "Die .md-Datei erhält denselben Namen wie das PDF. Mehrere Dateien und "
        "Ordner werden im Batch verarbeitet.",
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="PDF-Dateien und/oder Ordner")
    parser.add_argument(
        "-o", "--output-dir", type=Path,
        help="Zielordner (Standard: neben der jeweiligen PDF-Datei). "
        "Unterordner werden nachgebildet.",
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
    parser.add_argument(
        "-j", "--jobs", type=int, default=1,
        help="Anzahl Dateien, die parallel verarbeitet werden (Standard 1)",
    )
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

    counts = run_batch(
        pdfs, args.output_dir, language=args.lang, ocr=ocr_mode,
        overwrite=args.overwrite, jobs=max(1, args.jobs),
    )
    return 1 if counts["fehler"] else 0


if __name__ == "__main__":
    sys.exit(main())
