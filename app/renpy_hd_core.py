r"""
renpy_hd_core.py - moteur RenPyHD (sans interface graphique).

Ce module sait :
  * analyser un jeu Ren'Py (version, résolution, images référencées par les
    scripts .rpy, archives .rpa) ;
  * construire un plan de traitement (mode « HD 2x », « Remplacer sur place »
    ou « Dossier d'images libre ») ;
  * exécuter ce plan avec le DLSS 5 Visual Enhancer (src.images.convert_images
    et src.video.convert_videos) par lots, de façon reprenable et annulable ;
  * installer / désinstaller le hook zz_dlss_hd.rpy, restaurer les originaux ;
  * lister les paires avant/après pour la visionneuse.

Il est indépendant de Gradio : il ne fait que des appels de fonctions et des
callbacks (log, progression, événement d'annulation).
"""
from __future__ import annotations

import io
import json
import os
import pickle
import re
import shutil
import subprocess
from contextlib import suppress
import sys
import threading
import time
import zlib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

# ----------------------------------------------------------------------------
# Constantes
# ----------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
try:
    from renpy_hd_i18n import t as T   # textes localisés (fr par défaut) ; l'application choisit la langue avant d'importer ce module
except Exception:  # pragma: no cover - usage hors application
    def T(key: str, **fmt) -> str:  # type: ignore[misc]
        return key
HOOK_NAME = "zz_dlss_hd.rpy"
HOOK_TEMPLATE = APP_DIR / HOOK_NAME
BACKUP_DIR = "_dlss_backup"
BACKUP_MANIFEST = "manifest.json"
FACTOR_FILE = "factor.txt"
CONFIG_FILE = APP_DIR / "renpy_hd_config.json"

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTS = (".webm", ".ogv", ".mp4", ".mkv", ".avi")
FORMAT_BY_EXT = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WebP"}
EXT_BY_FORMAT = {"JPEG": ".jpg", "PNG": ".png", "WebP": ".webp", "AVIF": ".avif", "TIFF": ".tiff"}
ALL_FORMATS = ("PNG", "JPEG", "WebP", "AVIF", "TIFF")          # formats acceptés par l'outil
RENPY_FORMATS = ("JPEG", "PNG", "WebP")                        # formats que Ren'Py sait charger

# Codecs vidéo de sortie proposés pour Ren'Py : clé -> (libellé, conteneur, extension)
VIDEO_CODECS = {
    "vp9": ("WebM VP9 (recommandé, lu par Ren'Py 6.99 → 8.x)", "webm", ".webm"),
    "vp8": ("WebM VP8 (le plus compatible, moins efficace)", "webm", ".webm"),
    "av1": ("WebM AV1 (Ren'Py ≥ 8.1 seulement, décodage lourd)", "webm", ".webm"),
    "h264": ("MP4 H.264 (Ren'Py 7/8 sur PC ; encodage NVENC rapide)", "mp4", ".mp4"),
}
VIDEO_CAPS = {                     # plafond de résolution de sortie (largeur × hauteur)
    "1920×1080": (1920, 1080),
    "2560×1440": (2560, 1440),
    "3840×2160": (3840, 2160),
    "5120×2880": (5120, 2880),
    "7680×4320 (limite DLSS, aucun plafond)": (7680, 4320),
}
VIDEO_OVER_CAP = {"reduce": "Réduire le facteur (jusqu'à 1× DLAA)", "skip": "Ignorer la vidéo"}
VIDEO_MANIFEST = "videos.json"     # écrit dans le dossier HD : chemin -> facteur réel de chaque vidéo
VIDEO_PROBE_CACHE = APP_DIR / "video_probe_cache.json"
VIDEO_DIRS = ("movies/", "videos/", "video/")   # dossiers de vidéos ajoutés à l'analyse même sans citation dans un .rpy

# Valeurs exactes acceptées par src/video.py du DLSS 5 Visual Enhancer
UPSCALING_FACTORS = {
    1.0: "1× (DLAA, résolution native)",
    1.5: "1.5× (Quality)",
    1.724: "1.724× (Balanced)",
    2.0: "2× (Performance)",
    3.0: "3× (Ultra Performance)",
}
NR_STYLES = ("Default", "Natural", "Cinematic")
NR_PRESETS = ("Default", "Preset #1", "Preset #2", "Preset #3")
DLSS_MODEL_PRESETS = ("Default", "J", "K", "L", "M")
NR_RANGE = (0.0, 2.0)
SKIN_RANGE = (-1.0, 2.0)
QUALITY_RANGE = (1, 100)

MODE_HD = "hd2x"
MODE_REPLACE = "replace"
MODE_FOLDER = "folder"
MODE_AT2 = "at2"          # Ren'Py >= 8.4 : <nom>@2.<ext> à côté de l'original, sans hook
AT2_MANIFEST = "_dlss_at2.json"

IMG_RE = re.compile(r'"([^"\n]+?\.(?:jpg|jpeg|png|webp))"', re.IGNORECASE)
VID_RE = re.compile(r'"([^"\n]+?\.(?:webm|ogv|mp4|mkv|avi))"', re.IGNORECASE)

# Préréglages « Neural Rendering » proposés dans l'interface
PRESETS: dict[str, dict[str, object]] = {
    "Visages (K : tout au max)": dict(nr_style="Default", nr_preset="Default", nr_intensity=2.0,
                                      local_tone=2.0, local_structure=2.0, skin_structure=2.0),
    "Équilibré (défaut)": dict(nr_style="Default", nr_preset="Default", nr_intensity=1.0,
                               local_tone=1.0, local_structure=1.0, skin_structure=-1.0),
    "Fidèle (discret)": dict(nr_style="Natural", nr_preset="Default", nr_intensity=0.6,
                             local_tone=0.5, local_structure=0.6, skin_structure=-1.0),
    "Cinéma (contrasté)": dict(nr_style="Cinematic", nr_preset="Default", nr_intensity=1.2,
                               local_tone=1.3, local_structure=1.0, skin_structure=-1.0),
    "Portrait / peau": dict(nr_style="Natural", nr_preset="Default", nr_intensity=1.0,
                            local_tone=0.8, local_structure=0.8, skin_structure=1.0),
}


# ----------------------------------------------------------------------------
# Réglages
# ----------------------------------------------------------------------------
@dataclass
class DlssSettings:
    """Tous les réglages transmis à ImageConversionOptions (+ formats par type)."""
    factor: float = 2.0
    nr_style: str = "Default"
    nr_preset: str = "Default"
    nr_intensity: float = 1.0
    local_tone: float = 1.0
    local_structure: float = 1.0
    skin_structure: float = -1.0
    automatic_mask: bool = False
    warmup_frames: int = 0
    dlss_model_preset: str = "Default"
    quality: int = 92
    preserve_metadata: bool = False
    jpeg_as: str = "JPEG"      # format de sortie pour les sources JPEG
    png_as: str = "PNG"        # ... pour les sources PNG
    webp_as: str = "WebP"      # ... pour les sources WebP

    def output_format_for(self, source_format: str) -> str:
        return {"JPEG": self.jpeg_as, "PNG": self.png_as, "WebP": self.webp_as}.get(source_format, source_format)

    def validate(self, renpy_only: bool) -> None:
        if self.factor not in UPSCALING_FACTORS:
            raise ValueError(T("core.err.factor", factor=self.factor, choices=", ".join(map(str, UPSCALING_FACTORS))))
        for name, value, (lo, hi) in (
            ("Intensité NR", self.nr_intensity, NR_RANGE),
            ("Local tone", self.local_tone, NR_RANGE),
            ("Local structure", self.local_structure, NR_RANGE),
            ("Skin structure", self.skin_structure, SKIN_RANGE),
        ):
            if not lo <= float(value) <= hi:
                raise ValueError(T("core.err.range", name=name, lo=lo, hi=hi))
        if not QUALITY_RANGE[0] <= int(self.quality) <= QUALITY_RANGE[1]:
            raise ValueError(T("core.err.quality"))
        allowed = RENPY_FORMATS if renpy_only else ALL_FORMATS
        for fmt in (self.jpeg_as, self.png_as, self.webp_as):
            if fmt not in allowed:
                raise ValueError(T("core.err.format", fmt=fmt, choices=", ".join(allowed)))


@dataclass
class VideoSettings:
    """Réglages du chemin vidéo (DLSS 5 image par image, puis réencodage pour Ren'Py)."""
    enabled: bool = False
    codec: str = "vp9"         # clé de VIDEO_CODECS
    crf: int = 31              # VP9/VP8/AV1 : 0 … 63 ; H.264 : 0 … 51 (borné automatiquement)
    speed: int = 2             # 0 (lent, meilleur) … 5 (rapide) ; traduit par codec
    keep_audio: bool = True    # piste audio d'origine copiée telle quelle si le conteneur l'accepte, sinon réencodée
    audio_kbps: int = 128      # débit si réencodage (Opus pour WebM, AAC pour MP4)
    scene_reset: bool = True   # réinitialisation temporelle DLSS aux changements de plan
    warmup_frames: int = 0
    hw_encode: bool = True     # NVENC pour l'intermédiaire et pour H.264 / AV1 final quand disponible
    max_width: int = 3840      # plafond de résolution de sortie (Ren'Py décode en logiciel : 4K max conseillé)
    max_height: int = 2160
    over_cap: str = "reduce"   # "reduce" : baisse le facteur (jusqu'à 1× DLAA) ; "skip" : ignore la vidéo
    share_nr: bool = True      # réglages Neural Rendering partagés avec les images
    nr: DlssSettings | None = None   # réglages NR propres aux vidéos si share_nr est faux
    intermediate_quality: str = "Max"   # qualité de l'intermédiaire H.264/HEVC produit par l'outil
    limit_seconds: float = 0.0          # > 0 : ne traite que les N premières secondes (onglet « Tester une vidéo »)

    def nr_for(self, images: DlssSettings) -> DlssSettings:
        return images if self.share_nr or self.nr is None else self.nr

    def container_ext(self) -> str:
        return VIDEO_CODECS[self.codec][2]

    def validate(self) -> None:
        if self.codec not in VIDEO_CODECS:
            raise ValueError(f"Codec vidéo inconnu : {self.codec}")
        if not 0 <= int(self.crf) <= 63:
            raise ValueError("Le CRF vidéo doit être entre 0 et 63.")
        if not 0 <= int(self.speed) <= 5:
            raise ValueError("La vitesse d'encodage doit être entre 0 et 5.")
        if self.over_cap not in VIDEO_OVER_CAP:
            raise ValueError(f"Comportement au-delà du plafond inconnu : {self.over_cap}")


@dataclass
class VideoInfo:
    """Résultat de ffprobe sur une vidéo source."""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    frames: int = 0
    codec: str = ""
    size: int = 0
    audio_codec: str = ""      # "" = pas de piste audio
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.width > 0 and self.height > 0 and not self.error

    def label(self) -> str:
        fps = f"{self.fps:g}" if self.fps else "?"
        return f"{self.codec or '?'} {self.width}×{self.height} @{fps} i/s"


