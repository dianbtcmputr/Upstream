"""烟威争上游核心库
普通扑克去除王牌；无炸弹
牌型：单、对、顺子（见函数）、连对（见函数）、三带一（只能带单张）、四带二（两单或一对）"""

from collections import Counter
from enum import IntEnum
from random import shuffle

class Suit(IntEnum):
    SPADE = 0
    HEART = 1
    CLUB = 2
    DIAMOND = 3

class Rank(IntEnum):
    THREE = 3
    FOUR = 4
    FIVE = 5
    SIX = 6
    SEVEN = 7
    EIGHT = 8
    NINE = 9
    TEN = 10
    JACK = 11
    QUEEN = 12
    KING = 13
    ACE = 14
    TWO = 15

_SUIT_SHIFT = 8
_RANK_MASK = 0xFF

# 高 8 位花色，低 8 位牌面
def make_card(rank: int, suit: int) -> int:
    return (int(suit) << _SUIT_SHIFT) | int(rank)

def card_suit(card: int) -> int:
    "获取花色"
    return (card >> _SUIT_SHIFT) & 0xFF

def card_suits(cards: list[int]):
    "获取列表花色"
    return [card_suit(x) for x in cards]

def card_rank(card: int) -> int:
    """获取牌面，牌面经此函数将保持不变"""
    return card & _RANK_MASK

def card_ranks(cards: list[int]):
    """获取列表牌面"""
    return [card_rank(x) for x in cards]

DECK = [make_card(r, s) for r in Rank for s in Suit]

class ComboType(IntEnum):
    UNKNOWN = 0
    SINGLE = 1
    PAIR = 2
    TRIPLE_ONE = 3
    QUAD_TWO = 4
    SEQUENCE = 5 # 顺子
    PAIR_SEQ = 6

def classify(cards: list[int]) -> tuple[ComboType, int]:
    """识别 16 位牌或 8 位牌面的类型，获取比较标准"""
    ranks = card_ranks(cards)
    ranks.sort() # 增序

    # 固定长度牌型
    if len(ranks) == 0: pass
    
    elif len(ranks) == 1:
        return ComboType.SINGLE, ranks[0]

    elif len(ranks) == 2:
        a, b = ranks
        if a == b:
            return ComboType.PAIR, ranks[0]
        else:
            pass

    elif len(ranks) == 3: pass

    elif len(ranks) == 4:
        a, b, c, d = ranks
        if (a == b and b == c and c != d) or (a != b and b == c and c == d):
            return ComboType.TRIPLE_ONE, b
        else:
            pass

    elif len(ranks) == 6:
        a, b, c, d, e, f = ranks
        if (
            (a == b and b == c and c == d) or
            (b == c and c == d and d == e) or
            (c == d and d == e and e == f)
        ):
            return ComboType.QUAD_TWO, c
        else:
            pass

    # 可变长度牌型 重开 if
    if (std := _is_sequence(ranks)):
        return ComboType.SEQUENCE, std
    else:
        pass

    if (std := _is_pair_seq(ranks)):
        return ComboType.PAIR_SEQ, std
    else:
        pass

    return ComboType.UNKNOWN, 0

_CIRC_ORDER = [
    Rank.THREE, Rank.FOUR, Rank.FIVE, Rank.SIX, Rank.SEVEN, Rank.EIGHT,
    Rank.NINE, Rank.TEN, Rank.JACK, Rank.QUEEN, Rank.KING, Rank.ACE, Rank.TWO
]
_CIRC_ORDER_DOUBLE = _CIRC_ORDER + _CIRC_ORDER

def _is_sequence(ranks_sort: list[int]) -> int:
    """
    不是顺子，返回 0；否则返回比较标准
    前 5 张取值范围为 3 - A（= 不包含 2）
    自第 6 张开始，A 的后继是 2，2 的后继是 3
    不得出现恰好闭环或重复区段
    例如，10 J Q K A √；J Q K A 2 ×；10 J Q K A 2 3 4 √
    """
    lr = len(ranks_sort)

    if lr < 5 or lr > 12: # 12 = from 3 to A
        return 0

    ranks_set = set(ranks_sort)
    if len(ranks_set) != lr:
        return 0
    
    for start in range(len(_CIRC_ORDER)):
        subseq = _CIRC_ORDER_DOUBLE[start: start + lr]
        if set(subseq) == ranks_set:
            if Rank.TWO in subseq:
                idx2 = subseq.index(Rank.TWO)
                if idx2 < 5:
                    return 0
            return subseq[0] # 顺子头作标准

    return 0

