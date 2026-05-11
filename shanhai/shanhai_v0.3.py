#!/usr/bin/env python3
"""
山海 v0.3 — 暗宇宙 · 相变纪元
================================
v0.3 在 v0.2 签名+自持基础上，融合三个新系统：

  暗约束 — 不可见规则随机惩罚粒子的建造行为，粒子只能从后果猜测
  暗骨架 — 粒子活动留下残留场，周期性放大形成不可见引导骨架
  物质相变 — 已签名结构按能量阈值切换固/液/等离子态，改变形态与行为

三者闭环：暗约束→改变行为→暗骨架引导领地→RAISE触发相变→碎片触发新约束

用法: python shanhai_v0.3.py
      python shanhai_v0.3.py --no-viz --target 5
"""

import numpy as np
import time
import sys
import os
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
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
STABILITY_WINDOW = 200
STABILITY_THRESHOLD = 0.20
PERTURB_AMPLITUDE = 5.0
PERTURB_RECOVERY = 50
HOTSPOT_SIGMA = 2.0
MIN_HOTSPOT_SUM = 550.0
GRADIENT_FOLLOW = 0.95
MOVE_PROB = 0.35
RAISE_PROB = 0.45
LOWER_PROB = 0.10
SIGN_PROB = 0.10
SUBSTEPS_PER_TICK = 4
SUB_RAISE_AMOUNT = 9.0          # 子步RAISE幅度（补偿快速移动的能量分散）

# 暗约束
DARK_RULE_COUNT = 12           # 暗约束规则总数
DARK_PENALTY_MAX = 8.0         # 暗约束最大惩罚能量
DARK_TRIGGER_BASE = 0.15       # 暗约束基础触发概率
DARK_REVEAL_CHANCE = 0.02      # 每tick粒子有2%概率"瞥见"一条暗约束

# 暗骨架
RESIDUE_PER_ACTION = 0.02      # 每次RAISE/LOWER的残留量
RESIDUE_DECAY = 0.001          # 残留衰减率
SKELETON_SCAN_INTERVAL = 500   # 骨架扫描间隔
SKELETON_NODES_MAX = 20        # 骨架节点上限
SKELETON_BUILD_BONUS = 0.80    # 骨架上建造能量消耗倍率（省20%）

# 物质相变
PHASE_SOLID_ENERGY = 600       # 固态上限（3×3总能量）
PHASE_LIQUID_ENERGY = 1200     # 液态上限
PHASE_DRIFT_RATE = 0.1         # 液态漂移速度（格/tick）
PHASE_PLASMA_SPLIT = 3         # 等离子态裂解份数


class PhaseState(Enum):
    SOLID = "固态"
    LIQUID = "液态"
    PLASMA = "等离子态"


class SubstanceState(Enum):
    TRACKING = "tracking"
    STABLE = "stable_candidate"
    TESTING = "testing"
    CONFIRMED = "confirmed"
    SIGNED = "signed"
    DISSOLVED = "dissolved"


TYPE_ROUND = "原生圆"
TYPE_ELONGATED = "椭圆"
TYPE_IRREGULAR = "不规则"

# 可视化
CMAP = "inferno"
COLOR_PHASE = {PhaseState.SOLID: "#00ff88", PhaseState.LIQUID: "#4488ff",
               PhaseState.PLASMA: "#ff44ff"}
COLOR_PARTICLE = "#ffffff"
COLOR_SKELETON = "#ff880040"


# ============================================================================
# XORShift32
# ============================================================================
class XORShift32:
    def __init__(self, seed: int = 42):
        self.state = max(seed & 0xFFFFFFFF, 1)
    def next(self) -> int:
        x = self.state
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self.state = x
        return x
    def randint(self, lo: int, hi: int) -> int:
        return lo + (self.next() % (hi - lo))
    def random(self) -> float:
        return self.next() / 0x100000000


# ============================================================================
# 暗约束系统
# ============================================================================
@dataclass
class DarkRule:
    """不可见规则——粒子不知道它的存在，只能从后果猜测"""
    rid: int
    name: str                    # 规则描述（不暴露给粒子）
    condition: str               # 触发条件类型
    trigger_prob: float          # 触发概率
    penalty: float               # 惩罚能量
    hit_count: int = 0            # 实际触发次数
    particle_guesses: int = 0     # 粒子试探次数（每tick检测）

    def check(self, particle_x: int, particle_y: int,
              field: np.ndarray, energy_mean: float) -> bool:
        """检查当前粒子位置是否满足触发条件"""
        v = field[particle_y, particle_x]
        if self.condition == "high_energy" and v > energy_mean * 1.8:
            return True
        elif self.condition == "low_energy" and v < energy_mean * 0.6:
            return True
        elif self.condition == "near_signed":
            return True  # 在try_sign中单独判断
        elif self.condition == "gradient_steep":
            return True  # 在粒子step中采样判断
        elif self.condition == "cluster_dense":
            return True  # 物质密集区域
        elif self.condition == "edge_zone":
            return (particle_x < 10 or particle_x > 89 or
                    particle_y < 10 or particle_y > 89)
        elif self.condition == "center_zone":
            return (30 < particle_x < 70 and 30 < particle_y < 70)
        elif self.condition == "corner_zone":
            return ((particle_x < 20 or particle_x > 79) and
                    (particle_y < 20 or particle_y > 79))
        elif self.condition == "diagonal_zone":
            return abs(particle_x - particle_y) < 15
        elif self.condition == "even_position":
            return (particle_x + particle_y) % 2 == 0
        elif self.condition == "odd_position":
            return (particle_x + particle_y) % 2 == 1
        elif self.condition == "far_from_center":
            cx, cy = 50, 50
            return ((particle_x - cx)**2 + (particle_y - cy)**2) > 1600
        return False


