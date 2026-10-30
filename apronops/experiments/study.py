"""Paired sensitivity study: generate frozen scenarios, run, aggregate and replay.

python -m apronops.experiments.study --config configs/studies/air_ground.yaml --output results/experiments/air-ground-study-001
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import random
import statistics

from pydantic import Field, model_validator
import yaml

from .. import simulator
from ..policies import POLICIES
from .scenario import Spec, Scenario
from .run import run_one, write_json, provenance


class Condition(Spec):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]+$')
    label: str
    flight_count: int = Field(default=1, ge=1, le=20)
    fleet_copies: int = Field(default=1, ge=1, le=4)
    depot_capacity: int = Field(default=2, ge=1, le=10)
    fault_count: int = Field(default=1, ge=0, le=10)

    @model_validator(mode='after')
    def fits(self):
        if self.fleet_copies>self.depot_capacity:
            raise ValueError('initial fleet exceeds depot capacity')
        return self


class Study(Spec):
    schema_version: int = Field(default=1, ge=1, le=1)
    id: str = Field(pattern=r'^[A-Za-z0-9_-]+$')
    horizon_s: float = Field(default=4800, ge=1200, le=86400)
    sample_s: float = Field(default=10, ge=.5)
    seeds: list[int] = Field(default_factory=lambda:[1,2,3], min_length=1)
    policies: list[str] = Field(default_factory=lambda:list(POLICIES), min_length=1)
    conditions: list[Condition] = Field(min_length=1)

    @model_validator(mode='after')
    def unique(self):
        for values in (self.seeds,self.policies,[c.id for c in self.conditions]):
            if len(values)!=len(set(values)):
                raise ValueError('study IDs, seeds and policies must be unique')
        if any(p not in POLICIES for p in self.policies):
            raise ValueError('unknown policy')
        if 'priority_distance' not in self.policies:
            raise ValueError('paired study requires priority_distance baseline')
        return self


def _rng(seed, stream):
    return random.Random(int(hashlib.sha256(f'{seed}:{stream}'.encode()).hexdigest(),16))


def generate(spec, condition, seed):
    """Independent random streams keep common flights/faults paired across factors."""
    flights=[];planes=[]
    stands=['SP_07','SP_20','SP_01','SP_25','SP_09','SP_18']
    for i in range(condition.flight_count):
        rng=_rng(seed,f'flight-{i}')
        start=round(15+i*180+rng.uniform(0,35),3)
        # Keep planned deadlines inside the observation period, including under load.
        if start+180>=spec.horizon_s:
            raise ValueError('too many flights for observation horizon')
        fid=f'F_{i+1:02d}'
        flights.append(dict(id=fid,stand_id=stands[i%len(stands)],announce_s=0,
            arrival_s=start+100,turnaround_s=min(2400,spec.horizon_s-start-150)))
        planes.append(dict(id=f'AC_{i+1:02d}',flight_id=fid,taxi_start_s=start,speed_mps=4,length_m=12))
    raw=yaml.safe_load((simulator.DATA_DIR/'vehicles_seed.yaml').read_text())['vehicles']
    fleet=[]
    for copy in range(condition.fleet_copies):
        for v in raw:
            fleet.append({**v,'id':v['id'] if copy==0 else v['id']+f'_EXTRA{copy}'})
    depots=yaml.safe_load((simulator.DATA_DIR/'depots.yaml').read_text())['depots']
    depots={key:{**value,'capacity':condition.depot_capacity} for key,value in depots.items()}
    faults=[]
    # Non-overlapping time slots; equal prefixes preserve identical disturbances.
    slot=(spec.horizon_s-200)/10
    for i in range(condition.fault_count):
        rng=_rng(seed,f'fault-{i}')
        start=round(100+i*slot+rng.uniform(0,slot*.2),3)
        duration=round(rng.uniform(30,min(90,slot*.5)),3)
        faults.append(dict(id=f'FAULT_{i+1:02d}',start_s=start,end_s=start+duration,
            vehicle_id=raw[i%len(raw)]['id'],anomaly_type='position_quality_degraded' if i%2 else 'communication_lost'))
    return Scenario(id=condition.id,description=condition.label,horizon_s=spec.horizon_s,sample_s=spec.sample_s,
                    flights=flights,aircraft=planes,vehicles=fleet,depots=depots,faults=faults)


METRICS=('completion_rate','due_on_time_rate','unfinished_tasks','tardiness_lower_bound_s',
         'route_wait_s','depot_wait_s','deadlocks_unresolved','distance_m','empty_distance_m',
         'aircraft_departed','aircraft_departure_delay_lower_bound_s','aircraft_wait_s',
         'conflict_zone_violation_steps','depot_capacity_violation_steps')


def aggregate(spec, runs):
    """Only fully paired, hash-matched seeds enter summary or paired differences."""
    groups=[];deltas=[];excluded=[]
    for condition in spec.conditions:
        by_seed={}
        for seed in spec.seeds:
            rows=[r for r in runs if r['condition']==condition.id and r['seed']==seed and r['status']=='completed']
            if {r['policy'] for r in rows}!=set(spec.policies) or len(rows)!=len(spec.policies):
                excluded.append({'condition':condition.id,'seed':seed,'reason':'incomplete policy set'});continue
            if len({r['input_sha256'] for r in rows})!=1:
                excluded.append({'condition':condition.id,'seed':seed,'reason':'input hash mismatch'});continue
            by_seed[seed]={r['policy']:r for r in rows}
        for policy in spec.policies:
            group={'condition':condition.id,'label':condition.label,'policy':policy,'paired_seeds':len(by_seed),
                   'expected_seeds':len(spec.seeds)}
            delta={**group,'baseline':'priority_distance'}
            for metric in METRICS:
                values=[p[policy][metric] for p in by_seed.values() if p[policy].get(metric) is not None]
                differences=[p[policy][metric]-p['priority_distance'][metric] for p in by_seed.values()
                             if p[policy].get(metric) is not None and p['priority_distance'].get(metric) is not None]
                for target,nums in ((group,values),(delta,differences)):
                    target[metric+'_n']=len(nums)
                    target[metric+'_mean']=statistics.mean(nums) if nums else None
                    target[metric+'_sd']=statistics.stdev(nums) if len(nums)>1 else None
                    target[metric+'_min']=min(nums) if nums else None
                    target[metric+'_max']=max(nums) if nums else None
            groups.append(group);deltas.append(delta)
    return dict(schema_version=1,study_id=spec.id,groups=groups,paired_deltas=deltas,excluded_pairs=excluded,
                planned_runs=len(spec.conditions)*len(spec.seeds)*len(spec.policies),
                completed_runs=sum(r['status']=='completed' for r in runs),
                failed_runs=sum(r['status']=='failed' for r in runs),
                interrupted_runs=sum(r['status']=='interrupted' for r in runs),
                runs=runs)


def save_report(root, spec, runs):
    report=aggregate(spec,runs)
    write_json(root/'study-report.json',report)
    for name,rows in [('study-summary.csv',report['groups']),('paired-deltas.csv',report['paired_deltas'])]:
        with (root/name).open('w',encoding='utf-8',newline='') as file:
            writer=csv.DictWriter(file,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    return report


def run_study(spec, root):
    root=Path(root)
    # Validate every generated reference/resource before creating the output tree.
    from .engine import Simulation
    scenarios={(c.id,s):generate(spec,c,s) for c in spec.conditions for s in spec.seeds}
    for scene in scenarios.values():
        Simulation(scene,spec.seeds[0],spec.policies[0])
    root.mkdir(parents=True,exist_ok=False)
    (root/'scenarios').mkdir()
    meta={'spec':spec.model_dump(mode='json'),'provenance':provenance(),'status':'running'}
    write_json(root/'study.json',meta)
    runs=[];save_report(root,spec,runs)
    try:
        for c in spec.conditions:
            for seed in spec.seeds:
                scene=scenarios[c.id,seed]
                (root/'scenarios'/f'{c.id}-seed{seed}.yaml').write_text(yaml.safe_dump(scene.model_dump(mode='json'),allow_unicode=True,sort_keys=False),encoding='utf-8')
                for policy in spec.policies:
                    dest=root/f'{c.id}-{policy}-seed{seed}'
                    row={'condition':c.id,'label':c.label,'seed':seed,'policy':policy,'run_dir':dest.name}
                    try:
                        row.update(run_one(scene,seed,policy,dest))
                    except (RuntimeError,ValueError,OSError) as error:
                        row.update(status='failed',error=str(error))
                    except KeyboardInterrupt:
                        row.update(status='interrupted');runs.append(row);save_report(root,spec,runs);raise
                    runs.append(row)
                    save_report(root,spec,runs)
                    print(f"{dest.name}: {row['status']}; tasks={row.get('completed_tasks','?')}/{row.get('released_tasks','?')}; aircraft={row.get('aircraft_departed','?')}/{row.get('aircraft_total','?')}",flush=True)
        meta['status']='failed' if any(r['status']=='failed' for r in runs) else 'completed'
    except KeyboardInterrupt:
        meta['status']='interrupted';raise
    finally:
        write_json(root/'study.json',meta)
    return save_report(root,spec,runs)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        spec=Study.model_validate(yaml.safe_load(Path(args.config).read_text(encoding='utf-8')))
        report=run_study(spec,args.output)
        if report['failed_runs'] or report['excluded_pairs']:
            parser.exit(1,'Study contains failed or unmatched runs; see study-report.json\n')
    except KeyboardInterrupt:
        parser.exit(130,'Study interrupted; partial artifacts retained\n')
    except (ValueError,OSError) as error:
        parser.exit(1,str(error)+'\n')


if __name__=='__main__':
    main()
