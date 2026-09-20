# Lineup

**Compose ta liste de chaînes IPTV, une chaîne qui marche à la fois.**

Lineup est une petite application self-hosted qui fusionne plusieurs
playlists `.m3u`, te laisse choisir précisément quelles chaînes garder,
regrouper plusieurs liens pour une même chaîne avec bascule automatique
en cas de panne, puis exporte le résultat comme une playlist `.m3u`
propre — prête pour Jellyfin, VLC, ou tout autre lecteur compatible M3U.

![Image Docker](https://img.shields.io/badge/ghcr.io-m3u--lineup-blue)

---

## ✨ Fonctionnalités

- **Multi-sources** : ajoute autant de liens `.m3u` que tu veux, leurs
  chaînes sont fusionnées et cherchables depuis une seule interface.
- **Recherche & filtres** : par nom ou par groupe.
- **Fallback automatique** : regroupe plusieurs liens pour une même
  chaîne (ex. une source HD + une source de secours) — si le premier ne
  répond pas, le suivant est essayé automatiquement.
- **Rafraîchissement automatique** : relance le téléchargement de tes
  sources à une fréquence personnalisable.
- **Support HLS** : les manifests `.m3u8` sont détectés et réécrits pour
  que le lecteur récupère les segments directement depuis la source.
- **Cache de logos** : les images des chaînes sont mises en cache
  localement, une seule fois.
- **Léger** : un seul conteneur Python/Flask + SQLite, pensé pour tourner
  sur un petit VPS (entre 20 et 40MB de RAM).

## 🚀 Démarrage rapide

Aucun besoin de cloner le dépôt ni de builder quoi que ce soit — l'image
est publiée automatiquement sur GitHub Container Registry.

Crée un fichier `docker-compose.yml` :

```yaml
services:
  lineup:
    image: ghcr.io/loicbl0ndeau/m3u-lineup:latest
    container_name: lineup
    restart: unless-stopped
    environment:
      - DB_PATH=/data/lineup.sqlite3
      - LISTEN_PORT=9999
      - FETCH_TIMEOUT=20
      - STREAM_TIMEOUT=8
      - TZ=Europe/Paris
    volumes:
      - lineup-data:/data
    ports:
      - "127.0.0.1:9999:9999"

    # --- hardening ---
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    cap_add:
      - CHOWN      # entrypoint.sh doit pouvoir chown /data au démarrage
      - SETUID     # requis par su-exec pour passer à l'utilisateur non-root
      - SETGID     # idem, côté groupe
    security_opt:
      - no-new-privileges:true

volumes:
  lineup-data:
```

Puis :

```bash
docker compose up -d
```

L'interface est disponible sur `http://<ton-serveur>:9999`.

## 🖥️ Utilisation

1. Dans **Sources m3u**, ajoute un ou plusieurs liens `.m3u`. Les
   chaînes apparaissent fusionnées dans la liste de recherche.
2. Cherche une chaîne, clique **+** → crée une nouvelle chaîne dans ta
   lineup, ou ajoute le lien comme secours à une chaîne déjà créée.
3. Réordonne les liens d'une chaîne (le premier est essayé en premier),
   active/désactive, renomme ou supprime depuis **Ta lineup**.
4. Clique **Exporter le .m3u** pour télécharger la playlist finale, ou
   donne directement l'URL `http://<ton-serveur>:9999/api/export` à ton
   lecteur (dans Jellyfin : **Dashboard → Live TV → Tuner Devices → M3U
   Tuner**).

## ⚙️ Configuration

Variables d'environnement, toutes optionnelles :

| Variable            | Défaut                    | Description                                      |
|----------------------|---------------------------|---------------------------------------------------|
| `DB_PATH`            | `/data/lineup.sqlite3`    | Emplacement de la base SQLite                     |
| `LOGO_CACHE_DIR`      | `/data/logos`             | Dossier de cache des logos de chaînes             |
| `LISTEN_PORT`         | `9999`                    | Port HTTP interne au conteneur                    |
| `FETCH_TIMEOUT`       | `20`                      | Timeout (s) pour télécharger un m3u source         |
| `STREAM_TIMEOUT`      | `8`                       | Timeout (s) pour tester/proxier un flux            |
| `STREAM_USER_AGENT`   | `Mozilla/5.0 (Lineup)`    | User-Agent envoyé aux serveurs IPTV                |

## 🧠 Comment ça marche

- Chaque source `.m3u` est parsée et stockée en base ; ses chaînes sont
  identifiées par leur `tvg-id` (ou leur nom si absent).
- Une "chaîne" de ta lineup est une liste ordonnée de liens source.
  `/stream/<id>` essaie chaque lien dans l'ordre et sert le premier qui
  répond — pour un manifest HLS, seuls les chemins relatifs sont réécrits
  en URLs absolues, les segments sont ensuite chargés directement depuis
  le serveur d'origine.
- Au rafraîchissement d'une source (manuel ou automatique), Lineup
  retrouve les chaînes de ta lineup issues de cette source et met à jour
  leur URL si elle a changé, avant de régénérer la liste des chaînes
  disponibles. `/api/export` lit toujours la base en direct : le fichier
  exporté reflète immédiatement tout changement.

## 🛠️ Développement / build local

```bash
git clone https://github.com/loicblondeau/m3u-lineup.git
cd m3u-lineup
docker build -t lineup-dev .
docker run --rm -p 9999:9999 -v lineup-dev-data:/data lineup-dev
```

Un push sur `main` republie automatiquement l'image via GitHub Actions
(`.github/workflows/docker-publish.yml`).

## ⚠️ Limites connues

- Le fallback entre liens est vérifié à l'ouverture du flux, pas en
  cours de lecture — si un lien tombe en panne pendant la lecture, il
  faut relancer la lecture côté lecteur pour déclencher un nouvel essai.
- Une chaîne ajoutée à ta lineup depuis une source ensuite supprimée
  n'est plus rattachée à aucune source et ne sera plus réconciliée
  automatiquement.
- Process unique, pas de queue/cache lourd : pensé pour un usage
  personnel avec quelques flux simultanés, pas pour de la diffusion à
  grande échelle.
- Aucune authentification intégrée — si tu exposes le port au-delà de
  `127.0.0.1`, mets une authentification devant via ton reverse proxy.