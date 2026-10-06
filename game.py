import traceback
from queue import Empty, Queue
from threading import Lock, RLock, Thread
from time import sleep
from core import (
    classify, can_a_defeat_b, gen_rand_cards,
    ComboType, cards_to_str, card_rank, card_suit, Rank,
    combo_name, cards_prompt, sort_hand
)
from netengine import PokerNode

HOST_ID = 0

_RANKS = ("3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "2")
_SUITS = ("♠", "♥", "♣", "♦")

RED_SUITS = (1, 3)

def card_label(card: int) -> str:
    """返回用于显示的两字符牌面标签，如 '♠3'、'♥10'"""
    return _SUITS[card_suit(card)] + _RANKS[card_rank(card) - 3]

class UIBridge:
    def __init__(self):
        self._q: Queue = Queue()
        self._handlers: dict[str, callable] = {}

    def bind(self, name: str, handler):
        self._handlers[name] = handler

    def emit(self, name: str, *args):
        self._q.put((name, args))

    def drain(self):
        while True:
            try:
                name, args = self._q.get_nowait()
            except Empty:
                return
            handler = self._handlers.get(name)
            if handler is None:
                continue
            try:
                handler(*args)
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def log(self, text: str):
        self.emit("log", text)

    def status(self, text: str):
        self.emit("status", text)

    def players(self, lines: list):
        self.emit("players", lines)

    def hand(self, cards: list, highlight=None):
        self.emit("hand", cards, highlight)

    def last_play(self, text: str, cards=None):
        self.emit("last_play", text, cards)

    def alert(self, title: str, msg: str):
        self.emit("alert", title, msg)

class GameHost:
    def __init__(self, ui: UIBridge, nickname: str,
                 room_name: str, max_players: int = 4):
        self.ui = ui
        self.nickname = nickname
        self.room_name = room_name
        self.max_players = max_players
        self.node = PokerNode(nickname)
        self.lock = RLock()

        self.players = {HOST_ID: nickname}
        self.ready = {HOST_ID}
        self.hands: dict[int, list[int]] = {}
        self.order: list[int] = []
        self.turn_idx = 0
        self.last_peer = None
        self.last_cards = None
        self.pass_count = 0
        self.finished: list[int] = []
        self.started = False
        self.ai_controlled: set[int] = set()

        self.phase = "waiting"          # waiting / tribute / playing
        self.round_number = 0
        self.prev_ranking: list[int] = []
        self.tribute_pending = None
        self._tribute_log: list[str] = []

    def start(self) -> int:
        port = self.node.host(self.room_name, max_players=self.max_players)
        self.node.on_peer_join = self._on_join
        self.node.on_peer_leave = self._on_leave
        self.node.on_message = self._on_message
        self.node.can_join = lambda: not self.started
        return port

    def close(self):
        try:
            self.node.close()
        except Exception:
            pass

    def start_game(self):
        if self.started:
            return
        Thread(target=self._start_game, daemon=True).start()

    def _on_join(self, pid: int, nick: str):
        with self.lock:
            if self.started:
                self.node.send({"type": "error", "msg": "游戏已开始"}, to_peer=pid)
                self.node._drop_client(pid)
                return
            self.players[pid] = nick
            self._sync_room()
            self.node.send({"type": "system", "text": f"欢迎 {nick}"}, to_peer=pid)
        self.ui.log(f"[{nick}({pid})] 加入房间")

    def _on_leave(self, pid: int):
        with self.lock:
            nick = self.players.get(pid)
            if self.started and nick is not None:
                self.ai_controlled.add(pid)
                self._maybe_ai_act()
                self.node.send({"type": "system",
                                "text": f"{nick} 已离线，由 AI 接管"})
                self.ui.log(f"[{nick}({pid})] 已离线，由 AI 接管")
                return
            self.players.pop(pid, None)
            self.ready.discard(pid)
            self._sync_room()
            self.prev_ranking = []

    def _on_message(self, pid: int, msg: dict):
        t = msg.get("type")
        if t == "ready":
            with self.lock:
                self.ready.add(pid)
                self._sync_room()
            self.ui.log(f"[{self.players.get(pid)}] 已准备")
        elif t == "play" and self.phase == "playing":
            self._try_play(pid, msg.get("cards", []))
        elif t == "pass" and self.phase == "playing":
            self._try_pass(pid)
        elif t == "tribute_return" and self.phase == "tribute":
            self._try_tribute_return(pid, msg.get("card"))
        elif t == "chat":
            text = str(msg.get("text", "")).strip()[:200]
            if text:
                self._broadcast_chat(pid, text)

    def handle_input(self, cmd: str):
        cmd = (cmd or "").strip()
        if not cmd:
            return
        parts = cmd.split()
        c = parts[0].lower()
        args = parts[1:]

        if c == "chat": # ← 任何阶段都能聊天
            text = cmd[5:].strip()
            if text:
                self._broadcast_chat(HOST_ID, text)
            return

        if self.phase == "waiting":
            if c == "start" and not self.started:
                self._start_game()
            elif c == "win":
                self._set_winner(args)
            else:
                self.ui.log("输入 start 开始，win 设置赢家")
        elif self.phase == "tribute":
            if self.tribute_pending and self.tribute_pending["head"] == HOST_ID:
                self._handle_tribute_return_input(cmd)
        elif self.phase == "playing":
            with self.lock:
                if not self.order or self.order[self.turn_idx] != HOST_ID:
                    self.ui.log("还没轮到你")
                    return
                self._handle_play_cmd(HOST_ID, cmd)

    def _broadcast_chat(self, pid: int, text: str):
        nick = self.players.get(pid, "?")
        self.node.send({"type": "chat", "peer_id": pid,
                        "nick": nick, "text": text})
        who = "你" if pid == HOST_ID else nick
        self.ui.log(f"<{who}> {text}")

    def _set_winner(self, args):
        if not args:
            self.ui.log("用法: win <头客id> <大拉id>  或  win clear")
            return
        if args[0].lower() == "clear":
            self.prev_ranking = []
            self.ui.log("[win] 已清空上局排名，本局将按红桃 5 先出")
            return
        try:
            ids = [int(x) for x in args]
        except ValueError:
            self.ui.log("id 必须是整数")
            return
        if len(ids) != 2 or len(set(ids)) != 2:
            self.ui.log("必须指定 2 个不同的 id（头客、大拉）")
            return
        for i in ids:
            if i not in self.players:
                self.ui.log(f"id {i} 不在房间中")
                return
        self.prev_ranking = ids
        self.ui.log(f"[提示] 头客 {self.players[ids[0]]} 收贡，"
                    f"大拉 {self.players[ids[-1]]} 进贡")

    def _sync_room(self):
        info = [{"peer_id": p, "nick": n, "ready": p in self.ready}
                for p, n in self.players.items()]
        self.node.send({"type": "room", "players": info, "host": HOST_ID})
        self._render_state()

    def _start_game(self):
        if len(self.players) < 2:
            self.ui.log("至少需要 2 名玩家")
            self.started = False
            return

        self._tribute_log = []
        n = len(self.players)
        pids = [HOST_ID] + [p for p in self.players if p != HOST_ID]
        hands, first = gen_rand_cards(n)

        self.order = pids
        self.hands = {pids[i]: hands[i] for i in range(n)}
        self.last_peer = None
        self.last_cards = None
        self.pass_count = 0
        self.finished = []
        self.started = True
        self.round_number += 1
        self.ui.emit("game_started")

        for p in pids:
            if p != HOST_ID:
                self.node.send({"type": "hand", "cards": self.hands[p]}, to_peer=p)

        self.ui.log(f"=== 第 {self.round_number} 局 ===")
        for p in pids:
            self.ui.log(f"  {self.players[p]}")

        if self.prev_ranking:
            head, tail = self.prev_ranking[0], self.prev_ranking[-1]
            if head in self.players and tail in self.players:
                self._do_tribute(head, tail)
                # self.ui.hand(self.hands[HOST_ID])
                return
            self.prev_ranking = []

        self._begin_play(pids[first])
        self.ui.hand(self.hands[HOST_ID])

    def _do_tribute(self, head: int, tail: int):
        tail_hand = self.hands[tail]
        twos = [c for c in tail_hand if card_rank(c) == Rank.TWO]

        if not twos:
            self._tribute_log.append(f"[进贡] {self.players[tail]} 没有 2，进贡失败")
            self._tribute_log.append(f"[进贡] {self.players[head]} 先出")
            self.node.send({"type": "tribute", "success": False,
                            "head": head, "tail": tail})
            self.ui.log(f"[进贡] {self.players[tail]} 没有 2，进贡失败")
            self.ui.log(f"[进贡] {self.players[head]} 先出")
            self.ui.hand(self.hands[HOST_ID])
            self._begin_play(head)
            return

        two = twos[0]
        tail_hand.remove(two)
        self.hands[head].append(two)
        sort_hand(self.hands[head])

        self._tribute_log.append(
            f"[进贡] {self.players[tail]} → {self.players[head]}: "
            f"{cards_to_str([two])}")
        self.node.send({"type": "tribute", "success": True,
                        "head": head, "tail": tail, "card": two})
        if tail != HOST_ID:
            self.node.send({"type": "hand", "cards": self.hands[tail]}, to_peer=tail)
        self.ui.log(f"[进贡] {self.players[tail]} → {self.players[head]}: "
                    f"{cards_to_str([two])}")

        self.tribute_pending = {"head": head, "tail": tail, "received": two}
        self.phase = "tribute"

        if head == HOST_ID:
            self.ui.log(f"你收到进贡。请选一张牌还给 {self.players[tail]}")
            self.ui.status("请选一张牌还贡 ...")
            self.ui.hand(self.hands[HOST_ID], highlight=[two])
        else:
            self.node.send({"type": "tribute_choose", "tail": tail,
                            "received": two,
                            "cards": self.hands[head],
                            "highlight": [two]},
                           to_peer=head)
            self.ui.status(f"等待 {self.players[head]} 还贡 ...")
            if tail == HOST_ID:
                # host 是大拉：手牌少了一张 2，刷新一下
                self.ui.hand(self.hands[HOST_ID])
            self._maybe_ai_act()

    def _handle_tribute_return_input(self, cmd: str):
        try:
            idx = int(cmd.strip())
        except ValueError:
            self.ui.log("只能选一张牌")
            return
        hand = self.hands[HOST_ID]
        if idx < 0 or idx >= len(hand):
            self.ui.log("索引无效")
            return
        card = hand[idx]
        if card_rank(card) == Rank.TWO:
            self.ui.alert("不能还 2", "不能把 2 还回去，请另选一张牌")
            return
        self._finish_tribute(HOST_ID, card)

    def _try_tribute_return(self, pid: int, card):
        with self.lock:
            if not self.tribute_pending:
                return
            if pid != self.tribute_pending["head"]:
                return
            if card is None or card not in self.hands[pid]:
                self._err(pid, "无效的还牌")
                return
            if card_rank(card) == Rank.TWO:
                self.node.send({"type": "alert",
                                "title": "不能还 2",
                                "msg": "不能把 2 还回去，请另选一张牌"},
                               to_peer=pid)
                return
            self._finish_tribute(pid, card)

    def _finish_tribute(self, head: int, card: int):
        with self.lock:
            tail = self.tribute_pending["tail"]
            self.hands[head].remove(card)
            self.hands[tail].append(card)
            sort_hand(self.hands[tail])

            self._tribute_log.append(
                f"[进贡] {self.players[head]} → {self.players[tail]}: "
                f"{cards_to_str([card])}")
            self.node.send({"type": "tribute_done",
                            "head": head, "tail": tail, "card": card})
            self.ui.log(f"[进贡] {self.players[head]} → {self.players[tail]}: "
                        f"{cards_to_str([card])}")

            if head != HOST_ID:
                self.node.send(
                    {"type": "hand", "cards": self.hands[head]}, to_peer=head)

            if tail != HOST_ID:
                self.node.send(
                    {
                        "type": "hand",
                        "cards": self.hands[tail],
                        "highlight": [card]
                    },
                    to_peer=tail)

            self.tribute_pending = None
            self.last_peer = None
            self.last_cards = None
            self.pass_count = 0
            if tail == HOST_ID:
                self.ui.hand(self.hands[HOST_ID], highlight=[card])
            else:
                self.ui.hand(self.hands[HOST_ID])
            self._begin_play(tail)

    def _begin_play(self, first_pid: int):
        self.phase = "playing"
        self.last_peer = None
        self.last_cards = None
        self.pass_count = 0
        self.turn_idx = self.order.index(first_pid)

        tribute_log = self._tribute_log
        self._tribute_log = []

        self.node.send({"type": "start", "order": self.order,
                        "round": self.round_number, "first": first_pid,
                        "tribute": tribute_log})

        self.ui.log(f"{self.players[first_pid]} 先出")
        self.node.send({"type": "turn", "peer_id": first_pid,
                        "must_follow": False})
        self._print_turn_prompt(first_pid)
        self._maybe_ai_act()

    def _handle_play_cmd(self, pid: int, cmd: str):
        if cmd.strip().lower() == "p":
            self._try_pass(pid)
            return
        try:
            idxs = [int(x) for x in cmd.split()]
        except ValueError:
            self.ui.log("无效输入")
            return
        hand = self.hands[pid]
        if (not idxs or any(i < 0 or i >= len(hand) for i in idxs)
                or len(set(idxs)) != len(idxs)):
            self.ui.log("索引无效")
            return
        self._try_play(pid, [hand[i] for i in idxs])

    def _try_play(self, pid: int, cards: list[int]):
        with self.lock:
            if self.phase != "playing":
                return
            if self.order[self.turn_idx] != pid:
                return self._err(pid, "还没轮到你")
            hand = self.hands[pid]
            if not cards:
                return self._err(pid, "没有选牌")
            if len(set(cards)) != len(cards) or not all(c in hand for c in cards):
                return self._err(pid, "你没有这些牌")
            typ, _std = classify(cards)
            if typ == ComboType.UNKNOWN:
                return self._err(pid, "牌型不合法")
            if self.last_cards is not None and not can_a_defeat_b(cards, self.last_cards):
                return self._err(pid, "压不过上一手")

            for c in cards:
                hand.remove(c)
            if pid == HOST_ID:
                self.ui.hand(self.hands[HOST_ID])

            self.last_peer = pid
            self.last_cards = cards
            self.pass_count = 0

            self.node.send({"type": "played", "peer_id": pid, "cards": cards})
            who = "你" if pid == HOST_ID else self.players[pid]
            self.ui.log(f"{who} 出牌 [{combo_name(cards)}]: {cards_to_str(cards)}")

            if not hand:
                self.finished.append(pid)
                self.node.send({"type": "finished", "peer_id": pid})
                self.ui.log(f"*** {self.players[pid]} 出完了！***")
                if len(self.finished) >= len(self.players) - 1:
                    return self._end_game()
            self._advance()

    def _try_pass(self, pid: int):
        with self.lock:
            if self.phase != "playing":
                return
            if self.order[self.turn_idx] != pid:
                return self._err(pid, "还没轮到你")
            if self.last_cards is None:
                return self._err(pid, "自由出牌不能过")

            self.pass_count += 1
            self.node.send({"type": "passed", "peer_id": pid})
            who = "你" if pid == HOST_ID else self.players[pid]
            self.ui.log(f"{who} 过牌")

            if (self.last_peer is not None
                    and self.last_peer in self.finished
                    and self._is_immediate_downstream(pid)):
                self.ui.log(f"[接风] {self.players.get(pid)} 过牌，下家自由出牌")
                self._start_new_round_after(pid)
                return

            if self.last_peer is not None:
                active = [p for p in self.order if p not in self.finished]
                others = [p for p in active if p != self.last_peer]
                if self.pass_count >= len(others):
                    self.last_cards = None
                    self.pass_count = 0
                    self.node.send({"type": "new_round"})
                    self.turn_idx = self.order.index(self.last_peer)
                    self.last_peer = None
                    cur = self.order[self.turn_idx]
                    self.node.send({"type": "turn", "peer_id": cur,
                                    "must_follow": False})
                    self._print_turn_prompt(cur)
                    self._maybe_ai_act()
                    return

            self._advance()

    def _start_new_round_after(self, last_peer: int):
        n = len(self.order)
        lp = self.order.index(last_peer)
        for k in range(1, n + 1):
            ni = (lp + k) % n
            if self.order[ni] not in self.finished:
                self.turn_idx = ni
                break
        self.last_cards = None
        self.last_peer = None
        self.pass_count = 0
        self.node.send({"type": "new_round"})
        cur = self.order[self.turn_idx]
        self.node.send({"type": "turn", "peer_id": cur, "must_follow": False})
        self._print_turn_prompt(cur)
        self._maybe_ai_act()

    def _advance(self):
        n = len(self.order)
        for _ in range(n):
            self.turn_idx = (self.turn_idx + 1) % n
            if self.order[self.turn_idx] not in self.finished:
                break
        cur = self.order[self.turn_idx]
        must = self.last_cards is not None
        self.node.send({"type": "turn", "peer_id": cur, "must_follow": must})
        self._print_turn_prompt(cur)
        self._maybe_ai_act()

    def _is_immediate_downstream(self, pid: int) -> bool:
        if self.last_peer is None:
            return False
        lp_idx = self.order.index(self.last_peer)
        n = len(self.order)
        for k in range(1, n + 1):
            ni = (lp_idx + k) % n
            if self.order[ni] not in self.finished:
                return self.order[ni] == pid
        return False

    def _end_game(self):
        for p in self.order:
            if p not in self.finished:
                self.finished.append(p)

        self.prev_ranking = self.finished.copy()
        self.node.send({"type": "over", "order": self.finished,
                        "round": self.round_number})
        names = [self.players[p] for p in self.finished]
        self.ui.log(f"=== 第 {self.round_number} 局结束 ===")
        self.ui.log(f"排名: {' > '.join(names)}")
        self.ui.status("本局结束，正在准备下一局...")

        self.last_cards = None
        self.last_peer = None
        self.pass_count = 0

        Thread(target=self._delayed_restart, daemon=True).start()

    def _delayed_restart(self):
        sleep(1.2)
        self._start_game()

    def _err(self, pid: int, msg: str):
        if pid == HOST_ID:
            self.ui.log(f"[错误] {msg}")
        else:
            self.node.send({"type": "error", "msg": msg}, to_peer=pid)

    def _render_state(self):
        lines = []
        if self.phase == "playing" and self.order:
            for p in self.order:
                name = self.players.get(p, "?")
                marks = []
                if p == HOST_ID:
                    marks.append("你")
                if p in self.finished:
                    marks.append("已出完")
                elif p == self.order[self.turn_idx]:
                    marks.append("当前")
                tag = f" [{'/'.join(marks)}]" if marks else ""
                lines.append(f"{name}{tag}")
        else:
            for p, name in self.players.items():
                tag = " [你]" if p == HOST_ID else ""
                lines.append(f"{name}{tag}")

        self.ui.players(lines)

        if self.phase == "playing":
            if self.last_cards is not None:
                who = "你" if self.last_peer == HOST_ID \
                    else self.players.get(self.last_peer, "?")
                self.ui.last_play(
                    f"上一手  {who} 出 [{combo_name(self.last_cards)}]: ",
                    self.last_cards)
            else:
                self.ui.last_play("上一手  无（自由出牌）")

    def _print_turn_prompt(self, pid: int):
        self._render_state()
        if pid == HOST_ID:
            must = "必须压过上一手" if self.last_cards else "自由出牌"
            self.ui.status(f"轮到你（{must}）")
        else:
            self.ui.status(f"等待 {self.players[pid]} 出牌...")

    def _maybe_ai_act(self):
        if self.phase == "tribute":
            pend = self.tribute_pending
            if pend and pend["head"] in self.ai_controlled:
                Thread(target=self._ai_tribute_return,
                       args=(pend["head"],), daemon=True).start()
        elif self.phase == "playing":
            if not self.order:
                return
            cur = self.order[self.turn_idx]
            if cur in self.ai_controlled:
                Thread(target=self._ai_play, args=(cur,), daemon=True).start()

    def _ai_play(self, pid: int):
        sleep(0.8)
        with self.lock:
            if self.phase != "playing":
                return
            if not self.order or self.order[self.turn_idx] != pid:
                return
            if pid not in self.ai_controlled:
                return
            hand = self.hands.get(pid, [])
            if not hand:
                return
            if self.last_cards is None:
                card = min(hand, key=lambda c: card_rank(c))
                self._try_play(pid, [card])
            else:
                self._try_pass(pid)

    def _ai_tribute_return(self, pid: int):
        sleep(0.8)
        with self.lock:
            if self.phase != "tribute":
                return
            pend = self.tribute_pending
            if not pend or pend["head"] != pid:
                return
            if pid not in self.ai_controlled:
                return
            
            hand = self.hands.get(pid, [])
            cand = [c for c in hand if card_rank(c) != Rank.TWO]
            if not cand:
                return
            card = min(cand, key=lambda c: card_rank(c))
            self._finish_tribute(pid, card)

class GameClient:
    def __init__(self, ui: UIBridge, nickname: str,
                 host_ip: str, port: int):
        self.ui = ui
        self.nickname = nickname
        self.node = PokerNode(nickname)
        self.host_ip = host_ip
        self.port = port

        self.lock = Lock()
        self.hand: list[int] = []
        self.players: dict[int, str] = {}
        self.my_id: int | None = None
        self.started = False
        self.turn: int | None = None
        self.must_follow = False
        self.phase = "waiting"
        self.tribute_pending = None
        self.finished: set[int] = set()
        self.last_cards = None
        self.last_peer = None

    def start(self):
        self.node.on_message = self._on_message
        self.node.on_disconnect = lambda: self.ui.log("[断开] 与房主失去连接")
        self.my_id = self.node.join(self.host_ip, self.port)
        self.ui.log(f"已连接，我是 {self.nickname}（id={self.my_id}）")
        self.node.send({"type": "ready"})
        self.ui.log("已准备，等待房主开局...")

    def close(self):
        try:
            self.node.close()
        except Exception:
            pass

    def _on_message(self, _pid, msg: dict):
        t = msg.get("type")

        if t == "room":
            self.players = {p["peer_id"]: p["nick"] for p in msg["players"]}
            self._render_state()
            self.ui.log("[房间]")
            for p in msg["players"]:
                me = " (你)" if p["peer_id"] == self.my_id else ""
                r = "✓" if p["ready"] else "…"
                self.ui.log(f"  [{r}] {p['nick']}{me}")

        elif t == "chat": # ← 新增
            who = "你" if msg["peer_id"] == self.my_id \
                else self.players.get(msg["peer_id"], "?")
            self.ui.log(f"<{who}> {msg['text']}")

        elif t == "system":
            self.ui.log(f"[系统] {msg['text']}")

        elif t == "hand":
            with self.lock:
                self.hand = list(msg["cards"])
            self.ui.hand(self.hand, highlight=msg.get("highlight"))
            self.ui.log("手牌已更新")

        elif t == "tribute_choose":
            with self.lock:
                self.hand = list(msg["cards"])
            self.phase = "tribute"
            self.tribute_pending = {"tail": msg["tail"],
                                    "received": msg["received"]}
            self.ui.hand(self.hand, highlight=msg.get("highlight"))
            self.ui.log(f"[进贡] 你收到 {cards_to_str([msg['received']])}，"
                        f"请选一张牌还给 "
                        f"{self.players.get(msg['tail'], '?')}")
            self.ui.status("请选一张牌还贡")

        elif t == "tribute":
            if msg["success"]:
                who = self.players.get(msg["head"], "?")
                tail = self.players.get(msg["tail"], "?")
                self.ui.log(f"[进贡] {tail} → {who}: {cards_to_str([msg['card']])}")
            else:
                self.ui.log(f"[进贡] 失败，{self.players.get(msg['head'], '?')} 先出")

        elif t == "tribute_done":
            head = self.players.get(msg["head"], "?")
            tail = self.players.get(msg["tail"], "?")
            self.ui.log(f"[进贡] {head} → {tail}: {cards_to_str([msg['card']])}")
            self.phase = "waiting"

        elif t == "alert":
            self.ui.alert(msg.get("title", "提示"), msg.get("msg", ""))

        elif t == "start":
            self.started = True
            self.phase = "playing"
            self.turn = None
            self.last_cards = None
            self.last_peer = None
            self.finished = set()
            self._render_state()
            self.ui.log(f"=== 第 {msg['round']} 局开始 ===")
            for line in msg.get("tribute", []):
                self.ui.log(line)

        elif t == "turn":
            self.turn = msg["peer_id"]
            self.must_follow = msg.get("must_follow", False)
            self._render_state()
            if self.turn == self.my_id:
                must = "必须压过上一手" if self.must_follow else "自由出牌"
                self.ui.status(f"轮到你（{must}）")
            else:
                self.ui.status(f"等待 {self.players.get(self.turn, '?')} 出牌...")

        elif t == "played":
            self.last_cards = msg["cards"]
            self.last_peer = msg["peer_id"]
            who = "你" if msg["peer_id"] == self.my_id \
                else self.players.get(msg["peer_id"], "?")
            self.ui.log(f"{who} 出牌 [{combo_name(msg['cards'])}]: "
                        f"{cards_to_str(msg['cards'])}")
            if msg["peer_id"] == self.my_id:
                with self.lock:
                    for c in msg["cards"]:
                        if c in self.hand:
                            self.hand.remove(c)
                self.ui.hand(self.hand)
            self._render_state()

        elif t == "passed":
            who = "你" if msg["peer_id"] == self.my_id \
                else self.players.get(msg["peer_id"], "?")
            self.ui.log(f"{who} 过牌")

        elif t == "finished":
            self.finished.add(msg["peer_id"])
            who = "你" if msg["peer_id"] == self.my_id \
                else self.players.get(msg["peer_id"], "?")
            self.ui.log(f"*** {who} 出完了！***")
            self._render_state()

        elif t == "over":
            names = [self.players.get(p, "?") for p in msg["order"]]
            rnd = msg.get("round", "?")
            self.ui.log(f"=== 第 {rnd} 局结束 ===")
            self.ui.log(f"排名: {' > '.join(names)}")
            self.started = False
            self.phase = "waiting"
            self.last_cards = None
            self.last_peer = None
            self.finished = set()

        elif t == "error":
            self.ui.log(f"[错误] {msg['msg']}")

        elif t == "peer_join":
            self.ui.log(f"[{msg['nick']}] 加入")

        elif t == "peer_leave":
            self.ui.log(f"[{self.players.get(msg['peer_id'], '?')}] 离开")

        elif t == "new_round":
            self.last_cards = None
            self.last_peer = None
            self.ui.log("[新一轮]")

    def handle_input(self, cmd: str):
        cmd = (cmd or "").strip()
        if not cmd:
            return

        if cmd.startswith("chat "): # 放在最前，避免被还贡分支吃掉
            text = cmd[5:].strip()
            if text:
                self.node.send({"type": "chat", "text": text})
            return

        if self.phase == "tribute" and self.tribute_pending:
            try:
                idx = int(cmd)
            except ValueError:
                self.ui.log("只能选一张牌")
                return
            with self.lock:
                if idx < 0 or idx >= len(self.hand):
                    self.ui.log("索引无效")
                    return
                card = self.hand[idx]
            if card_rank(card) == Rank.TWO:
                self.ui.alert("不能还 2", "不能把 2 还回去，请另选一张牌")
                return
            self.node.send({"type": "tribute_return", "card": card})
            self.phase = "waiting"
            self.tribute_pending = None
            return

        if not self.started:
            if cmd == "ready":
                self.node.send({"type": "ready"})
            return

        if self.turn != self.my_id:
            self.ui.log("还没轮到你")
            return

        if cmd.lower() == "p":
            self.node.send({"type": "pass"})
            return

        try:
            idxs = [int(x) for x in cmd.split()]
        except ValueError:
            self.ui.log("无效输入")
            return
        with self.lock:
            if (not idxs
                    or any(i < 0 or i >= len(self.hand) for i in idxs)
                    or len(set(idxs)) != len(idxs)):
                self.ui.log("索引无效")
                return
            cards = [self.hand[i] for i in idxs]
        self.node.send({"type": "play", "cards": cards})

    def _render_state(self):
        lines = []
        for p, n in self.players.items():
            marks = []
            if p == self.my_id:
                marks.append("你")
            if p in self.finished:
                marks.append("已出完")
            elif p == self.turn and self.started:
                marks.append("当前")
            tag = f" [{'/'.join(marks)}]" if marks else ""
            lines.append(f"{n}{tag}")

        self.ui.players(lines)

        if self.started:
            if self.last_cards is not None:
                who = "你" if self.last_peer == self.my_id \
                    else self.players.get(self.last_peer, "?")
                self.ui.last_play(
                    f"上一手  {who} 出 [{combo_name(self.last_cards)}]: ",
                    self.last_cards)
            else:
                self.ui.last_play("上一手  无（自由出牌）")
