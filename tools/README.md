# tools/ — optional third-party extractor

RenPyHD extracts `.rpa` archives with **its own Python engine** (`renpy_hd_core.Rpa`: RPA-2.0 / RPA-3.0 index,
file by file, progress bar, cancellable, resumable, never overwrites existing files). Nothing else is required.

Optionally, you may place **`rpaExtract.exe`** (iwanPlays' Windows wrapper of [unrpa](https://github.com/Lattyware/unrpa),
GPLv3) in this folder. When the file `tools\rpaExtract.exe` exists, the "Extract the archives" tool offers it as an
alternative engine ("rpaExtract.exe (third-party tool: overwrites every file)"). It is called with the `.rpa` path as its
only argument and with standard input closed (to skip its "press a key" prompt); it extracts next to the archive and
overwrites everything.

`rpaExtract.exe` is **not part of this repository** and is not redistributed here (see `THIRD_PARTY.md`); it is ignored
by `.gitignore`.

---

# tools/ — extracteur tiers optionnel

RenPyHD extrait les archives `.rpa` avec **son propre moteur Python** (`renpy_hd_core.Rpa` : index RPA-2.0 / RPA-3.0,
fichier par fichier, barre de progression, annulable, reprenable, n'écrase jamais un fichier existant). Rien d'autre n'est
nécessaire.

Vous pouvez, en option, placer ici **`rpaExtract.exe`** (enveloppe Windows d'iwanPlays autour d'[unrpa](https://github.com/Lattyware/unrpa),
GPLv3). Quand `tools\rpaExtract.exe` existe, l'outil « Extraire les archives » le propose comme moteur alternatif
(« rpaExtract.exe (outil tiers : réécrit tous les fichiers) »). Il est appelé avec le chemin du `.rpa` en seul argument
et l'entrée standard fermée (pour sauter son « Appuyez sur une touche ») ; il extrait à côté de l'archive en écrasant tout.

`rpaExtract.exe` **ne fait pas partie de ce dépôt** et n'est pas redistribué ici (voir `THIRD_PARTY.md`) ; il est ignoré
par `.gitignore`.
