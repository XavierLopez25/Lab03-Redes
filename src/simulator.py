# node_link_state.py
import os, json, asyncio, time, argparse, socket, re
from typing import Dict, Any
import redis.asyncio as redis
from dotenv import load_dotenv
load_dotenv()

# ------------- Helpers de direcciones -----------------

def group_for_node(node: str, my_group: str) -> str:
    """
    Deriva el group correcto para 'node' usando el prefijo del group actual.
    Ej: my_group='test1', node='N5' -> 'test5'
        my_group='grupo3', node='N2' -> 'grupo2'
    """
    n = int(node[1:])
    base = re.sub(r'\d+$', '', my_group or '')
    return f"{base}{n}"

def address_of(node: str, group: str) -> str:
    # "N3" -> "sec30.<group>.nodo3"
    n = int(node[1:])
    return f"sec30.{group}.nodo{n}"

def address_for_dest(node: str, my_group: str) -> str:
    """Atajo para direccionar al canal del DESTINO (group por nodo)."""
    return address_of(node, group_for_node(node, my_group))

def node_of(addr: str) -> str:
    # "sec30.<group>.nodo3" -> "N3"
    try:
        last = addr.split(".")[-1]  # "nodo3"
        num = int(last.replace("nodo", ""))
        return f"N{num}"
    except Exception:
        return addr  # ya viene "N3"

def parse_neighbors(s: str | None) -> Dict[str, int]:
    # "N2:10,N5:14,N8:15" -> {"N2":10,"N5":14,"N8":15}
    if not s:
        return {}
    out: Dict[str, int] = {}
    for part in s.replace(" ", "").split(","):
        if not part:
            continue
        n, w = part.split(":")
        out[n] = int(w)
    return out

# ------------- Mensajes wire-format -------------------

def make_message(from_addr: str, to_addr: str, weight: int) -> dict:
    # Flooding de adyacencia (se propaga si cambia algo)
    return {
        "type": "message",
        "from": from_addr,
        "to": to_addr,
        "hops": int(weight),
    }

def make_hello(from_addr: str, to_addr: str, weight: int) -> dict:
    # Hello (no se propaga; solo resetea timer del vecino)
    return {
        "type": "hello",
        "from": from_addr,
        "to": to_addr,
        "hops": int(weight),
    }

# ------------- Dijkstra minimal -----------------------

def dijkstra(graph: Dict[str, Dict[str, int]], src: str):
    dist = {u: float("inf") for u in graph.keys()}
    prev: Dict[str, str] = {}
    dist[src] = 0
    visited = set()
    while True:
        u = None
        best = float("inf")
        for k, d in dist.items():
            if k not in visited and d < best:
                best, u = d, k
        if u is None:
            break
        visited.add(u)
        for v, w in graph.get(u, {}).items():
            nd = dist[u] + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
    return dist, prev

def build_next_hop(src: str, prev: Dict[str, str]) -> Dict[str, str]:
    nh: Dict[str, str] = {}
    for dst in prev.keys():
        cur = dst
        while prev.get(cur) and prev[cur] != src:
            cur = prev[cur]
        if prev.get(cur) == src:
            nh[dst] = cur
    return nh

# ------------- Nodo Link-State ------------------------

