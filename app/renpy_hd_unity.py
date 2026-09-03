r"""
renpy_hd_unity.py - RenPyHD « Unity » : améliore les textures d'un jeu Unity avec le DLSS 5 Neural Rendering,
à la taille d'origine (DLAA 1×), en les réécrivant dans les fichiers d'assets du jeu.

Pourquoi à la taille d'origine : les sprites et atlas Unity sont adressés au pixel (rectangles, pivots, bordures 9-slice,
UV) et les jeux 32 bits n'ont pas de place pour des textures 4× plus lourdes ; DLSS ne peut pas non plus être injecté au
rendu d'un exécutable x86. On extrait donc chaque Texture2D (UnityPy), on la passe dans DLSS 1× (netteté, structure,
peau… du Neural Rendering), et on la réécrit dans le **même format** (DXT1/DXT5/BC7 via etcpak, ETC/ASTC, RGBA32/RGB24…)
avec les mêmes dimensions, donc le même poids en mémoire vidéo.

Ce module sait :
  * analyser un jeu Unity (dossier *_Data, version, fichiers .assets / level* / globalgamemanagers / bundles UnityFS de
    StreamingAssets, textures : formats, dimensions, mipmaps, stockage .resS, sprites qui les référencent) ;
  * sauvegarder les fichiers touchés dans <jeu>\_renpyhd_backup\ (reprenable, jamais écrasé) et les restaurer ;
  * sélectionner les textures (taille minimale, expressions régulières, conteneurs, exclusion des textures d'interface) ;
  * produire un aperçu avant/après de quelques textures ;
  * améliorer les textures sélectionnées par lots (pipeline DLSS du cœur), les réécrire (en place dans le .resS quand la
    taille des données ne change pas, sinon réécriture du conteneur), avec reprise via _renpyhd_unity.json ;
  * lancer le jeu pour vérifier (processus vivant, capture de la fenêtre, journal Unity).

Il est indépendant de Gradio : appels de fonctions et callbacks (log, progression, événement d'annulation).
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import renpy_hd_core as core
from renpy_hd_core import T

BACKUP_DIR = "_renpyhd_backup"
MANIFEST = "_renpyhd_unity.json"
BACKUP_MANIFEST = "unity_backup.json"
DLSS_MIN_SIDE = 64                 # minimum absolu de DLSS (chaque dimension)
DLSS_MAX = (7680, 4320)
DEFAULT_MIN_SIDE = 256
DEFAULT_CHUNK = 48
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ENGINE_RESOURCE_FILES = {"unity default resources", "unity_builtin_extra"}
BUNDLE_MAGICS = (b"UnityFS", b"UnityWeb", b"UnityRaw", b"UnityArchive")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
# Textures d'interface / polices / atlas : exclues par défaut (heuristique sur le nom : mots entiers entre _ - . espace,
# plus quelques sous-chaînes sans ambiguïté)
UI_TOKENS = {"ui", "gui", "hud", "icon", "icons", "font", "fonts", "atlas", "cursor", "cursors", "button", "buttons", "btn", "arrow",
             "checkmark", "knob", "mask", "default", "unity", "logo", "spinner", "slider", "toggle", "scrollbar", "dropdown", "particle",
             "noise", "lut", "gradient", "menu", "overlay", "frame", "border", "panel", "badge", "emoji", "sprite", "sprites", "splash",
             "loading", "cursor", "checkbox", "tooltip", "fx"}
UI_SUBSTRINGS = ("font", "icon", "atlas", "unity", "spritesheet")
_TOKEN_SPLIT = re.compile(r"[_\-\s.:/()\[\]]+")


def looks_like_ui(name: str) -> bool:
    low = name.lower()
    if any(s in low for s in UI_SUBSTRINGS):
        return True
    return any(tok in UI_TOKENS for tok in _TOKEN_SPLIT.split(low) if tok)

# Formats réécrits à l'identique (UnityPy : etcpak pour BC/ETC, astc-encoder pour ASTC, Pillow pour les formats bruts)
SAME_FORMAT = {
    "DXT1", "DXT5", "BC4", "BC5", "BC7", "ETC_RGB4", "ETC_RGB4_3DS", "ETC2_RGB", "ETC2_RGBA8", "ETC2_RGBA1",
    "RGBA32", "ARGB32", "RGB24", "BGRA32", "Alpha8", "R8",
}
# Formats « crunched » : réécrits dans leur format de bloc non compressé (Unity les lit tels quels)
REMAPPED_FORMAT = {"DXT1Crunched": "DXT1", "DXT5Crunched": "DXT5", "ETC_RGB4Crunched": "ETC_RGB4", "ETC2_RGBA8Crunched": "ETC2_RGBA8"}
ASTC_RE = re.compile(r"^ASTC_(RGBA?_)?\d+x\d+$")


def log_default(_msg: str) -> None:
    pass


def unitypy_version() -> str:
    """Version d'UnityPy installée, ou "" si le module manque (l'onglet l'explique alors)."""
    try:
        import UnityPy
        return str(getattr(UnityPy, "__version__", "?"))
    except Exception:
        return ""


def writable_format(fmt: str) -> tuple[str, str]:
    """(format réécrit, mode) : mode = "same" | "remapped" | "fallback" (RGBA32, mémoire ×4–6 : à autoriser explicitement)."""
    if fmt in SAME_FORMAT or ASTC_RE.match(fmt):
        return fmt, "same"
    if fmt in REMAPPED_FORMAT:
        return REMAPPED_FORMAT[fmt], "remapped"
    return "RGBA32", "fallback"


