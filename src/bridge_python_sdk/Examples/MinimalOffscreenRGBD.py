#!/usr/bin/env python3
# MinimalOffscreenRGBD.py
import sys
import os
import io
import time
import urllib.request
import glfw
import numpy as np
from PIL import Image
from OpenGL import GL

# Make BridgeApi importable from sibling directory (adjust as needed)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from BridgeApi import BridgeAPI, PixelFormats

# ----------------------- helpers: tiny GL program for textured quad -----------------------
def _compile_shader(src, stype):
    sh = GL.glCreateShader(stype)
    GL.glShaderSource(sh, src)
    GL.glCompileShader(sh)
    ok = GL.glGetShaderiv(sh, GL.GL_COMPILE_STATUS)
    if not ok:
        log = GL.glGetShaderInfoLog(sh).decode("utf-8", errors="ignore")
        raise RuntimeError(f"Shader compile failed:\n{log}")
    return sh

def _make_quad_program():
    vs = "#version 330 core\nlayout(location=0) in vec2 p; layout(location=1) in vec2 t; out vec2 uv; void main(){ gl_Position=vec4(p,0.0,1.0); uv=vec2(t.x, 1.0 - t.y); }\n"
    fs = "#version 330 core\nin vec2 uv; out vec4 o; uniform sampler2D tex; void main(){ o = texture(tex, uv); }\n"
    v = _compile_shader(vs, GL.GL_VERTEX_SHADER)
    f = _compile_shader(fs, GL.GL_FRAGMENT_SHADER)
    prog = GL.glCreateProgram()
    GL.glAttachShader(prog, v)
    GL.glAttachShader(prog, f)
    GL.glLinkProgram(prog)
    ok = GL.glGetProgramiv(prog, GL.GL_LINK_STATUS)
    GL.glDeleteShader(v)
    GL.glDeleteShader(f)
    if not ok:
        log = GL.glGetProgramInfoLog(prog).decode("utf-8", errors="ignore")
        raise RuntimeError(f"Program link failed:\n{log}")
    return prog

def _make_screen_quad():
    # pos.xy, uv.xy
    verts = np.array([
        -1.0, -1.0, 0.0, 0.0,
         1.0, -1.0, 1.0, 0.0,
         1.0,  1.0, 1.0, 1.0,
        -1.0,  1.0, 0.0, 1.0
    ], dtype=np.float32)
    idx = np.array([0, 1, 2, 2, 3, 0], dtype=np.uint32)
    vao = GL.glGenVertexArrays(1)
    vbo = GL.glGenBuffers(1)
    ebo = GL.glGenBuffers(1)
    GL.glBindVertexArray(vao)
    GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vbo)
    GL.glBufferData(GL.GL_ARRAY_BUFFER, verts.nbytes, verts, GL.GL_STATIC_DRAW)
    GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, ebo)
    GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, idx.nbytes, idx, GL.GL_STATIC_DRAW)
    GL.glEnableVertexAttribArray(0)
    GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, GL.GL_FALSE, 4 * 4, GL.ctypes.c_void_p(0))
    GL.glEnableVertexAttribArray(1)
    GL.glVertexAttribPointer(1, 2, GL.GL_FLOAT, GL.GL_FALSE, 4 * 4, GL.ctypes.c_void_p(2 * 4))
    GL.glBindVertexArray(0)
    return vao, vbo, ebo

# ----------------------- load a sample RGBD into a GL texture -----------------------
with urllib.request.urlopen("https://s3.amazonaws.com/lkg-blocks/u/72d8084888a8489c/rgbd.png") as resp:
    data_bytes = resp.read()
rgbd_img = Image.open(io.BytesIO(data_bytes)).convert("RGBA")
rgbd_np = np.array(rgbd_img, dtype=np.uint8)
src_h, src_w, _ = rgbd_np.shape
depth_loc = 2

# ----------------------- GLFW + preview window -----------------------
if not glfw.init():
    print("Error: failed to initialize GLFW", file=sys.stderr)
    sys.exit(1)
glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
glfw.window_hint(glfw.VISIBLE, glfw.TRUE)
preview = glfw.create_window(800, 800, "Offscreen Bridge Preview", None, None)
if not preview:
    print("Error: failed to create preview window", file=sys.stderr)
    glfw.terminate()
    sys.exit(1)
glfw.make_context_current(preview)
glfw.swap_interval(0)

# Create GL resources in the preview context
quad_prog = _make_quad_program()
quad_vao, quad_vbo, quad_ebo = _make_screen_quad()

# Upload immutable RGBD source texture once
src_tex = GL.glGenTextures(1)
GL.glBindTexture(GL.GL_TEXTURE_2D, src_tex)
GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
try:
    GL.glTexStorage2D(GL.GL_TEXTURE_2D, 1, GL.GL_RGBA8, src_w, src_h)
except Exception:
    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, src_w, src_h, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None)
GL.glTexSubImage2D(GL.GL_TEXTURE_2D, 0, 0, 0, src_w, src_h, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, rgbd_np)
GL.glBindTexture(GL.GL_TEXTURE_2D, 0)

