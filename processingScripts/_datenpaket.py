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

SB_DSM: DSM-Raster gehen nach NV\\DSM, Hillshades nach NV\\HILLSHADE (daraus
macht GDWH die STAC-Assets dsm-raster.tif / dsm-hillshade.tif). Erkennung des
Hillshades am Dateinamen siehe is_hillshade.
"""

import os

PRECALC_DIR_KEY = "precalculatedformats"


def is_hillshade(filename):
    """True, wenn filename (SB_DSM) ein Hillshade ist. Zwei Namensformen:
      Namensteil 'hillshade':  2025_PLAINE_MORTE_hillshade_50cm_LV95_LN02.tif
      Endung '_HS' (ADS80):    2016_SILVRETTA_DSM_1m_LV95_LN02_GG_wf_raw_HS.tif
    Die ADS80-Form enthaelt auch '_DSM_' - diese Pruefung muss darum vor jeder
    DSM-Erkennung ueber den Namen kommen."""
    teile = os.path.splitext(os.path.basename(filename))[0].lower().split("_")
    return "hillshade" in teile or teile[-1] == "hs"


def sb_dsm_subfolder(filename):
    """Unterordner unter NV im SB_DSM-Datenpaket: 'HILLSHADE' oder 'DSM'."""
    return "HILLSHADE" if is_hillshade(filename) else "DSM"


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
