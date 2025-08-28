# BridgeApi.py
# --------------------------------------------------------------
# Loads Looking-Glass Bridge ≥2.6 automatically
# Extra diagnostics are printed to stderr when BridgeAPI(debug=True).
# --------------------------------------------------------------

# BridgeApi.py
# --------------------------------------------------------------
# Loads Looking-Glass Bridge ≥2.6 automatically
# Extra diagnostics are printed to stderr when BridgeAPI(debug=True).
# --------------------------------------------------------------

import json, os, re, sys, ctypes, platform
from ctypes import (
    c_bool, c_char_p, c_float, c_int32, c_int64,
    c_uint, c_uint32, c_uint64, c_ulong,
    c_void_p, c_wchar_p, POINTER, byref
)
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import importlib.resources as ir
import subprocess
from BridgeDataTypes import Window, PixelFormats, LKGCalibration, DefaultQuiltSettings

_MIN_BRIDGE_VERSION = "2.6.0"
_BRIDGE_VERSION     = "2.6.2"

# ----------------------------------------------------------------- helpers
def _ver_tuple(v: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def _settings_path() -> Path:
    if sys.platform.startswith("win"):
        return Path(os.getenv("APPDATA", "")) / "Looking Glass" / "Bridge" / "settings.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Looking Glass" / "Bridge" / "settings.json"
    return Path.home() / ".config" / "LookingGlass" / "Bridge" / "settings.json"  # JSON format (newer)


def _bridge_install_location(requested: str, log) -> Optional[Path]:
    inst: Dict[str, Path] = {}

    # ---- JSON settings path (newer installers) ----
    cfg_json = _settings_path()
    log(f"JSON settings path: {cfg_json}")
    if cfg_json.is_file():
        try:
            data = json.loads(cfg_json.read_text(encoding="utf-8"))
            inst |= {d["version"]: Path(d["path"])
                     for d in data.get("install_locations", [])
                     if _ver_tuple(d["version"]) >= _ver_tuple(_MIN_BRIDGE_VERSION)}
            log(f"Parsed JSON installs: {inst}")
        except Exception as e:
            log(f"Failed to parse JSON settings: {e}")

    # ---- Legacy plain-text list (Linux <2.6) ----
    legacy_file = Path.home() / ".lgf" / "bridge_install_locations"
    log(f"Legacy install list: {legacy_file}")
    if legacy_file.is_file():
        for line in legacy_file.read_text(encoding="utf-8").splitlines():
            path = line.strip()
            if not path:
                continue
            m = re.search(r"\d+\.\d+\.\d+", Path(path).name)
            if m and _ver_tuple(m.group(0)) >= _ver_tuple(_MIN_BRIDGE_VERSION):
                inst[m.group(0)] = Path(path)
        log(f"Parsed legacy installs: {inst}")

    if not inst:
        log("No suitable installs found")
        return None

    if requested in inst:
        log(f"Exact match for requested version {requested}")
        return inst[requested]

    req_major = requested.split(".")[0]
    same_major = [v for v in inst if v.split(".")[0] == req_major]
    chosen = inst[max(same_major, key=_ver_tuple)] if same_major else inst[max(inst, key=_ver_tuple)]
    log(f"Selected install {chosen}")
    return chosen

# ---------- cross-platform last-error helper --------------------
def _last_error() -> int:
    if hasattr(ctypes, "get_last_error"):
        return ctypes.get_last_error()
    return ctypes.get_errno()

# ---------- telemetry helper -----------------------------------
def _apply_tracking_setting(disable: bool, log) -> None:
    """
    Ensure the Bridge settings.json reflects the user's telemetry preference.
    When *disable* is True, the value is written as "false", otherwise "true".
    """
    try:
        p = _settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)

        new_val = "false" if disable else "true"
        data: Dict[str, Any] = {}

        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                log(f"Failed to parse existing settings.json: {e}")

        if not isinstance(data, dict):
            data = {}

        if data.get("enable_utilization_telemetry") != new_val:
            data["enable_utilization_telemetry"] = new_val
            p.write_text(json.dumps(data, indent=4), encoding="utf-8")
            log(f'Set "enable_utilization_telemetry" → {new_val} in {p}')
    except Exception as e:
        log(f"Unable to update telemetry setting: {e}")