# ----------------------- Bridge: true offscreen window -----------------------
# bridge = BridgeAPI()
bridge = BridgeAPI(library_path = r"C:\\Users\\alec\\source\\repos\\LookingGlassBridge\\out\\build\\x64-Release")
if not bridge.initialize("DisplayRGBD_Offscreen"):
    print("Bridge initialize failed", file=sys.stderr)
    glfw.destroy_window(preview)
    glfw.terminate()
    sys.exit(1)

br_wnd = bridge.instance_offscreen_window_gl(-1)
if br_wnd == 0:
    print("Bridge.instance_offscreen_window_gl failed", file=sys.stderr)
    glfw.destroy_window(preview)
    glfw.terminate()
    sys.exit(1)

asp, quilt_w, quilt_h, cols, rows = bridge.get_default_quilt_settings(br_wnd)
aspect = float(asp)

# --- YPU MUST POSITION THE WINDOW EXACTLY LIKE THIS ---
try:
    out_w, out_h = bridge.get_window_dimensions(br_wnd)
except Exception:
    out_w, out_h = quilt_w, quilt_h
try:
    win_x, win_y = bridge.get_window_position(br_wnd)
except Exception:
    try:
        disp_idx = bridge.get_display_for_window(br_wnd)
        win_x, win_y = bridge.get_window_position_for_display(disp_idx)
    except Exception:
        win_x, win_y = 0, 0
glfw.set_window_size(preview, int(out_w), int(out_h))
glfw.set_window_pos(preview, int(win_x), int(win_y))

# Focus/depthiness mapping consistent with your sample
focus_input = 0.0
depthiness_input = 1.0
focus_min = 0.005
focus_max = -0.007
def _normalized_focus(f_in, depth_in):
    return focus_min + ((((f_in * depth_in)) + 1.0) / 2.0) * (focus_max - focus_min)

# ----------------------- perf stats -----------------------
frame_times = []
last_report_time = time.perf_counter()

# ----------------------- main loop -----------------------
while not glfw.window_should_close(preview):
    start = time.perf_counter()

    # Submit RGBD → hologram on the offscreen Bridge window
    bridge.draw_interop_rgbd_texture_gl(
        br_wnd,
        src_tex,
        PixelFormats.RGBA,
        src_w,
        src_h,
        quilt_w,
        quilt_h,
        cols,
        rows,
        aspect,
        _normalized_focus(focus_input, depthiness_input),
        depthiness_input,
        1.0,
        depth_loc
    )

    # Mirror the offscreen Bridge window's final texture into our preview
    tex_id, fmt, hw, hh = bridge.get_offscreen_window_texture_gl(br_wnd)

    fb_w, fb_h = glfw.get_framebuffer_size(preview)
    GL.glViewport(0, 0, fb_w, fb_h)
    GL.glDisable(GL.GL_DEPTH_TEST)
    GL.glClearColor(0.0, 0.0, 0.0, 1.0)
    GL.glClear(GL.GL_COLOR_BUFFER_BIT)

    GL.glUseProgram(quad_prog)
    GL.glActiveTexture(GL.GL_TEXTURE0)
    GL.glBindTexture(GL.GL_TEXTURE_2D, tex_id)
    loc = GL.glGetUniformLocation(quad_prog, "tex")
    GL.glUniform1i(loc, 0)
    GL.glBindVertexArray(quad_vao)
    GL.glDrawElements(GL.GL_TRIANGLES, 6, GL.GL_UNSIGNED_INT, None)
    GL.glBindVertexArray(0)
    GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
    GL.glUseProgram(0)

    glfw.swap_buffers(preview)
    glfw.poll_events()

    now = time.perf_counter()
    frame_time = (now - start) * 1000.0
    frame_times.append(frame_time)
    if now - last_report_time >= 5.0 and frame_times:
        avg_fps = 1000.0 / (sum(frame_times) / len(frame_times))
        srt = sorted(frame_times)
        def pct(p):
            i = int(round(p * len(srt) + 0.5)) - 1
            i = max(0, min(i, len(srt) - 1))
            return srt[i]
        p95 = pct(0.95)
        p99 = pct(0.99)
        p999 = pct(0.999)
        title = f"Offscreen Bridge Preview  |  Avg FPS: {avg_fps:.2f}  |  95%: {p95:.2f} ms  99%: {p99:.2f} ms  99.9%: {p999:.2f} ms"
        glfw.set_window_title(preview, title)
        print(f"Avg FPS: {avg_fps:.2f}, Frame times (ms) - 95%: {p95:.2f}, 99%: {p99:.2f}, 99.9%: {p999:.2f}", flush=True)
        frame_times.clear()
        last_report_time = now

# ----------------------- cleanup -----------------------
try:
    GL.glDeleteBuffers(1, [quad_vbo])
    GL.glDeleteBuffers(1, [quad_ebo])
    GL.glDeleteVertexArrays(1, [quad_vao])
    GL.glDeleteProgram(quad_prog)
    GL.glDeleteTextures(1, [src_tex])
except Exception:
    pass
bridge.uninitialize()
glfw.destroy_window(preview)
glfw.terminate()
