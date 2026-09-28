"""Разбор robots.txt с поддержкой шаблонов `*` и `$` (как у Google и Seznam).

Стандартный urllib.robotparser понимает только префиксы, поэтому правила вида
`Disallow: /*hledat=` у Bazos он бы пропустил.

Решение по пути: среди подходящих правил побеждает самое длинное; при равной длине — Allow.
Учитываются только группы `User-agent: *` — поимённые правила для чужих роботов нас не касаются.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = re.escape(pattern[:-1] if anchored else pattern).replace(r"\*", ".*")
    return re.compile("^" + body + ("$" if anchored else ""))


@dataclass
class RobotsRules:
    """Правила одной группы robots.txt: список (allow, шаблон, регулярное выражение)."""

    rules: list[tuple[bool, str, re.Pattern[str]]] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str, agent: str = "*") -> RobotsRules:
        rules: list[tuple[bool, str, re.Pattern[str]]] = []
        group_agents: list[str] = []
        in_rules = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if in_rules:  # новая группа начинается после правил предыдущей
                    group_agents, in_rules = [], False
                group_agents.append(value.lower())
            elif key in ("allow", "disallow"):
                in_rules = True
                if agent in group_agents and value:  # пустой Disallow ничего не запрещает
                    rules.append((key == "allow", value, _compile(value)))
        return cls(rules)

    def is_allowed(self, path: str) -> bool:
        """path — путь с query-строкой, например '/20/' или '/?hledat=iphone'."""
        best: tuple[bool, str] | None = None
        for allow, pattern, rx in self.rules:
            if not rx.match(path):
                continue
            if best is None or len(pattern) > len(best[1]) or (len(pattern) == len(best[1]) and allow):
                best = (allow, pattern)
        return True if best is None else best[0]
