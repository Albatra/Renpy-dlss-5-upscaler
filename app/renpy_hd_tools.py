r"""
renpy_hd_tools.py - outils RenPyHD hors DLSS : extraction des archives .rpa et traduction du jeu.

Extraction (.rpa)
  * moteur Python intégré (classe Rpa du moteur : index RPA-2.0 / RPA-3.0, extraction fichier par fichier,
    reprenable, fichiers existants sautés) ;
  * moteur optionnel tools\rpaExtract.exe (iwanPlays, basé sur unrpa) : prend les .rpa en arguments et extrait
    à côté de l'archive en écrasant tout ; on ferme son entrée standard pour sauter son « Appuyez sur une touche ».

Traduction
  A. « Extraire les textes » : commande `translate <langue>` du moteur Ren'Py du jeu lui-même
     (lib\windows-i686\python.exe -EO <Jeu>.py . translate <langue> pour Ren'Py 7, lib\py3-windows-x86_64\python.exe
     pour Ren'Py 8). Un petit fichier temporaire zz_renpyhd_tl.rpy étend la liste des fichiers à traduire aux scripts
     compilés (.rpyc, archives .rpa) : sans lui, Ren'Py ne génère les blocs de dialogue que pour les .rpy présents.
  B. « Traduire » : les fichiers game\tl\<langue>\*.rpy sont analysés en segments (répliques et chaînes old/new) ;
     le balisage Ren'Py ({i}, {w}, [name], %(x)s, \", \n, …) est remplacé par des repères [1], [2]… avant la traduction
     automatique et restauré ensuite (retour au texte d'origine si un repère manque). Moteurs : modèles Argos Translate
     (hors ligne, via CTranslate2 + SentencePiece), Google Translate (en ligne, deep-translator), export / import manuel.
  C. « Installer » : zz_renpyhd_lang.rpy (langue par défaut + Maj+L pour basculer), vérification par lancement du jeu.

Indépendant de Gradio : fonctions, callbacks de progression, événement d'annulation.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import renpy_hd_core as core
from renpy_hd_core import T   # textes localisés

APP_DIR = Path(__file__).resolve().parent
TOOLS_DIR = APP_DIR.parent / "tools"
RPAEXTRACT = TOOLS_DIR / "rpaExtract.exe"
DATA_DIR = APP_DIR / "data"
ARGOS_DIR = DATA_DIR / "argos"
CACHE_DIR = DATA_DIR / "tl_cache"
ARGOS_INDEX_URL = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RenPyHD/1.0"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TL_HELPER = "zz_renpyhd_tl.rpy"          # temporaire : étend `translate` aux scripts compilés
TL_CHECK = "zz_renpyhd_check.rpy"        # temporaire : vérification après installation
LANG_HOOK = "zz_renpyhd_lang.rpy"        # installé : langue par défaut + bascule Maj+L
TL_MANIFEST = "renpyhd_translation.json"  # dans game/tl/<langue>/ : ce que RenPyHD a créé
TL_INFO = "renpyhd_tl_info.json"          # écrit par le helper à la racine du jeu
AUTO_ENGINES_ENABLED = False              # moteurs automatiques (ArgosEngine / GoogleEngine) conservés dans le code, sans interface

# (code Argos, nom de langue Ren'Py, libellé)
LANGUAGES = [
    ("fr", "french", "Français"), ("en", "english", "English"), ("de", "german", "Deutsch"), ("es", "spanish", "Español"),
    ("it", "italian", "Italiano"), ("pt", "portuguese", "Português (Brasil)"), ("nl", "dutch", "Nederlands"), ("pl", "polish", "Polski"),
    ("ru", "russian", "Русский"), ("uk", "ukrainian", "Українська"), ("tr", "turkish", "Türkçe"), ("ja", "japanese", "日本語"),
    ("zh", "chinese", "中文 (简体)"), ("ko", "korean", "한국어"), ("ar", "arabic", "العربية"), ("sv", "swedish", "Svenska"),
    ("cs", "czech", "Čeština"), ("hu", "hungarian", "Magyar"), ("ro", "romanian", "Română"), ("el", "greek", "Ελληνικά"),
    ("da", "danish", "Dansk"), ("fi", "finnish", "Suomi"), ("nb", "norwegian", "Norsk"), ("id", "indonesian", "Bahasa Indonesia"),
    ("vi", "vietnamese", "Tiếng Việt"), ("th", "thai", "ไทย"), ("he", "hebrew", "עברית"), ("ca", "catalan", "Català"),
]
LANG_BY_CODE = {c: (name, label) for c, name, label in LANGUAGES}
LANG_LABELS = {f"{label} ({name})": c for c, name, label in LANGUAGES}     # libellé d'interface -> code


def lang_label(code: str) -> str:
    name, label = LANG_BY_CODE.get(code, (code, code))
    return f"{label} ({name})"


def renpy_lang_name(code: str) -> str:
    return LANG_BY_CODE.get(code, (code, code))[0]


def log_default(_msg: str) -> None:
    pass


# ============================================================================
# Extraction des archives .rpa
# ============================================================================
@dataclass
class RpaInfo:
    path: Path
    name: str
    size: int
    entries: int
    bytes_total: int
    already_loose: int      # entrées déjà présentes en fichier libre
    images: int             # entrées image (jpg/png/webp)
    images_loose: int
    error: str = ""

    def label(self) -> str:
        if self.error:
            return T("tools.rpa.label_error", name=self.name, size=core.human_size(self.size), err=self.error)
        todo = self.entries - self.already_loose
        return T("tools.rpa.label", name=self.name, size=core.human_size(self.size), entries=self.entries,
                 total=core.human_size(self.bytes_total), images=self.images, todo=todo)


def _rpa_dest(game: Path, key: str) -> Path | None:
    parts = [p for p in key.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or ":" in key:
        return None
    return game.joinpath(*parts)


def list_rpas(game_root: str) -> tuple[Path, list[RpaInfo]]:
    game = core.find_game_dir(game_root)
    infos: list[RpaInfo] = []
    for p in sorted(game.glob("*.rpa")):
        try:
            rpa = core.Rpa(p)
        except Exception as exc:
            infos.append(RpaInfo(p, p.name, p.stat().st_size, 0, 0, 0, 0, 0, error=str(exc)[:80]))
            continue
        loose = imgs = imgs_loose = 0
        total = 0
        for key in rpa.index:
            total += rpa.size(key)
            dest = _rpa_dest(game, key)
            exists = dest is not None and dest.is_file()
            loose += exists
            if Path(key).suffix.lower() in core.FORMAT_BY_EXT:
                imgs += 1
                imgs_loose += exists
        infos.append(RpaInfo(p, p.name, p.stat().st_size, len(rpa.index), total, loose, imgs, imgs_loose))
    return game, infos


@dataclass
class ExtractProgress:
    total: int = 0
    done: int = 0
    skipped: int = 0
    bytes_done: int = 0
    current: str = ""
    archive: str = ""
    started: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    @property
    def fraction(self) -> float:
        return (self.done + self.skipped) / self.total if self.total else 0.0

    @property
    def eta(self) -> float:
        n = self.done + self.skipped
        return (self.total - n) * self.elapsed / n if n else 0.0


@dataclass
class ExtractSummary:
    written: int = 0
    skipped: int = 0
    bytes_written: int = 0
    elapsed: float = 0.0
    errors: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False
    renamed: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def extract_rpas(game_root: str, names: list[str], engine: str, skip_existing: bool, rename_bak: bool,
                 log: Callable[[str], None], on_progress: Callable[[ExtractProgress], None],
                 cancel: threading.Event) -> ExtractSummary:
    """Extrait les archives choisies dans game/ (mêmes chemins relatifs que dans l'archive, ce que Ren'Py attend)."""
    game = core.find_game_dir(game_root)
    summary = ExtractSummary()
    t0 = time.time()
    archives = [game / n for n in names if (game / n).is_file()]
    if not archives:
        summary.messages.append(T("tools.rpa.msg.none_chosen"))
        return summary
    if engine == "rpaextract":
        _extract_with_exe(archives, skip_existing, log, on_progress, cancel, summary)
    else:
        _extract_with_python(game, archives, skip_existing, log, on_progress, cancel, summary)
    if rename_bak and not summary.cancelled:
        failed = {a for a, _e in summary.errors}
        for a in archives:
            if a.name in failed:
                continue
            target = a.with_name(a.name + ".bak")
            try:
                if not target.exists():
                    a.rename(target)
                    summary.renamed.append(target.name)
                    log(f"Archive renommée : {target.name}")
            except OSError as exc:
                summary.errors.append((a.name, f"renommage impossible : {exc}"))
    summary.elapsed = time.time() - t0
    return summary


def _extract_with_python(game: Path, archives: list[Path], skip_existing: bool, log, on_progress, cancel,
                         summary: ExtractSummary) -> None:
    rpas = []
    for a in archives:
        try:
            rpas.append(core.Rpa(a))
        except Exception as exc:
            summary.errors.append((a.name, f"index illisible : {exc}"))
            log(f"ÉCHEC {a.name} : index illisible ({exc})")
    progress = ExtractProgress(total=sum(len(r.index) for r in rpas))
    last = 0.0
    for rpa in rpas:
        progress.archive = rpa.path.name
        log(f"[{rpa.path.name}] {len(rpa.index)} entrée(s), moteur Python intégré")
        errors_before = len(summary.errors)
        for key in sorted(rpa.index):
            if cancel.is_set():
                summary.cancelled = True
                break
            dest = _rpa_dest(game, key)
            if dest is None:
                summary.errors.append((rpa.path.name, f"chemin refusé : {key}"))
                progress.skipped += 1
                continue
            if skip_existing and dest.is_file():
                progress.skipped += 1
                summary.skipped += 1
            else:
                progress.current = key
                tmp = dest.with_name(f".{dest.name}.renpyhd.tmp")
                try:
                    rpa.extract(key, tmp)
                    os.replace(tmp, dest)
                    size = dest.stat().st_size
                    progress.bytes_done += size
                    summary.bytes_written += size
                    summary.written += 1
                    progress.done += 1
                except Exception as exc:
                    tmp.unlink(missing_ok=True)
                    summary.errors.append((rpa.path.name, f"{key} : {exc}"))
                    progress.skipped += 1
                    log(f"  ÉCHEC {key} : {exc}")
            now = time.time()
            if now - last > 0.3:
                last = now
                on_progress(progress)
        on_progress(progress)
        if summary.cancelled:
            log(f"  Annulé pendant {rpa.path.name} : {summary.written} fichier(s) écrit(s) jusqu'ici (relancer reprend).")
            break
        log(f"  {rpa.path.name} : {summary.written} écrit(s), {summary.skipped} déjà présent(s), "
            f"{len(summary.errors) - errors_before} erreur(s)")


def _extract_with_exe(archives: list[Path], skip_existing: bool, log, on_progress, cancel, summary: ExtractSummary) -> None:
    if not RPAEXTRACT.is_file():
        summary.errors.append(("rpaExtract.exe", T("tools.rpa.msg.exe_missing", path=RPAEXTRACT)))
        return
    progress = ExtractProgress(total=len(archives))
    if skip_existing:
        summary.messages.append(T("tools.rpa.msg.exe_overwrites"))
    for a in archives:
        if cancel.is_set():
            summary.cancelled = True
            break
        progress.archive = progress.current = a.name
        on_progress(progress)
        before = {p for p in a.parent.rglob("*") if p.is_file()}
        log(f"[{a.name}] rpaExtract.exe (tiers, écrase les fichiers existants)")
        t0 = time.time()
        try:
            proc = subprocess.run([str(RPAEXTRACT), str(a)], cwd=str(a.parent), stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=6 * 3600, creationflags=NO_WINDOW)
            out = (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:
            summary.errors.append((a.name, str(exc)))
            log(f"  ÉCHEC {a.name} : {exc}")
            continue
        tail = [ln for ln in out.splitlines() if ln.strip() and "Appuyez" not in ln and "Press any key" not in ln]
        for ln in tail[-6:]:
            log("  " + ln.strip())
        ok = "DONE" in out and "something weird" not in out
        after = {p for p in a.parent.rglob("*") if p.is_file()}
        new_files = [p for p in after - before if not p.name.endswith(".rpa")]
        written = sum(p.stat().st_size for p in new_files)
        summary.written += len(new_files)
        summary.bytes_written += written
        if not ok:
            summary.errors.append((a.name, (tail[-1] if tail else "échec inconnu")[:160]))
        progress.done += 1
        log(f"  {a.name} : {len(new_files)} nouveau(x) fichier(s) ({core.human_size(written)}) en {time.time() - t0:.0f} s"
            + ("" if ok else " — ERREUR signalée par rpaExtract"))
        on_progress(progress)


def rpa_images_pending(game_root: str) -> tuple[int, int]:
    """(images encore uniquement dans les .rpa, archives). Sert à proposer l'extraction dans l'onglet principal."""
    try:
        _game, infos = list_rpas(game_root)
    except Exception:
        return 0, 0
    return sum(i.images - i.images_loose for i in infos if not i.error), len(infos)


# ============================================================================
# Traduction — A. génération des fichiers tl/<langue> par le moteur Ren'Py du jeu
# ============================================================================
@dataclass
class RuntimeInfo:
    root: Path
    python: Path | None
    launcher: Path | None       # <Jeu>.py
    exe: Path | None            # <Jeu>.exe
    version: str
    label: str


def find_runtime(root: Path) -> RuntimeInfo:
    version = core.detect_renpy_version(root)
    python = None
    for rel in ("lib/py3-windows-x86_64/python.exe", "lib/windows-x86_64/python.exe", "lib/windows-i686/python.exe"):
        if (root / rel).is_file():
            python = root / rel
            break
    exes = [p for p in root.glob("*.exe") if p.stem.lower() not in ("unins000", "rpaextract", "renpyhd")]
    pys = [p for p in root.glob("*.py")]
    launcher = None
    for e in exes:
        cand = root / (e.stem + ".py")
        if cand.is_file():
            launcher = cand
            break
    if launcher is None and pys:
        launcher = pys[0]
    exe = (root / (launcher.stem + ".exe")) if launcher and (root / (launcher.stem + ".exe")).is_file() else (exes[0] if exes else None)
    if python and launcher:
        label = f"{python.relative_to(root).as_posix()} -EO {launcher.name}"
    elif exe:
        label = f"{exe.name} (attente de fin de processus)"
    else:
        label = "aucun lanceur trouvé"
    return RuntimeInfo(root, python, launcher, exe, version, label)


def run_renpy(rt: RuntimeInfo, args: list[str], log: Callable[[str], None], timeout: int = 3600,
              cancel: threading.Event | None = None) -> tuple[int, str]:
    """Exécute `<Jeu> . <args>` et attend la fin. Renvoie (code retour, sortie)."""
    if rt.python and rt.launcher:
        cmd = [str(rt.python), "-EO", str(rt.launcher), ".", *args]
    elif rt.exe:
        cmd = [str(rt.exe), ".", *args]
    else:
        raise RuntimeError("Aucun lanceur Ren'Py trouvé (python.exe dans lib\\, ni <Jeu>.exe).")
    log("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    proc = subprocess.Popen(cmd, cwd=str(rt.root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, env=env)
    out_lines: list[str] = []

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            out_lines.append(line.rstrip())

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t_start = time.time()
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            proc.kill()
            break
        if time.time() - t_start > timeout:
            proc.kill()
            out_lines.append("(délai dépassé, processus tué)")
            break
        time.sleep(0.2)
    proc.wait()
    t.join(timeout=2)
    if rt.python is None and rt.exe is not None:
        _wait_process_gone(rt.exe.name, timeout=timeout, cancel=cancel)
    return proc.returncode or 0, "\n".join(out_lines)


def _wait_process_gone(image_name: str, timeout: int, cancel: threading.Event | None) -> None:
    """Le .exe Ren'Py se détache (pythonw) : on attend qu'aucun processus de ce nom ne tourne plus."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cancel is not None and cancel.is_set():
            return
        try:
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH"], capture_output=True, text=True,
                                 creationflags=NO_WINDOW).stdout
        except Exception:
            return
        if image_name.lower() not in out.lower():
            return
        time.sleep(1.0)


