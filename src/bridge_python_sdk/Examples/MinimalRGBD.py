#!/usr/bin/env python3
# MinimalQuilt.py
import sys
import os
import io
import time
import urllib.request
import glfw

from PIL import Image
import numpy as np
from OpenGL import GL

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from BridgeApi import BridgeAPI, PixelFormats

# Download an example RGBD
with urllib.request.urlopen("https://s3.amazonaws.com/lkg-blocks/u/72d8084888a8489c/rgbd.png") as resp:
    data_bytes = resp.read()
image = Image.open(io.BytesIO(data_bytes)).convert("RGBA")
data = np.array(image, dtype=np.uint8)
h, w, _ = data.shape
depth_loc = 2

# Init GLFW
if not glfw.init():
    print("Error: failed to initialize GLFW", file=sys.stderr)
    sys.exit(1)
glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
dummy = glfw.create_window(1, 1, "", None, None)
if not dummy:
    print("Error: failed to create hidden GLFW window", file=sys.stderr)
    glfw.terminate()
    sys.exit(1)
glfw.make_context_current(dummy)
glfw.swap_interval(0)  # disable vsync exactly once

# Init Bridge
# bridge = BridgeAPI()
bridge = BridgeAPI(library_path = r"C:\\Users\\alec\\source\\repos\\LookingGlassBridge\\out\\build\\x64-Release")
if not bridge.initialize("DisplayRGBD"):
    print("Bridge initialize failed", file=sys.stderr)
    glfw.destroy_window(dummy)
    glfw.terminate()
    sys.exit(1)

br_wnd = bridge.instance_window_gl(-1)
if br_wnd == 0:
    print("Bridge.instance_window_gl failed", file=sys.stderr)
    glfw.destroy_window(dummy)
    glfw.terminate()
    sys.exit(1)

asp, quiltWidth, quiltHeight, cols, rows = bridge.get_default_quilt_settings(br_wnd)
aspect = float(asp)

focus_input = 0
depthiness_input = 1
focus_min = 0.005
focus_max = -0.007
normalized_focus = focus_min + ((((focus_input * depthiness_input)) + 1.0) / 2.0) * (focus_max - focus_min)

# Upload ONCE: immutable texture
tex = GL.glGenTextures(1)
GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
try:
    GL.glTexStorage2D(GL.GL_TEXTURE_2D, 1, GL.GL_RGBA8, w, h)
except Exception:
    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None)
GL.glTexSubImage2D(GL.GL_TEXTURE_2D, 0, 0, 0, w, h, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, data)

# Perf stats
frame_times = []
last_report_time = time.perf_counter()

# Main loop
while not glfw.window_should_close(dummy):
    start = time.perf_counter()

    bridge.draw_interop_rgbd_texture_gl(
        br_wnd,
        tex,
        PixelFormats.RGBA,
        w,
        h,
        quiltWidth,
        quiltHeight,
        cols,
        rows,
        aspect,
        normalized_focus,
        depthiness_input,
        1.0,
        depth_loc
    )
    glfw.poll_events()

    now = time.perf_counter()
    frame_time = (now - start) * 1000.0  # ms
    frame_times.append(frame_time)

    if now - last_report_time >= 5.0 and frame_times:
        avg_fps = 1000.0 / (sum(frame_times) / len(frame_times))
        sorted_times = sorted(frame_times)
        def percentile(p):
            idx = int(round(p * len(sorted_times) + 0.5)) - 1
            idx = max(0, min(idx, len(sorted_times) - 1))
            return sorted_times[idx]
        p95 = percentile(0.95)
        p99 = percentile(0.99)
        p999 = percentile(0.999)
        print(f"Avg FPS: {avg_fps:.2f}, Frame times (ms) - 95%: {p95:.2f}, 99%: {p99:.2f}, 99.9%: {p999:.2f}")
        frame_times.clear()
        last_report_time = now

# Cleanup
GL.glDeleteTextures(1, [tex])
glfw.destroy_window(dummy)
glfw.terminate()
