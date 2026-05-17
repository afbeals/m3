# Troubleshooting

Common issues and how to fix them.

---

## Quick diagnostic: check the logs first

```bash
# Container logs (last 50 lines)
docker logs pm --tail 50

# Full rotating log file
docker exec pm tail -100 /config/logs/pm.log

# Or on Unraid host
cat /mnt/user/appdata/pm/config/logs/pm.log | tail -100
```

Set `LOG_LEVEL=DEBUG` (restart required) for maximum detail.

---

## Container won't start

| Symptom | Cause | Fix |
|---|---|---|
| Container exits immediately (exit code 1) | Missing required env var | `docker logs pm` shows a validation error. Add `PLEX_URL` and `PLEX_TOKEN` as environment variables. |
| `SystemExit: library path does not exist` | `LIBRARY_PATHS` points to a non-existent container path | Verify the path matches the **container side** of your volume mount (e.g. `/media`, not `/mnt/user/media`) |
| `SystemExit: report path is not writable` | `/config/reports` directory can't be created or written | Confirm the `/config` volume mount has read-write access; check Unraid share permissions |
| Port already in use | Another process is using `WEB_PORT` (default 8765) | Set `WEB_PORT=8766` (or any free port) and update your port mapping |

---

## No files are being processed

| Symptom | Cause | Fix |
|---|---|---|
| Run report shows 0 files | `LIBRARY_PATHS` is wrong | Path must be the container-side mount path. E.g. if your Unraid media is at `/mnt/user/Media` and mounted to `/media` in the container, use `LIBRARY_PATHS=/media/Movies`. |
| Files are "skipped" | NFO sidecars already exist | Re-run with `--force` to ignore existing sidecars: `docker exec pm python -m app.main --once --force` |
| Files are "unmatched" | Filename doesn't parse, or no plugin registered for the site | See [Unmatched files](#unmatched-files) below |

---

## Plex not updating

| Symptom | Cause | Fix |
|---|---|---|
| Log shows "Failed to connect to Plex" | Wrong `PLEX_URL` or token | Test connectivity: `docker exec pm curl -s "$PLEX_URL/identity"` — should return XML. Verify the URL is reachable from inside the container (not just from your host). |
| Log shows "Plex item not found" | Media file not yet scanned into Plex | Trigger a Plex library scan, then re-run pm. The file must appear in Plex before pm can update it. |
| Metadata updates but Plex reverts it | Plex agent overwrote the unlocked fields | This shouldn't happen with pm's field locks. If it does, check that `item.edit()` is succeeding (no error in logs) and that Plex is not running its agent immediately after. |
| Plex slow fallback scan warning | Fast filepath filter not supported by your Plex version | Informational only — pm found the item but had to scan the whole library. Consider upgrading Plex. The scan has a 60-second timeout guard. |

---

## Unmatched files

A file is "unmatched" when pm can't parse the filename **or** no plugin is registered for the site token.

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
docker exec pm python3 -c "
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
docker logs pm | grep -i "plugin"
```

Look for:
- `Failed to import plugin file` — syntax error or import error in your plugin; the full traceback follows
- `Plugin class ... has no site_id; skipping` — add `site_id = "yoursite"` to the class
- `Plugin id ... already registered ... overwriting` — two plugins claim the same site_id; last one loaded wins

---

## Scrape errors

`status=scrape_error` in the run report means the plugin raised a `ScrapeError`
or `SelectorMissingError` — distinct from a generic plugin crash (`status=error`).

| Error type | Meaning | Fix |
|---|---|---|
| `ScrapeError: HTTP 403` | Site is blocking the bot | The site may require login, have rate limiting, or block the User-Agent. Consider adding a delay (`PLUGIN_RATE_LIMIT_SECS`) or check if the site requires an API key. |
| `ScrapeError: HTTP 404` | Scene/page not found | The scene was deleted or the ID is wrong. Check the site manually. |
| `ScrapeError: connection error` | Network issue | Transient; pm retries 3 times with backoff. If persistent, check container's network access. |
| `SelectorMissingError: title not found at h1.scene-title` | Site changed its HTML markup | Update the CSS selector in your plugin's `_parse_detail_page()` method. |
| `ScrapeError: response too large (>10MB)` | Site returned a huge page | Unlikely for scene pages; may indicate a CDN redirect or error page. |

---

## NFO files not appearing

| Symptom | Cause | Fix |
|---|---|---|
| No `.nfo` next to media | Write failed | Check `docker logs pm` for a permission error or disk-full error |
| `.nfo` appears but Plex doesn't read it | Plex doesn't use NFO sidecars by default | NFO files are for Kodi/Jellyfin; pm pushes metadata to Plex via its API directly |
| Old `.nfo` not updating | `--force` not set | Re-run with `--force` |

---

## Web dashboard issues

| Symptom | Cause | Fix |
|---|---|---|
| Can't reach `http://<host>:8765` | Port not mapped | Add a port mapping `8765:8765` in Docker/Unraid |
| Dashboard shows "No runs yet" | No runs have completed | Trigger one: click **Run Now** or run `docker exec pm python -m app.main --once` |
| Status badge stuck on "Running" | Run crashed without updating RunState | Restart the container — the badge resets on startup |
| "Run Now" button doesn't seem to do anything | Run already in progress | Watch the status badge in the header; it changes to "▶ Running" within 2 seconds |
| Dashboard returns HTTP 500 / web page errors but scheduler keeps running | Unhandled exception in a route handler | Check container logs (`docker logs pm --tail 50`) for the Python traceback. The scheduler runs on the main thread; the web server is a daemon thread, so a crash in a route doesn't stop runs. Fix: restart the container to recover the web UI. |

---

## Runs are very slow on a large library

If a run with hundreds of files takes hours, the most likely cause is the plugin rate limit.

By default `PLUGIN_RATE_LIMIT_SECS=1.0`, meaning pm waits 1 second between each plugin call. For a 3,600-file library that's 1 hour of sleeping. Options:

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
from apscheduler.triggers.cron import CronTrigger
t = CronTrigger.from_crontab('0 3 * * *')  # replace with your RUN_SCHEDULE
print('next run:', t.get_next_fire_time(None, __import__('datetime').datetime.now()))
"
```

Common mistakes:
- `RUN_SCHEDULE` must be a **5-field** cron expression (`min hour day month weekday`), not 6-field
- Times are in the **container's timezone** (UTC by default). If you want 3am local time, either set a TZ env var or adjust the hour offset.

---

## General debugging workflow

1. `docker logs pm --tail 50` — look for startup errors
2. `cat /config/logs/pm.log` — detailed run trace
3. Dashboard → Config (`/config`) — verify env vars the container actually sees
4. Dashboard → Plugins (`/plugins`) — verify your plugins loaded
5. `docker exec pm python -m app.main --once --force` + `cat /config/reports/run_latest.txt` — force a run and check the report
6. Set `LOG_LEVEL=DEBUG` and restart — maximum verbosity for any remaining mystery
