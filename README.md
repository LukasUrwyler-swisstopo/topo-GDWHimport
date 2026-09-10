# GDWH & STAC Import Pipeline

Ein GUI-Tool, das den kompletten Ablauf von der Datenvorbereitung bis zum STAC-Import automatisiert: XML-Metadaten erzeugen, Daten prüfen/bereinigen, ins GDWH-Bucket kopieren.

---

## Schnellstart

1. **cmd (Terminal) starten**: (Win-Taste + eingabe "cmd")
2. **Skript starten** im cmd-Terminal: 
```
python pfad/GUI_importToGDWH-STAC_SpezialBefliegung.py
```

<img width="1000" height="1400" alt="image" src="https://github.com/user-attachments/assets/94a84229-81f6-4514-99ee-1ad0c78f4058" />


Alle Angaben (GDS, Pfade, Meta-Informationen) werden direkt im GUI ausgefüllt – kein manuelles Bearbeiten der Scripts nötig.

---

## Was kann das Tool?

- **GDS auswählen** und passendes Datenpaket im GDWH-Portal öffnen (Button)
- **Meta-Informationen** interaktiv erfassen (Area, NoData, Kamerasystem, Line_IDs, …)
- **Quellordner automatisch bereinigen** (nur relevante Dateien behalten)
- **XML-Metadaten** pro Datei generieren
- **NoData-Tag & Maske** im TIFF setzen
- **Daten ins GDWH-Bucket kopieren** inkl. `files.csv` (Hash, TileKey, Footprint)
- Optionale Zusatzfunktion (siehe unten): falsche NoData-Pixel korrigieren
- Bei `SB_DSM_PUNKTWOLKE` läuft vor dem Import automatisch eine LAS 1.2 → 1.4 Vorkonversion (CRS-Tag wird dabei byte-exakt gesetzt)

Nach dem GDWH-Import erfolgt der **STAC-Import automatisch**.

---

## Unterstützte GDS-Typen

| GDS | Datenformat | Dateinamen-Format |
|-----|-------------|--------------------|
| `SB_DOP` | `.tif` / `.tfw` (8Bit RGB) | `202X_AREA_DOP_..._XXXX_YYYY_LV95.tif` |
| `SB_DOP_16` | `.tif` / `.tfw` (16Bit NRGB) | `202X_AREA_DOP_..._XXXX_YYYY_LV95.tif` |
| `SB_DSM` | `.tif` / `.tfw` (DSM + Hillshade) | `202X_AREA_DSM_..._LV95_LN02.tif` |
| `SB_DSM_PUNKTWOLKE` | `.laz` | `202X_AREA_TIN_..._XXXX_YYYY_LV95_LN02.laz` |

> `XXXX_YYYY` = TileKey (z.B. `2601_1136`). `_LV95` muss im Dateinamen enthalten sein – das GUI zeigt eine Live-Vorschau und warnt bei falschem Format.

---

## Ablauf im GUI

```
1. GDS wählen
2. Datenpaket im Portal anlegen  (Button "GDWH-PROD" / "GDWH-INT")
3. Meta-Informationen eingeben  (Dropdowns / Freitext)
4. Quell- und Zielpfad eingeben
5. Sicherheitscheck bestätigen  (Kontrollfragen)
6. Import starten
   → (nur SB_DSM_PUNKTWOLKE: LAS 1.2 → 1.4 Vorkonversion, automatisch)
   → Quellordner bereinigen
   → XML generieren
   → NoData-Tag & Maske setzen
   → Daten ins Bucket kopieren + files.csv erstellen
7. GDWH-Portal: Datenpaket prüfen (CHECK) und importieren
8. STAC-Import läuft automatisch
```

Der **Import-Button** bleibt gesperrt, bis alle Pflichtfelder ausgefüllt sind.

---

## Meta-Informationen (Übersicht)

