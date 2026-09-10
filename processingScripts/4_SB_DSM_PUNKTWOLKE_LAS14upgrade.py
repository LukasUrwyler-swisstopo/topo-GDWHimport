#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py

Batch-Vorkonversion fuer SB_DSM_PUNKTWOLKE-Tiles (LAZ, photogrammetrisch
abgeleitete DSM-Punktwolken aus Autokorrelation, ADS100-Luftbildstreifen).

Hebt die Quell-Tiles von LAS 1.2 (Point Data Record Format 1, keine CRS-
Angabe im Header) auf LAS 1.4 (Point Data Record Format 6, CRS-Tag LV95/LN02)
an, damit sie strukturell kongruent zu swissSURFACE3D sind und in den GDWH
importiert werden koennen. Laeuft VOR dem eigentlichen GDWH-Import
(1_allGDS_upload_GDWH_withCHECKxml.py); dieses Skript schreibt ausschliesslich
in ein separates Zielverzeichnis, die Quelldateien bleiben unveraendert.

Vorgehen pro Tile (siehe Docstrings der einzelnen Funktionen fuer Details):
  1. Kachelursprung deterministisch aus dem Dateinamen parsen (Regex), NICHT
     aus dem Datenminimum. Plausibilitaetspruefung gegen die Schweizer
     Landesgrenzen (LV95, in km).
  2. Bereits migrierte Tiles (LAS 1.4/PF6, CRS korrekt, global_encoding 17)
     werden erkannt und nur unveraendert kopiert, nicht nochmal konvertiert.
  3. PDAL-Pipeline (subprocess, siehe _find_pdal_exe): Scale 0.001 -> 0.01,
     Offset Datenminimum -> Kachelursprung, LAS 1.4/PF6, global_encoding 17.
     KEINE Reprojektion (kein filters.reprojection) - nur Requantisierung,
     die Koordinatenwerte aendern sich ausser durch die Scale-Rundung nicht.
  4. CRS-Tag: KEIN a_srs auf writers.las und KEIN las2las -epsg/-set_ogc_wkt.
     Stattdessen werden die zwei VLRs (GeoTIFF-KeyDirectory record_id 34735 +
     OGC-WKT record_id 2112) byte-exakt aus einer verifizierten
     swissSURFACE3D-Referenzkachel injiziert (inject_reference_vlrs).
     Begruendung (empirisch getestet, nicht angenommen):
       - PDAL erzeugt bei a_srs="EPSG:2056+5728" einen semantisch korrekten,
         aber NICHT byte-identischen WKT (COMPD_CS statt COMPOUNDCRS, anderer
         CRS-Name) und schreibt den GeoTIFF-VLR (34735) gar nicht.
       - las2las -epsg 2056 -vertical_epsg 5728 -set_ogc_wkt lieferte in der
         getesteten Version (260505) geodaetisch FALSCHE Oblique-Mercator-
         Parameter und liess die Vertikalkomponente (LN02/5728) komplett weg
         - trotz Erfolgsmeldung, nur eine nicht-fatale Warnung. Vor dem
         produktiven Batch unbedingt mit der Firmen-Version (231204) auf
         einer einzelnen Kachel gegenpruefen.
       - PDALs eigene writers.las-Option 'vlrs' verwirft VLRs mit
         user_id "LASF_Projection" (reserviert fuer PDAL-eigene SRS-VLRs)
         still und ohne Fehlermeldung - deshalb Byte-Patch statt PDAL-Option.
       - Manche Quell-Batches (z.B. Job BONDO 2017, Vorfall 27.8.2026) haben
         trotz "keine CRS-Angabe" im Sinne dieses Skripts bereits eine
         unvollstaendige/bedeutungslose GeoTIFF-VLR im Header (Ueberrest
         einer aelteren LAStools-Vorstufe wie 'lasclip', z.B.
         LOCAL_CS["unnamed"]). PDAL uebernimmt diese beim Lesen/Schreiben
         automatisch in die neue LAS-1.4-Datei (auch ohne dass das Skript
         das anfordert) - inject_reference_vlrs() entfernt eine solche
         uebernommene VLR deshalb, statt abzubrechen (siehe dortiger
         Docstring).
  5. Vollstaendige Nachkonversions-Validierung (siehe validate_target und
     validate_point_ranges). Die BBox wird dabei gegen die tatsaechlichen
     Punkt-Extremwerte der Quelle geprueft, NICHT gegen deren Header-BBox:
     manche Quell-Batches (z.B. Job RANDA 2020, Vorfall 10.9.2026) haben
     eine Header-BBox, die nicht zu den eigenen Punkten passt (Z bis 2.3 cm
     daneben, vermutlich nach Filterung/Ausduennung nicht nachgefuehrt).
     PDAL berechnet den Ziel-Header ohnehin neu aus den Punkten - ein
     ungenauer Quell-Header wird deshalb nur als Warnung gemeldet.
     Erst bei vollstaendigem Erfolg wird die Zieldatei atomar (os.replace)
     geschrieben. Bei jedem Fehler bleibt eine evtl. vorhandene Zieldatei
     unangetastet, die Temp-Datei wird verworfen.

Verwendung (aus dem Projekt-Hauptverzeichnis):
  Testlauf ohne Schreibzugriff (zeigt geplante Aktionen pro Tile):
    python processingScripts\4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py --input-dir Q:\...\input --output-dir Q:\...\output --dry-run

  Batch, nicht rekursiv:
    python processingScripts\4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py --input-dir Q:\...\input --output-dir Q:\...\output

  Batch, rekursiv (alle Unterordner nach .laz durchsuchen):
    python processingScripts\4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py --input-dir Q:\...\input --output-dir Q:\...\output --recursive

  Kacheln werden standardmaessig parallel verarbeitet (siehe
  _default_worker_count: Kernanzahl - 2, max. 8). Fuer seriellen Ablauf
  (z.B. Debugging) explizit --workers 1 setzen, fuer eine andere Anzahl
  z.B. --workers 4.

