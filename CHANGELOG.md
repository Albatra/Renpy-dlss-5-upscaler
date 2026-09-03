# Changelog

All notable changes to RenPyHD are documented here. / Toutes les évolutions notables de RenPyHD sont listées ici.

## [Unreleased] — 1.1.0

### Tools › Android (APK)
- New **Android (APK)** module: builds an APK of a Ren'Py game with the official **Ren'Py SDK + RAPT**, driven from their
  command line without any prompt (`renpy_hd_android.py`, `android_matrix.json`, `renpyhd_android_adapter.rpy`).
- Four steps: choose the game (Ren'Py version, `.rpy`/`.rpyc` coverage, HD 2x / backups / hook always excluded, images,
  videos, already extracted `.rpa`, estimated size) → prepare the environment once per version (SDK + RAPT from renpy.org,
  portable Temurin JDK 8 or 21, Android SDK through RAPT, signing keys in `android\keys\`, optional unrpyc) → configure
  (`.android.json`, icons, videos, image size limit, `.rpyc` as-is, app bundle) → build (progress, log, cancel, `apksigner`
  check, open folder, adb install).
- Compatibility table (Ren'Py 7.0 → 8.6): JDK 8 for Ren'Py ≤ 7.6 / 8.0–8.1, JDK 21 for Ren'Py ≥ 7.7 / 8.2; Ren'Py 7.0–7.3
  games are built with the latest Ren'Py 7 SDK (their RAPT depends on the closed jcenter/bintray repositories); versions
  missing from renpy.org fall back to the next patch of the same series, then the latest of the same major.
- Strings in the six interface languages, Help tab section, README sections.

## [1.0.0] — 2026-09-03

First public release. / Première version publique.

### Improve a game (DLSS 5 Neural Rendering)
- Five-step guided flow: choose the game (automatic analysis) → extract `.rpa` archives (when needed, resumable, skippable)
  → choose what to improve (images / videos, Neural Rendering preset, factor) → preview (10 random images + 3 s of one
  video, before/after slider, 1:1 magnifier, time/size estimate) → improve the game (progress, cancel/resume, Play,
  Compare, Uninstall).
- Modes: **HD 2x** (`game/hd2x/` + `zz_dlss_hd.rpy` hook, factors 1.5 / 1.724 / 2 / 3), **Replace in place** (1× DLAA,
  originals kept in `game/_dlss_backup/`), **native `@2` suffix** (Ren'Py ≥ 8.4, no hook), **free image folder**.
- Videos: frame-by-frame DLSS 5 with optical flow and scene-change reset, VP9 / VP8 / AV1 / H.264 output, audio copied
  when possible, resolution cap (4K by default), NVENC when available, per-video factor in `hd2x/videos.json`, hook
  support for `Movie(play=…)`, `renpy.movie_cutscene`, `play movie`.
- Resumable processing (existing outputs skipped), failed images retried one by one, small images (< 256 px) skipped,
  `.rpa` archives read directly, Ren'Py 7.x / 8.x supported, dry-run, configuration save/load.
- Expert mode: every engine setting (NR style/preset/intensity, local tone/structure, skin structure, DLSS model J/K/L/M,
  output formats and quality, filters, batch size, hook options, video codec/CRF/cap/audio).
- Compare / Test tab: before/after viewer with 1:1 magnifier for images and videos, single-image and single-video tests.

### Tools
- **Extract `.rpa` archives**: built-in Python extractor (RPA-2.0/3.0, resumable, does not overwrite), optional
  `rpaExtract.exe` engine, optional `.rpa.bak` rename.
- **Translate the game**: text extraction with the game's own Ren'Py engine (works with compiled/archived scripts),
  numbered `.txt` export for any translation service, tolerant import with tag/interpolation checks and a review table,
  `zz_renpyhd_lang.rpy` installer (default language + Shift+L toggle), in-game verification, uninstall.
  Target languages include french, english, spanish, german, russian, portuguese (brazil), italian, chinese (simplified),
  japanese, korean, turkish, polish and more.

### Application
- Multilingual UI: French (default), English, Spanish, German, Russian, Brazilian Portuguese (`app/i18n/*.json`), language
  menu with "Restart now", `--lang xx` option, Windows display-language auto-detection on first run.
- Edge/Chrome app window via `RenPyHD.exe` (C# launcher, restart support), `run.bat` fallback, `setup.bat` bootstrap that
  downloads and verifies (SHA-256) the DLSS 5 Visual Enhancer v3.0 and builds the launcher with `csc.exe`.
- 100 % local: the server listens on 127.0.0.1 only; no network access except the setup download.
