"""
Unit-Tests fuer die reinen Python-Funktionen der GDWH-Import-Scripts.
Getestet werden nur Funktionen ohne externe Abhaengigkeiten (kein GDAL, keine echten Dateien).

Ausfuehren (aus dem Projekt-Hauptverzeichnis):
    python test/test_functions.py
    python -m pytest test/test_functions.py -v   (falls pytest installiert)
"""

import base64
import importlib.util
import inspect
import os
import re
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from unittest.mock import MagicMock

import numpy as np

# ============================================================
#  osgeo/gdal als Mock registrieren, damit die Scripts ohne
#  OSGeo4W-Installation importierbar sind.
#  Nur Funktionen, die in den getesteten Funktionen NICHT
#  verwendet werden, muessen nicht exakt simuliert werden.
# ============================================================
_gdal_mock = MagicMock()
_gdal_mock.UseExceptions = MagicMock()
sys.modules.setdefault("osgeo", MagicMock())
sys.modules.setdefault("osgeo.gdal", _gdal_mock)


# ============================================================
#  Hilfsfunktion: Script per Pfad importieren
#  (noetig weil Dateinamen mit Ziffern beginnen)
# ============================================================
def _import_script(filename):
    # test_functions.py liegt in test/, die getesteten Scripts im
    # Nachbarordner processingScripts/ - siehe Projekt-Ordnerstruktur.
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(project_root, "processingScripts", filename)
    spec = importlib.util.spec_from_file_location(filename, path)
    mod  = importlib.util.module_from_spec(spec)
    # Ausgabe des version-print unterdruecken
    old_stdout = sys.stdout
    sys.stdout = open(os.devnull, "w")
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.stdout.close()
        sys.stdout = old_stdout
    return mod

allGDS  = _import_script("1_allGDS_upload_GDWH_withCHECKxml.py")
dop16   = _import_script("2_2_SB_DOP_16_GDS_upload_GDWH_withCHECKxml.py")
organiz = _import_script("2_1_SB_DOP_16_FOLDERorganize_by_lineID.py")

# osgeo_runner hat KEINE osgeo/gdal-Abhaengigkeit (reine Staging-/Orchestrierungs-
# Logik), kann also immer importiert werden.
osgeo_runner = _import_script("_osgeo_runner.py")

# Script 4 (LAS14-Vorkonversion) nutzt nur die Standardbibliothek (PDAL laeuft
# per subprocess) - die Validierungsfunktionen sind reine Dict-Vergleiche.
las14 = _import_script("4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py")

# fix_false_nodata (Script 3) braucht echtes scipy (ndimage.label) fuer
# sinnvolle Tests der Connected-Component-Klassifikation - das laesst sich
# nicht sinnvoll mocken. Import defensiv, damit die restliche Testsuite auch
# ohne installiertes scipy laeuft (TestClassifyMask wird dann uebersprungen).
try:
    fixnodata = _import_script("3_fix_false_nodata_dop.py")
    _FIXNODATA_IMPORT_ERROR = None
except Exception as _e:
    fixnodata = None
    _FIXNODATA_IMPORT_ERROR = str(_e)


# ============================================================
#  parse_line_id_to_hundredths  (aus allGDS und dop16)
# ============================================================
class TestParseLineId(unittest.TestCase):

    def _check(self, fn, line_id, expected):
        result = fn(line_id)
        self.assertIsNotNone(result, f"Ergebnis fuer '{line_id}' sollte nicht None sein")
        self.assertEqual(result, expected)

    # -- Format HHMM (keine Sekunden) --
    def test_hhmm_format(self):
        erwartet = {"year": 2023, "month": 8, "day": 20,
                    "hh": 9, "mm": 21, "ss": 0, "hundredths": 0}
        self._check(allGDS.parse_line_id_to_hundredths, "20230820_0921", erwartet)
        self._check(dop16.parse_line_id_to_hundredths,  "20230820_0921", erwartet)

    # -- Format HHMMSS --
    def test_hhmmss_format(self):
        erwartet = {"year": 2023, "month": 8, "day": 20,
                    "hh": 9, "mm": 21, "ss": 30, "hundredths": 0}
        self._check(allGDS.parse_line_id_to_hundredths, "20230820_092130", erwartet)
        self._check(dop16.parse_line_id_to_hundredths,  "20230820_092130", erwartet)

    # -- Format HHMMSSss (4 Stellen nach HHMM: Sekunden + Hundertstel) --
    def test_hhmmssss_format(self):
        erwartet = {"year": 2023, "month": 8, "day": 20,
                    "hh": 9, "mm": 21, "ss": 30, "hundredths": 45}
        self._check(allGDS.parse_line_id_to_hundredths, "20230820_09213045", erwartet)
        self._check(dop16.parse_line_id_to_hundredths,  "20230820_09213045", erwartet)

    # -- Format HHMMSSsss (5 Stellen: Millisekunden -> runden) --
    def test_hhmmsssss_millis_runden(self):
        # 456ms -> round(456/10) = 46
        erwartet = {"year": 2023, "month": 8, "day": 20,
                    "hh": 9, "mm": 21, "ss": 30, "hundredths": 46}
        self._check(allGDS.parse_line_id_to_hundredths, "20230820_092130456", erwartet)

    # -- Rundungsclamp: 999ms -> round(999/10)=100 -> clamped auf 99 --
    def test_millis_clamp_bei_99(self):
        result = allGDS.parse_line_id_to_hundredths("20230820_092130999")
        self.assertIsNotNone(result)
        self.assertEqual(result["hundredths"], 99)

    # -- Reales Beispiel aus den Scripts --
    def test_reales_beispiel(self):
        result = allGDS.parse_line_id_to_hundredths("20200913_1054_12501")
        self.assertIsNotNone(result)
        self.assertEqual(result["year"], 2020)
        self.assertEqual(result["month"], 9)
        self.assertEqual(result["day"], 13)
        self.assertEqual(result["hh"], 10)
        self.assertEqual(result["mm"], 54)

    # -- Ungueltige Eingaben liefern None --
    def test_ungueltige_eingabe_kein_unterstrich(self):
        self.assertIsNone(allGDS.parse_line_id_to_hundredths("20230820"))

    def test_leerer_string(self):
        self.assertIsNone(allGDS.parse_line_id_to_hundredths(""))


# ============================================================
#  format_iso8601_hundredths
# ============================================================
class TestFormatIso8601(unittest.TestCase):

    def _parsed(self):
        return {"year": 2023, "month": 8, "day": 20,
                "hh": 9, "mm": 21, "ss": 30, "hundredths": 45}

    def test_normalformat(self):
        self.assertEqual(allGDS.format_iso8601_hundredths(self._parsed()),
                         "2023-08-20T09:21:30.45")
        self.assertEqual(dop16.format_iso8601_hundredths(self._parsed()),
                         "2023-08-20T09:21:30.45")

    def test_null_liefert_unknown(self):
        self.assertEqual(allGDS.format_iso8601_hundredths(None), "UNKNOWN")
        self.assertEqual(dop16.format_iso8601_hundredths(None),  "UNKNOWN")

    def test_fuehrende_nullen(self):
        parsed = {"year": 2023, "month": 1, "day": 5,
                  "hh": 7, "mm": 3, "ss": 9, "hundredths": 4}
        self.assertEqual(allGDS.format_iso8601_hundredths(parsed),
                         "2023-01-05T07:03:09.04")