@dataclass
class ScanSettings:
    extensions: tuple[str, ...] = IMAGE_EXTS
    exclude_prefixes: tuple[str, ...] = ("gui/",)
    include_regex: str = ""
    exclude_regex: str = ""
    path_filter: str = ""
    use_rpa: bool = True
    min_dim: int = 256           # plus petit côté minimal : les petites images font échouer la vérification DLSS
    max_dim: int = 0
    limit: int = 0
    overwrite: bool = False
    scan_mode: str = "auto"      # "auto" | "scripts" (images citées dans les .rpy) | "all" (toutes les images du dossier)
    retry_failed: bool = True    # retente individuellement les images échouées d'un lot


@dataclass
class RunSettings:
    mode: str = MODE_HD
    game_root: str = ""
    out_name: str = "hd2x"
    input_dir: str = ""        # mode dossier libre
    output_dir: str = ""       # mode dossier libre
    install_hook: bool = True
    cache_mb: int = 1536
    chunk: int = 300
    dry_run: bool = False


# ----------------------------------------------------------------------------
# Archives RPA (RPA-3.0 / RPA-2.0)
# ----------------------------------------------------------------------------
class Rpa:
    def __init__(self, path: Path):
        self.path = path
        with path.open("rb") as f:
            header = f.readline()
            parts = header.split()
            version = parts[0]
            offset = int(parts[1], 16)
            key = int(parts[2], 16) if version == b"RPA-3.0" else 0
            f.seek(offset)
            index = pickle.loads(zlib.decompress(f.read()), encoding="latin1")
        self.index: dict[str, list] = {}
        for name, entries in index.items():
            fixed = []
            for entry in entries:
                off, length = entry[0] ^ key, entry[1] ^ key
                prefix = entry[2] if len(entry) > 2 else b""
                if isinstance(prefix, str):
                    prefix = prefix.encode("latin1")
                fixed.append((off, length, prefix))
            self.index[name.replace("\\", "/")] = fixed
        self.lower = {k.casefold(): k for k in self.index}

    def find(self, name: str) -> str | None:
        return self.lower.get(name.casefold())

    def size(self, name: str) -> int:
        return sum(length for _off, length, _p in self.index[name])

    def read(self, name: str) -> bytes:
        buf = io.BytesIO()
        self._write(name, buf)
        return buf.getvalue()

    def extract(self, name: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as out:
            self._write(name, out)
        return dest

    def _write(self, name: str, out) -> None:
        with self.path.open("rb") as f:
            for off, length, prefix in self.index[name]:
                out.write(prefix)
                f.seek(off)
                remaining = length - len(prefix)
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)


# ----------------------------------------------------------------------------
# Modèle : sources, tâches, plan
# ----------------------------------------------------------------------------
@dataclass
class Source:
    """Un fichier d'origine : fichier libre ou entrée d'archive .rpa."""
    path: Path | None = None
    rpa: Rpa | None = None
    rpa_key: str | None = None

    @property
    def from_rpa(self) -> bool:
        return self.rpa is not None

    @property
    def size(self) -> int:
        if self.path is not None:
            try:
                return self.path.stat().st_size
            except OSError:
                return 0
        return self.rpa.size(self.rpa_key)  # type: ignore[union-attr]

    def describe(self) -> str:
        if self.path is not None:
            return str(self.path)
        return f"{self.rpa.path.name}::{self.rpa_key}"  # type: ignore[union-attr]

    def read_bytes(self) -> bytes:
        if self.path is not None:
            return self.path.read_bytes()
        return self.rpa.read(self.rpa_key)  # type: ignore[union-attr]

    def materialize(self, tmp: Path) -> Path:
        """Renvoie un chemin de fichier réel (extrait l'entrée .rpa si besoin)."""
        if self.path is not None:
            return self.path
        return self.rpa.extract(self.rpa_key, tmp / self.rpa_key)  # type: ignore[union-attr]


@dataclass
class Job:
    rel: str                       # chemin relatif tel que référencé par le jeu
    source: Source
    dest: Path                     # fichier final
    source_format: str             # JPEG / PNG / WebP (ou "VIDEO")
    output_format: str             # format demandé à l'outil
    backup: Path | None = None     # mode remplacement : copie de sauvegarde
    is_video: bool = False
    video_info: VideoInfo | None = None
    video_factor: float = 1.0      # facteur réellement appliqué à cette vidéo (plafond de résolution)


@dataclass
class Plan:
    jobs: list[Job] = field(default_factory=list)
    already_done: int = 0
    missing: list[str] = field(default_factory=list)
    filtered_out: int = 0
    too_small: int = 0
    total_refs: int = 0
    video_refs: int = 0
    video_bytes: int = 0
    video_skipped: list[str] = field(default_factory=list)
    video_infos: dict[str, VideoInfo] = field(default_factory=dict)   # chemin -> sondage ffprobe (si vidéos activées)
    video_duration: float = 0.0      # secondes cumulées des vidéos trouvées
    video_frames_todo: int = 0       # images DLSS à rendre pour les vidéos du plan
    video_plan_lines: list[str] = field(default_factory=list)          # résumé « N × 1920×1080 → 3840×2160 (2×) »
    source_bytes: int = 0
    estimated_output_bytes: int = 0
    scan_mode_used: str = "scripts"
    notes: list[str] = field(default_factory=list)


@dataclass
class GameInfo:
    root: Path
    game_dir: Path
    renpy_version: str = "inconnue"
    resolution: str = "inconnue"
    rpa_files: list[str] = field(default_factory=list)
    rpa_entries: int = 0
    hook_installed: bool = False
    hd_dir_exists: bool = False
    hd_factor: str = ""
    backup_exists: bool = False
    loose_images: int = 0


# ----------------------------------------------------------------------------
# Analyse du jeu
# ----------------------------------------------------------------------------
def find_game_dir(root: str | os.PathLike[str]) -> Path:
    root_path = Path(root).expanduser()
    if not str(root).strip():
        raise ValueError(T("core.err.no_root"))
    if (root_path / "game").is_dir():
        return root_path / "game"
    if root_path.name.lower() == "game" and root_path.is_dir():
        return root_path
    raise ValueError(T("core.err.not_game", root=root_path))


def detect_renpy_version(root: Path) -> str:
    """Version de Ren'Py : 7.x (version_tuple = (7, 3, 5, vc_version)) ou 8.x (version = "8.3.4.24120703" dans vc_version.py)."""
    init = root / "renpy" / "__init__.py"
    vc_file = root / "renpy" / "vc_version.py"
    vc_text = vc_file.read_text(encoding="utf-8", errors="ignore") if vc_file.is_file() else ""
    # Ren'Py 8 : la version complète est dans renpy/vc_version.py
    m = re.search(r"""^\s*version\s*=\s*['"](\d+(?:\.\d+)+)['"]""", vc_text, re.MULTILINE)
    if m:
        return m.group(1)
    if not init.is_file():
        sv = root / "game" / "script_version.txt"
        if sv.is_file():
            nums = re.findall(r"\d+", sv.read_text(encoding="utf-8", errors="ignore"))
            if nums:
                return ".".join(nums) + " (d'après script_version.txt)"
        return "inconnue (renpy/__init__.py absent)"
    text = init.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"""^\s*version\s*=\s*['"](\d+(?:\.\d+)+)['"]""", text, re.MULTILINE)
    if m:
        return m.group(1)
    m = re.search(r"version_tuple\s*=\s*\(([^)]*)\)", text)
    if m:
        parts = [p.strip() for p in m.group(1).split(",") if p.strip()]
        nums = [p for p in parts if p.isdigit()]
        if nums:
            vc = ""
            if "vc_version" in parts:
                mv = re.search(r"vc_version\s*=\s*(\d+)", vc_text) or re.search(r"vc_version\s*=\s*(\d+)", text)
                vc = f".{mv.group(1)}" if mv and mv.group(1) != "0" else ""
            return ".".join(nums) + vc
    m = re.search(r"""version\s*=\s*['"]Ren'Py ([^'"]+)['"]""", text)
    return m.group(1) if m else "inconnue"


def renpy_version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version)[:4])


def supports_at2(version: str) -> bool:
    """Ren'Py >= 8.4 charge nativement les variantes <nom>@2.<ext>."""
    return renpy_version_tuple(version)[:2] >= (8, 4)


def detect_resolution(game: Path) -> str:
    for name in ("gui.rpy", "options.rpy", "screens.rpy"):
        f = game / name
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"gui\.init\s*\(\s*(\d+)\s*,\s*(\d+)", text)
        if m:
            return f"{m.group(1)}×{m.group(2)}"
    w = h = None
    for rpy in game.rglob("*.rpy"):
        try:
            text = rpy.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        mw = re.search(r"config\.screen_width\s*=\s*(\d+)", text)
        mh = re.search(r"config\.screen_height\s*=\s*(\d+)", text)
        w = w or (mw.group(1) if mw else None)
        h = h or (mh.group(1) if mh else None)
        if w and h:
            return f"{w}×{h}"
    # Jeux sans .rpy (scripts compilés dans un .rpa) : le log de Ren'Py note la résolution virtuelle.
    log = game.parent / "log.txt"
    if log.is_file():
        m = re.search(r"Screen sizes:\s*virtual=\((\d+),\s*(\d+)\)", log.read_text(encoding="utf-8", errors="ignore"))
        if m:
            return f"{m.group(1)}×{m.group(2)} (d'après log.txt)"
    return "inconnue"


def collect_refs(game: Path, out_name: str) -> tuple[list[str], list[str]]:
    """Chemins d'images et de vidéos cités dans les scripts .rpy (chaînes entre guillemets)."""
    images: set[str] = set()
    videos: set[str] = set()
    skip = (out_name.rstrip("/") + "/", BACKUP_DIR + "/")
    for rpy in game.rglob("*.rpy"):
        try:
            text = rpy.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for regex, bucket in ((IMG_RE, images), (VID_RE, videos)):
            for m in regex.finditer(text):
                rel = m.group(1).replace("\\", "/").strip().lstrip("/")
                if "[" in rel or "{" in rel or rel.startswith(skip) or "://" in rel:
                    continue
                bucket.add(rel)
    return sorted(images), sorted(videos)


def has_scripts(game: Path) -> bool:
    return any(game.rglob("*.rpy"))


def collect_all_files(game: Path, rpas: list[Rpa], out_name: str) -> tuple[list[str], list[str]]:
    """Mode « toutes les images du dossier » : game/images/** (fichiers libres) + entrées images/ des .rpa, et vidéos."""
    skip = tuple(s.casefold() for s in (out_name.rstrip("/") + "/", BACKUP_DIR + "/", "cache/", "saves/"))
    images: set[str] = set()
    videos: set[str] = set()

    def consider(rel: str) -> None:
        low = rel.casefold()
        if low.startswith(skip):
            return
        ext = Path(rel).suffix.lower()
        if ext in FORMAT_BY_EXT and (low.startswith("images/") or "/" not in rel):
            images.add(rel)
        elif ext in VIDEO_EXTS:
            videos.add(rel)

    for f in game.rglob("*"):
        if f.is_file():
            consider(f.relative_to(game).as_posix())
    for rpa in rpas:
        for key in rpa.index:
            consider(key)
    return sorted(images), sorted(videos)


