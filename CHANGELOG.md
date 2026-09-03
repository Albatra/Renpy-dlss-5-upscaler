# Changelog

All notable changes to RenPyHD are documented here. / Toutes les évolutions notables de RenPyHD sont listées ici.

## [Unreleased] — 1.1.0

### Improve a game
- In-game before/after (`zz_dlss_hd.rpy`): **Shift+J** cycles HD → original → split screen (the divider follows the mouse;
  each image is split at the same fraction of its own width), **Shift+H** shows the stats. Documented in Help (six
  languages) and the READMEs.
- **Pipelined image processing** (Expert mode › *Pipelined processing (GPU fed continuously)*, on by default, plus
  *Pipeline CPU threads*, 0 = half the logical cores): the tool's `convert_images` decodes, renders and encodes one image
  at a time, so the GPU idled during every 4K JPEG/PNG/WebP encode. RenPyHD now keeps **one DLSS session** fed by a
  decode pool (bounded prefetch of 8 frames) and drains it into an encode/write pool, calling the native worker strictly
  one frame at a time. Same options, same output names, one session per output size, the tool's per-session *feature 18*
  verification (ReShade + worker logs) with the same failure reports; anything unexpected falls back to the tool's own
  path for the remaining images (logged). Videos are unchanged; outputs are pixel-identical to the classic path.
  Untick the option to get the previous behaviour.

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
- **Big games without downscaling — separate data**: step 3 offers *Game data: inside the APK (≈ 2 GB limit) / separate
  (recommended for big games)*. Separate mode builds a **light APK** (engine, scripts, `gui/`, fonts, audio) and a **data
  pack** `android\out\<game>\<package>-data\game\` with every image and video at full size (hard links when the pack is on
  the same drive as the game: instant, no extra space). On the phone the pack's `game` folder goes into
  `Android/data/<package>/files/game/` — the folder every Ren'Py 7.x / 8.x engine natively puts first in
  `config.searchpath` — with `Android/obb/<package>/game/` as a fallback. A `zz_renpyhd_extdata.rpy` hook (Python 2/3)
  indexes `.rpa` archives dropped there, supports a desktop test override (`RENPYHD_EXTDATA`) and shows a bilingual
  "data not found" screen with the exact paths instead of crashing. New button *Copy the data to the phone (adb)*
  (`adb shell mkdir -p` + `adb push` into the app-private external folder, which adb and the app can write even under
  Android 11+ scoped storage), manual-copy instructions and a `LISEZMOI-README.txt` in the pack. RAPT's own expansion
  (OBB) mechanism was checked in the 7.3.5 / 7.8.7 / 8.1.2 / 8.6.0 sources: it only survives in Ren'Py 7.3 and relies on
  Google Play's downloader, so it is not used.
- **My APKs** (step 5): table of everything under `android\out\` (game, package, version / code, build date, Ren'Py SDK,
  data mode, APK size, data pack size, signature) from a `build.json` manifest written by each build (backfilled from
  existing folders), with *Open folder*, *Install on the phone (adb, + data)*, *Uninstall from the phone (adb)*, *Delete*
  (with confirmation, whole `out\<name>` folder + build copy), *Refresh*; *Clean unused SDKs* helper (Ren'Py SDKs, JDKs,
  Gradle cache, downloads, build copies with sizes and a guarded delete) and *Export the keys…* (PowerShell folder picker).
- Analysis now counts audio files and `gui/` images; the size warning names the real limits (≈ 2 GB practical, 4 GB
  absolute for the ZIP format) and suggests separate data.
- **Ren'Py 7.0–7.3 games really build and start**: RAPT 7.0–7.3 only failed on `com.danikula.expansion` (Google Play
  expansion, closed bintray repository). `patch_legacy_rapt` removes those dependencies and the `Downloader*` classes from
  `rapt\prototype`, adds `mavenCentral()` and marks `build.txt`; the exact SDK is then used (an SDK 7.4+ cannot start
  7.3 `.rpyc`: "could not find label 'start'"). Verified with A Mother's Love (Ren'Py 7.3.5): universal + per-ABI APKs in
  121 s, desktop launch OK.
- **Images on the phone** (step 3 « Images », shown when the game has DLSS outputs): *Original*; *Improved, original size
  (recommended, default)* — every image with an `hd2x` counterpart (same path, `.webp/.png/.jpg` alternatives like the
  hook) is replaced in the data pack by a Lanczos downscale of the DLSS output to the original pixel size, same format
  (JPEG/WebP quality 92, PNG keeps alpha), generated by a thread pool, resumable (existing generated files kept, never
  writes into a hard link to the original); *Full HD 2x + hook* — the whole `hd2x/` folder (links) goes into the pack and
  `zz_dlss_hd.rpy` into the APK with `config.image_cache_size_mb` capped at 512 (textures 4× bigger: fine for
  720p→1440p games on a 2176×1812 screen, too heavy for 1080p→4K). Games improved in *Replace in place* mode are told
  their originals already are the improved images. Size estimates and `build.json` (`image_mode`, `improved`) updated.
- **ABI-aware install**: *Search devices* reads model / Android version / `ro.product.cpu.abilist`; install picks the
  universal APK, else the split APK whose ABI the device accepts (arm64-v8a > armeabi-v7a > x86_64 > x86), and refuses
  with a clear message otherwise (no more `INSTALL_FAILED_NO_MATCHING_ABIS` from a blind x86_64 pick). New *Launch on the
  phone* button (`adb shell monkey`), install order APK → data pack → launch, in the build step and in *My APKs*.
- **arm64 for Ren'Py 7.0–7.3 games** (*Build for arm64*, step 3): RAPT 7.0–7.2 only produces armeabi-v7a + x86_64 APKs,
  which 64-bit-only phones refuse. The new route decompiles the `.rpyc` with unrpyc 1.x (Python 2 of the 7.8.7 SDK),
  drops the `.rpyc`, runs `renpy.py <copy> compile` with an automatic fix pass (`fix_script_line`: empty `with x:` blocks,
  stray indentation) and builds with the 7.8.7 SDK → universal arm64-v8a + armeabi-v7a + x86_64 APK. Verified on Melody
  (Ren'Py 7.1.0): 23 `.rpyc` decompiled, no fix needed, APK in 66 s, desktop launch OK, then installed and launched on a
  Samsung Galaxy Z Fold 6 (Android 16, arm64-v8a only) with the 6 GB data pack pushed over adb (209 s, 29 MB/s).
  On Android 11+ the folders created by `adb push` belong to the `shell` user and the app got `Permission denied`
  listing `files/game`: the push now ends with `chmod -R a+rX` (verified: Ren'Py then loads the pack on the phone).
- **Verify (launch on PC)** in *My APKs*: runs the build copy with the SDK of the build (`RENPYHD_EXTDATA` pointing to
  the data pack when relevant) and a probe (`renpyhd_verify_probe.rpy`) that checks the `start` label, the probe images
  and the rendered main menu within a timeout; result stored in `build.json` and shown in the table.

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
