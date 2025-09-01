import os, json, asyncio, time, uuid, argparse
from typing import Dict, List, Any
import redis.asyncio as redis

from topology import load_topology_from_file
from dijkstra import all_pairs_next_hops
from flooding import FloodingStrategy

# =========================
# Helpers: addresses & headers
# =========================

def address_of(node: str, group: str) -> str:
    # "N3" -> "sec30.<group>.nodo3"
    n = int(node[1:])
    return f"sec30.{group}.nodo{n}"

def normalize_prefix(prefix: str | None) -> str | None:
    if not prefix:
        return None
    return prefix if prefix.startswith("sec30.") else f"sec30.{prefix}"

def node_of(address: str) -> str:
    # "sec30.<group>.nodo3" -> "N3"
    try:
        last = address.split(".")[-1]  # "nodo3"
        num = int(last.replace("nodo", ""))
        return f"N{num}"
    except Exception:
        # Si ya viene "N3", devolverlo tal cual
        return address

def chan_of(prefix: str, node: str) -> str:
    n = int(node[1:])
    return f"{prefix}.nodo{n}"

def headers_to_dict(hlist: List[Dict[str, Any]] | None) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    for item in (hlist or []):
        d.update(item)
    return d

def dict_to_headers(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{k: v} for k, v in d.items()]

def make_msg(proto: str, mtype: str, src_node: str, dst_node: str, ttl: int,
             payload: Any, headers_list: List[Dict[str, Any]] | None,
             group: str) -> Dict[str, Any]:
    return {
        "message_id": str(uuid.uuid4()),
        "ts": time.time(),
        "proto": proto,                  # "dijkstra" | "flooding" | "lsr" | "dvr"
        "type": mtype,                   # "message" | "hello" | "echo" | "info"
        "from": address_of(src_node, group),
        "to": address_of(dst_node, group),
        "ttl": ttl,
        "headers": headers_list or [],   # LISTA de objetos {k:v}
        "payload": payload,
    }

# =========================
# Router
# =========================

