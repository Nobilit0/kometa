# Installation
Replace your existing script with the downloaded version at:
/kometa_restore_backup_assets_interactive.py
```bash
curl -fL --retry 3 -o /kometa_restore_backup_assets_interactive.py "https://raw.githubusercontent.com/Nobilit0/kometa/refs/heads/main/scripts/kometa_restore_backup_assets_interactive.py"
```

Install Pillow, which is required for detecting corrupted images. This installs the required dependency and verifies it:
```bash
apt update && apt install -y python3-pil
python3 -c "from PIL import Image; print('Pillow installed:', Image.__version__)"
```

Check the script for syntax errors and display all supported options:
```bash
python3 -m py_compile /kometa_restore_backup_assets_interactive.py
python3 /kometa_restore_backup_assets_interactive.py --help
```

Create the directory for preserving damaged artwork:
```bash
mkdir -p /media/backup/kometa-invalid-assets
```

Make sure /media/backup and /media/kometa-assets are mounted on the intended storage.
## Copy Original Posters to Kometa Assets
### Stop any active Kometa overlay processing first, then run:
1. Preview all libraries in Plex
```bash
python3 /kometa_restore_backup_assets_interactive.py --report /media/backup/kometa-assets-preview.csv
```
2. Initial run to copy files over
```bash
python3 /kometa_restore_backup_assets_interactive.py --apply --report /media/backup/kometa-assets-applied.csv
```

## Repair option 
1. Preview repairs without changing anything
```
python3 /kometa_restore_backup_assets_interactive.py --replace-invalid --report /media/backup/kometa-assets-preview.csv
```

The results will distinguish newly eligible artwork from corrupted artwork that can be repaired.
2. Apply the repairs

```bash
python3 /kometa_restore_backup_assets_interactive.py --apply --replace-invalid --invalid-backup-dir /media/backup/kometa-invalid-assets --report /media/backup/kometa-assets-applied.csv
```

Your original poster backups in /opt/kometa-quickstart/config/overlays/ will remain untouched.

The script also retains the previous behavior when --replace-invalid is omitted, so you can still run it without repairing existing files.


## Optional Command - By default the --apply option will prompt and log AMBIGUOUS DESTINATION due to different versions of the same movie
While the script is running it will display something like:
AMBIGUOUS DESTINATION:
  /media/kometa-assets/Movie Title (1972)/poster.jpg

1) 450414.jpg
2) 460424.jpg
3) 470425.jpg
0) Skip this destination

Select poster 1-3 or 0 to skip [0]:


Enter 1, 2, or 3 to select the source image. Enter 0 or press Enter to skip.

The script also creates a numbered JPEG contact sheet containing previews of all three images.

1. Preview the changes
```bash
python3 /kometa_restore_backup_assets_interactive.py --replace-invalid --scan-visual-blank --ambiguous-preview-dir /media/backup/kometa-ambiguous-previews --report /media/backup/kometa-assets-preview.csv
```

Dry-run mode shows ambiguous candidates but does not ask for selections or modify your assets.
2. Apply with interactive selection
After replacing the old script with the downloaded version, run:
```bash
python3 /kometa_restore_backup_assets_interactive.py --apply --replace-invalid --scan-visual-blank --invalid-backup-dir /media/backup/kometa-invalid-assets --ambiguous-preview-dir /media/backup/kometa-ambiguous-previews --report /media/backup/kometa-assets-applied.csv
```

Run this directly in an interactive terminal. If no terminal is available, ambiguous destinations are skipped rather than automatically chosen.
3. Review visually suspicious posters
The CSV will include suspected_blank_source and suspected_blank_existing records when the optional scan detects likely blank artwork. Those findings are for review; they do not trigger automatic replacement.

You can extract them with:
```bash
grep 'suspected_blank_' /media/backup/kometa-assets-applied.csv
```

The script retains /media/kometa-assets as its destination, preserves good existing artwork, and leaves Kometa's original backups untouched.

If you prefer to not be prompted for the ambiguous destination, run the following:
```bash
python3 /kometa_restore_backup_assets_interactive.py --apply --no-prompt-ambiguous --report /media/backup/kometa-assets-applied.csv
```
This skips ambiguous destinations instead of prompting you to choose a poster.

It is useful for scheduled jobs or unattended execution.

### NOTE:
Visual blank detection only inspects artwork associated with mapped Plex items. Images flagged as suspicious are not automatically repaired. 
Valid existing posters are preserved, even when multiple backup candidates are available.

## Other optional commands

This is especially useful when you want to fix only Movies without reprocessing all three libraries.

Movies only:
```bash
python3 /kometa_restore_backup_assets_interactive.py --library Movies --apply --report /media/backup/kometa-movies.csv
```

TV Shows only:
```bash
python3 /kometa_restore_backup_assets_interactive.py --library "TV Shows" --apply --report /media/backup/kometa-tv.csv
```

Anime only:
```bash
python3 /kometa_restore_backup_assets_interactive.py --library Anime --apply --report /media/backup/kometa-anime.csv
```

You can also repeat --library to process selected libraries together.


The script also generates image contact sheets. Run this command to locate them:
```bash
find /media/backup/kometa-ambiguous-previews -maxdepth 1 -type f -name '*.jpg'
```
The script prints each contact sheet's location; you must open the image separately to compare the posters visually.


When --scan-visual-blank is enabled, the script flags suspiciously blank source images and skips copying them. Existing valid artwork is preserved. 


### Optional: Include Small Images
The script deliberately skips backup images under 10 KiB unless the user overrides that behavior.

Add this optional command:
```bash
python3 /kometa_restore_backup_assets_interactive.py --include-small --report /media/backup/kometa-small-preview.csv
```

This allows small backup images to be considered for copying. It does not mean they are valid or appropriate artwork, so it is recommended to preview the results before using --apply.


### Final Verification
After the bulk copy or repair, these commands will help confirm that the operation succeeded:
```bash
du -sh /media/kometa-assets
find /media/kometa-assets -type f | wc -l
```

Check the report for processing errors:
```bash
sed 's/\r$//' /media/backup/kometa-assets-applied.csv | grep -E ',error$'
```

And inspect successful operations:
```bash
sed 's/\r$//' /media/backup/kometa-assets-applied.csv | grep -E ',(copied|replaced_invalid)$' | head -30
```

Check visually suspicious images:
```bash
grep -E 'suspected_blank_|invalid_existing' /media/backup/kometa-assets-applied.csv
```
The output should help distinguish newly copied posters from repaired corrupted ones.


## A short note
'--scan-visual-blank' uses an image-content heuristic. It can flag nearly uniform but intentionally designed artwork.

To review suspicious images in a completed report:
```bash
grep -E 'suspected_blank_|invalid_existing' /media/backup/kometa-assets-applied.csv
```
