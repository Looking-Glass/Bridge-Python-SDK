#!/usr/bin/env python3
import math
import time
import sys
import os
from types import MethodType

# --------------------------------------------------------------------------- #
#  Make our local rendering engine (Render, Mesh, Shader, etc.) importable
# --------------------------------------------------------------------------- #
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import glfw
import numpy as np
from OpenGL import GL

from Rendering.Shader     import Shader
from Rendering.Mesh       import Mesh
from Rendering.Render     import Render
from Rendering.TerrainMesh import TerrainMesh


# --------------------------------------------------------------------------- #
#  GLSL shader sources for terrain (with water fallback)
# --------------------------------------------------------------------------- #
vertex_common = """
#version 330 core
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec2 aUV;
"""

terrain_vs = vertex_common + """
uniform mat4 u_mvp;
uniform mat4 u_model;
out vec3 vPos;
out vec3 vNormal;
out vec2 vUV;
void main()
{
    vec4 worldPos = u_model * vec4(aPos, 1.0);
    mat3 normMat  = mat3(transpose(inverse(u_model)));
    vPos          = worldPos.xyz;
    vNormal       = normalize(normMat * aNormal);
    vUV           = aUV;
    gl_Position   = u_mvp * vec4(aPos, 1.0);
}
"""

terrain_fs = """
#version 330 core
in  vec3 vPos;
in  vec3 vNormal;
in  vec2 vUV;
out vec4 FragColor;
void main()
{
    if(vPos.y < 0.0)
    {
        FragColor = vec4(0.0, 0.3, 0.6, 1.0);
        return;
    }
    float slope = dot(vNormal, vec3(0.0, 1.0, 0.0));
    vec3 grass   = vec3(0.1, 0.8, 0.1);
    vec3 rock    = vec3(0.5, 0.5, 0.5);
    FragColor    = vec4(mix(rock, grass, slope), 1.0);
}
"""


# --------------------------------------------------------------------------- #
#  Terrain parameters
# --------------------------------------------------------------------------- #
TILE_SIZE   = 20.0
GRID_RADIUS = 3
RESOLUTION  = 64


# --------------------------------------------------------------------------- #
#  Island height
# --------------------------------------------------------------------------- #
def island_height(x: float, z: float) -> float:
    max_h = 8.0
    R     = TILE_SIZE * GRID_RADIUS * 0.8
    r     = math.hypot(x, z)
    return -2.0 if r >= R else max_h * (1.0 - (r / R) ** 2)


# --------------------------------------------------------------------------- #
#  Free camera
# --------------------------------------------------------------------------- #
def normalize(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)

def look_at(eye: np.ndarray, center: np.ndarray, up: np.ndarray) -> np.ndarray:
    f = normalize(center - eye)
    s = normalize(np.cross(f, up))
    u = np.cross(s, f)
    M = np.eye(4, dtype=np.float32)
    M[0,:3], M[1,:3], M[2,:3] = s, u, -f
    T = np.eye(4, dtype=np.float32)
    T[:3,3] = -eye
    return M @ T

class FreeCamera:
    def __init__(self, pos=(0,5,20), yaw=-90.0, pitch=-20.0):
        self.position    = np.array(pos, dtype=np.float32)
        self.yaw         = yaw
        self.pitch       = pitch
        self.speed       = 10.0
        self.sensitivity = 0.1
        self.last_mouse  = None

    def direction(self) -> np.ndarray:
        cy = math.radians(self.yaw)
        cp = math.radians(self.pitch)
        return normalize(np.array([
            math.cos(cp)*math.cos(cy),
            math.sin(cp),
            math.cos(cp)*math.sin(cy),
        ], dtype=np.float32))

    def view_matrix(self) -> np.ndarray:
        dir = self.direction()
        return look_at(self.position, self.position + dir, np.array([0,1,0], dtype=np.float32))

    def move(self, forward: float, right: float, dt: float) -> None:
        dir       = self.direction()
        right_vec = normalize(np.cross(dir, [0,1,0]))
        self.position += dir * forward * self.speed * dt
        self.position += right_vec * right * self.speed * dt

    def rotate(self, dx: float, dy: float) -> None:
        self.yaw   += dx * self.sensitivity
        self.pitch = max(-89.0, min(89.0, self.pitch + dy * self.sensitivity))


