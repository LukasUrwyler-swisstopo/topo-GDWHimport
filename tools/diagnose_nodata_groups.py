#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Diagnose der Echt/Falsch-NoData-Klassifikation (classify_mask aus
processingScripts/3_fix_false_nodata_dop.py), ohne die Tiles zu veraendern.

Gibt pro Gruppe, die Stufe A (Groesse) erfuellt, Randkontakt, die Stufe-D-
Kennzahlen (Einschluss-Anteil, Konturrauheit) und den Entscheid aus - mit und
ohne Stufe D. Dient zum Pruefen bzw. Nachjustieren der Schwellen
(DEFAULT_MIN_HOLE_RATIO, DEFAULT_MIN_ROUGHNESS, DEFAULT_MIN_BORDER_CONTACT_M)
an neuen Datensaetzen, insbesondere solchen mit gefeatherten Mosaikkanten
(Vorfall WALLIS_SAASTAL, 05.08.2026).

Liest mit GDAL (OSGeo4W) oder ersatzweise rasterio. Benoetigt numpy, scipy.

Verwendung (OSGeo4W-Shell):
  python tools/diagnose_nodata_groups.py tile1.tif [tile2.tif ...] --nodata 255
         [--csv report.csv] [--label-tif-dir ./diag]

  --label-tif-dir schreibt pro Tile ein Label-GeoTIFF (uint16, Wert =
  label_id) der Gruppen >= Stufe A, zum Abgleich mit der Tabelle in QGIS.

