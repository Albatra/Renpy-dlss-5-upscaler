# Third-party components / Composants tiers

RenPyHD itself (everything under `app/`, `launcher/`, `setup.*`, `run.bat`) is MIT-licensed (see `LICENSE`).
It **does not redistribute** the components below: `setup.bat` downloads the DLSS 5 Visual Enhancer release
from its author's GitHub page into `DLSS5/` on the user's machine, and `tools/rpaExtract.exe` is optional and
must be obtained by the user.

RenPyHD lui-même (`app/`, `launcher/`, `setup.*`, `run.bat`) est sous licence MIT (voir `LICENSE`).
Il **ne redistribue pas** les composants ci-dessous : `setup.bat` télécharge la version officielle du DLSS 5 Visual
Enhancer depuis la page GitHub de son auteur dans `DLSS5/`, et `tools/rpaExtract.exe` est optionnel.

| Component | License | Where | Notes |
|---|---|---|---|
| **DLSS 5 Visual Enhancer v3.0** by Merserk | MIT | `DLSS5/` (downloaded by `setup.bat` from https://github.com/Merserk/dlss5-visual-enhancer) | Python 3.13 embedded, `src/` image & video pipelines, `app.py`. RenPyHD imports its `src` modules. |
| **NVIDIA DLSS / NGX** (`nvngx_dlss*.dll`, DLSS Neural Rendering models) | NVIDIA RTX SDK license (proprietary) | `DLSS5/bin/runtime/` | Shipped inside the Visual Enhancer release. Downloaded by the user, **not redistributed here**. Use is subject to NVIDIA's terms. |
| **FFmpeg** (`ffmpeg.exe`, `ffprobe.exe`, build 9.0.x) | GPLv3 (build includes GPL components such as libx264/libx265) | `DLSS5/bin/ffmpeg/` | Used as an external executable only (probing, trimming, encoding). Source: https://ffmpeg.org — see `DLSS5/bin/ffmpeg/LICENSE`. |
| **ReShade** | BSD-3-Clause | `DLSS5/bin/runtime/` | Rendering host used by the Visual Enhancer. https://reshade.me |
| **RenoDX** | MIT | `DLSS5/bin/runtime/` | ReShade add-on used by the Visual Enhancer. https://github.com/clshortfuse/renodx |
| **Gradio** | Apache-2.0 | `DLSS5/bin/python-3.13.15-embed-amd64/Lib/site-packages` | Web UI framework used by `app/renpy_hd_app.py`. |
| **Python 3.13** (embeddable) + **Pillow**, **NumPy**, **OpenCV** and other packages bundled by the Visual Enhancer | PSF License / their respective licenses (HPND, BSD, Apache-2.0…) | `DLSS5/bin/python-3.13.15-embed-amd64/` | See each package's `LICENSE` file in `site-packages`. |
| **unrpa** (via `rpaExtract.exe`, iwanPlays' wrapper) | GPLv3 | `tools/rpaExtract.exe` — **optional, not included** | Alternative `.rpa` extraction engine. RenPyHD's default extractor is its own MIT Python implementation (`renpy_hd_core.Rpa`). See `tools/README.md`. |
| **Ren'Py** | MIT / LGPL (engine of the games being processed) | not included | `zz_dlss_hd.rpy` and `zz_renpyhd_lang.rpy` are small scripts installed **into the user's game**; the translation tool runs the game's own Ren'Py engine. https://www.renpy.org |

Game assets (images, videos, texts) processed by RenPyHD remain the property of their respective authors.
Les ressources des jeux traitées par RenPyHD restent la propriété de leurs auteurs.
