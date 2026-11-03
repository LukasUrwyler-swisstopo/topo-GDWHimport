#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
5_SB_DSM_PUNKTWOLKE_ASCIItoLAZtiles.py

SB_DSM_PUNKTWOLKE mit CameraSystem Leica ADS80: Die Punktwolke kommt nicht in
1-km-LAZ-Kacheln, sondern als eine ASCII-Datei pro Gebiet. Diese Stufe macht
daraus LAZ-Kacheln gemaess Grid-Shape (swissGRID 1km2, TileKey = Attribut NAME):

    <Jahr>_<AREA>_TIN_DSM_<NAME>_LV95_LN02.laz   z.B. 2016_SILVRETTA_TIN_DSM_2795_1189_LV95_LN02.laz

Laeuft in _osgeo_runner.py VOR Script 4. Script 4 bringt die Kacheln danach wie
jede andere Lieferung ins GDWH-Zielformat (LAS 1.4/PF6, byte-exakte CRS-VLRs
LV95/LN02, Kachelrahmen- und Punktpruefung) - das Zielformat ist damit nur an
einer Stelle definiert. Die Kacheln hier tragen deshalb noch KEINEN CRS-Tag
(wie eine ADS-LAS-1.2-Lieferung) - einen vorlaeufigen Tag wuerde Script 4 nur
wieder entfernen und dafuer je Kachel eine Warnung schreiben.

Input = alle ASCII-Dateien der obersten Ebene des Quellordners (ein Kachelsatz),
das Format wird am Inhalt erkannt, nicht an der Endung:
    ESRI-ASCII-Grid (Kopf NCOLS/NROWS/...)  -> readers.gdal, ein Punkt je Zellmitte,
                                              NODATA-Zellen werden verworfen
    XYZ-Punktliste                          -> readers.text, grosse Dateien an
                                              Zeilengrenzen geteilt und parallel gelesen

Lage immer LV95, Hoehe immer LN02 - nichts wird umgerechnet. Geprueft wird der
Koordinatenbereich (LV03 oder anderes -> Abbruch), Z nur als Warnung.

Uebernommen und gekuerzt aus topo-DSMpointcloudConverter (_osgeo_runner.py):
ASCII-Sniffing, Zerlegen grosser Dateien, 'pdal tile', Grid-Zuordnung,
Punktbilanz. Ohne Thinning, Raster, LAS-Input und LHN95. Neu: ESRI-ASCII-Grid
(der Converter lehnt es ab).

