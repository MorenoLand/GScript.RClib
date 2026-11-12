"""MCP server over grclib: connects to a Graal server as an RC and NC, reads and writes NPC, class and weapon scripts,
runs RC commands, and reports what the server answers, so behaviour can be checked against a real server.

It speaks the Model Context Protocol on stdin/stdout (one JSON message per line) and needs nothing but Python and
grclib (bin/grclib.dll, .so or .dylib next to the repository root).

Settings come from ~/.grc_mcp.json (or the file named by GRC_MCP_CONFIG) and can be overridden by environment:

    {
      "list_host": "...",       GRC_LIST_HOST     list server to log in on
      "list_port": 14900,       GRC_LIST_PORT
      "account": "...",         GRC_ACCOUNT
      "password": "...",        GRC_PASSWORD
      "server": "Dev SlimeKing" GRC_SERVER        name (or part of it) of the server in the list
    }
"""

import ctypes
import json
import os
import sys
import threading
import time
from collections import deque

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LIBRARY_NAMES = {"win32": "grclib.dll", "darwin": "grclib.dylib"}
DLL_PATH = os.path.join(ROOT, "bin", LIBRARY_NAMES.get(sys.platform, "grclib.so"))
CONFIG_PATH = os.environ.get("GRC_MCP_CONFIG") or os.path.join(os.path.expanduser("~"), ".grc_mcp.json")
PROTOCOL = "latin-1"


# ----- C types, as in include/grclib.h -----------------------------------------------------------------------------

class RCServer(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("ip", ctypes.c_char_p), ("port", ctypes.c_int), ("players", ctypes.c_int),
                ("language", ctypes.c_char_p), ("description", ctypes.c_char_p), ("version", ctypes.c_char_p),
                ("homepage", ctypes.c_char_p)]


class RCPlayer(ctypes.Structure):
    _fields_ = [("account", ctypes.c_char_p), ("id", ctypes.c_int), ("nick", ctypes.c_char_p), ("level", ctypes.c_char_p)]


class RCWeapon(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("image", ctypes.c_char_p), ("script", ctypes.c_char_p)]


class RCClass(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("script", ctypes.c_char_p)]


class RCNPC(ctypes.Structure):
    _fields_ = [("id", ctypes.c_int), ("name", ctypes.c_char_p), ("type", ctypes.c_char_p), ("image", ctypes.c_char_p),
                ("script", ctypes.c_char_p), ("level", ctypes.c_char_p)]


class RCLevel(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("type", ctypes.c_char_p)]


V = ctypes.c_void_p
S = ctypes.c_char_p
I = ctypes.c_int
CB_CONNECTED = ctypes.CFUNCTYPE(None, V)
CB_DISCONNECTED = ctypes.CFUNCTYPE(None, S, V)
CB_TEXT = ctypes.CFUNCTYPE(None, S, V)
CB_PM = ctypes.CFUNCTYPE(None, I, S, S, S, V)
CB_DATA = ctypes.CFUNCTYPE(None, S, S, V)
CB_SCRIPT = ctypes.CFUNCTYPE(None, S, S, I, S, V)
CB_NAME = ctypes.CFUNCTYPE(None, S, V)
CB_NPC_ADDED = ctypes.CFUNCTYPE(None, I, S, V)
CB_NPC_ID = ctypes.CFUNCTYPE(None, I, V)
CB_NPC_TEXT = ctypes.CFUNCTYPE(None, I, S, V)
CB_PLAYER = ctypes.CFUNCTYPE(None, S, I, V)


def text(value):
    return value.decode(PROTOCOL, errors="replace") if value else ""


def wire(value):
    return (value or "").encode(PROTOCOL, errors="replace")


# ----- the connection ----------------------------------------------------------------------------------------------