class DarkConstraintSystem:
    """暗约束管理器——95%规则不可见"""

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

    def __init__(self, seed: int = 999):
        self.rng = XORShift32(seed)
        self.rules: List[DarkRule] = []
        for i, (name, cond, prob, pen) in enumerate(self.DARK_RULE_DEFS):
            self.rules.append(DarkRule(
                rid=i, name=name, condition=cond,
                trigger_prob=prob, penalty=pen
            ))
        self.total_triggers = 0
        self.revealed: List[int] = []  # 粒子瞥见的规则ID

    def apply(self, particle_x: int, particle_y: int,
              field: np.ndarray, energy_mean: float,
              in_signed_zone: bool, gradient_steep: bool,
              cluster_dense: bool) -> Tuple[float, List[int]]:
        """
        对粒子当前位置判定所有暗约束。
        返回 (总惩罚, 触发的规则ID列表)
        """
        total_penalty = 0.0
        triggered = []

        for rule in self.rules:
            # 特殊条件处理
            if rule.condition == "near_signed" and not in_signed_zone:
                continue
            if rule.condition == "gradient_steep" and not gradient_steep:
                continue
            if rule.condition == "cluster_dense" and not cluster_dense:
                continue

            if rule.condition not in ("near_signed", "gradient_steep", "cluster_dense"):
                if not rule.check(particle_x, particle_y, field, energy_mean):
                    continue

            # 概率触发
            if self.rng.random() < rule.trigger_prob:
                total_penalty += rule.penalty
                rule.hit_count += 1
                triggered.append(rule.rid)
                rule.particle_guesses += 1

        self.total_triggers += len(triggered)
        return total_penalty, triggered

    def try_reveal(self) -> Optional[DarkRule]:
        """粒子有极低概率'瞥见'一条暗约束"""
        if self.rng.random() < DARK_REVEAL_CHANCE:
            unrevealed = [r for r in self.rules if r.rid not in self.revealed]
            if unrevealed:
                rule = unrevealed[self.rng.randint(0, len(unrevealed))]
                self.revealed.append(rule.rid)
                return rule
        return None


# ============================================================================
# 暗骨架系统
# ============================================================================
class DarkSkeleton:
    """残留场 + 骨架节点——空间本身拥有记忆"""

    def __init__(self, grid_size: int):
        self.grid_size = grid_size
        self.residue = np.zeros((grid_size, grid_size), dtype=np.float64)
        self.nodes: List[Tuple[int, int]] = []  # 骨架节点
        self.edges: List[Tuple[int, int, int, int]] = []  # 连线

    def deposit(self, x: int, y: int):
        """粒子行动留下残留"""
        self.residue[y, x] += RESIDUE_PER_ACTION

    def decay(self):
        """残留缓慢衰减"""
        self.residue -= RESIDUE_DECAY
        self.residue = np.maximum(self.residue, 0)

    def scan_skeleton(self, field: np.ndarray) -> int:
        """
        扫描残留场与能量扩散梯度的交点，生成骨架节点。
        返回新增节点数。
        """
        self.nodes.clear()
        self.edges.clear()

        # 残留梯度（水平和垂直方向）
        residue_gx = np.abs(np.diff(self.residue, axis=1, append=self.residue[:, -1:]))
        residue_gy = np.abs(np.diff(self.residue, axis=0, append=self.residue[-1:, :]))

        # 能量扩散梯度
        energy_gx = np.abs(np.diff(field, axis=1, append=field[:, -1:]))
        energy_gy = np.abs(np.diff(field, axis=0, append=field[-1:, :]))

        # 交点：残留梯度高且能量梯度高的位置
        combined = (residue_gx + residue_gy) * 0.3 + (energy_gx + energy_gy) * 0.7
        threshold = np.percentile(combined, 95)

        candidates = np.argwhere(combined > threshold)
        if len(candidates) > SKELETON_NODES_MAX:
            # 选top N
            scores = combined[combined > threshold]
            idx = np.argsort(scores)[-SKELETON_NODES_MAX:]
            candidates = candidates[idx]

        for cy, cx in candidates:
            self.nodes.append((int(cx), int(cy)))

        # 连线：相邻节点之间
        for i, (x1, y1) in enumerate(self.nodes):
            for x2, y2 in self.nodes[i + 1:]:
                dist = np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
                if dist < 15:  # 近距离节点连线
                    self.edges.append((x1, y1, x2, y2))

        return len(self.nodes)

    def is_on_skeleton(self, x: int, y: int, radius: int = 2) -> bool:
        """检查位置是否在骨架附近"""
        for nx, ny in self.nodes:
            if abs(x - nx) <= radius and abs(y - ny) <= radius:
                return True
        return False

    def residue_attraction(self, x: int, y: int, field: np.ndarray) -> Tuple[int, int]:
        """残留场吸引力——粒子有概率被拉向高残留区域"""
        best_x, best_y = x, y
        best_score = self.residue[y, x]

        for dy in range(-1, 2):
            for dx in range(-1, 2):
                if dx == 0 and dy == 0:
                    continue
                nx = (x + dx) % self.grid_size
                ny = (y + dy) % self.grid_size
                # 残留 + 0.3×能量梯度 综合得分
                score = self.residue[ny, nx] + 0.3 * field[ny, nx]
                if score > best_score:
                    best_score = score
                    best_x, best_y = nx, ny

        return best_x, best_y


