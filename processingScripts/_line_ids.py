"""
_line_ids.py  –  LineID-Formate je CameraSystem (nur Standardbibliothek).

Gemeinsame Quelle fuer GUI (Eingabe-Pruefung, Sortierung, STAC-Link) und
Script 1 (XML) - sonst laufen Sicherheitscheck, STAC-Link und XML auseinander.

ADS (ADS100 / ADS80):
    Eingabe = XML   YYYYMMDD_HHMM_QQQQQ               20200821_0952_12504

Leica DMC-4:
    Eingabe = XML   YYYYMMDD_GGGG_QQQQQ_HHMMSS        20260813_0822_41216_082221
                    Datum, Gruppennummer, Kamera-Seriennummer, Linienstart (UTC)

    Die LineIDs gehen unveraendert ins XML - nur Sortierung (chronologisch) und
    Duplikat-Entfernung finden statt.

    GGGG = Gruppennummer = HHMM der ersten beflogenen Linie. Sie kennzeichnet
    die Linien, die zusammen die AREA bilden, und ist Teil der Eingabe (sie wird
    NICHT berechnet). Alle LineIDs einer Area muessen deshalb in Datum,
    Gruppennummer und Seriennummer uebereinstimmen - siehe area_key().

    Der Anfang YYYYMMDD_HHMM_QQQQQ bleibt ADS-kompatibel.

    Achtung: Die Gruppennummer GGGG ist NICHT die BandID. Die BandID kommt aus
    dem Linienstart der ersten aufgelisteten Linie (HHMM des letzten Blocks),
    siehe band_id() - bei einer Teilmenge der Linien laufen die beiden
    auseinander.
"""

import re

DMC_CAMERA = "Leica DMC-4"

ADS_LINE_ID_PAT = re.compile(r'^\d{8}_\d{4}_\d{5}$')
DMC_LINE_ID_PAT = re.compile(r'^(\d{8})_(\d{4})_(\d{5})_(\d{6})$')

ADS_FORMAT  = "YYYYMMDD_HHMM_QQQQQ"
DMC_FORMAT  = "YYYYMMDD_GGGG_QQQQQ_HHMMSS"
ADS_EXAMPLE = "20200821_0952_12504"
DMC_EXAMPLE = "20260813_0822_41216_082221"


def is_dmc(camera):
    return camera == DMC_CAMERA


def format_hint(camera):
    return DMC_FORMAT if is_dmc(camera) else ADS_FORMAT


def example(camera):
    return DMC_EXAMPLE if is_dmc(camera) else ADS_EXAMPLE


def is_valid(line_id, camera):
    pat = DMC_LINE_ID_PAT if is_dmc(camera) else ADS_LINE_ID_PAT
    return bool(pat.match(line_id or ""))


def _parse_dmc(line_id):
    m = DMC_LINE_ID_PAT.match(line_id or "")
    if not m:
        raise ValueError(f"LineID '{line_id}' passt nicht zum DMC-4-Format {DMC_FORMAT}")
    date, group, serial, hhmmss = m.groups()
    return {"date": date, "group": group, "serial": serial, "hhmmss": hhmmss}


def line_key(line_id, camera):
    """Identitaet einer Befliegungslinie (Duplikat-Pruefung). Bei beiden
    Kamerasystemen ist die LineID selbst die Identitaet."""
    return line_id


def area_key(line_id, camera):
    """Datum + Gruppennummer + Seriennummer - der Teil, der bei allen LineIDs
    einer Area identisch sein muss. None bei ADS (dort gibt es keine Gruppe)."""
    if not is_dmc(camera):
        return None
    p = _parse_dmc(line_id)
    return f"{p['date']}_{p['group']}_{p['serial']}"


def sort_key(line_id, camera):
    """Chronologisch: Datum, dann Linienstart."""
    if not is_dmc(camera):
        return (line_id[0:8], line_id[9:13], line_id)
    p = _parse_dmc(line_id)
    return (p["date"], p["hhmmss"])


def normalize(line_ids, camera):
    """Prueft Format und (bei DMC) einheitliche Gruppe, entfernt Duplikate
    (erste Eingabe bleibt) und sortiert chronologisch. ValueError bei
    ungueltiger oder gruppenfremder LineID."""
    invalid = [l for l in line_ids if not is_valid(l, camera)]
    if invalid:
        raise ValueError(
            f"LineID(s) passen nicht zum Format {format_hint(camera)} "
            f"(CameraSystem '{camera}'): {', '.join(invalid)}")
    if is_dmc(camera) and line_ids:
        expected = area_key(line_ids[0], camera)
        fremd = [l for l in line_ids if area_key(l, camera) != expected]
        if fremd:
            raise ValueError(
                f"Alle LineIDs einer Area muessen in Datum, Gruppennummer und "
                f"Seriennummer uebereinstimmen (erwartet '{expected}_*'): "
                f"{', '.join(fremd)}")
    seen, unique = set(), []
    for l in line_ids:
        key = line_key(l, camera)
        if key not in seen:
            seen.add(key)
            unique.append(l)
    return sorted(unique, key=lambda l: sort_key(l, camera))


def xml_line_ids(line_ids, camera):
    """LineIDs, wie sie ins XML kommen: exakt wie eingegeben, bei DMC zusaetzlich
    chronologisch sortiert und ohne Duplikate. ADS unveraendert (Reihenfolge wie
    eingegeben, die GUI sortiert bereits)."""
    if not is_dmc(camera):
        return list(line_ids)
    return normalize(line_ids, camera)


def band_id(line_id, camera):
    """BandID aus der ersten im XML aufgelisteten LineID: HHMM des
    Aufnahmezeitpunkts - passt damit immer zu FirstAcquisitionTime.

    ADS: Zeitfeld [9:13]. DMC-4: die ersten vier Stellen des letzten Blocks
    (Linienstart HHMMSS), NICHT die Gruppennummer - BandID hat im XML eine
    andere Bedeutung als die Gruppierung der Area.
    """
    if is_dmc(camera):
        return _parse_dmc(line_id)["hhmmss"][0:4]
    return line_id[9:13] if len(line_id or "") >= 13 else ""


def dmc_acquisition(line_id):
    """Aufnahmezeit einer DMC-LineID als Dict wie parse_line_id_to_hundredths in
    Script 1. DMC liefert Sekunden, die Hundertstel sind immer 00."""
    p = _parse_dmc(line_id)
    return {
        "year": int(p["date"][0:4]), "month": int(p["date"][4:6]), "day": int(p["date"][6:8]),
        "hh": int(p["hhmmss"][0:2]), "mm": int(p["hhmmss"][2:4]), "ss": int(p["hhmmss"][4:6]),
        "hundredths": 0,
    }


def stac_datetime(line_ids, camera):
    """StacItemIdDatetime der ersten (fruehesten) Linie, YYYY-MM-DDtHHMMSSss.
    Identisch zu format_stac_datetime in Script 1. '—' bei ungueltiger Eingabe."""
    try:
        if is_dmc(camera):
            first = normalize(line_ids, camera)[0]
            t = dmc_acquisition(first)
            return (f"{t['year']:04d}-{t['month']:02d}-{t['day']:02d}"
                    f"t{t['hh']:02d}{t['mm']:02d}{t['ss']:02d}00")
        first = line_ids[0]
        if len(first) >= 13:
            return f"{first[0:4]}-{first[4:6]}-{first[6:8]}t{first[9:13]}0000"
    except (ValueError, IndexError):
        pass
    return "—"