class Node:
    def __init__(self, me: str, group: str, neighbors: Dict[str, int],
                 redis_host: str, redis_port: int, redis_pwd: str,
                 hello_period: int = 3, hello_misses: int = 10,
                 remote_age: int = 60, stable_nochange: int = 100,
                 debug: bool = False):
        self.me = me
        self.group = group
        self.addr_me = address_of(me, group)
        self.neighbors_cfg = dict(neighbors)  # pesos directos configurados
        self.HELLO_PERIOD = int(hello_period)
        self.HELLO_MISSES = int(hello_misses)    # presupuesto de hellos perdidos
        self.REMOTE_AGE = int(remote_age)        # segundos de vida de adyacencias remotas
        self.STABLE_NOCHANGE = int(stable_nochange)

        self.debug = debug

        # Tabla de topología:
        # topo[u][v] = {"weight": w, "time": int} (si u==me o v==me)  ó  {"weight": w, "age": int} (remoto)
        self.topo: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # Contador de mensajes "message" que NO cambian nada
        self.nochange_count = 0

        # Conexión Redis
        self.r = redis.Redis(host=redis_host, port=redis_port, password=redis_pwd, decode_responses=True)
        self.listen_channel = self.addr_me

        # Inicializar adyacencias directas (con timer "time")
        self._ensure_node(self.me)
        for nbr, w in self.neighbors_cfg.items():
            self._ensure_node(nbr)
            self.topo[self.me][nbr] = {"weight": w, "time": self.HELLO_MISSES}
            self.topo.setdefault(nbr, {}).setdefault(self.me, {"weight": w})
            # No ponemos "time" del lado remoto (solo del lado me->vecino)

    # -------- utilidades de tabla --------

    def _ensure_node(self, u: str):
        self.topo.setdefault(u, {})

    def _get_graph_for_dijkstra(self) -> Dict[str, Dict[str, int]]:
        # Convierte topo en grafo simplificado u->v:weight (solo entradas vigentes)
        g: Dict[str, Dict[str, int]] = {}
        for u, nbrs in self.topo.items():
            for v, meta in nbrs.items():
                w = meta.get("weight")
                if w is None:
                    continue
                # Si es vecino directo mio, requiere que su "time" > 0 del lado me->vecino
                if u == self.me and v in self.topo[self.me]:
                    t = self.topo[self.me][v].get("time", 0)
                    if t <= 0:
                        continue
                # Si es remoto, requiere "age" > 0 si existe
                age = meta.get("age", self.REMOTE_AGE)
                if age <= 0:
                    continue
                g.setdefault(u, {})[v] = int(w)
        return g

    def _live_neighbors(self):
        # Vecinos directos cuyo time>0
        out = []
        for v, meta in self.topo.get(self.me, {}).items():
            if meta.get("time", 0) > 0:
                out.append(v)
        return out

    # -------- envío --------

    async def _publish(self, channel: str, msg: dict):
        if self.debug:
            print(f"[{self.me}] PUBLISH {channel} → {json.dumps(msg, ensure_ascii=False)}")
        await self.r.publish(channel, json.dumps(msg))

    async def _flood_my_adjacencies(self):
        # Propaga TODAS mis adyacencias directas vivas a todos mis vecinos vivos
        live = self._live_neighbors()
        for nbr, meta in self.topo.get(self.me, {}).items():
            if meta.get("time", 0) <= 0:
                continue
            w = meta["weight"]
            # >>> cambio: el 'to' y el canal de salida usan el group del DESTINO
            msg = make_message(self.addr_me, address_for_dest(nbr, self.group), w)
            for out in live:
                await self._publish(address_for_dest(out, self.group), msg)

    async def _send_hellos(self):
        # Hello periódico a cada vecino directo (solo a ese vecino)
        for nbr, meta in self.topo.get(self.me, {}).items():
            if meta.get("time", 0) <= 0:
                continue
            w = meta["weight"]
            # >>> cambio: hello hacia el canal del DESTINO
            h = make_hello(self.addr_me, address_for_dest(nbr, self.group), w)
            await self._publish(address_for_dest(nbr, self.group), h)

    async def _forward_message_if_changed(self, msg: dict, changed: bool):
        if not changed:
            self.nochange_count += 1
            if self.nochange_count >= self.STABLE_NOCHANGE:
                await self._run_and_print_dijkstra()
                self.nochange_count = 0
            return
        self.nochange_count = 0
        live = self._live_neighbors()
        if self.debug:
            print(f"[{self.me}] FORWARD {msg.get('from')} -> {msg.get('to')} a vecinos {live}")
        for out in live:
            await self._publish(address_for_dest(out, self.group), msg)


    # -------- recepción --------

    async def handle_hello(self, msg: dict):
        frm = node_of(msg["from"])
        to  = node_of(msg["to"])
        w   = int(msg.get("hops", 1))

        # Solo resetea timer si es hello de vecino directo → yo soy 'to'
        if to != self.me:
            return

        # Si no es vecino configurado, ignoro (o podría registrarlo)
        if frm not in self.topo.get(self.me, {}):
            self.topo.setdefault(self.me, {})[frm] = {"weight": w, "time": self.HELLO_MISSES}
            self.topo.setdefault(frm, {})[self.me] = {"weight": w}
            await self._flood_my_adjacencies()
            return

        # Actualiza peso si cambió y resetea timer
        meta = self.topo[self.me][frm]
        changed = False
        if meta.get("weight") != w:
            meta["weight"] = w
            changed = True
        meta["time"] = self.HELLO_MISSES

        # Mantener lado inverso con mismo peso
        self.topo.setdefault(frm, {}).setdefault(self.me, {})["weight"] = w

        if changed:
            # Si cambió mi adyacencia directa, vuelvo a floodear mis adyacencias
            await self._flood_my_adjacencies()

    async def handle_message(self, msg: dict):
        # Mensaje con adyacencia (u --w--> v). Es *contenido*, no destino.
        u = node_of(msg["from"])
        v = node_of(msg["to"])
        raw = msg.get("hops")
        if raw is None:  # aceptar otros nombres por compatibilidad
            raw = msg.get("weight", msg.get("w"))

        try:
            w = int(raw)
        except (TypeError, ValueError):
            if self.debug:
                print(f"[{self.me}] Descarto mensaje por 'hops' inválido: {msg}")
            return

        # Asegurar nodos
        self._ensure_node(u)
        self._ensure_node(v)

        # Guardar (u->v) y (v->u) con weight y age (remoto)
        changed = False
        # u -> v
        cur = self.topo[u].get(v, {})
        if cur.get("weight") != w:
            self.topo[u][v] = {"weight": w, "age": self.REMOTE_AGE}
            changed = True
        else:
            # mismo peso → refrescar age
            cur["age"] = self.REMOTE_AGE
            self.topo[u][v] = cur

        # v -> u
        cur2 = self.topo[v].get(u, {})
        if cur2.get("weight") != w:
            self.topo[v][u] = {"weight": w, "age": self.REMOTE_AGE}
            changed = True
        else:
            cur2["age"] = self.REMOTE_AGE
            self.topo[v][u] = cur2

        # Reenviar SOLO si hubo cambio
        await self._forward_message_if_changed(msg, changed)

    # -------- tareas periódicas --------

    async def _hello_tick(self):
        while True:
            await self._send_hellos()
            await asyncio.sleep(self.HELLO_PERIOD)

    async def _neighbor_timer_tick(self):
        # Cada periodo HELLO, decrementa el contador de vecinos directos
        while True:
            await asyncio.sleep(self.HELLO_PERIOD)
            dead = []
            for nbr, meta in list(self.topo.get(self.me, {}).items()):
                t = meta.get("time", 0)
                if t > 0:
                    meta["time"] = t - 1
                if meta["time"] <= 0:
                    dead.append(nbr)
            if dead:
                for nbr in dead:
                    print(f"[{self.me}] Vecino {nbr} cayó (sin hellos). Removiendo adyacencia.")
                    # Eliminar adyacencia en ambos sentidos
                    self.topo[self.me].pop(nbr, None)
                    self.topo.get(nbr, {}).pop(self.me, None)
                # Floodear mis adyacencias vigentes
                await self._flood_my_adjacencies()
                # Recalcular ruta (opcional)
                await self._run_and_print_dijkstra()

    async def _remote_aging_tick(self):
        # Baja 1s el age de adyacencias remotas
        while True:
            await asyncio.sleep(1)
            to_del = []
            for u, nbrs in self.topo.items():
                for v, meta in list(nbrs.items()):
                    if u == self.me or v == self.me:
                        continue  # mis vecinos directos no usan 'age'
                    if "age" in meta:
                        meta["age"] -= 1
                        if meta["age"] <= 0:
                            to_del.append((u, v))
            if to_del:
                for (u, v) in to_del:
                    print(f"[{self.me}] Expiró adyacencia remota {u}-{v}")
                    self.topo[u].pop(v, None)
                # No hace falta floodear "borrados" remotos según instrucción;
                # cada nodo envejece de forma independiente.

    async def _run_and_print_dijkstra(self):
        g = self._get_graph_for_dijkstra()
        if self.me not in g:
            print(f"\n[{self.me}] Grafo aún no tiene vecinos vivos; no se corre Dijkstra.\n")
            return
        dist, prev = dijkstra(g, self.me)
        nh = build_next_hop(self.me, prev)

        print(f"\n[{self.me}] Tabla de ruteo (Dijkstra) sobre topología actual:")
        for dst in sorted(g.keys()):
            if dst == self.me:
                continue
            d = dist.get(dst, float("inf"))
            nx = nh.get(dst)
            # reconstruir path
            path = []
            cur = dst
            guard = 0
            while cur in prev and cur != self.me and guard < 10000:
                path.append(cur)
                cur = prev[cur]
                guard += 1
            if cur == self.me:
                path.append(self.me)
            path = list(reversed(path))
            print(f"  {self.me} -> {dst}: costo={d}, nextHop={nx}, path={path}")
        print()

    # -------- loop principal --------

    async def run(self):
        async with self.r.pubsub() as ps:
            await ps.subscribe(self.listen_channel)
            print(f"{self.me} listening on {self.listen_channel}")

            # Al iniciar, floodeo mis adyacencias
            await self._flood_my_adjacencies()

            # Lanzar tareas periódicas
            asyncio.create_task(self._hello_tick())
            asyncio.create_task(self._neighbor_timer_tick())
            asyncio.create_task(self._remote_aging_tick())

            while True:
                m = await ps.get_message(ignore_subscribe_messages=True, timeout=None)
                if not m:
                    continue
                try:
                    data = m["data"]
                    msg = json.loads(data if isinstance(data, str) else data.decode())
                except Exception:
                    continue

                if self.debug:
                    try:
                        print(f"[{self.me}] INBOUND {self.listen_channel} ← {json.dumps(msg, ensure_ascii=False)}")
                    except Exception:
                        print(f"[{self.me}] INBOUND {self.listen_channel} ← <no-json-printable>")

                mtype = msg.get("type")
                if mtype == "hello":
                    await self.handle_hello(msg)
                elif mtype == "message":
                    await self.handle_message(msg)
                else:
                    # ignorar otros tipos
                    pass

