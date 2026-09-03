r"""
renpy_hd_app.py - interface Gradio de RenPyHD (multilingue : fr, en, es, de, ru, pt-BR).

Lancement (normalement via RenPyHD.exe) :
  <DLSS5>\bin\python-3.13.15-embed-amd64\python.exe app\renpy_hd_app.py [--tool <dossier DLSS5>] [--port N] [--no-browser] [--lang xx]

Onglets :
  1. Améliorer un jeu      cinq étapes : choisir le jeu (analyse automatique) → extraire les archives .rpa (si besoin) →
                           que voulez-vous améliorer ? (images / vidéos, préréglage, facteur) → aperçu (10 images + 3 s de vidéo)
                           → « Améliorer le jeu » (progression, état final, Jouer) ; tout le reste dans l'accordéon « Mode expert »
  2. Comparer / Tester     sous-onglets : Comparer avant/après (curseur + loupe 1:1), Tester une image, Tester une vidéo
  3. Outils                sous-onglets : Extraire les archives (.rpa), Traduire le jeu (extraire → exporter → importer/installer),
                           Android (APK) (choisir le jeu → préparer l'environnement → configurer → construire)
  4. Aide

Code de sortie 75 = « redémarrer » (changement de langue) : le lanceur relance le même processus.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import renpy_hd_i18n as i18n  # noqa: E402

RESTART_EXIT_CODE = 75
BUILTIN_VERSION = "1.0.0"


def _read_version() -> str:
    for p in (APP_DIR.parent / "VERSION", APP_DIR / "VERSION"):
        try:
            v = p.read_text(encoding="utf-8").strip()
            if v:
                return v
        except OSError:
            pass
    return BUILTIN_VERSION


APP_VERSION = _read_version()


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", default=str(APP_DIR.parent / "DLSS5"), help="Racine du DLSS 5 Visual Enhancer")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--lang", default="", help="Langue de l'interface : " + ", ".join(i18n.LANGUAGES))
    return ap.parse_args()


ARGS = _parse_args()
TOOL_ROOT = Path(ARGS.tool).resolve()

import renpy_hd_core as core  # noqa: E402
import renpy_hd_tools as tools  # noqa: E402
import renpy_hd_android as android  # noqa: E402


def _choose_language() -> str:
    cfg = core.load_config()
    if ARGS.lang:
        return i18n.set_language(ARGS.lang)
    if cfg.get("ui_lang"):
        return i18n.set_language(str(cfg["ui_lang"]))
    return i18n.set_language(i18n.detect_system_language())


UI_LANG = _choose_language()
t = i18n.t

import gradio as gr  # noqa: E402
from PIL import Image  # noqa: E402

# ----------------------------------------------------------------------------
# État global (application locale mono-utilisateur)
# ----------------------------------------------------------------------------
_STATE: dict[str, object] = {
    "thread": None, "cancel": threading.Event(), "gpu": "", "runtime_error": "",
    "preview_sig": None, "preview_pairs": [], "preview_index": 0, "port": 0,
}
_VIEWER: dict[str, object] = {"pairs": [], "index": 0}

# Libellés d'interface (localisés) -> clés internes. La configuration enregistre les clés (voir LABEL_MAPS).
MODE_LABELS = {t("mode.hd"): core.MODE_HD, t("mode.replace"): core.MODE_REPLACE, t("mode.at2"): core.MODE_AT2, t("mode.folder"): core.MODE_FOLDER}
SCAN_LABELS = {t("scan.auto"): "auto", t("scan.scripts"): "scripts", t("scan.all"): "all"}
KIND_LABELS = {t("kind.hd2x"): "hd2x", t("kind.backup"): "backup", t("kind.at2"): "at2", t("kind.folder"): "folder"}
PREVIEW_MODES = {t("preview.random"): "random", t("preview.first"): "first", t("preview.choose"): "choose"}
PRESET_KEYS = {"faces": "Visages (K : tout au max)", "balanced": "Équilibré (défaut)", "faithful": "Fidèle (discret)",
               "cinema": "Cinéma (contrasté)", "portrait": "Portrait / peau"}
PRESET_LABELS = {t(f"preset.{k}"): k for k in PRESET_KEYS}                       # libellé -> clé courte
FACTOR_CHOICES = [(t(f"factor.{value:g}"), value) for value in core.UPSCALING_FACTORS]
INTERMEDIATE_QUALITIES = ("Auto (Default)", "Max", "Best", "Good")
VIDEO_CODEC_LABELS = {t(f"codec.{key}"): key for key in core.VIDEO_CODECS}
VIDEO_CAP_LABELS = {(t("cap.max") if key.startswith("7680") else key): key for key in core.VIDEO_CAPS}
OVER_CAP_LABELS = {t(f"overcap.{key}"): key for key in core.VIDEO_OVER_CAP}
LABEL_MAPS = {"mode": MODE_LABELS, "preset": PRESET_LABELS, "scan_mode": SCAN_LABELS, "video_codec": VIDEO_CODEC_LABELS,
              "video_cap": VIDEO_CAP_LABELS, "video_over_cap": OVER_CAP_LABELS, "preview_mode": PREVIEW_MODES}
VIDEO_DIALOG_FILTER = t("dialog.video_filter") + "|*.webm;*.mp4;*.ogv;*.mkv;*.avi|" + t("dialog.all_files") + "|*.*"
PREVIEW_KEYS = ("preview_count", "preview_mode", "preview_choice")   # ne participent pas à la signature des réglages
NOT_INVALIDATING = set(PREVIEW_KEYS)
MAX_VIEW = 2560
INPUT_KEYS: list[str] = []
DEFAULT_PREVIEW_COUNT = 10


def _startup_runtime_check() -> None:
    try:
        _STATE["gpu"] = core.check_runtime(TOOL_ROOT)
        print(t("console.runtime_ready", gpu=_STATE["gpu"]), flush=True)
    except Exception as exc:  # la visionneuse reste utilisable sans GPU
        _STATE["runtime_error"] = str(exc)
        print(t("console.runtime_missing", err=exc), file=sys.stderr, flush=True)


def _clean_path(v: object) -> str:
    return str(v or "").strip().strip('"').strip()


def _n(key: str, n: int, **fmt) -> str:
    """Pluriel simple : clé `<key>` (n == 1) ou `<key>_pl`."""
    return t(key if n == 1 else key + "_pl", n=n, **fmt)


# ----------------------------------------------------------------------------
# Construction des réglages à partir des valeurs de l'interface
# ----------------------------------------------------------------------------
def _settings_from(v: dict):
    mode = MODE_LABELS.get(v["mode"], core.MODE_HD)
    dlss = core.DlssSettings(
        factor=core.effective_factor(mode, float(v["factor"])),
        nr_style=v["nr_style"], nr_preset=v["nr_preset"], nr_intensity=float(v["nr_intensity"]),
        local_tone=float(v["local_tone"]), local_structure=float(v["local_structure"]),
        skin_structure=float(v["skin_structure"]), automatic_mask=bool(v["automatic_mask"]),
        warmup_frames=int(v["warmup_frames"]), dlss_model_preset=v["dlss_model_preset"],
        quality=int(v["quality"]), preserve_metadata=bool(v["preserve_metadata"]),
        jpeg_as=v["jpeg_as"], png_as=v["png_as"], webp_as=v["webp_as"],
    )
    scan = core.ScanSettings(
        extensions=tuple(v["extensions"] or core.IMAGE_EXTS),
        exclude_prefixes=tuple(p.strip() for p in str(v["exclude_prefixes"]).replace(";", ",").split(",") if p.strip()),
        include_regex=str(v["include_regex"]).strip(), exclude_regex=str(v["exclude_regex"]).strip(),
        path_filter=str(v["path_filter"]).strip(), use_rpa=bool(v["use_rpa"]),
        min_dim=int(v["min_dim"] or 0), max_dim=int(v["max_dim"] or 0),
        limit=int(v["limit"] or 0), overwrite=bool(v["overwrite"]),
        scan_mode=SCAN_LABELS.get(v["scan_mode"], "auto"), retry_failed=bool(v["retry_failed"]),
    )
    run = core.RunSettings(
        mode=mode, game_root=_clean_path(v["game_root"]), out_name=(str(v["out_name"]).strip().strip("/\\") or "hd2x"),
        input_dir=_clean_path(v["input_dir"]), output_dir=_clean_path(v["output_dir"]),
        install_hook=bool(v["install_hook"]), cache_mb=int(v["cache_mb"]), chunk=int(v["chunk"]), dry_run=bool(v["dry_run"]),
    )
    video_nr = None
    if not bool(v.get("video_share_nr", True)):
        video_nr = core.DlssSettings(
            nr_style=v["video_nr_style"], nr_preset=v["video_nr_preset"], nr_intensity=float(v["video_nr_intensity"]),
            local_tone=float(v["video_local_tone"]), local_structure=float(v["video_local_structure"]),
            skin_structure=float(v["video_skin_structure"]), automatic_mask=bool(v["video_automatic_mask"]),
            dlss_model_preset=v["video_model_preset"],
        )
    cap = core.VIDEO_CAPS.get(VIDEO_CAP_LABELS.get(str(v.get("video_cap")), str(v.get("video_cap"))), (3840, 2160))
    video = core.VideoSettings(
        enabled=bool(v["video_enabled"]) and mode != core.MODE_FOLDER,
        codec=VIDEO_CODEC_LABELS.get(str(v.get("video_codec")), "vp9"),
        crf=int(v["video_crf"]), speed=int(v["video_speed"]),
        keep_audio=bool(v.get("video_keep_audio", True)), audio_kbps=int(v["video_audio"]),
        scene_reset=bool(v.get("video_scene_reset", True)), warmup_frames=int(v.get("video_warmup") or 0),
        hw_encode=bool(v.get("video_hw", True)), max_width=cap[0], max_height=cap[1],
        over_cap=OVER_CAP_LABELS.get(str(v.get("video_over_cap")), "reduce"),
        share_nr=video_nr is None, nr=video_nr, intermediate_quality=v["video_inter_quality"],
    )
    return mode, dlss, scan, run, video


def _build_plan(v: dict):
    mode, dlss, scan, run, video = _settings_from(v)
    if mode == core.MODE_FOLDER:
        plan = core.build_folder_plan(run, scan, dlss)
        info = None
    else:
        info, plan = core.build_game_plan(run, scan, dlss, video)
    if not bool(v.get("images_enabled", True)):          # étape 3 : « Images » décoché -> seulement les vidéos
        plan.jobs = [j for j in plan.jobs if j.is_video]
    return mode, dlss, scan, run, video, info, plan


def _signature(v: dict) -> str:
    payload = {k: val for k, val in v.items() if k not in PREVIEW_KEYS}
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _info_markdown(info: core.GameInfo | None, plan: core.Plan, run: core.RunSettings, factor: float, mode: str) -> str:
    lines = []
    if info is not None:
        yes, no = t("common.yes"), t("common.no")
        lines += [
            t("report.game", root=info.root),
            t("report.version", version=info.renpy_version, resolution=info.resolution),
            t("report.rpa", n=len(info.rpa_files), entries=info.rpa_entries, names=", ".join(info.rpa_files) if info.rpa_files else ""),
            t("report.hook", hook=core.HOOK_NAME, installed=yes if info.hook_installed else no, out=run.out_name,
              hd=(t("report.hd_present", factor=info.hd_factor or "?") if info.hd_dir_exists else t("report.hd_absent")),
              backup=t("report.backup_present") if info.backup_exists else t("report.backup_absent")),
            t("report.refs", n=plan.total_refs, videos=plan.video_refs),
        ]
    else:
        lines += [t("report.folder", src=run.input_dir, dst=run.output_dir), t("report.folder_images", n=plan.total_refs)]
    eff = core.effective_factor(mode, factor)
    if info is not None:
        scan_label = t("report.scan_scripts") if plan.scan_mode_used == "scripts" else t("report.scan_all")
        lines.append(t("report.scan", scan=scan_label, videos=plan.video_refs, size=core.human_size(plan.video_bytes)))
        if plan.video_infos:
            infos = [i for i in plan.video_infos.values() if i.ok]
            dist: dict[str, int] = {}
            for i in infos:
                dist[i.label()] = dist.get(i.label(), 0) + 1
            top = sorted(dist.items(), key=lambda kv: -kv[1])
            summary = ", ".join(f"{n} × {lab}" for lab, n in top[:6]) + (t("report.more_profiles", n=len(top) - 6) if len(top) > 6 else "")
            with_audio = sum(1 for i in infos if i.audio_codec)
            lines.append(t("report.videos_probed", n=len(infos), duration=core.format_eta(plan.video_duration), audio=with_audio, summary=summary))
            if plan.video_plan_lines:
                lines.append(t("report.videos_todo", lines=" ; ".join(plan.video_plan_lines), frames=plan.video_frames_todo,
                               eta=core.format_eta(plan.video_frames_todo / 10.0 * 1.6)))
    for note in plan.notes:
        lines.append(f"- ⚠️ {note}")
    lines += [
        t("report.counts", done=plan.already_done, missing=len(plan.missing), filtered=plan.filtered_out, small=plan.too_small),
        t("report.todo", n=len(plan.jobs), videos=sum(1 for j in plan.jobs if j.is_video), src=core.human_size(plan.source_bytes),
          out=core.human_size(plan.estimated_output_bytes), factor=eff),
    ]
    if plan.video_skipped:
        lines.append(t("report.videos_skipped", n=len(plan.video_skipped)))
    if plan.missing:
        lines.append(t("report.missing_examples", examples=", ".join(f"`{m}`" for m in plan.missing[:5])))
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Onglet 1 : analyse, archives, choix, aperçu, traitement
# ----------------------------------------------------------------------------
def analyze(*args):
    v = dict(zip(INPUT_KEYS, args))
    try:
        mode, dlss, scan, run, video, info, plan = _build_plan(v)
    except Exception as exc:
        return t("err.generic", err=exc), "", gr.update()
    log = [f"{j.rel}  <-  {j.source.describe()}  ->  {j.dest}" for j in plan.jobs[:40]]
    if len(plan.jobs) > 40:
        log.append(t("log.and_more", n=len(plan.jobs) - 40))
    if plan.missing:
        log += ["", t("log.missing")] + [f"  {m}" for m in plan.missing[:50]]
    if plan.video_skipped:
        log += ["", t("log.videos_skipped")] + [f"  {m}" for m in plan.video_skipped]
    choices = [j.rel for j in plan.jobs if not j.is_video][:4000]
    return _info_markdown(info, plan, run, dlss.factor, mode), "\n".join(log), gr.update(choices=choices, value=[])


def _plain_summary(info: core.GameInfo | None, plan: core.Plan, run: core.RunSettings, mode: str) -> str:
    """Résumé de l'analyse en langage courant (étape 1)."""
    if info is None:
        head = t("summary.folder", name=Path(run.input_dir).name or run.input_dir, out=run.output_dir, n=plan.total_refs)
    else:
        vid = t("summary.and_videos", n=plan.video_refs) if plan.video_refs else ""
        head = t("summary.game", name=info.root.name, version=info.renpy_version, resolution=info.resolution, n=plan.total_refs, videos=vid)
    if not plan.jobs and plan.already_done:
        state = t("summary.all_done")
    elif plan.already_done:
        state = t("summary.partly_done", done=plan.already_done, left=len(plan.jobs))
    elif plan.jobs:
        state = t("summary.nothing_done", n=len(plan.jobs))
    else:
        state = t("summary.nothing_to_do")
    extras = []
    if info is not None and info.hook_installed and mode == core.MODE_HD:
        extras.append(t("summary.mod_installed"))
    if plan.too_small:
        extras.append(t("summary.too_small", n=plan.too_small))
    if plan.missing:
        extras.append(t("summary.missing", n=len(plan.missing)))
    line = head + " " + state + (f" ({' ; '.join(extras)})" if extras else "")
    for note in plan.notes:
        line += f"  \n⚠️ {note}"
    return line


def _rpa_state(info: core.GameInfo | None, plan: core.Plan, run: core.RunSettings) -> tuple[str, int, int, int]:
    """(état, archives, fichiers encore uniquement dans les archives, images du plan lues dans un .rpa)."""
    if info is None or not info.rpa_files:
        return "none", 0, 0, 0
    in_rpa = sum(1 for j in plan.jobs if j.source.from_rpa)
    pending, n_arch = 0, len(info.rpa_files)
    try:
        _game, infos = tools.list_rpas(run.game_root)
        pending = sum(i.entries - i.already_loose for i in infos if not i.error)
    except Exception:
        pass
    return ("pending" if (in_rpa or pending) else "loose"), n_arch, pending, in_rpa


def _video_note(plan: core.Plan, video_enabled: bool, mode: str, measured_fps: float = 0.0) -> str:
    if mode == core.MODE_FOLDER:
        return t("choose.videos_na")
    if not plan.video_refs:
        return t("choose.no_videos")
    parts = [t("choose.videos_found", n=plan.video_refs, size=core.human_size(plan.video_bytes))]
    if plan.video_infos and plan.video_duration:
        fps = measured_fps or 10.0
        est = plan.video_frames_todo / fps * 1.6
        parts.append(t("choose.videos_estimate", duration=core.format_eta(plan.video_duration), eta=core.format_eta(est), fps=f"{fps:.0f}"))
    else:
        parts.append(t("choose.videos_long"))
    if plan.video_skipped:
        parts.append(t("choose.videos_skipped", n=len(plan.video_skipped)))
    return " ".join(parts)


def analyze_step(*args):
    """Analyse automatique de l'étape 1 : résumé en clair, état de l'étape 2 (archives), ouverture de l'étape 3.

    Sorties : résumé, texte archives, boutons archives, indice 3, corps 3, note vidéos, indice 4, corps 4, indice 5, corps 5,
    estimation, rapport détaillé, liste des fichiers, choix d'aperçu, bloc « terminé », texte « terminé », bouton « Améliorer ».
    """
    v = dict(zip(INPUT_KEYS, args))
    shown, hidden = gr.update(visible=True), gr.update(visible=False)
    _STATE["preview_sig"] = None
    try:
        mode, dlss, scan, run, video, info, plan = _build_plan(v)
    except Exception as exc:
        return [t("err.analyze", err=exc), "", hidden, shown, hidden, "", shown, hidden, shown, hidden, "",
                t("err.generic", err=exc), "", gr.update(choices=[], value=[]), hidden, "", gr.update(interactive=False)]
    detail, listing, choices = analyze(*args)
    eff = core.effective_factor(mode, dlss.factor)
    state, n_arch, pending, in_rpa = _rpa_state(info, plan, run)
    if state == "none":
        rpa_md, rpa_actions, step3_open = t("rpa.none"), hidden, True
    elif state == "loose":
        rpa_md, rpa_actions, step3_open = t("rpa.loose", n=n_arch), hidden, True
    else:
        rpa_md, rpa_actions, step3_open = t("rpa.pending", n=n_arch, files=pending, images=in_rpa), shown, False
    already = bool(not plan.jobs and plan.already_done)
    done_md = ""
    if already:
        _STATE["preview_sig"] = _signature(v)   # rien à traiter : « Améliorer le jeu » reste possible (ne fera rien)
        done_md = t("done.already", mode=v["mode"])
    return [_plain_summary(info, plan, run, mode), rpa_md, rpa_actions,
            hidden if step3_open else shown, shown if step3_open else hidden, _video_note(plan, video.enabled, mode),
            shown if not already else hidden, hidden, hidden if already and step3_open else shown, shown if already and step3_open else hidden,
            _estimate_md(plan, run, eff, mode), detail, listing, choices, shown if already else hidden, done_md,
            gr.update(interactive=already)]


def clear_flow():
    """Avant une nouvelle analyse : efface les états des étapes 2, 4 et 5 (statut, barre, aperçu)."""
    hidden = gr.update(visible=False)
    _STATE["preview_sig"] = None
    _STATE["preview_pairs"] = []
    return ["", "", "", "", "", "", hidden, gr.update(interactive=False), "", gr.update(visible=False), gr.update(visible=False), ""]


def reset_flow():
    """« Améliorer un autre jeu » : revient à l'étape 1."""
    shown, hidden = gr.update(visible=True), gr.update(visible=False)
    _STATE["preview_sig"] = None
    _STATE["preview_pairs"] = []
    return ["", STEP1_HINT, "", hidden, shown, hidden, shown, hidden, shown, hidden] + clear_flow()


def skip_rpa():
    """« Passer cette étape » : RenPyHD lira directement dans les archives."""
    shown, hidden = gr.update(visible=True), gr.update(visible=False)
    return t("rpa.skipped"), hidden, shown


def open_steps45():
    """« Continuer » (étape 3) : ouvre l'aperçu et l'étape finale."""
    shown, hidden = gr.update(visible=True), gr.update(visible=False)
    return hidden, shown, hidden, shown


STEP1_HINT = t("step1.hint")


def _run_in_thread(plan, run, dlss, video, retry: bool = True):
    """Lance core.run_plan dans un thread ; renvoie (thread, file de log, dict résultats)."""
    cancel = threading.Event()
    _STATE["cancel"] = cancel
    log_q: "queue.Queue[str]" = queue.Queue()
    latest: dict[str, object] = {"progress": None, "summary": None, "error": None}

    def on_progress(p: core.Progress) -> None:
        latest["progress"] = p.copy()

    def worker() -> None:
        try:
            latest["summary"] = core.run_plan(plan, run, dlss, video, TOOL_ROOT, log_q.put, on_progress, cancel, retry_failed=retry)
        except Exception as exc:
            latest["error"] = f"{exc}\n{traceback.format_exc()}"

    thread = threading.Thread(target=worker, name="renpyhd-run", daemon=True)
    _STATE["thread"] = thread
    thread.start()
    return thread, log_q, latest


def _progress_fraction(p: core.Progress | None) -> float:
    if p is None:
        return 0.0
    if p.phase == "video":
        return p.video_frames_live / max(1, p.frames_total_all)
    return p.live_done / max(1, p.total)


def _progress_text(p: core.Progress | None) -> str:
    """Ligne de statut courte : pourcentage, compteur, temps restant, puis une ligne de détail."""
    if p is None:
        return t("progress.preparing")
    pct = 100.0 * _progress_fraction(p)
    if p.phase == "video":
        stage = p.video_stage or "DLSS"
        fps = f"{p.video_fps:.1f} {t('unit.fps')}" if p.video_fps else "…"
        return t("progress.video", pct=f"{pct:.0f}", index=p.video_index, count=p.video_count, eta=core.format_eta(p.eta_seconds),
                 name=p.video_name, stage=stage, frame=p.frame, frames=p.frame_total, fps=fps, live=p.video_frames_live,
                 total=p.frames_total_all, ok=p.done, failed=p.failed, elapsed=core.format_eta(p.elapsed))
    return t("progress.images", pct=f"{pct:.0f}", done=p.live_done, total=p.total, eta=core.format_eta(p.eta_seconds),
             rate=f"{p.rate:.1f}", chunk=p.chunk_index, chunks=p.chunk_count, failed=p.failed, elapsed=core.format_eta(p.elapsed),
             current=p.current)