Welche Nachbar-Tiles fehlen (find_missing_neighbors, Rand-Tile = mindestens
einer), wird wie in der GUI aus allen nicht-leeren .tif im Ordner des Tiles
bestimmt (Ordner mit _leere_Tiles\\ aus einem frueheren GUI-Lauf: dort
liegen die leeren Tiles bereits ausserhalb und zaehlen ebenfalls nicht).
Spalten: missing_neighbors = fehlende Nachbarn (N, NO, O, ...),
open_contact_m = Randkontakt an Seiten/Ecken ohne Nachbar-Tile,
edge_override = Stufe D haette "falsch" ergeben, offener Randkontakt ->
"echt".
"""

import argparse
import csv
import importlib.util
import logging
import os
import sys

import numpy as np

log = logging.getLogger("diagnose_nodata")

_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_mod3():
    # Modulname beginnt mit Ziffer -> Import ueber importlib
    path = os.path.join(_REPO_DIR, "processingScripts", "3_fix_false_nodata_dop.py")
    spec = importlib.util.spec_from_file_location("fixnodata", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_tile(path):
    """Rueckgabe: (Liste der RGB-Baender, GDAL-GeoTransform, CRS als WKT)."""
    try:
        from osgeo import gdal
        gdal.UseExceptions()
        ds = gdal.Open(path)
        if ds.RasterCount < 3:
            raise RuntimeError(f"erwarte mindestens 3 Baender, gefunden: {ds.RasterCount}")
        rgb = [ds.GetRasterBand(i).ReadAsArray() for i in (1, 2, 3)]
        return rgb, ds.GetGeoTransform(), ds.GetProjection()
    except ImportError:
        import rasterio
        with rasterio.open(path) as ds:
            if ds.count < 3:
                raise RuntimeError(f"erwarte mindestens 3 Baender, gefunden: {ds.count}")
            rgb = [ds.read(i) for i in (1, 2, 3)]
            return rgb, ds.transform.to_gdal(), ds.crs.to_wkt() if ds.crs else ""


def _write_labels(path, labels, gt, crs_wkt):
    try:
        from osgeo import gdal
        ds = gdal.GetDriverByName("GTiff").Create(
            path, labels.shape[1], labels.shape[0], 1, gdal.GDT_UInt16,
            options=["COMPRESS=DEFLATE"])
        ds.SetGeoTransform(gt)
        if crs_wkt:
            ds.SetProjection(crs_wkt)
        band = ds.GetRasterBand(1)
        band.WriteArray(labels)
        band.SetNoDataValue(0)
        ds = None
    except ImportError:
        import rasterio
        from affine import Affine
        with rasterio.open(path, "w", driver="GTiff", width=labels.shape[1],
                           height=labels.shape[0], count=1, dtype="uint16",
                           transform=Affine.from_gdal(*gt), crs=crs_wkt or None,
                           nodata=0, compress="deflate") as ds:
            ds.write(labels, 1)


def diagnose_tile(mod3, path, nodata, missing_neighbors=frozenset()):
    rgb, gt, crs_wkt = _read_tile(path)
    name = os.path.basename(path)
    # Script 3 setzt bei fehlender Projektion EPSG:2056 als Fallback
    if not crs_wkt:
        log.warning("%s: kein CRS im Tile, process_tile nimmt EPSG:2056 an", name)
    elif "2056" not in crs_wkt:
        log.warning("%s: CRS ist nicht EPSG:2056", name)
    # wie process_tile: Seiten nur bei nordorientiertem Raster zuordenbar
    if missing_neighbors and not (gt[1] > 0 and gt[5] < 0 and gt[2] == 0 and gt[4] == 0):
        log.warning("%s: Raster nicht nordorientiert, alle Seiten gelten als offen", name)
        missing_neighbors = mod3.ALL_NEIGHBORS

    threshold, min_border_contact = mod3.pixel_thresholds(gt)
    pixel_size = abs(gt[1] * gt[5]) ** 0.5
    mask = (rgb[0] == nodata) & (rgb[1] == nodata) & (rgb[2] == nodata)
    del rgb

    _, log_rows = mod3.classify_mask(
        mask, threshold=threshold, min_border_contact=min_border_contact,
        missing_neighbors=missing_neighbors)
    missing_txt = mod3.describe_neighbors(missing_neighbors)

    rows = []
    for g in log_rows:
        if g["border_contact_px"] is None:
            continue  # Stufe A nicht erfuellt
        abc_real = g["touches_border"] and g["border_contact_px"] >= min_border_contact
        rows.append({
            "tile": name,
            "label_id": g["label_id"],
            "area_m2": round(g["size_px"] * pixel_size ** 2, 1),
            "border_contact_m": round(g["border_contact_px"] * pixel_size, 1),
            "hole_ratio": None if g["hole_ratio"] is None else round(g["hole_ratio"], 4),
            "roughness": None if g["roughness"] is None else round(g["roughness"], 3),
            "decision_ABC": "real_nodata" if abc_real else "false_nodata",
            "missing_neighbors": missing_txt,
            "open_contact_m": (None if g["open_contact_px"] is None
                               else round(g["open_contact_px"] * pixel_size, 1)),
            "edge_override": g["edge_override"],
            "decision": g["decision"],
        })
    log.info("%s%s: GSD %.3f m, Stufe A >= %d px, Stufe C >= %d px, %d Gruppe(n), %d ab Stufe A",
             name, f" [Rand-Tile, fehlende Nachbarn: {missing_txt}]" if missing_txt else "",
             pixel_size, threshold, min_border_contact, len(log_rows), len(rows))
    return rows, mask, gt, crs_wkt


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tiles", nargs="+", help="RGB-GeoTIFFs")
    ap.add_argument("--nodata", type=int, choices=[0, 255], default=255)
    ap.add_argument("--csv", help="Ausgabe als CSV (Trennzeichen ;)")
    ap.add_argument("--label-tif-dir", help="Ordner fuer Label-GeoTIFFs (fuer QGIS)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        mod3 = _load_mod3()
    except Exception as exc:
        log.error("3_fix_false_nodata_dop.py nicht ladbar: %s", exc)
        return 1

    all_rows = []
    neighbor_maps = {}  # Ordner -> find_missing_neighbors-Ergebnis
    for path in args.tiles:
        try:
            folder = os.path.dirname(os.path.abspath(path))
            if folder not in neighbor_maps:
                # leere Tiles zaehlen wie in der GUI nicht als Nachbar
                neighbor_maps[folder] = mod3.find_missing_neighbors(
                    f for f in os.listdir(folder)
                    if f.lower().endswith((".tif", ".tiff"))
                    and not mod3.is_tile_empty(os.path.join(folder, f), args.nodata))
            missing = neighbor_maps[folder].get(os.path.basename(path), mod3.ALL_NEIGHBORS)
            rows, mask, gt, crs_wkt = diagnose_tile(mod3, path, args.nodata, missing)
        except Exception as exc:
            log.error("%s: %s", path, exc)
            continue
        all_rows.extend(rows)

        if args.label_tif_dir and rows:
            from scipy import ndimage
            labeled, _ = ndimage.label(mask, structure=np.ones((3, 3), dtype=int))
            keep = np.zeros(labeled.max() + 1, dtype=bool)
            keep[[r["label_id"] for r in rows]] = True
            labels = np.where(keep[labeled], labeled, 0).astype(np.uint16)
            os.makedirs(args.label_tif_dir, exist_ok=True)
            dst = os.path.join(args.label_tif_dir,
                               os.path.splitext(os.path.basename(path))[0] + "_groups.tif")
            _write_labels(dst, labels, gt, crs_wkt)
            log.info("Label-Raster: %s", dst)

    if not all_rows:
        log.info("Keine Gruppe erfuellt Stufe A.")
        return 0

    cols = list(all_rows[0])
    print("\t".join(cols))
    for r in all_rows:
        print("\t".join("" if r[c] is None else str(r[c]) for c in cols))

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=cols, delimiter=";")
            writer.writeheader()
            writer.writerows(all_rows)
        log.info("CSV: %s", args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
