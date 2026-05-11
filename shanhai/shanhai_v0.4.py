#!/usr/bin/env python3
"""
山海 v0.4 — 物质竞争 · 融合纪元
================================
v0.4 核心突破：物质不再是静态结构，而是消耗能量、争夺能量、融合演化的活体。

  维持消耗 — 每个物质每tick从周围3×3吸取能量维持自身
  能量争夺 — 相邻物质间，能量差>100时低能向高能流失
  融  合   — 低能物质耗尽后被高能吸收，形状合并重组
  粒子建造 — 保留 v0.3 暗约束+暗骨架+相变，粒子只管注入

用法: python shanhai_v0.4.py
      python shanhai_v0.4.py --no-viz --target 5
"""

import numpy as np
import time
import sys
import os
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set
from enum import Enum

# ============================================================================
# 常量
# ============================================================================
GRID_SIZE = 100
INIT_ENERGY = 50.0
INIT_NOISE = 10.0
BACKGROUND = 50.0
DIFFUSE_RATE = 0.02
DECAY_RATE = 0.003
RAISE_AMOUNT = 15.0
LOWER_AMOUNT = 10.0
SCAN_INTERVAL = 100
STABILITY_WINDOW = 100         # 时序验证窗口（加速诞生）
STABILITY_THRESHOLD = 0.20
PERTURB_AMPLITUDE = 5.0
PERTURB_RECOVERY = 50
HOTSPOT_SIGMA = 2.0
MIN_HOTSPOT_SUM = 550.0
MOVE_PROB = 0.35
RAISE_PROB = 0.45
LOWER_PROB = 0.10
SIGN_PROB = 0.10
SUBSTEPS_PER_TICK = 3
SUB_RAISE_AMOUNT = 12.0         # 注入（平衡竞争消耗）

# 物质竞争
MAINTENANCE_COST = 0.008       # 每tick消耗：体积×维持率×当前能量
COMPETITION_RATE = 0.05        # 能量争夺：差额的5%流向高能方
COMPETITION_THRESHOLD = 100.0  # 能量差阈值（超过才争夺）
FUSION_THRESHOLD = 300.0       # 融合阈值：低能方总能量<此值被融合
MERGE_DISTANCE = 4             # 融合/争夺检测距离（增大促进融合）
COMPETITION_INTERVAL = 10      # 竞争/融合运行间隔（降频，避免O(n²)爆炸）
MAX_FIELD_ENERGY = 1000.0       # 能量场上限（防溢出）

# 暗约束
DARK_REVEAL_CHANCE = 0.02

# 暗骨架
RESIDUE_PER_ACTION = 0.02
RESIDUE_DECAY = 0.001
SKELETON_SCAN_INTERVAL = 500
SKELETON_NODES_MAX = 20

# 物质相变
PHASE_SOLID_ENERGY = 600
PHASE_LIQUID_ENERGY = 1200
PHASE_DRIFT_RATE = 0.1
PHASE_PLASMA_SPLIT = 3

DARK_RULE_DEFS = [
    ("高压区惩罚", "high_energy", 0.12, 2.5),
    ("低压区惩罚", "low_energy", 0.10, 1.5),
    ("签名领地税", "near_signed", 0.15, 3.0),
    ("陡峭梯度过路费", "gradient_steep", 0.09, 2.0),
    ("密集区拥挤费", "cluster_dense", 0.11, 2.5),
    ("边疆开拓税", "edge_zone", 0.07, 1.5),
    ("中心区繁华税", "center_zone", 0.14, 3.5),
    ("角落荒地惩罚", "corner_zone", 0.06, 1.0),
    ("对角线通行费", "diagonal_zone", 0.10, 2.0),
    ("偶数格点税", "even_position", 0.04, 0.8),
    ("奇数格点税", "odd_position", 0.04, 0.8),
    ("远方征途惩罚", "far_from_center", 0.12, 2.5),
]


class PhaseState(Enum):
    SOLID = "固态"
    LIQUID = "液态"
    PLASMA = "等离子态"


TYPE_ROUND = "原生圆"
TYPE_ELONGATED = "椭圆"
TYPE_IRREGULAR = "不规则"

CMAP = "inferno"
COLOR_PHASE = {PhaseState.SOLID: "#00ff88", PhaseState.LIQUID: "#4488ff",
               PhaseState.PLASMA: "#ff44ff"}
COLOR_PARTICLE = "#ffffff"


# ============================================================================
# XORShift32
# ============================================================================
class XORShift32:
    def __init__(self, seed=42):
        self.state = max(seed & 0xFFFFFFFF, 1)
    def next(self):
        x = self.state
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self.state = x
        return x
    def randint(self, lo, hi):
        return lo + (self.next() % (hi - lo))
    def random(self):
        return self.next() / 0x100000000