HELPER_SRC = '''# RenPyHD : fichier temporaire (supprimé après l'extraction des textes).
# Étend la commande `translate` de Ren'Py aux scripts compilés (.rpyc, archives .rpa) et écrit un résumé.
init -999 python:
    import os as _rhd_os
    import renpy.translation.generation as _rhd_gen
    import renpy.translation.scanstrings as _rhd_scan
    _rhd_orig_list = _rhd_gen.translate_list_files
    _rhd_orig_short = _rhd_gen.shorten_filename
    _rhd_orig_scan = _rhd_scan.scan_strings
    def _rhd_list_files():
        seen = set()
        rv = []
        for fn in list(_rhd_orig_list()) + sorted(renpy.game.script.translator.file_translates.keys()):
            if fn not in seen and not _rhd_os.path.basename(fn).startswith("zz_renpyhd_"):
                seen.add(fn)
                rv.append(fn)
        return rv
    def _rhd_shorten(filename):
        fn, common = _rhd_orig_short(filename)
        if not common and not _rhd_os.path.isabs(filename):
            f = filename.replace("\\\\", "/")
            if f.startswith("game/"):
                f = f[5:]
            fn = f
        return fn, common
    def _rhd_scan_strings(filename):
        if _rhd_os.path.exists(filename):
            return _rhd_orig_scan(filename)
        rv = []
        for line, s in renpy.game.script.translator.additional_strings[filename]:
            rv.append(_rhd_scan.String(filename, line, s, False))
        return rv
    _rhd_gen.translate_list_files = _rhd_list_files
    _rhd_gen.shorten_filename = _rhd_scan.shorten_filename = _rhd_shorten
    _rhd_scan.scan_strings = _rhd_scan_strings

init 999 python:
    import json as _rhd_json
    try:
        _t = renpy.game.script.translator
        _langs = sorted(set([str(k[1]) for k in _t.language_translates.keys() if k[1] is not None]))
        _info = {"languages": _langs, "files": len(_t.file_translates),
                 "blocks": sum([len(v) for v in _t.file_translates.values()]),
                 "version": renpy.version(), "gamedir": renpy.config.gamedir}
        _data = _rhd_json.dumps(_info, ensure_ascii=True)
        if not isinstance(_data, bytes):
            _data = _data.encode("utf-8")
        with open(_rhd_os.path.join(renpy.config.basedir, "%(info)s"), "wb") as _f:
            _f.write(_data)
    except Exception as _e:
        pass
''' % {"info": TL_INFO}

CHECK_SRC = '''# RenPyHD : fichier temporaire de vérification (supprimé après le test).
init 1600 python:
    import json as _rhd_json, os as _rhd_os
    _lang = "%(lang)s"
    _t = renpy.game.script.translator
    _blocks = len([1 for k in _t.language_translates.keys() if k[1] == _lang])
    _samples = {}
    for _s in ("Back", "Skip", "Save", "Load", "Quit", "Preferences", "Yes", "No", "Start"):
        try:
            _samples[_s] = renpy.translation.translate_string(_s, _lang)
        except Exception as _e:
            _samples[_s] = "ERREUR: " + str(_e)
    _info = {"pref_language": renpy.game.preferences.language, "default_language": getattr(config, "default_language", None),
             "dialogue_blocks": _blocks, "samples": _samples, "version": renpy.version(),
             "hook_loaded": "renpyhd_language_toggle" in list(config.overlay_screens)}
    _data = _rhd_json.dumps(_info, ensure_ascii=True)
    if not isinstance(_data, bytes):
        _data = _data.encode("utf-8")
    with open(_rhd_os.path.join(renpy.config.basedir, "renpyhd_check.json"), "wb") as _f:
        _f.write(_data)
    _rhd_os._exit(0)
'''