# ------------- Preflight Redis ------------------------

async def preflight_redis(host: str, port: int, pwd: str):
    try:
        socket.getaddrinfo(host, port)
    except socket.gaierror as e:
        raise SystemExit(f"[preflight] DNS no pudo resolver {host}:{port} → {e}")

    try:
        test = redis.Redis(host=host, port=port, password=pwd, socket_connect_timeout=30)
        pong = await test.ping()
        if pong is not True:
            raise SystemExit("[preflight] Redis respondió algo distinto a PONG")
    except Exception as e:
        raise SystemExit(f"[preflight] No se pudo conectar a Redis {host}:{port} → {e}")

# ------------- Main -----------------------------------

async def main():
    p = argparse.ArgumentParser(description="Nodo Link-State (hello + flooding de adyacencias) con Dijkstra local.")
    p.add_argument("--me", default="N3")
    p.add_argument("--group", default=os.getenv("GROUP", "grupo3"))
    p.add_argument("--neighbors", required=True, help='Vecinos directos "N2:10,N5:14"')
    p.add_argument("--redis-host", default=os.getenv("REDIS_HOST", "localhost"))
    p.add_argument("--redis-port", type=int, default=int(os.getenv("REDIS_PORT", "6379")))
    p.add_argument("--redis-pwd",  default=os.getenv("REDIS_PWD", "testpass"))
    p.add_argument("--hello-period", type=int, default=3)
    p.add_argument("--hello-misses", type=int, default=10)
    p.add_argument("--remote-age", type=int, default=3000)
    p.add_argument("--stable-nochange", type=int, default=25)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    await preflight_redis(args.redis_host, args.redis_port, args.redis_pwd)

    node = Node(
        me=args.me,
        group=args.group,
        neighbors=parse_neighbors(args.neighbors),
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_pwd=args.redis_pwd,
        hello_period=args.hello_period,
        hello_misses=args.hello_misses,
        remote_age=args.remote_age,
        stable_nochange=args.stable_nochange,
        debug=args.debug,
    )
    await node.run()

if __name__ == "__main__":
    asyncio.run(main())
