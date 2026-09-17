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
- Bei `SB_DSM_PUNKTWOLKE`: automatische LAS-1.4-Vorkonversion vor dem Import (CRS-Tag byte-exakt; Punktformat PF6, bei `Leica DMC-4` PF7 mit Farbe)
- **CRS-Tag im TIFF** prüfen und bei Bedarf setzen (`SB_DSM`-DSM: `EPSG:2056+5728`, `SB_DSM`-Hillshade und `SB_DOP` mit `Leica DMC-4`: `EPSG:2056`)

Nach dem GDWH-Import erfolgt der **STAC-Import automatisch**.

---

## Unterstützte GDS-Typen

| GDS | Datenformat | Dateinamen-Format |
|-----|-------------|--------------------|
| `SB_DOP` | `.tif` / `.tfw` (8Bit RGB) | `202X_AREA_DOP_..._XXXX_YYYY_LV95.tif` |
| `SB_DOP_16` | `.tif` / `.tfw` (16Bit NRGB) | `202X_AREA_DOP_..._XXXX_YYYY_LV95.tif` |
| `SB_DSM` | `.tif` / `.tfw` (DSM + Hillshade) | `202X_AREA_DSM_..._LV95_LN02.tif` bzw. `202X_AREA_hillshade_..._LV95_LN02.tif` |
| `SB_DSM_PUNKTWOLKE` | `.laz` (LAS 1.4, PF6 bzw. PF7 mit RGB bei DMC-4) | `202X_AREA_TIN_..._XXXX_YYYY_LV95_LN02.laz` |

> `XXXX_YYYY` = TileKey (z.B. `2601_1136`). `_LV95` muss im Dateinamen enthalten sein. Das GUI zeigt den TileKey der ersten Datei als Vorschau (mit Warnhinweis, wenn er nicht passt); der Button **Check - NameFormat** prüft alle Dateinamen im Quellordner.

---

## Ablauf im GUI

