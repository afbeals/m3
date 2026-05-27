# -----------------------------------------------------------------------------
# app/tools/parse.py
#
# Standalone command-line tool to test the filename parser without running the
# full pipeline. Useful during plugin development to verify that a filename stem
# decodes into the fields you expect before writing any API calls.
#
# Usage:
#   python -m app.tools.parse "Jane Doe with Drama % examplesite - 12345"
#   python -m app.tools.parse "Add Jane Doe In My Scene At MyStudio"
#   python -m app.tools.parse --json "Jane Doe % ES - eager-hands"
#
# Output (default):
#   Prints each ParsedFilename field on its own line, skipping None/empty values.
#
# Output (--json):
#   Prints the full ParsedFilename as a JSON object (all fields, including nulls).
# -----------------------------------------------------------------------------

from __future__ import annotations

import argparse
import json
import sys

from app.parser import parse


def _to_dict(parsed) -> dict:
    """Convert a ParsedFilename dataclass to a plain dict for JSON serialisation."""
    return {
        "form": parsed.form,
        "actors": parsed.actors,
        "genres": parsed.genres,
        "site": parsed.site,
        "match_subtype": parsed.match_subtype,
        "date": parsed.date,
        "scene_id": parsed.scene_id,
        "studio_id": parsed.studio_id,
        "actress_id": parsed.actress_id,
        "direct_url": parsed.direct_url,
        "title": parsed.title,
        "extra_actors": parsed.extra_actors,
        "studio": parsed.studio,
        "raw_match_payload": parsed.raw_match_payload,
    }


def _print_human(stem: str, parsed) -> None:
    """Print a human-readable summary, skipping fields that are None or empty."""
    print(f"\nStem: {stem!r}")
    d = _to_dict(parsed)
    for key, value in d.items():
        # Skip None and empty lists — only show what was actually extracted
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        print(f"  {key:<22} {value!r}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test the m3 filename parser from the command line.",
        epilog='Example: python -m app.tools.parse "Jane Doe with Drama % mysite - 12345"',
    )
    parser.add_argument(
        "stems",
        nargs="+",
        metavar="STEM",
        help="One or more filename stems to parse (the filename without the extension)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output results as JSON (one object per line)",
    )
    args = parser.parse_args()

    exit_code = 0
    for stem in args.stems:
        result = parse(stem)
        if result is None:
            if args.as_json:
                print(json.dumps({"stem": stem, "parsed": None}))
            else:
                print(f"\nStem: {stem!r}")
                print("  UNMATCHED — could not parse this filename")
            exit_code = 1
        else:
            if args.as_json:
                print(json.dumps({"stem": stem, "parsed": _to_dict(result)}))
            else:
                _print_human(stem, result)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