# ============================================================================
# 物质结构（v0.3：新增相变）
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
    # v0.3 相变
    phase: PhaseState = PhaseState.SOLID
    phase_transition_tick: int = -1
    drift_vx: float = 0.0       # 液态漂移速度
    drift_vy: float = 0.0
    plasma_fragments: List[int] = field(default_factory=list)  # 等离子碎片UID

    @property
    def alive(self) -> bool:
        return self.dissolve_tick < 0

    @property
    def is_signed(self) -> bool:
        return self.signature != ""

    @property
    def total_energy(self) -> float:
        return float(np.sum(self.matrix))

    def update_phase(self, tick: int):
        """根据总能量自动切换状态"""
        e = self.total_energy
        old_phase = self.phase
        if e > PHASE_LIQUID_ENERGY:
            self.phase = PhaseState.PLASMA
        elif e > PHASE_SOLID_ENERGY:
            self.phase = PhaseState.LIQUID
        else:
            self.phase = PhaseState.SOLID
        if old_phase != self.phase:
            self.phase_transition_tick = tick

    def sign(self, particle_id: str, tick: int, current_matrix: np.ndarray = None):
        self.signature = particle_id
        self.signed_tick = tick
        self.decay_mult = 0.0
        if current_matrix is not None:
            self.matrix = current_matrix.copy()

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "position": [int(self.cx), int(self.cy)],
            "birth_tick": self.birth_tick,
            "signed_tick": self.signed_tick,
            "signature": self.signature,
            "phase": self.phase.value,
            "structure_type": self.structure_type,
            "circularity": round(self.circularity, 4),
            "energy_sum": round(self.total_energy, 1),
            "matrix": [[round(float(v), 1) for v in row] for row in self.matrix],
            "alive": self.alive,
            "plasma_fragments": self.plasma_fragments,
        }


# ============================================================================
# 形状分析器
# ============================================================================
class ShapeAnalyzer:
    @staticmethod
    def analyze(matrix: np.ndarray) -> Tuple[str, float]:
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
        circularity = (4.0 * np.pi * area) / (perimeter * perimeter)
        if circularity > 0.75:
            return TYPE_ROUND, circularity
        elif circularity > 0.50:
            return TYPE_ELONGATED, circularity
        return TYPE_IRREGULAR, circularity


# ============================================================================
# 物质检测器（v0.3：整合暗约束+相变判定）
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
    test_recovery_samples: int = 0


