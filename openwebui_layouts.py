"""Translate a small, static ChatGPT layout vocabulary; never evaluate JSX or JavaScript."""

import ast
import html
import json
import math
import re
from dataclasses import dataclass, field

MAX_BLOCK = 128 * 1024
MAX_NODES = 2048
MAX_DEPTH = 32
TAGS = {
    "box",
    "grid",
    "grid-item",
    "row",
    "text",
    "title",
    "caption",
    "badge",
    "divider",
    "spacer",
    "icon",
    "table",
    "table-row",
    "table-cell",
    "Link",
    "Entity",
    "Cite",
    "svg",
    "rect",
    "path",
    "g",
    "b",
}
ROOTS = r"box|grid|row|text|Entity|Cite"
TOKEN = re.compile(
    r"""<(?P<close>/)?(?P<name>[A-Za-z][\w:-]*)(?P<attrs>(?:[^<>"']|"[^"]*"|'[^']*')*)>"""
)
ATTRIBUTE = re.compile(
    r"""\s+([A-Za-z][\w-]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|\{\s*(-?\d+(?:\.\d+)?|true|false)\s*\}))?"""
)
REFERENCE_LIST = re.compile(
    r"""\s+refs\s*=\s*\{\s*(\[(?:\s*"[^"]*"\s*(?:,\s*"[^"]*"\s*)*)?\])\s*\}"""
)
SVELTE_SVG = re.compile(r"(?is)(<svg\b[^>]*>)(.*?)</svg>")
EACH_ITEMS = re.compile(r"(?s)\{#each\s+\[(.*?)\]\s+as\s+\w+\}")
SVG_LABEL = re.compile(r"""(?:\b(?:t|l1|l2)\s*:\s*"([^"]*)")|(?:<text\b[^>]*>([^<{]+)</text>)""")
CANDIDATE = re.compile(
    r"(?m)^ {0,3}(?:`{3,}|~{3,})[^\r\n]*(?:\r?\n|$)|`+|^(?: {4}|\t)[^\r\n]*(?:\r?\n|$)|(?<!\\)<(?:"
    + ROOTS
    + r")(?=[\s/>])"
)


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)


class UnsupportedLayout(ValueError):
    pass


def attributes(raw: str) -> dict[str, str]:
    result = {}
    pos = 0
    raw = raw.rstrip().removesuffix("/")
    while pos < len(raw):
        if not raw[pos:].strip():
            break
        refs = REFERENCE_LIST.match(raw, pos)
        if refs is not None:
            if "refs" in result:
                raise UnsupportedLayout("Duplicate attributes are not supported")
            try:
                values = json.loads(refs.group(1))
            except ValueError as exc:
                raise UnsupportedLayout("Citation references must be literal strings") from exc
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise UnsupportedLayout("Citation references must be literal strings")
            result["refs"] = "\u001f".join(values)
            pos = refs.end()
            continue
        match = ATTRIBUTE.match(raw, pos)
        if match is None:
            raise UnsupportedLayout("Only literal attributes are supported")
        name, double, single, literal = match.groups()
        if name.lower().startswith("on") or name in result:
            raise UnsupportedLayout("Event handlers and duplicate attributes are not supported")
        result[name] = html.unescape(
            next((v for v in (double, single, literal) if v is not None), "true")
        )
        pos = match.end()
    return result


def block_end(text: str, start: int) -> int:
    """Find a complete root without swallowing surrounding prose, even for unknown children."""
    first = TOKEN.match(text, start)
    if first is None:
        return min(len(text), start + MAX_BLOCK)
    if first.group("attrs").rstrip().endswith("/"):
        return first.end()
    name = first.group("name")
    depth = 1
    stop = min(len(text), start + MAX_BLOCK)
    for token in TOKEN.finditer(text, first.end(), stop):
        if token.group("name") != name:
            continue
        if token.group("close"):
            depth -= 1
        elif not token.group("attrs").rstrip().endswith("/"):
            depth += 1
        if depth == 0:
            return token.end()
    return stop


