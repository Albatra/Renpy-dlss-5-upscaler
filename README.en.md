# RenPyHD — DLSS 5 upscaler, RPA extractor and translation helper for Ren'Py games

*[Version française → README.md](README.md)*

**RenPyHD** applies NVIDIA's **DLSS 5 Neural Rendering** (through Merserk's [DLSS 5 Visual Enhancer](https://github.com/Merserk/dlss5-visual-enhancer))
to every image — and, if you want, every video — of a **Ren'Py** game, without touching the original game.
It can also **extract `.rpa` archives** and helps you **translate a game** with Ren'Py's native translation system.
Local interface (Gradio in an Edge/Chrome app window), in English, French, Spanish, German, Russian or Brazilian Portuguese.
**100% local**: nothing is sent anywhere.

## Before / after

![Before / after: original 1280×720 on the left, DLSS 5 ×2 (2560×1440) on the right](docs/screenshots/readme_before_after.jpg)

![1:1 zoom: original pixels enlarged ×2 versus the DLSS 5 result](docs/screenshots/readme_zoom.jpg)

Image from the Ren'Py game *Melody* (MrDots Games), processed with RenPyHD, preset "Faces (K)", factor 2× — full-size
files: [original 1280×720](docs/screenshots/readme_original_720p.jpg) · [DLSS 5 2560×1440](docs/screenshots/readme_dlss5_1440p.jpg).
Game assets remain the property of their authors.

## What it does

* **Improve a game** in five guided steps: choose the game → extract `.rpa` archives (when needed) → choose
  images / videos, preset and factor → preview (10 random images + 3 s of video, before/after slider, 1:1 magnifier,
  time and disk estimate) → improve (progress, cancel/resume, **Play**, Compare, Uninstall).
* **Modes**: *HD 2x* (recommended: HD images in `game/hd2x/` + a small `zz_dlss_hd.rpy` hook, factors 1.5 / 1.724 / 2 / 3),
  *Replace in place* (1× DLAA, originals backed up), *native `@2` suffix* (Ren'Py ≥ 8.4, no hook), *free image folder*.
* **Videos**: every frame goes through DLSS 5 (optical flow, reset on scene changes), re-encoded as VP9 / VP8 / AV1 / H.264
  for Ren'Py, audio track kept, resolution cap (4K by default), NVENC when available.
* **Resumable**: what already exists is skipped; failures are retried one by one; too-small images are ignored;
  `.rpa` archives are read directly; Ren'Py 7.x and 8.x.
* **Tools**: `.rpa` extraction (built-in Python engine, resumable) and game **translation** (text extraction by the
  game's own Ren'Py engine, numbered `.txt` export for the translation service of your choice, tolerant import with a
  review table, language hook installer with **Shift+L** toggle).
* **Expert mode**: every engine setting (NR style / preset / intensity, local tone & structure, skin structure,
  DLSS model J/K/L/M, output formats and quality, filters, video codec, CRF, cap, audio…).

| Five-step flow | Before/after preview | End of processing |
|---|---|---|
| ![Steps](docs/screenshots/01_flow_steps_fr.png) | ![Preview](docs/screenshots/02_preview_fr.png) | ![Done](docs/screenshots/03_done_fr.png) |

| Translate the game | English UI | Spanish UI |
|---|---|---|
| ![Translation](docs/screenshots/04_tools_translate_fr.png) | ![English](docs/screenshots/05_flow_en.png) | ![Español](docs/screenshots/06_flow_es.png) |

## Requirements

* **Windows 10 / 11 64-bit**.
* **NVIDIA GeForce RTX 40 or 50** card (DLSS 5 Neural Rendering does not run on other cards; RTX 30: experimental,
  very slow path). Recent NVIDIA driver — **≥ 610** for NVENC video encoding (otherwise software x264, slower).
* Disk space: ~1.2 GB for the DLSS 5 Visual Enhancer, and for each game about **4× the size of its images** at 2×.
* No Python, no SDK to install: `setup.bat` downloads the Visual Enhancer (which bundles Python 3.13, FFmpeg and the
  DLSS runtime) and builds the launcher with the C# compiler already present in Windows.

## Install in 3 commands

```bat
git clone https://github.com/Albatra/Renpy-dlss-5-upscaler.git RenPyHD
cd RenPyHD
setup.bat
```

`setup.bat` downloads the official **DLSS 5 Visual Enhancer v3.0** release (467 MB, resumes automatically if the
connection drops), checks its **SHA-256**, extracts it to `DLSS5\`, then builds `RenPyHD.exe`. Without `git`: download the
repository zip (*Code → Download ZIP*, or a [release](https://github.com/Albatra/Renpy-dlss-5-upscaler/releases)),
unzip it and run `setup.bat`. Offline: `setup.bat -LocalZip "C:\path\DLSS.5.Visual.Enhancer.v3.0.zip"`.

Then double-click **`RenPyHD.exe`** (or `run.bat`). A console opens, then the application window.

## Usage in 5 steps

1. **Choose the game** — *Browse…* and select the game folder (the one containing `game\`, next to the `.exe`).
   The analysis is automatic: Ren'Py version, number of images and videos, already improved or not.
2. **Extract the archives (.rpa)** — offered only when some images are only available inside archives.
   *Extract the archives* (existing files kept, the game works the same) or *Skip this step* (read directly from
   the archives). Without archives the step passes by itself.
3. **What do you want to improve?** — **Images** (checked) and **Videos** (unchecked: slow; the note gives the count,
   size and estimated duration) checkboxes, Neural Rendering preset ("Faces" by default) and factor (2×). *Continue*.
4. **Preview** — 10 random images (1–20, adjustable in Expert mode) and 3 seconds of one video are improved in
   `app\preview\`, never inside the game. Before/after slider, Previous / Next, 1:1 magnifier, estimate
   (files, duration, disk space). *Regenerate the preview* picks other images.
5. **Improve the game** — a single button, progress bar, remaining time, log in "Details". *Cancel* stops cleanly,
   clicking again resumes. At the end: **Play**, *Compare before / after*, *Uninstall the mod* (restores the original
   game). In game, **Shift+H** shows the number of replaced images.

A game that is already improved is recognized: the steps adapt (Play / Compare / Uninstall directly).

## Translating a game (Tools › Translate the game)

No API key, no automatic engine, no game script modified: RenPyHD relies on Ren'Py's native translation system
(`game/tl/<language>/`).

1. **Extract the texts** — choose the target language (`french`, `english`, `spanish`, `german`, `russian`,
   `portuguese`, `italian`, `chinese`, `japanese`, `korean`, `turkish`, `polish`…); the Ren'Py engine **of the game itself**
   generates `game/tl/<language>/*.rpy` (also works when the scripts are compiled or inside a `.rpa`).
2. **Export for translation** — `phrase_001.txt`, `phrase_002.txt`… files (one line = one text, `ligne1;text`,
   tags protected by `[t1]`… markers). Translate them with the website or tool of your choice (for example
   onlinedoctranslator.com, file by file), keeping numbers and markers.
3. **Import and install** — import the translated `.txt` files (any name, any order; report of translated /
   errors / missing numbers, review table), then *Install the translation*: the game starts in the chosen language,
   **Shift+L** toggles between translation and original. *Check by launching the game* and *Uninstall* are there too.

## FAQ

* **One DLSS job at a time.** The DLSS runtime shares a ReShade log: two simultaneous jobs (two RenPyHD windows, or
  RenPyHD plus another Visual Enhancer script) make each other fail. RenPyHD already serializes its own jobs
  (previews, tests, batch); do not start another one next to it.
* **Some images are "ignored as too small".** Images whose shorter side is below 256 px (icons, buttons, thumbnails)
  fail the DLSS check and would gain nothing: they are skipped. The threshold is adjustable in Expert mode.
* **Which Ren'Py versions?** 7.x and 8.x (the hook is written for Python 2/3). The *`@2` suffix* mode only exists from
  **Ren'Py 8.4** (native loading of `name@2.ext` variants); below that, use *HD 2x*.
* **Are videos slow?** Yes: every frame of every video goes through DLSS. Measured on an RTX 4090: ≈ 10 fps at
  1080p → 4K, i.e. **≈ 1.5 minutes of computing per minute of 1080p video**. The estimate is shown before you start.
  The 4K cap is recommended: Ren'Py decodes in software and cannot keep up above it.
* **Is the game intact?** Yes. In HD 2x mode only `game/hd2x/` and `game/zz_dlss_hd.rpy` are added; *Uninstall the mod*
  removes them. In Replace mode the originals are in `game/_dlss_backup/`; *Restore originals* puts them back.
* **Does extracting the `.rpa` change anything?** No: Ren'Py prefers loose files over archives; the game works with or
  without them. Extraction only makes the files visible and resuming simpler.
* **Changing the interface language?** Menu at the top right, then *Restart now* (or `--lang en` on the command line).

## Privacy

RenPyHD is **100% local**: the server listens on `127.0.0.1` only, no data, image or text is sent anywhere, Gradio
telemetry is disabled. The only network access is the download of the DLSS 5 Visual Enhancer by `setup.bat` (from
GitHub). The translation itself is done with the service **you** choose, outside RenPyHD.

## Repository layout

```
app\          renpy_hd_app.py (UI), renpy_hd_core.py (engine), renpy_hd_tools.py (.rpa, translation),
              renpy_hd_i18n.py + i18n\*.json (languages), zz_dlss_hd.rpy (Ren'Py hook), README.md (detailed doc, French)
launcher\     launcher.cs + build_launcher.bat (RenPyHD.exe, built by setup.bat)
tools\        README.md: optional rpaExtract.exe (not included)
setup.bat / setup.ps1   installation; run.bat: start without the exe; build_release.ps1: release zip
DLSS5\        (created by setup.bat, git-ignored) DLSS 5 Visual Enhancer: Python 3.13, DLSS engine, FFmpeg
```

Detailed documentation (modes, videos, codecs, translation, limits): [`app/README.md`](app/README.md) (French; the in-app
Help tab has the same content in every UI language).
Found a bug? Open an [issue](https://github.com/Albatra/Renpy-dlss-5-upscaler/issues) with the console log.

## Credits and licenses

* RenPyHD: © 2026 Valentin Levavasseur, **MIT** license ([LICENSE](LICENSE)).
* [DLSS 5 Visual Enhancer](https://github.com/Merserk/dlss5-visual-enhancer) by **Merserk** (MIT) — downloaded by
  `setup.bat`, not redistributed here; it bundles the **NVIDIA DLSS / NGX** runtime (NVIDIA RTX SDK license), **FFmpeg**
  (GPLv3), **ReShade** (BSD-3), **RenoDX** (MIT), **Python** (PSF) and **Gradio** (Apache-2.0).
* `rpaExtract.exe` (optional, not included): wrapper of **unrpa** (GPLv3).
* Details: [THIRD_PARTY.md](THIRD_PARTY.md). History: [CHANGELOG.md](CHANGELOG.md).
* Ren'Py is a project by Tom Rothamel and contributors. Images and texts of the processed games belong to their authors.
