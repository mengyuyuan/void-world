#!/usr/bin/env python3
"""
山海 v0.2 — 铭刻 · 签名与标记
================================
v0.2 在 v0.1 物质诞生基础上新增：
  · SIGN 操作 — 粒子对已确认物质签名，写入粒子ID
  · 签名衰减减半 — 已签名物质衰减速率 ×0.5（系统认证=更稳定）
  · 结构模板库 — templates.json 导出所有已签名物质
  · 形状分类 — 等周比圆形度 + 二值化形态分析
  · 可视化增强 — 已签名/未签名颜色区分

用法: python shanhai_v0.2.py
      python shanhai_v0.2.py --no-viz --target 5
"""

import numpy as np
import time
import sys
import os
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ============================================================================
# 常量
# ============================================================================
GRID_SIZE = 100
INIT_ENERGY = 50.0
INIT_NOISE = 10.0
BACKGROUND = 50.0
DIFFUSE_RATE = 0.02
DECAY_RATE = 0.003
SIGNED_DECAY_MULT = 0.0        # 签名后不向背景回归
SIGNED_SUSTAIN_RATE = 0.05     # 签名后向签名矩阵回归（自我维持，强于扩散0.02）
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
SIGN_PROB = 0.10               # 签名操作概率（从MOVE里扣，不削弱建造）
SUBSTEPS_PER_TICK = 6           # 每tick子步数（快速移动喷涂）
SUB_RAISE_AMOUNT = 5.0          # 子步RAISE幅度（单次小，多次覆盖）

# 物质状态机
STATE_TRACKING = "tracking"
STATE_STABLE = "stable_candidate"
STATE_TESTING = "testing"
STATE_CONFIRMED = "confirmed"
STATE_SIGNED = "signed"
STATE_DISSOLVED = "dissolved"

# 结构类型
TYPE_ROUND = "原生圆"          # 圆形度 > 0.75
TYPE_ELONGATED = "椭圆"        # 0.50 ~ 0.75
TYPE_IRREGULAR = "不规则"      # < 0.50

# 可视化
CMAP = "inferno"
COLOR_UNSIGNED = "#00ff88"     # 未签名物质
COLOR_SIGNED = "#ffd700"       # 已签名物质（金色）
COLOR_PARTICLE = "#ffffff"


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
# 物质结构
# ============================================================================
@dataclass
class Substance:
    uid: int
    cx: int
    cy: int
    matrix: np.ndarray              # 3×3 确认时快照
    birth_tick: int
    dissolve_tick: int = -1
    # v0.2 新增
    signature: str = ""             # 签名（粒子ID）
    signed_tick: int = -1           # 签名tick
    decay_mult: float = 1.0         # 衰减倍率（签名后=0.5）
    structure_type: str = ""        # 结构类型
    circularity: float = 0.0        # 圆形度

    @property
    def alive(self) -> bool:
        return self.dissolve_tick < 0

    @property
    def is_signed(self) -> bool:
        return self.signature != ""

    @property
    def total_energy(self) -> float:
        return float(np.sum(self.matrix))

    def sign(self, particle_id: str, tick: int, current_matrix: np.ndarray = None):
        self.signature = particle_id
        self.signed_tick = tick
        self.decay_mult = SIGNED_DECAY_MULT
        # 签名时更新基准矩阵——签名冻结的是当前状态，不是出生状态
        if current_matrix is not None:
            self.matrix = current_matrix.copy()

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "position": [int(self.cx), int(self.cy)],
            "birth_tick": self.birth_tick,
            "signed_tick": self.signed_tick,
            "signature": self.signature,
            "decay_multiplier": self.decay_mult,
            "structure_type": self.structure_type,
            "circularity": round(self.circularity, 4),
            "energy_sum": round(self.total_energy, 1),
            "matrix": [[round(float(v), 1) for v in row] for row in self.matrix],
            "alive": self.alive,
            "dissolve_tick": self.dissolve_tick if not self.alive else None,
        }


