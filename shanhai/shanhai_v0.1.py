#!/usr/bin/env python3
"""
山海 v0.1 — 孤工 · 单PC创世
================================
粒子在200×200能量场上移动+修改，系统自动识别第一个稳定结构（物质）。

物质诞生 = 三层递进判断：
  第一层 · 热点发现 — 3×3窗口总能量 > 全局均值+2σ
  第二层 · 时序验证 — 同一热点连续200 tick 波动<5%
  第三层 · 抗干扰    — 随机涨落±5后50 tick内恢复

粒子行为：xorshift32 随机源，70%梯度感知+30%随机探索
操作概率：70% MOVE / 20% RAISE / 10% LOWER
能量动力学：每tick全场扩散(邻域平均×0.1) + 衰减(-0.05)

用法: python shanhai_v0.1.py
      python shanhai_v0.1.py --no-viz   # 纯计算，不显示图形
"""

import numpy as np
import time
import sys
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ============================================================================
# 常量
# ============================================================================
GRID_SIZE = 100                # 能量场 100×100（单粒子可影响）
INIT_ENERGY = 50.0             # 初始能量均值 / 自然平衡态
INIT_NOISE = 10.0              # 初始随机噪声 ±10
BACKGROUND = 50.0              # 能量自然回归目标（宇宙背景）
DIFFUSE_RATE = 0.02            # 扩散系数（慢——让建造>抹平）
DECAY_RATE = 0.003             # 向背景回归（慢——结构更持久）
RAISE_AMOUNT = 15.0            # RAISE 操作幅度（强力建造）
LOWER_AMOUNT = 10.0            # LOWER 操作幅度
SCAN_INTERVAL = 100            # 热点扫描间隔(tick)
STABILITY_WINDOW = 200         # 时序验证窗口(tick)
STABILITY_THRESHOLD = 0.20     # 波动阈值 20%（容忍粒子游荡造成的自然涨落）
PERTURB_AMPLITUDE = 5.0        # 抗干扰测试涨落幅度
PERTURB_RECOVERY = 50          # 抗干扰恢复观察期(tick)
HOTSPOT_SIGMA = 2.0            # 热点 z-score 阈值
MIN_HOTSPOT_SUM = 550.0       # 热点绝对能量底线 (3×3总能量，背景50×9=450)
GRADIENT_FOLLOW = 0.95         # 梯度跟随概率（强吸引力——粒子忠诚于峰值）
MOVE_PROB = 0.45               # MOVE操作概率
RAISE_PROB = 0.45              # RAISE操作概率（大量建造）
LOWER_PROB = 0.10              # LOWER操作概率

# 物质状态机
STATE_TRACKING = "tracking"           # 追踪中
STATE_STABLE = "stable_candidate"     # 200 tick稳定，等待抗干扰
STATE_TESTING = "testing"             # 抗干扰测试中
STATE_CONFIRMED = "confirmed"         # 物质诞生 ✓
STATE_DISSOLVED = "dissolved"         # 已溶解

# 可视化颜色
CMAP = "inferno"                # 能量场配色
SUBSTANCE_COLOR = "#00ff88"     # 物质边框颜色
PARTICLE_COLOR = "#ffffff"      # 粒子颜色


# ============================================================================
# XORShift32 随机数生成器
# ============================================================================
class XORShift32:
    """xorshift32 — 快速、高质量、可复现"""
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
        """[lo, hi)"""
        return lo + (self.next() % (hi - lo))

    def random(self) -> float:
        """[0, 1)"""
        return self.next() / 0x100000000


# ============================================================================
# 物质记录
# ============================================================================
@dataclass
class Substance:
    """已确认的能量结构"""
    uid: int                       # 唯一ID
    cx: int                        # 中心x
    cy: int                        # 中心y
    matrix: np.ndarray             # 3×3能量矩阵（确认时快照）
    birth_tick: int                # 诞生tick
    dissolve_tick: int = -1        # 溶解tick（-1=存活中）

    @property
    def alive(self) -> bool:
        return self.dissolve_tick < 0

    @property
    def total_energy(self) -> float:
        return float(np.sum(self.matrix))


# ============================================================================
# 物质检测器
# ============================================================================
@dataclass
class HotspotTracker:
    """追踪一个候选热点区域"""
    cx: int                        # 中心x (3×3窗口左上角相对于场的坐标)
    cy: int                        # 中心y
    history: deque = field(default_factory=lambda: deque(maxlen=STABILITY_WINDOW))
    state: str = STATE_TRACKING
    detected_tick: int = 0         # 首次发现tick
    stable_mean: float = 0.0       # 确认稳定时的能量均值
    pre_test_matrix: Optional[np.ndarray] = None  # 抗干扰前快照
    test_start_tick: int = 0       # 抗干扰开始tick
    test_recovery_samples: int = 0 # 恢复期采样计数