# ----------------------------------------------------------------------------
# Analyse
# ----------------------------------------------------------------------------
@dataclass
class TexInfo:
    file: str            # conteneur, chemin relatif au dossier du jeu (posix)
    path_id: int
    name: str
    width: int
    height: int
    fmt: str             # nom TextureFormat d'origine
    mips: int = 1
    streamed: bool = False
    stream_size: int = 0
    sprites: int = 0     # nombre de Sprite du même conteneur qui la référencent

    @property
    def key(self) -> str:
        return f"{self.file}|{self.path_id}"

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def short_side(self) -> int:
        return min(self.width, self.height)

    def label(self) -> str:
        return f"{self.name} — {self.width}×{self.height} {self.fmt} ({Path(self.file).name})"


@dataclass
class ContainerInfo:
    rel: str
    kind: str            # "assets" (SerializedFile) | "bundle" (UnityFS)
    size: int = 0
    textures: int = 0
    error: str = ""


@dataclass
class UnityAnalysis:
    root: Path
    data_dir: Path
    exe: Path | None = None
    version: str = ""
    company: str = ""
    product: str = ""
    containers: list[ContainerInfo] = field(default_factory=list)
    textures: list[TexInfo] = field(default_factory=list)
    formats: dict[str, int] = field(default_factory=dict)
    dims: dict[str, int] = field(default_factory=dict)
    sprites: int = 0
    loose_images: int = 0
    loose_dirs: list[str] = field(default_factory=list)
    has_backup: bool = False
    backup_files: int = 0
    done: int = 0
    elapsed: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def big(self) -> list[TexInfo]:
        return [t for t in self.textures if t.short_side >= DEFAULT_MIN_SIDE]

    @property
    def pixels(self) -> int:
        return sum(t.pixels for t in self.textures)

    @property
    def mipmapped(self) -> int:
        return sum(1 for t in self.textures if t.mips > 1)

    @property
    def streamed(self) -> int:
        return sum(1 for t in self.textures if t.streamed)

    def container_labels(self) -> list[str]:
        return [c.rel for c in self.containers if not c.error and c.textures]


def find_data_dir(root: Path) -> Path:
    """Dossier <jeu>_Data (le dossier choisi peut être le jeu ou le _Data lui-même)."""
    root = Path(root)
    if root.name.lower().endswith("_data") and (root / "globalgamemanagers").exists():
        return root
    cands = [p for p in root.iterdir() if p.is_dir() and p.name.lower().endswith("_data") and (p / "globalgamemanagers").exists()]
    if not cands:
        raise RuntimeError(T("unity.err.no_data", root=root))
    return cands[0]


def find_exe(root: Path, data_dir: Path) -> Path | None:
    stem = data_dir.name[:-5]
    for p in root.glob("*.exe"):
        if p.stem.lower() == stem.lower():
            return p
    exes = [p for p in root.glob("*.exe") if "crashhandler" not in p.name.lower() and "reshade" not in p.name.lower()
            and "setup" not in p.name.lower()]
    return sorted(exes, key=lambda p: len(p.name))[0] if exes else None


