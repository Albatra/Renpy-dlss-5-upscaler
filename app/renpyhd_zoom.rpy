# renpyhd_zoom.rpy — RenPyHD « Zoom en jeu » / in-game zoom (Android et PC).
#
# Installé par RenPyHD dans game\ (APK : case « Zoom en jeu » de l'étape 3 ; PC : Mode expert › « Installer le zoom en jeu »).
# Le zoom ne s'applique qu'au calque « master » (décors et personnages) par renpy.show_layer_at(..., layer="master") :
# la fenêtre de dialogue, les menus et les écrans ne bougent pas. Le fichier est autonome : le supprimer (avec son .rpyc)
# rend le jeu strictement d'origine. Aucun clic normal n'est avalé : un simple appui avance toujours le dialogue.
#
#   Tactile (un seul doigt — Ren'Py ne transmet pas le pincement) :
#     * appui long (doigt maintenu ZOOM_HOLD = 0,45 s sans bouger) : zoom d'un cran centré sur le point touché
#       (1x → 2x → 3x → 1x, liste ZOOM_STEPS) ; le bouton « x2 » s'illumine pour dire de relâcher ; le relâchement est
#       avalé (le dialogue n'avance pas). Un appui court reste un appui normal : rien n'est avalé ni retardé ;
#     * glisser (au-delà de quelques pixels) quand l'image est zoomée : déplacement, sans jamais montrer de bord vide ;
#     * petit bouton « x1 / x2 / x3 » en haut à droite (toujours visible si ZOOM_BUTTON) qui passe au cran suivant,
#       et « 1:1 » (visible seulement en zoom) qui revient à 1x ;
#     * en option (ZOOM_DOUBLETAP = True, désactivé par défaut) : double-appui = cran suivant — mais le premier appui
#       d'un double-appui avance le dialogue d'une ligne, comme n'importe quel appui.
#   PC :
#     * clic long : cran suivant centré sur le pointeur ; Maj+Z : cran suivant centré sur l'écran ;
#     * Ctrl + molette au-dessus de l'image : zoom continu autour du pointeur (la molette seule garde son rôle Ren'Py :
#       retour arrière / avance) ; glisser au bouton droit (ou gauche) : déplacement ; clic droit simple : menu du jeu.
#
# Installed by RenPyHD into game\ (APK: step 3 "In-game zoom" box; PC: Expert mode > "Install the in-game zoom").
# Only the "master" layer (backgrounds, sprites) is zoomed through renpy.show_layer_at(..., layer="master"); the dialogue
# window, menus and screens stay put. Touch (single finger, Ren'Py does not report pinch): a long press (finger held
# ZOOM_HOLD = 0.45 s without moving) zooms one step around the touched point (1x → 2x → 3x → 1x), the "x2" button lights up
# to say "release", and that release is swallowed (the dialogue does not advance); a short tap stays a normal tap (nothing
# swallowed, no delay). Drag pans while zoomed (never shows an empty border); "x1 / x2 / x3" cycles and "1:1" resets.
# Optional ZOOM_DOUBLETAP (off): double-tap cycles, but its first tap advances the dialogue one line. PC: long click,
# Shift+Z, Ctrl + mouse wheel (the wheel alone keeps its Ren'Py role: rollback / rollforward), right-button (or
# left-button) drag pans. Deleting this file (and its .rpyc) restores the original game.
#
# Vérifié / verified: renpy.show_layer_at(at_list, layer) existe tel quel de Ren'Py 7.1 à 8.6 (scene_lists.layer_at_list) ;
# le transform est ré-appliqué à chaque interaction (config.interact_callbacks) car « scene » efface les transforms de calque.
# Compatible Python 2 (Ren'Py 7) et Python 3 (Ren'Py 8) : pas de f-strings ici.

