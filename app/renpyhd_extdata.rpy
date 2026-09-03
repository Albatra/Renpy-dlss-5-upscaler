# zz_renpyhd_extdata.rpy — RenPyHD « Données externes » / external game data (Android).
#
# Installé par RenPyHD dans la copie de construction (game\) quand les données du jeu (images, vidéos, gros audio)
# sont livrées à côté de l'APK au lieu d'être dedans. Sur le téléphone, le jeu lit ces données depuis :
#   Android/data/<paquet>/files/game/      (dossier « ANDROID_PUBLIC » du moteur : déjà dans config.searchpath
#                                           sur toutes les versions de Ren'Py 7.x / 8.x ; accessible à adb et à l'app)
#   Android/obb/<paquet>/game/             (repli : dossier OBB de l'application)
#   /storage/emulated/0/<paquet>/game/     (repli « ANDROID_OLD_PUBLIC », lisible seulement si l'app a l'accès au stockage)
# Sur PC (test), la variable d'environnement RENPYHD_EXTDATA peut pointer sur le dossier « game » externe.
# Les archives .rpa posées dans ces dossiers sont indexées aussi. Si les données manquent, un écran bilingue explique
# où les copier ; le jeu ne plante pas. Compatible Ren'Py 7 (Python 2) et Ren'Py 8 (Python 3).
#
# Installed by RenPyHD in the build copy (game\) when the game data (images, videos, big audio) ships next to the APK.
# On the phone the game reads it from Android/data/<package>/files/game/ (the engine's ANDROID_PUBLIC folder, already
# in config.searchpath on every Ren'Py 7.x / 8.x), with Android/obb/<package>/game/ as a fallback. On PC, the
# RENPYHD_EXTDATA environment variable can point to the external "game" folder (testing). Missing data: a clear
# bilingual screen instead of a crash.

init -1200 python in renpyhd_extdata:

    import os
    import json

    MANIFEST = "renpyhd_extdata.json"

    manifest = {}
    package = ""
    candidates = []      # dossiers examinés / folders examined
    found = []           # dossiers ajoutés à config.searchpath / folders added to the search path
    archives = []        # archives .rpa indexées depuis ces dossiers
    probes = []          # fichiers témoins attendus (depuis le manifeste)
    probes_missing = []
    missing = False      # True : données absentes (écran d'information)
    error = ""

    def _log(msg):
        try:
            print("RenPyHD extdata: " + msg)
        except Exception:
            pass

    def _norm(p):
        try:
            return os.path.normcase(os.path.normpath(os.path.abspath(p)))
        except Exception:
            return p

    def _read_manifest():
        try:
            f = renpy.loader.load(MANIFEST)
            try:
                data = f.read()
            finally:
                f.close()
            if not isinstance(data, str):
                data = data.decode("utf-8")
            return json.loads(data)
        except Exception as exc:
            _log("no manifest (%r)" % (exc,))
            return {}

    def _readable_dir(d):
        try:
            return bool(d) and os.path.isdir(d) and os.access(d, os.R_OK)
        except Exception:
            return False

    def _collect_candidates():
        out = []
        override = os.environ.get("RENPYHD_EXTDATA", "")
        if override:
            for p in override.split(os.pathsep):
                if p.strip():
                    out.append(p.strip())
        pub = os.environ.get("ANDROID_PUBLIC", "")
        if pub:
            out.append(os.path.join(pub, "game"))
            pkg_dir = os.path.dirname(pub)                       # .../Android/data/<package>
            pkg = package or os.path.basename(pkg_dir)
            android_root = os.path.dirname(os.path.dirname(pkg_dir))   # .../Android
            obb = os.path.join(android_root, "obb", pkg)
            out.append(os.path.join(obb, "game"))
            out.append(obb)
        old = os.environ.get("ANDROID_OLD_PUBLIC", "")
        if old:
            out.append(os.path.join(old, "game"))
        return out

    def _add_searchpath(d):
        sp = renpy.config.searchpath
        for existing in sp:
            if _norm(existing) == _norm(d):
                return False
        sp.insert(0, d)
        return True

    def _register_archives(d):
        added = []
        try:
            names = sorted(os.listdir(d))
        except Exception:
            return added
        for fn in names:
            if not fn.lower().endswith(".rpa"):
                continue
            stem = fn[:-4]
            arcs = renpy.config.archives
            if stem not in arcs:
                arcs.append(stem)
            added.append(os.path.join(d, fn))
        return added

    def _rescan():
        loader = renpy.loader
        try:
            loader.loadable_cache.clear()
        except Exception:
            pass
        try:
            if hasattr(loader, "index_files"):          # Ren'Py 8.4+
                loader.index_files()
            else:
                loader.cleardirfiles()
                loader.scandirfiles()
        except Exception as exc:
            _log("rescan failed: %r" % (exc,))
        try:
            loader.index_archives()
        except Exception as exc:
            _log("index_archives failed: %r" % (exc,))

    def setup():
        global manifest, package, candidates, found, archives, probes, probes_missing, missing, error
        try:
            manifest = _read_manifest()
            package = str(manifest.get("package", "") or "")
            probes = list(manifest.get("probe", []) or [])
            candidates = _collect_candidates()
            for d in candidates:
                if not _readable_dir(d):
                    continue
                if _add_searchpath(d):
                    found.append(d)
                    _log("using external data: " + d)
                archives.extend(_register_archives(d))
            if found:
                _rescan()
            if probes:
                probes_missing = [p for p in probes if not renpy.loader.loadable(p)]
                missing = bool(probes_missing)
            if missing:
                _log("external data missing: %d probe file(s) not found (e.g. %s)" % (len(probes_missing), probes_missing[0]))
        except Exception as exc:
            error = repr(exc)
            _log("setup error: " + error)

    def report():
        """Dictionnaire d'état (utilisé par les tests et l'écran d'information)."""
        return {
            "package": package, "candidates": list(candidates), "found": list(found), "archives": list(archives),
            "probes": list(probes), "probes_missing": list(probes_missing), "missing": missing, "error": error,
            "searchpath": list(renpy.config.searchpath),
        }

    def expected_paths():
        pkg = package or "<package>"
        return [
            "Android/data/%s/files/game/" % pkg,
            "Android/obb/%s/game/" % pkg,
        ]

    def data_size_text():
        n = manifest.get("files", 0)
        b = manifest.get("bytes", 0)
        try:
            gb = float(b) / (1024.0 ** 3)
            size = ("%.1f Go / GB" % gb) if gb >= 1 else ("%d Mo / MB" % int(float(b) / (1024.0 ** 2)))
        except Exception:
            size = "?"
        return "%s fichiers / files, %s" % (n, size)

    setup()