def parse(block: str) -> Node:
    stack: list[Node] = []
    root = None
    pos = count = 0
    for token in TOKEN.finditer(block):
        text = block[pos : token.start()]
        if stack:
            if "<" in text or "```" in text or "~~~" in text:
                raise UnsupportedLayout("Unparsed markup or code fence inside a layout")
            stack[-1].children.append(text)
        elif text.strip():
            raise UnsupportedLayout("Text outside root")
        name = token.group("name")
        if name not in TAGS:
            raise UnsupportedLayout("Unknown component")
        if token.group("close"):
            if token.group("attrs").strip() or not stack or stack[-1].tag != name:
                raise UnsupportedLayout("Mismatched closing tag")
            stack.pop()
        else:
            count += 1
            if count > MAX_NODES or len(stack) >= MAX_DEPTH:
                raise UnsupportedLayout("Layout complexity limit")
            node = Node(name, attributes(token.group("attrs")))
            if stack:
                stack[-1].children.append(node)
            elif root is None:
                root = node
            else:
                raise UnsupportedLayout("Multiple roots")
            if not token.group("attrs").rstrip().endswith("/"):
                stack.append(node)
        pos = token.end()
    if root is None or stack or block[pos:].strip():
        raise UnsupportedLayout("Incomplete layout")
    return root


def plain(value: str) -> str:
    return html.unescape(value).replace("<", "&lt;").replace(">", "&gt;")


def safe_url(value: str | None) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"https?://[^\s<>]+", value):
        raise UnsupportedLayout("Links must use literal http(s) URLs")
    return value


def inline(node: Node) -> str:
    return "".join(plain(c) if isinstance(c, str) else markdown(c) for c in node.children).strip()


def table_cell(value: str) -> str:
    return re.sub(r"\s+", " ", value).replace("|", "\\|").strip()


def table(rows: list[list[str]], headers: list[str]) -> str:
    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(table_cell(cell) for cell in row) + " |" for row in rows),
        ]
    )


def citation_labels(node: Node) -> list[str]:
    refs = node.attrs.get("refs", "").split("\u001f") if node.attrs.get("refs") else []
    ref = node.attrs.get("ref")
    if ref:
        refs.append(ref)
    if not refs or any(not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value) for value in refs):
        raise UnsupportedLayout("Citation references must be literal safe identifiers")
    return refs


def format_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not isinstance(value, float) or value.is_integer():
            return str(int(value))
    return str(value)


def literal_each_items(raw: str) -> list[dict]:
    # The supported form is a literal JS object array, not a general JS expression.
    normalized = re.sub(r"([,{]\s*)([A-Za-z][\w-]*)\s*:", r'\1"\2":', raw)
    try:
        items = json.loads(normalized)
    except ValueError as exc:
        raise UnsupportedLayout("Svelte each data must be literal JSON-like objects") from exc
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise UnsupportedLayout("Svelte each data must be literal object rows")
    return items


def evaluate_loop_expression(expression: str, variable: str, item: dict) -> str:
    expression = expression.strip()
    direct = re.fullmatch(re.escape(variable) + r"\.([A-Za-z][\w-]*)", expression)
    if direct:
        value = item.get(direct.group(1))
        if not isinstance(value, (str, int, float, bool)) or isinstance(value, bool):
            raise UnsupportedLayout("Svelte loop property must be a literal scalar")
        return format_literal(value)

    def replace_property(match):
        value = item.get(match.group(1))
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise UnsupportedLayout("Svelte arithmetic requires numeric literal properties")
        return format_literal(value)

    numeric = re.sub(re.escape(variable) + r"\.([A-Za-z][\w-]*)", replace_property, expression)
    if not re.fullmatch(r"[0-9+*/(). \t-]+", numeric):
        raise UnsupportedLayout("Unsupported Svelte expression")

    def calculate(node):
        if isinstance(node, ast.Expression):
            return calculate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = calculate(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
        ):
            left, right = calculate(node.left), calculate(node.right)
            return {
                ast.Add: left + right,
                ast.Sub: left - right,
                ast.Mult: left * right,
                ast.Div: left / right,
            }[type(node.op)]
        raise UnsupportedLayout("Unsupported Svelte numeric expression")

    try:
        value = calculate(ast.parse(numeric, mode="eval"))
    except (ArithmeticError, SyntaxError, TypeError, ValueError) as exc:
        raise UnsupportedLayout("Invalid Svelte numeric expression") from exc
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise UnsupportedLayout("Invalid Svelte numeric expression")
    return format_literal(value)


