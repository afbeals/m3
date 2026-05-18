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

```bash
# From the root of this project
docker build -t yourdockerhubuser/pm:latest .

# Push to Docker Hub (create a free account at hub.docker.com if needed)
docker login
docker push yourdockerhubuser/pm:latest
```

### Option B — Build directly on Unraid

1. Copy the project to your Unraid server (e.g., via SMB share or `scp`):
   ```bash
   scp -r /path/to/pm root@<unraid-ip>:/mnt/user/appdata/pm-build/
   ```
2. SSH into Unraid:
   ```bash
   ssh root@<unraid-ip>
   ```
3. Build the image:
   ```bash
   cd /mnt/user/appdata/pm-build
   docker build -t pm:latest .
   ```

> **Note:** Images built directly on Unraid are stored locally and won't survive an Unraid OS upgrade unless you rebuild them. Option A (Docker Hub) is more durable.

---

## Step 2 — Create the Appdata Directory Structure

On your Unraid server (via SSH or the terminal in the Unraid UI), create the directories the container will use:

```bash
mkdir -p /mnt/user/appdata/pm/plugins
mkdir -p /mnt/user/appdata/pm/config/logs
mkdir -p /mnt/user/appdata/pm/config/reports
```

Drop any plugin `.py` files into `/mnt/user/appdata/pm/plugins/` — see the Plugin Setup section below.

---

## Step 3 — Add the Container in Unraid

### Via the Unraid Docker UI (recommended)

1. Go to the Unraid web UI → **Docker** tab
2. Click **Add Container**
3. Fill in the form:

**Basic settings:**

| Field | Value |
|---|---|
| Name | `pm` |
| Repository | `yourdockerhubuser/pm:latest` (or `pm:latest` if built locally) |
| Network Type | `Bridge` |
| Restart Policy | `Unless Stopped` |

**Volume mappings** (click `Add another Path` for each):

| Container Path | Host Path | Access Mode |
|---|---|---|
| `/plugins` | `/mnt/user/appdata/pm/plugins` | Read/Write |
| `/config` | `/mnt/user/appdata/pm/config` | Read/Write |
| `/media` | `/mnt/user/<your-media-share>` | Read/Write |

> Replace `/mnt/user/<your-media-share>` with the actual path to your media on Unraid (e.g., `/mnt/user/Media` or `/mnt/user/data/media`). The container needs read access to scan files and write access to create sidecar files.

**Port mapping** (click `Add another Port`):

| Container Port | Host Port | Protocol | Notes |
|---|---|---|---|
| `8765` | `8765` | TCP | Web dashboard — change the host port if 8765 is already in use |

**Environment variables** (click `Add another Variable` for each):

| Key | Value | Notes |
|---|---|---|
| `PLEX_URL` | `http://<your-unraid-ip>:32400` | Your Plex server's local URL |
| `PLEX_TOKEN` | `your-plex-token` | See below for how to find this |
| `LIBRARY_PATHS` | `/media/Movies,/media/TV` | Comma-separated paths **inside the container** matching your `/media` mount |
| `PLUGIN_DIR` | `/plugins` | Leave as-is unless you changed the container path |
| `REPORT_PATH` | `/config/reports` | Leave as-is |
| `LOG_PATH` | `/config/logs` | Leave as-is |
| `RUN_SCHEDULE` | `0 3 * * *` | Cron expression — default is 3am nightly |
| `LOG_LEVEL` | `INFO` | Use `DEBUG` for troubleshooting |
| `LOG_RETENTION_DAYS` | `30` | Days before old log files are deleted |
| `REPORT_RETENTION_DAYS` | `90` | Days before old report files are deleted |
| `PLUGIN_RATE_LIMIT_SECS` | `1.0` | Seconds between plugin API calls; set to `0` to disable |
| `WEB_ENABLED` | `true` | Set to `false` to disable the dashboard |
| `WEB_PORT` | `8765` | Must match the container port in your port mapping above |
| `APP_NAME` | `pm` | Display name in the dashboard header and page title |
| `NOTIFY_URL` | *(empty)* | Webhook URL to receive a JSON run summary after each run (Apprise, Gotify, etc.) |

Add any plugin-specific API keys as additional variables (e.g., `MYSITE_API_KEY`).

> **Tip — debugging:** If files are not being processed as expected, temporarily change `LOG_LEVEL` to `DEBUG` and restart the container. This logs every routing decision, plugin call, and write operation. Switch back to `INFO` once the issue is resolved to reduce log volume.

4. Click **Apply** to create and start the container.

---

### Via docker-compose (alternative)

If you prefer to manage containers with Compose (e.g., using the Unraid **Docker Compose Manager** plugin), create `/mnt/user/appdata/pm/docker-compose.yml`:

