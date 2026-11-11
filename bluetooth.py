"""Windows Bluetooth speaker helpers, using only built-in Windows APIs.

* list_endpoints()  every playback endpoint Windows knows, including paired
                    Bluetooth speakers that are currently disconnected, with
                    connection state and a hands-free (call-mode) flag.
* reconnect(id)     ask Windows to reconnect a paired Bluetooth speaker (the
                    same one-shot request the Bluetooth stack exposes to tools
                    such as ToothTray).
* battery_levels()  battery percentage per physical device, as shown in
                    Windows Settings (only for speakers that report it).
* scan_for_speakers() / pair()  find and pair new speakers (needs the optional
                    `winrt-*` packages; everything else works without them).

No drivers, no admin rights. COM is called through raw vtables with ctypes so
no extra package is needed for the core features.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from ctypes import POINTER, byref, c_ubyte, c_ulong, c_ushort, c_void_p

HRESULT = ctypes.HRESULT

# ---------------------------------------------------------------- COM basics


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort), ("Data4", c_ubyte * 8)]

    def __init__(self, text=None):
        super().__init__()
        if text:
            ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p("{%s}" % text.strip("{}")), byref(self))

    def __str__(self):
        buf = ctypes.create_unicode_buffer(40)
        ctypes.windll.ole32.StringFromGUID2(byref(self), buf, 40)
        return buf.value.lower()


class PROPERTYKEY(ctypes.Structure):          # same layout as DEVPROPKEY
    _fields_ = [("fmtid", GUID), ("pid", c_ulong)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                ("val", c_void_p), ("pad", c_void_p)]


def _key(fmtid, pid):
    return PROPERTYKEY(GUID(fmtid), pid)


CLSID_MMDeviceEnumerator = GUID("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = GUID("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IDeviceTopology = GUID("2A07407E-6497-4A18-9787-32F79BD0D98F")
IID_IPart = GUID("AE2DE0E4-5BCA-4F2D-AA46-5D13F8FDB3A9")
IID_IKsControl = GUID("28F54685-06FD-11D2-B27A-00A0C9223196")
KSPROPSETID_BtAudio = GUID("7FA06C40-B8F6-4C7E-8556-E8C33A12E54D")
KSPROPERTY_ONESHOT_RECONNECT = 0
KSPROPERTY_ONESHOT_DISCONNECT = 1
KSPROPERTY_TYPE_GET = 1

PKEY_Device_FriendlyName = _key("A45C254E-DF1C-4EFD-8020-67D146A850E0", 14)
PKEY_Device_EnumeratorName = _key("A45C254E-DF1C-4EFD-8020-67D146A850E0", 24)
PKEY_DeviceInterface_FriendlyName = _key("026E516E-B814-414B-83CD-856D6FEF4822", 2)
PKEY_Device_ContainerId = _key("8C7ED206-3F8A-4827-B3AB-AE9E1FAEFC6C", 2)
DEVPKEY_Bluetooth_Battery = _key("104EA319-6EE2-4701-BD47-8DDBF425BBE5", 2)

E_RENDER = 0
STATE_ACTIVE, STATE_DISABLED, STATE_NOTPRESENT, STATE_UNPLUGGED = 1, 2, 4, 8
CLSCTX_ALL = 23
VT_LPWSTR, VT_CLSID = 31, 72
BT_ENUMERATORS = ("BTHENUM", "BTHHFENUM", "BTHLEDEVICE", "BTHLE")


def _vcall(obj, index, argtypes, *args):
    """Call method number `index` of the COM object pointer `obj`."""
    fn_addr = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0][index]
    fn = ctypes.WINFUNCTYPE(HRESULT, c_void_p, *argtypes)(fn_addr)
    return fn(obj, *args)


def _release(obj):
    if obj:
        fn_addr = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0][2]
        ctypes.WINFUNCTYPE(c_ulong, c_void_p)(fn_addr)(obj)


def com_init(single_threaded=False):
    """Initialise COM on the calling thread (safe to call more than once).
    Use single_threaded=True on the Qt GUI thread, which Qt sets up that way."""
    ctypes.windll.ole32.CoInitializeEx(None, 2 if single_threaded else 0)  # S_FALSE etc. are fine


def _enumerator():
    p = c_void_p()
    ctypes.oledll.ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                         byref(IID_IMMDeviceEnumerator), byref(p))
    return p


def _prop(store, key):
    pv = PROPVARIANT()
    try:
        _vcall(store, 5, [POINTER(PROPERTYKEY), POINTER(PROPVARIANT)], byref(key), byref(pv))
    except OSError:
        return None
    try:
        if pv.vt == VT_LPWSTR and pv.val:
            return ctypes.wstring_at(pv.val)
        if pv.vt == VT_CLSID and pv.val:
            return str(GUID.from_address(pv.val))
        return None
    finally:
        ctypes.windll.ole32.PropVariantClear(byref(pv))


def _device_info(dev):
    ident = ctypes.c_wchar_p()
    _vcall(dev, 5, [POINTER(ctypes.c_wchar_p)], byref(ident))
    dev_id = ident.value
    ctypes.windll.ole32.CoTaskMemFree(ident)
    state = wt.DWORD()
    _vcall(dev, 6, [POINTER(wt.DWORD)], byref(state))
    store = c_void_p()
    _vcall(dev, 4, [wt.DWORD, POINTER(c_void_p)], 0, byref(store))   # STGM_READ
    try:
        name = _prop(store, PKEY_Device_FriendlyName) or ""
        iface = _prop(store, PKEY_DeviceInterface_FriendlyName) or ""
        enum = (_prop(store, PKEY_Device_EnumeratorName) or "").upper()
        container = _prop(store, PKEY_Device_ContainerId) or ""
    finally:
        _release(store)
    bt = enum in BT_ENUMERATORS
    hands_free = enum == "BTHHFENUM" or "hands-free" in (name + " " + iface).lower()
    return {"id": dev_id, "name": name, "device": iface, "state": state.value,
            "connected": state.value == STATE_ACTIVE, "bluetooth": bt,
            "hands_free": hands_free, "container": container}


def list_endpoints():
    """Playback endpoints that are connected, plus paired Bluetooth speakers
    that are switched off / out of range (state 'unplugged')."""
    res = []
    en = _enumerator()
    coll = c_void_p()
    try:
        _vcall(en, 3, [ctypes.c_int, wt.DWORD, POINTER(c_void_p)], E_RENDER,
               STATE_ACTIVE | STATE_UNPLUGGED, byref(coll))
        count = ctypes.c_uint()
        _vcall(coll, 3, [POINTER(ctypes.c_uint)], byref(count))
        for i in range(count.value):
            dev = c_void_p()
            _vcall(coll, 4, [ctypes.c_uint, POINTER(c_void_p)], i, byref(dev))
            try:
                info = _device_info(dev)
            except OSError:
                continue
            finally:
                _release(dev)
            if info["connected"] or info["bluetooth"]:
                res.append(info)
    finally:
        _release(coll)
        _release(en)
    return res


def _bt_request(endpoint_id, prop_id):
    en = _enumerator()
    dev, topo, conn, other, part, ks = (c_void_p() for _ in range(6))
    try:
        _vcall(en, 5, [ctypes.c_wchar_p, POINTER(c_void_p)], endpoint_id, byref(dev))
        _vcall(dev, 3, [POINTER(GUID), wt.DWORD, c_void_p, POINTER(c_void_p)],
               byref(IID_IDeviceTopology), CLSCTX_ALL, None, byref(topo))
        _vcall(topo, 4, [ctypes.c_uint, POINTER(c_void_p)], 0, byref(conn))    # GetConnector(0)
        _vcall(conn, 8, [POINTER(c_void_p)], byref(other))                      # GetConnectedTo
        _vcall(other, 0, [POINTER(GUID), POINTER(c_void_p)], byref(IID_IPart), byref(part))
        _vcall(part, 13, [wt.DWORD, POINTER(GUID), POINTER(c_void_p)],
               CLSCTX_ALL, byref(IID_IKsControl), byref(ks))                  # IPart::Activate

        class KSPROPERTY(ctypes.Structure):
            _fields_ = [("Set", GUID), ("Id", c_ulong), ("Flags", c_ulong)]

        prop = KSPROPERTY(KSPROPSETID_BtAudio, prop_id, KSPROPERTY_TYPE_GET)
        returned = c_ulong()
        _vcall(ks, 3, [POINTER(KSPROPERTY), c_ulong, c_void_p, c_ulong, POINTER(c_ulong)],
               byref(prop), ctypes.sizeof(prop), None, 0, byref(returned))
    finally:
        for p in (ks, part, other, conn, topo, dev, en):
            _release(p)


def reconnect(endpoint_id):
    """Ask Windows to (re)connect the Bluetooth speaker behind this endpoint.
    Raises OSError if the request was refused."""
    _bt_request(endpoint_id, KSPROPERTY_ONESHOT_RECONNECT)


def disconnect(endpoint_id):
    _bt_request(endpoint_id, KSPROPERTY_ONESHOT_DISCONNECT)


# ------------------------------------------------------------------ battery

_cfg = ctypes.WinDLL("cfgmgr32")
CM_GETIDLIST_FILTER_PRESENT = 0x100
CM_GETIDLIST_FILTER_ENUMERATOR = 0x1


def _devnode_prop(devinst, key):
    typ, size = c_ulong(), c_ulong(0)
    _cfg.CM_Get_DevNode_PropertyW(devinst, byref(key), byref(typ), None, byref(size), 0)
    if not size.value:
        return None, None
    buf = (c_ubyte * size.value)()
    if _cfg.CM_Get_DevNode_PropertyW(devinst, byref(key), byref(typ), buf, byref(size), 0) != 0:
        return None, None
    return typ.value, bytes(buf)


def _present_ids(enumerator):
    size = c_ulong()
    flags = CM_GETIDLIST_FILTER_PRESENT | CM_GETIDLIST_FILTER_ENUMERATOR
    if _cfg.CM_Get_Device_ID_List_SizeW(byref(size), enumerator, flags) != 0 or not size.value:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if _cfg.CM_Get_Device_ID_ListW(enumerator, buf, size.value, flags) != 0:
        return []
    return [s for s in ctypes.wstring_at(buf, size.value).split("\0") if s]


def battery_levels():
    """{container id: battery %} for every connected Bluetooth device that
    reports its battery to Windows."""
    res = {}
    for enum in ("BTHENUM", "BTHLE", "BTHLEDEVICE", "BTHHFENUM"):
        for inst in _present_ids(enum):
            devinst = c_ulong()
            if _cfg.CM_Locate_DevNodeW(byref(devinst), inst, 0) != 0:
                continue
            typ, data = _devnode_prop(devinst, DEVPKEY_Bluetooth_Battery)
            if not data:
                continue
            _, cdata = _devnode_prop(devinst, PKEY_Device_ContainerId)
            if cdata and len(cdata) >= 16:
                container = str(GUID.from_buffer_copy(cdata[:16]))
                res[container] = int(data[0])
    return res


# ------------------------------------------------------------------ pairing
# Uses the Windows Runtime device APIs through the optional `winrt` packages.

AUDIO_VIDEO_MAJOR_CLASS = 4      # Bluetooth "Class of Device": audio/video


def pairing_available():
    try:
        import winrt.windows.devices.enumeration  # noqa: F401
        return True
    except Exception:
        return False


async def scan_for_speakers(seconds, on_found, should_stop=lambda: False):
    """Search for nearby unpaired Bluetooth speakers/headphones for `seconds`.
    Calls on_found(device_id, name) for each one."""
    import asyncio

    from winrt.windows.devices.bluetooth import BluetoothAdapter, BluetoothDevice
    from winrt.windows.devices.enumeration import DeviceInformation, DeviceInformationKind

    if await BluetoothAdapter.get_default_async() is None:
        raise RuntimeError("This PC has no Bluetooth adapter, or it is switched off in Device Manager.")

    major_key = "System.Devices.Aep.Bluetooth.Cod.Major"
    watcher = DeviceInformation.create_watcher_with_kind_aqs_filter_and_additional_properties(
        BluetoothDevice.get_device_selector_from_pairing_state(False),
        [major_key, "System.Devices.Aep.IsPaired"],
        DeviceInformationKind.ASSOCIATION_ENDPOINT)
    seen = set()

    def consider(info):
        if info.id in seen or not info.name:
            return
        try:
            major = info.properties.lookup(major_key)
        except Exception:
            major = None
        if major is not None and int(major) != AUDIO_VIDEO_MAJOR_CLASS:
            return
        seen.add(info.id)
        on_found(info.id, info.name)

    watcher.add_added(lambda _w, info: consider(info))
    watcher.add_updated(lambda _w, _u: None)     # required for the watcher to keep running
    watcher.start()
    try:
        for _ in range(int(seconds * 10)):
            if should_stop():
                break
            await asyncio.sleep(0.1)
    finally:
        try:
            watcher.stop()
        except Exception:
            pass


async def pair(device_id):
    """Pair with a speaker found by scan_for_speakers. Returns (ok, message)."""
    from winrt.windows.devices.enumeration import (DeviceInformation, DevicePairingKinds,
                                                   DevicePairingResultStatus)

    info = await DeviceInformation.create_from_id_async(device_id)
    if info.pairing.is_paired:
        return True, "Already paired."
    custom = info.pairing.custom

    def on_request(_sender, args):
        if args.pairing_kind == DevicePairingKinds.PROVIDE_PIN:
            args.accept_with_pin("0000")     # older speakers use 0000 as a fixed PIN
        else:
            args.accept()

    token = custom.add_pairing_requested(on_request)
    try:
        kinds = (DevicePairingKinds.CONFIRM_ONLY | DevicePairingKinds.DISPLAY_PIN |
                 DevicePairingKinds.CONFIRM_PIN_MATCH | DevicePairingKinds.PROVIDE_PIN)
        result = await custom.pair_async(kinds)
    finally:
        custom.remove_pairing_requested(token)
    status = result.status
    if status in (DevicePairingResultStatus.PAIRED, DevicePairingResultStatus.ALREADY_PAIRED):
        return True, "Paired."
    return False, "Pairing failed (%s)." % str(status).split(".")[-1].replace("_", " ").lower()
