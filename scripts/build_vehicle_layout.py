"""Generate an independent schematic service-road network around Layout H.

Run from any directory: python scripts/build_vehicle_layout.py
Geometry assumptions and operational limits: docs/vehicle-roads.md.
"""
from pathlib import Path
import hashlib
import math
import yaml

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / 'data/aircraft_layout_h.yaml'


def intersection(a, b, c, d):
    """Point intersection of nonparallel closed line segments."""
    ux, uy = b[0] - a[0], b[1] - a[1]
    vx, vy = d[0] - c[0], d[1] - c[1]
    det = ux * vy - uy * vx
    if abs(det) < 1e-9:
        return None
    wx, wy = c[0] - a[0], c[1] - a[1]
    t, s = (wx * vy - wy * vx) / det, (wx * uy - wy * ux) / det
    if -1e-8 <= t <= 1 + 1e-8 and -1e-8 <= s <= 1 + 1e-8:
        return (round(a[0] + t * ux, 5), round(a[1] + t * uy, 5))
    return None


def on_segment(p, a, b):
    return abs(math.dist(a, p) + math.dist(p, b) - math.dist(a, b)) < 1e-5


def build():
    aircraft = yaml.safe_load(REFERENCE.read_text())
    ref = {n['id']: n for n in aircraft['nodes']}
    named, segments = {}, []

    def point(x, y):
        return (round(x, 5), round(y, 5))

    def mark(id_, label, kind, xy):
        named[xy] = dict(id=id_, label=label, type=kind, x=xy[0], y=xy[1])

    def line(points, kind):
        for a, b in zip(points, points[1:]):
            if math.dist(a, b) > 1e-5:
                segments.append((a, b, kind))

    # Outer ring lies outside the aircraft reference network. Cross-apron trips
    # use the north link; there is no vehicle shortcut down a taxiway.
    line([(-145., -40.), (145., -40.), (145., 245.), (-145., 245.), (-145., -40.)], 'perimeter')
    for x in (-78., 78.):
        line([(x, 245.), (x, 40.)], 'service')
    # Stagger the centre service spine around the two inward-facing stands.
    line([(8., 245.), (8., 135.), (-8., 135.), (-8., 105.), (0., 95.), (0., 40.)], 'service')

    west_outer = {0, 1, 2, 3, 4, 26}
    west_inner = {6, 7, 8, 9, 11}
    east_outer = {22, 23, 24, 25, 29}
    east_inner = {15, 17, 19, 20, 21, 28}
    for i in range(30):
        stand, front = ref[str(i)], ref[str(2000 + i)]
        dx, dy = stand['x'] - front['x'], stand['y'] - front['y']
        length = math.hypot(dx, dy)
        sp = point(stand['x'] + 6 * dx / length, stand['y'] + 6 * dy / length)
        mark(f'SP_{i + 1:02d}', f'机位 {i + 1:02d} 服务点', 'service_point', sp)
        if i in west_outer:
            points = [sp, (-145., sp[1])]
        elif i in east_outer:
            points = [sp, (145., sp[1])]
        elif i == 11:
            points = [sp, (sp[0], 200.), (-78., 200.)]
        elif i == 17:
            points = [sp, (sp[0], 205.), (78., 205.)]
        elif i in west_inner:
            points = [sp, (-78., sp[1])]
        elif i in east_inner:
            points = [sp, (78., sp[1])]
        elif i in {12, 13, 14}:
            points = [sp, (sp[0], 245.)]
        elif i == 10:
            points = [sp, (8., sp[1])]
        elif i == 16:
            points = [sp, (-8., sp[1])]
        else:
            points = [sp, (0., sp[1])]
        line(points, 'stand_access')

    # Dedicated home bays, off the through-road; no idle vehicle occupies a stand.
    for id_, label, xy, attach in [
        ('DEPOT_BAG_W', '西区行李基地', (-125., -58.), (-125., -40.)),
        ('DEPOT_BAG_E', '东区行李基地', (100., -58.), (100., -40.)),
        ('DEPOT_BUS', '摆渡车基地', (-65., -58.), (-65., -40.)),
        ('DEPOT_CATER', '配餐基地', (55., -58.), (55., -40.)),
        ('DEPOT_AMR', '巡检充电基地', (-165., 220.), (-145., 220.)),
        ('CHECK_N', '北区巡检点', (110., 260.), (110., 245.)),
    ]:
        mark(id_, label, 'checkpoint' if id_.startswith('CHECK') else 'depot', xy)
        line([xy, attach], 'stand_access')

    cuts = [{a, b} for a, b, _ in segments]
    # All vehicle-road intersections share a graph vertex, including T junctions.
    for i, (a, b, _) in enumerate(segments):
        for j, (c, d, _) in enumerate(segments[:i]):
            p = intersection(a, b, c, d)
            if p is not None:
                cuts[i].add(p)
                cuts[j].add(p)
            for endpoint in (a, b, c, d):
                if on_segment(endpoint, a, b) and on_segment(endpoint, c, d):
                    cuts[i].add(endpoint)
                    cuts[j].add(endpoint)

    crossings = {}
    for i, (a, b, _) in enumerate(segments):
        for edge in aircraft['edges']:
            c, d = ref[edge['from']], ref[edge['to']]
            p = intersection(a, b, (c['x'], c['y']), (d['x'], d['y']))
            if p is not None:
                cuts[i].add(p)
                crossings.setdefault(p, set()).add(f"{edge['from']}-{edge['to']}")
    for i, p in enumerate(sorted(crossings), 1):
        mark(f'X_{i:02d}', f'滑行道交叉点 {i:02d}', 'crossing', p)
        named[p]['aircraft_edges'] = sorted(crossings[p])

    coords = sorted(set().union(*cuts))
    for i, xy in enumerate(coords, 1):
        if xy not in named:
            mark(f'R_{i:03d}', '车辆道路节点', 'road', xy)
    speeds = {'perimeter': 20 / 3.6, 'service': 12 / 3.6, 'stand_access': 5 / 3.6}
    edges, seen = [], set()
    for (a, b, kind), split in zip(segments, cuts):
        ordered = sorted(split, key=lambda p: math.dist(a, p))
        for p, q in zip(ordered, ordered[1:]):
            key = frozenset((p, q))
            if key in seen or math.dist(p, q) < 1e-5:
                continue
            seen.add(key)
            distance = round(math.dist(p, q), 5)
            edges.append({'from': named[p]['id'], 'to': named[q]['id'], 'road_type': kind,
                          'distance_m': distance, 'speed_limit_mps': speeds[kind],
                          'base_travel_s': round(distance / speeds[kind], 5)})
    return dict(name='Layout H 独立车辆道路 · 推演 v1', version='vehicle-h-v1',
                coordinate_unit='m', reference_file=REFERENCE.name,
                reference_sha256=hashlib.sha256(REFERENCE.read_bytes()).hexdigest(),
                design_note='保留 Layout H 机位；外围环路、分区服务路、机位短支路。尺寸与穿越规则均为仿真假设。',
                crossing_wait_s=3.0, nodes=[named[p] for p in coords], edges=edges)


if __name__ == '__main__':
    data = build()
    (ROOT / 'data/apron_layout.yaml').write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding='utf-8')
    print(f"{len(data['nodes'])} nodes, {len(data['edges'])} segments")
    for n in data['nodes']:
        if n['type'] == 'crossing':
            print(n['id'], n['aircraft_edges'])
