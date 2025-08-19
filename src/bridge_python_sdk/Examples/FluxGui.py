#!/usr/bin/env python3
"""
GUI for Flux Server using Dear ImGui + GLFW + BridgeAPI

Adds keyboard shortcuts:
 ← / →  : step through saved image history
 F5     : regenerate the current prompt immediately
 Space  : pause any active RGBD loop (random or themed)

All other behaviour is unchanged.
"""

import sys
import os
import base64
import requests
import threading
import queue
import time

from io import BytesIO

import imgui
from imgui.integrations.glfw import GlfwRenderer
import glfw
from PIL import Image

# Add the parent directory to the Python path for BridgeApi
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from BridgeApi import BridgeAPI, PixelFormats  # type: ignore

from FluxUtils import *


class FluxGUI:
    """
    GUI for Flux Server using Dear ImGui + GLFW + BridgeAPI

    Adds keyboard shortcuts:
     ← / →  : step through saved image history
     F5     : regenerate the current prompt immediately
     Space  : pause any active RGBD loop (random or themed)

    All other behaviour is unchanged.
    """

    def __init__(self) -> None:
        # ---------- Flux Client Wrappers ----------
        self.SERVER_URL = "http://127.0.0.1:8000"
        self.prompt_generator = RandomPromptGenerator('src/bridge_python_sdk/assets/prompts_landscapes.json')
        self.api_prompt_gen = APIPromptGenerator(self.SERVER_URL, self.prompt_generator)

        # GUI & Rendering state
        self.window = None
        self.impl = None
        self.bridge = None
        self.br_wnd = None
        self.qw = None
        self.qh = None
        self.cols = None
        self.rows = None
        self.aspect = None
        script_dir = os.path.dirname(__file__)
        out_dir = os.path.join(script_dir, 'out')
        self.image_mgr = ImageManager(out_dir)

        self.current_index = 0
        self.prompt = "A dog playing fetch"
        self.theme = ""
        self.focus = 0.0
        self.depthiness = 1.5
        self.steps = 4
        self.scale = 3.5
        self.height = 768
        self.width = 1360
        self.max_len = 256

        self.auto_looping = False
        self.themed_looping = False
        self.loop_thread = None
        self.themed_thread = None
        self.loop_stop_event = threading.Event()
        self.themed_stop_event = threading.Event()
        self.result_queue = queue.Queue()

        self.prev_key_states = {}

    def fetch_image(self, endpoint: str, payload: dict) -> bytes:
        url = f"{self.SERVER_URL.rstrip('/')}{endpoint}"
        resp = requests.post(url, json=payload)
        resp.raise_for_status()
        return base64.b64decode(resp.json()["image"])

    def generate_rgbd(self, prompt: str, steps: int, scale: float, h: int, w: int, max_len: int) -> bytes:
        return self.fetch_image("/flux/generate_rgbd", {
            "prompt": prompt,
            "num_inference_steps": steps,
            "guidance_scale": scale,
            "height": h,
            "width": w,
            "max_sequence_length": max_len,
        })

    def init_window(self) -> None:
        if not glfw.init():
            print("Could not initialize GLFW", file=sys.stderr)
            sys.exit(1)
        self.window = glfw.create_window(1600, 900, "Flux GUI", None, None)
        glfw.make_context_current(self.window)
        imgui.create_context()
        self.impl = GlfwRenderer(self.window)

    def init_bridge(self) -> None:
        # self.bridge = BridgeAPI(library_path=r"C:\\Users\\alec\\source\\repos\\LookingGlassBridge\\out\\build\\x64-Release")
        self.bridge = BridgeAPI()
        if not self.bridge.initialize("FluxGUI"):
            print("BridgeAPI init failed", file=sys.stderr)
            return
        self.br_wnd = self.bridge.instance_window_gl(-1)
        aspect_ratio, qw, qh, cols, rows = self.bridge.get_default_quilt_settings(self.br_wnd)
        self.cols, self.rows = 10, 10
        self.aspect = float(aspect_ratio)
        self.qw, self.qh = qw, qh

    def key_pressed(self, key: int) -> bool:
        curr = glfw.get_key(self.window, key) == glfw.PRESS
        prev = self.prev_key_states.get(key, False)
        self.prev_key_states[key] = curr
        return curr and not prev

    def enqueue(self, p: str, img: Image.Image) -> None:
        self.result_queue.put((p, img))

    def loop_random(self) -> None:
        while not self.loop_stop_event.is_set():
            p = self.prompt_generator.GetNextRandomPrompt()
            try:
                data = self.generate_rgbd(p, self.steps, self.scale, self.height, self.width, self.max_len)
                combo = Image.open(BytesIO(data)).convert("RGBA")
                self.enqueue(p, combo)
            except Exception as e:
                print(f"Error in random loop: {e}", file=sys.stderr)
            time.sleep(0.1)

    def loop_themed(self) -> None:
        while not self.themed_stop_event.is_set():
            try:
                p = self.api_prompt_gen.generate(self.theme)
                data = self.generate_rgbd(p, self.steps, self.scale, self.height, self.width, self.max_len)
                combo = Image.open(BytesIO(data)).convert("RGBA")
                self.enqueue(p, combo)
            except Exception as e:
                print(f"Error in themed loop: {e}", file=sys.stderr)
            time.sleep(0.1)

    def process_queue(self) -> None:
        while not self.result_queue.empty():
            p, img = self.result_queue.get()
            new_idx = self.image_mgr.save(img, p)
            self.current_index = new_idx

    def render(self) -> None:
        glfw.poll_events()
        self.impl.process_inputs()
        imgui.new_frame()
        io = imgui.get_io()

        # ---------- Keyboard shortcuts ----------
        if self.key_pressed(glfw.KEY_LEFT):
            if self.current_index > 0:
                self.current_index -= 1
        if self.key_pressed(glfw.KEY_RIGHT):
            if self.current_index < self.image_mgr.count() - 1:
                self.current_index += 1
        if self.key_pressed(glfw.KEY_F5):
            try:
                data = self.generate_rgbd(self.prompt, self.steps, self.scale, self.height, self.width, self.max_len)
                combo = Image.open(BytesIO(data)).convert("RGBA")
                self.enqueue(self.prompt, combo)
            except Exception as e:
                print(f"Error regenerating RGBD: {e}", file=sys.stderr)
        if self.key_pressed(glfw.KEY_SPACE):
            if self.auto_looping and self.loop_thread:
                self.auto_looping = False
                self.loop_stop_event.set()
                self.loop_thread.join()
            if self.themed_looping and self.themed_thread:
                self.themed_looping = False
                self.themed_stop_event.set()
                self.themed_thread.join()

        self.process_queue()

        imgui.set_next_window_position(0, 0)
        imgui.set_next_window_size(io.display_size[0], io.display_size[1])
        imgui.begin("Flux Fullscreen", flags=imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE | imgui.WINDOW_NO_MOVE)

        # Controls
        imgui.begin_child("Controls", width=io.display_size[0]*0.3, height=0, border=True)
        imgui.text("Flux Controls")
        _, self.prompt = imgui.input_text("Prompt", self.prompt, 256)
        _, self.theme = imgui.input_text("Theme", self.theme, 256)
        if imgui.button("Random Prompt"):
            self.prompt = self.prompt_generator.GetNextRandomPrompt()
        imgui.same_line()
        if imgui.button("Generate Prompt"):
            try:
                self.prompt = self.api_prompt_gen.generate(self.theme)
            except Exception as e:
                print(f"Error generating prompt: {e}", file=sys.stderr)

        imgui.separator()
        if not self.themed_looping:
            if imgui.button("Start Themed RGBD Loop"):
                self.themed_looping = True
                self.themed_stop_event.clear()
                self.themed_thread = threading.Thread(target=self.loop_themed, daemon=True)
                self.themed_thread.start()
        else:
            if imgui.button("Stop Themed RGBD Loop"):
                self.themed_looping = False
                self.themed_stop_event.set()
                self.themed_thread.join()

        imgui.separator()
        if not self.auto_looping:
            if imgui.button("Start RGBD Loop"):
                self.auto_looping = True
                self.loop_stop_event.clear()
                self.loop_thread = threading.Thread(target=self.loop_random, daemon=True)
                self.loop_thread.start()
        else:
            if imgui.button("Stop RGBD Loop"):
                self.auto_looping = False
                self.loop_stop_event.set()
                self.loop_thread.join()

        imgui.separator()
        _, self.focus = imgui.slider_float("Focus", self.focus, -1.0, 1.0)
        _, self.depthiness = imgui.slider_float("Depthiness", self.depthiness, 0.0, 3.0)
        if imgui.button("Generate RGBD"):
            try:
                data = self.generate_rgbd(self.prompt, self.steps, self.scale, self.height, self.width, self.max_len)
                combo = Image.open(BytesIO(data)).convert("RGBA")
                self.enqueue(self.prompt, combo)
            except Exception as e:
                print(f"Error generating RGBD: {e}", file=sys.stderr)
        imgui.same_line()
        imgui.end_child()

        # Preview
        imgui.same_line()
        imgui.begin_child("RGBD Panel", width=0, height=0, border=True)
        imgui.text("Generated RGBD Output")
        if self.image_mgr.count() > 0:
            tex_id, (w, h) = self.image_mgr.get_texture(self.current_index)
            max_w = io.display_size[0]*0.65 - 20
            disp_w = min(w, max_w)
            disp_h = int(h * disp_w / w)
            imgui.image(tex_id, disp_w, disp_h)
            imgui.separator()
            if imgui.button("Prev") and self.current_index > 0:
                self.current_index -= 1
            imgui.same_line()
            if imgui.button("Next") and self.current_index < self.image_mgr.count() - 1:
                self.current_index += 1
            imgui.separator()
            imgui.text_wrapped(self.image_mgr.get_prompt(self.current_index))
        else:
            imgui.text("No saved images yet.")
        imgui.end_child()

        imgui.end()

        # Render to Looking Glass
        if self.image_mgr.count() > 0:
            tex_id, (w, h) = self.image_mgr.get_texture(self.current_index)
            zoom = 1.0
            depth_loc = 2

            focus_min = 0.01
            focus_max = -0.01
            bridge_focus = self.focus * self.depthiness
            normalized_focus = focus_min + ((bridge_focus + 1.0) / 2.0) * (focus_max - focus_min)

            self.bridge.draw_interop_rgbd_texture_gl(
                self.br_wnd,
                tex_id,
                PixelFormats.RGBA,
                w,
                h,
                self.qw,
                self.qh,
                self.cols,
                self.rows,
                self.aspect,
                normalized_focus,
                self.depthiness,
                zoom,
                depth_loc
            )

        imgui.render()
        self.impl.render(imgui.get_draw_data())
        GL.glFinish()
        glfw.swap_buffers(self.window)

    def cleanup(self) -> None:
        self.loop_stop_event.set()
        self.themed_stop_event.set()
        if self.loop_thread:
            self.loop_thread.join()
        if self.themed_thread:
            self.themed_thread.join()
        self.image_mgr.clear_cache()
        self.impl.shutdown()
        glfw.terminate()

    def run(self) -> None:
        self.init_window()
        self.init_bridge()
        while self.window and not glfw.window_should_close(self.window):
            self.render()
        self.cleanup()


def main() -> None:
    gui = FluxGUI()
    gui.run()


if __name__ == "__main__":
    main()