class SubstanceDetector:
    """三层递进物质判断"""

    def __init__(self, grid_size: int):
        self.grid_size = grid_size
        self.trackers: Dict[Tuple[int, int], HotspotTracker] = {}
        self.substances: List[Substance] = []
        self.next_uid = 1
        self._pending_tests: List[Tuple[int, int, np.ndarray, float]] = []
        self._pending_confirms: List[Substance] = []

    def scan_hotspots(self, field: np.ndarray, tick: int) -> None:
        """
        第一层：扫描全场，找出所有 3×3 能量热点。
        热点条件：3×3总能量 > 全局均值 + HOTSPOT_SIGMA×全局标准差
        """
        # 高效计算所有 3×3 窗口的能量和
        sums = np.zeros((self.grid_size - 2, self.grid_size - 2))
        for dy in range(3):
            for dx in range(3):
                sums += field[dy:dy + self.grid_size - 2, dx:dx + self.grid_size - 2]

        global_mean = float(np.mean(field))
        global_std = float(np.std(field))
        threshold = global_mean + HOTSPOT_SIGMA * global_std

        # 找出所有超阈值的窗口（同时满足 z-score 和绝对底线）
        hot_y, hot_x = np.where((sums > threshold) & (sums > MIN_HOTSPOT_SUM))
        current_hotspots = set()
        for i in range(len(hot_y)):
            cy, cx = int(hot_y[i]), int(hot_x[i])
            # 3×3 窗口中心是 (cx+1, cy+1)，左上角是 (cx, cy)
            current_hotspots.add((cx, cy))
            e_sum = float(sums[cy, cx])

            if (cx, cy) not in self.trackers:
                # 新热点
                tracker = HotspotTracker(cx=cx, cy=cy, detected_tick=tick)
                tracker.history = deque([e_sum], maxlen=STABILITY_WINDOW)
                self.trackers[(cx, cy)] = tracker
            else:
                # 更新已有追踪器
                tracker = self.trackers[(cx, cy)]
                # 测试中的追踪器不更新（受干扰影响）
                if tracker.state not in (STATE_TESTING, STATE_CONFIRMED):
                    tracker.history.append(e_sum)

        # 清理不再活跃的追踪器
        dead = []
        for key, trk in self.trackers.items():
            if key not in current_hotspots and trk.state != STATE_CONFIRMED:
                dead.append(key)
        for key in dead:
            del self.trackers[key]

    def check_stability(self, tick: int) -> None:
        """
        第二层：检查追踪器的时序稳定性。
        用最近100个样本（滑动窗口）而非全部历史，
        容忍粒子游荡造成的长期偏移。
        条件：最近100样本中 (max-min)/mean < STABILITY_THRESHOLD
        """
        for key, trk in list(self.trackers.items()):
            if trk.state != STATE_TRACKING:
                continue
            if len(trk.history) < STABILITY_WINDOW:
                continue

            # 只用最近 100 个样本判断稳定性
            recent = list(trk.history)[-100:]
            mean_val = np.mean(recent)
            if mean_val < 1e-6:
                continue

            variation = (np.max(recent) - np.min(recent)) / mean_val
            if variation < STABILITY_THRESHOLD:
                trk.state = STATE_STABLE
                trk.stable_mean = mean_val

    def run_interference_tests(self, field: np.ndarray, tick: int) -> None:
        """
        第三层：对稳定候选进行抗干扰测试。
        在3×3区域施加 ±5 随机涨落，观察50 tick是否恢复。
        """
        cx, cy = 0, 0  # will be set
        for key, trk in list(self.trackers.items()):
            if trk.state != STATE_STABLE:
                continue

            # 进入测试状态
            trk.state = STATE_TESTING
            trk.test_start_tick = tick
            trk.pre_test_matrix = field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3].copy()
            trk.test_recovery_samples = 0

            # 施加随机涨落
            rng = np.random.RandomState(tick)  # 用tick做种子，可复现
            perturbation = rng.uniform(-PERTURB_AMPLITUDE, PERTURB_AMPLITUDE, (3, 3))
            field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3] += perturbation
            # 不能为负
            field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3] = np.maximum(
                field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3], 0
            )

    def check_recovery(self, field: np.ndarray, tick: int) -> None:
        """
        检查抗干扰测试的恢复情况。
        """
        for key, trk in list(self.trackers.items()):
            if trk.state != STATE_TESTING:
                continue

            if tick - trk.test_start_tick < PERTURB_RECOVERY:
                # 还在等待期，采样当前能量
                current_sum = float(np.sum(
                    field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3]
                ))
                trk.test_recovery_samples += 1
                continue

            # 恢复期结束，判断结果
            pre_sum = float(np.sum(trk.pre_test_matrix))
            current_sum = float(np.sum(
                field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3]
            ))

            if pre_sum < 1e-6:
                trk.state = STATE_TRACKING  # 降级
                continue

            recovery_ratio = abs(current_sum - pre_sum) / pre_sum
            if recovery_ratio < STABILITY_THRESHOLD:
                # 通过！物质诞生
                matrix = field[trk.cy:trk.cy + 3, trk.cx:trk.cx + 3].copy()
                sub = Substance(
                    uid=self.next_uid,
                    cx=trk.cx + 1,  # 中心
                    cy=trk.cy + 1,
                    matrix=matrix,
                    birth_tick=tick,
                )
                self.next_uid += 1
                self.substances.append(sub)
                self._pending_confirms.append(sub)
                trk.state = STATE_CONFIRMED
            else:
                # 未通过
                trk.state = STATE_TRACKING
                trk.history.clear()  # 清空历史重新追踪

    def check_dissolution(self, field: np.ndarray, tick: int) -> None:
        """
        检查已确认物质是否溶解。
        条件：3×3总能量偏离确认时均值 > 5%
        """
        for sub in self.substances:
            if not sub.alive:
                continue
            current_sum = float(np.sum(
                field[sub.cy - 1:sub.cy + 2, sub.cx - 1:sub.cx + 2]
            ))
            birth_sum = float(np.sum(sub.matrix))
            if birth_sum < 1e-6:
                continue
            if abs(current_sum - birth_sum) / birth_sum > STABILITY_THRESHOLD * 2:
                sub.dissolve_tick = tick

    def tick(self, field: np.ndarray, tick: int) -> List[Substance]:
        """
        每个tick调用，执行物质检测流水线。
        返回本轮新确认的物质列表。
        """
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


