"""Bounded Win32 read leases. No stat-only content cache or cross-batch trust."""
import hashlib
import os
from pathlib import Path


class WindowsReadSet:
    """Hold parent names and verified file bytes against ordinary write/rename.

    Privileged kernel changes are outside this guarantee. POSIX callers keep
    fresh per-call hashing instead of claiming equivalent locks from flock.
    """
    def __init__(self):
        if os.name != 'nt':
            raise OSError('Win32 leases unavailable')
        import ctypes
        from ctypes import wintypes as w
        self.ctypes, self.w = ctypes,w
        self.kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateFileW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE]
        self.kernel.CreateFileW.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.GetFileInformationByHandle.argtypes = [w.HANDLE,w.LPVOID]
        class Info(ctypes.Structure):
            _fields_ = [('attributes',w.DWORD),('created',w.FILETIME),('accessed',w.FILETIME),
                        ('written',w.FILETIME),('volume',w.DWORD),('size_high',w.DWORD),('size_low',w.DWORD),
                        ('links',w.DWORD),('index_high',w.DWORD),('index_low',w.DWORD)]
        self.Info = Info
        self.directories, self.files = {},{}

    def _open(self,path,*,directory):
        from fsf.core.filesystem import native_path
        c,w = self.ctypes,self.w
        # Attribute-only directory handles do not enforce delete sharing on
        # Windows. FILE_LIST_DIRECTORY is needed for the name lease.
        handle = self.kernel.CreateFileW(native_path(path),0x81 if directory else 0x80000000,
            3 if directory else 1,None,3,0x00200000 | (0x02000000 if directory else 0),None)
        if handle == w.HANDLE(-1).value:
            raise c.WinError(c.get_last_error())
        info = self.Info()
        if (not self.kernel.GetFileInformationByHandle(handle,c.byref(info))
                or info.attributes & 0x400 or bool(info.attributes & 0x10)!=directory):
            self.kernel.CloseHandle(handle)
            raise OSError('Unsupported read lease target')
        return handle

    @staticmethod
    def _key(path):
        return os.path.normcase(os.path.abspath(path))

    def add(self,path):
        import msvcrt
        absolute = Path(os.path.abspath(path))
        key = self._key(absolute)
        if key in self.files:
            return self.files[key]
        self.hold_directory(absolute.parent)
        handle = self._open(absolute,directory=False)
        try:
            descriptor = msvcrt.open_osfhandle(handle,os.O_RDONLY | os.O_BINARY)
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise
        stream = os.fdopen(descriptor,'rb')
        self.files[key] = stream
        return stream

    def hold_directory(self, path):
        """Protect every ancestor name, not a cached stat result."""
        absolute = Path(os.path.abspath(path))
        for parent in reversed((absolute, *absolute.parents)):
            parent_key = self._key(parent)
            if parent_key not in self.directories:
                self.directories[parent_key] = self._open(parent,directory=True)

    def read_json_once(self, path):
        """Read one current reply while locked; release its leaf before reuse."""
        import msvcrt
        from fsf.core.edit_store import decode_json, MAX_JSON_BYTES
        absolute = Path(os.path.abspath(path))
        self.hold_directory(absolute.parent)
        handle = self._open(absolute, directory=False)
        try:
            descriptor = msvcrt.open_osfhandle(handle,os.O_RDONLY | os.O_BINARY)
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise
        with os.fdopen(descriptor,'rb') as stream:
            return decode_json(stream.read(MAX_JSON_BYTES+1))

    def hash(self,path):
        stream = self.add(path)
        stream.seek(0)
        result = hashlib.sha256()
        while block := stream.read(1024*1024):
            result.update(block)
        stream.seek(0)
        return result.hexdigest()

    def json(self,path):
        from fsf.core.edit_store import decode_json, MAX_JSON_BYTES
        stream = self.add(path)
        stream.seek(0)
        value = decode_json(stream.read(MAX_JSON_BYTES+1))
        stream.seek(0)
        return value

    def close(self):
        for stream in self.files.values():
            stream.close()
        self.files.clear()
        for handle in reversed(list(self.directories.values())):
            self.kernel.CloseHandle(handle)
        self.directories.clear()
