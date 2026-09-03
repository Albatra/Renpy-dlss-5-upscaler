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

    def Image(arg, loose=False, **properties):
        rv = _dlss_orig_Image(arg, loose=loose, **properties)
        if isinstance(rv, _dlss_im.Image):
            hd = _dlss_hd_path(rv.filename)
            if hd:
                _dlss_hd_stats["hd"] += 1
                hd_img = _dlss_orig_Image(hd, **properties)
                if _dlss_hd_zoom == 1.0:
                    return hd_img
                return _dlss_motion.Transform(hd_img, zoom=_dlss_hd_zoom)
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

# Optionnel : Maj+H en jeu affiche combien d'images et de vidéos ont été remplacées.
init python:
    def _dlss_hd_report():
        renpy.notify("DLSS HD : %d HD / %d SD (zoom %.3f) — vidéos %d HD / %d SD" % (
            _dlss_hd_stats["hd"], _dlss_hd_stats["sd"], _dlss_hd_zoom,
            _dlss_hd_stats["video"], _dlss_hd_stats["video_sd"]))

screen _dlss_hd_hotkey():
    key "shift_K_h" action Function(_dlss_hd_report)

init python:
    config.overlay_screens.append("_dlss_hd_hotkey")
