import os, json, asyncio, time, uuid, argparse
from typing import Dict, List, Any
import redis.asyncio as redis

from topology import load_topology_from_file
from dijkstra import all_pairs_next_hops

# =========================
# Helpers: addresses & headers
# =========================

def address_of(node: str, group: str) -> str:
    # "N3" -> "sec30.<group>.nodo3"
    n = int(node[1:])
    return f"sec30.{group}.nodo{n}"

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
        self.next_hop_table = next_hop_table
        self.graph = graph
        self.group = group
        self.prefix = prefix
        self.r = redis.Redis(host=redis_host, port=redis_port, password=redis_pwd)

        # Canal de escucha ('group', escucha en sec30.<group>.nodoX)
        self.listen_channel = address_of(me, group)

    async def run(self):
        async with self.r.pubsub() as ps:
            await ps.subscribe(self.listen_channel)
            print(f"{self.me} listening on {self.listen_channel}")
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

    async def handle(self, msg: dict):
        ttl = msg.get("ttl", 0)
        if ttl <= 0:
            print(f"[{self.me}] TTL agotado, drop {msg.get('message_id')}")
            return

        mtype = msg.get("type")
        if mtype == "message":
            await self.on_message(msg)
        elif mtype == "hello":
            await self.on_hello(msg)
        elif mtype == "echo":
            h = headers_to_dict(msg.get("headers"))
            print(f"[{self.me}] ECHO de {node_of(msg['from'])} headers={h}")
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

# =========================
# Main
# =========================

async def main(args):
    # Cargar topología
    graph = load_topology_from_file(args.topology)

    # Resolver next-hops según el proto (por ahora siempre Dijkstra)
    if args.proto.lower() not in {"dijkstra", "flooding", "lsr", "dvr"}:
        raise SystemExit(f"--proto inválido: {args.proto}")
    if args.proto.lower() != "dijkstra":
        print(f"[warn] --proto={args.proto} aún no implementado; usando Dijkstra para next-hops.")

    tables = all_pairs_next_hops(graph)  # { "N1": {"N2":"N2", ...}, ... }

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
    p = argparse.ArgumentParser(description="Simulador Redis Pub/Sub con Dijkstra (headers como lista).")
    p.add_argument("--topology", default="topology.txt", help="Archivo de topología")
    p.add_argument("--source", default="N3", help="Nodo origen lógico (N*)")
    p.add_argument("--dest", default="N11", help="Nodo destino lógico (N*)")
    p.add_argument("--text", default="Hola desde Dijkstra 👋", help="Mensaje de prueba (payload)")
    p.add_argument("--ttl", type=int, default=20, help="TTL del mensaje")
    p.add_argument("--runtime", type=float, default=6.0, help="Segundos a mantener corriendo")
    p.add_argument("--proto", default="dijkstra", help="Algoritmo: dijkstra|flooding|lsr|dvr (por ahora usa Dijkstra)")
    # Conexión Redis
    p.add_argument("--redis-host", default=os.getenv("REDIS_HOST", "localhost"), help="Host Redis")
    p.add_argument("--redis-port", type=int, default=int(os.getenv("REDIS_PORT", "6379")), help="Puerto Redis")
    p.add_argument("--redis-pwd", default=os.getenv("REDIS_PWD", "testpass"), help="Password Redis")
    # Direccionamiento
    p.add_argument("--group", default=os.getenv("GROUP", "sim"), help="Grupo para from/to (sec30.<group>.nodoX)")
    p.add_argument("--prefix", default=os.getenv("CHAN_PREFIX", ""), help="Prefijo alterno (si quieres publicar en sec30.<prefix>.nodoX). Vacío = usar group.")
    args = p.parse_args()
    asyncio.run(main(args))