def expand_svelte_svg(block: str) -> str:
    """Expand only literal `{#each [...] as x}` SVG loops; never execute Svelte/JS."""

    def replace_svg(match):
        opening, body = match.groups()
        while (each := EACH_ITEMS.search(body)) is not None:
            close = body.find("{/each}", each.end())
            if close == -1:
                raise UnsupportedLayout("Unclosed Svelte each block")
            template = body[each.end() : close]
            variable_match = re.search(r"as\s+(\w+)\}", each.group(0))
            if variable_match is None:
                raise UnsupportedLayout("Invalid Svelte each variable")
            variable = variable_match.group(1)
            expanded = []
            for item in literal_each_items("[" + each.group(1) + "]"):

                def replace_expression(expression):
                    value = evaluate_loop_expression(expression.group(1), variable, item)
                    attribute_value = (
                        expression.start() > 0 and template[expression.start() - 1] == "="
                    )
                    if not attribute_value:
                        return value
                    if re.fullmatch(r"-?\d+(?:\.\d+)?", value):
                        return "{" + value + "}"
                    return json.dumps(value)

                expanded.append(re.sub(r"\{([^{}]+)\}", replace_expression, template))
            body = body[: each.start()] + "".join(expanded) + body[close + len("{/each}") :]
        if re.search(r"\{[#/:]|\{\s*[A-Za-z_]", body):
            raise UnsupportedLayout("SVG contains unsupported dynamic expressions")
        return opening + body + "</svg>"

    return SVELTE_SVG.sub(replace_svg, block)


def safe_svg(node: Node) -> str:
    allowed = {
        "svg": {"viewBox", "width", "height", "fill", "stroke", "role", "aria-label"},
        "rect": {
            "x",
            "y",
            "width",
            "height",
            "rx",
            "fill",
            "fillOpacity",
            "stroke",
            "strokeOpacity",
            "strokeWidth",
        },
        "path": {"d", "fill", "stroke", "strokeWidth", "strokeLinecap", "opacity"},
        "g": {"fill", "stroke", "strokeWidth", "strokeLinecap", "opacity"},
        "text": {"x", "y", "fill", "fontSize", "fontWeight", "textAnchor", "opacity"},
    }
    if node.tag not in allowed:
        raise UnsupportedLayout("Unsupported SVG element")
    attrs = " ".join(
        f'{name}="{html.escape(value, quote=True)}"'
        for name, value in node.attrs.items()
        if name in allowed[node.tag]
    )
    content = "".join(
        plain(child) if isinstance(child, str) else safe_svg(child) for child in node.children
    )
    if node.tag in {"rect", "path"}:
        if content.strip():
            raise UnsupportedLayout("SVG shape cannot have child content")
        return f"<{node.tag} {attrs}/>"
    return f"<{node.tag} {attrs}>{content}</{node.tag}>"


