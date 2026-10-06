import json
import os
import threading
import tkinter as tk
from tkinter import StringVar
import ttkbootstrap as tb
from ttkbootstrap.dialogs import Messagebox
from card_render import darken_color, draw_card
from game import GameClient, GameHost, UIBridge, HOST_ID

APP_TITLE = "烟威争上游"
THEME = "flatly"

CARD_AREA_BG = "#EAEAEA"

class HandCanvas(tb.Canvas):
    CARD_W      = 200
    CARD_H      = 280
    LIFT        = 16     # 选中时的竖直偏移
    MARGIN_X    = 14
    MARGIN_TOP  = 6
    MARGIN_BOT  = 6
    STEP    = 64

    def __init__(self, master, *, on_selection_changed=None, **kw):
        super().__init__(master, highlightthickness=0, bd=0, **kw)
        self._cards: list[int] = []
        self._selected: set[int] = set()
        self._hover: int | None = None
        self._layout: list[tuple[int, int, int, int]] = []
        self._hitboxes: list[tuple[int, int, int, int]] = []
        self._on_sel_changed = on_selection_changed
        self._shadow = darken_color(self.cget("bg"), 0.82)

        # 拖动选牌状态
        self._dragging = False
        self._drag_done: set[int] = set()

        self.bind("<Configure>",       lambda e: self._redraw())
        self.bind("<Motion>",          self._on_motion)
        self.bind("<Leave>",           self._on_leave)
        self.bind("<Button-1>",        self._on_press)
        self.bind("<B1-Motion>",       self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set_cards(self, cards, highlight=None):
        self._cards = list(cards)
        self._selected.clear()
        self._hover = None
        self._dragging = False
        self._drag_done.clear()

        if highlight:
            for c in highlight:
                try:
                    self._selected.add(self._cards.index(c))
                except ValueError:
                    pass

        self._redraw()

    def get_selected_indices(self) -> list[int]:
        return sorted(self._selected)

    def clear_selection(self):
        if not self._selected:
            return
        self._selected.clear()
        self._redraw()

    def _compute_layout(self):
        self._layout = []
        self._hitboxes = []
        n = len(self._cards)
        if n == 0:
            return

        W = self.winfo_width()  or 800
        H = self.winfo_height() or (self.CARD_H + self.LIFT
                                    + self.MARGIN_TOP + self.MARGIN_BOT)
        cw, ch = self.CARD_W, self.CARD_H
        avail = max(1, W - 2 * self.MARGIN_X)

        total   = cw + self.STEP * (n - 1)
        start_x = max(self.MARGIN_X, (W - total) / 2)
        base_y  = H - self.MARGIN_BOT - ch - self.LIFT

        for i in range(n):
            x = start_x + i * self.STEP
            # 命中区不随抬起变化，拖动过程中才稳定
            self._hitboxes.append((int(x), int(base_y),
                                   int(x + cw), int(base_y + ch)))
            lifted = (i in self._selected)
            y = base_y - (self.LIFT if lifted else 0)
            self._layout.append((int(x), int(y),
                                 int(x + cw), int(y + ch)))

    def _redraw(self):
        self.delete("all")
        self._compute_layout()
        for i, card in enumerate(self._cards):
            x1, y1, _, _ = self._layout[i]
            draw_card(self, x1, y1, card,
                      w=self.CARD_W, h=self.CARD_H,
                      selected=(i in self._selected),
                      hovered=(i == self._hover),
                      shadow_color=self._shadow)

    def _hit(self, mx, my) -> int | None:
        for i in range(len(self._hitboxes) - 1, -1, -1):
            x1, y1, x2, y2 = self._hitboxes[i]
            if x1 <= mx < x2 and y1 <= my < y2:
                return i
        return None

    def _toggle(self, i: int):
        if i in self._selected:
            self._selected.discard(i)
        else:
            self._selected.add(i)
        self._redraw()
        if self._on_sel_changed is not None:
            self._on_sel_changed()

    def _on_motion(self, e):
        h = self._hit(e.x, e.y)
        if h != self._hover:
            self._hover = h
            self._redraw()

    def _on_leave(self, e):
        if self._hover is not None:
            self._hover = None
            self._redraw()

    def _on_press(self, e):
        self._dragging = True
        self._drag_done = set()
        i = self._hit(e.x, e.y)
        if i is not None:
            self._drag_done.add(i)      # 本次拖动已处理
            self._toggle(i)

    def _on_drag(self, e):
        if not self._dragging:
            return
        i = self._hit(e.x, e.y)
        if i is None or i in self._drag_done:
            return
        self._drag_done.add(i)
        self._toggle(i)

    def _on_release(self, e):
        self._dragging = False
        self._drag_done = set()

    def refresh(self):
        self._redraw()

class PlayAreaCanvas(tb.Canvas):
    CARD_W = 100
    CARD_H = 140
    STEP   = 40
    PAD_L  = 4

    def __init__(self, master, **kw):
        super().__init__(master, highlightthickness=0, bd=0, **kw)
        self._cards: list[int] = []
        self.bind("<Configure>", lambda e: self._redraw())

    def show_cards(self, cards):
        self._cards = list(cards or [])
        self._redraw()

    def clear(self):
        self.show_cards([])

    def _redraw(self):
        self.delete("all")
        H = self.winfo_height() or (self.CARD_H + 8)
        n = len(self._cards)
        if n == 0:
            return
        x = self.PAD_L
        y = (H - self.CARD_H) / 2
        for i, c in enumerate(self._cards):
            draw_card(self, x + i * self.STEP, y, c,
                      w=self.CARD_W, h=self.CARD_H, shadow_color=None)

class PokerApp:
    def __init__(self):
        self.root = tb.Window(themename=THEME)
        self.root.title(APP_TITLE)
        self.root.resizable(0, 0)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ---- 事件总线 ----
        self.bridge = UIBridge()
        self.bridge.bind("log",          self._ui_log)
        self.bridge.bind("status",       self._ui_status)
        self.bridge.bind("players",      self._ui_players)
        self.bridge.bind("hand",         self._ui_hand)
        self.bridge.bind("last_play",    self._ui_last_play)
        self.bridge.bind("rooms",        self._ui_rooms)
        self.bridge.bind("join_failed",  self._ui_join_failed)
        self.bridge.bind("game_started", self._ui_game_started)
        self.bridge.bind("alert",        self._ui_alert)

        # ---- 状态 ----
        self.nickname = self._load_name()
        self.rooms: list[dict] = []
        self.session = None
        self.session_kind: str | None = None

        self._resize_job = None

        self._build_lobby()
        self.root.after(40, self._drain)

    def run(self):
        self.root.mainloop()

    def _drain(self):
        self.bridge.drain()
        self.root.after(40, self._drain)

    def _on_close(self):
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
        self.root.destroy()

    @staticmethod
    def _load_name() -> str:
        from namerandom import randname
        if os.path.isfile("name.json"):
            try:
                with open("name.json", encoding="utf-8") as f:
                    return str(json.loads(f.read()))
            except Exception:
                pass
        return randname()

    @staticmethod
    def _save_name(name: str):
        try:
            with open("name.json", "w", encoding="utf-8") as f:
                f.write(json.dumps(name, ensure_ascii=False))
        except Exception:
            pass

    def _current_nickname(self) -> str:
        name = self.nick_var.get().strip() or self.nickname
        self.nickname = name
        self._save_name(name)
        return name

    def _random_name(self):
        from namerandom import randname
        self.nick_var.set(randname())

    def _build_lobby(self):
        for w in self.root.winfo_children():
            w.destroy()

        outer = tb.Frame(self.root, padding=28)
        outer.pack(fill="both", expand=True)

        tb.Label(outer, text=APP_TITLE,
                 font=("Microsoft YaHei", 30, "bold"),
                 bootstyle="primary").pack(pady=(0, 20))

        # ---- 昵称 ----
        nick_box = tb.Labelframe(outer, text=" 昵称 ", padding=14,
                                 bootstyle="info")
        nick_box.pack(fill="x", pady=6)
        self.nick_var = StringVar(value=self.nickname)
        row = tb.Frame(nick_box)
        row.pack(fill="x")
        tb.Entry(row, textvariable=self.nick_var).pack(
            side="left", fill="x", expand=True, padx=(0, 8))
        tb.Button(row, text="随机", bootstyle="info",
                  command=self._random_name).pack(side="left")

        # ---- 创建房间 ----
        host_box = tb.Labelframe(outer, text=" 创建房间 ", padding=14,
                                 bootstyle="success")
        host_box.pack(fill="x", pady=6)
        r1 = tb.Frame(host_box)
        r1.pack(fill="x")
        tb.Label(r1, text="房间名:").pack(side="left")
        self.room_var = StringVar(value="周末牌局")
        tb.Entry(r1, textvariable=self.room_var, width=18).pack(
            side="left", padx=(6, 18))
        tb.Label(r1, text="最大人数:").pack(side="left")
        self.max_var = StringVar(value="4")
        tb.Spinbox(r1, from_=2, to=6, textvariable=self.max_var,
                   width=4).pack(side="left", padx=(6, 18))
        tb.Button(r1, text="创建房间", bootstyle="success",
                  command=self._do_host).pack(side="left")

        # ---- 加入房间 ----
        join_box = tb.Labelframe(outer, text=" 加入房间 ", padding=14,
                                 bootstyle="warning")
        join_box.pack(fill="both", expand=True, pady=6)

        top = tb.Frame(join_box)
        top.pack(fill="x")
        tb.Button(top, text="搜索房间", bootstyle="warning",
                  command=self._do_scan).pack(side="left")

        tree_frame = tb.Frame(join_box)
        tree_frame.pack(fill="both", expand=True, pady=8)
        self.room_tree = tb.Treeview(
            tree_frame, columns=("room", "players", "addr"),
            show="headings", height=6, bootstyle="warning")
        self.room_tree.heading("room", text="房间名")
        self.room_tree.heading("players", text="人数")
        self.room_tree.heading("addr", text="地址")
        self.room_tree.column("room", width=240)
        self.room_tree.column("players", width=90, anchor="center")
        self.room_tree.column("addr", width=240)
        self.room_tree.pack(side="left", fill="both", expand=True)

        bottom = tb.Frame(join_box)
        bottom.pack(fill="x")
        tb.Button(bottom, text="加入选中房间", bootstyle="warning",
                  command=self._do_join_selected).pack(side="left", padx=(0, 20))
        tb.Label(bottom, text="或直接输入地址  IP:").pack(side="left")
        self.ip_var = StringVar()
        tb.Entry(bottom, textvariable=self.ip_var, width=18).pack(
            side="left", padx=6)
        tb.Label(bottom, text="端口:").pack(side="left")
        self.port_var = StringVar()
        tb.Entry(bottom, textvariable=self.port_var, width=8).pack(
            side="left", padx=6)
        tb.Button(bottom, text="直接加入", bootstyle="warning-outline",
                  command=self._do_join_direct).pack(side="left", padx=6)

        self.root.place_window_center()

    def _do_host(self):
        name = self._current_nickname()
        room = self.room_var.get().strip() or "周末牌局"
        try:
            max_p = int(self.max_var.get())
            assert max_p >= 2
        except (ValueError, AssertionError):
            Messagebox.show_warning("最大人数必须是 ≥ 2 的整数",
                                    "参数错误", parent=self.root)
            return

        host = GameHost(self.bridge, name, room, max_p)
        try:
            host.start()
        except Exception as e:
            Messagebox.show_error(f"创建房间失败：{e}", "错误",
                                  parent=self.root)
            return
        self.session = host
        self._enter_game("host")

    def _do_scan(self):
        self.bridge.log("搜索房间中...")

        def worker():
            from netengine import PokerNode
            try:
                rooms = PokerNode.scan_rooms()
            except Exception as e:
                self.bridge.log(f"搜索失败：{e}")
                return
            self.bridge.emit("rooms", rooms)

        threading.Thread(target=worker, daemon=True).start()

    def _do_join_selected(self):
        sel = self.room_tree.selection()
        if not sel:
            Messagebox.show_warning("请先选择一个房间", "未选择", parent=self.root)
            return
        idx = int(sel[0])
        if idx >= len(self.rooms):
            return
        r = self.rooms[idx]
        self._join(r["host"], r["port"])

    def _do_join_direct(self):
        ip = self.ip_var.get().strip()
        port_s = self.port_var.get().strip()
        if not ip or not port_s:
            Messagebox.show_warning("请输入 IP 和端口", "参数缺失", parent=self.root)
            return
        try:
            port = int(port_s)
        except ValueError:
            Messagebox.show_warning("端口必须是整数", "参数错误", parent=self.root)
            return
        self._join(ip, port)

    def _join(self, ip: str, port: int):
        name = self._current_nickname()
        cli = GameClient(self.bridge, name, ip, port)

        def worker():
            try:
                cli.start()
            except Exception as e:
                self.bridge.emit("join_failed", str(e))
                return
            self.session = cli
            self.root.after(0, lambda: self._enter_game("client"))

        threading.Thread(target=worker, daemon=True).start()

    def _enter_game(self, kind: str):
        self.session_kind = kind
        self._build_game()

        if kind == "host":
            node = self.session.node
            self.bridge.status(
                f"房间: {self.session.room_name}  |  端口: {node.port}  |  "
                f"等待玩家加入...")
        else:
            self.bridge.status(f"已加入房间  |  昵称: {self.nickname}")

    def _build_game(self):
        for w in self.root.winfo_children():
            w.destroy()

        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)     # 日志区是唯一随窗口拉伸的行

        bg   = root.style.colors.bg
        fg   = root.style.colors.fg
        area = CARD_AREA_BG

        # ---- row 0：玩家横条 ----
        self.players_var = StringVar(value="")
        tb.Label(root, textvariable=self.players_var,
                 bootstyle="secondary", padding=(14, 6),
                 anchor="w", font=("Microsoft YaHei", 10),
                 ).grid(row=0, column=0, sticky="ew")

        # ---- row 1：日志 ----
        log_frame = tb.Frame(root, padding=(8, 4))
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, wrap="word", state="disabled",
                                height=10, borderwidth=0,
                                highlightthickness=0,
                                bg=bg, fg=fg, insertbackground=fg,
                                font=("Consolas", 12), width=120)
        sb = tb.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        # ---- row 2：状态栏（谁该出牌 / 等待）----
        self.status_var = StringVar(value="准备中...")
        self.status_label = tb.Label(
            root, textvariable=self.status_var,
            bootstyle="inverse-primary", padding=(14, 8),
            anchor="w", font=("Microsoft YaHei", 11, "bold"),
        )
        self.status_label.grid(row=2, column=0, sticky="ew")

        # ---- row 3：出牌区（左侧标签 + 右侧小牌）----
        play_box = tb.Frame(root, padding=(8, 2))
        play_box.grid(row=3, column=0, sticky="ew")
        play_box.columnconfigure(1, weight=1)

        # 左侧小文本框：显示上一手是谁出的 + 牌型
        self.play_label_var = StringVar(value="上一手 —————")
        self.play_label = tb.Label(
            play_box, textvariable=self.play_label_var,
            bootstyle="secondary",
            anchor="w", padding=(10, 6),
            font=("Microsoft YaHei", 10, "bold"),
        )
        self.play_label.grid(row=0, column=0, sticky="nsw", padx=(0, 8))

        self.play_area = PlayAreaCanvas(
            play_box, bg=area, height=PlayAreaCanvas.CARD_H + 8)
        self.play_area.grid(row=0, column=1, sticky="ew")

        # ---- row 4：手牌 ----
        self.hand_canvas = HandCanvas(
            root, bg=area, height=int(HandCanvas.CARD_H * 1.2),
            on_selection_changed=self._on_selection_changed,
        )
        self.hand_canvas.grid(row=4, column=0, sticky="ew")

        # ---- row 5：输入行 ----
        input_frame = tb.Frame(root, padding=(10, 6))
        input_frame.grid(row=5, column=0, sticky="ew")
        input_frame.columnconfigure(4, weight=1)

        tb.Button(input_frame, text="  提交  ", bootstyle="primary",
                  command=self._submit_input).grid(row=0, column=0, padx=2)
        tb.Button(input_frame, text="  过牌  ", bootstyle="secondary",
                  command=self._submit_pass).grid(row=0, column=1, padx=2)
        tb.Button(input_frame, text="清空选牌", bootstyle="secondary-outline",
                  command=self._clear_selection).grid(row=0, column=2, padx=2)
        tb.Button(input_frame, text="刷新手牌", bootstyle="info-outline",
          command=self._refresh_hand).grid(row=0, column=3, padx=2)

        self.input_var = StringVar()
        self.input_entry = tb.Entry(input_frame, textvariable=self.input_var)
        self.input_entry.grid(row=0, column=4, sticky="ew", padx=(0, 8))
        self.input_entry.bind("<Return>", lambda e: self._submit_chat())

        if self.session_kind == "host":
            self.start_btn = tb.Button(
                input_frame, text="开始游戏", bootstyle="success",
                command=self._host_start)
            self.start_btn.grid(row=0, column=5, padx=(10, 2))

        self.root.bind("<Configure>", self._on_resize)
        self.root.place_window_center()

    def _on_selection_changed(self):
        pass

    def _clear_selection(self):
        if hasattr(self, "hand_canvas"):
            self.hand_canvas.clear_selection()

    def _refresh_hand(self):
        if self.session is None or not hasattr(self, "hand_canvas"):
            return

        sess = self.session
        if self.session_kind == "host":
            with sess.lock:
                cards = list(sess.hands.get(HOST_ID, []))
        elif self.session_kind == "client":
            with sess.lock:
                cards = list(sess.hand)
        else:
            return

        canvas = self.hand_canvas
        if canvas._cards == cards:
            canvas.refresh() # 数据没变，仅重绘，保留选中
            self.bridge.log("[手牌] 已重绘")
        else:
            canvas.set_cards(cards) # 数据确实不同，重新同步
            self.bridge.log("[手牌] 已同步最新手牌")

    def _on_resize(self, event):
        if event.widget is not self.root:
            return
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(
            80, lambda: hasattr(self, "hand_canvas")
                        and self.hand_canvas._redraw())

    def _submit_input(self):
        if self.session is None:
            return
        text = self.input_var.get().strip()
        self.input_var.set("")

        if not text and hasattr(self, "hand_canvas"):
            idxs = self.hand_canvas.get_selected_indices()
            if idxs:
                text = " ".join(str(i) for i in idxs)
                self.hand_canvas.clear_selection()

        if not text:
            return

        sess = self.session
        threading.Thread(target=sess.handle_input, args=(text,),
                         daemon=True).start()

    def _submit_chat(self):
        if self.session is None:
            return
        text = self.input_var.get().strip()
        if not text:
            return
        self.input_var.set("")
        sess = self.session
        threading.Thread(target=sess.handle_input,
                         args=(f"chat {text}",), daemon=True).start()

    def _submit_pass(self):
        if self.session is None:
            return
        if (hasattr(self, "hand_canvas")
                and self.hand_canvas.get_selected_indices()):
            Messagebox.show_warning(
                "已选牌，不能过牌。请先清空选牌，再点过牌。",
                "提示", parent=self.root)
            return
        sess = self.session
        threading.Thread(target=sess.handle_input, args=("p",),
                         daemon=True).start()

    def _submit_input(self):
        if self.session is None:
            return
        text = self.input_var.get().strip()
        self.input_var.set("")

        idxs = (self.hand_canvas.get_selected_indices()
                if hasattr(self, "hand_canvas") else [])

        if not text and idxs:
            text = " ".join(str(i) for i in idxs)
            self.hand_canvas.clear_selection()
            idxs = []

        if not text:
            return

        if text.strip().lower() in ("p", "pass") and idxs:
            Messagebox.show_warning(
                "已选牌，不能过牌。请先清空选牌，再输入 p。",
                "提示", parent=self.root)
            return

        sess = self.session
        threading.Thread(target=sess.handle_input, args=(text,),
                         daemon=True).start()

    def _host_start(self):
        if self.session_kind == "host" and self.session is not None:
            self.session.start_game()

    def _ui_log(self, text: str):
        if not hasattr(self, "log_text"):
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        try:
            total = int(self.log_text.index("end-1c").split(".")[0])
            if total > 2000:
                self.log_text.delete("1.0", f"{total - 2000}.0")
        except Exception:
            pass
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _ui_status(self, text: str):
        if hasattr(self, "status_var"):
            self.status_var.set(text)
        if hasattr(self, "status_label"):
            if "轮到你" in text or "请选一张" in text:
                self.status_label.configure(bootstyle="inverse-success")
            else:
                self.status_label.configure(bootstyle="inverse-primary")

    def _ui_players(self, lines: list[str]):
        if hasattr(self, "players_var"):
            self.players_var.set("      ".join(lines))

    def _ui_hand(self, cards: list[int], highlight=None):
        if hasattr(self, "hand_canvas"):
            self.hand_canvas.set_cards(cards, highlight=highlight)

    def _ui_last_play(self, text: str, cards=None):
        # 左侧小框：谁 · 牌型
        if hasattr(self, "play_label_var"):
            self.play_label_var.set(text)
        # 右侧：牌
        if hasattr(self, "play_area"):
            self.play_area.show_cards(cards)

    def _ui_game_started(self):
        """成功开局后隐藏"开始游戏"按钮，并清空上一局的出牌区。"""
        if hasattr(self, "start_btn") and self.start_btn.winfo_exists():
            self.start_btn.grid_remove()
        # 新一局开始：清掉上一手牌面和标签
        if hasattr(self, "play_area"):
            self.play_area.clear()
        if hasattr(self, "play_label_var"):
            self.play_label_var.set("上一手 —————")

    def _ui_rooms(self, rooms: list[dict]):
        self.rooms = rooms
        if not hasattr(self, "room_tree") or not self.room_tree.winfo_exists():
            return
        for item in self.room_tree.get_children():
            self.room_tree.delete(item)
        if not rooms:
            self._ui_log("未发现房间")
            return
        self._ui_log(f"发现 {len(rooms)} 个房间：")
        for i, r in enumerate(rooms):
            self.room_tree.insert(
                "", "end", iid=str(i),
                values=(r["room"], f"{r['players']}/{r['max_players']}",
                        f"{r['host']}:{r['port']}"))

    def _ui_join_failed(self, msg: str):
        Messagebox.show_error(f"加入失败：{msg}", "错误", parent=self.root)

    def _ui_alert(self, title: str, msg: str):
        Messagebox.show_warning(msg, title, parent=self.root)

def main():
    app = PokerApp()
    app.run()