LANG_HOOK_SRC = '''# zz_renpyhd_lang.rpy — installé par RenPyHD (traduction « %(lang)s »).
# Sans ce fichier, le jeu reste dans sa langue d'origine : le supprimer suffit à tout annuler.
# Au premier lancement, la langue « %(lang)s » est sélectionnée ; Maj+L bascule entre la traduction et l'original.
define config.default_language = "%(lang)s"   # RENPYHD:LANG

init 999 python:
    if getattr(persistent, "_renpyhd_lang_applied", None) != "%(lang)s":
        persistent._renpyhd_lang_applied = "%(lang)s"
        _preferences.language = "%(lang)s"

    def _renpyhd_toggle_language():
        target = None if _preferences.language == "%(lang)s" else "%(lang)s"
        renpy.change_language(target)
        try:
            renpy.notify(u"Langue : " + (target if target else u"originale"))
        except Exception:
            pass

screen renpyhd_language_toggle():
    key "shift_K_l" action Function(_renpyhd_toggle_language)

init 999 python:
    if "renpyhd_language_toggle" not in config.overlay_screens:
        config.overlay_screens.append("renpyhd_language_toggle")
'''


def _remove_helper(game: Path, name: str) -> None:
    for n in (name, name + "c"):
        with_suppress_unlink(game / n)


def with_suppress_unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


@dataclass
class GenerateResult:
    tl_dir: Path
    files: list[str]                    # fichiers .rpy relatifs à tl/<langue>/
    dialogue: int = 0
    strings: int = 0
    existing_languages: list[str] = field(default_factory=list)
    preexisting: bool = False           # tl/<langue> existait déjà (pas créé par RenPyHD)
    merged: bool = False
    elapsed: float = 0.0
    output: str = ""
    runtime: str = ""
    version: str = ""
    error: str = ""


def _load_manifest(tl_dir: Path) -> dict:
    f = tl_dir / TL_MANIFEST
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_manifest(tl_dir: Path, data: dict) -> None:
    tl_dir.mkdir(parents=True, exist_ok=True)
    (tl_dir / TL_MANIFEST).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def tl_status(game_root: str, lang: str) -> dict:
    """État de tl/<langue> pour l'interface : existe, créé par nous, hook installé, langues du jeu."""
    game = core.find_game_dir(game_root)
    tl_dir = game / "tl" / lang
    manifest = _load_manifest(tl_dir)
    hook = game / LANG_HOOK
    hook_lang = ""
    if hook.is_file():
        m = re.search(r'default_language\s*=\s*"([^"]+)"', hook.read_text(encoding="utf-8", errors="ignore"))
        hook_lang = m.group(1) if m else "?"
    return {"exists": tl_dir.is_dir(), "ours": bool(manifest), "manifest": manifest, "hook": hook.is_file(), "hook_lang": hook_lang,
            "rpy_files": sum(1 for _ in tl_dir.rglob("*.rpy")) if tl_dir.is_dir() else 0}


def generate_tl(game_root: str, lang: str, merge: bool, log: Callable[[str], None], cancel: threading.Event) -> GenerateResult:
    game = core.find_game_dir(game_root)
    root = game.parent
    tl_dir = game / "tl" / lang
    result = GenerateResult(tl_dir=tl_dir, files=[])
    manifest = _load_manifest(tl_dir)
    preexisting = tl_dir.is_dir() and not manifest and any(tl_dir.rglob("*.rpy*"))
    result.preexisting = preexisting
    if preexisting and not merge:
        result.error = T("tools.tl.err.preexisting", lang=lang, files=sum(1 for _ in tl_dir.rglob('*.rpy')))
        return result
    rt = find_runtime(root)
    result.runtime, result.version = rt.label, rt.version
    if not ((rt.python and rt.launcher) or rt.exe):
        result.error = T("tools.tl.err.no_runtime")
        return result
    before = {p.relative_to(tl_dir).as_posix() for p in tl_dir.rglob("*") if p.is_file()} if tl_dir.is_dir() else set()
    t0 = time.time()
    info_file = root / TL_INFO
    with_suppress_unlink(info_file)
    tb = root / "traceback.txt"
    tb_before = tb.stat().st_mtime if tb.is_file() else 0.0
    (game / TL_HELPER).write_text(HELPER_SRC, encoding="utf-8")
    try:
        rc, out = run_renpy(rt, ["translate", lang, "--no-todo"], log, cancel=cancel)
    finally:
        _remove_helper(game, TL_HELPER)
    result.output = out
    result.elapsed = time.time() - t0
    if info_file.is_file():
        try:
            info = json.loads(info_file.read_text(encoding="utf-8"))
            result.existing_languages = [l for l in info.get("languages", []) if l != lang]
            if lang in info.get("languages", []):
                result.existing_languages.append(f"{lang} (déjà présente dans le jeu)")
        except Exception:
            pass
        with_suppress_unlink(info_file)
    if tb.is_file() and tb.stat().st_mtime > tb_before:
        result.error = T("tools.tl.err.traceback") + "\n" + tb.read_text(encoding="utf-8", errors="ignore")[-1500:]
    if cancel.is_set():
        result.error = T("tools.tl.err.cancelled")
    if rc and not result.error:
        result.error = T("tools.tl.err.exit_code", rc=rc) + f"\n{out[-1500:]}"
    if not tl_dir.is_dir():
        if not result.error:
            result.error = T("tools.tl.err.nothing_generated") + "\n" + out[-800:]
        return result
    files = sorted(p.relative_to(tl_dir).as_posix() for p in tl_dir.rglob("*.rpy"))
    result.files = files
    new_files = sorted(f for f in {p.relative_to(tl_dir).as_posix() for p in tl_dir.rglob("*") if p.is_file()} - before)
    segs = parse_tl_dir(tl_dir)
    result.dialogue = sum(1 for s in segs if s.kind == "say")
    result.strings = sum(1 for s in segs if s.kind == "string")
    result.merged = preexisting
    manifest = manifest or {"created_by_renpyhd": True, "language": lang, "created": time.strftime("%Y-%m-%d %H:%M")}
    manifest.update({"merged_into_existing": bool(preexisting) or manifest.get("merged_into_existing", False),
                     "files_created": sorted(set(manifest.get("files_created", [])) | set(new_files)),
                     "renpy_version": rt.version, "last_generation": time.strftime("%Y-%m-%d %H:%M"),
                     "dialogue": result.dialogue, "strings": result.strings})
    _save_manifest(tl_dir, manifest)
    for n in (TL_HELPER + "c",):
        with_suppress_unlink(game / n)
    return result


# ============================================================================
# Traduction — B. analyse des fichiers tl, protection du balisage, moteurs
# ============================================================================
@dataclass
class Segment:
    file: str            # relatif à tl/<langue>/
    line: int            # index de ligne (0-based) de la ligne à réécrire
    kind: str            # "say" | "string"
    prefix: str          # tout ce qui précède le guillemet ouvrant
    literal: str         # contenu du littéral (forme échappée, sans guillemets)
    suffix: str          # ce qui suit le guillemet fermant
    source: str          # texte d'origine (old "..." ou commentaire # original), forme échappée
    translated: bool = False   # déjà différent de la source dans le fichier

    @property
    def needs_translation(self) -> bool:
        return not self.translated


SAY_KEYWORDS = {"voice", "play", "queue", "stop", "pause", "window", "nvl", "show", "hide", "scene", "with", "jump", "call",
                "return", "python", "image", "define", "default", "menu", "if", "else", "elif", "while", "pass", "$", "label",
                "screen", "init", "translate", "old", "new", "sound", "music", "camera"}
TRANSLATE_HDR = re.compile(r"^translate\s+(\S+)\s+(\S+?):\s*$")