class SubstanceDetector:
    def __init__(self, grid_size: int):
        self.grid_size = grid_size
        self.trackers: Dict[Tuple[int, int], HotspotTracker] = {}
        self.substances: List[Substance] = []
        self.next_uid = 1
        self._pending_confirms: List[Substance] = []

    def scan_hotspots(self, field: np.ndarray, tick: int) -> None:
        sums = np.zeros((self.grid_size - 2, self.grid_size - 2))
        for dy in range(3):
            for dx in range(3):
                sums += field[dy:dy + self.grid_size - 2, dx:dx + self.grid_size - 2]
        global_mean = float(np.mean(field))
        global_std = float(np.std(field))
        threshold = global_mean + HOTSPOT_SIGMA * global_std
        hot_y, hot_x = np.where((sums > threshold) & (sums > MIN_HOTSPOT_SUM))
        current_hotspots = set()
        for i in range(len(hot_y)):
            cy, cx = int(hot_y[i]), int(hot_x[i])
            current_hotspots.add((cx, cy))
            e_sum = float(sums[cy, cx])
            if (cx, cy) not in self.trackers:
                tracker = HotspotTracker(cx=cx, cy=cy, detected_tick=tick)
                tracker.history = deque([e_sum], maxlen=STABILITY_WINDOW)
                self.trackers[(cx, cy)] = tracker
            else:
                tracker = self.trackers[(cx, cy)]
                if tracker.state not in ("testing", "confirmed", "signed"):
                    tracker.history.append(e_sum)
        dead = []
        for key, trk in self.trackers.items():
            if key not in current_hotspots and trk.state not in ("confirmed", "signed"):
                dead.append(key)
        for key in dead:
            del self.trackers[key]

    def check_stability(self, tick: int) -> None:
        for key, trk in list(self.trackers.items()):
            if trk.state != "tracking":
                continue
            if len(trk.history) < STABILITY_WINDOW:
                continue
            recent = list(trk.history)[-100:]
            mean_val = np.mean(recent)
            if mean_val < 1e-6:
                continue
            variation = (np.max(recent) - np.min(recent)) / mean_val
            if variation < STABILITY_THRESHOLD:
                trk.state = "stable_candidate"
                trk.stable_mean = mean_val

    def run_interference_tests(self, field: np.ndarray, tick: int) -> None:
        for key, trk in list(self.trackers.items()):
            if trk.state != "stable_candidate":
                continue
            trk.state = "testing"
            trk.test_start_tick = tick
            trk.pre_test_matrix = field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3].copy()
            rng = np.random.RandomState(tick)
            perturbation = rng.uniform(-PERTURB_AMPLITUDE, PERTURB_AMPLITUDE, (3, 3))
            field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3] += perturbation
            field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3] = np.maximum(
                field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3], 0)

    def check_recovery(self, field: np.ndarray, tick: int) -> None:
        for key, trk in list(self.trackers.items()):
            if trk.state != "testing":
                continue
            if tick - trk.test_start_tick < PERTURB_RECOVERY:
                continue
            pre_sum = float(np.sum(trk.pre_test_matrix))
            current_sum = float(np.sum(
                field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3]))
            if pre_sum < 1e-6:
                trk.state = "tracking"
                continue
            recovery_ratio = abs(current_sum - pre_sum) / pre_sum
            if recovery_ratio < STABILITY_THRESHOLD:
                matrix = field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3].copy()
                stype, circularity = ShapeAnalyzer.analyze(matrix)
                sub = Substance(
                    uid=self.next_uid, cx=trk.cx + 1, cy=trk.cy + 1,
                    matrix=matrix, birth_tick=tick,
                    structure_type=stype, circularity=circularity,
                )
                self.next_uid += 1
                self.substances.append(sub)
                self._pending_confirms.append(sub)
                trk.state = "confirmed"
            else:
                trk.state = "tracking"
                trk.history.clear()

    def check_dissolution(self, field: np.ndarray, tick: int) -> None:
        for sub in self.substances:
            if not sub.alive:
                continue
            current_sum = float(np.sum(
                field[sub.cy - 1:sub.cy + 2, sub.cx - 1:sub.cx + 2]))
            birth_sum = float(np.sum(sub.matrix))
            if birth_sum < 1e-6:
                continue
            threshold = 0.50 if sub.is_signed else STABILITY_THRESHOLD * 2
            if current_sum < birth_sum * (1 - threshold):
                sub.dissolve_tick = tick

    def update_phases(self, tick: int) -> List[Substance]:
        """检查所有签名物质，触发相变。返回进入等离子态的物质列表。"""
        plasma_list = []
        for sub in self.substances:
            if not sub.is_signed or not sub.alive:
                continue
            sub.update_phase(tick)
            if sub.phase == PhaseState.PLASMA and not sub.plasma_fragments:
                plasma_list.append(sub)
        return plasma_list

    def handle_plasma_split(self, sub: Substance, field: np.ndarray,
                            tick: int) -> List[Substance]:
        """等离子态裂解：结构分裂为多个碎片"""
        if sub.plasma_fragments:
            return []
        fragments = []
        matrix = field[sub.cy - 1:sub.cy + 2, sub.cx - 1:sub.cx + 2]
        total_e = float(np.sum(matrix))
        if total_e < 100:
            return []

        # 裂解为3个碎片，各携带部分能量
        rng = np.random.RandomState(tick)
        for i in range(PHASE_PLASMA_SPLIT):
            # 碎片在母体周围随机散落
            fx = sub.cx + rng.randint(-5, 6)
            fy = sub.cy + rng.randint(-5, 6)
            fx = max(1, min(self.grid_size - 2, fx))
            fy = max(1, min(self.grid_size - 2, fy))

            frag_matrix = rng.uniform(0.3, 0.7, (3, 3)) * matrix.mean()
            field[fy - 1:fy + 2, fx - 1:fx + 2] += frag_matrix
            field[fy - 1:fy + 2, fx - 1:fx + 2] = np.maximum(
                field[fy - 1:fy + 2, fx - 1:fx + 2], 0)

            stype, circ = ShapeAnalyzer.analyze(frag_matrix)
            frag = Substance(
                uid=self.next_uid, cx=fx, cy=fy,
                matrix=frag_matrix.copy(), birth_tick=tick,
                structure_type=stype, circularity=circ,
                signature=f"{sub.signature}-f{i}", signed_tick=tick,
                decay_mult=0.0,
            )
            self.next_uid += 1
            self.substances.append(frag)
            self._pending_confirms.append(frag)
            fragments.append(frag)
            sub.plasma_fragments.append(frag.uid)

        # 母体能量减半
        field[sub.cy - 1:sub.cy + 2, sub.cx - 1:sub.cx + 2] *= 0.5
        return fragments

    def apply_liquid_drift(self, sub: Substance, field: np.ndarray) -> None:
        """液态物质沿能量梯度漂移"""
        if sub.phase != PhaseState.LIQUID or not sub.alive:
            return
        # 计算3×3区域的能量梯度方向
        region = field[sub.cy - 1:sub.cy + 2, sub.cx - 1:sub.cx + 2]
        gy, gx = np.gradient(region)

        # 沿梯度方向漂移
        total_g = np.sqrt(np.mean(gx) ** 2 + np.mean(gy) ** 2)
        if total_g > 0.01:
            sub.drift_vx += PHASE_DRIFT_RATE * np.mean(gx) / total_g
            sub.drift_vy += PHASE_DRIFT_RATE * np.mean(gy) / total_g

        # 应用漂移（次格点精度累积）
        if abs(sub.drift_vx) >= 1.0:
            step_x = int(sub.drift_vx)
            new_cx = max(1, min(self.grid_size - 2, sub.cx + step_x))
            if new_cx != sub.cx:
                # 移动结构：复制能量到新位置
                old_region = field[sub.cy - 1:sub.cy + 2, sub.cx - 1:sub.cx + 2].copy()
                field[sub.cy - 1:sub.cy + 2, sub.cx - 1:sub.cx + 2] = BACKGROUND
                field[sub.cy - 1:sub.cy + 2, new_cx - 1:new_cx + 2] += old_region
                sub.cx = new_cx
            sub.drift_vx -= step_x

        if abs(sub.drift_vy) >= 1.0:
            step_y = int(sub.drift_vy)
            new_cy = max(1, min(self.grid_size - 2, sub.cy + step_y))
            if new_cy != sub.cy:
                old_region = field[sub.cy - 1:sub.cy + 2, sub.cx - 1:sub.cx + 2].copy()
                field[sub.cy - 1:sub.cy + 2, sub.cx - 1:sub.cx + 2] = BACKGROUND
                field[new_cy - 1:new_cy + 2, sub.cx - 1:sub.cx + 2] += old_region
                sub.cy = new_cy
            sub.drift_vy -= step_y

    def try_sign(self, particle_x: int, particle_y: int,
                 particle_id: str, tick: int,
                 field: np.ndarray) -> Optional[Substance]:
        for sub in self.substances:
            if not sub.alive or sub.is_signed:
                continue
            if (sub.cx - 1 <= particle_x <= sub.cx + 1 and
                    sub.cy - 1 <= particle_y <= sub.cy + 1):
                current = field[sub.cy - 1:sub.cy + 2, sub.cx - 1:sub.cx + 2].copy()
                sub.sign(particle_id, tick, current)
                key = (sub.cx - 1, sub.cy - 1)
                if key in self.trackers:
                    self.trackers[key].state = "signed"
                return sub
        return None

    def tick(self, field: np.ndarray, tick: int) -> List[Substance]:
        self._pending_confirms.clear()
        if tick % SCAN_INTERVAL == 0 and tick > 0:
            self.scan_hotspots(field, tick)
            self.check_stability(tick)
            self.run_interference_tests(field, tick)
        self.check_recovery(field, tick)
        self.check_dissolution(field, tick)

        # 相变更新
        plasma_subs = self.update_phases(tick)
        for sub in plasma_subs:
            self.handle_plasma_split(sub, field, tick)

        # 液态漂移
        for sub in self.substances:
            if sub.phase == PhaseState.LIQUID and sub.alive:
                self.apply_liquid_drift(sub, field)

        return list(self._pending_confirms)

    @property
    def alive_count(self) -> int:
        return sum(1 for s in self.substances if s.alive)

    @property
    def total_born(self) -> int:
        return len(self.substances)

    @property
    def signed_count(self) -> int:
        return sum(1 for s in self.substances if s.is_signed and s.alive)

    def phase_counts(self) -> dict:
        counts = {p: 0 for p in PhaseState}
        for s in self.substances:
            if s.alive:
                counts[s.phase] += 1
        return {k.value: v for k, v in counts.items()}

    def export_templates(self) -> List[dict]:
        return [s.to_dict() for s in self.substances if s.is_signed]

    def save_templates(self, path: str) -> None:
        templates = self.export_templates()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "version": "v0.3",
                "total_signed": len(templates),
                "templates": templates,
            }, f, ensure_ascii=False, indent=2)