def _is_pair_seq(ranks_sort: list[int]) -> int:
    """
    不是连对，返回 0；否则返回比较标准
    前 3 对取值范围为 3 - A（= 不包含 2）
    自第 4 对开始，A 的后继是 2，2 的后继是 3
    不得出现恰好闭环或重复区段
    """
    lr = len(ranks_sort)

    if lr % 2 != 0 or lr < 6 or lr > 24:
        return 0

    counts = Counter(ranks_sort)
    if any(v != 2 for v in counts.values()):
        return 0

    pair_ranks = list(counts.keys())
    pair_set = set(pair_ranks)
    pair_len = len(pair_ranks)

    for start in range(len(_CIRC_ORDER)):
        subseq = _CIRC_ORDER_DOUBLE[start:start + pair_len]
        if set(subseq) == pair_set:
            if Rank.TWO in subseq:
                idx2 = subseq.index(Rank.TWO)
                if idx2 < 3:
                    return 0
            return subseq[0] # 连对头作标准

    return 0

def can_a_defeat_b(cards_a: list[int], cards_b: list[int]):
    a_typ, a_std = classify(cards_a)
    b_typ, b_std = classify(cards_b)

    if a_typ == b_typ and len(cards_a) == len(cards_b) and a_std > b_std:
        return True
    else:
        return False

_HEART_FIVE = make_card(Rank.FIVE, Suit.HEART)

def gen_rand_cards(n_players: int):
    """分配牌，并锁定第一个出牌玩家（持有红桃 5）索引"""
    deck = DECK.copy()
    shuffle(deck)

    assert n_players >= 2
    if n_players == 2: # 双玩家，随机去掉一些牌，但必须保留红桃 5
        if _HEART_FIVE in deck[:4]:
            del deck[-4:]
        else:
            del deck[:4]

    q, r = divmod(len(deck), n_players)
    result: list[list[int]] = []
    start = 0
    first_go = 0

    for i in range(n_players):
        size = q + (1 if i < r else 0)
        handing = deck[start:start + size]
        sort_hand(handing)
        result.append(handing)
        start += size

        if _HEART_FIVE in handing:
            first_go = i

    return result, first_go

def card_to_str(card: int, color: bool = False):
    suit = ("b♠", "r♥", "b♣", "r♦")[card_suit(card)]
    rank = ("3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "2")[card_rank(card) - 3]
    if len(rank) == 1:
        rank += " "
    if not color:
        return suit[1] + rank
    return suit.replace("b", "\033[37m").replace("r", "\033[31m") + rank + "\033[0m"

def cards_to_str(cards: list[int], color: bool = False):
    return " ".join(card_to_str(c, color) for c in cards)

_COMBO_NAMES = {
    ComboType.SINGLE:     "单张",
    ComboType.PAIR:       "对子",
    ComboType.TRIPLE_ONE: "三带一",
    ComboType.QUAD_TWO:   "四带二",
    ComboType.SEQUENCE:   "顺子",
    ComboType.PAIR_SEQ:   "连对",
}

READ_MODE = 0

if READ_MODE == 0:
    _RANK_NAMES = ["3","4","5","6","7","8","9","10","钩","圈","K","尖","2"]
else:
    _RANK_NAMES = ["3","4","5","6","7","8","9","10","丁","球","凯","冒","2"]

def _rank_name(r):
    return _RANK_NAMES[r - 3]

def cards_prompt(cards: list[int]):
    cards = card_ranks(cards)
    cls, _std = classify(cards)
    pr: list[str | int] = {
        ComboType.SINGLE:     [_rank_name(cards[0])],
        ComboType.PAIR:       ["对", _rank_name(cards[0])],
        ComboType.TRIPLE_ONE: ["三带一"],
        ComboType.QUAD_TWO:   ["四带二"],
        ComboType.SEQUENCE:   ["顺子"],
        ComboType.PAIR_SEQ:   ["连对"],
    }.get(cls, [""])

    return " ".join(str(x) for x in pr)

def combo_name(cards: list[int]) -> str:
    """返回牌型的中文名；非法牌型返回 '未知'"""
    typ, _ = classify(cards)
    return _COMBO_NAMES.get(typ, "未知")

def sort_hand(cards: list[int]) -> None:
    """会修改 cards"""
    cards.sort(key=lambda c: (card_rank(c), card_suit(c)))