Benoetigt: pdal.exe im PATH oder im selben bin-Ordner wie der aktuelle
Python-Interpreter (OSGeo4W/QGIS-Python-Umgebung, siehe _find_pdal_exe).
Keine PDAL-Python-Bindings, kein pyproj - die CRS-Aufloesung fuer die
Validierung nutzt die von PDAL/PROJ bereits aufbereitete SRS-JSON-Struktur
aus 'pdal info --metadata' (siehe resolve_crs_epsg).
"""

import argparse
import base64
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ****************************** Log-Funktion ******************************
_log_file_handle = None


def log(message):
    print(message)
    if _log_file_handle:
        _log_file_handle.write(message + "\n")
        _log_file_handle.flush()


# ****************************** Zielwerte / Konstanten ******************************
TARGET_MINOR_VERSION = 4
TARGET_POINT_FORMAT = 6
TARGET_POINT_LENGTH = 30
TARGET_HEADER_SIZE = 375
TARGET_GLOBAL_ENCODING = 17  # Bit 0 (Adjusted Standard GPS Time) + Bit 4 (WKT)
BBOX_TOLERANCE_M = 0.01  # 1 cm, siehe Anforderung
# Dimensionen, deren tatsaechliche Min/Max-Werte (aus den Punkten, NICHT aus
# dem Header) waehrend der Konversion mitgemessen werden (siehe convert_tile)
STATS_DIMENSIONS = ("X", "Y", "Z", "Classification")
# Header-BBox-Feld -> (Dimension, Index in (minimum, maximum))
BBOX_FIELDS = (("minx", "X", 0), ("maxx", "X", 1), ("miny", "Y", 0),
               ("maxy", "Y", 1), ("minz", "Z", 0), ("maxz", "Z", 1))

# Kachelname-Muster: "..._<easting_km>_<northing_km>_LV95_LN02.laz"
# Bsp: 2025_BIRCH_BLATTEN_TIN_..._2623_1138_LV95_LN02.laz -> (2623, 1138)
TILE_NAME_PATTERN = re.compile(r'(\d{4})_(\d{4})_LV95_LN02\.laz$', re.IGNORECASE)

# Plausibilitaet der Kachelkoordinaten: Schweizer Landesgrenzen in km, LV95
LV95_EASTING_KM_RANGE = (2480, 2840)
LV95_NORTHING_KM_RANGE = (1070, 1300)

# Byte-exakte VLR-Payloads aus der verifizierten swissSURFACE3D-Referenzkachel
# 2655_1272.laz (LV95/LN02, EPSG:2056 horizontal + EPSG:5728 vertikal).
# NICHT aus GeoTIFF-Keys/EPSG-Code neu berechnen (siehe Modul-Docstring) -
# sondern unveraendert aus der Referenz uebernehmen.
REFERENCE_VLR_DESCRIPTION = "by LAStools of rapidlasso GmbH"
REFERENCE_VLR_34735_B64 = (
    "AQABAAAABQAABAAAAQABAAAMAAABAAgIBAwAAAEAKSMDEAAAAQApIwAQAAABAGAW"
)
REFERENCE_VLR_2112_B64 = (
    "Q09NUE9VTkRDUlNbIlByb2plY3RlZCBjb29yZGluYXRlIHN5c3RlbSB3aXRoIGVsZXZhdGlvbiIsUFJPSkNTWyJDSDE5MDMrIC8gTFY5"
    "NSIsR0VPR0NTWyJDSDE5MDMrIixEQVRVTVsiQ0gxOTAzKyIsU1BIRVJPSURbIkJlc3NlbCAxODQxIiw2Mzc3Mzk3LjE1NSwyOTkuMTUy"
    "ODEyOCxBVVRIT1JJVFlbIkVQU0ciLCI3MDA0Il1dLEFVVEhPUklUWVsiRVBTRyIsIjYxNTAiXV0sUFJJTUVNWyJHcmVlbndpY2giLDAs"
    "QVVUSE9SSVRZWyJFUFNHIiwiODkwMSJdXSxVTklUWyJkZWdyZWUiLDAuMDE3NDUzMjkyNTE5OTQzMyxBVVRIT1JJVFlbIkVQU0ciLCI5"
    "MTIyIl1dLEFVVEhPUklUWVsiRVBTRyIsIjQxNTAiXV0sUFJPSkVDVElPTlsiSG90aW5lX09ibGlxdWVfTWVyY2F0b3JfQXppbXV0aF9D"
    "ZW50ZXIiXSxQQVJBTUVURVJbImxhdGl0dWRlX29mX2NlbnRlciIsNDYuOTUyNDA1NTU1NTU1Nl0sUEFSQU1FVEVSWyJsb25naXR1ZGVf"
    "b2ZfY2VudGVyIiw3LjQzOTU4MzMzMzMzMzMzXSxQQVJBTUVURVJbImF6aW11dGgiLDkwXSxQQVJBTUVURVJbInJlY3RpZmllZF9ncmlk"
    "X2FuZ2xlIiw5MF0sUEFSQU1FVEVSWyJzY2FsZV9mYWN0b3IiLDFdLFBBUkFNRVRFUlsiZmFsc2VfZWFzdGluZyIsMjYwMDAwMF0sUEFS"
    "QU1FVEVSWyJmYWxzZV9ub3J0aGluZyIsMTIwMDAwMF0sVU5JVFsibWV0cmUiLDEsQVVUSE9SSVRZWyJFUFNHIiwiOTAwMSJdXSxBWElT"
    "WyJFYXN0aW5nIixFQVNUXSxBWElTWyJOb3J0aGluZyIsTk9SVEhdLEFVVEhPUklUWVsiRVBTRyIsIjIwNTYiXV0sVkVSVF9DU1siTE4w"
    "MiBoZWlnaHQiLFZFUlRfREFUVU1bIkxhbmRlc25pdmVsbGVtZW50IDE5MDIiLDIwMDUsQVVUSE9SSVRZWyJFUFNHIiwiNTEyNyJdXSxV"
    "TklUWyJtZXRyZSIsMSxBVVRIT1JJVFlbIkVQU0ciLCI5MDAxIl1dLEFYSVNbIkdyYXZpdHktcmVsYXRlZCBoZWlnaHQiLFVQXSxBVVRI"
    "T1JJVFlbIkVQU0ciLCI1NzI4Il1dXQA="
)


# ****************************** PDAL-Hilfsfunktionen ******************************
_pdal_exe_cache = None  # einmal ermittelt, dann wiederverwendet (siehe _find_pdal_exe)


def _find_pdal_exe():
    """Ermittelt den Pfad zur pdal.exe (PATH bevorzugt, sonst gleicher
    bin-Ordner wie der aktuelle Python-Interpreter - OSGeo4W/QGIS-Python).

    Das Ergebnis wird im Modul zwischengespeichert: 'shutil.which' durchsucht
    bei jedem Aufruf den kompletten PATH neu, was bei mehreren hundert Kacheln
    (mehrere PDAL-Aufrufe pro Kachel) unnoetig oft wiederholt wuerde. Der
    Pfad zur pdal.exe aendert sich waehrend eines Laufs nicht.
    Race-sicher genug fuer parallele Threads (siehe convert_folder):
    im schlimmsten Fall wird 'shutil.which' von zwei Threads gleichzeitig
    einmal zu viel ausgefuehrt, das Ergebnis ist aber deterministisch gleich.
    """
    global _pdal_exe_cache
    if _pdal_exe_cache is not None:
        return _pdal_exe_cache
    exe = shutil.which("pdal")
    if not exe:
        candidate = os.path.join(os.path.dirname(sys.executable), "pdal.exe")
        exe = candidate if os.path.isfile(candidate) else "pdal"
    _pdal_exe_cache = exe
    return exe


def _default_worker_count():
    """Anzahl paralleler PDAL-Worker (siehe convert_folder): reserviert 2 Kerne
    fuer OS/GUI/andere Prozesse, nutzt den Rest bis maximal 8 - auf den
    ueblichen 8-Kern-Zielmaschinen also 6, auf staerkeren Maschinen bis zu 8.
    os.cpu_count() kann in seltenen Faellen None liefern (Kernanzahl nicht
    bestimmbar) - dann konservativ 4 als Annahme."""
    cpu = os.cpu_count() or 4
    return max(1, min(cpu - 2, 8))


def pdal_metadata(file_path):
    """Ruft 'pdal info --metadata' auf und gibt das geparste JSON-Dict zurueck."""
    result = subprocess.run(
        [_find_pdal_exe(), "info", "--metadata", file_path],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _ranges_from_statistic(statistic):
    """Wandelt PDALs 'statistic'-Liste (ein Eintrag pro Dimension mit 'name',
    'minimum', 'maximum', ...) in {dimension: (minimum, maximum)} um."""
    if isinstance(statistic, dict):  # Absicherung, falls PDAL einen Einzeleintrag nicht als Liste ausgibt
        statistic = [statistic]
    return {stat.get("name"): (stat.get("minimum"), stat.get("maximum"))
            for stat in statistic or []}


def pdal_dimension_ranges(file_path, dimensions=STATS_DIMENSIONS):
    """Liest Minimum/Maximum der angegebenen Dimensionen aus den tatsaechlichen
    Punkten (gezielter Scan nur dieser Dimensionen - nicht 'pdal info --stats'
    ueber alle, um bei grossen Tiles nicht unnoetig viele Spalten auszuwerten).
    Gibt {dimension: (minimum, maximum)} zurueck."""
    result = subprocess.run(
        [_find_pdal_exe(), "info", "--dimensions", ",".join(dimensions), "--stats", file_path],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    return _ranges_from_statistic(data.get("stats", {}).get("statistic"))


def dimension_ranges_from_pipeline_metadata(pipeline_metadata):
    """Liest Minimum/Maximum pro Dimension aus der Metadata einer
    'pdal pipeline --metadata ...'-Ausfuehrung, deren Pipeline eine
    'filters.stats'-Stage enthaelt (siehe convert_tile). Struktur empirisch
    verifiziert (PDAL 2.10.0, mit Classification): identisch zu
    'pdal info --stats', nur unter metadata["stages"]["filters.stats"] statt
    metadata["stats"] verschachtelt. Gibt {} zurueck, falls die Stage fehlt -
    der Aufrufer faellt dann auf einen regulaeren 'pdal info'-Aufruf zurueck."""
    stats = ((pipeline_metadata or {}).get("stages") or {}).get("filters.stats") or {}
    return _ranges_from_statistic(stats.get("statistic"))


def run_pdal_pipeline(pipeline_dict, capture_metadata=False):
    """Schreibt pipeline_dict in eine temporaere JSON-Datei und fuehrt sie via
    'pdal pipeline' aus. Wirft CalledProcessError mit stderr bei Fehlern.

    Bei capture_metadata=True wird zusaetzlich '--metadata <tmp>.json'
    uebergeben und die geparste Pipeline-Metadata als Dict zurueckgegeben
    (sonst None). Damit lassen sich z.B. Ergebnisse einer angehaengten
    'filters.stats'-Stage OHNE einen separaten 'pdal info'-Aufruf (= ohne
    einen weiteren vollstaendigen Lese-/Dekompressionsdurchlauf durch die
    Datei) auslesen, siehe convert_tile."""
    fd, pipeline_path = tempfile.mkstemp(suffix=".json")
    metadata_path = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(pipeline_dict, f)
        cmd = [_find_pdal_exe(), "pipeline", pipeline_path]
        if capture_metadata:
            meta_fd, metadata_path = tempfile.mkstemp(suffix=".json")
            os.close(meta_fd)
            cmd += ["--metadata", metadata_path]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        if capture_metadata:
            with open(metadata_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return None
    finally:
        try:
            os.remove(pipeline_path)
        except Exception:
            pass
        if metadata_path:
            try:
                os.remove(metadata_path)
            except Exception:
                pass


# ****************************** Kachelname / Plausibilitaet ******************************
def parse_tile_from_filename(filename):
    """Parst Easting/Northing (in km) deterministisch aus dem Dateinamen
    (Muster '..._<E>_<N>_LV95_LN02.laz'), NICHT aus dem Datenminimum.

    Wirft ValueError mit klarer Meldung bei fehlendem Muster oder bei
    unplausiblen Werten (ausserhalb der Schweizer Landesgrenzen LV95, in km).
    """
    match = TILE_NAME_PATTERN.search(filename)
    if not match:
        raise ValueError(
            f"Kachelmuster '..._<Easting>_<Northing>_LV95_LN02.laz' nicht gefunden in "
            f"'{filename}' - Offset kann nicht deterministisch bestimmt werden."
        )
    easting_km, northing_km = int(match.group(1)), int(match.group(2))

    e_min, e_max = LV95_EASTING_KM_RANGE
    n_min, n_max = LV95_NORTHING_KM_RANGE
    if not (e_min <= easting_km <= e_max):
        raise ValueError(
            f"Kachel-Easting {easting_km} km aus '{filename}' liegt ausserhalb der "
            f"plausiblen Schweizer LV95-Ausdehnung ({e_min}-{e_max} km)."
        )
    if not (n_min <= northing_km <= n_max):
        raise ValueError(
            f"Kachel-Northing {northing_km} km aus '{filename}' liegt ausserhalb der "
            f"plausiblen Schweizer LV95-Ausdehnung ({n_min}-{n_max} km)."
        )
    return easting_km, northing_km


def check_tile_frame_plausibility(metadata, easting_km, northing_km, filename):
    """Vergleicht die Quell-BBox gegen den nominalen 1x1-km-Kachelrahmen.

    Luecken zum Rand (z.B. Datenloecher) werden nur als Warnung geloggt, NICHT
    repariert - die Konversion laeuft trotzdem weiter. Punkte AUSSERHALB des
    Kachelrahmens sind dagegen ein harter Fehler (deutet auf falsch geparste
    Kachelkoordinaten oder eine fehlplatzierte Datei hin).

    Gibt eine Liste von Warnungs-Strings zurueck; wirft ValueError bei Punkten
    ausserhalb des Rahmens.
    """
    md = metadata.get("metadata") or {}
    nominal_minx, nominal_miny = easting_km * 1000, northing_km * 1000
    nominal_maxx, nominal_maxy = nominal_minx + 999.99, nominal_miny + 999.99
    eps = 0.02  # Toleranz gegen Rundungsrauschen am Rand

    try:
        minx, maxx = float(md["minx"]), float(md["maxx"])
        miny, maxy = float(md["miny"]), float(md["maxy"])
    except (KeyError, TypeError, ValueError):
        return [f"{filename}: BBox nicht in Metadaten gefunden - Kachelrahmen-Pruefung uebersprungen."]

    if minx < nominal_minx - eps or maxx > nominal_maxx + eps or \
       miny < nominal_miny - eps or maxy > nominal_maxy + eps:
        raise ValueError(
            f"Punkte ausserhalb des Kachelrahmens: BBox (X {minx:.2f}-{maxx:.2f}, "
            f"Y {miny:.2f}-{maxy:.2f}) vs. erwarteter Rahmen "
            f"(X {nominal_minx:.2f}-{nominal_maxx:.2f}, Y {nominal_miny:.2f}-{nominal_maxy:.2f})."
        )

    warnings = []
    gap_w, gap_s = minx - nominal_minx, miny - nominal_miny
    gap_e, gap_n = nominal_maxx - maxx, nominal_maxy - maxy
    for label, gap in (("West", gap_w), ("Sued", gap_s), ("Ost", gap_e), ("Nord", gap_n)):
        if gap > eps:
            warnings.append(f"{filename}: Datenluecke am {label}-Rand von {gap:.2f} m "
                             f"(Kachelrahmen nicht vollstaendig gefuellt).")
    return warnings


# ****************************** CRS-Aufloesung (Validierung) ******************************
def resolve_crs_epsg(metadata):
    """Liest horizontalen und vertikalen EPSG-Code aus der von PDAL/PROJ
    bereits aufbereiteten 'srs.json'-Struktur in den Metadaten (CompoundCRS
    mit Bestandteilen 'ProjectedCRS'/'GeographicCRS' und 'VerticalCRS').
    Keine pyproj-Abhaengigkeit noetig - PDAL nutzt intern ohnehin PROJ dafuer.
    Gibt (horizontal_epsg, vertical_epsg) zurueck, je None falls nicht auflösbar.
    """
    srs = ((metadata.get("metadata") or {}).get("srs")) or {}
    j = srs.get("json") or {}
    components = j.get("components") or []
    horizontal_epsg = vertical_epsg = None
    for comp in components:
        ident = comp.get("id") or {}
        epsg = ident.get("code") if ident.get("authority") == "EPSG" else None
        if comp.get("type") == "VerticalCRS":
            vertical_epsg = epsg
        elif comp.get("type") in ("ProjectedCRS", "GeographicCRS", "GeodeticCRS"):
            horizontal_epsg = epsg
    if not components:
        ident = j.get("id") or {}
        if ident.get("authority") == "EPSG":
            horizontal_epsg = ident.get("code")
    return horizontal_epsg, vertical_epsg


def is_already_migrated(metadata):
    """True, wenn die Datei bereits LAS 1.4/PF6 mit korrektem CRS (2056+5728)
    und global_encoding 17 ist - dann muss NICHT nochmal konvertiert werden."""
    md = metadata.get("metadata") or {}
    if md.get("minor_version") != TARGET_MINOR_VERSION:
        return False
    if md.get("dataformat_id") != TARGET_POINT_FORMAT:
        return False
    if md.get("global_encoding") != TARGET_GLOBAL_ENCODING:
        return False
    h_epsg, v_epsg = resolve_crs_epsg(metadata)
    return h_epsg == 2056 and v_epsg == 5728


# ****************************** VLR-Byte-Injektion ******************************
def _build_vlr_record(user_id, record_id, description, payload):
    header = struct.pack(
        "<H16sHH32s",
        0,
        user_id.encode("ascii").ljust(16, b"\x00"),
        record_id,
        len(payload),
        description.encode("ascii").ljust(32, b"\x00"),
    )
    return header + payload


def inject_reference_vlrs(las_path):
    """Fuegt die zwei byte-exakten Referenz-VLRs (GeoTIFF-KeyDirectory 34735 +
    OGC-WKT 2112) in eine LAS/LAZ-Datei ein, OHNE eine CRS-Bibliothek den WKT
    neu berechnen zu lassen (siehe Modul-Docstring fuer die Begruendung).

    Funktioniert auch bei komprimierten (LAZ) Punktdaten - dafuer muss aber
    zusaetzlich zum Header ('offset_to_point_data') auch die 'chunk table
    start position' der LASzip-Kompression korrigiert werden: dieses Feld
    steht als int64 (absoluter Datei-Offset) ganz am Anfang des Punkt-Bereichs
    und verweist auf die Chunk-Tabelle nahe dem Dateiende. Wird nur der VLR-
    Block verschoben, ohne dieses Feld anzupassen, zeigt es auf die falsche
    Stelle - die Datei bleibt fuer 'pdal info --summary'/'--metadata' lesbar
    (die Anzahl Punkte kommt direkt aus dem Header), aber jeder echte
    Dekompressions-Durchlauf (z.B. 'pdal info --stats') bricht mit
    'Invalid version ... found in LAZ chunk table' ab (empirisch gefunden,
    siehe Modul-Docstring - ein rein VLR-verschiebender Patch reicht bei LAZ
    NICHT aus).

    Ein bereits vorhandener VLR mit user_id 'LASF_Projection' wird entfernt,
    NICHT als Fehler behandelt (fruehere Version brach hier ab). Grund
    (empirisch bestaetigt, siehe README/Vorfall 27.8.2026, Job BONDO 2017):
    is_already_migrated() prueft nur, ob die QUELLE bereits vollstaendig im
    Zielformat vorliegt - verhindert aber NICHT, dass PDAL beim Lesen/
    Schreiben eine in der Quelle erkannte, aber unvollstaendige/bedeutungs-
    lose GeoTIFF-VLR (z.B. LOCAL_CS["unnamed"] als Ueberrest einer aelteren
    LAStools-Vorstufe wie 'lasclip') automatisch in die frisch geschriebene
    LAS-1.4-Datei uebernimmt - selbst wenn dabei ein VLR entsteht (record_id
    2112, OGC WKT), den die Quelle gar nicht hatte. Eine solche uebernommene
    VLR ist NIEMALS autoritativ fuer die Ziel-CRS (die kommt ausschliesslich
    aus der byte-exakten Referenz weiter unten) und wird deshalb entfernt statt
    den Lauf abzubrechen. Arbeitet in-place auf las_path (soll nur auf einer
    Temp-Datei aufgerufen werden, siehe convert_tile).

    Gibt die Anzahl entfernter 'LASF_Projection'-VLRs zurueck (0 = normaler
    Fall, keine vorhanden).
    """
    with open(las_path, "rb") as f:
        data = f.read()

    header_size, offset_to_point_data, n_vlr = struct.unpack_from("<HII", data, 94)
    existing_vlr_block = data[header_size:offset_to_point_data]

    is_laszip = False
    n_stripped = 0
    kept_vlr_chunks = []
    pos = 0
    for _ in range(n_vlr):
        _, user_id_raw, record_id, record_len, _ = struct.unpack_from(
            "<H16sHH32s", existing_vlr_block, pos)
        user_id = user_id_raw.split(b"\x00")[0].decode("ascii", "replace")
        vlr_len = 54 + record_len
        if user_id == "LASF_Projection":
            n_stripped += 1
        else:
            kept_vlr_chunks.append(existing_vlr_block[pos:pos + vlr_len])
        if user_id == "laszip encoded" and record_id == 22204:
            is_laszip = True
        pos += vlr_len

    existing_vlr_block = b"".join(kept_vlr_chunks)
    n_vlr -= n_stripped

    payload_34735 = base64.b64decode(REFERENCE_VLR_34735_B64)
    payload_2112 = base64.b64decode(REFERENCE_VLR_2112_B64)
    vlr1 = _build_vlr_record("LASF_Projection", 34735, REFERENCE_VLR_DESCRIPTION, payload_34735)
    vlr2 = _build_vlr_record("LASF_Projection", 2112, REFERENCE_VLR_DESCRIPTION, payload_2112)
    new_vlr_block = bytes(existing_vlr_block) + vlr1 + vlr2
    new_offset_to_point_data = header_size + len(new_vlr_block)
    # Tatsaechliche Verschiebung der Punktdaten im Byte-Layout - NICHT einfach
    # len(vlr1)+len(vlr2): wurden oben VLRs entfernt (n_stripped > 0), wurden
    # gleichzeitig Bytes aus dem Block entfernt, die Nettoverschiebung ist
    # dann kleiner. Massgeblich ist die tatsaechliche Differenz der
    # offset_to_point_data-Werte.
    shift = new_offset_to_point_data - offset_to_point_data

    point_data = bytearray(data[offset_to_point_data:])
    if is_laszip:
        chunk_table_pos, = struct.unpack_from("<q", point_data, 0)
        if chunk_table_pos != -1:  # -1 = LASzip-Platzhalter, kommt bei fertig geschriebenen Dateien nicht vor
            struct.pack_into("<q", point_data, 0, chunk_table_pos + shift)

    new_data = bytearray(data[:header_size]) + new_vlr_block + point_data
    struct.pack_into("<I", new_data, 96, new_offset_to_point_data)
    struct.pack_into("<I", new_data, 100, n_vlr + 2)

    global_encoding, = struct.unpack_from("<H", new_data, 6)
    struct.pack_into("<H", new_data, 6, global_encoding | 0x10)  # WKT-Bit setzen

    with open(las_path, "wb") as f:
        f.write(new_data)

    return n_stripped


# ****************************** Validierung Quelle vs. Ziel ******************************
def validate_target(src_metadata, dst_metadata):
    """Nachkonversions-Validierung der Header-/CRS-Angaben (BBox und
    Classification separat, siehe validate_point_ranges). Gibt eine Liste
    von Fehler-Strings zurueck (leer = alles OK). Prueft NUR (keine Reparatur):
      - Punktanzahl identisch
      - minor_version==4, dataformat_id==6, point_length==30, header_size==375
      - global_encoding==17
      - beide CRS-VLRs vorhanden (record_id 34735 und 2112), VLR 2112 endet
        auf Nullbyte
      - CRS ueber die PDAL/PROJ-SRS-Struktur auflösbar: horizontal==2056,
        vertikal==5728
      - '5729' bzw. 'LHN95' kommen im Ziel-WKT NICHT vor
    """
    problems = []
    src_md = src_metadata.get("metadata") or {}
    dst_md = dst_metadata.get("metadata") or {}

    if src_md.get("count") != dst_md.get("count"):
        problems.append(f"Punktanzahl weicht ab: Quelle {src_md.get('count')} vs. Ziel {dst_md.get('count')}")

    if dst_md.get("minor_version") != TARGET_MINOR_VERSION:
        problems.append(f"minor_version={dst_md.get('minor_version')}, erwartet {TARGET_MINOR_VERSION}")
    if dst_md.get("dataformat_id") != TARGET_POINT_FORMAT:
        problems.append(f"dataformat_id={dst_md.get('dataformat_id')}, erwartet {TARGET_POINT_FORMAT}")
    if dst_md.get("point_length") != TARGET_POINT_LENGTH:
        problems.append(f"point_length={dst_md.get('point_length')}, erwartet {TARGET_POINT_LENGTH}")
    if dst_md.get("header_size") != TARGET_HEADER_SIZE:
        problems.append(f"header_size={dst_md.get('header_size')}, erwartet {TARGET_HEADER_SIZE}")
    if dst_md.get("global_encoding") != TARGET_GLOBAL_ENCODING:
        problems.append(f"global_encoding={dst_md.get('global_encoding')}, erwartet {TARGET_GLOBAL_ENCODING}")

    found_34735 = found_2112 = False
    vlr2112_ok = False
    i = 0
    while f"vlr_{i}" in dst_md:
        vlr = dst_md[f"vlr_{i}"]
        if vlr.get("record_id") == 34735 and vlr.get("user_id") == "LASF_Projection":
            found_34735 = True
        if vlr.get("record_id") == 2112 and vlr.get("user_id") == "LASF_Projection":
            found_2112 = True
            payload = base64.b64decode(vlr.get("data", ""))
            vlr2112_ok = payload.endswith(b"\x00")
        i += 1
    if not found_34735:
        problems.append("VLR record_id 34735 (GeoTIFF KeyDirectory) fehlt im Ziel.")
    if not found_2112:
        problems.append("VLR record_id 2112 (OGC WKT) fehlt im Ziel.")
    elif not vlr2112_ok:
        problems.append("VLR record_id 2112 (OGC WKT) endet nicht auf Nullbyte.")

    h_epsg, v_epsg = resolve_crs_epsg(dst_metadata)
    if h_epsg != 2056:
        problems.append(f"Horizontales CRS = EPSG:{h_epsg}, erwartet EPSG:2056")
    if v_epsg != 5728:
        problems.append(f"Vertikales CRS = EPSG:{v_epsg}, erwartet EPSG:5728")
    wkt_text = dst_md.get("spatialreference", "") or ""
    if "5729" in wkt_text or "LHN95" in wkt_text:
        problems.append("Ziel-WKT enthaelt '5729' oder 'LHN95' (LHN95 statt LN02) - FACHLICHER FEHLER.")

    return problems


def header_bbox_deviations(metadata, ranges):
    """Vergleicht die Header-BBox (minx..maxz aus 'pdal info --metadata') mit
    den tatsaechlichen Punkt-Extremwerten (ranges, siehe
    pdal_dimension_ranges). Gibt eine Liste mit Beschreibungen aller Felder
    zurueck, die mehr als BBOX_TOLERANCE_M abweichen oder nicht ermittelbar
    sind (leer = Header passt zu den Punkten)."""
    md = metadata.get("metadata") or {}
    deviations = []
    for key, dim, idx in BBOX_FIELDS:
        try:
            header_value = float(md[key])
            point_value = float(ranges[dim][idx])
        except (KeyError, TypeError, ValueError):
            deviations.append(f"'{key}' nicht ermittelbar")
            continue
        d = abs(header_value - point_value)
        if d > BBOX_TOLERANCE_M:
            deviations.append(f"'{key}' Header {header_value:.3f} vs. Punkte {point_value:.3f} ({d:.4f} m)")
    return deviations


def validate_point_ranges(src_ranges, dst_ranges, dst_metadata):
    """Vergleicht die tatsaechlichen Punkt-Extremwerte (X/Y/Z, Classification)
    von Quelle und Ziel sowie die Ziel-Header-BBox mit den Ziel-Punkten.
    Gibt eine Liste von Fehler-Strings zurueck (leer = OK).

    Bewusst NICHT Quell-Header gegen Ziel-Header (fruehere Version): PDAL
    schreibt die Ziel-BBox immer neu aus den Punkten, die Quell-BBox stammt
    dagegen aus deren Header und kann ungenau sein (Vorfall 10.9.2026, Job
    RANDA 2020, siehe Modul-Docstring) - dann schlug die Pruefung fehl,
    obwohl die Punkte korrekt konvertiert waren. Die Requantisierung
    (Scale 0.001 -> 0.01) verschiebt jede Koordinate um hoechstens eine halbe
    Ziel-Scale (5 mm), BBOX_TOLERANCE_M (1 cm) reicht also aus.

    src_ranges stammt aus dem ohnehin noetigen Lesedurchlauf der Konversions-
    Pipeline (filters.stats, siehe convert_tile) - die Quelle wird dafuer
    NICHT nochmal separat eingelesen. dst_ranges braucht einen eigenen
    'pdal info'-Aufruf: erst er bestaetigt, was nach der LAS1.2(PF1)->
    LAS1.4(PF6)-Punktformat-Umwandlung tatsaechlich auf der Platte steht (PF1
    packt Classification als 5-Bit-Wert zusammen mit Flag-Bits in ein Byte,
    PF6 trennt beides - das ist die Stelle, an der ein Konversionsfehler die
    Klasse tatsaechlich veraendern koennte)."""
    problems = []
    for key, dim, idx in BBOX_FIELDS:
        try:
            d = abs(float(src_ranges[dim][idx]) - float(dst_ranges[dim][idx]))
        except (KeyError, TypeError, ValueError):
            problems.append(f"Punkt-Extremwert '{key}' fehlt in Quelle oder Ziel.")
            continue
        if d > BBOX_TOLERANCE_M:
            problems.append(f"Punkt-Extremwert '{key}' weicht {d:.4f} m ab (Toleranz {BBOX_TOLERANCE_M} m).")

    src_class, dst_class = src_ranges.get("Classification"), dst_ranges.get("Classification")
    if not src_class or not dst_class or None in tuple(src_class) + tuple(dst_class):
        problems.append("Classification-Statistik fehlt in Quelle oder Ziel.")
    elif tuple(src_class) != tuple(dst_class):
        problems.append(
            f"Classification veraendert: Quelle min/max={src_class[0]}/{src_class[1]}, "
            f"Ziel min/max={dst_class[0]}/{dst_class[1]}"
        )

    header_problems = header_bbox_deviations(dst_metadata, dst_ranges)
    if header_problems:
        problems.append("Ziel-Header-BBox passt nicht zu den Ziel-Punkten: " + "; ".join(header_problems))
    return problems


# ****************************** Kernkonversion pro Tile ******************************
def convert_tile(src_path, dst_dir, target_scale=0.01, dry_run=False):
    """Konvertiert eine einzelne Tile. Gibt ein Ergebnis-Dict zurueck:
      {"status": "ok" | "skipped_already_migrated" | "warning" | "failed",
       "warnings": [...], "error": str oder None}
    Original wird NIE veraendert. Ziel wird nur bei vollstaendigem Erfolg
    atomar geschrieben (os.replace) - bei jedem Fehler bleibt eine evtl.
    vorhandene Zieldatei unangetastet.
    """
    name = os.path.basename(src_path)
    dst_path = os.path.join(dst_dir, name)
    result = {"status": "failed", "warnings": [], "error": None}

    try:
        easting_km, northing_km = parse_tile_from_filename(name)
    except ValueError as e:
        result["error"] = str(e)
        return result

    try:
        src_meta = pdal_metadata(src_path)
    except Exception as e:
        result["error"] = f"Quelldatei nicht lesbar (pdal info): {e}"
        return result

    try:
        result["warnings"].extend(
            check_tile_frame_plausibility(src_meta, easting_km, northing_km, name))
    except ValueError as e:
        result["error"] = str(e)
        return result

    src_md = src_meta.get("metadata") or {}
    if (src_md.get("global_encoding", 0) & 0x01) == 0:
        result["warnings"].append(
            f"{name}: global_encoding-Bit 0 (Adjusted Standard GPS Time) ist in der "
            f"Quelle NICHT gesetzt - Annahme ueber den GpsTime-Typ koennte nicht zutreffen."
        )

    if is_already_migrated(src_meta):
        # KEIN direkter log()-Aufruf hier (anders als frueher): convert_tile
        # laeuft unter workers>1 parallel in mehreren Threads (siehe
        # convert_folder), log() soll aber ausschliesslich seriell aus dem
        # Haupt-Thread heraus passieren (siehe _log_tile_result) - deshalb
        # nur im Status vermerkt, die Meldung wird dort ausgegeben.
        if not dry_run:
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copy2(src_path, dst_path)
        result["status"] = "skipped_already_migrated"
        return result

    offset_x, offset_y, offset_z = easting_km * 1000, northing_km * 1000, 0

    if dry_run:
        log(f"{name}: [DRY-RUN] wuerde konvertieren -> Offset ({offset_x},{offset_y},{offset_z}), "
            f"Scale {target_scale}, Ziel: {dst_path}")
        result["status"] = "warning" if result["warnings"] else "ok"
        return result

    os.makedirs(dst_dir, exist_ok=True)
    compression = "laszip" if name.lower().endswith(".laz") else None

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=os.path.splitext(name)[1], dir=dst_dir)
    os.close(tmp_fd)
    os.remove(tmp_path)  # writers.las soll die Datei selbst anlegen

    try:
        writer_opts = {
            "type": "writers.las",
            "filename": tmp_path,
            "minor_version": TARGET_MINOR_VERSION,
            "dataformat_id": TARGET_POINT_FORMAT,
            "scale_x": target_scale,
            "scale_y": target_scale,
            "scale_z": target_scale,
            "offset_x": offset_x,
            "offset_y": offset_y,
            "offset_z": offset_z,
            "global_encoding": TARGET_GLOBAL_ENCODING,
        }
        if compression:
            writer_opts["compression"] = compression

        # 'filters.stats' haengt sich als reiner Durchlauf-Filter (veraendert
        # keine Punkte) an den ohnehin noetigen Lesedurchlauf der Quelle an -
        # liefert deren tatsaechliche Min/Max-Werte (X/Y/Z, Classification)
        # praktisch gratis mit, ohne die Quelle dafuer ein zweites Mal
        # komplett einzulesen (siehe dimension_ranges_from_pipeline_metadata).
        pipeline = {"pipeline": [
            {"type": "readers.las", "filename": src_path},
            {"type": "filters.stats", "dimensions": ",".join(STATS_DIMENSIONS)},
            writer_opts,
        ]}
        pipeline_meta = run_pdal_pipeline(pipeline, capture_metadata=True)

        src_ranges = dimension_ranges_from_pipeline_metadata(pipeline_meta)
        if not all(dim in src_ranges for dim in STATS_DIMENSIONS):
            # Fallback, falls die Stage/Metadata unerwartet fehlt (z.B.
            # aeltere PDAL-Version) - dann ein separater Aufruf, damit die
            # Pruefung nie stillschweigend uebersprungen wird.
            try:
                src_ranges = pdal_dimension_ranges(src_path)
            except Exception as e:
                result["error"] = f"Statistik-Ermittlung (Quelle) fehlgeschlagen: {e}"
                return result

        stale_header = header_bbox_deviations(src_meta, src_ranges)
        if stale_header:
            result["warnings"].append(
                f"{name}: Header-BBox der Quelle passt nicht zu den eigenen Punkten "
                f"({'; '.join(stale_header)}) - Ziel-Header wird aus den Punkten neu berechnet."
            )

        n_stripped_vlrs = inject_reference_vlrs(tmp_path)
        if n_stripped_vlrs:
            result["warnings"].append(
                f"{name}: {n_stripped_vlrs} von PDAL aus der Quelle uebernommene(r) "
                f"'LASF_Projection'-VLR(s) entfernt (nicht autoritativ, siehe "
                f"inject_reference_vlrs-Docstring) - durch die Referenz-VLRs ersetzt."
            )

        dst_meta = pdal_metadata(tmp_path)
        problems = validate_target(src_meta, dst_meta)
        try:
            dst_ranges = pdal_dimension_ranges(tmp_path)
        except Exception as e:
            problems.append(f"Statistik-Ermittlung (Ziel) fehlgeschlagen: {e}")
        else:
            problems.extend(validate_point_ranges(src_ranges, dst_ranges, dst_meta))

        if problems:
            result["error"] = "; ".join(problems)
            return result

        os.replace(tmp_path, dst_path)
        tmp_path = None
        result["status"] = "warning" if result["warnings"] else "ok"
        return result

    except subprocess.CalledProcessError as e:
        result["error"] = f"PDAL-Fehler: {(e.stderr or '').strip()}"
        return result
    except Exception as e:
        result["error"] = str(e)
        return result
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# ****************************** Batch-Runner ******************************
def find_laz_files(input_dir, recursive):
    if recursive:
        for root, _dirs, files in os.walk(input_dir):
            for f in sorted(files):
                if f.lower().endswith(".laz"):
                    yield os.path.join(root, f)
    else:
        for f in sorted(os.listdir(input_dir)):
            full = os.path.join(input_dir, f)
            if os.path.isfile(full) and f.lower().endswith(".laz"):
                yield full


def _log_tile_result(name, result, summary):
    """Loggt das Ergebnis einer einzelnen Kachel und aktualisiert summary
    in-place. IMMER nur aus dem Haupt-Thread aufrufen (siehe convert_folder) -
    dadurch bleibt das Logging trotz paralleler Worker sauber seriell,
    ohne dass log() selbst thread-sicher gemacht werden muss."""
    log(f"\n[{name}]")
    for w in result["warnings"]:
        log(f"  [WARNUNG] {w}")

    if result["status"] == "skipped_already_migrated":
        log(f"  bereits LAS 1.4/PF6 mit korrektem CRS - wird unveraendert kopiert.")
        summary["skipped"] += 1
    elif result["status"] == "ok":
        log(f"  OK")
        summary["ok"] += 1
    elif result["status"] == "warning":
        log(f"  OK (mit Warnung)")
        summary["warning"] += 1
    else:
        log(f"  FEHLER: {result['error']}")
        summary["failed"] += 1
        summary["failed_files"].append((name, result["error"]))


def convert_folder(input_dir, output_dir, recursive=False, target_scale=0.01,
                    dry_run=False, workers=None):
    """Batch-Konversion aller .laz-Dateien in input_dir - wiederverwendbare
    Kernfunktion, sowohl fuer die CLI (main(), siehe unten) als auch fuer den
    Aufruf als Modul (siehe _osgeo_runner.py: laeuft dort IMMER automatisch
    vor Script 1, wenn im GUI GDS 'SB_DSM_PUNKTWOLKE' gewaehlt ist).

    Eine einzelne fehlgeschlagene Kachel bricht die Schleife NICHT ab und
    wirft KEINE Exception - der Aufrufer entscheidet anhand von
    summary['failed'], wie er reagiert.

    workers steuert die Anzahl gleichzeitig verarbeiteter Kacheln (Default
    None -> automatisch via _default_worker_count(), siehe dort). Die Kacheln
    sind voneinander unabhaengig (eigene Quelldatei, eigene Zieldatei, kein
    gemeinsamer Zustand), daher per ThreadPoolExecutor parallelisiert - NICHT
    per ProcessPoolExecutor/multiprocessing: dieses Modul wird von
    _osgeo_runner.py per importlib.util.spec_from_file_location dynamisch
    unter einem generischen Namen ('script_4') geladen und dabei bewusst
    NICHT in sys.modules eingetragen; multiprocessing muesste auf Windows
    (spawn-Methode) genau diesen Modulnamen in einem frischen Interpreter
    reimportieren koennen, um convert_tile zurueckzuholen, was in diesem
    Ladeszenario fehlschlaegt. Mit Threads entfaellt das Problem, da die
    eigentliche CPU-Arbeit ohnehin in den PDAL-Subprozessen steckt: jeder
    'subprocess.run'-Aufruf gibt den GIL waehrend des Wartens frei, wodurch
    mehrere pdal.exe-Prozesse echt parallel auf mehreren Kernen laufen
    koennen, ganz ohne das Pickling-/Modul-Identitaetsproblem.

    Gibt ein Zusammenfassungs-Dict zurueck:
      {"total", "ok", "warning", "skipped", "failed",
       "failed_files": [(name, error), ...]}
    """
    files = list(find_laz_files(input_dir, recursive))
    summary = {"total": len(files), "ok": 0, "warning": 0, "skipped": 0,
               "failed": 0, "failed_files": []}

    if not files:
        return summary

    if workers is None:
        workers = _default_worker_count()
    workers = max(1, min(workers, len(files)))

    if workers <= 1:
        for src_path in files:
            name = os.path.basename(src_path)
            result = convert_tile(src_path, output_dir, target_scale=target_scale, dry_run=dry_run)
            _log_tile_result(name, result, summary)
    else:
        log(f"Parallelisierung: {workers} gleichzeitige PDAL-Worker "
            f"(verfuegbare Kerne: {os.cpu_count()}).")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_name = {
                executor.submit(convert_tile, src_path, output_dir,
                                 target_scale, dry_run): os.path.basename(src_path)
                for src_path in files
            }
            for future in as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {"status": "failed", "warnings": [],
                              "error": f"Unerwarteter Fehler im Worker: {e}"}
                _log_tile_result(name, result, summary)

    log(f"\n=== Zusammenfassung: {summary['total']} verarbeitet, "
        f"{summary['ok']} gueltig, {summary['warning']} mit Warnung, "
        f"{summary['skipped']} bereits migriert (kopiert), {summary['failed']} fehlgeschlagen ===")

    if summary["failed_files"]:
        log("\nFehlgeschlagene Dateien:")
        for name, error in summary["failed_files"]:
            log(f"  - {name}: {error}")

    return summary


def main():
    global _log_file_handle

    parser = argparse.ArgumentParser(
        description="SB_DSM_PUNKTWOLKE: LAS 1.2 -> LAS 1.4 Batch-Vorkonversion (LV95/LN02).")
    parser.add_argument("--input-dir", required=True, help="Ordner mit Quell-Tiles (.laz)")
    parser.add_argument("--output-dir", required=True, help="Zielordner fuer konvertierte Tiles")
    parser.add_argument("--recursive", action="store_true", help="Input-Ordner rekursiv nach .laz durchsuchen")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, was getan wuerde - nichts schreiben")
    parser.add_argument("--target-scale", type=float, default=0.01,
                         help="Ziel-Scale in Metern (Default 0.01 = 1 cm, verlustbehaftete Rundung "
                              "gegenueber der Quelle mit Scale 0.001, siehe Modul-Docstring)")
    parser.add_argument("--log-file", help="Pfad fuer die Log-Datei (Default: <output-dir>/logs/...)")
    parser.add_argument("--workers", type=int, default=None,
                         help="Anzahl gleichzeitig verarbeiteter Kacheln (Default: automatisch, "
                              "siehe _default_worker_count - reserviert 2 Kerne, max. 8). "
                              "--workers 1 erzwingt seriellen Ablauf.")
    args = parser.parse_args()

    if args.workers is not None and args.workers < 1:
        parser.error("--workers muss >= 1 sein.")

    if os.path.abspath(args.input_dir) == os.path.abspath(args.output_dir):
        parser.error("--output-dir ist identisch mit --input-dir - Original darf nicht ueberschrieben werden.")
    if not os.path.isdir(args.input_dir):
        parser.error(f"--input-dir nicht gefunden: {args.input_dir}")

    log_path = args.log_file
    if not log_path and not args.dry_run:
        log_dir = os.path.join(args.output_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"LAS14upgrade_{datetime.now():%Y%m%d_%H%M%S}.log")

    if log_path:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        _log_file_handle = open(log_path, "w", encoding="utf-8")
        log(f"Log-Datei: {log_path}")

    log(f"=== SB_DSM_PUNKTWOLKE LAS 1.2 -> LAS 1.4 Batch-Vorkonversion ===")
    log(f"Input:  {args.input_dir}  (rekursiv: {args.recursive})")
    log(f"Output: {args.output_dir}")
    log(f"Ziel-Scale: {args.target_scale} m  (Quelle: 0.001 m - verlustbehaftete Rundung, siehe Docstring)")
    log(f"Dry-Run: {args.dry_run}")
    log(f"Worker: {args.workers if args.workers else f'automatisch ({_default_worker_count()} von {os.cpu_count()} Kernen)'}\n")

    if not list(find_laz_files(args.input_dir, args.recursive)):
        log(f"Keine .laz Dateien in {args.input_dir} gefunden.")
        sys.exit(1)

    summary = convert_folder(args.input_dir, args.output_dir, recursive=args.recursive,
                              target_scale=args.target_scale, dry_run=args.dry_run,
                              workers=args.workers)

    if _log_file_handle:
        _log_file_handle.close()

    sys.exit(1 if summary["failed"] else 0)


if __name__ == "__main__":
    main()