# ============================================================================
# 粒子（v0.3：暗约束感知 + 试探行为 + 骨架引导）
# ============================================================================
class Particle:
    def __init__(self, grid_size: int, pid: str = "PC-1", seed: int = 123456789):
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
        # v0.3 暗约束觉察
        self.dark_hits = 0           # 被暗约束惩罚次数
        self.revealed_rules: List[int] = []  # 瞥见的规则
        self.preferred_actions: Dict[str, float] = {"RAISE": 1.0}  # 偏好权重
        self.last_penalty = 0.0
        # 试探计数器
        self.explore_phase = True
        self.explore_step = 0

    def _neighbors(self, field: np.ndarray):
        nbrs = []
        for d in range(4):
            nx = (self.x + self._dx[d]) % self.grid_size
            ny = (self.y + self._dy[d]) % self.grid_size
            nbrs.append((nx, ny, float(field[ny, nx])))
        return nbrs

    def _substep_move(self, field: np.ndarray, skeleton: DarkSkeleton):
        """子步移动：梯度跟随 + 残留吸引 + 随机抖动"""
        r = self.rng.random()
        if r < 0.50:
            # 梯度跟随
            nbrs = self._neighbors(field)
            best = max(nbrs, key=lambda n: n[2])
            self.x, self.y = best[0], best[1]
        elif r < 0.75:
            # 残留场吸引（25%概率被骨架引导）
            self.x, self.y = skeleton.residue_attraction(self.x, self.y, field)
        else:
            # 随机抖动
            d = self.rng.randint(0, 8)
            self.x = (self.x + self._ddx[d]) % self.grid_size
            self.y = (self.y + self._ddy[d]) % self.grid_size
        self.moves += 1

    def _in_signed_zone(self, detector: SubstanceDetector) -> bool:
        for sub in detector.substances:
            if sub.is_signed and sub.alive:
                if (sub.cx - 1 <= self.x <= sub.cx + 1 and
                        sub.cy - 1 <= self.y <= sub.cy + 1):
                    return True
        return False

    def _check_gradient_steep(self, field: np.ndarray) -> bool:
        """检查当前位置梯度是否陡峭"""
        if (self.x < 1 or self.x >= self.grid_size - 1 or
                self.y < 1 or self.y >= self.grid_size - 1):
            return False
        gx = abs(field[self.y, self.x + 1] - field[self.y, self.x - 1])
        gy = abs(field[self.y + 1, self.x] - field[self.y - 1, self.x])
        return (gx + gy) > 30

    def _check_cluster_dense(self, detector: SubstanceDetector, radius: int = 5) -> bool:
        """检查周围是否物质密集"""
        count = 0
        for sub in detector.substances:
            if not sub.alive:
                continue
            if abs(sub.cx - self.x) <= radius and abs(sub.cy - self.y) <= radius:
                count += 1
        return count >= 3

    def step(self, field: np.ndarray, detector: SubstanceDetector,
             skeleton: DarkSkeleton, dark_rules: DarkConstraintSystem,
             tick: int) -> Tuple[int, int, int, int, Optional[Substance], float]:
        """
        快速移动喷涂 + 暗约束判定。
        返回 (moves, raises, lowers, signs, sign_result, dark_penalty)
        """
        self.step_count += 1
        m = r = l = s = 0
        sign_result = None
        total_dark_penalty = 0.0
        self.explore_step += 1

        energy_mean = float(np.mean(field))
        in_signed = self._in_signed_zone(detector)
        grad_steep = self._check_gradient_steep(field)
        cluster_dense = self._check_cluster_dense(detector)

        for _ in range(SUBSTEPS_PER_TICK):
            self._substep_move(field, skeleton)
            m += 1
            skeleton.deposit(self.x, self.y)

            # 按概率操作
            roll = self.rng.random()
            if roll < RAISE_PROB:
                # 子步RAISE前先判定暗约束
                penalty, triggered = dark_rules.apply(
                    self.x, self.y, field, energy_mean,
                    in_signed, grad_steep, cluster_dense
                )
                if penalty > 0:
                    total_dark_penalty += penalty
                    self.dark_hits += 1
                    # 试探：记录被罚的操作，调整偏好
                    if self.explore_phase:
                        self.preferred_actions["RAISE"] = max(0.3,
                            self.preferred_actions.get("RAISE", 1.0) - 0.05)

                field[self.y, self.x] += SUB_RAISE_AMOUNT
                self.raises += 1
                r += 1

            elif roll < RAISE_PROB + LOWER_PROB:
                penalty, triggered = dark_rules.apply(
                    self.x, self.y, field, energy_mean,
                    in_signed, grad_steep, cluster_dense
                )
                if penalty > 0:
                    total_dark_penalty += penalty
                    self.dark_hits += 1

                if not self._in_signed_zone(detector):
                    field[self.y, self.x] = max(0,
                        field[self.y, self.x] - LOWER_AMOUNT)
                    self.lowers += 1
                    l += 1

            elif roll < RAISE_PROB + LOWER_PROB + SIGN_PROB:
                penalty, triggered = dark_rules.apply(
                    self.x, self.y, field, energy_mean,
                    in_signed, grad_steep, cluster_dense
                )
                if penalty > 0:
                    total_dark_penalty += penalty
                    self.dark_hits += 1

                self.signs += 1
                s += 1
                result = detector.try_sign(self.x, self.y, self.pid, tick, field)
                if result:
                    sign_result = result

        # 粒子试图"瞥见"暗约束
        if self.explore_phase and self.rng.random() < DARK_REVEAL_CHANCE:
            revealed = dark_rules.try_reveal()
            if revealed and revealed.rid not in self.revealed_rules:
                self.revealed_rules.append(revealed.rid)

        # 试探阶段结束条件
        if self.explore_step > 5000:
            self.explore_phase = False

        self.last_penalty = total_dark_penalty
        return m, r, l, s, sign_result, total_dark_penalty