```
1. GDS wählen
2. Datenpaket im Portal anlegen  (Button "GDWH-PROD" / "GDWH-INT")
3. Meta-Informationen eingeben  (Dropdowns / Freitext)
4. Quell- und Zielpfad eingeben
5. Sicherheitscheck bestätigen  (Kontrollfragen)
6. Import starten
   → (nur SB_DSM_PUNKTWOLKE: LAS-1.4-Vorkonversion, automatisch)
   → Quellordner bereinigen
   → CRS-Tag im TIFF prüfen/setzen  (nur SB_DSM und SB_DOP mit DMC-4)
   → XML generieren
   → NoData-Tag & Maske setzen  (nur Raster)
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
| `TerrainModel` | verwendetes Geländemodell (bei DMC-4 fix DSM) |
| `CameraSystem` | Kamerasystem (Leica ADS100 / ADS80 / DMC-4). Bei DMC-4 gelten Sonderregeln, siehe [Leica DMC-4](#leica-dmc-4-sonderregeln) |
| `SourceRefSys` | fix `(EPSG:2056) CH1903+ / LV95_LN02`, bei `SB_DOP` mit DMC-4 `(EPSG:2056) CH1903+ / LV95_LHN95` |
| `CustomAttribute` | Beschreibung des Datenprodukts (automatisch je GDS und CameraSystem) |
| `Line_ID(s)` | Befliegungslinien – werden automatisch chronologisch sortiert; mehrere Zeilen per Copy/Paste aus Excel möglich. Format je CameraSystem, siehe [LineIDs bei DMC-4](#lineids-bei-dmc-4) |

> Bei `SB_DSM` wird NoData automatisch gesetzt, bei `SB_DSM_PUNKTWOLKE` entfällt es ganz. Bei `SB_DOP` mit `Leica DMC-4` ist es fix `0 0 0 0` (4-Band RGBN).

---

## Leica DMC-4: Sonderregeln

Ist im GUI `CameraSystem` = `Leica DMC-4` gewählt, gilt:

| GDS | Verhalten | Grund |
|-----|-----------|-------|
| alle | `TerrainModel` fix `Digital Surface Model (DSM photogrammetric autocorrelation)`, Dropdown gesperrt; LineIDs im DMC-Format, siehe [LineIDs bei DMC-4](#lineids-bei-dmc-4) | |
| `SB_DOP` | NoData immer `0 0 0 0` (vier Werte, Dropdown gesperrt), Option „fixing false NoData pixels“ ausgeblendet und aus | Die DMC-Pipeline (Reality Studio → `topo-DMCdataConverter`) liefert seit der Umstellung **4-Band RGBN** (8bit), schreibt NoData immer schwarz und erzeugt keine falschen NoData-Pixel in den Nutzdaten. |
| `SB_DOP` | `CustomAttribute` = `Digital OrthoPhoto - Mosaic RGBN 8BIT`; `SourceRefSys` = `(EPSG:2056) CH1903+ / LV95_LHN95`; CRS-Tag im TIFF = `EPSG:2056` (ohne Höhenbezug) | Das DOP wird mit LHN95 gerechnet. Im XML steht LHN95 nur als Text – gleiche Form wie `LV95_LN02`, der EPSG-Code in Klammern bleibt horizontal. |
| `SB_DOP_16` | gibt es nicht – Formular gesperrt, **IMPORT STARTEN** rot und deaktiviert (GDS und CameraSystem bleiben wählbar) | DMC liefert keine ADS-Einzellinien. |
| `SB_DSM` | wie ADS, `SourceRefSys` bleibt `LV95_LN02` | |
| `SB_DSM_PUNKTWOLKE` | immer **PF7** (PF6 + RGB), Kachel ohne RGB-Werte = **Abbruch**; `CustomAttribute` = `Digital Surface Model - PointCloud LAZ RGB (DSM photogrammetric autocorrelation)`; `SourceRefSys` bleibt `LV95_LN02` | Die Farbe der DMC-Punktwolke soll bis ins GDWH-Produkt erhalten bleiben, das XML nennt RGB. |

### LineIDs bei DMC-4

| | Format | Beispiel |
|---|---|---|
| Eingabe im GUI | `YYYYMMDD_LLL_HHMMSS_BBB_QQQQQ` (Datum, Linie, Linienstart UTC, Bildnummer, Kamera-Seriennummer) | `20260813_004_082750_012_41216` |
| im XML (`LineID`) | `YYYYMMDD_GGGG_QQQQQ_LLL_HHMMSS` | `20260813_0822_41216_004_082750` |

- `GGGG` ist die **Gruppennummer** = HHMM der ersten beflogenen Linie der Eingabe. Sie kennzeichnet die Linien, die zusammen die AREA bilden, und ist damit auch die `BandID`.
- Die Bildnummer fällt weg: weitere Bilder derselben Linie gelten als Duplikat.
- Sortiert wird nach Datum + Linienstart, **nicht** nach Liniennummer.
- `FirstAcquisitionTime`, `AcquisitionTimes` und `StacItemIdDatetime` sind sekundengenau, Hundertstel immer `00` (z.B. `2026-08-13T08:22:21.00` bzw. `2026-08-13t08222100`). Der STAC-Link im Sicherheitscheck und im Archiv-Log nutzt dieselbe Regel.
- Beim Wechsel ADS ↔ DMC-4 werden nicht passende LineIDs aus der Liste entfernt (mit Hinweis).

Die Logik liegt an einer Stelle in [`processingScripts/_line_ids.py`](processingScripts/_line_ids.py) und wird von GUI und Script 1 gemeinsam genutzt.

### Warum vier Werte bei DMC-4

Die Wertzahl **muss zur Bandzahl passen**: `tag_nodata_on_raster` und `_compute_nodata_mask`
(Script 1) überspringen eine Datei, wenn beides nicht übereinstimmt — und zwar mit einer
Log-Warnung, nicht mit einem Abbruch. Stünden bei 4-Band-Kacheln nur drei Werte, bekäme die
Kachel **weder NoData-Tag noch Flag Mask**. Darum ist die Auswahl bei DMC-4 auf genau eine
Option festgelegt und das Dropdown gesperrt.

**Wasserflächen bleiben sichtbar.** Im NIR ist 0 ein echter Messwert — Wasser reflektiert im
nahen Infrarot praktisch nicht. Die Maskenberechnung zählt ein Pixel nur dann als NoData, wenn
*alle vier* Bänder ihrem NoData-Wert entsprechen (`is_nodata &=` in `_compute_nodata_mask`),
nicht schon wenn eines es tut. Ein Wasserpixel `40 90 140 0` bleibt damit gültig, nur der echte
Rand `0 0 0 0` wird maskiert. Zusätzlich hebt der `topo-DMCdataConverter` solche Kollisionen
schon beim Erzeugen der Kacheln um einen Digitalwert an (`0` → `1`), damit auch die Rohkachel
ohne Flag Mask korrekt dargestellt wird.

> **Achtung bei gemischten Beständen:** Wird `Leica DMC-4` gewählt, die Dateien haben aber nur
> drei Bänder, überspringt Script 1 Tag und Maske stillschweigend (nur Log-Warnung). Das Log
> nach dem Lauf prüfen, wenn der Bestand nicht sicher 4-Band ist.

Bei ADS (ADS100 / ADS80) gilt: NoData frei wählbar, Vorkorrektur optional, TerrainModel frei wählbar, Punktwolken immer PF6. Enthält eine Punktwolken-Kachel trotzdem RGB-Werte, bricht der Import ab – ADS liefert nie Farbe, das `CameraSystem` ist dann vermutlich falsch gewählt. `SB_DOP_16` (ADS-Einzellinien) ist von beidem nicht betroffen.

---

## Optionale Zusatzfunktionen

**Falsche NoData-Pixel korrigieren** *(nur SB_DOP mit ADS, Checkbox, standardmässig aus)*
Vereinzelte Pixel/kleine Gruppen, die zufällig dem NoData-Wert entsprechen (z.B. dunkle Schatten, überstrahlte Flächen), aber eigentlich gültige Nutzdaten sind, werden vor dem Import erkannt und korrigiert, damit sie nicht fälschlich als NoData maskiert werden. Je nach DOP-Grösse dauert das einige Zeit; die Checkbox wird nach jedem erfolgreichen Import wieder zurückgesetzt.

**LAS-1.4-Vorkonversion** *(nur SB_DSM_PUNKTWOLKE, immer aktiv, keine Checkbox)*
Bringt die Punktwolken-Kacheln (LAZ) vor dem Import ins GDWH-Format: LAS 1.4, PF6 (bzw. PF7 mit Farbe bei DMC-4, siehe [Leica DMC-4](#leica-dmc-4-sonderregeln)), Scale 0.01, Offset = Kachelursprung, CRS `EPSG:2056+5728` – strukturell kongruent zu swissSURFACE3D. Das CRS wird byte-exakt aus einer verifizierten Referenzkachel injiziert (keine Reprojektion, keine Neuberechnung des WKT). Typischer Fall sind ADS-Kacheln in LAS 1.2/PF1 ohne CRS-Angabe; Kacheln, die schon im Zielformat vorliegen (z.B. die fertigen `…_LV95_LN02.laz` aus dem DMC-Converter), werden nur kopiert. Läuft automatisch auf einer Arbeitskopie; die Quellkacheln bleiben unverändert. Details siehe [4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py](processingScripts/4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py).

**CRS-Tag im TIFF** *(SB_DSM und SB_DOP mit DMC-4, immer aktiv, keine Checkbox)*
Vor der XML-Erzeugung wird das CRS aller TIFF gelesen (nur Header). Soll: `SB_DSM`-DSM `EPSG:2056+5728` (LV95 + LN02, als GeoTIFF 1.1 mit VerticalGeoKey, OGC 19-008r4), `SB_DSM`-Hillshade (`_hillshade_` im Dateinamen, reine Darstellung) und `SB_DOP` mit DMC-4 `EPSG:2056`. Fehlt das CRS (Koordinaten müssen in LV95 liegen) oder fehlt/stört der Höhenbezug, wird nur der Tag geschrieben – Pixel und Geotransformation bleiben unverändert. Widerspricht eine Kachel dem Soll (z.B. LV03, oder LHN95 bei SB_DSM), bricht der Import ab, **bevor** eine Datei verändert wird. Lässt sich LN02 nicht ins TIFF schreiben (GDAL ohne GeoTIFF 1.1), gibt es nur eine Warnung.

**Lokales Staging** *(Performance, Feld „Lokaler Temp-Ordner“)*
Bei grossen Lieferungen über ein Netzlaufwerk kann ein lokaler Zwischenordner angegeben werden – reduziert die Anzahl Netzwerktransfers pro Tile deutlich.

---

## Voraussetzungen

- **Normales Python 3.x** zum Starten der GUI (kein OSGeo4W-Start nötig)
  - Die GUI findet den OSGeo4W-Python-Pfad automatisch, alternativ Button **Ändern…**
- **PDAL-CLI**, nur für `SB_DSM_PUNKTWOLKE` (LAS-1.4-Vorkonversion, läuft automatisch)
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
python test/test_functions.py
```
Prüft die reinen Python-Funktionen der Scripts in `processingScripts/` ohne OSGeo4W/GDAL-Abhängigkeit (Mock). Benötigt `numpy`; die Tests zu `3_fix_false_nodata_dop.py` brauchen zusätzlich `scipy` und werden ohne scipy übersprungen.

