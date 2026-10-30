import json

import pytest
from streamlit.testing.v1 import AppTest

from apronops.experiments.engine import Simulation
from apronops.experiments.scenario import TaskSpec
from apronops.experiments.study import Study, Condition, generate, aggregate, save_report, METRICS
from apronops import policies


def spec():
    return Study(id='test',seeds=[1,2],conditions=[Condition(id='base',label='基准')])


def test_study_streams_preserve_shared_inputs_and_vary_real_seeds():
    s=spec();base=s.conditions[0]
    one=generate(s,base,1)
    assert one==generate(s,base,1)
    assert one.aircraft!=generate(s,base,2).aircraft
    heavy=generate(s,base.model_copy(update={'flight_count':3,'fault_count':3}),1)
    assert heavy.flights[:1]==one.flights and heavy.aircraft[:1]==one.aircraft
    assert heavy.faults[:1]==one.faults
    assert len({Simulation(one,1,p).input_hash for p in s.policies})==1
    with pytest.raises(ValueError):
        Condition(id='bad',label='bad',fleet_copies=2,depot_capacity=1)


def records(s):
    return [dict(condition='base',label='基准',seed=seed,policy=p,status='completed',input_sha256=str(seed),
            **{m:(1 if p=='priority_distance' else 2)*seed for m in METRICS})
            for seed in s.seeds for p in s.policies]


def test_statistics_use_only_complete_hash_matched_pairs_and_paired_deltas():
    s=spec();rows=records(s)
    report=aggregate(s,rows)
    value=next(g for g in report['paired_deltas'] if g['policy']=='rolling_optimization')
    assert value['completion_rate_mean']==1.5
    assert value['completion_rate_sd']==pytest.approx(2**-.5)
    rows[-1]['input_sha256']='mismatched'
    report=aggregate(s,rows)
    assert all(g['paired_seeds']==1 for g in report['groups'])
    assert report['excluded_pairs'][0]['reason']=='input hash mismatch'
    rows[0]['status']='failed'
    report=aggregate(s,rows)
    assert all(g['completion_rate_mean'] is None for g in report['groups'])
    assert report['failed_runs']==1


def test_large_fleet_does_not_exclude_all_compatible_vehicles():
    s=spec();condition=Condition(id='large',label='large',fleet_copies=2)
    scene=generate(s,condition,1).model_copy(update={'flights':[],'aircraft':[], 'faults':[],
        'tasks':[TaskSpec(id='B',type='baggage_tow',origin='DEPOT_BAG_W',destination='SP_01',deadline_s=1000)]})
    sim=Simulation(scene,1,'rolling_optimization')
    pairs=list(policies.assignments(sim.world))
    assert len(pairs)==1 and pairs[0][1].type.value=='tractor'


def test_study_dashboard_reads_reports_without_live_api(tmp_path,monkeypatch):
    from pathlib import Path
    import requests
    root=tmp_path/'batch';root.mkdir()
    s=spec();save_report(root,s,records(s))
    monkeypatch.setenv('APRONOPS_RESULTS_DIR',str(tmp_path))
    def offline(*args,**kwargs):raise requests.ConnectionError('offline')
    monkeypatch.setattr(requests,'get',offline)
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'apronops/dashboard/app.py'),default_timeout=20).run()
    app.sidebar.radio[0].set_value('实验对比').run()
    assert not app.exception and not app.error
    assert len(app.get('plotly_chart'))==1
    next(r for r in app.radio if r.label=='统计视角').set_value('相对最近车辆策略的配对差值').run()
    assert not app.exception and not app.error