| Feld | Beschreibung |
|------|-------------|
| `Auftragstyp` | `kry` Kryosphäre / `ram` Rapid Mapping / `bim` Biotop Monitoring / `mom` Moor Monitoring / `wam` Wald Monitoring |
| `Area` | AOI-Name, wird aus dem Quellordner vorgeschlagen, ist aber editierbar |
| `NoData` | NoData-Quellwert (bestimmt v.a. bei SB_DOP/SB_DOP_16 die Maskenberechnung) |
| `TerrainModel` | verwendetes Geländemodell |
| `CameraSystem` | Kamerasystem (z.B. Leica ADS100) |
| `CustomAttribute` | Beschreibung des Datenprodukts |
| `Line_ID(s)` | Befliegungslinien – werden automatisch chronologisch sortiert; mehrere Zeilen per Copy/Paste aus Excel möglich |

> Bei `SB_DSM` wird NoData automatisch gesetzt, bei `SB_DSM_PUNKTWOLKE` entfällt es ganz.

---

## Optionale Zusatzfunktionen

**Falsche NoData-Pixel korrigieren** *(nur SB_DOP, Checkbox, standardmässig aktiv)*
Vereinzelte Pixel/kleine Gruppen, die zufällig dem NoData-Wert entsprechen (z.B. dunkle Schatten, überstrahlte Flächen), aber eigentlich gültige Nutzdaten sind, werden vor dem Import erkannt und korrigiert, damit sie nicht fälschlich als NoData maskiert werden.

**LAS 1.2 → 1.4 Vorkonversion** *(nur SB_DSM_PUNKTWOLKE, immer aktiv, keine Checkbox)*
Hebt die photogrammetrisch abgeleiteten DSM-Punktwolken-Tiles (LAZ) von LAS 1.2/PF1 ohne CRS-Angabe auf LAS 1.4/PF6 an, damit sie strukturell kongruent zu swissSURFACE3D sind. Das CRS (`EPSG:2056+5728`) wird dabei byte-exakt aus einer verifizierten Referenzkachel injiziert (keine Reprojektion, keine Neuberechnung des WKT). Läuft automatisch vor dem eigentlichen Import auf einer Arbeitskopie; Quelltiles bleiben unverändert. Details siehe [4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py](processingScripts/4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py).

**Lokales Staging** *(Performance, Feld „Lokaler Temp-Ordner“)*
Bei grossen Lieferungen über ein Netzlaufwerk kann ein lokaler Zwischenordner angegeben werden – reduziert die Anzahl Netzwerktransfers pro Tile deutlich.

---

## Voraussetzungen

- **Normales Python 3.x** zum Starten der GUI (kein OSGeo4W-Start nötig)
  - Die GUI findet den OSGeo4W-Python-Pfad automatisch, alternativ Button **Ändern…**
- **PDAL-CLI**, nur für `SB_DSM_PUNKTWOLKE` (LAS 1.2 → 1.4 Vorkonversion, läuft automatisch)
- Netzwerkzugriff auf das GDWH-Bucket
- Korrektes Dateinamen-Format (siehe Tabelle oben) – zwingend für die XML-Generierung

---

## Log

Pro Import wird eine Logdatei geschrieben:
```
logs\GDWHimport_{GDS}_{AREA}_{Line_ID}_{YYYYMMDD_HHMMSS}.log
```
Zusätzlich ein fortlaufendes Archiv-Log mit einer Zeile pro Import:
```
logs\GDWHimport_archived_AREA_proGDS.log
```

---

## Tests

```bash
python standaloneTools/test_functions.py
```
Prüft die reinen Python-Funktionen ohne OSGeo4W/GDAL-Abhängigkeit (Mock).

---

## Wichtige Hinweise