Benoetigt OSGeo4W/QGIS-Python (osgeo) und pdal.exe.
"""

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
from _grid_1km import load_grid_cells  # noqa: E402  (gemeinsam mit Script 6)

ASCII_EXTENSIONS = (".asc", ".ascii", ".xyz")
SEPARATORS = {"space": " ", "tab": "\t", "comma": ",", "semicolon": ";"}

TILE_TOKEN    = "TIN_DSM"
TILE_SUFFIX   = "_LV95_LN02.laz"     # Muster von Script 4 (TILE_NAME_PATTERN)

OUT_MINOR_VERSION   = 4
OUT_POINT_FORMAT    = 6              # ADS: ohne Farbe
OUT_GLOBAL_ENCODING = 17
OUT_SCALE           = 0.01
FRAME_TOLERANCE_M   = 0.02

PLAUSIBLE_Z = (150.0, 4900.0)        # Schweiz 193-4634 m, mit Reserve
LV95_RANGE  = ((2400000.0, 2900000.0), (1000000.0, 1400000.0))
LV03_RANGE  = ((400000.0, 900000.0), (0.0, 400000.0))

# readers.text liest einspurig (~0.5 Mio Punkte/s) - grosse Dateien in Teilen
ASCII_CHUNK_MB = 256
PIECE_NAME_PATTERN = re.compile(r"^t(-?\d+)_(-?\d+)\.laz$", re.IGNORECASE)
PDAL_MAX_PATH = 259                  # pdal.exe ist nicht long-path-faehig

_GRID_HEADER_KEYS = ("ncols", "nrows", "xllcorner", "xllcenter", "yllcorner",
                     "yllcenter", "cellsize", "nodata_value")
_KNOWN_DIMS = {"x": "X", "y": "Y", "z": "Z", "h": "Z", "intensity": "Intensity",
               "classification": "Classification", "class": "Classification"}


def log(msg):
    print(msg, flush=True)


def _fmt_count(n):
    return f"{int(n):,}".replace(",", "'")


def find_ascii_files(src_dir):
    """ASCII-Dateien der obersten Ebene (wie Script 1/4: keine Unterordner)."""
    return sorted(os.path.join(src_dir, f) for f in os.listdir(src_dir)
                  if os.path.isfile(os.path.join(src_dir, f))
                  and f.lower().endswith(ASCII_EXTENSIONS))


def tile_name(jahr, area, name):
    return f"{jahr}_{area}_{TILE_TOKEN}_{name}{TILE_SUFFIX}"


# ─── PDAL ──────────────────────────────────────────────────────────────────────
def _find_pdal_exe():
    exe = os.path.join(os.path.dirname(sys.executable), "pdal.exe")
    if os.path.isfile(exe):
        return exe
    exe = shutil.which("pdal")
    if exe:
        return exe
    raise FileNotFoundError(f"pdal.exe nicht gefunden (weder neben {sys.executable} noch im PATH).")


def _run_pdal(pdal_exe, args):
    result = subprocess.run([pdal_exe] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    out = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        raise RuntimeError(f"pdal {args[0]} beendet mit Exit-Code {result.returncode}"
                           + (f": {out[:800]}" if out else " (kein Output - zu wenig RAM?)"))
    return out


def _run_pipeline(pdal_exe, stages, pipeline_path):
    with open(pipeline_path, "w", encoding="utf-8") as f:
        json.dump({"pipeline": stages}, f, indent=1)
    try:
        return _run_pdal(pdal_exe, ["pipeline", str(pipeline_path)])
    finally:
        _discard(pipeline_path)


def _las_writer(filename, offset=(0.0, 0.0, 0.0), global_encoding=None):
    writer = {"type": "writers.las", "filename": str(filename), "compression": "laszip",
              "minor_version": OUT_MINOR_VERSION, "dataformat_id": OUT_POINT_FORMAT,
              "scale_x": OUT_SCALE, "scale_y": OUT_SCALE, "scale_z": OUT_SCALE,
              "offset_x": offset[0], "offset_y": offset[1], "offset_z": offset[2]}
    if global_encoding is not None:
        writer["global_encoding"] = global_encoding
    return writer


def _discard(path):
    try:
        if path and os.path.isfile(str(path)):
            os.remove(str(path))
    except OSError:
        pass


def _read_las_header(path):
    """Felder des LAS-Public-Headers ohne Punktdaten (auch LAZ, Header unkomprimiert)."""
    with open(path, "rb") as f:
        b = f.read(375)
    if len(b) < 227 or b[:4] != b"LASF":
        raise ValueError(f"Keine gueltige LAS/LAZ-Datei: {path}")
    count, = struct.unpack_from("<I", b, 107)
    if b[25] >= 4 and len(b) >= 255:
        count = struct.unpack_from("<Q", b, 247)[0] or count
    sx, sy, sz, ox, oy, oz = struct.unpack_from("<6d", b, 131)
    maxx, minx, maxy, miny, maxz, minz = struct.unpack_from("<6d", b, 179)
    return {"minor_version": b[25], "point_format": b[104] & 0x3F, "count": int(count),
            "scale": (sx, sy, sz), "offset": (ox, oy, oz),
            "minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy, "minz": minz, "maxz": maxz}


def _classify_crs(minx, miny, maxx, maxy):
    def _inside(rng):
        (x0, x1), (y0, y1) = rng
        return x0 <= minx <= maxx <= x1 and y0 <= miny <= maxy <= y1
    if _inside(LV95_RANGE):
        return "LV95"
    if _inside(LV03_RANGE):
        return "LV03"
    return "unbekannt"


# ─── ASCII erkennen ────────────────────────────────────────────────────────────
def _is_number(token):
    try:
        float(token)
        return True
    except ValueError:
        return False


def _split_fields(line, sep_key):
    if sep_key == "space":
        return line.split()
    return [t.strip() for t in line.split(SEPARATORS[sep_key])]


def sniff_ascii(path, max_lines=50):
    """{'kind': 'grid'} fuer ein ESRI-ASCII-Grid, sonst Trennzeichen, Kopfzeilen,
    Spalten und LV95/LV03-Schaetzung aus den ersten Zeilen einer XYZ-Punktliste."""
    lines = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for _ in range(max_lines):
            line = f.readline()
            if not line:
                break
            if line.strip():
                lines.append(line.rstrip("\r\n"))
    if not lines:
        raise ValueError(f"Datei ist leer: {path}")
    if lines[0].split()[0].lower() in _GRID_HEADER_KEYS:
        return {"kind": "grid"}

    skip = 0
    for line in lines:
        if _is_number(re.split(r"[\s,;]+", line.strip())[0]):
            break
        skip += 1
    data = lines[skip:]
    if not data:
        raise ValueError(f"Keine numerische Datenzeile in den ersten {max_lines} Zeilen: {path}")
    sample = data[0]
    sep = ("tab" if "\t" in sample else "semicolon" if ";" in sample
           else "comma" if "," in sample else "space")
    fields = _split_fields(sample, sep)
    if len(fields) < 3 or not all(_is_number(t) for t in fields[:3]):
        raise ValueError(f"{os.path.basename(path)}: erste Datenzeile hat keine 3 numerischen "
                         f"Spalten (X Y Z): '{sample[:80]}'")
    columns = ["X", "Y", "Z"] + [f"Col{i}" for i in range(4, len(fields) + 1)]
    if skip:
        tokens = _split_fields(lines[skip - 1], sep)
        if len(tokens) == len(fields):
            mapped = [_KNOWN_DIMS.get(t.strip().strip('"').lower()) for t in tokens]
            columns = [m or columns[i] for i, m in enumerate(mapped)]
    xs, ys = [], []
    for line in data:
        parts = _split_fields(line, sep)
        if len(parts) >= 3 and all(_is_number(t) for t in parts[:2]):
            xs.append(float(parts[0]))
            ys.append(float(parts[1]))
    return {"kind": "xyz", "separator": sep, "skip": skip, "columns": columns,
            "crs_guess": _classify_crs(min(xs), min(ys), max(xs), max(ys))}


def grid_info(path):
    """Ausdehnung und NODATA eines ESRI-ASCII-Grids via GDAL (liest nur den Kopf)."""
    from osgeo import gdal
    gdal.UseExceptions()
    ds = gdal.Open(path)
    try:
        gt = ds.GetGeoTransform()
        nx, ny = ds.RasterXSize, ds.RasterYSize
        nodata = ds.GetRasterBand(1).GetNoDataValue()
    finally:
        ds = None
    minx, maxy = gt[0], gt[3]
    maxx, miny = gt[0] + nx * gt[1], gt[3] + ny * gt[5]
    return {"ncols": nx, "nrows": ny, "cellsize": gt[1], "nodata": nodata,
            "crs_guess": _classify_crs(minx, miny, maxx, maxy),
            "bbox": (minx, miny, maxx, maxy)}


def _split_ascii(src, chunk_dir, n_parts):
    """Zerlegt eine grosse XYZ-Datei an Zeilengrenzen in bis zu n_parts Teile
    (binaere Kopie). Kopfzeilen bleiben im ersten Teil."""
    size = os.path.getsize(src)
    cuts = [0]
    chunk_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    with open(src, "rb") as f:
        for k in range(1, n_parts):
            f.seek(size * k // n_parts)
            f.readline()
            if cuts[-1] < f.tell() < size:
                cuts.append(f.tell())
        cuts.append(size)
        for i in range(len(cuts) - 1):
            part = chunk_dir / f"c{i:02d}.txt"
            f.seek(cuts[i])
            remaining = cuts[i + 1] - cuts[i]
            with open(part, "wb") as out:
                while remaining > 0:
                    buf = f.read(min(remaining, 16 * 1024 * 1024))
                    if not buf:
                        break
                    out.write(buf)
                    remaining -= len(buf)
            parts.append(str(part))
    return parts


# ─── Worker (laufen parallel in Threads, die Arbeit macht pdal.exe) ────────────
def _xyz_to_laz_worker(args):
    """XYZ (Datei oder Teil) -> Zwischen-LAZ, scale 0.01 / offset 0: dasselbe
    Zentimeter-Gitter wie die Endkacheln. Rueckgabe: (ziel, punkte, ignorierte Zeilen)."""
    src, dst, pipeline_path, pdal_exe, columns, sep_key, skip = args
    sep = SEPARATORS[sep_key]
    reader = {"type": "readers.text", "filename": src, "separator": sep,
              "header": sep.join(columns), "skip": int(skip)}
    out = _run_pipeline(pdal_exe, [reader, _las_writer(dst)], pipeline_path)
    # readers.text ueberspringt unpassende Zeilen nur mit Warnung (Exit-Code 0)
    ignored = out.count("Ignoring")
    count = _read_las_header(dst)["count"] if os.path.isfile(dst) else 0
    if count == 0:
        raise ValueError(f"0 Punkte gelesen - Spalten '{' '.join(columns)}' / Trennzeichen "
                         f"passen nicht zur Datei. PDAL: {out[:300]}")
    return dst, count, ignored


def _grid_to_laz_worker(args):
    """ESRI-ASCII-Grid -> Zwischen-LAZ: ein Punkt je Zellmitte, NODATA verworfen.
    readers.gdal liefert NODATA-Zellen als Punkte mit dem NODATA-Wert - deshalb
    filters.range mit kleinem Band um den Wert (Float32-Rundung im Grid)."""
    src, dst, pipeline_path, pdal_exe, nodata = args
    stages = [{"type": "readers.gdal", "filename": src, "header": "Z"}]
    if nodata is not None:
        tol = max(abs(nodata) * 1e-6, 1e-6)
        stages.append({"type": "filters.range",
                       "limits": f"Z![{nodata - tol!r}:{nodata + tol!r}]"})
    stages.append(_las_writer(dst))
    _run_pipeline(pdal_exe, stages, pipeline_path)
    count = _read_las_header(dst)["count"] if os.path.isfile(dst) else 0
    if count == 0:
        raise ValueError("0 Punkte - Grid enthaelt nur NODATA-Zellen?")
    return dst, count, 0


def _split_worker(args):
    """'pdal tile' auf EINE Zwischen-LAZ (Streaming, ein Lesedurchlauf): Stuecke
    't<ix>_<iy>.laz', ix/iy = floor(koordinate / zellgroesse) - halb-offen, ein
    Punkt auf der km-Linie gehoert genau einer Kachel."""
    src, piece_dir, pdal_exe, size = args
    Path(piece_dir).mkdir(parents=True, exist_ok=True)
    _run_pdal(pdal_exe, [
        "tile", src, str(Path(piece_dir) / "t#.laz"),
        "--length", f"{size:g}", "--origin_x", "0", "--origin_y", "0",
        "--writers.las.compression=laszip",
        f"--writers.las.minor_version={OUT_MINOR_VERSION}",
        f"--writers.las.dataformat_id={OUT_POINT_FORMAT}",
        f"--writers.las.scale_x={OUT_SCALE}", f"--writers.las.scale_y={OUT_SCALE}",
        f"--writers.las.scale_z={OUT_SCALE}",
        "--writers.las.offset_x=0", "--writers.las.offset_y=0", "--writers.las.offset_z=0",
    ])
    pieces = []
    for p in sorted(Path(piece_dir).iterdir()):
        m = PIECE_NAME_PATTERN.match(p.name)
        if m:
            pieces.append(((int(m.group(1)), int(m.group(2))), str(p),
                           _read_las_header(str(p))["count"]))
    return pieces


def _tile_worker(args):
    """Fuegt die Stuecke EINER Grid-Zelle zur Kachel zusammen (Offset =
    Kachelursprung) und prueft Punktzahl und Kachelrahmen. Atomar: erst nach
    bestandener Pruefung per os.replace an ihren Platz.
    Rueckgabe: (dateiname, punktzahl, fehler)."""
    key, out_path, pieces, expected, pipeline_path, pdal_exe, size = args
    origin = (key[0] * size, key[1] * size)
    out_name = os.path.basename(out_path)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".laz", dir=os.path.dirname(out_path))
    os.close(fd)
    os.remove(tmp_path)  # writers.las legt die Datei selbst an
    stages = [{"type": "readers.las", "filename": p, "tag": f"r{i}"} for i, p in enumerate(pieces)]
    if len(stages) > 1:
        stages.append({"type": "filters.merge", "inputs": [s["tag"] for s in stages]})
    stages.append(_las_writer(tmp_path, (origin[0], origin[1], 0.0), OUT_GLOBAL_ENCODING))
    try:
        _run_pipeline(pdal_exe, stages, pipeline_path)
        h = _read_las_header(tmp_path)
        problems = []
        if h["count"] != expected:
            problems.append(f"Punktzahl {h['count']} statt {expected}")
        if (h["minor_version"], h["point_format"]) != (OUT_MINOR_VERSION, OUT_POINT_FORMAT):
            problems.append(f"LAS 1.{h['minor_version']}/PF{h['point_format']}")
        eps = FRAME_TOLERANCE_M
        if (h["minx"] < origin[0] - eps or h["maxx"] > origin[0] + size + eps
                or h["miny"] < origin[1] - eps or h["maxy"] > origin[1] + size + eps):
            problems.append(f"Punkte ausserhalb des Kachelrahmens (X {h['minx']:.2f}-{h['maxx']:.2f}, "
                            f"Y {h['miny']:.2f}-{h['maxy']:.2f})")
        if problems:
            return out_name, 0, "; ".join(problems)
        os.replace(tmp_path, out_path)
        tmp_path = None
        return out_name, h["count"], None
    except Exception as e:
        return out_name, 0, str(e)
    finally:
        _discard(tmp_path)


def _overlapping_pairs(boxes, min_share=0.01):
    """Paare von Dateien, deren BBoxen sich flaechig ueberlappen (> min_share der
    kleineren Flaeche) - beim Zusammenfuehren entstuenden doppelte Punkte."""
    items = sorted(boxes.items())
    pairs = []
    for i, (a, ba) in enumerate(items):
        for b, bb in items[i + 1:]:
            w = min(ba[2], bb[2]) - max(ba[0], bb[0])
            h = min(ba[3], bb[3]) - max(ba[1], bb[1])
            smaller = min((ba[2] - ba[0]) * (ba[3] - ba[1]), (bb[2] - bb[0]) * (bb[3] - bb[1]))
            if w > 0 and h > 0 and w * h > min_share * max(smaller, 1e-9):
                pairs.append((a, b))
    return pairs


def _default_worker_count():
    """Wie Script 1/4: Kernanzahl - 2, maximal 8."""
    return max(1, min((os.cpu_count() or 4) - 2, 8))


# ─── Ablauf ────────────────────────────────────────────────────────────────────
def convert_folder(src_dir, out_dir, grid_shape_path, jahr, area, work_dir, workers=None):
    """ASCII-Dateien in src_dir -> LAZ-Kacheln in out_dir (ein Kachelsatz).
    Die Quelldateien werden nie veraendert, Zwischendateien liegen in work_dir.
    Wirft bei jedem Fehler eine Exception (nichts halb Fertiges soll weiter
    nach Script 4/1 gelangen). Gibt die Anzahl geschriebener Kacheln zurueck."""
    workers = workers or _default_worker_count()
    if not re.fullmatch(r"\d{4}", str(jahr)):
        raise ValueError(f"Jahr '{jahr}' ungueltig (vierstellig aus der Line_ID erwartet).")
    if not area or re.search(r'[<>:"/\\|?*\s]', area):
        raise ValueError(f"Area '{area}' fehlt oder enthaelt Leer-/Sonderzeichen (geht in den Kachelnamen).")
    files = find_ascii_files(src_dir)
    if not files:
        raise FileNotFoundError(f"Keine ASCII-Datei ({', '.join(ASCII_EXTENSIONS)}) in: {src_dir}")
    pdal_exe = _find_pdal_exe()
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    names = {f: os.path.basename(f) for f in files}

    # Format je Datei erkennen, LV03 schon vor dem Einlesen abfangen
    kinds, not_lv95 = {}, []
    for f in files:
        sniff = sniff_ascii(f)
        if sniff["kind"] == "grid":
            sniff.update(grid_info(f))
        kinds[f] = sniff
        if sniff["crs_guess"] != "LV95":
            not_lv95.append(f"{names[f]}: {sniff['crs_guess']}")
    if not_lv95:
        raise ValueError("ASCII-Daten nicht in LV95 (EPSG:2056): " + ", ".join(not_lv95)
                         + " - LV03 zuerst mit GeoSuite/REFRAME nach LV95 transformieren.")
    longest = max(len(os.path.join(out_dir, tile_name(jahr, area, "0000_0000"))),
                  len(str(work / "p" / "00000" / "t0000_0000.laz")), max(len(f) for f in files))
    if longest > PDAL_MAX_PATH:
        raise ValueError(f"Pfad zu lang fuer pdal.exe ({longest} Zeichen, max. {PDAL_MAX_PATH}) - "
                         "kuerzeren Staging-Ordner waehlen.")

    log(f"Kachelname     : {tile_name(jahr, area, '<NAME>')}")
    log(f"Grid-Shape     : {grid_shape_path}")
    log("Referenzsystem : LV95 / LN02 - keine Umrechnung, CRS-Tag setzt danach Script 4")
    log(f"Parallel       : {workers} PDAL-Prozess(e)")

    # 1) ASCII -> Zwischen-LAZ
    log(f"\n[1/4] ASCII -> LAZ ({len(files)} Datei(en)) ...")
    chunk_bytes = ASCII_CHUNK_MB * 1024 ** 2
    jobs = []  # (worker, args, input-datei, label)
    for i, f in enumerate(files):
        k = kinds[f]
        if k["kind"] == "grid":
            nd = k["nodata"]
            log(f"  {names[f]}: ESRI-ASCII-Grid {k['ncols']} x {k['nrows']} Zellen a {k['cellsize']:g} m, "
                f"NODATA {nd if nd is not None else '- (keiner)'}")
            jobs.append((_grid_to_laz_worker, (f, str(work / f"g{i:03d}.laz"), work / f"g{i:03d}.json",
                                               pdal_exe, nd), f, names[f]))
            continue
        log(f"  {names[f]}: XYZ, Spalten {' '.join(k['columns'])}, Trennzeichen '{k['separator']}', "
            f"{k['skip']} Kopfzeile(n)")
        size_b = os.path.getsize(f)
        n_parts = min(workers, -(-size_b // chunk_bytes)) if workers > 1 else 1
        parts = _split_ascii(f, work / "c" / f"{i:03d}", n_parts) if n_parts > 1 else [f]
        if len(parts) > 1:
            log(f"    {size_b / 1024 ** 2:,.0f} MB -> {len(parts)} Teile (paralleles Einlesen)")
        for j, p in enumerate(parts):
            jobs.append((_xyz_to_laz_worker, (p, str(work / f"s{i:03d}_{j:02d}.laz"),
                                              work / f"s{i:03d}_{j:02d}.json", pdal_exe,
                                              k["columns"], k["separator"], k["skip"] if j == 0 else 0),
                         f, names[f] + (f" [Teil {j + 1}/{len(parts)}]" if len(parts) > 1 else "")))
    sources, origin_of, failed = [], {}, []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, a): (a[0], orig, label) for fn, a, orig, label in jobs}
        for fut in as_completed(futs):
            src, orig, label = futs[fut]
            try:
                dst, count, ignored = fut.result()
                sources.append(dst)
                origin_of[dst] = orig
                log(f"  {label}: {_fmt_count(count)} Punkte"
                    + (f"  - WARNUNG: {ignored} Zeile(n) ignoriert (Spaltenzahl)" if ignored else ""))
            except Exception as e:
                failed.append(label)
                log(f"  {label}: FEHLER - {e}")
            finally:
                if src != orig:
                    _discard(src)  # Teildatei sofort freigeben
    if failed:
        raise RuntimeError(f"{len(failed)} ASCII-Datei(en)/Teil(e) nicht lesbar - Abbruch.")
    sources.sort()

    # 2) Koordinaten, Z, Ueberlappung, Grid
    log("\n[2/4] Pruefen ...")
    headers = {s: _read_las_header(s) for s in sources}
    bad = [f"{names[origin_of[s]]} (X {h['minx']:.0f}-{h['maxx']:.0f}, Y {h['miny']:.0f}-{h['maxy']:.0f})"
           for s, h in headers.items() if _classify_crs(h["minx"], h["miny"], h["maxx"], h["maxy"]) != "LV95"]
    if bad:
        raise ValueError("Punkte ausserhalb LV95 (EPSG:2056): " + ", ".join(bad[:5]))
    file_box = {}
    for s, h in headers.items():
        o, box = origin_of[s], (h["minx"], h["miny"], h["maxx"], h["maxy"])
        old = file_box.get(o, box)
        file_box[o] = (min(old[0], box[0]), min(old[1], box[1]), max(old[2], box[2]), max(old[3], box[3]))
    clashes = _overlapping_pairs(file_box)
    if clashes:
        raise ValueError("ASCII-Dateien im Quellordner ueberlappen raeumlich - es entstuenden doppelte "
                         "Punkte:\n  " + "\n  ".join(f"{names[a]}  <->  {names[b]}" for a, b in clashes[:10])
                         + "\nZ.B. .asc UND .xyz desselben Gebiets? Nur eine Variante in den Quellordner legen.")
    total_in = sum(h["count"] for h in headers.values())
    bbox = (min(h["minx"] for h in headers.values()), min(h["miny"] for h in headers.values()),
            max(h["maxx"] for h in headers.values()), max(h["maxy"] for h in headers.values()))
    zmin = min(h["minz"] for h in headers.values())
    zmax = max(h["maxz"] for h in headers.values())
    log(f"  Koordinaten : LV95 plausibel, X {bbox[0]:.1f}-{bbox[2]:.1f}, Y {bbox[1]:.1f}-{bbox[3]:.1f}")
    log(f"  Z-Bereich   : {zmin:.2f} - {zmax:.2f} m")
    if zmin < PLAUSIBLE_Z[0] or zmax > PLAUSIBLE_Z[1]:
        log(f"  [WARNUNG] Z ausserhalb {PLAUSIBLE_Z[0]:g}-{PLAUSIBLE_Z[1]:g} m - nicht erkannte "
            "NODATA-Werte oder Ausreisser in der Quelle? Punkte werden unveraendert uebernommen.")
    log(f"  Punkte      : {_fmt_count(total_in)}")
    size, cells = load_grid_cells(grid_shape_path, bbox)
    log(f"  Grid        : {len(cells)} Zelle(n) im Datenbereich, Zellgroesse {size:g} m")

    # 3) Zerlegen in Kachelstuecke
    log("\n[3/4] In Kachelstuecke zerlegen (pdal tile) ...")
    by_cell = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_split_worker, (s, str(work / "p" / f"{i:05d}"), pdal_exe, size))
                for i, s in enumerate(sources)]
        for fut in as_completed(futs):
            for key, path, count in fut.result():
                by_cell.setdefault(key, []).append((path, count))
    outside = sorted(k for k in by_cell if k not in cells)
    outside_points = sum(c for k in outside for _, c in by_cell[k])
    if outside:
        log(f"  [WARNUNG] {_fmt_count(outside_points)} Punkt(e) in {len(outside)} Zelle(n) ausserhalb "
            "des Grid-Shapes - werden nicht geschrieben.")

    # 4) Endkacheln
    jobs = []
    for j, key in enumerate(sorted(k for k in by_cell if k in cells)):
        expected = sum(c for _, c in by_cell[key])
        if expected:
            jobs.append((key, os.path.join(out_dir, tile_name(jahr, area, cells[key])),
                         [p for p, _ in by_cell[key]], expected, work / f"t{j:05d}.json", pdal_exe, size))
    log(f"\n[4/4] {len(jobs)} Kachel(n) schreiben ...")
    total_out, errors = 0, []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(_tile_worker, job) for job in jobs]):
            out_name, count, err = fut.result()
            if err:
                errors.append(f"{out_name}: {err}")
                log(f"  {out_name}: FEHLER - {err}")
            else:
                total_out += count
                log(f"  {out_name}: {_fmt_count(count)} Punkte")
    if errors:
        raise RuntimeError(f"{len(errors)} Kachel(n) fehlgeschlagen - Abbruch.")
    balance = total_in - total_out - outside_points
    log(f"\nASCII -> LAZ: {len(jobs)} Kachel(n), {_fmt_count(total_out)} Punkte"
        + (f", {_fmt_count(outside_points)} ausserhalb Grid" if outside_points else "")
        + ("  |  Punktbilanz OK" if balance == 0 else f"  |  Punktbilanz DIFFERENZ {_fmt_count(balance)}"))
    if balance != 0:
        raise RuntimeError("Punktbilanz stimmt nicht (Input != Output) - Abbruch.")
    if not jobs:
        raise RuntimeError("Keine Kachel geschrieben - Daten ausserhalb des Grid-Shapes?")
    return len(jobs)