def _is_bundle(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return False
    return any(head.startswith(m) for m in BUNDLE_MAGICS)


def list_containers(root: Path, data_dir: Path) -> list[ContainerInfo]:
    """Fichiers d'assets du jeu : .assets, level*, globalgamemanagers*, bundles UnityFS de StreamingAssets (et de tout
    sous-dossier de _Data), sans les ressources internes du moteur."""
    out: list[ContainerInfo] = []
    seen: set[str] = set()

    def add(p: Path, kind: str) -> None:
        rel = p.relative_to(root).as_posix()
        if rel in seen:
            return
        seen.add(rel)
        out.append(ContainerInfo(rel, kind, p.stat().st_size))

    for p in sorted(data_dir.iterdir()):
        if not p.is_file():
            continue
        low = p.name.lower()
        if low.endswith(".assets") or low.startswith("level") and "." not in p.name or low.startswith("globalgamemanagers"):
            add(p, "assets")
    for sub in sorted(data_dir.iterdir()):
        if not sub.is_dir() or sub.name in ("Managed", "Plugins", "Mono", "MonoBleedingEdge", "il2cpp_data"):
            continue
        for p in sorted(sub.rglob("*")):
            if p.is_file() and p.name not in ENGINE_RESOURCE_FILES and p.suffix.lower() not in (".manifest", ".mp4", ".webm", ".ogg", ".wav",
                                                                                                    ".txt", ".json", ".xml", ".dll") and _is_bundle(p):
                add(p, "bundle")
    return out


def load_env(path: Path):
    """Charge un conteneur entièrement en mémoire : UnityPy.load(chemin) garde sinon un descripteur ouvert sur le fichier,
    ce qui empêche de le remplacer sous Windows après modification. Les dépendances (.resS) sont résolues dans son dossier."""
    import io
    import UnityPy

    path = Path(path)
    env = UnityPy.Environment(path=str(path.parent))
    env.load_file(io.BytesIO(path.read_bytes()), name=str(path))
    env.file = list(env.files.values())[0]
    return env


def _read_app_info(data_dir: Path) -> tuple[str, str]:
    try:
        lines = (data_dir / "app.info").read_text(encoding="utf-8", errors="replace").splitlines()
        return (lines[0].strip() if lines else ""), (lines[1].strip() if len(lines) > 1 else "")
    except OSError:
        return "", ""


def _dim_bucket(w: int, h: int) -> str:
    m = max(w, h)
    for b in (256, 512, 1024, 2048, 4096):
        if m < b:
            return f"< {b}"
    return "≥ 4096"


def analyze_unity_game(root_str: str, log: Callable[[str], None] = log_default) -> UnityAnalysis:
    """Étape 1 : inventaire des textures (métadonnées seulement, quelques secondes pour quelques milliers de textures)."""
    import UnityPy
    from UnityPy.enums import TextureFormat as TF

    t0 = time.time()
    root = Path(root_str)
    if not root.is_dir():
        raise RuntimeError(T("unity.err.no_folder", root=root))
    data_dir = find_data_dir(root)
    if data_dir == root:
        root = data_dir.parent
    a = UnityAnalysis(root=root, data_dir=data_dir, exe=find_exe(root, data_dir))
    a.company, a.product = _read_app_info(data_dir)
    a.containers = list_containers(root, data_dir)
    formats: dict[str, int] = {}
    dims: dict[str, int] = {}
    for c in a.containers:
        path = root / c.rel
        try:
            env = load_env(path)
        except Exception as exc:
            c.error = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            a.errors.append(f"{c.rel} : {c.error}")
            continue
        sprite_refs: dict[int, int] = {}
        texs: list[TexInfo] = []
        for obj in env.objects:
            tname = obj.type.name
            if tname == "Texture2D":
                try:
                    d = obj.read()
                    if not a.version:
                        a.version = str(getattr(obj.assets_file, "unity_version", "") or "")
                    try:
                        fmt = TF(d.m_TextureFormat).name
                    except Exception:
                        fmt = str(d.m_TextureFormat)
                    sd = getattr(d, "m_StreamData", None)
                    streamed = bool(sd is not None and getattr(sd, "path", ""))
                    texs.append(TexInfo(c.rel, int(obj.path_id), str(d.m_Name), int(d.m_Width), int(d.m_Height), fmt,
                                        int(getattr(d, "m_MipCount", 1) or 1), streamed, int(getattr(sd, "size", 0) or 0) if streamed else 0))
                except Exception as exc:
                    a.errors.append(f"{c.rel} #{obj.path_id} : {str(exc).splitlines()[0]}")
            elif tname == "Sprite":
                a.sprites += 1
                try:
                    s = obj.read()
                    pid = int(s.m_RD.texture.m_PathID)
                    sprite_refs[pid] = sprite_refs.get(pid, 0) + 1
                except Exception:
                    pass
        for t in texs:
            t.sprites = sprite_refs.get(t.path_id, 0)
            formats[t.fmt] = formats.get(t.fmt, 0) + 1
            b = _dim_bucket(t.width, t.height)
            dims[b] = dims.get(b, 0) + 1
        c.textures = len(texs)
        a.textures.extend(texs)
        log(T("unity.log.container", file=c.rel, n=len(texs)))
    a.formats = dict(sorted(formats.items(), key=lambda kv: -kv[1]))
    a.dims = dims
    # images « en vrac » (DLC, wallpapers…) hors des conteneurs Unity : à traiter avec l'onglet principal (mode dossier)
    for sub in sorted(root.iterdir()):
        if sub.is_dir() and sub != data_dir and sub.name not in (BACKUP_DIR, "savegames", "Mono", "MonoBleedingEdge", "reshade-shaders"):
            n = sum(1 for p in sub.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
            if n:
                a.loose_images += n
                a.loose_dirs.append(sub.name)
    bm = _load_backup_manifest(root)
    a.backup_files = len(bm.get("files", []))
    a.has_backup = a.backup_files > 0
    a.done = sum(len(v) for v in load_manifest(root).get("done", {}).values())
    a.elapsed = time.time() - t0
    return a


# ----------------------------------------------------------------------------
# Sélection
# ----------------------------------------------------------------------------
@dataclass
class UnitySettings:
    min_side: int = DEFAULT_MIN_SIDE
    include_re: str = ""
    exclude_re: str = ""
    containers: list[str] = field(default_factory=list)    # vide = tous
    skip_ui: bool = True
    allow_fallback: bool = False                            # réécrire en RGBA32 les formats non ré-encodables
    chunk: int = DEFAULT_CHUNK
    threads: int = 0
    limit: int = 0                                          # 0 = toutes ; sinon n premières (tests)


@dataclass
class Selection:
    chosen: list[TexInfo] = field(default_factory=list)
    skipped: dict[str, list[TexInfo]] = field(default_factory=dict)     # raison -> textures

    def skipped_count(self, reason: str) -> int:
        return len(self.skipped.get(reason, []))

    @property
    def pixels(self) -> int:
        return sum(t.pixels for t in self.chosen)


def _compile(pattern: str) -> re.Pattern | None:
    pattern = (pattern or "").strip()
    if not pattern:
        return None
    return re.compile(pattern, re.IGNORECASE)


def select_textures(a: UnityAnalysis, s: UnitySettings) -> Selection:
    sel = Selection()
    inc, exc = _compile(s.include_re), _compile(s.exclude_re)
    wanted = set(s.containers or [])

    def skip(reason: str, t: TexInfo) -> None:
        sel.skipped.setdefault(reason, []).append(t)

    for t in a.textures:
        if wanted and t.file not in wanted:
            skip("container", t)
            continue
        if t.short_side < max(int(s.min_side), DLSS_MIN_SIDE):
            skip("small", t)
            continue
        if t.width > DLSS_MAX[0] or t.height > DLSS_MAX[1] or t.height > DLSS_MAX[0] or t.width > DLSS_MAX[1]:
            skip("huge", t)
            continue
        if inc and not inc.search(t.name):
            skip("include", t)
            continue
        if exc and exc.search(t.name):
            skip("exclude", t)
            continue
        if s.skip_ui and looks_like_ui(t.name):
            skip("ui", t)
            continue
        _fmt, mode = writable_format(t.fmt)
        if mode == "fallback" and not s.allow_fallback:
            skip("format", t)
            continue
        sel.chosen.append(t)
    if s.limit and len(sel.chosen) > int(s.limit):
        sel.skipped.setdefault("limit", []).extend(sel.chosen[int(s.limit):])
        sel.chosen = sel.chosen[: int(s.limit)]
    return sel


# ----------------------------------------------------------------------------
# Manifeste (reprise) et sauvegarde
# ----------------------------------------------------------------------------
def load_manifest(root: Path) -> dict:
    p = Path(root) / MANIFEST
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "done": {}, "runs": []}


def save_manifest(root: Path, m: dict) -> None:
    (Path(root) / MANIFEST).write_text(json.dumps(m, indent=1, ensure_ascii=False), encoding="utf-8")


def done_keys(root: Path) -> set[str]:
    m = load_manifest(root)
    return {f"{f}|{pid}" for f, pids in m.get("done", {}).items() for pid in pids}


def _load_backup_manifest(root: Path) -> dict:
    p = Path(root) / BACKUP_DIR / BACKUP_MANIFEST
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "files": []}