class Grc:
    def __init__(self):
        self.lib = None
        self.handle = None
        self.events = deque(maxlen=20000)
        self.sequence = 0
        self.read_cursor = 0
        self.events_lock = threading.Lock()
        self.call_lock = threading.RLock()
        self.pump = None
        self.running = False
        self.callbacks = []
        self.started = time.time()
        self.server_name = ""

    # events
    def note(self, event_type, content, **extra):
        with self.events_lock:
            self.sequence += 1
            self.events.append({"n": self.sequence, "t": round(time.time() - self.started, 2), "type": event_type, "text": content, **extra})

    def events_after(self, sequence):
        with self.events_lock:
            return [event for event in self.events if event["n"] > sequence]

    def mark(self):
        with self.events_lock:
            return self.sequence

    # library
    def load(self):
        if self.lib:
            return
        if not os.path.exists(DLL_PATH):
            raise RuntimeError("grclib is not built: %s is missing (cmake --build build --config Release)" % DLL_PATH)
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(os.path.dirname(DLL_PATH))
        lib = ctypes.CDLL(DLL_PATH)
        sig = {
            "rc_connect": (V, [S, I, S, S]),
            "rc_get_servers": (I, [V, ctypes.POINTER(ctypes.POINTER(RCServer))]),
            "rc_connect_to_server": (I, [V, I]),
            "rc_connect_to_nc_server": (I, [V]),
            "rc_is_connected": (I, [V]),
            "rc_is_authenticated": (I, [V]),
            "rc_is_nc_connected": (I, [V]),
            "rc_is_nc_authenticated": (I, [V]),
            "rc_process_events": (None, [V]),
            "rc_disconnect": (None, [V]),
            "rc_disconnect_nc": (I, [V]),
            "rc_last_error": (S, [V]),
            "rc_execute": (I, [V, S]),
            "rc_get_players": (I, [V, ctypes.POINTER(ctypes.POINTER(RCPlayer))]),
            "rc_get_weapons": (I, [V, ctypes.POINTER(ctypes.POINTER(RCWeapon))]),
            "rc_get_classes": (I, [V, ctypes.POINTER(ctypes.POINTER(RCClass))]),
            "rc_get_npcs": (I, [V, ctypes.POINTER(ctypes.POINTER(RCNPC))]),
            "rc_get_levels": (I, [V, ctypes.POINTER(ctypes.POINTER(RCLevel))]),
            "rc_request_npc_script": (I, [V, I]),
            "rc_request_npc_attributes": (I, [V, I]),
            "rc_request_class_script": (I, [V, S]),
            "rc_request_weapon_script": (I, [V, S]),
            "rc_request_weapon_list": (I, [V]),
            "rc_add_weapon": (I, [V, S, S, S]),
            "rc_update_weapon": (I, [V, S, S, S]),
            "rc_delete_weapon": (I, [V, S]),
            "rc_add_class": (I, [V, S, S]),
            "rc_update_class": (I, [V, S, S]),
            "rc_delete_class": (I, [V, S]),
            "rc_create_npc_on_server": (I, [V, S, I, S, S, S, S, S]),
            "rc_update_npc": (I, [V, I, S]),
            "rc_delete_npc": (I, [V, I]),
            "rc_reset_npc": (I, [V, I]),
            "rc_get_npc_flags": (I, [V, I]),
        }
        for name, (restype, argtypes) in sig.items():
            function = getattr(lib, name, None)
            if function is not None:
                function.restype = restype
                function.argtypes = argtypes
        self.lib = lib

    def register(self):
        lib, handle = self.lib, self.handle

        def add(register_name, callback_type, function):
            callback = callback_type(function)
            self.callbacks.append(callback)
            register = getattr(lib, register_name, None)
            if register is not None:
                register.argtypes = [V, callback_type, V]
                register.restype = None
                register(handle, callback, None)

        add("rc_on_connected", CB_CONNECTED, lambda u: self.note("connected", "logged in to the server"))
        add("rc_on_disconnected", CB_DISCONNECTED, lambda r, u: self.note("disconnected", text(r)))
        add("rc_on_message", CB_TEXT, lambda m, u: self.note("rc_chat", text(m)))
        add("rc_on_private_message", CB_PM, lambda i, a, n, m, u: self.note("pm", text(m), player=i, account=text(a), nick=text(n)))
        add("rc_on_command_response", CB_TEXT, lambda m, u: self.note("command_response", text(m)))
        add("rc_on_server_data", CB_DATA, lambda t, c, u: self.note("server_data:" + text(t), text(c)))
        add("rc_on_script_received", CB_SCRIPT, lambda t, n, i, s, u: self.note("script", text(s), kind=text(t), name=text(n), id=i))
        add("rc_on_weapon_added", CB_NAME, lambda n, u: self.note("weapon_added", text(n)))
        add("rc_on_weapon_deleted", CB_NAME, lambda n, u: self.note("weapon_deleted", text(n)))
        add("rc_on_class_added", CB_NAME, lambda n, u: self.note("class_added", text(n)))
        add("rc_on_class_deleted", CB_NAME, lambda n, u: self.note("class_deleted", text(n)))
        add("rc_on_npc_added", CB_NPC_ADDED, lambda i, n, u: self.note("npc_added", text(n), id=i))
        add("rc_on_npc_deleted", CB_NPC_ID, lambda i, u: self.note("npc_deleted", str(i), id=i))
        add("rc_on_npc_attributes", CB_NPC_TEXT, lambda i, a, u: self.note("npc_attributes", text(a), id=i))
        add("rc_on_npc_flags", CB_NPC_TEXT, lambda i, a, u: self.note("npc_flags", text(a), id=i))
        add("rc_on_player_joined", CB_PLAYER, lambda a, i, u: self.note("player_joined", text(a), id=i))
        add("rc_on_player_left", CB_PLAYER, lambda a, i, u: self.note("player_left", text(a), id=i))
        add("rc_on_filebrowser_message", CB_TEXT, lambda m, u: self.note("filebrowser", text(m)))

    # pumping
    def start_pump(self):
        self.running = True

        def loop():
            while self.running and self.handle:
                with self.call_lock:
                    if self.handle:
                        self.lib.rc_process_events(self.handle)
                time.sleep(0.02)

        self.pump = threading.Thread(target=loop, daemon=True)
        self.pump.start()

    def wait_for(self, condition, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if condition():
                return True
            time.sleep(0.05)
        return condition()

    def call(self, function, *args):
        with self.call_lock:
            if not self.handle:
                raise RuntimeError("not connected: call grc_connect first")
            return getattr(self.lib, function)(self.handle, *args)

    def error(self):
        with self.call_lock:
            return text(self.lib.rc_last_error(self.handle)) if self.handle else ""

    # status
    def status(self):
        if not self.handle:
            return {"connected": False}
        with self.call_lock:
            return {"connected": bool(self.lib.rc_is_connected(self.handle)), "authenticated": bool(self.lib.rc_is_authenticated(self.handle)),
                    "nc_connected": bool(self.lib.rc_is_nc_connected(self.handle)), "nc_authenticated": bool(self.lib.rc_is_nc_authenticated(self.handle)),
                    "server": self.server_name, "last_error": self.error()}


grc = Grc()


def load_config():
    config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    for key, variable in (("list_host", "GRC_LIST_HOST"), ("list_port", "GRC_LIST_PORT"), ("account", "GRC_ACCOUNT"),
                          ("password", "GRC_PASSWORD"), ("server", "GRC_SERVER")):
        if os.environ.get(variable):
            config[key] = os.environ[variable]
    return config


# ----- tools -------------------------------------------------------------------------------------------------------

def render(events):
    lines = []
    for event in events:
        extra = {k: v for k, v in event.items() if k not in ("n", "t", "type", "text")}
        suffix = (" " + json.dumps(extra, ensure_ascii=False)) if extra else ""
        lines.append("[%s] %s: %s%s" % (event["t"], event["type"], event["text"], suffix))
    return "\n".join(lines) if lines else "(nothing came back)"


def tool_connect(args):
    config = load_config()
    missing = [key for key in ("list_host", "list_port", "account", "password", "server") if not config.get(key)]
    if missing:
        return "Missing settings: %s. Put them in %s (see the top of grc_mcp.py)." % (", ".join(missing), CONFIG_PATH)
    if grc.handle:
        tool_disconnect({})
    grc.load()
    grc.started = time.time()
    with grc.call_lock:
        handle = grc.lib.rc_connect(wire(str(config["list_host"])), int(config["list_port"]), wire(config["account"]), wire(config["password"]))
    if not handle:
        return "rc_connect failed: could not log in to the list server %s:%s" % (config["list_host"], config["list_port"])
    grc.handle = handle
    servers = ctypes.POINTER(RCServer)()
    count = 0
    end = time.time() + 25
    while time.time() < end:  # the list can arrive after the login: process events until it does
        with grc.call_lock:
            grc.lib.rc_process_events(handle)
            count = grc.lib.rc_get_servers(handle, ctypes.byref(servers))
        if count > 0:
            break
        time.sleep(0.2)
    names = [text(servers[i].name) for i in range(count)]
    wanted = str(config["server"]).lower()
    index = next((i for i, name in enumerate(names) if name.lower() == wanted), None)
    if index is None:
        index = next((i for i, name in enumerate(names) if wanted in name.lower()), None)
    if index is None:
        shown = ", ".join(names[:40])
        tool_disconnect({})
        return "No server named like '%s' in the list (%d servers). Some: %s" % (config["server"], count, shown)
    grc.server_name = names[index]
    grc.register()
    grc.start_pump()
    with grc.call_lock:
        started = grc.lib.rc_connect_to_server(handle, index)
    if not started:
        return "rc_connect_to_server failed: %s" % grc.error()
    if not grc.wait_for(lambda: grc.status().get("authenticated"), 30):
        return "Connected but not authenticated in 30s. last error: %s" % grc.error()
    # the server tells the client where the NC is a moment after the login: ask until it has
    opened = False
    end = time.time() + 30
    while time.time() < end and not opened:
        with grc.call_lock:
            opened = bool(grc.lib.rc_connect_to_nc_server(handle))
        if not opened:
            time.sleep(0.5)
    nc = opened and grc.wait_for(lambda: grc.status().get("nc_authenticated"), 30)
    time.sleep(1.5)
    return "Logged in to '%s'. NC %s.\n%s" % (grc.server_name, "ready" if nc else "NOT ready (script tools need it): " + grc.error(), json.dumps(grc.status()))


def tool_disconnect(args):
    grc.running = False
    if grc.pump:
        grc.pump.join(timeout=2)
    with grc.call_lock:
        if grc.handle:
            grc.lib.rc_disconnect(grc.handle)
        grc.handle = None
    grc.server_name = ""
    return "disconnected"


def tool_status(args):
    config = load_config()
    shown = {key: ("***" if key == "password" and config.get(key) else config.get(key)) for key in ("list_host", "list_port", "account", "password", "server")}
    return json.dumps({"config_file": CONFIG_PATH, "settings": shown, "library": DLL_PATH, "library_present": os.path.exists(DLL_PATH), "state": grc.status()}, indent=2)


def tool_servers(args):
    config = load_config()
    if not grc.handle:
        return "Not connected; the server list comes from the list server after grc_connect."
    servers = ctypes.POINTER(RCServer)()
    with grc.call_lock:
        count = grc.lib.rc_get_servers(grc.handle, ctypes.byref(servers))
    return "\n".join("%d. %s (%d players) %s:%d %s" % (i, text(servers[i].name), servers[i].players, text(servers[i].ip), servers[i].port, text(servers[i].language)) for i in range(count))


def tool_events(args):
    wait = float(args.get("wait", 0) or 0)
    mark = grc.read_cursor
    if wait > 0:
        time.sleep(wait)
    events = grc.events_after(mark)
    if events:
        grc.read_cursor = events[-1]["n"]
    return render(events)


def tool_execute(args):
    command = args.get("command", "")
    wait = float(args.get("wait", 3) or 3)
    mark = grc.mark()
    ok = grc.call("rc_execute", wire(command))
    time.sleep(wait)
    events = grc.events_after(mark)
    if events:
        grc.read_cursor = max(grc.read_cursor, events[-1]["n"])
    return ("sent" if ok else "NOT sent (" + grc.error() + ")") + "\n" + render(events)


def snapshot(kind):
    out = []
    with grc.call_lock:
        if not grc.handle:
            raise RuntimeError("not connected: call grc_connect first")
        if kind == "npc":
            array = ctypes.POINTER(RCNPC)()
            count = grc.lib.rc_get_npcs(grc.handle, ctypes.byref(array))
            out = [{"id": array[i].id, "name": text(array[i].name), "type": text(array[i].type), "level": text(array[i].level)} for i in range(count)]
        elif kind == "class":
            array = ctypes.POINTER(RCClass)()
            count = grc.lib.rc_get_classes(grc.handle, ctypes.byref(array))
            out = [{"name": text(array[i].name)} for i in range(count)]
        elif kind == "weapon":
            array = ctypes.POINTER(RCWeapon)()
            count = grc.lib.rc_get_weapons(grc.handle, ctypes.byref(array))
            out = [{"name": text(array[i].name), "image": text(array[i].image)} for i in range(count)]
        elif kind == "player":
            array = ctypes.POINTER(RCPlayer)()
            count = grc.lib.rc_get_players(grc.handle, ctypes.byref(array))
            out = [{"id": array[i].id, "account": text(array[i].account), "nick": text(array[i].nick), "level": text(array[i].level)} for i in range(count)]
        elif kind == "level":
            array = ctypes.POINTER(RCLevel)()
            count = grc.lib.rc_get_levels(grc.handle, ctypes.byref(array))
            out = [{"name": text(array[i].name), "type": text(array[i].type)} for i in range(count)]
        else:
            raise RuntimeError("kind must be npc, class, weapon, player or level")
    return out


def tool_list(args):
    kind = args.get("kind", "")
    items = snapshot(kind)
    filter_text = (args.get("filter") or "").lower()
    if filter_text:
        items = [item for item in items if filter_text in json.dumps(item).lower()]
    return "%d %s(s)\n%s" % (len(items), kind, "\n".join(json.dumps(item, ensure_ascii=False) for item in items[:500]))


def npc_id_for(args):
    if args.get("id") not in (None, ""):
        return int(args["id"])
    name = (args.get("name") or "").lower()
    for item in snapshot("npc"):
        if item["name"].lower() == name:
            return item["id"]
    raise RuntimeError("no NPC named '%s' (list them with grc_list kind=npc)" % args.get("name"))


def tool_get_script(args):
    kind = args.get("kind", "")
    mark = grc.mark()
    if kind == "npc":
        npc_id = npc_id_for(args)
        grc.call("rc_request_npc_script", npc_id)
        match = lambda e: e["type"] == "script" and e.get("id") == npc_id
    elif kind == "class":
        grc.call("rc_request_class_script", wire(args["name"]))
        match = lambda e: e["type"] == "script" and e.get("name", "").lower() == args["name"].lower()
    elif kind == "weapon":
        grc.call("rc_request_weapon_script", wire(args["name"]))
        match = lambda e: e["type"] == "script" and e.get("name", "").lower() == args["name"].lower()
    else:
        return "kind must be npc, class or weapon"
    found = []
    grc.wait_for(lambda: found.extend(e for e in grc.events_after(mark) if match(e)) or bool(found), 15)
    if not found:
        return "no script came back in 15s. last error: %s\n%s" % (grc.error(), render(grc.events_after(mark)))
    return found[0]["text"]


def tool_update_script(args):
    kind, script = args.get("kind", ""), args.get("script", "")
    mark = grc.mark()
    if kind == "npc":
        ok = grc.call("rc_update_npc", npc_id_for(args), wire(script))
    elif kind == "class":
        ok = grc.call("rc_update_class", wire(args["name"]), wire(script))
    elif kind == "weapon":
        ok = grc.call("rc_update_weapon", wire(args["name"]), wire(args.get("image", "")), wire(script))
    else:
        return "kind must be npc, class or weapon"
    time.sleep(float(args.get("wait", 3) or 3))
    return ("sent" if ok else "NOT sent (" + grc.error() + ")") + "\n" + render(grc.events_after(mark))


def tool_create(args):
    kind = args.get("kind", "")
    mark = grc.mark()
    if kind == "class":
        ok = grc.call("rc_add_class", wire(args["name"]), wire(args.get("script", "")))
    elif kind == "weapon":
        ok = grc.call("rc_add_weapon", wire(args["name"]), wire(args.get("image", "")), wire(args.get("script", "")))
    elif kind == "npc":
        # the server wants an explicit id (1000 or more); take the next one nobody uses
        npc_id = int(args.get("id", 0) or 0)
        if npc_id < 1000:
            npc_id = max([1499] + [item["id"] for item in snapshot("npc") if item["id"] < 10000]) + 1
        ok = grc.call("rc_create_npc_on_server", wire(args["name"]), npc_id, wire(args.get("type", "OBJECT")),
                      wire(args.get("scripter", "mcp")), wire(args.get("level", "")), wire(str(args.get("x", "30"))), wire(str(args.get("y", "30"))))
    else:
        return "kind must be npc, class or weapon"
    time.sleep(float(args.get("wait", 3) or 3))
    return ("sent" if ok else "NOT sent (" + grc.error() + ")") + "\n" + render(grc.events_after(mark))


def tool_delete(args):
    kind = args.get("kind", "")
    mark = grc.mark()
    if kind == "class":
        ok = grc.call("rc_delete_class", wire(args["name"]))
    elif kind == "weapon":
        ok = grc.call("rc_delete_weapon", wire(args["name"]))
    elif kind == "npc":
        ok = grc.call("rc_delete_npc", npc_id_for(args))
    else:
        return "kind must be npc, class or weapon"
    time.sleep(float(args.get("wait", 3) or 3))
    return ("sent" if ok else "NOT sent (" + grc.error() + ")") + "\n" + render(grc.events_after(mark))


def tool_reset_npc(args):
    mark = grc.mark()
    ok = grc.call("rc_reset_npc", npc_id_for(args))
    time.sleep(float(args.get("wait", 3) or 3))
    return ("sent" if ok else "NOT sent (" + grc.error() + ")") + "\n" + render(grc.events_after(mark))


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required)}


