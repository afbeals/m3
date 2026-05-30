# Unraid Setup Guide

This guide covers building the Docker image, deploying it on Unraid, and configuring the container.

---

## Prerequisites

- Unraid 6.9 or later
- Docker enabled in Unraid (Settings → Docker → Enable Docker: Yes)
- Your Plex Media Server running (on Unraid or elsewhere on your LAN)
- Your Plex token (see [How to find your Plex token](#finding-your-plex-token))

---

## Step 1 — Build the Docker Image

You have two options: build the image on your local machine and push it, or build directly on the Unraid server.

### Option A — Build on your local machine and push to Docker Hub

Tag both `:latest` and a version tag so you can roll back to a known-good image
if a new build has problems:

```bash
# Read the current version from app/__init__.py
VERSION=$(python3 -c "from app import __version__; print(__version__)")

# From the root of this project
docker build -t yourdockerhubuser/m3:latest -t yourdockerhubuser/m3:${VERSION} .

# Push both tags to Docker Hub (create a free account at hub.docker.com if needed)
docker login
docker push yourdockerhubuser/m3:latest
docker push yourdockerhubuser/m3:${VERSION}
```

Pinning the container to `yourdockerhubuser/m3:1.3.0` (for example) in Unraid
gives you a stable reference you can manually upgrade rather than having `:latest`
change unexpectedly. Unraid's "Check for Updates" button compares the local image
digest to the remote `:latest` tag — it does **not** auto-pull; you must click the
button to download and restart with the new image.

### Option B — Build directly on Unraid

1. Copy the project to your Unraid server (e.g., via SMB share or `scp`):
   ```bash
   scp -r /path/to/m3 root@<unraid-ip>:/mnt/user/appdata/m3-build/
   ```
2. SSH into Unraid:
   ```bash
   ssh root@<unraid-ip>
   ```
3. Build the image:
   ```bash
   cd /mnt/user/appdata/m3-build
   VERSION=$(python3 -c "from app import __version__; print(__version__)")
   docker build -t m3:latest -t m3:${VERSION} .
   ```

> **Note:** Images built directly on Unraid are stored locally and won't survive an Unraid OS upgrade unless you rebuild them. Option A (Docker Hub) is more durable.

---

## Step 2 — Create the Appdata Directory Structure

On your Unraid server (via SSH or the terminal in the Unraid UI), create the directories the container will use:

```bash
mkdir -p /mnt/user/appdata/m3/plugins
mkdir -p /mnt/user/appdata/m3/config/logs
mkdir -p /mnt/user/appdata/m3/config/reports
```

Drop any plugin `.py` files into `/mnt/user/appdata/m3/plugins/` — see the Plugin Setup section below.

---

## Step 3 — Add the Container in Unraid

### Via the Unraid Docker UI (recommended)

1. Go to the Unraid web UI → **Docker** tab
2. Click **Add Container**
3. Fill in the form:

**Basic settings:**

| Field | Value |
|---|---|
| Name | `m3` |
| Repository | `yourdockerhubuser/m3:latest` (or `m3:latest` if built locally) |
| Network Type | `Bridge` |
| Restart Policy | `Unless Stopped` |

**Volume mappings** (click `Add another Path` for each):

| Container Path | Host Path | Access Mode |
|---|---|---|
| `/plugins` | `/mnt/user/appdata/m3/plugins` | Read/Write |
| `/config` | `/mnt/user/appdata/m3/config` | Read/Write |
| `/media` | `/mnt/user/<your-media-share>` | Read/Write |

> Replace `/mnt/user/<your-media-share>` with the actual path to your media on Unraid (e.g., `/mnt/user/Media` or `/mnt/user/data/media`). The container needs read access to scan files and write access to create sidecar files.

**Port mapping** (click `Add another Port`):

| Container Port | Host Port | Protocol | Notes |
|---|---|---|---|
| `8765` | `8765` | TCP | Web dashboard — change the host port if 8765 is already in use |

**Environment variables** (click `Add another Variable` for each):

**Required — the container will refuse to start without these:**

| Key | Example Value | Notes |
|---|---|---|
| `PLEX_URL` | `http://192.168.1.100:32400` | Your Plex server's local URL, reachable from inside the container |
| `PLEX_TOKEN` | `xxxxxxxxxxxxxxxxxxxx` | See [Finding Your Plex Token](#finding-your-plex-token) below |

**Strongly recommended — set these to match your setup:**

| Key | Default | Notes |
|---|---|---|
| `LIBRARY_PATHS` | `/media` | Comma-separated paths **inside the container** matching your `/media` mount (e.g. `/media/Movies,/media/TV`) |
| `TZ` | `UTC` | Timezone for the schedule — times in `RUN_SCHEDULE` are interpreted in this timezone. Examples: `America/New_York`, `Europe/London`, `Australia/Sydney` |

**Optional — the defaults work fine; change only if needed:**

| Key | Default | Notes |
|---|---|---|
| `RUN_SCHEDULE` | `0 3 * * *` | Cron expression for scheduled runs — default is 3am nightly in the container's timezone |
| `PLUGIN_DIR` | `/plugins` | Leave as-is unless you changed the container path |
| `REPORT_PATH` | `/config/reports` | Leave as-is |
| `LOG_PATH` | `/config/logs` | Leave as-is |
| `LOG_LEVEL` | `INFO` | Use `DEBUG` for troubleshooting; switch back to `INFO` when done (verbose logs fill disk faster) |
| `LOG_RETENTION_DAYS` | `30` | Days before old log files are auto-deleted |
| `REPORT_RETENTION_DAYS` | `90` | Days before old run report files are auto-deleted |
| `PLUGIN_RATE_LIMIT_SECS` | `1.0` | Seconds to wait between plugin API calls; set to `0` to disable throttling |
| `PLUGIN_FETCH_TIMEOUT_SECS` | `60.0` | Seconds before a single plugin fetch is aborted and marked as `error` |
| `WEB_ENABLED` | `true` | Set to `false` to disable the dashboard entirely |
| `WEB_PORT` | `8765` | Must match the container port in your port mapping above |
| `APP_NAME` | `m3` | Display name in the dashboard header and page title |
| `LIBRARY_EXCLUDE_PATTERNS` | *(empty)* | Comma-separated glob patterns to skip during scanning (e.g. `*.part,/media/incoming/**`) |
| `NOTIFY_URL` | *(empty)* | Webhook URL to receive a JSON run summary after each run (Apprise, Gotify, etc.) — leave empty to disable |
| `NOTIFY_MIN_ERRORS` | `1` | Defaults to `1` (only notify when errors occurred). Set to `0` to notify after every run including clean ones. |

Add any plugin-specific API keys as additional variables (e.g., `MYSITE_API_KEY`).

> **Tip — debugging:** If files are not being processed as expected, temporarily change `LOG_LEVEL` to `DEBUG` and restart the container. This logs every routing decision, plugin call, and write operation. Switch back to `INFO` once the issue is resolved to reduce log volume.

4. Click **Apply** to create and start the container.

---

### Via docker-compose (alternative)

If you prefer to manage containers with Compose (e.g., using the Unraid **Docker Compose Manager** plugin), create `/mnt/user/appdata/m3/docker-compose.yml`:

```yaml
version: "3.8"

services:
  m3:
    image: yourdockerhubuser/m3:latest
    container_name: m3
    restart: unless-stopped
    volumes:
      - /mnt/user/appdata/m3/plugins:/plugins
      - /mnt/user/appdata/m3/config:/config
      - /mnt/user/Media:/media          # replace with your actual media path
    ports:
      - "8765:8765"
    environment:
      - PLEX_URL=http://192.168.1.x:32400
      - PLEX_TOKEN=your-plex-token
      - LIBRARY_PATHS=/media/Movies,/media/TV
      - PLUGIN_DIR=/plugins
      - REPORT_PATH=/config/reports
      - LOG_PATH=/config/logs
      - RUN_SCHEDULE=0 3 * * *
      - LOG_LEVEL=INFO
      - LOG_RETENTION_DAYS=30
      - REPORT_RETENTION_DAYS=90
      - PLUGIN_RATE_LIMIT_SECS=1.0
      - PLUGIN_FETCH_TIMEOUT_SECS=60.0
      - APP_NAME=m3
      # Optional: set timezone so RUN_SCHEDULE uses local time (default UTC)
      # - TZ=America/New_York
      # Optional: skip files matching these glob patterns (e.g. in-progress downloads)
      # - LIBRARY_EXCLUDE_PATTERNS=*.part,/media/incoming/**
      # Optional: webhook URL for post-run JSON notifications (Apprise, Gotify, etc.)
      # - NOTIFY_URL=
      # Plugin API keys:
      # - MYSITE_API_KEY=your-key-here
```

Then start it:
```bash
cd /mnt/user/appdata/m3
docker compose up -d
```

---

## Step 4 — Plugin Setup

Plugins are `.py` files dropped into the plugins directory (`/mnt/user/appdata/m3/plugins/` on the host). The container discovers them automatically on startup.

1. Copy your plugin file(s) into `/mnt/user/appdata/m3/plugins/`
2. Add any required API keys as environment variables on the container
3. Restart the container to pick up the new plugin:
   - In the Unraid Docker UI: click the container icon → **Restart**
   - Or via SSH: `docker restart m3`

See `plugins/example_plugin.py` in the project root for a reference implementation.

---

## Step 5 — Verify It's Running

### Check the container is up

In the Unraid Docker UI, the `m3` container should show a green icon. Click the icon → **Logs** to see startup output.

Or via SSH:
```bash
docker logs m3 --tail 50
```

You should see something like:
```
INFO  app.main - m3 1.3.0 starting up
INFO  app.main - Library paths: ['/media/Movies', '/media/TV']
INFO  app.plugins.loader - Registered plugin MySitePlugin for id 'mysite'
INFO  app.plugins.loader - Registered plugin OtherSitePlugin for id 'othersite'
INFO  app.main - Web dashboard started on http://0.0.0.0:8765
```

### Open the web dashboard

Navigate to `http://<your-unraid-ip>:8765` in your browser. You should see the m3 dashboard with the latest run summary and a "Run Now" button.

If you changed `WEB_PORT`, use that port number instead.

In Unraid you can also add a WebUI link to the container:
1. Click the container icon → **Edit**
2. In the **WebUI** field enter: `http://[IP]:[PORT:8765]`
3. Click **Apply**

After that, a small "WebUI" button appears next to the container icon in the Docker tab.

### Check log files

```bash
ls /mnt/user/appdata/m3/config/logs/
cat /mnt/user/appdata/m3/config/logs/m3.log
```

### Trigger a manual run (without waiting for the schedule)

Via the web dashboard: click **Run Now** on the main page.

Via SSH:
```bash
docker exec m3 python -m app.main --once
```

Force re-process all files (ignores existing sidecars):
```bash
docker exec m3 python -m app.main --once --force
```

### Check the run report

After a run completes, either:
- Open the dashboard → **Runs** → click any run
- Or read the text summary directly:

```bash
cat /mnt/user/appdata/m3/config/reports/run_latest.txt
```

---

## Finding Your Plex Token

1. Open Plex Web in a browser and sign in
2. Browse to any media item and click the **⋮** (three dots) menu → **Get Info**
3. In the info panel, click **View XML**
4. In the URL that opens, look for `X-Plex-Token=` — the value after it is your token

Alternatively, follow the [official Plex guide](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

---

## Updating the Container

### If using Docker Hub:
1. Pull the new image: `docker pull yourdockerhubuser/m3:latest`
2. In the Unraid Docker UI: click the container icon → **Update** (or Force Update)
3. The container restarts automatically with the new image

### If built locally on Unraid:
```bash
cd /mnt/user/appdata/m3-build
git pull   # or re-copy the updated source
docker build -t m3:latest .
docker restart m3
```

No data migration is needed between versions — m3 stores all state in the config directory (`/mnt/user/appdata/m3/config/`) which is mounted from the host and survives container restarts and updates.

---

## Backup and Restore

### What to back up

| Path | Priority | Contents | Notes |
|---|---|---|---|
| `plugins/` | **Essential** | Your plugin `.py` files | The only thing that cannot be recovered from anywhere else — lose these and you must rewrite your site integrations |
| Your media share (NFO sidecars) | **Essential** | `*.nfo`, `*-poster.jpg`, `*-fanart.jpg` next to each media file | Portable backup of all fetched metadata. If the Plex database is lost, m3 can rebuild everything from these files. Back them up with your media. |
| `docker-compose.yml` / env var notes | **Recommended** | Container configuration | Saves time reconstructing your `PLEX_URL`, `PLEX_TOKEN`, `LIBRARY_PATHS`, plugin API keys, etc. |
| `config/reports/` | Optional | Run history JSON + summary text | Useful for audit history; auto-purged after `REPORT_RETENTION_DAYS`. Safe to omit from backups. |
| `config/logs/` | Skip | Rotating log files | Transient; m3 recreates them on the next run |

**Why the NFO sidecars matter:** m3 pushes metadata directly to Plex's database (fast path), but the Plex database is not portable — it is lost if you rebuild Plex, migrate to a new server, or switch to Jellyfin. The NFO sidecar files on disk are the permanent backup that travels with your media files regardless of which media server you use.

**Plex database:** You do not need to back up the Plex database for m3's purposes. If the Plex database is lost, run:
```bash
docker exec m3 python -m app.main --once --force
```
m3 re-reads the NFO sidecars (via the rename workflow) and re-pushes all metadata to Plex. A full re-fetch from source sites is only needed if the sidecar files are also lost.

### Restore after data loss

1. Restore your plugin files to `/mnt/user/appdata/m3/plugins/`
2. Start the container — m3 recreates the config directory structure automatically
3. If NFO sidecars are intact, run a normal scheduled pass — m3 will skip files that already have sidecars. Plex will pick up the sidecars on its next scan.
4. If NFO sidecars are lost too, run `docker exec m3 python -m app.main --once --force` to re-fetch all metadata from source sites.

---

## Notifications (Gotify on Unraid)

The easiest way to get run notifications on Unraid is **Gotify** — a lightweight self-hosted push notification server available directly from Unraid Community Apps.

### Step 1 — Install Gotify

1. In the Unraid web UI go to **Apps** (Community Applications plugin required)
2. Search for **Gotify** and install it — the default settings work fine
3. Open the Gotify UI (default port `8080`) and sign in with admin / admin
4. Change the admin password immediately under **Users → Edit**

### Step 2 — Create an app token

1. In Gotify, click **Apps → Create Application**
2. Name it `m3` and click **Create**
3. Copy the displayed token — you'll paste it into the m3 container config

### Step 3 — Configure m3

Add these two env vars to the m3 container (Docker UI → Edit → Add variable):

| Key | Value | Notes |
|---|---|---|
| `NOTIFY_URL` | `http://<unraid-ip>:<gotify-port>/message?token=<your-app-token>` | Replace with your Gotify host/port and token |
| `NOTIFY_MIN_ERRORS` | `1` | This is the default — only notifies when errors occurred. Set `NOTIFY_MIN_ERRORS=0` to receive notifications after every run. |

> **Example:** `NOTIFY_URL=http://192.168.1.100:8080/message?token=AbCdEfGhIjKl`

After the next run (or click **Run Now** to test), you should receive a push notification on any device with the Gotify app installed.

### Notification payload

m3 POSTs a JSON body that Gotify receives as a message. The key fields:

```json
{
  "app_name": "m3",
  "started_at": "2025-05-17T03:00:01",
  "finished_at": "2025-05-17T03:02:34",
  "duration_seconds": 153,
  "updated": 12,
  "errors": 0,
  "scrape_errors": 1,
  "unmatched": 2,
  "total_scanned": 850,
  "first_error": null,
  "first_scrape_error": "title element not found at h1.scene-title"
}
```

`first_error` and `first_scrape_error` are included so alert systems that show a preview (e.g. Gotify, Apprise) can display the error message without you having to open the dashboard.

### Unraid native notifications (optional)

If you prefer alerts in the Unraid web UI itself rather than a push app, you can call the Unraid built-in notification script from a small wrapper script. Add a plugin that calls:

```bash
/usr/local/emhttp/webGui/scripts/notify \
  -e "m3" \
  -s "m3 run complete" \
  -d "Updated: 12  Errors: 0  Unmatched: 2" \
  -i "normal"
```

Severity is `normal`, `warning`, or `alert`. This requires running a custom script inside the container or from a User Script that polls the Gotify API — the webhook approach above is simpler for most setups.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Container exits immediately | `docker logs m3` — likely a missing required env var or bad `PLEX_TOKEN` |
| No files being processed | Verify `LIBRARY_PATHS` values match the container-side mount paths, not the host paths |
| Plex not updating | Confirm `PLEX_URL` is reachable from inside the container: `docker exec m3 curl -s "$PLEX_URL/identity"` |
| Plugin not loading | Check plugin file is in `/mnt/user/appdata/m3/plugins/`; check logs for import errors |
| Sidecar files not appearing | Confirm `/media` mount has write permissions; check `docker logs m3` for permission errors |
| Schedule not running | Verify `RUN_SCHEDULE` is a valid cron expression (5 fields: min hour day month weekday) |
