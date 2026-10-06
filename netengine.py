from json import JSONDecodeError, dumps, loads
from socket import (
    AF_INET, IPPROTO_TCP, SO_BROADCAST, SO_KEEPALIVE, SO_REUSEADDR,
    SOCK_DGRAM, SOCK_STREAM, SOL_SOCKET, TCP_KEEPCNT, TCP_KEEPIDLE,
    TCP_KEEPINTVL, TCP_NODELAY, socket, timeout as TimeoutError)
from threading import Lock, Thread
from time import sleep, time
from typing import Callable, Optional

DISCOVERY_PORT = 29999
DISCOVERY_MAGIC = "UPSTREAM_POKER_ROOM"
TCP_BIND_HOST = "0.0.0.0"
RECV_SIZE = 4096
MAX_MSG_SIZE = 1 << 20  # 1 MB

def _pack(msg: dict) -> bytes:
    return (dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")

class _LineReader:
    def __init__(self, sock: socket):
        self.sock = sock
        self.buf = bytearray()

    def read(self) -> Optional[dict]:
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(RECV_SIZE)
            except OSError:
                return None
            if not chunk:
                return None
            self.buf.extend(chunk)
            if len(self.buf) > MAX_MSG_SIZE:
                return None
        line, _, rest = self.buf.partition(b"\n")
        self.buf = bytearray(rest)
        try:
            return loads(line.decode("utf-8"))
        except (JSONDecodeError, UnicodeDecodeError):
            return None

class PokerNode:
    def __init__(self, nickname: str):
        self.nickname = nickname
        self.role: Optional[str] = None            # "host" / "client"
        self.room_name: Optional[str] = None

        # 房主状态
        self.port: Optional[int] = None
        self._server_sock: Optional[socket] = None
        self._clients: dict[int, dict] = {}        # peer_id -> {sock, reader, nick}
        self._next_peer_id = 1
        self._clients_lock = Lock()
        self._max_players = 4
        self.can_join: Optional[Callable[[], bool]] = None

        # 客户端状态
        self._conn: Optional[socket] = None
        self._conn_reader: Optional[_LineReader] = None
        self._my_peer_id: Optional[int] = None

        # UDP 发现
        self._udp_sock: Optional[socket] = None

        # 回调(可选)
        self.on_message: Optional[Callable[[int, dict], None]] = None
        self.on_peer_join: Optional[Callable[[int, str], None]] = None
        self.on_peer_leave: Optional[Callable[[int], None]] = None
        self.on_connected: Optional[Callable[[int], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None

        self._running = False

    def host(self, room_name: str, port: int = 0, max_players: int = 4) -> int:
        """启动房主。port=0 表示让系统随机分配。返回实际监听端口。"""
        if self.role is not None:
            raise RuntimeError("节点已启动, 不能重复 host/join")
        self.role = "host"
        self.room_name = room_name
        self._max_players = max_players

        self._server_sock = socket(AF_INET, SOCK_STREAM)
        self._server_sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        self._server_sock.bind((TCP_BIND_HOST, port))
        self._server_sock.listen(8)
        self.port = self._server_sock.getsockname()[1]
        self._running = True

        Thread(target=self._accept_loop, daemon=True).start()
        self._start_udp_responder()
        return self.port

    def _accept_loop(self):
        while self._running:
            try:
                sock, _addr = self._server_sock.accept()
            except OSError:
                break
            sock.setsockopt(IPPROTO_TCP, TCP_NODELAY, 1)
            try:
                sock.setsockopt(SOL_SOCKET, SO_KEEPALIVE, 1)
                sock.setsockopt(IPPROTO_TCP, TCP_KEEPIDLE, 30)   # 空闲 30s 开始探测
                sock.setsockopt(IPPROTO_TCP, TCP_KEEPINTVL, 5)   # 每 5s 一次
                sock.setsockopt(IPPROTO_TCP, TCP_KEEPCNT, 3)     # 3 次失败即断开
            except OSError:
                pass
            Thread(
                target=self._serve_client, args=(sock,), daemon=True
            ).start()

    def _serve_client(self, sock: socket):
        reader = _LineReader(sock)
        first = reader.read()
        if not first or first.get("type") != "join":
            sock.close()
            return
        nick = str(first.get("nick", "")).strip()[:32] or "匿名玩家"

        if self.can_join is not None and not self.can_join():
            try:
                sock.sendall(_pack({"type": "join_reject", "reason": "游戏已开始"}))
            except OSError:
                pass
            sock.close()
            return

        with self._clients_lock:
            if len(self._clients) + 1 >= self._max_players:
                try:
                    sock.sendall(_pack({"type": "join_reject", "reason": "房间已满"}))
                except OSError:
                    pass
                sock.close()
                return
            peer_id = self._next_peer_id
            self._next_peer_id += 1
            self._clients[peer_id] = {"sock": sock, "reader": reader, "nick": nick}

        # 通知新人
        self._send_to(peer_id, {
            "type": "join_ok",
            "peer_id": peer_id,
            "room": self.room_name,
            "nick": nick,
        })
        # 通知老玩家
        self._broadcast({"type": "peer_join", "peer_id": peer_id, "nick": nick},
                        exclude=peer_id)
        if self.on_peer_join:
            self.on_peer_join(peer_id, nick)

        # 读循环
        try:
            while self._running:
                msg = reader.read()
                if msg is None:
                    break
                if self.on_message:
                    self.on_message(peer_id, msg)
        finally:
            self._drop_client(peer_id)

    def _drop_client(self, peer_id: int):
        with self._clients_lock:
            info = self._clients.pop(peer_id, None)
        if not info:
            return
        try:
            info["sock"].close()
        except OSError:
            pass
        self._broadcast({"type": "peer_leave", "peer_id": peer_id})
        if self.on_peer_leave:
            self.on_peer_leave(peer_id)

    def _send_to(self, peer_id: int, msg: dict) -> bool:
        with self._clients_lock:
            info = self._clients.get(peer_id)
        if not info:
            return False
        try:
            info["sock"].sendall(_pack(msg))
            return True
        except OSError:
            self._drop_client(peer_id)
            return False

    def _broadcast(self, msg: dict, exclude: Optional[int] = None):
        with self._clients_lock:
            ids = list(self._clients.keys())
        for pid in ids:
            if pid == exclude:
                continue
            self._send_to(pid, msg)

    def list_players(self) -> list[dict]:
        """房主用: 返回当前玩家列表"""
        with self._clients_lock:
            return [{"peer_id": pid, "nick": info["nick"]}
                    for pid, info in self._clients.items()]

    def join(self, host_ip: str, port: int) -> int:
        """加入房间, 返回自己的 peer_id。"""
        if self.role is not None:
            raise RuntimeError("节点已启动, 不能重复 host/join")
        self.role = "client"

        self._conn = socket(AF_INET, SOCK_STREAM)
        self._conn.setsockopt(IPPROTO_TCP, TCP_NODELAY, 1)
        self._conn.connect((host_ip, port))
        self._conn_reader = _LineReader(self._conn)

        self._send_raw({"type": "join", "nick": self.nickname})

        first = self._conn_reader.read()
        if not first or first.get("type") != "join_ok":
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
            if isinstance(first, dict) and first.get("type") == "join_reject":
                raise ConnectionError(f"加入房间失败：{first.get('reason', '被拒绝')}")
            raise ConnectionError(f"加入房间失败: {first}")

        self._my_peer_id = first["peer_id"]
        self.room_name = first.get("room")
        self._running = True

        if self.on_connected:
            self.on_connected(self._my_peer_id)

        Thread(target=self._client_recv_loop, daemon=True).start()
        return self._my_peer_id

    def _client_recv_loop(self):
        try:
            while self._running:
                msg = self._conn_reader.read()
                if msg is None:
                    break
                if self.on_message:
                    self.on_message(self._my_peer_id or 0, msg)
        finally:
            self._running = False
            if self.on_disconnect:
                self.on_disconnect()

    def send(self, msg: dict, to_peer: Optional[int] = None):
        """房主: to_peer 指定则单发, 否则广播; 客户端: 发给房主。"""
        if self.role == "host":
            if to_peer is None:
                self._broadcast(msg)
            else:
                self._send_to(to_peer, msg)
        elif self.role == "client":
            self._send_raw(msg)
        else:
            raise RuntimeError("节点尚未启动")

    def _send_raw(self, msg: dict):
        if not self._conn:
            return
        try:
            self._conn.sendall(_pack(msg))
        except OSError:
            self._running = False

    def _start_udp_responder(self):
        sock = socket(AF_INET, SOCK_DGRAM)
        sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        sock.setsockopt(SOL_SOCKET, SO_BROADCAST, 1)
        try:
            sock.bind(("", DISCOVERY_PORT))
        except OSError:
            sock.close()
            return
        self._udp_sock = sock
        Thread(target=self._udp_respond_loop, daemon=True).start()

    def _udp_respond_loop(self):
        while self._running:
            try:
                data, addr = self._udp_sock.recvfrom(1024)
            except OSError:
                break
            try:
                msg = loads(data.decode("utf-8"))
            except Exception:
                continue
            if msg.get("magic") != DISCOVERY_MAGIC:
                continue
            if msg.get("cmd") != "discover":
                continue
            if self.can_join is not None and not self.can_join():
                continue
            with self._clients_lock:
                cur = len(self._clients) + 1
            reply = {
                "magic": DISCOVERY_MAGIC,
                "cmd": "room_info",
                "room": self.room_name,
                "port": self.port,
                "players": cur,
                "max_players": self._max_players,
            }
            try:
                self._udp_sock.sendto(_pack(reply), addr)
            except OSError:
                pass

    @staticmethod
    def scan_rooms(timeout: float = 1.5, quiet: float = 0.5) -> list[dict]:
        """局域网搜索房间。

        - timeout: 总等待上限(秒)
        - quiet:   收到最近一个响应后, 若安静这么久仍无新响应, 提前返回
        返回 [{room, host, port, players, max_players}, ...]
        """
        sock = socket(AF_INET, SOCK_DGRAM)
        sock.setsockopt(SOL_SOCKET, SO_BROADCAST, 1)
        sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        try:
            sock.bind(("", 0))
        except OSError:
            pass

        req = dumps({"magic": DISCOVERY_MAGIC, "cmd": "discover"}).encode("utf-8")
        try:
            sock.sendto(req, ("255.255.255.255", DISCOVERY_PORT))
        except OSError:
            pass

        rooms: dict[tuple, dict] = {}
        end = time() + timeout
        last_seen: Optional[float] = None

        while True:
            now = time()
            if now >= end:
                break
            if last_seen is not None and now - last_seen >= quiet:
                break

            # 计算本次 recv 该等多久
            if last_seen is None:
                wait = end - now # 还没收到任何东西, 等满剩余
            else:
                wait = min(quiet - (now - last_seen), end - now)
            wait = max(wait, 0.01)

            sock.settimeout(wait)
            try:
                data, addr = sock.recvfrom(1024)
            except TimeoutError:
                continue
            except OSError:
                break

            last_seen = time() # 重置静默计时
            try:
                msg = loads(data.decode("utf-8"))
            except Exception:
                continue
            if msg.get("magic") != DISCOVERY_MAGIC:
                continue
            if msg.get("cmd") != "room_info":
                continue
            key = (addr[0], msg.get("port"))
            rooms[key] = {
                "room": msg.get("room"),
                "host": addr[0],
                "port": msg.get("port"),
                "players": msg.get("players", 0),
                "max_players": msg.get("max_players", 0),
            }

        sock.close()
        return list(rooms.values())
    
    def close(self):
        self._running = False
        with self._clients_lock:
            infos = list(self._clients.values())
            self._clients.clear()
        for info in infos:
            try:
                info["sock"].close()
            except OSError:
                pass

        for s in (self._server_sock, self._conn, self._udp_sock):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        self._server_sock = None
        self._conn = None
        self._udp_sock = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

if __name__ == "__main__":
    # 房主端
    host = PokerNode("房主")
    port = host.host("周末牌局", max_players=4)
    host.on_message = lambda pid, msg: print("来自", host._clients[pid]["nick"], ":", msg)
    host.on_peer_join = lambda pid, nick: (
        print(nick, "加入：id =", pid),
        host.send({"type": "system", "text": f"欢迎 {nick}"}, to_peer=pid),
    )

    # 客户端
    rooms = PokerNode.scan_rooms()
    print("发现房间:", rooms)
    cli = PokerNode("小王")
    cli.on_message = lambda pid, msg: print("房主说:", msg)
    cli.join(rooms[0]["host"], rooms[0]["port"])
    cli.send({"type": "ready"})

    while True:
        sleep(2)
