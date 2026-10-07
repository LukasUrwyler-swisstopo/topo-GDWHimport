#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_false_nodata_dop.py

Vorkorrektur-Skript fuer DOP-Tiles (RGB 8bit, GeoTIFF mit .tfw-Begleitdatei).
Korrigiert "falsche" NoData-Pixel (0,0,0 oder 255,255,255, je nach
--nodata-value), welche durch zu starkes Histogramm-Stretch in dunklen
Schatten- bzw. hellen Ueberstrahlungsbereichen entstanden sind.

Dieses Skript korrigiert ausschliesslich die Pixelwerte im Tiff-Tile. Es
schreibt keine Flag Mask und macht keine GDWH-/STAC-Vorbereitung, das
passiert in nachgelagerten Skripten, auf Basis der hier bereits bereinigten
0,0,0-Werte. Es geht nicht um COG, sondern um die regulaeren GeoTIFF-Tiles
(.tif + .tfw), wie sie vor der eigentlichen COG-/Flag-Mask-Erstellung
vorliegen.

Hintergrund:
  - Echtes NoData in einem DOP-Tile besteht aus einer grossen zusammenhaengenden
    Pixelgruppe (>= 900 m²), deren Aussenkontur (Befliegungs-/Mosaik-
    Perimeter) ausserdem ueber eine laengere Strecke entlang eines Tile-Rands
    verlaeuft (>= 80 m Randkontakt).
  - "Falsches" NoData sind einzelne Pixel oder kleine Gruppen innerhalb der
    Nutzdaten, die durch die Radiometrie zufaellig auf den NoData-Wert
    gefallen sind (Schattenzonen bzw. Ueberstrahlung).
  - Sonderfall: grosse ueberstrahlte ("ausgebrannte") Gletscherflaechen
    koennen an einer Kachelgrenze liegen und erfuellen dann Groesse und
    Randkontakt. Unterschied liegt in der Form: ein Perimeter-Schnitt ist
    ein kompakter Block ohne Einschluesse mit glatter Kontur, ein
    Gletscher hat viele eingeschlossene 250-254er-Pixel (Spalten, Firn-
    strukturen) und einen zerfransten Rand (Stufe D, siehe classify_mask).
  - Ausnahme Rand-Tiles des Gesamt-Orthophotos (siehe find_missing_neighbors):
    grenzt dort ein ueberstrahlter Gletscher an echtes NoData, bilden beide
    eine einzige Gruppe, die nicht trennbar ist. Stufe D darf eine solche
    Gruppe dort nicht zu "falsch" erklaeren, sie bleibt echtes NoData -
    aber nur, wenn die Gruppe an eine Tile-Seite/-Ecke ohne Nachbar-Tile
    grenzt (offener Randkontakt). Gletscher, die nur an vorhandene Tiles
    grenzen, korrigiert Stufe D wie in inneren Tiles.

Vorgehen:
  1. Maske bilden: alle Pixel, bei denen R, G und B gleichzeitig dem
     NoData-Wert entsprechen.
  2. Connected-Component-Labeling auf dieser Maske (Standard: 8-Nachbarschaft).
  3. Klassifikation pro Gruppe, Stufen A-D (siehe classify_mask).
  4. Nur die Baender 1-3 (RGB) werden veraendert. Ein evtl. vorhandenes 4.
     Band (z.B. NIR/Alpha) wird unveraendert uebernommen.

Die Kernlogik (classify_mask) ist von der GDAL-I/O getrennt und wurde separat
mit synthetischen Testfaellen geprueft (Schwellenwert exakt, kleine Gruppe am
Rand, grosse Gruppe ohne Randkontakt, gemischtes Tile).

Benoetigt: GDAL Python-Bindings (osgeo.gdal), numpy, scipy
  -> im OSGeo4W/QGIS-Python-Environment normalerweise bereits vorhanden.

Verwendung:
  Einzelnes Tile:
    python fix_false_nodata_dop.py --input tile_001.tif --output tile_001_fixed.tif

  Ganzer Ordner (alle .tif):
    python fix_false_nodata_dop.py --input-dir ./dop_tiles --output-dir ./dop_tiles_fixed

  Tiles mit bereits vorhandener, falsch berechneter Flag Mask (Alpha-Band,
  internes Mask Band oder NoData-Tag) zuerst bereinigen und dann korrigieren:
    python fix_false_nodata_dop.py --input-dir ./dop_tiles_alt_maskiert \
        --output-dir ./dop_tiles_fixed --strip-existing-mask

  In place: Originale (tif + tfw) direkt durch die korrigierte Version
  ersetzen, mit Backup der Originale vorher:
    python fix_false_nodata_dop.py --input-dir ./dop_tiles --in-place --backup-dir ./dop_tiles_backup

  Optional CSV-Report aller klassifizierten Gruppen (Diagnose):
    python fix_false_nodata_dop.py --input-dir ./dop_tiles --output-dir ./dop_tiles_fixed --report report.csv