# ============================================================================
# 粒子
# ============================================================================
class Particle:
    """单粒子 — xorshift 随机源，梯度感知移动"""

    def __init__(self, grid_size: int, seed: int = 123456789):
        self.grid_size = grid_size
        self.rng = XORShift32(seed)
        self.x = self.rng.randint(0, grid_size)
        self.y = self.rng.randint(0, grid_size)
        self.step_count = 0
        self.raises = 0
        self.lowers = 0

        # 方向: 0=UP, 1=RIGHT, 2=DOWN, 3=LEFT
        self._dx = [0, 1, 0, -1]
        self._dy = [-1, 0, 1, 0]

    def _neighbors(self, field: np.ndarray):
        """返回四个邻居的 (x, y, energy) 列表"""
        nbrs = []
        for d in range(4):
            nx = (self.x + self._dx[d]) % self.grid_size
            ny = (self.y + self._dy[d]) % self.grid_size
            nbrs.append((nx, ny, float(field[ny, nx])))
        return nbrs

    def _gradient_move(self, field: np.ndarray):
        """70%向高能量方向移动，30%随机"""
        nbrs = self._neighbors(field)
        if self.rng.random() < GRADIENT_FOLLOW:
            # 向最高能量邻居移动
            best = max(nbrs, key=lambda n: n[2])
        else:
            best = nbrs[self.rng.randint(0, 4)]
        self.x, self.y = best[0], best[1]

    def apply(self, action: str, field: np.ndarray) -> None:
        """执行一次操作"""
        if action == "MOVE":
            self._gradient_move(field)
        elif action == "RAISE":
            field[self.y, self.x] += RAISE_AMOUNT
            self.raises += 1
        elif action == "LOWER":
            field[self.y, self.x] = max(0, field[self.y, self.x] - LOWER_AMOUNT)
            self.lowers += 1

    def random_action(self) -> str:
        """按概率选择操作"""
        r = self.rng.random()
        if r < MOVE_PROB:
            return "MOVE"
        elif r < MOVE_PROB + RAISE_PROB:
            return "RAISE"
        else:
            return "LOWER"

    def step(self, field: np.ndarray) -> str:
        """执行一步，返回执行的操作"""
        self.step_count += 1
        action = self.random_action()
        self.apply(action, field)
        return action