# ============================================================================
# 山海世界（v0.3：暗宇宙融合）
# ============================================================================
class ShanhaiWorld:
    def __init__(self, seed: int = 42, grid_size: int = GRID_SIZE):
        self.grid_size = grid_size
        self.tick = 0

        rng = np.random.RandomState(seed)
        self.field = np.full((grid_size, grid_size), INIT_ENERGY, dtype=np.float64)
        self.field += rng.uniform(-INIT_NOISE, INIT_NOISE, (grid_size, grid_size))
        self.field = np.maximum(self.field, 0)

        self.particle = Particle(grid_size, pid="PC-1", seed=seed + 1)
        self.detector = SubstanceDetector(grid_size)
        self.dark_rules = DarkConstraintSystem(seed=seed + 100)
        self.skeleton = DarkSkeleton(grid_size)

        self.log: List[dict] = []
        self.total_moves = 0
        self.total_raises = 0
        self.total_lowers = 0
        self.total_signs = 0
        self.total_dark_penalty = 0.0

    def _diffuse(self):
        rolled = [
            np.roll(self.field, (0, 1)), np.roll(self.field, (0, -1)),
            np.roll(self.field, (1, 0)), np.roll(self.field, (-1, 0)),
        ]
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

    def step(self) -> Optional[List[Substance]]:
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

        # 骨架扫描
        if self.tick % SKELETON_SCAN_INTERVAL == 0:
            nodes_added = self.skeleton.scan_skeleton(self.field)
            if nodes_added > 0:
                self.log.append({
                    "event": "skeleton_scan",
                    "tick": self.tick,
                    "nodes": nodes_added,
                })

        new_subs = self.detector.tick(self.field, self.tick)

        for sub in new_subs:
            self.log.append({
                "event": "substance_born",
                "tick": self.tick,
                "uid": sub.uid,
                "position": (sub.cx, sub.cy),
                "type": sub.structure_type,
                "phase": sub.phase.value,
            })
        if sign_result:
            self.log.append({
                "event": "substance_signed",
                "tick": self.tick,
                "uid": sign_result.uid,
            })

        # 暗约束揭示事件
        if self.particle.revealed_rules and len(self.particle.revealed_rules) > len(
                [e for e in self.log if e.get("event") == "dark_reveal"]):
            self.log.append({
                "event": "dark_reveal",
                "tick": self.tick,
                "rule_count": len(self.particle.revealed_rules),
            })

        return new_subs if new_subs else None

    def run(self, max_ticks: int = 100000, verbose: bool = True,
            target_substances: int = 1) -> List[Substance]:
        all_substances = []
        for _ in range(max_ticks):
            new_subs = self.step()
            if new_subs:
                all_substances.extend(new_subs)
                if verbose:
                    for sub in new_subs:
                        tag = "⚡" if sub.phase == PhaseState.PLASMA else "⛰️"
                        print(f"\n  {tag} 物质 #{sub.uid} 诞生！"
                              f" tick={self.tick} ({sub.cx},{sub.cy}) "
                              f"类型={sub.structure_type} {sub.phase.value}")

            if self.log and self.log[-1].get("event") == "substance_signed":
                e = self.log[-1]
                if verbose:
                    print(f"  🖊️  物质 #{e['uid']} 已签名！tick={e['tick']}")

            if self.log and self.log[-1].get("event") == "skeleton_scan":
                e = self.log[-1]
                if verbose and e["nodes"] > 0:
                    print(f"  💀 暗骨架: {e['nodes']} 节点 tick={e['tick']}")

            if self.log and self.log[-1].get("event") == "dark_reveal":
                e = self.log[-1]
                if verbose:
                    print(f"  👁️  粒子瞥见 {e['rule_count']} 条暗约束 tick={e['tick']}")

            if verbose and self.tick % 5000 == 0:
                m = self.field.mean()
                d = self.detector
                phases = d.phase_counts()
                print(f"  [{self.tick:6d}] μ={m:.1f} 物质={d.alive_count} "
                      f"✍={d.signed_count} 相态={phases} "
                      f"暗罚={self.total_dark_penalty:.0f}")

            if self.detector.total_born >= target_substances:
                break
        return all_substances


