"""Filesystem spelling helpers; these do not replace path authorization checks."""
import os


def native_path(path):
    """Use the Win32 extended absolute spelling for long filesystem operations."""
    value=os.fspath(path)
    if os.name != "nt":return value
    value=os.path.abspath(value)
    if value.startswith("\\\\?\\"):return value
    if value.startswith("\\\\"):return "\\\\?\\UNC\\"+value[2:]
    return "\\\\?\\"+value