def _save_backup_manifest(root: Path, m: dict) -> None:
    d = Path(root) / BACKUP_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / BACKUP_MANIFEST).write_text(json.dumps(m, indent=1, ensure_ascii=False), encoding="utf-8")


def sidecar_files(root: Path, rel: str) -> list[str]:
    """Fichiers de ressources qui accompagnent un conteneur (.resS / .resource) : sauvegardés et restaurés avec lui."""
    p = Path(root) / rel
    out = []
    for cand in (p.with_name(p.name + ".resS"), p.with_name(p.name + ".resource"), p.with_name(p.stem + ".resource")):
        if cand.is_file():
            r = cand.relative_to(root).as_posix()
            if r not in out:
                out.append(r)
    return out


@dataclass
class BackupProgress:
    done: int = 0
    total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    current: str = ""
    elapsed: float = 0.0

    @property
    def fraction(self) -> float:
        return self.bytes_done / self.bytes_total if self.bytes_total else (self.done / self.total if self.total else 0.0)


@dataclass
class BackupResult:
    copied: int = 0
    skipped: int = 0
    bytes_copied: int = 0
    cancelled: bool = False
    elapsed: float = 0.0
    errors: list[str] = field(default_factory=list)


def _copy_file(src: Path, dst: Path, tick: Callable[[int], None], cancel: threading.Event | None) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        while True:
            if cancel is not None and cancel.is_set():
                fo.close()
                tmp.unlink(missing_ok=True)
                raise InterruptedError("cancelled")
            buf = fi.read(8 * 1024 * 1024)
            if not buf:
                break
            fo.write(buf)
            tick(len(buf))
    shutil.copystat(src, tmp)
    os.replace(tmp, dst)


def backup_files_for(root: Path, files: list[str]) -> list[str]:
    out: list[str] = []
    for rel in files:
        if rel not in out:
            out.append(rel)
        for s in sidecar_files(root, rel):
            if s not in out:
                out.append(s)
    return out


def backup_containers(root: Path, files: list[str], log: Callable[[str], None], on_progress: Callable[[BackupProgress], None],
                      cancel: threading.Event | None = None) -> BackupResult:
    """Étape 2 : copie intégrale des conteneurs (et .resS) dans <jeu>\\_renpyhd_backup\\ ; un fichier déjà sauvegardé n'est
    JAMAIS écrasé (la sauvegarde reste l'original même après plusieurs passes)."""
    t0 = time.time()
    r = BackupResult()
    todo = backup_files_for(root, files)
    bm = _load_backup_manifest(root)
    known = set(bm.get("files", []))
    p = BackupProgress(total=len(todo))
    p.bytes_total = sum((root / rel).stat().st_size for rel in todo if (root / rel).is_file() and not (root / BACKUP_DIR / rel).is_file())
    on_progress(p)

    def tick(n: int) -> None:
        p.bytes_done += n
        p.elapsed = time.time() - t0
        on_progress(p)

    for rel in todo:
        src, dst = root / rel, root / BACKUP_DIR / rel
        p.current = rel
        if dst.is_file():
            r.skipped += 1
            if rel not in known:
                bm.setdefault("files", []).append(rel)
                known.add(rel)
            p.done += 1
            continue
        if not src.is_file():
            r.errors.append(f"{rel} : {T('unity.err.missing')}")
            p.done += 1
            continue
        try:
            _copy_file(src, dst, tick, cancel)
        except InterruptedError:
            r.cancelled = True
            break
        except Exception as exc:
            r.errors.append(f"{rel} : {exc}")
            p.done += 1
            continue
        r.copied += 1
        r.bytes_copied += src.stat().st_size
        bm.setdefault("files", []).append(rel)
        known.add(rel)
        _save_backup_manifest(root, bm)
        log(T("unity.log.backed_up", file=rel, size=core.human_size(src.stat().st_size)))
        p.done += 1
        on_progress(p)
    _save_backup_manifest(root, bm)
    r.elapsed = time.time() - t0
    return r


def restore_backup(root: Path, log: Callable[[str], None] = log_default) -> list[str]:
    """Remet les fichiers sauvegardés à leur place (la sauvegarde est conservée) et oublie les textures « faites »."""
    root = Path(root)
    bdir = root / BACKUP_DIR
    if not bdir.is_dir():
        return [T("unity.restore.none")]
    bm = _load_backup_manifest(root)
    files = list(bm.get("files", []))
    if not files:   # sauvegarde faite sans manifeste : tout ce qui est dans le dossier
        files = [p.relative_to(bdir).as_posix() for p in bdir.rglob("*") if p.is_file() and p.name != BACKUP_MANIFEST]
    restored, msgs = 0, []
    for rel in files:
        src, dst = bdir / rel, root / rel
        if not src.is_file():
            msgs.append(T("unity.restore.missing", file=rel))
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".renpyhd.tmp")
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            restored += 1
            log(T("unity.log.restored", file=rel))
        except Exception as exc:
            msgs.append(f"{rel} : {exc}")
    (root / MANIFEST).unlink(missing_ok=True)
    return [T("unity.restore.done", n=restored, total=len(files))] + msgs


