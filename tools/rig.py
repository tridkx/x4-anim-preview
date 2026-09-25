# -*- coding: utf-8 -*-
"""把 `.xac` 的骨架 + 网格和 `.xsm` 的动画合起来：正向运动学 + 线性混合蒙皮。

坐标与绑定姿态全部沿用 `.xac`（网格就是按它的绑定姿态蒙皮的）；
`.xsm` 只提供**相对绑定姿态的增量**：

`.xsm` 的旋转轨道存的就是**骨骼的绝对局部旋转**（实测第 0 帧与 `.xac` 的绑定
旋转一致），所以默认直接采用（``delta=False``）：::

    旋转  local_rot = anim_quat
    位移  local_pos = bind_pos_xac + (anim_pos - bind_pos_xsm)   # 仅根骨有位置轨道

``delta=True`` 会改成"相对 `.xsm` 记录头里绑定姿态的增量"，本意是抹平动画文件与
网格资产的版本差，但实测它会让不同资产对同一条动画产生不同姿态，**不要当默认**。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from xac import Asset, Mesh
from xsm import NodeAnim, Xsm, _norm


def quat_to_mat3(q) -> np.ndarray:
    x, y, z, w = (float(v) for v in q)
    n = x * x + y * y + z * z + w * w
    if n < 1e-12:
        return np.eye(3)
    s = 2.0 / n
    xx, yy, zz = x * x * s, y * y * s, z * z * s
    xy, xz, yz = x * y * s, x * z * s, y * z * s
    wx, wy, wz = w * x * s, w * y * s, w * z * s
    return np.array(
        [
            [1.0 - (yy + zz), xy - wz, xz + wy],
            [xy + wz, 1.0 - (xx + zz), yz - wx],
            [xz - wy, yz + wx, 1.0 - (xx + yy)],
        ]
    )


def quat_mul(a, b) -> np.ndarray:
    ax, ay, az, aw = (float(v) for v in a)
    bx, by, bz, bw = (float(v) for v in b)
    return np.array(
        [
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz,
        ]
    )


def quat_inv(q) -> np.ndarray:
    x, y, z, w = (float(v) for v in q)
    n = x * x + y * y + z * z + w * w
    if n < 1e-12:
        return np.array([0.0, 0.0, 0.0, 1.0])
    return np.array([-x / n, -y / n, -z / n, w / n])


@dataclass
class Rig:
    """一个可摆姿态的骨架 + 挂在它下面的网格。"""

    asset: Asset
    anim: Xsm | None = None
    delta: bool = False

    bone_ids: list[int] = field(default_factory=list)
    parents: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int32))
    bind_local: np.ndarray = field(default_factory=lambda: np.zeros((0, 4, 4)))
    bind_world: np.ndarray = field(default_factory=lambda: np.zeros((0, 4, 4)))
    bind_world_inv: np.ndarray = field(default_factory=lambda: np.zeros((0, 4, 4)))
    anim_bind_quat: list[np.ndarray | None] = field(default_factory=list)
    anim_bind_pos: list[np.ndarray | None] = field(default_factory=list)
    anim_nodes: list[NodeAnim | None] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    node_to_local: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int32))

    # -- 构建 -------------------------------------------------------------
    @classmethod
    def build(cls, asset: Asset, anim: Xsm | None = None, delta: bool = False) -> "Rig":
        # 需要参与 FK 的节点 = 所有网格权重引用到的骨 + 它们的全部祖先
        needed: set[int] = set()
        for mesh in asset.meshes:
            if mesh.bone_ids is not None and mesh.bone_ids.size:
                needed.update(int(b) for b in np.unique(mesh.bone_ids) if b >= 0)
        # 再补上"任何有动画的节点"——有些骨不参与蒙皮但会影响子骨（比如裙摆链的父节点）
        if anim is not None:
            index = asset.node_index()
            for node in anim.nodes:
                nid = index.get(_norm(node.name))
                if nid is not None:
                    needed.add(nid)
        stack = list(needed)
        while stack:
            nid = stack.pop()
            if 0 <= nid < len(asset.nodes):
                parent = asset.nodes[nid].parent_id
                if parent >= 0 and parent not in needed:
                    needed.add(parent)
                    stack.append(parent)

        bone_ids = sorted(needed)
        index_of = {nid: i for i, nid in enumerate(bone_ids)}
        n = len(bone_ids)

        parents = np.full(n, -1, dtype=np.int32)
        bind_local = np.zeros((n, 4, 4))
        for i, nid in enumerate(bone_ids):
            node = asset.nodes[nid]
            bind_local[i] = node.local_matrix()
            if node.parent_id in index_of:
                parents[i] = index_of[node.parent_id]

        bind_world = np.zeros((n, 4, 4))
        for i in range(n):  # 节点表已按父先于子排列
            p = parents[i]
            bind_world[i] = bind_local[i] if p < 0 else bind_world[p] @ bind_local[i]

        anim_nodes: list[NodeAnim | None] = []
        anim_bind_quat: list[np.ndarray | None] = []
        anim_bind_pos: list[np.ndarray | None] = []
        missing: list[str] = []
        for i, nid in enumerate(bone_ids):
            name = asset.nodes[nid].name
            node_anim = anim.get(name) if anim is not None else None
            anim_nodes.append(node_anim)
            if node_anim is None:
                anim_bind_quat.append(None)
                anim_bind_pos.append(None)
                if anim is not None:
                    missing.append(name)
            else:
                anim_bind_quat.append(node_anim.bind_quat)
                anim_bind_pos.append(node_anim.bind_pos)

        node_to_local = np.full(len(asset.nodes), -1, dtype=np.int32)
        for i, nid in enumerate(bone_ids):
            if 0 <= nid < len(node_to_local):
                node_to_local[nid] = i

        return cls(
            asset=asset,
            anim=anim,
            delta=delta,
            bone_ids=bone_ids,
            parents=parents,
            bind_local=bind_local,
            bind_world=bind_world,
            bind_world_inv=np.linalg.inv(bind_world),
            anim_nodes=anim_nodes,
            anim_bind_quat=anim_bind_quat,
            anim_bind_pos=anim_bind_pos,
            missing=missing,
            node_to_local=node_to_local,
        )

    # -- 查询 -------------------------------------------------------------
    def __len__(self):
        return len(self.bone_ids)

    @property
    def duration(self) -> float:
        return self.anim.duration if self.anim is not None else 0.0

    def bone_names(self):
        return [self.asset.nodes[nid].name for nid in self.bone_ids]

    def matched_fraction(self) -> float:
        """动画节点中有多少能对上骨架（归一化名字匹配）。"""
        if self.anim is None or len(self) == 0:
            return 0.0
        return 1.0 - len(self.missing) / len(self)

    # -- 姿态 -------------------------------------------------------------
    def local_matrices(self, t: float) -> np.ndarray:
        local = self.bind_local.copy()
        if self.anim is None:
            return local
        for i, node_anim in enumerate(self.anim_nodes):
            if node_anim is None:
                continue
            node = self.asset.nodes[self.bone_ids[i]]
            bind_q_xac = node.rotation
            if node_anim.rot is not None:
                q_anim = node_anim.rot.sample_quat(t)
                if self.delta and node_anim.bind_quat is not None:
                    q = quat_mul(quat_mul(q_anim, quat_inv(node_anim.bind_quat)), bind_q_xac)
                else:
                    q = q_anim
                local[i, :3, :3] = quat_to_mat3(q) * np.asarray(node.scale, dtype=np.float64)
            if node_anim.pos is not None:
                p = node_anim.pos.sample_pos(t)
                if self.delta and node_anim.bind_pos is not None:
                    p = np.asarray(node.position, dtype=np.float64) + (p - node_anim.bind_pos)
                local[i, :3, 3] = p
        return local

    def world_matrices(self, t: float) -> np.ndarray:
        local = self.local_matrices(t)
        n = len(self.bone_ids)
        world = np.zeros((n, 4, 4))
        for i in range(n):
            p = self.parents[i]
            world[i] = local[i] if p < 0 else world[p] @ local[i]
        return world

    def skin_matrices(self, t: float) -> np.ndarray:
        """蒙皮矩阵 = 动画世界矩阵 x 绑定世界矩阵的逆。"""
        return self.world_matrices(t) @ self.bind_world_inv

    # -- 蒙皮 -------------------------------------------------------------
    def skin_positions(self, mesh: Mesh, matrices: np.ndarray) -> np.ndarray:
        return self._skin(mesh, mesh.positions, matrices)

    def skin_normals(self, mesh: Mesh, matrices: np.ndarray) -> np.ndarray | None:
        if mesh.normals is None:
            return None
        return self._skin(mesh, mesh.normals, matrices, normals=True)

    def _skin(self, mesh: Mesh, values: np.ndarray, matrices: np.ndarray, normals: bool = False):
        if mesh.bone_ids is None or mesh.bone_weights is None:
            return values.copy()
        # 权重里存的是 .xac 的**全局节点 id**，要映射成本 rig 的骨索引
        v = values.shape[0]
        k = mesh.bone_ids.shape[1]
        out = np.zeros((v, 3), dtype=np.float64)
        total = np.zeros(v, dtype=np.float64)
        homo = np.concatenate([values.astype(np.float64), np.ones((v, 1))], axis=1)
        for slot in range(k):
            raw_ids = mesh.bone_ids[:, slot]
            w = mesh.bone_weights[:, slot].astype(np.float64)
            valid = (w > 1e-6) & (raw_ids >= 0) & (raw_ids < len(self.node_to_local))
            if not valid.any():
                continue
            rows = np.nonzero(valid)[0]
            local = self.node_to_local[raw_ids[rows]]
            keep = local >= 0
            if not keep.all():
                rows = rows[keep]
                local = local[keep]
            if rows.size == 0:
                continue
            m = matrices[local]
            if normals:
                p = np.einsum("vij,vj->vi", m[:, :3, :3], homo[rows, :3])
                n = np.linalg.norm(p, axis=1, keepdims=True)
                p = np.divide(p, np.where(n > 1e-9, n, 1.0))
            else:
                p = np.einsum("vij,vj->vi", m, homo[rows])[:, :3]
            out[rows] += p * w[rows, None]
            total[rows] += w[rows]
        bad = total < 1e-6
        if bad.any():
            out[bad] = values[bad]
            total[bad] = 1.0
        return out / total[:, None]

    def pose_all(self, t: float):
        """返回 [(mesh, 顶点位置, 法线), ...]。"""
        matrices = self.skin_matrices(t)
        out = []
        for mesh in self.asset.meshes:
            if mesh.vertex_count == 0:
                continue
            out.append((mesh, self.skin_positions(mesh, matrices), self.skin_normals(mesh, matrices)))
        return out
