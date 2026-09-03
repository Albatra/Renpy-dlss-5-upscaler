# RenPyHD

**Make Ren'Py games sharper with NVIDIA DLSS 5.** RenPyHD upscales every image (and, optionally, every video) of a Ren'Py game with DLSS 5 Neural Rendering, without modifying the game. It also extracts `.rpa` archives and helps you translate a game.

*Version française : [README.fr.md](README.fr.md)*

![Before / after: original 1280×720 on the left, DLSS 5 ×2 (2560×1440) on the right](docs/screenshots/readme_before_after.jpg)

![1:1 zoom: original pixels ×2 versus the DLSS 5 result](docs/screenshots/readme_zoom.jpg)

Image from the Ren'Py game *Melody* (MrDots Games), preset "Faces", factor 2× — full-size files: [original](docs/screenshots/readme_original_720p.jpg) · [DLSS 5](docs/screenshots/readme_dlss5_1440p.jpg). Game assets belong to their authors.

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

| Steps | Preview | Done |
|---|---|---|
| ![Steps](docs/screenshots/01_flow_steps_fr.png) | ![Preview](docs/screenshots/02_preview_fr.png) | ![Done](docs/screenshots/03_done_fr.png) |

## Tools

- **Extract `.rpa` archives** — built-in extractor, resumable, never overwrites.
- **Translate a game** — uses Ren'Py's own translation system, no script is modified:
  1. *Extract the texts* (the game's Ren'Py engine generates `game\tl\<language>\`),
  2. *Export* numbered `.txt` files and translate them with the service of your choice,
  3. *Import* the translated files and *Install*. In game, **Shift+L** toggles the language.

## Good to know

- **One DLSS job at a time** on the machine; RenPyHD serializes its own jobs.
- **Videos are slow**: every frame goes through DLSS (about 1.5 minutes of computing per minute of 1080p video on an RTX 4090). The estimate is shown before you start.
- **Small images** (under 256 px) are skipped: DLSS cannot process them and they would not benefit.
- **Ren'Py 7.x and 8.x** are supported. Ren'Py 8.4+ can also use the native `@2` suffix mode, without hook.
- **100 % local**: the interface listens on `127.0.0.1` only. The only network access is the download done by `setup.bat`.
- **Interface languages**: English, French, Spanish, German, Russian, Brazilian Portuguese (menu at the top right).

Expert mode exposes every engine setting (Neural Rendering style, intensity, skin structure, DLSS model, formats, video codec…). Details in the in-app *Help* tab and in [`app/README.md`](app/README.md).

## Credits and license

RenPyHD is © 2026 Valentin Levavasseur, [MIT license](LICENSE).
It relies on the [DLSS 5 Visual Enhancer](https://github.com/Merserk/dlss5-visual-enhancer) by Merserk (MIT), which bundles the NVIDIA DLSS / NGX runtime, FFmpeg, ReShade, RenoDX, Python and Gradio — downloaded by `setup.bat`, not redistributed here. See [THIRD_PARTY.md](THIRD_PARTY.md) and [CHANGELOG.md](CHANGELOG.md).

Found a bug? [Open an issue](https://github.com/Albatra/Renpy-dlss-5-upscaler/issues) with the console log.