def _bar_html(fraction: float, label: str = "", done: bool = False) -> str:
    pct = max(0.0, min(100.0, 100.0 * fraction))
    cls = "rhd-bar-fill rhd-bar-done" if done else "rhd-bar-fill"
    return (f'<div class="rhd-bar"><div class="{cls}" style="width:{pct:.1f}%"></div></div>'
            + (f'<div class="rhd-bar-label">{label}</div>' if label else ""))


def _push_bar(gr_progress, p: core.Progress | None) -> None:
    if gr_progress is None:
        return
    try:
        if p is None:
            gr_progress(0, desc=t("progress.preparing_short"))
        elif p.phase == "video":
            gr_progress((p.video_frames_live, max(1, p.frames_total_all)), unit=t("unit.video_frames"),
                        desc=f"{t('unit.video')} {p.video_index}/{p.video_count} : {p.video_stage} {p.frame}/{p.frame_total} — "
                             f"{p.video_fps:.1f} {t('unit.fps')} — {t('unit.eta')} {core.format_eta(p.eta_seconds)}")
        else:
            gr_progress((p.live_done, max(1, p.total)), unit=t("unit.images"),
                        desc=f"{p.current} — {p.rate:.2f} {t('unit.img_s')} — {t('unit.eta')} {core.format_eta(p.eta_seconds)}")
    except Exception:
        pass


def _busy() -> bool:
    t_ = _STATE.get("thread")
    return isinstance(t_, threading.Thread) and t_.is_alive()


def _done_markdown(mode: str, run: core.RunSettings, s: core.RunSummary) -> str:
    """Texte de l'état final (étape 5) : quoi faire maintenant, selon le mode."""
    if s.cancelled:
        return t("done.cancelled", n=s.written)
    if mode == core.MODE_HD:
        txt = t("done.hd", out=run.out_name, hook=core.HOOK_NAME)
        if not run.install_hook:
            txt = t("done.hd_nohook", out=run.out_name, hook=core.HOOK_NAME)
    elif mode == core.MODE_REPLACE:
        txt = t("done.replace")
    elif mode == core.MODE_AT2:
        txt = t("done.at2")
    else:
        txt = t("done.folder", out=s.output_dir)
    if s.failed:
        txt += "  \n" + t("done.failed", n=len(s.failed))
    return txt


def launch(progress=gr.Progress(), *args):
    """Traitement complet (générateur : rafraîchit l'interface toutes les 0,5 s).

    Sorties : statut court, journal complet, tableau des échecs, barre HTML, visibilité du bloc « terminé », texte « terminé ».
    """
    v = dict(zip(INPUT_KEYS, args))
    hidden = gr.update(visible=False)
    if _busy():
        yield t("err.busy"), gr.update(), [], gr.update(), hidden, gr.update()
        return
    try:
        mode, dlss, scan, run, video, info, plan = _build_plan(v)
        dlss.validate(renpy_only=mode != core.MODE_FOLDER)
    except Exception as exc:
        yield t("err.generic", err=exc), traceback.format_exc(limit=1), [], "", hidden, gr.update()
        return

    if run.dry_run:
        listing = "\n".join(f"{j.rel}  ->  {j.dest}" for j in plan.jobs)
        yield t("run.dry_run", n=len(plan.jobs)), listing or t("run.nothing"), [], "", hidden, gr.update()
        return
    if _STATE.get("runtime_error"):
        yield t("err.runtime", err=_STATE["runtime_error"]), "", [], "", hidden, gr.update()
        return

    thread, log_q, latest = _run_in_thread(plan, run, dlss, video, scan.retry_failed)
    lines = [t("run.start", n=len(plan.jobs), mode=v["mode"], gpu=_STATE["gpu"])]
    lines += [t("run.video_skipped", name=m) for m in plan.video_skipped]
    while thread.is_alive():
        while not log_q.empty():
            lines.append(log_q.get())
        p = latest["progress"]
        # pas de gr.Progress ici : une seule barre (HTML) + une ligne de statut, sans superposition
        frac = _progress_fraction(p)  # type: ignore[arg-type]
        yield (_progress_text(p), "\n".join(lines[-400:]), [],  # type: ignore[arg-type]
               _bar_html(frac, f"{100 * frac:.0f} %" if p else t("progress.starting")), hidden, gr.update())
        time.sleep(0.5)
    while not log_q.empty():
        lines.append(log_q.get())
    if latest["error"]:
        lines.append("ERREUR : " + str(latest["error"]))
        yield t("run.failed"), "\n".join(lines[-400:]), [], "", hidden, gr.update()
        return
    s: core.RunSummary = latest["summary"]  # type: ignore[assignment]
    state = t("run.state_cancelled") if s.cancelled else t("run.state_done")
    final = t("run.final", state=state, written=s.written, failed=len(s.failed), elapsed=core.format_eta(s.elapsed), out=s.output_dir)
    for m in s.messages:
        final += f"  \n{m}"
    lines.append(re.sub(r"[*`]", "", final).replace("  \n", "\n"))
    frac = 1.0 if not s.cancelled else _progress_fraction(latest["progress"])  # type: ignore[arg-type]
    yield (final, "\n".join(lines[-400:]), [[rel, err] for rel, err in s.failed],
           _bar_html(frac, t("progress.cancelled") if s.cancelled else t("progress.done"), done=not s.cancelled),
           gr.update(visible=True), _done_markdown(mode, run, s))


def cancel_run():
    if not _busy():
        return t("run.no_run")
    return core.request_cancel(_STATE["cancel"], TOOL_ROOT)  # type: ignore[arg-type]


def uninstall(game_root: str, out_name: str):
    try:
        return "\n".join(core.uninstall_mod(_clean_path(game_root), out_name.strip() or "hd2x"))
    except Exception as exc:
        return t("err.plain", err=exc)


def restore(game_root: str):
    try:
        return "\n".join(core.restore_originals(_clean_path(game_root)))
    except Exception as exc:
        return t("err.plain", err=exc)


def _find_game_exe(root: Path) -> Path | None:
    exes = [p for p in root.glob("*.exe") if not p.name.lower().endswith("-32.exe")]
    if not exes:
        return None
    for p in exes:
        if p.stem.lower() == root.name.lower():
            return p
    return sorted(exes, key=lambda p: len(p.name))[0]


def play_game(game_root: str):
    """« Jouer » : lance l'exécutable du jeu (à côté de game/)."""
    root = Path(_clean_path(game_root))
    if not root.is_dir():
        return t("play.no_folder")
    exe = _find_game_exe(root if not root.name.lower() == "game" else root.parent)
    if exe is None:
        return t("play.no_exe", root=root)
    try:
        import subprocess
        subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True)
        return t("play.started", exe=exe.name)
    except Exception as exc:
        return t("play.failed", err=exc)


def apply_preset(label: str):
    p = core.PRESETS.get(PRESET_KEYS.get(PRESET_LABELS.get(label, ""), label))
    if not p:
        return [gr.update()] * 6
    return [p["nr_style"], p["nr_preset"], p["nr_intensity"], p["local_tone"], p["local_structure"], p["skin_structure"]]


def _mode_help(mode: str) -> str:
    return t({core.MODE_HD: "modehelp.hd", core.MODE_REPLACE: "modehelp.replace", core.MODE_AT2: "modehelp.at2",
              core.MODE_FOLDER: "modehelp.folder"}[mode])


def on_mode_change(mode_label: str):
    mode = MODE_LABELS.get(mode_label, core.MODE_HD)
    is_game = mode != core.MODE_FOLDER
    fixed = mode in (core.MODE_REPLACE, core.MODE_AT2)        # facteur et formats imposés
    fmt_choices = list(core.RENPY_FORMATS if is_game else core.ALL_FORMATS)
    return [
        gr.update(visible=is_game),                                            # game_row
        gr.update(visible=not is_game),                                        # folder_row
        gr.update(interactive=not fixed, value=core.effective_factor(mode, 2.0)),  # factor
        gr.update(choices=fmt_choices, value="JPEG", interactive=not fixed),
        gr.update(choices=fmt_choices, value="PNG", interactive=not fixed),
        gr.update(choices=fmt_choices, value="WebP", interactive=not fixed),
        gr.update(visible=mode == core.MODE_HD),                                # hook_row
        gr.update(visible=mode in (core.MODE_HD, core.MODE_REPLACE)),          # video_group
        gr.update(visible=mode in (core.MODE_HD, core.MODE_AT2)),               # uninstall_btn
        gr.update(visible=mode == core.MODE_REPLACE),                           # restore_btn
        _mode_help(mode),
        gr.update(interactive=mode in (core.MODE_HD, core.MODE_REPLACE)),      # video_enabled (étape 3)
    ]


def _to_keys(v: dict) -> dict:
    out = dict(v)
    for k, mapping in LABEL_MAPS.items():
        if k in out:
            out[k] = mapping.get(out[k], out[k])
    return out


def _to_labels(v: dict) -> dict:
    out = dict(v)
    for k, mapping in LABEL_MAPS.items():
        if k in out:
            inverse = {key: label for label, key in mapping.items()}
            if out[k] in inverse:
                out[k] = inverse[out[k]]
            elif out[k] not in mapping:                       # ancien libellé d'une autre langue : on ignore
                out.pop(k)
    if "preset" in out and out["preset"] in PRESET_KEYS.values():   # anciens fichiers : nom français du préréglage
        for short, name in PRESET_KEYS.items():
            if name == out["preset"]:
                out["preset"] = t(f"preset.{short}")
    return out


def save_cfg(*args):
    v = _to_keys(dict(zip(INPUT_KEYS, args)))
    old = core.load_config()
    for k in ("ui_lang", "last_port", "restart_pending"):
        if k in old:
            v[k] = old[k]
    return t("cfg.saved", path=core.save_config(v))


def load_cfg():
    v = _to_labels(core.load_config())
    if not any(k in v for k in INPUT_KEYS):
        return [gr.update() for _ in INPUT_KEYS] + [t("cfg.none", name=core.CONFIG_FILE.name)]
    return [gr.update(value=v[k]) if k in v else gr.update() for k in INPUT_KEYS] + [t("cfg.loaded", path=core.CONFIG_FILE)]


def browse(title: str):
    def handler(current: str):
        chosen = core.pick_folder(title, _clean_path(current))
        return chosen or current
    return handler


def quit_app():
    def _exit():
        time.sleep(0.8)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()
    return t("app.quitting")


# ---- langue de l'interface ---------------------------------------------------
def set_ui_language(code: str):
    code = i18n.normalize(code) or UI_LANG
    cfg = core.load_config()
    cfg["ui_lang"] = code
    core.save_config(cfg)
    if code == UI_LANG:
        return t("lang.same"), gr.update(visible=False)
    flag, name = i18n.LANGUAGES[code]
    return t("lang.saved", lang=f"{flag} {name}"), gr.update(visible=True)


def restart_app():
    """Sort avec le code 75 : RenPyHD.exe / run.bat relancent le serveur, la page se recharge toute seule."""
    cfg = core.load_config()
    cfg["last_port"] = int(_STATE.get("port") or 0)
    cfg["restart_pending"] = True
    core.save_config(cfg)

    def _exit():
        time.sleep(0.8)
        os._exit(RESTART_EXIT_CODE)
    threading.Thread(target=_exit, daemon=True).start()
    return t("lang.restarting")


RELOAD_JS = """() => { setTimeout(async function poll() {
  try { const r = await fetch(location.pathname + '?_=' + Date.now(), {cache: 'no-store'}); if (r.ok) { location.reload(); return; } } catch (e) {}
  setTimeout(poll, 1000); }, 3500); }"""


# ---- aperçu avant validation ------------------------------------------------
def invalidate_preview():
    had_preview = _STATE.get("preview_sig") is not None
    _STATE["preview_sig"] = None
    warn = t("preview.stale") if had_preview else ""
    return gr.update(interactive=False), warn


def _estimate_md(plan: core.Plan, run: core.RunSettings, eff: float, mode: str, seconds: float | None = None) -> str:
    """Ligne d'estimation en clair : nombre de fichiers, durée (si mesurée), place disque."""
    n = len(plan.jobs)
    if n == 0:
        return t("estimate.nothing")
    where = f"`{run.output_dir}`" if mode == core.MODE_FOLDER else (
        t("estimate.beside") if mode in (core.MODE_AT2, core.MODE_REPLACE) else f"`game/{run.out_name}/`")
    videos = sum(1 for j in plan.jobs if j.is_video)
    files = t("estimate.files", n=n) + (t("estimate.files_videos", n=videos) if videos else "")
    parts = [files, t("estimate.time", eta=core.format_eta(seconds)) if seconds is not None else t("estimate.time_pending"),
             t("estimate.size", size=core.human_size(plan.estimated_output_bytes), where=where, factor=f"{eff:g}")]
    return " · ".join(parts)


def _preview_dir_for(run: core.RunSettings) -> Path:
    base = Path(run.game_root).name if run.mode != core.MODE_FOLDER else Path(run.input_dir).name
    safe = re.sub(r"[^\w.-]+", "_", base or "apercu")
    return core.PREVIEW_ROOT / safe


def _preview_video_job(plan: core.Plan, dlss: core.DlssSettings, video: core.VideoSettings, mode: str, preview_dir: Path):
    """Traite 3 secondes de la première vidéo du plan dans preview_dir/video ; renvoie (avant, après, secondes DLSS, images) ou None."""
    jobs = [j for j in plan.jobs if j.is_video]
    if not jobs or not video.enabled:
        return None
    job = jobs[0]
    out_dir = preview_dir / "video"
    out_dir.mkdir(parents=True, exist_ok=True)
    src = job.source.materialize(out_dir / "rpa") if job.source.from_rpa else job.source.path
    if src is None:
        return None
    vinfo = job.video_info or core.probe_video_file(src)
    if not vinfo.ok:
        return None
    factor = core.effective_factor(mode, dlss.factor)
    vf = job.video_factor or core.choose_video_factor(vinfo.width, vinfo.height, factor, video)
    if vf is None:
        return None
    pvideo = core.VideoSettings(**{**video.__dict__, "enabled": True, "limit_seconds": 3.0})
    dest = out_dir / f"{Path(job.rel).stem}_DLSS{pvideo.container_ext()}"
    pjob = core.Job(job.rel, core.Source(path=src), dest, "VIDEO", pvideo.codec, None, is_video=True, video_info=vinfo, video_factor=vf)
    frames = min(vinfo.frames, int(3.0 * (vinfo.fps or 30)) + 1) if vinfo.frames else 0
    plan_v = core.Plan(jobs=[pjob], video_frames_todo=frames)
    prun = core.RunSettings(mode=core.MODE_FOLDER, output_dir=str(out_dir), chunk=1, install_hook=False)
    pdlss = core.DlssSettings(**{**dlss.__dict__, "factor": factor})
    return pjob, plan_v, prun, pdlss, pvideo, src, dest