"""

import argparse
import csv
import os
import shutil
import sys
import tempfile

import numpy as np
from scipy import ndimage

try:
    from osgeo import gdal, osr
    gdal.UseExceptions()
except ImportError:
    gdal = None
    osr = None


# ---------------------------------------------------------------------------
# Fixe Korrekturwerte (keine GUI-/CLI-Parameter mehr, siehe Docstrings unten)
# ---------------------------------------------------------------------------

# "Falsche NoData" 255,255,255 (Ueberstrahlung/Gletscher, siehe classify_mask):
# fixe Verschiebung auf 254,254,254, statt frueher variablem --increment.
FALSE_NODATA_255_SHIFT = -1

# Schatten-Puffer (siehe _shadow_buffer_shift): alle Pixel, deren R-, G- und
# B-Wert gleichzeitig <= SHADOW_BUFFER_MAX_VALUE sind, gelten als potenzielle
# Schatten-Clipping-Pixel. Die Erhoehung richtet sich nach dem kleinsten
# Kanalwert des Pixels (Tiefe des "Schwarz-Einbruchs") - je naeher an exakt
# 0,0,0, desto groesser die Erhoehung. Das erzeugt einen weichen Uebergang
# statt eines scharfen Sprungs allein bei exakt 0,0,0.
SHADOW_BUFFER_MAX_VALUE = 5
SHADOW_BUFFER_TIERS = {0: 5, 1: 4, 2: 3, 3: 2, 4: 1, 5: 1}

# Stufe-A-Mindestgroesse (siehe classify_mask) als Flaeche statt fixer
# Pixelzahl: die fruehere fixe Schwelle (25000 px, bei 10cm GSD = 250 m²)
# war fuer alpines Gelaende zu knapp - ein einzelner ueberstrahlter
# Firn-/Schneefleck an einer (willkuerlichen) Kachelgrenze konnte das schon
# ueberschreiten. process_tile() rechnet dies pro Tile anhand der
# tatsaechlichen Pixelgroesse aus dem GeoTransform in eine Pixelzahl um,
# damit die Schwelle unabhaengig von der GSD dieselbe reale Flaeche meint.
DEFAULT_MIN_NODATA_AREA_M2 = 900.0

# Stufe-C-Mindestrandkontakt (siehe classify_mask), analog Stufe A in Metern
# statt fixer Pixelzahl (frueher 100 px = nur 10 m bei 10cm GSD).
# Herleitung: echtes NoData wird durch eine (annaehernd) gerade
# Perimeterlinie begrenzt. Bei >= 900 m² ist der kleinstmoegliche
# Randkontakt das gleichschenklige Eck-Dreieck: 2 * sqrt(2 * 900) ≈ 85 m.
# 80 m laesst etwas Reserve fuer geknickte Perimeterlinien.
DEFAULT_MIN_BORDER_CONTACT_M = 80.0

# Stufe D (siehe classify_mask): Form-Pruefung gegen ueberstrahlte
# Gletscherflaechen. Werte aus Testdaten 2019_BIS_HOHLICHT_TURTMANN
# (05.10.2026): echtes NoData hole_ratio <= 0.0001 / rough <= 1.005,
# Gletscher hole_ratio >= 0.035 / rough >= 2.28.
DEFAULT_MIN_HOLE_RATIO = 0.01
DEFAULT_MIN_ROUGHNESS = 1.5


# ---------------------------------------------------------------------------
# Kernlogik (ohne GDAL-Abhaengigkeit, separat testbar)
# ---------------------------------------------------------------------------

def _group_shape_metrics(group_crop):
    """
    Form-Kennzahlen fuer Stufe D, berechnet nur auf dem Bounding-Box-
    Ausschnitt einer Gruppe (kein Vollbild-Durchgang).

    group_crop: bool-Ausschnitt, True = Pixel der Gruppe.

    Rueckgabe: (hole_px, roughness)
      hole_px   : Pixel in Loechern - Nicht-Gruppen-Bereiche, die vollstaendig
                  von der Gruppe umschlossen sind. Ein zur Box-Kante (und damit
                  ggf. zum Tile-Rand) offener Bereich ist kein Loch.
      roughness : Konturlaenge (Anzahl 4er-Pixelkanten) der gefuellten Gruppe
                  / Umfang der Bounding-Box. Fuer jede orthogonal-konvexe
                  Form (Block, Keil, Perimeter-Schnitt, auch treppenfoermig)
                  exakt 1.0, fuer zerfranste Raender deutlich groesser.
    """
    padded = np.pad(group_crop, 1, constant_values=False)
    bg_labels, n_bg = ndimage.label(~padded)
    # Hintergrund-Komponenten, die den (aufgefuellten) Rand beruehren = aussen
    outside = np.zeros(n_bg + 1, dtype=bool)
    outside[bg_labels[0, :]] = True
    outside[bg_labels[-1, :]] = True
    outside[bg_labels[:, 0]] = True
    outside[bg_labels[:, -1]] = True
    outside[0] = True  # Label 0 = Gruppe selbst
    holes = ~outside[bg_labels]
    filled = padded | holes

    contour = (np.count_nonzero(filled[:, 1:] != filled[:, :-1])
               + np.count_nonzero(filled[1:, :] != filled[:-1, :]))
    h, w = group_crop.shape
    return int(holes.sum()), contour / (2.0 * (h + w))


def parse_tile_key(filename):
    """
    TileKey als (E, N) in km aus dem Dateinamen: die zwei Namensteile direkt
    vor 'LV95', identisch zu extract_tile_lv95 in Script 1
    (..._2601_1136_LV95.tif -> (2601, 1136)). None, falls nicht lesbar.
    """
    parts = os.path.splitext(os.path.basename(filename))[0].split("_")
    try:
        idx = parts.index("LV95")
        if idx < 2:
            return None
        return int(parts[idx - 2]), int(parts[idx - 1])
    except ValueError:
        return None


# Nachbar-Richtungen als (dE, dN) in Kachelschritten, dN=+1 = Norden
ALL_NEIGHBORS = frozenset((de, dn) for de in (-1, 0, 1) for dn in (-1, 0, 1) if de or dn)
_NEIGHBOR_NAMES = {(0, 1): "N", (1, 1): "NO", (1, 0): "O", (1, -1): "SO",
                   (0, -1): "S", (-1, -1): "SW", (-1, 0): "W", (-1, 1): "NW"}


def find_missing_neighbors(filenames):
    """
    Bestimmt pro Tile, welche der 8 Nachbar-Tiles (inkl. Diagonalen) im
    Tile-Set fehlen. Rand-Tile des Gesamt-Orthophotos = mindestens ein
    Nachbar fehlt. Vollstaendig leere Tiles (is_tile_empty) muss der
    Aufrufer vorher entfernen, sonst zaehlen sie als vorhandener Nachbar.
    Die Diagonalen zaehlen mit, weil echtes NoData auch nur in einer
    Tile-Ecke liegen kann (fehlendes Diagonal-Tile, Perimeter schneidet die
    Ecke ab). Die Kachelweite wird aus den TileKeys selbst bestimmt
    (kleinster Abstand), damit nicht nur 1-km-Kacheln funktionieren.

    Rueckgabe: dict Dateiname -> frozenset der fehlenden Richtungen (dE, dN),
    leer = inneres Tile. Tiles ohne lesbaren TileKey: ALL_NEIGHBORS (alle
    Seiten offen, im Zweifel echt).
    """
    keys = {fn: parse_tile_key(fn) for fn in filenames}
    present = {k for k in keys.values() if k is not None}

    def _step(values):
        vals = sorted(set(values))
        return min((b - a for a, b in zip(vals, vals[1:])), default=1)

    step_e = _step(k[0] for k in present)
    step_n = _step(k[1] for k in present)

    missing = {}
    for fn, key in keys.items():
        if key is None:
            missing[fn] = ALL_NEIGHBORS
            continue
        e, n = key
        missing[fn] = frozenset(
            (de, dn) for de, dn in ALL_NEIGHBORS
            if (e + de * step_e, n + dn * step_n) not in present
        )
    return missing


def describe_neighbors(directions):
    """Richtungen als Text fuers Log, z.B. 'N,NO,O'."""
    return ",".join(name for d, name in _NEIGHBOR_NAMES.items() if d in directions)


def _open_border_labels(labeled, missing_neighbors):
    """
    Labels aller Randpixel, hinter denen kein Nachbar-Tile liegt (offener
    Randkontakt). Setzt ein nordorientiertes Raster voraus (Zeile 0 =
    Norden, prueft process_tile). Seitenpixel: offen, wenn der direkte
    Nachbar fehlt. Eckpixel: offen, wenn eines der drei Tiles an dieser Ecke
    fehlt (zwei seitliche + diagonales).
    """
    parts = [px for d, px in (((0, 1), labeled[0, 1:-1]), ((0, -1), labeled[-1, 1:-1]),
                              ((-1, 0), labeled[1:-1, 0]), ((1, 0), labeled[1:-1, -1]))
             if d in missing_neighbors]
    for (de, dn), (row, col) in (((-1, 1), (0, 0)), ((1, 1), (0, -1)),
                                 ((-1, -1), (-1, 0)), ((1, -1), (-1, -1))):
        if {(de, 0), (0, dn), (de, dn)} & missing_neighbors:
            parts.append(np.atleast_1d(labeled[row, col]))
    if not parts:
        return np.empty(0, dtype=labeled.dtype)
    return np.concatenate(parts)


def classify_mask(mask_zero, threshold=25000, connectivity=8,
                  min_border_contact=100,
                  min_hole_ratio=DEFAULT_MIN_HOLE_RATIO,
                  min_roughness=DEFAULT_MIN_ROUGHNESS,
                  enable_shape_check=True, missing_neighbors=frozenset()):
    """
    Klassifiziert zusammenhaengende Gruppen von True-Werten in mask_zero
    als "echtes NoData" (bleibt) oder "falsches NoData" (wird korrigiert).

    Vierstufige Pruefung, jede Stufe nur, wenn die vorherige erfuellt ist:
      A) Groesse >= threshold?        Sonst sofort "falsch".
      B) Beruehrt die Gruppe ueberhaupt einen Tile-Rand?  Sonst "falsch".
      C) Randkontakt (Summe der Gruppenpixel auf allen vier Tile-Kanten
         zusammen - deckt auch Eck-Faelle ab) >= min_border_contact?
         Sonst "falsch".
      D) Form: hole_ratio >= min_hole_ratio UND roughness >= min_roughness?
         Dann trotz A-C "falsch" (ueberstrahlte Gletscherflaeche an einer
         Kachelgrenze), sonst "echt".
           hole_ratio = Pixel in Einschluessen / Gruppengroesse. Echtes
             NoData ist ein geschlossener Block, ein Gletscher enthaelt
             viele 250-254er-Pixel (Spalten, Firnstrukturen).
           roughness = Konturlaenge / Bounding-Box-Umfang (siehe
             _group_shape_metrics). Perimeter-Schnitte ~1.0, Gletscher-
             raender zerfranst.
         Beide Kriterien muessen zutreffen (UND): im Zweifel bleibt eine
         Gruppe echtes NoData. Grund ist der Vorfall WALLIS_SAASTAL
         (05.08.2026): die fruehere Stufe D (Grauwert-Gradient am inneren
         Rand) nahm weich ausgeblendete (gefeatherte) echte NoData-Flaechen
         faelschlich als Ueberstrahlung an und wurde entfernt. Die neue
         Stufe D wertet nur die Geometrie der exakten NoData-Pixel aus,
         nicht die Grauwerte daneben.

         Ausnahme Rand-Tile (missing_neighbors nicht leer, siehe
         find_missing_neighbors): dort bleibt eine Gruppe, die A-C erfuellt
         und an eine offene Tile-Seite/-Ecke grenzt (_open_border_labels),
         echt, auch wenn D "Gletscher" sagt (log_rows: edge_override=True).
         Grenzt ein ueberstrahlter Gletscher (255) direkt an echtes NoData,
         bilden beide eine einzige Gruppe; die Einschluesse und der
         zerfranste Rand des Gletscherteils erklaerten sonst die ganze
         Gruppe als falsch -> das echte NoData wurde 254 und als weisse
         Flaeche sichtbar (Vorfall 2019_BIS_HOHLICHT_TURTMANN, 06.10.2026).
         Die Grenze zwischen beiden Teilen ist aus den Pixelwerten nicht
         bestimmbar (beide exakt NoData-Wert), die ausgebrannten
         Gletscherpixel tragen ohnehin keine Bildinformation.
         Grenzt die Gruppe nur an vorhandene Nachbar-Tiles, kann sie kein
         Perimeter-NoData enthalten, das dort beginnt -> D gilt wie in
         inneren Tiles (Loch im Gletscher am Rand-Tile, 07.10.2026).
         In inneren Tiles gilt D unveraendert.

    min_border_contact ist hier in Pixeln. process_tile rechnet ihn pro Tile
    aus DEFAULT_MIN_BORDER_CONTACT_M um.

    Rueckgabe:
        increment_mask : bool-Array, True = diese Pixel sollen korrigiert werden
        log_rows       : Liste von Dicts pro Gruppe (fuer Report/Debug)
    """
    structure = np.ones((3, 3), dtype=int) if connectivity == 8 else None
    labeled, n_features = ndimage.label(mask_zero, structure=structure)

    if n_features == 0:
        return np.zeros_like(mask_zero, dtype=bool), []

    sizes = np.bincount(labeled.ravel(), minlength=n_features + 1)

    # Randkontakt nur berechnen, wenn ueberhaupt eine Gruppe Stufe A erfuellt
    # (im Regelfall keine). Eckpixel nur einmal zaehlen.
    is_candidate = sizes >= threshold
    is_candidate[0] = False
    border_contact_counts = None
    open_contact_counts = None
    slices = None
    if is_candidate.any():
        edge = np.concatenate([labeled[0, :], labeled[-1, :],
                               labeled[1:-1, 0], labeled[1:-1, -1]])
        border_contact_counts = np.bincount(edge[edge > 0], minlength=n_features + 1)
        slices = ndimage.find_objects(labeled)
        if missing_neighbors:
            open_edge = _open_border_labels(labeled, missing_neighbors)
            open_contact_counts = np.bincount(open_edge[open_edge > 0],
                                              minlength=n_features + 1)

    # Entscheid pro Label als Nachschlagetabelle: die Korrekturmaske entsteht
    # am Ende in einem einzigen Durchgang (false_lut[labeled]) statt einer
    # Vollbild-Maske pro Gruppe - bei Kacheln mit zehntausenden kleinen
    # Gruppen (Gletscher/Schnee) sonst O(Gruppen x Pixel).
    false_lut = np.ones(n_features + 1, dtype=bool)
    false_lut[0] = False

    log_rows = []
    for label_id in range(1, n_features + 1):
        size = int(sizes[label_id])
        border_contact_px = None
        touches_border = None
        hole_ratio = None
        roughness = None
        open_contact_px = None
        edge_override = None

        if not is_candidate[label_id]:
            # Stufe A nicht erfuellt
            decision = "false_nodata"
        else:
            border_contact_px = int(border_contact_counts[label_id])
            touches_border = border_contact_px > 0
            if open_contact_counts is not None:
                open_contact_px = int(open_contact_counts[label_id])
            if not (touches_border and border_contact_px >= min_border_contact):
                # Stufe B oder C nicht erfuellt
                decision = "false_nodata"
            elif not enable_shape_check:
                decision = "real_nodata"
            else:
                sl = slices[label_id - 1]
                hole_px, roughness = _group_shape_metrics(labeled[sl] == label_id)
                hole_ratio = hole_px / size
                looks_like_glacier = bool(hole_ratio >= min_hole_ratio
                                          and roughness >= min_roughness)
                # Rand-Tile mit offenem Randkontakt: Gletscher + echtes
                # NoData evtl. eine Gruppe, dort im Zweifel echt (siehe
                # Docstring)
                edge_override = looks_like_glacier and bool(open_contact_px)
                if looks_like_glacier and not edge_override:
                    decision = "false_nodata"
                else:
                    decision = "real_nodata"

        if decision == "real_nodata":
            false_lut[label_id] = False

        log_rows.append({
            "label_id": label_id,
            "size_px": size,
            "touches_border": touches_border,
            "border_contact_px": border_contact_px,
            "hole_ratio": hole_ratio,
            "roughness": roughness,
            "open_contact_px": open_contact_px,
            "edge_override": edge_override,
            "decision": decision,
        })

    return false_lut[labeled], log_rows


def _shadow_buffer_shift(band_arrays_rgb):
    """
    Bestimmt Maske und Erhoehungsbetrag fuer die Schatten-Puffer-Korrektur
    (siehe SHADOW_BUFFER_TIERS oben).

    Jedes Pixel, bei dem R, G und B gleichzeitig <= SHADOW_BUFFER_MAX_VALUE
    sind, ist ein Kandidat. Der Erhoehungsbetrag richtet sich nach dem
    kleinsten der drei Kanalwerte (min(R,G,B)) - je dunkler, desto groesser
    die Erhoehung. Damit werden nicht nur exakte 0,0,0-Pixel angehoben,
    sondern auch ihre nahe-schwarzen Nachbarn (z.B. 1,2,2), gestuft
    abnehmend. Ohne diesen Puffer bliebe der direkte Nachbar eines von
    0,0,0 auf 5,5,5 angehobenen Pixels unveraendert bei z.B. 1,2,2 - ein
    unnatuerlicher Sprung.

    band_arrays_rgb: Liste der drei RGB-Baender (Original-Pixelwerte, vor
    jeder Veraenderung in diesem Verarbeitungsschritt).

    Rueckgabe:
        mask  : bool-Array, True wo die Korrektur greift
        shift : int32-Array (gleiche Form), Erhoehungsbetrag pro Pixel
                (nur an Stellen mask=True gueltig, sonst 0)
    """
    stacked = np.stack([b.astype(np.int32) for b in band_arrays_rgb], axis=0)
    channel_max = stacked.max(axis=0)
    channel_min = stacked.min(axis=0)
    mask = channel_max <= SHADOW_BUFFER_MAX_VALUE

    tier_lookup = np.array(
        [SHADOW_BUFFER_TIERS[v] for v in range(SHADOW_BUFFER_MAX_VALUE + 1)],
        dtype=np.int32,
    )
    shift = np.zeros(mask.shape, dtype=np.int32)
    shift[mask] = tier_lookup[channel_min[mask]]
    return mask, shift


def _false_nodata_correction(band_arrays_rgb, mask_zero, increment_mask, nodata_value):
    """
    Bestimmt Maske und Verschiebungsbetrag fuer die "falsche NoData"-
    Korrektur (ersetzt die frueher hier verwendete flache +/-increment-
    Anhebung durch feste, nicht mehr per GUI/CLI konfigurierbare Werte).

    nodata_value == 255 (Ueberstrahlung/Gletscher):
      Fixe Verschiebung FALSE_NODATA_255_SHIFT (-1, also 255 -> 254) auf
      allen Pixeln der von classify_mask als "falsch" eingestuften
      255,255,255-Gruppen (increment_mask).

    nodata_value == 0 (Schatten-Clipping):
      Statt nur exakte 0,0,0-Pixel einer falschen Gruppe pauschal
      anzuheben, greift hier die gestufte Schatten-Puffer-Korrektur
      (_shadow_buffer_shift) - deckt auch die nahe-schwarzen Nachbarpixel
      ab, unabhaengig davon, ob sie ueberhaupt Teil einer von classify_mask
      erkannten Gruppe sind (die meisten davon sind es nicht, weil sie
      nicht exakt 0,0,0 sind). Einzige Ausnahme: Pixel, die exakt
      0,0,0 sind UND zu einer von classify_mask als "echt" eingestuften
      Gruppe gehoeren (mask_zero abzueglich increment_mask), bleiben
      unangetastet - sonst wuerde die nachgelagerte, wertbasierte Erkennung
      von echtem NoData (exakt 0,0,0) brechen.

    Rueckgabe: (mask, shift) - shift ist ein int32-Array, an mask=False
    Stellen 0 (ungueltig).
    """
    if nodata_value == 255:
        shift = np.zeros(mask_zero.shape, dtype=np.int32)
        shift[increment_mask] = FALSE_NODATA_255_SHIFT
        return increment_mask, shift

    buffer_mask, buffer_shift = _shadow_buffer_shift(band_arrays_rgb)
    real_zero_mask = mask_zero & ~increment_mask
    mask = buffer_mask & ~real_zero_mask
    shift = np.where(mask, buffer_shift, 0).astype(np.int32)
    return mask, shift


# ---------------------------------------------------------------------------
# GDAL I/O
# ---------------------------------------------------------------------------

_WRITE_CHUNK_ROWS = 1000


def _write_band_chunked(out_band, arr, chunk_rows=_WRITE_CHUNK_ROWS):
    """
    Schreibt arr zeilenweise in Bloecken statt in einem einzigen WriteArray-
    Aufruf (analog chunk_rows in Script 1s _compute_nodata_mask). Das
    Connected-Component-Labeling selbst braucht zwingend das komplette
    Array (kann nicht gechunkt werden, ohne den Algorithmus neu zu bauen),
    aber das Schreiben danach ist unabhaengig davon und profitiert bei
    sehr grossen Tiles von kleineren, aufeinanderfolgenden Schreibzugriffen
    statt einem einzelnen sehr grossen.
    """
    y_size = arr.shape[0]
    for y_off in range(0, y_size, chunk_rows):
        rows = min(chunk_rows, y_size - y_off)
        out_band.WriteArray(arr[y_off:y_off + rows, :], 0, y_off)


def _copy_sidecar_tfw(src_path, dst_path):
    """
    Kopiert eine vorhandene .tfw-Begleitdatei vom Input 1:1 zum Output
    (gleicher Basisname wie dst_path). Original-Werte bleiben so exakt
    erhalten, statt aus den internen GDAL-Tags neu berechnet zu werden.
    Gibt den Zielpfad zurueck, oder None falls keine .tfw gefunden wurde.
    """
    base_src, _ = os.path.splitext(src_path)
    base_dst, _ = os.path.splitext(dst_path)
    for ext in (".tfw", ".TFW"):
        tfw_src = base_src + ext
        if os.path.isfile(tfw_src):
            tfw_dst = base_dst + ".tfw"
            dst_dir = os.path.dirname(tfw_dst)
            if dst_dir:
                os.makedirs(dst_dir, exist_ok=True)
            shutil.copyfile(tfw_src, tfw_dst)
            return tfw_dst
    return None


def is_tile_empty(path, nodata_value, chunk_rows=512):
    """
    True, wenn in den Baendern 1-3 jedes Pixel exakt nodata_value ist
    (vollstaendig leeres Tile, z.B. ganz ausserhalb des Perimeters
    mitgeliefert). Liest blockweise und bricht beim ersten Nutzdaten-Pixel
    ab - bei einem normalen Tile genuegt meist der erste Block, nur leere
    Tiles werden ganz gelesen.
    """
    if gdal is None:
        raise RuntimeError("GDAL Python-Bindings (osgeo.gdal) nicht gefunden.")
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds.RasterCount < 3:
        raise RuntimeError(f"{path}: erwarte mindestens 3 Baender (RGB), gefunden: {ds.RasterCount}")
    xsize, ysize = ds.RasterXSize, ds.RasterYSize
    bands = [ds.GetRasterBand(i) for i in (1, 2, 3)]
    for y_off in range(0, ysize, chunk_rows):
        rows = min(chunk_rows, ysize - y_off)
        for band in bands:
            if np.any(band.ReadAsArray(0, y_off, xsize, rows) != nodata_value):
                return False
    return True


def pixel_thresholds(geotransform):
    """
    Rechnet die Stufe-A-Mindestflaeche (DEFAULT_MIN_NODATA_AREA_M2) und den
    Stufe-C-Mindestrandkontakt (DEFAULT_MIN_BORDER_CONTACT_M) anhand der
    Pixelgroesse des Tiles in Pixel um. Rueckgabe: (threshold_px,
    min_border_contact_px).
    """
    pixel_area_m2 = abs(geotransform[1] * geotransform[5])
    threshold = max(1, round(DEFAULT_MIN_NODATA_AREA_M2 / pixel_area_m2))
    min_border_contact = max(1, round(DEFAULT_MIN_BORDER_CONTACT_M / pixel_area_m2 ** 0.5))
    return threshold, min_border_contact


def process_tile(src_path, dst_path, threshold=None,
                  connectivity=8, write_tfw=False,
                  strip_existing_mask=False, fallback_epsg=2056,
                  nodata_value=0, write_mask=False,
                  rewrite_real_nodata_to_zero=False, min_border_contact=None,
                  min_hole_ratio=DEFAULT_MIN_HOLE_RATIO,
                  min_roughness=DEFAULT_MIN_ROUGHNESS,
                  enable_shape_check=True, missing_neighbors=frozenset()):
    """
    Liest ein RGB-Tile, korrigiert falsche NoData-Pixel und schreibt das
    Ergebnis nach dst_path. Gibt Zusammenfassungszahlen und alle
    klassifizierten Gruppen zurueck (fuer den CSV-Report/Diagnose).

    threshold:
      Stufe-A-Mindestgroesse fuer classify_mask, in Pixeln. None (Default)
      -> wird pro Tile aus dem GeoTransform berechnet, sodass sie
      DEFAULT_MIN_NODATA_AREA_M2 (900 m²) entspricht, unabhaengig von der
      GSD des Tiles (siehe Konstante oben). Explizit gesetzter Wert
      uebersteuert die Automatik (z.B. fuer Tests/Tuning).

    nodata_value:
      Der zu korrigierende NoData-Zielwert (0 -> schwarz, 255 -> weiss),
      z.B. aus der GUI-Wahl "NoData der Quelldaten" uebernommen. Die
      "falsche NoData"-Korrektur (siehe _false_nodata_correction) verwendet
      dafuer feste, nicht mehr konfigurierbare Werte: bei nodata_value=255
      FALSE_NODATA_255_SHIFT (-1, 255 -> 254); bei nodata_value=0 die
      gestufte Schatten-Puffer-Korrektur (SHADOW_BUFFER_TIERS).

    min_border_contact:
      Stufe-C-Mindestrandkontakt fuer classify_mask, in Pixeln. None
      (Default) -> pro Tile aus der Pixelgroesse berechnet, sodass er
      DEFAULT_MIN_BORDER_CONTACT_M (80 m) entspricht. Explizit gesetzter
      Wert uebersteuert die Automatik.

    min_hole_ratio / min_roughness / enable_shape_check:
      Stufe D (Form-Pruefung gegen ueberstrahlte Gletscherflaechen an
      Kachelgrenzen), siehe classify_mask.

    missing_neighbors:
      Fehlende Nachbar-Tiles (siehe find_missing_neighbors). Nicht leer =
      Rand-Tile des Gesamt-Orthophotos: Stufe D kann dann Gruppen mit
      offenem Randkontakt nicht mehr zu "falsch" erklaeren, siehe
      classify_mask. Default leer (inneres Tile) - die Nachbar-Info kennt
      nur der Aufrufer, der alle Tiles sieht. Ist das Raster nicht
      nordorientiert, lassen sich die Tile-Seiten nicht zuordnen -> alle
      Seiten gelten als offen (Verhalten wie vor dem 07.10.2026).

    write_mask:
      Nur zusammen mit strip_existing_mask=True unterstuetzt. Schreibt direkt
      im selben Schreibvorgang eine interne Flag Mask (GDAL_TIFF_INTERNAL_MASK,
      analog tag_mask_on_raster in Script 1), statt das nachgelagerten Skripten
      zu ueberlassen. Die Maske ist aequivalent zu einer Neuberechnung von
      _compute_nodata_mask() auf der bereits korrigierten Datei (echtes
      NoData = Pixel, die nach der Korrektur weiterhin nodata_value sind),
      spart aber einen zusaetzlichen vollstaendigen Lese-/Schreibdurchgang,
      weil die Pixel hier schon im Speicher vorliegen. Der NoData-GDAL-Tag
      wird bewusst NICHT gesetzt (bleibt Aufgabe des nachgelagerten Skripts,
      z.B. wegen GDS-spezifischer Normalisierung des Tag-Werts).

    rewrite_real_nodata_to_zero:
      Nur wirksam zusammen mit write_mask=True und nodata_value=255 (historische
      DOP-Tiles mit weissem NoData 255,255,255). Setzt die RGB-Baender (1-3)
      an allen als "echtes NoData" klassifizierten Pixeln (real_nodata_mask,
      also NICHT die soeben korrigierten "falschen" Gruppen) zusaetzlich
      direkt auf 0,0,0. Nutzt dieselben, bereits im Speicher vorliegenden
      Pixelarrays - kein zusaetzlicher Lese-/Schreibdurchgang. Ziel: Pixelwerte,
      Flag Mask und NoData-Tag/XML sind danach durchgehend konsistent auf 0
      normalisiert, nicht nur Tag/XML wie bisher.

      Schatten-Pixel-Schutz: unmittelbar VOR diesem 255->0-Wechsel laeuft die
      gestufte Schatten-Puffer-Korrektur (_shadow_buffer_shift, siehe dort)
      global ueber die ganze Kachel - alle zu diesem Zeitpunkt nahe-schwarzen
      Pixel (R,G,B <= SHADOW_BUFFER_MAX_VALUE, normale, sehr dunkle
      Nutzdaten-Schattenpixel - echtes NoData steht ja noch bei
      255,255,255) werden je nach Tiefe des Schwarz-Einbruchs gestuft
      angehoben (SHADOW_BUFFER_TIERS). Ohne diesen Schritt waeren exakte
      0,0,0-Schattenpixel nach dem Wechsel wertidentisch mit echtem NoData;
      da manche Software den NoData-Tag wertbasiert statt nur ueber die Flag
      Mask auswertet, wuerden sie sonst faelschlich als NoData/transparent
      behandelt. Die Stufung (statt einer einzigen festen Anhebung) vermeidet
      ausserdem einen sichtbaren Sprung an der Kante der angehobenen Zone.

    .tfw-Handling:
      - Existiert neben src_path eine .tfw-Datei, wird diese unveraendert
        zum Output kopiert (bevorzugt, exakte Werte).
      - Existiert keine .tfw beim Input und write_tfw=True, wird stattdessen
        via GDAL-Creation-Option eine .tfw aus den internen Tags erzeugt.

    strip_existing_mask:
      Fuer Tiles, die bereits eine (falsch berechnete) Flag Mask enthalten,
      z.B. ein Alpha-/4. Band, ein internes GDAL Mask Band oder einen
      NoData-Metadaten-Eintrag. In diesem Modus wird die Ausgabedatei NICHT
      per CreateCopy geklont (das wuerde die alte Maske mitkopieren),
      sondern komplett neu aufgebaut: nur die 3 RGB-Baender, kein NoData-Tag,
      kein Mask Band. Das entfernt jede Art von altem Flag-Mask-Mechanismus,
      unabhaengig davon wie er gespeichert war, weil er schlicht nicht
      mitgenommen wird. Georeferenzierung (Geotransform, Projektion) wird
      manuell vom Quellfile uebernommen; falls keine Projektion im Quellfile
      steht, wird ersatzweise fallback_epsg gesetzt (Default: 2056) und das
      wird geloggt.
    """
    if gdal is None:
        raise RuntimeError(
            "GDAL Python-Bindings (osgeo.gdal) nicht gefunden. "
            "Im OSGeo4W-Shell-Python bzw. QGIS-Python-Environment ausfuehren."
        )

    ds = gdal.Open(src_path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"Kann Datei nicht oeffnen: {src_path}")

    n_bands = ds.RasterCount
    if n_bands < 3:
        raise RuntimeError(
            f"{src_path}: erwarte mindestens 3 Baender (RGB), gefunden: {n_bands}"
        )

    xsize = ds.RasterXSize
    ysize = ds.RasterYSize

    geotransform = ds.GetGeoTransform()
    auto_threshold, auto_border_contact = pixel_thresholds(geotransform)
    if threshold is None:
        threshold = auto_threshold
    if min_border_contact is None:
        min_border_contact = auto_border_contact

    # Seiten-Zuordnung in _open_border_labels setzt Zeile 0 = Norden voraus
    north_up = (geotransform[1] > 0 and geotransform[5] < 0
                and geotransform[2] == 0 and geotransform[4] == 0)
    if missing_neighbors and not north_up:
        missing_neighbors = ALL_NEIGHBORS

    band_arrays = [ds.GetRasterBand(i).ReadAsArray() for i in range(1, n_bands + 1)]
    dtype = band_arrays[0].dtype
    gdal_dtype = ds.GetRasterBand(1).DataType

    mask_zero = np.ones((ysize, xsize), dtype=bool)
    for b in band_arrays[:3]:
        mask_zero &= (b == nodata_value)

    increment_mask, log_rows = classify_mask(
        mask_zero,
        threshold=threshold,
        connectivity=connectivity,
        min_border_contact=min_border_contact,
        min_hole_ratio=min_hole_ratio,
        min_roughness=min_roughness,
        enable_shape_check=enable_shape_check,
        missing_neighbors=missing_neighbors,
    )
    # group_rows: alle klassifizierten Gruppen dieses Tiles (fuer --report/
    # Diagnose).
    group_rows = log_rows

    # "Falsche NoData"-Korrektur (siehe _false_nodata_correction): fixer
    # -1-Shift bei nodata_value=255, gestufte Schatten-Puffer-Korrektur bei
    # nodata_value=0. correction_mask/-shift werden auf den noch
    # unveraenderten Original-Pixelwerten berechnet.
    correction_mask, correction_shift = _false_nodata_correction(
        band_arrays[:3], mask_zero, increment_mask, nodata_value
    )

    driver = gdal.GetDriverByName("GTiff")
    sidecar_copied = _copy_sidecar_tfw(src_path, dst_path)

    if strip_existing_mask:
        # Komplett neu aufbauen, nur 3 RGB-Baender, keine alte Maske/NoData
        projection_wkt = ds.GetProjection()
        used_fallback_epsg = False
        if not projection_wkt:
            if osr is None:
                raise RuntimeError("osgeo.osr nicht verfuegbar, fuer EPSG-Fallback benoetigt.")
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(fallback_epsg)
            projection_wkt = srs.ExportToWkt()
            used_fallback_epsg = True

        if write_mask:
            # Muss VOR driver.Create() gesetzt werden, damit die Maske als
            # interne 1-bit-DEFLATE-Maske im TIFF selbst landet (analog
            # tag_mask_on_raster in Script 1), statt als externe .msk-Datei.
            gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", "YES")

        create_options = ["TFW=YES"] if (write_tfw and not sidecar_copied) else []
        out_ds = driver.Create(dst_path, xsize, ysize, 3, gdal_dtype, options=create_options)
        out_ds.SetGeoTransform(geotransform)
        out_ds.SetProjection(projection_wkt)

        # Echtes NoData nach der Korrektur = Pixel, die weiterhin nodata_value
        # sind (mask_zero abzueglich der soeben hochgesetzten "falschen"
        # Pixel). Aequivalent zu einer Neuberechnung von _compute_nodata_mask()
        # auf der korrigierten Datei, aber ohne zusaetzlichen Lesedurchgang.
        real_nodata_mask = mask_zero & ~increment_mask
        do_rewrite = rewrite_real_nodata_to_zero and nodata_value == 255
        has_correction = correction_mask.any()
        has_rewrite = do_rewrite and real_nodata_mask.any()

        # Schatten-Pixel-Schutz (nur bei nodata_value=255 mit aktivem
        # rewrite_real_nodata_to_zero, siehe Docstring oben): zu diesem
        # Zeitpunkt stehen alle Original-Pixel noch unveraendert im Speicher,
        # echtes NoData ist also weiterhin 255,255,255 - jedes nahe-schwarze
        # Pixel (siehe _shadow_buffer_shift) kann daher nur ein ganz
        # normaler, sehr dunkler Nutzdaten-Schattenpixel sein (nicht Teil
        # von mask_zero/real_nodata_mask, die pruefen ja auf 255). Ohne
        # diese Anhebung waeren exakte 0,0,0-Schattenpixel nach dem gleich
        # folgenden echten NoData-Wechsel (255->0) wertidentisch mit echtem
        # NoData - manche Software liest den NoData-Tag wertbasiert (nicht
        # nur ueber die Flag Mask) und wuerde die Schattenpixel dann
        # faelschlich als NoData/transparent behandeln.
        shadow_mask = None
        shadow_shift = None
        has_shadow = False
        if do_rewrite:
            shadow_mask, shadow_shift = _shadow_buffer_shift(band_arrays[:3])
            has_shadow = shadow_mask.any()

        for i in range(3):
            # Kein .copy() mehr noetig: band_arrays[i] wird danach nicht
            # mehr im Originalzustand gebraucht, direktes In-Place-Aendern
            # spart eine komplette zusaetzliche Array-Kopie im Speicher.
            arr = band_arrays[i]
            if has_correction:
                new_vals = arr[correction_mask].astype(np.int32) + correction_shift[correction_mask]
                arr[correction_mask] = np.clip(new_vals, 0, 255).astype(dtype)
            if has_shadow:
                # Reihenfolge wichtig: MUSS vor has_rewrite laufen, sonst
                # waeren die soeben auf 0 gesetzten echten NoData-Pixel
                # (real_nodata_mask) mit im shadow_mask enthalten.
                shadow_vals = arr[shadow_mask].astype(np.int32) + shadow_shift[shadow_mask]
                arr[shadow_mask] = np.clip(shadow_vals, 0, 255).astype(dtype)
            if has_rewrite:
                arr[real_nodata_mask] = 0
            out_band = out_ds.GetRasterBand(i + 1)
            _write_band_chunked(out_band, arr)
            # sicherstellen, dass kein NoData-Tag gesetzt ist
            out_band.DeleteNoDataValue()

        if write_mask:
            out_ds.CreateMaskBand(gdal.GMF_PER_DATASET)
            mask_band = out_ds.GetRasterBand(1).GetMaskBand()
            mask_arr = np.where(real_nodata_mask, 0, 255).astype(np.uint8)
            _write_band_chunked(mask_band, mask_arr)

        out_ds.FlushCache()
        out_ds = None
        ds = None

        return {
            "n_groups": len(log_rows),
            "n_increment_px": int(correction_mask.sum()),
            "n_shadow_px": int(shadow_mask.sum()) if shadow_mask is not None else 0,
            "group_rows": group_rows,
            "tfw": "kopiert" if sidecar_copied else ("erzeugt" if create_options else "keine"),
            "epsg_fallback": fallback_epsg if used_fallback_epsg else None,
            "alte_baender_verworfen": n_bands - 3,
            "mask": "gesetzt" if write_mask else "keine",
        }

    # Standardmodus: Struktur per CreateCopy uebernehmen (fuer Tiles ohne
    # vorbestehende falsche Maske)
    create_options = ["TFW=YES"] if (write_tfw and not sidecar_copied) else []

    has_correction = correction_mask.any()
    if not has_correction:
        driver.CreateCopy(dst_path, ds, options=create_options)
        ds = None
        return {
            "n_groups": len(log_rows),
            "n_increment_px": 0,
            "n_shadow_px": 0,
            "group_rows": group_rows,
            "tfw": "kopiert" if sidecar_copied else ("erzeugt" if create_options else "keine"),
            "epsg_fallback": None,
            "alte_baender_verworfen": 0,
        }

    out_ds = driver.CreateCopy(dst_path, ds, options=create_options)

    for i in range(3):
        # Kein .copy() mehr noetig, siehe strip_existing_mask-Zweig oben.
        arr = band_arrays[i]
        new_vals = arr[correction_mask].astype(np.int32) + correction_shift[correction_mask]
        arr[correction_mask] = np.clip(new_vals, 0, 255).astype(dtype)
        _write_band_chunked(out_ds.GetRasterBand(i + 1), arr)

    # Weitere Baender (z.B. 4. Kanal) unveraendert uebernehmen
    for i in range(3, n_bands):
        _write_band_chunked(out_ds.GetRasterBand(i + 1), band_arrays[i])

    out_ds.FlushCache()
    out_ds = None
    ds = None

    return {
        "n_groups": len(log_rows),
        "n_increment_px": int(correction_mask.sum()),
        "n_shadow_px": 0,
        "group_rows": group_rows,
        "tfw": "kopiert" if sidecar_copied else ("erzeugt" if create_options else "keine"),
        "epsg_fallback": None,
        "alte_baender_verworfen": 0,
    }


def process_tile_inplace(path, backup_dir=None, **kwargs):
    """
    Verarbeitet ein Tile "in place": Input und Output sind dieselbe Datei
    (tiff + tfw). Schreibt dazu zuerst in eine temporaere Datei im selben
    Ordner (wichtig fuer os.replace, damit der finale Schritt atomar ist
    und nicht ueber Laufwerksgrenzen kopiert), und ersetzt das Original erst
    danach. Bei einem Fehler bleibt das Original unangetastet, die
    Temp-Datei wird aufgeraeumt.

    Falls backup_dir gesetzt ist, wird das unveraenderte Original (tif + tfw)
    vorher dorthin kopiert, als Sicherheitsnetz bei Produktionsdaten.

    **kwargs werden 1:1 an process_tile() weitergereicht (threshold,
    connectivity, write_tfw, strip_existing_mask, fallback_epsg,
    write_mask, rewrite_real_nodata_to_zero, min_border_contact,
    min_hole_ratio, min_roughness, enable_shape_check, missing_neighbors).
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    base = os.path.basename(path)

    fd, tmp_tif = tempfile.mkstemp(suffix=".tif", prefix=base + "_tmp_", dir=directory)
    os.close(fd)

    original_tfw = os.path.splitext(path)[0] + ".tfw"
    tmp_tfw = os.path.splitext(tmp_tif)[0] + ".tfw"

    try:
        result = process_tile(path, tmp_tif, **kwargs)

        if backup_dir:
            os.makedirs(backup_dir, exist_ok=True)
            shutil.copyfile(path, os.path.join(backup_dir, base))
            if os.path.isfile(original_tfw):
                shutil.copyfile(original_tfw, os.path.join(backup_dir, os.path.basename(original_tfw)))

        os.replace(tmp_tif, path)
        if os.path.isfile(tmp_tfw):
            os.replace(tmp_tfw, original_tfw)

        return result
    except Exception:
        if os.path.isfile(tmp_tif):
            os.remove(tmp_tif)
        if os.path.isfile(tmp_tfw):
            os.remove(tmp_tfw)
        raise


