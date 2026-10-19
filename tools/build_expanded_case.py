"""Build separate, connected research cases from copies of the original DAT."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gmss.expansion import build_expansion


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hour', choices=('13', '20', 'all'), default='all')
    parser.add_argument('--copies', type=int, default=5)
    parser.add_argument('--connection', choices=('ring', 'chain'), default='ring')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'data/synthetic')
    args = parser.parse_args()
    for hour in (('13', '20') if args.hour == 'all' else (args.hour,)):
        path = args.output_dir / f'sjz_{hour}_x{args.copies}_{args.connection}.dat'
        manifest = build_expansion(ROOT / f'raw_dataset/extracted/{hour}点sim2合并anony.dat',
                                   path, args.copies, args.connection)
        print(json.dumps({'file': str(path), 'buses': manifest['bus_count'],
                          'branches': manifest['branch_count'], 'ties': len(manifest['interties'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