def load_rpas(game: Path) -> list[Rpa]:
    rpas = []
    for p in sorted(game.glob("*.rpa")):
        try:
            rpas.append(Rpa(p))
        except Exception:
            continue
    return rpas


def resolve_source(game: Path, rel: str, rpas: list[Rpa]) -> Source | None:
    # Ren'Py cherche les images sous images/ et les vidéos (lues par le module audio) sous audio/.
    prefixes = ("", "images/") if Path(rel).suffix.lower() not in VIDEO_EXTS else ("", "audio/", "images/")
    for prefix in prefixes:
        cand = game / (prefix + rel)
        if cand.is_file():
            return Source(path=cand)
    for rpa in rpas:
        for prefix in prefixes:
            found = rpa.find(prefix + rel)
            if found:
                return Source(rpa=rpa, rpa_key=found)
    return None


def collect_video_dirs(game: Path, rpas: list[Rpa], out_name: str) -> list[str]:
    """Vidéos des dossiers game/movies, game/videos, game/video (fichiers libres et entrées .rpa)."""
    skip = tuple(s.casefold() for s in (out_name.rstrip("/") + "/", BACKUP_DIR + "/"))
    found: set[str] = set()
    for d in VIDEO_DIRS:
        folder = game / d.rstrip("/")
        if folder.is_dir():
            for f in folder.rglob("*"):
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                    found.add(f.relative_to(game).as_posix())
    for rpa in rpas:
        for key in rpa.index:
            low = key.casefold()
            if low.startswith(VIDEO_DIRS) and not low.startswith(skip) and Path(key).suffix.lower() in VIDEO_EXTS:
                found.add(key)
    return sorted(found)


def image_dimensions(source: Source) -> tuple[int, int] | None:
    try:
        from PIL import Image
        if source.path is not None:
            with Image.open(source.path) as im:
                return im.size
        with Image.open(io.BytesIO(source.read_bytes())) as im:
            return im.size
    except Exception:
        return None


def _passes_filters(rel: str, scan: ScanSettings) -> bool:
    low = rel.casefold()
    if scan.path_filter and scan.path_filter.casefold() not in low:
        return False
    for prefix in scan.exclude_prefixes:
        if prefix and low.startswith(prefix.casefold()):
            return False
    if scan.include_regex and not re.search(scan.include_regex, rel, re.IGNORECASE):
        return False
    if scan.exclude_regex and re.search(scan.exclude_regex, rel, re.IGNORECASE):
        return False
    return True


def _dims_check(source: Source, scan: ScanSettings) -> str:
    """"ok", "small" (plus petit côté < min_dim) ou "large" (plus grand côté > max_dim)."""
    if not scan.min_dim and not scan.max_dim:
        return "ok"
    dims = image_dimensions(source)
    if dims is None:
        return "ok"
    w, h = dims
    if scan.min_dim and min(w, h) < scan.min_dim:
        return "small"
    if scan.max_dim and max(w, h) > scan.max_dim:
        return "large"
    return "ok"


def _at2_dest(path: Path) -> Path:
    return path.with_name(f"{path.stem}@2{path.suffix}")


def _dest_with_format(base: Path, rel: str, out_fmt: str) -> Path:
    p = Path(rel)
    return base / p.with_suffix(EXT_BY_FORMAT[out_fmt])


def inspect_game(root: str) -> GameInfo:
    game = find_game_dir(root)
    root_path = game.parent
    rpas = load_rpas(game)
    info = GameInfo(root=root_path, game_dir=game)
    info.renpy_version = detect_renpy_version(root_path)
    info.resolution = detect_resolution(game)
    info.rpa_files = [r.path.name for r in rpas]
    info.rpa_entries = sum(len(r.index) for r in rpas)
    info.hook_installed = (game / HOOK_NAME).is_file()
    info.backup_exists = (game / BACKUP_DIR).is_dir()
    return info


def build_game_plan(run: RunSettings, scan: ScanSettings, dlss: DlssSettings, video: VideoSettings) -> tuple[GameInfo, Plan]:
    """Plan pour les modes HD et Remplacement."""
    info = inspect_game(run.game_root)
    game = info.game_dir
    out_dir = game / run.out_name
    info.hd_dir_exists = out_dir.is_dir()
    if (out_dir / FACTOR_FILE).is_file():
        info.hd_factor = (out_dir / FACTOR_FILE).read_text(encoding="utf-8", errors="ignore").strip()
    rpas = load_rpas(game) if scan.use_rpa else []
    scripts_present = has_scripts(game)
    mode_used = scan.scan_mode if scan.scan_mode in ("scripts", "all") else ("scripts" if scripts_present else "all")
    if mode_used == "scripts":
        refs, vrefs = collect_refs(game, run.out_name)
    else:
        refs, vrefs = collect_all_files(game, load_rpas(game) if scan.use_rpa else [], run.out_name)
    # Vidéos : citations dans les scripts + dossiers movies/ videos/ video/ (+ entrées .rpa)
    vrefs = sorted(set(vrefs) | set(collect_video_dirs(game, rpas, run.out_name)))
    plan = Plan(total_refs=len(refs), video_refs=len(vrefs), scan_mode_used=mode_used)
    if not scripts_present:
        plan.notes.append(T("core.note.no_rpy"))
    video_sources: dict[str, Source] = {}
    for rel in vrefs:
        src = resolve_source(game, rel, rpas)
        if src is not None:
            video_sources[rel] = src
            plan.video_bytes += src.size
    if video.enabled and video_sources:
        plan.video_infos = probe_videos(video_sources)
        plan.video_duration = sum(i.duration for i in plan.video_infos.values())
    if run.mode == MODE_AT2 and not supports_at2(info.renpy_version):
        plan.notes.append(T("core.note.at2_version", version=info.renpy_version))
    replace_mode = run.mode == MODE_REPLACE
    at2_mode = run.mode == MODE_AT2
    backup_root = game / BACKUP_DIR
    exts = tuple(e.lower() for e in scan.extensions)

    for rel in refs:
        ext = Path(rel).suffix.lower()
        if ext not in FORMAT_BY_EXT or ext not in exts or not _passes_filters(rel, scan):
            plan.filtered_out += 1
            continue
        src_fmt = FORMAT_BY_EXT[ext]
        source = resolve_source(game, rel, rpas)
        if source is None:
            plan.missing.append(rel)
            continue
        backup = None
        if replace_mode:
            out_fmt = src_fmt
            if source.path is not None:
                dest = source.path
                backup = backup_root / source.path.relative_to(game)
            else:
                # Image uniquement dans le .rpa : on écrit un fichier libre (prioritaire pour Ren'Py).
                dest = game / source.rpa_key  # type: ignore[arg-type]
            done = backup.is_file() if backup else dest.is_file()
        elif at2_mode:
            out_fmt = src_fmt
            dest = _at2_dest(source.path if source.path is not None else game / source.rpa_key)  # type: ignore[arg-type]
            done = dest.is_file()
        else:
            out_fmt = dlss.output_format_for(src_fmt)
            dest = _dest_with_format(out_dir, rel, out_fmt)
            done = dest.is_file()
        if done and not scan.overwrite:
            plan.already_done += 1
            continue
        check = _dims_check(source, scan)
        if check == "small":
            plan.too_small += 1
            continue
        if check == "large":
            plan.filtered_out += 1
            continue
        plan.jobs.append(Job(rel, source, dest, src_fmt, out_fmt, backup))

    if video.enabled and not at2_mode:
        video.validate()
        requested = 1.0 if replace_mode else float(dlss.factor)
        ext = video.container_ext()
        codec_label = VIDEO_CODECS[video.codec][0].split(" (")[0]
        plan_counts: dict[tuple[str, str, float], int] = {}
        for rel in vrefs:
            if not _passes_filters(rel, scan):
                plan.filtered_out += 1
                continue
            source = video_sources.get(rel)
            if source is None:
                plan.missing.append(rel)
                continue
            vinfo = plan.video_infos.get(rel) or VideoInfo(error="non sondé")
            if vinfo.error and not vinfo.ok:
                plan.video_skipped.append(f"{rel} (illisible par ffprobe : {vinfo.error})")
                continue
            factor = choose_video_factor(vinfo.width, vinfo.height, requested, video)
            if factor is None:
                plan.video_skipped.append(f"{rel} ({vinfo.width}×{vinfo.height} : au-delà du plafond {video.max_width}×{video.max_height})")
                continue
            if replace_mode:
                if Path(rel).suffix.lower() != ext:
                    plan.video_skipped.append(f"{rel} (remplacement : le codec choisi produit du {ext}, la source est en {Path(rel).suffix})")
                    continue
                dest = source.path if source.path is not None else game / source.rpa_key  # type: ignore[operator]
                backup = (backup_root / source.path.relative_to(game)) if source.path is not None else None
                done = backup.is_file() if backup else dest.is_file()
            else:
                dest = (out_dir / rel).with_suffix(ext)
                backup = None
                done = dest.is_file()
            if done and not scan.overwrite:
                plan.already_done += 1
                continue
            ow, oh = video_output_size(vinfo.width, vinfo.height, factor)
            key = (f"{vinfo.width}×{vinfo.height}", f"{ow}×{oh}", factor)
            plan_counts[key] = plan_counts.get(key, 0) + 1
            plan.video_frames_todo += vinfo.frames
            plan.jobs.append(Job(rel, source, dest, "VIDEO", codec_label, backup, is_video=True, video_info=vinfo, video_factor=factor))
        for (src_res, out_res, factor), n in sorted(plan_counts.items(), key=lambda kv: -kv[1]):
            tag = "1× DLAA" if factor == 1.0 else f"{factor:g}×"
            plan.video_plan_lines.append(f"{n} × {src_res} → {out_res} ({tag})")

    if scan.limit:
        plan.jobs = plan.jobs[: scan.limit]
    _estimate(plan, 1.0 if replace_mode else (2.0 if at2_mode else dlss.factor))
    return info, plan


# ----------------------------------------------------------------------------
# Vidéos : sondage ffprobe, choix du facteur, encodeurs
# ----------------------------------------------------------------------------
_PROBE_CACHE: dict[str, dict] = {}
_PROBE_LOCK = threading.Lock()
_ENCODERS: set[str] | None = None
_HW_OK: dict[str, bool] = {}


def _ffmpeg_paths() -> tuple[Path, Path]:
    """ffmpeg / ffprobe de l'outil (sans importer src.runtime, qui exige le GPU)."""
    root = Path(_TOOL["root"]) if _TOOL else None
    for cand in ([root] if root else []) + [APP_DIR.parent / "DLSS5", Path.cwd()]:
        ff = cand / "bin" / "ffmpeg" / "bin" / "ffmpeg.exe"
        if ff.is_file():
            return ff, ff.with_name("ffprobe.exe")
    raise RuntimeError("ffmpeg.exe introuvable (DLSS5\\bin\\ffmpeg\\bin).")