# ---------------------------------------------------------------------------
# CLI / Batch-Verarbeitung
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Korrigiert falsche NoData-Pixel (0,0,0) in DOP-Tiles."
    )
    parser.add_argument("--input", help="Einzelnes Input-Tile (.tif)")
    parser.add_argument("--output", help="Output-Pfad fuer Einzeltile")
    parser.add_argument("--input-dir", help="Ordner mit Input-Tiles (.tif)")
    parser.add_argument("--output-dir", help="Zielordner fuer korrigierte Tiles")
    parser.add_argument("--threshold", type=int, default=None,
                         help="Gruppen ab dieser Groesse (Pixel) gelten als echtes NoData, darunter als falsch. "
                              "Default: automatisch pro Tile aus der GSD berechnet, entsprechend 900 m² "
                              "(DEFAULT_MIN_NODATA_AREA_M2). Explizit gesetzter Wert uebersteuert die Automatik.")
    parser.add_argument("--min-border-contact", type=int, default=None,
                         help="Stufe C: Gruppe muss zusaetzlich zur Groesse ueber mindestens so viele "
                              "Pixel den Tile-Rand beruehren (Summe ueber alle Kanten). Default: "
                              "automatisch pro Tile aus der GSD, entsprechend 80 m "
                              "(DEFAULT_MIN_BORDER_CONTACT_M)")
    parser.add_argument("--min-hole-ratio", type=float, default=DEFAULT_MIN_HOLE_RATIO,
                         help="Stufe D: Mindestanteil eingeschlossener Nicht-NoData-Pixel, ab dem eine "
                              f"Gruppe als Gletscher gilt (Default: {DEFAULT_MIN_HOLE_RATIO})")
    parser.add_argument("--min-roughness", type=float, default=DEFAULT_MIN_ROUGHNESS,
                         help="Stufe D: Mindest-Konturrauheit (Konturlaenge / Bounding-Box-Umfang), ab "
                              f"der eine Gruppe als Gletscher gilt (Default: {DEFAULT_MIN_ROUGHNESS})")
    parser.add_argument("--disable-shape-check", action="store_true",
                         help="Stufe D (Form-Pruefung) ausschalten, Klassifikation nur ueber A-C")
    parser.add_argument("--nodata-value", type=int, choices=[0, 255], default=0,
                         help="NoData-Zielwert der Quelldaten: 0 = schwarz (Default), 255 = weiss")
    parser.add_argument("--connectivity", type=int, choices=[4, 8], default=8,
                         help="Nachbarschaft fuer Connected-Component-Labeling (Default: 8)")
    parser.add_argument("--write-tfw", action="store_true",
                         help="Zusaetzlich .tfw Worldfile schreiben statt nur interner Georeferenzierung")
    parser.add_argument("--strip-existing-mask", action="store_true",
                         help="Fuer Tiles mit bereits vorhandener, falsch berechneter Flag Mask: "
                              "Ausgabe komplett neu aufbauen (nur 3 RGB-Baender, kein NoData-Tag, "
                              "kein Mask Band), statt die Struktur zu klonen.")
    parser.add_argument("--epsg", type=int, default=2056,
                         help="Fallback-EPSG-Code, falls im Quellfile keine Projektion steht "
                              "(nur relevant mit --strip-existing-mask, Default: 2056)")
    parser.add_argument("--rewrite-nodata-to-zero", action="store_true",
                         help="Nur zusammen mit --strip-existing-mask und --nodata-value 255: "
                              "setzt die RGB-Baender an allen als echtes NoData erkannten Pixeln "
                              "zusaetzlich direkt auf 0,0,0 (historische 255er-NoData-DOPs).")
    parser.add_argument("--in-place", action="store_true",
                         help="Originaldateien (tif + tfw) direkt durch die korrigierte Version "
                              "ersetzen, statt in einen separaten Ordner zu schreiben. "
                              "Schreibt intern zuerst in eine Temp-Datei und ersetzt danach atomar.")
    parser.add_argument("--backup-dir",
                         help="Nur zusammen mit --in-place: Ordner, in den die unveraenderten "
                              "Originale (tif + tfw) vor dem Ueberschreiben kopiert werden.")
    parser.add_argument("--report", help="Optional: CSV-Pfad fuer alle klassifizierten Gruppen (Diagnose)")

    args = parser.parse_args()

    if not args.input and not args.input_dir:
        parser.error("Entweder --input oder --input-dir angeben.")

    if args.backup_dir and not args.in_place:
        parser.error("--backup-dir ist nur zusammen mit --in-place sinnvoll.")

    if args.in_place:
        if args.output or args.output_dir:
            parser.error("--in-place kann nicht zusammen mit --output/--output-dir verwendet werden.")
        if args.input:
            tasks = [(args.input, args.input)]
        else:
            tif_files = sorted(
                f for f in os.listdir(args.input_dir)
                if f.lower().endswith((".tif", ".tiff"))
            )
            if not tif_files:
                print(f"Keine .tif Dateien in {args.input_dir} gefunden.")
                sys.exit(1)
            tasks = [
                (os.path.join(args.input_dir, f), os.path.join(args.input_dir, f))
                for f in tif_files
            ]
    elif args.input:
        out_path = args.output or _default_output_path(args.input)
        if os.path.abspath(out_path) == os.path.abspath(args.input):
            parser.error("--output ist identisch mit --input. Dafuer --in-place verwenden.")
        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        tasks = [(args.input, out_path)]
    else:
        if not args.output_dir:
            parser.error("--output-dir wird zusammen mit --input-dir benoetigt.")
        if os.path.abspath(args.output_dir) == os.path.abspath(args.input_dir):
            parser.error("--output-dir ist identisch mit --input-dir. Dafuer --in-place verwenden.")
        os.makedirs(args.output_dir, exist_ok=True)
        tif_files = sorted(
            f for f in os.listdir(args.input_dir)
            if f.lower().endswith((".tif", ".tiff"))
        )
        if not tif_files:
            print(f"Keine .tif Dateien in {args.input_dir} gefunden.")
            sys.exit(1)
        tasks = [
            (os.path.join(args.input_dir, f), os.path.join(args.output_dir, f))
            for f in tif_files
        ]

    # Rand-Tiles immer aus allen Tiles des Quellordners bestimmen, auch bei
    # --input (ein einzelnes Tile allein waere sonst immer Rand-Tile).
    # Leere Tiles zaehlen nicht als Nachbar (siehe find_missing_neighbors),
    # werden hier aber nicht weggelassen, sondern komplett als echtes NoData
    # ausgegeben.
    src_dir = args.input_dir or os.path.dirname(os.path.abspath(args.input))
    src_tifs = [f for f in os.listdir(src_dir) if f.lower().endswith((".tif", ".tiff"))]
    empty = {f for f in src_tifs if is_tile_empty(os.path.join(src_dir, f), args.nodata_value)}
    neighbor_map = find_missing_neighbors(f for f in src_tifs if f not in empty)
    print(f"Rand-Tiles (TileKey, 8er-Nachbarschaft): {sum(map(bool, neighbor_map.values()))} "
          f"von {len(neighbor_map)} Tiles in {src_dir}, {len(empty)} leere Tile(s)")

    all_report_rows = []
    n_ok = 0
    n_failed = 0

    for src_path, dst_path in tasks:
        name = os.path.basename(src_path)
        try:
            common_kwargs = dict(
                threshold=args.threshold,
                connectivity=args.connectivity,
                write_tfw=args.write_tfw,
                strip_existing_mask=args.strip_existing_mask,
                fallback_epsg=args.epsg,
                nodata_value=args.nodata_value,
                rewrite_real_nodata_to_zero=args.rewrite_nodata_to_zero,
                min_border_contact=args.min_border_contact,
                min_hole_ratio=args.min_hole_ratio,
                min_roughness=args.min_roughness,
                enable_shape_check=not args.disable_shape_check,
                missing_neighbors=neighbor_map.get(name, ALL_NEIGHBORS),
            )
            if args.in_place:
                result = process_tile_inplace(src_path, backup_dir=args.backup_dir, **common_kwargs)
            else:
                result = process_tile(src_path, dst_path, **common_kwargs)
            extra = ""
            if result.get("epsg_fallback"):
                extra += f", EPSG-Fallback {result['epsg_fallback']} verwendet"
            if result.get("alte_baender_verworfen"):
                extra += f", {result['alte_baender_verworfen']} alte(s) Band/Baender verworfen"
            if result.get("n_shadow_px"):
                extra += f", {result['n_shadow_px']} Schattenpixel (0,0,0) geschuetzt"
            print(
                f"{name}: {result['n_groups']} Gruppen gefunden, "
                f"{result['n_increment_px']} Pixel angehoben, "
                f"tfw: {result['tfw']}{extra}"
            )
            for row in result["group_rows"]:
                all_report_rows.append({"tile": name, **row})
            n_ok += 1
        except Exception as exc:
            print(f"{name}: FEHLER - {exc}")
            n_failed += 1

    print(f"\nFertig: {n_ok} Tiles verarbeitet, {n_failed} Fehler.")

    if args.report and all_report_rows:
        with open(args.report, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["tile", "label_id", "size_px", "touches_border",
                               "border_contact_px", "hole_ratio",
                               "roughness", "open_contact_px", "edge_override",
                               "decision"]
            )
            writer.writeheader()
            writer.writerows(all_report_rows)
        print(f"Report geschrieben: {args.report} ({len(all_report_rows)} Zeilen)")


def _default_output_path(input_path):
    base, ext = os.path.splitext(input_path)
    return f"{base}_fixed{ext}"


if __name__ == "__main__":
    main()
