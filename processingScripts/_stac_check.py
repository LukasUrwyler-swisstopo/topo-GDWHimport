"""
_stac_check.py  –  Prueft vor dem Import, ob das Asset des GDS im STAC-Item
bereits existiert (nur Standardbibliothek, nur lesend).

Item-ID   = {Auftragstyp}-{StacItemIdDatetime}      kry-2021-09-21t12030000
Asset-Key = {Item-ID}-{Suffix}, Suffix fest je GDS (siehe ASSET_SUFFIXES)

Die Area kommt im STAC nicht vor: Zwei Areas mit derselben ersten Line_ID und
demselben Auftragstyp landen im selben Item und schreiben auf dieselben Assets
- in GDWH und STAC. Existiert das Asset schon, sperrt die GUI den Import; fuer
einen gewollten Re-Import wird das Asset vorher manuell aus dem Item geloescht.

PROD und INT haben dieselbe Collection und dasselbe Asset-Schema, die
Umgebung folgt dem GDWH-BUCKET-Path (BUCKET / BUCKET_INT).

Was im Bucket auf CHECK/Import wartet, ist im STAC noch nicht sichtbar. Darum
prueft check_queue zusaetzlich die XML der Datenpakete im Bucket - GDWH
loescht/verschiebt ein Paket nach dem Import, der Bucket ist also genau die
Warteschlange. Gesperrt wird nur, was dasselbe Asset erzeugen wuerde (gleiche
Item-ID im selben Catalog), die Area ist dafuer nicht massgebend.
"""

import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import _line_ids

COLLECTION    = "ch.swisstopo.spezialbefliegungen"
STAC_API_PROD = "https://data.geo.admin.ch/api/stac/v1"
STAC_API_INT  = "https://sys-data.int.bgdi.ch/api/stac/v1"
TIMEOUT_S     = 15

# Asset-Suffixe je GDS (geprueft an PROD und INT, 2026-11). SB_DOP mit DMC-4 ist
# 4-Band RGBN. Andere Produkte im selben Item (z.B. 'qdop-rgb-mosaic.tif',
# Fotos bei 'ram') stoeren nicht - verglichen wird der exakte Asset-Key.
ASSET_SUFFIXES = {
    "SB_DOP":            ("rgb-mosaic.tif",),
    "SB_DOP_16":         ("nrgb-16bit.tif",),
    "SB_DSM":            ("dsm-raster.tif", "dsm-hillshade.tif"),
    "SB_DSM_PUNKTWOLKE": ("pointcloud.copc.laz",),
}
ASSET_SUFFIXES_DMC = {"SB_DOP": ("rgbn-mosaic.tif",)}

ITEM_ID_PAT = re.compile(r'^[a-z]{3}-\d{4}-\d{2}-\d{2}t\d{8}$')


def asset_suffixes(gds, camera):
    if _line_ids.is_dmc(camera):
        return ASSET_SUFFIXES_DMC.get(gds, ASSET_SUFFIXES[gds])
    return ASSET_SUFFIXES[gds]


def item_id(auftragstyp, stac_dt):
    return f"{auftragstyp}-{stac_dt}"


def item_url(item, int_env=False):
    api = STAC_API_INT if int_env else STAC_API_PROD
    return f"{api}/collections/{COLLECTION}/items/{item}"