# ----------------------------------------------------------------------------
# Extraction / réécriture
# ----------------------------------------------------------------------------
def _safe_name(t: TexInfo) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", t.name)[:60] or "tex"
    return f"{base}__{abs(t.path_id)}"


def _texture_objects(env) -> dict[int, object]:
    return {int(obj.path_id): obj for obj in env.objects if obj.type.name == "Texture2D"}


def extract_texture(d, dest: Path):
    """Texture2D lue par UnityPy → PNG (alpha conservé). Renvoie l'image PIL."""
    img = d.image
    if img.mode not in ("RGBA", "RGB"):
        img = img.convert("RGBA")
    img.save(dest, format="PNG", compress_level=1)
    return img


def _encode_for(d, img, target_fmt: str) -> tuple[bytes, object]:
    from UnityPy.enums import TextureFormat as TF
    from UnityPy.export import Texture2DConverter

    platform = d.object_reader.platform if getattr(d, "object_reader", None) is not None else 0
    return Texture2DConverter.image_to_texture2d(img, TF[target_fmt], platform, d.m_PlatformBlob)


def write_back(root: Path, container: ContainerInfo, d, t: TexInfo, img, allow_inplace: bool = True) -> str:
    """Réécrit `img` (PIL, taille d'origine) dans la texture `d`. Renvoie "inplace" (données écrites directement dans le .resS,
    conteneur intact), "inline" (texture réécrite dans le conteneur, à sauvegarder) ou lève une exception."""
    from UnityPy.enums import TextureFormat as TF

    if img.size != (t.width, t.height):
        from PIL import Image
        img = img.resize((t.width, t.height), Image.Resampling.LANCZOS)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    target, _mode = writable_format(t.fmt)
    sd = getattr(d, "m_StreamData", None)
    if allow_inplace and container.kind == "assets" and t.mips <= 1 and sd is not None and getattr(sd, "path", "") and target == t.fmt:
        data, fmt = _encode_for(d, img, target)
        if len(data) == int(sd.size):
            res_name = os.path.basename(str(sd.path).replace("\\", "/"))
            res_path = (root / container.rel).with_name(res_name)
            if res_path.is_file():
                with open(res_path, "r+b") as fh:
                    fh.seek(int(sd.offset))
                    fh.write(data)
                return "inplace"
    d.set_image(img, target_format=TF[target], mipmap_count=max(1, int(t.mips)))
    d.save()
    return "inline"