# ============================================================================
# 形状分析器
# ============================================================================
class ShapeAnalyzer:
    """对3×3能量矩阵做形状分类"""

    @staticmethod
    def analyze(matrix: np.ndarray) -> Tuple[str, float]:
        """
        返回 (structure_type, circularity)
        方法：二值化 → 计算面积/周长 → 等周比
        """
        # 二值化：以矩阵均值为阈值
        thresh = np.mean(matrix)
        binary = (matrix > thresh).astype(int)

        area = int(np.sum(binary))
        if area == 0 or area == 9:
            return TYPE_IRREGULAR, 0.0

        # 计算周长（4-邻接边界）
        perimeter = 0
        rows, cols = binary.shape
        for y in range(rows):
            for x in range(cols):
                if binary[y, x] == 0:
                    continue
                # 检查4邻居，不在区域内或邻居为0则算边界
                for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                    ny, nx = y + dy, x + dx
                    if ny < 0 or ny >= rows or nx < 0 or nx >= cols:
                        perimeter += 1
                    elif binary[ny, nx] == 0:
                        perimeter += 1

        if perimeter == 0:
            return TYPE_IRREGULAR, 0.0

        # 等周比: 4π × area / perimeter²（最大=1.0即正圆）
        circularity = (4.0 * np.pi * area) / (perimeter * perimeter)

        if circularity > 0.75:
            stype = TYPE_ROUND
        elif circularity > 0.50:
            stype = TYPE_ELONGATED
        else:
            stype = TYPE_IRREGULAR

        return stype, circularity


# ============================================================================
# 物质检测器（v0.2 增强版）
# ============================================================================
@dataclass
class HotspotTracker:
    cx: int
    cy: int
    history: deque = field(default_factory=lambda: deque(maxlen=STABILITY_WINDOW))
    state: str = STATE_TRACKING
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
                if tracker.state not in (STATE_TESTING, STATE_CONFIRMED, STATE_SIGNED):
                    tracker.history.append(e_sum)

        dead = []
        for key, trk in self.trackers.items():
            if key not in current_hotspots and trk.state not in (STATE_CONFIRMED, STATE_SIGNED):
                dead.append(key)
        for key in dead:
            del self.trackers[key]

    def check_stability(self, tick: int) -> None:
        for key, trk in list(self.trackers.items()):
            if trk.state != STATE_TRACKING:
                continue
            if len(trk.history) < STABILITY_WINDOW:
                continue
            recent = list(trk.history)[-100:]
            mean_val = np.mean(recent)
            if mean_val < 1e-6:
                continue
            variation = (np.max(recent) - np.min(recent)) / mean_val
            if variation < STABILITY_THRESHOLD:
                trk.state = STATE_STABLE
                trk.stable_mean = mean_val

    def run_interference_tests(self, field: np.ndarray, tick: int) -> None:
        for key, trk in list(self.trackers.items()):
            if trk.state != STATE_STABLE:
                continue
            trk.state = STATE_TESTING
            trk.test_start_tick = tick
            trk.pre_test_matrix = field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3].copy()
            trk.test_recovery_samples = 0

            rng = np.random.RandomState(tick)
            perturbation = rng.uniform(-PERTURB_AMPLITUDE, PERTURB_AMPLITUDE, (3, 3))
            field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3] += perturbation
            field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3] = np.maximum(
                field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3], 0
            )

    def check_recovery(self, field: np.ndarray, tick: int) -> None:
        for key, trk in list(self.trackers.items()):
            if trk.state != STATE_TESTING:
                continue
            if tick - trk.test_start_tick < PERTURB_RECOVERY:
                trk.test_recovery_samples += 1
                continue

            pre_sum = float(np.sum(trk.pre_test_matrix))
            current_sum = float(np.sum(
                field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3]
            ))
            if pre_sum < 1e-6:
                trk.state = STATE_TRACKING
                continue

            recovery_ratio = abs(current_sum - pre_sum) / pre_sum
            if recovery_ratio < STABILITY_THRESHOLD:
                matrix = field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3].copy()

                # v0.2: 形状分析
                stype, circularity = ShapeAnalyzer.analyze(matrix)

                sub = Substance(
                    uid=self.next_uid,
                    cx=trk.cx + 1,
                    cy=trk.cy + 1,
                    matrix=matrix,
                    birth_tick=tick,
                    structure_type=stype,
                    circularity=circularity,
                )
                self.next_uid += 1
                self.substances.append(sub)
                self._pending_confirms.append(sub)
                trk.state = STATE_CONFIRMED
            else:
                trk.state = STATE_TRACKING
                trk.history.clear()

    def check_dissolution(self, field: np.ndarray, tick: int) -> None:
        for sub in self.substances:
            if not sub.alive:
                continue
            current_sum = float(np.sum(
                field[sub.cy - 1:sub.cy + 2, sub.cx - 1:sub.cx + 2]
            ))
            birth_sum = float(np.sum(sub.matrix))
            if birth_sum < 1e-6:
                continue
            deviation = abs(current_sum - birth_sum) / birth_sum
            # 只在下降低于阈值时溶解（能量升高不算溶解）
            threshold = 0.50 if sub.is_signed else STABILITY_THRESHOLD * 2
            if current_sum < birth_sum * (1 - threshold):
                sub.dissolve_tick = tick

    def try_sign(self, particle_x: int, particle_y: int,
                 particle_id: str, tick: int,
                 field: np.ndarray) -> Optional[Substance]:
        """
        粒子在已确认物质区域内执行 SIGN。
        返回被签名的物质，或 None。
        签名时传入当前能量矩阵作为新基准。
        """
        for sub in self.substances:
            if not sub.alive or sub.is_signed:
                continue
            # 粒子在物质的3×3区域内
            if (sub.cx - 1 <= particle_x <= sub.cx + 1 and
                    sub.cy - 1 <= particle_y <= sub.cy + 1):
                current = field[sub.cy - 1:sub.cy + 2, sub.cx - 1:sub.cx + 2].copy()
                sub.sign(particle_id, tick, current)
                # 同步更新tracker状态
                key = (sub.cx - 1, sub.cy - 1)
                if key in self.trackers:
                    self.trackers[key].state = STATE_SIGNED
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

    def export_templates(self) -> List[dict]:
        """导出所有已签名物质为模板列表"""
        return [s.to_dict() for s in self.substances if s.is_signed]

    def save_templates(self, path: str) -> None:
        templates = self.export_templates()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "version": "v0.2",
                "total_signed": len(templates),
                "templates": templates,
            }, f, ensure_ascii=False, indent=2)


