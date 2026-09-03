# zz_renpyhd_verify.rpy — sonde de vérification RenPyHD (copiée temporairement dans la copie de construction, jamais livrée).
# Juste avant le menu principal : écrit un rapport JSON (label « start » présent, données externes trouvées, images témoins
# chargeables) dans RENPYHD_PROBE_OUT, prend une capture (RENPYHD_PROBE_SHOT) puis quitte. Compatible Ren'Py 7 (Python 2) et 8.

label before_main_menu:
    python:
        import json, os
        _rhd_out = os.environ.get("RENPYHD_PROBE_OUT", "")
        _rhd_rep = {"stage": "before_main_menu", "renpy_version": renpy.version(), "has_start": bool(renpy.has_label("start"))}
        try:
            _rhd_rep["extdata"] = renpyhd_extdata.report()
        except Exception as e:
            _rhd_rep["extdata"] = None
        _rhd_rep["probe_images"] = []
        try:
            for _p in (_rhd_rep["extdata"] or {}).get("probes", [])[:3]:
                try:
                    _surf = renpy.display.im.cache.get(renpy.display.im.Image(_p), render=False)
                    _rhd_rep["probe_images"].append([_p, list(_surf.get_size())])
                except Exception as e:
                    _rhd_rep["probe_images"].append([_p, "ERROR: " + repr(e)])
        except Exception:
            pass
        try:
            _rhd_rep["files"] = len(renpy.list_files())
        except Exception:
            _rhd_rep["files"] = -1
        try:
            _rhd_rep["main_menu_screen"] = renpy.has_screen("main_menu")
        except Exception:
            _rhd_rep["main_menu_screen"] = None
        if _rhd_out:
            with open(_rhd_out, "w") as _f:
                _f.write(json.dumps(_rhd_rep, indent=2))
    # rend le menu principal (les écrans et images du menu sont chargés ici), puis capture et quitte
    $ renpy.pause(2.0, hard=True)
    python:
        _shot = os.environ.get("RENPYHD_PROBE_SHOT", "")
        if _shot:
            try:
                renpy.screenshot(_shot)
                _rhd_rep["screenshot"] = _shot
            except Exception as e:
                _rhd_rep["screenshot"] = repr(e)
        _rhd_rep["stage"] = "main_menu_rendered"
        if _rhd_out:
            with open(_rhd_out, "w") as _f:
                _f.write(json.dumps(_rhd_rep, indent=2))
        renpy.quit()
    return