def save_container(root: Path, container: ContainerInfo, env, log: Callable[[str], None]) -> int:
    """Sérialise le conteneur modifié (bundle : compression d'origine, sinon LZ4) et le remplace atomiquement. Renvoie la taille."""
    path = root / container.rel
    if container.kind == "bundle":
        try:
            blob = env.file.save(packer="original")
        except Exception as exc:
            log(T("unity.log.packer_fallback", err=str(exc).splitlines()[0]))
            blob = env.file.save(packer="lz4")
    else:
        blob = env.file.save()
    tmp = path.with_name(path.name + ".renpyhd.tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, path)
    return len(blob)


# ----------------------------------------------------------------------------
# Aperçu
# ----------------------------------------------------------------------------
@dataclass
class PreviewItem:
    label: str
    key: str
    before: Path
    after: Path
    seconds: float = 0.0


def preview_textures(a: UnityAnalysis, sel: Selection, count: int, dlss: core.DlssSettings, tool_root: Path, preview_dir: Path,
                     log: Callable[[str], None], on_progress: Callable[[float, str], None], cancel: threading.Event,
                     threads: int = 0) -> list[PreviewItem]:
    """Quelques textures au hasard : PNG avant dans preview_dir/before, sortie DLSS 1× dans preview_dir/after."""
    import UnityPy

    core.clear_preview(preview_dir)
    before_dir, after_dir = preview_dir / "before", preview_dir / "after"
    before_dir.mkdir(parents=True, exist_ok=True)
    after_dir.mkdir(parents=True, exist_ok=True)
    picked = random.sample(sel.chosen, min(int(count), len(sel.chosen))) if sel.chosen else []
    by_file: dict[str, list[TexInfo]] = {}
    for t in picked:
        by_file.setdefault(t.file, []).append(t)
    items: list[PreviewItem] = []
    paths: list[Path] = []
    for rel, texs in by_file.items():
        env = load_env(a.root / rel)
        objs = _texture_objects(env)
        for t in texs:
            obj = objs.get(t.path_id)
            if obj is None:
                continue
            dest = before_dir / f"{_safe_name(t)}.png"
            extract_texture(obj.read(), dest)
            items.append(PreviewItem(t.label(), t.key, dest, after_dir / f"{_safe_name(t)}.png"))
            paths.append(dest)
        del env
    if not paths or cancel.is_set():
        return []
    tool = core.load_tool(tool_root)
    opts = core._image_options(tool["images"], dlss, "PNG", 1.0)
    t0 = time.time()
    result = core._convert_images_auto(tool, paths, opts, on_progress, threads, log)
    out: list[PreviewItem] = []
    by_input = {str(Path(ok.input_path).resolve()).casefold(): ok for ok in result.successes}
    for it in items:
        ok = by_input.get(str(it.before.resolve()).casefold())
        if ok is None:
            continue
        shutil.move(str(ok.output_path), str(it.after))
        it.seconds = float(ok.elapsed_seconds)
        out.append(it)
    for f in result.failures:
        log(T("unity.log.failed", name=Path(f.input_path).name, err=str(f.error).splitlines()[0] if f.error else "?"))
    log(T("unity.log.preview_done", n=len(out), elapsed=core.format_eta(time.time() - t0)))
    return out


# ----------------------------------------------------------------------------
# Amélioration
# ----------------------------------------------------------------------------
@dataclass
class ImproveProgress:
    total: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    current: str = ""
    phase: str = ""          # "extract" | "dlss" | "write" | "save"
    container: str = ""
    container_index: int = 0
    container_count: int = 0
    chunk_fraction: float = 0.0
    chunk_size: int = 0
    started: float = field(default_factory=time.time)

    @property
    def live_done(self) -> float:
        return self.done + self.failed + self.chunk_fraction * self.chunk_size

    @property
    def fraction(self) -> float:
        return min(1.0, self.live_done / self.total) if self.total else 0.0

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    @property
    def rate(self) -> float:
        return (self.done + self.failed) / self.elapsed if self.elapsed > 0 and self.done + self.failed else 0.0

    @property
    def eta(self) -> float:
        remaining = self.total - self.live_done
        return remaining / self.rate if self.rate > 0 else 0.0


@dataclass
class ImproveSummary:
    written: int = 0
    inplace: int = 0
    inline: int = 0
    already: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    containers_saved: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    cancelled: bool = False
    elapsed: float = 0.0
    timings: list[float] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.written / self.elapsed if self.elapsed > 0 else 0.0


def improve_textures(a: UnityAnalysis, sel: Selection, s: UnitySettings, dlss: core.DlssSettings, tool_root: Path,
                     log: Callable[[str], None], on_progress: Callable[[ImproveProgress], None], cancel: threading.Event) -> ImproveSummary:
    """Étape 4 : pour chaque conteneur, par lots : extraction PNG → DLSS 1× (pipeline du cœur) → réécriture (même format,
    mêmes dimensions) → sauvegarde du conteneur → manifeste. Reprenable : les textures déjà faites sont sautées."""
    import UnityPy

    t0 = time.time()
    summary = ImproveSummary()
    root = Path(a.root)
    manifest = load_manifest(root)
    done = done_keys(root)
    todo = [t for t in sel.chosen if t.key not in done]
    summary.already = len(sel.chosen) - len(todo)
    by_file: dict[str, list[TexInfo]] = {}
    for t in todo:
        by_file.setdefault(t.file, []).append(t)
    containers = {c.rel: c for c in a.containers}
    p = ImproveProgress(total=len(todo), container_count=len(by_file))
    on_progress(p)
    if not todo:
        summary.elapsed = time.time() - t0
        return summary
    # les conteneurs doivent être sauvegardés avant toute écriture
    bm = set(_load_backup_manifest(root).get("files", []))
    missing = [rel for rel in by_file if rel not in bm or not (root / BACKUP_DIR / rel).is_file()]
    if missing:
        raise RuntimeError(T("unity.err.not_backed_up", files=", ".join(missing[:5]) + ("…" if len(missing) > 5 else "")))

    tool = core.load_tool(tool_root)
    opts = core._image_options(tool["images"], dlss, "PNG", 1.0)
    tmp = Path(tool_root) / "outputs" / "renpyhd_unity_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    last_tick = [0.0]

    def tool_progress(frac: float, msg: str) -> None:
        p.chunk_fraction = frac
        p.current = msg
        now = time.time()
        if now - last_tick[0] > 0.4:
            last_tick[0] = now
            on_progress(p)

    chunk_size = max(1, int(s.chunk or DEFAULT_CHUNK))
    try:
        for ci, (rel, texs) in enumerate(by_file.items(), 1):
            if cancel.is_set():
                summary.cancelled = True
                break
            c = containers[rel]
            p.container, p.container_index = rel, ci
            p.phase = "extract"
            on_progress(p)
            size_before = (root / rel).stat().st_size
            log(T("unity.log.container_start", i=ci, n=len(by_file), file=rel, textures=len(texs), size=core.human_size(size_before)))
            try:
                env = load_env(root / rel)
            except Exception as exc:
                for t in texs:
                    summary.failed.append((t.label(), str(exc).splitlines()[0]))
                    p.failed += 1
                continue
            objs = _texture_objects(env)
            changed_inline = 0
            done_here: list[int] = []
            inline_ids: list[int] = []
            for i in range(0, len(texs), chunk_size):
                if cancel.is_set():
                    summary.cancelled = True
                    break
                chunk = texs[i: i + chunk_size]
                p.phase, p.chunk_size, p.chunk_fraction = "extract", len(chunk), 0.0
                on_progress(p)
                paths: list[Path] = []
                by_path: dict[str, tuple[TexInfo, object]] = {}
                for t in chunk:
                    obj = objs.get(t.path_id)
                    if obj is None:
                        summary.failed.append((t.label(), T("unity.err.gone")))
                        p.failed += 1
                        continue
                    try:
                        d = obj.read()
                        dest = tmp / f"{_safe_name(t)}.png"
                        extract_texture(d, dest)
                    except Exception as exc:
                        summary.failed.append((t.label(), str(exc).splitlines()[0]))
                        p.failed += 1
                        continue
                    paths.append(dest)
                    by_path[str(dest.resolve()).casefold()] = (t, d)
                if not paths:
                    continue
                p.phase = "dlss"
                on_progress(p)
                try:
                    result = core._convert_images_auto(tool, paths, opts, tool_progress, int(s.threads or 0), log)
                except Exception as exc:
                    first = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
                    log(T("unity.log.batch_failed", err=first))
                    for t, _d in by_path.values():
                        summary.failed.append((t.label(), first))
                        p.failed += 1
                    if cancel.is_set():
                        summary.cancelled = True
                        break
                    continue
                p.phase, p.chunk_fraction = "write", 1.0
                on_progress(p)
                from PIL import Image
                for ok in result.successes:
                    hit = by_path.pop(str(Path(ok.input_path).resolve()).casefold(), None)
                    if hit is None:
                        continue
                    t, d = hit
                    try:
                        with Image.open(ok.output_path) as im:
                            im.load()
                            how = write_back(root, c, d, t, im)
                        Path(ok.output_path).unlink(missing_ok=True)
                    except Exception as exc:
                        summary.failed.append((t.label(), T("unity.err.write", err=str(exc).splitlines()[0])))
                        p.failed += 1
                        continue
                    if how == "inplace":
                        summary.inplace += 1
                    else:
                        summary.inline += 1
                        changed_inline += 1
                        inline_ids.append(t.path_id)
                    summary.written += 1
                    summary.timings.append(float(ok.elapsed_seconds))
                    done_here.append(t.path_id)
                    p.done += 1
                retry: list[tuple[TexInfo, object, Path]] = []
                for f in result.failures:
                    hit = by_path.pop(str(Path(f.input_path).resolve()).casefold(), None)
                    first = str(f.error).splitlines()[0] if f.error else "?"
                    if hit is None or (result.cancelled and "Cancelled" in first):
                        continue
                    if not result.cancelled and not cancel.is_set() and "at least 64 pixels" not in first:
                        retry.append((hit[0], hit[1], Path(f.input_path)))
                        continue
                    summary.failed.append((hit[0].label(), first))
                    p.failed += 1
                    log(T("unity.log.failed", name=hit[0].name, err=first))
                # seconde chance, comme le cœur : les échecs du lot sont retentés un par un (session DLSS dédiée, chemin classique)
                for t, d, src in retry:
                    if cancel.is_set():
                        summary.cancelled = True
                        break
                    p.current = T("unity.log.retry", name=t.name)
                    on_progress(p)
                    err = ""
                    try:
                        single = tool["images"].convert_images([src], opts, None)  # type: ignore[attr-defined]
                        if single.successes:
                            with Image.open(single.successes[0].output_path) as im:
                                im.load()
                                how = write_back(root, c, d, t, im)
                            Path(single.successes[0].output_path).unlink(missing_ok=True)
                            if how == "inplace":
                                summary.inplace += 1
                            else:
                                summary.inline += 1
                                changed_inline += 1
                                inline_ids.append(t.path_id)
                            summary.written += 1
                            summary.timings.append(float(single.successes[0].elapsed_seconds))
                            done_here.append(t.path_id)
                            p.done += 1
                            log(T("unity.log.retry_ok", name=t.name))
                            continue
                        err = single.failures[0].error if single.failures else "?"
                    except Exception as exc:
                        err = str(exc)
                    first = str(err).splitlines()[0] if err else "?"
                    summary.failed.append((t.label(), first))
                    p.failed += 1
                    log(T("unity.log.failed", name=t.name, err=first))
                for pth in paths:
                    pth.unlink(missing_ok=True)
                p.chunk_fraction, p.chunk_size = 0.0, 0
                on_progress(p)
                log(T("unity.log.progress", done=p.done, total=p.total, failed=p.failed, rate=f"{p.rate:.2f}", eta=core.format_eta(p.eta)))
                if result.cancelled or cancel.is_set():
                    summary.cancelled = True
                    break
            # sauvegarde du conteneur (même partielle, en cas d'annulation) puis manifeste
            if changed_inline:
                p.phase = "save"
                p.current = rel
                on_progress(p)
                try:
                    n = save_container(root, c, env, log)
                    summary.containers_saved += 1
                    summary.bytes_before += size_before
                    summary.bytes_after += n
                    log(T("unity.log.saved", file=rel, before=core.human_size(size_before), after=core.human_size(n)))
                except Exception as exc:
                    # conteneur intact sur le disque : les textures « inline » de ce fichier ne sont pas faites
                    first = str(exc).splitlines()[0]
                    log(T("unity.log.save_failed", file=rel, err=first))
                    summary.failed.append((rel, T("unity.err.save", err=first)))
                    lost = set(inline_ids)
                    done_here = [pid for pid in done_here if pid not in lost]
                    summary.written -= len(lost)
                    summary.inline -= len(lost)
                    p.failed += len(lost)
                    p.done -= len(lost)
            if done_here:
                lst = manifest.setdefault("done", {}).setdefault(rel, [])
                lst.extend(pid for pid in done_here if pid not in lst)
                save_manifest(root, manifest)
            del env, objs
            if summary.cancelled:
                break
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    manifest.setdefault("runs", []).append({"date": time.strftime("%Y-%m-%d %H:%M:%S"), "written": summary.written, "failed": len(summary.failed),
                                            "cancelled": summary.cancelled, "preset": dlss.dlss_model_preset, "elapsed": round(time.time() - t0, 1)})
    save_manifest(root, manifest)
    summary.elapsed = time.time() - t0
    return summary


# ----------------------------------------------------------------------------
# Vérification : lancer le jeu, attendre, capturer la fenêtre, lire le journal Unity
# ----------------------------------------------------------------------------
_CAPTURE_PS = r'''
Add-Type -AssemblyName System.Drawing
Add-Type -Namespace RHD -Name Cap -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdc, uint flags);
[DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hwnd, int cmd);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
public delegate bool EnumProc(IntPtr hwnd, IntPtr l);
[StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
'@
$targetPid = {pid}
$p = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
if ($null -eq $p) { 'NOPROCESS'; exit }
# fenêtre la plus grande du processus (MainWindowHandle est parfois nul ou de taille nulle au démarrage)
$best = [IntPtr]::Zero; $bestArea = 0
$cb = [RHD.Cap+EnumProc]{ param($hwnd, $l)
    $wp = 0; [RHD.Cap]::GetWindowThreadProcessId($hwnd, [ref]$wp) | Out-Null
    if ($wp -eq $targetPid -and [RHD.Cap]::IsWindowVisible($hwnd)) {
        $rr = New-Object RHD.Cap+RECT; [RHD.Cap]::GetWindowRect($hwnd, [ref]$rr) | Out-Null
        $area = ($rr.Right - $rr.Left) * ($rr.Bottom - $rr.Top)
        if ($area -gt $script:bestArea) { $script:bestArea = $area; $script:best = $hwnd }
    }
    return $true
}
[RHD.Cap]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
$h = if ($best -ne [IntPtr]::Zero) { $best } else { $p.MainWindowHandle }
if ($h -eq 0) { 'NOWINDOW'; exit }
[RHD.Cap]::ShowWindow($h, 9) | Out-Null
[RHD.Cap]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 700
$r = New-Object RHD.Cap+RECT
[RHD.Cap]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.Right - $r.Left; $hh = $r.Bottom - $r.Top
if ($w -lt 8 -or $hh -lt 8) { 'NOSIZE'; exit }
$bmp = New-Object System.Drawing.Bitmap $w, $hh
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
$ok = [RHD.Cap]::PrintWindow($h, $hdc, 2)
$g.ReleaseHdc($hdc)
$method = 'PrintWindow'
$dark = $true
foreach ($x in @(($w/4), ($w/2), (3*$w/4))) { foreach ($y in @(($hh/4), ($hh/2), (3*$hh/4))) { $c = $bmp.GetPixel([int]$x, [int]$y); if (($c.R + $c.G + $c.B) -gt 24) { $dark = $false } } }
if (-not $ok -or $dark) {
  $g.CopyFromScreen($r.Left, $r.Top, 0, 0, $bmp.Size)
  $method = 'BitBlt'
}
$g.Dispose()
$bmp.Save('{dest}', [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
"OK $w $hh $method"
'''


def unity_log_path(a: UnityAnalysis) -> Path | None:
    base = Path(os.environ.get("USERPROFILE", "")) / "AppData" / "LocalLow" / (a.company or "") / (a.product or "")
    for name in ("Player.log", "output_log.txt"):
        if (base / name).is_file():
            return base / name
    return base / "Player.log" if a.company and a.product else None


def _log_issues(text: str) -> list[str]:
    bad = re.compile(r"(crash|corrupt|Failed to|failed to|Could not|cannot be loaded|could not be loaded|is not a valid|"
                     r"Unable to|AssetBundle .* error|Error while reading|The file .* is corrupted|NullReferenceException)", re.IGNORECASE)
    return [ln.strip() for ln in text.splitlines() if bad.search(ln) and "d3d" not in ln.lower() and "reshade" not in ln.lower()][:12]


def verify_launch(a: UnityAnalysis, dest_png: Path, wait_s: int, log: Callable[[str], None], cancel: threading.Event | None = None,
                  keep_running: bool = False) -> dict:
    """Lance l'exécutable du jeu, attend, vérifie que le processus vit, capture sa fenêtre, relit le journal Unity."""
    out: dict = {"ok": False, "alive": False, "screenshot": "", "issues": [], "log": "", "seconds": 0.0, "exit_code": None}
    if a.exe is None or not a.exe.is_file():
        out["error"] = T("unity.verify.no_exe")
        return out
    logp = unity_log_path(a)
    log_before = logp.stat().st_mtime if logp and logp.is_file() else 0.0
    t0 = time.time()
    try:
        proc = subprocess.Popen([str(a.exe), "-screen-fullscreen", "0", "-screen-width", "1280", "-screen-height", "720"],
                                cwd=str(a.exe.parent), close_fds=True)
    except Exception as exc:
        out["error"] = str(exc)
        return out
    log(T("unity.log.launched", exe=a.exe.name, pid=proc.pid))
    deadline = time.time() + max(3, int(wait_s))
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if cancel is not None and cancel.is_set():
            break
        time.sleep(0.5)
    out["exit_code"] = proc.poll()
    out["alive"] = proc.poll() is None
    if out["alive"]:
        dest_png.parent.mkdir(parents=True, exist_ok=True)
        script = _CAPTURE_PS.replace("{pid}", str(proc.pid)).replace("{dest}", str(dest_png).replace("'", "''"))
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", script],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=NO_WINDOW)
            line = (r.stdout.strip().splitlines() or [""])[-1]
            log(T("unity.log.capture", result=line or r.stderr.strip()[:200]))
            if line.startswith("OK") and dest_png.is_file():
                out["screenshot"] = str(dest_png)
                out["capture"] = line
        except Exception as exc:
            log(T("unity.log.capture", result=str(exc)))
        if not keep_running:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    out["seconds"] = time.time() - t0
    if logp and logp.is_file():
        try:
            text = logp.read_text(encoding="utf-8", errors="replace")
            out["log"] = str(logp)
            out["log_updated"] = logp.stat().st_mtime > log_before
            out["issues"] = _log_issues(text[-200_000:])
        except OSError:
            pass
    out["ok"] = bool(out["alive"]) and not out["issues"]
    return out


def open_folder(path: Path) -> None:
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception:
        pass
