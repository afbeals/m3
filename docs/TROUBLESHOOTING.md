# Troubleshooting

Common issues and how to fix them.

---

## Quick diagnostic: check the logs first

```bash
# Container logs (last 50 lines)
docker logs m3 --tail 50

# Full rotating log file
docker exec m3 tail -100 /config/logs/m3.log

# Or on Unraid host
cat /mnt/user/appdata/m3/config/logs/m3.log | tail -100
```

Set `LOG_LEVEL=DEBUG` (restart required) for maximum detail.

---

## Container won't start

| Symptom | Cause | Fix |
|---|---|---|
| Container exits immediately (exit code 1) | Missing required env var | `docker logs m3` shows a validation error. Add `PLEX_URL` and `PLEX_TOKEN` as environment variables. |
| `SystemExit: library path does not exist` | `LIBRARY_PATHS` points to a non-existent container path | Verify the path matches the **container side** of your volume mount (e.g. `/media`, not `/mnt/user/media`) |
| `SystemExit: report path is not writable` | `/config/reports` directory can't be created or written | Confirm the `/config` volume mount has read-write access; check Unraid share permissions |
| Port already in use | Another process is using `WEB_PORT` (default 8765) | Set `WEB_PORT=8766` (or any free port) and update your port mapping |

---

## No files are being processed

| Symptom | Cause | Fix |
|---|---|---|
| Run report shows 0 files | `LIBRARY_PATHS` is wrong | Path must be the container-side mount path. E.g. if your Unraid media is at `/mnt/user/Media` and mounted to `/media` in the container, use `LIBRARY_PATHS=/media/Movies`. |
| Files are "skipped" | NFO sidecars already exist | Re-run with `--force` to ignore existing sidecars: `docker exec m3 python -m app.main --once --force` |
| Files are "unmatched" | Filename doesn't parse, or no plugin registered for the site | See [Unmatched files](#unmatched-files) below |
| Log shows "Library path X is a subdirectory of another configured path and will be skipped" | `LIBRARY_PATHS` contains both a parent path and one of its subdirectories (e.g. `/media` and `/media/Movies`) | Remove the subdirectory — the parent already covers it. Keep only `/media` and m3 will scan all subdirectories automatically. |

---

## Plex not updating

| Symptom | Cause | Fix |
|---|---|---|
| Log shows "Failed to connect to Plex" | Wrong `PLEX_URL` or token | Test connectivity: `docker exec m3 curl -s "$PLEX_URL/identity"` — should return XML. Verify the URL is reachable from inside the container (not just from your host). |
| Log shows "Plex item not found" | Media file not yet scanned into Plex | Trigger a Plex library scan, then re-run m3. The file must appear in Plex before m3 can update it. |
| Metadata updates but Plex reverts it | Plex agent overwrote the unlocked fields | This shouldn't happen with m3's field locks. If it does, check that `item.edit()` is succeeding (no error in logs) and that Plex is not running its agent immediately after. |
| Plex slow fallback scan warning | Fast filepath filter not supported by your Plex version | Informational only — m3 found the item but had to scan the whole library. Consider upgrading Plex. The scan has a 60-second timeout guard. |

---

## Unmatched files

A file is "unmatched" when m3 can't parse the filename **or** no plugin is registered for the site token.

**Step 1 — Check the unmatched digest**

Open the web dashboard at `http://<host>:8765/unmatched` to see all unmatched files grouped by path.

**Step 2 — Test the filename parser**

```bash
python3 - <<'EOF'
from app.parser import parse

stem = "Jane Doe with Drama % mysite - 12345"  # replace with your actual filename stem
result = parse(stem)
if result:
    print(f"site={result.site!r}  subtype={result.match_subtype}  scene_id={result.scene_id!r}")
else:
    print("Could not parse — filename doesn't match any known pattern")
EOF
```

If `parse()` returns `None`, the filename grammar doesn't match. See
[docs/FILENAME_PATTERNS.md](FILENAME_PATTERNS.md) for the full grammar.

**Step 3 — Check loaded plugins**

