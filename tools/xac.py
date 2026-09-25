# -*- coding: utf-8 -*-
"""X4 ``.xac`` 角色资产读取器：骨架节点、网格几何、蒙皮权重。

格式来自对 vanilla 9.00 资产与 EgoSoft 官方 X4CharacterConverter
（xac_format.py）的交叉验证，这里重写为不依赖 bpy / Blender 的版本，
并把顶点数据直接组织成 numpy 数组，便于实时蒙皮。

文件结构：:

    "XAC " + u8 major + u8 minor + u8 big_endian + u8 pad
    随后是一串 chunk：``u32 type | u32 length | u32 version | body``
      type  7 = metadata（若干字符串）
      type 11 = 节点表（骨架 + 网格节点 + 材质节点）
      type 13 = 材质总数
      type  3 = 单个材质
      type  1 = 网格
      type  2 = 蒙皮（权重）
      type 12 = morph 目标

坐标系：X4 引擎内部为 **Y 轴向上**，单位厘米。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

POSITION_TYPE = 0
NORMAL_TYPE = 1
TANGENT_TYPE = 2
UV_TYPE = 3
INFLUENCE_TYPE = 5


class XacError(RuntimeError):
    pass


class _Reader:
    __slots__ = ("data", "offset")

    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def read(self, size: int) -> bytes:
        end = self.offset + size
        if size < 0 or end > len(self.data):
            raise XacError(f"读取越界：offset={self.offset} size={size} len={len(self.data)}")
        out = self.data[self.offset:end]
        self.offset = end
        return out

    def byte(self) -> int:
        return self.read(1)[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.read(4))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.read(4))[0]

    def string(self) -> str:
        n = self.u32()
        return self.read(n).decode("utf-8", "replace")


@dataclass
class Node:
    """一个场景节点。骨骼与网格节点共用同一张表。"""

    node_id: int
    name: str
    parent_id: int
    position: tuple[float, float, float]
    rotation: tuple[float, float, float, float]  # (x, y, z, w)
    scale: tuple[float, float, float]

    def local_matrix(self) -> np.ndarray:
        """本节点的局部变换矩阵（行主序 4x4，作用于列向量）。"""
        m = np.eye(4, dtype=np.float64)
        x, y, z, w = self.rotation
        n = x * x + y * y + z * z + w * w
        if n < 1e-12:
            rot = np.eye(3)
        else:
            s = 2.0 / n
            xx, yy, zz = x * x * s, y * y * s, z * z * s
            xy, xz, yz = x * y * s, x * z * s, y * z * s
            wx, wy, wz = w * x * s, w * y * s, w * z * s
            rot = np.array(
                [
                    [1.0 - (yy + zz), xy - wz, xz + wy],
                    [xy + wz, 1.0 - (xx + zz), yz - wx],
                    [xz - wy, yz + wx, 1.0 - (xx + yy)],
                ]
            )
        sx, sy, sz = self.scale
        m[:3, :3] = rot * np.array([sx, sy, sz], dtype=np.float64)
        m[:3, 3] = self.position
        return m


@dataclass
class Mesh:
    mesh_id: int
    node_id: int
    collision: int
    positions: np.ndarray  # (V, 3) float32
    normals: np.ndarray | None  # (V, 3) float32
    uvs: np.ndarray | None  # (V, 2) float32
    faces: np.ndarray  # (F, 3) int32，已加上 submesh 的顶点偏移
    submesh_material: list[tuple[int, int, int]]  # (first_face, face_count, material_id)
    bone_ids: np.ndarray | None = None  # (V, K) int32
    bone_weights: np.ndarray | None = None  # (V, K) float32

    @property
    def vertex_count(self) -> int:
        return int(self.positions.shape[0])

    @property
    def face_count(self) -> int:
        return int(self.faces.shape[0])


@dataclass
class Asset:
    path: Path
    nodes: list[Node] = field(default_factory=list)
    meshes: list[Mesh] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    exporter: str = ""
    source_file: str = ""
    export_date: str = ""

    # -- 查询辅助 ---------------------------------------------------------
    def node_index(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for node in self.nodes:
            out.setdefault(_norm_name(node.name), node.node_id)
        return out

    def bones(self) -> list[Node]:
        """骨架节点（被网格权重引用的那些）。"""
        used: set[int] = set()
        for mesh in self.meshes:
            if mesh.bone_ids is not None and mesh.bone_ids.size:
                used.update(int(b) for b in np.unique(mesh.bone_ids) if b >= 0)
        return [self.nodes[i] for i in sorted(used) if i < len(self.nodes)]

    def bind_world(self) -> np.ndarray:
        """每根骨在绑定姿态下的世界矩阵，形状 (N, 4, 4)。

        X4 的节点表已经是按父先于子的顺序排列，所以单趟即可。
        """
        n = len(self.nodes)
        out = np.zeros((n, 4, 4), dtype=np.float64)
        for i, node in enumerate(self.nodes):
            local = node.local_matrix()
            p = node.parent_id
            out[i] = local if p < 0 or p >= n else out[p] @ local
        return out


def _norm_name(name: str) -> str:
    """骨骼名归一化：X4 的 xml / xsm / xac 三处对空格与下划线、大小写不一致。"""
    return name.strip().lower().replace("_", " ").replace(".", " ").strip()


# ---------------------------------------------------------------------------
# chunk 解析
# ---------------------------------------------------------------------------


def _read_metadata(r: _Reader):
    r.read(16)
    strings = [r.string() for _ in range(4)]
    return strings


def _read_nodes(r: _Reader) -> list[Node]:
    count = r.i32()
    r.read(4)
    nodes: list[Node] = []
    for node_id in range(count):
        rotation = struct.unpack("<4f", r.read(16))
        r.read(16)
        position = struct.unpack("<3f", r.read(12))
        scale = struct.unpack("<3f", r.read(12))
        r.read(12)
        r.read(8)
        parent_id = r.i32()
        r.i32()  # child_count
        r.read(4)
        r.read(64)
        r.read(4)
        name = r.string()
        nodes.append(Node(node_id, name, parent_id, position, rotation, scale))
    return nodes


def _read_material(r: _Reader, material_id: int) -> str:
    r.read(80)
    r.read(3)
    layer_count = r.byte()
    name = r.string()
    for _ in range(layer_count):
        r.read(28)
        r.string()
    return name


def _read_mesh(r: _Reader, mesh_id: int) -> Mesh:
    node_id = r.i32()
    range_count = r.i32()
    vertex_count = r.i32()
    index_count = r.i32()
    submesh_count = r.i32()
    attribute_count = r.i32()
    collision = r.byte()
    r.read(3)

    layers: dict[int, bytes] = {}
    for _ in range(attribute_count):
        type_id = r.i32()
        size = r.i32()
        r.read(4)
        layers[type_id] = r.read(vertex_count * size)

    submesh_meta: list[tuple[int, int, int]] = []
    all_faces: list[np.ndarray] = []
    vertex_start = 0
    for _ in range(submesh_count):
        sub_index_count = r.i32()
        sub_vertex_count = r.i32()
        material_id = r.i32()
        bone_count = r.i32()
        indices = np.frombuffer(r.read(sub_index_count * 4), dtype="<i4").astype(np.int32)
        r.read(bone_count * 4)
        if indices.size % 3 == 0:
            tri = indices.reshape(-1, 3) + vertex_start
            submesh_meta.append((int(sum(f.shape[0] for f in all_faces)), tri.shape[0], material_id))
            all_faces.append(tri)
        vertex_start += sub_vertex_count

    pos_raw = layers.get(POSITION_TYPE)
    if pos_raw is None:
        raise XacError(f"网格 {mesh_id} 缺少位置层")
    positions = np.frombuffer(pos_raw, dtype="<f4").reshape(-1, 3).astype(np.float32)

    normals = None
    if NORMAL_TYPE in layers:
        normals = np.frombuffer(layers[NORMAL_TYPE], dtype="<f4").reshape(-1, 3).astype(np.float32)

    uvs = None
    if UV_TYPE in layers:
        uvs = np.frombuffer(layers[UV_TYPE], dtype="<f4").reshape(-1, 2).astype(np.float32)

    faces = np.concatenate(all_faces) if all_faces else np.zeros((0, 3), dtype=np.int32)

    mesh = Mesh(
        mesh_id=mesh_id,
        node_id=node_id,
        collision=collision,
        positions=positions,
        normals=normals,
        uvs=uvs,
        faces=faces,
        submesh_material=submesh_meta,
    )
    mesh._range_ids = (  # type: ignore[attr-defined]
        np.frombuffer(layers[INFLUENCE_TYPE], dtype="<u4").astype(np.int64)
        if INFLUENCE_TYPE in layers
        else None
    )
    mesh._range_count = range_count  # type: ignore[attr-defined]
    return mesh


def _read_skin(r: _Reader, meshes: list[Mesh]):
    node_id = r.i32()
    local_bone_count = r.i32()
    influence_count = r.i32()
    collision = r.byte()
    r.read(3)

    influences = np.frombuffer(r.read(influence_count * 8), dtype=np.dtype([("w", "<f4"), ("b", "<i2"), ("pad", "<i2")]))
    weights = influences["w"].astype(np.float32)
    bones = influences["b"].astype(np.int32)

    target = None
    for mesh in meshes:
        if mesh.node_id == node_id and mesh.collision == collision:
            target = mesh
    if target is None:
        # 没有对应网格的蒙皮块，跳过（range 表仍需读掉）
        return None

    range_count = getattr(target, "_range_count", 0)
    ranges = np.frombuffer(r.read(range_count * 8), dtype="<i4").reshape(-1, 2)

    return target, weights, bones, ranges


def load_xac(path: str | Path, data: bytes | None = None) -> Asset:
    """解析一个 .xac 文件。"""
    path = Path(path)
    raw = data if data is not None else path.read_bytes()
    r = _Reader(raw)

    if r.read(4) != b"XAC ":
        raise XacError(f"不是 XAC 文件：{path}")
    major, minor, big_endian, _ = r.read(4)
    if major != 1 or minor != 0:
        raise XacError(f"不支持的 XAC 版本 {major}.{minor}")
    if big_endian:
        raise XacError("不支持大端 XAC")

    asset = Asset(path=path)
    material_count = None
    skins: list[tuple[Mesh, np.ndarray, np.ndarray, np.ndarray]] = []

    while r.offset < len(raw):
        chunk_type = r.i32()
        length = r.i32()
        r.i32()  # version
        body_start = r.offset
        if length < 0 or body_start + length > len(raw):
            raise XacError(f"chunk 0x{chunk_type:X} 长度非法")

        if chunk_type == 7:
            strings = _read_metadata(r)
            asset.exporter = strings[0] if strings else ""
            asset.source_file = strings[1] if len(strings) > 1 else ""
            asset.export_date = strings[2] if len(strings) > 2 else ""
        elif chunk_type == 11:
            asset.nodes = _read_nodes(r)
        elif chunk_type == 13:
            material_count = r.i32()
            r.read(8)
        elif chunk_type == 3:
            # 注意：材质 chunk 的 length 只覆盖到材质名结束，**不含** layer 数据，
            # 所以这里必须按实际读到的位置推进（实测 len=129 的 chunk 实际有 191 字节）。
            asset.materials.append(_read_material(r, len(asset.materials)))
        elif chunk_type == 1:
            asset.meshes.append(_read_mesh(r, len(asset.meshes)))
        elif chunk_type == 2:
            parsed = _read_skin(r, asset.meshes)
            if parsed is not None:
                skins.append(parsed)
        else:
            pass
        r.offset = max(body_start + length, r.offset)

    if material_count is not None and len(asset.materials) != material_count:
        raise XacError(f"材质数量不符：期望 {material_count}，实得 {len(asset.materials)}")

    # 把权重表摊平成 (V, K) 便于向量化蒙皮
    for mesh, weights, bones, ranges in skins:
        range_ids = getattr(mesh, "_range_ids", None)
        if range_ids is None:
            continue
        v = mesh.vertex_count
        per_vertex: list[np.ndarray] = []
        for rid in range_ids[:v]:
            if rid < 0 or rid >= len(ranges):
                per_vertex.append(np.zeros((0, 2), dtype=np.float32))
                continue
            first, count = int(ranges[rid][0]), int(ranges[rid][1])
            w = weights[first:first + count]
            b = bones[first:first + count]
            keep = w > 1e-6
            per_vertex.append(np.stack([b[keep].astype(np.float32), w[keep]], axis=1))

        kmax = max((p.shape[0] for p in per_vertex), default=0)
        if kmax == 0:
            mesh.bone_ids = np.zeros((v, 1), dtype=np.int32)
            mesh.bone_weights = np.zeros((v, 1), dtype=np.float32)
            continue
        ids = np.zeros((v, kmax), dtype=np.float32)
        vals = np.zeros((v, kmax), dtype=np.float32)
        for i, p in enumerate(per_vertex):
            k = p.shape[0]
            if k:
                ids[i, :k] = p[:, 0]
                vals[i, :k] = p[:, 1]
        mesh.bone_ids = ids.astype(np.int32)
        mesh.bone_weights = vals

    for mesh in asset.meshes:
        for attr in ("_range_ids", "_range_count"):
            if hasattr(mesh, attr):
                delattr(mesh, attr)

    return asset


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        a = load_xac(arg)
        nverts = sum(m.vertex_count for m in a.meshes)
        ntris = sum(m.face_count for m in a.meshes)
        print(f"{arg}: 节点 {len(a.nodes)} 网格 {len(a.meshes)} 材质 {len(a.materials)} 顶点 {nverts} 三角 {ntris}")
        print(f"  exporter={a.exporter!r} source={a.source_file!r} date={a.export_date!r}")
        if a.meshes:
            m = a.meshes[0]
            print(f"  mesh0: {m.vertex_count} 顶点, uv={'有' if m.uvs is not None else '无'}, "
                  f"权重={'有' if m.bone_ids is not None else '无'}")