init -900 python in renpyhd_zoom:

    import time as _time
    import types as _types
    import pygame_sdl2 as pygame
    import renpy.display.render as _render

    ZOOM_STEPS = [1.0, 2.0, 3.0]  # RENPYHD:ZOOM_STEPS
    ZOOM_BUTTON = True  # RENPYHD:ZOOM_BUTTON
    ZOOM_HOLD = 0.45  # RENPYHD:ZOOM_HOLD — appui long (s), 0 = désactivé / long press (s), 0 = off
    ZOOM_DOUBLETAP = False  # RENPYHD:ZOOM_DOUBLETAP — double-appui en plus (son premier appui avance le dialogue)
    LAYER = "master"
    WHEEL_STEP = 1.25            # Ctrl + molette : facteur par cran / per wheel notch
    MAX_ZOOM = max(4.0, max(ZOOM_STEPS))
    FLASH_TIME = 0.8             # s : le bouton « x2 » s'illumine quand l'appui long a déclenché le zoom
    DOUBLE_TAP_TIME = 0.35       # s
    DOUBLE_TAP_DIST = 48         # px (coordonnées virtuelles du jeu)
    DRAG_THRESHOLD = 12          # px avant qu'un appui devienne un glissement
    BUTTON_SIZE = 26             # taille du texte des boutons

    # État hors rollback (module Python simple : Ren'Py ne le suit pas) — le zoom reste celui choisi par le joueur,
    # même après un retour arrière ou un changement de décor ; le transform de calque est ré-appliqué à chaque interaction.
    # Un objet Python nu suffit (types.ModuleType exige un nom « str » natif : sous Ren'Py 7.4+ le store aliase str vers
    # unicode et le constructeur plantait — « module.__init__() argument 1 must be string, not unicode »).
    class _ZoomState(object):
        pass
    _st = _ZoomState()
    _st.zoom = 1.0
    _st.x = 0.0          # xpos du calque (<= 0)
    _st.y = 0.0
    _st.trans = None     # notre Transform dans la liste at du calque
    _st.active = None    # copie du Transform en cours de rendu (renpy.redraw)
    _st.down = None      # (x, y, temps, bouton) appui en cours
    _st.last_pos = None
    _st.moved = False
    _st.fired = False    # l'appui en cours a déjà déclenché le zoom (appui long)
    _st.flash_until = 0.0
    _st.last_tap = None  # (x, y, temps) dernier appui gauche (option double-appui)
    _st.swallow = set()  # boutons dont le relâchement doit être ignoré (fin de glissement, appui long, 2e appui)

    def _size():
        return float(renpy.config.screen_width), float(renpy.config.screen_height)

    def _clamp(z, x, y):
        if z <= 1.0 + 1e-6:
            return 1.0, 0.0, 0.0
        w, h = _size()
        x = min(0.0, max(w - w * z, x))
        y = min(0.0, max(h - h * z, y))
        return z, x, y

    def _tf(trans, st, at):
        """Fonction du Transform de calque : lit l'état à chaque rendu (aucun redémarrage d'interaction pour le déplacement)."""
        _st.active = trans
        trans.zoom = _st.zoom
        trans.xpos = int(round(_st.x))
        trans.ypos = int(round(_st.y))
        trans.xanchor = 0
        trans.yanchor = 0
        trans.subpixel = True
        return None

    def _current_list():
        try:
            entry = renpy.game.context().scene_lists.layer_at_list.get(LAYER)
        except Exception:
            entry = None
        return list(entry[1]) if entry else []

    def _sync(restart):
        """Met (ou retire) notre Transform dans la liste des transforms du calque ; sinon simple redessin."""
        if _st.trans is None:
            _st.trans = renpy.store.Transform(function=_tf)
        cur = _current_list()
        others = [t for t in cur if t is not _st.trans]
        want = others + [_st.trans] if _st.zoom > 1.0 + 1e-6 else others
        if len(want) != len(cur) or any(a is not b for a, b in zip(want, cur)):
            try:
                renpy.show_layer_at(want, layer=LAYER)
            except Exception:
                return
            if restart:
                renpy.restart_interaction()
        elif _st.active is not None:
            try:
                _render.redraw(_st.active, 0)
            except Exception:
                pass

    def _ensure():
        # config.interact_callbacks : appelé au début de chaque interaction, avant la composition des calques —
        # « scene » vide layer_at_list, un rollback le remet dans un état antérieur : on ré-applique l'état courant.
        try:
            _sync(False)
        except Exception:
            pass

    def get():
        """(zoom, xpos, ypos) courants — utilisé par les tests."""
        return _st.zoom, _st.x, _st.y

    def set_zoom(z, cx=None, cy=None, keep_point=False, restart=True):
        """Fixe le zoom. (cx, cy) : point de l'écran (coordonnées virtuelles) ; keep_point : ce point reste sous le pointeur
        (molette), sinon il devient le centre de l'écran (double-appui). Sans point : centre de l'écran."""
        w, h = _size()
        z = max(1.0, min(MAX_ZOOM, float(z)))
        if cx is None or cy is None:
            cx, cy = w / 2.0, h / 2.0
        fx = (cx - _st.x) / _st.zoom     # point de l'image sous (cx, cy)
        fy = (cy - _st.y) / _st.zoom
        if keep_point:
            x, y = cx - fx * z, cy - fy * z
        else:
            x, y = w / 2.0 - fx * z, h / 2.0 - fy * z
        _st.zoom, _st.x, _st.y = _clamp(z, x, y)
        _sync(restart)

    def pan(dx, dy):
        _st.zoom, _st.x, _st.y = _clamp(_st.zoom, _st.x + dx, _st.y + dy)
        _sync(False)

    def cycle(cx=None, cy=None):
        nxt = None
        for s in ZOOM_STEPS:
            if s > _st.zoom + 1e-3:
                nxt = s
                break
        if nxt is None:
            nxt = min(ZOOM_STEPS)
        set_zoom(nxt, cx, cy)

    def reset():
        set_zoom(1.0)

    def zoomed():
        return _st.zoom > 1.0 + 1e-6

    def label():
        z = _st.zoom
        return ("x%d" % int(round(z))) if abs(z - round(z)) < 0.05 else ("x%.1f" % z)

    def _ctrl_held():
        try:
            return bool(pygame.key.get_mods() & (pygame.KMOD_CTRL | pygame.KMOD_META))
        except Exception:
            return False

    def _in_menu():
        try:
            return renpy.context_nesting_level() > 0
        except Exception:
            return False

    def _ignore():
        try:
            return renpy.display.core.IgnoreEvent()
        except Exception:
            return renpy.display.displayable.IgnoreEvent()

    def _hold_pending():
        d = _st.down
        return d is not None and d[3] == 1 and not _st.moved and not _st.fired and ZOOM_HOLD > 0

    def _check_hold(now):
        """Appui long : déclenche le zoom dès que le bouton gauche est maintenu ZOOM_HOLD s sans bouger (doigt encore posé).
        Appelé sur tout événement, dont les TIMEEVENT demandés par renpy.timeout ; le relâchement qui suit est avalé."""
        if not _hold_pending() or _in_menu():
            return
        d = _st.down
        held = now - d[2]
        if held < ZOOM_HOLD:
            try:
                renpy.timeout(max(0.01, ZOOM_HOLD - held + 0.02))   # garantit un événement à l'échéance
            except Exception:
                pass
            return
        _st.fired = True
        _st.swallow.add(1)
        _st.last_tap = None
        _st.flash_until = now + FLASH_TIME
        cycle(d[0], d[1])

    def _handle(ev, x, y):
        """Observe les événements souris/tactiles sans les consommer (sauf : relâchement après appui long, glissement ou
        double-appui, et Ctrl + molette). Retourne None pour laisser passer l'événement."""
        now = _time.time()
        if ev.type == pygame.MOUSEBUTTONDOWN:
            b = ev.button
            if b in (4, 5):
                if _ctrl_held() and not _in_menu():
                    set_zoom(_st.zoom * (WHEEL_STEP if b == 4 else 1.0 / WHEEL_STEP), x, y, keep_point=True, restart=False)
                    raise _ignore()
                return None
            if b == 1:
                _st.down = (x, y, now, 1)
                _st.last_pos = (x, y)
                _st.moved = False
                _st.fired = False
                if _in_menu():
                    _st.last_tap = None
                    return None
                if ZOOM_DOUBLETAP:
                    lt = _st.last_tap
                    if lt is not None and (now - lt[2]) < DOUBLE_TAP_TIME and abs(x - lt[0]) < DOUBLE_TAP_DIST and abs(y - lt[1]) < DOUBLE_TAP_DIST:
                        _st.last_tap = None
                        _st.fired = True
                        _st.swallow.add(1)
                        cycle(x, y)
                        return None
                    _st.last_tap = (x, y, now)
                _check_hold(now)      # arme le délai (renpy.timeout)
            elif b == 3:
                _st.down = (x, y, now, 3)
                _st.last_pos = (x, y)
                _st.moved = False
                _st.fired = False
            return None
        _check_hold(now)              # TIMEEVENT, mouvement, relâchement… : l'appui long se déclenche doigt posé
        if ev.type == pygame.MOUSEMOTION:
            d = _st.down
            if d is None or _in_menu():
                return None
            buttons = getattr(ev, "buttons", None)
            if buttons is not None and not buttons[0 if d[3] == 1 else 2]:
                _st.down = None
                return None
            lx, ly = _st.last_pos
            _st.last_pos = (x, y)
            if not _st.moved:
                if abs(x - d[0]) < DRAG_THRESHOLD and abs(y - d[1]) < DRAG_THRESHOLD:
                    return None
                _st.moved = True          # annule l'appui long en attente ; en zoom, c'est un glissement
                _st.last_tap = None
                if zoomed():
                    _st.swallow.add(d[3])
            if zoomed():
                pan(x - lx, y - ly)
            return None
        if ev.type == pygame.MOUSEBUTTONUP:
            b = ev.button
            if _st.down is not None and _st.down[3] == b:
                _st.down = None
            if b in _st.swallow:
                _st.swallow.discard(b)
                raise _ignore()
            return None
        return None

    try:
        _DisplayableBase = renpy.display.core.Displayable
    except Exception:
        _DisplayableBase = renpy.display.displayable.Displayable

    class Watcher(_DisplayableBase):
        """Displayable invisible de l'écran overlay : reçoit tous les événements (calque overlay, au-dessus des écrans)."""

        def __init__(self, **properties):
            super(Watcher, self).__init__(**properties)

        def render(self, width, height, st, at):
            return _render.Render(0, 0)

        def event(self, ev, x, y, st):
            try:
                return _handle(ev, x, y)
            except renpy.display.core.IgnoreEvent:
                raise
            except Exception:
                return None

    def _label_cb(st, at):
        # Ctrl + molette ne redémarre pas l'interaction : l'étiquette se rafraîchit toute seule tant qu'on est en zoom.
        # Après un appui long, l'étiquette s'illumine FLASH_TIME s (signal « relâchez »).
        if _time.time() < _st.flash_until:
            return renpy.store.Text(label(), style="renpyhd_zoom_text_flash"), 0.1
        return renpy.store.Text(label(), style="renpyhd_zoom_text"), (0.2 if zoomed() else None)

    renpy.config.interact_callbacks.append(_ensure)


