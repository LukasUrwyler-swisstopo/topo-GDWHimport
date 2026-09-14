"""
_osgeo_runner.py  –  Wird via OSGeo4W Python aufgerufen (NICHT direkt starten).
Liest Parameter aus einer JSON-Datei und führt GDAL-abhängige Funktionen aus.
Ausgabe geht auf stdout → wird vom GUI live im Log angezeigt.
"""

import sys
import os
import json
import shutil
import builtins
import importlib.util
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

# preview_xml_attributes-Bestätigung automatisch mit Y beantworten
builtins.input = lambda prompt="": "Y"


def _lade_modul(name, pfad):
    spec = importlib.util.spec_from_file_location(name, pfad)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _default_worker_count():
    """Anzahl paralleler Worker-Threads: reserviert 2 Kerne fuer OS/GUI/andere
    Prozesse, nutzt den Rest bis maximal 8 - identisches Kalkuel wie
    2_2_SB_DOP_16_GDS_upload_GDWH_withCHECKxml.py::_default_worker_count()."""
    cpu = os.cpu_count() or 4
    return max(1, min(cpu - 2, 8))


def _fix_false_nodata_one(mod3, path, nodata_value):
    """Verarbeitet ein einzelnes Tile in-place (siehe Kommentare in
    _run_fix_false_nodata zu den Parametern). Jedes Tile ist unabhaengig
    (eigene Datei, eigene temporaere Datei via tempfile.mkstemp), kein
    gemeinsamer Zustand zwischen Tiles - GDAL-I/O sowie die numpy/scipy-
    Rasterarbeit (Connected-Component-Labeling in classify_mask) geben den
    GIL bei grossen Arrays frei, wodurch mehrere Tiles unter
    ThreadPoolExecutor echt parallel auf mehreren Kernen korrigiert werden
    koennen, analog _process_tile in Script 2_2."""
    return mod3.process_tile_inplace(
        path, nodata_value=nodata_value,
        strip_existing_mask=True, write_mask=True,
        rewrite_real_nodata_to_zero=(nodata_value == 255))