# ============================================================================
# 山海世界
# ============================================================================
class ShanhaiWorld:
    """山海世界 — 能量场 + 粒子 + 物质检测"""

    def __init__(self, seed: int = 42, grid_size: int = GRID_SIZE):
        self.grid_size = grid_size
        self.tick = 0

        # 能量场初始化
        rng = np.random.RandomState(seed)
        self.field = np.full((grid_size, grid_size), INIT_ENERGY, dtype=np.float64)
        self.field += rng.uniform(-INIT_NOISE, INIT_NOISE, (grid_size, grid_size))
        self.field = np.maximum(self.field, 0)

        # 粒子
        self.particle = Particle(grid_size, seed=seed + 1)

        # 物质检测器
        self.detector = SubstanceDetector(grid_size)

        # 日志
        self.log: List[dict] = []  # 重要事件

        # 统计
        self.total_raises = 0
        self.total_lowers = 0
        self.total_moves = 0

    def _diffuse(self):
        """全场能量扩散 + 向背景回归"""
        # 扩散：每个格点向其邻居平均靠拢 DIFFUSE_RATE
        rolled = [
            np.roll(self.field, (0, 1)),   # 上
            np.roll(self.field, (0, -1)),  # 下
            np.roll(self.field, (1, 0)),   # 左
            np.roll(self.field, (-1, 0)),  # 右
        ]
        neighbor_avg = sum(rolled) / 4.0
        self.field += DIFFUSE_RATE * (neighbor_avg - self.field)

        # 向背景回归（高于背景→衰减，低于背景→自然补充）
        self.field -= DECAY_RATE * (self.field - BACKGROUND)
        self.field = np.maximum(self.field, 0)

    def step(self) -> Optional[List[Substance]]:
        """
        演化一步。返回本轮新确认的物质列表（如果有）。
        """
        self.tick += 1

        # 粒子操作
        action = self.particle.step(self.field)
        if action == "MOVE":
            self.total_moves += 1
        elif action == "RAISE":
            self.total_raises += 1
        else:
            self.total_lowers += 1

        # 能量动力学
        self._diffuse()

        # 物质检测
        new_subs = self.detector.tick(self.field, self.tick)

        # 记录物质诞生
        for sub in new_subs:
            self.log.append({
                "event": "substance_born",
                "tick": self.tick,
                "uid": sub.uid,
                "position": (sub.cx, sub.cy),
                "energy_sum": sub.total_energy,
                "matrix": sub.matrix.tolist(),
            })

        return new_subs if new_subs else None

    def run(self, max_ticks: int = 100000, verbose: bool = True,
            target_substances: int = 1) -> List[Substance]:
        """
        运行演化直到达到目标物质数或最大tick。
        """
        all_substances = []
        for _ in range(max_ticks):
            new_subs = self.step()

            if new_subs:
                all_substances.extend(new_subs)
                if verbose:
                    for sub in new_subs:
                        print(f"\n  ⛰️  物质 #{sub.uid} 诞生！"
                              f" tick={self.tick} "
                              f"坐标=({sub.cx},{sub.cy}) "
                              f"能量={sub.total_energy:.1f}")

            if verbose and self.tick % 5000 == 0:
                m = self.field.mean()
                std = self.field.std()
                alive = self.detector.alive_count
                print(f"  [{self.tick:6d}] μ={m:.2f} σ={std:.2f} "
                      f"热点={len(self.detector.trackers)} "
                      f"物质={alive} "
                      f"粒子@({self.particle.x},{self.particle.y})")

            if self.detector.total_born >= target_substances:
                break

        return all_substances


