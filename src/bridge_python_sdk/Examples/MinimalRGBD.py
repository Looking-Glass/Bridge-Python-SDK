#!/usr/bin/env python3
# MinimalQuilt.py
import sys
import os
import io
import time
import urllib.request
import glfw
import math

from PIL import Image
import numpy as np
from OpenGL import GL

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from BridgeApi import BridgeAPI, PixelFormats

# ---------------------------
# Telemetry configuration
# ---------------------------
REPORT_INTERVAL_SEC = 15.0             # how often to print stats
HISTORY_FRAMES = 1024 * 16                  # window for spike pattern analysis
SPIKE_K_SIGMA_MAD = 6.0                # spike threshold = median + k * (1.4826 * MAD)
SPARKLINE_LEN = 60                     # glyphs per printed sparkline
SPARKLINE_BRAILLE = False              # leave False; Braille gives higher vertical resolution but can look noisy

# ---------------------------
# Telemetry helpers
# ---------------------------
_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
_SPARK_BRAILLE_ROWS = ["⣀","⣄","⣆","⣇","⣧","⣷","⣾","⣿"]

def _bounded(v, lo, hi):
    return lo if v < lo else hi if v > hi else v

def _robust_threshold_ms(values_ms, k=SPIKE_K_SIGMA_MAD):
    if len(values_ms) == 0:
        return (0.0, 0.0, 0.0)
    med = float(np.median(values_ms))
    mad = float(np.median(np.abs(values_ms - med)))
    sigma = 1.4826 * mad
    thr = med + (k * sigma if sigma > 0.0 else 0.25 * med if med > 0.0 else 0.5)
    return (thr, med, sigma)

def _ascii_sparkline(values_ms, length=SPARKLINE_LEN):
    if len(values_ms) == 0:
        return ""
    data = np.asarray(values_ms, dtype=np.float64)
    if len(data) > length:
        # downsample by taking the last 'length' values
        data = data[-length:]
    vmin = float(np.min(data))
    vmax = float(np.max(data))
    if vmax == vmin:
        return _SPARK_BLOCKS[0] * len(data)
    norm = (data - vmin) / (vmax - vmin)
    if SPARKLINE_BRAILLE:
        glyphs = _SPARK_BRAILLE_ROWS
    else:
        glyphs = _SPARK_BLOCKS
    out = []
    nlevels = len(glyphs)
    for x in norm:
        idx = int(round(_bounded(x, 0.0, 1.0) * (nlevels - 1)))
        out.append(glyphs[idx])
    return "".join(out)

def _autocorr_pattern(values_ms):
    """
    Return (best_lag_frames, corr_coeff, approx_period_ms) or (None, 0.0, None).
    Uses normalized autocorrelation over the last HISTORY_FRAMES.
    """
    n = len(values_ms)
    if n < 16:
        return (None, 0.0, None)
    window = min(n, HISTORY_FRAMES)
    x = np.asarray(values_ms[-window:], dtype=np.float64)
    x = x - np.mean(x)
    var = np.dot(x, x)
    if var <= 0.0:
        return (None, 0.0, None)
    acf_full = np.correlate(x, x, mode="full")
    acf = acf_full[acf_full.size // 2:] / var
    # Ignore lag 0; search for strongest peak in [2 .. window//2]
    max_lag = max(2, window // 2)
    if acf.shape[0] <= 2:
        return (None, 0.0, None)
    search = acf[2:max_lag]
    best_rel_idx = int(np.argmax(search))
    best_lag = best_rel_idx + 2
    best_corr = float(search[best_rel_idx])
    median_ft = float(np.median(x + np.mean(values_ms[-window:])))
    approx_period_ms = best_lag * median_ft if median_ft > 0 else None
    # Require a modest correlation to claim a pattern
    if best_corr < 0.1:
        return (None, best_corr, None)
    return (best_lag, best_corr, approx_period_ms)

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
# bridge = BridgeAPI(library_path = r"/home/alec/repo/LookingGlassBridge/build")
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

# Disables monitor state change detection the following things will stop working: closing the window if a looking glass monitor is disconnected, reopening the window when it is reconnected, calibration updates, new monitor connection updates
# This may reduce lag spikes on some os's
bridge.set_window_polling(br_wnd, False)

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
history_times = []
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
    history_times.append(frame_time)
    if len(history_times) > HISTORY_FRAMES:
        history_times = history_times[-HISTORY_FRAMES:]

    if now - last_report_time >= REPORT_INTERVAL_SEC and frame_times:
        ft = np.array(frame_times, dtype=np.float64)
        avg_fps = 1000.0 / (np.mean(ft)) if np.mean(ft) > 0.0 else 0.0
        pcts = np.percentile(ft, [50.0, 90.0, 95.0, 99.0])
        p50, p90, p95, p99 = [float(x) for x in pcts]
        worst = float(np.max(ft))
        thr, med, sigma = _robust_threshold_ms(ft)
        spikes_mask = ft >= thr
        n_spikes = int(np.count_nonzero(spikes_mask))
        max_spike = float(np.max(ft[spikes_mask])) if n_spikes > 0 else 0.0

        lag, corr, approx_ms = _autocorr_pattern(history_times)
        spark = _ascii_sparkline(history_times, SPARKLINE_LEN)
        hmin = float(np.min(history_times)) if len(history_times) else 0.0
        hmax = float(np.max(history_times)) if len(history_times) else 0.0

        print(f"Avg FPS: {avg_fps:.2f} | p50: {p50:.2f} ms, p90: {p90:.2f} ms, p95: {p95:.2f} ms, p99: {p99:.2f} ms | worst: {worst:.2f} ms")
        print(f"Spikes: {n_spikes} in last {len(ft)} frames (threshold: {thr:.2f} ms = median {med:.2f} + {SPIKE_K_SIGMA_MAD:.1f}×σ_MAD {sigma:.2f}); max spike: {max_spike:.2f} ms")
        if lag is not None and approx_ms is not None:
            print(f"Pattern: strongest autocorr lag {lag} frames (period=~{approx_ms:.2f} ms), r={corr:.2f}")
        else:
            print(f"Pattern: no strong periodicity detected (max r={corr:.2f})")
        print(f"Sparkline (last {min(len(history_times), SPARKLINE_LEN)} frames, ms): {spark}  [{hmin:.1f} .. {hmax:.1f}]")

        frame_times.clear()
        last_report_time = now

# Cleanup
GL.glDeleteTextures(1, [tex])
glfw.destroy_window(dummy)
glfw.terminate()