# ============================================================================
# 物质结构（v0.4：竞争+融合）
# ============================================================================
@dataclass
class Substance:
    uid: int
    cx: int
    cy: int
    matrix: np.ndarray
    birth_tick: int
    dissolve_tick: int = -1
    signature: str = ""
    signed_tick: int = -1
    decay_mult: float = 1.0
    structure_type: str = ""
    circularity: float = 0.0
    phase: PhaseState = PhaseState.SOLID
    phase_transition_tick: int = -1
    drift_vx: float = 0.0
    drift_vy: float = 0.0
    plasma_fragments: List[int] = field(default_factory=list)
    # v0.4 竞争
    energy_history: deque = field(default_factory=lambda: deque(maxlen=100))
    fused_from: List[int] = field(default_factory=list)  # 融合来源
    fusion_count: int = 0                                 # 融合次数

    @property
    def alive(self):
        return self.dissolve_tick < 0

    @property
    def is_signed(self):
        return self.signature != ""

    @property
    def total_energy(self):
        return float(np.sum(self.matrix))

    @property
    def age(self):
        return -1  # set externally

    def update_phase(self, tick):
        e = self.total_energy
        old = self.phase
        if e > PHASE_LIQUID_ENERGY:
            self.phase = PhaseState.PLASMA
        elif e > PHASE_SOLID_ENERGY:
            self.phase = PhaseState.LIQUID
        else:
            self.phase = PhaseState.SOLID
        if old != self.phase:
            self.phase_transition_tick = tick

    def sign(self, particle_id, tick, current_matrix=None):
        self.signature = particle_id
        self.signed_tick = tick
        self.decay_mult = 0.0
        if current_matrix is not None:
            self.matrix = current_matrix.copy()

    def to_dict(self):
        return {
            "uid": self.uid,
            "position": [int(self.cx), int(self.cy)],
            "birth_tick": self.birth_tick,
            "signed_tick": self.signed_tick,
            "phase": self.phase.value,
            "structure_type": self.structure_type,
            "circularity": round(self.circularity, 4),
            "energy_sum": round(self.total_energy, 1),
            "fusion_count": self.fusion_count,
            "fused_from": self.fused_from,
            "matrix": [[round(float(v), 1) for v in row] for row in self.matrix],
        }


# ============================================================================
# 形状分析
# ============================================================================
class ShapeAnalyzer:
    @staticmethod
    def analyze(matrix):
        thresh = np.mean(matrix)
        binary = (matrix > thresh).astype(int)
        area = int(np.sum(binary))
        if area == 0 or area == 9:
            return TYPE_IRREGULAR, 0.0
        perimeter = 0
        rows, cols = binary.shape
        for y in range(rows):
            for x in range(cols):
                if binary[y, x] == 0:
                    continue
                for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                    ny, nx = y + dy, x + dx
                    if ny < 0 or ny >= rows or nx < 0 or nx >= cols:
                        perimeter += 1
                    elif binary[ny, nx] == 0:
                        perimeter += 1
        if perimeter == 0:
            return TYPE_IRREGULAR, 0.0
        c = (4.0 * np.pi * area) / (perimeter * perimeter)
        if c > 0.75:
            return TYPE_ROUND, c
        elif c > 0.50:
            return TYPE_ELONGATED, c
        return TYPE_IRREGULAR, c


# ============================================================================
# 暗约束
# ============================================================================
@dataclass
class DarkRule:
    rid: int
    name: str
    condition: str
    trigger_prob: float
    penalty: float
    hit_count: int = 0

    def check(self, px, py, field, energy_mean):
        v = field[py, px]
        if self.condition == "high_energy" and v > energy_mean * 1.8:
            return True
        elif self.condition == "low_energy" and v < energy_mean * 0.6:
            return True
        elif self.condition == "edge_zone":
            return (px < 10 or px > 89 or py < 10 or py > 89)
        elif self.condition == "center_zone":
            return (30 < px < 70 and 30 < py < 70)
        elif self.condition == "corner_zone":
            return ((px < 20 or px > 79) and (py < 20 or py > 79))
        elif self.condition == "diagonal_zone":
            return abs(px - py) < 15
        elif self.condition == "even_position":
            return (px + py) % 2 == 0
        elif self.condition == "odd_position":
            return (px + py) % 2 == 1
        elif self.condition == "far_from_center":
            return ((px - 50) ** 2 + (py - 50) ** 2) > 1600
        return False


class DarkConstraintSystem:
    def __init__(self, seed=999):
        self.rng = XORShift32(seed)
        self.rules = [DarkRule(rid=i, name=n, condition=c, trigger_prob=p, penalty=pe)
                      for i, (n, c, p, pe) in enumerate(DARK_RULE_DEFS)]
        self.total_triggers = 0
        self.revealed = []

    def apply(self, px, py, field, energy_mean, in_signed, grad_steep, cluster_dense):
        total = 0.0
        triggered = []
        for rule in self.rules:
            if rule.condition == "near_signed" and not in_signed:
                continue
            if rule.condition == "gradient_steep" and not grad_steep:
                continue
            if rule.condition == "cluster_dense" and not cluster_dense:
                continue
            if rule.condition not in ("near_signed", "gradient_steep", "cluster_dense"):
                if not rule.check(px, py, field, energy_mean):
                    continue
            if self.rng.random() < rule.trigger_prob:
                total += rule.penalty
                rule.hit_count += 1
                triggered.append(rule.rid)
        self.total_triggers += len(triggered)
        return total, triggered

    def try_reveal(self):
        if self.rng.random() < DARK_REVEAL_CHANCE:
            unrevealed = [r for r in self.rules if r.rid not in self.revealed]
            if unrevealed:
                rule = unrevealed[self.rng.randint(0, len(unrevealed))]
                self.revealed.append(rule.rid)
                return rule
        return None