def markdown(node: Node) -> str:
    nodes = [c for c in node.children if isinstance(c, Node)]
    if node.tag == "grid" and nodes and all(c.tag in {"box", "grid-item"} for c in nodes):
        rows = []
        for card in nodes:
            children = [c for c in card.children if isinstance(c, Node)]
            labels = [c for c in children if c.tag in {"text", "caption"}]
            values = [c for c in children if c.tag == "title"]
            if (
                len(children) != 2
                or len(labels) != 1
                or len(values) != 1
                or any(isinstance(c, str) and c.strip() for c in card.children)
            ):
                break
            rows.append([inline(labels[0]), "**" + inline(values[0]) + "**"])
        else:
            if not any(isinstance(c, str) and c.strip() for c in node.children):
                return table(rows, ["Measure", "Value"])
    if (
        node.tag == "table"
        and nodes
        and all(c.tag == "table-row" for c in nodes)
        and not any(isinstance(c, str) and c.strip() for c in node.children)
        and all(
            all(
                (isinstance(c, Node) and c.tag == "table-cell")
                or (isinstance(c, str) and not c.strip())
                for c in row.children
            )
            for row in nodes
        )
    ):
        rows = [
            [inline(c) for c in row.children if isinstance(c, Node) and c.tag == "table-cell"]
            for row in nodes
        ]
        if rows and rows[0] and all(len(row) == len(rows[0]) for row in rows):
            return table(rows, [f"Column {i + 1}" for i in range(len(rows[0]))])
    if node.tag in {"divider", "spacer", "icon"} and any(
        isinstance(c, Node) or c.strip() for c in node.children
    ):
        raise UnsupportedLayout("Unexpected content in a leaf component")
    if node.tag == "divider":
        return "---"
    if node.tag == "spacer":
        return ""
    if node.tag == "icon":
        name = node.attrs.get("name", "icon")
        # Textual label rather than guessing what an unknown icon looks like.
        return "[" + plain(name).replace("[", "\\[").replace("]", "\\]") + "]"
    if node.tag == "svg":
        return safe_svg(node)
    if node.tag == "b":
        text = inline(node)
        return "**" + text + "**" if text else ""
    if node.tag == "Entity":
        value = node.attrs.get("value")
        if not isinstance(value, str) or not value.strip():
            raise UnsupportedLayout("Entity needs a literal value")
        return plain(value)
    if node.tag == "Cite":
        return "*(citation: " + ", ".join(citation_labels(node)) + ")*"
    if node.tag == "Link":
        label = plain(node.attrs.get("title") or inline(node) or safe_url(node.attrs.get("url")))
        label = re.sub(r"([\\`*_{}\[\]()!])", r"\\\1", label)
        return "[" + label + "](<" + safe_url(node.attrs.get("url")) + ">)"
    if node.tag in {"text", "caption", "title", "badge", "table-cell"}:
        text = inline(node)
        if node.tag in {"title", "badge"} or node.attrs.get("weight") in {"bold", "semibold"}:
            return "**" + text + "**" if text else ""
        return text
    parts = [plain(c).strip() if isinstance(c, str) else markdown(c) for c in node.children]
    parts = [part for part in parts if part]
    return (" · " if node.tag == "row" and all("\n" not in p for p in parts) else "\n\n").join(
        parts
    )


def integer(value: str | None, default: int, maximum: int) -> int:
    if value is None or not re.fullmatch(r"\d{1,3}", value):
        return default
    try:
        return min(int(value), maximum)
    except ValueError:
        return default


