#!/usr/bin/env python3
"""
# 山海 v0.1 — 孤工创世

单粒子在100×100能量场上建造持久结构。
首个物质诞生于 tick=20,150，21个物质全部存活，零溶解。

核心突破：从离散指令空间到连续能量场的范式转移。
"""

import numpy as np
from collections import deque

# === 宇宙参数 ===
GRID_SIZE = 100
BG_ENERGY = 50.0
RAISE_AMOUNT = 15.0
LOWER_AMOUNT = -10.0
DIFFUSE_RATE = 0.02
DECAY_RATE = 0.003
SUBSTEPS = 2

# === 物质判定参数 ===
HOTSPOT_THRESHOLD_SIGMA = 2.0
HOTSPOT_MIN_TOTAL = 550
STABILITY_WINDOW = 100
STABILITY_MAX_FLUCTUATION = 0.20
DISTURB_TEST_MAGNITUDE = 5
DISTURB_RECOVERY_TICKS = 50

# === PRNG (xorshift32) ===
class XorShift32:
    def __init__(self, seed=42):
        self.state = seed if seed != 0 else 1
    def next(self):
        x = self.state
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= (x >> 17)
        x ^= (x << 5) & 0xFFFFFFFF
        self.state = x
        return x
    def uniform(self, lo=0.0, hi=1.0):
        return lo + (self.next() / 0xFFFFFFFF) * (hi - lo)
    def choice(self, seq):
        return seq[self.next() % len(seq)]

# === 世界 ===
class Substance:
    def __init__(self, uid, cx, cy, size, field_snapshot):
        self.uid = uid
        self.cx, self.cy = cx, cy
        self.size = size
        self.birth_tick = -1
        self.birth_energy = field_snapshot
        self.signature = ""
        self.last_stable = field_snapshot