# ============================================================================
# 粒子（v0.2：快速移动喷涂）
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
        # 喷涂抖动：偶尔对角线
        self._ddx = [0, 1, 0, -1, 1, -1, -1, 1]
        self._ddy = [-1, 0, 1, 0, -1, -1, 1, 1]

    def _neighbors(self, field: np.ndarray):
        nbrs = []
        for d in range(4):
            nx = (self.x + self._dx[d]) % self.grid_size
            ny = (self.y + self._dy[d]) % self.grid_size
            nbrs.append((nx, ny, float(field[ny, nx])))
        return nbrs

    def _substep_move(self, field: np.ndarray):
        """子步移动：梯度跟随为主 + 随机抖动"""
        nbrs = self._neighbors(field)
        if self.rng.random() < 0.85:
            # 梯度跟随
            best = max(nbrs, key=lambda n: n[2])
            self.x, self.y = best[0], best[1]
        else:
            # 随机抖动（含对角线，产生更自然形状）
            d = self.rng.randint(0, 8)
            self.x = (self.x + self._ddx[d]) % self.grid_size
            self.y = (self.y + self._ddy[d]) % self.grid_size
        self.moves += 1

    def _in_signed_zone(self, detector: "SubstanceDetector") -> bool:
        for sub in detector.substances:
            if sub.is_signed and sub.alive:
                if (sub.cx - 1 <= self.x <= sub.cx + 1 and
                        sub.cy - 1 <= self.y <= sub.cy + 1):
                    return True
        return False

    def step(self, field: np.ndarray, detector: "SubstanceDetector",
             tick: int) -> Tuple[int, int, int, int, Optional[Substance]]:
        """
        快速移动喷涂：每tick执行 SUBSTEPS_PER_TICK 个子步。
        每个子步必移动，然后按概率 RAISE/LOWER/SIGN。
        返回 (moves, raises, lowers, signs, sign_result)
        """
        self.step_count += 1
        m, r, l, s = 0, 0, 0, 0
        sign_result = None

        for _ in range(SUBSTEPS_PER_TICK):
            # 必移动
            self._substep_move(field)
            m += 1

            # 按概率操作
            roll = self.rng.random()
            if roll < RAISE_PROB:
                field[self.y, self.x] += SUB_RAISE_AMOUNT
                self.raises += 1
                r += 1
            elif roll < RAISE_PROB + LOWER_PROB:
                if not self._in_signed_zone(detector):
                    field[self.y, self.x] = max(0, field[self.y, self.x] - LOWER_AMOUNT)
                    self.lowers += 1
                    l += 1
            elif roll < RAISE_PROB + LOWER_PROB + SIGN_PROB:
                self.signs += 1
                s += 1
                result = detector.try_sign(self.x, self.y, self.pid, tick, field)
                if result:
                    sign_result = result
            # else: 纯移动（35%）

        return m, r, l, s, sign_result