---

## Wichtige Hinweise

- **Line_IDs**: Die erste Line_ID bestimmt den Aufnahmezeitpunkt und muss die früheste Befliegung sein – die GUI sortiert automatisch (DMC-4 nach Linienstart). Bei `SB_DOP_16` ist nur eine Line_ID im Hauptfeld erlaubt, alle weiteren gehören ins Feld `allAreaLineIDs`.
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
| `processingScripts/4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py` | LAS-1.4-Vorkonversion (SB_DSM_PUNKTWOLKE), läuft immer automatisch vor Script 1, schreibt auf Arbeitskopie; Ziel PF6, bei DMC-4 PF7 mit Pflicht-RGB (CLI: `--keep-rgb`) | ✓ (eigenständiges CLI, siehe Docstring) |
| `processingScripts/_line_ids.py` | LineID-Formate je CameraSystem (Prüfung, Sortierung, XML-Umbau, STAC-Datum), von GUI und Script 1 genutzt | – |
| `processingScripts/_osgeo_runner.py` | Interner Subprocess-Runner (OSGeo4W Python) | – |
| `processingScripts/_tif_preview_reader.py` | Interner Subprocess-Helper: erzeugt die TIF-Vorschau im GUI | – |
| `test/test_functions.py` | Unit-Tests für die Sub-Scripts in `processingScripts/` | ✓ |

