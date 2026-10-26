"""Tiny, dependency-free HTML tree used by the platform parsers.

It is not a full browser DOM, but it handles the well-formed-ish markup returned
by e-commerce order pages and keeps the parsers testable without Playwright.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Iterable

VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    parent: "Node | None" = None

    def text(self, separator: str = " ", strip: bool = True) -> str:
        parts: list[str] = list(self.text_parts)
        for child in self.children:
            if child.tag in {"script", "style"}:
                continue
            child_text = child.text(separator=separator, strip=False)
            if child_text:
                parts.append(child_text)
        value = separator.join(part for part in parts if part)
        return value.strip() if strip else value

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def find_all(
        self,
        tag: str | None = None,
        *,
        class_: str | None = None,
        id_: str | None = None,
        attrs: dict[str, str] | None = None,
        predicate: Callable[["Node"], bool] | None = None,
        limit: int | None = None,
    ) -> list["Node"]:
        result: list[Node] = []

        def walk(node: Node) -> None:
            if limit is not None and len(result) >= limit:
                return
            ok = True
            if tag and node.tag != tag:
                ok = False
            if ok and class_ and class_ not in node.classes:
                ok = False
            if ok and id_ and node.attrs.get("id") != id_:
                ok = False
            if ok and attrs:
                ok = all(node.attrs.get(key) == value for key, value in attrs.items())
            if ok and predicate and not predicate(node):
                ok = False
            if ok:
                result.append(node)
            for child in node.children:
                walk(child)

        walk(self)
        return result

    def find(
        self,
        tag: str | None = None,
        *,
        class_: str | None = None,
        id_: str | None = None,
        attrs: dict[str, str] | None = None,
        predicate: Callable[["Node"], bool] | None = None,
    ) -> "Node | None":
        found = self.find_all(
            tag, class_=class_, id_=id_, attrs=attrs, predicate=predicate, limit=1
        )
        return found[0] if found else None

    def iter(self) -> Iterable["Node"]:
        yield self
        for child in self.children:
            yield from child.iter()


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(
            tag=tag.lower(),
            attrs={key.lower(): (value or "") for key, value in attrs},
            parent=self.stack[-1],
        )
        self.stack[-1].children.append(node)
        if node.tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(
            tag=tag.lower(),
            attrs={key.lower(): (value or "") for key, value in attrs},
            parent=self.stack[-1],
        )
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].text_parts.append(data)


def parse_html(html: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(html or "")
    builder.close()
    return builder.root


def first_text(node: Node | None, *selectors: tuple[str, str]) -> str:
    """selectors are (tag, class) pairs; empty tag means any tag."""
    if node is None:
        return ""
    for tag, class_ in selectors:
        found = node.find(tag or None, class_=class_ or None)
        if found and found.text().strip():
            return found.text().strip()
    return ""