def generate_preview(progress=gr.Progress(), *args):
    """Étape 4 : traite N images (10 par défaut) et 3 s d'une vidéo dans app/preview/<jeu>/ (jamais dans le jeu) puis les affiche.

    Sorties : statut, liste d'images, curseur, infos, loupe avant, loupe après, bouton « Améliorer », avertissement, estimation,
    vidéo avant, vidéo après, texte vidéo.
    """
    v = dict(zip(INPUT_KEYS, args))
    novid = gr.update(value=None, visible=False)
    empty = [gr.update(), gr.update(), None, "", None, None, gr.update(interactive=False), "", gr.update(), novid, novid, ""]
    if _busy():
        yield [t("err.busy")] + empty[1:]
        return
    try:
        mode, dlss, scan, run, video, info, plan = _build_plan(v)
        dlss.validate(renpy_only=mode != core.MODE_FOLDER)
    except Exception as exc:
        yield [t("err.generic", err=exc)] + empty[1:]
        return
    eff = core.effective_factor(mode, dlss.factor)
    if not plan.jobs:
        _STATE["preview_sig"] = _signature(v)   # rien à traiter : « Améliorer le jeu » reste possible (ne fera rien)
        yield [t("preview.nothing_all_done")] + empty[1:6] + [gr.update(interactive=True), "", _estimate_md(plan, run, eff, mode), novid, novid, ""]
        return
    if _STATE.get("runtime_error"):
        yield [t("err.runtime", err=_STATE["runtime_error"])] + empty[1:]
        return
    how = PREVIEW_MODES.get(v["preview_mode"], "random")
    preview_dir = _preview_dir_for(run)
    core.clear_preview(preview_dir)
    count = max(1, min(20, int(v["preview_count"] or DEFAULT_PREVIEW_COUNT)))
    sub = core.build_preview_plan(plan, count, how, v["preview_choice"] or [], preview_dir)
    image_jobs = [j for j in plan.jobs if not j.is_video]
    video_jobs = [j for j in plan.jobs if j.is_video]
    lines: list[str] = []
    pairs: list[core.ComparePair] = []
    s: core.RunSummary | None = None
    est_images = 0.0
    if sub.jobs:
        prun = core.RunSettings(mode=core.MODE_FOLDER, output_dir=str(preview_dir), chunk=len(sub.jobs), install_hook=False)
        pdlss = core.DlssSettings(**{**dlss.__dict__, "factor": eff})
        thread, log_q, latest = _run_in_thread(sub, prun, pdlss, core.VideoSettings())
        lines.append(t("preview.log_start", n=len(sub.jobs), dir=preview_dir))
        while thread.is_alive():
            while not log_q.empty():
                lines.append(log_q.get())
            yield [_progress_text(latest["progress"]) + "  \n" + "  \n".join(lines[-3:])] + empty[1:]  # type: ignore[arg-type]
            time.sleep(0.5)
        while not log_q.empty():
            lines.append(log_q.get())
        if latest["error"]:
            yield [t("preview.failed", err=latest["error"])] + empty[1:]
            return
        s = latest["summary"]  # type: ignore[assignment]
        by_rel = {j.rel: j for j in sub.jobs}
        for rel, produced in s.outputs:
            pairs.append(core.ComparePair(rel, by_rel[rel].source, core.Source(path=Path(produced)), "preview"))
        _STATE["preview_times"] = dict(s.timings)
        if not pairs:
            yield [t("preview.no_output") + " " + " ; ".join(f"{r} : {e}" for r, e in s.failed[:5])] + empty[1:]
            return
        per_image = [tm for _r, tm in s.timings]
        mean = sum(per_image) / len(per_image) if per_image else 0.0
        startup = max(0.0, s.elapsed - sum(per_image))
        remaining = len(image_jobs)
        chunks = -(-remaining // max(1, run.chunk))
        est_images = remaining * mean + chunks * startup
    _STATE["preview_pairs"] = pairs
    _STATE["preview_index"] = 0

    # ---- vidéo : 3 secondes de la première vidéo du plan
    video_md, vb, va = "", novid, novid
    measured_fps = 0.0
    est_video = 0.0
    if video_jobs and video.enabled:
        prep = None
        try:
            prep = _preview_video_job(plan, dlss, video, mode, preview_dir)
        except Exception as exc:
            video_md = t("preview.video_failed", err=exc)
        if prep is not None:
            pjob, plan_v, prun_v, pdlss_v, pvideo, src, dest = prep
            thread, log_q, latest = _run_in_thread(plan_v, prun_v, pdlss_v, pvideo)
            lines.append(t("preview.video_log_start", name=pjob.rel))
            while thread.is_alive():
                while not log_q.empty():
                    lines.append(log_q.get())
                yield [_progress_text(latest["progress"]) + "  \n" + "  \n".join(lines[-3:])] + empty[1:]  # type: ignore[arg-type]
                time.sleep(0.5)
            while not log_q.empty():
                lines.append(log_q.get())
            sv: core.RunSummary | None = latest["summary"]  # type: ignore[assignment]
            if latest["error"] or sv is None or not sv.outputs:
                why = latest["error"] or " ; ".join(f"{r} : {e}" for r, e in (sv.failed[:3] if sv else []))
                video_md = t("preview.video_failed", err=str(why)[:300])
            else:
                out = Path(sv.outputs[0][1])
                try:
                    before = core.trim_copy(src, 3.0, preview_dir / "video" / f"{Path(pjob.rel).stem}_avant{src.suffix}")
                except Exception:
                    before = src
                oinfo = core.probe_video_file(out)
                t_dlss = sv.timings[0][1] if sv.timings else 0.0
                measured_fps = oinfo.frames / t_dlss if t_dlss and oinfo.frames else 0.0
                fps_txt = f"{measured_fps:.1f} {t('unit.fps')}" if measured_fps else "?"
                video_md = t("preview.video_ready", name=pjob.rel, src=pjob.video_info.label() if pjob.video_info else "?",
                             w=oinfo.width, h=oinfo.height, factor=f"{pjob.video_factor:g}", fps=fps_txt, total=f"{sv.elapsed:.0f}")
                vb, va = gr.update(value=str(before), visible=True), gr.update(value=str(out), visible=True)
        fps = measured_fps or 10.0
        est_video = plan.video_frames_todo / fps * 1.6
    est = est_images + est_video
    _STATE["preview_sig"] = _signature(v)
    estimate = _estimate_md(plan, run, eff, mode, est)
    if not pairs:
        yield [t("preview.ready_video_only"), gr.update(choices=[], value=None), None, "", None, None, gr.update(interactive=True),
               t("preview.up_to_date"), estimate, vb, va, video_md]
        return
    assert s is not None
    per_image = [tm for _r, tm in s.timings]
    mean = sum(per_image) / len(per_image) if per_image else 0.0
    wall = s.elapsed / max(1, len(pairs))
    timing_md = t("preview.timing", n=len(pairs), elapsed=f"{s.elapsed:.1f}", mean=f"{mean:.2f}", wall=f"{wall:.2f}",
                  remaining=len(image_jobs), eta=core.format_eta(est), best=core.format_eta(len(image_jobs) * mean),
                  worst=core.format_eta(len(image_jobs) * wall))
    timing_md += "  \n" + "  \n".join(f"- `{r}` : {tm:.2f} s" for r, tm in s.timings[:12])
    if s.failed:
        timing_md += "  \n" + t("preview.failures") + " " + " ; ".join(f"`{r}` : {e}" for r, e in s.failed[:5])
    choices = [p.rel for p in pairs]
    slider, info_md, crop_b, crop_a = _show_pair(pairs[0], 50, 50, 200, _preview_caption(pairs[0].rel) + "  \n" + timing_md)
    yield [t("preview.ready", n=len(pairs)), gr.update(choices=choices, value=choices[0]), slider, info_md, crop_b, crop_a,
           gr.update(interactive=True), t("preview.up_to_date"), estimate, vb, va, video_md]


def _preview_pair(rel: str | None):
    pairs: list[core.ComparePair] = _STATE["preview_pairs"]  # type: ignore[assignment]
    for i, p in enumerate(pairs):
        if p.rel == rel:
            _STATE["preview_index"] = i
            return p
    return None


def _preview_caption(rel: str) -> str:
    times: dict = _STATE.get("preview_times") or {}  # type: ignore[assignment]
    pairs: list = _STATE["preview_pairs"]  # type: ignore[assignment]
    tm = times.get(rel)
    return (t("preview.render_time", s=f"{tm:.2f}") if tm is not None else "") + " — " + t("preview.index", i=_STATE["preview_index"] + 1, n=len(pairs))


def preview_show(rel: str | None, x: float, y: float, crop: int):
    pair = _preview_pair(rel)
    if pair is None:
        return None, "", None, None
    return _show_pair(pair, x, y, crop, _preview_caption(pair.rel))


def preview_step(rel: str | None, delta: int):
    pairs: list[core.ComparePair] = _STATE["preview_pairs"]  # type: ignore[assignment]
    if not pairs:
        return gr.update()
    idx = _STATE["preview_index"]
    for i, p in enumerate(pairs):
        if p.rel == rel:
            idx = i
            break
    idx = (idx + delta) % len(pairs)  # type: ignore[operator]
    _STATE["preview_index"] = idx
    return gr.update(value=pairs[idx].rel)


def preview_crop(rel: str | None, x: float, y: float, crop: int):
    pair = _preview_pair(rel)
    if pair is None:
        return None, None
    try:
        return _crops(core.open_image(pair.before).convert("RGB"), core.open_image(pair.after).convert("RGB"), x, y, crop)
    except Exception:
        return None, None


def validate_and_launch(progress=gr.Progress(), *args):
    v = dict(zip(INPUT_KEYS, args))
    if _STATE.get("preview_sig") != _signature(v):
        yield t("preview.changed_since"), gr.update(), [], gr.update(), gr.update(visible=False), gr.update()
        return
    yield from launch(progress, *args)


def update_video_note(*args):
    """Étape 3 : la case « Vidéos » change -> note (nombre, taille, durée estimée) et estimation."""
    v = dict(zip(INPUT_KEYS, args))
    try:
        mode, dlss, scan, run, video, info, plan = _build_plan(v)
    except Exception:
        return gr.update(), gr.update()
    return _video_note(plan, video.enabled, mode), _estimate_md(plan, run, core.effective_factor(mode, dlss.factor), mode)


# ----------------------------------------------------------------------------
# Onglet « Tester une image » : une seule image, réglages courants, sortie dans app/preview/single/
# ----------------------------------------------------------------------------
SINGLE_DIR = core.PREVIEW_ROOT / "single"


def single_browse(current: str):
    chosen = core.pick_file(t("dialog.pick_image"), initial=str(Path(_clean_path(current)).parent) if _clean_path(current) else "")
    return chosen or current


def single_from_upload(uploaded):
    return uploaded or gr.update()


def single_from_paste():
    """Bouton « Coller » : lit le presse-papiers Windows côté application (image, fichier image copié ou chemin)."""
    path, why = core.clipboard_to_file(SINGLE_DIR)
    if path:
        return path, ""
    return gr.update(), t("single.paste_none") if why in ("empty", "notimage") else t("single.paste_err", err=why)


def test_single(progress=gr.Progress(), *args):
    path_value, args = args[0], args[1:]
    v = dict(zip(INPUT_KEYS, args))
    path = Path(_clean_path(path_value))
    empty = [gr.update(), None, "", None, None, gr.update(interactive=False)]
    if not path.is_file():
        yield [t("single.pick_existing_image")] + empty[1:]
        return
    if _busy():
        yield [t("err.busy")] + empty[1:]
        return
    if _STATE.get("runtime_error"):
        yield [t("err.runtime", err=_STATE["runtime_error"])] + empty[1:]
        return
    try:
        mode, dlss, scan, run, video = _settings_from(v)
        pdlss = core.DlssSettings(**{**dlss.__dict__, "factor": core.effective_factor(mode, dlss.factor)})
        pdlss.validate(renpy_only=False)
    except Exception as exc:
        yield [t("err.generic", err=exc)] + empty[1:]
        return
    ext = path.suffix.lower()
    src_fmt = core.FORMAT_BY_EXT.get(ext, "PNG")
    out_fmt = pdlss.output_format_for(src_fmt) if ext in core.FORMAT_BY_EXT else "PNG"
    core.clear_preview(SINGLE_DIR)
    SINGLE_DIR.mkdir(parents=True, exist_ok=True)
    dest = SINGLE_DIR / f"{path.stem}_DLSS{core.EXT_BY_FORMAT[out_fmt]}"
    plan = core.Plan(jobs=[core.Job(path.name, core.Source(path=path), dest, src_fmt, out_fmt)])
    prun = core.RunSettings(mode=core.MODE_FOLDER, output_dir=str(SINGLE_DIR), chunk=1, install_hook=False)
    thread, log_q, latest = _run_in_thread(plan, prun, pdlss, core.VideoSettings())
    while thread.is_alive():
        _push_bar(progress, latest["progress"])  # type: ignore[arg-type]
        yield [_progress_text(latest["progress"])] + empty[1:]  # type: ignore[arg-type]
        time.sleep(0.5)
    if latest["error"]:
        yield [t("single.failed", err=latest["error"])] + empty[1:]
        return
    s: core.RunSummary = latest["summary"]  # type: ignore[assignment]
    if not s.outputs:
        yield [t("single.failed", err=" ; ".join(f"{r} : {e}" for r, e in s.failed[:3]))] + empty[1:]
        return
    pair = core.ComparePair(path.name, core.Source(path=path), core.Source(path=Path(s.outputs[0][1])), "single")
    _STATE["single_pair"] = pair
    tm = s.timings[0][1] if s.timings else s.elapsed
    slider, info_md, crop_b, crop_a = _show_pair(pair, 50, 50, 200, t("single.caption", s=f"{tm:.2f}", total=f"{s.elapsed:.1f}",
                                                                      factor=pdlss.factor, fmt=out_fmt, quality=pdlss.quality))
    yield [t("single.ready", out=pair.after.describe()), slider, info_md, crop_b, crop_a, gr.update(interactive=True)]


def single_crop(x: float, y: float, crop: int):
    pair = _STATE.get("single_pair")
    if not isinstance(pair, core.ComparePair):
        return None, None
    try:
        return _crops(core.open_image(pair.before).convert("RGB"), core.open_image(pair.after).convert("RGB"), x, y, crop)
    except Exception:
        return None, None


def single_save():
    pair = _STATE.get("single_pair")
    if not isinstance(pair, core.ComparePair) or pair.after.path is None or not pair.after.path.is_file():
        return t("save.nothing")
    target = core.save_file_dialog(t("dialog.save_result"), pair.after.path.name)
    if not target:
        return t("save.cancelled")
    try:
        import shutil
        shutil.copy2(pair.after.path, target)
        return t("save.done", path=target)
    except Exception as exc:
        return t("save.error", err=exc)


# ----------------------------------------------------------------------------
# Onglet « Tester une vidéo » : une seule vidéo (tronquée à N secondes), sortie dans app/preview/single_video/
# ----------------------------------------------------------------------------
SINGLE_VIDEO_DIR = core.PREVIEW_ROOT / "single_video"


def video_browse(current: str):
    initial = str(Path(_clean_path(current)).parent) if _clean_path(current) else ""
    chosen = core.pick_file(t("dialog.pick_video"), VIDEO_DIALOG_FILTER, initial=initial)
    return chosen or current


def _frame_pairs(before: Path, after: Path, out_dir: Path, tag: str, count: int = 3) -> list[core.ComparePair]:
    """Extrait `count` images aux mêmes instants des deux vidéos (PNG) pour le curseur avant/après et la loupe."""
    dur = core.video_duration(after) or core.video_duration(before)
    pairs = []
    for i in range(count):
        tm = dur * (0.15 + 0.7 * i / max(1, count - 1)) if dur else 0.0
        b = core.extract_frame(before, tm, out_dir / f"{tag}_avant_{i}.png")
        a = core.extract_frame(after, tm, out_dir / f"{tag}_apres_{i}.png")
        if b and a:
            pairs.append(core.ComparePair(t("video.frame_label", i=i + 1, t=f"{tm:.2f}"), core.Source(path=b), core.Source(path=a), "video"))
    return pairs


def test_video(progress=gr.Progress(), *args):
    path_value, limit, args = args[0], args[1], args[2:]
    v = dict(zip(INPUT_KEYS, args))
    path = Path(_clean_path(path_value))
    # sorties : statut, curseur, infos, loupe avant, loupe après, vidéo avant, vidéo après, choix d'image
    empty = [gr.update(), None, "", None, None, None, None, gr.update(choices=[], value=None)]
    if not path.is_file():
        yield [t("video.pick_existing")] + empty[1:]
        return
    if _busy():
        yield [t("err.busy")] + empty[1:]
        return
    if _STATE.get("runtime_error"):
        yield [t("err.runtime", err=_STATE["runtime_error"])] + empty[1:]
        return
    try:
        mode, dlss, scan, run, video = _settings_from(v)
        factor = core.effective_factor(mode, dlss.factor)
        limit_s = float(limit or 0)
        video = core.VideoSettings(**{**video.__dict__, "enabled": True, "limit_seconds": limit_s})
        video.validate()
        pdlss = core.DlssSettings(**{**dlss.__dict__, "factor": factor})
        pdlss.validate(renpy_only=False)
        vinfo = core.probe_video_file(path)
        if not vinfo.ok:
            raise ValueError(t("video.unreadable", err=vinfo.error or t("video.no_stream")))
        vf = core.choose_video_factor(vinfo.width, vinfo.height, factor, video)
        if vf is None:
            raise ValueError(t("video.over_cap", w=vinfo.width, h=vinfo.height, mw=video.max_width, mh=video.max_height))
    except Exception as exc:
        yield [t("err.generic", err=exc)] + empty[1:]
        return
    core.clear_preview(SINGLE_VIDEO_DIR)
    SINGLE_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    dest = SINGLE_VIDEO_DIR / f"{path.stem}_DLSS{video.container_ext()}"
    job = core.Job(path.name, core.Source(path=path), dest, "VIDEO", video.codec, None, is_video=True, video_info=vinfo, video_factor=vf)
    plan = core.Plan(jobs=[job], video_frames_todo=vinfo.frames)
    prun = core.RunSettings(mode=core.MODE_FOLDER, output_dir=str(SINGLE_VIDEO_DIR), chunk=1, install_hook=False)
    thread, log_q, latest = _run_in_thread(plan, prun, pdlss, video)
    lines: list[str] = []
    while thread.is_alive():
        while not log_q.empty():
            lines.append(log_q.get())
        _push_bar(progress, latest["progress"])  # type: ignore[arg-type]
        yield [_progress_text(latest["progress"]) + "  \n" + "  \n".join(lines[-3:])] + empty[1:]  # type: ignore[arg-type]
        time.sleep(0.5)
    while not log_q.empty():
        lines.append(log_q.get())
    if latest["error"]:
        yield [t("single.failed", err=latest["error"])] + empty[1:]
        return
    s: core.RunSummary = latest["summary"]  # type: ignore[assignment]
    if not s.outputs:
        yield [t("single.failed", err=" ; ".join(f"{r} : {e}" for r, e in s.failed[:3])) + "  \n" + "  \n".join(lines[-4:])] + empty[1:]
        return
    out = Path(s.outputs[0][1])
    before_path = path
    if limit_s > 0:
        try:
            before_path = core.trim_copy(path, limit_s, SINGLE_VIDEO_DIR / f"{path.stem}_avant{path.suffix}")
        except Exception:
            before_path = path
    oinfo = core.probe_video_file(out)
    pairs = _frame_pairs(before_path, out, SINGLE_VIDEO_DIR, "test")
    _STATE["video_pairs"] = pairs
    t_dlss = s.timings[0][1] if s.timings else 0.0
    fps_txt = f"{oinfo.frames / t_dlss:.1f} {t('unit.fps')}" if t_dlss and oinfo.frames else "?"
    caption = t("video.caption", src=vinfo.label(), w=oinfo.width, h=oinfo.height, factor=f"{vf:g}", frames=oinfo.frames, fps=fps_txt,
                dlss=f"{t_dlss:.0f}", total=f"{s.elapsed:.0f}", size=core.human_size(out.stat().st_size),
                codec=t(f"codec.{video.codec}").split(" (")[0], crf=video.crf)
    status = t("single.ready", out=out) + "  \n" + "  \n".join(lines[-3:])
    if not pairs:
        yield [status + "  \n" + t("video.no_frames"), None, caption, None, None, str(before_path), str(out), gr.update(choices=[], value=None)]
        return
    slider, info_md, crop_b, crop_a = _show_pair(pairs[0], 50, 50, 200, caption)
    yield [status, slider, info_md, crop_b, crop_a, str(before_path), str(out),
           gr.update(choices=[p.rel for p in pairs], value=pairs[0].rel)]


def _video_pair(rel: str | None):
    for p in _STATE.get("video_pairs") or []:  # type: ignore[union-attr]
        if p.rel == rel:
            return p
    return None


def video_show(rel: str | None, x: float, y: float, crop: int):
    pair = _video_pair(rel)
    if pair is None:
        return None, "", None, None
    return _show_pair(pair, x, y, crop, pair.rel)


def video_crop(rel: str | None, x: float, y: float, crop: int):
    pair = _video_pair(rel)
    if pair is None:
        return None, None
    try:
        return _crops(core.open_image(pair.before).convert("RGB"), core.open_image(pair.after).convert("RGB"), x, y, crop)
    except Exception:
        return None, None


def video_save():
    pairs = _STATE.get("video_pairs") or []
    out = None
    for f in sorted(SINGLE_VIDEO_DIR.glob("*_DLSS.*")) if SINGLE_VIDEO_DIR.is_dir() else []:
        out = f
    if out is None or not pairs:
        return t("save.nothing")
    target = core.save_file_dialog(t("dialog.save_video"), out.name, VIDEO_DIALOG_FILTER)
    if not target:
        return t("save.cancelled")
    try:
        import shutil
        shutil.copy2(out, target)
        return t("save.video_done", path=target)
    except Exception as exc:
        return t("save.error", err=exc)


# ----------------------------------------------------------------------------
# Onglet 2 : visionneuse
# ----------------------------------------------------------------------------
COMPARE_DIR = core.PREVIEW_ROOT / "compare"


def viewer_load(game_root: str, kind_label: str, out_name: str, input_dir: str, output_dir: str, fallback_root: str):
    kind = KIND_LABELS.get(kind_label, "hd2x")
    root = _clean_path(game_root) or _clean_path(fallback_root)
    try:
        pairs = core.list_pairs(root, kind, out_name.strip() or "hd2x", _clean_path(input_dir), _clean_path(output_dir))
    except Exception as exc:
        _VIEWER["pairs"] = []
        return gr.update(choices=[], value=None), t("err.generic", err=exc), root
    _VIEWER["pairs"] = pairs
    _VIEWER["index"] = 0
    choices = [p.rel for p in pairs]
    return gr.update(choices=choices, value=choices[0] if choices else None), t("viewer.count", n=len(pairs)), root


def _current_pair(rel: str | None):
    pairs: list[core.ComparePair] = _VIEWER["pairs"]  # type: ignore[assignment]
    for i, p in enumerate(pairs):
        if p.rel == rel:
            _VIEWER["index"] = i
            return p
    return None


def _fit(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    return im if im.size == size else im.resize(size, Image.Resampling.LANCZOS)


def _crops(before: Image.Image, after: Image.Image, x: float, y: float, crop: int):
    bw, bh = before.size
    aw, ah = after.size
    s = max(16, min(int(crop), bw, bh))
    cx = int((bw - s) * float(x) / 100.0)
    cy = int((bh - s) * float(y) / 100.0)
    rx, ry = aw / bw, ah / bh
    box_a = (int(cx * rx), int(cy * ry), int((cx + s) * rx), int((cy + s) * ry))
    out = (s * 2, s * 2)
    crop_b = before.crop((cx, cy, cx + s, cy + s)).resize(out, Image.Resampling.NEAREST)
    crop_a = after.crop(box_a).resize(out, Image.Resampling.LANCZOS if box_a[2] - box_a[0] > out[0] else Image.Resampling.NEAREST)
    return crop_b, crop_a


def _show_pair(pair: core.ComparePair, x: float, y: float, crop: int, index_text: str = ""):
    try:
        before = core.open_image(pair.before).convert("RGB")
        after = core.open_image(pair.after).convert("RGB")
    except Exception as exc:
        return None, t("viewer.read_error", err=exc), None, None
    bw, bh = before.size
    aw, ah = after.size
    scale = min(1.0, MAX_VIEW / max(aw, ah))
    view = (max(1, int(aw * scale)), max(1, int(ah * scale)))
    slider = (_fit(before, view), _fit(after, view))
    info = t("viewer.pair_info", rel=pair.rel, bw=bw, bh=bh, bsize=core.human_size(pair.before.size), bsrc=pair.before.describe(),
             aw=aw, ah=ah, asize=core.human_size(pair.after.size), asrc=pair.after.describe(), ratio=f"{aw / bw if bw else 1:.3f}")
    info += f"  \n{index_text}" if index_text else ""
    crop_b, crop_a = _crops(before, after, x, y, crop)
    return slider, info, crop_b, crop_a


def _video_pair_frames(pair: core.ComparePair, pct: float) -> tuple[Path | None, Path | None, float, Path]:
    """Images des deux vidéos à pct % de la durée (PNG mis en cache dans app/preview/compare/)."""
    COMPARE_DIR.mkdir(parents=True, exist_ok=True)
    before = pair.before.materialize(COMPARE_DIR / "rpa") if pair.before.from_rpa else pair.before.path
    after = pair.after.path
    assert before is not None and after is not None
    dur = core.video_duration(after) or core.video_duration(before)
    tm = dur * max(0.0, min(100.0, float(pct))) / 100.0
    key = hashlib.sha1(f"{after}|{tm:.2f}".encode("utf-8")).hexdigest()[:12]
    fb = core.extract_frame(before, tm, COMPARE_DIR / f"{key}_avant.png")
    fa = core.extract_frame(after, tm, COMPARE_DIR / f"{key}_apres.png")
    return fb, fa, tm, before


def viewer_show(rel: str | None, x: float, y: float, crop: int, pct: float = 50):
    hidden = gr.update(value=None, visible=False)
    pair = _current_pair(rel)
    if pair is None:
        return None, t("viewer.none_selected"), None, None, hidden, hidden
    index_text = t("viewer.file_index", i=_VIEWER["index"] + 1, n=len(_VIEWER["pairs"]))  # type: ignore[arg-type]
    if pair.is_video:
        try:
            fb, fa, tm, before = _video_pair_frames(pair, pct)
        except Exception as exc:
            return None, t("err.generic", err=exc), None, None, hidden, hidden
        if not fb or not fa:
            return None, t("viewer.frame_error"), None, None, hidden, hidden
        _VIEWER["video_frames"] = (fb, fa)
        fpair = core.ComparePair(pair.rel, core.Source(path=fb), core.Source(path=fa), "video")
        slider, info, cb, ca = _show_pair(fpair, x, y, crop, index_text + " — " + t("viewer.frame_at", t=f"{tm:.2f}"))
        vb, va = core.probe_video_file(before), core.probe_video_file(pair.after.path)  # type: ignore[arg-type]
        info = t("viewer.video_info", rel=pair.rel, before=vb.label(), bsize=core.human_size(pair.before.size), after=va.label(),
                 asize=core.human_size(pair.after.size)) + "  \n" + info
        return slider, info, cb, ca, gr.update(value=str(before), visible=True), gr.update(value=str(pair.after.path), visible=True)
    _VIEWER["video_frames"] = None
    slider, info, cb, ca = _show_pair(pair, x, y, crop, index_text)
    return slider, info, cb, ca, hidden, hidden


def viewer_crop(rel: str | None, x: float, y: float, crop: int):
    pair = _current_pair(rel)
    if pair is None:
        return None, None
    try:
        frames = _VIEWER.get("video_frames")
        if pair.is_video and frames:
            fb, fa = frames  # type: ignore[misc]
            return _crops(Image.open(fb).convert("RGB"), Image.open(fa).convert("RGB"), x, y, crop)
        return _crops(core.open_image(pair.before).convert("RGB"), core.open_image(pair.after).convert("RGB"), x, y, crop)
    except Exception:
        return None, None


def viewer_step(rel: str | None, delta: int):
    pairs: list[core.ComparePair] = _VIEWER["pairs"]  # type: ignore[assignment]
    if not pairs:
        return gr.update()
    idx = _VIEWER["index"]
    for i, p in enumerate(pairs):
        if p.rel == rel:
            idx = i
            break
    idx = (idx + delta) % len(pairs)  # type: ignore[operator]
    _VIEWER["index"] = idx
    return gr.update(value=pairs[idx].rel)


# ----------------------------------------------------------------------------
# Onglet « Outils » : extraction des archives .rpa, traduction du jeu
# ----------------------------------------------------------------------------
_TOOLS: dict[str, object] = {"thread": None, "cancel": threading.Event(), "rpa_names": {}, "samples": []}
RPA_ENGINES = {t("rpa.engine_python"): "python", t("rpa.engine_exe"): "rpaextract"}
TARGET_CHOICES = list(tools.LANG_LABELS)
DEFAULT_TARGET = tools.lang_label(UI_LANG.split("-")[0] if UI_LANG.split("-")[0] in tools.LANG_BY_CODE else "fr")
DEFAULT_SOURCE = tools.lang_label("en")
STEP_X1_HINT = t("tools.rpa.step1_hint")
STEP_T1_HINT = t("tools.tl.step1_hint")


def _tools_busy() -> bool:
    t_ = _TOOLS.get("thread")
    return isinstance(t_, threading.Thread) and t_.is_alive()


def _tools_thread(target) -> tuple[threading.Thread, "queue.Queue[str]", dict]:
    cancel = threading.Event()
    _TOOLS["cancel"] = cancel
    log_q: "queue.Queue[str]" = queue.Queue()
    latest: dict[str, object] = {"progress": None, "result": None, "error": None}

    def worker() -> None:
        try:
            latest["result"] = target(log_q.put, lambda p: latest.__setitem__("progress", p), cancel)
        except Exception as exc:
            latest["error"] = f"{exc}\n{traceback.format_exc()}"

    thread = threading.Thread(target=worker, name="renpyhd-tools", daemon=True)
    _TOOLS["thread"] = thread
    thread.start()
    return thread, log_q, latest


def _drain(log_q, lines: list[str]) -> None:
    while not log_q.empty():
        lines.append(log_q.get())


def tools_cancel():
    if not _tools_busy():
        return t("tools.no_op")
    _TOOLS["cancel"].set()  # type: ignore[union-attr]
    return t("tools.cancel_requested")


# ---- extraction --------------------------------------------------------------
def rpa_scan(game_root: str):
    """Étape 1 : archives de game/. Sorties : résumé, liste (cases), corps étape 2, corps étape 3, statut, barre."""
    root = _clean_path(game_root)
    shown, hidden = gr.update(visible=True), gr.update(visible=False)
    if not root:
        return STEP_X1_HINT, gr.update(choices=[], value=[]), hidden, hidden, "", ""
    try:
        _game, infos = tools.list_rpas(root)
    except Exception as exc:
        return t("err.analyze_short", err=exc), gr.update(choices=[], value=[]), hidden, hidden, "", ""
    if not infos:
        return t("tools.rpa.none"), gr.update(choices=[], value=[]), hidden, hidden, "", ""
    labels = {i.label(): i.name for i in infos}
    _TOOLS["rpa_names"] = labels
    ok = [i for i in infos if not i.error]
    todo = sum(i.entries - i.already_loose for i in ok)
    imgs = sum(i.images - i.images_loose for i in ok)
    total = sum(i.bytes_total for i in ok)
    head = t("tools.rpa.summary", n=len(infos), files=sum(i.entries for i in ok), size=core.human_size(total), todo=todo, images=imgs)
    if todo == 0:
        head += " " + t("tools.rpa.all_extracted")
    default = [lab for lab, name in labels.items() if any(i.name == name and not i.error and i.entries - i.already_loose > 0 for i in infos)]
    return head, gr.update(choices=list(labels), value=default), shown, shown, "", ""


def _extract_run(root: str, names: list[str], engine: str, engine_label: str, skip_existing: bool, rename_bak: bool):
    """Extraction (générateur commun à l'étape 2 du flux principal et à l'onglet Outils). Sorties : statut, barre, journal, bloc « terminé »."""
    hidden = gr.update(visible=False)
    thread, log_q, latest = _tools_thread(
        lambda log, prog, cancel: tools.extract_rpas(root, names, engine, bool(skip_existing), bool(rename_bak), log, prog, cancel))
    lines = [t("tools.rpa.log_start", n=len(names), engine=engine_label)]
    while thread.is_alive():
        _drain(log_q, lines)
        p: tools.ExtractProgress | None = latest["progress"]  # type: ignore[assignment]
        if p is None:
            txt, frac = t("tools.rpa.reading_index"), 0.0
        elif engine == "python":
            frac = p.fraction
            txt = t("tools.rpa.progress", pct=f"{100 * frac:.0f}", done=p.done + p.skipped, total=p.total, eta=core.format_eta(p.eta),
                    written=core.human_size(p.bytes_done), skipped=p.skipped, archive=p.archive, current=p.current)
        else:
            frac = p.fraction
            txt = t("tools.rpa.progress_exe", i=p.done + 1, n=p.total, archive=p.archive)
        yield txt, _bar_html(frac, f"{100 * frac:.0f} %"), "\n".join(lines[-300:]), hidden
        time.sleep(0.4)
    _drain(log_q, lines)
    if latest["error"]:
        lines.append("ERREUR : " + str(latest["error"]))
        yield t("tools.rpa.failed"), "", "\n".join(lines[-300:]), hidden
        return
    s: tools.ExtractSummary = latest["result"]  # type: ignore[assignment]
    lines += [t("tools.rpa.log_done", written=s.written, size=core.human_size(s.bytes_written), skipped=s.skipped, errors=len(s.errors),
                elapsed=core.format_eta(s.elapsed))] + [f"  {a} : {e}" for a, e in s.errors] + s.messages
    state = t("run.state_cancelled") if s.cancelled else t("run.state_done")
    final = t("tools.rpa.final", state=state, written=s.written, size=core.human_size(s.bytes_written), skipped=s.skipped,
              errors=len(s.errors), elapsed=core.format_eta(s.elapsed))
    if s.renamed:
        final += "  \n" + t("tools.rpa.renamed", names=", ".join(s.renamed))
    if s.messages:
        final += "  \n" + "  \n".join(s.messages)
    if s.cancelled:
        final += "  \n" + t("tools.rpa.resume_hint")
    else:
        final += "  \n" + t("tools.rpa.works_same")
    yield final, _bar_html(1.0 if not s.cancelled else 0.0, t("progress.cancelled") if s.cancelled else t("progress.done"), done=not s.cancelled), \
        "\n".join(lines[-300:]), gr.update(visible=True)


def rpa_extract(game_root: str, chosen: list[str], engine_label: str, skip_existing: bool, rename_bak: bool):
    """Onglet Outils, étape 3 : extraction des archives cochées (générateur)."""
    hidden = gr.update(visible=False)
    if _tools_busy():
        yield t("tools.busy"), gr.update(), gr.update(), hidden
        return
    names = [_TOOLS["rpa_names"].get(c, c) for c in (chosen or [])]  # type: ignore[union-attr]
    if not names:
        yield t("tools.rpa.pick_one"), "", "", hidden
        return
    engine = RPA_ENGINES.get(engine_label, "python")
    yield from _extract_run(_clean_path(game_root), names, engine, engine_label, skip_existing, rename_bak)


def flow_extract(game_root: str):
    """Étape 2 du flux principal : extrait toutes les archives (moteur Python, fichiers existants conservés). Sorties : statut, barre, journal."""
    root = _clean_path(game_root)
    if _tools_busy():
        yield t("tools.busy"), gr.update(), gr.update()
        return
    try:
        _game, infos = tools.list_rpas(root)
    except Exception as exc:
        yield t("err.analyze_short", err=exc), "", ""
        return
    names = [i.name for i in infos if not i.error and i.entries - i.already_loose > 0]
    if not names:
        yield t("rpa.nothing_pending"), "", ""
        return
    for status, bar, log, _done in _extract_run(root, names, "python", t("rpa.engine_python"), True, False):
        yield status, bar, log


# ---- traduction --------------------------------------------------------------
def _codes(target_label: str, source_label: str) -> tuple[str, str, str]:
    """(code source, code cible, nom Ren'Py de la langue cible)."""
    dst = tools.LANG_LABELS.get(target_label, "fr")
    src = tools.LANG_LABELS.get(source_label, "en")
    return src, dst, tools.renpy_lang_name(dst)


def _tl_dir(game_root: str, target_label: str) -> Path:
    game = core.find_game_dir(_clean_path(game_root))
    return game / "tl" / _codes(target_label, DEFAULT_SOURCE)[2]


def tl_status_md(game_root: str, target_label: str) -> str:
    root = _clean_path(game_root)
    if not root:
        return ""
    try:
        _src, _dst, lang = _codes(target_label, DEFAULT_SOURCE)
        st = tools.tl_status(root, lang)
        tl_dir = _tl_dir(root, target_label)
    except Exception as exc:
        return t("err.generic", err=exc)
    if not st["exists"]:
        return t("tl.no_dir", lang=lang)
    total, done, left = tools.tl_counts(tl_dir)
    who = t("tl.ours") if st["ours"] else t("tl.not_ours")
    hook = t("tl.hook_on", hook=tools.LANG_HOOK, lang=st["hook_lang"]) if st["hook"] else t("tl.hook_off")
    return t("tl.status", lang=lang, files=st["rpy_files"], who=who, done=done, total=total, left=left, hook=hook)


def tl_refresh(game_root: str, target_label: str):
    """État de tl/<langue> + ouverture des étapes 2 et 3 si les textes sont déjà extraits (retour dans l'application)."""
    shown, hidden = gr.update(visible=True), gr.update(visible=False)
    root = _clean_path(game_root)
    ready = False
    if root:
        try:
            _src, _dst, lang = _codes(target_label, DEFAULT_SOURCE)
            st = tools.tl_status(root, lang)
            ready = bool(st["exists"] and st["ours"])
        except Exception:
            ready = False
    return [tl_status_md(root, target_label), shown if ready else hidden, shown if ready else hidden,
            hidden if ready else shown, hidden if ready else shown]


def deeplink(request: gr.Request):
    """Ouverture directe d'un onglet : http://127.0.0.1:port/?tab=tab_tools&sub=sub_tl&game=D:\\Games\\MyGame."""
    try:
        q = dict(request.query_params) if request is not None else {}
    except Exception:
        q = {}
    tab, sub, game = q.get("tab", ""), q.get("sub", ""), _clean_path(q.get("game", ""))
    fill = game if game else gr.update()
    return [gr.Tabs(selected=tab) if tab else gr.update(), gr.Tabs(selected=sub) if sub else gr.update(), fill, fill, fill, fill]


def tl_generate(game_root: str, target_label: str, merge: bool):
    """Étape 1 : génération des tl par le moteur Ren'Py (générateur). Sorties : statut, corps 2, corps 3, indice 2, indice 3, état."""
    shown, hidden = gr.update(visible=True), gr.update(visible=False)
    root = _clean_path(game_root)
    if _tools_busy():
        yield t("tools.busy"), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()
        return
    try:
        _src, _dst, lang = _codes(target_label, DEFAULT_SOURCE)
        core.find_game_dir(root)
    except Exception as exc:
        yield t("err.analyze_short", err=exc), hidden, hidden, gr.update(), gr.update(), ""
        return
    thread, log_q, latest = _tools_thread(lambda log, prog, cancel: tools.generate_tl(root, lang, bool(merge), log, cancel))
    lines: list[str] = []
    t0 = time.time()
    while thread.is_alive():
        _drain(log_q, lines)
        yield (t("tl.generating", lang=lang, elapsed=core.format_eta(time.time() - t0)) + "  \n" + "  \n".join(f"`{ln}`" for ln in lines[-2:]),
               hidden, hidden, gr.update(), gr.update(), "")
        time.sleep(0.5)
    _drain(log_q, lines)
    if latest["error"]:
        yield t("single.failed", err=str(latest["error"]).splitlines()[0]), hidden, hidden, gr.update(), gr.update(), ""
        return
    r: tools.GenerateResult = latest["result"]  # type: ignore[assignment]
    if r.error:
        msg = r.error.replace("\n", "  \n")
        if r.preexisting and not merge:
            msg += "  \n" + t("tl.merge_hint")
        yield f"**{msg}**", hidden, hidden, gr.update(), gr.update(), tl_status_md(root, target_label)
        return
    head = t("tl.generated", elapsed=core.format_eta(r.elapsed), version=r.version, runtime=r.runtime, dialogue=r.dialogue,
             strings=r.strings, files=len(r.files), lang=lang)
    if r.merged:
        head += " " + t("tl.merged")
    if r.existing_languages:
        head += "  \n" + t("tl.existing_languages", langs=", ".join(r.existing_languages))
    if r.dialogue + r.strings == 0:
        head += "  \n" + t("tl.nothing_to_translate")
    yield head, shown, shown, hidden, hidden, tl_status_md(root, target_label)


def tl_apply_corrections(game_root: str, target_label: str, rows):
    try:
        tl_dir = _tl_dir(_clean_path(game_root), target_label)
        data = rows.values.tolist() if hasattr(rows, "values") else (rows or [])
        fixes = [{"file": r[0], "line": int(r[1]), "translated": r[3]} for r in data if len(r) >= 4]
        n = tools.apply_corrections(tl_dir, fixes)
        return t("tl.corrections_saved", n=n) if n else t("tl.no_corrections")
    except Exception as exc:
        return t("err.plain", err=exc)


ID_FORMAT_LABELS = {t("tl.idfmt.ligne"): "ligne", t("tl.idfmt.section"): "§"}
SCOPE_LABELS = {t("tl.scope.dialogue"): "dialogue", t("tl.scope.all"): "all"}
ONLY_UNTRANSLATED, ALL_TEXTS = t("tl.only_untranslated"), t("tl.all_texts")


def tl_export(game_root: str, target_label: str, base_name: str, chunk: float, max_mb: float, id_format_label: str, scope_label: str,
              protect_tags: bool, only_untranslated: str):
    """Étape 2 : fichiers numérotés à traduire ; ouvre le dossier dans l'Explorateur."""
    root = _clean_path(game_root)
    try:
        tl_dir = _tl_dir(root, target_label)
        if not tl_dir.is_dir():
            return t("tl.extract_first"), gr.update(), gr.update(visible=False)
        r = tools.export_segments(tl_dir, base_name or "phrase", int(chunk or 10000), int(float(max_mb or 0) * 1024 * 1024),
                                  ID_FORMAT_LABELS.get(id_format_label, "ligne"), SCOPE_LABELS.get(scope_label, "dialogue"),
                                  bool(protect_tags), only_untranslated != ALL_TEXTS)
        if not r.count:
            return t("tl.nothing_to_export"), gr.update(), gr.update(visible=True)
        try:
            os.startfile(str(r.out_dir))  # type: ignore[attr-defined]
        except Exception:
            pass
        files = "  \n".join(f"- `{f}`" for f in r.files[:12]) + (("  \n- … " + t("log.and_more", n=len(r.files) - 12)) if len(r.files) > 12 else "")
        sample = "`ligne1;…`" if ID_FORMAT_LABELS.get(id_format_label, "ligne") == "ligne" else "`§0001§ …`"
        msg = t("tl.exported", count=r.count, files=len(r.files), skipped=r.skipped, dir=r.out_dir) + f"  \n{files}  \n" + t("tl.export_howto", sample=sample)
        return msg, str(r.out_dir), gr.update(visible=True)
    except Exception as exc:
        return t("err.generic", err=exc), gr.update(), gr.update()


def tl_import_browse(current: str):
    first = _clean_path(current).splitlines()[0] if _clean_path(current) else ""
    initial = first if Path(first).is_dir() else (str(Path(first).parent) if first else "")
    chosen = tools.pick_files(t("dialog.pick_translated"), t("dialog.text_filter") + "|*.txt|" + t("dialog.all_files") + "|*.*", initial)
    return "\n".join(chosen) if chosen else current


def tl_import(game_root: str, target_label: str, paths: str):
    hidden = gr.update(visible=False)
    root = _clean_path(game_root)
    try:
        tl_dir = _tl_dir(root, target_label)
        files = [Path(_clean_path(p)) for p in str(paths or "").replace(";", "\n").splitlines() if _clean_path(p)]
        r = tools.import_files(tl_dir, files)
        samples = tools.sample_segments(tl_dir, 30)
        _TOOLS["samples"] = samples
        rows = [[x["file"], x["line"], x["source"], x["translated"]] for x in samples]
        anomalies = r.errors or r.missing_ids or r.unknown or r.merged or r.duplicates
        head = t("tl.import_done") if not anomalies else t("tl.import_anomalies")
        encs = ", ".join(f"{n} ({e})" for n, e in r.encodings.items() if not e.startswith("utf-8"))
        msg = t("tl.import_summary", head=head, files=len(r.files), lines=r.lines, applied=r.applied, untranslated=r.untranslated, errors=len(r.errors))
        if r.unknown:
            msg += ", " + t("tl.import_unknown", n=r.unknown)
        if r.merged:
            msg += ", " + t("tl.import_merged", n=r.merged)
        if r.duplicates:
            msg += ", " + t("tl.import_duplicates", n=r.duplicates)
        msg += "."
        if r.missing_ids:
            msg += "  \n" + t("tl.import_missing", n=len(r.missing_ids), examples=", ".join(r.missing_ids[:6]))
        if r.errors:
            msg += "  \n" + t("tl.import_errors") + " " + " ; ".join(r.errors[:6]) + (" ; …" if len(r.errors) > 6 else "")
        if encs:
            msg += "  \n" + t("tl.import_encoding", encs=encs)
        total, done, left = tools.tl_counts(tl_dir)
        msg += "  \n" + t("tl.state", done=done, total=total, left=left)
        if left:
            msg += " " + t("tl.state_left_hint")
        return msg, gr.update(value=rows), gr.update(visible=bool(rows)), tl_status_md(root, target_label)
    except Exception as exc:
        return t("err.generic", err=exc), gr.update(), hidden, gr.update()


def tl_install(game_root: str, target_label: str):
    root = _clean_path(game_root)
    try:
        _src, _dst, lang = _codes(target_label, DEFAULT_SOURCE)
        tl_dir = _tl_dir(root, target_label)
        if not tl_dir.is_dir():
            return t("tl.install_no_dir", lang=lang), tl_status_md(root, target_label)
        total, done, left = tools.tl_counts(tl_dir)
        target = tools.install_language_hook(root, lang)
        msg = t("tl.installed", file=target.name, lang=lang, done=done, total=total)
        if left:
            msg += "  \n" + t("tl.installed_left", n=left)
        return msg, tl_status_md(root, target_label)
    except Exception as exc:
        return t("err.generic", err=exc), gr.update()


def tl_check(game_root: str, target_label: str):
    root = _clean_path(game_root)
    if _tools_busy():
        yield t("tools.busy")
        return
    _src, _dst, lang = _codes(target_label, DEFAULT_SOURCE)
    thread, log_q, latest = _tools_thread(lambda log, prog, cancel: tools.check_translation(root, lang, log, cancel))
    t0 = time.time()
    while thread.is_alive():
        yield t("tl.checking", elapsed=core.format_eta(time.time() - t0))
        time.sleep(0.5)
    if latest["error"]:
        yield t("tl.check_failed", err=str(latest["error"]).splitlines()[0])
        return
    r: dict = latest["result"]  # type: ignore[assignment]
    if r.get("error") or r.get("traceback"):
        msg = t("tl.check_game_error")
        if r.get("traceback"):
            msg += "  \n```\n" + r["traceback"][-900:] + "\n```"
        elif r.get("error"):
            msg += f"  \n{r['error']}"
        if r.get("output"):
            msg += "  \n```\n" + r["output"][-600:] + "\n```"
        yield msg
        return
    ok = r.get("pref_language") == lang and r.get("dialogue_blocks", 0) > 0 and r.get("hook_loaded")
    samples = "  \n".join(f"- « {k} » → « {v} »" for k, v in list(r.get("samples", {}).items())[:6])
    version = str(r.get("version", "?")).replace("Ren'Py ", "")
    yield ((t("tl.check_ok", version=version, lang=r.get("pref_language"), blocks=r.get("dialogue_blocks", 0)) + f"  \n{samples}") if ok else
           (t("tl.check_partial", lang=r.get("pref_language"), expected=lang, blocks=r.get("dialogue_blocks", 0),
              hook=t("tl.hook_present") if r.get("hook_loaded") else t("tl.hook_absent")) + f"  \n{samples}"))


def tl_uninstall(game_root: str, target_label: str, remove_tl: bool):
    root = _clean_path(game_root)
    try:
        _src, _dst, lang = _codes(target_label, DEFAULT_SOURCE)
        return "  \n".join(tools.uninstall_translation(root, lang, bool(remove_tl))), tl_status_md(root, target_label)
    except Exception as exc:
        return t("err.generic", err=exc), gr.update()


# ---- Android (APK) -----------------------------------------------------------
_ANDROID: dict[str, object] = {"analysis": None, "sdk": None, "jdk": None, "build": None, "cfg": None, "sdk_version": ""}
ORIENTATION_LABELS = {t(f"android.orientation.{k}"): k for k in android.ORIENTATIONS}
STEP_A1_HINT = t("android.step1_hint")
A_CFG_KEYS = ("name", "icon_name", "package", "version", "numeric", "orientation", "internet", "videos", "budget", "icon", "bundle", "decompile",
              "skip_rpa", "prefer_rpyc", "data_mode", "ext_audio", "arm64", "estimate")
DATA_MODE_LABELS = {t("android.data.apk"): "apk", t("android.data.external"): "external"}
CACHE_KIND_LABELS = {k: t(f"android.cache.kind.{k}") for k in ("sdk", "jdk", "gradle", "unrpyc", "downloads", "build")}


def _a_version_from_choice(choice: str) -> str:
    return str(choice or "").split(" ")[0].strip()


def _a_env_md(version: str, manual_sdk: str) -> tuple[str, bool]:
    """Ligne d'état de l'environnement pour la version choisie ; (markdown, prêt)."""
    if not version and not manual_sdk:
        return "", False
    try:
        st = android.env_status(version, _clean_path(manual_sdk))
    except Exception as exc:
        return t("err.generic", err=exc), False
    sdk = st["sdk"]
    if st["ready"]:
        fam = android.load_matrix()["families"].get(sdk.family, {}).get("label", sdk.family)
        return t("android.env.ready", version=sdk.version, family=fam, jdk=sdk.jdk_major), True
    fam_jdk = sdk.jdk_major if sdk else android.load_matrix()["families"][android.family_for(version)]["jdk"]
    items = []
    if not st["sdk_present"] or not st["rapt_present"]:
        items.append(t("android.env.item.sdk", version=version))
    if st["jdk"] is None:
        items.append(t("android.env.item.jdk", jdk=fam_jdk))
    if not st["android_installed"]:
        items.append(t("android.env.item.android"))
    if not st["keys"]:
        items.append(t("android.env.item.keys"))
    return t("android.env.missing", items=", ".join(items) or "—") + "  \n" + t("android.env.sizes"), False


def _a_estimate_md(a: android.AndroidAnalysis, include_videos: bool, budget_mb: float, skip_rpa: bool, data_mode: str = "apk",
                   ext_audio: bool = False) -> str:
    if DATA_MODE_LABELS.get(data_mode, data_mode) == "external":
        apk, pack = a.estimated_split(bool(ext_audio), bool(skip_rpa))
        msg = t("android.cfg.estimate_split", apk=core.human_size(apk), pack=core.human_size(pack), path=android.phone_data_path("<package>"))
        if apk > android.APK_SOFT_LIMIT:
            msg += "  \n" + t("android.cfg.too_big")
        return msg
    est = a.estimated_apk(bool(include_videos), int(budget_mb or 0) * 1024 * 1024, bool(skip_rpa))
    msg = t("android.cfg.estimate", size=core.human_size(est))
    if est > android.APK_SOFT_LIMIT:
        msg += "  \n" + t("android.cfg.too_big") + " " + t("android.cfg.suggest_external")
    return msg


def _a_cfg_updates(a: android.AndroidAnalysis, sdk_version: str) -> list:
    """Valeurs par défaut des champs de l'étape 3 (dans l'ordre de A_CFG_KEYS)."""
    cfg = android.default_config(a, sdk_version)
    sdk = _ANDROID.get("sdk")
    supports_bundle = bool(android.ANDROID_BUNDLE_ENABLED and (sdk.supports_bundle if isinstance(sdk, android.SdkInfo) else a.family != "legacy"))
    show_decompile = bool(a.rpy_missing) and android.ANDROID_UNRPYC_ENABLED
    return [
        cfg.name, cfg.icon_name, cfg.package, cfg.version, cfg.numeric_version,
        {v: k for k, v in ORIENTATION_LABELS.items()}.get(cfg.orientation, list(ORIENTATION_LABELS)[0]),
        cfg.internet,
        gr.update(value=False, label=t("android.cfg.videos", n=a.videos_count, size=core.human_size(a.videos_bytes)), visible=a.videos_count > 0),
        0, cfg.icon_path,
        gr.update(value=False, visible=supports_bundle),
        gr.update(value=show_decompile, visible=show_decompile),
        gr.update(value=True, label=t("android.cfg.skip_rpa", n=len(a.rpa_extracted), size=core.human_size(a.rpa_extracted_bytes)), visible=bool(a.rpa_extracted)),
        gr.update(value=cfg.prefer_rpyc, visible=a.rpyc_count > 0),
        {v: k for k, v in DATA_MODE_LABELS.items()}[cfg.data_mode],
        gr.update(value=cfg.ext_audio, label=t("android.cfg.ext_audio", n=a.audio_count, size=core.human_size(a.audio_bytes)), visible=a.audio_count > 0),
        gr.update(value=a.family == "legacy", label=t("android.cfg.arm64", sdk=android.ARM64_LEGACY_SDK), visible=a.family == "legacy"),
        _a_estimate_md(a, False, 0, True, cfg.data_mode, cfg.ext_audio),
    ]


def android_estimate(include_videos: bool, budget_mb: float, skip_rpa: bool, data_mode: str, ext_audio: bool) -> str:
    a = _ANDROID.get("analysis")
    return _a_estimate_md(a, include_videos, budget_mb, skip_rpa, data_mode, ext_audio) if isinstance(a, android.AndroidAnalysis) else ""


def android_analyze(game_root: str):
    """Étape 1 : analyse. Sorties : résumé, corps 2, indice 2, choix du SDK, état env, corps 3, indice 3, corps 4, indice 4, champs de config."""
    shown, hidden = gr.update(visible=True), gr.update(visible=False)
    root = _clean_path(game_root)
    blank_cfg = [gr.update()] * len(A_CFG_KEYS)
    closed = [hidden, shown, gr.update(choices=[], value=None), "", hidden, shown, hidden, shown]
    if not root:
        return [STEP_A1_HINT] + closed + blank_cfg
    try:
        a = android.analyze_game(root)
    except Exception as exc:
        return [t("err.analyze_short", err=exc)] + closed + blank_cfg
    _ANDROID["analysis"] = a
    lines = []
    sdk_version, reason = a.sdk_version, a.sdk_reason
    if reason == "unsupported" or not sdk_version:
        lines.append(t("android.an.summary", version=a.version, sdk="—", reason=t(f"android.reason.{reason}")))
        return ["  \n".join(lines)] + closed + blank_cfg
    if a.family == "legacy":
        # RAPT 7.0–7.3 patché par RenPyHD (dépendance d'expansion Google Play retirée) : le SDK exact construit et démarre
        # (vérifié 7.3.5) ; les .rpyc du jeu sont compatibles, contrairement à une recompilation par un SDK 7.4+.
        lines.append(t("android.an.legacy_warn", version=a.version, sdk=sdk_version))
        lines.append(t("android.an.legacy_abi", sdk=android.ARM64_LEGACY_SDK))
    lines.append(t("android.an.summary", version=a.version, sdk=sdk_version, reason=t(f"android.reason.{reason}")))
    if not a.online:
        lines.append(t("android.an.offline"))
    if a.rpy_missing:
        lines.append(t("android.an.scripts_missing", n=len(a.rpy_missing), examples=", ".join(a.rpy_missing[:4]) + ("…" if len(a.rpy_missing) > 4 else "")))
    else:
        lines.append(t("android.an.scripts_ok", rpy=a.rpy_count, rpyc=a.rpyc_count))
    if a.has_hd2x:
        lines.append(t("android.an.hd2x", size=core.human_size(a.excluded_bytes)))
    if a.has_hook:
        lines.append(t("android.an.hook"))
    if a.has_backup:
        lines.append(t("android.an.backup"))
    lines.append(t("android.an.images", n=a.images_count, size=core.human_size(a.images_bytes)))
    if a.videos_count:
        lines.append(t("android.an.videos", n=a.videos_count, size=core.human_size(a.videos_bytes)))
    if a.rpa_count:
        lines.append(t("android.an.rpa", n=a.rpa_count, size=core.human_size(a.rpa_bytes), extracted=len(a.rpa_extracted), esize=core.human_size(a.rpa_extracted_bytes)))
    if a.has_tl:
        lines.append(t("android.an.tl"))
    if a.existing_json:
        lines.append(t("android.an.json", package=a.existing_json.get("package", "?"), version=a.existing_json.get("version", "?")))
    est = a.estimated_apk(False, 0, True)
    lines.append(t("android.an.estimate", size=core.human_size(est)))
    if est > 2 * 1024 ** 3:
        lines.append(t("android.an.too_big"))
    _ANDROID["sdk_version"] = sdk_version
    choices = [f"{sdk_version} — " + t("android.sdk_auto", version=sdk_version, reason=t(f"android.reason.{reason}"))]
    choices += [v for v in android.installed_sdk_versions() if v != sdk_version]
    env_md, ready = _a_env_md(sdk_version, "")
    if ready:
        st = android.env_status(sdk_version)
        _ANDROID["sdk"], _ANDROID["jdk"] = st["sdk"], st["jdk"]
    return (["  \n".join(lines), shown, hidden, gr.update(choices=choices, value=choices[0]), env_md,
             shown if ready else hidden, hidden if ready else shown, shown if ready else hidden, hidden if ready else shown]
            + _a_cfg_updates(a, sdk_version))


def android_env_refresh(sdk_choice: str, manual_sdk: str):
    return _a_env_md(_a_version_from_choice(sdk_choice), manual_sdk)[0]


def android_prepare(game_root: str, sdk_choice: str, manual_sdk: str, org: str, with_unrpyc: bool):
    """Étape 2 (générateur). Sorties : statut, barre, journal, note clés, corps 3, indice 3, corps 4, indice 4, état env, case .rpyc."""
    shown, hidden, keep = gr.update(visible=True), gr.update(visible=False), gr.update()
    if _tools_busy():
        yield [t("tools.busy")] + [keep] * 9
        return
    a = _ANDROID.get("analysis")
    version = _a_version_from_choice(sdk_choice)
    if not isinstance(a, android.AndroidAnalysis) or (not version and not _clean_path(manual_sdk)):
        yield [t("android.need_analysis"), "", ""] + [keep] * 7
        return
    thread, log_q, latest = _tools_thread(
        lambda log, prog, cancel: android.prepare_environment(version, str(org or "RenPyHD"), bool(with_unrpyc), log, prog, cancel, _clean_path(manual_sdk)))
    lines: list[str] = []
    while thread.is_alive():
        _drain(log_q, lines)
        p: android.Progress | None = latest["progress"]  # type: ignore[assignment]
        if p is None:
            txt, frac = t("android.phase.sdk") + "…", 0.0
        else:
            frac = p.fraction
            extra = f" — {core.human_size(p.bytes_done)} / {core.human_size(p.bytes_total)}" if p.bytes_total else ""
            txt = t("android.preparing", phase=p.phase, pct=f"{100 * frac:.0f}", elapsed=core.format_eta(p.elapsed), extra=extra)
        yield [txt, _bar_html(frac, f"{100 * frac:.0f} %"), "\n".join(lines[-300:])] + [keep] * 7
        time.sleep(0.5)
    _drain(log_q, lines)
    if latest["error"]:
        lines.append("ERREUR : " + str(latest["error"]))
        yield [t("android.prep_failed", err=str(latest["error"]).splitlines()[0]), "", "\n".join(lines[-300:])] + [keep] * 7
        return
    r: android.PrepResult = latest["result"]  # type: ignore[assignment]
    if r.cancelled:
        yield [t("android.prep_cancelled"), _bar_html(0.0, t("progress.cancelled")), "\n".join(lines[-300:])] + [keep] * 7
        return
    if r.error or r.sdk is None:
        yield [t("android.prep_failed", err=r.error), "", "\n".join(lines[-300:])] + [keep] * 7
        return
    _ANDROID["sdk"], _ANDROID["jdk"], _ANDROID["sdk_version"] = r.sdk, r.jdk, r.sdk.version
    keys_note = t("android.keys_created", dir=android.KEYS_DIR) if r.keys_created else ""
    env_md, _ready = _a_env_md(r.sdk.version, "")
    prefer = gr.update(value=not android.sdk_matches_game(r.sdk.version, a.version) and a.rpyc_count > 0, visible=a.rpyc_count > 0)
    yield [t("android.prep_done", elapsed=core.format_eta(r.elapsed)), _bar_html(1.0, t("progress.done"), done=True), "\n".join(lines[-300:]),
           keys_note, shown, hidden, shown, hidden, env_md, prefer]


def _a_config_from(v: list) -> android.BuildConfig:
    cfg = android.BuildConfig()
    (name, icon_name, package, version, numeric, orientation_label, internet, videos, budget, icon, bundle, decompile, skip_rpa, prefer_rpyc,
     data_mode, ext_audio, arm64, _estimate) = v
    cfg.name, cfg.icon_name, cfg.package, cfg.version = str(name).strip(), str(icon_name).strip(), str(package).strip(), str(version).strip()
    cfg.numeric_version = int(numeric or 0)
    cfg.orientation = ORIENTATION_LABELS.get(orientation_label, "sensorLandscape")
    cfg.data_mode = DATA_MODE_LABELS.get(data_mode, "apk")
    cfg.ext_audio = bool(ext_audio)
    cfg.arm64_legacy = bool(arm64)
    cfg.image_budget_mb = int(budget or 0)
    cfg.icon_path = _clean_path(icon)
    cfg.internet, cfg.include_videos, cfg.bundle = bool(internet), bool(videos), bool(bundle)
    cfg.decompile, cfg.skip_extracted_rpa, cfg.prefer_rpyc = bool(decompile), bool(skip_rpa), bool(prefer_rpyc)
    return cfg


def android_build(*v):
    """Étape 4 (générateur) : copie de construction → décompilation (option) → android_build. Sorties : statut, barre, journal, bloc fin, md fin."""
    hidden, keep = gr.update(visible=False), gr.update()
    if _tools_busy():
        yield t("tools.busy"), keep, keep, keep, keep
        return
    a, sdk, jdk = _ANDROID.get("analysis"), _ANDROID.get("sdk"), _ANDROID.get("jdk")
    if not isinstance(a, android.AndroidAnalysis):
        yield t("android.need_analysis"), "", "", hidden, ""
        return
    if not isinstance(sdk, android.SdkInfo) or jdk is None:
        yield t("android.need_env"), "", "", hidden, ""
        return
    cfg = _a_config_from(list(v))
    errs = android.validate_config(cfg)
    if errs:
        yield t("android.cfg.errors", errs=" ; ".join(errs)), "", "", hidden, ""
        return
    _ANDROID["cfg"] = cfg
    stage_info: dict[str, object] = {}
    arm64 = bool(cfg.arm64_legacy) and a.family == "legacy"
    if arm64:
        # route arm64 des jeux 7.0–7.3 : SDK 7.8.7 (Python 2, arm64-v8a) + décompilation unrpyc + recompilation
        st787 = android.env_status(android.ARM64_LEGACY_SDK)
        if not st787.get("ready") or st787.get("jdk") is None or not st787.get("unrpyc"):
            yield t("android.need_sdk_arm64", version=android.ARM64_LEGACY_SDK), "", "", hidden, ""
            return
        sdk, jdk = st787["sdk"], st787["jdk"]
        cfg.prefer_rpyc = False

    def work(log, prog, cancel):
        st = android.stage_build(a, cfg, sdk, log, prog, cancel)
        stage_info["stage"] = st
        if arm64:
            log(t("android.decompiling"))
            ok, errors, removed = android.decompile_all(sdk, st.build_dir, log, cancel)
            stage_info["decompile"] = (ok, errors)
            comp = android.compile_and_fix(sdk, st.build_dir, log, cancel)
            stage_info["compile"] = comp
            if not comp.get("ok"):
                raise RuntimeError(t("android.err.compile", errors="  \n".join(comp.get("errors", [])[:6])))
        elif cfg.decompile and a.rpy_missing:
            log(t("android.decompiling"))
            stage_info["decompile"] = android.decompile_missing(sdk, st.build_dir, a.rpy_missing, log, cancel)
        return android.build_apk(sdk, jdk, st.build_dir, cfg, log, prog, cancel)

    thread, log_q, latest = _tools_thread(work)
    lines: list[str] = []
    t0 = time.time()
    stage_phase = t("android.phase.stage")
    while thread.is_alive():
        _drain(log_q, lines)
        p: android.Progress | None = latest["progress"]  # type: ignore[assignment]
        if p is None:
            txt, frac = stage_phase + "…", 0.0
        elif p.phase == stage_phase:
            frac = 0.1 * p.fraction
            txt = t("android.staging", pct=f"{100 * p.fraction:.0f}", detail=p.detail)
        else:
            frac = 0.1 + 0.9 * p.fraction
            txt = t("android.building", phase=p.phase, pct=f"{100 * frac:.0f}", elapsed=core.format_eta(time.time() - t0), detail=p.detail)
        yield txt, _bar_html(frac, f"{100 * frac:.0f} %"), "\n".join(lines[-400:]), hidden, ""
        time.sleep(0.5)
    _drain(log_q, lines)
    if latest["error"]:
        lines.append("ERREUR : " + str(latest["error"]))
        yield t("android.build_failed", err=str(latest["error"]).splitlines()[0], log=android.LOG_DIR), "", "\n".join(lines[-400:]), hidden, ""
        return
    r: android.BuildResult = latest["result"]  # type: ignore[assignment]
    _ANDROID["build"] = r
    if r.cancelled:
        yield t("android.build_cancelled"), _bar_html(0.0, t("progress.cancelled")), "\n".join(lines[-400:]), hidden, ""
        return
    if not r.ok:
        yield t("android.build_failed", err=r.error.replace("\n", "  \n"), log=r.log_file), "", "\n".join(lines[-400:]), hidden, ""
        return
    main = android.pick_main_apk(r.files)
    md = t("android.build_done", elapsed=core.format_eta(r.elapsed), file=main, size=core.human_size(main.stat().st_size)) if main else ""
    st = stage_info.get("stage")
    if isinstance(st, android.StageResult):
        md += "  \n" + t("android.staged_note", files=st.files, size=core.human_size(st.bytes), excluded=", ".join(st.excluded) or "—")
        if st.images_skipped:
            md += " " + t("android.images_limited", n=len(st.images_skipped))
        if st.pack_dir is not None:
            pkg = cfg.package.strip().lower()
            md += "  \n" + t("android.pack_note", dir=st.pack_dir, files=st.pack_files, size=core.human_size(st.pack_bytes),
                             how=t("android.pack_linked") if st.pack_linked else t("android.pack_copied"))
            md += "  \n" + t("android.pack_howto", path=android.phone_data_path(pkg), obb=f"/sdcard/Android/obb/{pkg}/game", pkg=pkg)
    if "decompile" in stage_info:
        ok, errors = stage_info["decompile"]  # type: ignore[misc]
        md += "  \n" + t("android.decompile_result", ok=ok, errors=len(errors))
    if "compile" in stage_info:
        comp: dict = stage_info["compile"]  # type: ignore[assignment]
        md += "  \n" + t("android.arm64_result", sdk=sdk.version, rounds=comp.get("rounds", 0), fixes=len(comp.get("fixes", [])),
                         patterns=", ".join(sorted({f"{fn}:{ln} ({p})" for fn, ln, p in comp.get("fixes", [])})) or "—")
    ver: dict | None = None
    if main:
        try:
            ver = android.verify_apk(sdk, jdk, main)
            yes, no = t("android.yes"), t("android.no")
            signed = yes if ver.get("signed") else (no if ver.get("signed") is False else "?")
            md += "  \n" + t("android.verify", entries=ver.get("entries", 0), manifest=yes if ver.get("manifest") else no, game=ver.get("game_files", 0),
                             libs=", ".join(ver.get("libs", [])) or "—", signed=signed)
        except Exception as exc:
            md += "  \n" + t("err.plain", err=exc)
    others = [f.name for f in r.files if f != main]
    if others:
        md += "  \n" + t("android.other_files", list=", ".join(others))
    try:
        android.write_build_manifest(a, cfg, sdk, st if isinstance(st, android.StageResult) else None, r, ver)
    except Exception as exc:
        md += "  \n" + t("err.plain", err=exc)
    yield "", _bar_html(1.0, t("progress.done"), done=True), "\n".join(lines[-400:]), gr.update(visible=True), md


def android_push():
    """Copie le pack de données de la dernière construction sur le téléphone (adb push) — générateur : statut."""
    sdk, r, cfg = _ANDROID.get("sdk"), _ANDROID.get("build"), _ANDROID.get("cfg")
    if not isinstance(sdk, android.SdkInfo) or not isinstance(r, android.BuildResult) or not isinstance(cfg, android.BuildConfig):
        yield t("android.need_env")
        return
    pack = android.pack_dir_for(cfg)
    if cfg.data_mode != "external" or not (pack / "game").is_dir():
        yield t("android.push_no_pack")
        return
    if _tools_busy():
        yield t("tools.busy")
        return
    devs = android.adb_devices(sdk)
    if not devs:
        yield t("android.no_device")
        return
    pkg = cfg.package.strip().lower()
    thread, log_q, latest = _tools_thread(lambda log, prog, cancel: android.adb_push_data(sdk, pack, pkg, log, cancel))
    lines: list[str] = []
    t0 = time.time()
    while thread.is_alive():
        _drain(log_q, lines)
        yield t("android.pushing", elapsed=core.format_eta(time.time() - t0), detail=(lines[-1][:120] if lines else ""))
        time.sleep(0.7)
    _drain(log_q, lines)
    if latest["error"]:
        yield t("android.push_failed", rc="?", out=str(latest["error"]).splitlines()[0])
        return
    rc, out = latest["result"]  # type: ignore[misc]
    yield t("android.push_done", device=devs[0], path=android.phone_data_path(pkg)) if rc == 0 else t("android.push_failed", rc=rc, out=out[-400:])


# ---- Mes APK (gestionnaire) --------------------------------------------------
_MGR: dict[str, object] = {"builds": [], "caches": []}


def _mgr_label(e: android.BuildEntry) -> str:
    return f"{e.name} — {e.data.get('name') or e.name} ({e.data.get('package') or '?'})"


def android_mgr_refresh(refresh_sizes: bool = False):
    """Sorties : table, dropdown, résumé."""
    try:
        builds = android.list_builds(refresh_sizes=refresh_sizes)
    except Exception as exc:
        return gr.update(value=[]), gr.update(choices=[], value=None), t("err.plain", err=exc)
    _MGR["builds"] = builds
    yes, no = t("android.yes"), t("android.no")
    rows = []
    total = 0
    for e in builds:
        d = e.data
        signed = yes if d.get("signed") else (no if d.get("signed") is False else "?")
        mode = t("android.mgr.mode_external") if d.get("data_mode") == "external" else t("android.mgr.mode_apk")
        v = d.get("verified") or {}
        verified = (("✅ " if v.get("ok") else "❌ ") + str(v.get("when_text") or "")) if v else "—"
        rows.append([str(d.get("name") or e.name), str(d.get("package") or ""), f"{d.get('version') or '?'} ({d.get('numeric_version') or '?'})",
                     str(d.get("built_text") or ""), str(d.get("sdk_version") or "?"), mode, core.human_size(int(d.get("apk_bytes") or 0)),
                     core.human_size(int(d.get("pack_bytes") or 0)) if d.get("data_mode") == "external" else "—", signed, verified])
        total += e.total_bytes
    labels = [_mgr_label(e) for e in builds]
    summary = t("android.mgr.summary", n=len(builds), size=core.human_size(total), dir=android.OUT_DIR) if builds else t("android.mgr.empty", dir=android.OUT_DIR)
    return gr.update(value=rows), gr.update(choices=labels, value=labels[0] if labels else None), summary


def _mgr_pick(choice: str) -> android.BuildEntry | None:
    for e in _MGR.get("builds", []):  # type: ignore[union-attr]
        if _mgr_label(e) == choice:
            return e
    return None


def android_mgr_open(choice: str):
    e = _mgr_pick(choice)
    if e:
        android.open_folder(e.out_dir)


def android_mgr_delete(choice: str, confirm: bool):
    e = _mgr_pick(choice)
    if e is None:
        return t("android.mgr.pick_one"), gr.update(), gr.update(), gr.update()
    if not confirm:
        return t("android.mgr.confirm_first"), gr.update(), gr.update(), gr.update()
    size = e.total_bytes
    ok = android.delete_build(e.name)
    table, dd, summary = android_mgr_refresh()
    msg = t("android.mgr.deleted", name=e.name, size=core.human_size(size)) if ok else t("android.mgr.delete_failed", name=e.name)
    return msg, table, dd, summary


def android_mgr_install(choice: str):
    """Installe l'APK choisi (+ pack de données) sur le téléphone — générateur : statut."""
    e = _mgr_pick(choice)
    if e is None:
        yield t("android.mgr.pick_one")
        return
    sdk = _ANDROID.get("sdk") if isinstance(_ANDROID.get("sdk"), android.SdkInfo) else android.sdk_with_adb()
    if sdk is None:
        yield t("android.mgr.no_adb")
        return
    if _tools_busy():
        yield t("tools.busy")
        return
    info = android.adb_device_info(sdk)
    if not info:
        yield t("android.no_device")
        return
    devs = [info["serial"]]
    pack, pkg = e.pack_dir, str(e.data.get("package") or "")
    files = [e.out_dir / f for f in (e.data.get("files") or [])] or ([e.apk] if e.apk else [])
    apk, msg = _pick_for_device(files, info, e.data.get("sdk_family") == "legacy")
    if apk is None:
        yield _device_md(info) + "  \n" + (msg or t("android.mgr.no_apk", name=e.name))
        return
    yield _device_md(info) + "  \n" + t("android.install_picked", apk=apk.name, abi=android.apk_abi(apk))

    def work(log, prog, cancel):
        rc, out = android.adb_install(sdk, apk, log, cancel)
        if rc != 0:
            return rc, out, False
        if pack is not None and pkg:
            rc2, out2 = android.adb_push_data(sdk, pack, pkg, log, cancel)
            return rc2, out + "\n" + out2, True
        return 0, out, False

    thread, log_q, latest = _tools_thread(work)
    lines: list[str] = []
    t0 = time.time()
    while thread.is_alive():
        _drain(log_q, lines)
        yield t("android.pushing", elapsed=core.format_eta(time.time() - t0), detail=(lines[-1][:120] if lines else ""))
        time.sleep(0.7)
    _drain(log_q, lines)
    if latest["error"]:
        yield t("android.install_failed", rc="?", out=str(latest["error"]).splitlines()[0])
        return
    rc, out, pushed = latest["result"]  # type: ignore[misc]
    if rc != 0:
        yield t("android.install_failed", rc=rc, out=out[-400:])
    elif pushed:
        yield t("android.install_done", device=devs[0]) + " " + t("android.push_done", device=devs[0], path=android.phone_data_path(pkg))
    else:
        yield t("android.install_done", device=devs[0])


def android_mgr_verify(choice: str):
    """« Vérifier » : lance la copie sur PC avec la sonde (générateur : statut, table, dropdown, résumé)."""
    keep = gr.update()
    e = _mgr_pick(choice)
    if e is None:
        yield t("android.mgr.pick_one"), keep, keep, keep
        return
    if _tools_busy():
        yield t("tools.busy"), keep, keep, keep
        return
    thread, log_q, latest = _tools_thread(lambda log, prog, cancel: android.verify_build(e, log))
    lines: list[str] = []
    t0 = time.time()
    while thread.is_alive():
        _drain(log_q, lines)
        yield t("android.verify.running", name=e.name, elapsed=core.format_eta(time.time() - t0)), keep, keep, keep
        time.sleep(0.7)
    _drain(log_q, lines)
    table, dd, summary = android_mgr_refresh()
    if latest["error"]:
        yield t("android.verify.failed", name=e.name, detail=str(latest["error"]).splitlines()[0]), table, dd, summary
        return
    r: dict = latest["result"]  # type: ignore[assignment]
    msg = t("android.verify.ok", name=e.name, detail=r.get("detail", "")) if r.get("ok") else t("android.verify.failed", name=e.name, detail=r.get("detail", ""))
    yield msg, table, dd, summary


def android_mgr_uninstall(choice: str):
    e = _mgr_pick(choice)
    if e is None:
        return t("android.mgr.pick_one")
    sdk = _ANDROID.get("sdk") if isinstance(_ANDROID.get("sdk"), android.SdkInfo) else android.sdk_with_adb()
    if sdk is None:
        return t("android.mgr.no_adb")
    if not android.adb_devices(sdk):
        return t("android.no_device")
    pkg = str(e.data.get("package") or "")
    if not pkg:
        return t("android.mgr.no_package")
    rc, out = android.adb_uninstall(sdk, pkg, lambda _m: None)
    return t("android.mgr.uninstalled", pkg=pkg) if rc == 0 else t("android.mgr.uninstall_failed", pkg=pkg, out=out[-300:])


def android_cache_refresh():
    """Sorties : table des caches, dropdown, résumé."""
    current = str(_ANDROID.get("sdk_version") or "")
    try:
        caches = android.list_caches(current)
    except Exception as exc:
        return gr.update(value=[]), gr.update(choices=[], value=None), t("err.plain", err=exc)
    _MGR["caches"] = caches
    rows = [[CACHE_KIND_LABELS.get(c.kind, c.kind), c.name, core.human_size(c.bytes), t("android.yes") if c.in_use else ""] for c in caches]
    labels = [f"{CACHE_KIND_LABELS.get(c.kind, c.kind)} — {c.name} ({core.human_size(c.bytes)})" for c in caches]
    total = sum(c.bytes for c in caches)
    keys = android.KEYS_DIR / "android.keystore"
    summary = t("android.cache.summary", size=core.human_size(total), dir=android.ANDROID_ROOT, keys=(t("android.yes") if keys.is_file() else t("android.no")))
    return gr.update(value=rows), gr.update(choices=labels, value=None), summary


def android_cache_delete(choice: str, confirm: bool):
    caches: list[android.CacheEntry] = _MGR.get("caches", [])  # type: ignore[assignment]
    labels = [f"{CACHE_KIND_LABELS.get(c.kind, c.kind)} — {c.name} ({core.human_size(c.bytes)})" for c in caches]
    if choice not in labels:
        return t("android.mgr.pick_one"), gr.update(), gr.update(), gr.update()
    if not confirm:
        return t("android.mgr.confirm_first"), gr.update(), gr.update(), gr.update()
    c = caches[labels.index(choice)]
    if c.in_use and c.kind == "sdk":
        _ANDROID["sdk"], _ANDROID["jdk"] = None, None
    ok = android.delete_cache(c.path)
    table, dd, summary = android_cache_refresh()
    msg = t("android.cache.deleted", name=c.name, size=core.human_size(c.bytes)) if ok else t("android.cache.delete_failed", name=c.name)
    return msg, table, dd, summary


def android_export_keys():
    if not (android.KEYS_DIR / "android.keystore").is_file():
        return t("android.keys.none", dir=android.KEYS_DIR)
    dest = core.pick_folder(t("android.keys.pick"), "")
    if not dest:
        return t("android.keys.cancelled")
    try:
        done = android.export_keys(Path(dest))
    except Exception as exc:
        return t("err.plain", err=exc)
    return t("android.keys.exported", n=len(done), dir=dest)


def android_open_out():
    r = _ANDROID.get("build")
    if isinstance(r, android.BuildResult) and r.out_dir:
        android.open_folder(r.out_dir)
    return gr.update()


def android_open_keys():
    android.KEYS_DIR.mkdir(parents=True, exist_ok=True)
    android.open_folder(android.KEYS_DIR)
    return gr.update()


def _device_md(info: dict | None) -> str:
    if not info:
        return t("android.no_device")
    return t("android.device_info", model=info.get("model", "?"), android=info.get("android", "?"), sdk=info.get("sdk_int", "?"),
             abis=", ".join(info.get("abis") or []) or "?", serial=info.get("serial", "?"))


def _sdk_for_adb() -> android.SdkInfo | None:
    sdk = _ANDROID.get("sdk")
    return sdk if isinstance(sdk, android.SdkInfo) and sdk.adb.is_file() else android.sdk_with_adb()


def android_devices():
    sdk = _sdk_for_adb()
    if sdk is None:
        return t("android.need_env")
    return _device_md(android.adb_device_info(sdk))


def _pick_for_device(files: list, info: dict, legacy: bool) -> tuple:
    """(apk, message d'erreur) : APK compatible avec l'appareil, sinon explication (ABI acceptées / présentes, route arm64)."""
    apk, abi = android.pick_apk_for_device(files, info.get("abis") or [])
    if apk is not None:
        return apk, ""
    msg = t("android.install_no_abi", model=info.get("model", "?"), device_abis=", ".join(info.get("abis") or []) or "?", build_abis=abi)
    if legacy:
        msg += " " + t("android.install_no_abi_legacy", sdk=android.ARM64_LEGACY_SDK)
    return None, msg


def android_install():
    sdk, r, cfg = _sdk_for_adb(), _ANDROID.get("build"), _ANDROID.get("cfg")
    if sdk is None or not isinstance(r, android.BuildResult) or not r.files:
        return t("android.need_env")
    info = android.adb_device_info(sdk)
    if not info:
        return t("android.no_device")
    a = _ANDROID.get("analysis")
    legacy = isinstance(a, android.AndroidAnalysis) and a.family == "legacy" and not (isinstance(cfg, android.BuildConfig) and cfg.arm64_legacy)
    apk, msg = _pick_for_device(r.files, info, legacy)
    if apk is None:
        return _device_md(info) + "  \n" + msg
    rc, out = android.adb_install(sdk, apk, lambda _m: None)
    if rc != 0:
        return _device_md(info) + "  \n" + t("android.install_failed", rc=rc, out=out[-400:])
    return _device_md(info) + "  \n" + t("android.install_done", device=info.get("model", info.get("serial", "?"))) + " " + t("android.install_picked", apk=apk.name, abi=android.apk_abi(apk))


def android_launch():
    sdk, cfg = _sdk_for_adb(), _ANDROID.get("cfg")
    if sdk is None or not isinstance(cfg, android.BuildConfig):
        return t("android.need_env")
    if not android.adb_devices(sdk):
        return t("android.no_device")
    pkg = cfg.package.strip().lower()
    rc, out = android.adb_launch(sdk, pkg, lambda _m: None)
    return t("android.launch_done", pkg=pkg) if rc == 0 else t("android.launch_failed", out=out[-300:])


def android_mgr_launch(choice: str):
    e = _mgr_pick(choice)
    if e is None:
        return t("android.mgr.pick_one")
    sdk = _sdk_for_adb()
    if sdk is None:
        return t("android.mgr.no_adb")
    if not android.adb_devices(sdk):
        return t("android.no_device")
    pkg = str(e.data.get("package") or "")
    rc, out = android.adb_launch(sdk, pkg, lambda _m: None)
    return t("android.launch_done", pkg=pkg) if rc == 0 else t("android.launch_failed", out=out[-300:])


def android_icon_browse(current: str):
    initial = str(Path(_clean_path(current)).parent) if _clean_path(current) else ""
    return core.pick_file(t("android.cfg.icon"), core.IMAGE_DIALOG_FILTER, initial) or current


def android_sdk_browse(current: str):
    return core.pick_folder(t("android.manual_sdk"), _clean_path(current)) or current


# ----------------------------------------------------------------------------
# Interface
# ----------------------------------------------------------------------------
CSS = """
#log textarea, #analysis-log textarea { font-family: Consolas, monospace; font-size: 12px; }
#rhd-header { align-items: center; }
#rhd-header h1 { margin: 0 0 2px 0; font-size: 1.6rem; }
#rhd-header .rhd-version { font-size: 0.8rem; color: var(--body-text-color-subdued); margin-left: 8px; font-weight: 400; }
.rhd-step { border: 1px solid var(--border-color-primary) !important; border-radius: 12px !important; padding: 16px 20px !important;
            background: var(--block-background-fill) !important; margin-bottom: 10px !important; gap: 10px !important; }
.rhd-title h3 { display: flex; align-items: center; gap: 12px; margin: 0 0 4px 0; font-size: 1.3rem; font-weight: 600; }
.rhd-title .num { display: inline-flex; width: 32px; height: 32px; border-radius: 50%; align-items: center; justify-content: center;
                  background: var(--button-primary-background-fill); color: var(--button-primary-text-color); font-weight: 700; font-size: 15px; }
.rhd-hint, .rhd-hint p { color: var(--body-text-color-subdued) !important; font-size: 0.92rem !important; }
#go-btn { min-height: 64px !important; font-size: 1.35rem !important; font-weight: 700 !important; }
#browse-btn, #continue-btn, #rpa-btn { min-height: 46px !important; font-size: 1.05rem !important; font-weight: 600 !important; }
.rhd-bar { height: 14px; border-radius: 7px; background: var(--border-color-primary); overflow: hidden; margin-top: 6px; }
.rhd-bar-fill { height: 100%; border-radius: 7px; background: var(--button-primary-background-fill); transition: width .4s; }
.rhd-bar-done { background: #16a34a; }
.rhd-bar-label { font-size: 0.85rem; color: var(--body-text-color-subdued); margin-top: 4px; font-family: Consolas, monospace; }
#expert > .label-wrap { font-weight: 600; }
#lang-dd { min-width: 190px; }
"""


def _step_title(n: int, text: str) -> str:
    return f'<div class="rhd-title"><h3><span class="num">{n}</span><span>{text}</span></h3></div>'


def build_ui() -> gr.Blocks:
    global INPUT_KEYS
    inputs: dict[str, gr.components.Component] = {}
    lang_choices = [(f"{i18n.LANGUAGES[c][0]} {i18n.LANGUAGES[c][1]}", c) for c in i18n.available()]

    with gr.Blocks(title="RenPyHD") as demo:
        with gr.Row(elem_id="rhd-header"):
            gr.Markdown(f"# RenPyHD <span class=\"rhd-version\">v{APP_VERSION}</span>\n" + t("app.subtitle") + " — GPU : "
                        f"**{_STATE['gpu'] or t('app.gpu_unavailable')}**"
                        + (f" — ⚠️ {_STATE['runtime_error']}" if _STATE["runtime_error"] else ""), scale=6)
            lang_dd = gr.Dropdown(lang_choices, value=UI_LANG, label=t("lang.label"), scale=0, min_width=200, elem_id="lang-dd")
            quit_btn = gr.Button(t("app.quit"), variant="stop", scale=0, min_width=110)
        with gr.Row():
            top_msg = gr.Markdown("", scale=6)
            restart_btn = gr.Button(t("lang.restart_now"), variant="primary", scale=0, min_width=220, visible=False)
        with gr.Tabs() as tabs:
            # ---------------------------------------------------------- Onglet 1 : cinq étapes
            with gr.Tab(t("tab.main"), id="tab_main"):
                # ---- Étape 1 : choisir le jeu (analyse automatique)
                with gr.Column(elem_classes=["rhd-step"]):
                    gr.HTML(_step_title(1, t("step1.title")))
                    with gr.Row() as game_row:
                        inputs["game_root"] = gr.Textbox(label=t("step1.game_root"), scale=5, lines=1, max_lines=1, placeholder=r"D:\Games\MyGame-pc")
                        browse_game = gr.Button(t("common.browse"), variant="primary", scale=1, min_width=170, elem_id="browse-btn")
                    with gr.Row(visible=False) as folder_row:
                        inputs["input_dir"] = gr.Textbox(label=t("step1.input_dir"), scale=3, lines=1, max_lines=1)
                        browse_in = gr.Button(t("common.browse"), scale=1, min_width=110)
                        inputs["output_dir"] = gr.Textbox(label=t("step1.output_dir"), scale=3, lines=1, max_lines=1)
                        browse_out = gr.Button(t("common.browse"), scale=1, min_width=110)
                        analyze_btn2 = gr.Button(t("step1.analyze"), variant="primary", scale=1, min_width=110)
                    step1_summary = gr.Markdown(STEP1_HINT, elem_classes=["rhd-hint"])

                # ---- Étape 2 : archives .rpa
                with gr.Column(elem_classes=["rhd-step"]):
                    gr.HTML(_step_title(2, t("step2.title")))
                    rpa_md = gr.Markdown(t("step2.hint"), elem_classes=["rhd-hint"])
                    with gr.Row(visible=False) as rpa_actions:
                        rpa_extract_btn = gr.Button(t("step2.extract"), variant="primary", scale=2, min_width=260, elem_id="rpa-btn")
                        rpa_skip_btn = gr.Button(t("step2.skip"), variant="secondary", scale=1, min_width=200)
                    rpa_bar = gr.HTML("")
                    rpa_status = gr.Markdown("")
                    with gr.Accordion(t("common.details_log"), open=False):
                        rpa_log = gr.Textbox(label=t("common.log"), lines=8, max_lines=8, interactive=False, autoscroll=True, elem_id="log")

                # ---- Étape 3 : que voulez-vous améliorer ?
                with gr.Column(elem_classes=["rhd-step"]):
                    gr.HTML(_step_title(3, t("step3.title")))
                    step3_hint = gr.Markdown(t("step3.hint"), elem_classes=["rhd-hint"])
                    with gr.Column(visible=False) as step3_body:
                        with gr.Row():
                            inputs["images_enabled"] = gr.Checkbox(True, label=t("step3.images"), scale=1)
                            inputs["video_enabled"] = gr.Checkbox(False, label=t("step3.videos"), scale=1)
                        video_note = gr.Markdown("", elem_classes=["rhd-hint"])
                        with gr.Row():
                            inputs["preset"] = gr.Dropdown(list(PRESET_LABELS), value=t("preset.faces"), label=t("step3.preset"), info=t("step3.preset_info"))
                            inputs["factor"] = gr.Dropdown(FACTOR_CHOICES, value=2.0, label=t("step3.factor"), info=t("step3.factor_info"))
                        continue_btn = gr.Button(t("step3.continue"), variant="primary", elem_id="continue-btn")
                        gr.Markdown(t("step3.continue_hint"), elem_classes=["rhd-hint"])

                # ---- Étape 4 : aperçu avant/après
                with gr.Column(elem_classes=["rhd-step"]):
                    gr.HTML(_step_title(4, t("step4.title")))
                    step4_hint = gr.Markdown(t("step4.hint"), elem_classes=["rhd-hint"])
                    with gr.Column(visible=False) as step4_body:
                        preview_status = gr.Markdown("")
                        with gr.Row():
                            with gr.Column(scale=3):
                                pv_slider = gr.ImageSlider(type="pil", label=t("common.slider_label"), max_height=560)
                            with gr.Column(scale=1, min_width=250):
                                pv_pick = gr.Dropdown([], label=t("step4.pick"))
                                with gr.Row():
                                    pv_prev = gr.Button(t("common.prev"), min_width=100)
                                    pv_next = gr.Button(t("common.next"), min_width=100)
                                preview_btn = gr.Button(t("step4.regenerate"), variant="secondary")
                                gr.Markdown(t("step4.regenerate_hint"), elem_classes=["rhd-hint"])
                        pv_video_md = gr.Markdown("")
                        with gr.Row():
                            pv_video_before = gr.Video(label=t("step4.video_before"), interactive=False, visible=False, height=300)
                            pv_video_after = gr.Video(label=t("step4.video_after"), interactive=False, visible=False, height=300)
                        estimate_md = gr.Markdown("")
                        preview_warn = gr.Markdown("")
                        with gr.Accordion(t("step4.loupe_accordion"), open=False):
                            pv_info = gr.Markdown("")
                            with gr.Row():
                                pv_x = gr.Slider(0, 100, value=50, step=1, label=t("loupe.x"))
                                pv_y = gr.Slider(0, 100, value=50, step=1, label=t("loupe.y"))
                                pv_crop = gr.Slider(64, 512, value=200, step=8, label=t("loupe.zone"))
                            with gr.Row():
                                pv_crop_b = gr.Image(type="pil", label=t("loupe.before"), interactive=False)
                                pv_crop_a = gr.Image(type="pil", label=t("loupe.after"), interactive=False)

                # ---- Étape 5 : améliorer le jeu (progression, état final)
                with gr.Column(elem_classes=["rhd-step"]):
                    gr.HTML(_step_title(5, t("step5.title")))
                    step5_hint = gr.Markdown(t("step5.hint"), elem_classes=["rhd-hint"])
                    with gr.Column(visible=False) as step5_body:
                        validate_btn = gr.Button(t("step5.go"), variant="primary", interactive=False, elem_id="go-btn")
                        gr.Markdown(t("step5.go_hint"), elem_classes=["rhd-hint"])
                        progress_html = gr.HTML("")
                        status_md = gr.Markdown("")
                        with gr.Column(visible=False) as done_group:
                            done_md = gr.Markdown("")
                            with gr.Row():
                                play_btn = gr.Button(t("step5.play"), variant="primary", min_width=160, elem_id="play-btn")
                                compare_btn = gr.Button(t("step5.compare"), min_width=200)
                                reset_btn = gr.Button(t("step5.another"), min_width=200)
                        with gr.Row():
                            cancel_btn = gr.Button(t("common.cancel"), variant="stop", size="sm", scale=0, min_width=130)
                            uninstall_btn = gr.Button(t("step5.uninstall"), size="sm", scale=0, min_width=330, visible=True)
                            restore_btn = gr.Button(t("step5.restore"), size="sm", scale=0, min_width=300, visible=False)
                        with gr.Accordion(t("step5.details"), open=False):
                            log_box = gr.Textbox(label=t("common.log"), lines=16, max_lines=16, interactive=False, autoscroll=True, elem_id="log")
                            failures_df = gr.Dataframe(headers=[t("common.file"), t("common.error")], datatype=["str", "str"], label=t("step5.failures"),
                                                       interactive=False, wrap=True)

                # ---- Mode expert : tout le reste, replié
                with gr.Accordion(t("expert.title"), open=False, elem_id="expert"):
                    gr.Markdown(t("expert.hint"), elem_classes=["rhd-hint"])
                    with gr.Accordion(t("expert.mode"), open=True):
                        inputs["mode"] = gr.Radio(list(MODE_LABELS), value=t("mode.hd"), label=t("expert.mode_label"))
                        mode_help = gr.Markdown(_mode_help(core.MODE_HD))
                        with gr.Row(visible=True) as hook_row:
                            inputs["out_name"] = gr.Textbox("hd2x", label=t("expert.out_name"), lines=1, max_lines=1)
                            inputs["install_hook"] = gr.Checkbox(True, label=t("expert.install_hook"))
                            inputs["cache_mb"] = gr.Slider(256, 8192, value=1536, step=256, label=t("expert.cache_mb"))
                    with gr.Accordion(t("expert.nr"), open=False):
                        gr.Markdown(t("expert.nr_hint"), elem_classes=["rhd-hint"])
                        with gr.Row():
                            inputs["quality"] = gr.Slider(1, 100, value=92, step=1, label=t("expert.quality"))
                            inputs["nr_style"] = gr.Radio(list(core.NR_STYLES), value="Default", label=t("expert.nr_style"))
                            inputs["nr_preset"] = gr.Dropdown(list(core.NR_PRESETS), value="Default", label=t("expert.nr_preset"), info=t("expert.nr_preset_info"))
                            inputs["dlss_model_preset"] = gr.Dropdown(list(core.DLSS_MODEL_PRESETS), value="Default", label=t("expert.model"), info=t("expert.model_info"))
                        with gr.Row():
                            inputs["nr_intensity"] = gr.Slider(0.0, 2.0, value=2.0, step=0.05, label=t("expert.nr_intensity"))
                            inputs["local_tone"] = gr.Slider(0.0, 2.0, value=2.0, step=0.05, label=t("expert.local_tone"))
                        with gr.Row():
                            inputs["local_structure"] = gr.Slider(0.0, 2.0, value=2.0, step=0.05, label=t("expert.local_structure"))
                            inputs["skin_structure"] = gr.Slider(-1.0, 2.0, value=2.0, step=0.05, label=t("expert.skin_structure"))
                        with gr.Row():
                            inputs["automatic_mask"] = gr.Checkbox(False, label=t("expert.auto_mask"))
                            inputs["warmup_frames"] = gr.Slider(0, 8, value=0, step=1, label=t("expert.warmup"))
                            inputs["preserve_metadata"] = gr.Checkbox(False, label=t("expert.metadata"))
                    with gr.Accordion(t("expert.formats"), open=False):
                        with gr.Row():
                            inputs["jpeg_as"] = gr.Dropdown(list(core.RENPY_FORMATS), value="JPEG", label=t("expert.jpeg_as"))
                            inputs["png_as"] = gr.Dropdown(list(core.RENPY_FORMATS), value="PNG", label=t("expert.png_as"))
                            inputs["webp_as"] = gr.Dropdown(list(core.RENPY_FORMATS), value="WebP", label=t("expert.webp_as"))
                    with gr.Accordion(t("expert.selection"), open=False):
                        with gr.Row():
                            inputs["chunk"] = gr.Slider(10, 1000, value=300, step=10, label=t("expert.chunk"))
                            inputs["limit"] = gr.Number(0, precision=0, label=t("expert.limit"))
                            inputs["path_filter"] = gr.Textbox("", label=t("expert.path_filter"), placeholder="Character/")
                        with gr.Row():
                            inputs["extensions"] = gr.CheckboxGroup(list(core.IMAGE_EXTS), value=list(core.IMAGE_EXTS), label=t("expert.extensions"))
                            inputs["exclude_prefixes"] = gr.Textbox("gui/", label=t("expert.exclude_prefixes"))
                        with gr.Row():
                            inputs["include_regex"] = gr.Textbox("", label=t("expert.include_regex"), placeholder=r"^(bg|cg)/")
                            inputs["exclude_regex"] = gr.Textbox("", label=t("expert.exclude_regex"), placeholder=r"_thumb\.")
                        with gr.Row():
                            inputs["scan_mode"] = gr.Radio(list(SCAN_LABELS), value=t("scan.auto"), label=t("expert.scan_mode"), info=t("expert.scan_mode_info"))
                        with gr.Row():
                            inputs["min_dim"] = gr.Number(256, precision=0, label=t("expert.min_dim"))
                            inputs["max_dim"] = gr.Number(0, precision=0, label=t("expert.max_dim"))
                        with gr.Row():
                            inputs["use_rpa"] = gr.Checkbox(True, label=t("expert.use_rpa"))
                            inputs["overwrite"] = gr.Checkbox(False, label=t("expert.overwrite"))
                            inputs["retry_failed"] = gr.Checkbox(True, label=t("expert.retry_failed"))
                            inputs["dry_run"] = gr.Checkbox(False, label=t("expert.dry_run"))
                        analyze_btn = gr.Button(t("expert.analyze"), variant="secondary")
                        info_md = gr.Markdown(t("expert.report_placeholder"))
                        analysis_log = gr.Textbox(label=t("expert.analysis_log"), lines=10, max_lines=10, interactive=False, elem_id="analysis-log")
                    with gr.Accordion(t("expert.videos"), open=False, visible=True) as video_group:
                        gr.Markdown(t("expert.videos_hint"), elem_classes=["rhd-hint"])
                        with gr.Row():
                            inputs["video_codec"] = gr.Dropdown(list(VIDEO_CODEC_LABELS), value=list(VIDEO_CODEC_LABELS)[0], label=t("expert.codec"), info=t("expert.codec_info"))
                            inputs["video_crf"] = gr.Slider(0, 63, value=31, step=1, label=t("expert.crf"), info=t("expert.crf_info"))
                            inputs["video_speed"] = gr.Slider(0, 5, value=2, step=1, label=t("expert.speed"))
                        with gr.Row():
                            inputs["video_cap"] = gr.Dropdown(list(VIDEO_CAP_LABELS), value="3840×2160", label=t("expert.cap"), info=t("expert.cap_info"))
                            inputs["video_over_cap"] = gr.Radio(list(OVER_CAP_LABELS), value=list(OVER_CAP_LABELS)[0], label=t("expert.over_cap"))
                            inputs["video_keep_audio"] = gr.Checkbox(True, label=t("expert.keep_audio"))
                            inputs["video_audio"] = gr.Slider(32, 320, value=128, step=16, label=t("expert.audio_kbps"))
                        with gr.Row():
                            inputs["video_scene_reset"] = gr.Checkbox(True, label=t("expert.scene_reset"))
                            inputs["video_warmup"] = gr.Slider(0, 8, value=0, step=1, label=t("expert.video_warmup"))
                            inputs["video_hw"] = gr.Checkbox(True, label=t("expert.nvenc"))
                            inputs["video_inter_quality"] = gr.Dropdown(list(INTERMEDIATE_QUALITIES), value="Max", label=t("expert.inter_quality"))
                        inputs["video_share_nr"] = gr.Checkbox(True, label=t("expert.share_nr"))
                        with gr.Group(visible=False) as video_nr_group:
                            with gr.Row():
                                inputs["video_nr_style"] = gr.Radio(list(core.NR_STYLES), value="Default", label=t("expert.v_nr_style"))
                                inputs["video_nr_preset"] = gr.Dropdown(list(core.NR_PRESETS), value="Default", label=t("expert.v_nr_preset"))
                                inputs["video_model_preset"] = gr.Dropdown(list(core.DLSS_MODEL_PRESETS), value="Default", label=t("expert.v_model"))
                                inputs["video_automatic_mask"] = gr.Checkbox(False, label=t("expert.v_auto_mask"))
                            with gr.Row():
                                inputs["video_nr_intensity"] = gr.Slider(0.0, 2.0, value=1.0, step=0.05, label=t("expert.v_nr_intensity"))
                                inputs["video_local_tone"] = gr.Slider(0.0, 2.0, value=1.0, step=0.05, label=t("expert.v_local_tone"))
                                inputs["video_local_structure"] = gr.Slider(0.0, 2.0, value=1.0, step=0.05, label=t("expert.v_local_structure"))
                                inputs["video_skin_structure"] = gr.Slider(-1.0, 2.0, value=-1.0, step=0.05, label=t("expert.v_skin_structure"))
                    with gr.Accordion(t("expert.preview"), open=False):
                        with gr.Row():
                            inputs["preview_count"] = gr.Number(DEFAULT_PREVIEW_COUNT, precision=0, label=t("expert.preview_count"), minimum=1, maximum=20)
                            inputs["preview_mode"] = gr.Radio(list(PREVIEW_MODES), value=t("preview.random"), label=t("expert.preview_mode"))
                            inputs["preview_choice"] = gr.Dropdown([], multiselect=True, label=t("expert.preview_choice"), filterable=True, scale=3)
                    with gr.Row():
                        launch_btn = gr.Button(t("expert.launch_no_preview"), variant="secondary")
                        save_btn = gr.Button(t("expert.save_cfg"), size="sm")
                        load_btn = gr.Button(t("expert.load_cfg"), size="sm")
                        cfg_msg = gr.Markdown("")

            # ---------------------------------------------------------- Onglet 2 : Comparer / Tester
            with gr.Tab(t("tab.compare"), id="tab_compare"):
                with gr.Tabs():
                    with gr.Tab(t("compare.tab"), id="sub_compare"):
                        gr.Markdown(t("compare.hint"), elem_classes=["rhd-hint"])
                        with gr.Row():
                            v_root = gr.Textbox(label=t("compare.game_root"), scale=4, lines=1, max_lines=1)
                            v_browse = gr.Button(t("common.browse"), scale=1, min_width=110)
                            v_kind = gr.Radio(list(KIND_LABELS), value=list(KIND_LABELS)[0], label=t("compare.kind"), scale=4)
                        with gr.Row():
                            v_out = gr.Textbox("hd2x", label=t("compare.out_name"), scale=1, lines=1, max_lines=1)
                            v_in_dir = gr.Textbox(label=t("compare.in_dir"), scale=2, lines=1, max_lines=1)
                            v_out_dir = gr.Textbox(label=t("compare.out_dir"), scale=2, lines=1, max_lines=1)
                            v_load = gr.Button(t("compare.load"), variant="primary", scale=1)
                        v_count = gr.Markdown("")
                        with gr.Row():
                            v_prev = gr.Button(t("common.prev"), scale=1)
                            v_pick = gr.Dropdown([], label=t("compare.pick"), filterable=True, scale=6)
                            v_next = gr.Button(t("common.next"), scale=1)
                        v_time = gr.Slider(0, 100, value=50, step=1, label=t("compare.time"))
                        v_info = gr.Markdown("")
                        v_slider = gr.ImageSlider(type="pil", label=t("common.slider_label"), max_height=720)
                        with gr.Row():
                            v_before_video = gr.Video(label=t("compare.video_before"), interactive=False, visible=False, height=360)
                            v_after_video = gr.Video(label=t("compare.video_after"), interactive=False, visible=False, height=360)
                        with gr.Accordion(t("compare.loupe_accordion"), open=True):
                            with gr.Row():
                                v_x = gr.Slider(0, 100, value=50, step=1, label=t("compare.pos_x"))
                                v_y = gr.Slider(0, 100, value=50, step=1, label=t("compare.pos_y"))
                                v_crop = gr.Slider(64, 512, value=200, step=8, label=t("compare.zone"))
                            with gr.Row():
                                v_crop_b = gr.Image(type="pil", label=t("compare.crop_before"), interactive=False)
                                v_crop_a = gr.Image(type="pil", label=t("compare.crop_after"), interactive=False)

                    with gr.Tab(t("single.tab"), id="sub_image"):
                        gr.Markdown(t("single.hint"), elem_classes=["rhd-hint"])
                        with gr.Row():
                            t_path = gr.Textbox(label=t("single.path"), scale=5, lines=1, max_lines=1, placeholder=r"D:\...\image.png")
                            t_browse = gr.Button(t("common.browse"), scale=1, min_width=110)
                            t_run = gr.Button(t("single.run"), variant="primary", scale=1, min_width=140)
                        with gr.Row():
                            t_upload = gr.File(label=t("single.drop"), file_types=["image"], type="filepath", height=90, scale=2)
                            t_paste = gr.Button(t("single.paste"), scale=1, min_width=160)
                        t_status = gr.Markdown("")
                        t_info = gr.Markdown("")
                        t_slider = gr.ImageSlider(type="pil", label=t("common.slider_label"), max_height=640)
                        with gr.Row():
                            t_x = gr.Slider(0, 100, value=50, step=1, label=t("loupe.x"))
                            t_y = gr.Slider(0, 100, value=50, step=1, label=t("loupe.y"))
                            t_crop = gr.Slider(64, 512, value=200, step=8, label=t("loupe.zone"))
                        with gr.Row():
                            t_crop_b = gr.Image(type="pil", label=t("loupe.before"), interactive=False)
                            t_crop_a = gr.Image(type="pil", label=t("loupe.after"), interactive=False)
                        with gr.Row():
                            t_save = gr.Button(t("single.save"), interactive=False)
                            t_save_msg = gr.Markdown("")

                    with gr.Tab(t("video.tab"), id="sub_video"):
                        gr.Markdown(t("video.hint"), elem_classes=["rhd-hint"])
                        with gr.Row():
                            tv_path = gr.Textbox(label=t("video.path"), scale=5, lines=1, max_lines=1, placeholder=r"D:\...\game\movies\intro.webm")
                            tv_browse = gr.Button(t("common.browse"), scale=1, min_width=110)
                            tv_limit = gr.Number(5, precision=1, label=t("video.limit"), minimum=0, scale=1)
                            tv_run = gr.Button(t("single.run"), variant="primary", scale=1, min_width=140)
                        tv_upload = gr.File(label=t("video.drop"), file_types=["video"], type="filepath", height=90)
                        tv_status = gr.Markdown("")
                        with gr.Row():
                            tv_pick = gr.Dropdown([], label=t("video.pick"), scale=3)
                            tv_x = gr.Slider(0, 100, value=50, step=1, label=t("loupe.x"))
                            tv_y = gr.Slider(0, 100, value=50, step=1, label=t("loupe.y"))
                            tv_crop = gr.Slider(64, 512, value=200, step=8, label=t("loupe.zone"))
                        tv_info = gr.Markdown("")
                        tv_slider = gr.ImageSlider(type="pil", label=t("common.slider_label"), max_height=640)
                        with gr.Row():
                            tv_crop_b = gr.Image(type="pil", label=t("loupe.before"), interactive=False)
                            tv_crop_a = gr.Image(type="pil", label=t("loupe.after"), interactive=False)
                        with gr.Row():
                            tv_before = gr.Video(label=t("video.before"), interactive=False, height=360)
                            tv_after = gr.Video(label=t("video.after"), interactive=False, height=360)
                        with gr.Row():
                            tv_save = gr.Button(t("video.save"))
                            tv_save_msg = gr.Markdown("")

            # ---------------------------------------------------------- Onglet 3 : Outils
            with gr.Tab(t("tab.tools"), id="tab_tools"):
                with gr.Tabs() as tools_tabs:
                    # ---- Extraire les archives (.rpa)
                    with gr.Tab(t("tools.rpa.tab"), id="sub_rpa"):
                        with gr.Column(elem_classes=["rhd-step"]):
                            gr.HTML(_step_title(1, t("step1.title")))
                            with gr.Row():
                                x_root = gr.Textbox(label=t("step1.game_root"), scale=5, lines=1, max_lines=1, placeholder=r"D:\Games\MyGame-pc")
                                x_browse = gr.Button(t("common.browse"), variant="primary", scale=1, min_width=170)
                                x_scan = gr.Button(t("tools.rpa.scan"), scale=1, min_width=190)
                            x_summary = gr.Markdown(STEP_X1_HINT, elem_classes=["rhd-hint"])
                        with gr.Column(elem_classes=["rhd-step"]):
                            gr.HTML(_step_title(2, t("tools.rpa.step2_title")))
                            with gr.Column(visible=False) as x_step2:
                                x_list = gr.CheckboxGroup([], label=t("tools.rpa.list"))
                                with gr.Row():
                                    x_skip = gr.Checkbox(True, label=t("tools.rpa.skip_existing"))
                                    x_bak = gr.Checkbox(False, label=t("tools.rpa.rename_bak"))
                                x_engine = gr.Radio(list(RPA_ENGINES), value=list(RPA_ENGINES)[0], label=t("tools.rpa.engine"), visible=tools.RPAEXTRACT.is_file())
                                gr.Markdown(t("tools.rpa.step2_hint"), elem_classes=["rhd-hint"])
                        with gr.Column(elem_classes=["rhd-step"]):
                            gr.HTML(_step_title(3, t("tools.rpa.step3_title")))
                            with gr.Column(visible=False) as x_step3:
                                x_go = gr.Button(t("tools.rpa.go"), variant="primary", elem_id="go-btn")
                                x_bar = gr.HTML("")
                                x_status = gr.Markdown("")
                                with gr.Column(visible=False) as x_done:
                                    x_to_main = gr.Button(t("tools.rpa.to_main"), min_width=260)
                                with gr.Row():
                                    x_cancel = gr.Button(t("common.cancel"), variant="stop", size="sm", scale=0, min_width=130)
                                with gr.Accordion(t("common.details_log"), open=False):
                                    x_log = gr.Textbox(label=t("common.log"), lines=12, max_lines=12, interactive=False, autoscroll=True, elem_id="log")

                    # ---- Traduire le jeu
                    with gr.Tab(t("tools.tl.tab"), id="sub_tl"):
                        with gr.Column(elem_classes=["rhd-step"]):
                            gr.HTML(_step_title(1, t("tools.tl.step1_title")))
                            with gr.Row():
                                t_root = gr.Textbox(label=t("step1.game_root"), scale=5, lines=1, max_lines=1, placeholder=r"D:\Games\MyGame-pc")
                                t_browse2 = gr.Button(t("common.browse"), variant="primary", scale=1, min_width=170)
                                t_target = gr.Dropdown(TARGET_CHOICES, value=DEFAULT_TARGET, label=t("tools.tl.target"), scale=2)
                            with gr.Row():
                                t_gen = gr.Button(t("tools.tl.generate"), variant="primary", scale=1, min_width=220)
                                t_merge = gr.Checkbox(False, label=t("tools.tl.merge"), scale=3)
                            t_gen_status = gr.Markdown(STEP_T1_HINT, elem_classes=["rhd-hint"])
                            t_state = gr.Markdown("", elem_classes=["rhd-hint"])
                        with gr.Column(elem_classes=["rhd-step"]):
                            gr.HTML(_step_title(2, t("tools.tl.step2_title")))
                            t_step2_hint = gr.Markdown(t("tools.tl.step2_wait"), elem_classes=["rhd-hint"])
                            with gr.Column(visible=False) as t_step2:
                                gr.Markdown(t("tools.tl.step2_hint"), elem_classes=["rhd-hint"])
                                with gr.Row():
                                    t_export_scope = gr.Radio(list(SCOPE_LABELS), value=list(SCOPE_LABELS)[0], label=t("tools.tl.scope"), scale=2)
                                    t_export_only = gr.Radio([ONLY_UNTRANSLATED, ALL_TEXTS], value=ONLY_UNTRANSLATED, label=t("tools.tl.which"), scale=2)
                                with gr.Row():
                                    t_export_base = gr.Textbox("phrase", label=t("tools.tl.base_name"), lines=1, max_lines=1, scale=1)
                                    t_export_chunk = gr.Number(10000, precision=0, minimum=100, maximum=200000, label=t("tools.tl.lines_per_file"), scale=1)
                                    t_export_mb = gr.Number(1.0, precision=2, minimum=0, maximum=50, label=t("tools.tl.max_mb"), scale=1)
                                    t_export_idfmt = gr.Dropdown(list(ID_FORMAT_LABELS), value=list(ID_FORMAT_LABELS)[0], label=t("tools.tl.id_format"), scale=2)
                                    t_export_tags = gr.Checkbox(True, label=t("tools.tl.protect_tags"), scale=1)
                                t_export = gr.Button(t("tools.tl.export"), variant="primary", elem_id="go-btn")
                                t_export_msg = gr.Markdown("")
                        with gr.Column(elem_classes=["rhd-step"]):
                            gr.HTML(_step_title(3, t("tools.tl.step3_title")))
                            t_step3_hint = gr.Markdown(t("tools.tl.step3_wait"), elem_classes=["rhd-hint"])
                            with gr.Column(visible=False) as t_step3:
                                with gr.Row():
                                    t_import_path = gr.Textbox(label=t("tools.tl.import_path"), scale=4, lines=2, max_lines=4)
                                    t_import_browse = gr.Button(t("common.browse"), scale=1, min_width=110)
                                    t_import = gr.Button(t("tools.tl.import"), variant="primary", scale=1, min_width=240)
                                gr.Markdown(t("tools.tl.import_hint"), elem_classes=["rhd-hint"])
                                t_status = gr.Markdown("")
                                with gr.Column(visible=False) as t_review:
                                    gr.Markdown(t("tools.tl.review_hint"), elem_classes=["rhd-hint"])
                                    t_table = gr.Dataframe(headers=[t("common.file"), t("tools.tl.col_line"), t("tools.tl.col_source"), t("tools.tl.col_translation")],
                                                           datatype=["str", "number", "str", "str"], type="array", interactive=True, wrap=True,
                                                           static_columns=[0, 1, 2], column_widths=["14%", "6%", "40%", "40%"], max_height=420,
                                                           label=t("tools.tl.sample"))
                                    with gr.Row():
                                        t_fix = gr.Button(t("tools.tl.save_fixes"), min_width=220)
                                        t_fix_msg = gr.Markdown("")
                                t_install = gr.Button(t("tools.tl.install"), variant="primary", elem_id="go-btn")
                                gr.Markdown(t("tools.tl.install_hint", hook=tools.LANG_HOOK), elem_classes=["rhd-hint"])
                                t_install_status = gr.Markdown("")
                                with gr.Row():
                                    t_check = gr.Button(t("tools.tl.check"), min_width=220)
                                    t_uninstall = gr.Button(t("tools.tl.uninstall"), size="sm", scale=0, min_width=220)
                                    t_uninstall_tl = gr.Checkbox(True, label=t("tools.tl.uninstall_tl"), scale=0, min_width=330)
                                t_check_status = gr.Markdown("")

                    # ---- Android (APK)
                    with gr.Tab(t("android.tab"), id="sub_android"):
                        with gr.Column(elem_classes=["rhd-step"]):
                            gr.HTML(_step_title(1, t("step1.title")))
                            with gr.Row():
                                a_root = gr.Textbox(label=t("step1.game_root"), scale=5, lines=1, max_lines=1, placeholder=r"D:\Games\MyGame-pc")
                                a_browse = gr.Button(t("common.browse"), variant="primary", scale=1, min_width=170)
                                a_analyze = gr.Button(t("android.analyze"), scale=1, min_width=190)
                            a_summary = gr.Markdown(STEP_A1_HINT, elem_classes=["rhd-hint"])
                        with gr.Column(elem_classes=["rhd-step"]):
                            gr.HTML(_step_title(2, t("android.step2_title")))
                            a_step2_hint = gr.Markdown(t("android.step2_wait"), elem_classes=["rhd-hint"])
                            with gr.Column(visible=False) as a_step2:
                                gr.Markdown(t("android.step2_hint"), elem_classes=["rhd-hint"])
                                with gr.Row():
                                    a_sdk = gr.Dropdown([], label=t("android.sdk_choice"), scale=3, allow_custom_value=True)
                                    a_org = gr.Textbox("RenPyHD", label=t("android.org"), scale=2, lines=1, max_lines=1)
                                with gr.Row():
                                    a_manual_sdk = gr.Textbox(label=t("android.manual_sdk"), scale=5, lines=1, max_lines=1)
                                    a_manual_browse = gr.Button(t("common.browse"), scale=1, min_width=110)
                                a_unrpyc = gr.Checkbox(True, label=t("android.with_unrpyc"), visible=android.ANDROID_UNRPYC_ENABLED)
                                a_env_md = gr.Markdown("", elem_classes=["rhd-hint"])
                                a_prepare = gr.Button(t("android.prepare"), variant="primary", elem_id="go-btn")
                                a_prep_bar = gr.HTML("")
                                a_prep_status = gr.Markdown("")
                                a_keys_note = gr.Markdown("")
                                gr.Markdown(t("android.keys_warning"))
                                with gr.Row():
                                    a_open_keys = gr.Button(t("android.open_keys"), size="sm", scale=0, min_width=220)
                                    a_prep_cancel = gr.Button(t("common.cancel"), variant="stop", size="sm", scale=0, min_width=130)
                                with gr.Accordion(t("common.details_log"), open=False):
                                    a_prep_log = gr.Textbox(label=t("common.log"), lines=12, max_lines=12, interactive=False, autoscroll=True, elem_id="log")
                        with gr.Column(elem_classes=["rhd-step"]):
                            gr.HTML(_step_title(3, t("android.step3_title")))
                            a_step3_hint = gr.Markdown(t("android.step3_wait"), elem_classes=["rhd-hint"])
                            with gr.Column(visible=False) as a_step3:
                                gr.Markdown(t("android.step3_hint"), elem_classes=["rhd-hint"])
                                with gr.Row():
                                    a_name = gr.Textbox(label=t("android.cfg.name"), scale=3, lines=1, max_lines=1)
                                    a_icon_name = gr.Textbox(label=t("android.cfg.icon_name"), scale=2, lines=1, max_lines=1)
                                    a_package = gr.Textbox(label=t("android.cfg.package"), scale=3, lines=1, max_lines=1)
                                with gr.Row():
                                    a_version = gr.Textbox("1.0", label=t("android.cfg.version"), scale=1, lines=1, max_lines=1)
                                    a_numeric = gr.Number(100, precision=0, minimum=1, label=t("android.cfg.numeric"), scale=1)
                                    a_orientation = gr.Radio(list(ORIENTATION_LABELS), value=list(ORIENTATION_LABELS)[0], label=t("android.cfg.orientation"), scale=2)
                                    a_internet = gr.Checkbox(False, label=t("android.cfg.internet"), scale=1)
                                with gr.Row():
                                    a_icon = gr.Textbox(label=t("android.cfg.icon"), scale=5, lines=1, max_lines=1)
                                    a_icon_browse = gr.Button(t("common.browse"), scale=1, min_width=110)
                                a_data_mode = gr.Radio(list(DATA_MODE_LABELS), value=list(DATA_MODE_LABELS)[0], label=t("android.cfg.data_mode"))
                                gr.Markdown(t("android.cfg.data_hint"), elem_classes=["rhd-hint"])
                                a_ext_audio = gr.Checkbox(False, label=t("android.cfg.ext_audio", n=0, size="0"), visible=False)
                                gr.Markdown(t("android.cfg.images"), elem_classes=["rhd-hint"])
                                with gr.Row():
                                    a_budget = gr.Number(0, precision=0, minimum=0, label=t("android.cfg.budget"), scale=2)
                                    a_videos = gr.Checkbox(False, label=t("android.cfg.videos", n=0, size="0"), scale=2, visible=False)
                                a_skip_rpa = gr.Checkbox(True, label=t("android.cfg.skip_rpa", n=0, size="0"), visible=False)
                                a_prefer_rpyc = gr.Checkbox(False, label=t("android.cfg.prefer_rpyc"), visible=False)
                                a_decompile = gr.Checkbox(False, label=t("android.cfg.decompile"), visible=False)
                                a_bundle = gr.Checkbox(False, label=t("android.cfg.bundle"), visible=android.ANDROID_BUNDLE_ENABLED)
                                a_arm64 = gr.Checkbox(False, label=t("android.cfg.arm64", sdk=android.ARM64_LEGACY_SDK), visible=False)
                                a_estimate = gr.Markdown("", elem_classes=["rhd-hint"])
                        with gr.Column(elem_classes=["rhd-step"]):
                            gr.HTML(_step_title(4, t("android.step4_title")))
                            a_step4_hint = gr.Markdown(t("android.step4_wait"), elem_classes=["rhd-hint"])
                            with gr.Column(visible=False) as a_step4:
                                a_go = gr.Button(t("android.build"), variant="primary", elem_id="go-btn")
                                a_bar = gr.HTML("")
                                a_status = gr.Markdown("")
                                with gr.Column(visible=False) as a_done:
                                    a_done_md = gr.Markdown("")
                                    with gr.Row():
                                        a_open_out = gr.Button(t("android.open_folder"), min_width=200)
                                        a_install = gr.Button(t("android.install_adb"), min_width=260, visible=android.ANDROID_ADB_ENABLED)
                                        a_launch = gr.Button(t("android.launch_adb"), min_width=220, visible=android.ANDROID_ADB_ENABLED)
                                        a_push = gr.Button(t("android.push_adb"), min_width=300, visible=android.ANDROID_ADB_ENABLED)
                                        a_devices = gr.Button(t("android.refresh_devices"), size="sm", min_width=200, visible=android.ANDROID_ADB_ENABLED)
                                    a_device_md = gr.Markdown("")
                                    a_push_md = gr.Markdown("")
                                with gr.Row():
                                    a_cancel = gr.Button(t("common.cancel"), variant="stop", size="sm", scale=0, min_width=130)
                                with gr.Accordion(t("common.details_log"), open=False):
                                    a_log = gr.Textbox(label=t("common.log"), lines=14, max_lines=14, interactive=False, autoscroll=True, elem_id="log")
                        # ---- 5. Mes APK (gestionnaire)
                        with gr.Column(elem_classes=["rhd-step"]):
                            gr.HTML(_step_title(5, t("android.mgr.title")))
                            gr.Markdown(t("android.mgr.hint"), elem_classes=["rhd-hint"])
                            m_summary = gr.Markdown("")
                            m_table = gr.Dataframe(headers=[t("android.mgr.col_game"), t("android.mgr.col_package"), t("android.mgr.col_version"), t("android.mgr.col_date"),
                                                            t("android.mgr.col_sdk"), t("android.mgr.col_mode"), t("android.mgr.col_apk"), t("android.mgr.col_pack"),
                                                            t("android.mgr.col_signed"), t("android.mgr.col_verified")],
                                                   datatype=["str"] * 10, interactive=False, wrap=True, value=[])
                            with gr.Row():
                                m_pick = gr.Dropdown([], label=t("android.mgr.pick"), scale=4)
                                m_refresh = gr.Button(t("android.mgr.refresh"), size="sm", scale=0, min_width=160)
                            with gr.Row():
                                m_open = gr.Button(t("android.open_folder"), min_width=180)
                                m_verify = gr.Button(t("android.mgr.verify"), min_width=220)
                                m_install = gr.Button(t("android.mgr.install"), min_width=260, visible=android.ANDROID_ADB_ENABLED)
                                m_launch = gr.Button(t("android.launch_adb"), min_width=220, visible=android.ANDROID_ADB_ENABLED)
                                m_uninstall = gr.Button(t("android.mgr.uninstall"), min_width=220, visible=android.ANDROID_ADB_ENABLED)
                                m_delete = gr.Button(t("android.mgr.delete"), variant="stop", min_width=180)
                                m_confirm = gr.Checkbox(False, label=t("android.mgr.confirm"), scale=0, min_width=260)
                            m_status = gr.Markdown("")
                            with gr.Accordion(t("android.cache.title"), open=False):
                                gr.Markdown(t("android.cache.hint"), elem_classes=["rhd-hint"])
                                c_summary = gr.Markdown("")
                                c_table = gr.Dataframe(headers=[t("android.cache.col_kind"), t("android.cache.col_name"), t("android.cache.col_size"), t("android.cache.col_in_use")],
                                                       datatype=["str"] * 4, interactive=False, wrap=True, value=[])
                                with gr.Row():
                                    c_pick = gr.Dropdown([], label=t("android.cache.pick"), scale=4)
                                    c_refresh = gr.Button(t("android.mgr.refresh"), size="sm", scale=0, min_width=160)
                                with gr.Row():
                                    c_delete = gr.Button(t("android.cache.delete"), variant="stop", min_width=200)
                                    c_confirm = gr.Checkbox(False, label=t("android.mgr.confirm"), scale=0, min_width=260)
                                c_status = gr.Markdown("")
                            gr.Markdown(t("android.keys_warning"))
                            with gr.Row():
                                k_export = gr.Button(t("android.keys.export"), min_width=220)
                                k_open = gr.Button(t("android.open_keys"), size="sm", scale=0, min_width=220)
                            k_status = gr.Markdown("")

            # ---------------------------------------------------------- Onglet 4
            with gr.Tab(t("tab.help"), id="tab_help"):
                gr.Markdown(t("help.md", version=APP_VERSION) + t("help.android.md"))

        # ------------------------------------------------------------ câblage
        INPUT_KEYS = list(inputs)
        input_list = [inputs[k] for k in INPUT_KEYS]
        _STATE["input_defaults"] = {k: c.value for k, c in inputs.items()}   # utile pour piloter les handlers sans navigateur
        run_outputs = [status_md, log_box, failures_df, progress_html, done_group, done_md]
        preview_outputs = [preview_status, pv_pick, pv_slider, pv_info, pv_crop_b, pv_crop_a, validate_btn, preview_warn, estimate_md,
                           pv_video_before, pv_video_after, pv_video_md]
        step_outputs = [step1_summary, rpa_md, rpa_actions, step3_hint, step3_body, video_note, step4_hint, step4_body, step5_hint, step5_body,
                        estimate_md, info_md, analysis_log, inputs["preview_choice"], done_group, done_md, validate_btn]
        clear_outputs = [rpa_status, rpa_bar, rpa_log, preview_status, status_md, progress_html, done_group, validate_btn, preview_warn,
                         pv_video_before, pv_video_after, pv_video_md]
        reset_outputs = [inputs["game_root"], step1_summary, rpa_md, rpa_actions, step3_hint, step3_body, step4_hint, step4_body,
                         step5_hint, step5_body] + clear_outputs

        quit_btn.click(quit_app, None, top_msg, queue=False)
        lang_dd.change(set_ui_language, lang_dd, [top_msg, restart_btn], queue=False)
        restart_btn.click(restart_app, None, top_msg, queue=False, js=RELOAD_JS)
        inputs["mode"].change(on_mode_change, inputs["mode"],
                              [game_row, folder_row, inputs["factor"], inputs["jpeg_as"], inputs["png_as"], inputs["webp_as"],
                               hook_row, video_group, uninstall_btn, restore_btn, mode_help, inputs["video_enabled"]], queue=False)
        inputs["preset"].change(apply_preset, inputs["preset"],
                                [inputs["nr_style"], inputs["nr_preset"], inputs["nr_intensity"], inputs["local_tone"],
                                 inputs["local_structure"], inputs["skin_structure"]], queue=False)

        # Étape 1 → analyse automatique (étapes 2 et 3) : après « Parcourir… » ou Entrée dans le champ
        def _chain(event):
            event.then(clear_flow, None, clear_outputs, queue=False).then(analyze_step, input_list, step_outputs)

        _chain(browse_game.click(browse(t("dialog.pick_game")), inputs["game_root"], inputs["game_root"]))
        _chain(inputs["game_root"].submit(lambda v: v, inputs["game_root"], inputs["game_root"], queue=False))
        _chain(browse_in.click(browse(t("dialog.pick_input")), inputs["input_dir"], inputs["input_dir"]))
        _chain(browse_out.click(browse(t("dialog.pick_output")), inputs["output_dir"], inputs["output_dir"]))
        _chain(analyze_btn2.click(lambda: None, None, None, queue=False))
        v_browse.click(browse(t("dialog.pick_game_short")), v_root, v_root)

        # Étape 2 : extraction (toutes les archives, moteur Python) puis nouvelle analyse ; ou « Passer »
        rpa_extract_btn.click(flow_extract, inputs["game_root"], [rpa_status, rpa_bar, rpa_log], concurrency_limit=1,
                              show_progress="minimal").then(analyze_step, input_list, step_outputs)
        rpa_skip_btn.click(skip_rpa, None, [rpa_status, step3_hint, step3_body], queue=False)
        # Étape 3 : « Continuer » ouvre l'aperçu (étape 4) et l'étape finale (5), puis génère l'aperçu
        continue_btn.click(open_steps45, None, [step4_hint, step4_body, step5_hint, step5_body], queue=False).then(
            generate_preview, input_list, preview_outputs, concurrency_limit=1, show_progress="minimal")
        inputs["video_enabled"].change(update_video_note, input_list, [video_note, estimate_md])
        inputs["images_enabled"].change(update_video_note, input_list, [video_note, estimate_md])

        analyze_btn.click(analyze_step, input_list, step_outputs)
        preview_btn.click(generate_preview, input_list, preview_outputs, concurrency_limit=1, show_progress="minimal")
        pv_pick.change(preview_show, [pv_pick, pv_x, pv_y, pv_crop], [pv_slider, pv_info, pv_crop_b, pv_crop_a])
        for s in (pv_x, pv_y, pv_crop):
            s.release(preview_crop, [pv_pick, pv_x, pv_y, pv_crop], [pv_crop_b, pv_crop_a])
        pv_prev.click(lambda rel: preview_step(rel, -1), pv_pick, pv_pick)
        pv_next.click(lambda rel: preview_step(rel, +1), pv_pick, pv_pick)
        validate_btn.click(validate_and_launch, input_list, run_outputs, concurrency_limit=1, show_progress="minimal")
        for key in INPUT_KEYS:
            if key not in NOT_INVALIDATING:
                inputs[key].change(invalidate_preview, None, [validate_btn, preview_warn], queue=False)

        launch_btn.click(launch, input_list, run_outputs, concurrency_limit=1, show_progress="minimal")
        play_btn.click(play_game, inputs["game_root"], status_md, queue=False)
        compare_btn.click(lambda: gr.Tabs(selected="tab_compare"), None, tabs, queue=False)
        reset_btn.click(reset_flow, None, reset_outputs, queue=False)
        t_browse.click(single_browse, t_path, t_path)
        t_upload.change(single_from_upload, t_upload, t_path, queue=False)
        t_paste.click(single_from_paste, None, [t_path, t_status], queue=False)
        t_run.click(test_single, [t_path] + input_list, [t_status, t_slider, t_info, t_crop_b, t_crop_a, t_save], concurrency_limit=1)
        for s_ in (t_x, t_y, t_crop):
            s_.release(single_crop, [t_x, t_y, t_crop], [t_crop_b, t_crop_a])
        t_save.click(single_save, None, t_save_msg)
        inputs["video_share_nr"].change(lambda shared: gr.update(visible=not shared), inputs["video_share_nr"], video_nr_group, queue=False)
        tv_browse.click(video_browse, tv_path, tv_path)
        tv_upload.change(single_from_upload, tv_upload, tv_path, queue=False)
        tv_run.click(test_video, [tv_path, tv_limit] + input_list,
                     [tv_status, tv_slider, tv_info, tv_crop_b, tv_crop_a, tv_before, tv_after, tv_pick], concurrency_limit=1)
        tv_pick.change(video_show, [tv_pick, tv_x, tv_y, tv_crop], [tv_slider, tv_info, tv_crop_b, tv_crop_a])
        for s_ in (tv_x, tv_y, tv_crop):
            s_.release(video_crop, [tv_pick, tv_x, tv_y, tv_crop], [tv_crop_b, tv_crop_a])
        tv_save.click(video_save, None, tv_save_msg)
        cancel_btn.click(cancel_run, None, status_md, queue=False)
        uninstall_btn.click(uninstall, [inputs["game_root"], inputs["out_name"]], status_md)
        restore_btn.click(restore, inputs["game_root"], status_md)
        save_btn.click(save_cfg, input_list, cfg_msg, queue=False)
        load_btn.click(load_cfg, None, input_list + [cfg_msg], queue=False)
        demo.load(load_cfg, None, input_list + [cfg_msg], queue=False)

        # partage du dossier du jeu entre les onglets (sur saisie utilisateur uniquement, pour éviter les boucles)
        for ev in ("input", "blur"):
            getattr(inputs["game_root"], ev)(lambda v: v, inputs["game_root"], v_root, queue=False)
            getattr(v_root, ev)(lambda v: v, v_root, inputs["game_root"], queue=False)
        browse_game.click(lambda v: v, inputs["game_root"], v_root, queue=False)
        inputs["out_name"].input(lambda v: v, inputs["out_name"], v_out, queue=False)
        inputs["input_dir"].input(lambda v: v, inputs["input_dir"], v_in_dir, queue=False)
        inputs["output_dir"].input(lambda v: v, inputs["output_dir"], v_out_dir, queue=False)

        v_load.click(viewer_load, [v_root, v_kind, v_out, v_in_dir, v_out_dir, inputs["game_root"]], [v_pick, v_count, v_root])
        compare_btn.click(viewer_load, [v_root, v_kind, v_out, v_in_dir, v_out_dir, inputs["game_root"]], [v_pick, v_count, v_root])
        viewer_outputs = [v_slider, v_info, v_crop_b, v_crop_a, v_before_video, v_after_video]
        v_pick.change(viewer_show, [v_pick, v_x, v_y, v_crop, v_time], viewer_outputs)
        v_time.release(viewer_show, [v_pick, v_x, v_y, v_crop, v_time], viewer_outputs)
        for s in (v_x, v_y, v_crop):
            s.release(viewer_crop, [v_pick, v_x, v_y, v_crop], [v_crop_b, v_crop_a])
        v_prev.click(lambda rel: viewer_step(rel, -1), v_pick, v_pick)
        v_next.click(lambda rel: viewer_step(rel, +1), v_pick, v_pick)

        # ------------------------------------------------------------ Outils : dossier partagé avec l'onglet principal
        for other in (x_root, t_root):
            for ev in ("input", "blur"):
                getattr(inputs["game_root"], ev)(lambda v: v, inputs["game_root"], other, queue=False)
                getattr(other, ev)(lambda v: v, other, inputs["game_root"], queue=False)
            browse_game.click(lambda v: v, inputs["game_root"], other, queue=False)
        x_root.input(lambda v: v, x_root, t_root, queue=False)
        t_root.input(lambda v: v, t_root, x_root, queue=False)

        # ---- extraction .rpa
        x_scan_outputs = [x_summary, x_list, x_step2, x_step3, x_status, x_bar]
        x_browse.click(browse(t("dialog.pick_game")), x_root, x_root).then(
            lambda v: v, x_root, inputs["game_root"], queue=False).then(lambda v: v, x_root, t_root, queue=False).then(
            rpa_scan, x_root, x_scan_outputs)
        x_scan.click(rpa_scan, x_root, x_scan_outputs)
        x_root.submit(rpa_scan, x_root, x_scan_outputs)
        x_go.click(rpa_extract, [x_root, x_list, x_engine, x_skip, x_bak], [x_status, x_bar, x_log, x_done], concurrency_limit=1,
                   show_progress="minimal").then(lambda r: rpa_scan(r)[:4], x_root, [x_summary, x_list, x_step2, x_step3])
        x_cancel.click(tools_cancel, None, x_status, queue=False)
        x_to_main.click(lambda: gr.Tabs(selected="tab_main"), None, tabs, queue=False).then(
            clear_flow, None, clear_outputs, queue=False).then(analyze_step, input_list, step_outputs)

        # ---- traduction
        tl_refresh_outputs = [t_state, t_step2, t_step3, t_step2_hint, t_step3_hint]
        t_browse2.click(browse(t("dialog.pick_game")), t_root, t_root).then(
            lambda v: v, t_root, inputs["game_root"], queue=False).then(lambda v: v, t_root, x_root, queue=False).then(
            tl_refresh, [t_root, t_target], tl_refresh_outputs, queue=False)
        t_root.submit(tl_refresh, [t_root, t_target], tl_refresh_outputs, queue=False)
        t_target.change(tl_refresh, [t_root, t_target], tl_refresh_outputs, queue=False)
        # ouverture directe d'un onglet / d'un jeu par l'URL (?tab=tab_tools&sub=sub_rpa&game=…)
        a_cfg_fields = [a_name, a_icon_name, a_package, a_version, a_numeric, a_orientation, a_internet, a_videos, a_budget, a_icon, a_bundle,
                        a_decompile, a_skip_rpa, a_prefer_rpyc, a_data_mode, a_ext_audio, a_arm64, a_estimate]
        a_analyze_outputs = [a_summary, a_step2, a_step2_hint, a_sdk, a_env_md, a_step3, a_step3_hint, a_step4, a_step4_hint] + a_cfg_fields
        demo.load(deeplink, None, [tabs, tools_tabs, inputs["game_root"], x_root, t_root, a_root], queue=False).then(
            rpa_scan, x_root, x_scan_outputs, queue=False).then(tl_refresh, [t_root, t_target], tl_refresh_outputs, queue=False).then(
            android_analyze, a_root, a_analyze_outputs)
        t_gen.click(tl_generate, [t_root, t_target, t_merge], [t_gen_status, t_step2, t_step3, t_step2_hint, t_step3_hint, t_state],
                    concurrency_limit=1, show_progress="minimal")
        t_export.click(tl_export, [t_root, t_target, t_export_base, t_export_chunk, t_export_mb, t_export_idfmt, t_export_scope, t_export_tags, t_export_only],
                       [t_export_msg, t_import_path, t_step3])
        t_import_browse.click(tl_import_browse, t_import_path, t_import_path)
        t_import.click(tl_import, [t_root, t_target, t_import_path], [t_status, t_table, t_review, t_state])
        t_fix.click(tl_apply_corrections, [t_root, t_target, t_table], t_fix_msg)
        t_install.click(tl_install, [t_root, t_target], [t_install_status, t_state])
        t_check.click(tl_check, [t_root, t_target], t_check_status, concurrency_limit=1, show_progress="minimal")
        t_uninstall.click(tl_uninstall, [t_root, t_target, t_uninstall_tl], [t_install_status, t_state])

        # ---- Android (APK)
        for ev in ("input", "blur"):
            getattr(inputs["game_root"], ev)(lambda v: v, inputs["game_root"], a_root, queue=False)
            getattr(a_root, ev)(lambda v: v, a_root, inputs["game_root"], queue=False)
        browse_game.click(lambda v: v, inputs["game_root"], a_root, queue=False)
        x_root.input(lambda v: v, x_root, a_root, queue=False)
        t_root.input(lambda v: v, t_root, a_root, queue=False)
        a_root.input(lambda v: v, a_root, x_root, queue=False)
        a_root.input(lambda v: v, a_root, t_root, queue=False)
        a_browse.click(browse(t("dialog.pick_game")), a_root, a_root).then(
            lambda v: v, a_root, inputs["game_root"], queue=False).then(lambda v: v, a_root, x_root, queue=False).then(
            lambda v: v, a_root, t_root, queue=False).then(android_analyze, a_root, a_analyze_outputs)
        a_analyze.click(android_analyze, a_root, a_analyze_outputs)
        a_root.submit(android_analyze, a_root, a_analyze_outputs)
        a_sdk.change(android_env_refresh, [a_sdk, a_manual_sdk], a_env_md, queue=False)
        a_manual_sdk.change(android_env_refresh, [a_sdk, a_manual_sdk], a_env_md, queue=False)
        a_manual_browse.click(android_sdk_browse, a_manual_sdk, a_manual_sdk)
        a_prepare.click(android_prepare, [a_root, a_sdk, a_manual_sdk, a_org, a_unrpyc],
                        [a_prep_status, a_prep_bar, a_prep_log, a_keys_note, a_step3, a_step3_hint, a_step4, a_step4_hint, a_env_md, a_prefer_rpyc],
                        concurrency_limit=1, show_progress="minimal")
        a_prep_cancel.click(tools_cancel, None, a_prep_status, queue=False)
        a_open_keys.click(android_open_keys, None, None, queue=False)
        a_icon_browse.click(android_icon_browse, a_icon, a_icon)
        for comp in (a_videos, a_budget, a_skip_rpa, a_data_mode, a_ext_audio):
            comp.change(android_estimate, [a_videos, a_budget, a_skip_rpa, a_data_mode, a_ext_audio], a_estimate, queue=False)
        a_go.click(android_build, a_cfg_fields, [a_status, a_bar, a_log, a_done, a_done_md], concurrency_limit=1, show_progress="minimal").then(
            android_mgr_refresh, None, [m_table, m_pick, m_summary], queue=False)
        a_cancel.click(tools_cancel, None, a_status, queue=False)
        a_open_out.click(android_open_out, None, None, queue=False)
        a_devices.click(android_devices, None, a_device_md)
        a_install.click(android_install, None, a_device_md, concurrency_limit=1)
        a_push.click(android_push, None, a_push_md, concurrency_limit=1, show_progress="minimal")
        a_launch.click(android_launch, None, a_device_md, concurrency_limit=1)
        m_launch.click(android_mgr_launch, m_pick, m_status, concurrency_limit=1)
        # ---- Mes APK
        demo.load(android_mgr_refresh, None, [m_table, m_pick, m_summary], queue=False).then(android_cache_refresh, None, [c_table, c_pick, c_summary], queue=False)
        m_refresh.click(lambda: android_mgr_refresh(True), None, [m_table, m_pick, m_summary])
        m_open.click(android_mgr_open, m_pick, None, queue=False)
        m_delete.click(android_mgr_delete, [m_pick, m_confirm], [m_status, m_table, m_pick, m_summary]).then(lambda: False, None, m_confirm, queue=False)
        m_install.click(android_mgr_install, m_pick, m_status, concurrency_limit=1, show_progress="minimal")
        m_verify.click(android_mgr_verify, m_pick, [m_status, m_table, m_pick, m_summary], concurrency_limit=1, show_progress="minimal")
        m_uninstall.click(android_mgr_uninstall, m_pick, m_status, concurrency_limit=1)
        c_refresh.click(android_cache_refresh, None, [c_table, c_pick, c_summary])
        c_delete.click(android_cache_delete, [c_pick, c_confirm], [c_status, c_table, c_pick, c_summary]).then(lambda: False, None, c_confirm, queue=False)
        k_export.click(android_export_keys, None, k_status)
        k_open.click(android_open_keys, None, None, queue=False)
    return demo


def main() -> None:
    print(f"RenPyHD v{APP_VERSION} — {t('console.tool_root', root=TOOL_ROOT)} — {t('console.lang', lang=UI_LANG)}", flush=True)
    _startup_runtime_check()
    core.PREVIEW_ROOT.mkdir(exist_ok=True)
    # Redémarrage demandé depuis l'interface (changement de langue) : même port, pas de nouvelle fenêtre (la page se recharge).
    cfg = core.load_config()
    port = ARGS.port
    open_browser = not ARGS.no_browser
    if cfg.pop("restart_pending", False):
        port = port or int(cfg.pop("last_port", 0) or 0)
        open_browser = False
        core.save_config(cfg)
    demo = build_ui()
    # Gradio ne sert que les fichiers de dossiers autorisés : l'aperçu (app/preview) et les jeux, qui peuvent
    # être sur n'importe quel disque (vidéos avant/après renvoyées par chemin). Le serveur n'écoute que sur 127.0.0.1.
    _allowed = [str(APP_DIR)]
    for _letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        if Path(f"{_letter}:/").exists():
            _allowed.append(f"{_letter}:/")
    _app, local_url, _share = demo.queue(default_concurrency_limit=1).launch(
        allowed_paths=_allowed,
        server_name="127.0.0.1",
        server_port=port or None,
        inbrowser=False,
        share=False,
        show_error=True,
        prevent_thread_lock=True,
        quiet=True,
        css=CSS,
        theme=gr.themes.Soft(),
    )
    m = re.search(r":(\d+)", local_url or "")
    _STATE["port"] = int(m.group(1)) if m else 0
    print(t("console.url", url=local_url), flush=True)
    print(f"RENPYHD_URL={local_url}", flush=True)
    if open_browser:
        print(t("console.window", how=core.open_app_window(local_url)), flush=True)
    print(t("console.close_hint"), flush=True)
    try:
        demo.block_thread()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
