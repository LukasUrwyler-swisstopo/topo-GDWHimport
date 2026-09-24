print("\nVersion GDS_DOP_16: 16BIT , 2.1.0 (Opt: parallele Kachel-Verarbeitung/Kopieren via ThreadPoolExecutor, files.csv weiterhin deterministisch/seriell geschrieben)\n")

import os
import re
import time
import xml.etree.ElementTree as ET
import hashlib
import shutil
import numpy as np
from osgeo import gdal
from xml.dom import minidom
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys

gdal.UseExceptions()

# ****************************** Log-Functions ******************************
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

# ****************************** Helper Functions ******************************

def calculate_md5(file_path):
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def define_gds(path):
    GDS = os.path.basename(os.path.dirname(path))
    log(f"GDS erkannt: {GDS}\n")
    return GDS

def wkt_footprint(full_file_path):
    """Berechnet WKT-Footprint eines Rasters. Wird aktuell für SB_DSM benötigt."""
    raster_layer = gdal.Open(full_file_path)
    # Hinweis: mit gdal.UseExceptions() wirft gdal.Open() bereits eine Exception,
    # bevor None zurückgegeben wird — der Check bleibt als Sicherheitsnetz.
    if raster_layer is None:
        raise FileNotFoundError(f"Raster konnte nicht geöffnet werden: {full_file_path}")
    try:
        gt = raster_layer.GetGeoTransform()
        cols = raster_layer.RasterXSize
        rows = raster_layer.RasterYSize
        ulx, uly = gdal.ApplyGeoTransform(gt, 0, 0)
        urx, ury = gdal.ApplyGeoTransform(gt, cols, 0)
        llx, lly = gdal.ApplyGeoTransform(gt, 0, rows)
        lrx, lry = gdal.ApplyGeoTransform(gt, cols, rows)
        return f"POLYGON (({llx:.3f} {lly:.3f}, {lrx:.3f} {lry:.3f}, {urx:.3f} {ury:.3f}, {ulx:.3f} {uly:.3f}, {llx:.3f} {lly:.3f}))"
    finally:
        raster_layer = None  # GDAL-Dataset explizit freigeben

def parse_line_id_to_hundredths(line_id):
    """
    Parst eine LineID und gibt ein dict mit Datums- und Zeitkomponenten zurück.

    Unterstützte Formate im Zeitteil (parts[1]):
        HHMM        -> Sekunden = 00, Hundertstel = 00  -> HH:MM:00.00
        HHMMSS      -> Hundertstel = 00               -> HH:MM:SS.00
        HHMMSSss    -> Hundertstel direkt              -> HH:MM:SS.ss
        HHMMSSsss   -> Millisekunden -> auf Hundertstel runden -> HH:MM:SS.ss
    """
    try:
        parts = line_id.split("_")
        if len(parts) < 2:
            raise ValueError(f"LineID '{line_id}' hat zu wenige Teile (mind. 2, getrennt durch '_')")
        date_str = parts[0]   # z.B. "20230820"
        time_str = parts[1]   # z.B. "0921" oder "092130" etc.

        hh = int(time_str[0:2])
        mm = int(time_str[2:4])
        remaining = time_str[4:]

        if len(remaining) == 0:
            ss = 0
            hundredths = 0
        elif len(remaining) == 2:
            ss = int(remaining[0:2])
            hundredths = 0
        elif len(remaining) == 4:
            ss = int(remaining[0:2])
            hundredths = int(remaining[2:4])
        elif len(remaining) == 5:
            ss = int(remaining[0:2])
            millis = int(remaining[2:5])
            hundredths = round(millis / 10)
            if hundredths >= 100:
                hundredths = 99
        else:
            ss = int(remaining[0:2]) if len(remaining) >= 2 else 0
            hundredths = 0

        return {
            "year": int(date_str[0:4]), "month": int(date_str[4:6]), "day": int(date_str[6:8]),
            "hh": hh, "mm": mm, "ss": ss, "hundredths": hundredths
        }
    except (ValueError, IndexError) as e:
        log(f"Fehler beim Parsen der LineID '{line_id}': {e}")
        return None