style renpyhd_zoom_frame is default:
    background None
    xalign 1.0
    yalign 0.0
    xoffset -8
    yoffset 8

style renpyhd_zoom_button is default:
    background "#00000088"
    hover_background "#000000cc"
    padding (10, 4)
    xminimum 56
    xalign 0.5

style renpyhd_zoom_text is default:
    color "#ffffffcc"
    hover_color "#ffffff"
    size 26
    xalign 0.5
    outlines [(1, "#00000080", 0, 0)]

style renpyhd_zoom_text_flash is renpyhd_zoom_text:
    color "#ffe066"
    size 30

screen renpyhd_zoom_overlay():
    zorder 900
    add renpyhd_zoom.Watcher()
    key "shift_K_z" action Function(renpyhd_zoom.cycle)
    if renpyhd_zoom.ZOOM_BUTTON:
        frame style "renpyhd_zoom_frame":
            hbox:
                spacing 6
                button style "renpyhd_zoom_button" action Function(renpyhd_zoom.cycle):
                    add DynamicDisplayable(renpyhd_zoom._label_cb)
                if renpyhd_zoom.zoomed():
                    button style "renpyhd_zoom_button" action Function(renpyhd_zoom.reset):
                        text "1:1" style "renpyhd_zoom_text"   # ASCII : la police du jeu n'a pas forcément le glyphe ↺

init 999 python:
    # Fonction publique (tests, autres scripts) : _renpyhd_zoom_set(2.0, cx, cy) ; _renpyhd_zoom_reset().
    def _renpyhd_zoom_set(z, cx=None, cy=None):
        renpyhd_zoom.set_zoom(z, cx, cy)

    def _renpyhd_zoom_reset():
        renpyhd_zoom.reset()

    if "renpyhd_zoom_overlay" not in config.overlay_screens:
        config.overlay_screens.append("renpyhd_zoom_overlay")
