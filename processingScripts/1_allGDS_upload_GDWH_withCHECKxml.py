print("\nVersion 2.7.2 (Leica DMC-4: LineID-Format YYYYMMDD_GGGG_QQQQQ_HHMMSS, geht unveraendert ins XML (siehe _line_ids.py), FirstAcquisitionTime/StacItemIdDatetime sekundengenau, BandID aus dem Linienstart der ersten Linie | TIFF-CRS pruefen/setzen: SB_DSM-DSM EPSG:2056+5728, SB_DSM-Hillshade und SB_DOP mit DMC-4 EPSG:2056 | "
      "2.6.1: SB_DSM: historische falsche NoData-Pixel -9999 (LAStools, vom frueheren Extract-by-Mask nicht erfasst) werden automatisch auf den echten NoData-Wert korrigiert, siehe fix_dsm_false_nodata | Opt: parallele Kachel-Verarbeitung/Kopieren via ThreadPoolExecutor fuer SB_DOP/SB_DOP_16/SB_DSM/SB_DSM_PUNKTWOLKE, files.csv weiterhin deterministisch/seriell geschrieben | Bugfixes: WKT-Polygon, CSV-Leerzeile, GDAL-Handles, src-Parameter, Index-Guards | Stabilität: Log-Cleanup vollständig, Pfadprüfung, makedirs-Timing | Opt: MD5-Chunks 64KB, Fortschrittsanzeige, Traceback-Logging)\n")

import os
import re
import json
import hashlib
import shutil
import time
import subprocess
import tempfile
import traceback
import numpy as np
from osgeo import gdal, osr
import xml.etree.ElementTree as ET
from xml.dom import minidom
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys

# LineID-Formate je CameraSystem: gemeinsames Modul mit der GUI (liegt daneben)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import _line_ids

# ****************************** Log-Funktion ******************************
# Hinweis: Dieses Script gibt alles auf die Konsole aus. Beim Start via GUI
# faengt die GUI diese Ausgabe ab und schreibt sie in ihr eigenes Log
# (logs\GDWHimport_...). Ein separates Datei-Log wird hier nicht mehr gefuehrt.
# log_file bleibt als None bestehen, da der OSGeo4W-Runner nach dem Lauf
# darauf zugreift (if mod.log_file: ...).
log_file = None

def log(message):
    print(message)

def copy_with_retry(src, dst, retries=3, wait=15):
    """Kopiert src nach dst mit Retry-Logik und Dateigrössen-Verifikation.
    Bei Fehler: partielle Zieldatei löschen, warten, nochmal versuchen.
    """
    for attempt in range(1, retries + 1):
        try:
            shutil.copy2(src, dst)
            if os.path.getsize(src) != os.path.getsize(dst):
                raise IOError(f"Dateigrösse stimmt nicht überein: {os.path.basename(dst)}")
            return
        except Exception as e:
            log(f"  [Kopieren Versuch {attempt}/{retries}] Fehler: {e}")
            try:
                os.remove(dst)
            except Exception:
                pass
            if attempt < retries:
                log(f"  Warte {wait}s, dann nochmal…")
                time.sleep(wait)
            else:
                raise

def copy_with_retry_md5(src, dst, retries=3, wait=15):
    """Wie copy_with_retry, berechnet aber die MD5-Prüfsumme im selben
    Lese-/Schreibdurchgang, statt die Quelldatei danach separat nochmals
    komplett einzulesen (calculate_md5). Bei grossen Dateien über
    Netzlaufwerk spart das einen kompletten zusätzlichen Netzwerk-
    Lesevorgang pro Datei. Gibt den MD5-Hexdigest zurück.
    """
    for attempt in range(1, retries + 1):
        h = hashlib.md5()
        try:
            with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
                for chunk in iter(lambda: fsrc.read(65536), b""):
                    fdst.write(chunk)
                    h.update(chunk)
            shutil.copystat(src, dst)
            if os.path.getsize(src) != os.path.getsize(dst):
                raise IOError(f"Dateigrösse stimmt nicht überein: {os.path.basename(dst)}")
            return h.hexdigest()
        except Exception as e:
            log(f"  [Kopieren Versuch {attempt}/{retries}] Fehler: {e}")
            try:
                os.remove(dst)
            except Exception:
                pass
            if attempt < retries:
                log(f"  Warte {wait}s, dann nochmal…")
                time.sleep(wait)
            else:
                raise

# ****************************** Helper ******************************
def calculate_md5(file_path):
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def define_gds(path):
    gds = path.split("\\")[-2]
    log(f"GDS erkannt: {gds}\n")
    return gds

def parse_line_id_to_hundredths(line_id):
    """
    Parst eine LineID und gibt (date_str, time_str_with_hundredths) zurück.

    Unterstützte Formate im Zeitteil (parts[1]):
        HHMM        -> Sekunden = 00, Hundertstel = 00  -> HH:MM:00.00
        HHMMSS      -> Hundertstel = 00               -> HH:MM:SS.00
        HHMMSSss    -> Hundertstel direkt              -> HH:MM:SS.ss  (2 Stellen Hundertstel)
        HHMMSSsss   -> Millisekunden -> auf Hundertstel runden -> HH:MM:SS.ss

    Gibt zurück: dict mit Schlüsseln year/month/day/hh/mm/ss/hundredths, oder None bei Fehler.
    """
    try:
        parts = line_id.split("_")
        if len(parts) < 2:
            log(f"Ungültiges Line_ID Format (mind. ein '_' erwartet): '{line_id}'")
            return None
        date_str = parts[0]   # z.B. "20230820"
        time_str = parts[1]   # z.B. "0921" oder "092130" oder "09213045" etc.

        hh = int(time_str[0:2])
        mm = int(time_str[2:4])

        remaining = time_str[4:]  # alles nach HHMM

        if len(remaining) == 0:
            # nur HHMM
            ss = 0
            hundredths = 0
        elif len(remaining) == 2:
            # HHMMSS
            ss = int(remaining[0:2])
            hundredths = 0
        elif len(remaining) == 4:
            # HHMMSSss (2 Hundertstelstellen)
            ss = int(remaining[0:2])
            hundredths = int(remaining[2:4])
        elif len(remaining) == 5:
            # HHMMSSsss (3 Millisekunden-Stellen) -> auf Hundertstel runden
            ss = int(remaining[0:2])
            millis = int(remaining[2:5])
            hundredths = round(millis / 10)
            if hundredths >= 100:
                hundredths = 99  # Clamp bei Rundungsüberlauf
        else:
            # Unbekanntes Format: nur HHMMSS nehmen, Rest ignorieren
            ss = int(remaining[0:2]) if len(remaining) >= 2 else 0
            hundredths = 0

        year  = int(date_str[0:4])
        month = int(date_str[4:6])
        day   = int(date_str[6:8])

        return {
            "year": year, "month": month, "day": day,
            "hh": hh, "mm": mm, "ss": ss, "hundredths": hundredths
        }
    except Exception as e:
        log(f"Fehler beim Parsen der LineID '{line_id}': {e}")
        return None

def format_iso8601_hundredths(parsed):
    """
    Gibt ISO8601 mit Hundertstelsekunden zurück:
    z.B. 2023-08-20T09:21:00.00
    """
    if parsed is None:
        return "UNKNOWN"
    return (f"{parsed['year']:04d}-{parsed['month']:02d}-{parsed['day']:02d}"
            f"T{parsed['hh']:02d}:{parsed['mm']:02d}:{parsed['ss']:02d}"
            f".{parsed['hundredths']:02d}")

def format_stac_datetime(parsed):
    """
    Gibt StacItemIdDatetime zurück:
    z.B. 2023-08-20t09210000
    Format: YYYY-MM-DDtHHMMSSss  (Datum mit Bindestrich, kein Doppelpunkt, keine Punkte)
    """
    if parsed is None:
        return "UNKNOWN"
    return (f"{parsed['year']:04d}-{parsed['month']:02d}-{parsed['day']:02d}"
            f"t{parsed['hh']:02d}{parsed['mm']:02d}{parsed['ss']:02d}{parsed['hundredths']:02d}")