def format_iso8601_hundredths(parsed):
    """Gibt ISO8601 mit Hundertstelsekunden zurück: z.B. 2023-08-20T09:21:00.00"""
    if parsed is None:
        return "UNKNOWN"
    return (f"{parsed['year']:04d}-{parsed['month']:02d}-{parsed['day']:02d}"
            f"T{parsed['hh']:02d}:{parsed['mm']:02d}:{parsed['ss']:02d}"
            f".{parsed['hundredths']:02d}")

def format_stac_datetime(parsed):
    """Gibt StacItemIdDatetime zurück: z.B. 2023-08-20t09210000"""
    if parsed is None:
        return "UNKNOWN"
    return (f"{parsed['year']:04d}-{parsed['month']:02d}-{parsed['day']:02d}"
            f"t{parsed['hh']:02d}{parsed['mm']:02d}{parsed['ss']:02d}{parsed['hundredths']:02d}")

def format_acquisition_time(line_id):
    """Formatiert eine LineID als ISO8601-Zeitstempel mit Hundertstelsekunden."""
    return format_iso8601_hundredths(parse_line_id_to_hundredths(line_id))

def get_raster_attributes(file_path):
    raster_layer = gdal.Open(file_path)
    # Hinweis: mit gdal.UseExceptions() wirft gdal.Open() bereits eine Exception,
    # bevor None zurückgegeben wird — der Check bleibt als Sicherheitsnetz.
    if raster_layer is None:
        raise FileNotFoundError(f"Raster konnte nicht geöffnet werden: {file_path}")
    try:
        gt = raster_layer.GetGeoTransform()
        band = raster_layer.GetRasterBand(1)
        px, py = abs(gt[1]), abs(gt[5])
        cols, rows = raster_layer.RasterXSize, raster_layer.RasterYSize
        bx, by = band.GetBlockSize()
        return {
            "CellSize": f"{(px + py) / 2:.10g}",
            "BlockSizeX": str(bx),
            "BlockSizeY": str(by),
            "CellCountWidth": str(cols),
            "CellCountHeight": str(rows)
        }
    finally:
        raster_layer = None  # GDAL-Dataset explizit freigeben

def extract_area(filename):
    """Extrahiert AREA zwischen Jahr (202X_) und _DOP im Dateinamen.
    Beispiel: 2025_PLAINE_MORTE_DOP_... -> 'PLAINE_MORTE'
    Funktioniert auch bei einwortigen Areas wie 'BERN'.
    """
    match = re.search(r'20\d{2}_(.+?)_DOP', filename)
    return match.group(1) if match else "UNKNOWN"

def extract_tile(filename):
    """Extrahiert TileKey als die zwei Parts vor '_LV95' im Dateinamen.
    Beispiel: ..._2602_1145_LV95.tif -> '2602_1145'
    """
    base = filename.rsplit('.', 1)[0]
    parts = base.split('_')
    try:
        lv95_idx = parts.index('LV95')
        return parts[lv95_idx - 2] + "_" + parts[lv95_idx - 1]
    except (ValueError, IndexError) as e:
        log(f"Fehler beim Extrahieren des TileKey aus '{filename}': {e}")
        return "UNKNOWN"

# ****************************** Sicherheits-Check ******************************
def preview_xml_attributes(src, meta_info):
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

    AOI = extract_area(example_file)
    example_tilekey = extract_tile(example_file)

    print("CHECK-ref.SYS.; ReferenzSystem OK?: ", src)
    print(meta_info.get("Auftragstyp", ""))
    print(AOI)
    print(f"TileKey (Beispiel aus '{example_file}'): {example_tilekey}")
    print(meta_info.get("CustomAttribute", ""))
    print(meta_info.get("CameraSystem", ""))
    print(meta_info.get("SourceReferenceSystem", ""))
    print(meta_info.get("TerrainModel", ""))
    print(f"NoData: {meta_info.get('NoData', '')}")

    all_area_ids = meta_info.get("allAreaLineIDs", [])
    print(",".join(all_area_ids))
    acq_times = [format_acquisition_time(l) for l in all_area_ids]
    print(",".join(acq_times))

    line_ids = meta_info.get("Line_ID", [])
    print(",".join(line_ids))

    if line_ids:
        print(format_acquisition_time(line_ids[0]))

    print("\n============================================================\n")

    decision = input("Script wirklich starten? (Y/N): ").strip().upper()

    if decision != "Y":
        print("Script wurde vom Benutzer abgebrochen.\n")
        sys.exit(0)

