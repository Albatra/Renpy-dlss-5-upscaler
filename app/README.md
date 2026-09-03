# RenPyHD — DLSS 5 Neural Rendering pour les jeux Ren'Py (documentation détaillée)

RenPyHD applique le **DLSS 5 Neural Rendering** de NVIDIA (via le *DLSS 5 Visual Enhancer* de Merserk) à toutes les
images **et vidéos** d'un jeu Ren'Py, avec une visionneuse avant/après, un extracteur d'archives `.rpa` et un assistant de
traduction. Interface en français, anglais, espagnol, allemand, russe ou portugais du Brésil (menu en haut à droite,
option `--lang xx`, détection de la langue de Windows au premier lancement). Aucune installation : le dossier `RenPyHD`
se copie où l'on veut, une fois `setup.bat` exécuté.

```
RenPyHD\
  RenPyHD.exe        lanceur (console visible : journal + URL de l'interface ; relance après un changement de langue)
  run.bat            lancement de secours sans l'exe
  app\               renpy_hd_app.py (interface Gradio), renpy_hd_core.py (moteur DLSS), renpy_hd_tools.py (extraction .rpa,
                     traduction), renpy_hd_i18n.py + i18n\<code>.json (langues), zz_dlss_hd.rpy (hook Ren'Py), README.md
  tools\             rpaExtract.exe (extracteur .rpa tiers, optionnel, non fourni)
  DLSS5\             DLSS 5 Visual Enhancer (téléchargé par setup.bat) : Python 3.13 embarqué, moteur DLSS, FFmpeg
```

## Prérequis
* Windows 10/11 64 bits, carte **NVIDIA RTX 40 ou 50** (RTX 30 : chemin expérimental, très lent), pilote récent
  (≥ 610 pour NVENC).
* Espace disque : en 2×, les sorties pèsent environ 4× les images d'origine.
* **Une seule instance DLSS à la fois sur la machine** (voir plus bas).

