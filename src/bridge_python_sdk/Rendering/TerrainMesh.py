#!/usr/bin/env python3
# TerrainMesh.py – tiled terrain mesh generator

import math
from typing import Callable, List

import numpy as np

class TerrainMesh:
    """
    Generates interleaved vertex buffers for tiled terrain meshes.
    Each vertex consists of position (x,y,z), normal (nx,ny,nz), and UV (u,v).
    """

    def __init__(
        self,
        tile_size: float,
        resolution: int,
        height_func: Callable[[float, float], float]
    ) -> None:
        self.tile_size   = tile_size
        self.resolution  = resolution
        self.height_func = height_func
        self.epsilon     = tile_size / resolution

    def generate_tile(self, x_offset: float, z_offset: float) -> np.ndarray:
        """
        Generate one terrain tile at world offset (x_offset, z_offset).
        Returns a flat numpy array of float32 with stride = 8 floats per vertex.
        """
        verts: List[List[float]] = []
        for i in range(self.resolution + 1):
            for j in range(self.resolution + 1):
                x = x_offset + (i / self.resolution) * self.tile_size
                z = z_offset + (j / self.resolution) * self.tile_size
                y = self.height_func(x, z)

                # approximate normal via central differences
                hL = self.height_func(x - self.epsilon, z)
                hR = self.height_func(x + self.epsilon, z)
                hD = self.height_func(x, z - self.epsilon)
                hU = self.height_func(x, z + self.epsilon)
                nx = hL - hR
                ny = 2.0 * self.epsilon
                nz = hD - hU
                length = math.sqrt(nx*nx + ny*ny + nz*nz)
                nx /= length; ny /= length; nz /= length

                u = i / self.resolution
                v = j / self.resolution

                verts.append([x, y, z, nx, ny, nz, u, v])

        # build triangle stream
        stream: List[float] = []
        row = self.resolution + 1
        for i in range(self.resolution):
            for j in range(self.resolution):
                a = i * row + j
                b = a + 1
                c = (i + 1) * row + j
                d = c + 1

                stream.extend(verts[a]); stream.extend(verts[c]); stream.extend(verts[b])
                stream.extend(verts[b]); stream.extend(verts[c]); stream.extend(verts[d])

        return np.asarray(stream, dtype=np.float32)
