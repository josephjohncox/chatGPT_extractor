"""Render only reasoning actually present in ChatGPT exports as saved-history UI blocks."""

import html
import json
import math
import re

TAG_NAMES = ("think", "thinking", "reason", "reasoning", "thought")
CANDIDATE = re.compile(
    r"(?m)^ {0,3}(?:`{3,}|~{3,})[^\r\n]*(?:\r?\n|$)|`+|^(?: {4}|\t)[^\r\n]*(?:\r?\n|$)|"
    r"<(?:think|thinking|reason|reasoning|thought)>|<\|begin_of_thought\|>"
)


def reasoning_block(body: str, title: str, duration=None) -> str:
    attrs = 'type="reasoning" done="true"'
    if (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and math.isfinite(duration)
        and duration >= 0
    ):
        attrs += f' duration="{duration}"'
    # The saved-history tokenizer decodes the block body. Escape first so exported
    # </details> text cannot terminate the surrounding block during tokenization.
    escaped = html.escape(body, quote=False)
    quoted = "\n".join("> " + line for line in escaped.split("\n"))
    return f"<details {attrs}>\n<summary>{title}</summary>\n\n{quoted}\n\n</details>"


def convert_inline_reasoning(text: str) -> str:
    """Convert complete protocol tags, not examples inside Markdown code."""
    output = []
    pos = 0
    while match := CANDIDATE.search(text, pos):
        output.append(text[pos : match.start()])
        token = match.group()
        opening = re.match(r" {0,3}(`{3,}|~{3,})", token)
        if opening:
            mark = opening.group(1)
            close = re.compile(
                r"(?m)^ {0,3}" + re.escape(mark[0]) + "{" + str(len(mark)) + r",}[ \t]*(?:\r?\n|$)"
            ).search(text, match.end())
            end = close.end() if close else len(text)
            output.append(text[match.start() : end])
        elif token.startswith("`"):
            close = re.compile(r"(?<!`)" + re.escape(token) + r"(?!`)").search(text, match.end())
            end = close.end() if close else match.end()
            output.append(text[match.start() : end])
        elif token.startswith(("    ", "\t")):
            end = match.end()
            output.append(token)
        else:
            closing = "<|end_of_thought|>" if token == "<|begin_of_thought|>" else "</" + token[1:]
            stop = text.find(closing, match.end())
            if stop == -1:
                end = match.end()
                output.append(token)  # Incomplete tags are not guessed/repaired.
            else:
                end = stop + len(closing)
                body = text[match.end() : stop]
                output.append("\n\n" + reasoning_block(body, "Imported reasoning") + "\n\n")
        pos = end
    output.append(text[pos:])
    return "".join(output)


def render_reasoning(message: dict, rendered: str) -> str:
    """Keep separate source nodes/branches; do not invent provider-native reasoning state."""
    if (message.get("author") or {}).get("role") != "assistant":
        return rendered
    content = message.get("content") or {}
    meta = message.get("metadata") or {}
    kind = content.get("content_type")
    if kind == "thoughts":
        thoughts = content.get("thoughts")
        parts = []
        if isinstance(thoughts, list):
            for thought in thoughts:
                if isinstance(thought, dict):
                    summary, body = thought.get("summary"), thought.get("content")
                    if isinstance(summary, str) and summary.strip():
                        parts.append(summary)
                    if isinstance(body, str) and body.strip() and body != summary:
                        parts.append(body)
                    if not isinstance(summary, str) and not isinstance(body, str):
                        parts.append(json.dumps(thought, ensure_ascii=False))
                else:
                    parts.append(json.dumps(thought, ensure_ascii=False))
        if not parts:
            # Preserve unfamiliar/empty shapes without claiming missing internal reasoning exists.
            parts = [
                "No readable reasoning text was included. Exported record:\n"
                + json.dumps(content, ensure_ascii=False)
            ]
        return reasoning_block(
            "\n\n".join(parts),
            "Imported ChatGPT reasoning summary",
            meta.get("finished_duration_sec"),
        )
    if kind == "reasoning_recap":
        body = content.get("content")
        if not isinstance(body, str):
            body = json.dumps(content, ensure_ascii=False)
        title = "Imported ChatGPT reasoning recap"
        if meta.get("reasoning_status") == "reasoning_cancelled":
            title += " (cancelled)"
        return reasoning_block(body, title, meta.get("finished_duration_sec"))
    # Do not relabel tool invocations or ordinary commentary as reasoning merely
    # because they occurred during a thinking turn.
    if kind == "code" or message.get("recipient") not in {None, "all"}:
        return rendered
    if (
        message.get("channel") == "analysis"
        or meta.get("is_thinking_preamble_message") is True
        or meta.get("reasoning_status") == "is_reasoning"
    ):
        return reasoning_block(
            rendered, "Imported ChatGPT reasoning", meta.get("finished_duration_sec")
        )
    return convert_inline_reasoning(rendered)