# ============================================================
#  format_stac_datetime
# ============================================================
class TestFormatStacDatetime(unittest.TestCase):

    def _parsed(self):
        return {"year": 2023, "month": 8, "day": 20,
                "hh": 9, "mm": 21, "ss": 0, "hundredths": 0}

    def test_normalformat(self):
        self.assertEqual(allGDS.format_stac_datetime(self._parsed()),
                         "2023-08-20t09210000")
        self.assertEqual(dop16.format_stac_datetime(self._parsed()),
                         "2023-08-20t09210000")

    def test_null_liefert_unknown(self):
        self.assertEqual(allGDS.format_stac_datetime(None), "UNKNOWN")

    def test_mit_hundertstelsekunden(self):
        parsed = {"year": 2023, "month": 8, "day": 20,
                  "hh": 9, "mm": 21, "ss": 30, "hundredths": 45}
        self.assertEqual(allGDS.format_stac_datetime(parsed), "2023-08-20t09213045")


# ============================================================
#  extract_area  (aus allGDS – GDS-spezifisch)
# ============================================================
class TestExtractAreaAllGDS(unittest.TestCase):

    def test_sb_dop_einwort(self):
        self.assertEqual(allGDS.extract_area("2025_BERN_DOP_0001_LV95.tif", "SB_DOP"), "BERN")

    def test_sb_dop_mehrere_woerter(self):
        self.assertEqual(
            allGDS.extract_area("2025_PLAINE_MORTE_DOP_1001_LV95.tif", "SB_DOP"),
            "PLAINE_MORTE")

    def test_sb_dop_16(self):
        self.assertEqual(
            allGDS.extract_area("2025_PLAINE_MORTE_DOP_1005NRGB_2601_1136_LV95.tif", "SB_DOP_16"),
            "PLAINE_MORTE")

    def test_sb_dsm(self):
        self.assertEqual(
            allGDS.extract_area("2025_PLAINE_MORTE_DSM_1000_LV95.tif", "SB_DSM"),
            "PLAINE_MORTE")

    def test_sb_dsm_hillshade(self):
        self.assertEqual(
            allGDS.extract_area("2025_PLAINE_MORTE_hillshade_1000.tif", "SB_DSM"),
            "PLAINE_MORTE")

    def test_sb_dsm_punktwolke(self):
        self.assertEqual(
            allGDS.extract_area("2025_PLAINE_MORTE_TIN_2601_1136_LV95_LN02.laz", "SB_DSM_PUNKTWOLKE"),
            "PLAINE_MORTE")

    def test_kein_match_liefert_unknown(self):
        result = allGDS.extract_area("kein_jahres_prefix.tif", "SB_DOP")
        self.assertEqual(result, "UNKNOWN")


# ============================================================
#  extract_area  (aus dop16 – nur _DOP)
# ============================================================
class TestExtractAreaDop16(unittest.TestCase):

    def test_einwort(self):
        self.assertEqual(dop16.extract_area("2025_BERN_DOP_10cm_20250802_1005_12501NRGB_LV95.tif"), "BERN")

    def test_mehrere_woerter(self):
        self.assertEqual(
            dop16.extract_area("2025_PLAINE_MORTE_DOP_10cm_2601_1136_LV95.tif"),
            "PLAINE_MORTE")

    def test_kein_match(self):
        self.assertEqual(dop16.extract_area("irgendwas.tif"), "UNKNOWN")


# ============================================================
#  extract_tile_lv95  (aus allGDS)
# ============================================================
class TestExtractTileLv95AllGDS(unittest.TestCase):

    def test_laz_datei(self):
        self.assertEqual(
            allGDS.extract_tile_lv95("2025_PLAINE_MORTE_TIN_2601_1136_LV95_LN02.laz"),
            "2601_1136")

    def test_tif_datei(self):
        self.assertEqual(
            allGDS.extract_tile_lv95("2025_PLAINE_MORTE_DOP_1005NRGB_2602_1145_LV95_LN02.tif"),
            "2602_1145")

    def test_kein_lv95_liefert_unknown(self):
        result = allGDS.extract_tile_lv95("kein_lv95_hier.laz")
        self.assertEqual(result, "UNKNOWN")


# ============================================================
#  extract_tile  (aus dop16)
# ============================================================
class TestExtractTileDop16(unittest.TestCase):

    def test_normal(self):
        self.assertEqual(
            dop16.extract_tile("2025_PLAINE_MORTE_DOP_10cm_2602_1145_LV95.tif"),
            "2602_1145")

    def test_kein_lv95(self):
        result = dop16.extract_tile("kein_lv95.tif")
        self.assertEqual(result, "UNKNOWN")


# ============================================================
#  get_nodata_value  (aus allGDS)
# ============================================================
class TestGetNodataValue(unittest.TestCase):

    def test_hillshade_immer_weiss(self):
        # Hillshade ist 1-Band Grayscale - ein einzelner Wert, der von
        # tag_nodata_on_raster/tag_mask_on_raster automatisch auf die
        # tatsaechliche Bandanzahl expandiert wird (siehe TestTagMaskOnRaster).
        meta = {"NoData": "255 255 255"}
        self.assertEqual(
            allGDS.get_nodata_value("2025_AOI_hillshade_1000.tif", "SB_DSM", meta),
            "255")

    def test_dsm_immer_float_min(self):
        meta = {"NoData": "-3.4028235e+38"}
        self.assertEqual(
            allGDS.get_nodata_value("2025_AOI_DSM_1000.tif", "SB_DSM", meta),
            "-3.4028235e+38")

    def test_sb_dop_aus_meta(self):
        meta = {"NoData": "0 0 0"}
        self.assertEqual(
            allGDS.get_nodata_value("2025_AOI_DOP_2601_1136_LV95.tif", "SB_DOP", meta),
            "0 0 0")

    def test_sb_dop_16_aus_meta(self):
        meta = {"NoData": "0 0 0 0"}
        self.assertEqual(
            allGDS.get_nodata_value("2025_AOI_DOP_1005NRGB_2601_1136_LV95.tif", "SB_DOP_16", meta),
            "0 0 0 0")

    def test_fehlender_meta_wert(self):
        self.assertEqual(allGDS.get_nodata_value("datei.tif", "SB_DOP", {}), "")


