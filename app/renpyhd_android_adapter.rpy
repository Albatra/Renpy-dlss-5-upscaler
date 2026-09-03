# zz_renpyhd_android.rpy — copié par RenPyHD dans <SDK Ren'Py>\launcher\game\.
# Ajoute au lanceur Ren'Py une commande non interactive « renpyhd_android_installsdk » qui pilote RAPT
# (Ren'Py Android Packaging Tool) sans poser de questions : conditions du SDK Android acceptées par
# l'utilisateur dans RenPyHD, réponses « oui » aux questions de RAPT, clés de signature générées avec le nom
# d'organisation fourni. Compatible Ren'Py 7 (Python 2) et Ren'Py 8 (Python 3).
#
#   <sdk>\lib\<plateforme>\python.exe -EO renpy.py launcher renpyhd_android_installsdk [--keys-dir D] [--org NOM]
#   <sdk>\lib\<plateforme>\python.exe -EO renpy.py launcher renpyhd_android_state
#
# La construction elle-même passe par la commande officielle « android_build » du lanceur.

init 1 python:
    import os
    import sys
    import json

    def _renpyhd_print(s):
        try:
            sys.stdout.write(s + "\n")
            sys.stdout.flush()
        except Exception:
            pass

    if rapt is not None:

        class RenPyHDInterface(rapt.interface.Interface):
            """Interface RAPT scriptée : jamais de question, tout est tracé sur la sortie standard."""

            def __init__(self, org="RenPyHD"):
                self.org = org

            def write(self, s, style=""):
                _renpyhd_print(s)

            def info(self, prompt):
                _renpyhd_print("[info] " + prompt)

            def success(self, prompt):
                _renpyhd_print("[ok] " + prompt)

            def final_success(self, prompt):
                _renpyhd_print("[done] " + prompt)

            def yesno(self, prompt):
                _renpyhd_print("[auto-yes] " + prompt)
                return True

            def yesno_choice(self, prompt, default=None):
                _renpyhd_print("[auto-yes] " + prompt)
                return True

            def terms(self, url, prompt):
                _renpyhd_print("[terms] " + url + " (accepted in RenPyHD)")

            def input(self, prompt, empty=None):
                rv = empty if empty else self.org
                _renpyhd_print("[auto-input] " + prompt + " -> " + str(rv))
                return rv

            def choice(self, prompt, choices, default=None):
                rv = default if default is not None else choices[0][0]
                _renpyhd_print("[auto-choice] " + prompt + " -> " + str(rv))
                return rv

            def fail(self, prompt):
                _renpyhd_print("RENPYHD_FAIL: " + prompt)
                raise SystemExit(1)

            def open_directory(self, directory, prompt):
                _renpyhd_print("[keys] " + directory)

            def background(self, f):
                f()

            def download(self, url, dest):
                # RenPyHD pre-downloads the archive (with its own certificate store); RAPT then finds it in place.
                if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                    _renpyhd_print("[download] already present: " + dest)
                    return
                _renpyhd_print("[download] " + url)
                try:
                    import requests
                    resp = requests.get(url, stream=True)
                    total = int(resp.headers.get("Content-Length") or 0)
                    done = 0
                    last = -1
                    with open(dest, "wb") as f:
                        for chunk in resp.iter_content(1 << 20):
                            f.write(chunk)
                            done += len(chunk)
                            if total:
                                pct = int(100 * done / total)
                                if pct // 5 != last // 5:
                                    last = pct
                                    _renpyhd_print("[download] %d%% (%d / %d MB)" % (pct, done >> 20, total >> 20))
                    _renpyhd_print("[download] done (%d MB)" % (done >> 20))
                    return
                except ImportError:
                    pass
                try:
                    from urllib.request import urlopen
                except ImportError:
                    from urllib2 import urlopen
                resp = urlopen(url)
                try:
                    total = int(resp.headers.get("Content-Length") or 0)
                except Exception:
                    total = 0
                done = 0
                last = -1
                f = open(dest, "wb")
                try:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = int(100 * done / total)
                            if pct // 5 != last // 5:
                                last = pct
                                _renpyhd_print("[download] %d%% (%d / %d MB)" % (pct, done >> 20, total >> 20))
                finally:
                    f.close()
                _renpyhd_print("[download] done (%d MB)" % (done >> 20))

            def call(self, args, cancel=False, use_path=False, yes=False):
                _renpyhd_print("[run] " + " ".join(str(a) for a in args))
                return rapt.interface.Interface.call(self, args, cancel=cancel, use_path=use_path, yes=yes)

        def _renpyhd_installsdk_command():
            ap = renpy.arguments.ArgumentParser()
            ap.add_argument("--keys-dir", default=None, help="Directory receiving android.keystore / bundle.keystore (Ren'Py 7.6+ / 8.1+).")
            ap.add_argument("--org", default="RenPyHD", help="Organisation name used in the signing key.")
            args = ap.parse_args()
            os.environ["RAPT_NO_TERMS"] = "1"
            iface = RenPyHDInterface(args.org)
            rapt.install_sdk.install_sdk(iface)
            if args.keys_dir and hasattr(rapt, "keys"):
                if not os.path.isdir(args.keys_dir):
                    os.makedirs(args.keys_dir)
                rapt.keys.generate_keys(iface, args.keys_dir)
            _renpyhd_print("RENPYHD_INSTALL_OK")
            return False

        def _renpyhd_state_command():
            ap = renpy.arguments.ArgumentParser()
            ap.parse_args()
            state = {
                "rapt": rapt.plat.RAPT_PATH,
                "sdk": rapt.plat.sdk,
                "adb": rapt.plat.adb,
                "adb_exists": os.path.exists(rapt.plat.adb),
                "sdkmanager": rapt.plat.sdkmanager,
                "java_home": os.environ.get("JAVA_HOME", ""),
                "keys_module": hasattr(rapt, "keys"),
                "rapt_keystore": os.path.exists(rapt.plat.path("android.keystore")),
            }
            _renpyhd_print("RENPYHD_STATE " + json.dumps(state))
            return False

        renpy.arguments.register_command("renpyhd_android_installsdk", _renpyhd_installsdk_command)
        renpy.arguments.register_command("renpyhd_android_state", _renpyhd_state_command)
