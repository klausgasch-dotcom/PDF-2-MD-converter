import shutil
from pathlib import Path

import pymupdf
import pytest

import pdf2md


def make_digital_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 80), "Quartalsbericht 2026", fontsize=22)
    page.insert_text((72, 120), "Dies ist ein digitales PDF mit echter Textebene.", fontsize=11)
    doc.save(path)


def make_scanned_pdf(path: Path) -> None:
    src = pymupdf.open()
    page = src.new_page()
    page.insert_text((72, 80), "Eingescanntes Dokument", fontsize=24)
    page.insert_text((72, 130), "Sehr geehrte Damen und Herren", fontsize=16)
    pix = page.get_pixmap(dpi=200)
    doc = pymupdf.open()
    scan = doc.new_page()
    scan.insert_image(scan.rect, pixmap=pix)
    doc.save(path)


def test_digital_pdf_no_ocr_needed(tmp_path):
    pdf = tmp_path / "Bericht.pdf"
    make_digital_pdf(pdf)
    assert pdf2md.pages_needing_ocr(pdf) == []
    md = pdf2md.convert(pdf)
    assert md == tmp_path / "Bericht.md"
    assert "Quartalsbericht 2026" in md.read_text(encoding="utf-8")


def test_scanned_pdf_detected(tmp_path):
    pdf = tmp_path / "scan.pdf"
    make_scanned_pdf(pdf)
    assert pdf2md.pages_needing_ocr(pdf) == [0]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract nicht installiert")
def test_scanned_pdf_ocr(tmp_path):
    pdf = tmp_path / "Scan Brief.pdf"
    make_scanned_pdf(pdf)
    out = tmp_path / "out"
    md = pdf2md.convert(pdf, out)
    assert md == out / "Scan Brief.md"
    assert "Eingescanntes Dokument" in md.read_text(encoding="utf-8")


def test_existing_file_not_overwritten(tmp_path):
    pdf = tmp_path / "a.pdf"
    make_digital_pdf(pdf)
    (tmp_path / "a.md").write_text("alt", encoding="utf-8")
    assert pdf2md.convert(pdf) is None
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "alt"
    assert pdf2md.convert(pdf, overwrite=True) is not None
    assert "Quartalsbericht" in (tmp_path / "a.md").read_text(encoding="utf-8")


def test_batch_cli_keeps_structure_and_continues(tmp_path, caplog):
    src = tmp_path / "in"
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir()
    make_digital_pdf(src / "a" / "Bericht.pdf")
    make_digital_pdf(src / "b" / "Bericht.pdf")   # gleicher Name, anderer Ordner
    make_digital_pdf(src / "Top.pdf")
    (src / "defekt.pdf").write_bytes(b"%PDF-1.7 kaputt")
    out = tmp_path / "out"

    with caplog.at_level("INFO", logger="pdf2md"):
        rc = pdf2md.main([str(src), "-r", "-o", str(out)])
    assert rc == 1  # eine Datei fehlgeschlagen
    assert (out / "a" / "Bericht.md").exists()
    assert (out / "b" / "Bericht.md").exists()
    assert (out / "Top.md").exists()
    assert "3 umgewandelt, 0 übersprungen, 1 Fehler (von 4)" in caplog.text

    # zweiter Lauf: alles übersprungen, parallel
    rc = pdf2md.main([str(src / "a"), str(src / "Top.pdf"), "-o", str(out), "-j", "2"])
    assert rc == 0


def test_collect_dedupes(tmp_path):
    make_digital_pdf(tmp_path / "x.pdf")
    pdfs = pdf2md.collect_pdfs([tmp_path, tmp_path / "x.pdf"], recursive=False)
    assert len(pdfs) == 1
