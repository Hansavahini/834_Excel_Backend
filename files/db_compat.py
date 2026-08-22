"""
Version shim for CheckConstraint.

Django renamed the CheckConstraint keyword from `check` to `condition` in 5.1
and dropped `check` entirely in 6.0. Every models module in this project builds
its check constraints through `check_constraint()` so the same source runs on
4.2 LTS, 5.x and 6.x without edits.
"""

import django
from django.db import models

_USES_CONDITION = django.VERSION >= (5, 1)


def check_constraint(*, condition, name, violation_error_message=None):
    kwargs = {"name": name}
    if violation_error_message:
        kwargs["violation_error_message"] = violation_error_message
    if _USES_CONDITION:
        kwargs["condition"] = condition
    else:
        kwargs["check"] = condition
    return models.CheckConstraint(**kwargs)
