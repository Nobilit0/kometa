#!/usr/bin/env python3
"""Restore only existing Kometa overlay backups, or snapshot clean Plex artwork.

This script is independent of Kometa's overlay removal runner.  It never
removes poster backup files, does not delete Plex artwork, and leaves unlabeled
items untouched during restore.  Dry-run is the default; use --apply to change.
"""

import argparse
from collections import Counter
from datetime import datetime
from io import BytesIO
import os
from pathlib import Path
import shutil
import sys

import yaml
from PIL import Image, UnidentifiedImageError
from plexapi.server import PlexServer

#DEFAULT_CONFIG = ""
#DEFAULT_OVERLAYS = ""
EXTENSIONS = (".png", ".jpg", ".webp")
FORMAT_EXTENSION = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
MAX_IMAGE_BYTES = 25 * 1024 * 1024


def args_parse(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=("restore", "backup"), help="restore saved clean artwork, or back up current Plex posters")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="Kometa YAML configuration")
    p.add_argument("--overlays", default=DEFAULT_OVERLAYS, help="Kometa overlays folder")
    p.add_argument("--library", action="append", required=True, help="Plex library name; repeat for multiple libraries")
    p.add_argument("--types", nargs="+", choices=("movie", "show", "season", "episode"),
                   help="Optional media types (default: library-compatible types)")
    p.add_argument("--apply", action="store_true", help="Perform changes; otherwise dry-run only")
    p.add_argument("--overwrite", action="store_true", help="Backup mode: replace existing backups, archiving old copies")
    p.add_argument("--include-labeled", action="store_true",
                   help="Backup mode: INCLUDE items still tagged Overlay (risk of saving overlaid images)")
    p.add_argument("--only-missing", action="store_true", help="Backup mode: save only items with no prior backup (default unless --overwrite)")
    return p.parse_args(argv)


def get_plex(config_path):
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    plex_config = (cfg or {}).get("plex") or {}
    url = plex_config.get("url")
    token = plex_config.get("token")
    if not isinstance(url, str) or not isinstance(token, str) or not url or not token:
        raise RuntimeError("Missing literal plex.url or plex.token in YAML; supply a resolved Kometa configuration")
    return PlexServer(url, token, timeout=60), token


def library_types(section, requested):
    valid = ("show", "season", "episode") if section.type == "show" else ("movie",) if section.type == "movie" else ()
    if not valid:
        raise RuntimeError(f"Unsupported library type {section.type!r}")
    if requested:
        invalid = set(requested) - set(valid)
        if invalid:
            raise RuntimeError(f"Media types {sorted(invalid)} do not fit library {section.title} ({section.type})")
        return list(dict.fromkeys(requested))
    return valid


def backup_candidates(folder, key):
    return [folder / f"{key}{ext}" for ext in EXTENSIONS]


def existing_backup(folder, key):
    return next((path for path in backup_candidates(folder, key) if path.is_file()), None)


def image_format(data):
    with Image.open(BytesIO(data)) as im:
        if im.width <= 0 or im.height <= 0:
            raise ValueError("Invalid image dimensions")
        fmt = im.format
        im.verify()
    if fmt not in FORMAT_EXTENSION:
        raise ValueError(f"Unsupported image format: {fmt}")
    return FORMAT_EXTENSION[fmt]


def check_file(path):
    with path.open("rb") as f:
        data = f.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Poster backup exceeds 25 MiB")
    image_format(data)


def title(item):
    return " / ".join(str(x) for x in (
        getattr(item, "grandparentTitle", None),
        getattr(item, "parentTitle", None),
        getattr(item, "title", None)) if x)


def fetch_current_poster(plex, token, item):
    item.reload()  # Selected thumb may have changed since the library listing.
    thumb = getattr(item, "thumb", None)
    if not thumb or not str(thumb).startswith("/"):
        raise RuntimeError("Item has no direct Plex poster/thumbnail path")
    url = plex.url(thumb, includeToken=False)
    with plex._session.get(url, headers={"X-Plex-Token": token}, timeout=60, stream=True) as resp:
        resp.raise_for_status()
        content = bytearray()
        for chunk in resp.iter_content(chunk_size=65536):
            content.extend(chunk)
            if len(content) > MAX_IMAGE_BYTES:
                raise ValueError("Plex poster exceeds 25 MiB")
    data = bytes(content)
    extension = image_format(data)
    return data, extension