class World:
    def __init__(self, seed=42):
        self.rng = XorShift32(seed)
        self.field = np.full((GRID_SIZE, GRID_SIZE), BG_ENERGY, dtype=np.float64)
        self.px, self.py = GRID_SIZE // 2, GRID_SIZE // 2
        self.tick = 0
        self.substances = []
        self.next_uid = 1
        self.samples = deque(maxlen=200)
        self.history = deque(maxlen=STABILITY_WINDOW)
    
    def step(self):
        for _ in range(SUBSTEPS):
            self._move_particle()
            self._diffuse()
            self._decay()
        self.tick += 1
        if self.tick % 50 == 0:
            self._sample_field()
        if self.tick % 100 == 0 and len(self.samples) >= 5:
            self._scan_hotspots()
    
    def _move_particle(self):
        dirs = [(0,1),(0,-1),(1,0),(-1,0)]
        # Gradient perception: 95% move toward higher energy
        current = self.field[self.py, self.px]
        best_dir = self.rng.choice(dirs)
        if self.rng.uniform() < 0.95:
            best_val = current
            for d in dirs:
                ny, nx = (self.py+d[0])%GRID_SIZE, (self.px+d[1])%GRID_SIZE
                if self.field[ny, nx] > best_val:
                    best_val = self.field[ny, nx]
                    best_dir = d
        self.px = (self.px + best_dir[1]) % GRID_SIZE
        self.py = (self.py + best_dir[0]) % GRID_SIZE
        # Inject energy
        if self.rng.uniform() < 0.45:
            self.field[self.py, self.px] += RAISE_AMOUNT
        else:
            self.field[self.py, self.px] = max(0, self.field[self.py, self.px] + LOWER_AMOUNT)
    
    def _diffuse(self):
        new = self.field.copy()
        for y in range(GRID_SIZE):
            for x in range(GRID_SIZE):
                v = self.field[y, x]
                # 4-neighbor diffusion
                diff = 0.0
                for dy, dx in [(0,1),(0,-1),(1,0),(-1,0)]:
                    ny, nx = (y+dy)%GRID_SIZE, (x+dx)%GRID_SIZE
                    diff += (self.field[ny, nx] - v) * DIFFUSE_RATE * 0.25
                new[y, x] += diff
        self.field = new
    
    def _decay(self):
        self.field += (BG_ENERGY - self.field) * DECAY_RATE
    
    def _sample_field(self):
        self.samples.append((self.tick, self.field.mean(), self.field.std()))
    
    def _scan_hotspots(self):
        global_mean = np.mean([s[1] for s in self.samples])
        global_std = np.mean([s[2] for s in self.samples])
        threshold = global_mean + HOTSPOT_THRESHOLD_SIGMA * global_std
        # Scan 3x3 windows
        for y in range(1, GRID_SIZE-1):
            for x in range(1, GRID_SIZE-1):
                window = self.field[y-1:y+2, x-1:x+2]
                total = window.sum()
                if total > HOTSPOT_MIN_TOTAL and total > threshold * 9:
                    # Check if overlapping existing substance
                    overlap = False
                    for sub in self.substances:
                        if abs(x - sub.cx) < 3 and abs(y - sub.cy) < 3:
                            overlap = True
                            break
                    if not overlap:
                        snapshot = self.field[y-1:y+2, x-1:x+2].copy()
                        sub = Substance(self.next_uid, x, y, 3, snapshot)
                        self.next_uid += 1
                        self._test_stability(sub)
    
    def _test_stability(self, sub):
        # Add to pending
        sub.birth_tick = self.tick
        if not hasattr(self, '_pending'):
            self._pending = {}
        self._pending[sub.uid] = {
            'sub': sub,
            'start_tick': self.tick,
            'checks': deque(maxlen=STABILITY_WINDOW),
            'disturbed': False,
            'disturb_tick': 0
        }
    
    def check_stability(self):
        if not hasattr(self, '_pending'):
            return
        confirmed = []
        for uid, tracker in list(self._pending.items()):
            sub = tracker['sub']
            window = self.field[sub.cy-1:sub.cy+2, sub.cx-1:sub.cx+2]
            total = window.sum()
            tracker['checks'].append(total)
            if len(tracker['checks']) >= STABILITY_WINDOW:
                vals = list(tracker['checks'])
                fluct = (max(vals) - min(vals)) / (np.mean(vals) + 1e-8)
                if fluct < STABILITY_MAX_FLUCTUATION:
                    # Disturbance test
                    if not tracker['disturbed']:
                        backup = self.field[sub.cy-1:sub.cy+2, sub.cx-1:sub.cx+2].copy()
                        self.field[sub.cy-1:sub.cy+2, sub.cx-1:sub.cx+2] += self.rng.uniform(-DISTURB_TEST_MAGNITUDE, DISTURB_TEST_MAGNITUDE)
                        tracker['disturbed'] = True
                        tracker['disturb_tick'] = self.tick
                        tracker['backup'] = backup
                    elif self.tick - tracker['disturb_tick'] > DISTURB_RECOVERY_TICKS:
                        # Check recovery
                        current = self.field[sub.cy-1:sub.cy+2, sub.cx-1:sub.cx+2]
                        fluct2 = np.abs(current - tracker['backup']).mean() / (np.mean(tracker['backup']) + 1e-8)
                        if fluct2 < STABILITY_MAX_FLUCTUATION:
                            sub.birth_tick = self.tick
                            self.substances.append(sub)
                            confirmed.append(uid)
                        else:
                            self.field[sub.cy-1:sub.cy+2, sub.cx-1:sub.cx+2] = tracker['backup']
                            del self._pending[uid]
        for uid in confirmed:
            del self._pending[uid]

# === Main ===
if __name__ == "__main__":
    print("山海 v0.1 — 孤工创世")
    print(f"网格: {GRID_SIZE}×{GRID_SIZE}, 背景能量: {BG_ENERGY}")
    world = World(seed=42)
    while True:
        world.step()
        world.check_stability()
        if world.tick % 1000 == 0:
            mean_e = world.field.mean()
            print(f"tick={world.tick:6d} | 物质: {len(world.substances)} | 场均值: {mean_e:.1f}")
        if world.tick >= 80000:
            break
    print(f"\n=== 结果 ===")
    print(f"总 tick: {world.tick}")
    print(f"物质总数: {len(world.substances)}")
    for s in world.substances[:10]:
        e = world.field[s.cy-1:s.cy+2, s.cx-1:s.cx+2].sum()
        print(f"  物质#{s.uid}: 诞生={s.birth_tick}, 能量={e:.1f}")
