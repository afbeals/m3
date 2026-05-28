# -----------------------------------------------------------------------------
# app/plugins/loader.py
#
# Discovers and loads MetadataPlugin subclasses from .py files in the
# configured plugin directory (mounted at /plugins in the container).
#
# How it works:
#   1. List all .py files in the plugin directory (sorted, skipping __*.py)
#   2. Use importlib to load each file as a Python module at runtime
#   3. Inspect every class in the module; find subclasses of MetadataPlugin
#   4. Instantiate each plugin and register it in a dict keyed by its
#      site_id and all aliases (lowercase)
#
# The returned dict is handed to the Router, which uses it for O(1) dispatch.
#
# Adding a new plugin:
#   Drop a .py file into the plugins/ directory and restart the container.
#   No changes to core code needed.
# -----------------------------------------------------------------------------

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import sys

from app.plugins.base import MetadataPlugin

# load_plugins() is not thread-safe — it writes to sys.modules and must only be
# called from a single thread. Hot-reload via SIGUSR2 is serialized by _registry_lock
# in scheduler.py, ensuring only one reload runs at a time.

logger = logging.getLogger(__name__)


def load_plugins(plugin_dir: str, old_registry: dict | None = None) -> dict[str, MetadataPlugin]:
    """
    Discover and load MetadataPlugin subclasses from .py files in plugin_dir.

    Returns a dict mapping every registered id/alias (lowercase) to a plugin instance.
    Returns an empty dict (without raising) if plugin_dir doesn't exist, contains no
    valid plugins, or all files fail to import — callers receive a usable empty registry
    and the warnings are logged so the user can investigate.

    old_registry: if provided, close() is called on each plugin instance before loading
    new ones. This releases any open resources (e.g. httpx.Client connection pools).
    """
    # Close any plugins in the old registry before building the new one.
    # Deduplicate by object identity: a plugin registered under both its site_id
    # and one or more aliases appears multiple times in .values(), and calling
    # close() twice raises RuntimeError on httpx.Client.
    if old_registry is not None:
        for plugin in {id(p): p for p in old_registry.values()}.values():
            try:
                plugin.close()
            except Exception:
                logger.exception("Error closing plugin %s during reload", type(plugin).__name__)

    # The registry maps lowercase site_id / alias → plugin instance
    registry: dict[str, MetadataPlugin] = {}

    if not os.path.isdir(plugin_dir):
        logger.warning("Plugin directory not found: %s", plugin_dir)
        return registry

    # Process files in sorted order for deterministic loading
    for fname in sorted(os.listdir(plugin_dir)):
        # Only load .py files; skip __init__.py and other dunder files.
        # Files starting with "_" are intentionally skipped so the plugin dir
        # can contain private helper modules (e.g. _shared_auth.py) without them
        # being treated as plugins.
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(plugin_dir, fname)
        if fname.startswith("_"):
            logger.debug("Skipping non-plugin file (underscore prefix): %s", fpath)
            continue
        # Namespace the module name to avoid shadowing stdlib modules or other
        # plugins with the same base name (e.g. a plugin named "json.py" would
        # shadow the stdlib json module without this prefix).
        module_name = f"m3_plugin.{fname[:-3]}"

        # Dynamically import the file as a Python module
        try:
            spec = importlib.util.spec_from_file_location(module_name, fpath)
            if spec is None or spec.loader is None:
                logger.warning("Could not create module spec for plugin file: %s", fpath)
                continue
            module = importlib.util.module_from_spec(spec)
            # Register in sys.modules before exec_module so that intra-plugin
            # imports (e.g. a plugin importing a shared helper in the same dir)
            # resolve correctly without creating duplicate module instances.
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception:
            # Log the full traceback but continue loading other plugins
            logger.exception("Failed to import plugin file: %s", fpath)
            sys.modules.pop(module_name, None)  # clean up on failure
            continue

        # Walk every class defined in the module
        for _, obj in inspect.getmembers(module, inspect.isclass):
            # Skip the base class itself and any class that isn't a subclass of MetadataPlugin
            if obj is MetadataPlugin or not issubclass(obj, MetadataPlugin):
                continue
            # Skip classes imported from other modules (only load classes defined here)
            if inspect.getmodule(obj) is not module:
                continue  # skip classes imported from other modules
            # Skip abstract subclasses (intermediate base classes without a concrete fetch())
            if inspect.isabstract(obj):
                continue
            # A plugin must declare a non-empty site_id to be usable
            if not obj.site_id:
                logger.warning("Plugin class %s in %s has no site_id; skipping", obj.__name__, fname)
                continue

            try:
                instance = obj()
            except Exception:
                logger.exception("Failed to instantiate plugin class %s in %s; skipping", obj.__name__, fname)
                continue
            # Register the plugin under its site_id and every alias
            try:
                for key in dict.fromkeys(instance.all_ids()):
                    if key in registry:
                        evicted = registry[key]
                        logger.error(
                            "Plugin %r already registered for id %r; overwriting with %s — "
                            "remove the duplicate from your plugin directory.",
                            type(evicted).__name__, key, obj.__name__,
                        )
                        # Remove ALL keys pointing to the evicted instance before closing it
                        # so no stale references remain after close().
                        stale_keys = [k for k, v in registry.items() if v is evicted]
                        for k in stale_keys:
                            del registry[k]
                        evicted.close()
                    registry[key] = instance
                    logger.info("Registered plugin %s for id %r", obj.__name__, key)
            except Exception:
                logger.exception("Failed to register plugin %s — skipping", obj.__name__)
                # Clean up any partially-registered keys for this instance
                for k in list(registry):
                    if registry[k] is instance:
                        del registry[k]
                continue

        # Clean up sys.modules after successful extraction to prevent stale
        # module references from accumulating across reloads.
        sys.modules.pop(module_name, None)

    return registry
