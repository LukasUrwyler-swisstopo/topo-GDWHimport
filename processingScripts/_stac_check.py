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
import ssl
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import _line_ids

COLLECTION    = "ch.swisstopo.spezialbefliegungen"
STAC_API_PROD = "https://data.geo.admin.ch/api/stac/v1"
STAC_API_INT  = "https://sys-data.int.bgdi.ch/api/stac/v1"
TIMEOUT_S     = 15

# Die VDI kommt nur ueber den Proxy der Bundesverwaltung ins Internet. urllib
# wertet die PAC-Konfiguration von Windows nicht aus und liefe ohne explizite
# Angabe in den Timeout. Ausserhalb des Bundesnetzes loest der Name nicht auf,
# dann folgt sofort die direkte Verbindung (bzw. HTTPS_PROXY, falls gesetzt).
PROXIES = (("BVCOL", "http://proxy-bvcol.admin.ch:8080"),)

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


def _opener(proxy, verify):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    handlers = [urllib.request.HTTPSHandler(context=ctx)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def _cert_error(err):
    """Zertifikatspruefung fehlgeschlagen? ssl.SSLCertVerificationError gibt es
    erst ab Python 3.7, davor ein ssl.SSLError mit derselben reason."""
    return (isinstance(err, ssl.SSLError)
            and getattr(err, "reason", "") == "CERTIFICATE_VERIFY_FAILED")


def _get_json(url, timeout):
    """GET ueber die Proxies aus PROXIES, dann direkt. Der erste Weg mit einer
    Antwort der API gilt. Gibt (code, data, via, error) zurueck: code 200 mit
    data, 404, oder None, wenn kein Weg durchkam ('error' je Weg)."""
    fehler = []
    for via, proxy in [(f"Proxy {n}", u) for n, u in PROXIES] + [("direkt", None)]:
        # Proxy mit TLS-Inspektion, deren CA nicht im Windows-Store liegt: zweiter
        # Versuch ohne Zertifikatspruefung (nur lesend, oeffentliche Metadaten)
        for verify in (True, False):
            weg = via if verify else f"{via} ohne Zertifikatsprüfung"
            try:
                with _opener(proxy, verify).open(url, timeout=timeout) as r:
                    return 200, json.load(r), weg, ""
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return 404, None, weg, ""
                # z.B. 403/407 vom Proxy -> naechster Weg
                fehler.append(f"{weg}: HTTP {e.code} {e.reason}")
            except Exception as e:
                # URLError (DNS/Netz), Timeout, kaputtes JSON
                reason = getattr(e, "reason", e)
                if verify and _cert_error(reason):
                    continue
                fehler.append(f"{weg}: {reason}")
            break
    return None, None, "", "; ".join(fehler)


def check_asset(item, gds, camera, int_env=False, timeout=TIMEOUT_S):
    """Fragt das Item ab. 'status':
    'new'    Item existiert nicht (HTTP 404)
    'free'   Item existiert, Asset dieses GDS fehlt noch
    'exists' Asset existiert -> Import wuerde ueberschreiben
    'failed' nicht pruefbar ('error'), z.B. kein Netz, Timeout, ungueltige ID
    Dazu 'assets' (alle Keys im Item), 'conflict' (vorhandene Keys dieses GDS),
    'updated' (Item-Property) und 'via' (Verbindungsweg, siehe _get_json)."""
    res = {"item": item, "env": "INT" if int_env else "PROD", "status": "failed",
           "assets": [], "conflict": [], "updated": "", "via": "", "error": ""}
    if not ITEM_ID_PAT.match(item or ""):
        res["error"] = f"ungültige Item-ID '{item}' (Auftragstyp/Line_ID prüfen)"
        return res
    code, data, res["via"], res["error"] = _get_json(item_url(item, int_env), timeout)
    if code == 404:
        res["status"] = "new"
    if code != 200:
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
