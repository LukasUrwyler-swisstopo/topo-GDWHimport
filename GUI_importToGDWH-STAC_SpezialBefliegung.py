"""
GUI_importToGDWH-STAC_SpezialBefliegung.py  –  GDWH(Bucket) Import GUI
Tkinter-Oberfläche für den GDWH-Import.
Steuert die Sub-Scripts 1, 2_1 und 2_2 je nach gewähltem GDS.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import sys, os, re, threading, importlib.util, io, queue, builtins, traceback
import subprocess, json, tempfile, ctypes, webbrowser
from datetime import datetime

# ─── Sub-Script Pfade ─────────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROCESSING_DIR = os.path.join(SCRIPT_DIR, "processingScripts")
LOG_DIR     = os.path.join(SCRIPT_DIR, "logs")
SCRIPT_1    = os.path.join(PROCESSING_DIR, "1_allGDS_upload_GDWH_withCHECKxml.py")
SCRIPT_21   = os.path.join(PROCESSING_DIR, "2_1_SB_DOP_16_FOLDERorganize_by_lineID.py")
SCRIPT_22   = os.path.join(PROCESSING_DIR, "2_2_SB_DOP_16_GDS_upload_GDWH_withCHECKxml.py")
SCRIPT_3    = os.path.join(PROCESSING_DIR, "3_fix_false_nodata_dop.py")
SCRIPT_4    = os.path.join(PROCESSING_DIR, "4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py")
RUNNER_SCRIPT = os.path.join(PROCESSING_DIR, "_osgeo_runner.py")
SCRIPT_PREVIEW = os.path.join(PROCESSING_DIR, "_tif_preview_reader.py")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config", "_gdwh_config.json")

# LineID-Formate je CameraSystem - gemeinsames Modul mit Script 1 (nur Standardbibliothek)
if PROCESSING_DIR not in sys.path:
    sys.path.insert(0, PROCESSING_DIR)
import _line_ids

# ─── Auswahllisten ────────────────────────────────────────────────────────────
GDS_ITEMS = [
    ("SB_DOP",            "DOPmosaik, RGB, 8BIT, LV95"),
    ("SB_DOP_16",         "DOP Einzellinien, NRGB, 16BIT, LV95"),
    ("SB_DSM",            "DSM-Raster / Hillshade, LV95_LN02"),
    ("SB_DSM_PUNKTWOLKE", "DSM Punktwolke (LAZ), LV95_LN02"),
]

# CustomAttribute wird automatisch je GDS gesetzt – kein Dropdown nötig
GDS_CUSTOM_ATTR = {
    "SB_DOP":            "Digital OrthoPhoto - Mosaic RGB 8BIT",
    "SB_DOP_16":         "Digital OrthoPhoto - (ADS Line) NRGB 16BIT",
    "SB_DSM":            "Digital Surface Model  - Raster Mosaic (DSM photogrammetric autocorrelation)",
    "SB_DSM_PUNKTWOLKE": "Digital Surface Model - PointCloud LAZ (DSM photogrammetric autocorrelation)",
}
# Abweichungen bei Leica DMC-4 (DOP immer 4-Band RGBN, Punktwolke immer mit Farbe)
GDS_CUSTOM_ATTR_DMC = {
    "SB_DOP":            "Digital OrthoPhoto - Mosaic RGBN 8BIT",
    "SB_DSM_PUNKTWOLKE": "Digital Surface Model - PointCloud LAZ RGB (DSM photogrammetric autocorrelation)",
}

# GDWH-Catalog-Import-Link je GDS (für Hinweis-Dialog nach erfolgreicher Prozessierung).
# Zwei Umgebungen je nach GDWH-BUCKET-Path: "...\BUCKET_INT\..." -> INT-Portal
# (ltgdwhi), "...\BUCKET\..." -> Produktiv-Portal (ltgdwh).
CATALOG_HOST_PROD = "ltgdwh.adr.admin.ch"
CATALOG_HOST_INT  = "ltgdwhi.adr.admin.ch"
CATALOG_GDS_TYPES = ("SB_DOP", "SB_DOP_16", "SB_DSM", "SB_DSM_PUNKTWOLKE")


def _catalog_link(gds, ziel):
    """Baut den GDWH-Catalog-Import-Link fuer gds, je nach BUCKET/BUCKET_INT im Zielpfad."""
    if gds not in CATALOG_GDS_TYPES:
        return ""
    parts = [p for p in (ziel or "").replace("/", "\\").split("\\") if p]
    host = CATALOG_HOST_INT if "BUCKET_INT" in parts else CATALOG_HOST_PROD
    return f"https://{host}/catalog-ng/catalog/{gds}/import"

AUFTRAGSTYPEN  = ["kry", "ram", "bim", "mom", "wam"]
TERRAIN_MODELS = [
    "Digital Surface Model (DSM photogrammetric autocorrelation)",
    "swissALTI3D", "swissALTI3D/DHM25", "swissSURFACE3D",
]
CAMERA_SYSTEMS = ["Leica ADS100", "Leica ADS80", "Leica DMC-4"]
# DMC-4: SB_DOP-NoData fix '0 0 0 0' ohne Vorkorrektur (siehe _update_camera_nodata_rules),
# Punktwolken immer PF7 mit RGB (siehe Script 4 / _osgeo_runner.RGB_CAMERA_SYSTEMS),
# TerrainModel/SourceRefSys/CustomAttribute/SB_DOP_16 siehe _update_camera_meta_rules
DMC_CAMERA        = _line_ids.DMC_CAMERA
DMC_TERRAIN_MODEL = TERRAIN_MODELS[0]
SOURCE_REF_SYS = "(EPSG:2056) CH1903+ / LV95_LN02"
# SB_DOP mit DMC-4: DOP mit LHN95 gerechnet. Gleiche Form wie LV95_LN02 - der EPSG-Code
# in Klammern bleibt horizontal (2056), der Hoehenbezug steht nur als Text. Im TIFF
# selbst nur EPSG:2056 (siehe tiff_crs_target in Script 1).
SOURCE_REF_SYS_DOP_DMC = "(EPSG:2056) CH1903+ / LV95_LHN95"
NODATA_DOP_OPT = ["0 0 0   (schwarz, 8BIT RGB)",     "255 255 255   (weiss, 8BIT RGB)"]
NODATA_DOP_VAL = ["0 0 0",                            "255 255 255"]
NODATA_D16_OPT = ["0 0 0 0   (schwarz, 16BIT NRGB)", "65535 65535 65535 65535   (weiss, 16BIT NRGB)"]
NODATA_D16_VAL = ["0 0 0 0",                          "65535 65535 65535 65535"]
# SB_DOP mit Leica DMC-4: seit der Umstellung auf 4-Band RGBN (8bit) liefert die
# DMC-Pipeline (Reality Studio -> DMC-Converter) immer vier Baender, NoData immer
# schwarz. Darum genau eine Option, im GUI gesperrt. Die Wertzahl MUSS zur Bandzahl
# passen: tag_nodata_on_raster und _compute_nodata_mask (Script 1) ueberspringen
# Dateien, bei denen Werte- und Bandzahl nicht uebereinstimmen.
NODATA_DMC_OPT = ["0 0 0 0   (schwarz, 8BIT RGBN)"]
NODATA_DMC_VAL = ["0 0 0 0"]
LINE_ID_PAT    = _line_ids.ADS_LINE_ID_PAT  # SB_DOP_16 (nur ADS)

# ─── Dateinamen-Konvention pro GDS (Button "Check - NameFormat") ──────────────
NAME_FORMAT_SPECS = {
    "SB_DOP": {
        "extensions": (".tif", ".tiff", ".tfw"),
        "regexes": [re.compile(r'^20\d{2}_.+_DOP_.*\d{4}_\d{4}_LV95\.(tif|tiff|tfw)$', re.IGNORECASE)],
        "example":  "202X_AREANAME_DOP_..._XXXX_YYYY_LV95.tif / .tfw",
    },
    "SB_DOP_16": {
        "extensions": (".tif", ".tiff", ".tfw"),
        "regexes": [re.compile(r'^20\d{2}_.+_DOP_.*\d{4}_\d{4}_LV95\.(tif|tiff|tfw)$', re.IGNORECASE)],
        "example":  "202X_AREANAME_DOP_..._XXXX_YYYY_LV95.tif / .tfw",
    },
    "SB_DSM": {
        "extensions": (".tif", ".tiff", ".tfw"),
        "regexes": [
            re.compile(r'^20\d{2}_.+_DSM_.*_LV95_LN02\.(tif|tiff|tfw)$', re.IGNORECASE),
            re.compile(r'^20\d{2}_.+_hillshade_.*_LV95_LN02\.(tif|tiff)$', re.IGNORECASE),
        ],
        "example":  "202X_AREANAME_DSM_..._LV95_LN02.tif / .tfw  und/oder  "
                    "202X_AREANAME_hillshade_..._LV95_LN02.tif",
    },
    "SB_DSM_PUNKTWOLKE": {
        "extensions": (".laz",),
        "regexes": [re.compile(r'^20\d{2}_.+_TIN_.*\d{4}_\d{4}_LV95_LN02\.laz$', re.IGNORECASE)],
        "example":  "202X_AREANAME_TIN_..._XXXX_YYYY_LV95_LN02.laz",
    },
}

# ─── Farbpaletten ─────────────────────────────────────────────────────────────
LIGHT = {
    "root":      "#f0f0f0",
    "panel":     "#f5f5f5",
    "input":     "#ffffff",
    "fg":        "#1a1a1a",
    "fg_dim":    "#666666",
    "accent":    "#0063b1",
    "scroll":    "#9098a8",   # Grau mit leichtem Blaustich für Scrollbar
    "hdr_bg":    "#1a3a5c",
    "hdr_fg":    "#ffffff",
    "btn":       "#e1e1e1",
    "btn_hover": "#c8c8c8",
    "list":      "#ffffff",
    "log_bg":    "#1e1e1e",
    "log_fg":    "#d4d4d4",
    "sep":       "#c0c0c0",
    "sel_bg":    "#0078d4",
    "sel_fg":    "#ffffff",
    "ok":        "#2e7d32",
    "err":       "#c62828",
    "hint":      "#8a6f2e",   # Gedämpftes Amber für Info-Hinweise
}

DARK = {
    "root":      "#1e1e1e",   # Photoshop: tiefstes Dunkelgrau
    "panel":     "#252526",   # Section-Hintergrund
    "input":     "#3c3c3c",   # Eingabefelder
    "fg":        "#cccccc",   # Haupttext
    "fg_dim":    "#7a7a7a",   # Gedimmter Hinweistext
    "accent":    "#4fc3f7",   # Hellblau für Hervorhebungen
    "scroll":    "#6a6f7d",   # Grau mit leichtem Blaustich für Scrollbar
    "hdr_bg":    "#1a1a1a",   # Header-Balken
    "hdr_fg":    "#cccccc",   # Header-Text
    "btn":       "#3c3c3c",   # Button-Hintergrund
    "btn_hover": "#505050",   # Button hover
    "list":      "#2d2d30",   # Listbox-Hintergrund
    "log_bg":    "#1e1e1e",   # Log-Bereich
    "log_fg":    "#d4d4d4",
    "sep":       "#3c3c3c",   # Trennlinien / Rahmen
    "sel_bg":    "#094771",   # Selektion blau
    "sel_fg":    "#cccccc",
    "ok":        "#66bb6a",
    "err":       "#ef5350",
    "hint":      "#c9a84c",   # Gedämpftes Amber für Info-Hinweise
}

# ─── OSGeo4W Python Erkennung ─────────────────────────────────────────────────
def _detect_osgeo_python():
    """Gibt den Pfad zum OSGeo4W Python zurück (aus Config, System-Python oder bekannten Pfaden)."""
    # 1. Gespeicherte Konfiguration
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                path = json.load(f).get("osgeo_python", "")
            if path and os.path.isfile(path):
                return path
        except Exception:
            pass

    # 2. osgeo im aktuellen Python verfügbar → kein Subprocess nötig
    try:
        if importlib.util.find_spec("osgeo") is not None:
            return sys.executable
    except Exception:
        pass

    # 3. Bekannte Installationspfade
    candidates = [
        r"C:\OSGeo4W\bin\python3.exe",
        r"C:\OSGeo4W64\bin\python3.exe",
    ]
    for base in [r"C:\Program Files", r"C:\Program Files (x86)"]:
        if os.path.isdir(base):
            try:
                for entry in os.listdir(base):
                    if entry.lower().startswith("qgis"):
                        candidates.append(os.path.join(base, entry, "bin", "python3.exe"))
            except Exception:
                pass

    for p in candidates:
        if os.path.isfile(p):
            return p
    return ""


def _save_osgeo_config(path):
    """Speichert den OSGeo4W Python-Pfad in _gdwh_config.json."""
    try:
        cfg = {}
        if os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
        cfg["osgeo_python"] = path
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ─── Lokaler Staging-Ordner (Performance) ─────────────────────────────────────
# Quelle und (bestehendes) Ziel werden vor der Verarbeitung auf ein schnelles,
# lokales/VDI-eigenes Laufwerk gespiegelt statt direkt uebers Eingangs-/GDWH-
# Netzlaufwerk mehrfach gelesen/geschrieben zu werden (siehe _osgeo_runner.py).
# Reduziert bei grossen Lieferungen (viele/grosse Tiles) die Laufzeit deutlich.
DEFAULT_STAGING_FOLDERNAME = "00_GDWH-STAC_TempProcessingFolder"


def _detect_staging_dir():
    """Gibt den lokalen Staging-Ordner zurueck: aus gespeicherter Config, sonst
    Y:\\<Default>, falls das VDI-eigene Y:\\-Laufwerk vorhanden ist. Sonst leer
    (kein Staging, Verarbeitung direkt uebers Netzlaufwerk wie bisher) - der
    Benutzer muss dann selbst einen Ordner via 'Ändern…' waehlen (z.B. auf
    einer Workstation ohne Y:\\)."""
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                path = json.load(f).get("staging_dir", "")
            if path and os.path.isdir(os.path.splitdrive(path)[0] + "\\"):
                return path
        except Exception:
            pass

    if os.path.isdir("Y:\\"):
        return os.path.join("Y:\\", DEFAULT_STAGING_FOLDERNAME)

    return ""


def _save_staging_config(path):
    """Speichert den lokalen Staging-Ordner-Pfad in _gdwh_config.json."""
    try:
        cfg = {}
        if os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
        cfg["staging_dir"] = path
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ─── Hilfsfunktionen ──────────────────────────────────────────────────────────
def _detect_python_home(python_exe):
    """Leitet PYTHONHOME vom Python-Executable ab (QGIS: apps\\PythonXXX, OSGeo4W: root)."""
    bin_dir  = os.path.dirname(python_exe)
    root_dir = os.path.dirname(bin_dir)
    apps_dir = os.path.join(root_dir, "apps")
    if os.path.isdir(apps_dir):
        for entry in sorted(os.listdir(apps_dir)):
            if entry.lower().startswith("python"):
                candidate = os.path.join(apps_dir, entry)
                if os.path.isdir(os.path.join(candidate, "Lib")):
                    return candidate
    if os.path.isdir(os.path.join(root_dir, "Lib")):
        return root_dir
    return root_dir


def _lade_modul(name, pfad):
    if not os.path.isfile(pfad):
        raise FileNotFoundError(f"Sub-Script nicht gefunden:\n  {pfad}")
    spec = importlib.util.spec_from_file_location(name, pfad)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _QueueWriter(io.TextIOBase):
    def __init__(self, q):
        self.q = q
    def write(self, text):
        if text:
            self.q.put(("log", text))
        return len(text)
    def flush(self):
        pass


# ─── LineID-Listbox-Widget ────────────────────────────────────────────────────
class LineIDWidget(ttk.LabelFrame):
    def __init__(self, parent, label, comment="", camera_getter=None, **kw):
        super().__init__(parent, text=label, padding=6, **kw)
        self._max_ids = None  # None = unbegrenzt
        self._comment_text = comment
        self._comment_lbl = None
        # Liefert das CameraSystem (bestimmt das LineID-Format, siehe _line_ids.py);
        # ohne Getter immer ADS-Format (allAreaLineIDs, nur SB_DOP_16)
        self._camera_getter = camera_getter
        self._build()

    def _camera(self):
        return self._camera_getter() if self._camera_getter else ""

    def _fmt_text(self):
        text = f" Format: {_line_ids.format_hint(self._camera())}"
        if self._max_ids != 1:
            text += "  |  Mehrere IDs: aus Excel einfügen (Ctrl+V)"
        return text

    def _build(self):
        if self._comment_text:
            self._comment_lbl = ttk.Label(self, text=self._comment_text,
                                           font=("Segoe UI", 8, "italic"))
            self._comment_lbl.pack(fill="x", pady=(0, 3))
        ef = ttk.Frame(self)
        ef.pack(fill="x")
        self.var = tk.StringVar()
        self._entry = ttk.Entry(ef, textvariable=self.var, width=28)
        self._entry.pack(side="left", fill="x", expand=True)
        self._entry.bind("<Return>",     lambda _: self._add())
        self._entry.bind("<Control-v>",  self._on_paste)
        self._entry.bind("<Control-V>",  self._on_paste)
        ttk.Button(ef, text="  +  ", command=self._add).pack(side="left", padx=(6, 0))

        # Eigene Zeile unter Entry/Button, damit der "+"-Button (essentiell)
        # bei schmalerer Spaltenbreite nie durch den Hinweistext verdrängt wird.
        self._fmt_lbl = ttk.Label(self, text=self._fmt_text(), font=("", 8))
        self._fmt_lbl.pack(fill="x", pady=(2, 0))

        lf = ttk.Frame(self)
        lf.pack(fill="both", expand=True, pady=(4, 0))
        sb = ttk.Scrollbar(lf, orient="vertical")
        self.lb = tk.Listbox(lf, height=4, yscrollcommand=sb.set,
                              selectmode="extended", font=("Courier New", 9),
                              activestyle="none", relief="flat", borderwidth=1)
        sb.config(command=self.lb.yview)
        sb.pack(side="right", fill="y")
        self.lb.pack(side="left", fill="both", expand=True)
        ttk.Button(self, text="Ausgewählte entfernen", command=self._remove
                    ).pack(anchor="e", pady=(3, 0))

    def set_max_ids(self, n):
        """Setzt Limit auf n IDs (None = unbegrenzt). Entfernt überzählige Einträge."""
        self._max_ids = n
        self._fmt_lbl.config(text=self._fmt_text())
        if n is not None:
            while self.lb.size() > n:
                self.lb.delete(self.lb.size() - 1)

    def refresh_camera(self):
        """Nach einem CameraSystem-Wechsel: Formathinweis anpassen und Eintraege
        entfernen, die nicht zum neuen LineID-Format passen (ADS <-> DMC-4).
        Gibt die entfernten IDs zurueck."""
        self._fmt_lbl.config(text=self._fmt_text())
        cam = self._camera()
        ids = list(self.lb.get(0, "end"))
        removed = [i for i in ids if not _line_ids.is_valid(i, cam)]
        if removed:
            self.lb.delete(0, "end")
            for i in ids:
                if i not in removed:
                    self.lb.insert("end", i)
        self._resort()
        return removed

    def _existing_line_keys(self):
        cam = self._camera()
        return {_line_ids.line_key(i, cam) for i in self.lb.get(0, "end")
                if _line_ids.is_valid(i, cam)}

    def _add_one(self, val):
        """Fügt eine einzelne validierte LineID zur Liste hinzu. Gibt True zurück bei Erfolg."""
        cam = self._camera()
        if self._max_ids is not None and self.lb.size() >= self._max_ids:
            messagebox.showwarning("Limit erreicht",
                f"Für SB_DOP_16 darf nur genau 1 Line_ID angegeben werden.", parent=self)
            return False
        if not _line_ids.is_valid(val, cam):
            messagebox.showwarning("Ungültiges Format",
                f"Erwartet: {_line_ids.format_hint(cam)}\nBeispiel:  {_line_ids.example(cam)}"
                f"\n\nEingabe: {val}",
                parent=self)
            return False
        if _line_ids.line_key(val, cam) in self._existing_line_keys():
            # DMC-4: auch ein anderes Bild derselben Linie gilt als Duplikat
            messagebox.showwarning("Duplikat", f"Linie bereits erfasst: {val}", parent=self)
            return False
        self.lb.insert("end", val)
        self._resort()
        return True

    def _add(self):
        val = self.var.get().strip()
        if self._add_one(val):
            self.var.set("")

    def _on_paste(self, _=None):
        """Verarbeitet Clipboard-Inhalt – unterstützt Mehrfacheingabe aus Excel (eine ID pro Zeile)."""
        try:
            clipboard = self._entry.clipboard_get()
        except Exception:
            return
        lines = [l.strip() for l in clipboard.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                 if l.strip()]
        if len(lines) <= 1:
            return  # Einzelne Zeile → normales Paste-Verhalten
        # Mehrere Zeilen: Entry leeren und bulk verarbeiten
        self.var.set("")
        # Limit berücksichtigen
        if self._max_ids is not None:
            available = self._max_ids - self.lb.size()
            if available <= 0:
                messagebox.showwarning("Limit erreicht",
                    "Für SB_DOP_16 darf nur genau 1 Line_ID angegeben werden.", parent=self)
                return "break"
            lines = lines[:available]
        cam = self._camera()
        added, skipped = [], []
        existing = self._existing_line_keys()
        for line in lines:
            if not _line_ids.is_valid(line, cam):
                skipped.append(f"  {line}  →  falsches Format")
            elif _line_ids.line_key(line, cam) in existing:
                skipped.append(f"  {line}  →  Duplikat (Linie bereits erfasst)")
            else:
                self.lb.insert("end", line)
                existing.add(_line_ids.line_key(line, cam))
                added.append(line)
        if added:
            self._resort()
        if skipped:
            messagebox.showwarning(
                "Mehrfacheingabe – Warnung",
                f"{len(added)} ID(s) hinzugefügt.\n\n"
                f"Nicht importiert ({len(skipped)}):\n" +
                "\n".join(skipped[:15]) + ("\n…" if len(skipped) > 15 else ""),
                parent=self,
            )
        return "break"

    def _remove(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)

    def _resort(self):
        """Sortiert die Liste chronologisch, älteste zuoberst - bei DMC-4 nach
        Datum + Linienstart, nicht nach Liniennummer (siehe _line_ids.sort_key)."""
        cam = self._camera()
        try:
            ids = sorted(self.lb.get(0, "end"), key=lambda i: _line_ids.sort_key(i, cam))
        except (ValueError, IndexError):
            ids = sorted(self.lb.get(0, "end"))
        self.lb.delete(0, "end")
        for i in ids:
            self.lb.insert("end", i)

    def get_ids(self):
        return list(self.lb.get(0, "end"))

    def clear(self):
        self.lb.delete(0, "end")

    def apply_theme(self, T):
        self.lb.config(
            bg=T["list"], fg=T["fg"],
            selectbackground=T["sel_bg"], selectforeground=T["sel_fg"],
            highlightbackground=T["sep"], highlightcolor=T["sep"],
        )
        self._fmt_lbl.config(foreground=T["fg_dim"])
        if self._comment_lbl:
            self._comment_lbl.config(foreground=T["fg_dim"])



# ─── Sicherheitscheck-Dialogue ──────────────────────────────────────────────────
class SicherheitsCheckDialog(tk.Toplevel):
    """Modaler Sicherheitscheck-Dialog vor dem GDWH-Import."""

    _BOX_DARK = {
        "ok":   ("#1b5e20", "#a5d6a7"),
        "warn": ("#3e2a00", "#ffe082"),
        "err":  ("#7f0000", "#ef9a9a"),
        "skip": ("#2c2c2c", "#9e9e9e"),
    }
    _BOX_LIGHT = {
        "ok":   ("#e8f5e9", "#1b5e20"),
        "warn": ("#fff8e1", "#e65100"),
        "err":  ("#ffebee", "#b71c1c"),
        "skip": ("#f5f5f5", "#757575"),
    }

    def __init__(self, parent, gds, meta, quelle, ziel, area="", stac_dt="",
                 tilekey="", tilekey_file="", dark=True):
        super().__init__(parent)
        self.result = False
        self._dark = dark
        T   = DARK if dark else LIGHT
        box = self._BOX_DARK if dark else self._BOX_LIGHT
        self._T   = T
        self._box = box

        self.title("Sicherheitscheck – Import starten?")
        self.resizable(False, False)
        self.configure(bg=T["root"])
        self.grab_set()
        self.focus_set()

        # Header (fix oben)
        hdr = tk.Frame(self, bg=T["hdr_bg"])
        hdr.pack(side="top", fill="x")
        tk.Label(hdr,
                 text="  Sicherheitscheck  –  Alle Werte vor dem Import prüfen",
                 font=("Segoe UI", 11, "bold"),
                 bg=T["hdr_bg"], fg=T["hdr_fg"]).pack(side="left", pady=10)

        # Button-Bereich (fix unten) – wird zuerst gepackt, damit er beim
        # Verkleinern des Fensters nie vom Scrollbereich verdeckt wird.
        tk.Frame(self, height=1, bg=T["sep"]).pack(side="bottom", fill="x")
        btn_row = tk.Frame(self, bg=T["root"])
        btn_row.pack(side="bottom", fill="x", padx=14, pady=10)

        # Scrollbarer Inhaltsbereich dazwischen
        scroll_wrap = tk.Frame(self, bg=T["root"])
        scroll_wrap.pack(side="top", fill="both", expand=True)
        canvas = tk.Canvas(scroll_wrap, bg=T["root"],
                           highlightthickness=0, bd=0)
        vscroll = ttk.Scrollbar(scroll_wrap, orient="vertical",
                                command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        scroll_body = tk.Frame(canvas, bg=T["root"])
        body_win = canvas.create_window((0, 0), window=scroll_body, anchor="nw")

        def _on_body_config(_=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        scroll_body.bind("<Configure>", _on_body_config)

        def _on_canvas_config(event):
            # innerer Frame folgt der Canvas-Breite
            canvas.itemconfigure(body_win, width=event.width)
        canvas.bind("<Configure>", _on_canvas_config)

        # Mausrad-Scrolling (Windows / Linux)
        def _on_mousewheel(event):
            if event.num == 5 or event.delta < 0:
                canvas.yview_scroll(1, "units")
            elif event.num == 4 or event.delta > 0:
                canvas.yview_scroll(-1, "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", _on_mousewheel)
        canvas.bind_all("<Button-5>", _on_mousewheel)
        # Beim Schliessen die globalen Bindings wieder lösen
        self._unbind_wheel = lambda: (
            canvas.unbind_all("<MouseWheel>"),
            canvas.unbind_all("<Button-4>"),
            canvas.unbind_all("<Button-5>"),
        )

        def _section(title):
            outer = tk.Frame(scroll_body, bg=T["root"])
            outer.pack(fill="x", padx=14, pady=(10, 2))
            tk.Label(outer, text=title,
                     font=("Segoe UI", 8, "bold"),
                     bg=T["root"], fg=T["accent"]).pack(anchor="w")
            tk.Frame(outer, height=1, bg=T["sep"]).pack(fill="x", pady=(1, 4))
            body = tk.Frame(outer, bg=T["root"])
            body.pack(fill="x")
            return body

        def _kv(parent, key, value, wrap=560):
            row = tk.Frame(parent, bg=T["root"])
            row.pack(fill="x", pady=1)
            tk.Label(row, text=key, width=18, anchor="nw",
                     font=("Segoe UI", 9), bg=T["root"], fg=T["fg_dim"]
                     ).pack(side="left", anchor="n")
            tk.Label(row, text=value, anchor="nw",
                     font=("Segoe UI", 9),
                     bg=T["root"], fg=T["fg"],
                     wraplength=wrap, justify="left"
                     ).pack(side="left", fill="x", expand=True, anchor="n")

        def _path_block(parent, label, path):
            blk = tk.Frame(parent, bg=T["root"])
            blk.pack(fill="x", pady=(2, 6))
            tk.Label(blk, text=label,
                     font=("Segoe UI", 9, "bold"),
                     bg=T["root"], fg=T["fg_dim"]).pack(anchor="w")
            tk.Label(blk, text=path,
                     font=("Courier New", 9),
                     bg=T["panel"], fg=T["fg"],
                     anchor="w", wraplength=580, justify="left",
                     padx=8, pady=5).pack(fill="x")

        # Metadaten
        sec1 = _section("METADATEN")
        _kv(sec1, "GDS:", gds)
        _kv(sec1, "Area:", area)
        _kv(sec1, "TileKey (1. Tile):", f"{tilekey}   (aus: {tilekey_file})")
        _kv(sec1, "STAC ITEM - Name:", stac_dt)
        _kv(sec1, "Auftragstyp:", meta.get("Auftragstyp", ""))
        _kv(sec1, "CustomAttribute:", meta.get("CustomAttribute", ""))
        _kv(sec1, "Line_ID:", ", ".join(meta.get("Line_ID", [])))
        is_dmc = meta.get("CameraSystem") == DMC_CAMERA
        if is_dmc:
            try:
                xml_ids = ", ".join(_line_ids.xml_line_ids(meta.get("Line_ID", []), DMC_CAMERA))
            except ValueError as e:
                xml_ids = f"FEHLER – {e}"
            _kv(sec1, "LineID (XML):", xml_ids)
        if "allAreaLineIDs" in meta:
            _kv(sec1, "allAreaLineIDs:", ", ".join(meta["allAreaLineIDs"]))
        if gds == "SB_DSM":
            nodata = "automatisch per Dateiname"
        elif gds == "SB_DSM_PUNKTWOLKE":
            nodata = "keine NoData nötig"
        else:
            nodata = meta.get("NoData", "")
        _kv(sec1, "Input NoData", nodata)
        if gds == "SB_DOP":
            _kv(sec1, "Falsche NoData-Pixel vorkorrigieren:",
                "Ja" if meta.get("FixFalseNodata") else "Nein")
            fix_hint_row = tk.Frame(sec1, bg=T["root"])
            fix_hint_row.pack(fill="x", pady=(0, 3))
            tk.Label(fix_hint_row,
                     text="Hinweis: Je nach Grösse des DOP kann diese Vorkorrektur einige Zeit "
                          "dauern. Deaktiviert beschleunigt sie den Import, birgt aber das "
                          "Risiko, dass einzelne Pixel innerhalb der Nutzdaten (z.B. durch "
                          "radiometrische Korrekturen entstanden) fälschlicherweise als "
                          "NoData maskiert werden.",
                     font=("Segoe UI", 8, "italic"),
                     bg=T["root"], fg=T["fg_dim"], anchor="nw",
                     wraplength=560, justify="left").pack(anchor="w")
        _kv(sec1, "TerrainModel:", meta.get("TerrainModel", ""))
        _kv(sec1, "SourceRefSys:", meta.get("SourceReferenceSystem", ""))
        if gds == "SB_DSM":
            _kv(sec1, "TIFF-CRS:", "DSM EPSG:2056+5728 (LV95 + LN02), Hillshade EPSG:2056 "
                                   "– wird geprüft und bei Bedarf gesetzt")
        elif gds == "SB_DOP" and is_dmc:
            _kv(sec1, "TIFF-CRS:", "EPSG:2056 (LV95) – wird geprüft und bei Bedarf gesetzt")
        if gds == "SB_DSM_PUNKTWOLKE":
            _kv(sec1, "LAS 1.2 -> 1.4 Vorkonversion:",
                "immer aktiv (CRS-Tag EPSG:2056+5728 wird byte-exakt gesetzt, siehe Log)")
            _kv(sec1, "Punktformat:",
                "PF7 mit RGB (Kachel ohne RGB-Werte = Abbruch)"
                if is_dmc
                else "PF6 (ohne RGB, Kachel mit RGB-Werten = Abbruch)")
        _kv(sec1, "CameraSystem:", meta.get("CameraSystem", ""))

        # Pfade
        sec2 = _section("PFADE")
        if gds == "SB_DOP":
            _path_block(sec2, "Data-Input Path (DOP-Mosaik):", quelle)
        elif gds == "SB_DOP_16":
            _path_block(sec2, "Input-Pfad (Subfolder der Line_ID):", quelle)
        else:
            _path_block(sec2, "Quelle:", quelle)
        _path_block(sec2, "Ziel:", ziel)

        # Kontrollfragen durch Nutzer – GDS-spezifisch
        sec3 = _section("KONTROLLFRAGEN  –  NUTZERBESTÄTIGUNG ERFORDERLICH")
        bg_c, fg_c = box["warn"]

        if gds in ("SB_DSM", "SB_DSM_PUNKTWOLKE"):
            expected_crs = "DSM:  LV95_LN02  (EPSG:2056 horizontal  +  LN02 vertikal)"
        else:
            expected_crs = "DOP:  LV95  (EPSG:2056 horizontal)"

        # Fragenkatalog je GDS festlegen
        if gds == "SB_DOP_16":
            check_questions = [
                "Ist der Input-Pfad (Subfolder, welcher für die Ausführung des Scripts "
                "verwendet wird) korrekt und passt er zur Line_ID, die importiert werden soll?",
                "Sind die NoData-Werte korrekt? (16BIT, 4-Band: schwarze "
                "Background-Pixel = 0 0 0 0  /  weisse = 65535 65535 65535 65535)   "
                "Vorgängig mit dem Button «check input-NoData» visuell kontrollieren.",
            ]
        elif gds == "SB_DOP":
            nodata_frage = (
                "Sind die NoData-Werte korrekt? (8BIT RGBN, 4-Band: schwarze "
                "Background-Pixel = 0 0 0 0)   "
                if is_dmc else
                "Sind die NoData-Werte korrekt? (8BIT, 3-Band: schwarze "
                "Background-Pixel = 0 0 0  /  weisse = 255 255 255)   ")
            check_questions = [
                "Ist der Input Folder der korrekte Pfad zum DOP-Mosaik, "
                "welches importiert werden soll?",
                "Sind die Line_IDs korrekt?",
                nodata_frage +
                "Vorgängig mit dem Button «check input-NoData» visuell kontrollieren.",
            ]
        else:
            check_questions = [
                f"Ist der Inputpfad im korrekten Referenzsystem?   ({expected_crs})",
            ]

        chk_box = tk.Frame(sec3, bg=bg_c, padx=12, pady=10)
        chk_box.pack(fill="x", pady=(0, 4))

        # Eine BooleanVar pro Frage; Import erst aktiv wenn alle angehakt sind.
        self._check_vars = []
        for i, frage in enumerate(check_questions):
            var = tk.BooleanVar(value=False)
            self._check_vars.append(var)
            tk.Label(chk_box,
                     text=frage,
                     font=("Segoe UI", 10, "bold"),
                     bg=bg_c, fg=fg_c, anchor="w",
                     wraplength=560, justify="left").pack(anchor="w",
                     pady=(0 if i == 0 else 8, 4))
            tk.Checkbutton(chk_box,
                           text="  Ja – kontrolliert und korrekt",
                           variable=var,
                           font=("Segoe UI", 10, "bold"),
                           bg=bg_c, fg=fg_c,
                           activebackground=bg_c, activeforeground=fg_c,
                           selectcolor=bg_c,
                           command=self._on_crs_toggle).pack(anchor="w")

        # Buttons in das fix unten verankerte btn_row (oben definiert)
        self._import_btn = tk.Button(btn_row, text="▶   Import starten",
                  font=("Segoe UI", 10, "bold"),
                  bg=T["btn"], fg=T["fg_dim"],
                  activebackground=T["sel_bg"], activeforeground="#ffffff",
                  relief="flat", padx=18, pady=7, cursor="hand2",
                  state="disabled",
                  command=self._confirm)
        self._import_btn.pack(side="right")

        tk.Button(btn_row, text="Abbrechen",
                  font=("Segoe UI", 10),
                  bg=T["btn"], fg=T["fg"],
                  activebackground=T["btn_hover"], activeforeground=T["fg"],
                  relief="flat", padx=14, pady=7, cursor="hand2",
                  command=self._on_close).pack(side="right", padx=(0, 10))

        self.update_idletasks()
        w = 640
        # Wunschhoehe = voller Inhalt. Wird sie groesser als der Bildschirm,
        # greift der Scrollbereich und der Button bleibt trotzdem fix unten.
        screen_h = self.winfo_screenheight()
        req_h = self.winfo_reqheight() + 24
        h = min(req_h, screen_h - 80)
        px = parent.winfo_rootx() + parent.winfo_width() // 2 - w // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2 - h // 2
        self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        # Vertikal frei skalierbar (Breite fix), Mindesthoehe sichert Button-Sicht
        self.resizable(False, True)
        self.minsize(w, min(h, 420))

        # Titelleiste im Darkmode dunkel faerben (Windows DWM)
        self._set_titlebar_dark(self._dark)

        self.bind("<Escape>", lambda _: self._on_close())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        # Globale Mausrad-Bindings lösen, bevor das Fenster zerstört wird
        try:
            self._unbind_wheel()
        except Exception:
            pass
        self.destroy()

    def _set_titlebar_dark(self, dark):
        # Fenster muss sichtbar sein bevor DWM reagiert – sonst erneut versuchen
        if not self.winfo_ismapped():
            self.after(50, lambda: self._set_titlebar_dark(dark))
            return
        try:
            hwnd  = int(self.wm_frame(), 16)
            value = ctypes.c_int(1 if dark else 0)
            for attr in (20, 19):   # 20 = Win11 / Win10 2004+; 19 = ältere Builds
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
        except Exception:
            pass

    def _confirm(self):
        self.result = True
        try:
            self._unbind_wheel()
        except Exception:
            pass
        self.destroy()

    def _on_crs_toggle(self):
        if all(v.get() for v in self._check_vars):
            self._import_btn.config(
                state="normal",
                bg="#005fa3" if self._dark else "#0063b1",
                fg="#ffffff",
            )
        else:
            self._import_btn.config(
                state="disabled",
                bg=self._T["btn"],
                fg=self._T["fg_dim"],
            )

    def wait(self):
        self.wait_window()
        return self.result


class ImportDoneDialog(tk.Toplevel):
    """Hinweis-Dialog nach erfolgreicher Prozessierung – GDS-spezifischer GDWH-Catalog-Link
    und die nächsten Schritte für den manuellen GDWH-Import."""

    def __init__(self, parent, gds, ordner_name, ziel="", dark=True):
        super().__init__(parent)
        self._dark = dark
        T = DARK if dark else LIGHT
        self._link = _catalog_link(gds, ziel)

        self.title("Prozessierung abgeschlossen")
        self.resizable(False, False)
        self.configure(bg=T["root"])
        self.grab_set()
        self.focus_set()

        hdr = tk.Frame(self, bg=T["hdr_bg"])
        hdr.pack(side="top", fill="x")
        tk.Label(hdr, text="  ✓  Prozessierung erfolgreich abgeschlossen",
                 font=("Segoe UI", 11, "bold"),
                 bg=T["hdr_bg"], fg=T["hdr_fg"]).pack(side="left", pady=10)

        body = tk.Frame(self, bg=T["root"])
        body.pack(fill="both", expand=True, padx=16, pady=(12, 4))

        tk.Label(body,
                 text="Die Daten wurden für den GDWH-Import vorbereitet und sind nun "
                      "bereit für den Import im GDWH-Catalog.",
                 font=("Segoe UI", 9),
                 bg=T["root"], fg=T["fg"], anchor="w", justify="left",
                 wraplength=560).pack(fill="x", pady=(0, 10))

        steps = [
            "1.  Öffne den GDWH-Catalog (Button unten).",
            f"2.  Navigiere zum Datenpaket:",
            "3.  Führe zuerst den CHECK des Datenpakets aus.",
            "4.  Wenn CHECK-OK, starte den Import des Datenpakets.",
        ]
        for s in steps:
            tk.Label(body, text=s, font=("Segoe UI", 9),
                     bg=T["root"], fg=T["fg"], anchor="w", justify="left",
                     wraplength=560).pack(fill="x", pady=1)
            if s.startswith("2."):
                tk.Label(body, text=ordner_name or "–",
                         font=("Courier New", 9, "bold"),
                         bg=T["panel"], fg=T["accent"],
                         anchor="w", padx=8, pady=5).pack(fill="x", pady=(2, 6))

        tk.Frame(body, height=1, bg=T["sep"]).pack(fill="x", pady=(6, 8))

        tk.Label(body,
                 text="Per Mail wird eine Bestätigung zugestellt (import successful/failed).\n"
                      "Bei der Meldung \"successfully\" ist der Import nach GDWH und STAC abgeschlossen.",
                 font=("Segoe UI", 9), fg=T["fg_dim"], bg=T["root"],
                 anchor="w", justify="left", wraplength=560).pack(fill="x")

        tk.Frame(self, height=1, bg=T["sep"]).pack(side="bottom", fill="x")
        btn_row = tk.Frame(self, bg=T["root"])
        btn_row.pack(side="bottom", fill="x", padx=14, pady=10)

        tk.Button(btn_row, text="Schliessen",
                  font=("Segoe UI", 10),
                  bg=T["btn"], fg=T["fg"],
                  activebackground=T["btn_hover"], activeforeground=T["fg"],
                  relief="flat", padx=14, pady=7, cursor="hand2",
                  command=self._on_close).pack(side="right")

        if self._link:
            tk.Button(btn_row, text="GDWH-Catalog öffnen",
                      font=("Segoe UI", 10, "bold"),
                      bg="#005fa3" if dark else "#0063b1", fg="#ffffff",
                      activebackground=T["sel_bg"], activeforeground="#ffffff",
                      relief="flat", padx=14, pady=7, cursor="hand2",
                      command=self._open_link).pack(side="right", padx=(0, 10))

        self.update_idletasks()
        w = 600
        h = self.winfo_reqheight() + 20
        px = parent.winfo_rootx() + parent.winfo_width() // 2 - w // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2 - h // 2
        # Kein max(0, ...)-Clamping: bei Mehrmonitor-Setups mit einem Monitor
        # links/oberhalb des primären hat das GUI-Fenster dort negative
        # Bildschirmkoordinaten - ein Clamp auf 0 wuerde das Popup faelschlich
        # zurueck auf den primaeren Monitor zwingen, statt es dort zu zeigen,
        # wo das GUI tatsaechlich steht.
        self.geometry(f"{w}x{h}+{px}+{py}")

        self._set_titlebar_dark(dark)
        self.bind("<Escape>", lambda _: self._on_close())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _open_link(self):
        try:
            webbrowser.open(self._link)
        except Exception:
            pass

    def _set_titlebar_dark(self, dark):
        if not self.winfo_ismapped():
            self.after(50, lambda: self._set_titlebar_dark(dark))
            return
        try:
            hwnd  = int(self.wm_frame(), 16)
            value = ctypes.c_int(1 if dark else 0)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
        except Exception:
            pass

    def _on_close(self):
        self.destroy()

    def wait(self):
        self.wait_window()


class PunktwolkeAttributeHinweisDialog(tk.Toplevel):
    """Hinweis-Dialog vor dem Oeffnen des GDWH-Catalog-Portals fuer SB_DSM_PUNKTWOLKE:
    beim Erstellen des Datenpakets duerfen im Portal nur bestimmte Attribute gesetzt
    werden, der Rest muss leer bleiben."""

    def __init__(self, parent, url, env_label, dark=True):
        super().__init__(parent)
        self._dark = dark
        self._url = url
        T = DARK if dark else LIGHT

        self.title("Hinweis: Attribute Datenpaket (Punktwolke)")
        self.resizable(False, False)
        self.configure(bg=T["root"])
        self.grab_set()
        self.focus_set()

        hdr = tk.Frame(self, bg=T["hdr_bg"])
        hdr.pack(side="top", fill="x")
        tk.Label(hdr, text="  ⚠  Attribute beim Erstellen des Datenpakets",
                 font=("Segoe UI", 11, "bold"),
                 bg=T["hdr_bg"], fg=T["hdr_fg"]).pack(side="left", pady=10)

        body = tk.Frame(self, bg=T["root"])
        body.pack(fill="both", expand=True, padx=16, pady=(12, 4))

        tk.Label(body,
                 text="Für das Erstellen eines PUNKTWOLKE-Datenpakets im GDWH-Catalog "
                      "bitte nur folgende Attribute ausfüllen / wählen, Rest leer lassen:",
                 font=("Segoe UI", 9),
                 bg=T["root"], fg=T["fg"], anchor="w", justify="left",
                 wraplength=520).pack(fill="x", pady=(0, 8))

        for label in ("Name", "LayerReleaseKey", "ReleaseModelKey"):
            tk.Label(body, text=f"✓  {label}", font=("Segoe UI", 9, "bold"),
                     bg=T["root"], fg=T["accent"], anchor="w",
                     justify="left").pack(fill="x", pady=1)

        tk.Frame(body, height=1, bg=T["sep"]).pack(fill="x", pady=(10, 8))

        tk.Label(body, text="Felder müssen leer bleiben:",
                 font=("Segoe UI", 9),
                 bg=T["root"], fg=T["fg"], anchor="w", justify="left").pack(fill="x")

        for label in ("ReleaseKey", "FullExportFileNameKey"):
            tk.Label(body, text=f"✗  {label}", font=("Segoe UI", 9, "bold"),
                     bg=T["root"], fg=T["fg_dim"], anchor="w",
                     justify="left").pack(fill="x", pady=1)

        tk.Frame(self, height=1, bg=T["sep"]).pack(side="bottom", fill="x")
        btn_row = tk.Frame(self, bg=T["root"])
        btn_row.pack(side="bottom", fill="x", padx=14, pady=10)

        tk.Button(btn_row, text=f"Weiter zum GDWH-{env_label} Portal",
                  font=("Segoe UI", 10, "bold"),
                  bg="#005fa3" if dark else "#0063b1", fg="#ffffff",
                  activebackground=T["sel_bg"], activeforeground="#ffffff",
                  relief="flat", padx=14, pady=7, cursor="hand2",
                  command=self._on_continue).pack(side="right")

        self.update_idletasks()
        w = 520
        h = self.winfo_reqheight() + 20
        px = parent.winfo_rootx() + parent.winfo_width() // 2 - w // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2 - h // 2
        self.geometry(f"{w}x{h}+{px}+{py}")

        self._set_titlebar_dark(dark)
        self.bind("<Escape>", lambda _: self._on_cancel())
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _on_cancel(self):
        self.destroy()

    def _on_continue(self):
        try:
            webbrowser.open(self._url)
        except Exception:
            pass
        self.destroy()

    def _set_titlebar_dark(self, dark):
        if not self.winfo_ismapped():
            self.after(50, lambda: self._set_titlebar_dark(dark))
            return
        try:
            hwnd  = int(self.wm_frame(), 16)
            value = ctypes.c_int(1 if dark else 0)
            for attr in (20, 19):   # 20 = Win11 / Win10 2004+; 19 = ältere Builds
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
        except Exception:
            pass


class NoDataPreviewWindow(tk.Toplevel):
    """Popup-Viewer für die "check input-NoData"-Tiff-Vorschau (PPM-Datei).

    Rein visuelle Kontrolle der Rand-NoData-Pixel (schwarz/weiss), nicht
    georeferenziert. Zoom (Buttons, Stufen) und Pan (Ziehen / Pfeil-Buttons)
    über die Standard-Fähigkeiten von tk.Canvas / tk.PhotoImage - bewusst
    ohne Pillow, damit keine zusätzliche Abhängigkeit im restriktiven
    Firmenumfeld nötig ist. Kein Mausrad-Zoom (macht grosse Vorschaubilder
    spürbar langsam) - stattdessen feste Ecken-Zoom- und Pan-Buttons.
    """

    _ZOOM_STEPS = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0]

    # Ecken-Zoom-Massstab ca. 1:1.8 (etwas weiter herausgezoomt als 1:1),
    # als Bruch für PhotoImage.subsample()/.zoom() (nur Ganzzahlen möglich):
    # 5/9 = 0.5555… ≈ 1/1.8
    _CORNER_SUBSAMPLE = 9
    _CORNER_ZOOM      = 5

    def __init__(self, master, ppm_path, filename=""):
        super().__init__(master)
        self.title(f"check input-NoData – {filename}")
        self.geometry("900x700")

        self._base_img = tk.PhotoImage(file=ppm_path)
        try:
            os.remove(ppm_path)  # temporäre Vorschaudatei nicht mehr benötigt
        except Exception:
            pass

        self._zoom_idx = self._ZOOM_STEPS.index(1.0)
        self._display_img = None

        toolbar = ttk.Frame(self, padding=4)
        toolbar.pack(fill="x")

        # Pan-Pfeile: fixe Sprünge (wie Bild-Auf/-Ab) in den jeweils
        # naechsten, noch nicht sichtbaren Bereich.
        ttk.Button(toolbar, text="◄", width=3,
                   command=lambda: self.canvas.xview_scroll(-1, "pages")).pack(side="left")
        ttk.Button(toolbar, text="▲", width=3,
                   command=lambda: self.canvas.yview_scroll(-1, "pages")).pack(side="left", padx=(2, 0))
        ttk.Button(toolbar, text="▼", width=3,
                   command=lambda: self.canvas.yview_scroll(1, "pages")).pack(side="left", padx=(2, 0))
        ttk.Button(toolbar, text="►", width=3,
                   command=lambda: self.canvas.xview_scroll(1, "pages")).pack(side="left", padx=(2, 0))

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)

        ttk.Button(toolbar, text="–", width=3, command=lambda: self._zoom(-1)).pack(side="left")
        ttk.Button(toolbar, text="+", width=3, command=lambda: self._zoom(1)).pack(side="left", padx=(4, 0))
        ttk.Button(toolbar, text="100%", command=self._zoom_reset).pack(side="left", padx=(8, 0))

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)

        # Ecken-Zoom: springt direkt an eine Ecke des Tiffs (Massstab 1:1.8),
        # zur schnellen Kontrolle der Rand-NoData-Pixel ohne manuelles Pannen.
        ttk.Button(toolbar, text="↖", width=3,
                   command=lambda: self._zoom_to_corner(0.0, 0.0)).pack(side="left")
        ttk.Button(toolbar, text="↗", width=3,
                   command=lambda: self._zoom_to_corner(1.0, 0.0)).pack(side="left", padx=(2, 0))
        ttk.Button(toolbar, text="↙", width=3,
                   command=lambda: self._zoom_to_corner(0.0, 1.0)).pack(side="left", padx=(2, 0))
        ttk.Button(toolbar, text="↘", width=3,
                   command=lambda: self._zoom_to_corner(1.0, 1.0)).pack(side="left", padx=(2, 0))

        ttk.Label(toolbar, text="Ziehen (linke Maustaste) = Pan",
                  font=("", 8)).pack(side="left", padx=(12, 0))

        self.canvas = tk.Canvas(self, background="#303030", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B1-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))

        self._render()

    def _scaled_image(self):
        factor = self._ZOOM_STEPS[self._zoom_idx]
        if factor == 1.0:
            return self._base_img
        if factor > 1.0:
            return self._base_img.zoom(int(factor))
        return self._base_img.subsample(int(round(1 / factor)))

    def _render(self):
        img = self._scaled_image()
        self._display_img = img  # Referenz halten, sonst Garbage Collection durch Tk
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=img)
        self.canvas.config(scrollregion=(0, 0, img.width(), img.height()))

    def _zoom(self, direction):
        new_idx = self._zoom_idx + direction
        if 0 <= new_idx < len(self._ZOOM_STEPS):
            self._zoom_idx = new_idx
            self._render()

    def _zoom_reset(self):
        self._zoom_idx = self._ZOOM_STEPS.index(1.0)
        self._render()

    def _zoom_to_corner(self, x_frac, y_frac):
        """Springt an eine Ecke des Tiffs, Massstab ca. 1:1.8 (x_frac/y_frac:
        0.0 = linker/oberer Rand, 1.0 = rechter/unterer Rand)."""
        img = self._base_img.subsample(self._CORNER_SUBSAMPLE).zoom(self._CORNER_ZOOM)
        self._display_img = img
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=img)
        self.canvas.config(scrollregion=(0, 0, img.width(), img.height()))
        self.canvas.xview_moveto(x_frac)
        self.canvas.yview_moveto(y_frac)


# ─── Haupt-App ────────────────────────────────────────────────────────────────
class GDWHApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("GDWH Import")
        screen_h = self.winfo_screenheight()
        win_h = min(1060, screen_h - 80)
        self.geometry(f"860x{win_h}")
        self.minsize(720, min(860, win_h))
        self.resizable(True, True)

        self._running         = False
        self._dark            = False
        self._log_q           = queue.Queue()
        self._log_file        = None
        self._pending_archive = None
        self._pending_ziel     = None
        self._error_lines     = []
        self._log_visible     = False
        self.gds_var        = tk.StringVar(value="SB_DOP")
        self._dim_labels    = []   # Labels mit fg_dim (grau)
        self._accent_labels = []   # Labels mit accent (blau)
        self._hint_labels   = []   # Labels mit hint (amber) für Info-Hinweise
        self._check_format_btns = []   # "Check - NameFormat"-Buttons (amber/grün/rot)
        self._form_locked   = False    # SB_DOP_16 + DMC-4, siehe _set_form_locked
        self._locked_states = {}
        self._osgeo_python  = _detect_osgeo_python()
        self._osgeo_lbl     = None
        self._osgeo_status  = None
        self._staging_dir     = _detect_staging_dir()
        self._staging_lbl     = None
        self._staging_status  = None

        self._build_ui()
        self._on_gds_change()
        self._apply_theme(True)    # Dark Mode als Standard
        self.after(120, self._poll_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI Aufbau ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Header (tk.Frame für direkte Farb-Kontrolle)
        self._hdr = tk.Frame(self, height=52)
        self._hdr.pack(fill="x")
        self._hdr.pack_propagate(False)

        self._hdr_lbl = tk.Label(self._hdr, text="GDWH Import",
                                   font=("Segoe UI", 16, "bold"))
        self._hdr_lbl.pack(side="left", padx=16, pady=12)

        self._theme_btn = tk.Button(self._hdr, text="Dark",
                                     command=self._toggle_theme,
                                     relief="flat", borderwidth=0,
                                     font=("", 9), cursor="hand2", padx=10, pady=4)
        self._theme_btn.pack(side="right", padx=12)

        # GDS-Auswahl
        gds_frame = ttk.LabelFrame(self, text="GDS auswählen", padding=8, style="Section.TLabelframe")
        gds_frame.pack(fill="x", padx=12, pady=(8, 0))
        for col, (gds, desc) in enumerate(GDS_ITEMS):
            ttk.Radiobutton(gds_frame, text=f"{gds}\n{desc}",
                             variable=self.gds_var, value=gds,
                             command=self._on_gds_change
                             ).grid(row=0, column=col, padx=10, pady=4, sticky="nw")

        # Datenpacket in GDWH erstellen – öffnet den GDWH-Catalog-Import-Link
        # (PROD/INT) des aktuell gewählten GDS im Standardbrowser.
        gdwh_frame = ttk.LabelFrame(self, text="Datenpacket in GDWH erstellen", padding=8, style="Section.TLabelframe")
        gdwh_frame.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Button(gdwh_frame, text="GDWH-PROD",
                    command=lambda: self._open_catalog_portal(CATALOG_HOST_PROD)
                    ).pack(side="left", padx=(0, 8))
        ttk.Button(gdwh_frame, text="GDWH-INT",
                    command=lambda: self._open_catalog_portal(CATALOG_HOST_INT)
                    ).pack(side="left")

        # OSGeo4W Python Zeile
        self._osgeo_frame = ttk.Frame(self)
        self._osgeo_frame.pack(fill="x", padx=12, pady=(6, 0))
        osgeo_lbl_static = ttk.Label(self._osgeo_frame, text="OSGeo4W Python:", font=("", 9))
        osgeo_lbl_static.pack(side="left")
        self._dim_labels.append(osgeo_lbl_static)

        self._osgeo_lbl = ttk.Label(self._osgeo_frame, font=("Courier New", 8),
                                     text=self._osgeo_python or "(nicht gefunden)")
        self._osgeo_lbl.pack(side="left", padx=(6, 0))

        self._osgeo_status = ttk.Label(self._osgeo_frame, font=("", 8, "bold"))
        self._osgeo_status.pack(side="left", padx=(6, 0))

        ttk.Button(self._osgeo_frame, text="Ändern…",
                    command=self._set_osgeo_python).pack(side="right")
        self._update_osgeo_label()

        # Lokaler Staging-Ordner Zeile (Performance, siehe _osgeo_runner.py:
        # Quelle/bestehendes Ziel werden dorthin gespiegelt, verarbeitet, dann
        # zurueckkopiert - schnelleres Laufwerk als das Eingangs-/GDWH-
        # Netzlaufwerk, z.B. Y:\ auf der VDI)
        self._staging_frame = ttk.Frame(self)
        self._staging_frame.pack(fill="x", padx=12, pady=(2, 0))
        staging_lbl_static = ttk.Label(self._staging_frame, text="Lokaler Temp-Ordner (Staging):", font=("", 9))
        staging_lbl_static.pack(side="left")
        self._dim_labels.append(staging_lbl_static)

        self._staging_lbl = ttk.Label(self._staging_frame, font=("Courier New", 8),
                                       text=self._staging_dir or "(kein Staging – direkt übers Netzlaufwerk)")
        self._staging_lbl.pack(side="left", padx=(6, 0))

        self._staging_status = ttk.Label(self._staging_frame, font=("", 8, "bold"))
        self._staging_status.pack(side="left", padx=(6, 0))

        ttk.Button(self._staging_frame, text="Ändern…",
                    command=self._set_staging_dir).pack(side="right")
        self._update_staging_label()

        # Scrollbarer Formular-Bereich
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True, padx=12, pady=6)

        self._canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._sf   = ttk.Frame(self._canvas)
        win_id     = self._canvas.create_window((0, 0), window=self._sf, anchor="nw")

        self._sf.bind("<Configure>",
                       lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>",
                          lambda e: self._canvas.itemconfig(win_id, width=e.width))
        self._canvas.bind_all("<MouseWheel>",
                              lambda e: self._canvas.yview_scroll(-1*(e.delta//120), "units"))
        # Mausrad soll Combobox-Auswahl nicht versehentlich verstellen
        self.bind_class("TCombobox", "<MouseWheel>", self._fwd_wheel_to_canvas)

        self._build_paths(self._sf)
        self._build_meta(self._sf)

        # Log (initial versteckt – per Terminal-Button einblendbar)
        self._log_sep = ttk.Separator(self)
        self._log_frame = ttk.LabelFrame(self, text="Log-Ausgabe", padding=4, style="Section.TLabelframe")
        self.log_box = scrolledtext.ScrolledText(
            self._log_frame, height=11, wrap="word", state="disabled",
            font=("Courier New", 9))
        self.log_box.pack(fill="both", expand=True)

        # Fortschrittsbalken (anfangs unsichtbar – wird bei Import-Start eingeblendet)
        self._progress_frame = ttk.Frame(self)
        self._progress_bar = ttk.Progressbar(self._progress_frame, mode="indeterminate")
        self._progress_bar.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._progress_lbl = ttk.Label(self._progress_frame,
                                        text="Verarbeitung läuft…", font=("", 9))
        self._progress_lbl.pack(side="left")

        # Buttons
        self._btn_row = ttk.Frame(self)
        self._btn_row.pack(fill="x", padx=12, pady=(0, 10))
        # tk.Button (statt ttk.Button) für direkte Textfarben-Kontrolle:
        # orange solange nicht alle Pflichtfelder ausgefüllt sind, grün sobald
        # startbereit (siehe _update_start_btn_state) – analog zu den
        # "Check - NameFormat"-Buttons.
        self.start_btn = tk.Button(self._btn_row, text="▶   IMPORT STARTEN",
                                    font=("Segoe UI", 10, "bold"),
                                    relief="flat", cursor="hand2",
                                    padx=22, pady=7,
                                    command=self._start_import)
        self.start_btn.pack(side="right")
        ttk.Button(self._btn_row, text="Log löschen",
                    command=self._clear_log).pack(side="right", padx=(0, 10))
        self._terminal_btn = ttk.Button(self._btn_row, text="Terminal ▾",
                                         command=self._toggle_log)
        self._terminal_btn.pack(side="left")

    def _build_meta(self, parent):
        sec = ttk.LabelFrame(parent, text="Meta-Informationen", padding=10, style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)
        self._meta_sec = sec
        r = 0

        # Auftragstyp
        ttk.Label(sec, text="Auftragstyp:", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="w", pady=3)
        self.auftragstyp_var = tk.StringVar(value=AUFTRAGSTYPEN[0])
        ttk.Combobox(sec, textvariable=self.auftragstyp_var, values=AUFTRAGSTYPEN,
                      state="readonly", width=10
                      ).grid(row=r, column=1, sticky="w", padx=(8, 0), pady=3)
        r += 1

        # Area – aus erster Datei im Quellordner abgeleitet, editierbar (falls
        # Dateinamen-Format nicht passt und der Wert falsch abgeleitet wurde).
        ttk.Label(sec, text="Area:", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="w", pady=3)
        self.area_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self.area_var
                   ).grid(row=r, column=1, sticky="ew", padx=(8, 0), pady=3)
        area_hint = ttk.Label(sec,
            text="aus erster Datei im Quellordner abgeleitet – bei Bedarf korrigierbar",
            font=("", 8))
        area_hint.grid(row=r+1, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(area_hint)
        r += 2

        # CameraSystem – bewusst direkt nach Area/vor TileKey/NoData:
        # bei Leica DMC-4 ist NoData (SB_DOP) fix '0 0 0 0' und die "fixing false
        # NoData pixels"-Option (siehe unten) entfaellt; ausserdem haengen
        # TerrainModel, SourceRefSys, CustomAttribute und das LineID-Format davon ab.
        ttk.Label(sec, text="CameraSystem:", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="w", pady=3)
        self.camera_var = tk.StringVar(value=CAMERA_SYSTEMS[0])
        self.camera_cb = ttk.Combobox(sec, textvariable=self.camera_var, values=CAMERA_SYSTEMS,
                                       state="readonly", width=20)
        self.camera_cb.grid(row=r, column=1, sticky="w", padx=(8, 0), pady=3)
        self.camera_var.trace_add("write", lambda *_: self._on_camera_change())
        self.camera_block_lbl = ttk.Label(sec, font=("", 9, "bold"),
            text="SB_DOP_16 gibt es mit Leica DMC-4 nicht – Import gesperrt "
                 "(GDS oder CameraSystem wechseln)")
        self.camera_block_lbl.grid(row=r+1, column=1, sticky="w", padx=(8, 0))
        self.camera_block_lbl.grid_remove()
        self._hint_labels.append(self.camera_block_lbl)
        r += 2

        # TileKey – reine Diagnose-Vorschau (Beispiel aus der ersten Datei),
        # nicht editierbar: TileKey wird pro Datei einzeln berechnet.
        ttk.Label(sec, text="TileKey (Beispiel):", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="w", pady=3)
        self.tilekey_preview_var = tk.StringVar()
        self.tilekey_preview_lbl = ttk.Label(sec, textvariable=self.tilekey_preview_var, font=("", 9))
        self.tilekey_preview_lbl.grid(row=r, column=1, sticky="w", padx=(8, 0), pady=3)
        self.tilekey_hint = ttk.Label(sec, font=("", 8))
        self.tilekey_hint.grid(row=r+1, column=1, sticky="w", padx=(8, 0))
        r += 2

        # NoData
        self.nodata_lbl  = ttk.Label(sec, text="Input NoData:", font=("Segoe UI", 9, "bold"))
        self.nodata_lbl.grid(row=r, column=0, sticky="w", pady=3)
        self.nodata_var  = tk.StringVar()
        self.nodata_cb   = ttk.Combobox(sec, textvariable=self.nodata_var,
                                         state="readonly", width=55)
        self.nodata_cb.grid(row=r, column=1, sticky="w", padx=(8, 0), pady=3)
        self.nodata_auto = ttk.Label(sec, font=("", 8), justify="left")
        self.nodata_auto.grid(row=r+1, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(self.nodata_auto)

        # check input-NoData – öffnet das erste Tiff im Quellordner als
        # herunterskalierte Vorschau (Popup, Zoom/Pan), damit die Rand-
        # NoData-Pixel der Quelldaten visuell auf schwarz/weiss geprüft werden
        # können. Nur für SB_DOP / SB_DOP_16 relevant (bei SB_DSM* sind die
        # Meta-Daten fix, siehe _on_gds_change).
        self.check_nodata_btn = ttk.Button(
            sec, text="check input-NoData",
            command=self._open_nodata_check_viewer)
        self.check_nodata_btn.grid(row=r, column=2, sticky="w", padx=(16, 0), pady=3)

        # Falsche NoData-Pixel vorkorrigieren – nur relevant für GDS "SB_DOP":
        # korrigiert einzelne/kleine 0,0,0- bzw. 255,255,255-Pixelgruppen
        # INNERHALB der Nutzdaten (radiometrischer Zufallstreffer), bevor die
        # eigentliche Maskenberechnung (Script 1) startet. Echte grosse
        # NoData-Flächen bleiben unverändert. NoData-Zielwert wird aus der
        # Auswahl oben (self.nodata_var) übernommen.
        self.fix_nodata_var = tk.BooleanVar(value=False)
        self.fix_nodata_cb = ttk.Checkbutton(
            sec, variable=self.fix_nodata_var,
            text="ADS: fixing false NoData pixels in DATA",)
        self.fix_nodata_cb.grid(row=r + 1, column=2, sticky="w", padx=(16, 0), pady=3)
        r += 2

        # TerrainModel
        ttk.Label(sec, text="TerrainModel:", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="w", pady=3)
        self.terrain_var = tk.StringVar(value=TERRAIN_MODELS[0])
        self.terrain_cb = ttk.Combobox(sec, textvariable=self.terrain_var, values=TERRAIN_MODELS,
                                        state="readonly", width=68)
        self.terrain_cb.grid(row=r, column=1, sticky="ew", padx=(8, 0), pady=3)
        self.terrain_hint = ttk.Label(sec, font=("", 8),
            text="DMC-4: TerrainModel immer Digital Surface Model")
        self.terrain_hint.grid(row=r+1, column=1, sticky="w", padx=(8, 0))
        self.terrain_hint.grid_remove()
        self._dim_labels.append(self.terrain_hint)
        r += 2

        # CRS-Tagging LAZ (SB_DSM_PUNKTWOLKE) gibt es hier nicht mehr als
        # Option - die LAS 1.2 -> LAS 1.4 Vorkonversion (Script 4) laeuft
        # jetzt immer automatisch vor Script 1 (siehe _osgeo_runner.py) und
        # setzt das CRS dabei byte-exakt, kein Checkbox-Entscheid mehr noetig.

        # SourceReferenceSystem (nicht editierbar, abhaengig von GDS/CameraSystem,
        # siehe _source_ref_sys)
        ttk.Label(sec, text="SourceRefSys:", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="w", pady=3)
        srs_row = ttk.Frame(sec)
        srs_row.grid(row=r, column=1, sticky="w", padx=(8, 0), pady=3)
        srs_val = ttk.Label(srs_row, text=SOURCE_REF_SYS, font=("", 9, "bold"))
        srs_val.pack(side="left")
        self._srs_val_lbl = srs_val
        srs_fix = ttk.Label(srs_row, text="  [Standard]", font=("", 8))
        srs_fix.pack(side="left")
        self._accent_labels.append(srs_val)
        self._dim_labels.append(srs_fix)
        r += 1

        # CustomAttribute – automatisch per GDS, kein Dropdown
        ttk.Label(sec, text="CustomAttribute:", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="nw", pady=3)
        ca_row = ttk.Frame(sec)
        ca_row.grid(row=r, column=1, sticky="ew", padx=(8, 0), pady=3)
        self.custom_var  = tk.StringVar()
        self._custom_lbl = ttk.Label(ca_row, textvariable=self.custom_var,
                                      font=("", 9, "italic"), wraplength=560, justify="left")
        self._custom_lbl.pack(side="left", anchor="w")
        ca_fix = ttk.Label(ca_row, text="", font=("", 8))
        ca_fix.pack(side="left")
        self._accent_labels.append(self._custom_lbl)
        self._dim_labels.append(ca_fix)
        r += 1

        # Line_ID – Listbox für mehrere IDs (SB_DOP / SB_DSM / SB_DSM_PUNKTWOLKE)
        self.lineid_w = LineIDWidget(sec, "Line_ID",
            comment="alle LineIDs des AOI – werden chronologisch sortiert, die oberste ist die erste beflogene Linie",
            camera_getter=self.camera_var.get)
        self.lineid_w.grid(row=r, column=0, columnspan=2, sticky="ew", pady=4)

        # Line_ID – einfaches Eingabefeld für SB_DOP_16 (genau 1 ID)
        self.lineid_single_lf = ttk.LabelFrame(sec, text="Line_ID", padding=6)
        self.lineid_single_lf.grid(row=r, column=0, columnspan=2, sticky="ew", pady=4)
        _single_comment = ttk.Label(self.lineid_single_lf,
            text="Line_ID des DOP NRGB 16BIT-Flugstreifens – nur eine Line_ID möglich",
            font=("Segoe UI", 8, "italic"))
        _single_comment.pack(fill="x", pady=(0, 3))
        self._dim_labels.append(_single_comment)
        _ef = ttk.Frame(self.lineid_single_lf)
        _ef.pack(fill="x")
        self.lineid_single_var = tk.StringVar()
        ttk.Entry(_ef, textvariable=self.lineid_single_var, width=30).pack(side="left")
        _fmt_lbl = ttk.Label(_ef, text="  Format: YYYYMMDD_HHMM_QQQQQ", font=("", 8))
        _fmt_lbl.pack(side="left", padx=(6, 0))
        self._dim_labels.append(_fmt_lbl)
        self.lineid_single_lf.grid_remove()  # standardmässig versteckt
        r += 1

        # allAreaLineIDs (nur SB_DOP_16) – direkt nach Line_ID
        self.all_area_w = LineIDWidget(sec, "allAreaLineIDs",
            comment="alle Fluglinien Line_IDs des AOIs – mindestens eine Line_ID nötig")
        self.all_area_w.grid(row=r, column=0, columnspan=2, sticky="ew", pady=4)

    def _build_paths(self, parent):
        sec = ttk.LabelFrame(parent, text="Pfade", padding=10, style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)
        self._paths_sec = sec
        r = 0

        # Ziel (GDWH-BUCKET Path) – zuerst, damit das Zielpackage vor dem
        # Data-Input Path gewählt wird
        ttk.Label(sec, text="GDWH-BUCKET Path,\n(GDWH-Datapackage):", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="w", pady=3)
        self.ziel_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self.ziel_var
                   ).grid(row=r, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(sec, text="Ordner…",
                    command=lambda: self._browse(self.ziel_var, must_exist=False)
                    ).grid(row=r, column=2, pady=3)
        ziel_hint = ttk.Label(sec,
            text="Enthält GDS-Ordner vorletzter Ebene  (z.B. …\\SB_DSM\\2025_AREA_DSM)",
            font=("", 8))
        ziel_hint.grid(row=r+1, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(ziel_hint)
        r += 1

        # INPUT_FOLDER (nur SB_DOP_16) – Data-Input Path wird automatisch abgeleitet
        self.if_frame = ttk.Frame(sec)
        self.if_frame.grid(row=r, column=0, columnspan=3, sticky="ew")
        self.if_frame.columnconfigure(1, weight=1)
        ttk.Label(self.if_frame, text="INPUT_FOLDER\n(DOP_NRGB_16BITS\nHauptordner):",
                   justify="left", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w", pady=3)
        self.if_var = tk.StringVar()
        ttk.Entry(self.if_frame, textvariable=self.if_var
                   ).grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(self.if_frame, text="Ordner…",
                    command=lambda: self._browse(self.if_var)
                    ).grid(row=0, column=2, pady=3)
        self.if_checkbtn = tk.Button(self.if_frame, text="Check - NameFormat",
                    relief="flat", cursor="hand2",
                    command=lambda: self._check_name_format(self.if_var, self.if_checkbtn))
        self.if_checkbtn.grid(row=0, column=3, padx=(4, 0), pady=3)
        self._check_format_btns.append(self.if_checkbtn)
        self.if_var.trace_add("write", lambda *_: self._reset_check_btn(self.if_checkbtn))
        self.if_var.trace_add("write", lambda *_: self._refresh_area_tilekey_preview())
        self.if_hint = ttk.Label(self.if_frame, font=("", 8),
            text="Data-Input Path wird automatisch ergänzt:  INPUT_FOLDER\\<HHMM der Line_ID>")
        self.if_hint.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(self.if_hint)

        # Data-Input Path (für alle GDS ausser SB_DOP_16)
        self.quelle_frame = ttk.Frame(sec)
        self.quelle_frame.grid(row=r, column=0, columnspan=3, sticky="ew")
        self.quelle_frame.columnconfigure(1, weight=1)
        ttk.Label(self.quelle_frame, text="Data-Input Path:", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w", pady=3)
        self.quelle_var = tk.StringVar()
        ttk.Entry(self.quelle_frame, textvariable=self.quelle_var
                   ).grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(self.quelle_frame, text="Ordner…",
                    command=lambda: self._browse(self.quelle_var)
                    ).grid(row=0, column=2, pady=3)
        self.quelle_checkbtn = tk.Button(self.quelle_frame, text="Check - NameFormat",
                    relief="flat", cursor="hand2",
                    command=lambda: self._check_name_format(self.quelle_var, self.quelle_checkbtn))
        self.quelle_checkbtn.grid(row=0, column=3, padx=(4, 0), pady=3)
        self._check_format_btns.append(self.quelle_checkbtn)
        self.quelle_var.trace_add("write", lambda *_: self._reset_check_btn(self.quelle_checkbtn))
        self.quelle_var.trace_add("write", lambda *_: self._refresh_area_tilekey_preview())
        self.quelle_hint = ttk.Label(self.quelle_frame, font=("", 8))
        self.quelle_hint.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(self.quelle_hint)

    def _fwd_wheel_to_canvas(self, event):
        """Mausrad über Combobox scrollt den Canvas, ändert Auswahl nicht."""
        self._canvas.yview_scroll(-1 * (event.delta // 120), "units")
        return "break"

    # ── Windows Titelleiste Dark Mode ─────────────────────────────────────────
    def _set_titlebar_dark(self, dark):
        # Fenster muss sichtbar sein bevor DWM reagiert – sonst erneut versuchen
        if not self.winfo_ismapped():
            self.after(50, lambda: self._set_titlebar_dark(dark))
            return
        try:
            # wm_frame() liefert den echten Top-Level-HWND mit Titelleiste
            hwnd  = int(self.wm_frame(), 16)
            value = ctypes.c_int(1 if dark else 0)
            for attr in (20, 19):   # 20 = Windows 11 / Win10 2004+; 19 = ältere Builds
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
            # Frame-Neuzeichnung erzwingen
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
        except Exception:
            pass

    # ── Theme ─────────────────────────────────────────────────────────────────
    def _toggle_theme(self):
        self._apply_theme(not self._dark)

    def _apply_theme(self, dark):
        self._dark = dark
        T = DARK if dark else LIGHT

        # ttk Style (clam-Theme – unterstützt vollständige Farbanpassung)
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".",
            background=T["panel"], foreground=T["fg"],
            fieldbackground=T["input"],
            selectbackground=T["sel_bg"], selectforeground=T["sel_fg"],
            bordercolor=T["sep"], lightcolor=T["panel"], darkcolor=T["sep"],
            insertcolor=T["fg"], troughcolor=T["root"],
        )
        s.configure("TFrame",      background=T["panel"])
        s.configure("TLabelframe", background=T["panel"], bordercolor=T["sep"])
        s.configure("TLabelframe.Label", background=T["panel"], foreground=T["fg"],
                    font=("Segoe UI", 9, "bold"))
        s.configure("Section.TLabelframe", background=T["panel"], bordercolor=T["sep"])
        s.configure("Section.TLabelframe.Label", background=T["panel"], foreground=T["accent"],
                    font=("Segoe UI", 10, "bold"))
        s.configure("TLabel",      background=T["panel"], foreground=T["fg"])
        s.configure("TButton",
            background=T["btn"], foreground=T["fg"],
            bordercolor=T["sep"], relief="flat",
            padding=(8, 4), focuscolor=T["panel"],
        )
        s.map("TButton",
            background=[("active", T["btn_hover"]), ("pressed", T["sep"])],
            foreground=[("active", T["fg"])],
            relief=[("pressed", "flat")],
        )
        s.configure("TRadiobutton",
            background=T["panel"], foreground=T["fg"], focuscolor=T["panel"])
        s.map("TRadiobutton",
            background=[("active", T["panel"])], foreground=[("active", T["fg"])])
        s.configure("TCheckbutton",
            background=T["panel"], foreground=T["fg"], focuscolor=T["panel"])
        s.map("TCheckbutton",
            background=[("active", T["panel"])], foreground=[("active", T["fg"])])
        s.configure("TCombobox",
            fieldbackground=T["input"], background=T["btn"],
            foreground=T["fg"], arrowcolor=T["fg"],
            selectbackground=T["sel_bg"], selectforeground=T["sel_fg"],
            bordercolor=T["sep"], insertcolor=T["fg"],
        )
        s.map("TCombobox",
            fieldbackground=[("readonly", T["input"]), ("disabled", T["panel"])],
            selectbackground=[("readonly", T["input"])],
            selectforeground=[("readonly", T["fg"])],
            foreground=[("readonly", T["fg"]), ("disabled", T["fg_dim"])],
            background=[("active", T["btn_hover"])],
        )
        s.configure("TEntry",
            fieldbackground=T["input"], foreground=T["fg"],
            bordercolor=T["sep"], insertcolor=T["fg"],
            selectbackground=T["sel_bg"], selectforeground=T["sel_fg"],
        )
        s.configure("Vertical.TScrollbar",
            background=T["scroll"], troughcolor=T["root"],
            bordercolor=T["sep"], arrowcolor=T["fg"],
        )
        s.map("Vertical.TScrollbar",
            background=[("active", T["scroll"]), ("pressed", T["scroll"])],
        )
        s.configure("TSeparator", background=T["sep"])
        s.configure("TProgressbar",
            background=T["accent"], troughcolor=T["root"],
            bordercolor=T["sep"],
        )

        # Combobox-Dropdown Farben
        self.option_add("*TCombobox*Listbox.background",       T["list"])
        self.option_add("*TCombobox*Listbox.foreground",       T["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", T["sel_bg"])
        self.option_add("*TCombobox*Listbox.selectForeground", T["sel_fg"])

        # Root + Canvas
        self.configure(bg=T["root"])
        self._canvas.configure(bg=T["panel"], highlightbackground=T["sep"])

        # Header (tk.Frame – direkte Farbkontrolle)
        self._hdr.configure(bg=T["hdr_bg"])
        self._hdr_lbl.configure(bg=T["hdr_bg"], fg=T["hdr_fg"])
        self._theme_btn.configure(
            bg=T["hdr_bg"], fg=T["hdr_fg"],
            activebackground=T["btn"], activeforeground=T["fg"],
            text="Hell" if dark else "Dark",
        )

        # Log-Bereich
        self.log_box.configure(bg=T["log_bg"], fg=T["log_fg"],
                                insertbackground=T["log_fg"])

        # LineID-Widgets (tk.Listbox darin)
        self.lineid_w.apply_theme(T)
        self.all_area_w.apply_theme(T)

        # OSGeo4W Status-Label (ok/err Farbe unabhängig vom Theme-Switch)
        self._update_osgeo_label()
        self._update_staging_label()

        # Spezifisch gefärbte Labels
        for lbl in self._dim_labels:
            try: lbl.configure(foreground=T["fg_dim"])
            except tk.TclError: pass
        for lbl in self._accent_labels:
            try: lbl.configure(foreground=T["accent"])
            except tk.TclError: pass
        for lbl in self._hint_labels:
            try: lbl.configure(foreground=T["hint"])
            except tk.TclError: pass

        # "Check - NameFormat"-Buttons: aktuellen Status (amber/grün/rot) neu einfärben
        for btn in self._check_format_btns:
            try: self._set_check_btn_state(btn, getattr(btn, "_check_state", "idle"))
            except tk.TclError: pass

        # "IMPORT STARTEN"-Button: Textfarbe (amber/grün) neu bewerten
        if hasattr(self, "start_btn"):
            self._update_start_btn_state()

        self._set_titlebar_dark(dark)

    # ── OSGeo4W Python Verwaltung ─────────────────────────────────────────────
    def _update_osgeo_label(self):
        T = DARK if self._dark else LIGHT
        if self._osgeo_python and os.path.isfile(self._osgeo_python):
            self._osgeo_lbl.config(text=self._osgeo_python)
            self._osgeo_status.config(text="✓", foreground=T["ok"])
        else:
            self._osgeo_lbl.config(text=self._osgeo_python or "(nicht gefunden)")
            self._osgeo_status.config(text="✗ nicht gefunden", foreground=T["err"])

    def _set_osgeo_python(self):
        init_dir = os.path.dirname(self._osgeo_python) if self._osgeo_python else r"C:\OSGeo4W\bin"
        if not os.path.isdir(init_dir):
            init_dir = "C:\\"
        path = filedialog.askopenfilename(
            title="OSGeo4W Python auswählen",
            initialdir=init_dir,
            filetypes=[("Python", "python*.exe"), ("Executable", "*.exe"), ("Alle", "*.*")],
        )
        if path:
            path = path.replace("/", "\\")
            self._osgeo_python = path
            _save_osgeo_config(path)
            self._update_osgeo_label()

    # ── Lokaler Staging-Ordner Verwaltung ─────────────────────────────────────
    def _update_staging_label(self):
        T = DARK if self._dark else LIGHT
        if self._staging_dir:
            self._staging_lbl.config(text=self._staging_dir)
            drive = os.path.splitdrive(self._staging_dir)[0] + "\\"
            if os.path.isdir(drive):
                self._staging_status.config(text="✓", foreground=T["ok"])
            else:
                self._staging_status.config(text="✗ Laufwerk nicht verfügbar", foreground=T["err"])
        else:
            self._staging_lbl.config(text="(kein Staging – direkt übers Netzlaufwerk)")
            self._staging_status.config(text="")

    def _set_staging_dir(self):
        init_dir = self._staging_dir
        if not (init_dir and os.path.isdir(os.path.splitdrive(init_dir)[0] + "\\")):
            init_dir = "C:\\"
        path = filedialog.askdirectory(
            title="Lokalen Staging-Ordner wählen (z.B. Y:\\ oder D:\\)",
            initialdir=init_dir,
        )
        if path:
            path = path.replace("/", "\\")
            self._staging_dir = path
            _save_staging_config(path)
            self._update_staging_label()

    def _open_catalog_portal(self, host):
        """Oeffnet den GDWH-Catalog-Import-Link (PROD/INT) des gewaehlten GDS im Standardbrowser.
        Bei SB_DSM_PUNKTWOLKE wird vorher ein Hinweis zu den im Portal zu setzenden
        Attributen angezeigt (Bestaetigung ueber "Weiter zum GDWH-PROD/-INT Portal")."""
        gds = self.gds_var.get()
        url = f"https://{host}/catalog-ng/catalog/{gds}/import"
        if gds == "SB_DSM_PUNKTWOLKE":
            env_label = "PROD" if host == CATALOG_HOST_PROD else "INT"
            PunktwolkeAttributeHinweisDialog(self, url, env_label, dark=self._dark)
            return
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # ── Dynamische Anpassungen ────────────────────────────────────────────────
    def _on_gds_change(self):
        gds    = self.gds_var.get()
        is_d16 = (gds == "SB_DOP_16")

        # Sperre SB_DOP_16 + DMC-4 zuerst loesen, damit die Regeln unten auf den
        # echten Widget-Zustaenden arbeiten (neu bewertet in _update_camera_meta_rules)
        self._set_form_locked(False)

        # CustomAttribute – fix per GDS (+ CameraSystem)
        self.custom_var.set(self._custom_attribute())

        # Line_ID: einfaches Eingabefeld für SB_DOP_16, Listbox für alle anderen
        if is_d16:
            self.lineid_w.grid_remove()
            self.lineid_single_lf.grid()
        else:
            self.lineid_single_lf.grid_remove()
            self.lineid_w.grid()
        self.all_area_w.grid() if is_d16 else self.all_area_w.grid_remove()

        # NoData
        if gds == "SB_DSM":
            self.nodata_lbl.grid_remove()
            self.nodata_cb.grid_remove()
            self.check_nodata_btn.grid_remove()
            self.fix_nodata_cb.grid_remove()
            self.nodata_auto.config(
                text="NoData wird automatisch gesetzt:\n"
                     "  '_hillshade_' im Dateinamen  →  '255 255 255'\n"
                     "  '_DSM_'       im Dateinamen  →  '-3.4028235e+38'")
            self.nodata_auto.grid()
        elif gds == "SB_DSM_PUNKTWOLKE":
            self.nodata_lbl.grid_remove()
            self.nodata_cb.grid_remove()
            self.check_nodata_btn.grid_remove()
            self.fix_nodata_cb.grid_remove()
            self.nodata_auto.config(text="NoData: keine NoData nötig (Punktwolken / LAZ)")
            self.nodata_auto.grid()
        else:
            self.nodata_auto.grid_remove()
            self.nodata_lbl.grid()
            opts, _ = self._nodata_options()
            self.nodata_cb.config(values=opts)
            if self.nodata_var.get() not in opts:
                self.nodata_var.set(opts[0])
            self.nodata_cb.grid()
            # Sichtbarkeit von "check input-NoData": für SB_DOP / SB_DOP_16
            # (hier landet man ohnehin nur im else-Zweig).
            self.check_nodata_btn.grid()
            # Vorkorrektur falsche NoData-Pixel: nur für SB_DOP (Mosaik, 8BIT
            # RGB) sinnvoll, nicht für SB_DOP_16 (Einzellinien, eigene Radiometrie).
            # Zusaetzlich abhaengig von CameraSystem (bei DMC-4 auch NoData fix),
            # siehe _update_camera_nodata_rules().
            self._update_camera_nodata_rules()

        # INPUT_FOLDER (SB_DOP_16) vs. Data-Input Path (andere GDS)
        self.if_frame.grid()         if is_d16 else self.if_frame.grid_remove()
        self.quelle_frame.grid_remove() if is_d16 else self.quelle_frame.grid()
        self.quelle_hint.config(text="Ordner mit TIF- / LAZ-Quelldateien")

        # GDS gewechselt -> alte Check-Ergebnisse sind hinfällig
        if hasattr(self, "if_checkbtn"):
            self._reset_check_btn(self.if_checkbtn)
            self._reset_check_btn(self.quelle_checkbtn)

        # Theme für die neu sichtbaren Widgets aktualisieren
        if hasattr(self, "_dark"):
            T = DARK if self._dark else LIGHT
            for lbl in self._dim_labels:
                try: lbl.configure(foreground=T["fg_dim"])
                except tk.TclError: pass
            for lbl in self._accent_labels:
                try: lbl.configure(foreground=T["accent"])
                except tk.TclError: pass
            for lbl in self._hint_labels:
                try: lbl.configure(foreground=T["hint"])
                except tk.TclError: pass

        self._update_camera_meta_rules()
        self._refresh_area_tilekey_preview()
        self._update_start_btn_state()

    def _on_camera_change(self):
        """CameraSystem gewechselt: NoData-, Meta- und LineID-Regeln neu anwenden."""
        if not hasattr(self, "lineid_w"):
            return
        self._set_form_locked(False)
        self._update_camera_nodata_rules()
        removed = self.lineid_w.refresh_camera()
        self._update_camera_meta_rules()
        if removed:
            messagebox.showwarning(
                "Line_IDs entfernt",
                f"Das LineID-Format hängt vom CameraSystem ab "
                f"(jetzt: {_line_ids.format_hint(self.camera_var.get())}).\n\n"
                f"Nicht passende Line_IDs wurden entfernt ({len(removed)}):\n"
                + "\n".join(f"  {i}" for i in removed[:15])
                + ("\n  …" if len(removed) > 15 else ""),
                parent=self)

    def _is_dop16_dmc(self):
        """SB_DOP_16 gibt es mit Leica DMC-4 nicht (nur ADS-Einzellinien)."""
        return self.gds_var.get() == "SB_DOP_16" and self.camera_var.get() == DMC_CAMERA

    def _custom_attribute(self):
        gds = self.gds_var.get()
        if self.camera_var.get() == DMC_CAMERA and gds in GDS_CUSTOM_ATTR_DMC:
            return GDS_CUSTOM_ATTR_DMC[gds]
        return GDS_CUSTOM_ATTR[gds]

    def _source_ref_sys(self):
        """SB_DSM/SB_DSM_PUNKTWOLKE bleiben immer LV95_LN02 (Wertebereich GDWH)."""
        if self.gds_var.get() == "SB_DOP" and self.camera_var.get() == DMC_CAMERA:
            return SOURCE_REF_SYS_DOP_DMC
        return SOURCE_REF_SYS

    def _update_camera_meta_rules(self):
        """Meta-Regeln, die vom CameraSystem abhaengen (NoData siehe
        _update_camera_nodata_rules). Bei Leica DMC-4:
          - TerrainModel fix 'Digital Surface Model (...)', gesperrt (alle GDS)
          - SB_DOP: SourceRefSys nur EPSG:2056, CustomAttribute RGBN
          - SB_DSM_PUNKTWOLKE: CustomAttribute mit RGB
          - SB_DOP_16: gibt es nicht - Formular gesperrt, IMPORT STARTEN rot
        Wird bei GDS- und CameraSystem-Wechsel aufgerufen."""
        if not hasattr(self, "terrain_cb"):
            return
        if self.camera_var.get() == DMC_CAMERA:
            self.terrain_var.set(DMC_TERRAIN_MODEL)
            self.terrain_cb.config(state="disabled")
            self.terrain_hint.grid()
        else:
            self.terrain_cb.config(state="readonly")
            self.terrain_hint.grid_remove()
        self.custom_var.set(self._custom_attribute())
        self._srs_val_lbl.config(text=self._source_ref_sys())

        blocked = self._is_dop16_dmc()
        if blocked:
            self.camera_block_lbl.grid()
        else:
            self.camera_block_lbl.grid_remove()
        self._set_form_locked(blocked)
        self._update_start_btn_state()

    def _set_form_locked(self, locked):
        """Sperrt alle Eingaben in 'Pfade' und 'Meta-Informationen' ausser dem
        CameraSystem (sonst kaeme man aus der Sperre nicht mehr heraus) und stellt
        beim Entsperren die vorherigen Zustaende wieder her. Die GDS-Auswahl liegt
        ausserhalb dieser Bereiche und bleibt bedienbar."""
        if locked == self._form_locked or not hasattr(self, "_meta_sec"):
            return
        self._form_locked = locked
        if not locked:
            for w, state in self._locked_states.items():
                try: w.configure(state=state)
                except tk.TclError: pass
            self._locked_states = {}
            return
        stack = [self._paths_sec, self._meta_sec]
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            if w is self.camera_cb or not isinstance(
                    w, (ttk.Entry, ttk.Button, ttk.Checkbutton, tk.Button, tk.Listbox)):
                continue
            try:
                self._locked_states[w] = str(w.cget("state")) or "normal"
                w.configure(state="disabled")
            except tk.TclError:
                pass

    def _update_camera_nodata_rules(self):
        """NoData-Regeln, die vom CameraSystem abhaengen:
          - 'fixing false NoData pixels' (nur ADS100-Radiometrie relevant, siehe
            Checkbox-Text): ausgeblendet ausser bei GDS SB_DOP UND CameraSystem
            != DMC-4. Bei DMC-4 wird die Option zusaetzlich aktiv deaktiviert
            (nicht nur versteckt), falls sie zuvor angehakt war.
          - SB_DOP mit DMC-4: NoData immer '0 0 0 0', Dropdown gesperrt. Die
            DMC-Pipeline (Reality Studio -> DMC-Converter) schreibt NoData immer
            schwarz, falsche NoData-Pixel in den Nutzdaten entstehen dort nicht.
        Wird sowohl bei GDS- als auch bei CameraSystem-Wechsel aufgerufen (siehe
        _on_gds_change bzw. camera_var-Trace)."""
        if not hasattr(self, "fix_nodata_cb"):
            return
        gds = self.gds_var.get()
        is_dmc = self.camera_var.get() == DMC_CAMERA
        if gds == "SB_DOP" and not is_dmc:
            self.fix_nodata_cb.grid()
        else:
            self.fix_nodata_cb.grid_remove()
            if is_dmc:
                self.fix_nodata_var.set(False)

        opts, _ = self._nodata_options()
        if gds == "SB_DOP" and is_dmc:
            # Vier Werte, weil DMC-4 seit der RGBN-Umstellung 4-Band liefert. Die
            # Wertzahl muss zur Bandzahl passen, sonst ueberspringen Script 1 den
            # NoData-Tag UND die Flag Mask (beide pruefen Werte gegen Baender).
            self.nodata_cb.config(values=opts)
            self.nodata_var.set(opts[0])
            self.nodata_cb.config(state="disabled")
            self.nodata_auto.config(
                text="DMC-4: NoData immer '0 0 0 0' (schwarz, 4-Band RGBN), keine Vorkorrektur nötig")
            self.nodata_auto.grid()
        else:
            # Zurueck auf die GDS-Auswahl - sonst blieben die DMC-Werte stehen,
            # wenn man das CameraSystem wieder wechselt.
            self.nodata_cb.config(values=opts, state="readonly")
            if self.nodata_var.get() not in opts:
                self.nodata_var.set(opts[0])
            # bei SB_DSM* traegt nodata_auto den Auto-Hinweis (siehe _on_gds_change)
            if gds in ("SB_DOP", "SB_DOP_16"):
                self.nodata_auto.grid_remove()

    # ── Area / TileKey Live-Vorschau ─────────────────────────────────────────
    _TILEKEY_RE = re.compile(r'^\d{4}_\d{4}$')

    def _refresh_area_tilekey_preview(self):
        """Aktualisiert die Area-/TileKey-Vorschaufelder anhand der ersten
        passenden Datei im aktuellen Quellordner. Wird bei GDS-Wechsel und bei
        Aenderung des Quellordner-Pfads aufgerufen (siehe _build_paths)."""
        if not hasattr(self, "area_var"):
            return  # Widgets noch nicht aufgebaut

        gds = self.gds_var.get()
        src = (self.if_var.get() if gds == "SB_DOP_16" else self.quelle_var.get()).strip().strip('"')

        T = DARK if self._dark else LIGHT
        if not os.path.isdir(src):
            # Noch kein (gueltiger) Quellordner gesetzt - Vorschau leer lassen,
            # statt verwirrend schon beim Oeffnen einen roten Fehler zu zeigen.
            self.area_var.set("")
            self.tilekey_preview_var.set("")
            self.tilekey_preview_lbl.configure(foreground=T["fg"])
            self.tilekey_hint.config(text="")
            return

        self.area_var.set(self._extract_area_from_source(src, gds))

        tilekey, _fn = self._extract_tilekey_from_source(src, gds)
        self.tilekey_preview_var.set(tilekey)

        if gds == "SB_DSM" or self._TILEKEY_RE.match(tilekey):
            self.tilekey_preview_lbl.configure(foreground=T["fg"])
            self.tilekey_hint.config(text="")
        else:
            self.tilekey_preview_lbl.configure(foreground=T["err"])
            self.tilekey_hint.config(text="TileKey falsch – Dateinamen-Format prüfen und ändern",
                                      foreground=T["err"])

    # ── Check - NameFormat ───────────────────────────────────────────────────
    def _set_check_btn_state(self, btn, state):
        """state: 'idle' (amber), 'ok' (grün) oder 'err' (rot)."""
        btn._check_state = state
        T = DARK if self._dark else LIGHT
        color = {"idle": T["hint"], "ok": T["ok"], "err": T["err"]}[state]
        btn.configure(fg=color, activeforeground=color,
                       bg=T["btn"], activebackground=T["btn_hover"])

    def _reset_check_btn(self, btn):
        self._set_check_btn_state(btn, "idle")

    def _check_name_format(self, path_var, btn):
        """Prüft die ersten (max. 2) passenden Dateien im Quellordner gegen die
        GDS-spezifische Namenskonvention (siehe NAME_FORMAT_SPECS)."""
        gds  = self.gds_var.get()
        spec = NAME_FORMAT_SPECS.get(gds)
        src  = path_var.get().strip().strip('"')

        if not spec:
            messagebox.showerror("Check - NameFormat",
                f"Kein Namensformat für GDS '{gds}' hinterlegt.", parent=self)
            self._set_check_btn_state(btn, "err")
            return
        if not os.path.isdir(src):
            messagebox.showerror("Check - NameFormat",
                f"Quellordner nicht gefunden:\n  {src}", parent=self)
            self._set_check_btn_state(btn, "err")
            return

        exts = spec["extensions"]
        candidates = []
        try:
            for fn in sorted(os.listdir(src)):
                if fn.lower().endswith(exts):
                    candidates.append(fn)
                if len(candidates) >= 2:
                    break
            if not candidates:
                # Eine Ebene tiefer (z.B. SB_DOP_16 vor Reorganisation)
                for sub in sorted(os.listdir(src)):
                    sub_path = os.path.join(src, sub)
                    if os.path.isdir(sub_path):
                        for fn in sorted(os.listdir(sub_path)):
                            if fn.lower().endswith(exts):
                                candidates.append(fn)
                            if len(candidates) >= 2:
                                break
                    if len(candidates) >= 2:
                        break
        except Exception as e:
            messagebox.showerror("Check - NameFormat",
                f"Fehler beim Lesen des Ordners:\n  {e}", parent=self)
            self._set_check_btn_state(btn, "err")
            return

        if not candidates:
            messagebox.showerror("Check - NameFormat",
                f"Keine passende Datei ({', '.join(exts)}) im Quellordner gefunden.",
                parent=self)
            self._set_check_btn_state(btn, "err")
            return

        bad = [fn for fn in candidates if not any(rx.match(fn) for rx in spec["regexes"])]
        if bad:
            messagebox.showerror("Check - NameFormat",
                "Die Namenskonvention ist im falschen Format:\n\n"
                + "\n".join(f"  • {fn}" for fn in bad)
                + f"\n\nErwartetes Format ({gds}):\n  {spec['example']}",
                parent=self)
            self._set_check_btn_state(btn, "err")
            return

        self._set_check_btn_state(btn, "ok")

    # ── check input-NoData (Tiff-Vorschau, Rand-Pixel schwarz/weiss) ─────────────
    @staticmethod
    def _find_first_tif(src_folder):
        """Erste .tif/.tiff-Datei im Quellordner (inkl. 1 Unterebene, analog zu
        _extract_area_from_source). Gibt den vollen Pfad zurück oder None."""
        extensions = ('.tif', '.tiff')
        if not os.path.isdir(src_folder):
            return None
        try:
            for fn in sorted(os.listdir(src_folder)):
                if fn.lower().endswith(extensions):
                    return os.path.join(src_folder, fn)
            for sub in sorted(os.listdir(src_folder)):
                sub_path = os.path.join(src_folder, sub)
                if os.path.isdir(sub_path):
                    for fn in sorted(os.listdir(sub_path)):
                        if fn.lower().endswith(extensions):
                            return os.path.join(sub_path, fn)
        except Exception:
            pass
        return None

    def _open_nodata_check_viewer(self):
        """Öffnet das erste Tiff im aktuellen Quellordner als herunterskalierte
        Vorschau in einem Popup (Zoom/Pan), zur rein visuellen Kontrolle der
        Rand-NoData-Pixel. Die Vorschau wird via OSGeo4W-Python-Subprocess
        erzeugt (Haupt-GUI läuft ohne GDAL-Bindings, siehe _exec_with_osgeo)."""
        gds = self.gds_var.get()
        src = (self.if_var.get() if gds == "SB_DOP_16" else self.quelle_var.get()).strip().strip('"')

        if not os.path.isdir(src):
            messagebox.showerror("check input-NoData",
                f"Quellordner nicht gefunden:\n  {src}", parent=self)
            return
        tif_path = self._find_first_tif(src)
        if not tif_path:
            messagebox.showerror("check input-NoData",
                "Kein Tiff (.tif/.tiff) im Quellordner gefunden.", parent=self)
            return
        if not self._osgeo_python or not os.path.isfile(self._osgeo_python):
            messagebox.showerror("check input-NoData",
                "OSGeo4W Python nicht gefunden.\n"
                "Bitte Pfad via 'Ändern…' festlegen  (z.B. C:\\OSGeo4W\\bin\\python3.exe).",
                parent=self)
            return

        self.check_nodata_btn.config(state="disabled", text="lädt…")
        threading.Thread(
            target=self._generate_nodata_preview,
            args=(tif_path,),
            daemon=True
        ).start()

    def _generate_nodata_preview(self, tif_path):
        """Läuft im Hintergrund-Thread: erzeugt via Subprocess eine PPM-Vorschau
        des Tiffs (dezimiert, 8BIT RGB), die das Haupt-GUI ohne Pillow anzeigen
        kann (tk.PhotoImage kennt PPM nativ)."""
        ppm_path = os.path.join(
            tempfile.gettempdir(), f"gdwh_nodata_preview_{os.getpid()}.ppm")
        env = os.environ.copy()
        env["PYTHONHOME"] = _detect_python_home(self._osgeo_python)
        env["PYTHONNOUSERSITE"] = "1"  # siehe _exec_with_osgeo (ABI-Konflikt-Schutz)

        try:
            proc = subprocess.run(
                [self._osgeo_python, SCRIPT_PREVIEW, tif_path, ppm_path, "1600"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True, encoding="utf-8", errors="replace",
                env=env, timeout=60,
            )
            if proc.returncode != 0 or not os.path.isfile(ppm_path):
                raise RuntimeError(proc.stdout.strip() or "Vorschau-Skript fehlgeschlagen.")
        except Exception as e:
            self.after(0, lambda: self._on_preview_error(e))
            return
        self.after(0, lambda: self._on_preview_ready(ppm_path, os.path.basename(tif_path)))

    def _on_preview_ready(self, ppm_path, filename):
        self.check_nodata_btn.config(state="normal", text="check input-NoData")
        try:
            NoDataPreviewWindow(self, ppm_path, filename)
        except Exception as e:
            messagebox.showerror("check input-NoData",
                f"Vorschau konnte nicht angezeigt werden:\n  {e}", parent=self)

    def _on_preview_error(self, error):
        self.check_nodata_btn.config(state="normal", text="check input-NoData")
        messagebox.showerror("check input-NoData",
            f"Vorschau konnte nicht erzeugt werden:\n  {error}", parent=self)

    # ── Ordner-Dialog ─────────────────────────────────────────────────────────
    def _browse(self, var, must_exist=True):
        init   = var.get()
        init   = init if os.path.isdir(init) else os.path.expanduser("~")
        folder = filedialog.askdirectory(initialdir=init, title="Ordner auswählen")
        if folder:
            var.set(folder.replace("/", "\\"))

    # ── Validierung ───────────────────────────────────────────────────────────
    def _validate(self):
        gds    = self.gds_var.get()
        errors = []

        if self._is_dop16_dmc():
            errors.append("SB_DOP_16 ist mit CameraSystem Leica DMC-4 nicht möglich.")

        if not self._osgeo_python or not os.path.isfile(self._osgeo_python):
            errors.append(
                "OSGeo4W Python nicht gefunden.\n"
                "Bitte Pfad via 'Ändern…' festlegen  (z.B. C:\\OSGeo4W\\bin\\python3.exe)."
            )

        if gds == "SB_DOP_16":
            lid = self.lineid_single_var.get().strip()
            if not lid:
                errors.append("Line_ID ist erforderlich.")
            elif not LINE_ID_PAT.match(lid):
                errors.append(
                    f"Line_ID – ungültiges Format.\n"
                    f"Erwartet: YYYYMMDD_HHMM_QQQQQ\nEingabe: {lid}")
            if not self.all_area_w.get_ids():
                errors.append("Mindestens eine allAreaLineID ist erforderlich.")
            if_path = self.if_var.get().strip().strip('"')
            if not if_path:
                errors.append("INPUT_FOLDER fehlt.")
            elif not os.path.isdir(if_path):
                errors.append(f"INPUT_FOLDER nicht gefunden:\n  {if_path}")
        else:
            if not self.lineid_w.get_ids():
                errors.append("Mindestens eine Line_ID ist erforderlich.")
            else:
                try:
                    _line_ids.normalize(self.lineid_w.get_ids(), self.camera_var.get())
                except ValueError as e:
                    errors.append(f"Line_ID – {e}")
            q = self.quelle_var.get().strip().strip('"')
            if not q:
                errors.append("Quelle fehlt.")
            elif not os.path.isdir(q):
                errors.append(f"Quelle nicht gefunden oder nicht erreichbar:\n  {q}")

        if not self.ziel_var.get().strip():
            errors.append("Ziel fehlt.")

        if gds not in ("SB_DSM", "SB_DSM_PUNKTWOLKE"):
            opts, _ = self._nodata_options()
            if self.nodata_var.get() not in opts:
                errors.append(
                    "Input NoData: keine gültige Auswahl erkannt.\n"
                    "Bitte den NoData-Wert im Dropdown erneut auswählen."
                )

        if errors:
            messagebox.showerror("Eingabe-Fehler",
                                  "\n\n".join(f"• {e}" for e in errors), parent=self)
            return False
        return True

    def _nodata_options(self):
        """Gueltige NoData-Auswahl (Beschriftungen, Werte) fuer die aktuelle
        GDS-/CameraSystem-Kombination. Einzige Quelle fuer das Dropdown
        (_on_gds_change, _update_camera_nodata_rules), die Validierung
        (_validate) und den geschriebenen Wert (_get_nodata) - sonst laufen
        die vier Stellen auseinander."""
        gds = self.gds_var.get()
        if gds == "SB_DOP" and self.camera_var.get() == DMC_CAMERA:
            return NODATA_DMC_OPT, NODATA_DMC_VAL
        if gds == "SB_DOP_16":
            return NODATA_D16_OPT, NODATA_D16_VAL
        return NODATA_DOP_OPT, NODATA_DOP_VAL

    def _get_nodata(self):
        gds  = self.gds_var.get()
        if gds in ("SB_DSM", "SB_DSM_PUNKTWOLKE"):
            return ""
        if gds == "SB_DOP" and self.camera_var.get() == DMC_CAMERA:
            # DMC-4: immer '0 0 0 0' (4-Band RGBN), siehe _update_camera_nodata_rules
            return NODATA_DMC_VAL[0]
        val  = self.nodata_var.get()
        opts, vals = self._nodata_options()
        if val not in opts:
            # _validate() prueft das bereits vor jedem Start (siehe dort) und
            # verhindert, dass wir hier ueberhaupt ankommen - kein stiller
            # Fallback auf "0 0 0" mehr, das haette frueher einen falsch
            # gewaehlten NoData-Wert unbemerkt auf schwarz zurückfallen lassen.
            raise ValueError(f"Ungueltige NoData-Auswahl fuer {gds}: {val!r}")
        idx = opts.index(val)
        return vals[idx]

    def _build_meta_info(self):
        gds  = self.gds_var.get()
        meta = {
            "Auftragstyp":           self.auftragstyp_var.get(),
            "CustomAttribute":       self._custom_attribute(),
            "Line_ID":               ([self.lineid_single_var.get().strip()]
                                      if gds == "SB_DOP_16" else self.lineid_w.get_ids()),
            "TerrainModel":          (DMC_TERRAIN_MODEL if self.camera_var.get() == DMC_CAMERA
                                      else self.terrain_var.get()),
            "SourceReferenceSystem": self._source_ref_sys(),
            "CameraSystem":          self.camera_var.get(),
        }
        if gds == "SB_DOP_16":
            meta["allAreaLineIDs"] = self.all_area_w.get_ids()
        if gds != "SB_DSM_PUNKTWOLKE":
            meta["NoData"] = self._get_nodata()
        if gds == "SB_DOP":
            # DMC-4 nie mit Vorkorrektur, auch falls die Checkbox-Variable noch gesetzt ist
            meta["FixFalseNodata"] = (bool(self.fix_nodata_var.get())
                                      and self.camera_var.get() != DMC_CAMERA)
        # Area-Override nur uebernehmen, wenn das Feld einen echten Wert enthaelt
        # (kein Platzhalter wie "—  (Ordner nicht gefunden)") - sonst leitet
        # jedes Sub-Script den Area-Namen wie bisher selbst pro Datei ab.
        area_override = self.area_var.get().strip()
        if area_override and not area_override.startswith("—"):
            meta["AreaOverride"] = area_override
        return meta

    # ── Log ───────────────────────────────────────────────────────────────────
    def _log(self, text):
        for zeile in text.splitlines():
            z = zeile.strip()
            if z.startswith("[FEHLER]") or z.startswith("[ABBRUCH"):
                self._error_lines.append(z)
        self.log_box.config(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.config(state="disabled")
        if self._log_file:
            try:
                self._log_file.write(text)
                self._log_file.flush()
            except Exception:
                pass

    def _clear_log(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")

    def _toggle_log(self):
        if self._log_visible:
            self._log_frame.pack_forget()
            self._log_sep.pack_forget()
            self._terminal_btn.config(text="Terminal ▾")
            self._log_visible = False
        else:
            self._log_sep.pack(fill="x", padx=12, pady=4,
                               before=self._btn_row)
            self._log_frame.pack(fill="x", padx=12, pady=(0, 4),
                                 before=self._btn_row)
            self._terminal_btn.config(text="Terminal ▴")
            self._log_visible = True

    def _poll_log(self):
        while True:
            try:
                kind, data = self._log_q.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(data)
            elif kind == "done":
                self._on_done(success=data)
        self._update_start_btn_state()
        self.after(120, self._poll_log)

    def _update_start_btn_state(self):
        """Aktiviert IMPORT STARTEN erst, wenn alle noetigen Meta-Felder und
        Pfade einen Eintrag haben. Wird laufend ueber den Log-Poll-Zyklus
        (alle 120ms) neu ausgewertet - deckt damit alle Eingabewege ab
        (Tippen, Dropdown, Ordner-Dialog, LineIDWidget) ohne Einzel-Traces."""
        if self._running or not hasattr(self, "start_btn"):
            return

        T = DARK if self._dark else LIGHT
        if self._is_dop16_dmc():
            # SB_DOP_16 + DMC-4: rot und deaktiviert, siehe _update_camera_meta_rules
            self.start_btn.config(state="disabled", fg=T["err"], disabledforeground=T["err"],
                                  bg=T["btn"], activebackground=T["btn_hover"])
            return

        gds    = self.gds_var.get()
        quelle = (self.if_var.get() if gds == "SB_DOP_16" else self.quelle_var.get()).strip().strip('"')
        ziel   = self.ziel_var.get().strip().strip('"')
        area   = self.area_var.get().strip()

        ok = (bool(quelle) and os.path.isdir(quelle)
              and bool(ziel)
              and bool(area) and not area.startswith("—")
              and bool(self.terrain_var.get().strip())
              and bool(self.camera_var.get().strip()))

        if gds not in ("SB_DSM", "SB_DSM_PUNKTWOLKE"):
            ok = ok and bool(self.nodata_var.get().strip())

        if gds == "SB_DOP_16":
            ok = ok and bool(self.lineid_single_var.get().strip())
            ok = ok and bool(self.all_area_w.get_ids())
        else:
            ok = ok and bool(self.lineid_w.get_ids())

        color = T["ok"] if ok else T["hint"]
        self.start_btn.config(
            state=("normal" if ok else "disabled"),
            fg=color, disabledforeground=color,
            bg=T["btn"], activebackground=T["btn_hover"],
        )

    def _on_done(self, success):
        self._running = False
        self._update_start_btn_state()
        self._progress_bar.stop()
        self._progress_frame.pack_forget()
        if success:
            self._log("\n✓  Import erfolgreich abgeschlossen.\n")
            gds  = self._pending_archive["gds"] if self._pending_archive else self.gds_var.get()
            ziel = self._pending_ziel or ""
            if self._pending_archive:
                p = self._pending_archive
                self._write_archive_log(p["gds"], p["area"], p["line_id"], p["stac_dt"],
                                        auftragstyp=p["auftragstyp"])
            self._pending_archive = None
            self._pending_ziel    = None
            ordner_name = os.path.basename(ziel.replace("/", "\\").rstrip("\\")) if ziel else ""
            # GDWH-BUCKET Path zuruecksetzen: verhindert, dass eine naechste
            # Prozessierung versehentlich ins selbe (alte) Bucket schreibt.
            self.ziel_var.set("")
            # "ADS: fixing false NoData pixels"-Option zuruecksetzen: verhindert,
            # dass sie bei einer naechsten Prozessierung faelschlicherweise
            # noch aktiv ist.
            self.fix_nodata_var.set(False)
            self._update_start_btn_state()
            ImportDoneDialog(self, gds, ordner_name, ziel=ziel, dark=self._dark).wait()
        else:
            self._pending_archive = None
            self._pending_ziel    = None
            self._log("\n✗  Import fehlgeschlagen oder abgebrochen.\n")
            detail = "\n".join(dict.fromkeys(self._error_lines))  # Duplikate raus, Reihenfolge erhalten
            if not detail:
                detail = "Kein detaillierter Fehlertext verfuegbar - siehe Terminal-Log."
            if len(detail) > 1500:
                detail = detail[:1500] + " […]"
            messagebox.showerror(
                "Import fehlgeschlagen",
                f"Der Import wurde mit einem Fehler abgebrochen:\n\n{detail}\n\n"
                f"Details siehe Terminal-Log unten.",
            )
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def _on_close(self):
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
        self.destroy()

    # ── Helfer: AREA + Archiv-Log ─────────────────────────────────────────────
    @staticmethod
    def _area_from_ziel(ziel, gds):
        """Leitet den AREA-Namen aus dem Zielordner ab.

        Zielordner folgt dem Muster '20XX_AREA_<TYP>...' (z.B.
        '2025_GRIES_DOP16_1005'). Es wird der Teil zwischen Jahr und dem
        naechsten Typ-Token (DOP/DSM/TIN/hillshade) genommen. Schlaegt das
        fehl, dient der bereinigte Ordnername als Fallback (nur fuer den
        Log-Namen, daher unkritisch).
        """
        base = os.path.basename(ziel.rstrip("/\\"))
        m = re.search(r'20\d{2}_(.+?)_(?:DOP|DSM|TIN|hillshade)',
                      base, re.IGNORECASE)
        if m:
            return m.group(1)
        # Fallback: fuehrendes Jahr abschneiden, sonst ganzer Basename
        m2 = re.match(r'20\d{2}_(.+)', base)
        return (m2.group(1) if m2 else base) or "UNKNOWN"

    @staticmethod
    def _extract_area_from_filename(filename, gds):
        """Identische Regex-Logik wie extract_area() in den Sub-Scripts."""
        if gds in ("SB_DOP", "SB_DOP_16"):
            m = re.search(r'20\d{2}_(.+?)_DOP', filename, re.IGNORECASE)
        elif gds == "SB_DSM":
            if "hillshade" in filename.lower():
                m = re.search(r'20\d{2}_(.+?)_hillshade', filename, re.IGNORECASE)
            else:
                m = re.search(r'20\d{2}_(.+?)_DSM', filename, re.IGNORECASE)
        elif gds == "SB_DSM_PUNKTWOLKE":
            m = re.search(r'20\d{2}_(.+?)_TIN', filename, re.IGNORECASE)
        else:
            m = None
        return m.group(1) if m else "UNKNOWN"

    @staticmethod
    def _extract_area_from_source(src_folder, gds):
        """Area aus erstem passenden Dateinamen im Quellordner (inkl. 1 Unterebene).

        Für SB_DOP_16: src_folder = INPUT_FOLDER (Dateien evtl. noch nicht reorganisiert).
        Für andere GDS: src_folder = Data-Input Path.
        """
        extensions = ('.tif', '.tiff', '.laz')
        if not os.path.isdir(src_folder):
            return "—  (Ordner nicht gefunden)"
        try:
            for fn in sorted(os.listdir(src_folder)):
                if fn.lower().endswith(extensions):
                    return GDWHApp._extract_area_from_filename(fn, gds)
            # Eine Ebene tiefer (SB_DOP_16 vor/nach Reorganisation)
            for sub in sorted(os.listdir(src_folder)):
                sub_path = os.path.join(src_folder, sub)
                if os.path.isdir(sub_path):
                    for fn in sorted(os.listdir(sub_path)):
                        if fn.lower().endswith(extensions):
                            return GDWHApp._extract_area_from_filename(fn, gds)
        except Exception:
            pass
        return "—  (keine passende Datei gefunden)"

    @staticmethod
    def _extract_tilekey_from_filename(filename, gds):
        """Identische Logik wie extract_tile()/extract_tile_lv95() in den Sub-Scripts
        (zwei Namensteile direkt vor 'LV95', z.B. ..._2601_1136_LV95_LN02.laz -> '2601_1136')."""
        if gds == "SB_DSM":
            return "1000  (fix für SB_DSM)"
        parts = filename.rsplit('.', 1)[0].split('_')
        if "LV95" not in parts:
            return "NICHT GEFUNDEN – 'LV95' fehlt im Dateinamen!"
        idx = parts.index("LV95")
        if idx < 2:
            return f"FEHLER – 'LV95' steht zu früh (Position {idx})"
        return parts[idx - 2] + "_" + parts[idx - 1]

    @staticmethod
    def _extract_tilekey_from_source(src_folder, gds):
        """TileKey aus erstem passenden Dateinamen im Quellordner (inkl. 1 Unterebene).

        Gibt (tilekey, dateiname) zurück, damit im Sicherheitscheck ersichtlich ist,
        aus welcher Datei der TileKey abgeleitet wurde (Kontrolle der Namenskonvention).
        """
        extensions = ('.tif', '.tiff', '.laz')
        if not os.path.isdir(src_folder):
            return "—", "(Ordner nicht gefunden)"
        try:
            for fn in sorted(os.listdir(src_folder)):
                if fn.lower().endswith(extensions):
                    return GDWHApp._extract_tilekey_from_filename(fn, gds), fn
            # Eine Ebene tiefer (SB_DOP_16 vor/nach Reorganisation)
            for sub in sorted(os.listdir(src_folder)):
                sub_path = os.path.join(src_folder, sub)
                if os.path.isdir(sub_path):
                    for fn in sorted(os.listdir(sub_path)):
                        if fn.lower().endswith(extensions):
                            return GDWHApp._extract_tilekey_from_filename(fn, gds), fn
        except Exception:
            pass
        return "—", "(keine passende Datei gefunden)"

    @staticmethod
    def _sanitize(text):
        """Macht einen String dateinamen-tauglich (keine Pfad-/Sonderzeichen)."""
        return re.sub(r'[^A-Za-z0-9_.-]', '_', str(text)).strip("_") or "UNKNOWN"

    def _write_archive_log(self, gds, area, line_id, stac_dt, auftragstyp=""):
        """Haengt einen Eintrag an das fortlaufende Archiv-Log an.

        Format:  {stamp}  {GDS}_{AREA}_{Line_ID} = {STAC-Link}
        stac_dt: StacItemIdDatetime, siehe _line_ids.stac_datetime
        """
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            archive_path = os.path.join(LOG_DIR, "GDWHimport_archived_AREA_proGDS.log")
            stamp     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            stac_link = (
                "https://data.geo.admin.ch/browser/index.html#/collections/"
                "ch.swisstopo.spezialbefliegungen/items/"
                f"{auftragstyp}-{stac_dt}"
            )
            entry = f"{gds}_{area}_{line_id} = {stac_link}"
            with open(archive_path, "a", encoding="utf-8") as f:
                f.write(f"{stamp}  {entry}\n")
        except Exception as e:
            print(f"[WARNUNG] Archiv-Log konnte nicht geschrieben werden: {e}")

    # ── Import starten ────────────────────────────────────────────────────────
    def _start_import(self):
        if self._running:
            return
        if not self._validate():
            return

        gds  = self.gds_var.get()
        meta = self._build_meta_info()
        ziel = self.ziel_var.get().strip().strip('"')

        # SB_DOP_16: quelle = INPUT_FOLDER; effektive Quelle = INPUT_FOLDER\HHMM
        if gds == "SB_DOP_16":
            quelle         = self.if_var.get().strip().strip('"')
            quelle_display = os.path.join(quelle, meta["Line_ID"][0][9:13])
        else:
            quelle         = self.quelle_var.get().strip().strip('"')
            quelle_display = quelle

        # Zielpfad-Warnung
        norm  = ziel.replace("/", "\\")
        parts = [p for p in norm.split("\\") if p]
        if len(parts) >= 2 and parts[-2] != gds:
            if not messagebox.askyesno("Zielpfad-Warnung",
                f"Im Zielpfad wurde der GDS-Ordner '{gds}' nicht als vorletzten Ordner erkannt.\n\n"
                f"Vorletzter Ordner:  '{parts[-2]}'\nErwartet:            '{gds}'\n\n"
                f"Trotzdem fortfahren?", parent=self):
                return

        # Sicherheitscheck
        # StacItemIdDatetime der ersten Linie - dieselbe Regel wie das XML (Script 1)
        stac_dt = _line_ids.stac_datetime(meta.get("Line_ID", []), meta.get("CameraSystem", ""))
        _tilekey, _tilekey_file = self._extract_tilekey_from_source(quelle, gds)
        dlg = SicherheitsCheckDialog(
            self, gds, meta, quelle_display, ziel,
            area=meta.get("AreaOverride") or self._extract_area_from_source(quelle, gds),
            stac_dt=stac_dt,
            tilekey=_tilekey, tilekey_file=_tilekey_file,
            dark=self._dark,
        )
        if not dlg.wait():
            return

        self._running = True
        T = DARK if self._dark else LIGHT
        self.start_btn.config(state="disabled", fg=T["fg_dim"], disabledforeground=T["fg_dim"])
        self._progress_frame.pack(fill="x", padx=12, pady=(0, 4), before=self._btn_row)
        self._progress_bar.start(10)
        self._clear_log()
        self._error_lines = []

        # Logdatei öffnen (logs-Ordner neben diesem Script)
        # Name: GDWHimport_{GDS}_{AREA}_{Line_ID}_{YYYYmmdd_HHMMSS}.log
        line_ids   = meta.get("Line_ID", [])
        first_line = line_ids[0] if line_ids else "UNKNOWN"
        area       = self._area_from_ziel(ziel, gds)
        area_s     = self._sanitize(area)
        line_s     = self._sanitize(first_line)
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = os.path.join(
                LOG_DIR, f"GDWHimport_{gds}_{area_s}_{line_s}_{ts}.log")
            self._log_file = open(log_path, "w", encoding="utf-8")
        except Exception as e:
            self._log_file = None
            print(f"[WARNUNG] Logdatei konnte nicht erstellt werden: {e}")

        # Archiv-Log-Parameter merken – Eintrag wird erst nach erfolgreichem Abschluss geschrieben
        self._pending_archive = {
            "gds":         gds,
            "area":        area_s,
            "line_id":     line_s,
            "stac_dt":     stac_dt,
            "auftragstyp": meta.get("Auftragstyp", ""),
        }
        self._pending_ziel = ziel

        self._log(f"=== GDWH Import gestartet – GDS: {gds} ===\n\n")

        threading.Thread(
            target=self._run_thread,
            args=(gds, meta, quelle, ziel),
            daemon=True
        ).start()

    # ── Import-Thread ─────────────────────────────────────────────────────────
    def _run_thread(self, gds, meta, quelle, ziel):
        old_stdout = sys.stdout
        orig_input = builtins.input
        sys.stdout    = _QueueWriter(self._log_q)
        builtins.input = lambda prompt="": "Y"

        try:
            if gds == "SB_DOP_16":
                self._exec_dop16(gds, meta, quelle, ziel)
            else:
                self._exec_standard(gds, meta, quelle, ziel)
            self._log_q.put(("done", True))
        except SystemExit as e:
            if str(e) not in ("0", "None", ""):
                print(f"\n[ABBRUCH durch Script] Exit-Code: {e}")
            self._log_q.put(("done", False))
        except Exception as e:
            print(f"\n[FEHLER] {e}\n")
            print(traceback.format_exc())
            self._log_q.put(("done", False))
        finally:
            sys.stdout    = old_stdout
            builtins.input = orig_input

    # ── OSGeo4W Subprocess Ausführung ─────────────────────────────────────────
    def _exec_with_osgeo(self, action, gds, meta, quelle, ziel):
        """Führt Script 1 oder Script 2_2 via OSGeo4W Python als Subprocess aus."""
        cfg = {
            "action":           action,
            "gds":              gds,
            "meta_info":        meta,
            "quelle":           quelle,
            "ziel":             ziel,
            "script_1":         SCRIPT_1,
            "script_22":        SCRIPT_22,
            "script_3":         SCRIPT_3,
            "script_4":         SCRIPT_4,
            "fix_false_nodata": bool(meta.get("FixFalseNodata")),
            "staging_dir":      self._staging_dir,
        }
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        try:
            json.dump(cfg, tmp, ensure_ascii=False, indent=2)
            tmp.close()
            env = os.environ.copy()
            env["PYTHONHOME"] = _detect_python_home(self._osgeo_python)
            # Verhindert, dass private User-Site-Packages (z.B. eine per pip
            # installierte NumPy-Version) die OSGeo4W-eigenen, dazu passenden
            # Paketversionen ueberschatten - siehe Vorfall 22.7. (ABI-Konflikt
            # NumPy 2.x vs. gegen NumPy 1.x kompilierte osgeo.gdal_array-Bindung).
            env["PYTHONNOUSERSITE"] = "1"
            print(f"[Subprocess] {self._osgeo_python}\n", flush=True)
            proc = subprocess.Popen(
                [self._osgeo_python, RUNNER_SCRIPT, tmp.name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            for line in proc.stdout:
                print(line, end="", flush=True)
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"OSGeo4W Subprocess beendet mit Exit-Code {proc.returncode}"
                )
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    def _exec_standard(self, gds, meta, quelle, ziel):
        self._exec_with_osgeo("standard", gds, meta, quelle, ziel)

    def _exec_dop16(self, gds, meta, input_folder, ziel):
        # Script 2_1: Ordner organisieren (erkennt bereits vorhandene Struktur automatisch)
        print("Script 2_1 wird geladen…\n")
        m21 = _lade_modul("script_21", SCRIPT_21)
        print(">>> .pyr und .xml Dateien löschen <<<\n")
        m21.delete_unwanted_files(input_folder)
        print("\n>>> VORSCHAU – keine Dateien werden verschoben <<<\n")
        m21.organize_files(input_folder, dry_run=True)
        print("\n>>> Dateien werden verschoben… <<<\n")
        m21.organize_files(input_folder, dry_run=False)
        try:
            subs = sorted(d for d in os.listdir(input_folder)
                           if os.path.isdir(os.path.join(input_folder, d)))
            if subs:
                print(f"\nUnterordner in:\n  {input_folder}")
                for s in subs:
                    print(f"  → {s}")
        except Exception:
            pass

        # Data-Input Path: INPUT_FOLDER + HHMM-Teil der Line_ID (= BandID)
        band_id          = meta["Line_ID"][0][9:13]
        effective_quelle = os.path.join(input_folder, band_id)
        print(f"\n[SB_DOP_16] Data-Input Path: {effective_quelle}\n")

        print("\nScript 2_2 wird via OSGeo4W Python gestartet…\n")
        self._exec_with_osgeo("dop16", gds, meta, effective_quelle, ziel)


# ─── Start ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = GDWHApp()
    app.mainloop()