# ============================================================================
# 暗骨架
# ============================================================================
class DarkSkeleton:
    def __init__(self, grid_size):
        self.grid_size = grid_size
        self.residue = np.zeros((grid_size, grid_size), dtype=np.float64)
        self.nodes = []
        self.edges = []

    def deposit(self, x, y):
        self.residue[y, x] += RESIDUE_PER_ACTION

    def decay(self):
        self.residue -= RESIDUE_DECAY
        self.residue = np.maximum(self.residue, 0)

    def scan_skeleton(self, field):
        self.nodes.clear()
        self.edges.clear()
        rgx = np.abs(np.diff(self.residue, axis=1, append=self.residue[:, -1:]))
        rgy = np.abs(np.diff(self.residue, axis=0, append=self.residue[-1:, :]))
        egx = np.abs(np.diff(field, axis=1, append=field[:, -1:]))
        egy = np.abs(np.diff(field, axis=0, append=field[-1:, :]))
        combined = (rgx + rgy) * 0.3 + (egx + egy) * 0.7
        threshold = np.percentile(combined, 95)
        candidates = np.argwhere(combined > threshold)
        if len(candidates) > SKELETON_NODES_MAX:
            scores = combined[combined > threshold]
            idx = np.argsort(scores)[-SKELETON_NODES_MAX:]
            candidates = candidates[idx]
        for cy, cx in candidates:
            self.nodes.append((int(cx), int(cy)))
        for i, (x1, y1) in enumerate(self.nodes):
            for x2, y2 in self.nodes[i + 1:]:
                if np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2) < 15:
                    self.edges.append((x1, y1, x2, y2))
        return len(self.nodes)

    def residue_attraction(self, x, y, field):
        best_x, best_y = x, y
        best = self.residue[y, x]
        for dy in range(-1, 2):
            for dx in range(-1, 2):
                if dx == 0 and dy == 0:
                    continue
                nx = (x + dx) % self.grid_size
                ny = (y + dy) % self.grid_size
                score = self.residue[ny, nx] + 0.3 * field[ny, nx]
                if score > best:
                    best = score
                    best_x, best_y = nx, ny
        return best_x, best_y


# ============================================================================
# 物质检测器（v0.4：竞争+融合）
# ============================================================================
@dataclass
class HotspotTracker:
    cx: int
    cy: int
    history: deque = field(default_factory=lambda: deque(maxlen=STABILITY_WINDOW))
    state: str = "tracking"
    detected_tick: int = 0
    stable_mean: float = 0.0
    pre_test_matrix: Optional[np.ndarray] = None
    test_start_tick: int = 0