def _scan_string(line: str, start: int) -> int:
    """Index du guillemet fermant d'un littéral commençant en `start` (guillemet double), -1 sinon."""
    i = start + 1
    while i < len(line):
        c = line[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i
        i += 1
    return -1


def split_say_line(line: str) -> tuple[str, str, str] | None:
    """(préfixe, littéral, suffixe) du dernier littéral "..." hors parenthèses d'une ligne de code ; None si pas une réplique."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    first = stripped.split(None, 1)[0]
    if first in SAY_KEYWORDS or first.startswith("$"):
        return None
    spans: list[tuple[int, int]] = []
    depth = 0
    i = 0
    while i < len(line):
        c = line[i]
        if c == '"':
            end = _scan_string(line, i)
            if end < 0:
                return None
            if depth == 0:
                spans.append((i, end))
            i = end + 1
            continue
        if c == "'":
            # littéral simple (expression `who`) : on le saute
            j = i + 1
            while j < len(line) and line[j] != "'":
                j += 2 if line[j] == "\\" else 1
            i = j + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth = max(0, depth - 1)
        i += 1
    if not spans:
        return None
    s, e = spans[-1]
    return line[:s + 1], line[s + 1:e], line[e:]


def split_literal_line(line: str) -> tuple[str, str, str] | None:
    """(préfixe, littéral, suffixe) du premier littéral "..." d'une ligne `old "..."` / `new "..."`."""
    s = line.find('"')
    if s < 0:
        return None
    e = _scan_string(line, s)
    if e < 0:
        return None
    return line[:s + 1], line[s + 1:e], line[e:]


def parse_tl_file(path: Path, rel: str) -> tuple[list[str], list[Segment]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    segs: list[Segment] = []
    mode = ""                # "strings" | "block" | ""
    originals: list[str] = []
    old_literal: str | None = None
    for idx, raw in enumerate(lines):
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            continue
        if not line.startswith((" ", "\t")):
            m = TRANSLATE_HDR.match(stripped.lstrip("\ufeff"))
            if m:
                mode = "strings" if m.group(2) == "strings" else "block"
                originals = []
                old_literal = None
            else:
                mode = ""
            continue
        if mode == "strings":
            if stripped.startswith("old "):
                parts = split_literal_line(line)
                old_literal = parts[1] if parts else None
            elif stripped.startswith("new ") and old_literal is not None:
                parts = split_literal_line(line)
                if parts:
                    pre, lit, suf = parts
                    segs.append(Segment(rel, idx, "string", pre, lit, suf, old_literal, translated=(lit != old_literal and lit != "")))
                old_literal = None
        elif mode == "block":
            if stripped.startswith("#"):
                parts = split_say_line(stripped[1:].strip())
                if parts:
                    originals.append(parts[1])
                continue
            parts = split_say_line(line)
            if not parts:
                continue
            pre, lit, suf = parts
            src = originals.pop(0) if originals else lit
            segs.append(Segment(rel, idx, "say", pre, lit, suf, src, translated=(lit != src and lit != "")))
    return lines, segs


def parse_tl_dir(tl_dir: Path) -> list[Segment]:
    segs: list[Segment] = []
    for p in sorted(tl_dir.rglob("*.rpy")):
        rel = p.relative_to(tl_dir).as_posix()
        try:
            _lines, s = parse_tl_file(p, rel)
        except Exception:
            continue
        segs.extend(s)
    return segs


def write_tl_file(path: Path, lines: list[str], segs: list[Segment]) -> None:
    for s in segs:
        lines[s.line] = s.prefix + s.literal + s.suffix
    tmp = path.with_name(path.name + ".renpyhd.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8", newline="")
    os.replace(tmp, path)


# ---- protection du balisage --------------------------------------------------
_TAG = r"\{[^{}]*\}"
_INTERP = r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]"
_PERCENT = r"%\([^)]*\)[sdifr]|%[sdifr%]"
_ESC = r"\\."
_URL = r"(?:https?://|www\.)\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
PROTECT_RE = re.compile("|".join([r"\{\{", r"\[\[", _ESC, _TAG, _INTERP, _PERCENT, _URL]))
BREAK_RE = re.compile(r"\{(?:p|w|nw|fast|clear|done)(?:=[^}]*)?\}|\\n")
PLACEHOLDER_RE = re.compile(r"\[\s*(\d+)\s*\]")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+(?=[\"'(\[A-Z0-9¿¡])")
LETTERS_RE = re.compile(r"[^\W\d_]", re.UNICODE)
ONOMATOPOEIA_RE = re.compile(
    r"^(?:[a-z]*([a-z])\1\1[a-z]*"                       # lettre triplée : Aaargh, Noooo, Mmmh
    r"|(?:aa|uu|ii|hh|mm|nn|ww)[a-z]*|[a-z]*(?:aa|uu|ii|hh|mm|nn|ww)"   # double lettre en tête ou en queue : Uuhah, Hmm, Ahh
    r"|[bcdfghjklmnpqrstvwxz]+"                           # sans voyelle : tsk, pfft, grr, nngh
    r"|h+[aeiou]+h*|[aeiou]+h+"                           # ha, hah, heh, hee, ah, oh, uh, eh
    r"|argh|ugh|huh|meh|wow|whoa|oops|ouch|yay|yikes|phew|ew|um|er|erm|hm|hmph|hmpf|mhm|uh-huh|uh-uh|ha-ha|ho-ho)$",
    re.IGNORECASE)
QUOTE_ESC_RE = re.compile(r"\\(.)")


def _unescape_quotes(s: str) -> str:
    """\\" et \\' deviennent des guillemets simples/doubles ordinaires pour la traduction (les autres échappements restent)."""
    return QUOTE_ESC_RE.sub(lambda m: m.group(1) if m.group(1) in "\"'" else m.group(0), s)


def _escape_quotes(s: str) -> str:
    """Le littéral est écrit entre guillemets doubles : tout " nu redevient \\"."""
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append(s[i:i + 2])
            i += 2
            continue
        out.append('\\"' if c == '"' else c)
        i += 1
    return "".join(out)


def tokenize(text: str) -> list[tuple[str, str]]:
    """Découpe un littéral en (kind, s) : "text", "tag" (balise, échappement : sans sens pour la phrase),
    "interp" (interpolation, %, adresse : un élément de la phrase), "break" (saut : {p}, {w}, \\n…)."""
    out: list[tuple[str, str]] = []
    pos = 0
    for m in PROTECT_RE.finditer(text):
        if m.start() > pos:
            out.append(("text", text[pos:m.start()]))
        tok = m.group(0)
        if BREAK_RE.fullmatch(tok):
            kind = "break"
        elif tok.startswith(("{", "\\")) or tok == "[[":
            kind = "tag"
        else:
            kind = "interp"
        out.append((kind, tok))
        pos = m.end()
    if pos < len(text):
        out.append(("text", text[pos:]))
    return out


def is_untranslatable(s: str) -> bool:
    """Vrai si le texte n'a rien à traduire : pas de lettres, ou seulement des onomatopées / interjections."""
    if len(LETTERS_RE.findall(s)) < 2:
        return True
    words = re.findall(r"[^\W\d_]+", s)
    return bool(words) and all(ONOMATOPOEIA_RE.match(w) for w in words)


def parse_glossary(text: str) -> list[tuple[str, str]]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        term, _, repl = line.partition("=")
        term, repl = term.strip(), repl.strip()
        if term:
            out.append((term, repl or term))
    return sorted(out, key=lambda kv: -len(kv[0]))


def parse_regex_list(text: str) -> list[re.Pattern]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                out.append(re.compile(line))
            except re.error:
                continue
    return out


class Protector:
    """Transforme un littéral en phrases à traduire (repères [1], [2]…) et recompose le résultat."""

    def __init__(self, glossary: list[tuple[str, str]], skip: list[re.Pattern]):
        self.glossary = glossary
        self.skip = skip
        self.gloss_re = re.compile("|".join(r"(?<![\w])" + re.escape(t) + r"(?![\w])" for t, _ in glossary)) if glossary else None
        self.gloss_map = dict(glossary)

    def _apply_glossary(self, tokens: list[tuple[str, str]]) -> list[tuple[str, str]]:
        if self.gloss_re is None:
            return tokens
        out: list[tuple[str, str]] = []
        for kind, s in tokens:
            if kind != "text":
                out.append((kind, s))
                continue
            pos = 0
            for m in self.gloss_re.finditer(s):
                if m.start() > pos:
                    out.append(("text", s[pos:m.start()]))
                out.append(("gloss", self.gloss_map.get(m.group(0), m.group(0))))
                pos = m.end()
            if pos < len(s):
                out.append(("text", s[pos:]))
        return out

    def prepare(self, literal: str) -> tuple[list[tuple[str, object]], list[str]]:
        """Renvoie (plan, phrases). plan : liste de ("raw", texte) ou ("tr", (index_phrases, repères, espaces))."""
        tokens = self._apply_glossary(tokenize(_unescape_quotes(literal)))
        plan: list[tuple[str, object]] = []
        sentences: list[str] = []
        chunk: list[tuple[str, str]] = []

        def is_content(k: str, s: str) -> bool:
            return (k == "text" and bool(s.strip())) or k in ("interp", "gloss")

        def flush() -> None:
            if not chunk:
                return
            has_text = any(k == "text" and s.strip() for k, s in chunk)
            first = next((i for i, (k, s) in enumerate(chunk) if is_content(k, s)), None)
            last = next((i for i in range(len(chunk) - 1, -1, -1) if is_content(*chunk[i])), None)
            if not has_text or first is None or last is None:
                plan.append(("raw", "".join(s for _k, s in chunk)))
                chunk.clear()
                return
            for k, s in chunk[:first]:
                plan.append(("raw", s))
            core_tokens = chunk[first:last + 1]
            keeps: list[str] = []
            parts: list[str] = []
            for i, (k, s) in enumerate(core_tokens):
                if k == "text":
                    parts.append(s)
                else:
                    # repère isolé par des espaces (le moteur traduit mieux « [1] fierté [2] » que « [1]pride[2] ») ;
                    # les espaces ajoutés sont retirés à la restauration (voir assemble : marqueur NUL = collé)
                    prev_glued = i > 0 and core_tokens[i - 1][0] == "text" and core_tokens[i - 1][1][-1:] not in ("", " ")
                    next_glued = i + 1 < len(core_tokens) and core_tokens[i + 1][0] == "text" and core_tokens[i + 1][1][:1] not in ("", " ")
                    keeps.append(("\x00" if prev_glued else "") + s + ("\x00" if next_glued else ""))
                    parts.append((" " if prev_glued else "") + f"[{len(keeps)}]" + (" " if next_glued else ""))
            core_str = "".join(parts)
            lead_ws = core_str[:len(core_str) - len(core_str.lstrip())]
            trail_ws = core_str[len(core_str.rstrip()):]
            inner = core_str.strip()
            if is_untranslatable(inner) or any(p.search(inner) for p in self.skip):
                plan.append(("raw", "".join(s for _k, s in core_tokens)))
            else:
                idx = []
                for sent in SENTENCE_SPLIT_RE.split(inner):
                    if not sent:
                        continue
                    idx.append(len(sentences))
                    sentences.append(sent)
                plan.append(("tr", (idx, keeps, lead_ws, trail_ws)))
            for k, s in chunk[last + 1:]:
                plan.append(("raw", s))
            chunk.clear()

        for kind, s in tokens:
            if kind == "break":
                flush()
                plan.append(("raw", s))
            else:
                chunk.append((kind, s))
        flush()
        return plan, sentences

    @staticmethod
    def _tag_ids(keeps: list[str]) -> set[int]:
        return {i + 1 for i, k in enumerate(keeps) if k.strip("\x00").startswith(("{", "\\")) or k.strip("\x00") == "[["}

    @classmethod
    def retry_variant(cls, sentence: str, keeps: list[str]) -> str | None:
        """Variante sans les balises de mise en forme (second essai quand le moteur perd un repère) ; None si inutile."""
        ids = cls._tag_ids(keeps)
        if not ids:
            return None
        var = PLACEHOLDER_RE.sub(lambda m: "" if int(m.group(1)) in ids else m.group(0), sentence)
        var = re.sub(r"\s{2,}", " ", var).strip()
        return var if var and var != sentence else None

    @staticmethod
    def _ids_ok(src: str, tr: str | None) -> bool:
        return bool(tr and tr.strip()) and sorted(PLACEHOLDER_RE.findall(src)) == sorted(PLACEHOLDER_RE.findall(tr))  # type: ignore[arg-type]

    def retry_variants(self, plan: list[tuple[str, object]], sentences: list[str], lookup) -> list[str]:
        """Phrases à retenter sans mise en forme (celles dont la traduction a perdu un repère)."""
        out: list[str] = []
        for kind, payload in plan:
            if kind != "tr":
                continue
            idx, keeps, _l, _t = payload  # type: ignore[misc]
            for i in idx:
                src = sentences[i]
                if not self._ids_ok(src, lookup(src)):
                    var = self.retry_variant(src, keeps)
                    if var and var not in out:
                        out.append(var)
        return out

    @classmethod
    def assemble(cls, plan: list[tuple[str, object]], sentences: list[str], translations: list[str | None], lookup=None) -> tuple[str, int]:
        """Recompose le littéral ; renvoie (texte, nombre de phrases revenues au texte d'origine).
        `lookup` (phrase -> traduction) sert au second essai sans balises de mise en forme."""
        out: list[str] = []
        fallbacks = 0
        for kind, payload in plan:
            if kind == "raw":
                out.append(payload)  # type: ignore[arg-type]
                continue
            idx, keeps, lead_ws, trail_ws = payload  # type: ignore[misc]
            pieces: list[str] = []
            for i in idx:
                src = sentences[i]
                tr = translations[i]
                if cls._ids_ok(src, tr):
                    pieces.append(tr)  # type: ignore[arg-type]
                    continue
                var = cls.retry_variant(src, keeps) if lookup is not None else None
                tr2 = lookup(var) if var else None
                if var and cls._ids_ok(var, tr2):
                    pieces.append(tr2)  # type: ignore[arg-type]   # mise en forme interne perdue, texte traduit
                    continue
                pieces.append(src)
                fallbacks += 1
            joined = " ".join(pieces)
            restored: list[str] = []
            pos = 0
            for m in PLACEHOLDER_RE.finditer(joined):
                n = int(m.group(1))
                if not 0 < n <= len(keeps):
                    continue
                keep = keeps[n - 1]
                before = joined[pos:m.start()]
                if keep.startswith("\x00"):
                    before = before.rstrip(" ")   # repère collé au mot précédent : espace ajouté pour la traduction retiré
                restored.append(before)
                restored.append(keep.strip("\x00"))
                pos = m.end()
                if keep.endswith("\x00"):
                    while pos < len(joined) and joined[pos] == " ":
                        pos += 1
            restored.append(joined[pos:])
            out.append(lead_ws + "".join(restored) + trail_ws)
        return _escape_quotes("".join(out)), fallbacks


# ---- moteurs ---------------------------------------------------------------
class TranslationCache:
    def __init__(self, engine: str, src: str, dst: str):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.path = CACHE_DIR / f"{engine}_{src}-{dst}.json"
        self.data: dict[str, str] = {}
        self.dirty = 0
        self.lock = threading.Lock()
        if self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def get(self, s: str) -> str | None:
        return self.data.get(s)

    def put(self, s: str, t: str) -> None:
        with self.lock:
            self.data[s] = t
            self.dirty += 1

    def save(self, force: bool = False) -> None:
        with self.lock:
            if not force and self.dirty < 500:
                return
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            self.dirty = 0

    def __len__(self) -> int:
        return len(self.data)


def argos_installed() -> list[dict]:
    """Paquets de langue présents dans app/data/argos (dossiers extraits ; les .argosmodel sont extraits au passage)."""
    ARGOS_DIR.mkdir(parents=True, exist_ok=True)
    for z in ARGOS_DIR.glob("*.argosmodel"):
        try:
            with zipfile.ZipFile(z) as zf:
                top = {n.split("/")[0] for n in zf.namelist()}
                if not all((ARGOS_DIR / t).is_dir() for t in top):
                    zf.extractall(ARGOS_DIR)
        except Exception:
            continue
    out = []
    for md in ARGOS_DIR.glob("*/metadata.json"):
        try:
            meta = json.loads(md.read_text(encoding="utf-8"))
            meta["path"] = str(md.parent)
            meta["size"] = sum(p.stat().st_size for p in md.parent.rglob("*") if p.is_file())
            out.append(meta)
        except Exception:
            continue
    return out


def argos_find(src: str, dst: str) -> dict | None:
    for p in argos_installed():
        if p.get("from_code") == src and p.get("to_code") == dst:
            return p
    return None


def argos_route(src: str, dst: str) -> list[dict] | None:
    """Paquet direct, sinon pivot par l'anglais (src→en, en→dst)."""
    direct = argos_find(src, dst)
    if direct:
        return [direct]
    if src != "en" and dst != "en":
        a, b = argos_find(src, "en"), argos_find("en", dst)
        if a and b:
            return [a, b]
    return None


def argos_index(timeout: int = 60) -> list[dict]:
    import urllib.request
    req = urllib.request.Request(ARGOS_INDEX_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def argos_download(src: str, dst: str, log: Callable[[str], None], on_progress: Callable[[int, int], None],
                   cancel: threading.Event | None = None) -> list[str]:
    """Télécharge (une fois) les paquets nécessaires pour src→dst (direct, sinon pivot anglais). Renvoie les dossiers installés."""
    import urllib.request
    if argos_route(src, dst):
        return [p["path"] for p in argos_route(src, dst) or []]
    index = argos_index()
    by_pair = {(p["from_code"], p["to_code"]): p for p in index}
    wanted: list[tuple[str, str]] = []
    if (src, dst) in by_pair:
        wanted = [(src, dst)]
    elif (src, "en") in by_pair and ("en", dst) in by_pair:
        wanted = [(src, "en"), ("en", dst)]
        log(f"Pas de modèle direct {src}→{dst} : pivot par l'anglais ({src}→en puis en→{dst}).")
    else:
        raise RuntimeError(f"Aucun modèle Argos pour {src}→{dst} (ni via l'anglais). Langues sources disponibles : "
                           + ", ".join(sorted({p['from_code'] for p in index})))
    installed = []
    ARGOS_DIR.mkdir(parents=True, exist_ok=True)
    for pair in wanted:
        if argos_find(*pair):
            installed.append(argos_find(*pair)["path"])  # type: ignore[index]
            continue
        pkg = by_pair[pair]
        links = pkg.get("links") or []
        if not links:
            raise RuntimeError(f"Paquet {pkg.get('code')} sans lien de téléchargement.")
        dest = ARGOS_DIR / f"{pkg['code']}-{str(pkg.get('package_version', '1')).replace('.', '_')}.argosmodel"
        last_exc: Exception | None = None
        for url in links:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=120) as r, open(dest.with_name(dest.name + ".part"), "wb") as f:
                    total = int(r.headers.get("Content-Length") or 0)
                    log(f"Téléchargement {pkg['code']} : {core.human_size(total)} depuis {url}")
                    done = 0
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise RuntimeError("Téléchargement annulé.")
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        on_progress(done, total)
                os.replace(dest.with_name(dest.name + ".part"), dest)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                log(f"  échec depuis {url} : {exc}")
        if last_exc is not None:
            raise RuntimeError(f"Téléchargement impossible : {last_exc}")
        with zipfile.ZipFile(dest) as zf:
            zf.extractall(ARGOS_DIR)
        found = argos_find(*pair)
        if not found:
            raise RuntimeError(f"Paquet {pkg['code']} téléchargé mais métadonnées introuvables.")
        installed.append(found["path"])
        log(f"Modèle installé : {found['path']} ({core.human_size(found['size'])})")
    return installed


class ArgosEngine:
    """Modèle Argos Translate exécuté directement par CTranslate2 + SentencePiece (sans le paquet argostranslate,
    dont les dépendances stanza/spacy/torch pèsent plusieurs centaines de Mo et ne servent qu'au découpage de phrases)."""
    name = "argos"
    batch = 512

    def __init__(self, src: str, dst: str, beam: int = 4, device: str = "auto"):
        import ctranslate2
        import sentencepiece as spm

        route = argos_route(src, dst)
        if not route:
            raise RuntimeError(f"Modèle de langue {src}→{dst} absent : cliquez sur « Télécharger le modèle de langue ».")
        self.beam = max(1, int(beam))
        self.device = "cpu"
        self.device_note = ""
        if device in ("auto", "cuda"):
            try:
                if ctranslate2.get_cuda_device_count() > 0:
                    probe = ctranslate2.Translator(str(Path(route[0]["path"]) / "model"), device="cuda")
                    probe.translate_batch([["▁a"]], beam_size=1)   # échoue si cuBLAS/cuDNN manquent
                    self.device = "cuda"
                    del probe
                else:
                    self.device_note = "aucun GPU CUDA vu par CTranslate2"
            except Exception as exc:
                self.device_note = f"GPU indisponible ({str(exc).splitlines()[0][:80]}) : CPU"
        self.steps: list[tuple[object, object]] = []
        for pkg in route:
            base = Path(pkg["path"])
            sp = spm.SentencePieceProcessor(model_file=str(base / "sentencepiece.model"))
            kw = dict(device=self.device, compute_type="default", inter_threads=1)
            if self.device == "cpu":
                kw["intra_threads"] = max(1, (os.cpu_count() or 4) - 1)
            tr = ctranslate2.Translator(str(base / "model"), **kw)
            self.steps.append((sp, tr))
        self.label = f"Argos {' → '.join(p['from_code'] for p in route)} → {route[-1]['to_code']} ({self.device}, beam {self.beam})"

    def translate(self, sentences: list[str]) -> list[str]:
        texts = list(sentences)
        for sp, tr in self.steps:
            toks = [sp.encode(s, out_type=str) for s in texts]  # type: ignore[attr-defined]
            res = tr.translate_batch(toks, beam_size=self.beam, max_batch_size=2048, batch_type="tokens",  # type: ignore[attr-defined]
                                     replace_unknowns=True, length_penalty=0.2)
            texts = ["".join(r.hypotheses[0]).replace("▁", " ").strip() for r in res]
        return texts


class GoogleEngine:
    """Google Translate via deep-translator (point d'entrée web gratuit, non officiel) : en ligne, lent, limité."""
    name = "google"
    batch = 24

    def __init__(self, src: str, dst: str):
        from deep_translator import GoogleTranslator
        self.gt = GoogleTranslator(source=src or "auto", target=dst)
        self.label = f"Google Translate {src}→{dst} (en ligne)"
        self.last = 0.0

    def _one(self, text: str) -> str:
        for attempt in range(4):
            wait = max(0.0, 0.4 - (time.time() - self.last))
            if wait:
                time.sleep(wait)
            try:
                out = self.gt.translate(text)
                self.last = time.time()
                return out if isinstance(out, str) else text
            except Exception:
                time.sleep(2.0 * (attempt + 1))
        return text

    def translate(self, sentences: list[str]) -> list[str]:
        out: list[str] = []
        i = 0
        while i < len(sentences):
            group: list[str] = []
            size = 0
            while i < len(sentences) and len(group) < self.batch and size + len(sentences[i]) < 4000:
                group.append(sentences[i])
                size += len(sentences[i]) + 1
                i += 1
            if not group:
                group.append(sentences[i][:4500])
                i += 1
            joined = "\n".join(group)
            res = self._one(joined).split("\n") if len(group) > 1 else [self._one(joined)]
            if len(res) != len(group):
                res = [self._one(s) for s in group]
            out.extend(r.strip() for r in res)
        return out


# ---- exécution ---------------------------------------------------------------
@dataclass
class TlProgress:
    total: int = 0            # segments à traduire
    done: int = 0
    sentences_total: int = 0
    sentences_done: int = 0
    fallbacks: int = 0
    current_file: str = ""
    file_index: int = 0
    file_count: int = 0
    started: float = field(default_factory=time.time)
    file_segments: int = 0
    file_sentences: int = 0
    file_sentences_done: int = 0

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    @property
    def live_done(self) -> int:
        frac = self.file_sentences_done / self.file_sentences if self.file_sentences else 0.0
        return min(self.total, self.done + int(frac * self.file_segments))

    @property
    def rate(self) -> float:
        return self.live_done / self.elapsed if self.elapsed > 0 and self.live_done else 0.0

    @property
    def eta(self) -> float:
        return (self.total - self.live_done) / self.rate if self.rate else 0.0

    @property
    def fraction(self) -> float:
        return self.live_done / self.total if self.total else 0.0


@dataclass
class TlSummary:
    translated: int = 0
    unchanged: int = 0
    fallbacks: int = 0
    sentences: int = 0
    cached: int = 0
    elapsed: float = 0.0
    cancelled: bool = False
    files: int = 0
    engine: str = ""
    error: str = ""


_TERMINAL = (".", "!", "?", "…", ":", ";", "»", ")", "\"", "'")

# Chaînes d'interface standard de Ren'Py (renpy/common, écrans par défaut) : traduction fixe, car un moteur
# statistique se trompe sur les mots isolés (« No » → « Numéro », « Back » → « Précédent »).
UI_STRINGS: dict[str, dict[str, str]] = {
    "fr": {
        "Yes": "Oui", "No": "Non", "Back": "Retour", "Skip": "Passer", "Auto": "Auto", "Save": "Sauvegarder", "Load": "Charger",
        "Quit": "Quitter", "Start": "Commencer", "History": "Historique", "Preferences": "Préférences", "Return": "Retour",
        "Main Menu": "Menu principal", "About": "À propos", "Help": "Aide", "Q.Save": "Sauv. rapide", "Q.Load": "Charg. rapide",
        "Page {}": "Page {}", "Automatic saves": "Sauvegardes automatiques", "Quick saves": "Sauvegardes rapides", "empty slot": "emplacement vide",
        "Empty Slot": "Emplacement vide", "Display": "Affichage", "Window": "Fenêtre", "Fullscreen": "Plein écran", "Rollback Side": "Côté retour arrière",
        "Left": "Gauche", "Right": "Droite", "Disable": "Désactiver", "Unseen Text": "Texte non lu", "After Choices": "Après les choix",
        "Transitions": "Transitions", "Text Speed": "Vitesse du texte", "Auto-Forward Time": "Délai d'avance automatique",
        "Music Volume": "Volume de la musique", "Sound Volume": "Volume des sons", "Voice Volume": "Volume des voix", "Mute All": "Tout couper",
        "Test": "Test", "Language": "Langue", "Continue": "Continuer", "Load Game": "Charger une partie", "Save Game": "Sauvegarder la partie",
        "Are you sure?": "Êtes-vous sûr ?", "Are you sure you want to quit?": "Voulez-vous vraiment quitter ?",
        "Are you sure you want to delete this save?": "Voulez-vous vraiment supprimer cette sauvegarde ?",
        "Are you sure you want to overwrite your save?": "Voulez-vous vraiment écraser cette sauvegarde ?",
        "Loading will lose unsaved progress.\\nAre you sure you want to do this?": "Charger fera perdre la progression non sauvegardée.\\nVoulez-vous vraiment continuer ?",
        "Are you sure you want to return to the main menu?\\nThis will lose unsaved progress.": "Voulez-vous vraiment revenir au menu principal ?\\nLa progression non sauvegardée sera perdue.",
        "Are you sure you want to end the replay?": "Voulez-vous vraiment arrêter la relecture ?",
        "Are you sure you want to begin skipping?": "Voulez-vous vraiment commencer à passer le texte ?",
        "Are you sure you want to skip to the next choice?": "Voulez-vous vraiment passer jusqu'au prochain choix ?",
        "Are you sure you want to skip unseen dialogue to the next choice?": "Voulez-vous vraiment passer le texte non lu jusqu'au prochain choix ?",
        "Skipping": "Passage rapide", "Menu": "Menu", "Rollback": "Retour arrière", "Screenshot": "Capture d'écran", "Hide Interface": "Cacher l'interface",
        "Self-voicing": "Lecture vocale", "Clipboard voicing": "Lecture du presse-papiers", "Debug self-voicing": "Débogage de la lecture vocale",
        "Open the accessibility menu.": "Ouvrir le menu d'accessibilité.", "Font Override": "Police de remplacement", "Default": "Par défaut",
        "DejaVu Sans": "DejaVu Sans", "Opendyslexic": "Opendyslexic", "Text Size Scaling": "Taille du texte", "Reset": "Réinitialiser",
        "Line Spacing Scaling": "Interligne", "High Contrast Text": "Texte à contraste élevé", "Enable": "Activer",
    },
}


def ui_strings_for(dst: str) -> dict[str, str]:
    return UI_STRINGS.get(dst, {})


def engine_translate(engine, batch: list[str]) -> list[str]:
    """Appel du moteur avec un point final ajouté aux fragments sans ponctuation (« Leave First » → « Partez d'abord »
    au lieu de « Premier congé ») ; le point ajouté est retiré du résultat."""
    padded = [s if s.rstrip().endswith(_TERMINAL) else s + "." for s in batch]
    out = engine.translate(padded)
    res: list[str] = []
    for s, p, o in zip(batch, padded, out):
        o = (o or "").strip()
        if p != s and o.endswith(".") and not o.endswith("..."):
            o = o[:-1].rstrip()
        res.append(o)
    return res


def translate_tl_dir(tl_dir: Path, engine, src: str, dst: str, glossary_text: str, skip_text: str, limit: int,
                     log: Callable[[str], None], on_progress: Callable[[TlProgress], None], cancel: threading.Event,
                     only_untranslated: bool = True) -> TlSummary:
    """Traduit en place les fichiers tl/<langue>/*.rpy. Reprenable : les segments déjà différents de la source sont sautés,
    le cache (app/data/tl_cache) rend instantanée toute phrase déjà traduite."""
    summary = TlSummary(engine=getattr(engine, "label", engine.name))
    t0 = time.time()
    protector = Protector(parse_glossary(glossary_text), parse_regex_list(skip_text))
    cache = TranslationCache(engine.name, src, dst)
    ui_map = ui_strings_for(dst)
    files = sorted(tl_dir.rglob("*.rpy"))
    parsed: list[tuple[Path, list[str], list[Segment]]] = []
    total = 0
    for p in files:
        lines, segs = parse_tl_file(p, p.relative_to(tl_dir).as_posix())
        todo = [s for s in segs if (s.needs_translation or not only_untranslated)]
        if limit and total + len(todo) > limit:
            todo = todo[:max(0, limit - total)]
        total += len(todo)
        parsed.append((p, lines, todo))
        if limit and total >= limit:
            break
    progress = TlProgress(total=total, file_count=len(parsed))
    log(f"{total} segment(s) à traduire dans {len(parsed)} fichier(s) — moteur {summary.engine} — cache : {len(cache)} phrase(s)")
    on_progress(progress)
    last_tick = [0.0]

    def tick(force: bool = False) -> None:
        now = time.time()
        if force or now - last_tick[0] > 0.4:
            last_tick[0] = now
            on_progress(progress)

    try:
        for fi, (path, lines, todo) in enumerate(parsed, 1):
            if cancel.is_set():
                summary.cancelled = True
                break
            if not todo:
                continue
            progress.file_index = fi
            progress.current_file = path.relative_to(tl_dir).as_posix()
            plans = []
            all_sentences: list[str] = []
            fixed: list[Segment] = []
            for s in todo:
                if s.source in ui_map:            # chaîne d'interface standard : traduction fixe
                    if s.literal != ui_map[s.source]:
                        s.literal = ui_map[s.source]
                        s.translated = True
                        fixed.append(s)
                    continue
                plan, sentences = protector.prepare(s.literal if not s.translated else s.source)
                plans.append((s, plan, sentences))
                all_sentences.extend(sentences)
            unique = list(dict.fromkeys(all_sentences))
            pending = [u for u in unique if cache.get(u) is None]
            progress.file_segments = len(todo)
            progress.file_sentences = len(pending)
            progress.file_sentences_done = 0
            progress.sentences_total += len(unique)
            summary.cached += len(unique) - len(pending)
            tick(True)
            def run_batches(items: list[str]) -> None:
                for i in range(0, len(items), engine.batch):
                    if cancel.is_set():
                        summary.cancelled = True
                        return
                    batch = items[i:i + engine.batch]
                    try:
                        out = engine_translate(engine, batch)
                    except Exception as exc:
                        raise RuntimeError(f"Le moteur de traduction a échoué : {exc}") from exc
                    for s_, t_ in zip(batch, out):
                        if t_ and t_.strip():
                            cache.put(s_, t_.strip())
                    progress.file_sentences_done += len(batch)
                    progress.sentences_done += len(batch)
                    summary.sentences += len(batch)
                    tick()

            run_batches(pending)
            if not summary.cancelled:
                # phrases dont un repère a été perdu : second essai sans les balises de mise en forme
                retry: list[str] = []
                for _s, plan, sentences in plans:
                    for var in protector.retry_variants(plan, sentences, cache.get):
                        if cache.get(var) is None and var not in retry:
                            retry.append(var)
                if retry:
                    progress.file_sentences += len(retry)
                    run_batches(retry)
            changed: list[Segment] = list(fixed)
            summary.translated += len(fixed)
            for s, plan, sentences in plans:
                trs = [cache.get(x) for x in sentences]
                if summary.cancelled and any(t is None for t in trs):
                    continue
                new_lit, fb = protector.assemble(plan, sentences, trs, cache.get)
                summary.fallbacks += fb
                progress.fallbacks += fb
                if new_lit != s.literal:
                    s.literal = new_lit
                    s.translated = True
                    changed.append(s)
                    summary.translated += 1
                else:
                    summary.unchanged += 1
            if changed:
                write_tl_file(path, lines, changed)
                summary.files += 1
            progress.done += len(todo) if not summary.cancelled else len(changed)
            progress.file_segments = progress.file_sentences = progress.file_sentences_done = 0
            cache.save()
            tick(True)
            log(f"  {progress.current_file} : {len(changed)} segment(s) traduit(s) — {progress.rate:.1f} seg/s — reste ≈ {core.format_eta(progress.eta)}")
            if summary.cancelled:
                break
    except Exception as exc:
        summary.error = str(exc)
        log(f"ERREUR : {exc}")
    finally:
        cache.save(force=True)
    summary.elapsed = time.time() - t0
    return summary


# ---- export / import manuel ---------------------------------------------------
# Format éprouvé avec https://www.onlinedoctranslator.com : une ligne « ligneN;texte » par texte (le site garde le numéro,
# ajoute un espace après le « ; », peut changer la casse), fichiers phrase_001.txt… d'au plus N lignes / M octets,
# interpolations Ren'Py [name] laissées telles quelles (le site les conserve), balises {…} protégées par des repères [t1], [t2]…
EXPORT_DIR = "renpyhd_export"
EXPORT_MAP = "_map.json"
ID_FORMATS = {"ligne": "ligneN;texte (recommandé, éprouvé avec onlinedoctranslator.com)", "§": "§N§ texte"}
ID_LINE_RE = re.compile(r"^\s*(?:§\s*(\d+)\s*§|(?:ligne|line)\s*(\d+)\s*[;:])\s?(.*)$", re.IGNORECASE)
ID_INSIDE_RE = re.compile(r"(?:§\s*\d+\s*§|\b(?:ligne|line)\s*\d+\s*[;:])", re.IGNORECASE)
TAG_MARK_RE = re.compile(r"\[\s*t\s*(\d+)\s*\]", re.IGNORECASE)
INTERP_RE = re.compile(r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]|%\([^)]*\)[sdifr]|%[sdifr]")
UI_FILES = ("common.rpy", "screens.rpy", "gui.rpy", "options.rpy", "version_update.rpy", "cheat_mode.rpy", "images.rpy")
UI_LABELS = {"previous", "next", "return", "save", "load", "options", "preferences", "auto", "skip", "history", "quit", "back",
             "yes", "no", "start", "menu", "main menu", "about", "help", "continue", "ok", "cancel", "page {}"}
ASSET_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".webm", ".mp4", ".mkv", ".ogg", ".opus", ".mp3", ".wav", ".ttf", ".otf")


