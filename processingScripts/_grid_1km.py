"""
_grid_1km.py  –  Grid-Shape (swissGRID 1km2) lesen, gemeinsam fuer Script 5
(ADS80-ASCII -> LAZ-Kacheln) und Script 6 (ADS80-DOP -> 1-km-Kacheln).

TileKey = Attribut NAME, z.B. '2795_1189' (km Ost / km Nord) - dasselbe Muster,
das Script 1, 2_2 und 4 aus den Kachelnamen lesen. Benoetigt osgeo (OGR), wird
erst in der Funktion importiert.
"""

import re
from collections import Counter

TILE_NAME_RE = re.compile(r"^\d{4}_\d{4}$")


def load_grid_cells(grid_shape_path, bbox):
    """{(ix, iy): NAME} der Grid-Zellen, die bbox (minx, miny, maxx, maxy)
    beruehren, und die Zellgroesse. Erwartet ein achsparalleles Quadratraster
    in EPSG:2056 auf Vielfachen der Zellgroesse (wie swissGRID 1km2) - nur dann
    ist Zelle (ix, iy) = [ix*size, (ix+1)*size) x [iy*size, (iy+1)*size)."""
    from osgeo import ogr, osr
    ogr.UseExceptions()
    osr.UseExceptions()
    ds = ogr.Open(grid_shape_path, 0)
    if ds is None:
        raise FileNotFoundError(f"Grid-Shape nicht lesbar: {grid_shape_path}")
    layer = ds.GetLayer()
    defn = layer.GetLayerDefn()
    if defn.GetFieldIndex("NAME") < 0:
        raise ValueError(f"Grid-Shape ohne Feld 'NAME': {grid_shape_path}")
    srs = layer.GetSpatialRef()
    target = osr.SpatialReference()
    target.ImportFromEPSG(2056)
    if srs is None or (srs.GetAuthorityCode(None) != "2056" and not srs.IsSame(target)):
        raise ValueError(f"Grid-Shape ist nicht in EPSG:2056 (LV95): {grid_shape_path}")

    layer.SetSpatialFilterRect(*bbox)
    size, cells, irregular = None, {}, []
    for feat in layer:
        name = feat.GetField("NAME")
        geom = feat.GetGeometryRef()
        if name is None or not str(name).strip() or geom is None:
            continue
        x0, x1, y0, y1 = geom.GetEnvelope()
        if size is None:
            size = round(x1 - x0, 6)
        if (abs((x1 - x0) - size) > 1e-6 or abs((y1 - y0) - size) > 1e-6
                or abs(x0 / size - round(x0 / size)) > 1e-9
                or abs(y0 / size - round(y0 / size)) > 1e-9):
            irregular.append(str(name))
            continue
        cells[(int(round(x0 / size)), int(round(y0 / size)))] = str(name).strip()
    ds = None
    if size is None or not cells:
        raise ValueError("Grid-Shape hat keine Zelle im Bereich der Daten - Koordinaten pruefen.")
    if irregular:
        raise ValueError(f"{len(irregular)} Grid-Zelle(n) sind keine achsparallelen "
                         f"{size:g}-m-Quadrate auf dem Raster (z.B. {irregular[:3]}).")
    dupes = [n for n, c in Counter(cells.values()).items() if c > 1]
    if dupes:
        raise ValueError(f"Doppelte NAME-Werte im Grid-Shape: {dupes[:5]}")
    bad = sorted(n for n in cells.values() if not TILE_NAME_RE.match(n))
    if bad:
        raise ValueError(f"NAME passt nicht zum Kachelmuster <E>_<N> (4+4 Ziffern): {bad[:5]}")
    return size, cells
