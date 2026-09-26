"""Migration contract for replacing legacy ``admin`` data safely."""

from __future__ import annotations

import importlib
from typing import Any


class _OperationsRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def drop_constraint(self, *args: Any, **_kwargs: Any) -> None:
        self.calls.append(("drop_constraint", args))

    def execute(self, statement: Any) -> None:
        self.calls.append(("execute", (str(statement),)))

    def create_check_constraint(self, *args: Any, **_kwargs: Any) -> None:
        self.calls.append(("create_check_constraint", args))


def test_legacy_admin_rows_are_mapped_to_provisioner_before_new_constraint(
    monkeypatch: Any,
) -> None:
    migration = importlib.import_module("migrations.versions.c3d4e5f6a7b8_organisation_roles")
    recorder = _OperationsRecorder()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert recorder.calls[0][0] == "drop_constraint"
    assert recorder.calls[1] == (
        "execute",
        ("UPDATE users SET system_role = 'provisioner' WHERE system_role = 'admin'",),
    )
    assert recorder.calls[2] == (
        "create_check_constraint",
        (
            "ck_users_system_role",
            "users",
            "system_role IS NULL OR system_role IN ('provisioner', 'case_head')",
        ),
    )
