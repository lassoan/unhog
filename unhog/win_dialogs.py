"""Native Windows folder picker (IFileOpenDialog with FOS_PICKFOLDERS) via ctypes.

No third-party dependency: the COM interface is driven through raw vtable
calls. ``pick_folder`` returns the chosen path, or ``None`` if the user
cancelled. It raises ``OSError`` on non-Windows platforms or COM failures so
callers can fall back to another dialog.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import POINTER, byref, c_void_p, c_wchar_p
from typing import Optional

if sys.platform == "win32":
    from ctypes import wintypes

    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

        @classmethod
        def from_str(cls, text: str) -> "GUID":
            g = cls()
            ole32.CLSIDFromString(c_wchar_p(text), byref(g))
            return g

    CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
    IID_IFileOpenDialog = "{D57C7288-D4AD-4768-BE02-9D969532D960}"
    IID_IShellItem = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"

    CLSCTX_INPROC_SERVER = 0x1
    COINIT_APARTMENTTHREADED = 0x2
    RPC_E_CHANGED_MODE = -2147417850  # 0x80010106
    HRESULT_CANCELLED = -2147023673   # 0x800704C7, HRESULT_FROM_WIN32(ERROR_CANCELLED)
    FOS_PICKFOLDERS = 0x20
    FOS_FORCEFILESYSTEM = 0x40
    FOS_PATHMUSTEXIST = 0x800
    SIGDN_FILESYSPATH = 0x80058000

    # vtable slots (IUnknown: 0-2; IModalWindow::Show: 3; IFileDialog: 4-26)
    VT_RELEASE = 2
    VT_SHOW = 3
    VT_SETOPTIONS = 9
    VT_GETOPTIONS = 10
    VT_SETFOLDER = 12
    VT_SETTITLE = 17
    VT_GETRESULT = 20
    VT_SHELLITEM_GETDISPLAYNAME = 5

    def _method(obj: c_void_p, index: int, *argtypes):
        """Bind vtable slot ``index`` of COM object ``obj`` as a callable."""
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
        proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)
        return proto(vtbl[index])

    def _release(obj: c_void_p) -> None:
        if obj:
            _method(obj, VT_RELEASE)(obj)

    def _find_owner(title: str) -> int:
        return user32.FindWindowW(None, title) or 0

    def pick_folder(initial: Optional[str] = None, title: str = "Select folder",
                    owner_title: Optional[str] = None) -> Optional[str]:
        hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        if hr < 0 and hr != RPC_E_CHANGED_MODE:
            raise OSError(f"CoInitializeEx failed: 0x{hr & 0xFFFFFFFF:08X}")
        uninit = hr >= 0

        dialog = c_void_p()
        try:
            hr = ole32.CoCreateInstance(byref(GUID.from_str(CLSID_FileOpenDialog)), None,
                                        CLSCTX_INPROC_SERVER, byref(GUID.from_str(IID_IFileOpenDialog)),
                                        byref(dialog))
            if hr < 0 or not dialog:
                raise OSError(f"CoCreateInstance(FileOpenDialog) failed: 0x{hr & 0xFFFFFFFF:08X}")

            opts = wintypes.DWORD()
            _method(dialog, VT_GETOPTIONS, POINTER(wintypes.DWORD))(dialog, byref(opts))
            _method(dialog, VT_SETOPTIONS, wintypes.DWORD)(
                dialog, opts.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST)
            _method(dialog, VT_SETTITLE, c_wchar_p)(dialog, title)

            if initial:
                item = c_void_p()
                hr = shell32.SHCreateItemFromParsingName(c_wchar_p(initial), None,
                                                         byref(GUID.from_str(IID_IShellItem)), byref(item))
                if hr >= 0 and item:
                    _method(dialog, VT_SETFOLDER, c_void_p)(dialog, item)
                    _release(item)

            owner = _find_owner(owner_title) if owner_title else 0
            try:
                # ctypes raises OSError for failed HRESULTs; cancelling is one of them.
                _method(dialog, VT_SHOW, wintypes.HWND)(dialog, owner)
            except OSError as exc:
                if exc.winerror == HRESULT_CANCELLED:
                    return None
                raise

            result = c_void_p()
            _method(dialog, VT_GETRESULT, POINTER(c_void_p))(dialog, byref(result))
            try:
                name = c_wchar_p()
                _method(result, VT_SHELLITEM_GETDISPLAYNAME, ctypes.c_int, POINTER(c_wchar_p))(
                    result, SIGDN_FILESYSPATH, byref(name))
                path = name.value
                ole32.CoTaskMemFree(name)
                return path
            finally:
                _release(result)
        finally:
            _release(dialog)
            if uninit:
                ole32.CoUninitialize()

else:  # pragma: no cover - other platforms

    def pick_folder(initial: Optional[str] = None, title: str = "Select folder",
                    owner_title: Optional[str] = None) -> Optional[str]:
        raise OSError("Native folder picker is only available on Windows")