- **Line_IDs**: Die erste Line_ID bestimmt den Aufnahmezeitpunkt und muss die früheste Befliegung sein – die GUI sortiert automatisch. Bei `SB_DOP_16` ist nur eine Line_ID im Hauptfeld erlaubt, alle weiteren gehören ins Feld `allAreaLineIDs`.
- **Zielpfad**: muss den GDS-Namen als vorletzten Ordner enthalten (z.B. `…\SB_DSM\2025_AREA_DSM`).
- **Sicherheitscheck**: Vor dem Import müssen alle Kontrollfragen bestätigt werden (Pfade, Line_IDs, NoData-Werte vorgängig visuell prüfen).
- **Nach dem Import**: Die Validierung im GDWH-Portal (CHECK) muss erfolgreich sein, bevor der eigentliche Import gestartet wird. STAC folgt danach automatisch.

---

## Für Entwickler / Sub-Scripts

<details>
<summary>Details zu den einzelnen Scripts, Whitelist-Bereinigung, Klassifikations-Logik und Implementierungsdetails ausklappen</summary>

| Script | Rolle | Direkt ausführbar |
|--------|-------|:-----------------:|
| `GUI_importToGDWH-STAC_SpezialBefliegung.py` | Hauptscript (GUI) – steuert alle Sub-Scripts | ✓ |
| `processingScripts/1_allGDS_upload_GDWH_withCHECKxml.py` | Sub-Script für `SB_DOP`, `SB_DSM`, `SB_DSM_PUNKTWOLKE` | (direkt möglich, Working Part anpassen) |
| `processingScripts/2_1_SB_DOP_16_FOLDERorganize_by_lineID.py` | Sortiert 16BIT-DOP-Dateien nach LineID | (direkt möglich, Pfad anpassen) |
| `processingScripts/2_2_SB_DOP_16_GDS_upload_GDWH_withCHECKxml.py` | Sub-Script für `SB_DOP_16` | (direkt möglich, Working Part anpassen) |
| `processingScripts/3_fix_false_nodata_dop.py` | Optionale NoData-Vorkorrektur (SB_DOP), läuft in-place vor Script 1 | ✓ (eigenständiges CLI, siehe Docstring) |
| `processingScripts/4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py` | LAS 1.2 → 1.4 Vorkonversion (SB_DSM_PUNKTWOLKE), läuft immer automatisch vor Script 1, schreibt auf Arbeitskopie | ✓ (eigenständiges CLI, siehe Docstring) |
| `processingScripts/_osgeo_runner.py` | Interner Subprocess-Runner (OSGeo4W Python) | – |
| `processingScripts/_tif_preview_reader.py` | Interner Subprocess-Helper: erzeugt die TIF-Vorschau im GUI | – |
| `standaloneTools/5_LAS12_LAS14_batch_inplace_upgrade.py` | Standalone Batch-Tool: LAS 1.2 → 1.4 Inplace-Upgrade über viele Ordner (Textliste), Output = Input, für unbeaufsichtigte Vorprozessierung ausserhalb der GUI-Pipeline | ✓ (eigenständiges CLI, siehe Docstring) |
| `standaloneTools/5_1_LAS12_FolderCopy_4_BatchProcessing.py` | Standalone Tool: kopiert Archiv-Ordner mit LAS-1.2-.laz-Kacheln gespiegelt in eine Kopie, als Vorbereitung für obiges Batch-Tool | ✓ (eigenständiges CLI, siehe Docstring) |
| `standaloneTools/test_functions.py` | Unit-Tests | ✓ |

`GUI_importToGDWH-STAC_SpezialBefliegung.py` liegt im Projekt-Hauptverzeichnis, die Sub-Scripts in `processingScripts/` (von der GUI/vom Runner dynamisch geladen bzw. per Subprocess gestartet), die eigenständigen CLI-Tools in `standaloneTools/` und die Konfiguration in `config/_gdwh_config.json` (wird beim ersten GUI-Start automatisch erstellt).

**Whitelist bei der Quellordner-Bereinigung:**

