"""Original-input invariants, live callbacks, and partition AC regressions."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from scipy.sparse import csr_matrix

from gmss.core import ROOT, run_case
from gmss.dat import newton


class CoreTests(unittest.TestCase):
    def test_original_dat_and_xls_bytes_match_cleanup_manifest(self):
        manifest=json.loads((ROOT/'doc/repository_cleanup.json').read_text())
        self.assertEqual(len(manifest['preserved_raw_sha256']),21)
        for name,expected in manifest['preserved_raw_sha256'].items():
            self.assertEqual(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(),expected,name)

    def test_classic_algorithms_agree_and_callbacks_contain_actual_voltages(self):
        buses=[{'subtype':'S','vm':1.,'va':0.,'p':0.,'q':0.},
               {'subtype':' ','vm':1.,'va':0.,'p':-80.,'q':-30.}]
        y=csr_matrix([[-10j,10j],[10j,-10j]])
        result=[]
        for damped in (False,True):
            events=[]
            v,history=newton(100,buses,y,observer=lambda row,v:events.append((row,v)),damped=damped)
            self.assertEqual(len(events),len(history))
            self.assertEqual([r['iteration'] for r,_ in events],list(range(len(history))))
            self.assertGreater(history[0],history[-1])
            np.testing.assert_allclose(events[-1][1],v)
            result.append(v)
        np.testing.assert_allclose(*result,atol=1e-10)
        with self.assertRaisesRegex(RuntimeError,'did not converge'):
            newton(100,buses,y,max_iterations=0)

    def test_all_algorithms_preserve_original_overloads_and_verify_partitions(self):
        for hour,expected in (('13',397.89571637),('20',489.25963521)):
            source=ROOT/f'raw_dataset/extracted/{hour}点sim2合并anony.dat'
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            for algorithm in ('nr','damped-nr','bpa-style'):
                events=[]
                result=run_case(source,algorithm,lambda kind,data:events.append((kind,data)),builtin=True)
                scene=result['scenario']; run=result['run']; lookup={b['id']:b for b in scene['buses']}
                target=next(t for t in scene['trafos'] if lookup[t['from']]['name']=='GXVS2' and lookup[t['to']]['name']=='GXVY2')
                self.assertAlmostEqual(target['loading'],expected,places=5)
                self.assertEqual(len(scene['trafos']),91)
                self.assertTrue(run['converged']);self.assertTrue(run['source_unchanged'])
                self.assertEqual(run['source_sha256'],digest)
                self.assertEqual(run['partition_failed'],0)
                self.assertLess(run['residual_mva'],1e-4)
                self.assertLessEqual(run['solver_ms'],run['total_ms'])
                self.assertTrue(any(kind=='iteration' for kind,_ in events))
                self.assertTrue(any(kind=='partition' for kind,_ in events))
                membership=[i for p in scene['partitions'] for i in p['internal_buses']]
                self.assertEqual(len(membership),len(set(membership)))
                self.assertEqual(set(membership),{b['id'] for b in scene['buses'] if b['kv']<35})
                self.assertLess(scene['summary']['max_vm_error'],1e-5)
                json.dumps(result,allow_nan=False)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),digest)

    def test_identical_imported_case_keeps_the_same_model_and_layout_ids(self):
        source=ROOT/'raw_dataset/extracted/20点sim2合并anony.dat'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'custom.dat';path.write_bytes(source.read_bytes())
            result=run_case(path,builtin=False)
            self.assertEqual(result['run']['source_sha256'],hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(set(result['layout']),{str(b['id']) for b in result['scenario']['buses']})

    def test_custom_case_without_known_station_names_is_supported(self):
        def card(kind, fields):
            row=list(' '*90)
            row[:len(kind)]=kind
            for start,stop,value in fields:
                row[start-1:stop]=str(value).rjust(stop-start+1)
            return ''.join(row).rstrip()
        rows=[card('BS',[(7,14,'SOURCE'),(15,18,'10.'),(58,61,'1.02')]),
              card('B ',[(7,14,'LOAD'),(15,18,'10.'),(21,25,'1.'),(26,30,'.2')]),
              card('L ',[(7,14,'SOURCE'),(15,18,'10.'),(20,27,'LOAD'),(28,31,'10.'),
                         (34,37,'100.'),(39,44,'.01'),(45,50,'.1')])]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'custom.dat'
            path.write_text('\n'.join(['(POWERFLOW)','/MVA_BASE=100\\','/NETWORK_DATA\\',
                                       *rows,'/SOLUTION\\','>SOL_ITER,DECOUPLED=2,NEWTON=15<','(END)'])+'\n',encoding='gbk')
            for algorithm in ('nr','damped-nr','bpa-style'):
                result=run_case(path,algorithm,builtin=False)
                self.assertTrue(result['run']['converged'])
                self.assertEqual(result['scenario']['bus_count'],2)
                self.assertEqual(result['scenario']['microgrids'],[])
                self.assertEqual(result['run']['partition_failed'],0)
                self.assertEqual(set(result['layout']),{'1','2'})


if __name__=='__main__':unittest.main()