def is_story_text(seg: "Segment") -> bool:
    """Portée « dialogues et choix » : répliques + chaînes des scripts (choix de menus), hors interface / assets / titres."""
    if seg.kind == "string" and Path(seg.file).name.lower() in UI_FILES:
        return False
    t = re.sub(r"\{[^{}]*\}", "", seg.source).strip()
    low = t.lower()
    if not t:
        return False
    if ("/" in t or "\\" in t) and low.endswith(ASSET_EXTS):
        return False
    if low in UI_LABELS or re.fullmatch(r"(?:part|chapter|chapitre|episode|day|jour)\s*\d+[a-z]?", low):
        return False
    return True


def protect_segment(literal: str, protect_tags: bool = True) -> tuple[str, list[str]]:
    """Forme « une ligne » d'un texte : balises {…}, sauts et échappements → repères [t1], [t2]… ; les interpolations
    [name] / %(x)s restent telles quelles (les traducteurs en ligne les conservent)."""
    text = _unescape_quotes(literal)
    keeps: list[str] = []
    if not protect_tags:
        return text.replace("\r", " ").replace("\n", " "), keeps
    parts: list[str] = []
    for kind, s in tokenize(text):
        if kind in ("tag", "break"):
            keeps.append(s)
            parts.append(f"[t{len(keeps)}]")
        else:
            parts.append(s)
    return "".join(parts).replace("\r", " ").replace("\n", " "), keeps