# ============================================================================
# 可视化（v0.3：暗骨架叠加 + 相态着色）
# ============================================================================
class Visualizer:
    def __init__(self, world: ShanhaiWorld):
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
        self.fig.canvas.manager.set_window_title("山海 v0.3 — 暗宇宙")
        self.im = self.ax.imshow(
            self.world.field, cmap=CMAP, aspect="equal",
            vmin=0, vmax=100, origin="upper", interpolation="bilinear"
        )
        self.ax.set_title("山海 v0.3 · 暗宇宙")
        self.ax.set_xlabel("x")
        self.ax.set_ylabel("y")
        plt.colorbar(self.im, ax=self.ax, label="能量")

        self.particle_dot, = self.ax.plot(
            [], [], "o", color=COLOR_PARTICLE, markersize=10,
            markeredgecolor="black", zorder=10
        )

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

        # 清除旧标记
        for rect, ann in self.sub_elements:
            rect.remove()
            ann.remove()
        self.sub_elements.clear()

        for line in self.skel_lines:
            line.remove()
        self.skel_lines.clear()

        # 暗骨架连线
        for x1, y1, x2, y2 in self.world.skeleton.edges:
            line, = self.ax.plot([x1, x2], [y1, y2], color="#ff880055",
                                 linewidth=1, zorder=1)
            self.skel_lines.append(line)

        # 骨架节点
        if self.world.skeleton.nodes:
            nx = [n[0] for n in self.world.skeleton.nodes]
            ny = [n[1] for n in self.world.skeleton.nodes]
            dots = self.ax.scatter(nx, ny, c="#ff8800", s=20, marker="x",
                                   alpha=0.6, zorder=2)
            self.skel_lines.append(dots)

        # 物质标记（按相态着色）
        for sub in self.world.detector.substances:
            if not sub.alive:
                continue

            color = COLOR_PHASE.get(sub.phase, "#00ff88")
            lw = 3 if sub.is_signed else 1.5
            style = "-" if sub.is_signed else "--"
            alpha = 0.9 if sub.phase == PhaseState.PLASMA else 0.7

            rect = plt.Rectangle(
                (sub.cx - 1.5, sub.cy - 1.5), 3, 3,
                fill=False, edgecolor=color, linewidth=lw,
                linestyle=style, alpha=alpha
            )
            self.ax.add_patch(rect)

            label = f"#{sub.uid}"
            if sub.phase != PhaseState.SOLID:
                label += f" {sub.phase.value[0]}"
            ann = self.ax.annotate(
                label, (sub.cx, sub.cy - 2.2),
                color=color, fontsize=6, ha="center"
            )
            self.sub_elements.append((rect, ann))

        det = self.world.detector
        p = self.world.particle
        phases = det.phase_counts()
        self.ax.set_title(
            f"山海 v0.3 · tick={self.world.tick} · "
            f"物质={det.alive_count} ✍{det.signed_count} "
            f"暗罚={self.world.total_dark_penalty:.0f} · "
            f"{phases}"
        )

        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events()

    def run_interactive(self, max_ticks: int = 100000):
        self.setup()
        print(f"\n  山海 v0.3 — 暗宇宙 启动")
        print(f"  暗约束: {len(self.world.dark_rules.rules)} 条（粒子不可见）")
        print(f"  暗骨架: 残留场扫描间隔 {SKELETON_SCAN_INTERVAL}tick")
        print(f"  物质相变: 固态<{PHASE_SOLID_ENERGY}<液态<{PHASE_LIQUID_ENERGY}<等离子态")
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
                        print(f"\n  ⛰️  物质 #{e['uid']} {e.get('phase','')}")
                    if log and log[-1].get("event") == "substance_signed":
                        print(f"  🖊️  签名 #{log[-1]['uid']}")
                    if log and log[-1].get("event") == "dark_reveal":
                        print(f"  👁️  粒子瞥见暗约束")

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
    parser = argparse.ArgumentParser(description="山海 v0.3 — 暗宇宙")
    parser.add_argument("--no-viz", action="store_true")
    parser.add_argument("--ticks", type=int, default=100000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--target", type=int, default=1)
    parser.add_argument("--templates", type=str, default="templates_v3.json")
    args = parser.parse_args()

    print("=" * 60)
    print("  山海 v0.3 — 暗宇宙 · 相变纪元")
    print("  暗约束 · 暗骨架 · 物质相变")
    print("=" * 60)

    world = ShanhaiWorld(seed=args.seed)

    if args.no_viz:
        print(f"\n  暗约束: {len(world.dark_rules.rules)} 条（不可见）")
        print(f"  暗骨架: 间隔 {SKELETON_SCAN_INTERVAL}tick")
        print(f"  相变阈值: 固态<{PHASE_SOLID_ENERGY}<液态<{PHASE_LIQUID_ENERGY}<等离子\n")
        t0 = time.time()
        substances = world.run(max_ticks=args.ticks, verbose=True,
                               target_substances=args.target)
        elapsed = time.time() - t0

        det = world.detector
        print(f"\n  {'='*60}")
        print(f"  tick={world.tick} ({elapsed:.1f}s)")
        print(f"  物质: {det.total_born} born, {det.alive_count} alive, "
              f"{det.signed_count} signed")
        print(f"  相态: {det.phase_counts()}")
        print(f"  暗约束触发: {world.dark_rules.total_triggers} 次")
        print(f"  粒子被暗罚: {world.particle.dark_hits} 次, "
              f"瞥见 {len(world.particle.revealed_rules)} 条规则")
        print(f"  暗骨架: {len(world.skeleton.nodes)} 节点 "
              f"({len(world.skeleton.edges)} 连线)")

        if det.signed_count > 0:
            tmpl_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), args.templates)
            det.save_templates(tmpl_path)
            print(f"  模板导出: {tmpl_path}")
    else:
        viz = Visualizer(world)
        viz.run_interactive(max_ticks=args.ticks)


if __name__ == "__main__":
    main()
