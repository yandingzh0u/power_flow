"""Export original Shijiazhuang DAT power-flow results without editing inputs."""
from pathlib import Path
import argparse
import json
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from gmss.raw_solver import solve


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw-dir',type=Path,default=ROOT/'raw_dataset/extracted')
    parser.add_argument('--algorithm',choices=('nr','damped-nr','bpa-style','partitioned-nr'),default='partitioned-nr')
    parser.add_argument('--output-dir',type=Path,default=ROOT/'runtime/exports')
    parser.add_argument('--transformer-base',choices=('100','system'),default='100',
                        help='T-card impedance base: user-selected 100 MVA assumption (default), or declared system base')
    args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    results=[]
    for hour in ('13','20'):
        result=solve(args.raw_dir/f'{hour}点sim2合并anony.dat',args.output_dir,args.algorithm,
                     transformer_base_mva=100.0 if args.transformer_base=='100' else None)
        results.append(result)
        print(hour,'converged=',result['converged'],'iterations=',result['iterations'],
              'residual_MVA=',result['original_bus_equation_residual_mva'])
    (args.output_dir/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


if __name__=='__main__':main()