```bash
# Dashboard: http://<host>:8765/plugins
# Or from the container:
docker exec m3 python3 -c "
from app.config import load_config
from app.plugins.loader import load_plugins
cfg = load_config()
plugins = load_plugins(cfg.plugin_dir)
for k, v in sorted(plugins.items()):
    print(f'{k!r:20} → {type(v).__name__}')
"
```

If your site token doesn't appear in the list, the plugin file either:
- Isn't in the plugin directory
- Has a Python syntax error (check container logs)
- Has an empty `site_id`

---

## Plugin not loading

```bash
docker logs m3 | grep -i "plugin"
```

Look for:
- `Failed to import plugin file` — syntax error or import error in your plugin; the full traceback follows
- `Plugin class ... has no site_id; skipping` — add `site_id = "yoursite"` to the class
- `Plugin id ... already registered ... overwriting` — two plugins claim the same site_id; last one loaded wins

---

## Add-form files (status=add_form)

Files with `status=add_form` used the Manual Add filename grammar (starting with "Add ...").
These files are **not** auto-processed by plugins — they need human follow-up.

**What to do:**
- If the file already exists in Plex: no action needed — the Add form is for manual metadata entry
- If you want m3 to auto-process it: rename the file using the standard grammar: `Actor % site - 12345.mp4`
- To add the metadata manually in Plex: use Plex's "Fix Incorrect Match" feature

---

## Image errors (status=image_error)

When images fail to download, the NFO is **not written** (the file shows as image_error, not updated).

**Common causes:**
- The poster/fanart URL returned a 403 (CDN rate limiting or authentication required)
- The image URL has expired (some sites rotate URLs periodically)
- The image URL points to a deleted or moved file (404)
- Network connectivity issues to the image CDN

**How to diagnose:** Check the full error in the run detail page or run_latest.txt.
**How to fix:** Correct the URL in your plugin's `_to_result()`, then use `--retry-failed` to reprocess.

---

## Scrape errors

`status=scrape_error` in the run report means the plugin raised a `ScrapeError`
or `SelectorMissingError` — distinct from a generic plugin crash (`status=error`).

| Error type | Meaning | Fix |
|---|---|---|
| `ScrapeError: HTTP 403` | Site is blocking the bot | The site may require login, have rate limiting, or block the User-Agent. Consider adding a delay (`PLUGIN_RATE_LIMIT_SECS`) or check if the site requires an API key. |
| `ScrapeError: HTTP 404` | Scene/page not found | The scene was deleted or the ID is wrong. Check the site manually. |
| `ScrapeError: connection error` | Network issue | Transient; m3 retries 3 times with backoff. If persistent, check container's network access. |
| `SelectorMissingError: title not found at h1.scene-title` | Site changed its HTML markup | Update the CSS selector in your plugin's `_parse_detail_page()` method. |
| `ScrapeError: response too large (>10MB)` | Site returned a huge page | Unlikely for scene pages; may indicate a CDN redirect or error page. |

Once the underlying issue is fixed (plugin updated, API key corrected, network restored), re-process just the failed files without a full library rescan:

```bash
docker exec m3 python -m app.main --retry-failed
```

`--retry-failed` reads the most recent run report and re-runs only files with `status=error`, `status=scrape_error`, `status=image_error`, or `status=plugin_error`. This is faster and safer than `--force`, which reprocesses everything.

---

## Deleting files

When you delete a media file (and its sidecar files), m3 itself does nothing automatically — the cleanup is handled by Plex.

### What Plex does after you delete a file

Plex does **not** remove deleted items immediately. The sequence is:

1. **Next library scan** — Plex detects the file is missing and marks the item as unavailable. This happens on the next scheduled scan or when you manually trigger one (Plex → Library → "Scan Library Files").
2. **Empty Trash** — The database entry is not fully removed until you run "Empty Trash" (Plex → Library → "Empty Trash", or Settings → Troubleshooting → "Empty Trash"). Until then the item remains visible in the library but may show as unplayable.
3. **Automatic cleanup** — If you have "Automatically fix incorrectly matched items" and scheduled tasks enabled, Plex will eventually clean up on its own, but the timing is unpredictable (can be hours or days).

