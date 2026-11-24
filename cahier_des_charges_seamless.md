# Cahier des Charges Technique et Fonctionnel – Seamless (v1.1)

## 1. Vue d'ensemble du Projet

### 1.1 Objectif

Développer une application desktop cross-platform (Linux / Windows) permettant d'assurer la gestion musicale autonome d'une soirée (Seamless). Le logiciel doit permettre d'importer une bibliothèque de morceaux, d'enchaîner des transitions fluides sans aucun silence, de gérer dynamiquement la file d'attente (ajout à chaud, téléchargement YouTube), et de baisser le son à la volée pour les prises de parole.

### 1.2 Environnement de Développement & Tests Local

- Dossier de travail du projet : racine du dépôt (clone local).
- Dossier de musiques de test : dossier local au choix, configurable via `SEAMLESS_MUSIC_DIR` (ou `--music-dir`).

### 1.3 Public Cible et Contexte

Utilisation lors d'événements/soirées privées. L'application doit fonctionner en local (hors-ligne pour la lecture), être très stable, consommer peu de ressources et proposer une interface épurée et réactive.

## 2. Spécifications Techniques & OS

- **Plateformes cibles** :
  - Principale : Linux (Zorin OS / Ubuntu / Debian-based).
  - Secondaire : Windows 10 / 11.
  - Optionnel (Mac OS) : Code portable (Python) sans dev spécifique.
- **Architecture** :
  - Backend Python 3.12 (FastAPI local + vanilla HTML/JS) — moteur audio via ffmpeg (pipes PCM) + sounddevice + numpy.
  - Gestion vidéo/audio web : intégration du binaire yt-dlp en local.
- **Mode de fonctionnement** : 100 % local pour la partie DJ/Lecture (hors-ligne, sauf téléchargement YouTube).

## 3. Fonctionnalités Détaillées

### 3.1 Indexation et Importation

- Source des fichiers : dossier local (configurable via `SEAMLESS_MUSIC_DIR` / `--music-dir`) + glisser-déposer de fichiers audio (MP3, WAV, FLAC, AAC, M4A, OGG).
- **Banque / Réserve (Panier de musiques)** : toutes les musiques importées ou téléchargées arrivent dans cette réserve globale.
- Scan des métadonnées basiques (Titre, Artiste, Durée) via ffprobe.

### 3.2 Moteur Seamless et Transitions (Règle d'Or : Zéro Silence)

**Version 1 (Cible immédiate) : Crossfade Intelligent & Silence Stripping**

- **Buffer & Préchargement dynamique** : ne précharger et n'analyser que les 3 à 4 pistes suivantes dans la file (pas toute la soirée en RAM d'un coup).
- **Détection et Suppression des Silences (Trim Silent Cue)** :
  - Analyse amplitude/RMS pour repérer le « début réel » (Cue-In) et la « fin réelle » (Cue-Out) du signal sonore.
  - Règle absolue : ne jamais lire le silence en fin de morceau. La transition s'amorce dès que le niveau sonore repasse sous le seuil critique (ex : −35 dBFS).
- **Fondu Croisé (Crossfade) Adaptatif** : fondu enchaîné réglable (4 à 8 secondes), déclenché automatiquement au point Cue-Out.

**Version 2 (Évolution future — hors périmètre v1.1)** : Beatmatching & Tempo Sync (détection BPM, alignement de phase, time-stretching).

**Contrôles de lecture** : PLAY, PAUSE, SKIP (force la transition immédiate vers la piste suivante).

### 3.3 Module de Téléchargement Direct (yt-dlp)

- Interface : champ d'URL YouTube / YT Music + bouton de validation.
- Traitement en tâche de fond : téléchargement de l'audio haute qualité.
- Destination : envoi direct dans la Banque / Panier de musiques (panneau droit, dossier `downloads/` du projet).
- Notification visuelle discrète quand le titre est prêt.

### 3.4 Gestion de la File d'Attente (Queue Live)

- Réorganisation dynamique : glisser-déposer depuis la Banque vers la file, ou réorganisation directe dans la file.
- Ordre de passage clair (Piste 1 en cours, Piste 2 à suivre, etc.).
- Option clic droit : « Placer en piste suivante » (priorité absolue).

### 3.5 Mode Micro / Ducking (ESPACE)

- Appui (ou maintien) sur ESPACE = mode Talkover : baisse instantanée du volume master (ex : 20-30 %) avec rampe douce (~100 ms).
- Nouvel appui sur ESPACE = rétablissement à 100 %.
- Pas d'entrée micro capturée par le logiciel (micro géré externe/système).

### 3.6 Sécurité & Persistance (Auto-Save / Crash Recovery)

- Sauvegarde automatique permanente de l'état (piste en cours, position de lecture en secondes, file d'attente ordonnée).
- Au relancement après crash/coupure : proposition « Reprendre la soirée au même endroit ».

## 4. Architecture de l'Interface Utilisateur (UI)

Deux colonnes principales + barre inférieure :

```
+------------------------------------+------------------------------------+
| COLONNE GAUCHE : FILE D'ATTENTE    | COLONNE DROITE : PANIER / BANQUE   |
| (Live Playlist / Prochains titres) | (Musiques locales + Téléchargées)  |
|                                    |                                    |
| [NOW PLAYING] Title 1 - Artist     | [ Zone de recherche / Input URL ]  |
| ---------------------------------- | [ Bouton Download YT ]             |
| 1. Title 2 (Prochain)              | ---------------------------------- |
| 2. Title 3                         | - Track A.mp3                      |
| 3. Title 4                         | - Downloaded_Song.mp3              |
| (Glisser-déposer pour ordonner)    | (Glisser vers la gauche pour ajouter)|
+------------------------------------+------------------------------------+
| BARRE INFÉRIEURE : LECTEUR & CONTROLES MASTER                           |
| [PLAY/PAUSE] [SKIP] | Progress Bar (Waveform) | Volume | [DUCKING]      |
+-------------------------------------------------------------------------+
```

## 5. Exigences Non-Fonctionnelles

- **Stabilité** : tolérance zéro aux crashs en lecture. Fichier corrompu → saut automatique à la piste suivante + alerte visuelle discrète.
- **Ressources** : faible consommation CPU/RAM (laptop standard Linux/Windows).
- **Packaging** : Linux → `.AppImage`/`.deb` ; Windows → `.exe` portable/installateur. *(v1 : lanceur `lancer.sh` + raccourci .desktop ; packaging binaire en suivi.)*

## 6. Déroulement Agent (réalisé)

Phases exécutées dans l'ordre : Audio Engine → File & Persistance → Downloader → UI → Intégration & Vérification.
