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

logger = logging.getLogger(__name__)


def load_plugins(plugin_dir: str) -> dict[str, MetadataPlugin]:
    """
    Discover and load MetadataPlugin subclasses from .py files in plugin_dir.

    Returns a dict mapping every registered id/alias (lowercase) to a plugin instance.
    Returns an empty dict (without raising) if plugin_dir doesn't exist, contains no
    valid plugins, or all files fail to import — callers receive a usable empty registry
    and the warnings are logged so the user can investigate.
    """
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
        if not fname.endswith(".py") or fname.startswith("_"):
            continue

        fpath = os.path.join(plugin_dir, fname)
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
            for key in instance.all_ids():
                if key in registry:
                    # Two plugins claim the same id — last one loaded wins
                    logger.error(
                        "Plugin id %r already registered by %s; overwriting with %s",
                        key,
                        type(registry[key]).__name__,
                        obj.__name__,
                    )
                registry[key] = instance
                logger.info("Registered plugin %s for id %r", obj.__name__, key)

    return registry
