"""
Resolve a client-supplied storage path to a real path on disk, safely.

This is the fix for the most serious defect in the original workflow. The upload
endpoint returned a storage-relative path such as "uploads/OOE20250701.X12", the
convert endpoint took that string straight from the request body and handed it
to open(). Two consequences.

The benign one: the path is relative to MEDIA_ROOT, but open() resolves it
against the process working directory, so every conversion raised
FileNotFoundError unless the server happened to be started from inside media/.
That is verifiable — the endpoint could not have worked as written.

The serious one: the string came from the request and was never checked, so
"../../../../etc/passwd" was a valid file_path, and so was any other upload on
the box. On a system holding PHI that is a reportable breach, not a bug.

get_file_path() now refuses anything that escapes MEDIA_ROOT.
"""

from __future__ import annotations

import os

from django.conf import settings


class UnsafePathError(ValueError):
    """The requested path resolves outside the media root."""


def media_root() -> str:
    return os.path.realpath(str(settings.MEDIA_ROOT))


def get_file_path(relative_path: str, must_exist: bool = True) -> str:
    """
    Turn a storage-relative path into an absolute one inside MEDIA_ROOT.

    Raises UnsafePathError for absolute paths, traversal, symlinks pointing out
    of the tree, and anything else that would let a caller read a file the
    application did not put there.
    """
    if not relative_path or not str(relative_path).strip():
        raise UnsafePathError("No file path was supplied.")

    candidate = str(relative_path).strip().replace("\\", "/").lstrip("/")

    if os.path.isabs(relative_path) or (len(str(relative_path)) > 1 and str(relative_path)[1] == ":"):
        raise UnsafePathError("Absolute paths are not accepted; supply the path returned by the upload endpoint.")

    if "\x00" in candidate:
        raise UnsafePathError("Invalid characters in file path.")

    root = media_root()
    resolved = os.path.realpath(os.path.join(root, candidate))

    # commonpath rather than startswith: "/media-evil" starts with "/media".
    try:
        if os.path.commonpath([resolved, root]) != root:
            raise UnsafePathError("File path resolves outside the media directory.")
    except ValueError:  # different drives on Windows
        raise UnsafePathError("File path resolves outside the media directory.")

    if must_exist and not os.path.isfile(resolved):
        raise UnsafePathError("No such file: {path}".format(path=candidate))

    return resolved


def relative_to_media(absolute_path: str) -> str:
    return os.path.relpath(os.path.realpath(absolute_path), media_root()).replace(os.sep, "/")