def check_asset(item, gds, camera, int_env=False, timeout=TIMEOUT_S):
    """Fragt das Item ab. 'status':
    'new'    Item existiert nicht (HTTP 404)
    'free'   Item existiert, Asset dieses GDS fehlt noch
    'exists' Asset existiert -> Import wuerde ueberschreiben
    'failed' nicht pruefbar ('error'), z.B. kein Netz, Timeout, ungueltige ID
    Dazu 'assets' (alle Keys im Item), 'conflict' (vorhandene Keys dieses GDS)
    und 'updated' (Item-Property)."""
    res = {"item": item, "env": "INT" if int_env else "PROD", "status": "failed",
           "assets": [], "conflict": [], "updated": "", "error": ""}
    if not ITEM_ID_PAT.match(item or ""):
        res["error"] = f"ungültige Item-ID '{item}' (Auftragstyp/Line_ID prüfen)"
        return res
    try:
        with urllib.request.urlopen(item_url(item, int_env), timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            res["status"] = "new"
        else:
            res["error"] = f"HTTP {e.code} {e.reason}"
        return res
    except Exception as e:
        # URLError (kein Netz/Proxy/DNS), Timeout, kaputtes JSON
        res["error"] = str(getattr(e, "reason", e))
        return res

    keys   = sorted((data.get("assets") or {}).keys())
    wanted = {f"{item}-{s}" for s in asset_suffixes(gds, camera)}
    res.update(assets=keys,
               conflict=[k for k in keys if k in wanted],
               updated=(data.get("properties") or {}).get("updated", ""))
    res["status"] = "exists" if res["conflict"] else "free"
    return res


def _first_xml(folder, depth=1):
    """Erstes XML (alphabetisch) in folder oder eine Ebene tiefer - SB_DOP
    direkt in NV, SB_DSM in NV\\DSM / NV\\HILLSHADE, Punktwolke in
    NV\\SB_DSM_PUNKTWOLKE. None, wenn keines da ist (Paket noch leer)."""
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name)
    except OSError:
        return None
    for e in entries:
        if e.is_file() and e.name.lower().endswith(".xml"):
            return e.path
    if depth > 0:
        for e in entries:
            if e.is_dir():
                found = _first_xml(e.path, depth - 1)
                if found:
                    return found
    return None


def check_queue(ziel, item, catalog, catalogs):
    """Vergleicht die Item-ID mit den wartenden Datenpaketen im selben Bucket
    (Bucket = zwei Ebenen ueber dem Zielpaket, Catalog-Ordner aus catalogs).
    Das Zielpaket selbst wird uebersprungen, pro Paket genuegt ein XML (alle
    XML eines Pakets haben dieselben Werte).

    Ein Treffer (gleiche Item-ID) im selben Catalog ist ein Konflikt - Catalog
    und Asset-Name gehoeren 1:1 zusammen (siehe ASSET_SUFFIXES). Treffer in
    anderen Catalogs ergaenzen das Item nur (anderes Asset).
    'status': 'ok', 'conflict' oder 'failed' ('error'). Dazu 'hits' (catalog,
    paket, area - nur zur Anzeige, conflict), 'checked' (gelesene Pakete) und
    'unreadable'."""
    res = {"status": "failed", "hits": [], "checked": 0, "unreadable": [], "error": ""}
    paket_pfad = os.path.normpath(ziel)
    bucket = os.path.dirname(os.path.dirname(paket_pfad))
    vorhanden = [c for c in catalogs if os.path.isdir(os.path.join(bucket, c))]
    if not vorhanden:
        res["error"] = f"keine Catalog-Ordner unter '{bucket}' gefunden (Zielpfad prüfen)"
        return res

    for c in vorhanden:
        try:
            pakete = sorted((e for e in os.scandir(os.path.join(bucket, c)) if e.is_dir()),
                            key=lambda e: e.name)
        except OSError as e:
            res["unreadable"].append(f"{c}: Ordner nicht lesbar ({e})")
            continue
        for p in pakete:
            if os.path.normcase(p.path) == os.path.normcase(paket_pfad):
                continue
            xml = _first_xml(os.path.join(p.path, "NV"))
            if not xml:
                continue
            try:
                root = ET.parse(xml).getroot()
            except (ET.ParseError, OSError) as e:
                res["unreadable"].append(f"{c}\\{p.name}: {e}")
                continue
            res["checked"] += 1
            x_item = (f"{(root.findtext('Auftragstyp') or '').strip()}-"
                      f"{(root.findtext('StacItemIdDatetime') or '').strip()}")
            if x_item != item:
                continue
            res["hits"].append({"catalog": c, "paket": p.name,
                                "area": (root.findtext("Area") or "").strip(),
                                "conflict": c == catalog})

    res["status"] = "conflict" if any(h["conflict"] for h in res["hits"]) else "ok"
    return res
