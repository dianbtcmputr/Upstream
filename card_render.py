from core import card_rank, card_suit

_RANK_LABELS = ("3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "2")
_SUIT_SYMBOLS = ("♠", "♥", "♣", "♦")
_RED_SUITS = (1, 3)   # HEART, DIAMOND

CARD_FACE          = "#FFFFFF"
CARD_BORDER_NORMAL = "#9E9E9E"
CARD_BORDER_HOVER  = "#2196F3"
CARD_BORDER_SEL    = "#FF9800"
COLOR_BLACK        = "#111111"
COLOR_RED          = "#D32F2F"

_SUIT_FONT = "Segoe UI Symbol"
_RANK_FONT = "Arial"

_REF_W = 62.0
DEFAULT_SHADOW = "#CFCFCF"

def darken_color(hex_color: str, factor: float = 0.82) -> str:
    """把 #RRGGBB 颜色按 factor 变暗。非法输入时回退到 DEFAULT_SHADOW。"""
    if not isinstance(hex_color, str):
        return DEFAULT_SHADOW
    s = hex_color.strip().lstrip("#")
    if len(s) == 3:                        # #RGB -> #RRGGBB
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return DEFAULT_SHADOW
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
    except ValueError:
        return DEFAULT_SHADOW
    r = max(0, min(255, int(r * factor)))
    g = max(0, min(255, int(g * factor)))
    b = max(0, min(255, int(b * factor)))
    return f"#{r:02X}{g:02X}{b:02X}"

def card_rank_label(card: int) -> str:
    return _RANK_LABELS[card_rank(card) - 3]

def card_suit_symbol(card: int) -> str:
    return _SUIT_SYMBOLS[card_suit(card)]

def card_is_red(card: int) -> bool:
    return card_suit(card) in _RED_SUITS

def card_color(card: int) -> str:
    return COLOR_RED if card_is_red(card) else COLOR_BLACK

def _rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [
        x1 + r, y1,  x2 - r, y1,  x2, y1,
        x2, y1 + r,  x2, y2 - r,  x2, y2,
        x2 - r, y2,  x1 + r, y2,  x1, y2,
        x1, y2 - r,  x1, y1 + r,  x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)

def draw_card(canvas, x, y, card, *, w=62, h=88,
              selected=False, hovered=False,
              shadow_color: str | None = None, tags=()):
    """以 (x, y) 为左上角画一张牌"""
    scale = w / _REF_W
    radius = max(4, int(6 * scale))
    items: list[int] = []

    if shadow_color:
        off = max(1, int(2 * scale))
        items.append(_rounded_rect(
            canvas, x + off, y + off + 1,
            x + w + off, y + h + off + 1, radius,
            fill=shadow_color, outline="", tags=tags))

    if selected:
        border, bw = CARD_BORDER_SEL, max(1, int(2 * scale))
    elif hovered:
        border, bw = CARD_BORDER_HOVER, max(1, int(2 * scale))
    else:
        border, bw = CARD_BORDER_NORMAL, max(1, int(0.5 * scale))

    items.append(_rounded_rect(
        canvas, x, y, x + w, y + h, radius,
        fill=CARD_FACE, outline=border, width=bw, tags=tags))

    color = card_color(card)
    rank  = card_rank_label(card)
    suit  = card_suit_symbol(card)

    rank_size = max(5,  int(8 * scale))
    small_sz  = max(5,  int(8 * scale))
    big_sz    = max(10, int(22 * scale))
    pad       = max(1,  int(3  * scale))

    # 左上：点数
    items.append(canvas.create_text(
        x + pad, y + pad, text=rank, anchor="nw",
        fill=color, font=(_RANK_FONT, rank_size, "bold"), tags=tags))

    # 左上：小花色
    items.append(canvas.create_text(
        x + pad, y + pad + rank_size + max(2, int(6 * scale)),
        text=suit, anchor="nw",
        fill=color, font=(_SUIT_FONT, small_sz), tags=tags))

    # 中心：大花色
    items.append(canvas.create_text(
        x + w / 2, y + h / 2 + max(2, int(7 * scale)),
        text=suit, anchor="center",
        fill=color, font=(_SUIT_FONT, big_sz), tags=tags))

    return items