def static_html(node: Node) -> str:
    """Emit only generated tags/styles and escaped text. Never forward arbitrary attributes."""
    tag = "div"
    styles = []
    if node.tag == "grid":
        columns = max(1, integer(node.attrs.get("columns"), 2, 6))
        styles.extend(["display:grid", f"grid-template-columns:repeat({columns},minmax(0,1fr))"])
    elif node.tag in {"box", "grid-item", "row"}:
        styles.extend(
            [
                "display:flex",
                "flex-direction:row"
                if node.tag == "row" or node.attrs.get("direction") == "row"
                else "flex-direction:column",
            ]
        )
    elif node.tag == "divider":
        return "<hr>"
    elif node.tag == "spacer":
        return '<div style="height:1rem"></div>'
    elif node.tag == "table":
        tag = "table"
    elif node.tag == "table-row":
        tag = "tr"
    elif node.tag == "table-cell":
        tag = "td"
    elif node.tag in {"text", "caption", "badge", "icon", "Link", "Entity", "Cite", "b"}:
        tag = "a" if node.tag == "Link" else "span"
    if node.tag in {"title", "badge"} or node.attrs.get("weight") in {"bold", "semibold"}:
        styles.append("font-weight:700")
    if node.attrs.get("tabularNums") == "true":
        styles.append("font-variant-numeric:tabular-nums")
    if "gap" in node.attrs:
        styles.append(f"gap:{integer(node.attrs['gap'], 2, 12) * 0.25}rem")
    if "padding" in node.attrs:
        styles.append(f"padding:{integer(node.attrs['padding'], 3, 16) * 0.25}rem")
    if node.attrs.get("background") in {"surface-secondary", "surface-tertiary"}:
        styles.append("background:var(--surface)")
    if node.attrs.get("radius") in {"sm", "md", "lg", "xl", "full"}:
        styles.append("border-radius:0.75rem")
    if node.attrs.get("color") == "secondary" or node.tag == "caption":
        styles.append("color:var(--secondary)")
    sizes = {
        "xs": ".75rem",
        "sm": ".875rem",
        "md": "1rem",
        "lg": "1.25rem",
        "xl": "1.75rem",
        "2xl": "2rem",
    }
    if node.attrs.get("size") in sizes:
        styles.append("font-size:" + sizes[node.attrs["size"]])
    if node.attrs.get("textAlign") in {"left", "right", "center"}:
        styles.append("text-align:" + node.attrs["textAlign"])
    content = "".join(
        html.escape(html.unescape(c)) if isinstance(c, str) else static_html(c)
        for c in node.children
    )
    if node.tag == "svg":
        return safe_svg(node)
    if node.tag == "icon":
        content = html.escape(node.attrs.get("name", "icon"))
    if node.tag == "Link":
        content = html.escape(
            node.attrs.get("title") or inline(node) or safe_url(node.attrs.get("url"))
        )
        return f'<a href="{html.escape(safe_url(node.attrs.get("url")), quote=True)}" rel="noreferrer" style="{";".join(styles)}">{content}</a>'
    if node.tag == "Entity":
        return "<span>" + html.escape(markdown(node)) + "</span>"
    if node.tag == "Cite":
        return "<span>" + html.escape(markdown(node)) + "</span>"
    return f'<{tag} style="{";".join(styles)}">{content}</{tag}>'


def fence(text: str, language: str) -> str:
    longest = max((len(m.group()) for m in re.finditer(r"`+", text)), default=0)
    marker = "`" * max(3, longest + 1)
    return f"{marker}{language}\n{text}\n{marker}"


def render_layouts(text: str, mode: str = "markdown") -> str:
    if mode not in {"markdown", "html", "source"}:
        raise ValueError("Unknown layout rendering mode")
    if mode == "source":
        return text
    output = []
    pos = 0
    while match := CANDIDATE.search(text, pos):
        output.append(text[pos : match.start()])
        token = match.group()
        opening = re.match(r" {0,3}(`{3,}|~{3,})", token)
        if opening:
            mark = opening.group(1)
            closing = re.compile(
                r"(?m)^ {0,3}" + re.escape(mark[0]) + "{" + str(len(mark)) + r",}[ \t]*(?:\r?\n|$)"
            ).search(text, match.end())
            end = closing.end() if closing else len(text)
            output.append(text[match.start() : end])
        elif token.startswith("`"):
            closing = re.compile(r"(?<!`)" + re.escape(token) + r"(?!`)").search(text, match.end())
            end = closing.end() if closing else match.end()
            output.append(text[match.start() : end])
        elif token.startswith(("    ", "\t")):
            end = match.end()
            output.append(token)
        else:
            end = block_end(text, match.start())
            block = text[match.start() : end]
            inline_component = False
            try:
                root = parse(expand_svelte_svg(block))
                inline_component = root.tag in {"Entity", "Cite"}
                rendered = markdown(root)
                if mode == "html":
                    document = (
                        '<!doctype html><html><head><meta charset="utf-8">'
                        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
                        "<style>:root{color-scheme:light dark;--surface:light-dark(#f3f4f6,#25262a);--secondary:light-dark(#666,#bbb)}"
                        "body{font-family:system-ui;padding:1rem}td{padding:.5rem}</style></head><body>"
                        + static_html(root)
                        + "</body></html>"
                    )
                    rendered += "\n\n" + fence(document, "html")
            except UnsupportedLayout:
                rendered = "Unsupported layout (original source):\n\n" + fence(block, "text")
            output.append(rendered if inline_component else "\n\n" + rendered + "\n\n")
        pos = end
    output.append(text[pos:])
    return "".join(output)