# ============================================================================
# 可视化
# ============================================================================
class Visualizer:
    """matplotlib 实时能量场热力图"""

    def __init__(self, world: ShanhaiWorld):
        self.world = world
        self.fig = None
        self.ax = None
        self.im = None
        self.paused = False
        self.speed = 1  # tick per frame
        self._last_frame = 0.0
        self._fps = 0

    def setup(self):
        import matplotlib
        matplotlib.use("TkAgg")
        import matplotlib.pyplot as plt

        self.fig, self.ax = plt.subplots(figsize=(8, 7))
        self.fig.canvas.manager.set_window_title("山海 v0.1 — 能量场")
        self.im = self.ax.imshow(
            self.world.field, cmap=CMAP, aspect="equal",
            vmin=0, vmax=100, origin="upper", interpolation="bilinear"
        )
        self.ax.set_title(f"山海 v0.1 · tick=0 · 物质=0")
        self.ax.set_xlabel("x")
        self.ax.set_ylabel("y")
        plt.colorbar(self.im, ax=self.ax, label="能量")

        # 粒子标记
        self.particle_dot, = self.ax.plot(
            [], [], "o", color=PARTICLE_COLOR, markersize=8, markeredgecolor="black"
        )

        # 物质标记（矩形）
        self.sub_rects = []

        # 键盘事件
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)

        plt.ion()
        plt.show()

    def _on_key(self, event):
        if event.key == " ":
            self.paused = not self.paused
            print(f"\n  {'⏸ 暂停' if self.paused else '▶ 继续'}")
        elif event.key == "q":
            print("\n  退出")
            sys.exit(0)
        elif event.key == "+":
            self.speed = min(self.speed * 2, 64)
            print(f"\n  速度: {self.speed}x")
        elif event.key == "-":
            self.speed = max(self.speed // 2, 1)
            print(f"\n  速度: {self.speed}x")

    def update(self):
        if self.fig is None:
            return

        import matplotlib.pyplot as plt

        # 更新图像
        self.im.set_array(self.world.field)
        self.im.set_clim(vmin=0, vmax=max(100, float(np.max(self.world.field))))

        # 更新粒子位置
        self.particle_dot.set_data([self.world.particle.x], [self.world.particle.y])

        # 清除旧物质标记
        for rect in self.sub_rects:
            rect.remove()
        self.sub_rects.clear()

        # 绘制物质标记
        for sub in self.world.detector.substances:
            if not sub.alive:
                continue
            rect = plt.Rectangle(
                (sub.cx - 1.5, sub.cy - 1.5), 3, 3,
                fill=False, edgecolor=SUBSTANCE_COLOR, linewidth=2
            )
            self.ax.add_patch(rect)
            self.sub_rects.append(rect)
            self.ax.annotate(
                f"#{sub.uid}", (sub.cx, sub.cy - 2),
                color=SUBSTANCE_COLOR, fontsize=8, ha="center"
            )

        # 更新标题
        self.ax.set_title(
            f"山海 v0.1 · tick={self.world.tick} · "
            f"物质={self.world.detector.alive_count}/{self.world.detector.total_born} · "
            f"热点={len(self.world.detector.trackers)}"
        )

        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events()

        # FPS 控制
        now = time.time()
        if self._last_frame > 0:
            self._fps = 0.9 * self._fps + 0.1 / (now - self._last_frame)
        self._last_frame = now

    def run_interactive(self, max_ticks: int = 100000):
        """交互式运行"""
        self.setup()

        print(f"\n  山海 v0.1 启动")
        print(f"  网格: {self.world.grid_size}×{self.world.grid_size}")
        print(f"  初始能量: μ={INIT_ENERGY}±{INIT_NOISE}")
        print(f"  粒子初始位置: ({self.world.particle.x}, {self.world.particle.y})")
        print(f"  操作: [空格]暂停 [+/-]变速 [q]退出")
        print(f"  等待第一个稳定结构...\n")

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
                                  f"坐标=({sub.cx},{sub.cy}) "
                                  f"能量={sub.total_energy:.1f}")

                if self.world.tick % 100 == 0:
                    self.update()

            # 处理 GUI 事件
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
    parser = argparse.ArgumentParser(description="山海 v0.1 — 孤工创世")
    parser.add_argument("--no-viz", action="store_true", help="无图形模式")
    parser.add_argument("--ticks", type=int, default=50000, help="最大tick数")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--target", type=int, default=1, help="目标物质数")
    args = parser.parse_args()

    print("=" * 60)
    print("  山海 v0.1 — 孤工 · 单PC创世")
    print("  万物以形相生，能量为海，结构为山")
    print("=" * 60)

    world = ShanhaiWorld(seed=args.seed)

    if args.no_viz:
        # 纯计算模式
        print(f"\n  无图形模式，目标: {args.target} 个物质\n")
        t0 = time.time()
        substances = world.run(max_ticks=args.ticks, verbose=True,
                               target_substances=args.target)
        elapsed = time.time() - t0

        print(f"\n  {'='*60}")
        print(f"  运行完成: {world.tick} tick, {elapsed:.1f}s")
        print(f"  物质总数: {world.detector.total_born}")
        print(f"  当前存活: {world.detector.alive_count}")
        print(f"  粒子操作: MOVE={world.total_moves} "
              f"RAISE={world.total_raises} LOWER={world.total_lowers}")

        if substances:
            for sub in substances:
                print(f"\n  物质 #{sub.uid}:")
                print(f"    诞生 tick={sub.birth_tick}")
                print(f"    坐标=({sub.cx},{sub.cy})")
                print(f"    3×3能量矩阵:")
                for row in sub.matrix:
                    print(f"      {[f'{v:.1f}' for v in row]}")
    else:
        # 交互式可视化
        viz = Visualizer(world)
        viz.run_interactive(max_ticks=args.ticks)


if __name__ == "__main__":
    main()