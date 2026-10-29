"""Archived DAT bytes for the original-data numerical regression baselines."""
import json
from gmss.core import ROOT


def original_dat(hour):
    manifest = json.loads((ROOT / 'data/backups/before_microgrid/manifest.json').read_text())
    return ROOT / next(row['backup'] for row in manifest['files']
                       if row['path'].endswith(f'/{hour}点sim2合并anony.dat'))
