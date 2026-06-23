import importlib
import sys
import unittest
from pathlib import Path


def import_bridge_api_from_src():
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    sys.path.insert(0, src_path)
    try:
        return importlib.import_module("bridge_python_sdk.BridgeApi")
    finally:
        sys.path.remove(src_path)


class PackageImportTests(unittest.TestCase):
    def test_bridge_api_imports_from_package_layout(self):
        src_path = str(Path(__file__).resolve().parents[1] / "src")
        sys.path.insert(0, src_path)
        try:
            module = importlib.import_module("bridge_python_sdk.BridgeApi")
        finally:
            sys.path.remove(src_path)

        self.assertTrue(hasattr(module, "BridgeAPI"))

    def test_bridge_api_imports_from_legacy_module_layout(self):
        package_path = str(Path(__file__).resolve().parents[1] / "src" / "bridge_python_sdk")
        sys.path.insert(0, package_path)
        try:
            module = importlib.import_module("BridgeApi")
        finally:
            sys.path.remove(package_path)
            sys.modules.pop("BridgeApi", None)

        self.assertTrue(hasattr(module, "BridgeAPI"))


class BridgeApiWrapperTests(unittest.TestCase):
    def test_get_display_aspect_uses_bound_native_name(self):
        module = import_bridge_api_from_src()
        api = object.__new__(module.BridgeAPI)

        def get_displayaspect(_window_handle, out):
            out._obj.value = 0.75
            return True

        api._get_displayaspect = get_displayaspect

        self.assertAlmostEqual(api.get_display_aspect(123), 0.75)

    def test_for_display_scalar_wrappers_pass_debug_flag(self):
        module = import_bridge_api_from_src()
        api = object.__new__(module.BridgeAPI)
        api.debug = False
        calls = []

        def get_pitch_for_display(display_index, out):
            calls.append(display_index)
            out._obj.value = 52.5
            return True

        api._get_pitch_for_display = get_pitch_for_display

        self.assertAlmostEqual(api.get_pitch_for_display(42), 52.5)
        self.assertEqual(calls, [42])


if __name__ == "__main__":
    unittest.main()