# ============================================================
#  tag_mask_on_raster / _compute_nodata_mask  (aus allGDS)
#  GDAL wird durch ein minimales Fake-Dataset simuliert, damit nur die
#  reine Masken-Logik (numpy-Vergleich ueber alle Baender) sowie das
#  Fail-Safe-Verhalten (kein Schreiben bei Lesefehler) geprueft werden.
# ============================================================
class _FakeBand:
    def __init__(self, array, mask_band, raise_on_read=False):
        self._array = array
        self._mask_band = mask_band
        self._raise_on_read = raise_on_read

    def ReadAsArray(self, xoff, yoff, xsize, ysize):
        if self._raise_on_read:
            raise RuntimeError("simulierter Lesefehler (z.B. NumPy-ABI-Konflikt)")
        return self._array[yoff:yoff + ysize, xoff:xoff + xsize]

    def GetMaskBand(self):
        return self._mask_band


class _FakeMaskBand:
    def __init__(self, shape):
        self.written = np.zeros(shape, dtype=np.uint8)
        self.write_calls = 0

    def WriteArray(self, array, xoff=0, yoff=0):
        self.write_calls += 1
        ysize, xsize = array.shape
        self.written[yoff:yoff + ysize, xoff:xoff + xsize] = array


class _FakeDataset:
    def __init__(self, band_arrays, raise_on_read=False):
        self.RasterCount = len(band_arrays)
        self.RasterYSize, self.RasterXSize = band_arrays[0].shape
        self._mask_band = _FakeMaskBand((self.RasterYSize, self.RasterXSize))
        self._bands = [_FakeBand(arr, self._mask_band, raise_on_read) for arr in band_arrays]
        self.mask_created = False

    def GetRasterBand(self, i):
        return self._bands[i - 1]

    def CreateMaskBand(self, flags):
        self.mask_created = True

    def FlushCache(self):
        pass


class TestTagMaskOnRaster(unittest.TestCase):

    def _run(self, band_arrays, nodata_str, raise_on_read=False):
        ds = _FakeDataset(band_arrays, raise_on_read=raise_on_read)
        with unittest.mock.patch.object(allGDS.gdal, "Open", return_value=ds):
            allGDS.tag_mask_on_raster("dummy.tif", nodata_str)
        return ds

    def test_einzelnes_band_rand_ist_nodata(self):
        arr = np.full((4, 4), 100, dtype=np.uint8)
        arr[0, :] = 0
        ds = self._run([arr], "0")
        mask = ds._mask_band.written
        self.assertTrue((mask[0, :] == 0).all())
        self.assertTrue((mask[1:, :] == 255).all())
        self.assertTrue(ds.mask_created)

    def test_weisses_nodata_bei_16bit_dop(self):
        # SB_DOP_16 NRGB mit weissem NoData (65535 65535 65535 65535).
        arr = np.full((2, 2), 65535, dtype=np.uint16)
        arr[0, 0] = 12345  # ein gueltiges Pixel
        ds = self._run([arr, arr, arr, arr], "65535 65535 65535 65535")
        expected = np.array([[255, 0], [0, 0]], dtype=np.uint8)
        self.assertTrue((ds._mask_band.written == expected).all())

    def test_sb_dsm_float32_nodata_praezision(self):
        # SB_DSM NoData-Sentinel als Dezimalstring ("-3.4028235e+38") muss
        # trotz Rundung exakt den echten float32-Wert (-FLT_MAX) im Raster
        # treffen (NumPy>=2.0 NEP-50-Promotion: Skalar wird auf float32
        # heruntergecastet, nicht das Array auf float64 hochgecastet).
        sentinel = np.float32(-3.4028235e+38)
        arr = np.array([[sentinel, 1234.5], [1234.5, 1234.5]], dtype=np.float32)
        ds = self._run([arr], "-3.4028235e+38")
        expected = np.array([[0, 255], [255, 255]], dtype=np.uint8)
        self.assertTrue((ds._mask_band.written == expected).all())

    def test_rgb_nur_ungueltig_wenn_alle_baender_nodata(self):
        # Pixel (0,0): alle drei Baender 0 -> ungueltig.
        # Pixel (0,1): nur zwei von drei Baendern 0 -> gueltig.
        r = np.array([[0, 0], [100, 100]], dtype=np.uint8)
        g = np.array([[0, 50], [100, 100]], dtype=np.uint8)
        b = np.array([[0, 100], [100, 100]], dtype=np.uint8)
        ds = self._run([r, g, b], "0 0 0")
        expected = np.array([[0, 255], [255, 255]], dtype=np.uint8)
        self.assertTrue((ds._mask_band.written == expected).all())

    def test_einzelwert_wird_auf_alle_baender_expandiert(self):
        r = np.full((2, 2), 0, dtype=np.uint8)
        g = np.full((2, 2), 0, dtype=np.uint8)
        ds = self._run([r, g], "0")
        self.assertTrue((ds._mask_band.written == 0).all())

    def test_falsche_anzahl_werte_verhindert_maskenerstellung(self):
        arr = np.full((2, 2), 0, dtype=np.uint8)
        # 2 NoData-Werte fuer 3 Baender -> Funktion soll ohne Fehler abbrechen,
        # OHNE ueberhaupt eine Maske anzulegen.
        ds = self._run([arr, arr, arr], "0 0")
        self.assertFalse(ds.mask_created)
        self.assertEqual(ds._mask_band.write_calls, 0)

    def test_lesefehler_verhindert_maskenerstellung(self):
        # Kernszenario des Vorfalls vom 22.7.: ein Fehler beim Lesen
        # (z.B. NumPy-ABI-Konflikt) darf NICHT zu einer halbfertig
        # geschriebenen "alles ungueltig"-Maske fuehren. Fail-safe:
        # CreateMaskBand()/WriteArray() duerfen dann gar nicht erst
        # aufgerufen werden.
        arr = np.full((4, 4), 100, dtype=np.uint8)
        ds = self._run([arr], "0", raise_on_read=True)
        self.assertFalse(ds.mask_created)
        self.assertEqual(ds._mask_band.write_calls, 0)