# ****************************** XML & CSV Functions ******************************

def create_xml(file_path, GDS, meta_info, cached_raster_attrs=None):
    filename = os.path.basename(file_path)

    # AREA: robust aus Dateiname extrahieren (zwischen Jahr und _DOP) - ausser
    # die GUI liefert eine manuell im Meta-Informationen-Feld "Area" gesetzte/
    # korrigierte Ueberschreibung (z.B. weil das Dateinamen-Format nicht passt).
    AOI = meta_info.get("AreaOverride") or extract_area(filename)

    root = ET.Element("MetaObject", {"xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance"})

    # Standardfelder
    fields = {
        "Auftragstyp": meta_info.get("Auftragstyp", ""),
        "Area": AOI,
        "TerrainModel": meta_info.get("TerrainModel", ""),
        "CameraSystem": meta_info.get("CameraSystem", ""),
        "CoordinateReferenceSystem": meta_info.get("CoordinateReferenceSystem", meta_info.get("SourceReferenceSystem", "")),
        "Commentary": meta_info.get("Commentary", meta_info.get("CustomAttribute", ""))
    }

    for k, v in fields.items():
        ET.SubElement(root, k).text = v

    # NoData aus meta_info (normalisiert, siehe normalize_nodata_for_output)
    ET.SubElement(root, "NoData").text = normalize_nodata_for_output(meta_info.get("NoData", ""))

    line_ids = meta_info.get("Line_ID", [])
    if not line_ids:
        raise ValueError("Mindestens eine Line_ID muss angegeben werden!")
    ET.SubElement(root, "LineID").text = ",".join(line_ids)

    # AcquisitionTimes aus allAreaLineIDs mit Hundertstelsekunden
    all_area_ids = meta_info.get("allAreaLineIDs", [])
    if not all_area_ids:
        raise ValueError("Meta-Attribut 'allAreaLineIDs' muss mindestens eine LineID enthalten!")

    formatted_times = [format_acquisition_time(x) for x in all_area_ids]
    ET.SubElement(root, "AcquisitionTimes").text = ",".join(formatted_times)

    # FirstAcquisitionTime aus erster Line_ID mit Hundertstelsekunden
    first_parsed = parse_line_id_to_hundredths(line_ids[0])
    first_time = format_iso8601_hundredths(first_parsed)
    ET.SubElement(root, "FirstAcquisitionTime").text = first_time

    # StacItemIdDatetime: z.B. 2023-08-20t09210000
    ET.SubElement(root, "StacItemIdDatetime").text = format_stac_datetime(first_parsed)

    # BandID aus Line_ID
    if len(line_ids[0]) >= 13:
        ET.SubElement(root, "BandID").text = line_ids[0][9:13]

    # Raster-Attribute
    if file_path.lower().endswith(('.tif', '.tiff')):
        attrs = cached_raster_attrs if cached_raster_attrs is not None else get_raster_attributes(file_path)
        for k, v in attrs.items():
            ET.SubElement(root, k).text = v

    xml_filename = file_path.rsplit('.', 1)[0] + ".xml"
    rough_string = ET.tostring(root, 'utf-8')
    reparsed = minidom.parseString(rough_string)
    formatted_xml = reparsed.toprettyxml(indent="    ")
    formatted_xml = "\n".join([line for line in formatted_xml.split("\n") if line.strip() != ""])
    with open(xml_filename, 'w', encoding="utf-8") as f:
        f.write(formatted_xml)


def normalize_nodata_for_output(nodata_str):
    """
    Der im GUI gewaehlte NoData-Wert ("weiss" oder "schwarz") dient ab jetzt
    NUR NOCH als Quellwert fuer die Maskenberechnung (_compute_nodata_mask,
    Vergleich gegen den tatsaechlichen Pixelwert). Der Wert, der als GDAL-Tag
    (tag_nodata_on_raster) UND im XML <NoData> landet, wird immer auf 0
    normalisiert.

    Grund (Vorfall 23.7.2026, per Test in QGIS/STAC verifiziert, siehe
    Skript 1 / README): die STAC-VRT-Pipeline fuellt Luecken zwischen
    Kacheln (Bereiche ganz ohne Quelldatei) mit dem XML-NoData-Wert, waehrend
    innerhalb einer Kachel maskierte Pixel beim gdal_translate-Schritt
    ohnehin als 0 (schwarz) interpretiert werden. Bei "weiss" ergab das zwei
    unterschiedliche NoData-Farben im Resultat. Mit "0 0 0 0" als
    geschriebenem Wert stimmen beide ueberein.
    """
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
    try:
        ds = gdal.Open(file_path, gdal.GA_Update)
    except Exception as e:
        log(f"[WARNUNG] NoData-Tag: '{os.path.basename(file_path)}' konnte nicht zum Schreiben geoeffnet werden: {e}")
        return
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