def format_first_acquisition(line_id):
    """Formatiert FirstAcquisitionTime mit Hundertstelsekunden (ISO8601)."""
    parsed = parse_line_id_to_hundredths(line_id)
    return format_iso8601_hundredths(parsed)

def line_id_xml_fields(meta_info):
    """
    Alle von den LineIDs abhaengigen XML-Werte: LineID, AcquisitionTimes,
    FirstAcquisitionTime, StacItemIdDatetime, BandID, Year (nur LAZ).

    ADS: LineIDs unveraendert in Eingabe-Reihenfolge (die GUI sortiert bereits).
    Leica DMC-4: LineIDs unveraendert, nur chronologisch sortiert und ohne
    Duplikate (siehe _line_ids.py). Die Zeiten kommen aus dem Linienstart
    (letztes Feld HHMMSS, Sekunden, Hundertstel 00).

    BandID ist bei beiden Kamerasystemen das HHMM des Aufnahmezeitpunkts der
    ersten aufgelisteten Linie (siehe _line_ids.band_id) - bei DMC also der
    Linienstart, NICHT die Gruppennummer.
    """
    line_ids = meta_info.get("Line_ID", [])
    if not line_ids:
        raise ValueError("Keine Line_ID angegeben!")
    camera = meta_info.get("CameraSystem", "")
    xml_ids = _line_ids.xml_line_ids(line_ids, camera)
    if _line_ids.is_dmc(camera):
        times = [_line_ids.dmc_acquisition(l) for l in _line_ids.normalize(line_ids, camera)]
    else:
        times = [parse_line_id_to_hundredths(l) for l in line_ids]
    first = xml_ids[0]
    return {
        "LineID":               ",".join(xml_ids),
        "AcquisitionTimes":     ",".join(format_iso8601_hundredths(t) for t in times),
        "FirstAcquisitionTime": format_iso8601_hundredths(times[0]),
        "StacItemIdDatetime":   format_stac_datetime(times[0]),
        "BandID":               _line_ids.band_id(first, camera),
        "Year":                 first[0:4],
    }

def wkt_footprint(full_file_path):
    if not full_file_path.lower().endswith(('.tif', '.tiff')):
        return ""
    raster = gdal.Open(full_file_path)
    if raster is None:
        return ""
    try:
        gt = raster.GetGeoTransform()
        cols, rows = raster.RasterXSize, raster.RasterYSize
        ulx, uly = gdal.ApplyGeoTransform(gt, 0, 0)
        urx, ury = gdal.ApplyGeoTransform(gt, cols, 0)
        llx, lly = gdal.ApplyGeoTransform(gt, 0, rows)
        lrx, lry = gdal.ApplyGeoTransform(gt, cols, rows)
        return f"POLYGON (({llx} {lly}, {lrx} {lry}, {urx} {ury}, {ulx} {uly}, {llx} {lly}))"
    finally:
        raster = None

def get_raster_attributes(file_path):
    raster = gdal.Open(file_path)
    if raster is None:
        raise FileNotFoundError(f"Konnte Raster nicht öffnen: {file_path}")
    try:
        gt = raster.GetGeoTransform()
        band = raster.GetRasterBand(1)
        px, py = abs(gt[1]), abs(gt[5])
        cols, rows = raster.RasterXSize, raster.RasterYSize
        bx, by = band.GetBlockSize()
        return {
            "CellSize": f"{(px+py)/2:.10g}",
            "BlockSizeX": str(bx),
            "BlockSizeY": str(by),
            "CellCountWidth": str(cols),
            "CellCountHeight": str(rows)
        }
    finally:
        raster = None

def extract_area(filename, GDS):
    """
    Extrahiert den AREA-Namen robust per Regex aus dem Dateinamen.

    SB_DOP:              zwischen '202X_' und '_DOP'
                         Bsp: 2025_PLAINE_MORTE_DOP_...  -> 'PLAINE_MORTE'

    SB_DSM (hillshade):  zwischen '202X_' und '_hillshade'
                         Bsp: 2025_PLAINE_MORTE_hillshade_... -> 'PLAINE_MORTE'

    SB_DSM (DSM):        zwischen '202X_' und '_DSM'
                         Bsp: 2025_PLAINE_MORTE_DSM_...  -> 'PLAINE_MORTE'

    SB_DSM_PUNKTWOLKE:   zwischen '202X_' und '_TIN'
                         Bsp: 2025_PLAINE_MORTE_TIN_...  -> 'PLAINE_MORTE'
    """
    if GDS == "SB_DOP" or GDS == "SB_DOP_16":
        match = re.search(r'20\d{2}_(.+?)_DOP', filename, re.IGNORECASE)

    elif GDS == "SB_DSM":
        if "hillshade" in filename.lower():
            match = re.search(r'20\d{2}_(.+?)_hillshade', filename, re.IGNORECASE)
        else:
            match = re.search(r'20\d{2}_(.+?)_DSM', filename, re.IGNORECASE)

    elif GDS == "SB_DSM_PUNKTWOLKE":
        match = re.search(r'20\d{2}_(.+?)_TIN', filename, re.IGNORECASE)

    else:
        match = None

    if match:
        return match.group(1)
    else:
        log(f"Warnung: AREA konnte nicht aus '{filename}' ermittelt werden (GDS={GDS})")
        return "UNKNOWN"

def extract_tile_lv95(filename):
    """
    Extrahiert TileKey als die zwei Parts direkt vor '_LV95' im Dateinamen.
    Bsp: ..._2601_1136_LV95_LN02.laz -> '2601_1136'
    """
    base = filename.rsplit('.', 1)[0]
    parts = base.split('_')
    try:
        lv95_idx = parts.index('LV95')
        if lv95_idx < 2:
            raise ValueError(f"'LV95' steht an Position {lv95_idx}, mind. 2 Parts davor nötig")
        return parts[lv95_idx - 2] + "_" + parts[lv95_idx - 1]
    except (ValueError, IndexError) as e:
        log(f"Fehler beim Extrahieren des TileKey aus '{filename}': {e}")
        return "UNKNOWN"

def get_nodata_value(filename, GDS, meta_info):
    """
    Gibt den NoData-Wert zurück:
    - SB_DSM + '_hillshade_' im Dateinamen -> immer '255' (1-Band Grayscale)
    - SB_DSM + '_DSM_' im Dateinamen       -> immer '-3.4028235e+38'
    - Alle anderen GDS                     -> aus meta_info["NoData"]
    """
    if GDS == "SB_DSM":
        fn_lower = filename.lower()
        if "_hillshade_" in fn_lower:
            # Hillshade ist 1-Band Grayscale (Type=Byte, ColorInterp=Gray) -
            # ein einzelner Wert wird von tag_nodata_on_raster/tag_mask_on_raster
            # automatisch auf die tatsaechliche Bandanzahl expandiert. Mit einem
            # fixen "255 255 255" wuerde das bei 1 Band nie zutreffen (3 != 1)
            # und NoData-Tag sowie Maske wuerden stillschweigend uebersprungen.
            return "255"
        elif "_dsm_" in fn_lower:
            return "-3.4028235e+38"
    return meta_info.get("NoData", "")


# SB_DSM (DSM-Raster, nicht Hillshade): historischer falscher NoData-Wert aus
# der urspruenglichen LAStools-DSM-Erzeugung, IMMER exakt -9999 (kein
# Toleranzbereich, vom Nutzer bestaetigt). Ein spaeteres "Extract by Mask"
# (ArcMap) hat NoData-Bereiche INNERHALB des Masken-Polygons bereits korrekt
# auf den echten NoData-Wert -3.4028235e+38 umgeschrieben (siehe
# get_nodata_value); Flaechen AUSSERHALB der Maske blieben mit dem alten
# falschen -9999 stehen. Das sind also echte NoData-Pixel, keine gueltigen
# Hoehenwerte - in der Schweiz gibt es keine realen Hoehen unter -9999m.
DSM_FALSE_NODATA_VALUE = -9999.0