```yaml
version: "3.8"

services:
  pm:
    image: yourdockerhubuser/pm:latest
    container_name: pm
    restart: unless-stopped
    volumes:
      - /mnt/user/appdata/pm/plugins:/plugins
      - /mnt/user/appdata/pm/config:/config
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
      - APP_NAME=pm
      # Optional: webhook URL for post-run JSON notifications (Apprise, Gotify, etc.)
      # - NOTIFY_URL=
      # Plugin API keys:
      # - MYSITE_API_KEY=your-key-here
```

Then start it:
```bash
cd /mnt/user/appdata/pm
docker compose up -d
```

---

## Step 4 — Plugin Setup

Plugins are `.py` files dropped into the plugins directory (`/mnt/user/appdata/pm/plugins/` on the host). The container discovers them automatically on startup.

1. Copy your plugin file(s) into `/mnt/user/appdata/pm/plugins/`
2. Add any required API keys as environment variables on the container
3. Restart the container to pick up the new plugin:
   - In the Unraid Docker UI: click the container icon → **Restart**
   - Or via SSH: `docker restart pm`

See `plugins/example_plugin.py` in the project root for a reference implementation.

---

## Step 5 — Verify It's Running

### Check the container is up

In the Unraid Docker UI, the `pm` container should show a green icon. Click the icon → **Logs** to see startup output.

Or via SSH:
```bash
docker logs pm --tail 50
```

You should see something like:
```
INFO  app.main - pm 1.0.0 starting up
INFO  app.main - Library paths: ['/media/Movies', '/media/TV']
INFO  app.plugins.loader - Registered plugin MySitePlugin for id 'mysite'
INFO  app.plugins.loader - Registered plugin OtherSitePlugin for id 'othersite'
INFO  app.main - Web dashboard started on http://0.0.0.0:8765
```

### Open the web dashboard

Navigate to `http://<your-unraid-ip>:8765` in your browser. You should see the pm dashboard with the latest run summary and a "Run Now" button.

If you changed `WEB_PORT`, use that port number instead.

In Unraid you can also add a WebUI link to the container:
1. Click the container icon → **Edit**
2. In the **WebUI** field enter: `http://[IP]:[PORT:8765]`
3. Click **Apply**

After that, a small "WebUI" button appears next to the container icon in the Docker tab.

### Check log files

```bash
ls /mnt/user/appdata/pm/config/logs/
cat /mnt/user/appdata/pm/config/logs/pm.log
```

### Trigger a manual run (without waiting for the schedule)

Via the web dashboard: click **Run Now** on the main page.

Via SSH:
```bash
docker exec pm python -m app.main --once
```

Force re-process all files (ignores existing sidecars):
```bash
docker exec pm python -m app.main --once --force
```

### Check the run report

After a run completes, either:
- Open the dashboard → **Runs** → click any run
- Or read the text summary directly:

```bash
cat /mnt/user/appdata/pm/config/reports/run_latest.txt
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
1. Pull the new image: `docker pull yourdockerhubuser/pm:latest`
2. In the Unraid Docker UI: click the container icon → **Update** (or Force Update)
3. The container restarts automatically with the new image

### If built locally on Unraid:
```bash
cd /mnt/user/appdata/pm-build
git pull   # or re-copy the updated source
docker build -t pm:latest .
docker restart pm
```

No data migration is needed between versions — pm stores all state in the config directory (`/mnt/user/appdata/pm/config/`) which is mounted from the host and survives container restarts and updates.

---

## Backup and Restore

### What to back up

The only persistent state pm writes is in `/mnt/user/appdata/pm/`:

| Path | Contents | Notes |
|---|---|---|
| `config/reports/` | Run history JSON + summary text | Safe to delete old ones; they only affect dashboard history |
| `config/logs/` | Rotating log files | Safe to delete; pm recreates them on next run |
| `plugins/` | Your plugin `.py` files | **Back these up** — they are not recoverable from the container |

NFO sidecars and poster images live **next to your media files** in your media share — they are already backed up with your media.

Plex does not need to be backed up separately; if you lose the Plex database you can always re-run pm with `--force` to rebuild all metadata.

### Restore after data loss

1. Restore your plugin files to `/mnt/user/appdata/pm/plugins/`
2. Start the container — pm recreates the config directory structure automatically
3. Run `docker exec pm python -m app.main --once --force` to re-process all media files

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Container exits immediately | `docker logs pm` — likely a missing required env var or bad `PLEX_TOKEN` |
| No files being processed | Verify `LIBRARY_PATHS` values match the container-side mount paths, not the host paths |
| Plex not updating | Confirm `PLEX_URL` is reachable from inside the container: `docker exec pm curl -s "$PLEX_URL/identity"` |
| Plugin not loading | Check plugin file is in `/mnt/user/appdata/pm/plugins/`; check logs for import errors |
| Sidecar files not appearing | Confirm `/media` mount has write permissions; check `docker logs pm` for permission errors |
| Schedule not running | Verify `RUN_SCHEDULE` is a valid cron expression (5 fields: min hour day month weekday) |