class SubstanceDetector:
    def __init__(self, grid_size):
        self.grid_size = grid_size
        self.trackers: Dict[Tuple[int, int], HotspotTracker] = {}
        self.substances: List[Substance] = []
        self.next_uid = 1
        self._pending_confirms: List[Substance] = []
        # v0.4 竞争统计
        self.total_fusions = 0
        self.total_energy_transferred = 0.0
        self.total_maintenance_consumed = 0.0

    # ---- 热点/稳定性/抗干扰（与v0.3相同） ----
    def scan_hotspots(self, field, tick):
        sums = np.zeros((self.grid_size - 2, self.grid_size - 2))
        for dy in range(3):
            for dx in range(3):
                sums += field[dy:dy + self.grid_size - 2, dx:dx + self.grid_size - 2]
        threshold = float(np.mean(field)) + HOTSPOT_SIGMA * float(np.std(field))
        hot_y, hot_x = np.where((sums > threshold) & (sums > MIN_HOTSPOT_SUM))
        current = set()
        for i in range(len(hot_y)):
            cy, cx = int(hot_y[i]), int(hot_x[i])
            current.add((cx, cy))
            e_sum = float(sums[cy, cx])
            if (cx, cy) not in self.trackers:
                t = HotspotTracker(cx=cx, cy=cy, detected_tick=tick)
                t.history = deque([e_sum], maxlen=STABILITY_WINDOW)
                self.trackers[(cx, cy)] = t
            else:
                t = self.trackers[(cx, cy)]
                if t.state not in ("testing", "confirmed", "signed"):
                    t.history.append(e_sum)
        dead = [k for k, t in self.trackers.items()
                if k not in current and t.state not in ("confirmed", "signed")]
        for k in dead:
            del self.trackers[k]

    def check_stability(self, tick):
        for key, trk in list(self.trackers.items()):
            if trk.state != "tracking" or len(trk.history) < STABILITY_WINDOW:
                continue
            recent = list(trk.history)[-50:]  # 最近50样本（半窗口）
            m = np.mean(recent)
            if m < 1e-6:
                continue
            if (np.max(recent) - np.min(recent)) / m < STABILITY_THRESHOLD:
                trk.state = "stable_candidate"
                trk.stable_mean = m

    def run_interference_tests(self, field, tick):
        for key, trk in list(self.trackers.items()):
            if trk.state != "stable_candidate":
                continue
            trk.state = "testing"
            trk.test_start_tick = tick
            trk.pre_test_matrix = field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3].copy()
            p = np.random.RandomState(tick).uniform(-PERTURB_AMPLITUDE, PERTURB_AMPLITUDE, (3, 3))
            field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3] += p
            field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3] = np.maximum(
                field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3], 0)

    def check_recovery(self, field, tick):
        for key, trk in list(self.trackers.items()):
            if trk.state != "testing":
                continue
            if tick - trk.test_start_tick < PERTURB_RECOVERY:
                continue
            pre = float(np.sum(trk.pre_test_matrix))
            cur = float(np.sum(field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3]))
            if pre < 1e-6:
                trk.state = "tracking"
                continue
            if abs(cur - pre) / pre < STABILITY_THRESHOLD:
                matrix = field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3].copy()
                st, circ = ShapeAnalyzer.analyze(matrix)
                sub = Substance(uid=self.next_uid, cx=trk.cx + 1, cy=trk.cy + 1,
                                matrix=matrix, birth_tick=tick,
                                structure_type=st, circularity=circ)
                self.next_uid += 1
                self.substances.append(sub)
                self._pending_confirms.append(sub)
                trk.state = "confirmed"
            else:
                trk.state = "tracking"
                trk.history.clear()

    # ---- v0.4 核心：物质竞争 ----

    def _substance_region(self, sub):
        """返回物质在field中的3×3切片（可变视图）"""
        return slice(sub.cy - 1, sub.cy + 2), slice(sub.cx - 1, sub.cx + 2)

    def _distance(self, a, b):
        return np.sqrt((a.cx - b.cx) ** 2 + (a.cy - b.cy) ** 2)

    def apply_maintenance(self, field, tick):
        """
        维持消耗：每个物质每tick从自身3×3区域消耗能量。
        签名物质消耗减半。
        """
        total_consumed = 0.0
        for sub in self.substances:
            if not sub.alive:
                continue
            region = field[self._substance_region(sub)]
            total_e = float(np.sum(region))
            # 消耗 = 体积 × 维持率 × 当前能量
            cost = 9 * MAINTENANCE_COST * (total_e / 500.0)  # 归一化
            if sub.is_signed:
                cost *= 0.5

            # 从区域中均匀扣除
            if total_e > cost:
                reduction = cost * (region / max(total_e, 1e-6))
                field[self._substance_region(sub)] -= reduction
                field[self._substance_region(sub)] = np.maximum(
                    field[self._substance_region(sub)], 0)
                total_consumed += cost

        self.total_maintenance_consumed += total_consumed

    def apply_competition(self, field, tick):
        """
        能量争夺：近邻物质间，能量差>阈值时低能向高能流失。
        优化：只用空间哈希，只检查距离<=3的物质对。
        """
        total_transferred = 0.0
        alive = [s for s in self.substances if s.alive]
        if len(alive) < 2:
            return

        # 空间哈希：按5×5格分组
        buckets = {}
        for s in alive:
            bx, by = s.cx // 5, s.cy // 5
            key = (bx, by)
            if key not in buckets:
                buckets[key] = []
            buckets[key].append(s)

        checked = set()
        for (bx, by), bucket in buckets.items():
            # 检查本桶和相邻桶
            for dbx in range(-1, 2):
                for dby in range(-1, 2):
                    nkey = (bx + dbx, by + dby)
                    if nkey not in buckets:
                        continue
                    for a in bucket:
                        for b in buckets[nkey]:
                            if a.uid >= b.uid:
                                continue
                            pair = (a.uid, b.uid)
                            if pair in checked:
                                continue
                            checked.add(pair)

                            dist = self._distance(a, b)
                            if dist > MERGE_DISTANCE + 2:
                                continue

                            e_a = float(np.sum(field[self._substance_region(a)]))
                            e_b = float(np.sum(field[self._substance_region(b)]))
                            diff = abs(e_a - e_b)
                            if diff < COMPETITION_THRESHOLD:
                                continue

                            transfer = diff * COMPETITION_RATE
                            if e_a > e_b:
                                region_b = field[self._substance_region(b)]
                                total_b = float(np.sum(region_b))
                                if total_b > transfer:
                                    drain = transfer * (region_b / max(total_b, 1e-6))
                                    field[self._substance_region(b)] -= drain
                                    field[self._substance_region(b)] = np.maximum(
                                        field[self._substance_region(b)], 0)
                                    field[self._substance_region(a)] += drain
                                    total_transferred += transfer
                            else:
                                region_a = field[self._substance_region(a)]
                                total_a = float(np.sum(region_a))
                                if total_a > transfer:
                                    drain = transfer * (region_a / max(total_a, 1e-6))
                                    field[self._substance_region(a)] -= drain
                                    field[self._substance_region(a)] = np.maximum(
                                        field[self._substance_region(a)], 0)
                                    field[self._substance_region(b)] += drain
                                    total_transferred += transfer

        self.total_energy_transferred += total_transferred

    def apply_fusion(self, field, tick):
        """
        融合：低能物质能量<阈值时被最近的邻居吸收。
        胜者形状重组，继承败者的部分特征。
        """
        alive = [s for s in self.substances if s.alive]

        for sub in list(alive):
            if not sub.alive:
                continue
            e_sub = float(np.sum(field[self._substance_region(sub)]))
            if e_sub >= FUSION_THRESHOLD:
                continue

            # 找最近的邻居
            neighbors = [(self._distance(sub, n), n) for n in alive
                         if n.uid != sub.uid and n.alive
                         and self._distance(sub, n) <= MERGE_DISTANCE + 2]
            if not neighbors:
                continue

            neighbors.sort(key=lambda x: x[0])
            _, winner = neighbors[0]

            # 融合：败者能量归胜者
            loser_region = field[self._substance_region(sub)]
            winner_region = field[self._substance_region(winner)]

            # 胜者吸收了败者的能量矩阵
            field[self._substance_region(winner)] += loser_region * 0.7
            field[self._substance_region(sub)] = BACKGROUND  # 败者清零

            # 胜者更新
            new_matrix = field[self._substance_region(winner)].copy()
            st, circ = ShapeAnalyzer.analyze(new_matrix)
            winner.matrix = new_matrix
            winner.structure_type = st
            winner.circularity = circ
            winner.fusion_count += 1
            winner.fused_from.append(sub.uid)

            # 败者溶解
            sub.dissolve_tick = tick
            self.total_fusions += 1

            self._pending_confirms.append(winner)

    def check_dissolution(self, field, tick):
        for sub in self.substances:
            if not sub.alive:
                continue
            cur = float(np.sum(field[self._substance_region(sub)]))
            birth = float(np.sum(sub.matrix))
            if birth < 1e-6:
                continue
            threshold = 0.50 if sub.is_signed else STABILITY_THRESHOLD * 2
            if cur < birth * (1 - threshold):
                sub.dissolve_tick = tick

    def update_phases(self, tick):
        plasma_list = []
        for sub in self.substances:
            if not sub.is_signed or not sub.alive:
                continue
            sub.update_phase(tick)
            if sub.phase == PhaseState.PLASMA and not sub.plasma_fragments:
                plasma_list.append(sub)
        return plasma_list

    def handle_plasma_split(self, sub, field, tick):
        if sub.plasma_fragments:
            return []
        fragments = []
        matrix = field[self._substance_region(sub)]
        total_e = float(np.sum(matrix))
        if total_e < 100:
            return []
        rng = np.random.RandomState(tick)
        for i in range(PHASE_PLASMA_SPLIT):
            fx = sub.cx + rng.randint(-5, 6)
            fy = sub.cy + rng.randint(-5, 6)
            fx = max(1, min(self.grid_size - 2, fx))
            fy = max(1, min(self.grid_size - 2, fy))
            frag_matrix = rng.uniform(0.3, 0.7, (3, 3)) * matrix.mean()
            field[fy - 1:fy + 2, fx - 1:fx + 2] += frag_matrix
            field[fy - 1:fy + 2, fx - 1:fx + 2] = np.maximum(
                field[fy - 1:fy + 2, fx - 1:fx + 2], 0)
            st, circ = ShapeAnalyzer.analyze(frag_matrix)
            frag = Substance(uid=self.next_uid, cx=fx, cy=fy,
                             matrix=frag_matrix.copy(), birth_tick=tick,
                             structure_type=st, circularity=circ,
                             signature=f"{sub.signature}-f{i}", signed_tick=tick)
            self.next_uid += 1
            self.substances.append(frag)
            self._pending_confirms.append(frag)
            fragments.append(frag)
            sub.plasma_fragments.append(frag.uid)
        field[self._substance_region(sub)] *= 0.5
        return fragments

    def apply_liquid_drift(self, sub, field):
        if sub.phase != PhaseState.LIQUID or not sub.alive:
            return
        region = field[self._substance_region(sub)]
        gy, gx = np.gradient(region)
        total_g = np.sqrt(np.mean(gx) ** 2 + np.mean(gy) ** 2)
        if total_g > 0.01:
            sub.drift_vx += PHASE_DRIFT_RATE * np.mean(gx) / total_g
            sub.drift_vy += PHASE_DRIFT_RATE * np.mean(gy) / total_g
        if abs(sub.drift_vx) >= 1.0:
            step_x = int(sub.drift_vx)
            new_cx = max(1, min(self.grid_size - 2, sub.cx + step_x))
            if new_cx != sub.cx:
                old = field[self._substance_region(sub)].copy()
                field[self._substance_region(sub)] = BACKGROUND
                field[sub.cy - 1:sub.cy + 2, new_cx - 1:new_cx + 2] += old
                sub.cx = new_cx
            sub.drift_vx -= step_x
        if abs(sub.drift_vy) >= 1.0:
            step_y = int(sub.drift_vy)
            new_cy = max(1, min(self.grid_size - 2, sub.cy + step_y))
            if new_cy != sub.cy:
                old = field[self._substance_region(sub)].copy()
                field[self._substance_region(sub)] = BACKGROUND
                field[new_cy - 1:new_cy + 2, sub.cx - 1:sub.cx + 2] += old
                sub.cy = new_cy
            sub.drift_vy -= step_y

    def try_sign(self, px, py, pid, tick, field):
        for sub in self.substances:
            if not sub.alive or sub.is_signed:
                continue
            if (sub.cx - 1 <= px <= sub.cx + 1 and
                    sub.cy - 1 <= py <= sub.cy + 1):
                current = field[self._substance_region(sub)].copy()
                sub.sign(pid, tick, current)
                key = (sub.cx - 1, sub.cy - 1)
                if key in self.trackers:
                    self.trackers[key].state = "signed"
                return sub
        return None

    def tick(self, field, tick):
        self._pending_confirms.clear()
        confirmed_uids = set()  # 防重复

        if tick % SCAN_INTERVAL == 0 and tick > 0:
            self.scan_hotspots(field, tick)
            self.check_stability(tick)
            self.run_interference_tests(field, tick)
        self.check_recovery(field, tick)

        # v0.4 竞争管线（降频运行）
        if tick % COMPETITION_INTERVAL == 0:
            self.apply_maintenance(field, tick)
            self.apply_competition(field, tick)
            self.apply_fusion(field, tick)

        self.check_dissolution(field, tick)

        # 相变
        plasma_subs = self.update_phases(tick)
        for sub in plasma_subs:
            self.handle_plasma_split(sub, field, tick)
        for sub in self.substances:
            if sub.phase == PhaseState.LIQUID and sub.alive:
                self.apply_liquid_drift(sub, field)

        # 去重
        unique = []
        for s in self._pending_confirms:
            if s.uid not in confirmed_uids:
                confirmed_uids.add(s.uid)
                unique.append(s)
        return unique

    @property
    def alive_count(self):
        return sum(1 for s in self.substances if s.alive)

    @property
    def total_born(self):
        return len(self.substances)

    @property
    def signed_count(self):
        return sum(1 for s in self.substances if s.is_signed and s.alive)

    def phase_counts(self):
        counts = {p: 0 for p in PhaseState}
        for s in self.substances:
            if s.alive:
                counts[s.phase] += 1
        return {k.value: v for k, v in counts.items()}

    def export_templates(self):
        return [s.to_dict() for s in self.substances if s.is_signed]

    def save_templates(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "version": "v0.4",
                "total_signed": self.signed_count,
                "total_fusions": self.total_fusions,
                "templates": self.export_templates(),
            }, f, ensure_ascii=False, indent=2)