# ============================================================================
# 山海世界（v0.2：签名衰减）
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

        self.log: List[dict] = []
        self.total_moves = 0
        self.total_raises = 0
        self.total_lowers = 0
        self.total_signs = 0

    def _diffuse(self):
        # 全场扩散
        rolled = [
            np.roll(self.field, (0, 1)),
            np.roll(self.field, (0, -1)),
            np.roll(self.field, (1, 0)),
            np.roll(self.field, (-1, 0)),
        ]
        neighbor_avg = sum(rolled) / 4.0
        self.field += DIFFUSE_RATE * (neighbor_avg - self.field)

        # 向背景回归（签名区域跳过——它们有自己的平衡点）
        decay_field = DECAY_RATE * (self.field - BACKGROUND)
        self.field -= decay_field

        # 签名区域：向签名矩阵回归（自我维持）
        for sub in self.detector.substances:
            if sub.is_signed and sub.alive:
                y0, y1 = sub.cy - 1, sub.cy + 2
                x0, x1 = sub.cx - 1, sub.cx + 2
                # 撤销标准回归
                self.field[y0:y1, x0:x1] += DECAY_RATE * (
                    self.field[y0:y1, x0:x1] - BACKGROUND
                )
                # 向签名矩阵回归（自我维持——强于扩散）
                self.field[y0:y1, x0:x1] += SIGNED_SUSTAIN_RATE * (
                    sub.matrix - self.field[y0:y1, x0:x1]
                )

        self.field = np.maximum(self.field, 0)

    def step(self) -> Optional[List[Substance]]:
        self.tick += 1

        moves, raises, lowers, signs, sign_result = self.particle.step(
            self.field, self.detector, self.tick
        )

        self.total_moves += moves
        self.total_raises += raises
        self.total_lowers += lowers
        self.total_signs += signs

        self._diffuse()

        new_subs = self.detector.tick(self.field, self.tick)

        # 记录事件
        for sub in new_subs:
            self.log.append({
                "event": "substance_born",
                "tick": self.tick,
                "uid": sub.uid,
                "position": (sub.cx, sub.cy),
                "type": sub.structure_type,
                "circularity": sub.circularity,
                "energy_sum": sub.total_energy,
            })
        if sign_result:
            self.log.append({
                "event": "substance_signed",
                "tick": self.tick,
                "uid": sign_result.uid,
                "signature": sign_result.signature,
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
                        print(f"\n  ⛰️  物质 #{sub.uid} 诞生！"
                              f" tick={self.tick} "
                              f"({sub.cx},{sub.cy}) "
                              f"类型={sub.structure_type} "
                              f"○度={sub.circularity:.3f}")

            # 检查签名事件
            if self.log and self.log[-1].get("event") == "substance_signed":
                entry = self.log[-1]
                if verbose:
                    print(f"  🖊️  物质 #{entry['uid']} 已签名！"
                          f" tick={entry['tick']} "
                          f"签名={entry['signature']}")

            if verbose and self.tick % 5000 == 0:
                m = self.field.mean()
                std = self.field.std()
                print(f"  [{self.tick:6d}] μ={m:.2f} σ={std:.2f} "
                      f"物质={self.detector.alive_count} "
                      f"签名={self.detector.signed_count} "
                      f"SIGN={self.total_signs}")

            if self.detector.total_born >= target_substances:
                break
        return all_substances


# ============================================================================
# 可视化（v0.2：签名颜色区分）
# ============================================================================
class Visualizer:
    def __init__(self, world: ShanhaiWorld):
        self.world = world
        self.fig = None
        self.ax = None
        self.im = None
        self.paused = False
        self.speed = 1
        self._last_frame = 0.0
        self._fps = 0
        self.particle_dot = None
        self.sub_elements = []   # (rect, annotation)

    def setup(self):
        import matplotlib
        matplotlib.use("TkAgg")
        import matplotlib.pyplot as plt

        self.fig, self.ax = plt.subplots(figsize=(9, 8))
        self.fig.canvas.manager.set_window_title("山海 v0.2 — 铭刻")
        self.im = self.ax.imshow(
            self.world.field, cmap=CMAP, aspect="equal",
            vmin=0, vmax=100, origin="upper", interpolation="bilinear"
        )
        self.ax.set_title(f"山海 v0.2 · 铭刻 · tick=0")
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
            state = "⏸ 暂停" if self.paused else "▶ 继续"
            # 只在暂停时打印一次
        elif event.key == "q":
            print("\n  退出")
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

        # 绘制物质标记
        for sub in self.world.detector.substances:
            if not sub.alive:
                continue

            color = COLOR_SIGNED if sub.is_signed else COLOR_UNSIGNED
            lw = 2.5 if sub.is_signed else 1.5
            style = "-" if sub.is_signed else "--"

            rect = plt.Rectangle(
                (sub.cx - 1.5, sub.cy - 1.5), 3, 3,
                fill=False, edgecolor=color, linewidth=lw,
                linestyle=style,
            )
            self.ax.add_patch(rect)

            label = f"#{sub.uid}"
            if sub.is_signed:
                label += " ✍"
            ann = self.ax.annotate(
                label, (sub.cx, sub.cy - 2.2),
                color=color, fontsize=7, ha="center", fontweight="bold"
            )
            self.sub_elements.append((rect, ann))

        # 标题信息
        det = self.world.detector
        p = self.world.particle
        self.ax.set_title(
            f"山海 v0.2 · 铭刻 · tick={self.world.tick} · "
            f"物质={det.alive_count}(✍{det.signed_count}) · "
            f"粒子@({p.x},{p.y})"
        )

        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events()

    def run_interactive(self, max_ticks: int = 100000):
        self.setup()

        print(f"\n  山海 v0.2 — 铭刻 启动")
        print(f"  网格: {self.world.grid_size}×{self.world.grid_size}")
        print(f"  SIGN: 粒子移动到已确认物质区域时执行签名")
        print(f"  操作: [空格]暂停 [+/-]变速 [q]退出\n")

        step = 0
        while step < max_ticks and self.fig is not None:
            if not self.paused:
                for _ in range(self.speed):
                    if step >= max_ticks:
                        break
                    new_subs = self.world.step()
                    step += 1
                    if new_subs:
                        for sub in new_subs:
                            print(f"\n  ⛰️  物质 #{sub.uid} 诞生！"
                                  f" tick={self.world.tick} "
                                  f"({sub.cx},{sub.cy}) "
                                  f"类型={sub.structure_type}")
                    # 签名事件
                    log = self.world.log
                    if log and log[-1].get("event") == "substance_signed":
                        e = log[-1]
                        print(f"  🖊️  物质 #{e['uid']} 已签名！"
                              f" tick={e['tick']}")

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
    parser = argparse.ArgumentParser(description="山海 v0.2 — 铭刻")
    parser.add_argument("--no-viz", action="store_true", help="无图形模式")
    parser.add_argument("--ticks", type=int, default=100000, help="最大tick数")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--target", type=int, default=1, help="目标物质数")
    parser.add_argument("--templates", type=str, default="templates.json",
                        help="模板导出路径")
    args = parser.parse_args()

    print("=" * 60)
    print("  山海 v0.2 — 铭刻 · 签名与标记")
    print("  粒子可对稳定结构签名，签名后衰减减半")
    print("=" * 60)

    world = ShanhaiWorld(seed=args.seed)

    if args.no_viz:
        print(f"\n  无图形模式，目标: {args.target} 个物质\n")
        t0 = time.time()
        substances = world.run(max_ticks=args.ticks, verbose=True,
                               target_substances=args.target)
        elapsed = time.time() - t0

        print(f"\n  {'='*60}")
        print(f"  运行完成: {world.tick} tick, {elapsed:.1f}s")
        print(f"  物质总数: {world.detector.total_born}")
        print(f"  当前存活: {world.detector.alive_count}")
        print(f"  已签名:   {world.detector.signed_count}")
        print(f"  粒子操作: M={world.total_moves} R={world.total_raises} "
              f"L={world.total_lowers} SIGN={world.total_signs}")

        # 形状分布
        types = {}
        for sub in world.detector.substances:
            t = sub.structure_type
            types[t] = types.get(t, 0) + 1
        print(f"  形状分布: {types}")

        # 导出模板
        signed = world.detector.signed_count
        if signed > 0:
            tmpl_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), args.templates
            )
            world.detector.save_templates(tmpl_path)
            print(f"  模板已导出: {tmpl_path} ({signed}个签名结构)")
        else:
            print(f"  ⚠ 无签名结构，未导出模板")

        # 显示签名物质详情
        for sub in world.detector.substances:
            if not sub.is_signed:
                continue
            print(f"\n  ✍ 物质 #{sub.uid}:")
            print(f"    诞生 tick={sub.birth_tick} 签名 tick={sub.signed_tick}")
            print(f"    类型={sub.structure_type} ○度={sub.circularity:.3f}")
            print(f"    衰减倍率={sub.decay_mult}")
            for row in sub.matrix:
                print(f"      {[f'{v:.1f}' for v in row]}")
    else:
        viz = Visualizer(world)
        viz.run_interactive(max_ticks=args.ticks)

        # 退出时导出模板
        if world.detector.signed_count > 0:
            tmpl_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), args.templates
            )
            world.detector.save_templates(tmpl_path)
            print(f"\n  模板已导出: {tmpl_path}")


if __name__ == "__main__":
    main()
