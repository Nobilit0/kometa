#!/usr/bin/env python3
"""Safely copy Kometa clean overlay backups to properly named asset folders.

Dry-run by default. Reads local Plex URL/token from Kometa YAML without
third-party dependencies. No Plex metadata is changed by this program.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import collections
import csv
import os
from pathlib import Path, PureWindowsPath, PurePosixPath
import re
import shutil
import tempfile
import datetime
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DEFAULT_CONFIG = Path('/opt/kometa-quickstart/config/kryptonian.yml')
DEFAULT_ASSETS = Path('/media/kometa-assets')
DEFAULT_OVERLAYS = Path('/opt/kometa-quickstart/config/overlays')
LIBRARIES = ('Movies', 'TV Shows', 'Anime')
EXTS = ('jpg', 'jpeg', 'png', 'webp', 'tbn')
SEASON_RE = re.compile(r'^(?:season\s*[0-9]+|series\s*[0-9]+|s[0-9]+|specials)$', re.I)


def parse_plex_config(filename: Path):
    """Read only top-level plex url, token and verify_ssl from Kometa config."""
    props = {}
    in_plex = False
    for raw in filename.read_text(encoding='utf-8').splitlines():
        if re.match(r'^plex:\s*(?:#.*)?$', raw):
            in_plex = True
            continue
        if in_plex and raw.strip() and not raw[0].isspace() and not raw.lstrip().startswith('#'):
            break
        if not in_plex or not raw.strip() or raw.lstrip().startswith('#'):
            continue
        match = re.match(r'^\s+(url|token|verify_ssl):\s*(.*?)\s*$', raw)
        if match:
            k, v = match.groups()
            props[k] = v.split(' #', 1)[0].strip().strip('"\'')
    url = os.environ.get('PLEX_URL') or props.get('url', '')
    token = os.environ.get('PLEX_TOKEN') or props.get('token', '')
    if not url or not token:
        raise ValueError('Plex url or token not found in config; set PLEX_URL and PLEX_TOKEN.')
    return url.rstrip('/'), token, props.get('verify_ssl', 'true').lower() != 'false'


class PlexClient:
    def __init__(self, url, token, verify_ssl):
        self.url = url
        self.token = token
        self.context = None if verify_ssl else ssl._create_unverified_context()

    def get(self, path, params=None):
        endpoint = self.url + '/' + path.lstrip('/')
        if params:
            endpoint += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(endpoint, headers={
            'X-Plex-Token': self.token,
            'Accept': 'application/xml',
            'X-Plex-Client-Identifier': 'kometa-backup-asset-import',
            'X-Plex-Product': 'Kometa Backup Asset Import',
        })
        with urllib.request.urlopen(req, context=self.context, timeout=90) as r:
            return ET.fromstring(r.read())

    def sections(self):
        out = {}
        for e in self.get('/library/sections').findall('./Directory'):
            out[e.get('title')] = (e.get('key'), e.get('type'))
        return out

    def items(self, section_key, plex_type, leaves=False):
        """Stream all Plex items, accepting its native XML element names.

        Plex XML uses <Video> for movies/episodes and <Directory> for
        shows/seasons; <Metadata> is used by other API representations.
        """
        offset = 0
        pagesize = 500
        previous_page_first_key = None
        endpoint = (f'/library/sections/{section_key}/allLeaves' if leaves
                    else f'/library/sections/{section_key}/all')
        while True:
            params = {
                'includeMedia': 1,
                'X-Plex-Container-Start': offset,
                'X-Plex-Container-Size': pagesize,
            }
            if not leaves:
                params['type'] = plex_type
            root = self.get(endpoint, params)
            if root.tag != 'MediaContainer':
                raise RuntimeError(f'Unexpected Plex response root: {root.tag}')
            items = [el for el in root if el.tag in ('Video', 'Directory', 'Metadata')]
            if not items:
                if offset == 0:
                    print(f'  Plex returned no media entries from {endpoint} '
                          f'(XML tags: {[el.tag for el in root][:6]})')
                break
            if offset and items[0].get('ratingKey') == previous_page_first_key:
                raise RuntimeError('Plex ignored pagination; stopping to avoid an incomplete map')
            previous_page_first_key = items[0].get('ratingKey')
            for item in items:
                yield item
            offset += len(items)
            total = root.get('totalSize')
            if total is not None and offset >= int(total):
                break
            if len(items) < pagesize:
                break


def item_path(item):
    file_paths = {
        p.get('file')
        for p in item.findall('.//Media/Part')
        if p.get('file')
    }
    if not file_paths:
        return None
    # If one item has multiple media files in different folders, don't guess.
    if len({str(media_file_parent(p)) for p in file_paths}) > 1:
        return None
    return next(iter(file_paths))


def pure_path(filepath):
    return PureWindowsPath(filepath) if '\\' in filepath else PurePosixPath(filepath)


def media_file_parent(filepath):
    return pure_path(filepath).parent


def movie_asset_name(filepath):
    folder = media_file_parent(filepath).name
    return safe_name(folder)


def show_asset_name(filepath):
    folder = media_file_parent(filepath)
    if SEASON_RE.fullmatch(folder.name):
        folder = folder.parent
    return safe_name(folder.name)


def safe_name(name):
    if not name or name in ('.', '..') or '/' in name or '\\' in name:
        return None
    return name


def int_attr(element, attribute):
    value = element.get(attribute)
    try:
        result = int(value)
        return result if result >= 0 else None
    except (ValueError, TypeError):
        return None


def movie_map(client, section):
    mapping = {}
    for item in client.items(section, 1):
        key = int_attr(item, 'ratingKey')
        filepath = item_path(item)
        folder = movie_asset_name(filepath) if filepath else None
        if key is not None and folder:
            mapping[key] = (folder, 'poster')
    return mapping


def show_map(client, section):
    """Use episode media paths to resolve the true show folder name."""
    def build_from(episodes):
        episode_rows = []
        folders_by_show = collections.defaultdict(set)
        for item in episodes:
            filepath = item_path(item)
            show_key = int_attr(item, 'grandparentRatingKey')
            show_folder = show_asset_name(filepath) if filepath else None
            if show_key is not None and show_folder:
                folders_by_show[show_key].add(show_folder)
            episode_rows.append((item, show_key))
        return episode_rows, folders_by_show

    episode_rows, folders_by_show = build_from(client.items(section, 4))
    if not folders_by_show:
        print('  No episode file paths found using type=4; trying Plex allLeaves...')
        episode_rows, folders_by_show = build_from(client.items(section, 4, leaves=True))

    # Different folders for the same Plex show are ambiguous: skip safely.
    unique_show_folder = {
        show_key: next(iter(folders))
        for show_key, folders in folders_by_show.items()
        if len(folders) == 1
    }
    mapping = {k: (folder, 'poster') for k, folder in unique_show_folder.items()}
    for episode, show_key in episode_rows:
        if show_key not in unique_show_folder:
            continue
        folder = unique_show_folder[show_key]
        season_key = int_attr(episode, 'parentRatingKey')
        season = int_attr(episode, 'parentIndex')
        episode_key = int_attr(episode, 'ratingKey')
        number = int_attr(episode, 'index')
        if season is None:
            continue
        if season_key is not None:
            mapping[season_key] = (folder, f'Season{season:02d}')
        if episode_key is not None and number is not None:
            mapping[episode_key] = (folder, f'S{season:02d}E{number:02d}')
    return mapping


def existing_assets(destination):
    """Recognized existing Kometa artwork names, including alternate formats."""
    names = [destination.stem]
    if destination.stem == 'poster':
        names += ['cover', 'default', 'folder', 'movie']
    return [destination.parent / f'{name}.{ext}'
            for name in names for ext in EXTS
            if (destination.parent / f'{name}.{ext}').exists()
            or (destination.parent / f'{name}.{ext}').is_symlink()]


def has_existing_asset(destination):
    return bool(existing_assets(destination))


def existing_image_condition(path, pillow):
    """Conservatively distinguish damaged images from valid custom artwork.

    A small or visually blank image may be intentional and is not 'corrupt'.
    Unreadable files or symlinks must not be replaced automatically.
    """
    if path.is_symlink():
        return 'unsafe_symlink'
    if not path.is_file():
        return 'existing_not_regular_file'
    try:
        with pillow.open(path) as img:
            img.verify()
        # verify() only checks container integrity; decode pixels as well.
        with pillow.open(path) as img:
            img.load()
            if img.width <= 0 or img.height <= 0:
                return 'invalid_existing'
        return 'valid'
    except PermissionError:
        return 'existing_unreadable'
    except (OSError, ValueError, SyntaxError, TypeError):
        return 'invalid_existing'
    except Exception:
        # Unexpected decode/IO failures are not proof of corruption.
        return 'existing_unreadable'


def safe_asset_path(target, root):
    """Reject any destination whose parent resolves outside the asset root."""
    try:
        return target.parent.resolve().is_relative_to(root.resolve()) and not target.is_symlink()
    except (OSError, RuntimeError):
        return False


def replacement_copy(source, destination, bad_assets, backup_root, assets_root, pillow):
    """Back up every corrupt asset, then atomically publish a good JPEG.

    Caller must stop active Kometa artwork processing. Backups live outside the
    asset root, keeping damaged .png/.webp alternatives from being selected.
    """
    if not safe_asset_path(destination, assets_root):
        raise RuntimeError('Unsafe asset destination or symlink')
    current = existing_assets(destination)
    if set(current) != set(bad_assets):
        raise FileExistsError('Existing artwork changed since preflight')
    for path in current:
        if existing_image_condition(path, pillow) != 'invalid_existing':
            raise FileExistsError(f'Artwork no longer confirmed corrupt: {path}')
    if valid_jpeg(source, True, pillow):
        raise RuntimeError('Source JPEG failed revalidation')

    # Fully stage the new image before disturbing any existing poster.
    fd, temp_name = tempfile.mkstemp(prefix='.kometa-restore-', suffix='.tmp', dir=destination.parent)
    staged = Path(temp_name)
    backup_paths = {}
    removed = []
    published = False
    try:
        with os.fdopen(fd, 'wb') as out, source.open('rb') as inp:
            shutil.copyfileobj(inp, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        shutil.copystat(source, staged)
        # Back up before deleting or replacing a single original.
        for path in current:
            backup_path = backup_root / path.relative_to(assets_root)
            exclusive_copy(path, backup_path)
            backup_paths[path] = backup_path
        # Recheck immediately before removal, as a second race guard.
        if set(existing_assets(destination)) != set(current):
            raise FileExistsError('Asset set changed during backup')
        for path in current:
            if existing_image_condition(path, pillow) != 'invalid_existing':
                raise FileExistsError(f'Existing artwork changed: {path}')
        for path in current:
            path.unlink()
            removed.append(path)
        os.replace(staged, destination)
        published = True
    except BaseException:
        # Restore preexisting files on failure; never overwrite new files.
        if not published:
            for path in removed:
                if not path.exists() and path in backup_paths:
                    try:
                        exclusive_copy(backup_paths[path], path)
                    except Exception as exc:
                        print(f'CRITICAL: Unable to restore {path} from backup: {exc}', file=sys.stderr)
        raise
    finally:
        staged.unlink(missing_ok=True)
    return len(current)


def valid_jpeg(path, allow_small, pillow):
    try:
        if path.stat().st_size < 10 * 1024 and not allow_small:
            return 'small_source'
        with path.open('rb') as f:
            if not f.read(3).startswith(b'\xff\xd8\xff'):
                return 'non_jpeg_source'
        if pillow:
            with pillow.open(path) as img:
                if img.format != 'JPEG':
                    return 'non_jpeg_source'
                img.verify()
    except Exception:
        return 'corrupt_source'
    return None


def suspected_visual_blank(path, pillow):
    """Conservative *heuristic*, never proof of a visually blank image.

    An image is suspicious if almost every sampled pixel is white, black,
    transparent, or a single flat color. Do not use for automatic replacement.
    Returns a short description or None.
    """
    if pillow is None:
        return None
    try:
        with pillow.open(path) as img:
            # Downsample, bounding the cost even for very large JPEGs.
            img.thumbnail((72, 72))
            rgba = img.convert('RGBA')
            pixels = list(rgba.getdata())
        if not pixels:
            return 'empty_pixel_grid'
        count = len(pixels)
        mostly_transparent = sum(a < 10 for _, _, _, a in pixels)
        if mostly_transparent / count >= .99:
            return 'almost_all_transparent'
        visible = [(r, g, b) for r, g, b, a in pixels if a >= 10]
        if not visible:
            return 'almost_all_transparent'
        n = len(visible)
        white = sum(min(p) >= 248 for p in visible)
        black = sum(max(p) <= 7 for p in visible)
        if white / n >= .995:
            return 'almost_all_white'
        if black / n >= .995:
            return 'almost_all_black'
        means = [sum(pixel[c] for pixel in visible) / n for c in range(3)]
        variances = [sum((pixel[c] - means[c]) ** 2 for pixel in visible) / n for c in range(3)]
        if max(math.sqrt(v) for v in variances) <= 1.75:
            return 'almost_uniform_color'
        return None
    except Exception:
        # A read failure is covered by normal corruption validation instead.
        return None


def ambiguous_preview(options, target, preview_dir, pillow):
    """Generate numbered contact sheet for server-side review, if requested."""
    if pillow is None or preview_dir is None:
        return None
    try:
        from PIL import ImageDraw, ImageOps
        maxcols = min(4, len(options))
        cell_w, cell_h = 250, 380
        rows = math.ceil(len(options) / maxcols)
        out = pillow.new('RGB', (maxcols * cell_w, rows * cell_h), '#ededed')
        draw = ImageDraw.Draw(out)
        for i, (library, source, _) in enumerate(options):
            x = (i % maxcols) * cell_w
            y = (i // maxcols) * cell_h
            try:
                with pillow.open(source) as im:
                    image = ImageOps.contain(im.convert('RGB'), (232, 330))
                    out.paste(image, (x + (cell_w-image.width)//2, y + 7))
            except Exception:
                draw.text((x+10, y+70), 'Unreadable image', fill='black')
            draw.text((x+9, y+340), f'{i+1}: {source.stem} ({library})', fill='black')
        preview_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(str(target).encode('utf-8')).hexdigest()[:12]
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        # Unique filename even if successive runs occur within the same second.
        fd, name = tempfile.mkstemp(prefix=f'ambiguous-{digest}-{timestamp}-',
                                     suffix='.jpg', dir=preview_dir)
        result = Path(name)
        try:
            with os.fdopen(fd, 'wb') as out_file:
                out.save(out_file, format='JPEG', quality=88)
        except BaseException:
            result.unlink(missing_ok=True)
            raise
        return result
    except Exception as e:
        print(f'WARNING: Could not create preview for {target}: {e}', file=sys.stderr)
        return None


def resolve_ambiguous(jobs, conflicting_destinations, args, pillow):
    """Resolve destination collisions through explicit interactive choice.

    Never auto-pick, even if only one candidate appears valid. In dry run,
    print numbered candidates and retain ambiguous_destination status.
    """
    groups = collections.defaultdict(list)
    for job in jobs:
        if str(job[2]) in conflicting_destinations:
            groups[str(job[2])].append(job)
    selected = {}
    if not groups:
        return selected
    interactive = args.apply and not args.no_prompt_ambiguous and sys.stdin.isatty()
    if args.apply and not interactive:
        print('WARNING: Ambiguous destinations will be SKIPPED (requires interactive terminal during --apply).')
    preview_dir = args.ambiguous_preview_dir
    if preview_dir is None and args.report:
        preview_dir = args.report.parent / 'kometa-ambiguous-previews'
    for destination in sorted(groups):
        options = sorted(groups[destination], key=lambda j: (j[0], str(j[1])))
        print(f'\nAMBIGUOUS DESTINATION: {destination}')
        for i, (library, src, _) in enumerate(options, 1):
            try:
                size = src.stat().st_size
                validity = valid_jpeg(src, args.include_small, pillow)
                detail = f'{size:,} bytes; {validity or "valid JPEG"}'
                if pillow:
                    try:
                        with pillow.open(src) as im:
                            detail += f'; {im.width}x{im.height}'
                    except Exception:
                        pass
                if args.scan_visual_blank and validity is None:
                    suspicion = suspected_visual_blank(src, pillow)
                    if suspicion:
                        detail += f'; SUSPECTED BLANK ({suspicion})'
            except Exception as e:
                detail = f'cannot read: {e}'
            print(f'  {i}) [{library}] Plex key {src.stem}: {src} [{detail}]')
        print('  0) Skip this destination (safe default)')
        sheet = ambiguous_preview(options, Path(destination), preview_dir, pillow)
        if sheet:
            print(f'  PREVIEW: {sheet}')
        if not interactive:
            print('  No selection made (dry run or noninteractive apply).')
            continue
        while True:
            try:
                answer = input(f'Select poster 1-{len(options)} or 0 to skip [0]: ').strip()
            except (EOFError, KeyboardInterrupt):
                print('\n  Selection cancelled; destination skipped.')
                break
            if answer in ('', '0'):
                print('  Skipped.')
                break
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                job = options[int(answer)-1]
                source_err = valid_jpeg(job[1], args.include_small, pillow)
                if source_err:
                    print(f'  This image cannot be selected: {source_err}. Choose another.')
                    continue
                if args.scan_visual_blank:
                    susp = suspected_visual_blank(job[1], pillow)
                    if susp:
                        print(f'  This image appears blank ({susp}); choose another or 0.')
                        continue
                selected[destination] = job
                print(f'  Selected: {job[1].name} -> {job[2]}')
                break
            print('  Invalid choice; enter one of the displayed numbers.')
    return selected


def exclusive_copy(source, target):
    """Copy only if target absent; no overwrites even if another process races."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, 'wb') as out, source.open('rb') as inp:
            shutil.copyfileobj(inp, out, 1024 * 1024)
        shutil.copystat(source, target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--assets', type=Path, default=DEFAULT_ASSETS)
    parser.add_argument('--overlays', type=Path, default=DEFAULT_OVERLAYS)
    parser.add_argument('--library', choices=LIBRARIES, action='append',
                        help='One or more libraries; omit for all three')
    parser.add_argument('--apply', action='store_true',
                        help='Actually copy files (otherwise dry-run)')
    parser.add_argument('--include-small', action='store_true',
                        help='Also copy images smaller than 10 KiB')
    parser.add_argument('--replace-invalid', action='store_true',
                        help='Replace only corrupted existing images, preserving backups; requires Pillow')
    parser.add_argument('--invalid-backup-dir', type=Path,
                        help='Required for --replace-invalid --apply if repairs are needed; must be outside assets')
    parser.add_argument('--scan-visual-blank', action='store_true',
                        help='Heuristically report near-uniform/black/white/transparent images; never auto-replace')
    parser.add_argument('--no-prompt-ambiguous', action='store_true',
                        help='Skip ambiguous destinations without asking, even during interactive --apply')
    parser.add_argument('--ambiguous-preview-dir', type=Path,
                        help='Where to create numbered contact sheets (default: report parent/kometa-ambiguous-previews)')
    parser.add_argument('--report', type=Path,
                        help='Optional CSV report with per-item results')
    args = parser.parse_args()

    try:
        plex_url, token, verify_ssl = parse_plex_config(args.config)
        plex = PlexClient(plex_url, token, verify_ssl)
        sections = plex.sections()
    except Exception as e:
        sys.exit(f'Unable to access Plex: {e}')

    chosen = args.library or list(LIBRARIES)
    jobs = []
    skipped = collections.Counter()
    try:
        for library in chosen:
            source_dir = args.overlays / f'{library} Original Posters'
            if not source_dir.is_dir():
                print(f'WARNING: Missing directory {source_dir}; skipping')
                continue
            if library not in sections:
                print(f'WARNING: Plex library "{library}" not found; skipping')
                continue
            section, library_type = sections[library]
            print(f'Reading Plex metadata for {library}...', flush=True)
            if library_type == 'movie':
                mapping = movie_map(plex, section)
            elif library_type == 'show':
                mapping = show_map(plex, section)
            else:
                print(f'WARNING: Unsupported Plex type {library_type}; skipping')
                continue
            print(f'  Indexed {len(mapping)} poster/season/episode assets')
            if not mapping:
                sys.exit(
                    f'ERROR: Plex library "{library}" indexed zero usable entries. '
                    'No backups have been copied. Check the Plex response and '
                    'whether media file paths are present in the API output.'
                )
            for source in source_dir.glob('*.jpg'):
                try:
                    key = int(source.stem)
                except ValueError:
                    skipped[(library, 'invalid_rating_key')] += 1
                    continue
                dest_info = mapping.get(key)
                if dest_info is None:
                    skipped[(library, 'not_mapped')] += 1
                    continue
                folder, name = dest_info
                dest = args.assets / folder / f'{name}.jpg'
                jobs.append((library, source, dest))
    except Exception as e:
        sys.exit(f'Could not finish Plex metadata mapping: {e}')

    # A shared asset root must not silently mix two different items targeting
    # the same asset, even across library boundaries.
    by_destination = collections.defaultdict(set)
    for library, source, target in jobs:
        by_destination[str(target)].add(str(source))
    conflicting_destinations = {
        dest for dest, srcs in by_destination.items() if len(srcs) > 1
    }

    try:
        from PIL import Image
    except ImportError:
        Image = None
        if args.replace_invalid or args.scan_visual_blank:
            sys.exit('ERROR: --replace-invalid and --scan-visual-blank require Pillow. Install it with: apt install python3-pil')
        print('NOTE: Pillow not installed; checking JPEG file signatures only. Numbered previews unavailable.')

    selected_ambiguous = resolve_ambiguous(jobs, conflicting_destinations, args, Image)
    rows = []
    copy_jobs = []  # (library, source, destination, kind, invalid_existing_paths)
    visual_alerts = collections.Counter()
    for library, source, target in jobs:
        status = None
        source_suspicion = suspected_visual_blank(source, Image) if args.scan_visual_blank else None
        if source_suspicion:
            visual_alerts[(library, 'suspected_blank_source')] += 1
            print(f'  SUSPECTED BLANK original backup ({source_suspicion}): {source}')
        if str(target) in conflicting_destinations:
            selection = selected_ambiguous.get(str(target))
            if selection is None:
                status = 'ambiguous_destination'
            elif (library, source, target) != selection:
                status = 'ambiguous_not_selected'
        if status is None and not safe_asset_path(target, args.assets):
            status = 'unsafe_destination'
        if status is None:
            existing = existing_assets(target)
            # For image that decodes but appears blank, log a separate row.
            # Never replace on the basis of appearance alone.
            if args.scan_visual_blank:
                for old in existing:
                    suspicion = suspected_visual_blank(old, Image)
                    if suspicion:
                        rows.append((library, old, target, 'suspected_blank_existing'))
                        visual_alerts[(library, 'suspected_blank_existing')] += 1
                        print(f'  SUSPECTED BLANK existing asset ({suspicion}): {old}')
            if not existing:
                kind = 'copy'
                invalid_paths = []
            elif not args.replace_invalid:
                status = 'existing_asset'
            else:
                results = [existing_image_condition(x, Image) for x in existing]
                if 'valid' in results:
                    status = 'existing_asset_valid'
                elif any(r != 'invalid_existing' for r in results):
                    status = 'existing_asset_unreadable_or_unsafe'
                else:
                    kind = 'replace_invalid'
                    invalid_paths = existing
                    # Include a diagnostic record distinct from the operation result.
                    for bad in existing:
                        rows.append((library, bad, target, 'invalid_existing'))
            if status is None:
                status = valid_jpeg(source, args.include_small, Image)
            if status is None and source_suspicion:
                status = 'suspected_blank_source'
        if source_suspicion and status != 'suspected_blank_source':
            rows.append((library, source, target, 'suspected_blank_source'))
        if status:
            skipped[(library, status)] += 1
            rows.append((library, source, target, status))
        else:
            copy_jobs.append((library, source, target, kind, invalid_paths))

    new_jobs = [j for j in copy_jobs if j[3] == 'copy']
    repair_jobs = [j for j in copy_jobs if j[3] == 'replace_invalid']
    bytes_needed = sum(source.stat().st_size for _, source, _, _, _ in copy_jobs)
    backup_bytes = sum(path.stat().st_size for *_, paths in repair_jobs for path in paths)
    print(f'\nMode: {"APPLY" if args.apply else "DRY RUN"}')
    print(f'Eligible images: {len(copy_jobs)}')
    print(f'  New assets: {len(new_jobs)}')
    print(f'  Corrupt assets to repair: {len(repair_jobs)}')
    print(f'Additional asset storage (conservative): {bytes_needed / 1024**3:.2f} GiB')
    if repair_jobs:
        print(f'Additional space for damaged-file backups: {backup_bytes / 1024**3:.2f} GiB')
    if skipped:
        print('Skipped:')
        for (library, reason), count in sorted(skipped.items()):
            print(f'  {library}: {reason}: {count}')
    if args.scan_visual_blank:
        print('Visual-blank inspection (heuristic, not a corruption test):')
        if visual_alerts:
            for (library, reason), count in sorted(visual_alerts.items()):
                print(f'  {library}: {reason}: {count}')
        print('  Suspected blank source warnings:', sum(v for (lib, reason), v in visual_alerts.items() if reason == 'suspected_blank_source'))
        print('  Existing suspicious assets are flagged in CSV but left intact.')
    print('First eligible destinations:')
    for library, src, dest, kind, _ in copy_jobs[:12]:
        print(f'  [{library}] {kind}: {src.name} -> {dest}')

    backup_root = None
    if args.apply:
        # Never silently create the root of a missing network mount.
        if not args.assets.is_dir():
            sys.exit(f'Asset directory {args.assets} is unavailable; check the mount first.')
        required_asset_space = bytes_needed
        if repair_jobs:
            if args.invalid_backup_dir is None:
                sys.exit('ERROR: --replace-invalid --apply needs --invalid-backup-dir for preserving bad files.')
            if not args.invalid_backup_dir.is_dir():
                sys.exit('ERROR: Invalid-artwork backup directory does not exist; create it on the intended mounted storage first.')
            assets_root = args.assets.resolve()
            backups = args.invalid_backup_dir.resolve()
            if backups == assets_root or backups.is_relative_to(assets_root) or assets_root.is_relative_to(backups):
                sys.exit('ERROR: Invalid-artwork backups must be outside the asset directory.')
            if backups.stat().st_dev == assets_root.stat().st_dev:
                required_asset_space += backup_bytes
            else:
                if backup_bytes > shutil.disk_usage(backups).free:
                    sys.exit('Insufficient free space in invalid-artwork backup storage.')
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup_root = backups / f'kometa-invalid-{stamp}-{os.getpid()}'
        if required_asset_space > shutil.disk_usage(args.assets).free:
            sys.exit('Insufficient free asset storage; no changes made.')

    copied = 0
    repaired = 0
    errors = 0
    for library, source, target, kind, bad_paths in copy_jobs:
        if not args.apply:
            rows.append((library, source, target,
                         'would_replace_invalid' if kind == 'replace_invalid' else 'would_copy'))
            continue
        try:
            if not safe_asset_path(target, args.assets):
                raise RuntimeError('Unsafe asset destination')
            if kind == 'copy':
                if has_existing_asset(target):
                    rows.append((library, source, target, 'existing_asset'))
                    continue
                exclusive_copy(source, target)
                rows.append((library, source, target, 'copied'))
                copied += 1
            else:
                if backup_root is None:
                    raise RuntimeError('Invalid backup directory was not configured')
                replacement_copy(source, target, bad_paths, backup_root,
                                 args.assets, Image)
                rows.append((library, source, target, 'replaced_invalid'))
                repaired += 1
        except FileExistsError:
            rows.append((library, source, target, 'existing_asset_changed_or_valid'))
        except Exception as e:
            rows.append((library, source, target, 'error'))
            errors += 1
            print(f'ERROR processing {source}: {e}', file=sys.stderr)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['library', 'source', 'destination', 'status'])
            writer.writerows(rows)
        print(f'Report written to {args.report}')
    if backup_root and repaired:
        print(f'Damaged originals preserved under: {backup_root}')
    print(f'Done. Copied: {copied}, repaired: {repaired}, errors: {errors}. Original Kometa backups left unchanged.')
    if not args.apply:
        print('This was a dry run. Add --apply to perform the copies.')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
