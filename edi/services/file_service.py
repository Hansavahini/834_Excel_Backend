import os
from django.conf import settings


def get_file_path(relative_path):

    return os.path.join(
        settings.MEDIA_ROOT,
        relative_path
    )