def _interps(s: str) -> list[str]:
    return sorted(INTERP_RE.findall(TAG_MARK_RE.sub("", s)))


def restore_segment(translated: str, keeps: list[str], source_text: str) -> tuple[str | None, str]:
    """Réinjecte les balises ; vérifie repères et interpolations. Renvoie (littéral, raison d'échec)."""
    ids = sorted(int(m) for m in TAG_MARK_RE.findall(translated))
    if ids != list(range(1, len(keeps) + 1)):
        return None, "repères [t n] manquants ou en trop"
    if _interps(translated) != _interps(source_text):
        return None, "interpolation [nom] / %(x)s altérée"
    out = TAG_MARK_RE.sub(lambda m: keeps[int(m.group(1)) - 1], translated)
    return _escape_quotes(out), ""


@dataclass
class ExportResult:
    out_dir: Path
    files: list[str]
    map_path: Path
    count: int
    skipped: int
    chunk: int


def export_segments(tl_dir: Path, base_name: str = "phrase", chunk: int = 10000, max_bytes: int = 0, id_format: str = "ligne",
                    scope: str = "dialogue", protect_tags: bool = True, only_untranslated: bool = True) -> ExportResult:
    """Écrit <base>_001.txt, _002.txt… (au plus `chunk` lignes et `max_bytes` octets chacun ; UTF-8 sans BOM, fins de ligne
    Windows) : une ligne « ligneN;texte » (ou « §N§ texte ») par texte, numéros globaux à partir de 1, plus <base>_map.json."""
    chunk = max(100, int(chunk or 10000))
    max_bytes = max(0, int(max_bytes or 0))
    base = re.sub(r"[^\w.-]+", "_", base_name or "phrase").strip("_") or "phrase"
    out_dir = tl_dir / EXPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in list(out_dir.glob("*.txt")) + list(out_dir.glob(f"*{EXPORT_MAP}")):
        with_suppress_unlink(old)
    entries: list[dict] = []
    skipped = 0
    for p in sorted(tl_dir.rglob("*.rpy")):
        rel = p.relative_to(tl_dir).as_posix()
        _lines, segs = parse_tl_file(p, rel)
        for s in segs:
            if only_untranslated and not s.needs_translation:
                continue
            if scope == "dialogue" and not is_story_text(s):
                skipped += 1
                continue
            text, keeps = protect_segment(s.source, protect_tags)
            if is_untranslatable(TAG_MARK_RE.sub("", text)):
                skipped += 1
                continue
            entries.append({"file": rel, "line": s.line, "kind": s.kind, "original": s.source, "text": text, "keeps": keeps})
    width = max(4, len(str(len(entries))))
    files: list[str] = []
    mapping: dict = {"version": 2, "base": base, "language_dir": tl_dir.name, "chunk": chunk, "max_bytes": max_bytes,
                     "id_format": id_format, "protect_tags": protect_tags, "width": width, "files": {}, "entries": {}}
    buf: list[bytes] = []
    size = 0
    first = 1

    def flush(last: int) -> None:
        nonlocal buf, size, first
        if not buf:
            return
        name = f"{base}_{len(files) + 1:03d}.txt"
        (out_dir / name).write_bytes(b"".join(buf))
        files.append(name)
        mapping["files"][name] = {"first": first, "last": last}
        buf, size, first = [], 0, last + 1

    for i, e in enumerate(entries, 1):
        line = (f"§{i:0{width}d}§ {e['text']}" if id_format == "§" else f"ligne{i};{e['text']}") + "\r\n"
        data = line.encode("utf-8")
        if buf and (len(buf) >= chunk or (max_bytes and size + len(data) > max_bytes)):
            flush(i - 1)
        buf.append(data)
        size += len(data)
        mapping["entries"][str(i)] = e
    flush(len(entries))
    map_path = out_dir / f"{base}{EXPORT_MAP}"
    map_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=0), encoding="utf-8")
    return ExportResult(out_dir, files, map_path, len(entries), skipped, chunk)