def _load_probe_cache() -> None:
    if _PROBE_CACHE or not VIDEO_PROBE_CACHE.is_file():
        return
    try:
        _PROBE_CACHE.update(json.loads(VIDEO_PROBE_CACHE.read_text(encoding="utf-8")))
    except Exception:
        pass


def probe_video_file(path: Path) -> VideoInfo:
    """Dimensions, cadence, durée, nombre d'images et piste audio d'un fichier vidéo (ffprobe, mis en cache)."""
    _load_probe_cache()
    try:
        st = path.stat()
        key = f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    except OSError as exc:
        return VideoInfo(error=str(exc))
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(key)
    if cached:
        return VideoInfo(**cached)
    try:
        _ff, ffprobe = _ffmpeg_paths()
        cmd = [str(ffprobe), "-v", "error", "-show_entries",
               "stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,nb_frames,duration:format=duration,size",
               "-of", "json", str(path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode:
            return VideoInfo(error=(proc.stderr.strip().splitlines() or ["ffprobe"])[-1][:200])
        data = json.loads(proc.stdout or "{}")
        info = VideoInfo(size=int((data.get("format") or {}).get("size") or st.st_size))
        video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
        if video is None:
            return VideoInfo(error="aucun flux vidéo")
        info.width, info.height = int(video.get("width") or 0), int(video.get("height") or 0)
        info.codec = str(video.get("codec_name") or "")
        rate = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")
        num, _, den = rate.partition("/")
        info.fps = float(num) / float(den or 1) if float(den or 1) else 0.0
        info.duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0.0)
        info.frames = int(video.get("nb_frames") or 0) or int(round(info.duration * info.fps))
        info.audio_codec = str(audio.get("codec_name") or "") if audio else ""
    except Exception as exc:
        return VideoInfo(error=str(exc)[:200])
    with _PROBE_LOCK:
        _PROBE_CACHE[key] = asdict(info)
        try:
            VIDEO_PROBE_CACHE.write_text(json.dumps(_PROBE_CACHE), encoding="utf-8")
        except OSError:
            pass
    return info


def probe_videos(sources: dict[str, Source], workers: int = 6) -> dict[str, VideoInfo]:
    """Sonde en parallèle les fichiers libres ; les entrées .rpa ne sont pas sondées (facteur demandé appliqué tel quel)."""
    from concurrent.futures import ThreadPoolExecutor

    result: dict[str, VideoInfo] = {}
    loose = {rel: s.path for rel, s in sources.items() if s.path is not None}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for rel, info in zip(loose, pool.map(probe_video_file, loose.values())):
            result[rel] = info
    for rel, s in sources.items():
        if s.path is None:
            result[rel] = VideoInfo(size=s.size, error="")   # dimensions inconnues (archive .rpa)
    return result


def _even(v: float) -> int:
    return max(2, int(v / 2.0 + 0.5) * 2)


def video_output_size(w: int, h: int, factor: float) -> tuple[int, int]:
    return _even(w * factor), _even(h * factor)


def choose_video_factor(w: int, h: int, requested: float, video: VideoSettings) -> float | None:
    """Plus grand facteur DLSS ≤ demandé dont la sortie tient dans le plafond ; None si la vidéo doit être ignorée."""
    if not w or not h:
        return float(requested)
    max_w = min(int(video.max_width), 7680)
    max_h = min(int(video.max_height), 4320)
    for f in sorted((f for f in UPSCALING_FACTORS if f <= float(requested) + 1e-9), reverse=True):
        ow, oh = video_output_size(w, h, f)
        if ow <= max_w and oh <= max_h and max(ow, oh) <= 7680 and min(ow, oh) <= 4320:
            return f
    if video.over_cap == "reduce" and _even(w) <= 7680 and _even(h) <= 4320:
        return 1.0
    return None


def available_encoders() -> set[str]:
    global _ENCODERS
    if _ENCODERS is None:
        try:
            ff, _ = _ffmpeg_paths()
            out = subprocess.run([str(ff), "-hide_banner", "-encoders"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace").stdout
            _ENCODERS = set(re.findall(r"^\s*[VAS][A-Z.]{5}\s+(\S+)", out, re.MULTILINE))
        except Exception:
            _ENCODERS = set()
    return _ENCODERS


def hw_encoder_ok(name: str) -> bool:
    """Vérifie qu'un encodeur NVENC fonctionne vraiment (test d'une image 256×256)."""
    if name not in _HW_OK:
        ok = False
        if name in available_encoders():
            try:
                ff, _ = _ffmpeg_paths()
                ok = subprocess.run([str(ff), "-v", "error", "-f", "lavfi", "-i", "color=size=256x256:rate=1", "-frames:v", "1",
                                     "-c:v", name, "-f", "null", "-"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            except Exception:
                ok = False
        _HW_OK[name] = ok
    return _HW_OK[name]


def video_encoder_args(video: VideoSettings, w: int, h: int, fps: float) -> tuple[list[str], str]:
    """Arguments ffmpeg de l'encodeur final (+ nom de l'encodeur retenu)."""
    crf = int(video.crf)
    speed = max(0, min(5, int(video.speed)))
    if video.codec == "vp9":
        return (["-c:v", "libvpx-vp9", "-crf", str(crf), "-b:v", "0", "-row-mt", "1", "-tile-columns", "2",
                 "-deadline", "good", "-cpu-used", str(speed), "-pix_fmt", "yuv420p"], "libvpx-vp9")
    if video.codec == "vp8":
        kbps = max(500, int(w * h * max(fps, 1.0) * 0.12 / 1000))   # VP8 exige un débit cible ; CRF le module
        return (["-c:v", "libvpx", "-crf", str(crf), "-b:v", f"{kbps}k", "-deadline", "good", "-cpu-used", str(speed),
                 "-auto-alt-ref", "1", "-pix_fmt", "yuv420p"], "libvpx")
    if video.codec == "av1":
        if video.hw_encode and hw_encoder_ok("av1_nvenc"):
            return (["-c:v", "av1_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(min(crf, 51)), "-b:v", "0",
                     "-pix_fmt", "yuv420p"], "av1_nvenc")
        return (["-c:v", "libsvtav1", "-crf", str(crf), "-preset", str(min(13, 4 + 2 * speed)), "-pix_fmt", "yuv420p"], "libsvtav1")
    if video.codec == "h264":
        if video.hw_encode and hw_encoder_ok("h264_nvenc") and w <= 4096 and h <= 4096:
            return (["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(min(crf, 51)), "-b:v", "0",
                     "-pix_fmt", "yuv420p", "-movflags", "+faststart"], "h264_nvenc")
        preset = ("veryslow", "slower", "slow", "medium", "fast", "veryfast")[speed]
        return (["-c:v", "libx264", "-crf", str(min(crf, 51)), "-preset", preset, "-pix_fmt", "yuv420p", "-movflags", "+faststart"], "libx264")
    raise ValueError(f"Codec vidéo inconnu : {video.codec}")


def video_audio_args(video: VideoSettings, audio_codec: str) -> list[str]:
    if not video.keep_audio or not audio_codec:
        return ["-an"]
    container = VIDEO_CODECS[video.codec][1]
    copyable = {"webm": ("opus", "vorbis"), "mp4": ("aac", "mp3", "ac3", "alac")}[container]
    if audio_codec in copyable:
        return ["-map", "1:a:0", "-c:a", "copy"]
    if container == "webm":
        return ["-map", "1:a:0", "-c:a", "libopus", "-b:a", f"{int(video.audio_kbps)}k"]
    return ["-map", "1:a:0", "-c:a", "aac", "-b:a", f"{int(video.audio_kbps)}k"]


def video_duration(path: Path) -> float:
    return probe_video_file(Path(path)).duration


def extract_frame(path: Path, t: float, dest: Path) -> Path | None:
    """Extrait l'image affichée à l'instant t (PNG). Renvoie None en cas d'échec."""
    ff, _ = _ffmpeg_paths()
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(ff), "-y", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1", "-an", str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return dest if proc.returncode == 0 and dest.is_file() else None


def trim_copy(path: Path, seconds: float, dest: Path) -> Path:
    """Copie sans réencodage des N premières secondes (coupe à la dernière image-clé)."""
    ff, _ = _ffmpeg_paths()
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(ff), "-y", "-loglevel", "error", "-i", str(path), "-t", f"{seconds:.3f}", "-c", "copy", str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode:
        raise RuntimeError(f"ffmpeg (copie tronquée) : {proc.stderr.strip()[-300:]}")
    return dest


def load_video_manifest(out_dir: Path) -> dict:
    f = out_dir / VIDEO_MANIFEST
    if f.is_file():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("videos"), dict):
                return data
        except Exception:
            pass
    return {"version": 1, "videos": {}}


def save_video_manifest(out_dir: Path, manifest: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / (VIDEO_MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out_dir / VIDEO_MANIFEST)


def effective_factor(mode: str, factor: float) -> float:
    if mode == MODE_REPLACE:
        return 1.0
    if mode == MODE_AT2:
        return 2.0
    return float(factor)


def build_folder_plan(run: RunSettings, scan: ScanSettings, dlss: DlssSettings) -> Plan:
    """Plan pour le mode « dossier d'images libre » (hors jeu Ren'Py)."""
    src_dir = Path(run.input_dir).expanduser()
    dst_dir = Path(run.output_dir).expanduser()
    if not src_dir.is_dir():
        raise ValueError(T("core.err.src_dir", dir=src_dir))
    if not str(run.output_dir).strip():
        raise ValueError(T("core.err.out_dir"))
    if dst_dir.resolve() == src_dir.resolve():
        raise ValueError(T("core.err.same_dir"))
    plan = Plan()
    exts = tuple(e.lower() for e in scan.extensions)
    for p in sorted(src_dir.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in FORMAT_BY_EXT or ext not in exts:
            continue
        try:
            if dst_dir.resolve() in p.resolve().parents:
                continue
        except OSError:
            pass
        rel = p.relative_to(src_dir).as_posix()
        plan.total_refs += 1
        if not _passes_filters(rel, scan):
            plan.filtered_out += 1
            continue
        src_fmt = FORMAT_BY_EXT[ext]
        out_fmt = dlss.output_format_for(src_fmt)
        dest = _dest_with_format(dst_dir, rel, out_fmt)
        if dest.is_file() and not scan.overwrite:
            plan.already_done += 1
            continue
        source = Source(path=p)
        check = _dims_check(source, scan)
        if check == "small":
            plan.too_small += 1
            continue
        if check == "large":
            plan.filtered_out += 1
            continue
        plan.jobs.append(Job(rel, source, dest, src_fmt, out_fmt))
    if scan.limit:
        plan.jobs = plan.jobs[: scan.limit]
    _estimate(plan, dlss.factor)
    return plan


def _estimate(plan: Plan, factor: float) -> None:
    plan.source_bytes = sum(j.source.size for j in plan.jobs)
    plan.estimated_output_bytes = int(sum(
        j.source.size * (j.video_factor if j.is_video else factor) ** 2 for j in plan.jobs))


def human_size(n: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if n < 1024 or unit == "To":
            return f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} To"


# ----------------------------------------------------------------------------
# Hook Ren'Py, désinstallation, restauration
# ----------------------------------------------------------------------------
def render_hook(out_name: str, cache_mb: int) -> str:
    text = HOOK_TEMPLATE.read_text(encoding="utf-8")
    text = re.sub(r'(_dlss_hd_dir\s*=\s*)"[^"]*"(\s*# RENPYHD:DIR)', rf'\g<1>"{out_name.strip("/")}/"\g<2>', text)
    text = re.sub(r"(_dlss_hd_cache_mb\s*=\s*)\d+(\s*# RENPYHD:CACHE)", rf"\g<1>{int(cache_mb)}\g<2>", text)
    return text


def install_hook(game: Path, out_name: str, cache_mb: int) -> Path:
    target = game / HOOK_NAME
    target.write_text(render_hook(out_name, cache_mb), encoding="utf-8")
    rpyc = target.with_suffix(".rpyc")
    if rpyc.exists():
        rpyc.unlink()  # Ren'Py recompilera le .rpy
    return target


def uninstall_mod(game_root: str, out_name: str) -> list[str]:
    game = find_game_dir(game_root)
    done = []
    out_dir = game / out_name
    if out_dir.is_dir():
        shutil.rmtree(out_dir)
        done.append(f"Dossier supprimé : {out_dir}")
    for name in (HOOK_NAME, HOOK_NAME + "c"):
        f = game / name
        if f.exists():
            f.unlink()
            done.append(f"Fichier supprimé : {f}")
    at2 = game / AT2_MANIFEST
    if at2.is_file():
        removed = 0
        try:
            for rel in json.loads(at2.read_text(encoding="utf-8")).get("files", []):
                f = game / rel
                if f.is_file() and "@2" in f.stem:
                    f.unlink()
                    removed += 1
        except Exception as exc:
            done.append(f"Manifeste @2 illisible : {exc}")
        at2.unlink()
        done.append(f"{removed} fichier(s) @2 supprimé(s) (d'après {AT2_MANIFEST}).")
    return done or ["Rien à désinstaller (aucun dossier HD, hook ni fichier @2 trouvé)."]


def _load_at2_manifest(game: Path) -> dict:
    f = game / AT2_MANIFEST
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"files": []}


def _save_at2_manifest(game: Path, manifest: dict) -> None:
    manifest["files"] = sorted(set(manifest["files"]))
    (game / AT2_MANIFEST).write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")


def _load_backup_manifest(backup_root: Path) -> dict:
    f = backup_root / BACKUP_MANIFEST
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"files": {}}


def _save_backup_manifest(backup_root: Path, manifest: dict) -> None:
    backup_root.mkdir(parents=True, exist_ok=True)
    (backup_root / BACKUP_MANIFEST).write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")


def restore_originals(game_root: str) -> list[str]:
    game = find_game_dir(game_root)
    backup_root = game / BACKUP_DIR
    if not backup_root.is_dir():
        return ["Aucune sauvegarde (_dlss_backup) trouvée."]
    manifest = _load_backup_manifest(backup_root)
    restored = deleted = 0
    # 1) fichiers extraits d'un .rpa : on supprime le fichier libre créé
    for rel, kind in list(manifest.get("files", {}).items()):
        if kind == "rpa":
            f = game / rel
            if f.is_file():
                f.unlink()
                deleted += 1
    # 2) sauvegardes : on recopie
    for f in backup_root.rglob("*"):
        if not f.is_file() or f.name == BACKUP_MANIFEST:
            continue
        rel = f.relative_to(backup_root)
        target = game / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        restored += 1
    shutil.rmtree(backup_root, ignore_errors=True)
    return [f"{restored} fichier(s) restauré(s), {deleted} fichier(s) extraits d'archive supprimé(s).",
            f"Sauvegarde supprimée : {backup_root}"]


# ----------------------------------------------------------------------------
# Chargement de l'outil DLSS 5
# ----------------------------------------------------------------------------
_TOOL: dict[str, object] = {}


def load_tool(tool_root: Path) -> dict[str, object]:
    """Importe src.images / src.video / src.runtime de l'outil, sans zip ni rapports JSON."""
    if _TOOL:
        return _TOOL
    tool_root = Path(tool_root).resolve()
    if not (tool_root / "app.py").is_file() or not (tool_root / "src").is_dir():
        raise RuntimeError(T("core.err.tool_missing", root=tool_root))
    if str(tool_root) not in sys.path:
        sys.path.insert(0, str(tool_root))
    os.chdir(tool_root)  # l'outil résout bin/, outputs/, logs/ relativement à sa racine
    from src import images as images_mod  # noqa: E402
    from src import runtime as runtime_mod  # noqa: E402
    from src import video as video_mod  # noqa: E402
    from src import prepare as prepare_mod  # noqa: E402

    images_mod._build_manifest_and_zip = lambda stamp, options, successes, failures, cancelled: ("", None)
    images_mod._write_report = lambda *a, **k: ""
    _TOOL.update(images=images_mod, runtime=runtime_mod, video=video_mod, prepare=prepare_mod, root=tool_root)
    return _TOOL


def check_runtime(tool_root: Path) -> str:
    """Vérifie GPU + fichiers du runtime sans lancer de rendu. Renvoie le nom du GPU."""
    tool = load_tool(tool_root)
    prepared = tool["prepare"].prepare_runtime()  # type: ignore[attr-defined]
    return str(prepared.gpu["display_name"])


# ----------------------------------------------------------------------------
# Exécution
# ----------------------------------------------------------------------------
@dataclass
class Progress:
    total: int = 0
    done: int = 0
    failed: int = 0
    current: str = ""
    started: float = field(default_factory=time.time)
    chunk_index: int = 0
    chunk_count: int = 0
    chunk_fraction: float = 0.0   # avancement dans le lot courant (0..1), mis à jour à chaque image
    chunk_base: int = 0           # images terminées avant le lot courant
    chunk_size: int = 0           # taille du lot courant
    # --- phase vidéo -------------------------------------------------------
    phase: str = "images"         # "images" | "video"
    video_index: int = 0          # vidéo en cours (1..video_count)
    video_count: int = 0
    video_name: str = ""
    video_stage: str = ""         # "DLSS" | "encodage" | "audio"
    frame: int = 0                # image DLSS courante dans la vidéo
    frame_total: int = 0
    frames_done_all: int = 0      # images DLSS rendues dans les vidéos précédentes
    frames_total_all: int = 0     # images DLSS de toutes les vidéos du plan
    video_fps: float = 0.0        # cadence DLSS mesurée (images/s) sur la vidéo en cours
    video_started: float = 0.0    # début du rendu DLSS de la vidéo en cours
    video_phase_started: float = 0.0
    video_dlss_seconds: float = 0.0     # cumul (vidéos terminées) du temps DLSS
    video_encode_seconds: float = 0.0   # cumul du temps d'encodage final

    @property
    def live_done(self) -> int:
        """Nombre d'images traitées, estimé image par image (et non par lot)."""
        return min(self.total, max(self.done + self.failed, self.chunk_base + int(self.chunk_fraction * self.chunk_size)))

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    @property
    def rate(self) -> float:
        return self.live_done / self.elapsed if self.elapsed > 0 and self.live_done else 0.0

    @property
    def video_frames_live(self) -> int:
        return self.frames_done_all + (self.frame if self.video_stage == "DLSS" else self.frame_total)

    @property
    def video_eta_seconds(self) -> float:
        """Temps restant estimé de la phase vidéo : images DLSS restantes / cadence mesurée, + encodage au prorata."""
        remaining = max(0, self.frames_total_all - self.video_frames_live)
        fps = self.video_fps
        if fps <= 0 and self.video_dlss_seconds > 0 and self.frames_done_all:
            fps = self.frames_done_all / self.video_dlss_seconds
        if fps <= 0:
            return 0.0
        ratio = (self.video_encode_seconds / self.video_dlss_seconds) if self.video_dlss_seconds > 0 else 0.5
        eta = remaining / fps * (1.0 + ratio)
        if self.video_stage != "DLSS" and self.frame_total:
            eta += (self.frame_total / fps) * ratio * max(0.0, 1.0 - self.chunk_fraction)
        return eta

    @property
    def eta_seconds(self) -> float:
        if self.phase == "video":
            return self.video_eta_seconds
        remaining = self.total - self.live_done
        return remaining / self.rate if self.rate else 0.0

    def copy(self) -> "Progress":
        return replace(self)


@dataclass
class RunSummary:
    written: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False
    elapsed: float = 0.0
    output_dir: str = ""
    messages: list[str] = field(default_factory=list)
    timings: list[tuple[str, float]] = field(default_factory=list)   # (chemin, secondes) par image réussie
    outputs: list[tuple[str, str]] = field(default_factory=list)     # (chemin relatif, fichier produit)


def format_eta(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h} h {m:02d} min" if h else (f"{m} min {s:02d} s" if m else f"{s} s")


def _image_options(images_mod, dlss: DlssSettings, out_fmt: str, factor: float):
    return images_mod.ImageConversionOptions(
        nr_style=dlss.nr_style,
        nr_intensity=float(dlss.nr_intensity),
        local_tone_strength=float(dlss.local_tone),
        local_structure_strength=float(dlss.local_structure),
        skin_structure_strength=float(dlss.skin_structure),
        upscaling_factor=float(factor),
        output_format=out_fmt,
        quality=int(dlss.quality),
        preserve_metadata=bool(dlss.preserve_metadata),
        warmup_frames=int(dlss.warmup_frames),
        nr_preset=dlss.nr_preset,
        automatic_mask=bool(dlss.automatic_mask),
        rename_mode="Auto",
        dlss_model_preset=dlss.dlss_model_preset,
    )


def _commit_output(job: Job, produced: Path, game: Path | None, manifest: dict | None) -> None:
    """Place le fichier produit à sa destination (sauvegarde préalable en mode remplacement)."""
    if job.backup is not None:
        if not job.backup.is_file():
            job.backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(job.dest, job.backup)
        if manifest is not None and game is not None:
            manifest["files"][job.dest.relative_to(game).as_posix()] = "backup"
    elif manifest is not None and game is not None and job.source.from_rpa:
        manifest["files"][job.dest.relative_to(game).as_posix()] = "rpa"
    job.dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = job.dest.with_name(f".{job.dest.name}.renpyhd.tmp")
    shutil.move(str(produced), str(tmp))
    os.replace(tmp, job.dest)


def run_plan(
    plan: Plan,
    run: RunSettings,
    dlss: DlssSettings,
    video: VideoSettings,
    tool_root: Path,
    log: Callable[[str], None],
    on_progress: Callable[[Progress], None],
    cancel: threading.Event,
    retry_failed: bool = True,
) -> RunSummary:
    """Exécute un plan. Reprenable : relancer le même plan saute ce qui existe déjà. Un échec n'arrête jamais le lot."""
    summary = RunSummary()
    t0 = time.time()
    replace_mode = run.mode == MODE_REPLACE
    at2_mode = run.mode == MODE_AT2
    factor = effective_factor(run.mode, dlss.factor)
    dlss.validate(renpy_only=run.mode != MODE_FOLDER)
    game: Path | None = None
    manifest: dict | None = None
    at2_manifest: dict | None = None
    out_dir: Path
    if run.mode == MODE_FOLDER:
        out_dir = Path(run.output_dir)
    else:
        game = find_game_dir(run.game_root)
        out_dir = game / run.out_name
    summary.output_dir = str(out_dir if run.mode in (MODE_HD, MODE_FOLDER) else game)

    if not plan.jobs:
        summary.messages.append(T("core.msg.nothing"))
        if run.mode == MODE_HD and run.install_hook and game is not None and out_dir.is_dir():
            install_hook(game, run.out_name, run.cache_mb)
            summary.messages.append(f"Hook {HOOK_NAME} (ré)installé dans {game}.")
        summary.elapsed = time.time() - t0
        return summary

    tool = load_tool(tool_root)
    images_mod = tool["images"]
    runtime_mod = tool["runtime"]
    tmp = Path(tool_root) / "outputs" / "renpyhd_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    if replace_mode and game is not None:
        manifest = _load_backup_manifest(game / BACKUP_DIR)
    if at2_mode and game is not None:
        at2_manifest = _load_at2_manifest(game)
    if run.mode in (MODE_HD, MODE_FOLDER):
        out_dir.mkdir(parents=True, exist_ok=True)
        if run.mode == MODE_HD:
            (out_dir / FACTOR_FILE).write_text(f"{factor}\n", encoding="utf-8")

    image_jobs = [j for j in plan.jobs if not j.is_video]
    video_jobs = [j for j in plan.jobs if j.is_video]
    progress = Progress(total=len(plan.jobs))
    chunk_size = max(1, int(run.chunk))

    # Un appel convert_images = un seul format de sortie : on groupe par format.
    groups: dict[str, list[Job]] = {}
    for j in image_jobs:
        groups.setdefault(j.output_format, []).append(j)
    chunks: list[tuple[str, list[Job]]] = []
    for fmt, jobs in groups.items():
        for i in range(0, len(jobs), chunk_size):
            chunks.append((fmt, jobs[i : i + chunk_size]))
    progress.chunk_count = len(chunks) + (1 if video_jobs else 0)

    last_tick = [0.0]

    def tool_progress(frac: float, msg: str) -> None:
        progress.chunk_fraction = frac
        progress.current = msg
        now = time.time()
        if now - last_tick[0] > 0.5:
            last_tick[0] = now
            on_progress(progress)

    try:
        for idx, (fmt, chunk) in enumerate(chunks, 1):
            if cancel.is_set():
                summary.cancelled = True
                break
            progress.chunk_index = idx
            log(f"[Lot {idx}/{progress.chunk_count}] {len(chunk)} image(s) → {fmt} (facteur {factor}×)")
            on_progress(progress)
            paths: list[Path] = []
            by_path: dict[str, Job] = {}
            for job in chunk:
                try:
                    p = job.source.materialize(tmp)
                except Exception as exc:
                    summary.failed.append((job.rel, f"extraction impossible : {exc}"))
                    progress.failed += 1
                    continue
                paths.append(p)
                by_path[str(p.resolve()).casefold()] = job
            if not paths:
                continue
            progress.chunk_base = progress.done + progress.failed
            progress.chunk_size = len(paths)
            progress.chunk_fraction = 0.0
            opts = _image_options(images_mod, dlss, fmt, factor)
            try:
                result = images_mod.convert_images(paths, opts, tool_progress)
            except Exception as exc:
                # Un échec global du lot (runtime, GPU) ne doit pas arrêter le reste : on note et on continue.
                first_line = str(exc).splitlines()[0] if str(exc) else "erreur inconnue"
                log(f"  ÉCHEC du lot : {first_line}")
                for job in chunk:
                    summary.failed.append((job.rel, first_line))
                    progress.failed += 1
                if cancel.is_set():
                    summary.cancelled = True
                    break
                continue
            retry: list[Job] = []
            for ok in result.successes:
                job = by_path.get(str(Path(ok.input_path).resolve()).casefold())
                if job is None:
                    log(f"  ! sortie non associée : {ok.input_path}")
                    continue
                try:
                    _commit_output(job, Path(ok.output_path), game, manifest)
                    if at2_manifest is not None and game is not None:
                        at2_manifest["files"].append(job.dest.relative_to(game).as_posix())
                    summary.written += 1
                    summary.timings.append((job.rel, float(ok.elapsed_seconds)))
                    summary.outputs.append((job.rel, str(job.dest)))
                    progress.done += 1
                except Exception as exc:
                    summary.failed.append((job.rel, f"écriture impossible : {exc}"))
                    progress.failed += 1
            for fail in result.failures:
                job = by_path.get(str(Path(fail.input_path).resolve()).casefold())
                rel = job.rel if job else fail.input_path
                first_line = str(fail.error).splitlines()[0] if fail.error else "erreur inconnue"
                if result.cancelled and "Cancelled" in first_line:
                    continue
                if job is not None and retry_failed and "at least 64 pixels" not in first_line and not result.cancelled:
                    retry.append(job)
                    continue
                summary.failed.append((rel, first_line))
                progress.failed += 1
                log(f"  ÉCHEC {rel} : {first_line}")
            if result.zip_path and Path(result.zip_path).exists():
                Path(result.zip_path).unlink()
            # Seconde chance : les images échouées du lot sont retentées une par une (session DLSS dédiée).
            for job in retry:
                if cancel.is_set():
                    break
                progress.current = f"Nouvel essai : {job.rel}"
                on_progress(progress)
                try:
                    p = job.source.materialize(tmp)
                    single = images_mod.convert_images([p], opts, None)
                    if single.successes:
                        _commit_output(job, Path(single.successes[0].output_path), game, manifest)
                        if at2_manifest is not None and game is not None:
                            at2_manifest["files"].append(job.dest.relative_to(game).as_posix())
                        summary.written += 1
                        summary.timings.append((job.rel, float(single.successes[0].elapsed_seconds)))
                        summary.outputs.append((job.rel, str(job.dest)))
                        progress.done += 1
                        log(f"  OK au 2e essai : {job.rel}")
                        continue
                    err = single.failures[0].error if single.failures else "erreur inconnue"
                except Exception as exc:
                    err = str(exc)
                first_line = str(err).splitlines()[0] if err else "erreur inconnue"
                summary.failed.append((job.rel, first_line))
                progress.failed += 1
                log(f"  ÉCHEC {job.rel} : {first_line}")
            if manifest is not None and game is not None:
                _save_backup_manifest(game / BACKUP_DIR, manifest)
            if at2_manifest is not None and game is not None:
                _save_at2_manifest(game, at2_manifest)
            # nettoyage des extractions .rpa de ce lot
            for job in chunk:
                if job.source.from_rpa:
                    try:
                        (tmp / job.source.rpa_key).unlink(missing_ok=True)  # type: ignore[operator]
                    except OSError:
                        pass
            progress.chunk_base = progress.done + progress.failed
            progress.chunk_fraction = 0.0
            on_progress(progress)
            log(f"  {progress.done}/{progress.total} faites, {progress.failed} échec(s), "
                f"{progress.rate:.2f} img/s, reste ≈ {format_eta(progress.eta_seconds)}")
            if result.cancelled or cancel.is_set():
                summary.cancelled = True
                break

        if video_jobs and not summary.cancelled:
            progress.chunk_index = progress.chunk_count
            _run_videos(video_jobs, tool, dlss, video, factor, tmp, game, manifest, summary, progress, log, on_progress, cancel)
            if manifest is not None and game is not None:
                _save_backup_manifest(game / BACKUP_DIR, manifest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if run.mode == MODE_HD and run.install_hook and game is not None and not summary.cancelled:
        install_hook(game, run.out_name, run.cache_mb)
        summary.messages.append(f"Hook {HOOK_NAME} installé dans {game} (dossier {run.out_name}/, cache {run.cache_mb} Mo).")
    elif run.mode == MODE_HD and run.install_hook and game is not None and summary.cancelled and summary.written:
        install_hook(game, run.out_name, run.cache_mb)
        summary.messages.append(f"Hook {HOOK_NAME} installé (traitement partiel : relancez pour reprendre).")
    summary.elapsed = time.time() - t0
    return summary


def _install_scene_reset(video_mod, enabled: bool) -> None:
    """Le générateur de guides de l'outil réinitialise DLSS quand la différence inter-images dépasse 0,24 :
    on remplace la classe par une sous-classe au seuil réglable (désactivé = jamais)."""
    import cv2
    import numpy as np

    base = getattr(video_mod, "_renpyhd_base_guides", None) or video_mod.TemporalGuideGenerator
    video_mod._renpyhd_base_guides = base
    threshold = 0.24 if enabled else 9.0

    class Guides(base):  # type: ignore[misc, valid-type]
        def process(self, rgba):
            current = self._small_gray(rgba)
            if self.previous_gray is None:
                motion, reset, score = self.zero_motion, True, 1.0
            else:
                score = float(np.mean(cv2.absdiff(current, self.previous_gray))) / 255.0
                reset = score > threshold
                if reset:
                    motion = self.zero_motion
                else:
                    motion = self.dis.calc(current, self.previous_gray, None)
                    motion = cv2.resize(motion, (self.width, self.height), interpolation=cv2.INTER_LINEAR)
                    motion[..., 0] *= self.width / self.flow_width
                    motion[..., 1] *= self.height / self.flow_height
                    motion = np.ascontiguousarray(motion.astype(np.float16))
            self.previous_gray = current
            return video_mod.GuideFrame(motion=motion, reset=reset, scene_score=score)

    video_mod.TemporalGuideGenerator = Guides


def _install_intermediate_encoder(video_mod, hw_encode: bool) -> str:
    """L'outil encode son intermédiaire en H.264/HEVC NVENC, sinon en libx264 « slow » CRF 0 (très lent en 4K).
    Sans NVENC utilisable (pilote trop ancien pour cette version de FFmpeg), on impose un x264 ultrafast
    quasi sans perte : l'intermédiaire est de toute façon réencodé ensuite. Renvoie le nom retenu."""
    ffm = video_mod.ffmpeg
    orig = getattr(ffm, "_renpyhd_orig_codec_command", None) or ffm._codec_command
    ffm._renpyhd_orig_codec_command = orig
    use_nvenc = bool(hw_encode) and hw_encoder_ok("h264_nvenc")
    if use_nvenc:
        ffm._codec_command = orig
        return "NVENC"

    def fast_software(codec, quality_name, width, height, fps):
        if codec not in ("H.264", "HEVC"):
            return orig(codec, quality_name, width, height, fps)
        quality = ffm.resolve_encoding_quality(quality_name, codec, width, height, fps)
        if codec == "HEVC" or width > 4096 or height > 4096:   # x264 plafonne à 8192×4320 (niveau 6.2), x265 monte plus haut
            return (["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "qp=4:log-level=error", "-pix_fmt", "yuv420p"],
                    "libx265 ultrafast qp4", quality)
        return (["-c:v", "libx264", "-preset", "ultrafast", "-qp", "4", "-pix_fmt", "yuv420p"], "libx264 ultrafast qp4", quality)

    ffm._codec_command = fast_software
    return "libx264/libx265 ultrafast (NVENC indisponible)"


_FRAME_RE = re.compile(r"frame (\d+)/(\d+)")


def _final_encode(ffmpeg: Path, inter: Path, src: Path, dest_tmp: Path, job: Job, video: VideoSettings, vinfo: VideoInfo,
                  out_w: int, out_h: int, progress: Progress, on_progress, cancel: threading.Event) -> str:
    """Réencode l'intermédiaire pour Ren'Py (audio d'origine copié ou réencodé). Renvoie le nom de l'encodeur."""
    enc_args, enc_name = video_encoder_args(video, out_w, out_h, vinfo.fps)
    duration = video_duration(inter)
    cmd = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
           "-i", str(inter), "-i", str(src), "-map", "0:v:0", *video_audio_args(video, vinfo.audio_codec),
           "-t", f"{duration:.6f}", *enc_args, "-fps_mode", "passthrough", str(dest_tmp)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    err_lines: list[str] = []
    t = threading.Thread(target=lambda: err_lines.extend(proc.stderr.read().splitlines()), daemon=True)  # type: ignore[union-attr]
    t.start()
    total = max(1, progress.frame_total)
    last = 0.0
    assert proc.stdout is not None
    for line in proc.stdout:
        if cancel.is_set():
            proc.terminate()
            break
        if line.startswith("frame="):
            try:
                progress.chunk_fraction = min(1.0, int(line[6:].strip()) / total)
            except ValueError:
                pass
            now = time.time()
            if now - last > 0.5:
                last = now
                progress.current = f"{job.rel} : encodage {enc_name} {progress.chunk_fraction * 100:.0f} %"
                on_progress(progress)
    proc.wait()
    t.join(timeout=2)
    if cancel.is_set():
        dest_tmp.unlink(missing_ok=True)
        raise RuntimeError("Render stopped by user.")
    if proc.returncode:
        dest_tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg {enc_name} : {' '.join(err_lines)[-400:]}")
    return enc_name


def _normalize_timestamps(ffmpeg: Path, src: Path, tmp: Path, fps: float) -> Path:
    """Copie quasi sans perte à cadence constante et horodatages régénérés (x264 qp 0, ou x265 au-delà de 4096 px).
    Certains WebM de jeux ont des horodatages dupliqués ou non monotones : le muxeur NUT de l'outil les refuse
    (« Invalid argument … returned 22 »)."""
    dest = tmp / f"{src.stem}.cfr.mkv"
    rate = f"{fps:.6f}".rstrip("0").rstrip(".") if fps and fps > 0 else "30"
    probe = subprocess.run([str(ffmpeg).replace("ffmpeg.exe", "ffprobe.exe"), "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(src)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    big = False
    try:
        w, h = (int(x) for x in probe.stdout.strip().split(",")[:2]); big = w > 4096 or h > 4096
    except Exception:
        pass
    codec = (["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "qp=0:log-level=error"] if big
             else ["-c:v", "libx264", "-preset", "ultrafast", "-qp", "0"])
    cmd = [str(ffmpeg), "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-fps_mode", "cfr", "-r", rate,
           *codec, "-pix_fmt", "yuv420p", "-an", str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0 or not dest.exists():
        raise RuntimeError(f"normalisation des horodatages impossible : {r.stderr.strip()[-300:]}")
    return dest


def _run_videos(jobs, tool, dlss, video, factor, tmp, game, manifest, summary, progress, log, on_progress, cancel) -> None:
    """Chaque vidéo : DLSS 5 image par image (src/video.py, intermédiaire NVENC quasi sans perte) puis réencodage
    dans le codec choisi pour Ren'Py. Reprenable (les sorties existantes ont été écartées du plan), annulable."""
    video_mod = tool["video"]
    runtime_mod = tool["runtime"]
    ffmpeg = Path(runtime_mod.FFMPEG)
    nr = video.nr_for(dlss)
    out_dir = jobs[0].dest.parent if game is None else None
    vmanifest = None
    hd_root: Path | None = None
    if game is not None and jobs and jobs[0].backup is None:
        # mode HD : le manifeste des facteurs est écrit dans le dossier HD (parent commun des sorties)
        hd_root = game / Path(jobs[0].dest.relative_to(game)).parts[0]
        vmanifest = load_video_manifest(hd_root)
    _install_scene_reset(video_mod, bool(video.scene_reset))
    inter_name = _install_intermediate_encoder(video_mod, bool(video.hw_encode))
    codec_label = VIDEO_CODECS[video.codec][0].split(" (")[0]
    progress.phase = "video"
    progress.video_count = len(jobs)
    progress.frames_total_all = sum((j.video_info.frames if j.video_info else 0) for j in jobs)
    log(f"[Vidéos] {len(jobs)} fichier(s) → DLSS 5 (intermédiaire {inter_name}) puis {codec_label} (CRF {video.crf}, "
        f"vitesse {video.speed}, audio {'conservé' if video.keep_audio else 'supprimé'}, plafond {video.max_width}×{video.max_height}, "
        f"réinit. changement de plan {'oui' if video.scene_reset else 'non'})")
    for idx, job in enumerate(jobs, 1):
        if cancel.is_set():
            summary.cancelled = True
            return
        vinfo = job.video_info or VideoInfo()
        jf = float(job.video_factor or factor)
        progress.video_index = idx
        progress.video_name = job.rel
        progress.chunk_base = progress.done + progress.failed   # live_done : une vidéo = un fichier
        progress.chunk_size = 1
        progress.frame = 0
        progress.frame_total = max(1, vinfo.frames)
        progress.video_stage = "DLSS"
        progress.video_fps = 0.0
        progress.chunk_fraction = 0.0
        progress.video_started = time.time()
        progress.current = f"Vidéo {idx}/{len(jobs)} : {job.rel} — démarrage DLSS"
        on_progress(progress)
        t_video = time.time()
        inter: Path | None = None
        dest_tmp: Path | None = None
        try:
            src = job.source.materialize(tmp)
            out_w, out_h = video_output_size(vinfo.width, vinfo.height, jf) if vinfo.ok else (0, 0)
            inter_codec = "HEVC" if (out_w > 4096 or out_h > 4096) else "H.264"
            opts = video_mod.ConversionOptions(
                nr_preset=nr.nr_preset, nr_style=nr.nr_style, nr_intensity=float(nr.nr_intensity),
                local_tone_strength=float(nr.local_tone), local_structure_strength=float(nr.local_structure),
                skin_structure_strength=float(nr.skin_structure), automatic_mask=bool(nr.automatic_mask),
                upscaling_factor=jf, codec=inter_codec, container="MKV", quality=video.intermediate_quality,
                warmup_frames=int(video.warmup_frames), rename_mode="Auto", dlss_model_preset=nr.dlss_model_preset,
                preview_seconds=(float(video.limit_seconds) if video.limit_seconds and video.limit_seconds > 0 else None),
            )
            last_tick = [0.0]

            def vprog(frac: float, msg: str) -> None:
                m = _FRAME_RE.search(msg)
                if m:
                    progress.frame = int(m.group(1))
                    progress.frame_total = max(1, int(m.group(2)))
                    dt = time.time() - progress.video_started
                    progress.video_fps = progress.frame / dt if dt > 0.5 else 0.0
                    progress.chunk_fraction = progress.frame / progress.frame_total
                progress.current = f"Vidéo {idx}/{len(jobs)} : {job.rel} — {msg}"
                now = time.time()
                if now - last_tick[0] > 0.3 or frac >= 1.0:
                    last_tick[0] = now
                    on_progress(progress)

            t0 = time.time()
            try:
                result = video_mod.convert_video(src, opts, vprog)
            except Exception as exc_first:
                msg = str(exc_first)
                if "Invalid argument" not in msg and "returned 22" not in msg:
                    raise
                log(f"  Horodatages invalides dans {job.rel} : copie à cadence constante puis nouvel essai.")
                progress.current = f"Vidéo {idx}/{len(jobs)} : {job.rel} — correction des horodatages"
                on_progress(progress)
                cfr_src = _normalize_timestamps(ffmpeg, src, tmp, vinfo.fps if vinfo.ok else 0.0)
                progress.video_started = time.time()
                result = video_mod.convert_video(cfr_src, opts, vprog)   # l'audio final est repris de `src`, l'original
                with suppress(Exception):
                    cfr_src.unlink()
            dlss_seconds = time.time() - t0
            inter = Path(result.output_path)
            progress.frame = progress.frame_total = int(result.frames)
            progress.video_dlss_seconds += dlss_seconds
            progress.video_stage = "encodage"
            progress.chunk_fraction = 0.0
            on_progress(progress)
            dest_tmp = tmp / f"{Path(job.rel).stem}_{idx}{video.container_ext()}"
            t1 = time.time()
            enc_name = _final_encode(ffmpeg, inter, src, dest_tmp, job, video, vinfo, result.output_width, result.output_height,
                                     progress, on_progress, cancel)
            progress.video_encode_seconds += time.time() - t1
            inter.unlink(missing_ok=True)
            inter = None
            _commit_output(job, dest_tmp, game, manifest)
            dest_tmp = None
            if vmanifest is not None and hd_root is not None:
                vmanifest["videos"][job.rel] = {
                    "file": job.dest.relative_to(game).as_posix(),  # type: ignore[arg-type]
                    "factor": jf, "width": result.output_width, "height": result.output_height,
                }
                save_video_manifest(hd_root, vmanifest)
            summary.written += 1
            summary.timings.append((job.rel, float(result.elapsed_seconds)))
            summary.outputs.append((job.rel, str(job.dest)))
            progress.done += 1
            progress.frames_done_all += int(result.frames)
            total_s = time.time() - t_video
            log(f"  OK {job.rel} : {result.frames} images {result.input_width}×{result.input_height} → "
                f"{result.output_width}×{result.output_height} ({jf:g}×), DLSS {result.frames / max(dlss_seconds, 1e-6):.1f} i/s "
                f"({dlss_seconds:.0f} s), encodage {enc_name} {time.time() - t1:.0f} s, total {total_s:.0f} s, "
                f"{human_size(job.dest.stat().st_size)}")
        except Exception as exc:
            first = str(exc).splitlines()[0] if str(exc) else "erreur inconnue"
            if inter is not None:
                inter.unlink(missing_ok=True)
            if dest_tmp is not None:
                dest_tmp.unlink(missing_ok=True)
            if "stopped by user" in str(exc).lower() or cancel.is_set():
                summary.cancelled = True
                log(f"  Annulé pendant {job.rel} (fichiers temporaires supprimés).")
                return
            summary.failed.append((job.rel, first))
            progress.failed += 1
            log(f"  ÉCHEC vidéo {job.rel} : {first}")
        finally:
            if job.source.from_rpa:
                try:
                    (tmp / job.source.rpa_key).unlink(missing_ok=True)  # type: ignore[operator]
                except OSError:
                    pass
        on_progress(progress)


def request_cancel(cancel: threading.Event, tool_root: Path) -> str:
    """Annulation : drapeau vérifié entre les lots + arrêt du lot en cours via l'outil."""
    cancel.set()
    if _TOOL:
        try:
            return str(_TOOL["runtime"].cancel_active_job())  # type: ignore[attr-defined]
        except Exception as exc:
            return T("core.msg.cancel_requested_err", err=exc)
    return T("core.msg.cancel_requested")


# ----------------------------------------------------------------------------
# Aperçu avant validation : quelques images traitées dans un dossier temporaire
# ----------------------------------------------------------------------------
PREVIEW_ROOT = APP_DIR / "preview"


def build_preview_plan(plan: Plan, count: int, how: str, chosen: Iterable[str], preview_dir: Path) -> Plan:
    """Sous-ensemble du plan (images seulement) redirigé vers preview_dir, sans sauvegarde ni hook."""
    import random

    candidates = [j for j in plan.jobs if not j.is_video]
    count = max(1, int(count))
    if how == "choose":
        wanted = {c for c in chosen}
        picked = [j for j in candidates if j.rel in wanted]
    elif how == "random":
        picked = random.sample(candidates, min(count, len(candidates))) if candidates else []
    else:
        picked = candidates[:count]
    preview = Plan(total_refs=plan.total_refs)
    for j in picked:
        dest = _dest_with_format(preview_dir, j.rel, j.output_format)
        preview.jobs.append(Job(j.rel, j.source, dest, j.source_format, j.output_format, None, False))
    _estimate(preview, 1.0)
    return preview


def clear_preview(preview_dir: Path) -> None:
    shutil.rmtree(preview_dir, ignore_errors=True)


# ----------------------------------------------------------------------------
# Intégration Windows : sélecteur de dossier, fenêtre « application »
# ----------------------------------------------------------------------------
# Fenêtre parente invisible, au premier plan : les boîtes de dialogue s'ouvrent devant l'application.
_DIALOG_PRELUDE = 'Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; Add-Type -Namespace RHD -Name U32 -MemberDefinition \'[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);\'; $wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea; $owner = New-Object System.Windows.Forms.Form; $owner.TopMost = $true; $owner.ShowInTaskbar = $false; $owner.FormBorderStyle = \'None\'; $owner.StartPosition = \'Manual\'; $owner.Opacity = 0.01; $owner.Size = New-Object System.Drawing.Size(1, 1); $owner.Location = New-Object System.Drawing.Point([int]($wa.Left + $wa.Width / 2), [int]($wa.Top + $wa.Height / 2)); $owner.Show(); [System.Windows.Forms.SendKeys]::SendWait(\'%\'); $owner.Activate(); [RHD.U32]::SetForegroundWindow($owner.Handle) | Out-Null; $owner.BringToFront(); '


def pick_folder(title: str = "", initial: str = "") -> str:
    """Boîte de dialogue native (PowerShell + WinForms) ; renvoie "" si annulée."""
    safe_title = (title or T("core.dialog.pick_folder")).replace("'", "''")
    safe_initial = str(initial).replace("'", "''")
    script = (
        _DIALOG_PRELUDE + "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        f"$d.Description = '{safe_title}'; $d.ShowNewFolderButton = $true; "
        + (f"if (Test-Path -LiteralPath '{safe_initial}') {{ $d.SelectedPath = '{safe_initial}' }}; " if safe_initial else "")
        + "if ($d.ShowDialog($owner) -eq 'OK') { $d.SelectedPath }; $owner.Close()"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return ""
    return proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""


def _run_dialog(script: str) -> str:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return ""
    return proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""


IMAGE_DIALOG_FILTER = (T("core.dialog.image_filter") + "|*.png;*.jpg;*.jpeg;*.webp;*.avif;*.tiff|" + T("core.dialog.all_files") + "|*.*")


def pick_file(title: str = "", filter_: str = IMAGE_DIALOG_FILTER, initial: str = "") -> str:
    """OpenFileDialog natif ; renvoie "" si annulé."""
    t, f, i = (x.replace("'", "''") for x in (title or T("core.dialog.pick_image"), filter_, str(initial)))
    script = (
        _DIALOG_PRELUDE + "$d = New-Object System.Windows.Forms.OpenFileDialog; "
        f"$d.Title = '{t}'; $d.Filter = '{f}'; $d.Multiselect = $false; "
        + (f"if (Test-Path -LiteralPath '{i}') {{ $d.InitialDirectory = '{i}' }}; " if i else "")
        + "if ($d.ShowDialog($owner) -eq 'OK') { $d.FileName }; $owner.Close()"
    )
    return _run_dialog(script)


def save_file_dialog(title: str = "", default_name: str = "", filter_: str = IMAGE_DIALOG_FILTER) -> str:
    """SaveFileDialog natif ; renvoie "" si annulé."""
    t, f, n = (x.replace("'", "''") for x in (title or T("core.dialog.save_as"), filter_, default_name))
    ext = Path(default_name).suffix.lstrip(".")
    script = (
        _DIALOG_PRELUDE + "$d = New-Object System.Windows.Forms.SaveFileDialog; "
        f"$d.Title = '{t}'; $d.Filter = '{f}'; $d.FileName = '{n}'; $d.DefaultExt = '{ext}'; $d.OverwritePrompt = $true; $d.AddExtension = $true; "
        "if ($d.ShowDialog($owner) -eq 'OK') { $d.FileName }; $owner.Close()"
    )
    return _run_dialog(script)


def find_app_browser() -> Path | None:
    """Edge ou Chrome (mode --app) : chemins standards, registre App Paths, puis PATH."""
    import shutil as _sh

    candidates = []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidates += [Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                           Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"]
    try:
        import winreg
        for exe in ("msedge.exe", "chrome.exe"):
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                        candidates.append(Path(winreg.QueryValue(k, None)))
                except OSError:
                    pass
    except ImportError:
        pass
    for exe in ("msedge", "chrome"):
        found = _sh.which(exe)
        if found:
            candidates.append(Path(found))
    for c in candidates:
        if c.is_file():
            return c
    return None


def open_app_window(url: str) -> str:
    """Ouvre l'URL dans une fenêtre sans barre d'adresse (Edge/Chrome --app), sinon navigateur par défaut."""
    exe = find_app_browser()
    if exe is not None:
        try:
            subprocess.Popen([str(exe), f"--app={url}", "--window-size=1400,1000", "--new-window"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
            return T("core.window.app_mode", exe=exe.name)
        except Exception:
            pass
    import webbrowser
    webbrowser.open(url)
    return T("core.window.default_browser")


# ----------------------------------------------------------------------------
# Visionneuse avant / après
# ----------------------------------------------------------------------------
@dataclass
class ComparePair:
    rel: str
    before: Source
    after: Source
    kind: str  # "hd2x" | "backup" | "folder"

    @property
    def is_video(self) -> bool:
        p = self.after.path
        return p is not None and p.suffix.lower() in VIDEO_EXTS


def _find_original(game: Path, rel_after: Path, rpas: list[Rpa]) -> Source | None:
    """Retrouve l'original d'une sortie HD (même chemin, extension éventuellement différente)."""
    stems = [rel_after]
    for ext in IMAGE_EXTS + VIDEO_EXTS:
        if ext != rel_after.suffix.lower():
            stems.append(rel_after.with_suffix(ext))
    for cand in stems:
        rel = cand.as_posix()
        src = resolve_source(game, rel, rpas)
        if src is not None:
            return src
    return None


def list_pairs(game_root: str, kind: str, out_name: str = "hd2x", input_dir: str = "", output_dir: str = "") -> list[ComparePair]:
    pairs: list[ComparePair] = []
    if kind == "folder":
        src_dir, dst_dir = Path(input_dir), Path(output_dir)
        if not src_dir.is_dir() or not dst_dir.is_dir():
            return pairs
        for f in sorted(dst_dir.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in FORMAT_BY_EXT and f.suffix.lower() not in EXT_BY_FORMAT.values():
                continue
            rel = f.relative_to(dst_dir)
            orig = None
            for ext in [rel.suffix] + [e for e in IMAGE_EXTS if e != rel.suffix.lower()]:
                cand = src_dir / rel.with_suffix(ext)
                if cand.is_file():
                    orig = cand
                    break
            if orig:
                pairs.append(ComparePair(rel.as_posix(), Source(path=orig), Source(path=f), kind))
        return pairs
    game = find_game_dir(game_root)
    if kind == "backup":
        backup_root = game / BACKUP_DIR
        if not backup_root.is_dir():
            return pairs
        for f in sorted(backup_root.rglob("*")):
            if not f.is_file() or f.name == BACKUP_MANIFEST or (f.suffix.lower() not in FORMAT_BY_EXT and f.suffix.lower() not in VIDEO_EXTS):
                continue
            rel = f.relative_to(backup_root)
            cur = game / rel
            if cur.is_file():
                pairs.append(ComparePair(rel.as_posix(), Source(path=f), Source(path=cur), kind))
        return pairs
    if kind == "at2":
        manifest = _load_at2_manifest(game)
        for rel in manifest.get("files", []):
            f = game / rel
            orig = f.with_name(f.name.replace("@2", "", 1))
            if f.is_file() and orig.is_file():
                pairs.append(ComparePair(orig.relative_to(game).as_posix(), Source(path=orig), Source(path=f), kind))
        return pairs
    out_dir = game / out_name
    if not out_dir.is_dir():
        return pairs
    rpas = load_rpas(game)
    for f in sorted(out_dir.rglob("*")):
        if not f.is_file() or (f.suffix.lower() not in FORMAT_BY_EXT and f.suffix.lower() not in VIDEO_EXTS):
            continue
        rel = f.relative_to(out_dir)
        orig = _find_original(game, rel, rpas)
        if orig is not None:
            pairs.append(ComparePair(rel.as_posix(), orig, Source(path=f), kind))
    return pairs


def open_image(source: Source):
    from PIL import Image
    if source.path is not None:
        im = Image.open(source.path)
    else:
        im = Image.open(io.BytesIO(source.read_bytes()))
    im.load()
    return im


# ----------------------------------------------------------------------------
# Configuration JSON
# ----------------------------------------------------------------------------
def save_config(values: dict, path: Path = CONFIG_FILE) -> Path:
    path.write_text(json.dumps(values, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_config(path: Path = CONFIG_FILE) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


__all__ = [name for name in dir() if not name.startswith("_")]
