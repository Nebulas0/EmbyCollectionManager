# Emby Collection Manager

Automatically generates and syncs movie collections in your Emby server using TMDb, Trakt, and MDBList. Includes a full web UI for managing everything without touching config files.

Fork of [d3v1l1989/EmbyCollectionManager](https://github.com/d3v1l1989/EmbyCollectionManager) with added Web UI, per-collection library selection, artwork management, recipe overrides/duplicates, and Saltbox integration.

## Features

### Web UI (port 8282)
- **Dashboard** — sync control, next sync time, sync history, quick status
- **Collections** — browse all 459 built-in recipes across 13 categories with live search/filter
- **Libraries** — select which Emby libraries to scan globally
- **Settings** — all config options with API key masking, test buttons, notifications, export/import
- **Trakt Lists** — create/edit/delete Trakt list files with per-collection library selection
- **MDBLists** — create/edit/delete MDBList files with per-collection library selection
- **Artwork** — preview, upload, reset, and delete collection artwork in Emby
- **Logs** — live sync log viewer with auto-refresh and download

### Collection Management
- 459 pre-configured collection recipes across 13 categories
- Per-recipe overrides: custom name, item limit, target libraries, extra MDBList/Trakt URLs, extra TMDb IDs, custom artwork
- Duplicate recipes (e.g. one "Popular Movies" for 1080p, one for 4K)
- Single-recipe test sync from the UI
- Collection preview — see what TMDb IDs would be in a collection before syncing
- Enable/disable individual recipes or entire categories

### Per-Collection Library Selection
- Each Trakt/MDBList file can target specific Emby libraries
- Each recipe override can target specific libraries
- Each duplicate can target specific libraries
- Falls back to global library setting if not specified

### YAML List Format
Both Trakt and MDBList support `.yaml` files in addition to `.txt`:
```yaml
collection_name: My Merged Collection
library_ids:
  - lib_id_1
  - lib_id_2
items:
  - https://mdblist.com/lists/user/list1
  - https://mdblist.com/lists/user/list2
  - 12345
  - "Movie Title"
```
- Multiple URLs merged into one collection
- Custom collection name overrides filename
- Per-collection library_ids, poster_url, backdrop_url, category_id
- Also supports YAML frontmatter in `.txt` files

### Artwork Management
- View all Emby collections with poster thumbnails
- Upload custom poster/backdrop images (png/jpg/gif/webp, max 10MB)
- Reset images to default (deletes custom, lets sync regenerate)
- Delete collections from Emby directly

### Sync Features
- Background scheduler with configurable interval
- Manual sync from dashboard
- Single-recipe sync for testing
- Sync history log (last 50 runs)
- Live log viewer

### Notifications
- Discord/webhook notifications on sync start, success, and error
- Configurable from Settings page

### Backup/Restore
- Export all config, state, overrides, and list metadata as one JSON file
- Import to restore everything at once

## Deployment

### Saltbox (recommended for this fork)

Designed for Saltbox with Traefik + Authelia:

1. Clone to `/opt/EmbyCollectionManager/`:
```sh
sudo mkdir -p /opt/EmbyCollectionManager
sudo chown $(whoami):$(whoami) /opt/EmbyCollectionManager
git clone https://github.com/Nebulas0/EmbyCollectionManager.git /opt/EmbyCollectionManager
```

2. Build and start:
```sh
cd /opt/EmbyCollectionManager
docker compose up --build -d
```

3. Add a DNS record for `embycollectionmanager.yourdomain.tld` pointing to your server.

4. Visit `https://embycollectionmanager.yourdomain.tld` — Authelia will prompt for login.

The `compose.yaml` uses:
- `saltbox` external network
- Traefik labels with `authelia@docker` middleware
- `cfdns` certresolver with wildcard `*.yourdomain.tld` cert
- Volumes from `/opt/EmbyCollectionManager/`

### Docker Compose (standalone)

1. Clone and build:
```sh
git clone https://github.com/Nebulas0/EmbyCollectionManager.git
cd EmbyCollectionManager
docker compose up --build -d
```

2. Access at `http://localhost:8282`

### Direct Python

```sh
git clone https://github.com/Nebulas0/EmbyCollectionManager.git
cd EmbyCollectionManager
pip install -r requirements.txt
python main.py
```

Environment variables:
- `WEBUI_PORT` — web UI port (default 8282)
- `SYNC_TARGET` — sync target: `auto` or `emby`
- `SYNC_ONLY=true` — run scheduler without web UI
- `RUN_ONCE=true` — run a single sync and exit

## Configuration

All configuration is managed through the Web UI at `http://localhost:8282/settings`. The config file is at `config/config.yaml`:

```yaml
tmdb:
  api_key: "YOUR_TMDB_API_KEY"

emby:
  api_key: "YOUR_EMBY_API_KEY"
  server_url: "http://emby:8096"
  user_id: "YOUR_EMBY_USER_ID"
  library_ids: []  # Optional: restrict scanning to specific libraries

trakt:
  client_id: "YOUR_TRAKT_CLIENT_ID"
  client_secret: "YOUR_TRAKT_CLIENT_SECRET"
  username: "me"

traktlists:
  enabled: true
  directory: "traktlists"
  max_items_per_collection: 0

mdblist:
  api_key: "YOUR_MDBLIST_API_KEY"

mdblists:
  enabled: true
  directory: "mdblists"
  max_items_per_collection: 0

poster_settings:
  enable_custom_posters: true
  template_name: "default.png"
  text_color: [255, 255, 255]
  bg_color: [0, 0, 0, 128]
  text_position: 0.5

notifications:
  enabled: false
  webhook_url: ""
  notify_on_success: true
  notify_on_error: true
```

### Getting API Keys
- **TMDb**: [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
- **Emby**: Emby Dashboard → Advanced → Security
- **Trakt**: [trakt.tv/oauth/applications](https://trakt.tv/oauth/applications) — Client ID (and Secret for private lists)
- **MDBList**: [mdblist.com/preferences](https://mdblist.com/preferences)

## User-Defined Lists

### Trakt Lists (`traktlists/`)
Create `.txt` or `.yaml` files — one file = one collection (filename = collection name).

`.txt` format (one item per line):
```
# TMDb IDs, movie titles, or Trakt URLs
550
The Matrix
https://trakt.tv/users/username/lists/my-list
```

### MDBLists (`mdblists/`)
Create `.txt` or `.yaml` files — one file = one collection.

```
# TMDb IDs, movie titles, or MDBList URLs
550
The Dark Knight
https://mdblist.com/lists/user/best-movies
```

### YAML format (both Trakt and MDBList)
```yaml
collection_name: Custom Name
library_ids:
  - library_id_here
items:
  - https://trakt.tv/users/username/lists/list-name
  - https://mdblist.com/lists/user/another-list
  - 12345
  - "Movie Title"
```

## Per-Collection Library Selection

From the Web UI:
1. Go to **Trakt Lists** or **MDBLists**
2. Click **Libraries** on any list file
3. Select which Emby libraries that collection should scan

For built-in recipes:
1. Go to **Collections**
2. Click **Edit** on any recipe
3. Select target libraries in the override settings

This enables scenarios like:
- One trending list scanning only the 1080p library
- Another trending list scanning only the 4K library
- A recipe duplicated for different quality libraries

## Recipe Overrides and Duplicates

### Overrides
Click **Edit** on any recipe to override:
- Custom collection name
- Item limit
- Target libraries
- Extra MDBList/Trakt URLs (merged into the collection)
- Extra TMDb IDs
- Custom poster/backdrop URLs

### Duplicates
Click **Duplicate** on any recipe to create a copy with:
- New collection name
- Different target libraries
- Extra items

Stored in `config/recipe_overrides.json`.

## Artwork

- **Franchise collections**: Uses official TMDb collection artwork
- **Dynamic collections**: Custom generated posters with category-based templates
- **Trakt/MDBList collections**: Custom branded poster templates
- **Manual management**: Upload, reset, or delete artwork from the Artwork page

## Project Structure

```
EmbyCollectionManager/
├── main.py                    # Entry point: web UI + sync scheduler
├── compose.yaml               # Saltbox-compatible Docker Compose
├── Dockerfile
├── entrypoint.sh
├── requirements.txt
├── config/
│   ├── config.yaml            # Main configuration
│   ├── webui_state.json       # UI state (enabled recipes, sync interval)
│   ├── list_metadata.json     # Per-list library selection
│   ├── recipe_overrides.json  # Recipe overrides + duplicates
│   └── sync_history.json      # Sync run history
├── src/
│   ├── app_logic.py           # Main sync orchestration
│   ├── emby_client.py         # Emby API client
│   ├── tmdb_client.py         # TMDb API client
│   ├── trakt_client.py        # Trakt API client
│   ├── mdblist_client.py      # MDBList API client
│   ├── collection_recipes.py  # 459 built-in recipes
│   ├── trakt_list_processor.py
│   ├── mdblist_processor.py
│   ├── poster_generator.py    # Custom poster generation
│   ├── collection_poster_manager.py
│   ├── collection_poster_mapper.py
│   ├── config_loader.py
│   ├── logging_setup.py
│   ├── list_metadata.py       # Per-list metadata manager
│   ├── recipe_override.py     # Recipe override/duplicate manager
│   ├── sync_history.py        # Sync history tracker
│   └── notifier.py            # Discord/webhook notifications
├── web/
│   ├── app.py                 # Flask web application
│   └── templates/
│       ├── base.html
│       ├── dashboard.html
│       ├── collections.html
│       ├── libraries.html
│       ├── settings.html
│       ├── artwork.html
│       ├── logs.html
│       ├── traktlists.html
│       └── mdblists.html
├── traktlists/                # User Trakt list files
├── mdblists/                  # User MDBList files
├── resources/                 # Poster templates
└── examples/
    └── custom_lists.yaml
```

## Troubleshooting

1. **Container exits immediately** — check TMDb API key and config file
2. **Collections don't appear in Emby** — verify Emby API key, user ID, and server URL in Settings
3. **No collections synced** — set `SYNC_TARGET=auto`, check logs page
4. **Web UI not accessible** — check port 8282 is exposed, or Traefik/Authelia config for Saltbox
5. **Artwork not updating** — check `poster_settings.enable_custom_posters` in Settings
6. **Preview shows 0 movies** — TMDb API key not configured or invalid

View logs at `http://localhost:8282/logs` or with `docker logs embycollectionmanager`.

## License

MIT License