class Router:
    def __init__(self,
                 me: str,                         # "N3"
                 next_hop_table: Dict[str, str],  # dest "N*" -> nextHop "N*"
                 graph: Dict[str, Dict[str, int]],# para poder sumar cost
                 redis_host: str, redis_port: int, redis_pwd: str,
                 group: str,                      # para from/to addresses
                 prefix: str | None = None):      # opcional: prefijo alternativo
        self.me = me
        self.next_hop_table = next_hop_table or {}
        self.graph = graph
        self.group = group
        self.r = redis.Redis(host=redis_host, port=redis_port, password=redis_pwd)
        self.prefix = normalize_prefix(prefix)   
        self.flood = FloodingStrategy()         
        self.seen_ids: set[str] = set()          

        # Canal de escucha ('group', escucha en sec30.<group>.nodoX)
        self.listen_channel = address_of(me, group)

        # --- LSR state ---
        self.lsdb: Dict[str, Dict[str, int]] = {}      # origin -> {neighbor: cost}
        self.last_seq: Dict[str, int] = {}             # origin -> last seq seen
        self.my_seq: int = 0                           # seq para mis LSPs
        self.proto: str | None = None                  # se setea luego desde main
        self.my_neighbors = dict(self.graph.get(self.me, {}))  # vecinos locales

        # Bootstrap de next-hop por si aún no hay LSDB (evita "sin ruta")
        try:
            from dijkstra import dijkstra, build_next_hop
            _, prev = dijkstra(self.graph, self.me)
            boot = build_next_hop(self.me, prev)
            # No pisar si ya venía algo desde arriba:
            for k, v in (boot or {}).items():
                self.next_hop_table.setdefault(k, v)
        except Exception:
            pass

    async def run(self):
        async with self.r.pubsub() as ps:
            await ps.subscribe(self.listen_channel)
            print(f"{self.me} listening on {self.listen_channel}")

            # Tarea periódica de anuncios LSR:
            if self.proto == "lsr":
                asyncio.create_task(self._lsr_periodic_lsp())

            while True:
                m = await ps.get_message(ignore_subscribe_messages=True, timeout=None)
                if not m:
                    continue
                try:
                    data = m["data"]
                    msg = json.loads(data if isinstance(data, str) else data.decode())
                except Exception:
                    print(f"[{self.me}] Ignoro mensaje no-JSON")
                    continue
                await self.handle(msg)

    def publish_channel_for(self, node: str) -> str:
        if self.prefix:
            return chan_of(self.prefix, node)
        return address_of(node, self.group)

    async def publish(self, node: str, msg: dict):
        ch = self.publish_channel_for(node)
        await self.r.publish(ch, json.dumps(msg))

    async def flood_to_neighbors(self, except_node: str | None, msg: dict):
        # except_node usa ID lógico "N*"
        for nbr in self.my_neighbors.keys():
            if except_node and nbr == except_node:
                continue
            await self.publish(nbr, msg)

    async def handle(self, msg: dict):
        ttl = msg.get("ttl", 0)
        if ttl <= 0:
            print(f"[{self.me}] TTL agotado, drop {msg.get('message_id')}")
            return

        mtype = msg.get("type")
        if mtype == "message":
            proto = (msg.get("proto") or "dijkstra").lower()
            if proto == "flooding":
                await self.flood.handle_message(self, msg)
            else:
                await self.on_message(msg)
        elif mtype == "hello":
            await self.on_hello(msg)
        elif mtype == "echo":
            h = headers_to_dict(msg.get("headers"))
            print(f"[{self.me}] ECHO de {node_of(msg['from'])} headers={h}")
        elif mtype == "info" and msg.get("proto") == "lsr":
            await self.on_lsr_info(msg)
        else:
            print(f"[{self.me}] Tipo desconocido: {mtype}")

    async def on_hello(self, msg: dict):
        h = headers_to_dict(msg.get("headers"))
        h["echo"] = True
        rep = make_msg(
            proto=msg.get("proto","dijkstra"),
            mtype="echo",
            src_node=self.me,
            dst_node=node_of(msg["from"]),
            ttl=3,
            payload={"ok": True},
            headers_list=dict_to_headers(h),
            group=self.group
        )
        await self.publish(node_of(msg["from"]), rep)

    async def on_message(self, msg: dict):
        dst_addr = msg["to"]
        dst_node = node_of(dst_addr)

        # ¿Soy destino?
        if dst_node == self.me or dst_addr == self.listen_channel:
            print(f"{self.me} DESTINO ▶ {msg['payload']}")
            return

        nh = self.next_hop_table.get(dst_node)
        if not nh:
            print(f"[{self.me}] sin ruta a {dst_node}")
            return

        # Preparar forward
        fwd = dict(msg)
        fwd["ttl"] = msg.get("ttl", 0) - 1

        h = headers_to_dict(msg.get("headers"))
        path = list(h.get("path", []))
        if not path:
            # inicia con el origen lógico
            path = [node_of(msg.get("from", address_of(self.me, self.group)))]

        # agrega self.me solo si no es igual al último
        if not path or path[-1] != self.me:
            path.append(self.me)

        # costo acumulado
        cost = h.get("cost", 0)
        try:
            cost += self.graph[self.me][nh]
        except Exception:
            pass

        h.update({"via": self.me, "path": path, "cost": cost})
        fwd["headers"] = dict_to_headers(h)

        await self.publish(nh, fwd)
        print(f"[{self.me}] → {nh} (dest {dst_node}) path={path} cost={cost}")

    # ---------- LSR ----------
    async def _lsr_periodic_lsp(self):
        # Envía un LSP cada 2 segundos (ajustable)
        while True:
            await asyncio.sleep(2.0)
            await self._lsr_send_lsp()

    async def _lsr_send_lsp(self):
        self.my_seq += 1
        payload = {
            "origin": self.me,
            "seq": self.my_seq,
            "neighbors": self.my_neighbors  # dict: N* -> costo
        }
        lsp = make_msg(
            proto="lsr",
            mtype="info",
            src_node=self.me,
            dst_node=self.me,   # semántico; realmente floodeamos a vecinos
            ttl=10,
            payload=payload,
            headers_list=[{"lsp": True}, {"path":[self.me]}],
            group=self.group
        )
        await self.flood_to_neighbors(except_node=None, msg=lsp)
        print(f"[{self.me}] LSP seq={self.my_seq} flooded to neighbors")

    async def on_lsr_info(self, msg: dict):
        # 1) Decode
        try:
            payload = msg.get("payload", {})
            origin = payload["origin"]
            seq = int(payload["seq"])
            neighbors = dict(payload["neighbors"])
        except Exception:
            print(f"[{self.me}] LSP malformado, drop")
            return

        # 2) Duplicates / ordering
        if self.last_seq.get(origin, -1) >= seq:
            # viejo o duplicado
            return
        self.last_seq[origin] = seq

        # 3) Update LSDB
        self.lsdb[origin] = neighbors

        # 4) Recompute SPF graph from LSDB
        #    Construimos un grafo no dirigido a partir de todas las entradas
        spf_graph: Dict[str, Dict[str, int]] = {}
        for u, nbrs in self.lsdb.items():
            spf_graph.setdefault(u, {})
            for v, w in nbrs.items():
                spf_graph.setdefault(v, {})
                spf_graph[u][v] = w
                spf_graph[v][u] = w

        # Incluye mis vecinos si aún no hay suficiente info
        if self.me not in spf_graph:
            spf_graph[self.me] = dict(self.my_neighbors)

        # 5) Run Dijkstra y actualiza tabla de next-hop
        try:
            from dijkstra import dijkstra, build_next_hop
            _, prev = dijkstra(spf_graph, self.me)
            self.next_hop_table = build_next_hop(self.me, prev)
        except Exception as e:
            print(f"[{self.me}] SPF error: {e}")

        # 6) Flood onwards (except the neighbor it came from)
        headers = headers_to_dict(msg.get("headers"))
        path = list(headers.get("path", []))
        last_hop = node_of(msg.get("from"))

        if msg.get("ttl", 0) <= 1:
            return
        fwd = dict(msg)
        fwd["ttl"] = msg.get("ttl", 0) - 1

        if not path or path[-1] != self.me:
            path.append(self.me)
        headers.update({"via": self.me, "path": path})
        fwd["headers"] = dict_to_headers(headers)

        await self.flood_to_neighbors(except_node=last_hop, msg=fwd)

