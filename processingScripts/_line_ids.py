"""
_line_ids.py  –  LineID-Formate je CameraSystem (nur Standardbibliothek).

Gemeinsame Quelle fuer GUI (Eingabe-Pruefung, Sortierung, STAC-Link) und
Script 1 (XML) - sonst laufen Sicherheitscheck, STAC-Link und XML auseinander.

ADS (ADS100 / ADS80):
    Eingabe = XML   YYYYMMDD_HHMM_QQQQQ               20200821_0952_12504

Leica DMC-4:
    Eingabe         YYYYMMDD_LLL_HHMMSS_BBB_QQQQQ     20260813_004_082750_012_41216
                    Datum, Linie, Linienstart (UTC), Bildnummer, Kamera-Seriennummer
    XML             YYYYMMDD_GGGG_QQQQQ_LLL_HHMMSS    20260813_0822_41216_004_082750
                    GGGG = Gruppennummer = HHMM der ersten beflogenen Linie der
                    Eingabe - kennzeichnet die Linien, die zusammen die AREA bilden.

    Die Bildnummer faellt weg: alle Bilder einer Linie tragen denselben
    Linienstart, mehrere Eingaben derselben Linie ergeben also eine LineID.
    Der Anfang YYYYMMDD_HHMM_QQQQQ bleibt ADS-kompatibel, BandID ([9:13]) ist
    damit die Gruppennummer.
"""

import re

DMC_CAMERA = "Leica DMC-4"

ADS_LINE_ID_PAT = re.compile(r'^\d{8}_\d{4}_\d{5}$')
DMC_LINE_ID_PAT = re.compile(r'^(\d{8})_(\d{3})_(\d{6})_(\d{3})_(\d{5})$')

ADS_FORMAT  = "YYYYMMDD_HHMM_QQQQQ"
DMC_FORMAT  = "YYYYMMDD_LLL_HHMMSS_BBB_QQQQQ"
ADS_EXAMPLE = "20200821_0952_12504"
DMC_EXAMPLE = "20260813_003_082221_001_41216"


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
    date, line, hhmmss, _image, serial = m.groups()
    return {"date": date, "line": line, "hhmmss": hhmmss, "serial": serial}


def line_key(line_id, camera):
    """Identitaet einer Befliegungslinie (Duplikat-Pruefung). DMC ohne
    Bildnummer, damit zwei Bilder derselben Linie als Duplikat gelten."""
    if not is_dmc(camera):
        return line_id
    p = _parse_dmc(line_id)
    return f"{p['date']}_{p['line']}_{p['hhmmss']}_{p['serial']}"


def sort_key(line_id, camera):
    """Chronologisch: Datum, dann Uhrzeit. Bei DMC NICHT nach Liniennummer -
    die Linien werden nicht zwingend in Nummernreihenfolge geflogen."""
    if not is_dmc(camera):
        return (line_id[0:8], line_id[9:13], line_id)
    p = _parse_dmc(line_id)
    return (p["date"], p["hhmmss"], p["line"])


def normalize(line_ids, camera):
    """Prueft das Format, entfernt Duplikate (gleiche Linie, erste Eingabe
    bleibt) und sortiert chronologisch. ValueError bei ungueltiger LineID."""
    invalid = [l for l in line_ids if not is_valid(l, camera)]
    if invalid:
        raise ValueError(
            f"LineID(s) passen nicht zum Format {format_hint(camera)} "
            f"(CameraSystem '{camera}'): {', '.join(invalid)}")
    seen, unique = set(), []
    for l in line_ids:
        key = line_key(l, camera)
        if key not in seen:
            seen.add(key)
            unique.append(l)
    return sorted(unique, key=lambda l: sort_key(l, camera))


def xml_line_ids(line_ids, camera):
    """LineIDs, wie sie ins XML kommen: chronologisch, ohne Duplikate.
    ADS unveraendert (Reihenfolge wie eingegeben), DMC umgebaut (siehe oben)."""
    if not is_dmc(camera):
        return list(line_ids)
    lines = [_parse_dmc(l) for l in normalize(line_ids, camera)]
    if not lines:
        return []
    group = lines[0]["hhmmss"][0:4]
    return [f"{p['date']}_{group}_{p['serial']}_{p['line']}_{p['hhmmss']}" for p in lines]


def dmc_acquisition(line_id):
    """Aufnahmezeit einer DMC-LineID (Eingabe-Format) als Dict wie
    parse_line_id_to_hundredths in Script 1. DMC liefert Sekunden, die
    Hundertstel sind immer 00."""
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
