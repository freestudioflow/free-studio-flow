"""Exact hierarchical quality coverage; no averaging away a failed window.

The caller pins required checks before evaluation. These records do not attest
that an evaluator was calibrated or that a person reviewed the film.
"""
from __future__ import annotations

import re

from fsf.production.contracts import ProductionError, digest, integer, key, seal


def _sha(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
        raise ProductionError('PRODUCTION_QUALITY_BINDING_INVALID')
    return value


def quality_rollup(manifest, reports):
    """Require one exact bound report for every segment/seam/chapter/film check."""
    try:
        return _rollup(manifest,reports)
    except ProductionError:
        raise
    except (KeyError,TypeError,ValueError,RecursionError):
        raise ProductionError('PRODUCTION_QUALITY_COVERAGE_INVALID') from None


def _rollup(manifest,reports):
    if not isinstance(manifest,dict) or set(manifest)!={'plan_digest','policy_digest','frame_count','checks'}:
        raise ProductionError('PRODUCTION_QUALITY_COVERAGE_INVALID')
    _sha(manifest['plan_digest']);_sha(manifest['policy_digest'])
    integer(manifest['frame_count'],1,172800)
    if not isinstance(manifest['checks'],list) or not 1<=len(manifest['checks'])<=100000:
        raise ProductionError('PRODUCTION_QUALITY_COVERAGE_INVALID')
    checks={};scopes={scope:[] for scope in ('segment','seam','chapter','film')}
    for check in manifest['checks']:
        if not isinstance(check,dict) or set(check)!={'check_id','scope','start_frame','end_frame','artifact_sha256','evaluator_digest'}:
            raise ProductionError('PRODUCTION_QUALITY_COVERAGE_INVALID')
        identifier=key(check['check_id'])
        if identifier in checks or check['scope'] not in scopes:
            raise ProductionError('PRODUCTION_QUALITY_COVERAGE_INVALID')
        integer(check['start_frame'],0,manifest['frame_count']-1)
        integer(check['end_frame'],check['start_frame']+1,manifest['frame_count'])
        _sha(check['artifact_sha256']);_sha(check['evaluator_digest'])
        checks[identifier]=check;scopes[check['scope']].append(check)
    # Partition coverage is independent of the number of evaluator windows.
    for scope in ('segment','chapter','film'):
        cursor=0
        for check in sorted(scopes[scope],key=lambda x:x['start_frame']):
            if check['start_frame']!=cursor:
                raise ProductionError('PRODUCTION_QUALITY_COVERAGE_INVALID')
            cursor=check['end_frame']
        if cursor!=manifest['frame_count'] or (scope=='film' and len(scopes[scope])!=1):
            raise ProductionError('PRODUCTION_QUALITY_COVERAGE_INVALID')
    boundaries={c['end_frame'] for scope in ('segment','chapter') for c in scopes[scope] if c['end_frame']<manifest['frame_count']}
    if any(not any(c['start_frame']<b<c['end_frame'] for c in scopes['seam']) for b in boundaries):
        raise ProductionError('PRODUCTION_QUALITY_COVERAGE_INVALID')
    if not isinstance(reports,list) or len(reports)>len(checks):
        raise ProductionError('PRODUCTION_QUALITY_COVERAGE_INVALID')
    observed={}
    for report in reports:
        if not isinstance(report,dict) or set(report)!={'check_id','check_digest','plan_digest','policy_digest','artifact_sha256','evaluator_digest','verdict','report_digest'}:
            raise ProductionError('PRODUCTION_QUALITY_BINDING_INVALID')
        cid=key(report['check_id'])
        if cid not in checks or cid in observed or seal(report,'report_digest')!=report:
            raise ProductionError('PRODUCTION_QUALITY_BINDING_INVALID')
        if report['check_digest']!=digest(checks[cid]):
            raise ProductionError('PRODUCTION_QUALITY_BINDING_INVALID')
        for name in ('plan_digest','policy_digest'):
            if report[name]!=manifest[name]:raise ProductionError('PRODUCTION_QUALITY_BINDING_INVALID')
        for name in ('artifact_sha256','evaluator_digest'):
            if report[name]!=checks[cid][name]:raise ProductionError('PRODUCTION_QUALITY_BINDING_INVALID')
        if report['verdict'] not in ('accept','reject','unknown'):
            raise ProductionError('PRODUCTION_QUALITY_BINDING_INVALID')
        observed[cid]=report
    missing=sorted(set(checks)-set(observed))
    rejected=sorted(cid for cid,r in observed.items() if r['verdict']=='reject')
    unknown=sorted(cid for cid,r in observed.items() if r['verdict']=='unknown')
    return seal({'schema_version':'fsf_quality_rollup_v1','manifest':manifest,
        'report_digests':{cid:r['report_digest'] for cid,r in sorted(observed.items())},
        'missing':missing,'rejected':rejected,'unknown':unknown,
        'machine_accepted':not (missing or rejected or unknown),
        'calibration_status':'not_attested','human_review':'not_reviewed'},'rollup_digest')