**Recommended workflow when deleting files:**

1. Delete the video file and its sidecars (`.nfo`, `-poster.jpg`, `-fanart.jpg`)
2. In Plex: trigger a library scan → then "Empty Trash"
3. m3 has no record of the file after the sidecars are gone — no action needed in m3

### What m3 leaves behind

After you delete a file, the JSON run reports in `/config/reports/` will still contain historical entries for that file (visible in the file history page `/files?path=…`). These are cleaned up automatically when they age past `REPORT_RETENTION_DAYS` (default 90 days). If you want to remove them sooner, delete the relevant JSON report files in `/config/reports/`.

---

## Renamed files not detected

m3 detects a rename when there is **exactly one orphan `.nfo`** (an NFO with no matching video) and **exactly one new video** (a video with no matching NFO) in the same directory. Both conditions must be true at the same time.

| Symptom | Cause | Fix |
|---|---|---|
| Renamed file processed as a new file (fresh API fetch instead of just rename) | Multiple new videos or multiple orphan NFOs in the same directory simultaneously | m3 treats ambiguous cases as new files to avoid misattribution. Rename files one at a time between runs, or use `--retry-failed` to re-process any resulting `error` files after the situation resolves. |
| Old NFO/images not renamed | Rename was detected but `rename_nfo_assets()` failed | Check `docker logs m3` for a file-permission error. Confirm the `/media` mount has write access. |
| Plex not updated after rename | Plex push failed during rename workflow | Check logs for "Failed to push renamed NFO to Plex". The sidecar is renamed correctly on disk; re-run with `--retry-failed` to retry the Plex push. |
| Rename not detected at all | Running with `--force` | `--force` bypasses rename detection and treats all files as new. Use `--force` only when you want a full re-fetch of all metadata. |

---

## Disk full / storage issues

| Symptom | Cause | Fix |
|---|---|---|
| `Could not write JSON report` or `Could not write text report` in logs | `REPORT_PATH` volume is full | Free space on the Unraid share or lower `REPORT_RETENTION_DAYS` to delete old reports sooner: `docker exec m3 python3 -c "import app.logging_setup as l; l.cleanup_old_files('/config/reports', 7, '.json')"` |
| `Could not write log file` / log rotation warning | `LOG_PATH` volume is full | Free space or lower `LOG_RETENTION_DAYS`. Set `LOG_LEVEL=WARNING` temporarily to reduce log volume. |
| Container exits at startup with `LOG_PATH ... is not writable` | Log directory is missing or the volume mount lacks write permission | Verify `/mnt/user/appdata/m3/config/logs` exists and is mounted read-write. Check Unraid share permissions. |
| `REPORT_PATH ... is not writable` at startup | Same as above but for reports directory | Same fix — confirm `/mnt/user/appdata/m3/config/reports` is mounted read-write. |

---

## NFO files not appearing

| Symptom | Cause | Fix |
|---|---|---|
| No `.nfo` next to media | Write failed | Check `docker logs m3` for a permission error or disk-full error |
| `.nfo` appears but Plex doesn't read it | Plex doesn't use NFO sidecars by default | NFO files are for Kodi/Jellyfin; m3 pushes metadata to Plex via its API directly |
| Old `.nfo` not updating | `--force` not set | Re-run with `--force` |

---

## Web dashboard issues

