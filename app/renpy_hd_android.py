r"""
renpy_hd_android.py - construction d'un APK Android d'un jeu Ren'Py avec le SDK Ren'Py officiel et RAPT.

Tout est mis en cache sous <RenPyHD>\android\ :
  sdk\<version>\renpy-<version>-sdk\   SDK Ren'Py + rapt\ (RAPT, Android SDK dans rapt\Sdk, projet Gradle dans rapt\project)
  jdk\<jdk-xx>\                        JDK Temurin portable (8 pour Ren'Py ≤ 7.6 / 8.1, 21 pour Ren'Py ≥ 7.7 / 8.2)
  keys\android.keystore, bundle.keystore  clés de signature (à sauvegarder : nécessaires pour mettre à jour un APK installé)
  unrpyc\unrpyc-<ver>\                 décompilateur optionnel (1.x pour Ren'Py 7, 2.x pour Ren'Py 8)
  build\<jeu>\                         copie de construction du jeu (jamais l'original), out\<jeu>\ : APK produits
  gradle\                              cache Gradle (GRADLE_USER_HOME), logs\ : journaux

Le SDK est piloté par sa propre ligne de commande (`lib\<plateforme>\python.exe -EO renpy.py launcher <commande>`) :
  - renpyhd_android_installsdk  commande ajoutée par zz_renpyhd_android.rpy (copié dans launcher\game\) : installation
                                non interactive du SDK Android + clés (réponses automatiques aux questions de RAPT) ;
  - android_build               commande officielle du lanceur (distribution + RAPT + Gradle) ; forme 7.0–7.3 :
                                `android_build <projet> assembleRelease --destination <dossier>`, forme 7.4+/8.x :
                                `android_build <projet> [--bundle] --destination <dossier>`.
Les téléchargements (renpy.org, adoptium.net/GitHub, dl.google.com via RAPT, Gradle/Maven via Gradle) sont les seuls
accès réseau ; tout le reste est local.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import renpy_hd_core as core
from renpy_hd_core import T

APP_DIR = Path(__file__).resolve().parent
ANDROID_ROOT = APP_DIR.parent / "android"
SDK_DIR = ANDROID_ROOT / "sdk"
JDK_DIR = ANDROID_ROOT / "jdk"
KEYS_DIR = ANDROID_ROOT / "keys"
UNRPYC_DIR = ANDROID_ROOT / "unrpyc"
BUILD_DIR = ANDROID_ROOT / "build"
OUT_DIR = ANDROID_ROOT / "out"
LOG_DIR = ANDROID_ROOT / "logs"
GRADLE_HOME = ANDROID_ROOT / "gradle"
VERSIONS_CACHE = ANDROID_ROOT / "renpy_versions.json"
MATRIX_FILE = APP_DIR / "android_matrix.json"
ADAPTER_SRC = APP_DIR / "renpyhd_android_adapter.rpy"
ADAPTER_NAME = "zz_renpyhd_android.rpy"
EXTDATA_HOOK_SRC = APP_DIR / "renpyhd_extdata.rpy"     # hook « données externes » (copié dans game\ de la copie de construction)
EXTDATA_HOOK_NAME = "zz_renpyhd_extdata.rpy"
EXTDATA_MANIFEST = "renpyhd_extdata.json"              # manifeste lu par le hook (paquet, fichiers témoins, taille du pack)
BUILD_MANIFEST = "build.json"                          # écrit dans android\out\<jeu>\ après chaque construction (gestionnaire « Mes APK »)
AUDIO_EXTS = (".ogg", ".opus", ".mp3", ".wav", ".flac", ".m4a")
EXT_AUDIO_THRESHOLD = 200 * 1024 * 1024        # au-delà, l'audio est proposé dans le pack de données
APK_SOFT_LIMIT = 2 * 1024 ** 3                 # au-delà : nombreux appareils refusent l'installation (entiers 32 bits signés dans l'installateur)
APK_HARD_LIMIT = 4 * 1024 ** 3                 # limite du format ZIP sans ZIP64 (offsets 32 bits) : un APK ne peut pas dépasser 4 Go
DATA_MODES = ("apk", "external")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RenPyHD/1.1"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ENGINE_OVERHEAD = 45 * 1024 * 1024          # moteur Ren'Py + bibliothèques natives dans l'APK (≈ 35–55 Mo)
EXCLUDED_DIRS = {"saves", "cache", "_dlss_backup", "renpyhd_export", "__pycache__"}
EXCLUDED_DIR_PREFIXES = ("hd2x",)
EXCLUDED_FILE_PREFIXES = ("zz_dlss_hd.rpy", "zz_renpyhd_tl.rpy", "zz_renpyhd_check.rpy", "zz_renpyhd_extdata.rpy", "renpyhd_extdata.json")
ORIENTATIONS = ("sensorLandscape", "portrait", "sensor")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ANDROID_BUNDLE_ENABLED = True       # app bundle (.aab) pour Ren'Py ≥ 7.4 / 8.x
ANDROID_ADB_ENABLED = True          # « Installer sur le téléphone » via platform-tools\adb.exe
ANDROID_UNRPYC_ENABLED = True       # décompilation optionnelle des .rpyc sans .rpy (sur la copie de construction)

_MATRIX: dict | None = None


def log_default(_msg: str) -> None:
    pass


# ----------------------------------------------------------------------------
# Matrice de compatibilité
# ----------------------------------------------------------------------------
def load_matrix() -> dict:
    global _MATRIX
    if _MATRIX is None:
        _MATRIX = json.loads(MATRIX_FILE.read_text(encoding="utf-8"))
    return _MATRIX


def vtuple(version: str) -> tuple[int, int, int]:
    nums = [int(n) for n in re.findall(r"\d+", version or "")[:3]]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def vstr(v: tuple[int, ...]) -> str:
    return ".".join(str(n) for n in v[:3])


def matrix_entry(version: str) -> dict | None:
    v = vtuple(version)
    for entry in load_matrix()["versions"]:
        if vtuple(entry["min"]) <= v <= vtuple(entry["max"]):
            return entry
    return None


def family_for(version: str) -> str:
    entry = matrix_entry(version)
    if entry:
        return entry["family"]
    v = vtuple(version)
    if v < (7, 4, 0):
        return "legacy"
    if v < (7, 7, 0) or (8, 0, 0) <= v < (8, 2, 0):
        return "gradle6"
    return "modern"


def known_versions(refresh: bool = False, timeout: int = 15) -> tuple[list[str], bool]:
    """Versions publiées sur renpy.org/dl/ (cache 7 jours dans android\renpy_versions.json) ; (liste, en_ligne)."""
    cached: list[str] = []
    try:
        if VERSIONS_CACHE.is_file():
            data = json.loads(VERSIONS_CACHE.read_text(encoding="utf-8"))
            cached = list(data.get("versions", []))
            if not refresh and time.time() - float(data.get("fetched", 0)) < 7 * 86400 and cached:
                return cached, True
    except Exception:
        cached = []
    try:
        req = urllib.request.Request(load_matrix()["downloads"]["index_url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", "replace")
        found = sorted({m for m in re.findall(r'href="(\d+\.\d+\.\d+)/"', html)}, key=vtuple)
        if found:
            ANDROID_ROOT.mkdir(parents=True, exist_ok=True)
            VERSIONS_CACHE.write_text(json.dumps({"fetched": time.time(), "versions": found}), encoding="utf-8")
            return found, True
    except Exception:
        pass
    if cached:
        return cached, False
    return list(load_matrix()["known_versions"]), False


def resolve_sdk_version(game_version: str, versions: list[str] | None = None) -> tuple[str, str]:
    """SDK à utiliser pour un jeu : (version, raison) — raison ∈ exact, same_minor, same_major, unsupported, unknown."""
    v = vtuple(game_version)
    if v < vtuple(load_matrix()["fallback"]["min_supported"]):
        return "", "unsupported"
    if versions is None:
        versions, _online = known_versions()
    vs = sorted({vtuple(x) for x in versions})
    if v in vs:
        return vstr(v), "exact"
    same_minor = [x for x in vs if x[:2] == v[:2] and x > v]
    if same_minor:
        return vstr(same_minor[0]), "same_minor"
    same_major = [x for x in vs if x[0] == v[0]]
    if same_major:
        return vstr(same_major[-1]), "same_major"
    return "", "unknown"


def installed_sdk_versions() -> list[str]:
    out = []
    if SDK_DIR.is_dir():
        for d in SDK_DIR.iterdir():
            if d.is_dir() and (d / f"renpy-{d.name}-sdk" / "renpy.py").is_file():
                out.append(d.name)
    return sorted(out, key=vtuple)


# ----------------------------------------------------------------------------
# Analyse du jeu
# ----------------------------------------------------------------------------
@dataclass
class AndroidAnalysis:
    root: Path
    game: Path
    version: str = ""
    family: str = ""
    sdk_version: str = ""
    sdk_reason: str = ""
    online: bool = True
    rpy_count: int = 0
    rpyc_count: int = 0
    rpy_missing: list[str] = field(default_factory=list)
    rpa_count: int = 0
    rpa_bytes: int = 0
    rpa_extracted: list[str] = field(default_factory=list)     # archives dont toutes les entrées existent déjà en fichiers libres
    rpa_extracted_bytes: int = 0
    has_hd2x: bool = False
    hd2x_dir: Path | None = None      # dossier des sorties DLSS (hd2x/, avec factor.txt)
    hd2x_bytes: int = 0
    hd2x_count: int = 0
    hd2x_factor: float = 2.0
    has_backup: bool = False
    has_hook: bool = False
    has_tl: bool = False
    images_bytes: int = 0
    images_count: int = 0
    videos_bytes: int = 0
    videos_count: int = 0
    audio_bytes: int = 0
    audio_count: int = 0
    gui_images_bytes: int = 0        # images sous game/gui (toujours dans l'APK : menus)
    other_bytes: int = 0
    excluded_bytes: int = 0
    existing_json: dict | None = None
    config_name: str = ""
    build_name: str = ""
    config_version: str = ""
    window_icon: Path | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def included_bytes(self) -> int:
        return self.images_bytes + self.videos_bytes + self.other_bytes

    def estimated_apk(self, include_videos: bool, image_budget: int = 0, skip_extracted_rpa: bool = True) -> int:
        images = min(self.images_bytes, image_budget) if image_budget else self.images_bytes
        other = self.other_bytes - (self.rpa_extracted_bytes if skip_extracted_rpa else 0)
        return images + (self.videos_bytes if include_videos else 0) + other + ENGINE_OVERHEAD

    def estimated_split(self, ext_audio: bool, skip_extracted_rpa: bool = True, image_mode: str = "original") -> tuple[int, int]:
        """Mode « données séparées » : (APK léger, pack de données). Les images de gui/ restent dans l'APK.
        image_mode improved : même ordre de grandeur que les originales (qualité 92) ; hd2x : dossier hd2x ajouté au pack."""
        other = self.other_bytes - (self.rpa_extracted_bytes if skip_extracted_rpa else 0)
        rpa_left = self.rpa_bytes - (self.rpa_extracted_bytes if skip_extracted_rpa else 0)
        pack = (self.images_bytes - self.gui_images_bytes) + self.videos_bytes + (self.audio_bytes if ext_audio else 0) + rpa_left
        if image_mode == "hd2x":
            pack += self.hd2x_bytes
        apk = other - rpa_left - (self.audio_bytes if ext_audio else 0) + self.gui_images_bytes + ENGINE_OVERHEAD
        return max(apk, ENGINE_OVERHEAD), max(pack, 0)


def _is_excluded_dir(name: str) -> bool:
    low = name.lower()
    return low in EXCLUDED_DIRS or low.startswith(EXCLUDED_DIR_PREFIXES)


def _is_excluded_file(name: str) -> bool:
    return name.lower().startswith(EXCLUDED_FILE_PREFIXES)


def _read_define(text: str, name: str) -> str:
    m = re.search(r"^\s*define\s+" + re.escape(name) + r"\s*=\s*_?\(?\s*([\"'])(.*?)\1", text, re.MULTILINE)
    return m.group(2) if m else ""


def analyze_game(root: str, refresh_versions: bool = False) -> AndroidAnalysis:
    game = core.find_game_dir(root)
    a = AndroidAnalysis(root=game.parent, game=game)
    a.version = core.detect_renpy_version(a.root)
    clean = vstr(vtuple(a.version)) if re.search(r"\d+\.\d+", a.version) else ""
    a.family = family_for(clean) if clean else "modern"
    versions, a.online = known_versions(refresh=refresh_versions)
    if clean:
        a.sdk_version, a.sdk_reason = resolve_sdk_version(clean, versions)
    else:
        a.sdk_version, a.sdk_reason = "", "unknown"
    rpy: set[str] = set()
    rpyc: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(game):
        dirnames[:] = [d for d in dirnames if not _is_excluded_dir(d)]
        rel_dir = Path(dirpath).relative_to(game)
        for fn in filenames:
            if _is_excluded_file(fn):
                continue
            p = Path(dirpath) / fn
            try:
                size = p.stat().st_size
            except OSError:
                continue
            ext = p.suffix.lower()
            rel = (rel_dir / fn).as_posix()
            if ext == ".rpy":
                rpy.add(rel[:-4])
            elif ext == ".rpyc":
                rpyc.add(rel[:-5])
            elif ext == ".rpa":
                a.rpa_count += 1
                a.rpa_bytes += size
            if ext in core.IMAGE_EXTS:
                a.images_bytes += size
                a.images_count += 1
                if rel.lower().startswith("gui/"):
                    a.gui_images_bytes += size
            elif ext in core.VIDEO_EXTS:
                a.videos_bytes += size
                a.videos_count += 1
            elif ext == ".rpy":
                continue        # jamais empaqueté (liste noire RAPT)
            else:
                if ext in AUDIO_EXTS:
                    a.audio_bytes += size
                    a.audio_count += 1
                a.other_bytes += size
    if a.rpa_count:
        try:
            import renpy_hd_tools as tools
            _g, infos = tools.list_rpas(str(a.root))
            for info in infos:
                if not info.error and info.entries and info.already_loose >= info.entries:
                    a.rpa_extracted.append(info.name)
                    a.rpa_extracted_bytes += info.size
        except Exception as exc:
            a.notes.append(f"rpa: {exc}")
    for d in game.iterdir():
        if d.is_dir() and _is_excluded_dir(d.name):
            low = d.name.lower()
            if low.startswith("hd2x") and (d / "factor.txt").is_file() and (a.hd2x_dir is None or d.name.lower() == "hd2x"):
                a.has_hd2x = True
                a.hd2x_dir = d
                try:
                    a.hd2x_bytes = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                    a.hd2x_count = sum(1 for f in d.rglob("*") if f.is_file() and f.suffix.lower() in core.IMAGE_EXTS)
                    a.hd2x_factor = float((d / "factor.txt").read_text(encoding="utf-8").strip() or "2")
                except Exception:
                    pass
            if low == core.BACKUP_DIR:
                a.has_backup = True
            try:
                a.excluded_bytes += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            except OSError:
                pass
    a.has_hook = (game / core.HOOK_NAME).is_file()
    a.has_tl = (game / "tl").is_dir() and any(p.is_dir() and p.name != "None" for p in (game / "tl").iterdir())
    a.rpy_count, a.rpyc_count = len(rpy), len(rpyc)
    a.rpy_missing = sorted(x + ".rpyc" for x in rpyc if x not in rpy and not x.startswith("tl/"))
    json_file = a.root / ".android.json"
    if json_file.is_file():
        try:
            a.existing_json = json.loads(json_file.read_text(encoding="utf-8"))
        except Exception:
            a.existing_json = None
    opts = game / "options.rpy"
    if opts.is_file():
        text = opts.read_text(encoding="utf-8", errors="ignore")
        a.config_name = _read_define(text, "config.name")
        a.build_name = _read_define(text, "build.name")
        a.config_version = _read_define(text, "config.version")
    for cand in ("gui/window_icon.png", "gui/icon.png", "icon.png"):
        if (game / cand).is_file():
            a.window_icon = game / cand
            break
    return a


# ----------------------------------------------------------------------------
# SDK / JDK installés
# ----------------------------------------------------------------------------
@dataclass
class SdkInfo:
    version: str
    root: Path
    python: Path | None
    rapt: Path
    family: str
    jdk_major: str
    sdk_tools: str
    keys_mode: str            # rapt (android.keystore dans rapt\) ou project (android.keystore + bundle.keystore dans le projet)
    legacy_build: bool        # android_build <projet> <commandes gradle> (7.0–7.3)
    supports_bundle: bool
    adb: Path
    apksigner: Path | None
    adapter_ok: bool
    sdk_installed: bool

    @property
    def ready(self) -> bool:
        return self.python is not None and self.sdk_installed and self.adapter_ok and keys_present(self)


def sdk_root_for(version: str) -> Path:
    return SDK_DIR / version / f"renpy-{version}-sdk"


def inspect_sdk(root: Path, version: str = "") -> SdkInfo | None:
    if not (root / "renpy.py").is_file():
        return None
    if not version:
        m = re.search(r"renpy-([\d.]+)-sdk", root.name)
        version = m.group(1) if m else core.detect_renpy_version(root)
    family = family_for(version)
    python = None
    for rel in ("lib/py3-windows-x86_64/python.exe", "lib/py2-windows-x86_64/python.exe", "lib/windows-x86_64/python.exe", "lib/windows-i686/python.exe"):
        if (root / rel).is_file():
            python = root / rel
            break
    rapt = root / "rapt"
    plat_text = ""
    plat_py = rapt / "buildlib" / "rapt" / "plat.py"
    if plat_py.is_file():
        plat_text = plat_py.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"jdk_requirement\s*=\s*(\d+)", plat_text)
    jdk_major = m.group(1) if m else ("8" if (rapt / "buildlib" / "CheckJDK8.java").is_file() or not plat_text else load_matrix()["families"][family]["jdk"])
    m = re.search(r"sdk_version\s*=\s*[\"']([^\"']+)[\"']", plat_text)
    sdk_tools = m.group(1) if m else ""
    keys_mode = "project" if (rapt / "buildlib" / "rapt" / "keys.py").is_file() else "rapt"
    android_rpy = root / "launcher" / "game" / "android.rpy"
    android_text = android_rpy.read_text(encoding="utf-8", errors="ignore") if android_rpy.is_file() else ""
    legacy_build = "gradle_commands" in android_text
    supports_bundle = "--bundle" in android_text
    sdk = rapt / "Sdk"
    adb = sdk / "platform-tools" / "adb.exe"
    apksigner = None
    bt = sdk / "build-tools"
    if bt.is_dir():
        cands = sorted((d for d in bt.iterdir() if (d / "apksigner.bat").is_file()), key=lambda d: vtuple(d.name))
        if cands:
            apksigner = cands[-1] / "apksigner.bat"
    adapter = root / "launcher" / "game" / ADAPTER_NAME
    adapter_ok = adapter.is_file() and ADAPTER_SRC.is_file() and adapter.read_bytes() == ADAPTER_SRC.read_bytes()
    installed = adb.is_file() and (sdk / "platforms").is_dir() and (rapt / "project").is_dir()
    return SdkInfo(version, root, python, rapt, family, jdk_major, sdk_tools, keys_mode, legacy_build, supports_bundle, adb, apksigner, adapter_ok, installed)


def keys_present(sdk: SdkInfo) -> bool:
    if sdk.keys_mode == "rapt":
        return (sdk.rapt / "android.keystore").is_file() or (KEYS_DIR / "android.keystore").is_file()
    return (KEYS_DIR / "android.keystore").is_file()


def find_jdk(major: str) -> Path | None:
    if not JDK_DIR.is_dir():
        return None
    for d in sorted(JDK_DIR.iterdir()):
        if not (d / "bin" / "java.exe").is_file():
            continue
        release = d / "release"
        text = release.read_text(encoding="utf-8", errors="ignore") if release.is_file() else ""
        m = re.search(r'JAVA_VERSION="([^"]+)"', text)
        ver = m.group(1) if m else d.name
        head = ver.split(".")
        found = head[1] if head[0] == "1" and len(head) > 1 else head[0]
        if found == major:
            return d
    return None


def unrpyc_for(sdk: SdkInfo) -> Path | None:
    key = "py2" if sdk.python and "i686" in str(sdk.python) or (sdk.python and "py2" in str(sdk.python)) or vtuple(sdk.version)[0] < 8 else "py3"
    d = UNRPYC_DIR / load_matrix()["downloads"]["unrpyc"][key]["dir"]
    return d / "unrpyc.py" if (d / "unrpyc.py").is_file() else None


def env_status(version: str, manual_sdk: str = "") -> dict:
    root = Path(manual_sdk) if manual_sdk else sdk_root_for(version)
    sdk = inspect_sdk(root, version if not manual_sdk else "")
    jdk = find_jdk(sdk.jdk_major) if sdk else find_jdk(load_matrix()["families"][family_for(version)]["jdk"])
    return {
        "sdk": sdk, "sdk_present": sdk is not None, "rapt_present": bool(sdk and (sdk.rapt / "android.py").is_file()),
        "jdk": jdk, "android_installed": bool(sdk and sdk.sdk_installed), "keys": bool(sdk and keys_present(sdk)),
        "unrpyc": bool(sdk and unrpyc_for(sdk)), "ready": bool(sdk and sdk.ready and jdk),
    }


# ----------------------------------------------------------------------------
# Téléchargements et exécution du lanceur
# ----------------------------------------------------------------------------
@dataclass
class Progress:
    phase: str = ""
    fraction: float = 0.0
    detail: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    elapsed: float = 0.0


class Cancelled(Exception):
    pass


def download_file(url: str, dest: Path, log: Callable[[str], None], on_progress: Callable[[int, int], None] | None,
                  cancel: threading.Event | None, timeout: int = 120) -> Path:
    """Télécharge `url` dans `dest` (.part puis renommage) ; si `dest` existe déjà et n'est pas vide, ne fait rien."""
    if dest.is_file() and dest.stat().st_size > 0:
        log(T("android.log.cached", file=dest.name))
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    log(T("android.log.download", url=url))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        last = -1.0
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if on_progress and (time.time() - last > 0.3):
                last = time.time()
                on_progress(done, total)
    if on_progress:
        on_progress(done, total or done)
    part.replace(dest)
    log(T("android.log.downloaded", file=dest.name, size=core.human_size(dest.stat().st_size)))
    return dest