# ============================================================================
# 粒子（v0.3 不变）
# ============================================================================
class Particle:
    def __init__(self, grid_size, pid="PC-1", seed=123456789):
        self.grid_size = grid_size
        self.pid = pid
        self.rng = XORShift32(seed)
        self.x = self.rng.randint(0, grid_size)
        self.y = self.rng.randint(0, grid_size)
        self.step_count = 0
        self.raises = 0
        self.lowers = 0
        self.signs = 0
        self.moves = 0
        self._dx = [0, 1, 0, -1]
        self._dy = [-1, 0, 1, 0]
        self._ddx = [0, 1, 0, -1, 1, -1, -1, 1]
        self._ddy = [-1, 0, 1, 0, -1, -1, 1, 1]
        self.dark_hits = 0
        self.revealed_rules = []
        self.last_penalty = 0.0

    def _neighbors(self, field):
        return [((self.x + self._dx[d]) % self.grid_size,
                 (self.y + self._dy[d]) % self.grid_size,
                 float(field[(self.y + self._dy[d]) % self.grid_size,
                             (self.x + self._dx[d]) % self.grid_size]))
                for d in range(4)]

    def _substep_move(self, field, skeleton):
        r = self.rng.random()
        if r < 0.50:
            nbrs = self._neighbors(field)
            best = max(nbrs, key=lambda n: n[2])
            self.x, self.y = best[0], best[1]
        elif r < 0.75:
            self.x, self.y = skeleton.residue_attraction(self.x, self.y, field)
        else:
            d = self.rng.randint(0, 8)
            self.x = (self.x + self._ddx[d]) % self.grid_size
            self.y = (self.y + self._ddy[d]) % self.grid_size
        self.moves += 1

    def _in_signed_zone(self, detector):
        for sub in detector.substances:
            if sub.is_signed and sub.alive:
                if (sub.cx - 1 <= self.x <= sub.cx + 1 and
                        sub.cy - 1 <= self.y <= sub.cy + 1):
                    return True
        return False

    def _check_gradient_steep(self, field):
        if (self.x < 1 or self.x >= self.grid_size - 1 or
                self.y < 1 or self.y >= self.grid_size - 1):
            return False
        gx = abs(field[self.y, self.x + 1] - field[self.y, self.x - 1])
        gy = abs(field[self.y + 1, self.x] - field[self.y - 1, self.x])
        return (gx + gy) > 30

    def _check_cluster_dense(self, detector, radius=5):
        return sum(1 for s in detector.substances
                   if s.alive and abs(s.cx - self.x) <= radius
                   and abs(s.cy - self.y) <= radius) >= 3

    def step(self, field, detector, skeleton, dark_rules, tick):
        self.step_count += 1
        m = r = l = s = 0
        sign_result = None
        total_dark_penalty = 0.0
        energy_mean = float(np.mean(field))
        in_signed = self._in_signed_zone(detector)
        grad_steep = self._check_gradient_steep(field)
        cluster_dense = self._check_cluster_dense(detector)

        for _ in range(SUBSTEPS_PER_TICK):
            self._substep_move(field, skeleton)
            m += 1
            skeleton.deposit(self.x, self.y)

            roll = self.rng.random()
            if roll < RAISE_PROB:
                penalty, _ = dark_rules.apply(
                    self.x, self.y, field, energy_mean,
                    in_signed, grad_steep, cluster_dense)
                total_dark_penalty += penalty
                if penalty > 0:
                    self.dark_hits += 1
                field[self.y, self.x] += SUB_RAISE_AMOUNT
                self.raises += 1
                r += 1
            elif roll < RAISE_PROB + LOWER_PROB:
                penalty, _ = dark_rules.apply(
                    self.x, self.y, field, energy_mean,
                    in_signed, grad_steep, cluster_dense)
                total_dark_penalty += penalty
                if penalty > 0:
                    self.dark_hits += 1
                if not self._in_signed_zone(detector):
                    field[self.y, self.x] = max(0,
                        field[self.y, self.x] - LOWER_AMOUNT)
                    self.lowers += 1
                    l += 1
            elif roll < RAISE_PROB + LOWER_PROB + SIGN_PROB:
                penalty, _ = dark_rules.apply(
                    self.x, self.y, field, energy_mean,
                    in_signed, grad_steep, cluster_dense)
                total_dark_penalty += penalty
                if penalty > 0:
                    self.dark_hits += 1
                self.signs += 1
                s += 1
                result = detector.try_sign(self.x, self.y, self.pid, tick, field)
                if result:
                    sign_result = result

        self.last_penalty = total_dark_penalty
        return m, r, l, s, sign_result, total_dark_penalty