def fix_dsm_false_nodata(file_path, false_nodata_value=DSM_FALSE_NODATA_VALUE,
                          nodata_value=None, chunk_rows=1000):
    """
    Korrigiert Pixel mit dem historischen falschen NoData-Wert (siehe
    DSM_FALSE_NODATA_VALUE) auf den echten NoData-Wert (nodata_value) -
    dadurch werden diese Pixel korrekt als NoData erkannt/getaggt, statt als
    (falsche) gueltige Hoehenwerte in die COG/STAC-Statistik einzufliessen.

    Chunk-weiser Lesedurchgang (Chunk-Groesse wie _compute_nodata_mask):
    Chunks mit mindestens einem falschen NoData-Pixel werden neu eingelesen
    und geschrieben. Ohne Treffer wird NICHTS auf die Platte geschrieben
    (Datei bleibt byte-identisch).

    Gibt n_fixed_px (Anzahl korrigierter Pixel) zurueck.
    """
    if nodata_value is None:
        raise ValueError("fix_dsm_false_nodata benoetigt einen echten nodata_value zum Korrigieren.")
    ds = gdal.Open(file_path, gdal.GA_Update)
    if ds is None:
        raise FileNotFoundError(f"Konnte Raster nicht zum Schreiben oeffnen: {file_path}")
    try:
        band = ds.GetRasterBand(1)
        x_size, y_size = ds.RasterXSize, ds.RasterYSize

        n_fixed = 0
        for y_off in range(0, y_size, chunk_rows):
            rows = min(chunk_rows, y_size - y_off)
            arr = band.ReadAsArray(0, y_off, x_size, rows)
            is_false_nodata = (arr == false_nodata_value)
            if is_false_nodata.any():
                n_fixed += int(is_false_nodata.sum())
                arr[is_false_nodata] = nodata_value
                band.WriteArray(arr, 0, y_off)

        return n_fixed
    finally:
        ds.FlushCache()
        ds = None

def normalize_nodata_for_output(GDS, nodata_str):
    """
    SB_DOP: der im GUI gewaehlte NoData-Wert ("weiss" oder "schwarz") dient
    ab jetzt NUR NOCH als Quellwert fuer die Maskenberechnung
    (_compute_nodata_mask, Vergleich gegen den tatsaechlichen Pixelwert).
    Der Wert, der als GDAL-Tag (tag_nodata_on_raster) UND im XML <NoData>
    landet, wird immer auf 0 normalisiert.

    Grund (Vorfall 23.7.2026, per Test in QGIS/STAC verifiziert): die
    STAC-VRT-Pipeline fuellt Luecken zwischen Kacheln (Bereiche ganz ohne
    Quelldatei) mit dem XML-NoData-Wert, waehrend innerhalb einer Kachel
    maskierte Pixel beim gdal_translate-Schritt ohnehin als 0 (schwarz)
    interpretiert werden. Bei "weiss" ergab das zwei unterschiedliche
    NoData-Farben im Resultat (weisse Luecken, schwarze Maske). Mit "0 0 0"
    (bzw. "0 0 0 0") als geschriebenem Wert stimmen beide ueberein.

    SB_DSM/SB_DSM_PUNKTWOLKE haben eigene, feste NoData-Werte und bleiben
    unveraendert.
    """
    if GDS in ("SB_DSM", "SB_DSM_PUNKTWOLKE"):
        return nodata_str
    values = nodata_str.split()
    if not values:
        return nodata_str
    return " ".join("0" for _ in values)

def tag_nodata_on_raster(file_path, nodata_str):
    """
    Schreibt den NoData-Wert zusaetzlich als echten GDAL-Tag auf jedes Band
    des TIFF (per SetNoDataValue), statt ihn nur im XML zu vermerken. Ohne
    diesen Tag fehlt bei einer spaeteren COG-Ableitung im GDWH-Catalog die
    NoData-Angabe im gdalinfo des Ergebnisses.
    """
    values = nodata_str.split()
    if not values:
        return
    ds = gdal.Open(file_path, gdal.GA_Update)
    if ds is None:
        log(f"[WARNUNG] NoData-Tag: '{os.path.basename(file_path)}' konnte nicht zum Schreiben geoeffnet werden.")
        return
    try:
        n_bands = ds.RasterCount
        if len(values) == 1:
            values = values * n_bands
        if len(values) != n_bands:
            log(f"[WARNUNG] NoData-Tag: {len(values)} Wert(e) fuer {n_bands} Baender "
                f"in '{os.path.basename(file_path)}' - uebersprungen.")
            return
        for i in range(1, n_bands + 1):
            ds.GetRasterBand(i).SetNoDataValue(float(values[i - 1]))
    except Exception as e:
        log(f"[WARNUNG] NoData-Tag konnte nicht gesetzt werden fuer '{os.path.basename(file_path)}': {e}")
    finally:
        ds.FlushCache()
        ds = None


def _compute_nodata_mask(ds, nodata_str, rewrite_real_nodata_to_zero=False):
    """
    Liest alle Baender und berechnet die vollstaendige Gueltigkeits-Maske
    im Speicher (0=NoData, 255=gueltig). Ein Pixel gilt nur dann als
    ungueltig, wenn ALLE Baender ihrem jeweiligen NoData-Wert entsprechen
    (analog gdalwarp-Verhalten). Es wird dabei NICHTS auf der Platte
    veraendert - schlaegt das Lesen fehl, bleibt die Datei unberuehrt.
    Gibt None zurueck, wenn die Anzahl NoData-Werte nicht zu den Baendern passt.

    rewrite_real_nodata_to_zero:
      Ausnahme von obiger Garantie: setzt zusaetzlich die RGB-Baender (1-3)
      an allen als NoData erkannten Pixeln direkt auf 0 (nur sinnvoll fuer
      GDS SB_DOP mit historischem NoData-Wert 255,255,255, siehe
      tag_mask_on_raster). Nutzt denselben Chunk-Durchlauf wie die
      Maskenberechnung - kein zusaetzlicher Lese-/Schreibdurchgang.
    """
    values = nodata_str.split()
    n_bands = ds.RasterCount
    if len(values) == 1:
        values = values * n_bands
    if len(values) != n_bands:
        log(f"[WARNUNG] Maske: {len(values)} Wert(e) fuer {n_bands} Baender "
            f"- uebersprungen.")
        return None
    nodata_values = [float(v) for v in values]

    x_size, y_size = ds.RasterXSize, ds.RasterYSize
    full_mask = np.empty((y_size, x_size), dtype=np.uint8)
    n_rgb_bands = min(3, n_bands)

    chunk_rows = 1000
    for y_off in range(0, y_size, chunk_rows):
        rows = min(chunk_rows, y_size - y_off)
        is_nodata = np.ones((rows, x_size), dtype=bool)
        band_arrs = []
        for i in range(1, n_bands + 1):
            band_arr = ds.GetRasterBand(i).ReadAsArray(0, y_off, x_size, rows)
            is_nodata &= (band_arr == nodata_values[i - 1])
            band_arrs.append(band_arr)
        full_mask[y_off:y_off + rows, :] = np.where(is_nodata, 0, 255).astype(np.uint8)

        if rewrite_real_nodata_to_zero and is_nodata.any():
            for i in range(n_rgb_bands):
                arr = band_arrs[i]
                arr[is_nodata] = 0
                ds.GetRasterBand(i + 1).WriteArray(arr, 0, y_off)

    return full_mask


