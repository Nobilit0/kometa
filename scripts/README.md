# Installation
Replace your existing script with the downloaded version at:
/kometa_restore_backup_assets.py


Install Pillow, which is required for detecting corrupted images:
```bash
apt update && apt install -y python3-pil
```

Create the directory for preserving damaged artwork:
```bash
mkdir -p /media/backup/kometa-invalid-assets
```

Make sure /media/backup and /media/kometa-assets are mounted on the intended storage.
## Basic copy of assets to your working directory

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
Stop any active Kometa overlay processing first, then run:
```bash
python3 /kometa_restore_backup_assets_interactive.py --apply --replace-invalid --invalid-backup-dir /media/backup/kometa-invalid-assets --report /media/backup/kometa-assets-applied.csv
```

Your original poster backups in /opt/kometa-quickstart/config/overlays/ will remain untouched.

The script also retains the previous behavior when --replace-invalid is omitted, so you can still run it without repairing existing files.


## Optional Command - getting AMBIGUOUS DESTINATION in the log due to different versions of the same movie
When running with --apply, the script will display something like:
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
4. Review visually suspicious posters
The CSV will include suspected_blank_source and suspected_blank_existing records when the optional scan detects likely blank artwork. Those findings are for review; they do not trigger automatic replacement.

You can extract them with:
```bash
grep 'suspected_blank_' /media/backup/kometa-assets-applied.csv
```

The script retains /media/kometa-assets as its destination, preserves good existing artwork, and leaves Kometa's original backups untouched.