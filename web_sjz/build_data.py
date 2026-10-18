"""Rebuild the initial 13/20 snapshots using the same solver as live runs."""
from pathlib import Path
import json
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from gmss.core import run_case


def main():
    payload = {'scenarios':{},'layouts':{},'provenance':{}}
    for hour in ('13','20'):
        result=run_case(ROOT/f'raw_dataset/extracted/{hour}点sim2合并anony.dat',label=f'石家庄 {hour}:00',builtin=True)
        payload['scenarios'][hour]=result['scenario']
        payload['layouts'][hour]=result['layout']
        payload['provenance'][hour]=result['run']
        print(hour,result['run']['total_ms'],'ms',result['scenario']['summary'])
    (ROOT/'web_sjz/data.js').write_text('window.SJZ_DATA = '+json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False)+';\n')


if __name__=='__main__':
    main()