| GDS | Behalten | Gelöscht (Beispiele) |
|-----|----------|----------------------|
| `SB_DOP` / `SB_DOP_16` | `.tif` / `.tiff` / `.tfw` | `.xml`, `.pyr`, `.rdx`, `.ovr`, … |
| `SB_DSM` | `.tif` / `.tiff` / `.tfw` | `.xml`, `.ovr`, `.cpg`, `.dbf`, `.lock`, … |
| `SB_DSM_PUNKTWOLKE` | `.laz` / `.ascii` | `.xml`, `.lax`, `.lasx`, … |

Bereinigung läuft erst nach dem Sicherheitscheck; bei Abbruch wird nichts gelöscht.

**Klassifikation falscher NoData-Pixel** (`3_fix_false_nodata_dop.py`, Connected-Component-Labeling):
- Grösse der Pixelgruppe ≥ Schwelle (Default: automatisch pro Tile aus der GSD berechnet, entsprechend 900 m² – siehe `DEFAULT_MIN_NODATA_AREA_M2`; die frühere fixe Schwelle von 25'000 Pixel entsprach bei 10cm GSD nur 250 m² und war für alpines Gelände zu knapp bemessen)
- Randkontakt zum Tile-Rand (Default ≥100 Pixel)
- Zusätzliche Rand-/Füllgrad-Prüfungen existieren als CLI-Flags (`--enable-gradient-check`, `--enable-fill-ratio-check`), sind aber standardmässig **deaktiviert**, da sie bei weich ausgeblendeten Mosaikkanten (Feathering) zu Fehlklassifikationen führen können.

Korrekturwerte sind fix im Skript hinterlegt (keine GUI-/CLI-Parameter): falsche 255er-Gruppen werden um −1 verschoben, nahe-schwarze Schattenpixel gestuft angehoben. Bei NoData-Wahl `255 255 255` werden zusätzlich alle echten NoData-Pixel auf `0 0 0` normalisiert (GDAL-Tag und XML sind bei SB_DOP ohnehin immer `0`-normalisiert).

**Bekannte Design-Entscheidungen:**
- `SB_DSM` DSM-Raster (nicht Hillshade): historische falsche NoData-Pixel mit dem festen Wert `-9999` (aus der ursprünglichen LAStools-DSM-Erzeugung) werden vor dem NoData-Tag automatisch auf den echten NoData-Wert `-3.4028235e+38` korrigiert (`fix_dsm_false_nodata`). Hintergrund: ein früheres "Extract by Mask" (ArcMap) hat NoData-Bereiche *innerhalb* des Masken-Polygons bereits korrekt umgeschrieben, Flächen *ausserhalb* der Maske blieben mit dem alten falschen `-9999` stehen. Läuft automatisch, keine GUI-Option nötig.
- SB_DSM DSM-Raster (nicht Hillshade) erhält nur den NoData-Tag, keine interne Maske – bei Hillshade bleibt die Maske aktiv.
- Die Maske wird immer erst vollständig im Speicher berechnet und erst bei Erfolg geschrieben (Fail-Safe gegen halbfertige Masken).
- Ist bei `SB_DOP` mit NoData `0 0 0` bereits eine interne Maske vorhanden (z.B. fortgesetzter Lauf), wird die Neuberechnung übersprungen.
- `SB_DSM_PUNKTWOLKE`: Das CRS wird NICHT über PDALs `a_srs` oder `las2las -epsg` gesetzt, sondern als zwei VLRs (GeoTIFF-KeyDirectory 34735 + OGC-WKT 2112) byte-exakt aus einer verifizierten swissSURFACE3D-Referenzkachel injiziert – beide genannten Standardwege lieferten in Tests einen abweichenden bzw. fachlich falschen WKT (siehe Docstring von `4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py`). Die Kachelkoordinaten (Offset) werden deterministisch aus dem Dateinamen geparst, nicht aus dem Datenminimum.

**Netzwerk-I/O:** `copy_with_retry_md5()` berechnet die MD5-Prüfsumme im selben Lese-/Schreibdurchgang wie das Kopieren, statt die Datei separat erneut einzulesen.

</details>
