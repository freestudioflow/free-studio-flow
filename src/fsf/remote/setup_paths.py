"""Explicit, portable workdir argv for opt-in setup commands."""
import re
from pathlib import Path, PureWindowsPath

from fsf.core.asset_library import AssetValidationError, _no_links, _path_parts
from fsf.remote.contracts import RemoteError


def setup_argv(args, root, replacements):
    if not any('{workdir' in arg for arg in args):
        return None
    root = Path(root).absolute() if root is not None else None
    resolved = []
    try:
        if root is not None:
            _no_links(root)
        for arg in args:
            if arg == '{workdir}' or arg.startswith('{workdir}/'):
                suffix = arg.removeprefix('{workdir}/') if arg != '{workdir}' else None
                if suffix is not None and ('{' in suffix or '}' in suffix):
                    raise ValueError()
                parts = () if suffix is None else _path_parts(suffix)
                if root is not None:
                    target = root.joinpath(*parts)
                    for item in (target, *target.parents):
                        if item.exists() or item.is_symlink():
                            _no_links(item)
                            break
                    target.resolve().relative_to(root.resolve())
                    resolved.append(str(target))
            elif arg == '{python}':
                resolved.append(replacements['python'])
            elif ('{' in arg or '}' in arg or PureWindowsPath(arg).drive
                    or any(part == '..' for part in re.split(r'[/\\]', arg))
                    or re.search(r'(^|=)(?:[A-Za-z]:|[/\\])', arg)):
                raise ValueError()
            else:
                resolved.append(arg)
    except (AssetValidationError, OSError, ValueError, KeyError):
        raise RemoteError('REMOTE_SETUP_PATH') from None
    return resolved
