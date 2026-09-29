"""Персонализация письма.

Плейсхолдеры: ``{{contact.NAME}}``, ``{{deal.TITLE}}``, ``{{deal.UF_CRM_...}}``,
со значением по умолчанию: ``{{contact.NAME|default:"коллега"}}``.
В HTML значения экранируются; в теме письма — нет.
"""

from __future__ import annotations

import html
import re

PLACEHOLDER = re.compile(r"\{\{\s*(contact|deal)\.([A-Za-z0-9_]+)\s*(?:\|\s*default\s*:\s*\"([^\"]*)\"\s*)?\}\}")
ANY_BRACES = re.compile(r"\{\{.*?\}\}", re.S)


class TemplateError(ValueError):
    pass


def placeholders(*texts: str) -> set[tuple[str, str]]:
    found = set()
    for t in texts:
        for m in PLACEHOLDER.finditer(t or ""):
            found.add((m.group(1), m.group(2)))
        for m in ANY_BRACES.finditer(PLACEHOLDER.sub("", t or "")):
            raise TemplateError(f"непонятный плейсхолдер {m.group(0)}; формат: {{{{contact.NAME}}}} или {{{{deal.TITLE}}}}")
    return found


def _value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, list):  # мультиполя EMAIL/PHONE и множественные UF
        return ", ".join(_value(x.get("VALUE") if isinstance(x, dict) else x) for x in v)
    return str(v)


def render(text: str, context: dict[str, dict], escape: bool) -> str:
    def sub(m: re.Match) -> str:
        entity, fld, default = m.group(1), m.group(2), m.group(3)
        val = _value((context.get(entity) or {}).get(fld)).strip()
        if not val:
            val = default or ""
        return html.escape(val) if escape else val

    return PLACEHOLDER.sub(sub, text)


def html_to_text(body: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", body)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()
