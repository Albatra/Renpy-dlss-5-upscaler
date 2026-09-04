# RenPyHD

**Make Ren'Py games sharper with NVIDIA DLSS 5.** RenPyHD upscales every image (and, optionally, every video) of a Ren'Py game with DLSS 5 Neural Rendering, without modifying the game. It also extracts `.rpa` archives, helps you translate a game, builds Android APKs and improves the textures of **Unity** games at their original size.

*Version française : [README.fr.md](README.fr.md)*

![Before / after: original 1280×720 on the left, DLSS 5 ×2 (2560×1440) on the right](docs/screenshots/readme_before_after.jpg)

![1:1 zoom: original pixels ×2 versus the DLSS 5 result](docs/screenshots/readme_zoom.jpg)

Image from a Ren'Py game, preset "Faces", factor 2× — full-size files: [original](docs/screenshots/readme_original_720p.jpg) · [DLSS 5](docs/screenshots/readme_dlss5_1440p.jpg). Game assets belong to their authors.

## Requirements

- Windows 10 / 11, 64-bit
- NVIDIA GeForce **RTX 40 or 50** (DLSS 5 does not run on other cards)
- About 1.2 GB of disk for the DLSS runtime, plus ~4× the size of the game's images at 2×

Nothing else to install: no Python, no SDK.

## Install