| Symptom | Cause | Fix |
|---|---|---|
| Can't reach `http://<host>:8765` | Port not mapped | Add a port mapping `8765:8765` in Docker/Unraid |
| Dashboard shows "No runs yet" | No runs have completed | Trigger one: click **Run Now** or run `docker exec m3 python -m app.main --once` |
| Status badge stuck on "Running" | Run crashed without updating RunState | Restart the container — the badge resets on startup |
| "Run Now" button doesn't seem to do anything | Run already in progress | Watch the status badge in the header; it changes to "▶ Running" within 2 seconds |
| HTMX status badge stops updating / "Run Now" button unresponsive | unpkg.com CDN is unreachable | The dashboard loads HTMX from `unpkg.com` but falls back automatically to `/static/htmx.min.js` if that CDN is unreachable. Fix: download `htmx.min.js` from the [HTMX releases page](https://github.com/bigskysoftware/htmx/releases) and place it at `app/web/static/htmx.min.js` — the fallback script in `base.html` will pick it up with no config change needed. Scheduled runs are unaffected by CDN issues. |
| Dashboard returns HTTP 500 / web page errors but scheduler keeps running | Unhandled exception in a route handler | Check container logs (`docker logs m3 --tail 50`) for the Python traceback. The scheduler runs on the main thread; the web server is a daemon thread, so a crash in a route doesn't stop runs. Fix: restart the container to recover the web UI. |

---

## Runs are very slow on a large library

If a run with hundreds of files takes hours, the most likely cause is the plugin rate limit.

By default `PLUGIN_RATE_LIMIT_SECS=1.0`, meaning m3 waits 1 second between each plugin call. For a 3,600-file library that's 1 hour of sleeping. Options:

| Action | Effect |
|---|---|
| Set `PLUGIN_RATE_LIMIT_SECS=0.25` | 4× faster; still polite to most APIs |
| Set `PLUGIN_RATE_LIMIT_SECS=0` | No delay; only safe for sites that explicitly allow it |
| Run nightly on a subset of paths | Split `LIBRARY_PATHS` into smaller sets and stagger them |

Note: only files **without** an existing `.nfo` sidecar are processed. Once all files are tagged, subsequent runs process only newly added files — the rate limit impact drops to near zero.

---

## Schedule not running

```bash
# Verify your cron expression parses correctly
python3 -c "
import os, datetime
from apscheduler.triggers.cron import CronTrigger
tz = os.environ.get('TZ') or 'UTC'
schedule = os.environ.get('RUN_SCHEDULE', '0 3 * * *')
t = CronTrigger.from_crontab(schedule, timezone=tz)
print('timezone:', tz)
print('schedule:', schedule)
print('next run:', t.get_next_fire_time(None, datetime.datetime.now()))
"
```

Common mistakes:
- `RUN_SCHEDULE` must be a **5-field** cron expression (`min hour day month weekday`), not 6-field
- Times are in the **container's timezone** (UTC by default). To use local time, set `TZ` as an environment variable (e.g. `TZ=America/New_York`, `TZ=Europe/London`, `TZ=Australia/Sydney`). Add it alongside your other Docker env vars and restart the container.

---

## Webhook not firing

| Symptom | Cause | Fix |
|---|---|---|
| `NOTIFY_URL` is set but no notification arrives after a clean run | `NOTIFY_MIN_ERRORS` is `1` and the run had no errors | Expected — the webhook only fires when `errors + scrape_errors + image_errors + plugin_errors >= NOTIFY_MIN_ERRORS`. This is the default — set `NOTIFY_MIN_ERRORS=0` to receive notifications after every run. |
| Webhook fires but Gotify shows no message | Wrong app token or URL format | Verify with: `curl -s -X POST "http://<gotify-host>/message?token=<token>" -H 'Content-Type: application/json' -d '{"message":"test"}'` — should return `{"id":...}`. |
| Log shows `Webhook notification failed for ...` | Network error or endpoint unreachable | Webhook failure is non-fatal; the run still completes. Test reachability: `docker exec m3 curl -s -o /dev/null -w "%{http_code}" -X POST <NOTIFY_URL>`. |
| Webhook fires but receiving service shows 400 | Payload format mismatch | m3 sends raw JSON. Some services (e.g. Gotify) expect a specific schema (`"message"` key). Use an Apprise relay or a thin wrapper script to translate m3's payload. |

---

## General debugging workflow

1. `docker logs m3 --tail 50` — look for startup errors
2. `cat /config/logs/m3.log` — detailed run trace
3. Dashboard → Config (`/config`) — verify env vars the container actually sees
4. Dashboard → Plugins (`/plugins`) — verify your plugins loaded
5. `docker exec m3 python -m app.main --once --force` + `cat /config/reports/run_latest.txt` — force a run and check the report
6. Set `LOG_LEVEL=DEBUG` and restart — maximum verbosity for any remaining mystery
