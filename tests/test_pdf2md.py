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
