#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
6_SB_DOP_ADS80_TILEStoGRID1km.py

SB_DOP / SB_DOP_16 mit CameraSystem Leica ADS80: Die DOP-Kacheln aus OrthoVista
sind nicht auf 1 km geschnitten, sondern beliebig gross (< 4 GB, z.B. 4375 x
3000 m) und heissen ..._LV95_<Kachel>. Diese Stufe schneidet sie auf das
Grid-Shape (swissGRID 1km2, TileKey = Attribut NAME):

    2016_SILVRETTA_DOP_25cm_LV95_1178-33.tif  ->  2016_SILVRETTA_DOP_25cm_<NAME>_LV95.tif (+ .tfw)

wie die ADS100-Kacheln (2020_SILVRETTA_DOP_10cm_2797_1191_LV95.tif). Der
Namensteil vor '_LV95' bleibt, bei SB_DOP_16 also auch '<HHMM>NRGB'.

Ablauf:
  1. Input pruefen: gleiche Baender/Datentyp/GSD, keine Rotation, Lage LV95,
     keine Ueberlappung, Pixelraster auf den km-Linien (1000 m = n x GSD,
     Ursprung auf dem GSD-Raster) - sonst Abbruch statt Resampling.
  2. Virtuelles Mosaik (GDAL-VRT statt physischem Merge-TIFF, das ueber 100 GB
     gross werden kann), Hintergrund = NoData-Wert aus dem GUI, ohne NoData-Tag.
  3. Je Grid-Zelle pixelgenauer Ausschnitt (gdal.Translate mit srcWin, keine
     Resampling-Stufe) im Format der ADS100-Kacheln: GTiff unkomprimiert,
     256er-Bloecke, TFW. Kacheln nur aus NoData werden nicht geschrieben.

Danach laeuft alles wie bei ADS100 (SB_DOP: Script 3 optional + Script 1,
SB_DOP_16: Script 2_2): NoData-Tag, Maske, CRS-Tag EPSG:2056, XML, Kopie. Die
Kacheln tragen hier deshalb - wie die ADS100-Kacheln - noch keinen CRS-Tag.
Liegen die Kacheln schon auf dem 1-km-Grid, passiert nichts (Rueckgabe None).
Die Quelldateien werden nie veraendert.

