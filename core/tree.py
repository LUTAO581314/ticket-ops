"""
控件树的层级推断 — 补 uiautomator dump 被正则解析丢掉的那一半信息。

为什么需要这个：
uiautomator dump 输出的是嵌套 XML，`parse_ui_xml` 用逐 node 正则把它拍平成一维列表，
丢掉了父子关系。大麦场景只按文字匹配，拍平无所谓；猫眼不行——猫眼的"某一档缺货"标记
挂在票档行的**子树**里，观演人的勾选状态挂在姓名节点的**兄弟 Image** 上，只看单节点的
text/desc 判不出来。

不重新解析 XML 的理由：dump 的节点顺序就是先序遍历，先序 + bounds 包含关系足以还原
我们要问的两个问题（谁是我的祖先 / 我的子树里有什么），不需要真的建树。这样 device.py
和 fsm.py 一行都不用改。

不变量（先序遍历给的性质）：
1. 祖先一定出现在后代之前 → 只需前向扫描就能找祖先，遇到自己就停。
2. 一旦某个节点不在我的 bounds 内，后面所有节点也都不在 → 找子树时可以提前 break。
"""
from __future__ import annotations

from typing import Iterator

from core.fsm import Node, Snapshot


def _index_of(snap: Snapshot, node: Node) -> int:
    """按身份（is）定位下标。不能用 ==：bounds/text 全同的两个节点会被混淆。"""
    for i, n in enumerate(snap.nodes):
        if n is node:
            return i
    return -1


def area(bounds: tuple[int, int, int, int]) -> int:
    l, t, r, b = bounds
    return max(0, r - l) * max(0, b - t)


def contains(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]) -> bool:
    """outer 是否包住 inner（允许边界相等，但 bounds 完全一致不算包含关系）。"""
    if outer == inner:
        return False
    if outer == (0, 0, 0, 0) or inner == (0, 0, 0, 0):
        return False
    ol, ot, orr, ob = outer
    il, it, ir, ib = inner
    return ol <= il and ot <= it and orr >= ir and ob >= ib


def ancestors(snap: Snapshot, node: Node) -> Iterator[Node]:
    """从近到远产出祖先（最小包含者优先）。"""
    i = _index_of(snap, node)
    if i <= 0:
        return
    cand = [n for n in snap.nodes[:i] if contains(n.bounds, node.bounds)]
    for n in sorted(cand, key=lambda x: area(x.bounds)):
        yield n


def container(snap: Snapshot, node: Node) -> Node | None:
    """
    最小包含祖先 — 也就是"这个节点所在的那一行 / 那一格"。

    猫眼判缺货要的就是它：票档节点 `¥1280` 自己可能不带"缺货"字样，
    缺货标签是它的兄弟节点，所以要拿它所在的格子再查格子子树。
    """
    for n in ancestors(snap, node):
        return n
    return None


def descendants(snap: Snapshot, node: Node) -> list[Node]:
    """先序子树。遇到第一个不被包含的节点立即停止（先序性质保证不会再有后代）。"""
    i = _index_of(snap, node)
    if i < 0:
        return []
    out: list[Node] = []
    for n in snap.nodes[i + 1:]:
        if contains(node.bounds, n.bounds):
            out.append(n)
        else:
            break
    return out


def descendants_nested(snap: Snapshot, node: Node, depth: int = 2) -> list[Node]:
    """
    只取前 depth 层子树 — 用于"这一行里有没有缺货标签"，但不要把小字说明也扫进来。
    实现上近似：按面积从大到小保留前 N 个。层级本身拿不到，用面积做代理。
    """
    kids = descendants(snap, node)
    if not kids:
        return []
    kids.sort(key=lambda n: area(n.bounds), reverse=True)
    return kids[: max(1, depth * 2)]


def subtree_text(snap: Snapshot, node: Node, *, include_children: bool = True) -> str:
    """节点的 text + desc + 子树 text + 子树 desc，全部拼起来做关键词判断。"""
    parts = [node.text or "", node.desc or ""]
    if include_children:
        for d in descendants(snap, node):
            parts.append(d.text or "")
            parts.append(d.desc or "")
    return "\x00".join(parts)


def rows(snap: Snapshot, key: str) -> list[Node]:
    """在一维列表里找出"某类行"的容器节点，用于批量结构判断。"""
    out = []
    for n in snap.nodes:
        if key and key in f"{n.text}\x00{n.desc}":
            c = container(snap, n)
            if c is not None:
                out.append(c)
    return out
