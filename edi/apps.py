"""
edi app configuration.

ready() runs a recovery sweep, which is the piece that turns "my file has said
CONVERTING for two days" into a self-correcting condition. A process that is
killed mid-conversion - a deploy, a container eviction, an OOM - leaves a
ProcessingJob at RUNNING and an UploadedFile at CONVERTING, and neither can move
again on its own because the thread that owned them no longer exists. On the
next start there are by definition no in-flight jobs from the previous process,
so every active row is orphaned and is released.
"""

import logging
import os
import sys

from django.apps import AppConfig

logger = logging.getLogger("edi.apps")


class EdiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "edi"

    def ready(self):
        # Not during migrate, makemigrations, collectstatic or test collection.
        # Touching the jobs table before it exists is a crash on a fresh
        # database, and a sweep is meaningless in a management command anyway.
        skip = {
            "migrate",
            "makemigrations",
            "collectstatic",
            "shell",
            "test",
            "createsuperuser",
            "bootstrap_users",
            "seed_segment_elements",
        }
        if any(arg in skip for arg in sys.argv):
            return

        # The sweep needs the database, and Django rightly objects to queries in
        # ready() - at that point the connection settings are loaded but the app
        # registry is still being built, and a query here is the classic way to
        # make a process that cannot start against a cold database. So it is
        # hung off the first request instead, where everything is up, and
        # disconnected immediately after: it is a one-shot, not a hook.
        from django.core.signals import request_started

        def sweep_once(**_kwargs):
            request_started.disconnect(sweep_once)
            try:
                from edi.services.runner import reap_stale

                released = reap_stale(include_queued=True)
                if released:
                    logger.warning(
                        "Released %d job(s) left in flight by a previous process.",
                        released,
                    )
            except Exception:  # noqa: BLE001 - never break a request over this
                logger.debug("Startup job sweep skipped.", exc_info=True)

        request_started.connect(sweep_once, weak=False)
