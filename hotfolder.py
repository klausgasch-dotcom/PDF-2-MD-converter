"""Hot Folder für pdf2md: überwacht einen Ordner und wandelt neue PDFs automatisch um.

    pdf2md watch EINGANG [-o AUSGANG] [--done-dir ARCHIV] [--failed-dir FEHLER] [-r]

Ablauf je PDF:
  1. Warten, bis die Datei fertig kopiert ist (Größe/Änderungszeit stabil).
  2. Umwandeln -> ``AUSGANG/<Unterordner>/<Name>.md`` (ohne -o: neben dem PDF).
  3. Erfolg: PDF nach ARCHIV verschieben (oder mit --delete löschen, oder liegen lassen).
     Fehler:  PDF nach FEHLER verschieben + ``<Name>.fehler.txt`` mit Begründung
              (ohne --failed-dir bleibt das PDF liegen, die .fehler.txt landet im Ausgang).

Es wird per Polling abgefragt statt über Dateisystem-Events, weil Events bei
Netzwerkfreigaben (SMB/NFS) und manchen Docker-Mounts nicht zuverlässig ankommen.
Liegengelassene PDFs werden nicht erneut verarbeitet, solange die .md (bzw.
.fehler.txt) neuer ist als das PDF; ein geändertes PDF wird neu umgewandelt.
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import shutil
import signal
import time
from datetime import datetime
from pathlib import Path

import pdf2md

log = logging.getLogger("pdf2md.watch")

IGNORED_PREFIXES = (".", "~$", "._")


class HotFolder:
    def __init__(
        self,
        in_dir: Path,
        out_dir: Path | None = None,
        *,
        done_dir: Path | None = None,
        failed_dir: Path | None = None,
        delete: bool = False,
        recursive: bool = False,
        language: str = "deu+eng",
        ocr: str = "auto",
        settle: float = 5.0,
    ) -> None:
        self.in_dir = in_dir
        self.out_dir = out_dir
        self.done_dir = done_dir
        self.failed_dir = failed_dir
        self.delete = delete
        self.recursive = recursive
        self.language = language
        self.ocr = ocr
        self.settle = settle
        self._seen: dict[Path, tuple[int, int]] = {}
        # PDFs, die umgewandelt wurden, aber nicht verschoben/gelöscht werden konnten
        # (z. B. fehlende Schreibrechte) -> nicht erneut verarbeiten, solange unverändert
        self._blocked: dict[Path, tuple[int, int]] = {}
        self._stop = False
        # Ziel-/Archivordner innerhalb des Eingangs nicht erneut einlesen
        self._excluded = [
            d.resolve() for d in (out_dir, done_dir, failed_dir)
            if d is not None and d.resolve() != in_dir.resolve()
        ]

    # ------------------------------------------------------------------ Hilfen
    def stop(self, *_args) -> None:
        if not self._stop:
            log.info("Beende nach der aktuellen Datei …")
        self._stop = True

    def _target_dir(self, pdf: Path) -> Path:
        rel = pdf.parent.relative_to(self.in_dir)
        return (self.out_dir / rel) if self.out_dir is not None else pdf.parent

    def _is_excluded(self, path: Path) -> bool:
        resolved = path.resolve()
        return any(resolved == d or d in resolved.parents for d in self._excluded)

    def _candidates(self) -> list[Path]:
        pattern = "**/*" if self.recursive else "*"
        result = []
        for p in sorted(self.in_dir.glob(pattern)):
            if (
                p.is_file()
                and p.suffix.lower() == ".pdf"
                and not p.name.startswith(IGNORED_PREFIXES)
                and not self._is_excluded(p)
            ):
                result.append(p)
        return result

    def _is_ready(self, pdf: Path, now: float) -> bool:
        """Erst verarbeiten, wenn die Datei zwischen zwei Durchläufen unverändert war."""
        try:
            st = pdf.stat()
        except FileNotFoundError:
            self._seen.pop(pdf, None)
            return False
        sig = (st.st_size, st.st_mtime_ns)
        previous = self._seen.get(pdf)
        self._seen[pdf] = sig
        # ctime ändert sich bei jedem Schreiben, auch wenn das Kopierprogramm mtime erhält
        changed = max(st.st_mtime, st.st_ctime)
        return previous == sig and st.st_size > 0 and now - changed >= self.settle

    @property
    def keeps_pdfs(self) -> bool:
        return self.done_dir is None and not self.delete

    def _already_done(self, pdf: Path) -> bool:
        """Wurde dieses PDF (in dieser Version) schon verarbeitet?

        - .fehler.txt neuer als das PDF -> nicht endlos erneut versuchen
        - PDFs bleiben liegen und .md ist neuer -> schon erledigt
        """
        target = self._target_dir(pdf)
        st = pdf.stat()
        changed = max(st.st_mtime, st.st_ctime)
        markers = [target / f"{pdf.stem}.fehler.txt"]
        if self.keeps_pdfs:
            markers.append(target / f"{pdf.stem}.md")
        return any(m.exists() and m.stat().st_mtime >= changed for m in markers)

    @staticmethod
    def _signature(pdf: Path) -> tuple[int, int] | None:
        try:
            st = pdf.stat()
        except FileNotFoundError:
            return None
        return (st.st_size, st.st_mtime_ns)

    def _move(self, pdf: Path, base: Path) -> Path:
        """PDF verschieben; bei Fehlern bleibt keine halbe Kopie im Ziel zurück."""
        dest_dir = base / pdf.parent.relative_to(self.in_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / pdf.name
        n = 1
        while dest.exists():
            n += 1
            dest = dest_dir / f"{pdf.stem} ({n}){pdf.suffix}"
        try:
            os.rename(pdf, dest)
        except OSError as exc:
            if exc.errno != errno.EXDEV:  # nur bei anderem Dateisystem kopieren
                raise
            shutil.copy2(pdf, dest)
            try:
                pdf.unlink()
            except OSError:
                dest.unlink(missing_ok=True)
                raise
        return dest

    def _block(self, pdf: Path, what: str, exc: OSError) -> None:
        log.error(
            "%s: %s nicht möglich (%s). Schreibrechte im Eingangsordner prüfen. "
            "Die Datei wird nicht erneut verarbeitet, solange sie unverändert bleibt.",
            pdf.relative_to(self.in_dir), what, exc.strerror or exc,
        )
        sig = self._signature(pdf)
        if sig is not None:
            self._blocked[pdf] = sig

    # ------------------------------------------------------------------ Verarbeitung
    def process(self, pdf: Path) -> bool:
        target = self._target_dir(pdf)
        error_file = target / f"{pdf.stem}.fehler.txt"
        log.info("Neue Datei: %s", pdf.relative_to(self.in_dir))
        try:
            md = pdf2md.convert(pdf, target, language=self.language, ocr=self.ocr, overwrite=True)
        except Exception as exc:  # noqa: BLE001 - Dienst soll weiterlaufen
            msg = pdf2md.describe_error(exc, pdf)
            log.error("Fehler bei %s: %s", pdf.name, msg)
            report = (
                f"Datei: {pdf.name}\n"
                f"Zeit:  {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"Fehler: {msg}\n"
            )
            if self.failed_dir is not None:
                try:
                    moved = self._move(pdf, self.failed_dir)
                    moved.with_name(moved.stem + ".fehler.txt").write_text(report, encoding="utf-8")
                    return False
                except OSError as move_exc:
                    self._block(pdf, "Verschieben nach Fehlerordner", move_exc)
            target.mkdir(parents=True, exist_ok=True)
            error_file.write_text(report, encoding="utf-8")
            return False

        error_file.unlink(missing_ok=True)
        log.info("Fertig: %s", md)
        try:
            if self.done_dir is not None:
                self._move(pdf, self.done_dir)
            elif self.delete:
                pdf.unlink(missing_ok=True)
        except OSError as exc:
            self._block(pdf, "Archivieren" if self.done_dir else "Löschen", exc)
        return True

    def scan_once(self, *, wait_until_stable: bool = True) -> int:
        """Ein Durchlauf; gibt die Anzahl verarbeiteter Dateien zurück."""
        now = time.time()
        processed = 0
        candidates = self._candidates()
        current = set(candidates)
        for cache in (self._seen, self._blocked):
            for gone in set(cache) - current:
                del cache[gone]
        for pdf in candidates:
            if self._stop:
                break
            try:
                blocked = self._blocked.get(pdf)
                if blocked is not None:
                    if blocked == self._signature(pdf):
                        continue
                    del self._blocked[pdf]  # Datei wurde ersetzt -> wieder zulassen
                if wait_until_stable and not self._is_ready(pdf, now):
                    continue
                if self._already_done(pdf):
                    continue
                self.process(pdf)
                processed += 1
            except FileNotFoundError:
                pass  # inzwischen entfernt
            except Exception:  # noqa: BLE001 - eine Datei darf den Rest nicht blockieren
                log.exception("Unerwarteter Fehler bei %s", pdf)
            self._seen.pop(pdf, None)
        return processed

    def run(self, interval: float = 5.0) -> None:
        for d in (self.in_dir, self.out_dir, self.done_dir, self.failed_dir):
            if d is not None:
                d.mkdir(parents=True, exist_ok=True)
        log.info(
            "Überwache %s (alle %gs)%s -> %s", self.in_dir, interval,
            " rekursiv" if self.recursive else "", self.out_dir or "neben dem PDF",
        )
        while not self._stop:
            try:
                self.scan_once()
            except Exception:  # noqa: BLE001 - z. B. Freigabe kurz weg
                log.exception("Fehler beim Durchsuchen von %s", self.in_dir)
            deadline = time.monotonic() + interval
            while not self._stop and time.monotonic() < deadline:
                time.sleep(0.2)
        log.info("Hot Folder beendet.")


def main(argv: list[str] | None = None) -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(
        prog="pdf2md watch",
        description="Überwacht einen Ordner und wandelt neue PDFs automatisch in "
        "gleichnamige Markdown-Dateien um (Hot Folder).",
    )
    parser.add_argument("in_dir", type=Path, help="Eingangsordner, der überwacht wird")
    parser.add_argument("-o", "--output-dir", type=Path, help="Ausgabeordner (Standard: neben dem PDF)")
    after = parser.add_mutually_exclusive_group()
    after.add_argument("--done-dir", type=Path, help="Erfolgreich verarbeitete PDFs hierhin verschieben")
    after.add_argument("--delete", action="store_true", help="Erfolgreich verarbeitete PDFs löschen")
    parser.add_argument("--failed-dir", type=Path, help="Fehlgeschlagene PDFs hierhin verschieben")
    parser.add_argument("-r", "--recursive", action="store_true", help="Unterordner mit überwachen")
    parser.add_argument("-l", "--lang", default=env("PDF2MD_DEFAULT_LANG", "deu+eng"), help="OCR-Sprachen")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--force-ocr", action="store_true", help="OCR immer ausführen")
    group.add_argument("--no-ocr", action="store_true", help="OCR nie ausführen")
    parser.add_argument("--interval", type=float, default=float(env("PDF2MD_WATCH_INTERVAL", "5")),
                        help="Sekunden zwischen zwei Durchläufen (Standard 5)")
    parser.add_argument("--settle", type=float, default=5.0,
                        help="Datei muss so viele Sekunden unverändert sein (Standard 5)")
    parser.add_argument("--once", action="store_true",
                        help="Nur einmal alle vorhandenen PDFs verarbeiten und beenden (z. B. für cron)")
    parser.add_argument("-q", "--quiet", action="store_true", help="Nur Fehler ausgeben")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    level = logging.ERROR if args.quiet else logging.INFO
    pdf2md.log.setLevel(level)
    log.setLevel(level)

    if not args.in_dir.exists() and args.once:
        log.error("Ordner nicht gefunden: %s", args.in_dir)
        return 1

    hf = HotFolder(
        args.in_dir, args.output_dir,
        done_dir=args.done_dir, failed_dir=args.failed_dir, delete=args.delete,
        recursive=args.recursive, language=args.lang,
        ocr="force" if args.force_ocr else "off" if args.no_ocr else "auto",
        settle=args.settle,
    )
    if args.once:
        hf.scan_once(wait_until_stable=False)
        return 0

    signal.signal(signal.SIGTERM, hf.stop)
    signal.signal(signal.SIGINT, hf.stop)
    hf.run(args.interval)
    return 0
