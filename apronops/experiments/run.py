"""python -m apronops.experiments.run --scenario ... --seeds 1 2 --output ..."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import platform
import subprocess
from time import perf_counter

from ..policies import POLICIES, POLICY_VERSION
from .. import simulator
from .engine import Simulation, digest
from .scenario import read_scenario

ROOT = Path(__file__).resolve().parents[2]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False), encoding='utf-8')


def provenance():
    h = hashlib.sha256()
    for path in sorted((ROOT/'apronops').rglob('*.py')):
        h.update(str(path.relative_to(ROOT)).encode())
        h.update(path.read_bytes())
    try:
        commit = subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(['git','status','--porcelain'], cwd=ROOT, text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    import pydantic, yaml
    return {'source_sha256': h.hexdigest(), 'git_commit': commit, 'git_dirty': dirty,
            'python': platform.python_version(), 'pydantic': pydantic.__version__, 'pyyaml': yaml.__version__}


def layout_payload(layout):
    return {'metadata': layout.metadata, 'reference': layout.reference,
            'nodes': [vars(n) for n in layout.nodes.values()],
            'edges': [{'from': a, 'to': b, 'distance_m': d, **layout.edge_attributes.get((a,b),{})}
                      for a, neighbors in layout.adjacency.items() for b,d,_ in neighbors]}


def run_one(scenario, seed, policy, output, data_dir=simulator.DATA_DIR):
    sim = Simulation(scenario, seed, policy, data_dir)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)  # Never overwrite another experiment.
    metadata = {'schema_version': 1, 'scenario_id': scenario.id, 'seed': seed, 'policy': policy,
                'policy_version': POLICY_VERSION, 'scenario': scenario.model_dump(mode='json'),
                'input_sha256': sim.input_hash, 'provenance': provenance(), 'status': 'running'}
    write_json(output/'manifest.json', metadata)
    write_json(output/'inputs.json', {'events': sim.events, 'initial_world': sim.initial_world})
    layout = layout_payload(sim.world.layout)
    write_json(output/'layout.json', layout)
    frames = []
    status, error = 'completed', None
    started = perf_counter()
    next_sample = 0.
    with (output/'events.jsonl').open('w', encoding='utf-8') as event_file, (output/'frames.jsonl').open('w', encoding='utf-8') as frame_file:
        def capture():
            nonlocal next_sample
            for event in sim.drain_events():
                event_file.write(json.dumps(event, ensure_ascii=False, sort_keys=True)+'\n')
            if sim.elapsed_s+1e-7 >= next_sample or sim.elapsed_s >= scenario.horizon_s:
                frame = sim.observe()
                if not frames or frame['elapsed_s'] != frames[-1]['elapsed_s']:
                    frames.append(frame)
                    frame_file.write(json.dumps(frame, ensure_ascii=False, sort_keys=True)+'\n')
                next_sample += scenario.sample_s
        try:
            capture()
            while sim.elapsed_s < scenario.horizon_s-1e-7:
                sim.step()
                capture()
        except KeyboardInterrupt:
            status = 'interrupted'
        except Exception as exc:
            status, error = 'failed', repr(exc)
        finally:
            capture()
            if not frames or frames[-1]['elapsed_s'] != sim.elapsed_s:
                frame = sim.observe()
                frames.append(frame)
                frame_file.write(json.dumps(frame, ensure_ascii=False, sort_keys=True)+'\n')
    summary = sim.summary(status)
    write_json(output/'summary.json', summary)
    performance = {'wall_time_s': perf_counter()-started, 'policy_timing': sim.world.policy_timing}
    write_json(output/'performance.json', performance)
    with (output/'tasks.csv').open('w', encoding='utf-8', newline='') as f:
        fields = ['id','flight_id','type','status','release_at','deadline','started_at','completed_at','assigned_vehicle_id']
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for task in sim.world.tasks.values():
            writer.writerow({k: task.model_dump(mode='json')[k] for k in fields})
    metadata.update(status=status, error=error,
                    events_sha256=hashlib.sha256((output/'events.jsonl').read_bytes()).hexdigest(),
                    frames_sha256=hashlib.sha256((output/'frames.jsonl').read_bytes()).hexdigest())
    write_json(output/'manifest.json', metadata)
    write_json(output/'replay.json', {'schema_version': 1, 'manifest': metadata, 'layout': layout, 'summary': summary, 'frames': frames})
    if error:
        raise RuntimeError(f'Experiment failed; partial artifacts saved at {output}: {error}')
    if status == 'interrupted':
        raise KeyboardInterrupt
    return {**summary, 'scenario': scenario.id, 'seed': seed, 'policy': policy, 'input_sha256': sim.input_hash}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', required=True)
    parser.add_argument('--seeds', nargs='+', type=int, default=[1,2,3])
    parser.add_argument('--policies', nargs='+', choices=list(POLICIES), default=list(POLICIES))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    scenario = read_scenario(args.scenario)
    root = Path(args.output)
    rows = []
    try:
        root.mkdir(parents=True, exist_ok=False)
        for seed in dict.fromkeys(args.seeds):
            for policy in dict.fromkeys(args.policies):
                dest = root/f'{scenario.id}-{policy}-seed{seed}'
                row = run_one(scenario, seed, policy, dest)
                rows.append(row)
                print(f"{dest.name}: {row['completed_tasks']}/{row['released_tasks']} completed; violations={row['conflict_zone_violation_steps']}", flush=True)
                with (root/'comparison.csv').open('w', newline='', encoding='utf-8') as file:
                    writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
        # Paired seeds are checked explicitly, not silently pooled.
        for seed in args.seeds:
            if len({r['input_sha256'] for r in rows if r['seed'] == seed}) != 1:
                raise RuntimeError(f'Policies received different inputs for seed {seed}')
    except KeyboardInterrupt:
        parser.exit(130, 'Interrupted: partial run artifacts retained.\n')
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, str(exc)+'\n')


if __name__ == '__main__':
    main()