def _compute_nodata_mask(ds, nodata_str):
    """
    Liest alle Baender und berechnet die vollstaendige Gueltigkeits-Maske
    im Speicher (0=NoData, 255=gueltig). Ein Pixel gilt nur dann als
    ungueltig, wenn ALLE Baender ihrem jeweiligen NoData-Wert entsprechen
    (analog gdalwarp-Verhalten). Es wird dabei NICHTS auf der Platte
    veraendert - schlaegt das Lesen fehl, bleibt die Datei unberuehrt.
    Gibt None zurueck, wenn die Anzahl NoData-Werte nicht zu den Baendern passt.
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

    chunk_rows = 1000
    for y_off in range(0, y_size, chunk_rows):
        rows = min(chunk_rows, y_size - y_off)
        is_nodata = np.ones((rows, x_size), dtype=bool)
        for i in range(1, n_bands + 1):
            band_arr = ds.GetRasterBand(i).ReadAsArray(0, y_off, x_size, rows)
            is_nodata &= (band_arr == nodata_values[i - 1])
        full_mask[y_off:y_off + rows, :] = np.where(is_nodata, 0, 255).astype(np.uint8)

    return full_mask


def tag_mask_on_raster(file_path, nodata_str):
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
        full_mask = _compute_nodata_mask(ds, nodata_str)
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


def _build_file_csv_row(full_file_path, GDS):
    """Berechnet Tile-Key und MD5 und gibt die fertige files.csv-Zeile
    zurueck (OHNE sie zu schreiben - siehe _process_tile/files_in_order:
    das Schreiben passiert bewusst erst seriell im Hauptthread)."""
    filename = os.path.basename(full_file_path)
    name_parts = filename.rsplit('.', 1)[0].split('_')

    if GDS == "SB_DOP":
        tile = name_parts[4] + "_" + name_parts[5]
    elif GDS == "SB_DOP_16":
        tile = extract_tile(filename)
    elif GDS == "SB_DSM":
        tile = "1000"
    elif GDS == "SB_DSM_PUNKTWOLKE":
        tile = name_parts[-4] + "_" + name_parts[-3]
    else:
        tile = ""

    md5_hash = calculate_md5(full_file_path)
    return f"NV\\{filename};{md5_hash};{tile};add;"


def _append_csv_row(output_path, row):
    csv_file_path = os.path.join(output_path, 'files.csv')
    file_is_new = not os.path.exists(csv_file_path) or os.path.getsize(csv_file_path) == 0
    with open(csv_file_path, 'a', encoding='utf-8') as f:
        if not file_is_new:
            f.write("\n")
        f.write(row)


# ****************************** Parallelisierung ******************************
def _default_worker_count():
    """Anzahl paralleler Worker-Threads: reserviert 2 Kerne fuer OS/GUI/andere
    Prozesse, nutzt den Rest bis maximal 8 - identisches Kalkuel wie
    4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py::_default_worker_count()."""
    cpu = os.cpu_count() or 4
    return max(1, min(cpu - 2, 8))


def _process_tile(fn, path, GDS, meta_info, cached_attrs):
    """Verarbeitet eine einzelne Kachel (XML, NoData-Tag, Maske, files.csv-
    Zeile berechnen). Jede Kachel ist unabhaengig (eigene Datei, kein
    gemeinsamer Zustand) - GDAL-Rasteroperationen und hashlib.md5 geben den
    GIL bei grossen Arrays/Buffern frei, wodurch mehrere Kacheln unter
    ThreadPoolExecutor echt parallel auf mehreren Kernen verarbeitet werden
    koennen (siehe files_in_order). Schreibt bewusst NICHT direkt in
    files.csv - der Aufrufer haengt die zurueckgegebene csv_row seriell im
    Hauptthread an, in der urspruenglichen (sortierten) Dateireihenfolge,
    unabhaengig davon welcher Worker zuerst fertig wird."""
    full_file_path = os.path.join(path, fn)
    try:
        create_xml(full_file_path, GDS, meta_info, cached_raster_attrs=cached_attrs)
        if fn.lower().endswith(('.tif', '.tiff')):
            nodata_str = meta_info.get("NoData", "")
            if nodata_str:
                # nodata_str (Quellwert, z.B. "65535 65535 65535 65535") wird nur
                # fuer die Maskenberechnung verwendet. Der geschriebene GDAL-Tag
                # nutzt den normalisierten Wert (immer 0, siehe
                # normalize_nodata_for_output).
                tag_nodata_on_raster(full_file_path, normalize_nodata_for_output(nodata_str))
                tag_mask_on_raster(full_file_path, nodata_str)
        return {"status": "ok", "error": None, "csv_row": _build_file_csv_row(full_file_path, GDS)}
    except Exception as e:
        return {"status": "failed", "error": str(e), "csv_row": None}


# ****************************** Main Functions ******************************

def files_in_order(path, output_path, GDS, meta_info, workers=None):
    # Output-Verzeichnis sicherstellen — files.csv wird direkt hinein geschrieben
    os.makedirs(output_path, exist_ok=True)

    all_files = sorted(fn for fn in os.listdir(path)
                       if os.path.isfile(os.path.join(path, fn))
                       and fn.lower().endswith(('.tif', '.tiff', '.las', '.laz')))

    # Raster-Attribute nur einmal aus erster Datei lesen (alle Kacheln gleich gross)
    cached_attrs = None
    first_tif = next((fn for fn in all_files if fn.lower().endswith(('.tif', '.tiff'))), None)
    if first_tif:
        cached_attrs = get_raster_attributes(os.path.join(path, first_tif))
        log(f"Raster-Attribute aus '{first_tif}' gecacht (gilt für alle {len(all_files)} XML).\n")

    if workers is None:
        workers = _default_worker_count()
    workers = max(1, min(workers, len(all_files) or 1))

    results = {}
    if workers <= 1 or len(all_files) <= 1:
        for fn in all_files:
            log(f"Verarbeite Datei: {fn}")
            results[fn] = _process_tile(fn, path, GDS, meta_info, cached_attrs)
    else:
        log(f"Parallelisierung: {workers} gleichzeitige Worker (verfuegbare Kerne: {os.cpu_count()}).")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_fn = {
                executor.submit(_process_tile, fn, path, GDS, meta_info, cached_attrs): fn
                for fn in all_files
            }
            done = 0
            for future in as_completed(future_to_fn):
                fn = future_to_fn[future]
                done += 1
                log(f"[{done}/{len(all_files)}] verarbeitet: {fn}")
                try:
                    results[fn] = future.result()
                except Exception as e:
                    results[fn] = {"status": "failed", "error": f"Unerwarteter Fehler im Worker: {e}", "csv_row": None}

    # files.csv seriell im Hauptthread schreiben, in der urspruenglichen
    # (sortierten) Dateireihenfolge - siehe _process_tile-Docstring.
    for fn in all_files:
        result = results[fn]
        if result["status"] == "ok":
            _append_csv_row(output_path, result["csv_row"])
        else:
            log(f"Fehler bei Datei {fn}: {result['error']}")

def create_and_copy_order(output_path, input_path, GDS, workers=None):
    new_order_path = os.path.join(output_path, 'NV')
    os.makedirs(new_order_path, exist_ok=True)

    if GDS == "SB_DSM":
        os.makedirs(os.path.join(new_order_path, 'HILLSHADE'), exist_ok=True)
        os.makedirs(os.path.join(new_order_path, 'DSM'), exist_ok=True)
    if GDS == "SB_DSM_PUNKTWOLKE":
        os.makedirs(os.path.join(new_order_path, 'SB_DSM_PUNKTWOLKE'), exist_ok=True)
        os.makedirs(os.path.join(output_path, 'PrecalculatedFormats', 'SB_DSM_PUNKTWOLKE'), exist_ok=True)

    files_to_copy = sorted(fn for fn in os.listdir(input_path)
                            if os.path.isfile(os.path.join(input_path, fn)))

    log("\nStarte Kopiervorgang...\n")

    if workers is None:
        workers = _default_worker_count()
    workers = max(1, min(workers, len(files_to_copy) or 1))

    def _copy_one(file_name):
        copy_with_retry(os.path.join(input_path, file_name), os.path.join(new_order_path, file_name))

    nb = 0
    if workers <= 1 or len(files_to_copy) <= 1:
        for file_name in files_to_copy:
            try:
                _copy_one(file_name)
                nb += 1
                log(f"Datei {nb}/{len(files_to_copy)} kopiert: {file_name}")
            except Exception as e:
                log(f"Fehler beim Kopieren {file_name}: {e}")
    else:
        log(f"Parallelisierung: {workers} gleichzeitige Kopier-Worker (verfuegbare Kerne: {os.cpu_count()}).")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_name = {executor.submit(_copy_one, fn): fn for fn in files_to_copy}
            for future in as_completed(future_to_name):
                file_name = future_to_name[future]
                try:
                    future.result()
                    nb += 1
                    log(f"Datei {nb}/{len(files_to_copy)} kopiert: {file_name}")
                except Exception as e:
                    log(f"Fehler beim Kopieren {file_name}: {e}")

    log(f"Kopiervorgang abgeschlossen ({nb}/{len(files_to_copy)} erfolgreich).")

# ****************************** Working Part ******************************
# Wird nur ausgeführt wenn das Script direkt gestartet wird (nicht bei Import).
# Beim Start via GUI (GUI_GDWHimport.py) werden Pfade und meta_info
# als Subprocess-Config übergeben und die Funktionen direkt aufgerufen.

if __name__ == "__main__":

    Quelle = r"A:\2025\BIRCH\DOP\LV95\DOP_NRGB_16BITS\1005"
    output_path = r"\\v0t0020a.adr.admin.ch\iprod\gdwh-ingest\BUCKET_INT\RASTER\SB_DOP_16\2025_BIRCH_DOP16_1005"

    if not os.path.isdir(Quelle):
        print(f"FEHLER: Quellpfad nicht erreichbar: {Quelle}")
        sys.exit(1)

    GDS = define_gds(output_path)

    # *********************** Meta-Information *******************************
    meta_info = {
        "Auftragstyp": "kry",
            # kontrollieren:
            # "kry"
            # "ram"
            # "bim"
            # "mom"
            # "wam"
        "Line_ID": ["20250919_1005_12501"],
            # (!)NUR die zu importierende Line_ID(!): kontrollieren!
        "allAreaLineIDs": ["20250919_0947_12501", "20250919_0955_12501", "20250919_1005_12501", "20250919_1013_12501"],
            # kontrollieren; (!)Alle LineIDs(!) des Mosaiks angeben!
            # erste LineID (!)muss(!) die erste BefliegungsLinie (AufnahmeZeitpunkt) des AOIs sein!
            # (z.B.: "20200821_0952_12504", "20200821_1009_12504", "20200821_1026_12504")
        "NoData": "0 0 0 0",
            # kontrollieren! Typische Werte (bei 16BIT, 4-Band TIF):
            # "0 0 0 0" (schwarze Background-Pixel) / "65535 65535 65535 65535" (weisse Background-Pixel)!
        "CustomAttribute": "Digital OrthoPhoto - (ADS Line) NRGB 16BIT",
            # kontrollieren; "Digital OrthoPhoto - (ADS Line) NRGB 16BIT"
        "SourceReferenceSystem": "(EPSG:2056) CH1903+ / LV95_LN02",
            # kontrollieren! only possible Value ("EPSG:2056) CH1903+ / LV95_LN02"
        "CameraSystem": "Leica ADS100",
            # kontrollieren;
            # "Leica ADS100"
            # "Leica ADS80"
            # "Leica DMC-4"
        "TerrainModel": "Digital Surface Model (DSM photogrammetric autocorrelation)",
            # kontrollieren;
            # "Digital Surface Model (DSM photogrammetric autocorrelation)"
            # "swissALTI3D"
            # "swissALTI3D/DHM25"
            # "swissSURFACE3D"
    }

    # Sicherheitsvorschau anzeigen
    preview_xml_attributes(Quelle, meta_info)

    files_in_order(Quelle, output_path, GDS, meta_info)
    create_and_copy_order(output_path, Quelle, GDS)
