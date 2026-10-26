import copy

import pytest

from fsf.production.contracts import ProductionError,digest,seal
from fsf.production.quality_rollup import quality_rollup


def fixture():
    checks=[]
    for scope,spans in [('segment',[(n,n+4320) for n in range(0,172800,4320)]),
                        ('chapter',[(n,n+17280) for n in range(0,172800,17280)]),
                        ('seam',[(n-1,n+1) for n in range(4320,172800,4320)]),
                        ('film',[(0,172800)])]:
        for i,(start,end) in enumerate(spans):
            checks.append(dict(check_id=f'{scope}_{i}',scope=scope,start_frame=start,end_frame=end,
                               artifact_sha256='a'*64,evaluator_digest='e'*64))
    manifest=dict(plan_digest='b'*64,policy_digest='c'*64,frame_count=172800,checks=checks)
    reports=[seal(dict(check_id=c['check_id'],check_digest=digest(c),plan_digest=manifest['plan_digest'],
                       policy_digest=manifest['policy_digest'],artifact_sha256=c['artifact_sha256'],
                       evaluator_digest=c['evaluator_digest'],verdict='accept'),'report_digest') for c in checks]
    return manifest,reports


def test_120_minute_hierarchical_coverage_is_not_calibration_or_human_review():
    m,r=fixture();result=quality_rollup(m,r)
    assert result['machine_accepted']
    assert result['calibration_status']=='not_attested'
    assert result['human_review']=='not_reviewed'


@pytest.mark.parametrize('scope',['segment','seam','chapter','film'])
@pytest.mark.parametrize('failure',['missing','reject','unknown'])
def test_local_failure_cannot_be_hidden_by_full_film_accept(scope,failure):
    m,r=fixture();index=next(i for i,x in enumerate(r) if x['check_id'].startswith(scope+'_'))
    if failure=='missing':r.pop(index)
    else:r[index]=seal({**r[index],'verdict':failure},'report_digest')
    assert not quality_rollup(m,r)['machine_accepted']


@pytest.mark.parametrize('mutation',[
    lambda m,r:m['checks'][0].update(end_frame=4319),
    lambda m,r:m['checks'].extend([copy.deepcopy(m['checks'][0])]),
    lambda m,r:m['checks'].__setitem__(0,{**m['checks'][0],'start_frame':True}),
    lambda m,r:m.update(checks=[c for c in m['checks'] if c['scope']!='seam']),
    lambda m,r:r.__setitem__(0,seal({**r[0],'artifact_sha256':'d'*64},'report_digest')),
    lambda m,r:r.__setitem__(0,seal({**r[0],'policy_digest':'d'*64},'report_digest')),
    lambda m,r:r.append(copy.deepcopy(r[0])),
])
def test_gaps_duplicate_checks_and_stale_bindings_are_rejected(mutation):
    m,r=fixture();mutation(m,r)
    with pytest.raises(ProductionError):quality_rollup(m,r)
