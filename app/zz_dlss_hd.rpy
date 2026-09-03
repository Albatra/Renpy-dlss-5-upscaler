# zz_dlss_hd.rpy - chargeur du mod « HD » DLSS 5 pour Ren'Py 7.x / 8.x (RenPyHD)
#
# Ce fichier est installé par RenPyHD dans le dossier  game/  du jeu, à côté du
# dossier de sorties HD (hd2x/ par défaut).
#
# Images : chaque image créée à partir d'un nom de fichier (instructions image,
# ATL, screens, Frame(), Image()) est cherchée dans  <hd>/<même chemin>
# (l'extension peut différer : .webp / .png / .jpg). Si une version HD existe,
# elle est chargée à la place et affichée à la taille logique d'origine grâce à
# Transform(zoom = 1 / facteur) : positions, screens et interface ne bougent pas.
#
# Vidéos : Movie(play=...), renpy.movie_cutscene(...), « play movie » et tout
# renpy.music.play(...) sur un canal vidéo sont redirigés vers  <hd>/<chemin>
# (.webm / .mp4 / .ogv). Le facteur propre à chaque vidéo est lu dans
# <hd>/videos.json (RenPyHD peut baisser le facteur des vidéos déjà en 4K).
#   * Movie sans size= : Ren'Py affiche la vidéo à sa taille native en pixels ;
#     on l'enveloppe donc dans Transform(zoom = 1 / facteur) et on compense
#     start_image / image (déjà à la taille logique) par un zoom inverse.
#   * Movie(size=...) : Ren'Py adapte lui-même la vidéo à size, rien à faire.
#   * Cutscene plein écran (movie_cutscene, play movie) : Ren'Py adapte la
#     vidéo à l'écran, seul le fichier change.
#   * mask= : remplacé seulement si le masque a lui aussi une version HD au même
#     facteur (sinon la vidéo reste en SD pour garder les deux flux alignés).
#
# Pour revenir au jeu d'origine : supprimer ce fichier (et son .rpyc) ou le
# dossier HD. RenPyHD propose un bouton « Désinstaller le mod » qui fait cela.
#
# Compatible Python 2 (Ren'Py 7) et Python 3 (Ren'Py 8) : pas de f-strings ici.

