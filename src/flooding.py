from __future__ import annotations
from typing import Dict, Any, List

def headers_to_dict(hlist: List[Dict[str, Any]] | None) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    for item in (hlist or []):
        d.update(item)
    return d

def dict_to_headers(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{k: v} for k, v in d.items()]

def node_of(address: str) -> str:
    # "sec30.<group>.nodo3" -> "N3"
    try:
        last = address.split(".")[-1]  # "nodo3"
        num = int(last.replace("nodo", ""))
        return f"N{num}"
    except Exception:
        # Si ya viene "N3", devolverlo tal cual
        return address

class FloodingStrategy:
    async def handle_message(self, router, msg: dict) -> None:
        mid = msg.get("message_id")
        if mid in router.seen_ids:
            return
        router.seen_ids.add(mid)

        dst_addr = msg.get("to", "")
        dst_node = node_of(dst_addr)

        # ¿Soy destino?
        if dst_node == router.me or dst_addr == router.listen_channel:
            print(f"{router.me} DESTINO -> {msg['payload']}")
            return

        # Último hop y path/cost
        h = headers_to_dict(msg.get("headers"))
        last_via = h.get("via")  # "N*"
        path = list(h.get("path", []))
        if not path:
            path = [node_of(msg.get("from", router.listen_channel))]
        if not path or path[-1] != router.me:
            path.append(router.me)

        # Vecinos del nodo actual
        neighbors = list(router.graph[router.me].keys())
        # Evitar devolver al que lo mandó
        fanout = [nb for nb in neighbors if nb != last_via]

        # Propagar a cada vecino
        for nb in fanout:
            fwd = dict(msg)
            fwd["ttl"] = msg.get("ttl", 0) - 1
            if fwd["ttl"] <= 0:
                continue

            hh = dict(h)
            cost = hh.get("cost", 0)
            try:
                cost += router.graph[router.me][nb]
            except Exception:
                pass
            hh.update({"via": router.me, "path": path, "cost": cost})
            fwd["headers"] = dict_to_headers(hh)

            await router.publish(nb, fwd)

        if fanout:
            print(f"[{router.me}] FLOOD → {fanout} (dest {dst_node}) path={path} seen={len(router.seen_ids)}")