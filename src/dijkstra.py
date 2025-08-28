from __future__ import annotations
from typing import Dict, Tuple, List
import heapq

Graph = Dict[str, Dict[str, int]]

def dijkstra(graph: Graph, source: str) -> Tuple[Dict[str, int], Dict[str, str]]:
    """
    Dijkstra clásico desde `source`.

    Retorna:
      - dist[dest] = costo mínimo
      - prev[dest] = predecesor inmediato en el camino óptimo (para reconstruir ruta)
    """
    dist: Dict[str, int] = {v: float("inf") for v in graph}
    prev: Dict[str, str] = {}
    dist[source] = 0
    pq: List[Tuple[int, str]] = [(0, source)]
    visited = set()

    while pq:
        d, u = heapq.heappop(pq)
        if u in visited:
            continue
        visited.add(u)
        for v, w in graph[u].items():
            alt = d + w
            if alt < dist[v]:
                dist[v] = alt
                prev[v] = u
                heapq.heappush(pq, (alt, v))
    return dist, prev

def build_next_hop(source: str, prev: Dict[str, str]) -> Dict[str, str]:
    """
    A partir de la relación prev, reconstruye el primer salto (next-hop)
    desde `source` hacia cada destino.
    """
    next_hop: Dict[str, str] = {}
    for dest in list(prev.keys()):
        if dest == source:
            continue
        # retrocede dest -> ... -> source
        cur = dest
        chain = [cur]
        while cur in prev and prev[cur] != source:
            cur = prev[cur]
            chain.append(cur)
        if cur in prev and prev[cur] == source:
            first = cur  # vecino directo desde source
            next_hop[dest] = first
    return next_hop

def all_pairs_next_hops(graph: Graph) -> Dict[str, Dict[str, str]]:
    """
    Para cada nodo `u`, calcula su tabla de next-hop hacia todos los destinos.
    (Simula el cálculo que haría LSR/Dijkstra en cada router).
    """
    tables: Dict[str, Dict[str, str]] = {}
    for node in graph.keys():
        _, prev = dijkstra(graph, node)
        tables[node] = build_next_hop(node, prev)
    return tables