def save_image(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def restore(section, types, folder, apply, counts):
    if not folder.is_dir():
        print(f"WARN: Backup directory does not exist: {folder}")
    for kind in types:
        # The same type-filtered Overlay label search Kometa uses.
        print(f"Scanning {section.title} / {kind} with Overlay labels...", flush=True)
        for item in section.search(libtype=kind, label="Overlay"):
            key = str(item.ratingKey)
            name = title(item)
            path = existing_backup(folder, key)
            if not path:
                counts["missing_backup"] += 1
                print(f"SKIP no backup  [{kind}:{key}] {name}")
                continue
            try:
                check_file(path)
                if not apply:
                    counts["would_restore"] += 1
                    print(f"DRY RESTORE     [{kind}:{key}] {name} <- {path.name}")
                    continue
                # Upload clean image first. Never remove the Overlay label if upload fails.
                item.uploadPoster(filepath=str(path))
                item.removeLabel("Overlay")
                counts["restored"] += 1
                print(f"RESTORED        [{kind}:{key}] {name} <- {path.name}")
                # Do not delete the clean backup; Kometa normally deletes it.
            except Exception as exc:
                counts["failed"] += 1
                print(f"ERROR           [{kind}:{key}] {name}: {type(exc).__name__}: {exc}", file=sys.stderr)


def backup(plex, token, section, types, folder, apply, overwrite, include_labeled, counts, archive_dir):
    for kind in types:
        print(f"Scanning {section.title} / {kind} for selected Plex posters...", flush=True)
        for item in section.all(libtype=kind):
            key = str(item.ratingKey)
            name = title(item)
            try:
                # Some objects from Plex lists can be partial; labels may require reload.
                item.reload()
                labels = {label.tag for label in getattr(item, "labels", [])}
                if "Overlay" in labels and not include_labeled:
                    counts["labeled_skipped"] += 1
                    print(f"SKIP Overlay label [{kind}:{key}] {name}")
                    continue
                previous = [p for p in backup_candidates(folder, key) if p.is_file()]
                if previous and not overwrite:
                    counts["already_exists"] += 1
                    print(f"SKIP already saved [{kind}:{key}] {name}")
                    continue
                if not getattr(item, "thumb", None):
                    counts["no_art"] += 1
                    print(f"SKIP no poster    [{kind}:{key}] {name}")
                    continue
                if not apply:
                    counts["would_backup"] += 1
                    print(f"DRY BACKUP      [{kind}:{key}] {name}{' (overwrite)' if previous else ''}")
                    continue
                data, ext = fetch_current_poster(plex, token, item)
                dest = folder / f"{key}{ext}"
                folder.mkdir(parents=True, exist_ok=True)
                if previous:
                    safe_archive = archive_dir / folder.name
                    safe_archive.mkdir(parents=True, exist_ok=True)
                    for old_file in previous:
                        # Preserve an older original before replacing with new clean artwork.
                        shutil.copy2(old_file, safe_archive / old_file.name)
                save_image(dest, data)
                for old_file in previous:
                    if old_file != dest:
                        old_file.unlink()
                counts["backed_up"] += 1
                print(f"BACKED UP       [{kind}:{key}] {name} -> {dest.name}")
            except Exception as exc:
                counts["failed"] += 1
                print(f"ERROR           [{kind}:{key}] {name}: {type(exc).__name__}: {exc}", file=sys.stderr)


def main(argv=None):
    a = args_parse(argv)
    if a.mode == "restore" and (a.overwrite or a.include_labeled or a.only_missing):
        raise SystemExit("Backup-only flags cannot be used with restore mode")
    if a.overwrite and a.only_missing:
        raise SystemExit("Use either --overwrite or --only-missing, not both")
    base = Path(a.overlays).expanduser().resolve()
    plex, token = get_plex(a.config)
    counts = Counter()
    archive_dir = base / "_previous_clean_posters" / datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"Mode: {a.mode} | {'APPLY' if a.apply else 'DRY RUN'} | Root: {base}")
    if a.mode == "backup" and a.include_labeled:
        print("WARNING: --include-labeled can save posters with burned-in Kometa overlays!")
    for library in list(dict.fromkeys(a.library)):
        section = plex.library.section(library)
        types = library_types(section, a.types)
        folder = base / f"{library} Original Posters"
        print(f"\n### {library} ({', '.join(types)}) ###")
        if a.mode == "restore":
            restore(section, types, folder, a.apply, counts)
        else:
            backup(plex, token, section, types, folder, a.apply, a.overwrite, a.include_labeled, counts, archive_dir)
    print("\nSummary: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing found"))
    print("No poster backup was deleted during restore.")
    if counts["failed"]:
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