def _raster_has_internal_mask(file_path):
    """
    Prueft, ob file_path bereits eine interne per-Dataset Flag Mask
    (GDAL_TIFF_INTERNAL_MASK, siehe tag_mask_on_raster) besitzt.

    Nur fuer den Fall SB_DOP + NoData 0,0,0 (schwarz) ohne FixFalseNodata
    relevant: dort hat tag_mask_on_raster keinen Pixel-Rewrite als
    Nebenwirkung (dieser existiert nur bei NoData 255,255,255, siehe
    rewrite_real_nodata_to_zero), die Maskenberechnung ist also eine reine,
    seiteneffektfreie Funktion der Pixelwerte. Ist bei unveraenderten Pixeln
    bereits eine Maske vorhanden (z.B. fortgesetzter Lauf auf teilweise
    bereits verarbeiteten Dateien), liefert eine Neuberechnung exakt
    dasselbe Ergebnis - kann also gefahrlos uebersprungen werden.
    """
    try:
        ds = gdal.Open(file_path, gdal.GA_ReadOnly)
    except Exception:
        return False
    if ds is None:
        return False
    try:
        flags = ds.GetRasterBand(1).GetMaskFlags()
        return bool(flags & gdal.GMF_PER_DATASET)
    finally:
        ds = None


def tag_mask_on_raster(file_path, nodata_str, rewrite_real_nodata_to_zero=False):
    """
    Erzeugt zusaetzlich zum NoData-Tag eine interne per-Dataset-Maske
    (GDAL_TIFF_INTERNAL_MASK, 1-bit DEFLATE, im TIFF selbst gespeichert).

    Hintergrund: Bei der COG-Ableitung im GDWH-Catalog wird JPEG-Kompression
    verwendet. Der COG-Treiber schreibt bei JPEG (verlustbehaftet) keinen
    NoData-Wert, da ein exakter Pixelwert nach der Kompression nicht mehr
    garantiert ist. Eine interne Maske bleibt dagegen verlustfrei erhalten
    und wird vom COG-Treiber auch bei JPEG korrekt uebernommen.

    Fail-safe: die Maske wird zuerst vollstaendig im Speicher berechnet
    (_compute_nodata_mask). Erst bei Erfolg wird CreateMaskBand() aufgerufen
    und geschrieben. Schlaegt die Berechnung fehl (z.B. GDAL/NumPy-Fehler),
    bleibt die Datei unveraendert - es kann keine halbfertige "alles
    ungueltig"-Maske mehr auf der Platte landen (siehe Vorfall 22.7.).

    rewrite_real_nodata_to_zero:
      Nur fuer GDS SB_DOP mit historischem NoData-Wert 255,255,255 (weiss):
      schreibt die echten NoData-Pixel in den RGB-Baendern zusaetzlich direkt
      auf 0,0,0, damit Pixelwerte, Flag Mask und NoData-Tag/XML (dieser ist
      bei SB_DOP ohnehin immer auf 0 normalisiert, siehe
      normalize_nodata_for_output) konsistent sind. Siehe _compute_nodata_mask.
    """
    values = nodata_str.split()
    if not values:
        return

    gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", "YES")
    try:
        ds = gdal.Open(file_path, gdal.GA_Update)
    except Exception as e:
        log(f"[WARNUNG] Maske: '{os.path.basename(file_path)}' konnte nicht zum Schreiben geoeffnet werden: {e}")
        return
    if ds is None:
        log(f"[WARNUNG] Maske: '{os.path.basename(file_path)}' konnte nicht zum Schreiben geoeffnet werden.")
        return

    try:
        full_mask = _compute_nodata_mask(ds, nodata_str, rewrite_real_nodata_to_zero=rewrite_real_nodata_to_zero)
        if full_mask is None:
            return

        ds.CreateMaskBand(gdal.GMF_PER_DATASET)
        mask_band = ds.GetRasterBand(1).GetMaskBand()
        y_size, x_size = full_mask.shape
        chunk_rows = 1000
        for y_off in range(0, y_size, chunk_rows):
            rows = min(chunk_rows, y_size - y_off)
            mask_band.WriteArray(full_mask[y_off:y_off + rows, :], 0, y_off)
    except Exception as e:
        log(f"[FEHLER] Maske NICHT gesetzt fuer '{os.path.basename(file_path)}': {e} "
            f"(Datei sollte unveraendert sein, Fehler vor dem Schreiben abgefangen)")
    finally:
        ds.FlushCache()
        ds = None


# ****************************** Koordinatensystem (TIFF) ******************************
# Soll-CRS im GeoTIFF-Tag:
#   SB_DSM DSM: EPSG:2056+5728 - Hoehen in LN02. GDAL schreibt Compound-CRS als
#     GeoTIFF 1.1 mit VerticalGeoKey (OGC 19-008r4).
#   SB_DSM Hillshade: nur EPSG:2056 - reine Darstellung, keine Hoehenwerte (mit
#     Hoehenbezug meldet GDAL sonst faelschlich 'Unit Type: metre').
#   SB_DOP mit Leica DMC-4: nur EPSG:2056 - der Hoehenbezug der Orthorektifizierung
#     (LHN95) gehoert nicht ins TIFF, im XML steht er nur als Text
#     ('(EPSG:2056) CH1903+ / LV95_LHN95', analog LV95_LN02 der anderen GDS).
# Alle anderen Faelle: keine Pruefung, Verhalten wie bisher.
CRS_LV95      = "EPSG:2056"
CRS_LV95_LN02 = "EPSG:2056+5728"

# Grobe LV95-Ausdehnung (CH/FL, grosszuegig gepuffert) - nur fuer Kacheln OHNE
# CRS-Tag: LV03-Koordinaten (6-stellig) fallen klar heraus.
LV95_E_RANGE = (2_400_000, 2_900_000)
LV95_N_RANGE = (1_000_000, 1_350_000)


def tiff_crs_target(GDS, meta_info, filename=""):
    """Soll-CRS im TIFF-Tag der Datei filename oder None (keine Pruefung)."""
    if GDS == "SB_DSM":
        # Hillshade-Erkennung wie get_nodata_value
        return CRS_LV95 if "_hillshade_" in filename.lower() else CRS_LV95_LN02
    if GDS == "SB_DOP" and _line_ids.is_dmc(meta_info.get("CameraSystem")):
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
        return "set"  # Hoehenbezug entfernen, siehe tiff_crs_target
    if info["vertical_epsg"] == "5728":
        return "ok"
    raise ValueError(f"Hoehenbezug in '{info['name']}' ist nicht LN02 (EPSG:5728)")


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


def ensure_tiff_crs(src, files, GDS, meta_info):
    """
    Prueft den CRS-Tag aller TIFF (read-only) und setzt ihn danach, wo noetig
    (siehe tiff_crs_target). Widerspricht auch nur eine Kachel dem Soll, bricht
    der Lauf ab, BEVOR eine Datei veraendert wurde.

    SB_DSM: laesst sich LN02 nicht ins TIFF schreiben (GDAL ohne GeoTIFF 1.1),
    nur Warnung - der Hoehenbezug steht auch im XML und in der STAC-Beschreibung.
    """
    targets = {fn: tiff_crs_target(GDS, meta_info, fn) for fn in files
               if fn.lower().endswith(('.tif', '.tiff'))}
    targets = {fn: t for fn, t in targets.items() if t}
    if not targets:
        return

    actions, errors = {}, []
    for fn, target in targets.items():
        try:
            actions[fn] = decide_crs_action(read_crs_info(os.path.join(src, fn)), target)
        except Exception as e:
            errors.append(f"{fn} (Soll {target}): {e}")
    if errors:
        log("[FEHLER] CRS-Pruefung - Daten pruefen, es wurde nichts veraendert:")
        for e in errors:
            log("   - " + e)
        sys.exit(1)

    to_set = [fn for fn in targets if actions[fn] == "set"]
    not_persisted = [fn for fn in to_set if not set_raster_crs(os.path.join(src, fn), targets[fn])]
    for target in sorted(set(targets.values())):
        group = [fn for fn in targets if targets[fn] == target]
        n_set = sum(1 for fn in group if actions[fn] == "set")
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


