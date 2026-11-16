"""The environment handed to commands the runtime and checker launch.

Commands run on the model's behalf must not see the provider credential the
harness itself uses: anything they print is stored as evidence and returned to
the model. Child processes therefore get the parent environment with the
configured key variable and every ``VHARNESS_*``/``OPENAI_*`` variable removed,
or, when the operator names an allow-list, only the listed variables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from ..errors import ContractError

#: Variable-name prefixes withheld from child processes by default.
WITHHELD_PREFIXES = ("VHARNESS_", "OPENAI_")

_POLICY = "provider-secrets-withheld-v1"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class ChildEnvironment:
    """How to derive a child process environment from the parent's.

    ``provider_key_env`` names the variable holding the provider key; it never
    reaches a child, even through ``allow``. ``allow`` of ``None`` passes every
    other variable except the withheld prefixes; a tuple passes only the listed
    names. The key's variable name is deliberately absent from ``describe()``
    because an operator may change it between resumes of the same session.
    """

    provider_key_env: str | None = None
    allow: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.provider_key_env is not None and not _ENV_NAME.match(
            self.provider_key_env
        ):
            raise ContractError("provider_key_env must be an environment variable name")
        if self.allow is None:
            return
        for name in self.allow:
            if not _ENV_NAME.match(name):
                raise ContractError(
                    f"child environment allow-list entry {name!r} is not a "
                    "variable name"
                )
        if self.provider_key_env is not None and self.provider_key_env in self.allow:
            raise ContractError(
                f"the provider key variable {self.provider_key_env!r} cannot be "
                "passed to child processes"
            )

    def build(self, parent: Mapping[str, str]) -> dict[str, str]:
        """Return the environment for one child process."""
        if self.allow is not None:
            return {name: parent[name] for name in self.allow if name in parent}
        return {
            name: value
            for name, value in parent.items()
            if name != self.provider_key_env and not name.startswith(WITHHELD_PREFIXES)
        }

    def describe(self) -> dict[str, object]:
        """Return the policy for a component identity, without the key's name."""
        return {
            "policy": _POLICY,
            "withheld_prefixes": list(WITHHELD_PREFIXES),
            "allow": None if self.allow is None else sorted(self.allow),
        }