1. Download the latest `RenPyHD-vX.Y.Z-win64.zip` from [Releases](https://github.com/Albatra/Renpy-dlss-5-upscaler/releases) and extract it anywhere.
2. Run **`setup.bat`** once. It downloads the official [DLSS 5 Visual Enhancer](https://github.com/Merserk/dlss5-visual-enhancer) (≈ 470 MB, checksum verified) into `DLSS5\` and builds the launcher.
3. Run **`RenPyHD.exe`**.

Or with git:

```bat
git clone https://github.com/Albatra/Renpy-dlss-5-upscaler.git RenPyHD
cd RenPyHD
setup.bat
```

## Use

1. **Choose the game** — click *Browse…* and pick the game folder (the one that contains `game\`). The analysis runs by itself.
2. **Extract the archives** — offered only if the images are inside `.rpa` files. One click; the game keeps working.
3. **Choose what to improve** — images (default) and/or videos, preset and factor. *Continue*.
4. **Preview** — 10 random images are improved in a test folder. Drag the before/after slider, check the estimate, regenerate if you like.
5. **Improve the game** — one button, a progress bar, then **Play**. The result lives in `game\hd2x\`; *Uninstall* puts everything back.
   In game, **Shift+J** cycles HD → original → before | after split screen (the divider follows the mouse; every image is
   split at the same fraction of its own width, for a pixel-exact comparison) and **Shift+H** shows the stats.
   Optional **in-game zoom** (Expert mode › *Install the in-game zoom (PC)*, also a step-3 box of the Android tab):
   a long press / long click (0.45 s) zooms the picture one step around the pointer (1x → 2x → 3x; the release does not
   advance the dialogue, a short tap stays a normal tap), drag pans, Ctrl + wheel zooms continuously, Shift+Z cycles — only
   backgrounds and sprites move, the dialogue window stays put; no pinch.

| Steps | Preview | Done |
|---|---|---|
| ![Steps](docs/screenshots/01_flow_steps_fr.png) | ![Preview](docs/screenshots/02_preview_fr.png) | ![Done](docs/screenshots/03_done_fr.png) |

## Tools

- **Extract `.rpa` archives** — built-in extractor, resumable, never overwrites.
- **Translate a game** — uses Ren'Py's own translation system, no script is modified:
  1. *Extract the texts* (the game's Ren'Py engine generates `game\tl\<language>\`),
  2. *Export* numbered `.txt` files and translate them with the service of your choice,
  3. *Import* the translated files and *Install*. In game, **Shift+L** toggles the language.
- **Android (APK)** — builds an APK of the game with the official **Ren'Py SDK + RAPT**, driven without any prompt:
  1. *Choose the game* (Ren'Py version, scripts, images/videos, estimated size),
  2. *Prepare the environment* once per Ren'Py version (SDK + RAPT from renpy.org, portable Temurin JDK 8 or 21, Android
     SDK through RAPT, signing keys in `android\keys\` — **back them up**),
  3. *Configure* (name, package id, version, orientation, icon, **game data inside the APK or separate**, videos, image
     size limit),
  4. *Build* (progress, log, cancel), then *Open folder*, *Install on the phone (adb)*, *Copy the data to the phone (adb)*,
  5. *My APKs*: everything built so far (sizes, SDK, signature) — open, install / uninstall on the phone, delete; clean
     unused SDKs / JDKs / caches; export the signing keys.
  Works with Ren'Py 7.0 → 8.6 SDKs: for Ren'Py 7.0–7.3 games RenPyHD patches the original RAPT (its Google Play expansion
  dependency lived on the closed bintray repository) and builds with the exact SDK, so the game's compiled scripts run
  as-is (verified by a real launch with Ren'Py 7.3.5). *Verify (launch on PC)* in *My APKs* starts any build copy with its
  SDK and checks the `start` label, the main menu and the data pack. HD 2x images and DLSS backups are never included.
  **Ren'Py 6.99 games** build too, through the *arm64 route*: their RAPT (32-bit Python 2, dead Gradle repositories) is
  not revived — the `.rpyc` are decompiled with unrpyc 1.x, the copy keeps the game's `script_version` so the engine
  applies its 6.99 compatibility settings, known 6.99 → 7.x script incompatibilities are fixed automatically
  (`screen name tag x:` …) and the 7.8.7 SDK builds a universal arm64 APK (verified on DMD Chapter 1, Ren'Py 6.99.14).
  **DLSS images on the phone** (step 3 *Images*): *Improved, original size* (default) ships each DLSS output downscaled
  back to the original pixel size — the neural-rendering gain without 4× heavier textures; *Full HD 2x + hook* ships
  `hd2x/` as-is with the hook (image cache capped at 512 MB — good for 720p→1440p games, too heavy for 1080p→4K).
  **Phones and ABIs**: *Search devices* shows the model, Android version and accepted ABIs; install picks the universal
  APK or the one matching the device (arm64-v8a > armeabi-v7a > x86_64) and refuses clearly otherwise. 64-bit-only
  phones (Galaxy Z Fold 6, Pixel 7+…) need arm64-v8a, which the original RAPT of Ren'Py 7.0–7.2 games cannot produce:
  tick *Build for arm64* — the `.rpyc` are decompiled with unrpyc, recompiled and packaged by the 7.8.7 SDK (verified on
  Melody / Ren'Py 7.1.0, installed and launched on a Galaxy Z Fold 6). Then *Launch on the phone*.
  **Big games**: an APK must stay under ≈ 2 GB (4 GB absolute). With *separate data* the APK only holds the engine, the
  scripts, the interface and the audio (a few dozen MB) and **all images and videos, at full size**, go into a data pack
  that is copied to `Android/data/<package>/files/game/` on the phone (adb button, or USB from Windows) — the folder the
  Ren'Py engine reads natively. If the data is missing, the game shows a clear screen with the exact path instead of crashing.

- **Unity** — improves the **textures of a Unity game** (Windows, 32 or 64-bit) with DLSS 5 Neural Rendering **at the
  original size** (DLAA 1×, *Faces* preset, model K): no render-time injection, the textures are extracted from the asset
  files with UnityPy, run through DLSS and written back in their **original format and dimensions** (DXT1/DXT5/BC7 via
  etcpak, ETC/ASTC, RGBA32/RGB24…), so sprites and atlases stay valid and video memory does not grow.
  1. *Choose the game* (folder with the `.exe` and `<name>_Data`: version, asset files, textures per format / size, sprites),
  2. *Back up* every asset file into `_renpyhd_backup\` (never overwritten; *Restore the originals*),
  3. *Settings and preview* (preset, model, minimum size, name / file filters, interface textures skipped, slider + loupe),
  4. *Improve* (batches, progress, cancel, resume), then *Verify by launching the game* (20 s, window capture, Unity log).
  Verified on *Man of the House* (Unity 2018.1, x86, 3,865 DXT1/DXT5/RGBA32/RGB24/BC7 textures).

## Good to know

- **One DLSS job at a time** on the machine; RenPyHD serializes its own jobs.
- **Videos are slow**: every frame goes through DLSS (about 1.5 minutes of computing per minute of 1080p video on an RTX 4090). The estimate is shown before you start.
- **Small images** (under 256 px) are skipped: DLSS cannot process them and they would not benefit.
- **Ren'Py 7.x and 8.x** are supported. Ren'Py 8.4+ can also use the native `@2` suffix mode, without hook.
- **100 % local**: the interface listens on `127.0.0.1` only. The only network access is the download done by `setup.bat`.
- **Interface languages**: English, French, Spanish, German, Russian, Brazilian Portuguese (menu at the top right).

Expert mode exposes every engine setting (Neural Rendering style, intensity, skin structure, DLSS model, formats, video codec…) and the **pipelined processing** switch (one DLSS session fed continuously by decode/encode thread pools — on by default, pixel-identical outputs, several times faster on images). Details in the in-app *Help* tab and in [`app/README.md`](app/README.md).

## Credits and license

RenPyHD is © 2026 Valentin Levavasseur, [MIT license](LICENSE).
It relies on the [DLSS 5 Visual Enhancer](https://github.com/Merserk/dlss5-visual-enhancer) by Merserk (MIT), which bundles the NVIDIA DLSS / NGX runtime, FFmpeg, ReShade, RenoDX, Python and Gradio — downloaded by `setup.bat`, not redistributed here. See [THIRD_PARTY.md](THIRD_PARTY.md) and [CHANGELOG.md](CHANGELOG.md).

Found a bug? [Open an issue](https://github.com/Albatra/Renpy-dlss-5-upscaler/issues) with the console log.