# ============================================================================
# 山海世界
# ============================================================================
class ShanhaiWorld:
    def __init__(self, seed=42):
        self.grid_size = GRID_SIZE
        self.tick = 0
        rng = np.random.RandomState(seed)
        self.field = np.full((GRID_SIZE, GRID_SIZE), INIT_ENERGY, dtype=np.float64)
        self.field += rng.uniform(-INIT_NOISE, INIT_NOISE, (GRID_SIZE, GRID_SIZE))
        self.field = np.maximum(self.field, 0)
        self.particle = Particle(GRID_SIZE, pid="PC-1", seed=seed + 1)
        self.detector = SubstanceDetector(GRID_SIZE)
        self.dark_rules = DarkConstraintSystem(seed=seed + 100)
        self.skeleton = DarkSkeleton(GRID_SIZE)
        self.log = []
        self.total_moves = 0
        self.total_raises = 0
        self.total_lowers = 0
        self.total_signs = 0
        self.total_dark_penalty = 0.0

    def _diffuse(self):
        rolled = [np.roll(self.field, (0, 1)), np.roll(self.field, (0, -1)),
                  np.roll(self.field, (1, 0)), np.roll(self.field, (-1, 0))]
        neighbor_avg = sum(rolled) / 4.0
        self.field += DIFFUSE_RATE * (neighbor_avg - self.field)
        decay_field = DECAY_RATE * (self.field - BACKGROUND)
        self.field -= decay_field
        for sub in self.detector.substances:
            if sub.is_signed and sub.alive:
                y0, y1 = sub.cy - 1, sub.cy + 2
                x0, x1 = sub.cx - 1, sub.cx + 2
                self.field[y0:y1, x0:x1] += DECAY_RATE * (
                    self.field[y0:y1, x0:x1] - BACKGROUND)
                self.field[y0:y1, x0:x1] += 0.05 * (
                    sub.matrix - self.field[y0:y1, x0:x1])
        self.field = np.maximum(self.field, 0)
        self.field = np.minimum(self.field, MAX_FIELD_ENERGY)

    def step(self):
        self.tick += 1
        moves, raises, lowers, signs, sign_result, dark_penalty = \
            self.particle.step(self.field, self.detector,
                               self.skeleton, self.dark_rules, self.tick)
        self.total_moves += moves
        self.total_raises += raises
        self.total_lowers += lowers
        self.total_signs += signs
        self.total_dark_penalty += dark_penalty
        self.skeleton.decay()
        self._diffuse()
        if self.tick % SKELETON_SCAN_INTERVAL == 0:
            self.skeleton.scan_skeleton(self.field)
        new_subs = self.detector.tick(self.field, self.tick)
        for sub in new_subs:
            self.log.append({
                "event": "substance_born",
                "tick": self.tick,
                "uid": sub.uid,
                "position": (sub.cx, sub.cy),
                "type": sub.structure_type,
                "phase": sub.phase.value,
                "fusion_count": sub.fusion_count,
            })
        if sign_result:
            self.log.append({"event": "substance_signed", "tick": self.tick,
                             "uid": sign_result.uid})
        return new_subs if new_subs else None

    def run(self, max_ticks=100000, verbose=True, target_substances=1):
        all_substances = []
        announced = set()  # 已公告的物质UID
        for _ in range(max_ticks):
            new_subs = self.step()
            if new_subs:
                for sub in new_subs:
                    if sub.uid not in announced:
                        announced.add(sub.uid)
                        all_substances.append(sub)
                        if verbose:
                            tag = "⚡" if sub.phase == PhaseState.PLASMA else (
                                "🔄" if sub.fusion_count > 0 else "⛰️")
                            extra = f" 融合×{sub.fusion_count}" if sub.fusion_count > 0 else ""
                            print(f"\n  {tag} 物质 #{sub.uid}！tick={self.tick} "
                                  f"({sub.cx},{sub.cy}) {sub.structure_type} "
                                  f"{sub.phase.value}{extra}")

            if self.log and self.log[-1].get("event") == "substance_signed":
                if verbose:
                    print(f"  🖊️  签名 #{self.log[-1]['uid']}")

            if verbose and self.tick % 5000 == 0:
                d = self.detector
                print(f"  [{self.tick:6d}] μ={self.field.mean():.1f} "
                      f"物质={d.alive_count} ✍={d.signed_count} "
                      f"{d.phase_counts()} "
                      f"融合={d.total_fusions} 争能={d.total_energy_transferred:.0f}")

            if self.detector.total_born >= target_substances:
                break
        return all_substances