init 999 python:
    if renpyhd_extdata.missing:
        import base64 as _renpyhd_b64
        # PNG 1×1 sombre (#141418) : le rappel doit renvoyer un manipulateur d'image (avec .load()), pas un Displayable.
        _renpyhd_missing_png = _renpyhd_b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR42mMQEZEAAACAAEEKWnqDAAAAAElFTkSuQmCC")

        def _renpyhd_missing_image(name):
            # Image absente (données externes non copiées) : aplat sombre à la taille de l'écran plutôt qu'une exception.
            return im.Scale(im.Data(_renpyhd_missing_png, "renpyhd_missing.png"), config.screen_width, config.screen_height)
        config.missing_image_callback = _renpyhd_missing_image

        def _renpyhd_show_missing():
            # Les écrans « overlay » ne sont pas affichés dans le menu principal : on montre l'écran avant chaque interaction.
            try:
                if not renpy.get_screen("renpyhd_extdata_missing"):
                    renpy.show_screen("renpyhd_extdata_missing")
            except Exception:
                pass
        config.interact_callbacks.append(_renpyhd_show_missing)
        if "renpyhd_extdata_missing" not in config.overlay_screens:
            config.overlay_screens.append("renpyhd_extdata_missing")


screen renpyhd_extdata_missing():
    zorder 1000
    modal True
    frame:
        xalign 0.5
        yalign 0.5
        xsize 0.92
        padding (36, 28, 36, 28)
        background Solid("#1b1b22e6")
        vbox:
            spacing 14
            text "Données du jeu introuvables / Game data not found" size 34 color "#ffffff" bold True
            text "Ce jeu a été construit par RenPyHD avec ses données (images, vidéos) séparées de l'APK. Copiez le dossier « game » du pack de données dans l'un de ces dossiers du téléphone, puis relancez le jeu :" size 22 color "#e8e8e8"
            text "This game was built by RenPyHD with its data (images, videos) kept outside the APK. Copy the data pack's \"game\" folder into one of these folders on the phone, then restart the game:" size 22 color "#e8e8e8"
            for p in renpyhd_extdata.expected_paths():
                text "  •  " + p size 24 color "#ffd166" font "DejaVuSans.ttf"
            text "Pack de données / data pack : " + renpyhd_extdata.data_size_text() size 20 color "#c0c0c8"
            text "Depuis le PC : RenPyHD › Android (APK) › « Copier les données sur le téléphone (adb) ». / From the PC: RenPyHD › Android (APK) › \"Copy the data to the phone (adb)\". Android 11+ : un gestionnaire de fichiers ne peut pas toujours écrire dans Android/data ; adb est la voie sûre. / A file manager may not be able to write to Android/data; adb is the reliable way." size 18 color "#a8a8b0"
            if renpyhd_extdata.probes_missing:
                text "Exemple de fichier attendu / expected file: " + renpyhd_extdata.probes_missing[0] size 18 color "#a8a8b0"
            hbox:
                spacing 30
                xalign 0.5
                textbutton "Réessayer / Retry" action Function(renpy.utter_restart) text_size 26
                textbutton "Quitter / Quit" action Quit(confirm=False) text_size 26