# --------------------------------------------------------------------------- #
#  Monkey‑patch drawing to upload matrices
# --------------------------------------------------------------------------- #
def _draw_objects_with_model(self, view: np.ndarray, proj: np.ndarray) -> None:
    for obj in self._objects:
        model = obj["model"]
        mvp   = proj @ view @ model
        sh    = obj["shader"]
        sh.use()
        loc_mvp   = GL.glGetUniformLocation(sh.id,   "u_mvp")
        loc_model = GL.glGetUniformLocation(sh.id,   "u_model")
        GL.glUniformMatrix4fv(loc_mvp,   1, GL.GL_TRUE, mvp)
        GL.glUniformMatrix4fv(loc_model, 1, GL.GL_TRUE, model)
        obj["mesh"].draw(GL.GL_TRIANGLES)


# --------------------------------------------------------------------------- #
#  Monkey‑patch render_frame for free camera + LKG
# --------------------------------------------------------------------------- #
def patched_render_frame(self, dt: float = 0.016) -> None:
    glfw.poll_events()
    f = self.window.is_key_pressed(glfw.KEY_W) - self.window.is_key_pressed(glfw.KEY_S)
    r = self.window.is_key_pressed(glfw.KEY_D) - self.window.is_key_pressed(glfw.KEY_A)
    free_cam.move(f, r, dt)

    cv, cp = self.camera.compute_view_projection_matrices(0.5, False, self.offset, self.focus)
    combo_view = cv @ free_cam.view_matrix()

    # primary
    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
    w, h = self.window.framebuffer_size()
    GL.glViewport(0, 0, w, h)
    GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
    self._draw_objects(combo_view, cp)

    # quilt
    if self.bridge_ok:
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.quilt_fbo)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        vw, vh = self.qw // self.cols, self.qh // self.rows
        total  = self.cols * self.rows
        for y in range(self.rows):
            for x in range(self.cols):
                idx = y * self.cols + x
                nrm = idx / (total - 1) if total > 1 else 0.5
                GL.glViewport(x*vw, (self.rows-1-y)*vh, vw, vh)
                vmat, pmat = self.camera.compute_view_projection_matrices(nrm, True, self.offset, self.focus)
                self._draw_objects(vmat @ free_cam.view_matrix(), pmat)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        self.bridge.draw_interop_quilt_texture_gl(
            self.br_wnd, self.quilt_tex,
            GL.GL_RGBA, self.qw, self.qh,
            self.cols, self.rows, self.br_aspect, 1.0
        )

    self.window.swap_buffers()


# --------------------------------------------------------------------------- #
#  Main scene driver
# --------------------------------------------------------------------------- #
def main() -> None:
    global free_cam
    free_cam = FreeCamera()

    renderer = Render(lkg_size=25.0)
    renderer._draw_objects = MethodType(_draw_objects_with_model, renderer)
    renderer.render_frame  = MethodType(patched_render_frame, renderer)

    # left‑button drag to look + capture
    def mouse_btn_cb(win, button, action, mods):
        if button == glfw.MOUSE_BUTTON_LEFT and action == glfw.PRESS:
            renderer.window.disable_cursor()
            free_cam.last_mouse = None
        elif button == glfw.MOUSE_BUTTON_LEFT and action == glfw.RELEASE:
            renderer.window.show_cursor()
            free_cam.last_mouse = None

    def cursor_cb(win, x, y):
        if free_cam.last_mouse is None:
            free_cam.last_mouse = (x, y)
            return
        dx = x - free_cam.last_mouse[0]
        dy = y - free_cam.last_mouse[1]
        free_cam.rotate(dx, -dy)
        free_cam.last_mouse = (x, y)

    renderer.window.set_mouse_button_callback(mouse_btn_cb)
    renderer.window.set_cursor_pos_callback(cursor_cb)

    # position LKG camera
    renderer.camera_distance = GRID_RADIUS * TILE_SIZE * 2.0
    renderer.camera.center   = np.array([0.0, 0.0, 0.0], dtype=np.float32)

    shader  = Shader(terrain_vs, terrain_fs)
    terrain = TerrainMesh(TILE_SIZE, RESOLUTION, island_height)

    stride  = 8 * 4
    attribs = [
        (0, 3, GL.GL_FLOAT, False, stride, 0),
        (1, 3, GL.GL_FLOAT, False, stride, 3*4),
        (2, 2, GL.GL_FLOAT, False, stride, 6*4),
    ]

    for ix in range(-GRID_RADIUS, GRID_RADIUS+1):
        for iz in range(-GRID_RADIUS, GRID_RADIUS+1):
            data = terrain.generate_tile(ix*TILE_SIZE, iz*TILE_SIZE)
            mesh = Mesh(data, attribs)
            renderer.add_object(mesh, shader)

    GL.glEnable(GL.GL_DEPTH_TEST)

    last = time.time()
    while not renderer.should_close():
        now = time.time()
        dt  = now - last
        last = now
        renderer.render_frame(dt)

    renderer.close()


if __name__ == '__main__':
    main()
