"""
_tiff_crs.py  –  CRS-Tag im GeoTIFF pruefen und setzen.

Gemeinsam genutzt von Script 1 (SB_DOP, SB_DSM), Script 2_2 (SB_DOP_16) und der
CRS-Vorpruefung im GUI (Aufruf via OSGeo4W Python, siehe main()).

Soll-CRS im GeoTIFF-Tag:
  SB_DSM DSM: EPSG:2056+5728 - Hoehen in LN02. GDAL schreibt Compound-CRS als
    GeoTIFF 1.1 mit VerticalGeoKey (OGC 19-008r4).
  SB_DSM Hillshade: nur EPSG:2056 - reine Darstellung, keine Hoehenwerte (mit
    Hoehenbezug meldet GDAL sonst faelschlich 'Unit Type: metre').
  SB_DOP / SB_DOP_16 (alle CameraSysteme): nur EPSG:2056 - DOP ohne Hoehenwerte,
    kein Hoehenbezug im TIFF (das XML-Feld CoordinateReferenceSystem ist davon
    unabhaengig, siehe SOURCE_REF_SYS im GUI).
  SB_DSM_PUNKTWOLKE: keine Pruefung hier (LAZ, CRS setzt die LAS-1.4-Vorkonversion).

DOP mit Hoehenbezug im Tag:
  LN02 (EPSG:5728) wird still entfernt - LN02 ist die Referenz der uebrigen Daten.
  Jeder andere Hoehenbezug (z.B. LHN95, EPSG:5729) ist eine WARNUNG: Es ist zu
  pruefen, ob nur der Tag falsch gesetzt wurde oder ob mit dem falschen
  Hoehenbezug orthorektifiziert wurde (Lageversatz = Hoehenfehler x tan(Blickwinkel),
  v.a. in steilem Gelaende und am Bildrand). Bestaetigung im GUI vor dem Start,
  im Script per input() (im GUI-Lauf automatisch 'Y').

DOP mit fremdem/unbekanntem horizontalem CRS (z.B. 'unnamed'), Koordinaten aber
  in der LV95-Ausdehnung ('retag'): standardmaessig Abbruch wie 'error'. Nur wenn
  im GUI bestaetigt (meta_info[FORCE_LV95_KEY]), wird der Tag auf EPSG:2056
  ueberschrieben - Pixel und Geotransformation bleiben unveraendert. Liegen die
  Koordinaten ausserhalb (z.B. LV03), bleibt es ein harter Fehler.

Aufruf (GUI-Vorpruefung, nur lesend):
    <osgeo_python> _tiff_crs.py <config.json>
    config.json: {"gds": "...", "meta_info": {...}, "files": ["<voller Pfad>", ...]}
    Ausgabe: letzte Zeile auf stdout = JSON (siehe check_files)
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from osgeo import gdal, osr

gdal.UseExceptions()

CRS_LV95      = "EPSG:2056"
CRS_LV95_LN02 = "EPSG:2056+5728"
VERTICAL_LN02 = "5728"
DOP_GDS       = ("SB_DOP", "SB_DOP_16")
# meta_info-Flag aus dem GUI: 'retag'-Kacheln auf EPSG:2056 umtaggen
FORCE_LV95_KEY = "ForceCrsLV95"

# Grobe LV95-Ausdehnung (CH/FL, grosszuegig gepuffert) - nur fuer Kacheln OHNE
# CRS-Tag: LV03-Koordinaten (6-stellig) fallen klar heraus.
LV95_E_RANGE = (2_400_000, 2_900_000)
LV95_N_RANGE = (1_000_000, 1_350_000)

# Header lesen ist I/O-gebunden (Netzlaufwerk) - Threads reichen
CHECK_WORKERS = 8


def tiff_crs_target(GDS, meta_info, filename=""):
    """Soll-CRS im TIFF-Tag der Datei filename oder None (keine Pruefung)."""
    if GDS == "SB_DSM":
        # Hillshade-Erkennung wie get_nodata_value in Script 1
        return CRS_LV95 if "_hillshade_" in filename.lower() else CRS_LV95_LN02
    if GDS in DOP_GDS:
        return CRS_LV95
    return None


def _in_lv95_extent(gt, cols, rows):
    corners = ((0, 0), (cols, 0), (0, rows), (cols, rows))
    xs = [gt[0] + c * gt[1] + r * gt[2] for c, r in corners]
    ys = [gt[3] + c * gt[4] + r * gt[5] for c, r in corners]
    return (LV95_E_RANGE[0] <= min(xs) and max(xs) <= LV95_E_RANGE[1]
            and LV95_N_RANGE[0] <= min(ys) and max(ys) <= LV95_N_RANGE[1])


def read_crs_info(file_path):
    """Liest das CRS eines TIFF (read-only) als Dict fuer decide_crs_action."""
    ds = gdal.Open(file_path, gdal.GA_ReadOnly)
    if ds is None:
        raise FileNotFoundError(f"Konnte Raster nicht öffnen: {file_path}")
    try:
        srs = ds.GetSpatialRef()
        in_extent = _in_lv95_extent(ds.GetGeoTransform(), ds.RasterXSize, ds.RasterYSize)
    finally:
        ds = None

    info = {"has_crs": srs is not None, "in_lv95_extent": in_extent, "name": "",
            "is_compound": False, "horizontal_is_lv95": False, "vertical_epsg": None}
    if srs is None:
        return info

    lv95 = osr.SpatialReference()
    lv95.ImportFromEPSG(2056)
    info["name"] = srs.GetName() or ""
    info["is_compound"] = bool(srs.IsCompound())
    if not info["is_compound"]:
        # IsSame statt EPSG-Code: auch ein gleichwertiges ESRI-WKT ohne ID gilt als LV95
        info["horizontal_is_lv95"] = bool(srs.IsSame(lv95))
        return info
    if hasattr(srs, "StripVertical"):
        horizontal = srs.Clone()
        horizontal.StripVertical()
        info["horizontal_is_lv95"] = bool(horizontal.IsSame(lv95))
    else:
        # GDAL < 3.6 kennt StripVertical nicht
        info["horizontal_is_lv95"] = srs.GetAuthorityCode("PROJCS") == "2056"
    info["vertical_epsg"] = srs.GetAuthorityCode("VERT_CS")
    return info


def decide_crs_action(info, target):
    """
    'ok' (Tag stimmt) oder 'set' (Tag setzen). ValueError, wenn das CRS im TIFF
    dem Soll widerspricht: dann wird bewusst NICHT umgetaggt, sonst passten
    Koordinaten und CRS nicht mehr zusammen (z.B. LV03-Daten als LV95 getaggt,
    LHN95-Hoehen als LN02).
    """
    with_ln02 = target == CRS_LV95_LN02
    if not info["has_crs"]:
        if info["in_lv95_extent"]:
            return "set"
        raise ValueError(f"kein CRS im TIFF und Koordinaten ausserhalb LV95 - "
                         f"{target} wird nicht gesetzt")
    if not info["horizontal_is_lv95"]:
        raise ValueError(f"CRS '{info['name']}' ist nicht LV95 (EPSG:2056)")
    if not info["is_compound"]:
        return "set" if with_ln02 else "ok"
    if not with_ln02:
        return "set"  # Hoehenbezug entfernen - bei DOP siehe vertical_warning
    if info["vertical_epsg"] == VERTICAL_LN02:
        return "ok"
    raise ValueError(f"Hoehenbezug in '{info['name']}' ist nicht LN02 (EPSG:5728)")


def vertical_warning(GDS, info):
    """Meldung, wenn ein DOP-TIFF einen anderen Hoehenbezug als LN02 traegt (z.B.
    LHN95), sonst None. Hillshade (SB_DSM) bleibt ohne Warnung - der Hoehenbezug
    des DSM wird dort separat und strikt geprueft."""
    if GDS not in DOP_GDS or not info.get("is_compound"):
        return None
    vertical = info.get("vertical_epsg")
    if vertical == VERTICAL_LN02:
        return None
    return f"Hoehenbezug '{info.get('name', '')}' (EPSG:{vertical or 'unbekannt'})"


def retag_allowed(GDS, info):
    """True, wenn ein DOP-TIFF ein fremdes/unbekanntes horizontales CRS traegt, die
    Koordinaten aber in der LV95-Ausdehnung liegen: dann ist nur der Tag falsch
    (in diesem Wertebereich kommt praktisch nur LV95 in Frage) und darf nach
    Bestaetigung ueberschrieben werden."""
    return (GDS in DOP_GDS and info.get("has_crs") and not info.get("horizontal_is_lv95")
            and bool(info.get("in_lv95_extent")))


def _check_one(path, GDS, meta_info):
    fn = os.path.basename(path)
    target = tiff_crs_target(GDS, meta_info, fn)
    if not target:
        return fn, None, None, None
    try:
        info = read_crs_info(path)
    except Exception as e:
        return fn, target, "error", str(e)
    try:
        action = decide_crs_action(info, target)
    except Exception as e:
        if retag_allowed(GDS, info):
            return fn, target, "retag", f"{e}, Koordinaten in LV95"
        return fn, target, "error", str(e)
    warning = vertical_warning(GDS, info)
    return fn, target, ("warn" if warning else action), warning


def check_files(GDS, meta_info, paths, workers=CHECK_WORKERS):
    """Prueft die CRS-Tags aller TIFF in paths (nur lesend). Ergebnis:
    {"ok": [fn], "set": [fn], "warn": [[fn, meldung]], "retag": [[fn, meldung]],
     "error": [[fn, meldung]], "targets": {fn: soll}} - 'warn'-Dateien werden beim
    Setzen wie 'set' behandelt, 'retag'-Dateien nur mit FORCE_LV95_KEY."""
    paths = [p for p in paths if p.lower().endswith(('.tif', '.tiff'))]
    result = {"ok": [], "set": [], "warn": [], "retag": [], "error": [], "targets": {}}
    if not paths:
        return result
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(paths)))) as executor:
        checked = list(executor.map(lambda p: _check_one(p, GDS, meta_info), paths))
    for fn, target, action, message in checked:
        if not target:
            continue
        result["targets"][fn] = target
        if action in ("warn", "retag", "error"):
            result[action].append([fn, message if action == "warn" else f"{message} (Soll {target})"])
        else:
            result[action].append(fn)
    return result


def set_raster_crs(file_path, target):
    """Schreibt target als CRS-Tag - nur die GeoKeys, Pixel und Geotransformation
    bleiben unveraendert. True, wenn das CRS danach dem Soll entspricht."""
    srs = osr.SpatialReference()
    srs.SetFromUserInput(target)
    ds = gdal.Open(file_path, gdal.GA_Update)
    if ds is None:
        raise IOError(f"Konnte Raster nicht zum Schreiben oeffnen: {file_path}")
    try:
        if ds.SetSpatialRef(srs) != 0:
            raise IOError(f"CRS konnte nicht gesetzt werden: {file_path}")
    finally:
        ds.FlushCache()
        ds = None
    try:
        return decide_crs_action(read_crs_info(file_path), target) == "ok"
    except ValueError:
        return False


def ensure_tiff_crs(src, files, GDS, meta_info, log=print):
    """
    Prueft den CRS-Tag aller TIFF (read-only) und setzt ihn danach, wo noetig
    (siehe tiff_crs_target). Widerspricht auch nur eine Kachel dem Soll, bricht
    der Lauf ab, BEVOR eine Datei veraendert wurde. DOP mit falschem Hoehenbezug
    (vertical_warning): Warnung und Rueckfrage, bei 'Y' wird auf EPSG:2056 gesetzt.
    DOP mit fremdem CRS in LV95-Ausdehnung (retag_allowed): nur mit
    meta_info[FORCE_LV95_KEY] (GUI-Bestaetigung) umgetaggt, sonst Abbruch.

    SB_DSM: laesst sich LN02 nicht ins TIFF schreiben (GDAL ohne GeoTIFF 1.1),
    nur Warnung - der Hoehenbezug steht auch im XML und in der STAC-Beschreibung.
    """
    res = check_files(GDS, meta_info, [os.path.join(src, fn) for fn in files])
    targets = res["targets"]
    if not targets:
        return

    force = bool(meta_info.get(FORCE_LV95_KEY))
    errors = res["error"] + ([] if force else res["retag"])
    if errors:
        log("[FEHLER] CRS-Pruefung - Daten pruefen, es wurde nichts veraendert:")
        for fn, msg in errors:
            log(f"   - {fn}: {msg}")
        sys.exit(1)

    if res["retag"]:
        log(f"[WARNUNG] {len(res['retag'])} DOP-Datei(en) mit fremdem CRS-Tag, Koordinaten "
            f"in LV95 - im GUI bestaetigt, Tag wird auf {CRS_LV95} ueberschrieben:")
        for fn, msg in res["retag"]:
            log(f"   - {fn}: {msg}")

    if res["warn"]:
        log(f"[WARNUNG] {len(res['warn'])} DOP-Datei(en) mit falschem Hoehenbezug im CRS-Tag "
            f"- pruefen, ob nur der Tag falsch ist oder mit dem falschen Hoehenbezug "
            f"orthorektifiziert wurde:")
        for fn, msg in res["warn"]:
            log(f"   - {fn}: {msg}")
        decision = input(f"Tags auf {CRS_LV95} setzen und fortfahren? (Y/N): ").strip().upper()
        if decision != "Y":
            log("Abbruch durch Benutzer - es wurde nichts veraendert.")
            sys.exit(1)
        log(f"Bestaetigt - Tags werden auf {CRS_LV95} gesetzt.")

    to_set = res["set"] + [fn for fn, _ in res["warn"] + res["retag"]]
    not_persisted = [fn for fn in to_set if not set_raster_crs(os.path.join(src, fn), targets[fn])]
    for target in sorted(set(targets.values())):
        group = [fn for fn in targets if targets[fn] == target]
        n_set = sum(1 for fn in group if fn in to_set)
        log(f"CRS-Tag {target}: {len(group) - n_set} Datei(en) bereits korrekt, {n_set} gesetzt.")

    ln02_failed  = [fn for fn in not_persisted if targets[fn] == CRS_LV95_LN02]
    other_failed = [fn for fn in not_persisted if targets[fn] != CRS_LV95_LN02]
    if ln02_failed:
        log(f"[WARNUNG] LN02 (EPSG:5728) liess sich bei {len(ln02_failed)} Datei(en) nicht "
            f"ins TIFF schreiben (GDAL {gdal.__version__}) - horizontal EPSG:2056 gesetzt, "
            f"LN02 steht im XML: " + ", ".join(ln02_failed))
    if other_failed:
        log("[FEHLER] CRS konnte nicht gesetzt werden: " + ", ".join(other_failed))
        sys.exit(1)
    log("")


def main():
    """GUI-Vorpruefung: liest nur, veraendert keine Datei."""
    try:
        with open(sys.argv[1], encoding="utf-8") as f:
            cfg = json.load(f)
        res = check_files(cfg["gds"], cfg.get("meta_info", {}), cfg["files"])
    except Exception as e:
        print(json.dumps({"failed": str(e)}, ensure_ascii=False), flush=True)
        sys.exit(1)
    print(json.dumps(res, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