`GUI_importToGDWH-STAC_SpezialBefliegung.py` liegt im Projekt-Hauptverzeichnis, die Sub-Scripts in `processingScripts/` (von der GUI/vom Runner dynamisch geladen bzw. per Subprocess gestartet), die Unit-Tests in `test/` und die Konfiguration in `config/_gdwh_config.json` (wird beim ersten GUI-Start automatisch erstellt).

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
- Ist bei `SB_DOP` mit NoData `0 0 0` bzw. `0 0 0 0` (DMC-4) bereits eine interne Maske vorhanden (z.B. fortgesetzter Lauf), wird die Neuberechnung übersprungen — die Maskenberechnung ist dort seiteneffektfrei und liefert dasselbe Ergebnis.
- `SB_DSM_PUNKTWOLKE`: Das CRS wird NICHT über PDALs `a_srs` oder `las2las -epsg` gesetzt, sondern als zwei VLRs (GeoTIFF-KeyDirectory 34735 + OGC-WKT 2112) byte-exakt aus einer verifizierten swissSURFACE3D-Referenzkachel injiziert – beide genannten Standardwege lieferten in Tests einen abweichenden bzw. fachlich falschen WKT (siehe Docstring von `4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py`). Die Kachelkoordinaten (Offset) werden deterministisch aus dem Dateinamen geparst, nicht aus dem Datenminimum.

**Netzwerk-I/O:** `copy_with_retry_md5()` berechnet die MD5-Prüfsumme im selben Lese-/Schreibdurchgang wie das Kopieren, statt die Datei separat erneut einzulesen.

</details>