# CRS-Tagging fuer SB_DSM_PUNKTWOLKE LAZ-Tiles gibt es hier nicht mehr - das
# uebernimmt jetzt vollstaendig die LAS 1.2 -> LAS 1.4 Vorkonversion
# (4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py), die in _osgeo_runner.py IMMER vor
# files_in_order() laeuft, wenn GDS == "SB_DSM_PUNKTWOLKE". Dort wird das CRS
# byte-exakt aus einer verifizierten swissSURFACE3D-Referenzkachel injiziert
# (siehe Docstring dort) statt nur getaggt - die alte "CRS-Tag setzen"-GUI-
# Option ist deshalb entfallen, dieser Schritt laeuft jetzt immer.



# ****************************** Sicherheits-Checker ******************************
def preview_xml_attributes(src, GDS, meta_info):
    """
    Zeigt eine Vorschau der XML-Attribute an (nur Anzeige, keine Datei-Erstellung).
    Gibt alle Attributwerte jeweils in einer separaten Zeile aus.
    Danach Benutzerabfrage Y/N.
    """

    print("\n==================== SICHERHEITS-CHECK ====================\n")
    print("Beispiel-XML Attribute (nur Vorschau, keine Datei wird erzeugt):\n")

    example_file = None
    for fn in os.listdir(src):
        if fn.lower().endswith(('.tif', '.tiff', '.laz')):
            example_file = fn
            break

    if not example_file:
        print("Keine geeignete Datei für Vorschau gefunden!")
        sys.exit(1)

    AOI = extract_area(example_file, GDS)

    # TileKey aus Beispieldatei ermitteln
    if GDS == "SB_DSM_PUNKTWOLKE":
        example_tilekey = extract_tile_lv95(example_file)
    elif GDS in ["SB_DOP", "SB_DOP_16"]:
        _parts = example_file.rsplit('.', 1)[0].split('_')
        if "LV95" in _parts:
            _idx = _parts.index("LV95")
            if _idx >= 2:
                example_tilekey = _parts[_idx - 2] + "_" + _parts[_idx - 1]
            else:
                example_tilekey = f"FEHLER – 'LV95' steht zu früh (Position {_idx})"
        else:
            example_tilekey = "NICHT GEFUNDEN – 'LV95' fehlt im Dateinamen!"
    elif GDS == "SB_DSM":
        example_tilekey = "1000  (fix für SB_DSM)"
    else:
        _parts = example_file.rsplit('.', 1)[0].split('_')
        example_tilekey = _parts[-2] + "_" + _parts[-1] if len(_parts) >= 2 else "UNBEKANNT"

    print("CHECK-ref.SYS.; ReferenzSystem OK?: ", src)
    print(meta_info.get("Auftragstyp", ""))
    print(AOI)
    print(f"TileKey (Beispiel aus '{example_file}'): {example_tilekey}")
    print(meta_info.get("CustomAttribute", ""))
    print(meta_info.get("CameraSystem", ""))
    print(meta_info.get("SourceReferenceSystem", ""))
    print(meta_info.get("TerrainModel", ""))
    if GDS != "SB_DSM_PUNKTWOLKE":
        print(f"NoData: {get_nodata_value(example_file, GDS, meta_info)}")
    else:
        print("NoData: <LAZ hat kein noData-Value>")
        print("CRS-Zuweisung: bereits durch die LAS 1.2 -> LAS 1.4 Vorkonversion "
              "(4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py) erledigt (EPSG:2056+5728, siehe Log davor).")
    if GDS == "SB_DSM":
        print(f"TIFF-CRS: wird geprueft und bei Bedarf gesetzt, Soll DSM {CRS_LV95_LN02}, "
              f"Hillshade {CRS_LV95}")
    elif tiff_crs_target(GDS, meta_info):
        print(f"TIFF-CRS: wird geprueft und bei Bedarf gesetzt, Soll {tiff_crs_target(GDS, meta_info)}")

    # Wirft bei ungueltigen LineIDs (z.B. DMC-Format bei ADS) - Abbruch vor der Verarbeitung
    fields = line_id_xml_fields(meta_info)
    print(f"LineID: {fields['LineID']}")
    print(f"FirstAcquisitionTime: {fields['FirstAcquisitionTime']}")
    print(f"StacItemIdDatetime: {fields['StacItemIdDatetime']}")

    print("\n============================================================\n")

    decision = input("Script wirklich starten? (Y/N): ").strip().upper()

    if decision != "Y":
        print("Script wurde vom Benutzer abgebrochen.\n")
        sys.exit(0)

# ****************************** XML Creation ******************************
def create_xml(file_path, GDS, meta_info, cached_raster_attrs=None):
    filename = os.path.basename(file_path)

    # AREA: robust aus Dateiname extrahieren - ausser die GUI liefert eine
    # manuell im Meta-Informationen-Feld "Area" gesetzte/korrigierte
    # Ueberschreibung (z.B. weil das Dateinamen-Format nicht passt).
    AOI = meta_info.get("AreaOverride") or extract_area(filename, GDS)

    root = ET.Element("MetaObject", {"xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance"})

    for key, val in {
        "Auftragstyp": meta_info.get("Auftragstyp", ""),
        "Area": AOI,
        "TerrainModel": meta_info.get("TerrainModel", ""),
        "CameraSystem": meta_info.get("CameraSystem", ""),
        "CoordinateReferenceSystem": meta_info.get("CoordinateReferenceSystem", meta_info.get("SourceReferenceSystem", "")),
        "Commentary": meta_info.get("Commentary", meta_info.get("CustomAttribute", ""))
    }.items():
        ET.SubElement(root, key).text = val

    # NoData: automatisch für SB_DSM, sonst aus meta_info
    # NoData nur schreiben, wenn NICHT SB_DSM_PUNKTWOLKE
    if GDS != "SB_DSM_PUNKTWOLKE":
        ET.SubElement(root, "NoData").text = normalize_nodata_for_output(
            GDS, get_nodata_value(filename, GDS, meta_info))

    # LineID-abhaengige Werte (ADS/DMC-4), Zeiten mit Hundertstelsekunden,
    # StacItemIdDatetime z.B. 2023-08-20t09210000
    fields = line_id_xml_fields(meta_info)
    for key in ("LineID", "AcquisitionTimes", "FirstAcquisitionTime", "StacItemIdDatetime"):
        ET.SubElement(root, key).text = fields[key]
    if fields["BandID"]:
        ET.SubElement(root, "BandID").text = fields["BandID"]

    if file_path.lower().endswith(('.tif', '.tiff')):
        attrs = cached_raster_attrs if cached_raster_attrs is not None else get_raster_attributes(file_path)
        for k, v in attrs.items():
            ET.SubElement(root, k).text = v
    elif file_path.lower().endswith('.laz'):
        # Year aus Line_ID ableiten (erste 4 Ziffern = Jahr)
        ET.SubElement(root, "Year").text = fields["Year"]

    xml_path = file_path.rsplit('.', 1)[0] + ".xml"
    pretty = minidom.parseString(ET.tostring(root, 'utf-8')).toprettyxml(indent="    ")
    pretty = "\n".join([line for line in pretty.split("\n") if line.strip() != ""])
    with open(xml_path, 'w', encoding='utf-8') as f:
        f.write(pretty)
    return xml_path, AOI, fields["FirstAcquisitionTime"]

# ****************************** CSV & Kopieren ******************************
def _csv_append(csv_path, row):
    """
    Hängt eine Zeile an die CSV-Datei an.
    FIX: Kein führendes '\\n' bei einer neuen/leeren Datei (war: jeder Eintrag
         begann mit '\\n', was bei einer neuen Datei eine Leerzeile erzeugte).
    CSV-Zeileninhalt (row) bleibt identisch.
    """
    file_exists_with_content = os.path.isfile(csv_path) and os.path.getsize(csv_path) > 0
    with open(csv_path, 'a', encoding='utf-8') as f:
        f.write(("\n" if file_exists_with_content else "") + row)

