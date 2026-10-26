"""
_datenpaket.py  –  Ordnerstruktur des GDWH-Datenpakets (nur Standardbibliothek).

Gemeinsame Quelle fuer GUI (Pruefung vor dem Start) und Script 1 (Kopieren,
files.csv) - sonst laufen Pruefung und geschriebener Pfad auseinander.

SB_DSM_PUNKTWOLKE: GDWH legt beim Erstellen des Datenpakets im Portal die
Unterordner selbst an, darunter direkt im Bucket-Pfad den Ordner fuer die
vorberechneten LAZ. Dessen Schreibweise stellt GDWH mit einem Deploy von
"PrecalculatedFormats" auf "PreCalculatedFormats" um (Mail P. 2026-10, Datum
offen). Der Pfad in files.csv muss exakt der Schreibweise im Paket
entsprechen, deshalb wird sie pro Datenpaket aus dem Ordner gelesen statt fest
codiert - das Script funktioniert so vor und nach dem Deploy.
"""

import os

PRECALC_DIR_KEY = "precalculatedformats"


def precalc_dir(paket_pfad):
    """Name des PreCalculatedFormats-Ordners im Datenpaket, in genau der
    Schreibweise, in der GDWH ihn angelegt hat.

    os.listdir statt os.path.isdir: NTFS/SMB unterscheidet beim Zugriff nicht
    zwischen gross und klein, nur listdir liefert die echte Schreibweise.

    ValueError, wenn das Paket nicht lesbar ist, der Ordner fehlt (Datenpaket
    nicht im Portal erstellt oder falscher Bucket-Pfad) oder in mehreren
    Schreibweisen vorkommt (nur auf case-sensitivem Speicher moeglich)."""
    try:
        eintraege = os.listdir(paket_pfad)
    except OSError as e:
        raise ValueError(f"Datenpaket nicht lesbar:\n  {paket_pfad}\n  ({e})")
    treffer = sorted(n for n in eintraege
                     if n.lower() == PRECALC_DIR_KEY
                     and os.path.isdir(os.path.join(paket_pfad, n)))
    if not treffer:
        raise ValueError(
            f"Ordner 'PreCalculatedFormats' fehlt im Datenpaket:\n  {paket_pfad}\n"
            "GDWH legt ihn beim Erstellen eines SB_DSM_PUNKTWOLKE-Datenpakets im "
            "Portal an - Datenpaket erstellt und Bucket-Pfad korrekt?")
    if len(treffer) > 1:
        raise ValueError(
            f"Ordner 'PreCalculatedFormats' in mehreren Schreibweisen im Datenpaket "
            f"({', '.join(treffer)}):\n  {paket_pfad}")
    return treffer[0]
