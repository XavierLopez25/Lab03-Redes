import re
from collections import defaultdict
from typing import Dict

EDGE_RE = re.compile(r"N(\d+)-N(\d+):(\d+)")

def parse_topology(text: str) -> Dict[str, Dict[str, int]]:
    """
    Devuelve un grafo no dirigido con pesos:
      g["N3"]["N9"] = 2
    """
    g = defaultdict(dict)
    for n1, n2, w in EDGE_RE.findall(text.replace("\n", " ")):
        a, b, w = f"N{n1}", f"N{n2}", int(w)
        g[a][b] = w
        g[b][a] = w
    return dict(g)

def neighbors_of(graph: Dict[str, Dict[str, int]], node: str) -> Dict[str, int]:
    return graph.get(node, {})

def load_topology_from_file(path: str) -> Dict[str, Dict[str, int]]:
    with open(path, "r", encoding="utf-8") as f:
        return parse_topology(f.read())