## Utilisation
1. Double-cliquer sur `RenPyHD.exe`. Une console s'ouvre, puis une fenêtre « application » (Edge/Chrome en mode `--app`,
   sinon le navigateur par défaut) sur `http://127.0.0.1:<port>` (l'URL est affichée dans la console).
2. Onglet **Améliorer un jeu**, cinq étapes numérotées (chaque étape n'apparaît qu'une fois la précédente faite) :
   1. **Choisir le jeu** : « Parcourir… » → dossier du jeu (celui qui contient `game\`, à côté du `.exe`). L'analyse se
      lance toute seule et résume en clair : nom du jeu, version de Ren'Py, résolution, nombre d'images et de vidéos, déjà
      amélioré ou non, images trop petites ignorées, introuvables.
   2. **Extraire les archives (.rpa)** : si des fichiers ne sont disponibles que dans des archives, la carte propose
      « Extraire les archives » (moteur Python intégré, barre de progression, fichiers existants conservés) ou « Passer
      cette étape » (RenPyHD lit directement dans les archives). Sans archive : « Aucune archive : rien à extraire ».
   3. **Que voulez-vous améliorer ?** : cases **Images** (cochée) et **Vidéos** (décochée ; nombre, taille et durée
      estimée affichés — chaque image de chaque vidéo passe dans DLSS, c'est long), préréglage Neural Rendering (défaut
      « Visages (K : tout au max) ») et facteur d'agrandissement (2×). « Continuer ».
   4. **Aperçu** : 10 images tirées au hasard (1–20, Mode expert › Aperçu) — et 3 secondes d'une vidéo si les vidéos
      sont cochées — sont améliorées dans `app\preview\<jeu>\`, jamais dans le jeu, et affichées avant/après (curseur
      glissant, Précédent / Suivant, loupe 1:1 et temps par image dans l'accordéon). Une ligne d'estimation donne le
      nombre de fichiers, la durée et la place disque. « Régénérer l'aperçu » tire d'autres images.
   5. **Améliorer le jeu** : un seul gros bouton. Barre de progression, pourcentage, compteur, temps restant, ligne de
      statut ; journal complet et liste des échecs dans « Détails ». **Annuler** arrête proprement ; cliquer à nouveau
      reprend là où on s'était arrêté. À la fin : « Jouer » (lance l'exécutable du jeu ; Maj+H en jeu affiche le compteur
      d'images remplacées), « Comparer avant / après », « Améliorer un autre jeu », « Désinstaller le mod ».

   Un jeu déjà amélioré est reconnu à l'analyse : l'étape 5 s'ouvre directement avec Jouer / Comparer / Désinstaller.
   Tout le reste est dans l'accordéon **Mode expert** en bas de l'onglet (replié par défaut) : mode de traitement, hook,
   qualité, réglages Neural Rendering, formats de sortie, sélection des images et analyse détaillée (bouton « Analyser
   (rapport détaillé) »), vidéos (codec, CRF, plafond, audio…), aperçu, « Lancer sans aperçu », enregistrer / charger la
   configuration. Tout changement dans le Mode expert (ou à l'étape 3) invalide l'aperçu : il faut le régénérer avant
   « Améliorer le jeu » (ou « Lancer sans aperçu »).
3. Choisir le mode (Mode expert › Mode de traitement ; défaut : HD 2x) :
   * **HD 2x (recommandé)** : sorties dans `game\hd2x\`, hook `zz_dlss_hd.rpy` installé dans `game\`. Facteurs 1.5 / 1.724 / 2 / 3.
   * **Remplacer sur place (1×, DLAA)** : originaux copiés dans `game\_dlss_backup\`, puis écrasés (même format).
     Ren'Py travaille en pixels logiques : seul le 1× est possible en remplacement.
   * **Suffixe @2 natif (Ren'Py ≥ 8.4, sans hook)** : écrit `<nom>@2.<ext>` à côté de chaque original (facteur forcé 2×) ;
     Ren'Py 8.4+ charge ces variantes tout seul. Les fichiers créés sont listés dans `game\_dlss_at2.json` pour la désinstallation.
     Les vidéos ne sont pas traitées dans ce mode.
   * **Dossier d'images libre** : dossier → dossier, arborescence conservée, tous formats (PNG, JPEG, WebP, AVIF, TIFF).

   Jeux sans fichiers `.rpy` (scripts compilés dans un `.rpa`, ex. Ren'Py 8) : l'analyse bascule automatiquement sur
   « Toutes les images du dossier » (`game\images` + entrées `images/` des `.rpa`, toutes les vidéos du dossier) et lit la
   résolution dans `log.txt`. Les images dont le petit côté est inférieur à « Dimension minimale » (256 px par défaut) sont
   ignorées : elles font échouer la vérification DLSS. Un échec n'arrête jamais le lot ; les images échouées sont retentées une par une.
4. Pendant le traitement, les images sont traitées d'abord, puis les vidéos si l'option est cochée (progression par vidéo
   **et** par image, cadence DLSS, temps restant). Les sorties existantes sont sautées (reprise).
5. Onglet **Comparer / Tester**, trois sous-onglets :
   * **Comparer avant / après** : le dossier du jeu de l'étape 1 est repris ; « Charger la liste » puis curseur glissant entre
     l'original et le résultat, loupe 1:1 (original ×2 au plus proche contre la même zone HD), navigation Précédent / Suivant,
     dimensions et tailles de fichiers. Les paires de **vidéos** y figurent aussi : un curseur « instant comparé » extrait
     l'image correspondante des deux vidéos, et les deux lecteurs s'affichent.
   * **Tester une image** : une seule image (chemin, « Parcourir… » ou dépôt de fichier) passée dans DLSS avec les réglages
     courants, résultat dans `app\preview\single\`, comparaison avant/après + loupe, « Enregistrer le résultat sous… ».
   * **Tester une vidéo** : une seule vidéo, avec « Limiter à N secondes » (5 s par défaut) pour un essai rapide ; résultat
     dans `app\preview\single_video\`, trois images extraites aux mêmes instants (curseur avant/après + loupe), les deux
     vidéos lisibles côte à côte, « Enregistrer la vidéo sous… ».
6. **Désinstaller le mod** (étape 5) supprime `game\hd2x\` (images, vidéos, `videos.json`), `zz_dlss_hd.rpy(.rpyc)` et les
   fichiers `@2` créés ; **Restaurer les originaux** (mode Remplacer) recopie `_dlss_backup\` (images et vidéos) et supprime
   les fichiers libres extraits d'un `.rpa`.

**Attention : une seule instance DLSS à la fois sur la machine.** Deux traitements simultanés (par exemple RenPyHD et un
autre script du Visual Enhancer, ou deux fenêtres RenPyHD) partagent `bin\runtime\ReShade.log` et se font mutuellement
échouer la vérification (« signed DLSSNR feature-18 execution was not verified »). RenPyHD sérialise déjà ses propres
traitements (images, vidéos, aperçus, tests) ; n'en lancez pas un autre à côté. Un autre programme GPU (diffusion, jeu) peut
tourner en parallèle, il ralentit seulement le rendu.

## Mode expert
Tous les réglages du moteur sont exposés dans l'accordéon **Mode expert** de l'onglet « Améliorer un jeu » : style / preset /
intensité NR, local tone, local structure, skin structure, masque automatique, warmup, preset de modèle DLSS (J/K/L/M),
format de sortie par type de source, qualité, métadonnées ; plus les nôtres : nom du dossier de sortie, extensions incluses,
préfixes exclus (`gui/` par défaut), regex d'inclusion / exclusion, lecture des `.rpa`, taille de lot, limite, retraitement,
dimensions min/max, installation automatique du hook, `config.image_cache_size_mb` écrit dans le hook, simulation (dry-run).
Les préréglages (Visages / Équilibré / Fidèle / Cinéma / Portrait-peau, étape 3) remplissent les curseurs NR.
« Enregistrer / Charger la configuration » utilise `app\renpy_hd_config.json` (les choix de listes y sont stockés sous forme
de clés internes, indépendantes de la langue ; la langue de l'interface y est enregistrée sous `ui_lang`).

## Vidéos
### Ce qui est fait
* **Détection** : vidéos citées par les scripts `.rpy` (`Movie(play=…)`, `renpy.movie_cutscene(…)`, `play movie`, chaînes
  `"….webm"`), plus tout le contenu de `game\movies`, `game\videos`, `game\video` (fichiers libres et entrées `.rpa`).
  Sans `.rpy`, toutes les vidéos du dossier `game\`. Ren'Py cherche les vidéos sous `game\`, `game\audio\` et `game\images\` :
  RenPyHD fait pareil.
* **Rendu** : chaque image de la vidéo passe dans DLSS 5 (module `src\video.py` de l'outil : flux optique DIS comme guide de
  mouvement, réinitialisation temporelle aux changements de plan — désactivable —, warmup, mêmes réglages Neural Rendering
  que les images ou réglages propres aux vidéos). L'outil produit un intermédiaire H.264/HEVC quasi sans perte
  (NVENC si disponible, sinon x264/x265 *ultrafast* qp 4), que RenPyHD réencode ensuite pour Ren'Py.
* **Codec de sortie** (à choisir selon la version de Ren'Py du jeu ; l'analyse l'affiche) :

  | Codec | Conteneur | Lu par | Encodeur |
  |---|---|---|---|
  | **VP9** (défaut) | WebM | Ren'Py 6.99 → 8.x | libvpx-vp9 (logiciel, ~12 i/s en 4K) |
  | VP8 | WebM | Ren'Py 6.99 → 8.x | libvpx |
  | AV1 | WebM | Ren'Py ≥ 8.1 seulement (décodeur `av1`/libaom présent dans `librenpython.dll`), décodage lourd | av1_nvenc ou SVT-AV1 |
  | H.264 | MP4 | Ren'Py 7/8 sur PC (décodeurs `h264`/`hevc` présents) — pas sur mobile/web | h264_nvenc ou libx264 |

  CRF 0–63 (VP9/AV1 ≈ 31, VP8 10–20, H.264 18–23, borné à 51), vitesse 0–5. En mode **Remplacer sur place**, seules les
  sources dont l'extension correspond au conteneur choisi sont réencodées (`.webm` ↔ VP8/VP9/AV1, `.mp4` ↔ H.264).
* **Audio** : la piste d'origine est **copiée telle quelle** quand le conteneur l'accepte (Opus/Vorbis → WebM, AAC/MP3 → MP4),
  sinon réencodée (Opus ou AAC au débit choisi) ; ou supprimée.
* **Plafond de résolution** (3840×2160 par défaut, 1080p → 8K au choix) : pour une vidéo dont `source × facteur` dépasse le
  plafond, RenPyHD prend le plus grand facteur DLSS qui tient (jusqu'à **1× DLAA**, qui améliore sans agrandir) ou ignore la
  vidéo. Le facteur réel de chaque vidéo est écrit dans `game\hd2x\videos.json`, lu par le hook.
* **Hook Ren'Py** (`zz_dlss_hd.rpy`, Python 2 et 3) : `Movie(play=…)` est redirigé vers la version HD ; sans `size=`, Ren'Py
  affiche une vidéo à sa taille native en pixels, donc le Movie est enveloppé dans `Transform(zoom=1/facteur)` et
  `start_image` / `image` reçoivent le zoom inverse ; avec `size=`, Ren'Py adapte lui-même la vidéo ; `mask=` n'est remplacé
  que si le masque a aussi une version HD au même facteur. `renpy.movie_cutscene`, `play movie` et tout `renpy.music.play` sur
  un canal vidéo reçoivent le fichier HD. Listes de fichiers (`play=[…]`) gérées. Maj+H en jeu affiche le nombre d'images et
  de vidéos remplacées.
* **Robustesse** : reprise (sorties existantes sautées), annulation (le rendu DLSS et l'encodage ffmpeg sont interrompus,
  les fichiers temporaires supprimés), échecs reportés sans arrêter le lot, sondage ffprobe mis en cache
  (`app\video_probe_cache.json`).

### Performances mesurées (RTX 4090, Ren'Py 8.1.2, jeu 3840×2160)
* DLSS 5 : **≈ 10,5 images/s** en 1080p → 4K (2×), ≈ 20 images/s en 720p → 1440p (2×), ≈ 6,6 images/s en 4K DLAA (1×).
  Encodage VP9 final : ≈ 12 images/s en 4K. Soit, pour une vidéo 1080p 30 i/s : **≈ 1,5 minute de calcul par minute de vidéo**
  (une bibliothèque de 6 h de vidéo 1080p demande donc ~9 h). Le réencodage VP9 est logiciel : un CPU rapide compte.
* Lecture dans le jeu (décodage logiciel de Ren'Py) : la sortie 3840×2160 @ 30 i/s VP9 est décodée à **30,0 i/s**, comme
  l'original 1080p ; une vidéo 4K @ 60 i/s d'origine tient 59,5 i/s. **Au-delà de la 4K (8K), Ren'Py ne suit plus** et DLSS
  plafonne à 7680×4320 : gardez le plafond par défaut 3840×2160, ou 2560×1440 sur un CPU modeste.
* NVENC : cette version de FFmpeg (9.0.x) exige l'API NVENC 13.1 (pilote NVIDIA ≥ 610). Avec un pilote plus ancien,
  l'intermédiaire passe automatiquement par x264/x265 *ultrafast* (le journal l'indique) ; H.264/AV1 finals passent alors par
  libx264 / SVT-AV1.

### Limites connues
* Un `Movie(channel="movie")` sans `play=` (qui affiche ce que joue `play movie`) sans `size=` s'afficherait à la taille
  native de la vidéo HD : cas rare, non corrigé par le hook (utiliser `size=`).
* Les vidéos dans un `.rpa` ne sont pas sondées avant traitement (facteur demandé appliqué tel quel, plafond vérifié au rendu).
* AV1 dans Ren'Py 8.1 n'a pas été validé en lecture réelle (décodeur présent, performance inconnue) ; VP9 reste le choix sûr.
* Le mode « Suffixe @2 » ignore les vidéos ; le mode « Dossier libre » ne traite que des images.

## Onglet « Outils »
Deux sous-onglets, chacun en trois étapes numérotées. Le dossier du jeu est partagé avec l'onglet principal. L'URL accepte
`?tab=tab_tools&sub=sub_rpa|sub_tl&game=<dossier>` pour ouvrir directement un outil sur un jeu.

### Extraire les archives (.rpa)
1. **Choisir le jeu** : les archives `game\*.rpa` sont listées (taille, nombre de fichiers, images, reste à extraire).
2. **Archives et options** : cases par archive ; « ne pas écraser les fichiers existants » (coché : reprise, rien de perdu) ;
   « renommer l'archive en `.rpa.bak` » (décoché : Ren'Py fonctionne dans les deux cas, les fichiers libres ayant priorité ;
   renommer ne sert qu'à vérifier que le jeu tourne sans l'archive — retirer le `.bak` annule). Moteur **Python intégré**
   (classe `Rpa` du moteur : index RPA-2.0/3.0, fichier par fichier, barre de progression, annulable, reprenable) ou
   **rpaExtract.exe** (outil tiers optionnel basé sur *unrpa*, à placer dans `tools\` ; voir `tools\README.md`).
3. **Extraire** : fichiers écrits dans `game\` avec les chemins de l'archive ; résumé (fichiers, taille, durée, erreurs).

### Traduire le jeu
Traduction **faite par vous** (aucun moteur automatique, aucune clé, rien d'envoyé nulle part par RenPyHD), **sans modifier les
scripts du jeu** : système de traduction natif de Ren'Py (`game\tl\<langue>\`) et un fichier `game\zz_renpyhd_lang.rpy`
(langue par défaut au premier lancement, **Maj+L** en jeu pour basculer).
1. **Extraire les textes** : commande `translate` du moteur Ren'Py **du jeu** —
   `lib\windows-i686\python.exe -EO <Jeu>.py . translate <langue> --no-todo` (Ren'Py 7) ou
   `lib\py3-windows-x86_64\python.exe -EO <Jeu>.py . translate <langue> --no-todo` (Ren'Py 8), attendue de façon synchrone
   (sinon `<Jeu>.exe` puis attente de la fin du processus). Un fichier temporaire `zz_renpyhd_tl.rpy` (retiré ensuite) étend
   la liste des fichiers traduits aux scripts compilés : sans lui Ren'Py ignore les `.rpyc` seuls ou archivés dans un `.rpa`.
   Il écrit aussi la liste des langues déjà présentes dans le jeu. Un dossier `tl\<langue>` existant qui n'est pas de RenPyHD
   n'est jamais écrasé : « Fusionner » ne complète que les textes manquants. `renpyhd_translation.json` marque ce que RenPyHD a créé.
   Langues cibles proposées : french, english, spanish, german, russian, portuguese (brazil), italian, chinese (simplified),
   japanese, korean, turkish, polish, et d'autres (noms de dossiers `tl` de Ren'Py).
2. **Exporter pour traduction** : `phrase_001.txt`, `phrase_002.txt`… (au plus 10 000 lignes **et** 1 Mo chacun, réglables ; nom
   de base réglable ; UTF-8 sans BOM, fins de ligne Windows) dans `game\tl\<langue>\renpyhd_export\` (ouvert dans l'Explorateur)
   + `phrase_map.json`. Une ligne par texte : `ligne1;texte` (numéros globaux à partir de 1 — format éprouvé avec
   onlinedoctranslator.com ; `§0001§ texte` en option). Balises `{i}…{/i}`, `{w}`, `{p}`, `{size=}`, `{color=}`, `{a=}`,
   échappements → repères `[t1]`, `[t2]`… (option « Protéger les balises ») ; interpolations `[name]`, `%(x)s` laissées telles
   quelles. « Quoi exporter » : dialogues et choix seulement (défaut) ou tout, interface comprise.
3. **Importer et installer** : fichiers `.txt` de n'importe quel nom ou dossier, dans n'importe quel ordre, encodage UTF-8 /
   cp1252 / latin-1 détecté ; lignes reconnues par leur numéro (`ligne1;`, `ligne 1 ;`, `Ligne1:`, `line1;`, `§1§`, `§ 1 §`),
   repères `[t n]` et interpolations vérifiés ; bilan traduits / non traduits / erreurs / lignes sans numéro / lignes fusionnées /
   doublons / numéros manquants. Un nouvel import remplace les traductions précédentes. Échantillon de 30 textes avant/après
   éditable (« Enregistrer les corrections »). « Installer » écrit `zz_renpyhd_lang.rpy` (`config.default_language`,
   `_preferences.language` au premier lancement via un drapeau `persistent`, écran overlay `key "shift_K_l"`). « Vérifier en
   lançant le jeu » lance le jeu avec un `zz_renpyhd_check.rpy` temporaire (langue active, blocs chargés, chaînes traduites,
   puis `os._exit`) et affiche `traceback.txt` en cas d'erreur. « Désinstaller » retire le hook et le dossier `tl\<langue>` créé.

Limites : les textes dessinés dans des images ne sont pas traduits ; un jeu dont la langue est déjà intégrée dans ses
archives ne produit rien ; les traducteurs qui fusionnent ou renumérotent des lignes provoquent des « numéros manquants ».
(Des moteurs automatiques — modèles Argos via CTranslate2, Google via deep-translator — restent dans `renpy_hd_tools.py`
derrière `AUTO_ENGINES_ENABLED = False`, sans interface.)

### Android (APK)
Construit un APK du jeu avec les outils officiels de Ren'Py — le **SDK Ren'Py** et **RAPT** (Ren'Py Android Packaging Tool) —
pilotés par leur propre ligne de commande, sans aucune question (module `renpy_hd_android.py`, table `android_matrix.json`,
adaptateur `renpyhd_android_adapter.rpy` copié dans `launcher\game\` du SDK sous le nom `zz_renpyhd_android.rpy`).
Tout est rangé dans `android\` à côté de l'application : `sdk\<version>\` (SDK + `rapt\`, Android SDK dans `rapt\Sdk`),
`jdk\` (Temurin portable), `keys\` (`android.keystore`, `bundle.keystore` — **à sauvegarder**), `unrpyc\`, `build\<jeu>\`
(copie de construction), `out\<jeu>\` (APK), `gradle\` (cache), `logs\`.

1. **Choisir le jeu** — version de Ren'Py, `.rpy` présents pour chaque `.rpyc`, `hd2x*` / `_dlss_backup` / hook (toujours
   exclus), images, vidéos, archives `.rpa` déjà extraites, taille estimée.
2. **Préparer l'environnement** — une fois par version : `renpy-<ver>-sdk.zip` + `renpy-<ver>-rapt.zip` (renpy.org),
   JDK Temurin **8** (Ren'Py ≤ 7.6 / 8.1) ou **21** (Ren'Py ≥ 7.7 / 8.2), outils Android pré-téléchargés
   (`commandlinetools-win-<n>.zip` ou `sdk-tools-windows-<n>.zip`, numéro lu dans `rapt\buildlib\rapt\plat.py`), puis
   `python.exe -EO renpy.py launcher renpyhd_android_installsdk --org <nom> [--keys-dir android\keys]` (interface RAPT
   scriptée : conditions acceptées via `RAPT_NO_TERMS`, réponses « oui », licences via `sdkmanager --licenses`).
   Ren'Py 7.6+/8.1+ : clés par projet (`rapt\buildlib\rapt\keys.py`) ; 7.0–7.5/8.0 : clé dans `rapt\android.keystore`.
   Version absente : correctif suivant de la même série, sinon dernière version de la même majeure. Ren'Py 7.0–7.3 : le RAPT
   d'origine (Gradle 4.4, `jcenter()` + `dl.bintray.com`, fermé) ne construit plus → dernier Ren'Py 7 (7.8.x) proposé.
3. **Configurer** — `.android.json` (clés RAPT : `package`, `name`, `icon_name`, `version`, `numeric_version`, `orientation`,
   `permissions`, `include_pil`, `include_sqlite`, `layout`, `source`, `expansion`, `google_play_key`, `google_play_salt`,
   `store`, `update_icons`, `update_always` + `heap_size`, `update_keystores` pour 7.4+/8.x), icônes
   `android-icon_foreground.png` / `android-icon_background.png` générées depuis `gui/window_icon.png`, vidéos, limite de
   taille des images (dossiers pris dans l'ordre), archives `.rpa` déjà extraites exclues, `.rpyc` du jeu utilisés tels quels
   (recommandé quand le SDK n'a pas la version exacte : le SDK ne recompile pas les `.rpy`), app bundle `.aab`.
4. **Construire** — copie de construction puis commande officielle du lanceur : Ren'Py 7.0–7.3
   `renpy.py launcher android_build <projet> assembleRelease --destination <dossier>`, Ren'Py 7.4+/8.x
   `renpy.py launcher android_build <projet> [--bundle] --destination <dossier>` (`JAVA_HOME` = JDK portable,
   `GRADLE_USER_HOME` = `android\gradle`, démon Gradle désactivé). Sortie : APK universel (arm64-v8a, armeabi-v7a, x86_64),
   vérification `zipfile` + `apksigner verify` (build-tools installés par Gradle), « Ouvrir le dossier », « Installer sur le
   téléphone » (`platform-tools\adb.exe install -r`).

5. **Données séparées (gros jeux)** — `BuildConfig.data_mode = "external"` : `stage_build` met dans la copie de construction
   uniquement scripts, `gui/`, polices, audio (sauf `ext_audio`) et envoie images, vidéos, `.rpa` restants dans le pack
   `android\out\<jeu>\<paquet>-data\game\` (liens physiques `os.link` si même volume, sinon copies) ; il écrit dans `game\`
   de la copie le hook `zz_renpyhd_extdata.rpy` (source `renpyhd_extdata.rpy`, Python 2/3) et le manifeste
   `renpyhd_extdata.json` (paquet, nombre/taille des fichiers, 3 images témoins). Sur le téléphone, le moteur Ren'Py met
   nativement `ANDROID_PUBLIC/game` = `/sdcard/Android/data/<paquet>/files/game/` en tête de `config.searchpath`
   (`renpy/main.py` 7.3.5, `android_searchpath` 8.1.2, `predefined_searchpath` 7.8.7 / 8.6.0) ; le hook ajoute les replis
   `Android/obb/<paquet>/game/` et `ANDROID_OLD_PUBLIC/game`, indexe les `.rpa` trouvés (`config.archives` + réindexation),
   accepte `RENPYHD_EXTDATA` pour les tests sur PC, et si les images témoins manquent installe `config.missing_image_callback`
   et un écran overlay bilingue (chemins exacts, Réessayer / Quitter). `adb_push_data` : `adb shell mkdir -p` puis `adb push
   <pack>\game /sdcard/Android/data/<paquet>/files/` (dossier privé de l'app : accessible à adb et à l'app même sous Android
   11+ ; un gestionnaire de fichiers sur le téléphone ne peut en général pas y écrire, l'USB/MTP depuis Windows oui).
   Recherche RAPT : l'expansion native (`.android.json` `expansion`, OBB `main.<code>.<paquet>.obb`, `ANDROID_EXPANSION`,
   `DownloaderActivity` Google Play) n'existe complète que dans RAPT 7.3.5 ; 7.8.7 / 8.1.2 / 8.6.0 n'ont plus que le champ
   `expansion = False` (8.1.2 garde du code mort dans `loader.py`) et passent par les asset packs Play (`ANDROID_PACK_FF1..4`,
   bundle uniquement). Limites d'installation : format ZIP sans ZIP64 → 4 Go absolus ; en pratique ≈ 2 Go (entiers 32 bits
   signés dans plusieurs installateurs) — documentation Ren'Py : « Universal APKs can be up to 2 GB in size ».
6. **Mes APK** — `write_build_manifest` écrit `android\out\<jeu>\build.json` (jeu, paquet, version, code, date, SDK, famille,
   mode, APK, pack, signature) ; `list_builds` relit ces manifestes et déduit ceux des dossiers antérieurs (nom de l'APK,
   `android.json` de la copie, journaux) ; `delete_build`, `adb_uninstall`, `list_caches` / `delete_cache` (jamais `keys\`),
   `export_keys`.

Limites : APK ≤ ≈ 2 Go (4 Go absolus) — au-delà, données séparées ; pas de clé Google Play ; Ren'Py < 7.0 non pris en charge ; Ren'Py ≥ 7.8/8.3
prend la version de l'APK dans `config.version` du jeu (chiffres et points seulement, sinon `1.0`) ; les scripts décompilés par unrpyc
ne servent qu'à la copie de construction. Drapeaux : `ANDROID_BUNDLE_ENABLED`, `ANDROID_ADB_ENABLED`, `ANDROID_UNRPYC_ENABLED`.

## Langue de l'interface
`app\i18n\<code>.json` : `fr` (référence), `en`, `es`, `de`, `ru`, `pt-BR`. Toute clé absente d'une langue retombe sur le
français. Ordre de choix : `--lang xx`, puis `ui_lang` dans `renpy_hd_config.json`, puis langue d'affichage de Windows, sinon
anglais. Le menu « Langue » enregistre le choix ; « Redémarrer maintenant » fait sortir le serveur avec le code 75, que
`RenPyHD.exe` et `run.bat` interprètent comme « relancer » (même port, la page se recharge toute seule).
Pour ajouter une langue : copier `fr.json` en `<code>.json`, traduire, ajouter le code dans `renpy_hd_i18n.LANGUAGES`.

## Lancement manuel (sans l'exe)
```
set PYTHONNOUSERSITE=1
DLSS5\bin\python-3.13.15-embed-amd64\python.exe app\renpy_hd_app.py --tool DLSS5 [--port 7860] [--no-browser] [--lang en]
```
`renpy_hd_core.py` est aussi importable pour scripter un traitement (voir `build_game_plan` / `run_plan`, `VideoSettings`).

## Licences
Code de RenPyHD (`app\`) : MIT. Le dossier `DLSS5\` (téléchargé par `setup.bat`) regroupe le DLSS 5 Visual Enhancer (Merserk,
MIT) et des composants NVIDIA (nvngx, DLSS NR — licence NVIDIA RTX SDK), FFmpeg (GPLv3), ReShade (BSD-3) et RenoDX (MIT),
chacun sous sa propre licence : ne les redistribuez pas séparément. Voir `THIRD_PARTY.md` à la racine du dépôt.