init -1000 python:
    import json as _dlss_json
    import renpy.display.im as _dlss_im
    import renpy.display.motion as _dlss_motion
    import renpy.audio.music as _dlss_music
    import renpy.exports as _dlss_exports

    _dlss_hd_dir = "hd2x/"  # RENPYHD:DIR
    _dlss_hd_cache_mb = 1536  # RENPYHD:CACHE

    try:
        _dlss_hd_factor = float(renpy.file(_dlss_hd_dir + "factor.txt").read().strip())
    except Exception:
        _dlss_hd_factor = 2.0
    _dlss_hd_zoom = 1.0 / _dlss_hd_factor

    _dlss_str = basestring if str is bytes else str
    _dlss_orig_Image = Image
    _dlss_orig_Movie = Movie
    _dlss_hd_cache = {}
    _dlss_hd_stats = {"hd": 0, "sd": 0, "video": 0, "video_sd": 0}
    _dlss_hd_alt_exts = (".webp", ".png", ".jpg", ".jpeg")
    _dlss_video_exts = (".webm", ".mp4", ".ogv", ".mkv", ".avi")

    # ---- images ------------------------------------------------------------
    def _dlss_hd_path(filename):
        hd = _dlss_hd_cache.get(filename)
        if hd is None:
            name = filename.replace("\\", "/")
            # Ren'Py cherche aussi les images sous images/ (config.search_prefixes) : on fait pareil.
            bases = [name] if name.lower().startswith("images/") else [name, "images/" + name]
            candidates = []
            for base in bases:
                candidates.append(_dlss_hd_dir + base)
                stem, dot, ext = base.rpartition(".")
                if dot:
                    for alt in _dlss_hd_alt_exts:
                        if alt != "." + ext.lower():
                            candidates.append(_dlss_hd_dir + stem + alt)
            hd = ""
            for cand in candidates:
                if renpy.loader.loadable(cand):
                    hd = cand
                    break
            _dlss_hd_cache[filename] = hd
        return hd

    # ---- avant / après en jeu -------------------------------------------------
    # Maj+J bascule l'affichage : HD (normal) → original → écran partagé (la ligne suit la souris).
    _dlss_view_mode = "hd"
    _dlss_view_modes = ("hd", "sd", "split")
    _dlss_view_labels = {"hd": "HD (DLSS 5)", "sd": "Original", "split": "Avant | Apres (ligne = souris)"}
    _dlss_split_frac = 0.5
    import weakref as _dlss_weakref
    _dlss_compare_instances = _dlss_weakref.WeakSet()
    try:
        _DlssDisplayableBase = renpy.display.core.Displayable
    except Exception:
        _DlssDisplayableBase = renpy.display.displayable.Displayable

    class _DlssCompare(_DlssDisplayableBase):
        """Affiche la version HD, l'originale, ou les deux côte à côte selon _dlss_view_mode."""

        def __init__(self, sd, hd, **properties):
            super(_DlssCompare, self).__init__(**properties)
            self.sd = sd
            self.hd = hd
            _dlss_compare_instances.add(self)

        def visit(self):
            return [self.sd, self.hd]

        def predict_one(self):
            child = self.hd if _dlss_view_mode == "hd" else self.sd
            renpy.display.predict.displayable(child)
            if _dlss_view_mode == "split":
                renpy.display.predict.displayable(self.hd)

        def render(self, width, height, st, at):
            mode = _dlss_view_mode
            if mode == "hd":
                r = renpy.display.render.render(self.hd, width, height, st, at)
                rv = renpy.display.render.Render(r.width, r.height)
                rv.blit(r, (0, 0))
                return rv
            r_sd = renpy.display.render.render(self.sd, width, height, st, at)
            if mode == "sd":
                rv = renpy.display.render.Render(r_sd.width, r_sd.height)
                rv.blit(r_sd, (0, 0))
                return rv
            r_hd = renpy.display.render.render(self.hd, width, height, st, at)
            _dlss_view_update_mouse()
            w, h = r_sd.width, r_sd.height
            x = int(max(0, min(w, round(_dlss_split_frac * w))))
            rv = renpy.display.render.Render(w, h)
            if x > 0:
                rv.blit(r_sd.subsurface((0, 0, x, h)), (0, 0))
            if x < w:
                rv.blit(r_hd.subsurface((x, 0, w - x, h)), (x, 0))
            if 0 < x < w:
                line = renpy.display.render.render(Solid("#ffffff"), 2, h, st, at)
                rv.blit(line, (x - 1, 0))
            renpy.redraw(self, 0.05)
            return rv

        def get_placement(self):
            return self.hd.get_placement()

    def _dlss_view_update_mouse():
        """En mode partagé : la ligne suit la position horizontale de la souris."""
        global _dlss_split_frac
        try:
            mx, my = renpy.get_mouse_pos()
            _dlss_split_frac = max(0.0, min(1.0, float(mx) / float(config.screen_width)))
        except Exception:
            pass

    def _dlss_view_cycle():
        global _dlss_view_mode
        i = _dlss_view_modes.index(_dlss_view_mode)
        _dlss_view_mode = _dlss_view_modes[(i + 1) % len(_dlss_view_modes)]
        _dlss_view_invalidate()
        renpy.notify("RenPyHD : " + _dlss_view_labels[_dlss_view_mode])
        renpy.restart_interaction()

    def _dlss_view_invalidate():
        # Ren'Py garde les rendus en cache : on force le redessin de chaque image comparée.
        for inst in list(_dlss_compare_instances):
            try:
                renpy.redraw(inst, 0)
            except Exception:
                pass

    def _dlss_view_set(mode):
        global _dlss_view_mode
        _dlss_view_mode = mode
        _dlss_view_invalidate()
        renpy.restart_interaction()

    def Image(arg, loose=False, **properties):
        rv = _dlss_orig_Image(arg, loose=loose, **properties)
        if isinstance(rv, _dlss_im.Image):
            hd = _dlss_hd_path(rv.filename)
            if hd:
                _dlss_hd_stats["hd"] += 1
                hd_img = _dlss_orig_Image(hd, **properties)
                if _dlss_hd_zoom != 1.0:
                    hd_img = _dlss_motion.Transform(hd_img, zoom=_dlss_hd_zoom)
                return _DlssCompare(rv, hd_img)
            _dlss_hd_stats["sd"] += 1
        return rv

    # renpy.easy.displayable() passe par renpy.display.im.image : on le remplace aussi.
    _dlss_im.image = Image

    # ---- vidéos ------------------------------------------------------------
    _dlss_hd_videos = {}
    try:
        _dlss_hd_videos = _dlss_json.loads(renpy.file(_dlss_hd_dir + "videos.json").read().decode("utf-8")).get("videos", {})
    except Exception:
        _dlss_hd_videos = {}
    _dlss_hd_vcache = {}

    def _dlss_hd_video(name):
        """Pour un chemin de vidéo : (chemin HD, facteur) ou None."""
        if not isinstance(name, _dlss_str):
            return None
        hit = _dlss_hd_vcache.get(name)
        if hit is None:
            hit = False
            clean = name.replace("\\", "/")
            if not clean.startswith(_dlss_hd_dir):
                entry = _dlss_hd_videos.get(clean)
                if entry and renpy.loader.loadable(entry["file"]):
                    hit = (entry["file"], float(entry.get("factor", _dlss_hd_factor)))
                else:
                    stem, dot, ext = clean.rpartition(".")
                    if dot and ("." + ext.lower()) in _dlss_video_exts:
                        for alt in ("." + ext.lower(),) + _dlss_video_exts:
                            cand = _dlss_hd_dir + stem + alt
                            if renpy.loader.loadable(cand):
                                hit = (cand, _dlss_hd_factor)
                                break
            _dlss_hd_vcache[name] = hit
        return hit or None

    def _dlss_hd_play(play):
        """Chaîne ou liste de chaînes : (nouveau play, facteur) ou None si une des vidéos n'a pas de version HD."""
        if isinstance(play, _dlss_str):
            return _dlss_hd_video(play)
        if isinstance(play, (list, tuple)):
            out = []
            factor = None
            for item in play:
                hit = _dlss_hd_video(item)
                if not hit:
                    return None
                out.append(hit[0])
                factor = factor or hit[1]
            if out:
                return (out, factor)
        return None

    def Movie(*args, **kwargs):
        hit = _dlss_hd_play(kwargs.get("play"))
        if not hit:
            if kwargs.get("play"):
                _dlss_hd_stats["video_sd"] += 1
            return _dlss_orig_Movie(*args, **kwargs)
        new_play, vf = hit
        mask = kwargs.get("mask")
        if mask:
            mhit = _dlss_hd_play(mask)
            if not mhit or abs(mhit[1] - vf) > 1e-6:
                _dlss_hd_stats["video_sd"] += 1
                return _dlss_orig_Movie(*args, **kwargs)
            kwargs["mask"] = mhit[0]
        kwargs["play"] = new_play
        _dlss_hd_stats["video"] += 1
        size = kwargs.get("size", args[1] if len(args) > 1 else None)
        if size is not None or abs(vf - 1.0) < 1e-6:
            return _dlss_orig_Movie(*args, **kwargs)
        # Sans size=, la vidéo s'affiche à sa taille en pixels : on la ramène à la taille logique,
        # et on compense pour start_image / image qui sont déjà à la taille logique.
        for key in ("start_image", "image"):
            d = kwargs.get(key)
            if d is not None:
                kwargs[key] = _dlss_motion.Transform(renpy.easy.displayable(d), zoom=vf)
        return _dlss_motion.Transform(_dlss_orig_Movie(*args, **kwargs), zoom=1.0 / vf)

    # play movie / renpy.music.play(..., channel="movie") / cutscenes : le fichier est remplacé,
    # Ren'Py adapte les vidéos plein écran à la fenêtre.
    _dlss_orig_music_play = _dlss_music.play

    def _dlss_music_play(filenames, channel="music", *a, **k):
        try:
            is_movie = (channel == "movie") or bool(getattr(_dlss_music.get_channel(channel), "movie", 0))
        except Exception:
            is_movie = False
        if is_movie:
            hit = _dlss_hd_play(filenames)
            if hit:
                filenames = hit[0]
        return _dlss_orig_music_play(filenames, channel, *a, **k)

    _dlss_music.play = _dlss_music_play

    _dlss_orig_movie_cutscene = _dlss_exports.movie_cutscene

    def _dlss_movie_cutscene(filename, *a, **k):
        hit = _dlss_hd_play(filename)
        if hit:
            filename = hit[0]
            _dlss_hd_stats["video"] += 1
        return _dlss_orig_movie_cutscene(filename, *a, **k)

    _dlss_exports.movie_cutscene = _dlss_movie_cutscene
    renpy.movie_cutscene = _dlss_movie_cutscene

    # Les surfaces RGBA 4K pèsent ~33 Mo chacune : on élargit le cache d'images.
    if config.image_cache_size_mb < _dlss_hd_cache_mb:
        config.image_cache_size_mb = _dlss_hd_cache_mb

# Maj+H en jeu : nombre d'images et de vidéos remplacées. Maj+J : HD → original → écran partagé (avant | après).
init python:
    def _dlss_hd_report():
        renpy.notify("DLSS HD : %d HD / %d SD (zoom %.3f) — vidéos %d HD / %d SD" % (
            _dlss_hd_stats["hd"], _dlss_hd_stats["sd"], _dlss_hd_zoom,
            _dlss_hd_stats["video"], _dlss_hd_stats["video_sd"]))

screen _dlss_hd_hotkey():
    key "shift_K_h" action Function(_dlss_hd_report)
    key "shift_K_j" action Function(_dlss_view_cycle)

init python:
    config.overlay_screens.append("_dlss_hd_hotkey")