# ============================================================================
# 可视化
# ============================================================================
class Visualizer:
    def __init__(self, world):
        self.world = world
        self.fig = None
        self.ax = None
        self.im = None
        self.paused = False
        self.speed = 1
        self.particle_dot = None
        self.sub_elements = []
        self.skel_lines = []

    def setup(self):
        import matplotlib
        matplotlib.use("TkAgg")
        import matplotlib.pyplot as plt
        self.fig, self.ax = plt.subplots(figsize=(10, 9))
        self.fig.canvas.manager.set_window_title("山海 v0.4 — 物质竞争")
        self.im = self.ax.imshow(self.world.field, cmap=CMAP, aspect="equal",
                                 vmin=0, vmax=100, origin="upper",
                                 interpolation="bilinear")
        self.ax.set_title("山海 v0.4 · 物质竞争")
        plt.colorbar(self.im, ax=self.ax, label="能量")
        self.particle_dot, = self.ax.plot([], [], "o", color=COLOR_PARTICLE,
                                          markersize=10, markeredgecolor="black", zorder=10)
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)
        plt.ion()
        plt.show()

    def _on_key(self, event):
        if event.key == " ":
            self.paused = not self.paused
        elif event.key == "q":
            sys.exit(0)
        elif event.key == "+":
            self.speed = min(self.speed * 2, 64)
        elif event.key == "-":
            self.speed = max(self.speed // 2, 1)

    def update(self):
        if self.fig is None:
            return
        import matplotlib.pyplot as plt
        self.im.set_array(self.world.field)
        self.im.set_clim(vmin=0, vmax=max(100, float(np.max(self.world.field))))
        self.particle_dot.set_data([self.world.particle.x], [self.world.particle.y])
        for rect, ann in self.sub_elements:
            rect.remove()
            ann.remove()
        self.sub_elements.clear()
        for line in self.skel_lines:
            line.remove()
        self.skel_lines.clear()
        for x1, y1, x2, y2 in self.world.skeleton.edges:
            line, = self.ax.plot([x1, x2], [y1, y2], color="#ff880055", linewidth=1, zorder=1)
            self.skel_lines.append(line)
        if self.world.skeleton.nodes:
            dots = self.ax.scatter([n[0] for n in self.world.skeleton.nodes],
                                   [n[1] for n in self.world.skeleton.nodes],
                                   c="#ff8800", s=20, marker="x", alpha=0.6, zorder=2)
            self.skel_lines.append(dots)
        for sub in self.world.detector.substances:
            if not sub.alive:
                continue
            color = COLOR_PHASE.get(sub.phase, "#00ff88")
            lw = 3 if sub.is_signed else 1.5
            style = "-" if sub.is_signed else "--"
            alpha = 0.9 if sub.phase == PhaseState.PLASMA else 0.7
            rect = plt.Rectangle((sub.cx - 1.5, sub.cy - 1.5), 3, 3,
                                 fill=False, edgecolor=color, linewidth=lw,
                                 linestyle=style, alpha=alpha)
            self.ax.add_patch(rect)
            label = f"#{sub.uid}"
            if sub.fusion_count > 0:
                label += f"∪{sub.fusion_count}"
            if sub.phase != PhaseState.SOLID:
                label += f" {sub.phase.value[0]}"
            ann = self.ax.annotate(label, (sub.cx, sub.cy - 2.2),
                                   color=color, fontsize=6, ha="center")
            self.sub_elements.append((rect, ann))
        det = self.world.detector
        self.ax.set_title(
            f"山海 v0.4 · tick={self.world.tick} · "
            f"物质={det.alive_count} ✍{det.signed_count} "
            f"融合={det.total_fusions} · {det.phase_counts()}"
        )
        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events()

    def run_interactive(self, max_ticks=100000):
        self.setup()
        print(f"\n  山海 v0.4 — 物质竞争 启动")
        print(f"  维持消耗 | 能量争夺 | 融合演化")
        print(f"  操作: [空格]暂停 [+/-]变速 [q]退出\n")
        step = 0
        while step < max_ticks and self.fig is not None:
            if not self.paused:
                for _ in range(self.speed):
                    if step >= max_ticks:
                        break
                    self.world.step()
                    step += 1
                    log = self.world.log
                    if log and log[-1].get("event") == "substance_born":
                        e = log[-1]
                        fc = e.get("fusion_count", 0)
                        print(f"\n  {'🔄' if fc else '⛰️'} #{e['uid']} "
                              f"{e.get('type','')} {e.get('phase','')}"
                              + (f" 融合×{fc}" if fc else ""))
                    if log and log[-1].get("event") == "substance_signed":
                        print(f"  🖊️  #{log[-1]['uid']}")
                if self.world.tick % 100 == 0:
                    self.update()
            try:
                self.fig.canvas.flush_events()
            except Exception:
                break
            time.sleep(0.01)
        if self.fig:
            plt.ioff()
            plt.show()


# ============================================================================
# 主入口
# ============================================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="山海 v0.4 — 物质竞争")
    parser.add_argument("--no-viz", action="store_true")
    parser.add_argument("--ticks", type=int, default=100000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--target", type=int, default=1)
    parser.add_argument("--templates", type=str, default="templates_v4.json")
    args = parser.parse_args()

    print("=" * 60)
    print("  山海 v0.4 — 物质竞争 · 融合纪元")
    print("  维持消耗 · 能量争夺 · 融合演化")
    print("=" * 60)

    world = ShanhaiWorld(seed=args.seed)

    if args.no_viz:
        t0 = time.time()
        substances = world.run(max_ticks=args.ticks, verbose=True,
                               target_substances=args.target)
        elapsed = time.time() - t0
        det = world.detector
        print(f"\n  {'='*60}")
        print(f"  tick={world.tick} ({elapsed:.1f}s)")
        print(f"  物质: {det.total_born} born / {det.alive_count} alive / "
              f"{det.signed_count} signed")
        print(f"  相态: {det.phase_counts()}")
        print(f"  融合: {det.total_fusions} 次")
        print(f"  能量争夺: {det.total_energy_transferred:.0f} 总转移")
        print(f"  维持消耗: {det.total_maintenance_consumed:.0f} 总消耗")

        # 融合物质详情
        fused = [s for s in det.substances if s.fusion_count > 0]
        if fused:
            print(f"\n  融合物质:")
            for s in fused[:10]:
                print(f"    #{s.uid}: 融合×{s.fusion_count} "
                      f"来源={s.fused_from} {s.structure_type} "
                      f"○={s.circularity:.3f} {s.phase.value}")

        if det.signed_count > 0:
            tmpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     args.templates)
            det.save_templates(tmpl_path)
            print(f"\n  模板导出: {tmpl_path}")
    else:
        viz = Visualizer(world)
        viz.run_interactive(max_ticks=args.ticks)


if __name__ == "__main__":
    main()