# =========================
# Main
# =========================

async def main(args):
    # Cargar topología
    graph = load_topology_from_file(args.topology)

    # Resolver según proto
    proto = args.proto.lower()
    if proto not in {"dijkstra", "flooding", "lsr", "dvr"}:
        raise SystemExit(f"--proto inválido: {args.proto}")

    tables = {}
    if proto == "dijkstra":
        tables = all_pairs_next_hops(graph)
    elif proto == "flooding":
        tables = {}
    elif proto == "lsr":
        print("[info] LSR: next-hops se recalculan dinámicamente con LSDB (vía LSPs)")
        # tables queda vacío; cada router arranca con bootstrap local
    else:
        print(f"[warn] --proto={args.proto} aún no implementado; usando bootstrap con Dijkstra")
        tables = all_pairs_next_hops(graph)

    # Lanzar un router por cada nodo
    routers = [
        Router(
            me=n,
            next_hop_table=tables.get(n, {}),
            graph=graph,
            redis_host=args.redis_host,
            redis_port=args.redis_port,
            redis_pwd=args.redis_pwd,
            group=args.group,
            prefix=(args.prefix if args.prefix else None),
        )
        for n in graph.keys()
    ]
    for r in routers:
        r.proto = proto

    tasks = [asyncio.create_task(r.run()) for r in routers]

    # Pequeña pausa para asegurar suscripciones
    await asyncio.sleep(0.5)

    # Enviar HELLO del origen (con headers poblados)
    hello = make_msg(
        proto=args.proto,
        mtype="hello",
        src_node=args.source,
        dst_node=args.source,
        ttl=3,
        payload={"hi": "there"},
        headers_list=[{"op":"hello"}, {"path":[args.source]}],
        group=args.group
    )
    # Publish HELLO al canal del origen
    r = redis.Redis(host=args.redis_host, port=args.redis_port, password=args.redis_pwd)
    await r.publish(address_of(args.source, args.group), json.dumps(hello))

    # Enviar DATA desde source hacia dest (payload como string)
    data = make_msg(
        proto=args.proto,
        mtype="message",
        src_node=args.source,
        dst_node=args.dest,
        ttl=args.ttl,
        payload=args.text,
        headers_list=[{"op":"data"}],
        group=args.group
    )
    await r.publish(address_of(args.source, args.group), json.dumps(data))

    # Mantener la simulación corriendo
    await asyncio.sleep(args.runtime)
    for t in tasks:
        t.cancel()

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Simulador Redis Pub/Sub con Dijkstra y LSR (headers como lista).")
    p.add_argument("--topology", default="topology.txt", help="Archivo de topología")
    p.add_argument("--source", default="N3", help="Nodo origen lógico (N*)")
    p.add_argument("--dest", default="N11", help="Nodo destino lógico (N*)")
    p.add_argument("--text", default="Hola desde Dijkstra/LSR 👋", help="Mensaje de prueba (payload)")
    p.add_argument("--ttl", type=int, default=20, help="TTL del mensaje")
    p.add_argument("--runtime", type=float, default=8.0, help="Segundos a mantener corriendo")
    p.add_argument("--proto", default="dijkstra", help="Algoritmo: dijkstra|flooding|lsr|dvr (lsr implementado)")

    # Conexión Redis
    p.add_argument("--redis-host", default=os.getenv("REDIS_HOST", "localhost"), help="Host Redis")
    p.add_argument("--redis-port", type=int, default=int(os.getenv("REDIS_PORT", "6379")), help="Puerto Redis")
    p.add_argument("--redis-pwd", default=os.getenv("REDIS_PWD", "testpass"), help="Password Redis")

    # Direccionamiento
    p.add_argument("--group", default=os.getenv("GROUP", "sim"), help="Grupo para from/to (sec30.<group>.nodoX)")
    p.add_argument("--prefix", default=os.getenv("CHAN_PREFIX", ""), help="Prefijo alterno para publicar (sec30.<prefix>.nodoX)")

    args = p.parse_args()
    asyncio.run(main(args))
