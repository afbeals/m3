#!/usr/bin/env python3
"""
generate_test_library.py — create a fake m3 media library for local testing.

Creates empty .mp4 files with realistic filenames covering every parse subtype
(exact, enhanced, limited) and the Manual Add form. No real media needed.

Usage:
    python scripts/generate_test_library.py
    python scripts/generate_test_library.py --output /tmp/m3-test-media --site mysite
    python scripts/generate_test_library.py --output C:\\m3-test-media --site mysite

The generated directory can be used directly as LIBRARY_PATHS when running m3:
    LIBRARY_PATHS=./test-media python -m app.main --once --dry-run
"""

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a fake m3 test media library")
    parser.add_argument(
        "--output",
        default="./test-media",
        help="Directory to create (default: ./test-media)",
    )
    parser.add_argument(
        "--site",
        default="examplesite",
        help="Site token to use in filenames (default: examplesite)",
    )
    args = parser.parse_args()

    out = args.output
    site = args.site.lower()

    os.makedirs(out, exist_ok=True)

    files = [
        # --- exact match (scene ID only) ---
        f"Jane Doe % {site} - 12345.mp4",
        f"Mary Smith % {site} - 67890.mp4",

        # --- exact match (direct URL slug) ---
        f"Jane Doe % {site} - eager-hands.mp4",

        # --- enhanced match (date + scene ID + title) ---
        f"Jane Doe with Drama % {site} - 2024-06-15 - 12345 - An Interesting Scene.mp4",
        f"Alice Brown And Carol White with Comedy % {site} - 2023-11-01 - 99999 - Another Title.mp4",

        # --- enhanced match (date only, no ID) ---
        f"Jane Doe % {site} - 2024-01-20 - Some Title Here.mp4",

        # --- limited match (title only, no date/ID) ---
        f"Jane Doe with Romance % {site} - My Scene Title.mp4",
        f"Mary Smith And Alice Brown % {site} - A Different Scene.mp4",

        # --- already has NFO (will be skipped on normal runs) ---
        f"Processed File % {site} - 11111.mp4",

        # --- Manual Add form ---
        "Add Jane Doe And Mary Smith In Great Scene At MyStudio.mp4",
        "Add Alice Brown In Her Scene At AnotherStudio With Drama, Comedy.mp4",

        # --- unmatched (no site token, not an Add form) ---
        "Unknown File Without Token.mp4",
        "Another Unmatched File.mp4",

        # --- alias test (uppercase alias) ---
        f"Jane Doe % {site.upper()} - 55555.mp4",
    ]

    # Write a companion .nfo for the "already processed" file so m3 skips it
    processed_stem = f"Processed File % {site} - 11111"
    nfo_path = os.path.join(out, f"{processed_stem}.nfo")
    nfo_content = '<?xml version="1.0" encoding="UTF-8"?>\n<movie><title>Processed</title></movie>\n'
    with open(nfo_path, "w", encoding="utf-8") as fh:
        fh.write(nfo_content)

    created = 0
    for fname in files:
        fpath = os.path.join(out, fname)
        if not os.path.exists(fpath):
            open(fpath, "w").close()
            created += 1
        else:
            print(f"  (exists) {fname}")

    print(f"\nCreated {created} file(s) in: {os.path.abspath(out)}")
    print(f"Pre-existing .nfo written for: {processed_stem}")
    print(f"\nTest with:")
    if sys.platform == "win32":
        print(f"  set LIBRARY_PATHS={os.path.abspath(out)}")
        print(f"  set PLUGIN_DIR=plugins")
        print(f"  python -m app.main --once --dry-run --list-unmatched")
    else:
        print(f"  LIBRARY_PATHS={os.path.abspath(out)} PLUGIN_DIR=./plugins \\")
        print(f"  python -m app.main --once --dry-run")


if __name__ == "__main__":
    main()