KIND = {"type": "string", "enum": ["npc", "class", "weapon"], "description": "npc, class or weapon"}
WAIT = {"type": "number", "description": "seconds to listen for the server's answer (default 3)"}
TOOLS = [
    ("grc_status", "Settings file, library and connection state.", schema({}), tool_status),
    ("grc_connect", "Log in to the configured Graal server as RC and NC. Needs ~/.grc_mcp.json (list_host, list_port, account, password, server).", schema({}), tool_connect),
    ("grc_disconnect", "Log out.", schema({}), tool_disconnect),
    ("grc_servers", "The servers the list server returned.", schema({}), tool_servers),
    ("grc_execute", "Send an RC chat line or /command and return what the server printed back.", schema({"command": {"type": "string"}, "wait": WAIT}, ["command"]), tool_execute),
    ("grc_events", "Everything the server sent since the last call (RC chat, NC output, script errors, data).", schema({"wait": {"type": "number", "description": "seconds to wait first"}}), tool_events),
    ("grc_list", "List NPCs, classes, weapons, players or levels the server reported.", schema({"kind": {"type": "string", "enum": ["npc", "class", "weapon", "player", "level"]}, "filter": {"type": "string"}}, ["kind"]), tool_list),
    ("grc_get_script", "Fetch the script of an NPC (id or name), class (name) or weapon (name) from the server.", schema({"kind": KIND, "name": {"type": "string"}, "id": {"type": "integer"}}, ["kind"]), tool_get_script),
    ("grc_update_script", "Replace the script of an existing NPC, class or weapon, then report the server's answer (compile errors arrive as events).", schema({"kind": KIND, "name": {"type": "string"}, "id": {"type": "integer"}, "script": {"type": "string"}, "image": {"type": "string", "description": "weapon image"}, "wait": WAIT}, ["kind", "script"]), tool_update_script),
    ("grc_create", "Create a class, weapon or NPC on the server.", schema({"kind": KIND, "name": {"type": "string"}, "script": {"type": "string"}, "image": {"type": "string"}, "id": {"type": "integer"}, "type": {"type": "string"}, "scripter": {"type": "string"}, "level": {"type": "string"}, "x": {"type": "string"}, "y": {"type": "string"}, "wait": WAIT}, ["kind", "name"]), tool_create),
    ("grc_delete", "Delete a class, weapon or NPC.", schema({"kind": KIND, "name": {"type": "string"}, "id": {"type": "integer"}, "wait": WAIT}, ["kind"]), tool_delete),
    ("grc_reset_npc", "Reset (restart) an NPC by id or name.", schema({"name": {"type": "string"}, "id": {"type": "integer"}, "wait": WAIT}), tool_reset_npc),
]
HANDLERS = {name: handler for name, _, _, handler in TOOLS}


# ----- MCP over stdio ----------------------------------------------------------------------------------------------

def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(request_id, result=None, error=None):
    if request_id is None:
        return
    message = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    send(message)


def handle(message):
    method, request_id, params = message.get("method"), message.get("id"), message.get("params") or {}
    if method == "initialize":
        reply(request_id, {"protocolVersion": params.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
                           "serverInfo": {"name": "grc-mcp", "version": "1.0.0"}})
    elif method == "ping":
        reply(request_id, {})
    elif method == "tools/list":
        reply(request_id, {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS]})
    elif method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            reply(request_id, error={"code": -32602, "message": "unknown tool " + str(name)})
            return
        try:
            content, failed = handler(arguments), False
        except Exception as problem:  # the tool failed; the model reads why
            content, failed = "%s: %s" % (type(problem).__name__, problem), True
        reply(request_id, {"content": [{"type": "text", "text": content}], "isError": failed})
    elif request_id is not None:
        reply(request_id, error={"code": -32601, "message": "method not found: " + str(method)})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle(message)
    tool_disconnect({})


if __name__ == "__main__":
    main()