# ============================================================
#  create_xml: AreaOverride  (aus allGDS)
#  Deckt die GUI-Erweiterung ab, mit der ein manuell im Feld "Area"
#  korrigierter Wert die dateinamen-basierte Ableitung uebersteuert
#  (z.B. bei falschem Dateinamen-Format).
# ============================================================
class TestCreateXmlAreaOverride(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tif_path = os.path.join(self.tmpdir, "2025_PLAINE_MORTE_DOP_2601_1136_LV95.tif")
        with open(self.tif_path, "w") as f:
            f.write("dummy")
        self.xml_path = self.tif_path.rsplit(".", 1)[0] + ".xml"
        self.meta = {
            "Auftragstyp": "ram",
            "Line_ID": ["20230820_0921_12501"],
            "NoData": "0 0 0",
        }

    def tearDown(self):
        for p in (self.tif_path, self.xml_path):
            if os.path.exists(p):
                os.unlink(p)
        os.rmdir(self.tmpdir)

    def _area_from_xml(self):
        with open(self.xml_path, encoding="utf-8") as f:
            content = f.read()
        m = re.search(r"<Area>(.*?)</Area>", content)
        return m.group(1) if m else None

    def test_ohne_override_wird_area_aus_dateiname_abgeleitet(self):
        allGDS.create_xml(self.tif_path, "SB_DOP", self.meta, cached_raster_attrs={})
        self.assertEqual(self._area_from_xml(), "PLAINE_MORTE")

    def test_mit_override_wird_dieser_verwendet(self):
        meta = dict(self.meta, AreaOverride="KORRIGIERT")
        allGDS.create_xml(self.tif_path, "SB_DOP", meta, cached_raster_attrs={})
        self.assertEqual(self._area_from_xml(), "KORRIGIERT")


# ============================================================
#  _csv_append  (aus allGDS)
# ============================================================
class TestCsvAppend(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="w")
        self.tmp.close()
        self.csv_path = self.tmp.name

    def tearDown(self):
        os.unlink(self.csv_path)

    def test_erste_zeile_ohne_leerzeile(self):
        allGDS._csv_append(self.csv_path, "zeile1")
        with open(self.csv_path, encoding="utf-8") as f:
            inhalt = f.read()
        self.assertEqual(inhalt, "zeile1")

    def test_zweite_zeile_mit_newline_davor(self):
        allGDS._csv_append(self.csv_path, "zeile1")
        allGDS._csv_append(self.csv_path, "zeile2")
        with open(self.csv_path, encoding="utf-8") as f:
            zeilen = f.read().splitlines()
        self.assertEqual(zeilen, ["zeile1", "zeile2"])

    def test_drei_zeilen_korrekt(self):
        for i in range(1, 4):
            allGDS._csv_append(self.csv_path, f"zeile{i}")
        with open(self.csv_path, encoding="utf-8") as f:
            zeilen = f.read().splitlines()
        self.assertEqual(zeilen, ["zeile1", "zeile2", "zeile3"])


# ============================================================
#  extract_line_id  (aus 2_1 Organizer-Script)
# ============================================================
class TestExtractLineId(unittest.TestCase):

    def test_standard_dateiname(self):
        self.assertEqual(
            organiz.extract_line_id("2025_BIRCH_DOP_1005NRGB_2601_1136_LV95_LN02.tif"),
            "1005")

    def test_anderer_wert(self):
        self.assertEqual(
            organiz.extract_line_id("2025_BIRCH_DOP_0947NRGB_2601_1136_LV95_LN02.tif"),
            "0947")

    def test_case_insensitive(self):
        self.assertEqual(
            organiz.extract_line_id("2025_BIRCH_1005nrgb_LV95.tif"),
            "1005")

    def test_kein_match_liefert_none(self):
        self.assertIsNone(organiz.extract_line_id("kein_lineid_datei.tif"))

    def test_leerer_string(self):
        self.assertIsNone(organiz.extract_line_id(""))


# ============================================================
#  Integrations-Test: parse + format kombiniert (Ende-zu-Ende)
# ============================================================
class TestParseUndFormatKombiniert(unittest.TestCase):

    def test_roundtrip_iso8601(self):
        line_id = "20200913_1054_12501"
        parsed = allGDS.parse_line_id_to_hundredths(line_id)
        iso    = allGDS.format_iso8601_hundredths(parsed)
        self.assertEqual(iso, "2020-09-13T10:54:00.00")

    def test_roundtrip_stac(self):
        line_id = "20250919_1005_12501"
        parsed = allGDS.parse_line_id_to_hundredths(line_id)
        stac   = allGDS.format_stac_datetime(parsed)
        self.assertEqual(stac, "2025-09-19t10050000")

    def test_mehrere_line_ids(self):
        ids = ["20200913_1054_12501", "20200913_1104_12501"]
        times = [allGDS.format_iso8601_hundredths(allGDS.parse_line_id_to_hundredths(l)) for l in ids]
        self.assertEqual(times, ["2020-09-13T10:54:00.00", "2020-09-13T11:04:00.00"])


# ============================================================
#  copy_with_retry_md5  (aus allGDS)
#  MD5 wird im selben Lese-/Schreibdurchgang wie das Kopieren berechnet,
#  statt die Quelldatei danach separat nochmals komplett einzulesen.
# ============================================================
class TestCopyWithRetryMd5(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.src = os.path.join(self.tmpdir, "quelle.bin")
        with open(self.src, "wb") as f:
            f.write(os.urandom(50_000))
        self.dst = os.path.join(self.tmpdir, "ziel.bin")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_md5_identisch_zu_calculate_md5(self):
        erwartet = allGDS.calculate_md5(self.src)
        md5 = allGDS.copy_with_retry_md5(self.src, self.dst)
        self.assertEqual(md5, erwartet)

    def test_inhalt_und_groesse_identisch(self):
        allGDS.copy_with_retry_md5(self.src, self.dst)
        with open(self.src, "rb") as f:
            src_bytes = f.read()
        with open(self.dst, "rb") as f:
            dst_bytes = f.read()
        self.assertEqual(src_bytes, dst_bytes)
        self.assertEqual(os.path.getsize(self.src), os.path.getsize(self.dst))


# ============================================================
#  _copytree_merge  (aus _osgeo_runner.py)
#  Ersatz fuer shutil.copytree(..., dirs_exist_ok=True), da dieser
#  Parameter erst ab Python 3.8 existiert (OSGeo4W/QGIS-Python kann aelter
#  sein). Zentral fuer das Y:\-Staging: bestehender Zielordner-Inhalt
#  (z.B. files.csv aus frueheren Laeufen) darf nicht verloren gehen.
# ============================================================
class TestCopytreeMerge(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.src = os.path.join(self.tmpdir, "src")
        self.dst = os.path.join(self.tmpdir, "dst")
        os.makedirs(self.src)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, root, relpath, content):
        full = os.path.join(root, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)

    def test_ziel_existiert_noch_nicht(self):
        self._write(self.src, "tile1.tif", "neu")
        osgeo_runner._copytree_merge(self.src, self.dst)
        with open(os.path.join(self.dst, "tile1.tif"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "neu")

    def test_bestehende_dateien_im_ziel_bleiben_erhalten(self):
        # Simuliert files.csv/NV-Ordner aus einem frueheren Lauf.
        self._write(self.dst, "files.csv", "alte_zeile")
        self._write(self.dst, "NV/alt_tile.tif", "alt")

        self._write(self.src, "files.csv", "alte_zeile\nneue_zeile")
        self._write(self.src, "NV/alt_tile.tif", "alt")
        self._write(self.src, "NV/neu_tile.tif", "neu")

        osgeo_runner._copytree_merge(self.src, self.dst)

        with open(os.path.join(self.dst, "files.csv"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "alte_zeile\nneue_zeile")
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "NV", "alt_tile.tif")))
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "NV", "neu_tile.tif")))

    def test_gleichnamige_datei_wird_ueberschrieben(self):
        self._write(self.dst, "files.csv", "alter_stand")
        self._write(self.src, "files.csv", "neuer_stand")
        osgeo_runner._copytree_merge(self.src, self.dst)
        with open(os.path.join(self.dst, "files.csv"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "neuer_stand")


# ============================================================
#  _write_band_chunked  (aus Script 3)
#  Reine Verifikation, dass zeilenweises Schreiben in Bloecken exakt dem
#  Original-Array entspricht - unabhaengig von scipy, deshalb hier statt in
#  TestClassifyMask (die vom vorhandenen scipy abhaengt).
# ============================================================
class _FakeRasterBand:
    def __init__(self, shape, dtype):
        self.buf = np.zeros(shape, dtype=dtype)

    def WriteArray(self, chunk, xoff=0, yoff=0):
        rows, cols = chunk.shape
        self.buf[yoff:yoff + rows, xoff:xoff + cols] = chunk


@unittest.skipIf(fixnodata is None, f"3_fix_false_nodata_dop.py nicht importierbar: {_FIXNODATA_IMPORT_ERROR}")
class TestWriteBandChunked(unittest.TestCase):

    def test_glatte_groesse(self):
        arr = np.random.randint(0, 256, size=(2000, 500), dtype=np.uint8)
        band = _FakeRasterBand(arr.shape, arr.dtype)
        fixnodata._write_band_chunked(band, arr, chunk_rows=1000)
        self.assertTrue(np.array_equal(band.buf, arr))

    def test_ungerade_groesse_letzter_chunk_kleiner(self):
        arr = np.random.randint(0, 256, size=(2437, 137), dtype=np.uint8)
        band = _FakeRasterBand(arr.shape, arr.dtype)
        fixnodata._write_band_chunked(band, arr, chunk_rows=1000)
        self.assertTrue(np.array_equal(band.buf, arr))

    def test_kleinere_chunk_groesse(self):
        arr = np.random.randint(0, 256, size=(500, 500), dtype=np.uint8)
        band = _FakeRasterBand(arr.shape, arr.dtype)
        fixnodata._write_band_chunked(band, arr, chunk_rows=137)
        self.assertTrue(np.array_equal(band.buf, arr))


# ============================================================
#  classify_mask  (aus Script 3, 3_fix_false_nodata_dop.py)
#  Kernlogik der Echt/Falsch-NoData-Klassifikation (Stufen A-E). Deckt
#  insbesondere den Produktionsvorfall vom 05.08.2026 (WALLIS_SAASTAL) ab:
#  Stufe D/E muessen standardmaessig deaktiviert bleiben, siehe README
#  "Vorkorrektur falscher NoData-Pixel (SB_DOP, optional)".
# ============================================================
@unittest.skipIf(fixnodata is None, f"3_fix_false_nodata_dop.py nicht importierbar: {_FIXNODATA_IMPORT_ERROR}")
class TestClassifyMask(unittest.TestCase):

    def _flat_bands(self, size_y, size_x, fill_value, dtype=np.uint8):
        return [np.full((size_y, size_x), fill_value, dtype=dtype) for _ in range(3)]

    # -- Stufe A: Groesse --
    def test_stufe_a_kleine_gruppe_ist_falsch(self):
        mask = np.zeros((50, 50), dtype=bool)
        mask[0:5, 0:5] = True  # 25 px, weit unter Threshold, beruehrt sogar den Rand
        bands = self._flat_bands(50, 50, 100)
        for b in bands:
            b[mask] = 0
        inc, logs = fixnodata.classify_mask(mask, bands, 0, threshold=1000)
        self.assertEqual(logs[0]["decision"], "false_nodata")
        self.assertTrue(inc[mask].all())

    def test_stufe_a_grenzwert_gleich_threshold_ist_kandidat(self):
        size = 300
        mask = np.zeros((size, size), dtype=bool)
        mask[0, 0:100] = True  # exakt 100 px, volle Randzeile
        bands = self._flat_bands(size, size, 100)
        for b in bands:
            b[mask] = 0
        inc, logs = fixnodata.classify_mask(mask, bands, 0, threshold=100, min_border_contact=100)
        self.assertEqual(logs[0]["size_px"], 100)
        self.assertEqual(logs[0]["decision"], "real_nodata")

    def test_stufe_a_knapp_unter_threshold_ist_falsch(self):
        size = 300
        mask = np.zeros((size, size), dtype=bool)
        mask[0, 0:99] = True  # 99 px, knapp unter Threshold 100
        bands = self._flat_bands(size, size, 100)
        for b in bands:
            b[mask] = 0
        inc, logs = fixnodata.classify_mask(mask, bands, 0, threshold=100, min_border_contact=100)
        self.assertEqual(logs[0]["decision"], "false_nodata")

    # -- Stufe B: ueberhaupt Randkontakt --
    def test_stufe_b_grosse_gruppe_mitten_im_tile_ist_falsch(self):
        mask = np.zeros((100, 100), dtype=bool)
        mask[40:60, 40:60] = True  # 400 px, beruehrt keinen Rand
        bands = self._flat_bands(100, 100, 100)
        for b in bands:
            b[mask] = 0
        inc, logs = fixnodata.classify_mask(mask, bands, 0, threshold=100, min_border_contact=10)
        self.assertEqual(logs[0]["decision"], "false_nodata")
        self.assertFalse(logs[0]["touches_border"])

    # -- Stufe C: Randkontakt-Laenge (der urspruengliche Gletscher-Blowout-Fall) --
    def test_stufe_c_schmaler_keil_am_rand_ist_falsch(self):
        size = 300
        mask = np.zeros((size, size), dtype=bool)
        for y in range(230, size):
            half_w = max(2, (size - 1 - y) // 3)
            mask[y, 150 - half_w:150 + half_w] = True
        bands = self._flat_bands(size, size, 120)
        for b in bands:
            b[mask] = 0
        inc, logs = fixnodata.classify_mask(mask, bands, 0, threshold=1000, min_border_contact=100)
        self.assertEqual(logs[0]["decision"], "false_nodata")
        self.assertTrue(logs[0]["touches_border"])
        self.assertLess(logs[0]["border_contact_px"], 100)

    def test_stufe_c_lange_perimeterkante_ist_echt(self):
        size = 300
        mask = np.zeros((size, size), dtype=bool)
        mask[280:300, 0:250] = True  # lange Kontaktzone am unteren Rand
        bands = self._flat_bands(size, size, 120)
        for b in bands:
            b[mask] = 0
        inc, logs = fixnodata.classify_mask(mask, bands, 0, threshold=1000, min_border_contact=100)
        self.assertEqual(logs[0]["decision"], "real_nodata")

    def test_stufe_c_ecke_zaehlt_beide_kanten_zusammen(self):
        size = 300
        mask = np.zeros((size, size), dtype=bool)
        for i in range(60):
            mask[0:60 - i, i] = True  # Dreieck oben links, beruehrt 2 Kanten je kurz
        bands = self._flat_bands(size, size, 120)
        for b in bands:
            b[mask] = 0
        inc, logs = fixnodata.classify_mask(mask, bands, 0, threshold=1000, min_border_contact=50)
        self.assertEqual(logs[0]["decision"], "real_nodata")

    # -- Regressionstest Vorfall WALLIS_SAASTAL (05.08.2026) --
    def test_regression_weicher_uebergang_bleibt_echt_per_default(self):
        """
        Stufe D/E sind seit diesem Vorfall standardmaessig deaktiviert:
        eine riesige, echte NoData-Flaeche mit weichem (gefeathertem)
        inneren Uebergang wurde faelschlich als 'falsch' erkannt und
        angehoben statt maskiert. Dieser Test stellt sicher, dass eine
        solche Flaeche mit den Standard-Parametern 'echt' bleibt.
        """
        size = 300
        mask = np.zeros((size, size), dtype=bool)
        mask[:, 0:150] = True
        bands = self._flat_bands(size, size, 120)
        for b in bands:
            b[mask] = 0
            b[:, 150] = 5  # weicher/gefeatherter Uebergang, nahe 0
        inc, logs = fixnodata.classify_mask(mask, bands, 0, threshold=1000, min_border_contact=100)
        self.assertEqual(logs[0]["decision"], "real_nodata")
        self.assertFalse(inc[mask].any())

    def test_stufe_d_erkennt_weichen_uebergang_wenn_explizit_aktiviert(self):
        size = 300
        mask = np.zeros((size, size), dtype=bool)
        mask[:, 0:150] = True
        bands = self._flat_bands(size, size, 120)
        for b in bands:
            b[mask] = 0
            b[:, 150] = 5
        inc, logs = fixnodata.classify_mask(
            mask, bands, 0, threshold=1000, min_border_contact=100,
            enable_gradient_check=True)
        self.assertEqual(logs[0]["decision"], "false_nodata")

    def test_stufe_d_harter_schnitt_bleibt_echt_wenn_aktiviert(self):
        size = 300
        mask = np.zeros((size, size), dtype=bool)
        mask[:, 0:150] = True
        bands = self._flat_bands(size, size, 120)
        for b in bands:
            b[mask] = 0
        inc, logs = fixnodata.classify_mask(
            mask, bands, 0, threshold=1000, min_border_contact=100,
            enable_gradient_check=True)
        self.assertEqual(logs[0]["decision"], "real_nodata")

    # -- Automatische Gradient-Toleranz (nur bei enable_gradient_check) --
    def test_gradient_toleranz_automatik_weiss_grenzwert(self):
        size = 300
        for nachbarwert, erwartet in ((220, "false_nodata"), (219, "real_nodata")):
            mask = np.zeros((size, size), dtype=bool)
            mask[:, 0:150] = True
            bands = self._flat_bands(size, size, 120)
            for b in bands:
                b[mask] = 255
                b[:, 150] = nachbarwert
            inc, logs = fixnodata.classify_mask(
                mask, bands, 255, threshold=1000, min_border_contact=100,
                enable_gradient_check=True)
            self.assertEqual(logs[0]["decision"], erwartet,
                              f"nodata=255, Nachbarwert={nachbarwert}")

    def test_gradient_toleranz_automatik_schwarz_grenzwert(self):
        size = 300
        for nachbarwert, erwartet in ((20, "false_nodata"), (21, "real_nodata")):
            mask = np.zeros((size, size), dtype=bool)
            mask[:, 0:150] = True
            bands = self._flat_bands(size, size, 120)
            for b in bands:
                b[mask] = 0
                b[:, 150] = nachbarwert
            inc, logs = fixnodata.classify_mask(
                mask, bands, 0, threshold=1000, min_border_contact=100,
                enable_gradient_check=True)
            self.assertEqual(logs[0]["decision"], erwartet,
                              f"nodata=0, Nachbarwert={nachbarwert}")

    # -- Stufe E: Bounding-Box-Fuellgrad (nur bei enable_fill_ratio_check) --
    def test_stufe_e_kompakter_block_bleibt_echt(self):
        size = 300
        mask = np.zeros((size, size), dtype=bool)
        mask[:, 0:150] = True
        bands = self._flat_bands(size, size, 120)
        for b in bands:
            b[mask] = 0
        inc, logs = fixnodata.classify_mask(
            mask, bands, 0, threshold=1000, min_border_contact=100,
            enable_gradient_check=True, enable_fill_ratio_check=True)
        self.assertEqual(logs[0]["decision"], "real_nodata")

    def test_stufe_e_duenne_verzweigte_form_ist_falsch(self):
        size = 300
        mask = np.zeros((size, size), dtype=bool)
        mask[:, 0:5] = True
        for y in range(0, size, 25):
            mask[y:y + 1, 5:250] = True
        bands = self._flat_bands(size, size, 120)
        for b in bands:
            b[mask] = 0
        inc, logs = fixnodata.classify_mask(
            mask, bands, 0, threshold=1000, min_border_contact=100,
            enable_gradient_check=True, enable_fill_ratio_check=True)
        self.assertEqual(logs[0]["decision"], "false_nodata")

    # -- Randfaelle --
    def test_keine_nodata_pixel_liefert_leere_liste(self):
        mask = np.zeros((50, 50), dtype=bool)
        bands = self._flat_bands(50, 50, 100)
        inc, logs = fixnodata.classify_mask(mask, bands, 0)
        self.assertEqual(logs, [])
        self.assertFalse(inc.any())

    def test_gesamtes_tile_ist_nodata_bleibt_echt(self):
        # Kein innerer Rand vorhanden (Gruppe = ganzes Tile) -> Stufe D kann
        # nicht widerlegen, muss "echt" bleiben.
        mask = np.ones((50, 50), dtype=bool)
        bands = self._flat_bands(50, 50, 0)
        inc, logs = fixnodata.classify_mask(
            mask, bands, 0, threshold=100, min_border_contact=10,
            enable_gradient_check=True, enable_fill_ratio_check=True)
        self.assertEqual(logs[0]["decision"], "real_nodata")


# ============================================================
#  Sicherheits-Defaults (aus Script 3)
#  Regressionsschutz: diese Defaults wurden nach dem WALLIS_SAASTAL-
#  Vorfall bewusst konservativ gesetzt - ein versehentliches Aendern soll
#  hier auffallen.
# ============================================================
@unittest.skipIf(fixnodata is None, f"3_fix_false_nodata_dop.py nicht importierbar: {_FIXNODATA_IMPORT_ERROR}")
class TestFixNodataSicherheitsDefaults(unittest.TestCase):

    def test_classify_mask_stufe_d_e_defaults_aus(self):
        sig = inspect.signature(fixnodata.classify_mask)
        self.assertFalse(sig.parameters["enable_gradient_check"].default)
        self.assertFalse(sig.parameters["enable_fill_ratio_check"].default)

    def test_process_tile_defaults(self):
        sig = inspect.signature(fixnodata.process_tile)
        # threshold=None -> wird pro Tile aus der GSD berechnet (900 m²,
        # siehe DEFAULT_MIN_NODATA_AREA_M2), statt eines fixen Pixelwerts.
        self.assertIsNone(sig.parameters["threshold"].default)
        self.assertEqual(fixnodata.DEFAULT_MIN_NODATA_AREA_M2, 900.0)
        self.assertNotIn("increment", sig.parameters)
        self.assertEqual(sig.parameters["min_border_contact"].default, 100)
        self.assertFalse(sig.parameters["enable_gradient_check"].default)
        self.assertFalse(sig.parameters["enable_fill_ratio_check"].default)


# ============================================================
#  BBox-/Classification-Validierung (aus Script 4)
#  Regressionsschutz Vorfall RANDA 2020 (10.9.2026): Quell-Header-BBox
#  passte nicht zu den eigenen Punkten -> 13 Kacheln faelschlich abgelehnt.
# ============================================================
_LAS14_XY = {"X": (2629000.012, 2629999.987), "Y": (1106000.004, 1106999.991)}


def _las14_ranges(minz, maxz, classification=(1, 6)):
    return dict(_LAS14_XY, Z=(minz, maxz), Classification=classification)


def _las14_header(ranges, **override):
    """Metadaten wie aus 'pdal info --metadata', Header-BBox passend zu ranges."""
    md = {key: ranges[dim][idx] for key, dim, idx in las14.BBOX_FIELDS}
    md.update(override)
    return {"metadata": md}


class TestLas14PunktValidierung(unittest.TestCase):

    def test_ungenauer_quell_header_nur_warnung(self):
        # wie 2629_1106: Quell-Header maxz 2.3 cm ueber dem hoechsten Punkt
        src = _las14_ranges(2412.345, 4190.100)
        dst = _las14_ranges(2412.35, 4190.10)
        deviations = las14.header_bbox_deviations(_las14_header(src, maxz=4190.123), src)
        self.assertEqual(len(deviations), 1)
        self.assertIn("'maxz'", deviations[0])
        self.assertEqual(las14.validate_point_ranges(src, dst, _las14_header(dst)), [])

    def test_requantisierung_innerhalb_toleranz(self):
        # Scale 0.001 -> 0.01: max. 5 mm Rundung
        src = _las14_ranges(2412.345, 4190.105)
        dst = _las14_ranges(2412.35, 4190.11)
        self.assertEqual(las14.validate_point_ranges(src, dst, _las14_header(dst)), [])

    def test_verschobene_punkte_sind_fehler(self):
        src = _las14_ranges(2412.345, 4190.100)
        dst = _las14_ranges(2412.345, 4190.150)
        problems = las14.validate_point_ranges(src, dst, _las14_header(dst))
        self.assertEqual(len(problems), 1)
        self.assertIn("'maxz'", problems[0])

    def test_ziel_header_passt_nicht_zu_ziel_punkten(self):
        pts = _las14_ranges(2412.345, 4190.100)
        problems = las14.validate_point_ranges(pts, pts, _las14_header(pts, minz=2412.0))
        self.assertEqual(len(problems), 1)
        self.assertIn("Ziel-Header", problems[0])

    def test_classification_veraendert(self):
        src = _las14_ranges(2412.345, 4190.100, classification=(1, 6))
        dst = _las14_ranges(2412.345, 4190.100, classification=(0, 6))
        problems = las14.validate_point_ranges(src, dst, _las14_header(dst))
        self.assertEqual(len(problems), 1)
        self.assertIn("Classification veraendert", problems[0])

    def test_fehlende_statistik_ist_fehler(self):
        # darf nie stillschweigend als OK durchgehen
        src = _las14_ranges(2412.345, 4190.100)
        self.assertTrue(las14.validate_point_ranges(src, {}, _las14_header(src)))
        dst = _las14_ranges(2412.345, 4190.100, classification=(None, None))
        self.assertTrue(las14.validate_point_ranges(src, dst, _las14_header(dst)))

    def test_ranges_aus_pipeline_metadata(self):
        meta = {"stages": {"filters.stats": {"statistic": [
            {"name": "Z", "minimum": 2412.345, "maximum": 4190.1},
            {"name": "Classification", "minimum": 1, "maximum": 6}]}}}
        self.assertEqual(las14.dimension_ranges_from_pipeline_metadata(meta),
                         {"Z": (2412.345, 4190.1), "Classification": (1, 6)})
        self.assertEqual(las14.dimension_ranges_from_pipeline_metadata(None), {})


# ============================================================
#  Zielformat PF6/PF7 je nach Farbe und CameraSystem (aus Script 4)
#  DMC-4: Kacheln mit RGB-Werten -> PF7, ohne -> PF6.
#  ADS: immer PF6, RGB-Werte = Fehler (ADS liefert nie Farbe).
# ============================================================
_RGB = {"Red": (0, 65280), "Green": (256, 65280), "Blue": (0, 60160)}
_RGB_NULL = {"Red": (0, 0), "Green": (0, 0), "Blue": (0, 0)}


def _las14_target_meta(point_format, point_length):
    """Ziel-Metadaten wie aus 'pdal info --metadata': LAS 1.4, CRS 2056+5728."""
    return {"metadata": {
        "count": 100, "minor_version": 4, "dataformat_id": point_format,
        "point_length": point_length, "header_size": 375, "global_encoding": 17,
        "vlr_0": {"record_id": 34735, "user_id": "LASF_Projection", "data": ""},
        "vlr_1": {"record_id": 2112, "user_id": "LASF_Projection",
                  "data": base64.b64encode(b"COMPOUNDCRS[]\x00").decode("ascii")},
        "srs": {"json": {"components": [
            {"type": "ProjectedCRS", "id": {"authority": "EPSG", "code": 2056}},
            {"type": "VerticalCRS", "id": {"authority": "EPSG", "code": 5728}}]}},
    }}


class TestLas14Zielformat(unittest.TestCase):

    def test_dmc_mit_rgb_wird_pf7(self):
        self.assertEqual(las14.choose_target_point_format(_RGB, True, "k.laz"), 7)

    def test_dmc_ohne_rgb_wird_pf6(self):
        # kein Farbfeld (None) und lauter Nullen gelten beide als "keine Farbe"
        self.assertEqual(las14.choose_target_point_format(None, True, "k.laz"), 6)
        self.assertEqual(las14.choose_target_point_format(_RGB_NULL, True, "k.laz"), 6)

    def test_ads_ohne_rgb_bleibt_pf6(self):
        self.assertEqual(las14.choose_target_point_format(None, False, "k.laz"), 6)
        self.assertEqual(las14.choose_target_point_format(_RGB_NULL, False, "k.laz"), 6)

    def test_ads_mit_rgb_ist_fehler(self):
        # sonst ginge die Farbe still verloren und die Metadaten nennten die falsche Kamera
        with self.assertRaises(ValueError):
            las14.choose_target_point_format(_RGB, False, "k.laz")

    def test_rgb_scan_nur_bei_punktformat_mit_farbfeld(self):
        with unittest.mock.patch.object(las14, "pdal_dimension_ranges", return_value=_RGB) as scan:
            for pf in (1, 6):
                self.assertIsNone(las14.source_rgb_ranges({"metadata": {"dataformat_id": pf}}, "k.laz"))
            scan.assert_not_called()
            self.assertEqual(las14.source_rgb_ranges({"metadata": {"dataformat_id": 7}}, "k.laz"), _RGB)
            scan.assert_called_once_with("k.laz", las14.RGB_DIMENSIONS)

    def test_bereits_migriert_haengt_vom_zielformat_ab(self):
        pf7 = _las14_target_meta(7, 36)
        self.assertTrue(las14.is_already_migrated(pf7, 7))
        self.assertFalse(las14.is_already_migrated(pf7))  # Ziel PF6 -> konvertieren

    def test_validate_target_pf7(self):
        src = {"metadata": {"count": 100}}
        self.assertEqual(las14.validate_target(src, _las14_target_meta(7, 36), 7), [])
        self.assertEqual(las14.validate_target(src, _las14_target_meta(6, 30)), [])
        # PF6-Datei bei Ziel PF7: dataformat_id und point_length falsch
        self.assertEqual(len(las14.validate_target(src, _las14_target_meta(6, 30), 7)), 2)

    def test_rgb_muss_unveraendert_bleiben(self):
        self.assertEqual(las14.validate_rgb_ranges(_RGB, dict(_RGB)), [])
        problems = las14.validate_rgb_ranges(_RGB, dict(_RGB, Blue=(0, 255)))
        self.assertEqual(len(problems), 1)
        self.assertIn("Blue veraendert", problems[0])
        self.assertTrue(las14.validate_rgb_ranges(_RGB, {}))  # fehlende Statistik nie OK

    def test_runner_farbe_nur_bei_dmc4(self):
        self.assertIn("Leica DMC-4", osgeo_runner.RGB_CAMERA_SYSTEMS)
        self.assertNotIn("Leica ADS100", osgeo_runner.RGB_CAMERA_SYSTEMS)
        self.assertNotIn("Leica ADS80", osgeo_runner.RGB_CAMERA_SYSTEMS)


# ============================================================
#  Kachelrahmen-Pruefung (aus Script 4)
#  Vorfall RHONE 2017 (14.9.2026): zwei Kacheln mit 2 km breiter BBox - der
#  Punkt-Scan bestaetigte, dass auch die Punkte 2 km breit sind (Quelle nicht
#  auf 1 km zugeschnitten) -> harter Fehler (test_punkte_ausserhalb_ist_fehler).
# ============================================================
_RHONE_NAME = "2017_RHONE_TIN_raw_2672_1163_LV95_LN02.laz"
_RHONE_1KM = {"X": (2672000.00, 2672999.99), "Y": (1163000.01, 1163999.99)}
_RHONE_2KM = {"X": (2672000.00, 2673999.99), "Y": (1163000.01, 1163999.99)}


def _frame_meta(xy, **extra):
    """Metadaten wie aus 'pdal info --metadata', Header-BBox aus xy."""
    md = {"minx": xy["X"][0], "maxx": xy["X"][1], "miny": xy["Y"][0], "maxy": xy["Y"][1]}
    md.update(extra)
    return {"metadata": md}


class TestLas14Kachelrahmen(unittest.TestCase):

    def _check(self, header_xy, punkte):
        with unittest.mock.patch.object(las14, "pdal_dimension_ranges", return_value=punkte) as scan:
            result = las14.check_tile_frame_plausibility(
                _frame_meta(header_xy), 2672, 1163, _RHONE_NAME, "k.laz")
        return result, scan

    def test_passender_header_ohne_punktscan(self):
        (warnings, header_stale), scan = self._check(_RHONE_1KM, _RHONE_1KM)
        self.assertEqual(warnings, [])
        self.assertFalse(header_stale)
        scan.assert_not_called()  # Normalfall: kein zusaetzlicher Lesedurchlauf

    def test_nur_header_zu_gross_ist_warnung(self):
        (warnings, header_stale), scan = self._check(_RHONE_2KM, _RHONE_1KM)
        self.assertTrue(header_stale)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Header veraltet", warnings[0])
        scan.assert_called_once_with("k.laz", ("X", "Y"))

    def test_punkte_ausserhalb_ist_fehler(self):
        with self.assertRaisesRegex(ValueError, "Punkte ausserhalb"):
            self._check(_RHONE_2KM, _RHONE_2KM)

    def test_punktscan_ohne_ergebnis_ist_fehler(self):
        # Header verdaechtig, Punkte nicht pruefbar -> nie stillschweigend OK
        with self.assertRaises(ValueError):
            self._check(_RHONE_2KM, {})
        with unittest.mock.patch.object(las14, "pdal_dimension_ranges", side_effect=OSError("pdal")):
            with self.assertRaises(ValueError):
                las14.check_tile_frame_plausibility(
                    _frame_meta(_RHONE_2KM), 2672, 1163, _RHONE_NAME, "k.laz")

    def test_migrierte_kachel_mit_veraltetem_header_wird_neu_geschrieben(self):
        # sonst wuerde sie samt falschem Header nur kopiert
        def status(header_xy):
            meta = _las14_target_meta(6, 30)
            meta["metadata"].update(_frame_meta(header_xy)["metadata"])
            with unittest.mock.patch.object(las14, "pdal_metadata", return_value=meta), \
                 unittest.mock.patch.object(las14, "pdal_dimension_ranges", return_value=_RHONE_1KM), \
                 unittest.mock.patch.object(las14, "log"):
                return las14.convert_tile(os.path.join("q", _RHONE_NAME), "z", dry_run=True)["status"]
        self.assertEqual(status(_RHONE_1KM), "skipped_already_migrated")
        self.assertEqual(status(_RHONE_2KM), "warning")

    def test_punkte_ausserhalb_trotz_passendem_header_ist_fehler(self):
        # umgekehrter Fall: Header meldet 1 km, Punkte ragen hinaus ->
        # Nachpruefung gegen die Punkt-Extremwerte aus filters.stats
        src_meta = _frame_meta(_RHONE_1KM, minor_version=2, dataformat_id=1, global_encoding=1)
        stats = dict(_RHONE_2KM, Z=(400.0, 500.0), Classification=(1, 1))
        pipeline_meta = {"stages": {"filters.stats": {"statistic": [
            {"name": dim, "minimum": lo, "maximum": hi} for dim, (lo, hi) in stats.items()]}}}
        dst_dir = tempfile.mkdtemp()
        try:
            with unittest.mock.patch.object(las14, "pdal_metadata", return_value=src_meta), \
                 unittest.mock.patch.object(las14, "run_pdal_pipeline", return_value=pipeline_meta), \
                 unittest.mock.patch.object(las14, "inject_reference_vlrs") as inject:
                result = las14.convert_tile(os.path.join("q", _RHONE_NAME), dst_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("Punkte ausserhalb", result["error"])
            inject.assert_not_called()
            self.assertEqual(os.listdir(dst_dir), [])  # kein Ziel, keine Temp-Datei
        finally:
            shutil.rmtree(dst_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
