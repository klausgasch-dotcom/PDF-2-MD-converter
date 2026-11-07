import os
import shutil
import threading
import time

import pytest

import hotfolder
from test_pdf2md import make_digital_pdf, make_scanned_pdf


def age(path, seconds=60):
    """Datei 'älter' machen, damit die Wartezeit (settle) erfüllt ist."""
    t = time.time() - seconds
    os.utime(path, (t, t))


@pytest.fixture
def dirs(tmp_path):
    d = {k: tmp_path / k for k in ("eingang", "ausgang", "archiv", "fehler")}
    for p in d.values():
        p.mkdir()
    return d


def make_hf(dirs, **kw):
    return hotfolder.HotFolder(
        dirs["eingang"], dirs["ausgang"], done_dir=dirs["archiv"], failed_dir=dirs["fehler"],
        settle=0, **kw,
    )


def test_processes_after_file_is_stable(dirs):
    pdf = dirs["eingang"] / "Rechnung 1.pdf"
    make_digital_pdf(pdf)
    hf = make_hf(dirs)
    assert hf.scan_once() == 0          # erster Blick: Datei könnte noch kopiert werden
    assert hf.scan_once() == 1          # unverändert -> verarbeiten
    assert "Quartalsbericht" in (dirs["ausgang"] / "Rechnung 1.md").read_text(encoding="utf-8")
    assert not pdf.exists()
    assert (dirs["archiv"] / "Rechnung 1.pdf").exists()


def test_growing_file_is_not_processed(dirs):
    pdf = dirs["eingang"] / "gross.pdf"
    make_digital_pdf(pdf)
    hf = make_hf(dirs)
    hf.scan_once()
    with pdf.open("ab") as fh:          # Datei wird noch geschrieben
        fh.write(b"\n%")
    assert hf.scan_once() == 0


def test_failed_file_moved_with_reason(dirs):
    bad = dirs["eingang"] / "kaputt.pdf"
    bad.write_bytes(b"%PDF-1.4 kaputt")
    hf = make_hf(dirs)
    hf.scan_once(wait_until_stable=False)
    assert (dirs["fehler"] / "kaputt.pdf").exists()
    assert "Fehler" in (dirs["fehler"] / "kaputt.fehler.txt").read_text(encoding="utf-8")


def test_failed_without_failed_dir_not_retried(dirs, monkeypatch):
    bad = dirs["eingang"] / "kaputt.pdf"
    bad.write_bytes(b"%PDF-1.4 kaputt")
    age(bad)
    hf = hotfolder.HotFolder(dirs["eingang"], dirs["ausgang"], done_dir=dirs["archiv"], settle=0)
    assert hf.scan_once(wait_until_stable=False) == 1
    assert (dirs["ausgang"] / "kaputt.fehler.txt").exists()
    assert hf.scan_once(wait_until_stable=False) == 0   # nicht endlos wiederholen


def test_keep_mode_and_subfolders(dirs):
    sub = dirs["eingang"] / "2026" / "Mai"
    sub.mkdir(parents=True)
    make_digital_pdf(sub / "Brief.pdf")
    age(sub / "Brief.pdf")
    hf = hotfolder.HotFolder(dirs["eingang"], dirs["ausgang"], recursive=True, settle=0)
    assert hf.scan_once(wait_until_stable=False) == 1
    assert (dirs["ausgang"] / "2026" / "Mai" / "Brief.md").exists()
    assert (sub / "Brief.pdf").exists()                  # PDF bleibt liegen
    assert hf.scan_once(wait_until_stable=False) == 0    # nicht doppelt
    make_digital_pdf(sub / "Brief.pdf")                  # PDF geändert -> neu umwandeln
    later = time.time() + 5
    os.utime(sub / "Brief.pdf", (later, later))
    assert hf.scan_once(wait_until_stable=False) == 1


def test_ignores_temp_files_and_archive_inside_input(dirs):
    inbox = dirs["eingang"]
    make_digital_pdf(inbox / "~$tmp.pdf")
    make_digital_pdf(inbox / ".versteckt.pdf")
    (inbox / "_archiv").mkdir()
    make_digital_pdf(inbox / "_archiv" / "alt.pdf")
    hf = hotfolder.HotFolder(inbox, inbox, done_dir=inbox / "_archiv", recursive=True, settle=0)
    assert hf.scan_once(wait_until_stable=False) == 0


def test_name_collision_in_archive(dirs):
    make_digital_pdf(dirs["archiv"] / "a.pdf")
    make_digital_pdf(dirs["eingang"] / "a.pdf")
    make_hf(dirs).scan_once(wait_until_stable=False)
    assert (dirs["archiv"] / "a (2).pdf").exists()


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract nicht installiert")
def test_run_loop_with_scan_and_stop(dirs):
    hf = hotfolder.HotFolder(dirs["eingang"], dirs["ausgang"], done_dir=dirs["archiv"], settle=0.2)
    t = threading.Thread(target=hf.run, kwargs={"interval": 0.3})
    t.start()
    try:
        make_scanned_pdf(dirs["eingang"] / "Scan.pdf")
        md = dirs["ausgang"] / "Scan.md"
        end = time.time() + 60
        while not md.exists() and time.time() < end:
            time.sleep(0.2)
        assert "Eingescanntes Dokument" in md.read_text(encoding="utf-8")
    finally:
        hf.stop()
        t.join(10)
    assert not t.is_alive()


def test_cli_once(dirs):
    make_digital_pdf(dirs["eingang"] / "x.pdf")
    import pdf2md
    rc = pdf2md.main(["watch", str(dirs["eingang"]), "-o", str(dirs["ausgang"]), "--delete", "--once"])
    assert rc == 0
    assert (dirs["ausgang"] / "x.md").exists()
    assert not (dirs["eingang"] / "x.pdf").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignoriert Dateirechte")
def test_readonly_input_no_duplicates_and_others_processed(dirs):
    locked = dirs["eingang"] / "gesperrt"
    locked.mkdir()
    make_digital_pdf(locked / "a.pdf")
    make_digital_pdf(dirs["eingang"] / "b.pdf")
    locked.chmod(0o555)  # PDF darf nicht entfernt werden
    try:
        hf = make_hf(dirs, recursive=True)
        hf.scan_once(wait_until_stable=False)
        hf.scan_once(wait_until_stable=False)
        hf.scan_once(wait_until_stable=False)
    finally:
        locked.chmod(0o755)
    assert (dirs["ausgang"] / "gesperrt" / "a.md").exists()
    assert (dirs["ausgang"] / "b.md").exists()
    archived = sorted(p.name for p in dirs["archiv"].rglob("*.pdf"))
    assert archived == ["b.pdf"]   # keine Kopien von a.pdf
    assert (locked / "a.pdf").exists()


def test_move_failure_blocks_reprocessing(dirs, monkeypatch):
    make_digital_pdf(dirs["eingang"] / "a.pdf")
    hf = make_hf(dirs)
    calls = []

    def failing_move(pdf, base):
        calls.append(pdf)
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(hf, "_move", failing_move)
    assert hf.scan_once(wait_until_stable=False) == 1
    assert hf.scan_once(wait_until_stable=False) == 0
    assert len(calls) == 1
    assert list(dirs["archiv"].iterdir()) == []