def _build_csv_rows_and_copy(output_path, full_file_path, GDS):
    """Kopiert die Datei(en) an ihr NV-/PrecalculatedFormats-Ziel (inkl. MD5
    im selben Lese-/Schreibdurchgang) und gibt die dabei entstandenen
    files.csv-Zeile(n) als Liste zurueck - OHNE sie zu schreiben.

    Das Schreiben passiert bewusst erst seriell im Hauptthread (siehe
    files_in_order/_process_tile): diese Funktion laeuft unter
    ThreadPoolExecutor parallel pro Kachel, files.csv darf aber nicht aus
    mehreren Threads gleichzeitig beschrieben werden und soll ausserdem
    deterministisch in der urspruenglichen Dateireihenfolge entstehen,
    unabhaengig davon welcher Worker zuerst fertig wird."""
    name = os.path.basename(full_file_path)
    name_parts = name.rsplit('.', 1)[0].split('_')
    ext = os.path.splitext(name)[1].lower()

    # === SB_DSM ===
    if GDS == "SB_DSM":
        subfolder = "HILLSHADE" if "hillshade" in name.lower() else "DSM"
        dst_folder = os.path.join(output_path, "NV", subfolder)
        os.makedirs(dst_folder, exist_ok=True)
        md5 = None
        for fext in [ext, ".xml", ".tfw"]:
            src = full_file_path.rsplit('.', 1)[0] + fext
            if os.path.exists(src):
                dst = os.path.join(dst_folder, os.path.basename(src))
                if fext == ext:
                    # Hauptdatei: MD5 im selben Lese-/Schreibdurchgang wie
                    # das Kopieren berechnen, statt hinterher nochmals
                    # komplett einzulesen.
                    md5 = copy_with_retry_md5(src, dst)
                else:
                    copy_with_retry(src, dst)
        tilekey = "1000"
        return [f"NV\\{subfolder}\\{name};{md5};{tilekey};add;{wkt_footprint(full_file_path)}"]

    # === SB_DSM_PUNKTWOLKE ===
    elif GDS == "SB_DSM_PUNKTWOLKE" and ext == ".laz":
        # TileKey: die zwei Parts direkt vor '_LV95' (robust, von hinten)
        tilekey = extract_tile_lv95(name)

        # Hauptziel: NV\SB_DSM_PUNKTWOLKE - MD5 waehrend diesem Kopiervorgang
        # berechnen (spart den separaten calculate_md5-Lesevorgang).
        dst_nv = os.path.join(output_path, "NV", "SB_DSM_PUNKTWOLKE")
        os.makedirs(dst_nv, exist_ok=True)
        dst_nv_file = os.path.join(dst_nv, name)
        md5 = copy_with_retry_md5(full_file_path, dst_nv_file)

        xml_src = full_file_path.rsplit('.', 1)[0] + ".xml"
        if os.path.exists(xml_src):
            copy_with_retry(xml_src, os.path.join(dst_nv, os.path.basename(xml_src)))

        # Zweites Ziel: PrecalculatedFormats\SB_DSM_PUNKTWOLKE (anderer
        # Zieldateiname, identischer Inhalt). Quelle dafuer ist die soeben
        # geschriebene dst_nv_file, NICHT nochmal full_file_path - spart den
        # zweiten vollen Lesevorgang der (ggf. entfernten) Original-Quelle.
        dst_pre = os.path.join(output_path, "PrecalculatedFormats", "SB_DSM_PUNKTWOLKE")
        os.makedirs(dst_pre, exist_ok=True)
        new_name = f"SB_DSM_PUNKTWOLKE_LAZ_CHLV95_LN02_{tilekey}.laz"
        copy_with_retry(dst_nv_file, os.path.join(dst_pre, new_name))

        row_nv  = f"NV\\SB_DSM_PUNKTWOLKE\\{name};{md5};{tilekey};add;"
        row_pre = f"PrecalculatedFormats\\SB_DSM_PUNKTWOLKE\\{new_name};{md5};{tilekey};add;"
        return [row_nv, row_pre]

    # === SB_DOP / SB_DOP_16 ===
    elif GDS in ["SB_DOP", "SB_DOP_16"]:
        # MD5 und files.csv-Zeile fuer die TIF-Datei werden bewusst NICHT
        # hier berechnet, sondern erst in create_and_copy_order() beim
        # tatsaechlichen Kopieren dorthin - spart einen kompletten
        # zusaetzlichen Netzwerk-Lesevorgang der Quelldatei (vorher: hier
        # volle MD5-Lesung, spaeter beim Kopieren nochmals volles Lesen).
        # Der xml-Sidecar ist klein, dessen separate Kopie hier bleibt.
        xml_src = full_file_path.rsplit('.', 1)[0] + ".xml"
        if os.path.exists(xml_src):
            nv_path = os.path.join(output_path, "NV")
            os.makedirs(nv_path, exist_ok=True)
            copy_with_retry(xml_src, os.path.join(nv_path, os.path.basename(xml_src)))
        return []

    # === Default ===
    else:
        tile = name_parts[-2] + "_" + name_parts[-1]
        md5 = calculate_md5(full_file_path)
        return [f"NV\\{name};{md5};{tile};add;"]

# ****************************** Hauptlogik ******************************
def cleanup_input_folder(src, GDS):
    """Loescht vor der Verarbeitung Altbestaende im Quellordner.

    Pro GDS wird eine Whitelist der erlaubten Endungen definiert. Alles, was
    nicht auf der Whitelist steht, wird geloescht (z.B. alte .xml, .ovr, .cpg,
    .dbf, .lock, .pyr, .rdx, .lax). So enthaelt der Ordner vor dem Erstellen
    der neuen XML nur noch die eigentlichen Nutzdaten.

        SB_DOP / SB_DOP_16 / SB_DSM : nur .tif/.tiff/.tfw bleiben
        SB_DSM_PUNKTWOLKE           : nur .laz/.ascii bleiben

    Fuer unbekannte GDS wird nichts geloescht (Sicherheitsnetz).
    """
    whitelists = {
        "SB_DOP":            {".tif", ".tiff", ".tfw"},
        "SB_DOP_16":         {".tif", ".tiff", ".tfw"},
        "SB_DSM":            {".tif", ".tiff", ".tfw"},
        "SB_DSM_PUNKTWOLKE": {".laz", ".ascii"},
    }

    keep = whitelists.get(GDS)
    if keep is None:
        log(f"Bereinigung uebersprungen (kein Regelsatz fuer GDS '{GDS}').")
        return

    deleted = 0
    for fn in os.listdir(src):
        fp = os.path.join(src, fn)
        if not os.path.isfile(fp):
            continue
        if os.path.splitext(fn)[1].lower() not in keep:
            try:
                os.remove(fp)
                log(f"[BEREINIGT] geloescht: {fn}")
                deleted += 1
            except Exception as e:
                log(f"[WARNUNG] konnte {fn} nicht loeschen: {e}")

    erlaubt = "/".join(sorted(keep))
    if deleted == 0:
        log(f"Bereinigung: nichts zu loeschen (erlaubt: {erlaubt}).")
    else:
        log(f"Bereinigung: {deleted} Datei(en) geloescht (behalten: {erlaubt}).")


# ****************************** Parallelisierung ******************************
def _default_worker_count():
    """Anzahl paralleler Worker-Threads: reserviert 2 Kerne fuer OS/GUI/andere
    Prozesse, nutzt den Rest bis maximal 8 - identisches Kalkuel wie
    4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py::_default_worker_count()."""
    cpu = os.cpu_count() or 4
    return max(1, min(cpu - 2, 8))