Benoetigt OSGeo4W/QGIS-Python (osgeo, numpy).
"""

import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
from _grid_1km import load_grid_cells  # noqa: E402  (gemeinsam mit Script 5)

TIFF_EXTENSIONS = (".tif", ".tiff")
LV95_TOKEN      = "_LV95"
# Format wie die ADS100-OrthoVista-Kacheln (gdalinfo: Block 256x256, ohne Kompression)
CREATION_OPTIONS = ["TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256", "TFW=YES"]
ALREADY_GRID_RE  = re.compile(r"_\d{4}_\d{4}_LV95\.tiff?$", re.IGNORECASE)
LV95_RANGE       = ((2400000.0, 2900000.0), (1000000.0, 1400000.0))
ALIGN_TOL_PX     = 1e-6
EMPTY_CHECK_ROWS = 256


def log(msg):
    print(msg, flush=True)


def find_tiffs(src_dir):
    """TIFF der obersten Ebene (wie Script 1/2_2: keine Unterordner)."""
    return sorted(os.path.join(src_dir, f) for f in os.listdir(src_dir)
                  if os.path.isfile(os.path.join(src_dir, f))
                  and f.lower().endswith(TIFF_EXTENSIONS))


def output_prefix(paths):
    """Gemeinsamer Namensteil vor '_LV95' aller Input-Kacheln, z.B.
    '2016_SILVRETTA_DOP_25cm'. ValueError bei fehlendem '_LV95' oder
    unterschiedlichen Praefixen (gehoeren nicht in denselben Kachelsatz)."""
    prefixes = {}
    for p in paths:
        stem = os.path.splitext(os.path.basename(p))[0]
        idx = stem.upper().find(LV95_TOKEN)
        if idx <= 0:
            raise ValueError(f"'{LV95_TOKEN}' fehlt im Dateinamen: {os.path.basename(p)} "
                             "(erwartet z.B. 2016_SILVRETTA_DOP_25cm_LV95_1178-33.tif)")
        prefixes.setdefault(stem[:idx], []).append(os.path.basename(p))
    if len(prefixes) > 1:
        raise ValueError("Input-Kacheln haben unterschiedliche Namen vor '_LV95' - nur einen "
                         "Kachelsatz in den Inputordner legen: "
                         + ", ".join(f"{k} ({len(v)})" for k, v in sorted(prefixes.items())))
    return next(iter(prefixes))


def tile_name(prefix, name):
    return f"{prefix}_{name}{LV95_TOKEN}.tif"


def _tile_info(path):
    from osgeo import gdal
    ds = gdal.Open(path)
    try:
        gt = ds.GetGeoTransform(can_return_null=True)
        b1 = ds.GetRasterBand(1)
        info = {"path": path, "nx": ds.RasterXSize, "ny": ds.RasterYSize, "gt": gt,
                "bands": ds.RasterCount, "dtype": gdal.GetDataTypeName(b1.DataType)}
    finally:
        ds = None
    if gt is None:
        raise ValueError(f"{os.path.basename(path)}: keine Georeferenz (TFW fehlt?)")
    info.update(minx=gt[0], maxy=gt[3], maxx=gt[0] + info["nx"] * gt[1], miny=gt[3] + info["ny"] * gt[5])
    return info


def _check_inputs(infos):
    """Gleiches Pixelraster fuer alle Kacheln (Ursprung auf dem GSD-Raster),
    Lage LV95, keine Ueberlappung. Gibt die GSD zurueck."""
    names = [os.path.basename(i["path"]) for i in infos]
    first = infos[0]
    gsd = first["gt"][1]
    for i, n in zip(infos, names):
        gt = i["gt"]
        if gt[2] or gt[4]:
            raise ValueError(f"{n}: rotiertes Pixelraster - nicht unterstuetzt.")
        if abs(gt[1] - gsd) > 1e-9 or abs(gt[5] + gsd) > 1e-9:
            raise ValueError(f"{n}: Pixelgroesse {gt[1]:g} / {gt[5]:g} m statt {gsd:g} / {-gsd:g} m "
                             f"wie {names[0]} - alle Kacheln brauchen dieselbe GSD.")
        if (i["bands"], i["dtype"]) != (first["bands"], first["dtype"]):
            raise ValueError(f"{n}: {i['bands']} Baender {i['dtype']} statt {first['bands']} "
                             f"{first['dtype']} wie {names[0]}.")
        (x0, x1), (y0, y1) = LV95_RANGE
        if not (x0 <= i["minx"] <= i["maxx"] <= x1 and y0 <= i["miny"] <= i["maxy"] <= y1):
            raise ValueError(f"{n}: Koordinaten nicht in LV95 (X {i['minx']:.0f}, Y {i['maxy']:.0f}).")
        for label, v in (("X-Ursprung", gt[0]), ("Y-Ursprung", gt[3])):
            if abs(v / gsd - round(v / gsd)) > ALIGN_TOL_PX:
                raise ValueError(f"{n}: {label} {v} liegt nicht auf dem {gsd:g}-m-Pixelraster - die "
                                 "km-Linien fielen zwischen Pixel, Schneiden ginge nur mit Resampling.")
    for a in range(len(infos)):
        for b in range(a + 1, len(infos)):
            ia, ib = infos[a], infos[b]
            w = min(ia["maxx"], ib["maxx"]) - max(ia["minx"], ib["minx"])
            h = min(ia["maxy"], ib["maxy"]) - max(ia["miny"], ib["miny"])
            if w > gsd / 2 and h > gsd / 2:
                raise ValueError(f"Input-Kacheln ueberlappen sich: {names[a]} <-> {names[b]} - "
                                 "im Mosaik waere unklar, welches Pixel gilt.")
    return gsd


def _is_empty(path, nodata):
    """True, wenn jedes Pixel in jedem Band dem NoData-Wert entspricht. Liest in
    Zeilenbloecken und hoert beim ersten gueltigen Pixel auf."""
    import numpy as np
    from osgeo import gdal
    ds = gdal.Open(path)
    try:
        nd = np.array(nodata, dtype="float64").reshape(-1, 1, 1)
        for y in range(0, ds.RasterYSize, EMPTY_CHECK_ROWS):
            rows = min(EMPTY_CHECK_ROWS, ds.RasterYSize - y)
            arr = ds.ReadAsArray(0, y, ds.RasterXSize, rows)
            if arr.ndim == 2:
                arr = arr[np.newaxis]
            if np.any(arr != nd):
                return False
        return True
    finally:
        ds = None


def _remove_tile(path):
    for p in (path, os.path.splitext(path)[0] + ".tfw", path + ".aux.xml"):
        if os.path.isfile(p):
            os.remove(p)


def _cut_worker(args):
    """Schneidet EINE Grid-Zelle pixelgenau aus dem VRT. Rueckgabe:
    (dateiname, status 'written' | 'empty', fehler)."""
    from osgeo import gdal
    gdal.UseExceptions()
    vrt_path, out_path, src_win, origin, gsd, n_px, info0, nodata = args
    name = os.path.basename(out_path)
    try:
        gdal.Translate(out_path, vrt_path, options=gdal.TranslateOptions(
            format="GTiff", srcWin=src_win, creationOptions=CREATION_OPTIONS))
        ds = gdal.Open(out_path)
        gt, size = ds.GetGeoTransform(), (ds.RasterXSize, ds.RasterYSize)
        bands, dtype = ds.RasterCount, gdal.GetDataTypeName(ds.GetRasterBand(1).DataType)
        ds = None
        problems = []
        if size != (n_px, n_px):
            problems.append(f"Groesse {size} statt {n_px} x {n_px} Pixel")
        if (abs(gt[0] - origin[0]) > gsd * ALIGN_TOL_PX or abs(gt[3] - origin[1]) > gsd * ALIGN_TOL_PX
                or abs(gt[1] - gsd) > 1e-9 or abs(gt[5] + gsd) > 1e-9):
            problems.append(f"Georeferenz {gt[:2]}/{gt[3]} statt Ursprung {origin}, GSD {gsd:g}")
        if (bands, dtype) != (info0["bands"], info0["dtype"]):
            problems.append(f"{bands} Baender {dtype} statt {info0['bands']} {info0['dtype']}")
        if problems:
            _remove_tile(out_path)
            return name, "error", "; ".join(problems)
        if _is_empty(out_path, nodata):
            _remove_tile(out_path)
            return name, "empty", None
        return name, "written", None
    except Exception as e:
        _remove_tile(out_path)
        return name, "error", str(e)


def _default_worker_count():
    """Wie Script 1/4: Kernanzahl - 2, maximal 8."""
    return max(1, min((os.cpu_count() or 4) - 2, 8))


def retile_folder(src_dir, out_dir, grid_shape_path, nodata_str, work_dir, workers=None):
    """DOP-Kacheln in src_dir -> 1-km-Kacheln in out_dir. nodata_str = NoData
    aus dem GUI (z.B. '255 255 255'), ein Wert je Band - Hintergrund der
    Kacheln. Gibt die Anzahl geschriebener Kacheln zurueck, oder None, wenn
    die Kacheln schon auf dem 1-km-Grid liegen (dann bleibt alles wie bei ADS100)."""
    from osgeo import gdal
    gdal.UseExceptions()
    workers = workers or _default_worker_count()
    files = find_tiffs(src_dir)
    if not files:
        raise FileNotFoundError(f"Keine TIFF-Datei im Inputordner: {src_dir}")
    infos = [_tile_info(f) for f in files]

    # Schon 1-km-Kacheln (Name ..._<E>_<N>_LV95.tif und Ausdehnung = eine Zelle)?
    def _is_grid_tile(i):
        return (ALREADY_GRID_RE.search(os.path.basename(i["path"]))
                and abs(i["maxx"] - i["minx"] - 1000.0) < 1e-6 and abs(i["maxy"] - i["miny"] - 1000.0) < 1e-6
                and abs(i["minx"] / 1000.0 - round(i["minx"] / 1000.0)) < 1e-9
                and abs(i["miny"] / 1000.0 - round(i["miny"] / 1000.0)) < 1e-9)
    if all(_is_grid_tile(i) for i in infos):
        log(f"Input liegt bereits in 1-km-Kacheln ({len(files)}) - kein Zuschnitt noetig.")
        return None

    try:
        nodata = [float(v) for v in (nodata_str or "").split()]
    except ValueError:
        raise ValueError(f"NoData '{nodata_str}' ist keine Zahlenliste.")
    if len(nodata) != infos[0]["bands"]:
        raise ValueError(f"NoData '{nodata_str}' hat {len(nodata)} Wert(e), die Kacheln aber "
                         f"{infos[0]['bands']} Baender - NoData-Auswahl im GUI pruefen.")
    prefix = output_prefix(files)
    gsd = _check_inputs(infos)
    bbox = (min(i["minx"] for i in infos), min(i["miny"] for i in infos),
            max(i["maxx"] for i in infos), max(i["maxy"] for i in infos))
    cell_size, cells = load_grid_cells(grid_shape_path, bbox)
    if abs(cell_size / gsd - round(cell_size / gsd)) > ALIGN_TOL_PX:
        raise ValueError(f"Zellgroesse {cell_size:g} m ist kein Vielfaches der GSD {gsd:g} m - "
                         "die Zellgrenzen fielen zwischen Pixel.")
    n_px = int(round(cell_size / gsd))

    log(f"Input          : {len(files)} Kachel(n), {infos[0]['bands']} Baender {infos[0]['dtype']}, "
        f"GSD {gsd:g} m, X {bbox[0]:.0f}-{bbox[2]:.0f}, Y {bbox[1]:.0f}-{bbox[3]:.0f}")
    log(f"Kachelname     : {tile_name(prefix, '<NAME>')}  ({n_px} x {n_px} Pixel)")
    log(f"Hintergrund    : {nodata_str} (NoData aus dem GUI)")
    log(f"Grid-Shape     : {grid_shape_path}  ({len(cells)} Zelle(n) im Datenbereich)")

    # Virtuelles Mosaik auf km-Grenzen erweitert: jede Zelle liegt ganz im VRT,
    # ausserhalb der Kacheln liefert es den NoData-Wert (hidenodata: ohne Tag)
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    vx0 = math.floor(bbox[0] / cell_size) * cell_size
    vy0 = math.floor(bbox[1] / cell_size) * cell_size
    vx1 = math.ceil(bbox[2] / cell_size) * cell_size
    vy1 = math.ceil(bbox[3] / cell_size) * cell_size
    vrt_path = os.path.join(work_dir, "mosaik.vrt")
    gdal.BuildVRT(vrt_path, files, options=gdal.BuildVRTOptions(
        outputBounds=(vx0, vy0, vx1, vy1), resolution="user", xRes=gsd, yRes=gsd,
        VRTNodata=" ".join(f"{v:g}" for v in nodata), hideNodata=True))

    jobs = []
    for (ix, iy), name in sorted(cells.items()):
        cx0, cy0 = ix * cell_size, iy * cell_size
        # Zellen ohne Kachel darin gar nicht erst schneiden (sicher leer)
        if not any(i["minx"] < cx0 + cell_size and i["maxx"] > cx0 and
                   i["miny"] < cy0 + cell_size and i["maxy"] > cy0 for i in infos):
            continue
        src_win = [int(round((cx0 - vx0) / gsd)), int(round((vy1 - (cy0 + cell_size)) / gsd)), n_px, n_px]
        jobs.append((vrt_path, os.path.join(out_dir, tile_name(prefix, name)), src_win,
                     (cx0, cy0 + cell_size), gsd, n_px, infos[0], nodata))

    log(f"\nSchneiden: {len(jobs)} Zelle(n) mit Daten-Ueberdeckung, {workers} parallel ...")
    written, empty, errors = 0, 0, []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(_cut_worker, j) for j in jobs]):
            name, status, err = fut.result()
            if status == "written":
                written += 1
                log(f"  {name}")
            elif status == "empty":
                empty += 1
                log(f"  {name}: nur NoData - nicht geschrieben")
            else:
                errors.append(f"{name}: {err}")
                log(f"  {name}: FEHLER - {err}")
    if errors:
        raise RuntimeError(f"{len(errors)} Kachel(n) fehlgeschlagen - Abbruch.")
    if not written:
        raise RuntimeError("Keine Kachel mit Daten - NoData-Wert und Input pruefen.")
    log(f"\nADS80 -> 1-km-Grid: {written} Kachel(n) geschrieben, {empty} leere weggelassen.")
    return written