def extract_zip(zip_path: Path, dest_dir: Path, log: Callable[[str], None], cancel: threading.Event | None,
                on_progress: Callable[[int, int], None] | None = None) -> list[str]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        tops = sorted({n.split("/")[0] for n in names})
        log(T("android.log.extract", file=zip_path.name, n=len(names), dest=dest_dir))
        for i, info in enumerate(z.infolist()):
            if cancel is not None and cancel.is_set() and i % 50 == 0:
                raise Cancelled()
            z.extract(info, dest_dir)
            if on_progress and i % 200 == 0:
                on_progress(i, len(names))
    return tops


def launcher_env(jdk: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONNOUSERSITE", None)
    if jdk is not None:
        env["JAVA_HOME"] = str(jdk)
        env["PATH"] = str(jdk / "bin") + os.pathsep + env.get("PATH", "")
    env["RAPT_NO_TERMS"] = "1"
    GRADLE_HOME.mkdir(parents=True, exist_ok=True)
    env["GRADLE_USER_HOME"] = str(GRADLE_HOME)
    env["GRADLE_OPTS"] = (env.get("GRADLE_OPTS", "") + " -Dorg.gradle.daemon=false").strip()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def launcher_command(sdk: SdkInfo, args: list[str]) -> list[str]:
    if sdk.python is None:
        raise RuntimeError(T("android.err.no_sdk_python", root=sdk.root))
    return [str(sdk.python), "-EO", "renpy.py", "launcher", *args]


def _kill_tree(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW, timeout=30)
    except Exception:
        pass


def run_launcher(sdk: SdkInfo, jdk: Path | None, args: list[str], log: Callable[[str], None], cancel: threading.Event | None,
                 on_line: Callable[[str], None] | None = None, timeout: int = 4 * 3600, log_file: Path | None = None) -> tuple[int, list[str]]:
    """Exécute une commande du lanceur du SDK ; chaque ligne (sans codes ANSI) va dans `log` et `on_line`. Renvoie (code, lignes)."""
    cmd = launcher_command(sdk, args)
    log("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    lines: list[str] = []
    lf = open(log_file, "a", encoding="utf-8", errors="replace") if log_file else None
    proc = subprocess.Popen(cmd, cwd=str(sdk.root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=NO_WINDOW, env=launcher_env(jdk))
    spam = re.compile(r"^(Writing the .* package\. - |\[download\] \d+% )")
    spam_count = 0

    def emit(raw: str) -> None:
        nonlocal spam_count
        line = ANSI_RE.sub("", raw).rstrip()
        if not line.strip():
            return
        if lf:
            lf.write(line + "\n")
        lines.append(line)
        if on_line:
            on_line(line)
        if spam.match(line):
            spam_count += 1
            if spam_count % 100 != 1:
                return
        try:
            log(line)
        except Exception:
            pass

    def reader() -> None:
        assert proc.stdout is not None
        buf = b""
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                m = re.search(rb"[\r\n]", buf)
                if not m:
                    break
                piece, buf = buf[:m.start()], buf[m.end():]
                emit(piece.decode("utf-8", "replace"))
        if buf:
            emit(buf.decode("utf-8", "replace"))

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    t0 = time.time()
    killed = False
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            _kill_tree(proc.pid)
            killed = True
            break
        if time.time() - t0 > timeout:
            _kill_tree(proc.pid)
            lines.append(T("android.log.timeout"))
            break
        time.sleep(0.25)
    proc.wait()
    th.join(timeout=5)
    if lf:
        lf.close()
    if killed:
        raise Cancelled()
    return proc.returncode or 0, lines


# ----------------------------------------------------------------------------
# Étape 2 : préparer l'environnement (SDK Ren'Py + RAPT + JDK + Android SDK + clés + unrpyc)
# ----------------------------------------------------------------------------
@dataclass
class PrepResult:
    sdk: SdkInfo | None = None
    jdk: Path | None = None
    elapsed: float = 0.0
    messages: list[str] = field(default_factory=list)
    cancelled: bool = False
    error: str = ""
    keys_created: bool = False


def _tools_archive_name(sdk: SdkInfo) -> str:
    inst = sdk.rapt / "buildlib" / "rapt" / "install_sdk.py"
    text = inst.read_text(encoding="utf-8", errors="ignore") if inst.is_file() else ""
    m = re.search(r'archive\s*=\s*"([^"]*win[^"]*)"\.format\(plat\.sdk_version\)', text)
    if m and sdk.sdk_tools:
        return m.group(1).replace("{}", sdk.sdk_tools)
    fam = load_matrix()["families"][sdk.family]
    return fam["android_tools_archive"].format(sdk_tools=sdk.sdk_tools)


def install_adapter(sdk: SdkInfo) -> None:
    dest = sdk.root / "launcher" / "game" / ADAPTER_NAME
    shutil.copy2(ADAPTER_SRC, dest)
    for stale in (dest.with_suffix(".rpyc"),):
        if stale.is_file():
            stale.unlink()
    sdk.adapter_ok = True


LEGACY_PATCH_MARK = "RenPyHD-legacy-patch-2"


def patch_legacy_rapt(sdk: SdkInfo, log: Callable[[str], None]) -> bool:
    r"""RAPT 7.0–7.3 : le projet Gradle dépend de com.danikula.expansion (dépôt bintray fermé en 2021) uniquement pour
    l'expansion Google Play (jamais utilisée par RenPyHD). Selon la version, la dépendance est dans
    prototype
enpyandroiduild.gradle (7.3) ou dans templatespp-build.gradle (7.0–7.2, rendu dans projectpp à chaque
    build). On retire ces lignes de tous les .gradle (prototype, templates, project), les dépôts bintray/jcenter, on ajoute
    mavenCentral(), on supprime les classes Downloader* et on marque prototypeuild.txt pour que RAPT recopie le prototype.
    Idempotent ; True si patché."""
    proto = sdk.rapt / "prototype"
    root_gradle = proto / "build.gradle"
    if sdk.family != "legacy" or not root_gradle.is_file():
        return False
    bt = proto / "build.txt"
    stamp = bt.read_text(encoding="utf-8", errors="ignore") if bt.is_file() else ""
    if LEGACY_PATCH_MARK in stamp:
        return True
    gradle_files: list[Path] = []
    for base in (proto, sdk.rapt / "templates", sdk.rapt / "project"):
        if base.is_dir():
            gradle_files += [p for p in base.rglob("*.gradle") if "build" not in p.relative_to(base).parts[:-1]]
    touched = []
    for gf in gradle_files:
        try:
            s = gf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        orig = s
        s = re.sub(r"\n[ \t]*maven \{[^\n]*bintray[^\n]*\}[ \t]*", "", s)
        s = "".join(ln for ln in s.splitlines(True) if "com.danikula" not in ln and "bintray" not in ln)
        s = s.replace("jcenter()", "mavenCentral()")
        if "google()" in s and "mavenCentral()" not in s:
            s = s.replace("google()", "google()\n        mavenCentral()")
        if s != orig:
            gf.write_text(s, encoding="utf-8")
            touched.append(str(gf.relative_to(sdk.rapt)))
    removed = []
    for base in (proto, sdk.rapt / "project"):
        if base.is_dir():
            for f in list(base.rglob("Downloader*.java")):
                f.unlink()
                removed.append(f.name)
    # build.txt marqué : copy_project() de RAPT (update_always) recopiera le prototype dans rapt\project en conservant local.properties
    bt.write_text(stamp.rstrip("\n") + "\n" + LEGACY_PATCH_MARK + "\n", encoding="utf-8")
    log(T("android.log.legacy_patch", removed=", ".join(sorted(set(removed)) + touched) or "—"))
    return True


def sync_keys(sdk: SdkInfo, log: Callable[[str], None]) -> bool:
    r"""Garde une seule paire de clés dans android\keys et la met là où RAPT la cherche. Renvoie True si les clés existent."""
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    main = KEYS_DIR / "android.keystore"
    bundle = KEYS_DIR / "bundle.keystore"
    rapt_key = sdk.rapt / "android.keystore"
    if not main.is_file() and rapt_key.is_file():
        shutil.copy2(rapt_key, main)
        log(T("android.log.keys_saved", dir=KEYS_DIR))
    if main.is_file() and not bundle.is_file():
        shutil.copy2(main, bundle)
    if main.is_file() and sdk.keys_mode == "rapt":
        if not rapt_key.is_file() or rapt_key.read_bytes() != main.read_bytes():
            shutil.copy2(main, rapt_key)
    return main.is_file()


def prepare_environment(version: str, org: str, with_unrpyc: bool, log: Callable[[str], None], on_progress: Callable[[Progress], None],
                        cancel: threading.Event, manual_sdk: str = "") -> PrepResult:
    t0 = time.time()
    res = PrepResult()
    mx = load_matrix()["downloads"]
    steps = 7
    p = Progress()

    def step(i: int, phase: str, detail: str = "") -> None:
        p.phase, p.detail, p.fraction, p.elapsed = phase, detail, i / steps, time.time() - t0
        p.bytes_done = p.bytes_total = 0
        on_progress(p)

    def dl_progress(i: int):
        def cb(done: int, total: int) -> None:
            p.bytes_done, p.bytes_total = done, total
            p.fraction = (i + (done / total if total else 0)) / steps
            p.elapsed = time.time() - t0
            on_progress(p)
        return cb

    try:
        ANDROID_ROOT.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if manual_sdk:
            root = Path(manual_sdk)
            sdk = inspect_sdk(root)
            if sdk is None:
                raise RuntimeError(T("android.err.manual_sdk", root=root))
            version = sdk.version
            log(T("android.log.manual_sdk", root=root, version=version))
        else:
            # 1. SDK Ren'Py
            step(0, T("android.phase.sdk"))
            root = sdk_root_for(version)
            if not (root / "renpy.py").is_file():
                zip_path = ANDROID_ROOT / "downloads" / f"renpy-{version}-sdk.zip"
                download_file(mx["sdk_url"].format(ver=version), zip_path, log, dl_progress(0), cancel)
                tops = extract_zip(zip_path, SDK_DIR / version, log, cancel)
                if not (root / "renpy.py").is_file() and tops:
                    cand = SDK_DIR / version / tops[0]
                    if (cand / "renpy.py").is_file():
                        cand.rename(root)
            else:
                log(T("android.log.sdk_present", root=root))
            # 2. RAPT
            step(1, T("android.phase.rapt"))
            if not (root / "rapt" / "android.py").is_file():
                zip_path = ANDROID_ROOT / "downloads" / f"renpy-{version}-rapt.zip"
                download_file(mx["rapt_url"].format(ver=version), zip_path, log, dl_progress(1), cancel)
                extract_zip(zip_path, root, log, cancel)
            else:
                log(T("android.log.rapt_present"))
            sdk = inspect_sdk(root, version)
            if sdk is None:
                raise RuntimeError(T("android.err.sdk_broken", root=root))
        install_adapter(sdk)
        patch_legacy_rapt(sdk, log)
        log(T("android.log.sdk_info", version=sdk.version, family=sdk.family, jdk=sdk.jdk_major, tools=sdk.sdk_tools or "?",
              keys=sdk.keys_mode, python=sdk.python.relative_to(sdk.root).as_posix() if sdk.python else "?"))
        # 3. JDK
        step(2, T("android.phase.jdk", major=sdk.jdk_major))
        jdk = find_jdk(sdk.jdk_major)
        if jdk is None:
            info = mx["jdk"].get(sdk.jdk_major)
            if info is None:
                raise RuntimeError(T("android.err.jdk_unknown", major=sdk.jdk_major))
            zip_path = JDK_DIR / "downloads" / info["file"]
            download_file(info["url"], zip_path, log, dl_progress(2), cancel)
            extract_zip(zip_path, JDK_DIR, log, cancel)
            jdk = find_jdk(sdk.jdk_major)
            if jdk is None:
                raise RuntimeError(T("android.err.jdk_extract", dir=JDK_DIR))
        res.jdk = jdk
        log(T("android.log.jdk", path=jdk))
        # 4. Outils Android (pré-téléchargés pour RAPT, qui les trouve en place)
        step(3, T("android.phase.tools"))
        if not sdk.sdk_installed:
            archive = _tools_archive_name(sdk)
            if archive:
                download_file(mx["android_repository"].format(archive=archive), sdk.rapt / archive, log, dl_progress(3), cancel)
        # 5. Installation du SDK Android + clés (commande non interactive du lanceur)
        step(4, T("android.phase.install"))
        had_keys = keys_present(sdk)
        sync_keys(sdk, log)
        if not sdk.sdk_installed or not keys_present(sdk):
            args = ["renpyhd_android_installsdk", "--org", org or "RenPyHD"]
            if sdk.keys_mode == "project":
                args += ["--keys-dir", str(KEYS_DIR)]
            rc, lines = run_launcher(sdk, jdk, args, log, cancel, timeout=3600, log_file=LOG_DIR / f"install_{sdk.version}.log")
            ok = any("RENPYHD_INSTALL_OK" in ln for ln in lines)
            if rc != 0 or not ok:
                fail = [ln for ln in lines if "RENPYHD_FAIL" in ln or "Error" in ln or "error:" in ln.lower()]
                raise RuntimeError(T("android.err.install_failed", detail=(fail[-1] if fail else lines[-1] if lines else f"rc={rc}")))
            sdk = inspect_sdk(sdk.root, sdk.version) or sdk
        else:
            log(T("android.log.android_present"))
        # 6. Clés
        step(5, T("android.phase.keys"))
        if not sync_keys(sdk, log):
            raise RuntimeError(T("android.err.no_keys", dir=KEYS_DIR))
        res.keys_created = not had_keys
        # 7. unrpyc (optionnel)
        step(6, T("android.phase.unrpyc"))
        if with_unrpyc and ANDROID_UNRPYC_ENABLED and unrpyc_for(sdk) is None:
            key = "py2" if vtuple(sdk.version)[0] < 8 else "py3"
            info = mx["unrpyc"][key]
            zip_path = UNRPYC_DIR / "downloads" / f"unrpyc-v{info['version']}.zip"
            download_file(info["url"], zip_path, log, dl_progress(6), cancel)
            extract_zip(zip_path, UNRPYC_DIR, log, cancel)
        step(7, T("android.phase.done"))
        res.sdk = sdk
    except Cancelled:
        res.cancelled = True
    except Exception as exc:
        res.error = str(exc)
        log(T("android.log.error", err=exc))
    res.elapsed = time.time() - t0
    return res


# ----------------------------------------------------------------------------
# Étape 3 : configuration (.android.json, icônes) et copie de construction
# ----------------------------------------------------------------------------
@dataclass
class BuildConfig:
    name: str = ""
    package: str = ""
    icon_name: str = ""
    version: str = "1.0"
    numeric_version: int = 100
    orientation: str = "sensorLandscape"
    internet: bool = False
    include_videos: bool = False
    image_budget_mb: int = 0
    icon_path: str = ""
    bundle: bool = False
    decompile: bool = False
    skip_extracted_rpa: bool = True
    prefer_rpyc: bool = False        # scripts compilés du jeu tels quels (les .rpy ayant un .rpyc ne sont pas copiés : pas de recompilation)
    org: str = "RenPyHD"
    data_mode: str = "apk"           # apk : tout dans l'APK (limite ≈ 2 Go) ; external : APK léger + pack de données (images, vidéos, gros audio)
    ext_audio: bool = False          # external : l'audio va aussi dans le pack
    link_pack: bool = True           # external : liens physiques NTFS vers les fichiers du jeu quand le pack est sur le même disque (instantané, 0 octet)
    arm64_legacy: bool = False       # jeu Ren'Py 7.0–7.3 : décompiler (unrpyc) et construire avec le SDK ARM64_LEGACY_SDK (arm64-v8a) au lieu du RAPT d'origine
    image_mode: str = "original"     # original | improved (hd2x réduit à la taille d'origine, Lanczos) | hd2x (dossier hd2x complet + hook zz_dlss_hd.rpy)
    hd2x_cache_mb: int = 512         # hd2x : config.image_cache_size_mb écrit dans le hook (1536 sur PC : trop pour un téléphone)


IMAGE_MODES = ("original", "improved", "hd2x")
IMPROVED_QUALITY = 92
HD2X_ANDROID_CACHE_MB = 512


ARM64_LEGACY_SDK = "7.8.7"           # dernier Ren'Py 7 (Python 2) : produit arm64-v8a + armeabi-v7a + x86_64 ; recompile les .rpy décompilés


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "", (name or "game").lower().replace("'", ""))
    if not s or not s[0].isalpha():
        s = "game" + s
    return s[:40]


def numeric_from_version(version: str) -> int:
    v = 0
    for part in re.findall(r"\d+", version)[:3]:
        v = v * 100 + min(int(part), 99)
    return max(1, min(v, 2_000_000_000)) if v else 100


def sdk_matches_game(sdk_version: str, game_version: str) -> bool:
    return vtuple(sdk_version) == vtuple(game_version)


def default_config(a: AndroidAnalysis, sdk_version: str = "") -> BuildConfig:
    cfg = BuildConfig()
    cfg.prefer_rpyc = bool(sdk_version) and not sdk_matches_game(sdk_version, a.version) and a.rpyc_count > 0
    ex = a.existing_json or {}
    name = a.config_name or a.build_name or a.root.name
    cfg.name = str(ex.get("name") or name)
    cfg.icon_name = str(ex.get("icon_name") or (a.build_name or name))[:30]
    cfg.package = str(ex.get("package") or f"com.renpyhd.{slug(a.build_name or name)}")
    nums = re.findall(r"\d+", a.config_version or "")
    cfg.version = str(ex.get("version") or (".".join(nums[:3]) if nums else "1.0"))
    if not re.match(r"^[\d.]+$", cfg.version):
        cfg.version = "1.0"
    cfg.numeric_version = int(ex.get("numeric_version") or numeric_from_version(cfg.version))
    cfg.orientation = str(ex.get("orientation") or "sensorLandscape")
    if cfg.orientation == "landscape":
        cfg.orientation = "sensorLandscape"
    cfg.internet = "INTERNET" in (ex.get("permissions") or [])
    cfg.icon_path = str(a.window_icon) if a.window_icon else ""
    cfg.decompile = bool(a.rpy_missing)
    cfg.data_mode = "external" if a.estimated_apk(False) > APK_SOFT_LIMIT else "apk"
    cfg.ext_audio = a.audio_bytes > EXT_AUDIO_THRESHOLD
    cfg.image_mode = "improved" if a.hd2x_dir is not None else "original"
    cfg.hd2x_cache_mb = HD2X_ANDROID_CACHE_MB
    return cfg


def validate_config(cfg: BuildConfig) -> list[str]:
    errs = []
    if not cfg.name.strip():
        errs.append(T("android.cfg.err_name"))
    pkg = cfg.package.strip().lower()
    if "." not in pkg or " " in pkg or not all(re.match(r"^[a-z_]\w*$", p) for p in pkg.split(".")):
        errs.append(T("android.cfg.err_package"))
    if not re.match(r"^\d+(\.\d+)*$", cfg.version.strip()):
        errs.append(T("android.cfg.err_version"))
    if int(cfg.numeric_version) <= 0:
        errs.append(T("android.cfg.err_numeric"))
    if cfg.orientation not in ORIENTATIONS:
        errs.append(T("android.cfg.err_orientation"))
    if cfg.data_mode not in DATA_MODES:
        errs.append(T("android.cfg.err_data_mode"))
    return errs


def build_name_for(cfg: BuildConfig) -> str:
    return slug(cfg.package.rsplit(".", 1)[-1] or cfg.name)


def pack_dir_for(cfg: BuildConfig) -> Path:
    """Dossier du pack de données : android\\out\\<jeu>\\<paquet>-data\\ (contient game\\)."""
    return OUT_DIR / build_name_for(cfg) / f"{cfg.package.strip().lower()}-data"


def phone_data_path(package: str) -> str:
    """Chemin sur le téléphone où le moteur Ren'Py cherche les données externes (ANDROID_PUBLIC/game)."""
    return f"/sdcard/Android/data/{package}/files/game"


def android_json(cfg: BuildConfig, sdk: SdkInfo) -> dict:
    perms = ["VIBRATE"] + (["INTERNET"] if cfg.internet else [])
    d = {"package": cfg.package.strip().lower(), "name": cfg.name.strip(), "icon_name": (cfg.icon_name or cfg.name).strip(),
         "version": cfg.version.strip(), "numeric_version": str(int(cfg.numeric_version)) if sdk.family == "legacy" else int(cfg.numeric_version),
         "orientation": cfg.orientation, "permissions": perms, "include_pil": False, "include_sqlite": False, "layout": None,
         "source": False, "expansion": False, "google_play_key": None, "google_play_salt": None, "store": "none",
         "update_icons": True, "update_always": True}
    if sdk.family != "legacy":
        d["heap_size"] = None
        d["update_keystores"] = True
    return d


def make_icons(build_dir: Path, icon_path: str, log: Callable[[str], None]) -> None:
    from PIL import Image
    size = 432
    fg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg = Image.new("RGBA", (size, size), (28, 28, 38, 255))
    if icon_path and Path(icon_path).is_file():
        try:
            im = Image.open(icon_path).convert("RGBA")
            im.thumbnail((int(size * 0.66), int(size * 0.66)), Image.LANCZOS)
            fg.paste(im, ((size - im.width) // 2, (size - im.height) // 2), im)
            # fond : couleur moyenne de l'icône, assombrie
            small = im.resize((1, 1), Image.BOX).getpixel((0, 0))
            bg = Image.new("RGBA", (size, size), (int(small[0] * 0.35), int(small[1] * 0.35), int(small[2] * 0.35), 255))
            log(T("android.log.icon", path=icon_path))
        except Exception as exc:
            log(T("android.log.icon_failed", err=exc))
    fg.save(build_dir / "android-icon_foreground.png")
    bg.save(build_dir / "android-icon_background.png")
    flat = bg.copy()
    flat.alpha_composite(fg)
    flat.convert("RGB").save(build_dir / "android-icon.png")


@dataclass
class StageResult:
    build_dir: Path
    files: int = 0
    bytes: int = 0
    images_bytes: int = 0
    images_skipped: list[str] = field(default_factory=list)
    videos_skipped: int = 0
    rpy_skipped: int = 0
    excluded: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    pack_dir: Path | None = None          # mode « données séparées » : dossier du pack (contient game\)
    pack_files: int = 0
    pack_bytes: int = 0
    pack_linked: bool = False             # pack fait de liens physiques (même volume NTFS) plutôt que de copies
    probes: list[str] = field(default_factory=list)
    image_mode: str = "original"
    improved: int = 0                     # images améliorées (réduites depuis hd2x) présentes dans le pack
    improved_skipped: int = 0             # déjà présentes (reprise)
    improved_failed: int = 0
    hd2x_files: int = 0


_HD_ALT_EXTS = (".webp", ".png", ".jpg", ".jpeg")


def hd2x_counterpart(hd_dir: Path | None, rel: str) -> Path | None:
    """Sortie DLSS correspondant à l'image `rel` (chemin relatif à game/), avec les mêmes règles que le hook zz_dlss_hd.rpy :
    même chemin sous hd2x/, extension éventuellement différente (.webp/.png/.jpg), et aussi sous images/ pour un chemin nu."""
    if hd_dir is None:
        return None
    name = rel.replace("\\", "/")
    bases = [name] if name.lower().startswith("images/") else [name, "images/" + name]
    for base in bases:
        cand = hd_dir / base
        if cand.is_file():
            return cand
        stem, dot, ext = base.rpartition(".")
        if dot:
            for alt in _HD_ALT_EXTS:
                if alt != "." + ext.lower():
                    c2 = hd_dir / (stem + alt)
                    if c2.is_file():
                        return c2
    return None


def _improve_one(hd: Path, dst: Path, orig: Path) -> tuple[str, int]:
    """Réduit la sortie DLSS `hd` à la taille en pixels de `orig`, dans le format de `orig` (qualité 92 ; PNG garde l'alpha).
    Renvoie ("done" | "skipped" | "failed", octets écrits). Ne réécrit jamais un fichier lié à l'original (st_nlink > 1)."""
    from PIL import Image
    try:
        if dst.is_file():
            st = dst.stat()
            if st.st_nlink == 1 and st.st_size > 0 and st.st_mtime >= hd.stat().st_mtime:
                return "skipped", st.st_size
        with Image.open(orig) as o:
            size = o.size
            fmt = (o.format or "").upper()
        with Image.open(hd) as h:
            h.load()
            im = h if h.size == size else h.resize(size, Image.LANCZOS)
            ext = orig.suffix.lower()
            if ext in (".jpg", ".jpeg") or fmt == "JPEG":
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                params = {"quality": IMPROVED_QUALITY, "subsampling": 0, "optimize": True}
                fmt_out = "JPEG"
            elif ext == ".webp" or fmt == "WEBP":
                params = {"quality": IMPROVED_QUALITY, "method": 4}
                fmt_out = "WEBP"
            else:
                if im.mode not in ("RGBA", "RGB", "L", "LA", "P"):
                    im = im.convert("RGBA")
                params = {"optimize": False, "compress_level": 6}
                fmt_out = "PNG"
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst.unlink()                      # jamais d'écriture dans un lien physique vers l'original
            tmp = dst.with_name(dst.name + ".part")
            im.save(tmp, fmt_out, **params)
            tmp.replace(dst)
        return "done", dst.stat().st_size
    except Exception:
        try:
            if dst.exists() and dst.stat().st_nlink == 1:
                dst.unlink()
        except OSError:
            pass
        return "failed", 0


def improve_images(jobs: list[tuple[Path, Path, Path]], log: Callable[[str], None], on_progress: Callable[[Progress], None],
                   cancel: threading.Event, t0: float) -> tuple[int, int, int, int]:
    """Pool de threads (PIL libère le GIL au décodage / redimensionnement / encodage). Renvoie (faites, sautées, échecs, octets)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    workers = max(2, min(16, (os.cpu_count() or 4)))
    p = Progress(phase=T("android.phase.improve"))
    done = skipped = failed = total_bytes = 0
    last = 0.0
    log(T("android.log.improve_start", n=len(jobs), workers=workers))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_improve_one, hd, dst, orig): dst for hd, dst, orig in jobs}
        for i, fut in enumerate(as_completed(futures), 1):
            if cancel.is_set():
                for f in futures:
                    f.cancel()
                raise Cancelled()
            status, nbytes = fut.result()
            total_bytes += nbytes
            if status == "done":
                done += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1
                log(T("android.log.improve_failed", file=futures[fut].name))
            if time.time() - last > 0.5 or i == len(jobs):
                last = time.time()
                p.fraction, p.detail, p.elapsed = i / len(jobs), f"{i} / {len(jobs)}", time.time() - t0
                on_progress(p)
            if i % 1000 == 0:
                log(T("android.log.improve_progress", i=i, n=len(jobs), elapsed=core.format_eta(time.time() - t0)))
    log(T("android.log.improve_done", done=done, skipped=skipped, failed=failed, size=core.human_size(total_bytes)))
    return done, skipped, failed, total_bytes


def _same_volume(a: Path, b: Path) -> bool:
    try:
        return os.path.splitdrive(str(a.resolve()))[0].lower() == os.path.splitdrive(str(b.resolve()))[0].lower()
    except Exception:
        return False


def _writable(path: Path) -> None:
    """Lève l'attribut « lecture seule » (certains jeux sont distribués avec des fichiers en lecture seule : Windows
    refuse ensuite de les écraser ou de les supprimer dans la copie de construction → WinError 5)."""
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass


def _rmtree_force(path: Path) -> None:
    """shutil.rmtree qui lève d'abord la lecture seule sur les fichiers récalcitrants."""
    def _onerror(func, p, exc_info):
        _writable(Path(p))
        try:
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_onerror)


def _place_file(src: Path, dst: Path, link: bool) -> bool:
    """Copie `src` vers `dst` (lien physique si `link`, sinon copie). Renvoie True si un lien a été créé.
    Un fichier source en lecture seule est toujours copié (un lien physique partagerait l'attribut)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if link and os.access(src, os.W_OK):
        try:
            if dst.exists():
                _writable(dst)
                dst.unlink()
            os.link(src, dst)
            return True
        except OSError:
            pass
    if dst.exists():
        _writable(dst)
    shutil.copy2(src, dst)
    _writable(dst)
    return False


def stage_build(a: AndroidAnalysis, cfg: BuildConfig, sdk: SdkInfo, log: Callable[[str], None], on_progress: Callable[[Progress], None],
                cancel: threading.Event) -> StageResult:
    r"""Copie de construction sous android\build\<jeu>\ : game/ filtré (jamais hd2x*, _dlss_backup, hook, saves, cache),
    vidéos et budget d'images selon la configuration, tl/ conservé, .android.json, icônes, clés (mode projet)."""
    t0 = time.time()
    build_dir = BUILD_DIR / build_name_for(cfg)
    res = StageResult(build_dir)
    skip_rpa = set(a.rpa_extracted) if cfg.skip_extracted_rpa else set()
    if build_dir.exists():
        log(T("android.log.stage_clean", dir=build_dir))
        _rmtree_force(build_dir)
    (build_dir / "game").mkdir(parents=True)
    external = cfg.data_mode == "external"
    pack_dir = pack_dir_for(cfg) if external else None
    link = False
    image_mode = cfg.image_mode if (external and a.hd2x_dir is not None and cfg.image_mode in IMAGE_MODES) else "original"
    res.image_mode = image_mode
    improve_jobs: list[tuple[Path, Path, Path]] = []      # (hd2x source, destination du pack, original)
    if external and pack_dir is not None:
        res.pack_dir = pack_dir
        if pack_dir.exists():
            if image_mode == "improved":
                log(T("android.log.pack_reuse", dir=pack_dir))       # reprise : les images déjà réduites sont gardées
                if (pack_dir / "game" / "hd2x").is_dir():
                    _rmtree_force(pack_dir / "game" / "hd2x")
            else:
                log(T("android.log.pack_clean", dir=pack_dir))
                _rmtree_force(pack_dir)
        (pack_dir / "game").mkdir(parents=True, exist_ok=True)
        link = bool(cfg.link_pack) and _same_volume(a.game, pack_dir)
        log(T("android.log.pack_mode", dir=pack_dir, how=T("android.log.pack_linked") if link else T("android.log.pack_copied")))
    if external:
        budget = 0                          # tout va dans le pack : pas de limite d'images
    else:
        budget = int(cfg.image_budget_mb) * 1024 * 1024 if cfg.image_budget_mb else 0
    total_expected = a.included_bytes if (cfg.include_videos or external) else a.included_bytes - a.videos_bytes
    p = Progress(phase=T("android.phase.stage"))
    copied = 0
    last = 0.0

    def to_pack(rel: str, ext: str) -> bool:
        if not external:
            return False
        low = rel.lower()
        if ext in core.IMAGE_EXTS:
            return not low.startswith("gui/")
        if ext in core.VIDEO_EXTS or ext == ".rpa":
            return True
        return bool(cfg.ext_audio) and ext in AUDIO_EXTS

    def place(sp: Path, rel: str, size: int) -> None:
        """Copie un fichier de game/ soit dans la copie de construction, soit dans le pack de données."""
        ext = sp.suffix.lower()
        if to_pack(rel, ext):
            assert pack_dir is not None
            dst = pack_dir / "game" / rel
            hd = hd2x_counterpart(a.hd2x_dir, rel) if (image_mode == "improved" and ext in core.IMAGE_EXTS) else None
            if hd is not None:
                improve_jobs.append((hd, dst, sp))          # généré après la copie (pool de threads)
            elif _place_file(sp, dst, link):
                res.pack_linked = True
            res.pack_files += 1
            res.pack_bytes += size
            if ext in core.IMAGE_EXTS and len(res.probes) < 3:
                res.probes.append(rel)
        else:
            dst = build_dir / "game" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sp, dst)
            res.files += 1
            res.bytes += size
            if ext in core.IMAGE_EXTS:
                res.images_bytes += size

    def tick(size: int, detail: str) -> None:
        nonlocal copied, last
        copied += size
        if time.time() - last > 0.3:
            last = time.time()
            p.fraction = min(1.0, copied / total_expected) if total_expected else 1.0
            p.detail, p.bytes_done, p.bytes_total, p.elapsed = detail, copied, total_expected, time.time() - t0
            on_progress(p)

    # images : dossiers de premier niveau dans l'ordre alphabétique jusqu'au budget
    images_used = 0

    def copy_tree(src: Path, dst: Path, rel_root: Path) -> None:
        nonlocal images_used
        for dirpath, dirnames, filenames in os.walk(src):
            if cancel.is_set():
                raise Cancelled()
            dirnames[:] = sorted(d for d in dirnames if not _is_excluded_dir(d))
            rel = Path(dirpath).relative_to(src)
            (dst / rel).mkdir(parents=True, exist_ok=True)
            for fn in sorted(filenames):
                if _is_excluded_file(fn) or fn.lower() == ".android.json":
                    continue
                sp = Path(dirpath) / fn
                ext = sp.suffix.lower()
                if ext in core.VIDEO_EXTS and not cfg.include_videos and not external:
                    res.videos_skipped += 1
                    continue
                if ext == ".rpy" and cfg.prefer_rpyc and sp.with_suffix(".rpyc").is_file():
                    res.rpy_skipped += 1
                    continue
                try:
                    size = sp.stat().st_size
                except OSError:
                    continue
                place(sp, (rel_root / rel / fn).relative_to("game").as_posix(), size)
                tick(size, (rel_root / rel / fn).as_posix())

    game_src, game_dst = a.game, build_dir / "game"
    for item in sorted(game_src.iterdir(), key=lambda x: x.name.lower()):
        if item.is_dir():
            if _is_excluded_dir(item.name):
                res.excluded.append(item.name + "/")
                continue
            if item.name.lower() == "images" and budget:
                (game_dst / item.name).mkdir(exist_ok=True)
                for sub in sorted(item.iterdir(), key=lambda x: x.name.lower()):
                    if sub.is_dir():
                        size = sum(f.stat().st_size for f in sub.rglob("*") if f.is_file() and f.suffix.lower() in core.IMAGE_EXTS)
                    else:
                        size = sub.stat().st_size
                    if images_used + size > budget:
                        res.images_skipped.append(sub.name)
                        continue
                    images_used += size
                    if sub.is_dir():
                        copy_tree(sub, game_dst / item.name / sub.name, Path("game") / item.name / sub.name)
                    else:
                        place(sub, f"{item.name}/{sub.name}", size)
                        tick(size, sub.name)
                continue
            copy_tree(item, game_dst / item.name, Path("game") / item.name)
        else:
            if _is_excluded_file(item.name) or item.name in skip_rpa:
                res.excluded.append(item.name)
                continue
            if item.suffix.lower() in core.VIDEO_EXTS and not cfg.include_videos and not external:
                res.videos_skipped += 1
                continue
            if item.suffix.lower() == ".rpy" and cfg.prefer_rpyc and item.with_suffix(".rpyc").is_file():
                res.rpy_skipped += 1
                continue
            size = item.stat().st_size
            place(item, item.name, size)
            tick(size, item.name)
    if external and pack_dir is not None and image_mode == "improved" and improve_jobs:
        # images améliorées à la taille d'origine : réduction Lanczos des sorties DLSS (pool de threads, reprise = fichiers existants gardés)
        done, skipped, failed, gen_bytes = improve_images(improve_jobs, log, on_progress, cancel, t0)
        res.improved, res.improved_skipped, res.improved_failed = done + skipped, skipped, failed
        res.pack_bytes += gen_bytes - sum(j[2].stat().st_size for j in improve_jobs if j[2].is_file())
    if external and pack_dir is not None and image_mode == "hd2x" and a.hd2x_dir is not None:
        # dossier hd2x complet dans le pack (liens) + hook zz_dlss_hd.rpy dans l'APK avec un cache d'images adapté au téléphone
        hd_dst = pack_dir / "game" / "hd2x"
        n = 0
        for f in a.hd2x_dir.rglob("*"):
            if f.is_file():
                if cancel.is_set():
                    raise Cancelled()
                rel_hd = f.relative_to(a.hd2x_dir)
                if _place_file(f, hd_dst / rel_hd, link):
                    res.pack_linked = True
                n += 1
                sz = f.stat().st_size
                res.pack_files += 1
                res.pack_bytes += sz
                tick(sz, ("hd2x" / rel_hd).as_posix())
        (build_dir / "game" / core.HOOK_NAME).write_text(core.render_hook("hd2x", int(cfg.hd2x_cache_mb or HD2X_ANDROID_CACHE_MB)), encoding="utf-8")
        res.hd2x_files = n
        log(T("android.log.hd2x_packed", n=n, size=core.human_size(a.hd2x_bytes), cache=int(cfg.hd2x_cache_mb or HD2X_ANDROID_CACHE_MB)))
    if external and pack_dir is not None:
        # hook + manifeste dans la copie de construction (donc dans l'APK), mode d'emploi dans le pack
        shutil.copy2(EXTDATA_HOOK_SRC, build_dir / "game" / EXTDATA_HOOK_NAME)
        manifest = {"package": cfg.package.strip().lower(), "name": cfg.name.strip(), "version": cfg.version.strip(),
                    "files": res.pack_files, "bytes": res.pack_bytes, "probe": res.probes, "generator": "RenPyHD"}
        (build_dir / "game" / EXTDATA_MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (pack_dir / "LISEZMOI-README.txt").write_text(pack_readme(cfg, res), encoding="utf-8")
        log(T("android.log.pack_done", files=res.pack_files, size=core.human_size(res.pack_bytes), dir=pack_dir))
    # présplash et fichiers android-* du jeu d'origine (s'ils existent)
    for extra in a.root.glob("android-*.*"):
        if extra.is_file():
            shutil.copy2(extra, build_dir / extra.name)
    (build_dir / ".android.json").write_text(json.dumps(android_json(cfg, sdk), indent=2), encoding="utf-8")
    make_icons(build_dir, cfg.icon_path, log)
    if sdk.keys_mode == "project":
        for k in ("android.keystore", "bundle.keystore"):
            src = KEYS_DIR / k
            if src.is_file():
                shutil.copy2(src, build_dir / k)
    res.elapsed = time.time() - t0
    log(T("android.log.staged", files=res.files, size=core.human_size(res.bytes), dir=build_dir, elapsed=core.format_eta(res.elapsed)))
    if res.excluded:
        log(T("android.log.stage_excluded", names=", ".join(res.excluded)))
    if res.images_skipped:
        log(T("android.log.stage_images_skipped", n=len(res.images_skipped), budget=cfg.image_budget_mb))
    if res.videos_skipped:
        log(T("android.log.stage_videos_skipped", n=res.videos_skipped))
    if res.rpy_skipped:
        log(T("android.log.stage_rpy_skipped", n=res.rpy_skipped))
    # Lecture seule héritée du jeu (copies tl/, extras…) : on la lève sur toute la copie de construction.
    for p in build_dir.rglob('*'):
        if p.is_file() and not os.access(p, os.W_OK):
            _writable(p)

    return res


def pack_readme(cfg: BuildConfig, st: StageResult) -> str:
    pkg = cfg.package.strip().lower()
    return (
        f"RenPyHD - pack de donnees / data pack : {cfg.name.strip()} ({pkg}) - {st.pack_files} fichiers / files, {core.human_size(st.pack_bytes)}\n\n"
        "FR - Ce dossier contient les images, videos (et parfois l'audio) du jeu, livres a cote de l'APK.\n"
        "     Copiez le dossier  game  (tout entier) dans ce dossier du telephone, puis lancez le jeu :\n"
        f"       Android/data/{pkg}/files/game/\n"
        "     Le plus simple : RenPyHD > Android (APK) > Copier les donnees sur le telephone (adb), ou en ligne de commande :\n"
        f"       adb install -r <fichier.apk>\n       adb shell mkdir -p /sdcard/Android/data/{pkg}/files\n       adb push game /sdcard/Android/data/{pkg}/files/\n"
        "     Copie manuelle (cable USB / gestionnaire de fichiers) : Android 11 et plus n'autorise pas toujours l'ecriture dans\n"
        "     Android/data depuis un gestionnaire de fichiers ; par USB depuis Windows (MTP) cela fonctionne en general, sinon adb.\n"
        f"     Repli accepte par le jeu : Android/obb/{pkg}/game/\n\n"
        "EN - This folder holds the game's images, videos (sometimes audio), shipped next to the APK.\n"
        "     Copy the whole  game  folder into this folder on the phone, then start the game:\n"
        f"       Android/data/{pkg}/files/game/\n"
        "     Easiest: RenPyHD > Android (APK) > Copy the data to the phone (adb), or from a command line:\n"
        f"       adb install -r <file.apk>\n       adb shell mkdir -p /sdcard/Android/data/{pkg}/files\n       adb push game /sdcard/Android/data/{pkg}/files/\n"
        "     Manual copy (USB cable / file manager): on Android 11+ a file manager may not be allowed to write into Android/data;\n"
        "     USB from Windows (MTP) usually works, otherwise use adb.\n"
        f"     Fallback accepted by the game: Android/obb/{pkg}/game/\n"
    )


def decompile_missing(sdk: SdkInfo, build_dir: Path, missing: list[str], log: Callable[[str], None], cancel: threading.Event) -> tuple[int, list[str]]:
    """Décompile (unrpyc, interpréteur Python du SDK) les .rpyc sans .rpy — uniquement dans la copie de construction."""
    script = unrpyc_for(sdk)
    if script is None or sdk.python is None:
        return 0, [T("android.err.no_unrpyc")]
    files = [build_dir / "game" / m for m in missing if (build_dir / "game" / m).is_file()]
    if not files:
        return 0, []
    errors: list[str] = []
    ok = 0
    env = launcher_env(None)
    for i in range(0, len(files), 40):
        if cancel.is_set():
            raise Cancelled()
        batch = files[i:i + 40]
        cmd = [str(sdk.python), "-EO", str(script), "--clobber", *[str(f) for f in batch]]
        log("$ " + " ".join(cmd[:3]) + f" … ({len(batch)} .rpyc)")
        try:
            proc = subprocess.run(cmd, cwd=str(script.parent), capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  creationflags=NO_WINDOW, env=env, timeout=1800)
        except Exception as exc:
            errors.append(str(exc))
            continue
        for f in batch:
            if f.with_suffix(".rpy").is_file():
                ok += 1
            else:
                errors.append(f.name)
        tail = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()][-3:]
        for ln in tail:
            log("  " + ln)
    log(T("android.log.decompiled", ok=ok, errors=len(errors)))
    return ok, errors


def decompile_all(sdk: SdkInfo, build_dir: Path, log: Callable[[str], None], cancel: threading.Event) -> tuple[int, list[str], int]:
    """Route arm64 des jeux 7.0–7.3 : décompile tous les .rpyc sans .rpy de la copie (unrpyc 1.x, Python 2 du SDK), puis supprime
    tous les .rpyc pour que le SDK recompile les sources. Renvoie (décompilés, échecs, .rpyc supprimés)."""
    game = build_dir / "game"
    missing = []
    for f in game.rglob("*.rpyc"):
        rel = f.relative_to(game).as_posix()
        if not f.with_suffix(".rpy").is_file() and not rel.startswith("tl/"):
            missing.append(rel)
    ok, errors = decompile_missing(sdk, build_dir, sorted(missing), log, cancel) if missing else (0, [])
    removed = 0
    for f in list(game.rglob("*.rpyc")):
        if f.with_suffix(".rpy").is_file():
            f.unlink()
            removed += 1
    log(T("android.log.rpyc_removed", n=removed))
    return ok, errors, removed


_COMPILE_ERR_RE = re.compile(r'^File "([^"]+)", line (\d+): (.*)$')


def fix_script_line(lines: list[str], idx: int, message: str) -> str:
    """Corrige, quand c'est un motif connu, la ligne `idx` (0-based) d'un script décompilé refusé par un Ren'Py plus récent.
    Renvoie le nom du motif appliqué, ou "" si rien n'a été changé. Motifs :
      empty_block : « expected a non-empty block » — `with dissolve:` / `show x:` etc. sans corps : le deux-points final est retiré ;
      trailing_colon_stmt : instruction suivie d'un deux-points mais d'aucun bloc (même correction, autre message) ;
      bad_indent : « indentation mismatch » sur une ligne vide ou faite d'espaces : la ligne est vidée."""
    if idx < 0 or idx >= len(lines):
        return ""
    line = lines[idx]
    low = message.lower()
    if "expected a non-empty block" in low or "expected statement" in low and line.rstrip().endswith(":"):
        stripped = line.rstrip()
        if stripped.endswith(":") and not stripped.lstrip().startswith(("if ", "elif ", "else", "while ", "for ", "menu", "label ", "screen ", "init", "python", "translate ", "layeredimage", "style ", "transform ", "define ", "default ")):
            lines[idx] = stripped[:-1] + line[len(stripped):]
            return "empty_block"
    if "indentation mismatch" in low and not line.strip():
        lines[idx] = ""
        return "bad_indent"
    return ""


def compile_and_fix(sdk: SdkInfo, build_dir: Path, log: Callable[[str], None], cancel: threading.Event, rounds: int = 5) -> dict:
    """`renpy.py <copie> compile` avec le SDK cible ; à chaque erreur de syntaxe connue, corrige la ligne (fix_script_line) et
    recommence (au plus `rounds` fois). Renvoie {ok, rounds, fixes:[(fichier, ligne, motif)], errors:[messages restants]}."""
    res: dict = {"ok": False, "rounds": 0, "fixes": [], "errors": []}
    if sdk.python is None:
        res["errors"].append(T("android.err.no_sdk_python", root=sdk.root))
        return res
    env = launcher_env(None)
    for r in range(rounds):
        if cancel.is_set():
            raise Cancelled()
        res["rounds"] = r + 1
        err_file = build_dir / "errors.txt"
        if err_file.is_file():
            err_file.unlink()
        cmd = [str(sdk.python), "-EO", "renpy.py", str(build_dir), "compile"]
        log("$ " + " ".join(cmd))
        proc = subprocess.run(cmd, cwd=str(sdk.root), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              creationflags=NO_WINDOW, env=env, timeout=1800)
        text = (proc.stdout or "") + (proc.stderr or "")
        if err_file.is_file():
            text += "\n" + err_file.read_text(encoding="utf-8", errors="replace")
        errors = []
        for ln in text.splitlines():
            m = _COMPILE_ERR_RE.match(ln.strip())
            if m:
                errors.append((m.group(1), int(m.group(2)), m.group(3).strip()))
        if proc.returncode == 0 and not errors:
            res["ok"] = True
            log(T("android.log.compile_ok", round=r + 1))
            return res
        fixed_any = False
        seen = set()
        for fn, lineno, msg in errors:
            key = (fn, lineno)
            if key in seen:
                continue
            seen.add(key)
            path = build_dir / fn if not Path(fn).is_absolute() else Path(fn)
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
            pattern = fix_script_line(lines, lineno - 1, msg)
            if pattern:
                path.write_text("".join(lines), encoding="utf-8")
                res["fixes"].append((fn, lineno, pattern))
                log(T("android.log.fix_applied", file=fn, line=lineno, pattern=pattern))
                fixed_any = True
        if not fixed_any:
            res["errors"] = [f"{fn}:{lineno}: {msg}" for fn, lineno, msg in errors] or [ln for ln in text.splitlines() if "Error" in ln or "error" in ln][-5:] or [f"rc={proc.returncode}"]
            log(T("android.log.compile_failed", n=len(res["errors"])))
            for e in res["errors"][:8]:
                log("  " + e)
            return res
    res["errors"] = [T("android.log.compile_rounds", n=rounds)]
    return res


# ----------------------------------------------------------------------------
# Étape 4 : construction
# ----------------------------------------------------------------------------
@dataclass
class BuildResult:
    ok: bool = False
    files: list[Path] = field(default_factory=list)
    elapsed: float = 0.0
    error: str = ""
    cancelled: bool = False
    log_file: Path | None = None
    out_dir: Path | None = None
    command: str = ""


_PHASES = [
    (re.compile(r"Scanning project files|Building distribution|Dumping|Compiling", re.I), 0.05, "android.phase.dist_scan"),
    (re.compile(r"Writing the .* package|Copying files", re.I), 0.15, "android.phase.dist_write"),
    (re.compile(r"Updating project", re.I), 0.30, "android.phase.rapt_project"),
    (re.compile(r"Creating assets directory", re.I), 0.34, "android.phase.rapt_assets"),
    (re.compile(r"Packaging internal data", re.I), 0.42, "android.phase.rapt_private"),
    (re.compile(r"using Gradle to build", re.I), 0.50, "android.phase.gradle"),
    (re.compile(r"> Task :app:package|> Task :app:assemble|> Task :app:bundle", re.I), 0.90, "android.phase.package"),
    (re.compile(r"BUILD SUCCESSFUL|The build seems to have succeeded|Copying Android files", re.I), 0.97, "android.phase.copy_out"),
]
_GRADLE_TASK = re.compile(r"^> Task :")
_WRITE_RE = re.compile(r"package\. - (\d+) of (\d+)")


def build_apk(sdk: SdkInfo, jdk: Path, build_dir: Path, cfg: BuildConfig, log: Callable[[str], None], on_progress: Callable[[Progress], None],
              cancel: threading.Event) -> BuildResult:
    t0 = time.time()
    res = BuildResult()
    out_dir = OUT_DIR / build_dir.name
    res.out_dir = out_dir
    if out_dir.exists():
        for f in out_dir.iterdir():
            if f.suffix.lower() in (".apk", ".aab"):
                f.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    res.log_file = LOG_DIR / f"build_{build_dir.name}_{sdk.version}.log"
    if res.log_file.is_file():
        res.log_file.unlink()
    patch_legacy_rapt(sdk, log)
    if sdk.legacy_build:
        args = ["android_build", str(build_dir), "assembleRelease", "--destination", str(out_dir)]
    else:
        args = ["android_build", str(build_dir)] + (["--bundle"] if cfg.bundle and sdk.supports_bundle else []) + ["--destination", str(out_dir)]
    res.command = " ".join(launcher_command(sdk, args))
    p = Progress(phase=T("android.phase.dist_scan"), fraction=0.02)
    gradle_tasks = 0
    state = {"fraction": 0.02}

    def on_line(line: str) -> None:
        nonlocal gradle_tasks
        for rx, frac, key in _PHASES:
            if rx.search(line):
                if frac > state["fraction"]:
                    state["fraction"] = frac
                    p.phase = T(key)
                break
        m = _WRITE_RE.search(line)
        if m and state["fraction"] < 0.30:
            done, total = int(m.group(1)), int(m.group(2))
            state["fraction"] = 0.15 + 0.14 * done / max(1, total)
            p.detail = f"{done} / {total}"
        elif _GRADLE_TASK.match(line):
            gradle_tasks += 1
            state["fraction"] = max(state["fraction"], min(0.88, 0.50 + 0.38 * gradle_tasks / 80))
            p.detail = line[7:90]
        elif line.strip():
            p.detail = line.strip()[:110]
        p.fraction, p.elapsed = state["fraction"], time.time() - t0
        on_progress(p)

    try:
        rc, lines = run_launcher(sdk, jdk, args, log, cancel, on_line=on_line, log_file=res.log_file)
        files = sorted([f for f in out_dir.iterdir() if f.suffix.lower() in (".apk", ".aab")], key=lambda f: f.stat().st_size, reverse=True)
        if not files:
            # repli : sortie de RAPT (rapt\bin)
            bin_dir = sdk.rapt / "bin"
            if bin_dir.is_dir():
                for f in bin_dir.iterdir():
                    if f.suffix.lower() in (".apk", ".aab") and f.stat().st_mtime >= t0 - 5:
                        shutil.copy2(f, out_dir / f.name)
                files = sorted([f for f in out_dir.iterdir() if f.suffix.lower() in (".apk", ".aab")], key=lambda f: f.stat().st_size, reverse=True)
        res.files = files
        res.ok = rc == 0 and bool(files)
        if not res.ok:
            errs = [ln for ln in lines if re.search(r"FAILED|error|Error|Exception|RENPYHD_FAIL|failed", ln)]
            res.error = "\n".join(errs[-6:]) if errs else (lines[-1] if lines else f"rc={rc}")
    except Cancelled:
        res.cancelled = True
    except Exception as exc:
        res.error = str(exc)
        log(T("android.log.error", err=exc))
    res.elapsed = time.time() - t0
    if res.ok:
        cleanup_after_build(sdk, build_dir)
        p.phase, p.fraction, p.elapsed = T("android.phase.done"), 1.0, res.elapsed
        on_progress(p)
    return res


def cleanup_after_build(sdk: SdkInfo, build_dir: Path) -> None:
    """Libère les copies intermédiaires (distribution du lanceur, assets du projet Gradle, APK dans rapt\\bin) : plusieurs Go par jeu."""
    for d in (sdk.root / "tmp" / build_dir.name, sdk.rapt / "project" / "app" / "src" / "main" / "assets",
              sdk.rapt / "project" / "app" / "build" / "intermediates", sdk.rapt / "project" / "app" / "build" / "outputs"):
        try:
            if d.is_dir():
                _rmtree_force(d)
        except Exception:
            pass
    bin_dir = sdk.rapt / "bin"
    if bin_dir.is_dir():
        for f in bin_dir.iterdir():
            if f.suffix.lower() in (".apk", ".aab"):
                try:
                    f.unlink()
                except OSError:
                    pass


def pick_main_apk(files: list[Path]) -> Path | None:
    for f in files:
        if "universal" in f.name.lower():
            return f
    apks = [f for f in files if f.suffix.lower() == ".apk"]
    return apks[0] if apks else (files[0] if files else None)


ABI_ORDER = ("arm64-v8a", "armeabi-v7a", "x86_64", "x86")
_ABI_NAME_RE = re.compile(r"-(universal|arm64-v8a|armeabi-v7a|x86_64|x86)-release\.apk$", re.I)


def apk_abi(apk: Path) -> str:
    """ABI d'un APK : d'après son nom RAPT (…-<abi>-release.apk), sinon d'après lib/<abi>/ dans le ZIP ; « universal » si plusieurs."""
    m = _ABI_NAME_RE.search(apk.name)
    if m:
        return m.group(1).lower()
    try:
        with zipfile.ZipFile(apk) as z:
            libs = sorted({n.split("/")[1] for n in z.namelist() if n.startswith("lib/") and n.count("/") >= 2})
    except Exception:
        return "?"
    if len(libs) > 1:
        return "universal"
    return libs[0] if libs else "?"


def pick_apk_for_device(files: list[Path], abis: list[str]) -> tuple[Path | None, str]:
    """APK installable sur un appareil acceptant `abis` (ro.product.cpu.abilist) : universel d'abord, sinon l'APK dont l'ABI est
    acceptée dans l'ordre arm64-v8a > armeabi-v7a > x86_64 > x86. (None, liste des ABI de la construction) si rien ne convient."""
    apks = [f for f in files if f.suffix.lower() == ".apk" and f.is_file()]
    by_abi = {apk_abi(f): f for f in apks}
    if "universal" in by_abi:
        return by_abi["universal"], "universal"
    wanted = [a.strip().lower() for a in abis if a.strip()]
    for abi in ABI_ORDER:
        if abi in wanted and abi in by_abi:
            return by_abi[abi], abi
    return None, ", ".join(sorted(by_abi)) or "—"


def adb_device_info(sdk: SdkInfo) -> dict | None:
    """Premier appareil branché : {serial, model, android, sdk_int, abis} ; None sans appareil (débranché à tout moment : tolérant)."""
    devs = adb_devices(sdk)
    if not devs:
        return None
    info: dict = {"serial": devs[0], "model": "?", "android": "?", "sdk_int": "?", "abis": []}
    props = {"model": "ro.product.model", "android": "ro.build.version.release", "sdk_int": "ro.build.version.sdk", "abis": "ro.product.cpu.abilist"}
    for key, prop in props.items():
        try:
            proc = subprocess.run([str(sdk.adb), "-s", devs[0], "shell", "getprop", prop], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", creationflags=NO_WINDOW, timeout=30)
            val = proc.stdout.strip()
        except Exception:
            val = ""
        if key == "abis":
            info["abis"] = [a for a in val.split(",") if a]
        elif val:
            info[key] = val
    return info


def adb_launch(sdk: SdkInfo, package: str, log: Callable[[str], None]) -> tuple[int, str]:
    """Lance l'application installée (monkey : activité LAUNCHER du paquet)."""
    cmd = [str(sdk.adb), "shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"]
    log("$ " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=120)
    except Exception as exc:
        return 1, str(exc)
    out = (proc.stdout + proc.stderr).strip()
    ok = proc.returncode == 0 and "Events injected: 1" in out
    return (0 if ok else (proc.returncode or 1)), out


# ----------------------------------------------------------------------------
# Vérification, adb
# ----------------------------------------------------------------------------
def verify_apk(sdk: SdkInfo, jdk: Path | None, apk: Path) -> dict:
    r: dict = {"file": str(apk), "size": apk.stat().st_size, "entries": 0, "manifest": False, "assets": 0, "game_files": 0, "libs": [],
               "signed": None, "apksigner": ""}
    try:
        with zipfile.ZipFile(apk) as z:
            names = z.namelist()
            r["entries"] = len(names)
            r["manifest"] = "AndroidManifest.xml" in names
            r["assets"] = sum(1 for n in names if n.startswith("assets/"))
            r["game_files"] = sum(1 for n in names if n.startswith("assets/x-game/") or n.startswith("assets/game/") or "/x-game/" in n)
            r["private"] = any(n.endswith("private.mp3") for n in names)
            r["libs"] = sorted({n.split("/")[1] for n in names if n.startswith("lib/") and n.count("/") >= 2})
    except Exception as exc:
        r["error"] = str(exc)
    if sdk.apksigner is None:            # build-tools installés par Gradle pendant la première construction
        sdk = inspect_sdk(sdk.root, sdk.version) or sdk
    if apk.suffix.lower() == ".apk" and sdk.apksigner and jdk is not None:
        try:
            env = launcher_env(jdk)
            proc = subprocess.run(["cmd", "/c", str(sdk.apksigner), "verify", "--print-certs", str(apk)], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", creationflags=NO_WINDOW, env=env, timeout=300)
            r["signed"] = proc.returncode == 0
            r["apksigner"] = (proc.stdout + proc.stderr).strip()[:1200]
        except Exception as exc:
            r["apksigner"] = str(exc)
    return r


def adb_devices(sdk: SdkInfo) -> list[str]:
    if not sdk.adb.is_file():
        return []
    try:
        proc = subprocess.run([str(sdk.adb), "devices"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              creationflags=NO_WINDOW, timeout=60)
    except Exception:
        return []
    out = []
    for ln in proc.stdout.splitlines()[1:]:
        parts = ln.split()
        if len(parts) >= 2 and parts[1] == "device":
            out.append(parts[0])
    return out


def adb_install(sdk: SdkInfo, apk: Path, log: Callable[[str], None], cancel: threading.Event | None = None) -> tuple[int, str]:
    cmd = [str(sdk.adb), "install", "-r", str(apk)]
    log("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            creationflags=NO_WINDOW)
    out: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        out.append(line.rstrip())
        log(line.rstrip())
        if cancel is not None and cancel.is_set():
            proc.kill()
            break
    proc.wait()
    return proc.returncode or 0, "\n".join(out)


def open_folder(path: Path) -> None:
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception:
        pass


def adb_push_data(sdk: SdkInfo, pack_dir: Path, package: str, log: Callable[[str], None], cancel: threading.Event | None = None) -> tuple[int, str]:
    r"""Copie le pack de données (pack_dir\game) dans /sdcard/Android/data/<paquet>/files/game via adb (dossier privé de l'app :
    accessible à adb et à l'application, sans permission, y compris sous Android 11+ à stockage cloisonné)."""
    files_dir = phone_data_path(package).rsplit("/", 1)[0]
    src = pack_dir / "game"
    if not src.is_dir():
        return 1, T("android.err.no_pack", dir=pack_dir)
    out: list[str] = []
    for cmd in ([str(sdk.adb), "shell", "mkdir", "-p", files_dir], [str(sdk.adb), "push", str(src), files_dir + "/"]):
        log("$ " + " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                creationflags=NO_WINDOW)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                out.append(line)
                log(line)
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.wait()
                return 130, "\n".join(out)
        proc.wait()
        if proc.returncode:
            return proc.returncode or 1, "\n".join(out)
    # Android 11+ (vérifié Galaxy Z Fold 6 / Android 16) : les dossiers créés par « adb push » appartiennent à l'utilisateur shell
    # (drwxrws--- shell ext_data_rw) et l'application obtient « Permission denied » en les listant ; a+rX règle le problème.
    try:
        subprocess.run([str(sdk.adb), "shell", "chmod", "-R", "a+rX", phone_data_path(package)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=1800)
        log("$ adb shell chmod -R a+rX " + phone_data_path(package))
    except Exception:
        pass
    # contrôle : nombre de fichiers présents sur le téléphone
    try:
        proc = subprocess.run([str(sdk.adb), "shell", "find", phone_data_path(package), "-type", "f", "|", "wc", "-l"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=600)
        out.append(T("android.log.push_count", n=proc.stdout.strip() or "?"))
        log(out[-1])
    except Exception:
        pass
    return 0, "\n".join(out)


def adb_uninstall(sdk: SdkInfo, package: str, log: Callable[[str], None]) -> tuple[int, str]:
    cmd = [str(sdk.adb), "uninstall", package]
    log("$ " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=600)
    except Exception as exc:
        return 1, str(exc)
    out = (proc.stdout + proc.stderr).strip()
    log(out)
    return proc.returncode or 0, out


def any_adb() -> Path | None:
    """adb.exe d'un SDK installé (le plus récent), pour le gestionnaire quand aucun environnement n'est chargé."""
    for v in reversed(installed_sdk_versions()):
        sdk = inspect_sdk(sdk_root_for(v), v)
        if sdk and sdk.adb.is_file():
            return sdk.adb
    return None


def sdk_with_adb() -> SdkInfo | None:
    for v in reversed(installed_sdk_versions()):
        sdk = inspect_sdk(sdk_root_for(v), v)
        if sdk and sdk.adb.is_file():
            return sdk
    return None


# ----------------------------------------------------------------------------
# Gestionnaire « Mes APK » : manifestes android\out\<jeu>\build.json
# ----------------------------------------------------------------------------
def dir_size(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def write_build_manifest(a: AndroidAnalysis, cfg: BuildConfig, sdk: SdkInfo, st: StageResult | None, r: BuildResult, verify: dict | None) -> Path | None:
    if not r.out_dir:
        return None
    main = pick_main_apk(r.files)
    d = {
        "generator": "RenPyHD", "name": cfg.name.strip(), "package": cfg.package.strip().lower(), "version": cfg.version.strip(),
        "numeric_version": int(cfg.numeric_version), "game_root": str(a.root), "renpy_version": a.version, "sdk_version": sdk.version,
        "sdk_family": sdk.family, "built": time.time(), "built_text": time.strftime("%Y-%m-%d %H:%M"),
        "data_mode": cfg.data_mode, "image_mode": st.image_mode if st else "original", "improved": st.improved if st else 0,
        "hd2x_files": st.hd2x_files if st else 0, "hd2x_cache_mb": int(cfg.hd2x_cache_mb) if cfg.image_mode == "hd2x" else 0,
        "apk": main.name if main else "", "apk_bytes": main.stat().st_size if main else 0,
        "files": [f.name for f in r.files], "bundle": bool(cfg.bundle), "elapsed": r.elapsed,
        "pack_dir": st.pack_dir.name if (st and st.pack_dir) else "", "pack_files": st.pack_files if st else 0, "pack_bytes": st.pack_bytes if st else 0,
        "pack_linked": bool(st.pack_linked) if st else False, "signed": (verify or {}).get("signed"), "phone_data_path": phone_data_path(cfg.package.strip().lower()),
    }
    path = r.out_dir / BUILD_MANIFEST
    path.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


_APK_NAME_RE = re.compile(r"^(?P<pkg>[a-z_][\w]*(?:\.[a-z_][\w]*)+?)(?:-(?P<ver>\d+(?:\.\d+)*))?(?:-(?P<num>\d+))?-(?:universal-)?release\.apk$", re.I)


def _backfill_manifest(out_dir: Path) -> dict:
    """Manifeste déduit pour un dossier construit avant l'existence de build.json (nom d'APK, android.json de la copie, journaux)."""
    apks = sorted([f for f in out_dir.iterdir() if f.suffix.lower() in (".apk", ".aab")], key=lambda f: f.stat().st_size, reverse=True)
    main = pick_main_apk(apks)
    d: dict = {"generator": "", "name": out_dir.name, "package": "", "version": "", "numeric_version": 0, "renpy_version": "", "sdk_version": "",
               "sdk_family": "", "built": main.stat().st_mtime if main else out_dir.stat().st_mtime, "data_mode": "apk", "apk": main.name if main else "",
               "apk_bytes": main.stat().st_size if main else 0, "files": [f.name for f in apks], "pack_dir": "", "pack_files": 0, "pack_bytes": 0,
               "signed": None, "backfilled": True}
    d["built_text"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["built"]))
    if main:
        m = _APK_NAME_RE.match(main.name)
        if m:
            d["package"] = m.group("pkg").lower()
            d["version"] = m.group("ver") or ""
            d["numeric_version"] = int(m.group("num") or 0)
    aj = BUILD_DIR / out_dir.name / "android.json"
    if not aj.is_file():
        aj = BUILD_DIR / out_dir.name / ".android.json"
    if aj.is_file():
        try:
            j = json.loads(aj.read_text(encoding="utf-8"))
            d["name"] = str(j.get("name") or d["name"])
            d["package"] = str(j.get("package") or d["package"])
            d["version"] = str(j.get("version") or d["version"])
            d["numeric_version"] = int(j.get("numeric_version") or d["numeric_version"] or 0)
        except Exception:
            pass
    if (BUILD_DIR / out_dir.name / "game" / EXTDATA_MANIFEST).is_file():
        d["data_mode"] = "external"
    for lg in sorted(LOG_DIR.glob(f"build_{out_dir.name}_*.log")) if LOG_DIR.is_dir() else []:
        m = re.search(r"_(\d+\.\d+\.\d+)\.log$", lg.name)
        if m:
            d["sdk_version"] = m.group(1)
            d["sdk_family"] = family_for(m.group(1))
    for sub in out_dir.iterdir():
        if sub.is_dir() and sub.name.endswith("-data") and (sub / "game").is_dir():
            d["data_mode"], d["pack_dir"] = "external", sub.name
            d["pack_bytes"] = dir_size(sub)
            d["pack_files"] = sum(1 for f in sub.rglob("*") if f.is_file())
            if not d["package"]:
                d["package"] = sub.name[:-5]
    if d["package"]:
        d["phone_data_path"] = phone_data_path(d["package"])
    return d


@dataclass
class BuildEntry:
    name: str                 # nom du dossier android\out\<name>
    out_dir: Path
    data: dict

    @property
    def apk(self) -> Path | None:
        f = self.out_dir / str(self.data.get("apk") or "")
        return f if self.data.get("apk") and f.is_file() else None

    @property
    def pack_dir(self) -> Path | None:
        p = self.out_dir / str(self.data.get("pack_dir") or "")
        return p if self.data.get("pack_dir") and p.is_dir() else None

    @property
    def total_bytes(self) -> int:
        return int(self.data.get("apk_bytes") or 0) + int(self.data.get("pack_bytes") or 0)


def list_builds(refresh_sizes: bool = False) -> list[BuildEntry]:
    """Toutes les constructions sous android\\out\\ (manifeste build.json, sinon déduit et écrit)."""
    out: list[BuildEntry] = []
    if not OUT_DIR.is_dir():
        return out
    for d in sorted(OUT_DIR.iterdir(), key=lambda x: x.name.lower()):
        if not d.is_dir():
            continue
        mf = d / BUILD_MANIFEST
        data: dict | None = None
        if mf.is_file():
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:
                data = None
        if data is None:
            data = _backfill_manifest(d)
            if data.get("apk") or data.get("pack_dir"):
                try:
                    mf.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
            else:
                continue
        if refresh_sizes:
            apk = d / str(data.get("apk") or "")
            data["apk_bytes"] = apk.stat().st_size if data.get("apk") and apk.is_file() else 0
            pk = d / str(data.get("pack_dir") or "")
            data["pack_bytes"] = dir_size(pk) if data.get("pack_dir") and pk.is_dir() else 0
        out.append(BuildEntry(d.name, d, data))
    out.sort(key=lambda e: float(e.data.get("built") or 0), reverse=True)
    return out


VERIFY_PROBE_SRC = APP_DIR / "renpyhd_verify_probe.rpy"
VERIFY_PROBE_NAME = "zz_renpyhd_verify.rpy"


def verify_build(entry: BuildEntry, log: Callable[[str], None], timeout: int = 300) -> dict:
    r"""« Vérifier » : lance la copie de construction sur PC avec le SDK Ren'Py de la construction (RENPYHD_EXTDATA vers le pack si
    données séparées) et une sonde qui, juste avant le menu principal, vérifie le label start, les images témoins et le rendu du
    menu, puis quitte. Résultat écrit dans build.json (clé verified)."""
    name = entry.name
    build_dir = BUILD_DIR / name
    res: dict = {"when": time.time(), "when_text": time.strftime("%Y-%m-%d %H:%M"), "ok": False, "detail": "", "report": None}

    def store() -> dict:
        entry.data["verified"] = res
        try:
            (entry.out_dir / BUILD_MANIFEST).write_text(json.dumps(entry.data, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return res

    if not (build_dir / "game").is_dir():
        res["detail"] = T("android.verify.no_copy", dir=build_dir)
        return store()
    version = str(entry.data.get("sdk_version") or "")
    sdk = inspect_sdk(sdk_root_for(version), version) if version else None
    if sdk is None or sdk.python is None:
        res["detail"] = T("android.verify.no_sdk", version=version or "?")
        return store()
    probe = build_dir / "game" / VERIFY_PROBE_NAME
    shutil.copy2(VERIFY_PROBE_SRC, probe)
    rep = LOG_DIR / f"verify_{name}.json"
    shot = entry.out_dir / "verify.png"
    for f in (rep, shot, build_dir / "traceback.txt"):
        if f.exists():
            f.unlink()
    env = launcher_env(None)
    env.pop("RENPYHD_EXTDATA", None)
    pack = entry.pack_dir
    if entry.data.get("data_mode") == "external" and pack is not None:
        env["RENPYHD_EXTDATA"] = str(pack / "game")
    env["RENPYHD_PROBE_OUT"] = str(rep)
    env["RENPYHD_PROBE_SHOT"] = str(shot)
    cmd = [str(sdk.python), "-EO", "renpy.py", str(build_dir)]
    log("$ " + " ".join(cmd) + (f"  (RENPYHD_EXTDATA={env['RENPYHD_EXTDATA']})" if "RENPYHD_EXTDATA" in env else ""))
    t0 = time.time()
    try:
        proc = subprocess.Popen(cmd, cwd=str(sdk.root), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=NO_WINDOW)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            out, _ = proc.communicate()
            res["detail"] = T("android.verify.timeout", s=timeout)
        text = out.decode("utf-8", "replace") if out else ""
        for ln in text.splitlines():
            if "extdata" in ln or "Error" in ln or "Traceback" in ln:
                log("  | " + ln.strip()[:200])
    except Exception as exc:
        res["detail"] = str(exc)
    finally:
        for f in (probe, probe.with_suffix(".rpyc")):
            if f.is_file():
                f.unlink()
    res["elapsed"] = time.time() - t0
    tb = build_dir / "traceback.txt"
    if rep.is_file():
        try:
            report = json.loads(rep.read_text(encoding="utf-8"))
        except Exception:
            report = None
        res["report"] = report
        if report:
            ext = report.get("extdata") or {}
            images_ok = all(isinstance(x[1], list) for x in report.get("probe_images", [])) if entry.data.get("data_mode") == "external" else True
            if not report.get("has_start"):
                res["detail"] = T("android.verify.no_start")
            elif entry.data.get("data_mode") == "external" and (ext.get("missing") or not images_ok):
                res["detail"] = T("android.verify.images_missing")
            elif report.get("stage") != "main_menu_rendered":
                res["detail"] = res["detail"] or T("android.verify.no_menu")
            else:
                res["ok"] = True
                res["detail"] = T("android.verify.ok_detail", version=report.get("renpy_version", "?"), files=report.get("files", "?"),
                                  images=len([x for x in report.get("probe_images", []) if isinstance(x[1], list)]))
    elif not res["detail"]:
        res["detail"] = T("android.verify.no_report")
    if tb.is_file() and not res["ok"]:
        try:
            last = [ln for ln in tb.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()][-1:]
            res["detail"] += " — " + (last[0][:200] if last else "traceback.txt")
        except OSError:
            pass
    log(("OK: " if res["ok"] else "KO: ") + res["detail"])
    return store()


def delete_build(name: str) -> bool:
    d = OUT_DIR / name
    if not d.is_dir() or d.resolve().parent != OUT_DIR.resolve():
        return False
    _rmtree_force(d)
    b = BUILD_DIR / name
    if b.is_dir():
        _rmtree_force(b)
    return not d.exists()


# ----------------------------------------------------------------------------
# Nettoyage des caches (SDK, JDK, Gradle, unrpyc) et sauvegarde des clés
# ----------------------------------------------------------------------------
@dataclass
class CacheEntry:
    kind: str          # sdk | jdk | gradle | unrpyc | downloads | build
    name: str
    path: Path
    bytes: int
    in_use: bool


def list_caches(current_sdk: str = "") -> list[CacheEntry]:
    out: list[CacheEntry] = []
    if SDK_DIR.is_dir():
        for d in sorted(SDK_DIR.iterdir(), key=lambda x: vtuple(x.name)):
            if d.is_dir():
                out.append(CacheEntry("sdk", d.name, d, dir_size(d), d.name == current_sdk))
    if JDK_DIR.is_dir():
        for d in sorted(JDK_DIR.iterdir()):
            if d.is_dir() and d.name != "downloads":
                out.append(CacheEntry("jdk", d.name, d, dir_size(d), False))
    for kind, d in (("gradle", GRADLE_HOME), ("unrpyc", UNRPYC_DIR), ("downloads", ANDROID_ROOT / "downloads"), ("downloads", JDK_DIR / "downloads")):
        if d.is_dir():
            out.append(CacheEntry(kind, d.name if kind != "downloads" else str(d.relative_to(ANDROID_ROOT)), d, dir_size(d), False))
    if BUILD_DIR.is_dir():
        for d in sorted(BUILD_DIR.iterdir()):
            if d.is_dir():
                out.append(CacheEntry("build", d.name, d, dir_size(d), False))
    return out


def delete_cache(path: Path) -> bool:
    p = Path(path).resolve()
    root = ANDROID_ROOT.resolve()
    if root not in p.parents or p in (SDK_DIR.resolve(), JDK_DIR.resolve(), KEYS_DIR.resolve(), OUT_DIR.resolve()):
        return False
    if KEYS_DIR.resolve() in p.parents or p == KEYS_DIR.resolve():
        return False
    _rmtree_force(p)
    return not p.exists()


def export_keys(dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    done: list[Path] = []
    for k in ("android.keystore", "bundle.keystore"):
        src = KEYS_DIR / k
        if src.is_file():
            shutil.copy2(src, dest / k)
            done.append(dest / k)
    return done
