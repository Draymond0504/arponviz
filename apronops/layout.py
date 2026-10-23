"""加载机坪拓扑（模拟数据）并提供最短路径查询。"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Node:
    id: str
    label: str
    type: str
    x: float
    y: float


@dataclass
class Layout:
    nodes: dict[str, Node] = field(default_factory=dict)
    adjacency: dict[str, list[tuple[str, float, float]]] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    reference: dict = field(default_factory=dict)
    edge_attributes: dict[tuple[str, str], dict] = field(default_factory=dict)
    # adjacency[node_id] = [(neighbor_id, distance_m, base_travel_s), ...]

    def node_xy(self, node_id: str) -> tuple[float, float]:
        n = self.nodes[node_id]
        return n.x, n.y

    def shortest_path(self, start: str, end: str) -> list[str]:
        if start == end:
            return [start]
        dist = {start: 0.0}
        prev: dict[str, str] = {}
        visited: set[str] = set()
        heap = [(0.0, start)]
        while heap:
            d, u = heapq.heappop(heap)
            if u in visited:
                continue
            visited.add(u)
            if u == end:
                break
            for v, w, _ in self.adjacency.get(u, []):
                nd = d + w
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(heap, (nd, v))
        if end not in dist:
            raise ValueError(f"no path from {start} to {end}")
        path = [end]
        while path[-1] != start:
            path.append(prev[path[-1]])
        path.reverse()
        return path

    def path_distance(self, start: str, end: str) -> float:
        path = self.shortest_path(start, end)
        total = 0.0
        for a, b in zip(path, path[1:]):
            total += self.edge_distance(a, b)
        return total

    def edge_distance(self, a: str, b: str) -> float:
        for v, w, _ in self.adjacency.get(a, []):
            if v == b:
                return w
        raise ValueError(f"no direct edge {a}-{b}")

    def speed_limit(self, a: str, b: str) -> float:
        return self.edge_attributes.get((a, b), {}).get("speed_limit_mps", float("inf"))


def load_layout(path: str) -> Layout:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    layout = Layout()
    layout.metadata = {k: v for k, v in raw.items() if k not in ("nodes", "edges")}
    if raw.get("reference_file"):
        with (Path(path).parent / raw["reference_file"]).open(encoding="utf-8") as f:
            layout.reference = yaml.safe_load(f)
    for n in raw.get("nodes", []):
        layout.nodes[n["id"]] = Node(id=n["id"], label=n["label"], type=n["type"], x=n["x"], y=n["y"])
    for e in raw.get("edges", []):
        a, b, dist, tsec = e["from"], e["to"], float(e["distance_m"]), float(e["base_travel_s"])
        layout.adjacency.setdefault(a, []).append((b, dist, tsec))
        layout.adjacency.setdefault(b, []).append((a, dist, tsec))
        attrs = {k: v for k, v in e.items() if k not in ("from", "to")}
        layout.edge_attributes[a, b] = attrs
        layout.edge_attributes[b, a] = attrs
    return layout
