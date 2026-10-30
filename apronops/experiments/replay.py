"""Validate portable JSON before any uploaded values reach the map renderer."""
from __future__ import annotations

import json
import math

MAX_BYTES = 100 * 1024 * 1024


def _require(condition, message='回放字段无效'):
    if not condition:
        raise ValueError(message)


def _number(value):
    _require(type(value) in (int, float) and math.isfinite(value), '回放数值必须有限')


def _strings(row, fields):
    _require(isinstance(row, dict))
    for field in fields:
        _require(isinstance(row[field], str))


def _list(value):
    _require(isinstance(value, list))
    return value


def _graph(graph, *, required=False):
    _require(isinstance(graph, dict))
    if required:
        _require('nodes' in graph and 'edges' in graph, '回放缺少完整路网')
    nodes = _list(graph.get('nodes', []))
    _require((bool(nodes) or not required) and len(nodes) <= 20000, '回放路网节点无效')
    ids = set()
    for node in nodes:
        _strings(node, ('id', 'label', 'type'))
        _require(node['id'] and node['id'] not in ids, '回放节点 ID 重复或为空')
        ids.add(node['id'])
        for key in ('x', 'y'):
            _number(node[key])
    for edge in _list(graph.get('edges', [])):
        _strings(edge, ('from', 'to'))
        _require(edge['from'] in ids and edge['to'] in ids, '回放路段引用了不存在的节点')
        if required:
            _require(edge.get('road_type', 'service') in ('perimeter', 'service', 'stand_access'))
    return ids


def load_replay(data: bytes):
    if len(data) > MAX_BYTES:
        raise ValueError('回放文件超过 100 MB')
    try:
        raw = json.loads(data)
        _require(isinstance(raw, dict) and raw['schema_version'] == 1, '不支持的回放版本')
        layout = raw['layout']
        ids = _graph(layout, required=True)
        _require(isinstance(layout.get('metadata', {}), dict))
        aircraft_ids = _graph(layout.get('reference', {}))
        frames = _list(raw['frames'])
        _require(0 < len(frames) <= 200000, '回放帧数量无效')
        previous = -1.
        for frame in frames:
            _strings(frame, ('simulation_time',))
            at = frame['elapsed_s']
            _number(at)
            _require(at >= 0 and at > previous, '回放时间必须严格递增')
            previous = at
            for vehicle in _list(frame['vehicles']):
                _strings(vehicle, ('id', 'state'))
                for key in ('x', 'y', 'battery'):
                    _number(vehicle[key])
                _number(vehicle.get('current_speed_mps', 0.))
                for key in ('node_id', 'next_node_id', 'target_node_id'):
                    if vehicle.get(key) is not None:
                        _require(isinstance(vehicle[key], str) and vehicle[key] in ids)
                for node in _list(vehicle.get('planned_path', [])):
                    _require(isinstance(node, str) and node in ids)
            for task in _list(frame['tasks']):
                _strings(task, ('id', 'type', 'status'))
            for plane in _list(frame.get('aircraft', [])):
                _strings(plane, ('id','flight_id','state','node_id','wait_reason'))
                _require(plane['state'] in ('scheduled','taxi_in','parked','taxi_out','departed'))
                for key in ('x','y','heading_deg','current_speed_mps'):
                    _number(plane[key])
                _require(plane['node_id'] in aircraft_ids)
                _require(plane.get('next_node_id') is None or plane['next_node_id'] in aircraft_ids)
            for crossing in _list(frame.get('crossings', [])):
                _strings(crossing, ('id', 'status'))
                _require(crossing['id'] in ids)
                for key in ('radius_m', 'x', 'y'):
                    _number(crossing[key])
                _require(crossing['radius_m'] > 0)
                for key in ('aircraft', 'vehicles', 'waiting_aircraft'):
                    _list(crossing[key])
            for closure in _list(frame.get('closures', [])):
                _strings(closure, ('from_node', 'to_node'))
                _require(closure['from_node'] in ids and closure['to_node'] in ids)
                _require(closure['reopened_at'] is None or isinstance(closure['reopened_at'], str))
            for depot in _list(frame.get('depots', [])):
                _strings(depot, ('id',))
                _require(depot['id'] in ids)
                for key in ('capacity','occupied','reserved','queue_length'):
                    _require(type(depot[key]) is int and depot[key] >= 0)
                _require(depot['occupied']+depot['reserved'] <= depot['capacity'])
                if depot['predicted_wait_s'] is not None:
                    _number(depot['predicted_wait_s'])
            if frame.get('recovery'):
                for key in ('resolved','relocations','unresolved'):
                    _require(type(frame['recovery'][key]) is int and frame['recovery'][key] >= 0)
        _strings(raw['manifest'], ('policy', 'scenario_id', 'status'))
        _require(type(raw['manifest']['seed']) is int)
        _require(raw['manifest']['status'] in ('completed', 'interrupted', 'failed'))
        _require(isinstance(raw['summary'], dict), '回放缺少指标')
        return raw
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError, OverflowError, RecursionError) as error:
        raise ValueError('无效回放文件，请选择实验生成的 replay.json') from error


def frame_tracks(frames, index, tail=80):
    tracks = {}
    for frame in frames[max(0, index-tail+1):index+1]:
        for v in frame['vehicles']:
            tracks.setdefault(v['id'], []).append({'x': v['x'], 'y': v['y']})
    return tracks