def _run_fix_false_nodata(mod3, quelle, meta, workers=None):
    """
    Vorkorrektur falscher NoData-Pixel (SB_DOP): laeuft in-place auf allen
    .tif im Quellordner, bevor Script 1 XML/Maske/CSV erzeugt. Der
    NoData-Zielwert (0 oder 255) kommt aus der GUI-Wahl (meta["NoData"]),
    damit Vorkorrektur und die spaetere Maskenberechnung
    (_compute_nodata_mask in Script 1) denselben Wert verwenden.

    Verarbeitet die Tiles parallel ueber ThreadPoolExecutor (siehe
    _fix_false_nodata_one), sobald mehr als ein Tile vorliegt - identisches
    Muster wie files_in_order in Script 2_2. Jedes Tile schreibt nur seine
    eigene Datei (in-place ueber eine eigene Temp-Datei, siehe
    process_tile_inplace), das Ergebnis ist dadurch unabhaengig von der
    Verarbeitungsreihenfolge identisch zum bisherigen sequenziellen Lauf -
    nur die Log-Reihenfolge der Fortschrittszeilen kann abweichen, die
    Detail-Zeilen pro Gruppe werden bewusst erst danach in der urspruenglichen
    (sortierten) Dateireihenfolge ausgegeben.

    Bricht beim ersten Fehler eines Tiles ab (noch nicht gestartete Tiles
    werden nicht mehr eingeplant) und wirft die Exception weiter, exakt wie
    zuvor die sequenzielle for-Schleife - der Aufrufer (_osgeo_runner-
    Hauptblock) setzt darauf exit_code=1 und kopiert die gestagten Daten
    NICHT zurueck (siehe "if staged and exit_code == 0").
    """
    nodata_tokens = (meta.get("NoData") or "").split()
    if not nodata_tokens:
        print("[WARNUNG] Vorkorrektur uebersprungen: kein NoData-Wert gesetzt.", flush=True)
        return
    nodata_value = int(float(nodata_tokens[0]))
    print(f"NoData-Zielwert fuer Vorkorrektur (aus GUI-Auswahl): {nodata_value}", flush=True)

    tif_files = sorted(
        fn for fn in os.listdir(quelle)
        if fn.lower().endswith((".tif", ".tiff"))
    )
    if not tif_files:
        print("[WARNUNG] Vorkorrektur uebersprungen: keine .tif Dateien im Quellordner gefunden.", flush=True)
        return

    if workers is None:
        workers = _default_worker_count()
    workers = max(1, min(workers, len(tif_files)))

    results = {}
    if workers <= 1 or len(tif_files) <= 1:
        for fn in tif_files:
            print(f"Verarbeite Datei: {fn}", flush=True)
            results[fn] = _fix_false_nodata_one(mod3, os.path.join(quelle, fn), nodata_value)
    else:
        print(f"Parallelisierung: {workers} gleichzeitige Worker (verfuegbare Kerne: {os.cpu_count()}).", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_fn = {
                executor.submit(_fix_false_nodata_one, mod3, os.path.join(quelle, fn), nodata_value): fn
                for fn in tif_files
            }
            done = 0
            try:
                for future in as_completed(future_to_fn):
                    fn = future_to_fn[future]
                    results[fn] = future.result()
                    done += 1
                    print(f"[{done}/{len(tif_files)}] verarbeitet: {fn}", flush=True)
            except Exception:
                # Fail-fast wie die urspruengliche sequenzielle Schleife: noch
                # nicht gestartete Tiles nicht mehr einplanen, bereits
                # laufende duerfen noch fertig werden (kein hartes Abbrechen
                # mitten im Schreibvorgang einer Datei), dann weiterwerfen.
                for f in future_to_fn:
                    f.cancel()
                raise

    n_px_total = 0
    for fn in tif_files:
        result = results[fn]
        n_px_total += result["n_increment_px"]
        shadow_info = (
            f", {result['n_shadow_px']} Schattenpixel (0,0,0) geschuetzt"
            if result.get("n_shadow_px") else ""
        )
        print(f"  {fn}: {result['n_groups']} Gruppe(n), {result['n_increment_px']} Pixel korrigiert{shadow_info}", flush=True)
        for g in result["group_rows"]:
            print(f"    Gruppe {g['label_id']}: {g['size_px']} px, "
                  f"Randkontakt={g['border_contact_px']}, "
                  f"decision={g['decision']}", flush=True)

    print(f"Vorkorrektur abgeschlossen: {len(tif_files)} Datei(en), {n_px_total} Pixel insgesamt korrigiert.\n", flush=True)


# ---------------------------------------------------------------------------
# Lokales Staging (Performance)
# ---------------------------------------------------------------------------
# Quelle (Eingangs-Netzlaufwerk) und ein evtl. bereits bestehendes Ziel-
# Datenpaket (GDWH-Bucket-Netzlaufwerk) werden vor der Verarbeitung auf ein
# schnelles lokales/VDI-eigenes Laufwerk gespiegelt (z.B. Y:\, ersetzt bei
# der VDI das frueher lokale D:\). Alle Lese-/Schreibzugriffe waehrend der
# eigentlichen Verarbeitung (Vorkorrektur, MD5, XML, Maske, Kopieren) laufen
# dann lokal statt mehrfach uebers (langsamere) Netzlaufwerk - reduziert die
# Anzahl vollstaendiger Netzwerktransfers pro Tile auf genau zwei (einmal
# hin, einmal zurueck).

def _stage_locally(quelle, ziel, staging_root):
    """
    Spiegelt Quelle und ein bereits bestehendes Ziel-Datenpaket (falls
    vorhanden) in einen neuen Job-Unterordner von staging_root.

    Das bestehende Ziel wird mitgespiegelt (nicht nur ein leerer Ordner
    angelegt), weil files.csv im Zielordner ueber mehrere separate Laeufe
    hinweg angehaengt wird (siehe update_file_csv/_csv_append in Script 1) -
    ohne das wuerden beim Zurueckkopieren bereits vorhandene Eintraege aus
    frueheren Laeufen verloren gehen.

    Der Job-Ordner wird nach dem letzten Ordnernamen von ziel (Bucket-Ordner)
    benannt statt einem Zufallsnamen - erleichtert die Zuordnung bei manueller
    Kontrolle des Staging-Verzeichnisses. Ist der Name bereits belegt (z.B.
    Ueberrest eines abgebrochenen Laufs), wird ein Zaehler-Suffix angehaengt.

    Gibt (local_quelle, local_ziel, job_dir) zurueck. job_dir muss nach der
    Verarbeitung mit _cleanup_staging() entfernt werden.
    """
    os.makedirs(staging_root, exist_ok=True)
    job_name = os.path.basename(os.path.normpath(ziel)) or "job"
    job_dir = os.path.join(staging_root, job_name)
    if os.path.exists(job_dir):
        for i in range(2, 1000):
            candidate = os.path.join(staging_root, f"{job_name}_{i}")
            if not os.path.exists(candidate):
                job_dir = candidate
                break
    os.makedirs(job_dir)
    local_quelle = os.path.join(job_dir, "quelle")
    local_ziel = os.path.join(job_dir, "ziel")

    print(f"[STAGING] Spiegle Quelle nach {local_quelle} ...", flush=True)
    shutil.copytree(quelle, local_quelle)

    if os.path.isdir(ziel):
        print(f"[STAGING] Spiegle bestehendes Ziel-Datenpaket nach {local_ziel} ...", flush=True)
        shutil.copytree(ziel, local_ziel)
    else:
        os.makedirs(local_ziel, exist_ok=True)

    print("[STAGING] Lokale Kopie bereit, Verarbeitung startet lokal.\n", flush=True)
    return local_quelle, local_ziel, job_dir


def _copytree_merge(src, dst):
    """
    Wie shutil.copytree(src, dst, dirs_exist_ok=True), aber ohne den erst ab
    Python 3.8 verfuegbaren dirs_exist_ok-Parameter (das OSGeo4W/QGIS-Python
    kann je nach Installation aelter sein). Kopiert src rekursiv nach dst;
    bestehende Dateien/Ordner in dst bleiben erhalten, gleichnamige Dateien
    aus src ueberschreiben sie (wie beim Original-Verhalten).
    """
    os.makedirs(dst, exist_ok=True)
    for entry in os.listdir(src):
        src_path = os.path.join(src, entry)
        dst_path = os.path.join(dst, entry)
        if os.path.isdir(src_path):
            _copytree_merge(src_path, dst_path)
        else:
            shutil.copy2(src_path, dst_path)


def _finish_staging(local_ziel, ziel):
    """
    Kopiert das lokal fertiggestellte Datenpaket zurueck ins tatsaechliche
    Ziel (Netzlaufwerk) - ueberschreibt/ergaenzt bestehende Dateien dort
    (z.B. die um diesen Lauf ergaenzte files.csv). Wird nur nach
    erfolgreichem Durchlauf aufgerufen.
    """
    print(f"[STAGING] Kopiere fertiges Datenpaket zurueck nach {ziel} ...", flush=True)
    _copytree_merge(local_ziel, ziel)
    print("[STAGING] Ziel-Datenpaket aktualisiert.\n", flush=True)


def _cleanup_staging(job_dir):
    """Entfernt den lokalen Staging-Job-Ordner. Wird NICHT aufgerufen, wenn
    _finish_staging fehlgeschlagen ist (dann ist die lokale Kopie die
    einzige vollstaendige und muss fuer eine manuelle Wiederholung erhalten
    bleiben)."""
    try:
        shutil.rmtree(job_dir, ignore_errors=True)
    except Exception as e:
        print(f"[WARNUNG] Staging-Ordner konnte nicht vollstaendig entfernt werden: {job_dir} ({e})", flush=True)


# ---------------------------------------------------------------------------
# SB_DSM_PUNKTWOLKE: LAS 1.2 -> LAS 1.4 Vorkonversion (Script 4)
# ---------------------------------------------------------------------------
# Laeuft IMMER vor Script 1, wenn GDS == "SB_DSM_PUNKTWOLKE" (siehe main()) -
# keine GUI-Checkbox mehr dafuer, siehe 4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py.
# Quelle (quelle_dir, i.d.R. bereits die lokal gestagte Kopie aus proc_quelle)
# wird NIE veraendert - die konvertierten Tiles landen in einem eigenen,
# temporaeren Scratch-Ordner, dessen Pfad als neue proc_quelle fuer Script 1
# weiterverwendet wird.

# Kamerasysteme mit Farbe in der Punktwolke: Kacheln mit RGB-Werten werden dort
# als PF7 statt PF6 geschrieben (siehe choose_target_point_format in Script 4).
# Wert wie in CAMERA_SYSTEMS der GUI.
RGB_CAMERA_SYSTEMS = ("Leica DMC-4",)


def _convert_punktwolke_las14(script4_path, quelle_dir, staging_root_dir, keep_rgb=False):
    """Fuehrt die LAS 1.2 -> LAS 1.4 Batch-Vorkonversion aus (siehe
    convert_folder() in Script 4). Bricht den ganzen Lauf per Exception ab,
    wenn auch nur eine Kachel fehlschlaegt - eine nicht korrekt konvertierte
    Punktwolke soll nicht unbemerkt weiter nach GDWH gelangen.

    keep_rgb=True (CameraSystem in RGB_CAMERA_SYSTEMS): Kacheln mit RGB-Werten
    werden PF7, ohne RGB-Werte PF6. keep_rgb=False (ADS): immer PF6, eine
    Kachel mit RGB-Werten schlaegt fehl und bricht damit den Lauf ab.

    Der Scratch-Ordner fuer die konvertierten Tiles liegt innerhalb
    staging_root_dir (i.d.R. job_dir - selbe schnelle lokale Platte wie die
    gestagte Quelle), falls vorhanden, sonst im System-Temp.

    Gibt den Pfad zum Scratch-Ordner zurueck (muss vom Aufrufer nach
    Gebrauch aufgeraeumt werden, siehe _cleanup_staging-Analogie in main()).
    """
    mod4 = _lade_modul("script_4", script4_path)
    scratch_dir = tempfile.mkdtemp(prefix="SB_DSM_PUNKTWOLKE_LAS14_", dir=(staging_root_dir or None))

    print("\n=== LAS 1.2 -> LAS 1.4 Vorkonversion (Script 4) ===\n", flush=True)
    print("Zielformat: " + ("PF7 fuer Kacheln mit RGB-Werten, sonst PF6 (CameraSystem mit Farbe)"
                            if keep_rgb else "PF6 (RGB-Werte in einer Kachel = Abbruch)") + "\n",
          flush=True)
    summary = mod4.convert_folder(quelle_dir, scratch_dir, recursive=False,
                                   target_scale=0.01, dry_run=False, keep_rgb=keep_rgb)
    print(f"\nLAS14-Vorkonversion: {summary['total']} verarbeitet, {summary['ok']} gueltig, "
          f"{summary['warning']} mit Warnung, {summary['skipped']} bereits migriert, "
          f"{summary['failed']} fehlgeschlagen; davon PF7 mit RGB: {summary['pf7']}\n", flush=True)

    if summary["failed"] > 0:
        raise RuntimeError(
            f"LAS 1.2 -> LAS 1.4 Vorkonversion fehlgeschlagen fuer {summary['failed']} "
            f"Datei(en) (siehe Log oben) - Import abgebrochen."
        )

    return scratch_dir


def main():
    if len(sys.argv) < 2:
        print("[FEHLER] Kein Konfigurationspfad übergeben.", flush=True)
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        cfg = json.load(f)

    action = cfg["action"]      # "standard" | "dop16"
    gds    = cfg["gds"]
    meta   = cfg["meta_info"]
    quelle = cfg["quelle"]
    ziel   = cfg.get("ziel", "")
    staging_root = (cfg.get("staging_dir") or "").strip()

    # Staging nur, wenn ein Ordner konfiguriert ist, dessen Laufwerk gerade
    # verfuegbar ist (z.B. kein Y:\ auf einer Workstation ohne VDI), und ein
    # Ziel ueberhaupt angegeben wurde. Schlaegt das Spiegeln selbst fehl
    # (z.B. Platz auf Y:\ voll), wird auf direkte Verarbeitung uebers
    # Netzlaufwerk zurueckgefallen statt den ganzen Lauf abzubrechen.
    proc_quelle, proc_ziel = quelle, ziel
    job_dir = None
    staged = False
    punktwolke_scratch_dir = None

    if staging_root and ziel and os.path.isdir(os.path.splitdrive(staging_root)[0] + "\\"):
        try:
            proc_quelle, proc_ziel, job_dir = _stage_locally(quelle, ziel, staging_root)
            staged = True
        except Exception as e:
            print(f"[WARNUNG] Staging fehlgeschlagen ({e}), verarbeite direkt uebers Netzlaufwerk.", flush=True)
            if job_dir:
                _cleanup_staging(job_dir)
            proc_quelle, proc_ziel, job_dir, staged = quelle, ziel, None, False

    exit_code = 0
    try:
        if action == "standard":
            # SB_DSM_PUNKTWOLKE: LAS 1.2 -> LAS 1.4 Vorkonversion (Script 4)
            # laeuft IMMER zuerst, bevor Script 1 auch nur die Sicherheits-
            # vorschau sieht - proc_quelle wird danach auf den Scratch-Ordner
            # mit den konvertierten Tiles umgebogen.
            if gds == "SB_DSM_PUNKTWOLKE":
                punktwolke_scratch_dir = _convert_punktwolke_las14(
                    cfg["script_4"], proc_quelle, job_dir,
                    keep_rgb=meta.get("CameraSystem") in RGB_CAMERA_SYSTEMS)
                proc_quelle = punktwolke_scratch_dir

            # Script 1 (SB_DOP / SB_DSM / SB_DSM_PUNKTWOLKE)
            mod = _lade_modul("script_1", cfg["script_1"])
            print("=== Sicherheitsvorschau ===", flush=True)
            mod.preview_xml_attributes(proc_quelle, gds, meta)

            if gds == "SB_DOP" and cfg.get("fix_false_nodata"):
                print("\n=== Vorkorrektur falsche NoData-Pixel ===\n", flush=True)
                mod3 = _lade_modul("script_3", cfg["script_3"])
                _run_fix_false_nodata(mod3, proc_quelle, meta)

            print("\n=== Verarbeitung gestartet ===\n", flush=True)
            try:
                mod.files_in_order(proc_quelle, proc_ziel, gds, meta)
                mod.create_and_copy_order(proc_ziel, proc_quelle, gds)
            finally:
                if mod.log_file:
                    mod.log_file.close()

        elif action == "dop16":
            # Script 2_2 (SB_DOP_16)
            mod = _lade_modul("script_22", cfg["script_22"])
            print("=== Sicherheitsvorschau ===", flush=True)
            mod.preview_xml_attributes(proc_quelle, meta)
            print("\n=== Verarbeitung gestartet ===\n", flush=True)
            try:
                mod.files_in_order(proc_quelle, proc_ziel, gds, meta)
                mod.create_and_copy_order(proc_ziel, proc_quelle, gds)
            finally:
                if mod.log_file:
                    mod.log_file.close()

        else:
            print(f"[FEHLER] Unbekannte Aktion: '{action}'", flush=True)
            exit_code = 1

    except SystemExit as e:
        code = str(e)
        if code not in ("0", "None", ""):
            print(f"\n[ABBRUCH] Script Exit-Code: {code}", flush=True)
            exit_code = 1
        # code "0"/"None": normale Beendigung, exit_code bleibt 0

    except Exception as e:
        print(f"\n[FEHLER] {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        exit_code = 1

    # Nur bei erfolgreicher Verarbeitung zurueckkopieren - sonst wuerde ein
    # unvollstaendiges/fehlerhaftes Ergebnis ins Ziel gelangen (wie bisher
    # ohne Staging: files_in_order() bricht bei Fehlern vor dem Kopieren ab).
    if staged and exit_code == 0:
        try:
            _finish_staging(proc_ziel, ziel)
        except Exception as e:
            print(f"\n[FEHLER] Zurueckkopieren ins Ziel fehlgeschlagen: {e}", flush=True)
            print(traceback.format_exc(), flush=True)
            print(f"[HINWEIS] Das lokal fertiggestellte Datenpaket bleibt erhalten unter:\n"
                  f"  {job_dir}\n  Bitte manuell nach '{ziel}' kopieren oder den Lauf wiederholen.", flush=True)
            # job_dir bewusst NICHT aufraeumen - lokale Kopie ist die einzige
            # vollstaendige und wird fuer eine manuelle Wiederholung gebraucht.
            sys.exit(1)

    if staged:
        _cleanup_staging(job_dir)

    # Scratch-Ordner der LAS14-Vorkonversion ist reine Arbeitskopie (nie die
    # einzige vollstaendige Version von irgendwas) - immer aufraeumen, auch
    # bei Fehlern. ignore_errors=True: liegt er innerhalb von job_dir, wurde
    # er ggf. bereits durch _cleanup_staging() oben entfernt.
    if punktwolke_scratch_dir:
        shutil.rmtree(punktwolke_scratch_dir, ignore_errors=True)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