# ----------------------------------------------------------------- BridgeAPI
class BridgeAPI:
    # ------------- private utility -------------------------------------
    @staticmethod
    def _bind(fn, argtypes, restype):
        fn.argtypes, fn.restype = argtypes, restype
        return fn

    def _fn(self, name: str):
        return getattr(self.lib, name)

    @staticmethod
    def _scalar_call(native_fn, idx, c_type, debug: bool):
        val = c_type()
        if not native_fn(idx, byref(val)):
            err = _last_error()
            msg = f"{native_fn.__name__} failed (error {err})"
            if debug:
                print(msg, file=sys.stderr)
            raise RuntimeError(msg)
        return val.value

    # ------------- ctor -------------------------------------------------
    def __init__(self, debug: bool = True, library_path: Optional[str] = None,
                 requested_version: str = _BRIDGE_VERSION, disableTracking: bool = False):

        import ctypes.util

        self.debug = bool(debug)

        def _log(msg: str):
            if self.debug:
                print("[BridgeAPI]", msg, file=sys.stderr)

        self._log = _log
        self._log(f"Requested Bridge version: {requested_version}")

        # ----------- apply telemetry preference -------------------------
        _apply_tracking_setting(disableTracking, self._log)

        # ---------------- locate Bridge install -----------------
        if library_path is None:
            install_dir = _bridge_install_location(
                requested_version,
                self._log if self.debug else (lambda *_: None)
            )
            if install_dir is None:
                pkg_root = ir.files("bridge_python_sdk") / "bin"
                if sys.platform.startswith("win"):
                    subdir = "win";                      lib_name = "bridge_inproc.dll"
                elif sys.platform == "darwin":
                    subdir = "mac-m1" if platform.machine().lower() in ("arm64", "aarch64") else "mac-x64"
                    lib_name = "libbridge_inproc.dylib"
                else:
                    subdir = "ubuntu";                   lib_name = "libbridge_inproc.so"
                install_dir = pkg_root / subdir
                library_path = str(install_dir / lib_name)
                if sys.platform.startswith("win"):
                    ctypes.windll.kernel32.SetDllDirectoryW(str(install_dir))
                self._log(f"Using bundled Bridge binary: {library_path}")
            else:
                self._log(f"Found installed Bridge {requested_version} at {install_dir}")
                if sys.platform.startswith("win"):
                    ctypes.windll.kernel32.SetDllDirectoryW(str(install_dir))
                    library_path = str(install_dir / "bridge_inproc.dll")
                elif sys.platform == "darwin":
                    library_path = str(install_dir / "libbridge_inproc.dylib")
                else:
                    library_path = str(install_dir / "libbridge_inproc.so")
        else:
            # ---- user supplied a path ----
            supplied = Path(library_path)
            if supplied.is_dir():
                # try to locate the binary inside the directory
                self._log(f"'library_path' points to a directory: {supplied}")
                suffix = { "win32": ".dll", "cygwin": ".dll",
                           "msys":  ".dll",
                           "darwin": ".dylib" }.get(sys.platform, ".so")
                candidates = sorted(
                    list(supplied.glob(f"libbridge_inproc*{suffix}")) +
                    list(supplied.glob(f"bridge_inproc*{suffix}"))
                )
                if not candidates:
                    self._log(f"No bridge_inproc*{suffix} candidate found in {supplied}")
                    raise FileNotFoundError(
                        f"No Bridge binary found in directory: {supplied}"
                    )
                if len(candidates) > 1:
                    self._log(f"Multiple candidates found: {candidates}; choosing first.")
                library_path = str(candidates[0])
                install_dir  = supplied
                if sys.platform.startswith("win"):
                    ctypes.windll.kernel32.SetDllDirectoryW(str(install_dir))
                self._log(f"Using Bridge binary: {library_path}")
            else:
                library_path = str(supplied)
                install_dir  = supplied.parent
                if sys.platform.startswith("win"):
                    ctypes.windll.kernel32.SetDllDirectoryW(str(install_dir))

        # --- verify DLL presence and show directory listing --------------
        lib_path_obj = Path(library_path)
        dll_exists = lib_path_obj.is_file()
        self._log(f"Resolved library path: {library_path}")
        self._log(f"Library exists on disk: {dll_exists}")
        if not dll_exists:
            try:
                contents = [p.name for p in install_dir.glob('bridge_inproc*')]
                self._log(f"Directory listing for {install_dir}: {contents or 'None'}")
            except Exception as e:
                self._log(f"Unable to list contents of {install_dir}: {e}")
            raise FileNotFoundError(f"Bridge binary not found: {library_path}")
        else:
            try:
                st = lib_path_obj.stat()
                self._log(f"Library size: {st.st_size} bytes, mtime: {st.st_mtime}")
            except Exception as e:
                self._log(f"Could not stat library: {e}")

        # -------- Linux: preload hard dependencies -----------------------
        if sys.platform.startswith("linux"):
            # Make TLS and AppIndicator symbols globally visible
            deepbind_flag = getattr(ctypes, "RTLD_DEEPBIND", 0)
            global_mode   = ctypes.RTLD_GLOBAL | deepbind_flag

            # TLS chain (order matters)
            for dep in ("libmbedcrypto.so.1",
                        "libmbedx509.so.0",
                        "libmbedtls.so.10"):
                p = install_dir / dep
                if p.is_file():
                    ctypes.CDLL(str(p), mode=global_mode)
                    self._log(f"Pre-loaded {p.name}")

            # AppIndicator variants found on different Ubuntu flavours
            for cand in ("libappindicator3.so.1",
                         "libappindicator3.so",
                         "libappindicator.so.1",
                         "libappindicator.so",
                         "libayatana-appindicator3.so.1",
                         "libayatana-appindicator3.so"):
                lib = ctypes.util.find_library(Path(cand).stem) or cand
                try:
                    ctypes.CDLL(lib, mode=global_mode)
                    self._log(f"Pre-loaded {Path(lib).name}")
                    break
                except OSError:
                    continue  # try next candidate

        # -------- finally load Bridge itself -----------------------------
        try:
            self.lib = (ctypes.WinDLL(library_path, use_last_error=True)
                        if sys.platform.startswith("win")
                        else ctypes.CDLL(library_path))
        except OSError as e:
            self._log(f"Failed to load '{library_path}': {e}")
            self._log(f"LD_LIBRARY_PATH: {os.getenv('LD_LIBRARY_PATH')}")
            if sys.platform.startswith("win"):
                err_code = ctypes.get_last_error()
                if err_code:
                    buf = ctypes.create_unicode_buffer(1024)
                    ctypes.windll.kernel32.FormatMessageW(
                        0x00001000, None, err_code, 0, buf, len(buf), None)
                    self._log(f"Win32 last-error {err_code}: {buf.value.strip()}")
                try:
                    subprocess.check_output(
                        ["dumpbin", "/DEPENDENTS", library_path],
                        text=True, stderr=subprocess.STDOUT
                    )
                except FileNotFoundError:
                    self._log("dumpbin not found on PATH")
                except subprocess.CalledProcessError as e3:
                    self._log(f"dumpbin /DEPENDENTS failed: {e3.output.strip()}")
            else:
                try:
                    out = subprocess.check_output(["ldd", library_path],
                                                  text=True, stderr=subprocess.STDOUT)
                    self._log(f"ldd output for {library_path}:\n{out}")
                except FileNotFoundError:
                    self._log("ldd not found on PATH")
                except subprocess.CalledProcessError as e2:
                    self._log(f"ldd failed: {e2.output.strip()}")
            raise

        self._log(f"Successfully loaded {library_path}")
        self._bind_functions()

    # ------------- bind native exports ---------------------------------
    def _bind_functions(self) -> None:
        filename_t = c_wchar_p if sys.platform.startswith("win") else c_char_p

        specs: Dict[str, Tuple[List[Any], Any]] = {
            # core --------------------------------------------------------
            "initialize_bridge":                ([c_wchar_p],                           c_bool),
            "uninitialize_bridge":              ([],                                    c_bool),

            # window creation --------------------------------------------
            "instance_window_gl":               ([POINTER(c_uint32), c_uint32],         c_bool),
            "instance_offscreen_window_gl":     ([POINTER(c_uint32), c_uint32],         c_bool),

            # GL basics ---------------------------------------------------
            "get_window_dimensions":            ([c_uint32, POINTER(c_ulong),
                                                  POINTER(c_ulong)],                   c_bool),
            "get_max_texture_size":             ([c_uint32, POINTER(c_ulong)],          c_bool),

            "set_interop_quilt_texture_gl":     ([c_uint32, c_uint64, c_uint,
                                                  c_ulong, c_ulong, c_ulong, c_ulong,
                                                  c_float, c_float],                   c_bool),

            "draw_interop_quilt_texture_gl":    ([c_uint32, c_uint64, c_uint,
                                                  c_ulong, c_ulong, c_ulong, c_ulong,
                                                  c_float, c_float],                   c_bool),

            "draw_interop_rgbd_texture_gl":     ([c_uint32, c_uint64, c_uint,
                                                  c_ulong, c_ulong, c_ulong, c_ulong,
                                                  c_ulong, c_ulong,
                                                  c_float, c_float, c_float, c_float,
                                                  c_int32],                            c_bool),

            "save_texture_to_file_gl":          ([c_uint32, filename_t, c_uint64,
                                                  c_uint, c_ulong],                    c_bool),

            "show_window":                      ([c_uint32, c_bool],                    c_bool),

            "get_offscreen_window_texture_gl":  ([c_uint32, POINTER(c_uint64), POINTER(c_uint),
                                                  POINTER(c_ulong), POINTER(c_ulong)], c_bool),

            # quilt / per-window -----------------------------------------
            "get_default_quilt_settings":       ([c_uint32, POINTER(c_float),
                                                  POINTER(c_int32), POINTER(c_int32),
                                                  POINTER(c_int32), POINTER(c_int32)], c_bool),
            "get_display_for_window":           ([c_uint32, POINTER(c_uint64)],         c_bool),

            # scalar per-window ------------------------------------------
            "get_device_type":   ([c_uint32, POINTER(c_int32)],   c_bool),
            "get_viewcone":      ([c_uint32, POINTER(c_float)],   c_bool),
            "get_invview":       ([c_uint32, POINTER(c_int32)],   c_bool),
            "get_ri":            ([c_uint32, POINTER(c_int32)],   c_bool),
            "get_bi":            ([c_uint32, POINTER(c_int32)],   c_bool),
            "get_tilt":          ([c_uint32, POINTER(c_float)],   c_bool),
            "get_displayaspect": ([c_uint32, POINTER(c_float)],   c_bool),
            "get_fringe":        ([c_uint32, POINTER(c_float)],   c_bool),
            "get_subp":          ([c_uint32, POINTER(c_float)],   c_bool),
            "get_pitch":         ([c_uint32, POINTER(c_float)],   c_bool),
            "get_center":        ([c_uint32, POINTER(c_float)],   c_bool),
            "get_window_position":([c_uint32, POINTER(c_int64),
                                    POINTER(c_int64)],            c_bool),

            # calibration -------------------------------------------------
            "get_calibration":   ([c_uint32, POINTER(c_float), POINTER(c_float),
                                   POINTER(c_float), POINTER(c_int32), POINTER(c_int32),
                                   POINTER(c_float), POINTER(c_float), POINTER(c_int32),
                                   POINTER(c_float), POINTER(c_float),
                                   POINTER(c_int32), POINTER(c_int32), c_void_p],        c_bool),

            # strings -----------------------------------------------------
            "get_device_name":   ([c_uint32, POINTER(c_int32), c_void_p],               c_bool),
            "get_device_serial": ([c_uint32, POINTER(c_int32), c_void_p],               c_bool),

            # generic image save ------------------------------------------
            "save_image_to_file":([c_uint32, filename_t, c_void_p, c_uint,
                                   c_ulong, c_ulong],                                    c_bool),

            # helper ------------------------------------------------------
            "quiltify_rgbd":     ([c_uint32, c_ulong, c_ulong, c_ulong,
                                   c_float, c_float, c_float, c_float, c_float, c_float,
                                   c_ulong, c_ulong, c_ulong,
                                   c_float, c_float, c_float,
                                   filename_t, filename_t],                              c_bool),

            # display-enumeration ----------------------------------------
            "get_displays":                     ([POINTER(c_int32), POINTER(c_uint64)], c_bool),
            "get_device_name_for_display":      ([c_uint64, POINTER(c_int32), c_void_p],c_bool),
            "get_device_serial_for_display":    ([c_uint64, POINTER(c_int32), c_void_p],c_bool),
            "get_dimensions_for_display":       ([c_uint64, POINTER(c_ulong),
                                                  POINTER(c_ulong)],                   c_bool),
            "get_device_type_for_display":      ([c_uint64, POINTER(c_int32)],          c_bool),
            "get_calibration_for_display":      ([c_uint64, POINTER(c_float),
                                                  POINTER(c_float), POINTER(c_float),
                                                  POINTER(c_int32), POINTER(c_int32),
                                                  POINTER(c_float), POINTER(c_float),
                                                  POINTER(c_int32), POINTER(c_float),
                                                  POINTER(c_float), POINTER(c_int32),
                                                  POINTER(c_int32), c_void_p],         c_bool),
        }

        # bind the table above
        for name, (args, ret) in specs.items():
            setattr(self, f"_{name}", self._bind(self._fn(name), args, ret))

        # scalar *for_display* getters -----------------------------------
        scalar_map = {
            "get_invview_for_display":  c_int32,
            "get_ri_for_display":       c_int32,
            "get_bi_for_display":       c_int32,
            "get_tilt_for_display":     c_float,
            "get_displayaspect_for_display": c_float,
            "get_fringe_for_display":   c_float,
            "get_subp_for_display":     c_float,
            "get_viewcone_for_display": c_float,
            "get_center_for_display":   c_float,
            "get_pitch_for_display":    c_float,
        }
        for name, ctype in scalar_map.items():
            setattr(self, f"_{name}",
                    self._bind(self._fn(name), [c_uint64, POINTER(ctype)], c_bool))

        # multi-value *for_display* getters ------------------------------
        self._get_default_quilt_settings_for_display = self._bind(
            self._fn("get_default_quilt_settings_for_display"),
            [c_uint64, POINTER(c_float),
             POINTER(c_int32), POINTER(c_int32),
             POINTER(c_int32), POINTER(c_int32)], c_bool)

        self._get_window_position_for_display = self._bind(
            self._fn("get_window_position_for_display"),
            [c_uint64, POINTER(c_int64), POINTER(c_int64)], c_bool)

    # ------------- public API (unchanged except scalar wrappers) --------
    # initialise / shut-down
    def initialize(self, app_name: str) -> bool: return self._initialize_bridge(app_name)
    def uninitialize(self) -> bool:             return self._uninitialize_bridge()

    # window helpers
    def instance_window_gl(self, head_index=-1) -> Window:
        h = c_uint32()
        if not self._instance_window_gl(byref(h), head_index):
            raise RuntimeError("instance_window_gl failed")
        return Window(h.value)

    def instance_offscreen_window_gl(self, head_index=-1) -> Window:
        h = c_uint32()
        if not self._instance_offscreen_window_gl(byref(h), head_index):
            raise RuntimeError("instance_offscreen_window_gl failed")
        return Window(h.value)
    
    # Texture queries
    def get_window_dimensions(self, window_handle: Window) -> tuple[int, int]:
        w = c_uint(0)
        h = c_uint(0)
        if self._get_window_dimensions(window_handle, byref(w), byref(h)):
            return w.value, h.value
        raise RuntimeError("get_window_dimensions failed")

    def get_max_texture_size(self, window_handle: Window) -> int:
        sz = c_uint(0)
        if self._get_max_texture_size(window_handle, byref(sz)):
            return sz.value
        raise RuntimeError("get_max_texture_size failed")

    # Quilt / draw
    def set_interop_quilt_texture_gl(self, window_handle: Window, texture: int, fmt: PixelFormats,
                                     width: int, height: int, vx: int, vy: int,
                                     aspect: float, zoom: float) -> None:
        if not self._set_interop_quilt_texture_gl(window_handle, texture, fmt, width, height, vx, vy, aspect, zoom):
            raise RuntimeError("set_interop_quilt_texture_gl failed")

    def draw_interop_quilt_texture_gl(self, window_handle: Window, texture: int, fmt: PixelFormats,
                                      width: int, height: int, vx: int, vy: int,
                                      aspect: float, zoom: float) -> None:
        if not self._draw_interop_quilt_texture_gl(window_handle, texture, fmt, width, height, vx, vy, aspect, zoom):
            raise RuntimeError("draw_interop_quilt_texture_gl failed")

    def draw_interop_rgbd_texture_gl(self, window_handle: Window, texture: int, fmt: PixelFormats,
                                     width: int, height: int,
                                     quilt_width: int, quilt_height: int,
                                     vx: int, vy: int,
                                     aspect: float, focus: float, offset: float,
                                     zoom: float, depth_loc: int) -> None:
        if not self._draw_interop_rgbd_texture_gl(
            window_handle, texture, fmt, width, height,
            quilt_width, quilt_height, vx, vy,
            aspect, focus, offset, zoom, depth_loc
        ):
            raise RuntimeError("draw_interop_rgbd_texture_gl failed")

    def save_texture_to_file_gl(self, window_handle: Window, filename: str,
                                texture: int, fmt: PixelFormats,
                                width: int, height: int) -> None:
        if not self._save_texture_to_file_gl(window_handle, filename, texture, fmt, width, height):
            raise RuntimeError("save_texture_to_file_gl failed")

    def show_window(self, window_handle: Window, flag: bool) -> None:
        if not self._show_window(window_handle, flag):
            raise RuntimeError("show_window failed")

    # Offscreen
    def get_offscreen_window_texture_gl(self, window_handle: Window) -> tuple[int, PixelFormats, int, int]:
        tex = c_uint64(0)
        fmt = c_uint(0)
        w = c_uint(0)
        h = c_uint(0)
        if self._get_offscreen_window_texture_gl(window_handle, byref(tex), byref(fmt), byref(w), byref(h)):
            return tex.value, PixelFormats(fmt.value), w.value, h.value
        raise RuntimeError("get_offscreen_window_texture_gl failed")

    # Display / calibration getters
    def get_default_quilt_settings(self, window_handle: Window) -> tuple[float, int, int, int, int]:
        aspect = c_float()
        qx = c_int32()
        qy = c_int32()
        tx = c_int32()
        ty = c_int32()
        if self._get_default_quilt_settings(
            window_handle, byref(aspect),
            byref(qx), byref(qy),
            byref(tx), byref(ty)
        ):
            return aspect.value, qx.value, qy.value, tx.value, ty.value
        raise RuntimeError("get_default_quilt_settings failed")

    def get_display_for_window(self, window_handle: Window) -> int:
        idx = c_uint64()
        if self._get_display_for_window(window_handle, byref(idx)):
            return idx.value
        raise RuntimeError("get_display_for_window failed")

    def get_device_type(self, window_handle: Window) -> int:
        t = c_int32()
        if self._get_device_type(window_handle, byref(t)):
            return t.value
        raise RuntimeError("get_device_type failed")

    def get_viewcone(self, window_handle: Window) -> float:
        v = c_float()
        if self._get_viewcone(window_handle, byref(v)):
            return v.value
        raise RuntimeError("get_viewcone failed")

    def get_invview(self, window_handle: Window) -> int:
        v = c_int32()
        if self._get_invview(window_handle, byref(v)):
            return v.value
        raise RuntimeError("get_invview failed")

    def get_ri(self, window_handle: Window) -> int:
        r = c_int32()
        if self._get_ri(window_handle, byref(r)):
            return r.value
        raise RuntimeError("get_ri failed")

    def get_bi(self, window_handle: Window) -> int:
        b = c_int32()
        if self._get_bi(window_handle, byref(b)):
            return b.value
        raise RuntimeError("get_bi failed")

    def get_tilt(self, window_handle: Window) -> float:
        t = c_float()
        if self._get_tilt(window_handle, byref(t)):
            return t.value
        raise RuntimeError("get_tilt failed")

    def get_display_aspect(self, window_handle: Window) -> float:
        a = c_float()
        if self._get_display_aspect(window_handle, byref(a)):
            return a.value
        raise RuntimeError("get_displayaspect failed")

    def get_fringe(self, window_handle: Window) -> float:
        f = c_float()
        if self._get_fringe(window_handle, byref(f)):
            return f.value
        raise RuntimeError("get_fringe failed")

    def get_subp(self, window_handle: Window) -> float:
        s = c_float()
        if self._get_subp(window_handle, byref(s)):
            return s.value
        raise RuntimeError("get_subp failed")

    def get_pitch(self, window_handle: Window) -> float:
        p = c_float()
        if self._get_pitch(window_handle, byref(p)):
            return p.value
        raise RuntimeError("get_pitch failed")

    def get_center(self, window_handle: Window) -> float:
        c = c_float()
        if self._get_center(window_handle, byref(c)):
            return c.value
        raise RuntimeError("get_center failed")

    def get_window_position(self, window_handle: Window) -> tuple[int, int]:
        x = c_int64()
        y = c_int64()
        if self._get_window_position(window_handle, byref(x), byref(y)):
            return x.value, y.value
        raise RuntimeError("get_window_position failed")
    
    def get_displays(self) -> list[int]:
        count = c_int32(0)
        # first call to get count
        if not self._get_displays(byref(count), None):
            raise RuntimeError("get_displays failed")
        if count.value == 0:
            return []
        # allocate array
        arr_type = c_uint64 * count.value
        arr = arr_type()
        if not self._get_displays(byref(count), arr):
            raise RuntimeError("get_displays failed on second pass")
        return list(arr)

    def get_device_name_for_display(self, display_index: int) -> str:
        # two-pass to get buffer length
        length = c_int32(0)
        self._get_device_name_for_display(display_index, byref(length), None)
        if length.value <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length.value)
        if not self._get_device_name_for_display(display_index, byref(length), ctypes.cast(buf, ctypes.c_void_p)):
            raise RuntimeError("get_device_name_for_display failed")
        return buf.value

    def get_device_serial_for_display(self, display_index: int) -> str:
        length = c_int32(0)
        self._get_device_serial_for_display(display_index, byref(length), None)
        if length.value <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length.value)
        if not self._get_device_serial_for_display(display_index, byref(length), ctypes.cast(buf, ctypes.c_void_p)):
            raise RuntimeError("get_device_serial_for_display failed")
        return buf.value

    def get_dimensions_for_display(self, display_index: int) -> tuple[int, int]:
        w = c_uint64()
        h = c_uint64()
        if self._get_dimensions_for_display(display_index, byref(w), byref(h)):
            return w.value, h.value
        raise RuntimeError("get_dimensions_for_display failed")

    def get_device_type_for_display(self, display_index: int) -> int:
        t = c_int32()
        if self._get_device_type_for_display(display_index, byref(t)):
            return t.value
        raise RuntimeError("get_device_type_for_display failed")

    def get_calibration_for_display(self, display_index: int) -> LKGCalibration:
        cal = LKGCalibration()
        # first pass to get number_of_cells
        num = c_int32(0)
        if not self._get_calibration_for_display(
            display_index,
            byref(cal.Center), byref(cal.Pitch), byref(cal.Slope),
            byref(cal.Width), byref(cal.Height),
            byref(cal.Dpi), byref(cal.FlipX),
            byref(cal.InvView), byref(cal.Viewcone),
            byref(cal.Fringe), byref(cal.CellPatternMode),
            byref(num),
            None
        ):
            raise RuntimeError("get_calibration_for_display failed")
        if num.value > 0:
            # buffer of float[num]
            buf = (c_float * num.value)()
            if not self._get_calibration_for_display(
                display_index,
                byref(cal.Center), byref(cal.Pitch), byref(cal.Slope),
                byref(cal.Width), byref(cal.Height),
                byref(cal.Dpi), byref(cal.FlipX),
                byref(cal.InvView), byref(cal.Viewcone),
                byref(cal.Fringe), byref(cal.CellPatternMode),
                byref(num),
                ctypes.cast(buf, ctypes.c_void_p)
            ):
                raise RuntimeError("get_calibration_for_display failed on second pass")
            # copy into Python list
            cal.Cells = list(buf)
        else:
            cal.Cells = []
        return cal

    # and then one‐liner wrappers for the rest of the “for_display” calls:
    def get_invview_for_display(self, idx):   return self._scalar_call(self._get_invview_for_display,  idx, c_int32)
    def get_ri_for_display(self, idx):        return self._scalar_call(self._get_ri_for_display,       idx, c_int32)
    def get_bi_for_display(self, idx):        return self._scalar_call(self._get_bi_for_display,       idx, c_int32)
    def get_tilt_for_display(self, idx):      return self._scalar_call(self._get_tilt_for_display,     idx, c_float)
    def get_display_aspect_for_display(self, idx): return self._scalar_call(self._get_displayaspect_for_display, idx, c_float)
    def get_fringe_for_display(self, idx):    return self._scalar_call(self._get_fringe_for_display,   idx, c_float)
    def get_subp_for_display(self, idx):      return self._scalar_call(self._get_subp_for_display,     idx, c_float)
    def get_viewcone_for_display(self, idx):  return self._scalar_call(self._get_viewcone_for_display, idx, c_float)
    def get_center_for_display(self, idx):    return self._scalar_call(self._get_center_for_display,   idx, c_float)
    def get_pitch_for_display(self, idx):     return self._scalar_call(self._get_pitch_for_display,    idx, c_float)
    def get_default_quilt_settings_for_display(
        self, idx
    ) -> DefaultQuiltSettings:
        d = DefaultQuiltSettings()
        if self._get_default_quilt_settings_for_display(
            idx,
            byref(d.Aspect),
            byref(d.QuiltWidth), byref(d.QuiltHeight),
            byref(d.QuiltColumns), byref(d.QuiltRows)
        ):
            return d
        raise RuntimeError("get_default_quilt_settings_for_display failed")

    def get_window_position_for_display(self, idx) -> tuple[int,int]:
        x = c_int64()
        y = c_int64()
        if self._get_window_position_for_display(idx, byref(x), byref(y)):
            return x.value, y.value
        raise RuntimeError("get_window_position_for_display failed")

    def get_calibration(self, window_handle: Window) -> LKGCalibration:
        cal = LKGCalibration()
        num = c_int32(0)
        # first pass to get cell count
        ok = self._get_calibration(
            window_handle,
            byref(cal.Center), byref(cal.Pitch), byref(cal.Slope),
            byref(cal.Width), byref(cal.Height),
            byref(cal.Dpi), byref(cal.FlipX),
            byref(cal.InvView), byref(cal.Viewcone),
            byref(cal.Fringe), byref(cal.CellPatternMode),
            byref(num), None
        )
        if not ok:
            raise RuntimeError("get_calibration failed")
        if num.value:
            buf = (c_float * num.value)()
            ok = self._get_calibration(
                window_handle,
                byref(cal.Center), byref(cal.Pitch), byref(cal.Slope),
                byref(cal.Width), byref(cal.Height),
                byref(cal.Dpi), byref(cal.FlipX),
                byref(cal.InvView), byref(cal.Viewcone),
                byref(cal.Fringe), byref(cal.CellPatternMode),
                byref(num),
                ctypes.cast(buf, ctypes.c_void_p)
            )
            if not ok:
                raise RuntimeError("get_calibration (2nd pass) failed")
            cal.Cells = list(buf)
        else:
            cal.Cells = []
        return cal

    def get_device_name(self, window_handle: Window) -> str:
        length = c_int32(0)
        self._get_device_name(window_handle, byref(length), None)
        if length.value <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length.value) \
              if sys.platform.startswith("win") else ctypes.create_string_buffer(length.value)
        if not self._get_device_name(
            window_handle, byref(length),
            ctypes.cast(buf, ctypes.c_void_p)
        ):
            raise RuntimeError("get_device_name failed")
        return buf.value

    def get_device_serial(self, window_handle: Window) -> str:
        length = c_int32(0)
        self._get_device_serial(window_handle, byref(length), None)
        if length.value <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length.value) \
              if sys.platform.startswith("win") else ctypes.create_string_buffer(length.value)
        if not self._get_device_serial(
            window_handle, byref(length),
            ctypes.cast(buf, ctypes.c_void_p)
        ):
            raise RuntimeError("get_device_serial failed")
        return buf.value

    def save_image_to_file(self, window_handle: Window,
                           filename: str, image_ptr: int,
                           fmt: PixelFormats, width: int, height: int) -> None:
        arg = filename if sys.platform.startswith("win") else filename.encode("utf-8")
        if not self._save_image_to_file(
            window_handle, arg,
            ctypes.c_void_p(image_ptr),
            fmt, width, height
        ):
            raise RuntimeError("save_image_to_file failed")

    def quiltify_rgbd(self, window_handle: Window,
                      columns: int, rows: int, views: int,
                      aspect: float, zoom: float,
                      cam_dist: float, fov: float,
                      crop_pos_x: float, crop_pos_y: float,
                      depth_inversion: int, chroma_depth: int,
                      depth_loc: int,
                      depthiness: float, depth_cutoff: float,
                      focus: float,
                      input_path: str, output_path: str) -> None:
        inp = input_path if sys.platform.startswith("win") else input_path.encode("utf-8")
        out = output_path if sys.platform.startswith("win") else output_path.encode("utf-8")
        if not self._quiltify_rgbd(
            window_handle,
            columns, rows, views,
            aspect, zoom, cam_dist, fov,
            crop_pos_x, crop_pos_y,
            depth_inversion, chroma_depth, depth_loc,
            depthiness, depth_cutoff, focus,
            inp, out
        ):
            raise RuntimeError("quiltify_rgbd failed")