def _process_tile(fn, src, out, GDS, meta, cached_attrs):
    """Verarbeitet eine einzelne Kachel (XML, NoData-Tag, Maske, Kopieren,
    files.csv-Zeile(n) berechnen). Jede Kachel ist unabhaengig (eigene
    Datei, kein gemeinsamer Zustand) - GDAL-Rasteroperationen und
    hashlib.md5 geben den GIL bei grossen Arrays/Buffern frei, wodurch
    mehrere Kacheln unter ThreadPoolExecutor echt parallel auf mehreren
    Kernen verarbeitet werden koennen (siehe files_in_order). Schreibt
    bewusst NICHT direkt in files.csv - der Aufrufer haengt die
    zurueckgegebenen csv_rows seriell im Hauptthread an, in der
    urspruenglichen Dateireihenfolge, unabhaengig davon welcher Worker
    zuerst fertig wird."""
    fp = os.path.join(src, fn)
    try:
        create_xml(fp, GDS, meta, cached_raster_attrs=cached_attrs)
        if GDS != "SB_DSM_PUNKTWOLKE" and fn.lower().endswith(('.tif', '.tiff')):
            nodata_str = get_nodata_value(fn, GDS, meta)
            # SB_DSM DSM-Raster (nicht Hillshade) bewusst OHNE interne Maske:
            # der NoData-Tag war hier bereits vorher korrekt gesetzt, die
            # zusaetzliche Maske fuehrte in STAC/Kartenviewer zu falscher
            # noData-Darstellung (Vorfall 23.7.2026). Fuer Hillshade bleibt
            # die Maske wie gehabt bestehen.
            is_sb_dsm_raster = GDS == "SB_DSM" and "_hillshade_" not in fn.lower()

            if is_sb_dsm_raster:
                # Historischer falscher NoData-Wert (siehe DSM_FALSE_NODATA_VALUE):
                # sehr wenige Pixel mit fixem Wert -9999, IMMER exakt - echte
                # NoData-Pixel, die vom frueheren "Extract by Mask" nicht erfasst
                # wurden. Vor dem NoData-Tag auf den echten NoData-Wert korrigieren,
                # damit sie korrekt als NoData erkannt werden - laeuft automatisch,
                # keine GUI-Option noetig.
                n_fixed = fix_dsm_false_nodata(
                    fp, nodata_value=float(nodata_str) if nodata_str else None)
                if n_fixed:
                    log(f"  {fn}: {n_fixed} falsche NoData-Pixel ({DSM_FALSE_NODATA_VALUE}) "
                        f"auf NoData ({nodata_str}) korrigiert.")

            if nodata_str:
                # nodata_str (Quellwert, z.B. "255 255 255") wird nur fuer die
                # Maskenberechnung verwendet. Der geschriebene GDAL-Tag nutzt
                # den normalisierten Wert (SB_DOP: immer 0, siehe
                # normalize_nodata_for_output).
                tag_nodata_on_raster(fp, normalize_nodata_for_output(GDS, nodata_str))
                # SB_DOP mit vorgeschalteter Vorkorrektur (3_fix_false_nodata_dop.py,
                # via GUI-Option): die Flag Mask wurde dort bereits direkt beim
                # Korrigieren der Pixel geschrieben (spart einen zusaetzlichen
                # vollstaendigen Lese-/Schreibdurchgang pro Tile) - hier nicht
                # nochmals berechnen. Der NoData-Tag oben wird trotzdem wie
                # gehabt gesetzt.
                mask_already_set = GDS == "SB_DOP" and meta.get("FixFalseNodata")
                if not is_sb_dsm_raster and not mask_already_set:
                    # Historische 255er-NoData-DOPs: echte NoData-Pixel
                    # gleich beim Maske-Berechnen auf 0,0,0 umschreiben
                    # (siehe README "Historische 255er-NoData-DOPs").
                    rewrite_to_zero = (GDS == "SB_DOP"
                                       and all(float(v) == 255 for v in nodata_str.split()))
                    # SB_DOP mit NoData 0,0,0 (kein Pixel-Rewrite, siehe
                    # rewrite_to_zero oben): Maskenberechnung ist hier
                    # seiteneffektfrei, deshalb Skip erlaubt, falls die
                    # Datei bereits eine Maske hat (z.B. fortgesetzter
                    # Lauf). Bei 255,255,255 bewusst NICHT skippen - dort
                    # ist die Maskenberechnung mit dem Pixel-Rewrite
                    # gekoppelt (siehe _raster_has_internal_mask).
                    skip_if_present = GDS == "SB_DOP" and not rewrite_to_zero
                    if skip_if_present and _raster_has_internal_mask(fp):
                        log(f"  Flag Mask bereits vorhanden, Berechnung uebersprungen: {fn}")
                    else:
                        tag_mask_on_raster(fp, nodata_str, rewrite_real_nodata_to_zero=rewrite_to_zero)
        rows = _build_csv_rows_and_copy(out, fp, GDS)
        return {"status": "ok", "error": None, "traceback": None, "rows": rows}
    except Exception as e:
        return {"status": "failed", "error": str(e), "traceback": traceback.format_exc(), "rows": []}


def files_in_order(src, out, GDS, meta, workers=None):
    missing_xml = []

    # Altbestaende bereinigen (GDS-spezifische Whitelist),
    # bevor neue XML erzeugt werden.
    cleanup_input_folder(src, GDS)

    # SB_DSM_PUNKTWOLKE: die LAS 1.2 -> LAS 1.4 Vorkonversion (inkl. CRS-Tag,
    # siehe 4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py) ist an dieser Stelle bereits
    # erledigt - sie laeuft in _osgeo_runner.py IMMER vor files_in_order().

    # OPT: Dateien zuerst sammeln → ermöglicht Fortschrittsanzeige [i/n]
    files = [
        fn for fn in os.listdir(src)
        if os.path.isfile(os.path.join(src, fn))
        and fn.lower().endswith(('.tif', '.tiff', '.laz'))
    ]

    # CRS-Tag pruefen/setzen (SB_DSM, SB_DOP mit DMC-4) - bricht ab, bevor eine
    # Kachel veraendert wird, falls eine dem Soll widerspricht
    ensure_tiff_crs(src, files, GDS, meta)

    # Für Kacheldatensätze: Raster-Attribute nur einmal lesen und für alle XML wiederverwenden.
    # SB_DSM muss weiterhin jede Datei einzeln öffnen (unterschiedliche Dimensionen/Typen).
    cached_attrs = None
    if GDS in ["SB_DOP", "SB_DOP_16"] and files:
        first_tif = next((fn for fn in files if fn.lower().endswith(('.tif', '.tiff'))), None)
        if first_tif:
            cached_attrs = get_raster_attributes(os.path.join(src, first_tif))
            log(f"Raster-Attribute aus '{first_tif}' gecacht (gilt für alle {len(files)} XML).\n")

    if workers is None:
        workers = _default_worker_count()
    workers = max(1, min(workers, len(files) or 1))

    results = {}
    if workers <= 1 or len(files) <= 1:
        for fn in files:
            log(f"Verarbeite: {fn}")
            results[fn] = _process_tile(fn, src, out, GDS, meta, cached_attrs)
    else:
        log(f"Parallelisierung: {workers} gleichzeitige Worker (verfuegbare Kerne: {os.cpu_count()}).")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_fn = {
                executor.submit(_process_tile, fn, src, out, GDS, meta, cached_attrs): fn
                for fn in files
            }
            done = 0
            for future in as_completed(future_to_fn):
                fn = future_to_fn[future]
                done += 1
                log(f"[{done}/{len(files)}] verarbeitet: {fn}")
                try:
                    results[fn] = future.result()
                except Exception as e:
                    results[fn] = {"status": "failed", "error": f"Unerwarteter Fehler im Worker: {e}",
                                   "traceback": traceback.format_exc(), "rows": []}

    # files.csv seriell im Hauptthread schreiben, in der urspruenglichen
    # Dateireihenfolge - siehe _process_tile/_build_csv_rows_and_copy.
    csv_path = os.path.join(out, 'files.csv')
    for fn in files:
        result = results[fn]
        if result["status"] == "ok":
            for row in result["rows"]:
                _csv_append(csv_path, row)
        else:
            # OPT: Vollständiger Traceback im Log für einfacheres Debugging
            log(f"Fehler bei {fn}: {result['error']}")
            log(result["traceback"])
            missing_xml.append(fn)

    if missing_xml:
        log("Fehler: Einige XML wurden nicht erstellt:")
        for f in missing_xml:
            log("   - " + f)
        sys.exit(1)
    else:
        log("Alle XML-Dateien erfolgreich erstellt und kopiert.\n")
    # Log-Datei bleibt offen – wird auf Script-Ebene im finally-Block geschlossen,
    # damit auch der nachfolgende create_and_copy_order-Aufruf noch ins Log schreibt.