@dataclass
class ImportReport:
    files: list[str] = field(default_factory=list)
    lines: int = 0
    applied: int = 0
    untranslated: int = 0
    unknown: int = 0
    duplicates: int = 0
    merged: int = 0
    errors: list[str] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    encodings: dict[str, str] = field(default_factory=dict)


def read_text_guess(path: Path) -> tuple[str, str]:
    """UTF-8 (BOM toléré) sinon cp1252 / latin-1 : les fichiers traduits ont parfois changé d'encodage."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8 (caractères remplacés)"


def parse_translated_lines(text: str) -> tuple[dict[str, str], int, int, int]:
    """Lignes « ligneN;texte » (tolère « ligne 1 ; », « Ligne1: », « line1; », « §1§ », « § 1 § »).
    Renvoie (numéro -> texte, lignes sans numéro, doublons, lignes fusionnées)."""
    found: dict[str, str] = {}
    unknown = duplicates = merged = 0
    for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not ln.strip():
            continue
        m = ID_LINE_RE.match(ln)
        if not m:
            unknown += 1
            continue
        key = str(int(m.group(1) or m.group(2)))
        body = m.group(3).rstrip()
        if ID_INSIDE_RE.search(body):
            merged += 1        # deux textes sur une même ligne : on garde la partie avant le second numéro, signalée
            body = ID_INSIDE_RE.split(body)[0].rstrip()
        if key in found:
            duplicates += 1
        found[key] = body
    return found, unknown, duplicates, merged


def _expand_import_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(x for x in p.glob("*.txt")))
        elif p.is_file():
            out.append(p)
    return [p for p in out if not p.name.endswith(EXPORT_MAP)]


def import_files(tl_dir: Path, paths: list[Path]) -> ImportReport:
    """Relit des fichiers traduits (n'importe quel nom, n'importe quel ordre) : lignes reconnues par leur numéro,
    repères / interpolations vérifiés, puis écriture dans les fichiers tl (écrase une traduction précédente)."""
    maps = sorted((tl_dir / EXPORT_DIR).glob(f"*{EXPORT_MAP}")) if (tl_dir / EXPORT_DIR).is_dir() else []
    if not maps:
        raise RuntimeError("Exportez d'abord les textes : table de correspondance (_map.json) introuvable.")
    mapping = json.loads(maps[-1].read_text(encoding="utf-8"))
    entries: dict[str, dict] = mapping["entries"]
    width = int(mapping.get("width", 4))
    report = ImportReport()
    files = _expand_import_paths(paths)
    if not files:
        raise RuntimeError("Aucun fichier .txt à importer (indiquez un ou plusieurs fichiers, ou un dossier).")
    found: dict[str, str] = {}
    for f in files:
        report.files.append(f.name)
        text, enc = read_text_guess(f)
        report.encodings[f.name] = enc
        got, unknown, dup, merged = parse_translated_lines(text)
        report.lines += len(got) + unknown
        report.unknown += unknown
        report.duplicates += dup
        report.merged += merged
        for k, v in got.items():
            if k not in entries:
                report.errors.append(f"{f.name} : numéro {k} inconnu de l'export")
                continue
            found[k] = v
    if found:
        lo, hi = min(int(k) for k in found), max(int(k) for k in found)
        for n in range(lo, hi + 1):
            if str(n) in entries and str(n) not in found:
                report.missing_ids.append(f"ligne{n}" if mapping.get("id_format", "ligne") == "ligne" else f"§{n:0{width}d}§")
    by_file: dict[str, list[tuple[dict, str]]] = {}
    for key, text in found.items():
        e = entries[key]
        if not text.strip() or text.strip() == e["text"].strip():
            report.untranslated += 1
            continue
        restored, why = restore_segment(text, e["keeps"], e["text"])
        if restored is None:
            report.errors.append(f"ligne{key} : {why}")
            continue
        by_file.setdefault(e["file"], []).append((e, restored))
    for rel, items in by_file.items():
        path = tl_dir / rel
        if not path.is_file():
            report.errors.append(f"fichier tl absent : {rel}")
            continue
        lines_, segs = parse_tl_file(path, rel)
        by_line = {s.line: s for s in segs}
        changed = []
        for e, restored in items:
            s = by_line.get(int(e["line"]))
            if s is None or s.source != e["original"]:
                report.errors.append(f"{rel} ligne {e['line']} : le fichier tl a changé depuis l'export")
                continue
            if s.literal != restored:
                s.literal = restored
                s.translated = True
                changed.append(s)
            report.applied += 1
        if changed:
            write_tl_file(path, lines_, changed)
    return report


def pick_files(title: str, filter_: str, initial: str = "") -> list[str]:
    """OpenFileDialog natif à sélection multiple ; liste vide si annulé."""
    t, f, i = (x.replace("'", "''") for x in (title, filter_, str(initial)))
    script = (
        core._DIALOG_PRELUDE + "$d = New-Object System.Windows.Forms.OpenFileDialog; "
        f"$d.Title = '{t}'; $d.Filter = '{f}'; $d.Multiselect = $true; "
        + (f"if (Test-Path -LiteralPath '{i}') {{ $d.InitialDirectory = '{i}' }}; " if i else "")
        + "if ($d.ShowDialog($owner) -eq 'OK') { $d.FileNames -join '|' }; $owner.Close()"
    )
    out = core._run_dialog(script)
    return [p for p in out.split("|") if p.strip()] if out else []


# ---- échantillon pour relecture ---------------------------------------------
def sample_segments(tl_dir: Path, count: int = 30) -> list[dict]:
    """Segments traduits répartis sur tout le jeu : [{file, line, source, translated}]."""
    segs = [s for s in parse_tl_dir(tl_dir) if s.translated and s.kind == "say"] or [s for s in parse_tl_dir(tl_dir) if s.translated]
    if not segs:
        return []
    step = max(1, len(segs) // max(1, count))
    picked = segs[::step][:count]
    return [{"file": s.file, "line": s.line, "source": s.source, "translated": s.literal} for s in picked]


def apply_corrections(tl_dir: Path, rows: list[dict]) -> int:
    """Réécrit les segments corrigés à la main (file, line, translated)."""
    by_file: dict[str, list[dict]] = {}
    for r in rows:
        by_file.setdefault(r["file"], []).append(r)
    n = 0
    for rel, items in by_file.items():
        path = tl_dir / rel
        if not path.is_file():
            continue
        lines, segs = parse_tl_file(path, rel)
        by_line = {s.line: s for s in segs}
        changed = []
        for r in items:
            s = by_line.get(int(r["line"]))
            new = str(r.get("translated") or "")
            if s is None or new == s.literal:
                continue
            s.literal = new
            changed.append(s)
        if changed:
            write_tl_file(path, lines, changed)
            n += len(changed)
    return n


def tl_counts(tl_dir: Path) -> tuple[int, int, int]:
    """(segments, traduits, restants) dans tl/<langue>."""
    segs = parse_tl_dir(tl_dir) if tl_dir.is_dir() else []
    done = sum(1 for s in segs if s.translated)
    return len(segs), done, len(segs) - done


# ============================================================================
# Traduction — C. installation, vérification, désinstallation
# ============================================================================
def install_language_hook(game_root: str, lang: str) -> Path:
    game = core.find_game_dir(game_root)
    target = game / LANG_HOOK
    target.write_text(LANG_HOOK_SRC % {"lang": lang}, encoding="utf-8")
    with_suppress_unlink(target.with_suffix(".rpyc"))
    return target


def check_translation(game_root: str, lang: str, log: Callable[[str], None], cancel: threading.Event) -> dict:
    """Lance le jeu avec un script de vérification (init 1600 : langue, blocs, chaînes traduites, puis sortie)."""
    game = core.find_game_dir(game_root)
    root = game.parent
    rt = find_runtime(root)
    if not ((rt.python and rt.launcher) or rt.exe):
        raise RuntimeError("Aucun moteur Ren'Py trouvé pour lancer le jeu.")
    out_file = root / "renpyhd_check.json"
    with_suppress_unlink(out_file)
    tb = root / "traceback.txt"
    tb_before = tb.stat().st_mtime if tb.is_file() else 0.0
    (game / TL_CHECK).write_text(CHECK_SRC % {"lang": lang}, encoding="utf-8")
    try:
        rc, out = run_renpy(rt, [], log, timeout=600, cancel=cancel)
    finally:
        _remove_helper(game, TL_CHECK)
    result: dict = {"rc": rc, "output": out[-2000:]}
    if out_file.is_file():
        try:
            result.update(json.loads(out_file.read_text(encoding="utf-8")))
        except Exception as exc:
            result["error"] = f"renpyhd_check.json illisible : {exc}"
        with_suppress_unlink(out_file)
    else:
        result["error"] = "Le jeu n'a pas atteint la fin de l'initialisation (pas de renpyhd_check.json)."
    if tb.is_file() and tb.stat().st_mtime > tb_before:
        result["traceback"] = tb.read_text(encoding="utf-8", errors="ignore")[-1500:]
    log_txt = root / "log.txt"
    if log_txt.is_file():
        result["log_tail"] = log_txt.read_text(encoding="utf-8", errors="ignore")[-800:]
    return result


def uninstall_translation(game_root: str, lang: str, remove_tl: bool = True) -> list[str]:
    game = core.find_game_dir(game_root)
    done = []
    for n in (LANG_HOOK, LANG_HOOK + "c", TL_HELPER, TL_HELPER + "c", TL_CHECK, TL_CHECK + "c"):
        f = game / n
        if f.exists():
            f.unlink()
            done.append(f"Fichier supprimé : {f.name}")
    tl_dir = game / "tl" / lang
    manifest = _load_manifest(tl_dir)
    if remove_tl and tl_dir.is_dir():
        if not manifest:
            done.append(f"Dossier `tl/{lang}` conservé : il n'a pas été créé par RenPyHD.")
        elif manifest.get("merged_into_existing"):
            removed = 0
            for rel in manifest.get("files_created", []):
                f = tl_dir / rel
                if f.is_file():
                    f.unlink()
                    removed += 1
                    with_suppress_unlink(f.with_suffix(".rpyc"))
            with_suppress_unlink(tl_dir / TL_MANIFEST)
            shutil.rmtree(tl_dir / "renpyhd_export", ignore_errors=True)
            done.append(f"{removed} fichier(s) créé(s) par RenPyHD supprimé(s) dans `tl/{lang}` (dossier préexistant conservé ; "
                        "les blocs ajoutés aux fichiers existants restent).")
        else:
            shutil.rmtree(tl_dir, ignore_errors=True)
            done.append(f"Dossier supprimé : tl/{lang}")
    return done or ["Rien à désinstaller."]


__all__ = [name for name in dir() if not name.startswith("_")]