# ****************************** DOP-Kopieren ******************************
def create_and_copy_order(out, src, GDS, workers=None):
    if GDS in ["SB_DOP", "SB_DOP_16"]:
        nv_path = os.path.join(out, "NV")
        csv_path = os.path.join(out, "files.csv")
        os.makedirs(nv_path, exist_ok=True)

        tif_files = sorted(fn for fn in os.listdir(src) if fn.lower().endswith('.tif'))
        tfw_files = sorted(fn for fn in os.listdir(src) if fn.lower().endswith('.tfw'))

        def _copy_tif(fn):
            # MD5 + files.csv-Zeile im selben Lese-/Schreibdurchgang wie das
            # eigentliche Kopieren berechnen (spart den vorher separaten,
            # vollen MD5-Lesevorgang der Quelldatei) - Zeile wird
            # zurueckgegeben statt direkt geschrieben, siehe Aufrufer unten.
            name_parts = fn.rsplit('.', 1)[0].split('_')
            if "LV95" not in name_parts:
                raise ValueError(f"LV95 nicht im Dateinamen gefunden: {fn}")
            lv95_index = name_parts.index("LV95")
            if lv95_index < 2:
                raise ValueError(f"'LV95' steht zu früh im Dateinamen (Position {lv95_index}): {fn}")
            tile = name_parts[lv95_index - 2] + "_" + name_parts[lv95_index - 1]
            md5 = copy_with_retry_md5(os.path.join(src, fn), os.path.join(nv_path, fn))
            return f"NV\\{fn};{md5};{tile};add;"

        if workers is None:
            workers = _default_worker_count()
        workers = max(1, min(workers, len(tif_files) or 1))

        rows = {}
        errors = []
        if workers <= 1 or len(tif_files) <= 1:
            for fn in tif_files:
                try:
                    rows[fn] = _copy_tif(fn)
                except Exception as e:
                    errors.append((fn, e))
        else:
            log(f"Parallelisierung: {workers} gleichzeitige Kopier-Worker (verfuegbare Kerne: {os.cpu_count()}).")
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_fn = {executor.submit(_copy_tif, fn): fn for fn in tif_files}
                for future in as_completed(future_to_fn):
                    fn = future_to_fn[future]
                    try:
                        rows[fn] = future.result()
                    except Exception as e:
                        errors.append((fn, e))

        # files.csv seriell im Hauptthread schreiben, in der urspruenglichen
        # (sortierten) Dateireihenfolge - nur fuer erfolgreich kopierte
        # Dateien. Anders als vorher (rein serielle Schleife, brach beim
        # ersten Fehler sofort ab) werden hier alle TIFs versucht, bevor am
        # Ende eine Exception geworfen wird, falls welche fehlgeschlagen sind
        # - der Lauf gilt damit weiterhin wie bisher als fehlgeschlagen,
        # es bleibt aber nichts unbemerkt unversucht.
        for fn in tif_files:
            if fn in rows:
                _csv_append(csv_path, rows[fn])

        if errors:
            details = "; ".join(f"{fn}: {e}" for fn, e in errors)
            raise RuntimeError(f"{len(errors)} TIF(s) konnten nicht kopiert werden: {details}")

        for fn in tfw_files:
            copy_with_retry(os.path.join(src, fn), os.path.join(nv_path, fn))

        log("DOP-Dateien kopiert.\n")

# ****************************** Working Part ******************************
# Wird nur ausgeführt wenn das Script direkt gestartet wird (nicht bei Import).
# Beim Start via GUI (GUI_GDWHimport.py) werden Pfade und meta_info
# als Subprocess-Config übergeben und die Funktionen direkt aufgerufen.

if __name__ == "__main__":

    Quelle = r"A:\20XX\AOI\DSM\LV95_LN02\ORIGINAL"
    Ziel = r"\\v0t0020a.adr.admin.ch\iprod\gdwh-ingest\BUCKET_INT\RASTER\SB_DSM\20XX_AOI_DSM"
            # --> für GDS "SB_DSM_PUNKTWOLKE" Settings beim Datenpacket kontrollieren:
                # beim erstellen des Datenpackets in GDWH, nur folgende Attribute Wählen, Rest leerlassen.
                    #(Wählen: "Name", "LayerRealeaseKey", "ReleaseModelKey") (leer: "ReleaseKey" und "FullExportFileNameKey")

    GDS = define_gds(Ziel)

    # *********************** Meta-Information *******************************

    meta_info = {
        "Auftragstyp": "kry",
            # kontrollieren:
            # "kry"
            # "ram"
            # "bim"
            # "mom"
            # "wam"
        "CustomAttribute": "Digital Surface Model  - Raster Mosaic (DSM photogrammetric autocorrelation)" ,
            # kontrollieren;
            # "Digital Surface Model  - Raster Mosaic (DSM photogrammetric autocorrelation)"
            # "Digital Surface Model - PointCloud LAZ (DSM photogrammetric autocorrelation)"
            # "Digital OrthoPhoto - Mosaic RGB 8BIT"
            # Leica DMC-4:
            # "Digital OrthoPhoto - Mosaic RGBN 8BIT"
            # "Digital Surface Model - PointCloud LAZ RGB (DSM photogrammetric autocorrelation)"
        "Line_ID": ["20200913_1054_12501", "20200913_1104_12501"],
            # kontrollieren;
            # (!)Alle LineIDs(!) des Mosaiks angeben!
            # ADS: erste LineID (!)muss(!) die erste BefliegungsLinie (AufnahmeZeitpunkt) des AOIs sein!
            #(z.B.: "20200821_0952_12504", "20200821_1009_12504", "20200821_1026_12504")
            # Leica DMC-4: Format YYYYMMDD_GGGG_QQQQQ_HHMMSS, Reihenfolge egal
            # (wird nur sortiert, geht unveraendert ins XML, siehe _line_ids.py)
            #(z.B.: "20260813_0822_41216_082221", "20260813_0822_41216_082750")
        "NoData": "0 0 0",
            # kontrollieren! Typische Werte:
            # "0 0 0"    /   "255 255 255"   (8BIT, 3-Band RGB TIF)
            # Hinweis: Für GDS "SB_DSM" wird NoData automatisch gesetzt:
            #   - '_hillshade_' im Dateinamen -> "255 255 255"
            #   - '_DSM_'       im Dateinamen -> "-3.4028235e+38"
            #   (dieser Wert wird dann ignoriert)
            # Hinweis: Für GDS "SB_DSM_PUNKTWOLKE" gibt es kein NoData-Value
        "TerrainModel": "Digital Surface Model (DSM photogrammetric autocorrelation)",
            # kontrollieren;
            # "Digital Surface Model (DSM photogrammetric autocorrelation)"
            # "swissALTI3D"
            # "swissALTI3D/DHM25"
            # "swissSURFACE3D"
        "SourceReferenceSystem": "(EPSG:2056) CH1903+ / LV95_LN02",
            # INPUT kontrollieren! only possible Value:
            # ("EPSG:2056) CH1903+ / LV95_LN02"
            # Ausnahme SB_DOP mit Leica DMC-4: "(EPSG:2056) CH1903+ / LV95_LHN95"
        "CameraSystem": "Leica ADS100",
            # kontrollieren;
            # "Leica ADS100"
            # "Leica ADS80"
            # "Leica DMC-4"
    }

    # ======================================================================

    if not os.path.isdir(Quelle):
        sys.exit(f"\nFEHLER: Quellordner nicht gefunden:\n  {Quelle}\n")

    # Sicherheitsvorschau anzeigen
    preview_xml_attributes(Quelle, GDS, meta_info)

    # Processierung wenn YES
    files_in_order(Quelle, Ziel, GDS, meta_info)
    create_and_copy_order(Ziel, Quelle, GDS)
