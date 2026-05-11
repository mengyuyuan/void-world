#!/usr/bin/env python3
"""
山海 v0.6 — 变长二进制 · 物理涌现
====================================
v0.6 核心突破：
  结构窗口动态增长——融合时边长合并，上限仅受网格约束
  01排列天然产生不同物理——不是解释器，是扩散方程本身奖励等周比高的形状
  
  环形(1包0)→能量阱 | 密集块→扩散损失低 | 稀疏→高损耗

用法: python shanhai_v0.6.py
      python shanhai_v0.6.py --no-viz --target 1
"""

import numpy as np, time, sys, os, json
from collections import deque, Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

GRID_SIZE = 200                    # 巨量空间
INIT_ENERGY = 50.0; INIT_NOISE = 10.0; BACKGROUND = 50.0
DIFFUSE_RATE = 0.015; DECAY_RATE = 0.002  # 大空间慢扩散
MIN_WINDOW = 3; MAX_WINDOW = 195     # 物质窗口最小/最大边长
SUBSTEPS_PER_TICK = 4; SUB_RAISE_AMOUNT = 15.0  # 更多能量注入
SCAN_INTERVAL = 100; STABILITY_WINDOW = 80; STABILITY_THRESHOLD = 0.20
PERTURB_AMPLITUDE = 5.0; PERTURB_RECOVERY = 50
HOTSPOT_SIGMA = 2.0; MIN_HOTSPOT_SUM = 550.0
RAISE_PROB = 0.45; LOWER_PROB = 0.10; SIGN_PROB = 0.10

MAINTENANCE_COST = 0.003; COMPETITION_RATE = 0.04
COMPETITION_THRESHOLD = 300.0; FUSION_THRESHOLD = 1000.0
MERGE_DISTANCE = 8; COMPETITION_INTERVAL = 10
MAX_FIELD_ENERGY = 3000.0
DARK_REVEAL_CHANCE = 0.02
RESIDUE_PER_ACTION = 0.02; RESIDUE_DECAY = 0.001
SKELETON_SCAN_INTERVAL = 500; SKELETON_NODES_MAX = 20
PHASE_SOLID_BASE = 600; PHASE_LIQUID_BASE = 2000
PHASE_DRIFT_RATE = 0.1; PHASE_PLASMA_SPLIT = 3

class PhaseState(Enum): SOLID="固态"; LIQUID="液态"; PLASMA="等离子态"
CMAP="inferno"
COLOR_PHASE={PhaseState.SOLID:"#00ff88",PhaseState.LIQUID:"#4488ff",PhaseState.PLASMA:"#ff44ff"}

DARK_RULE_DEFS=[("高压区惩罚","high_energy",0.12,2.5),("低压区惩罚","low_energy",0.10,1.5),
("签名领地税","near_signed",0.15,3.0),("陡峭梯度过路费","gradient_steep",0.09,2.0),
("密集区拥挤费","cluster_dense",0.11,2.5),("边疆开拓税","edge_zone",0.07,1.5),
("中心区繁华税","center_zone",0.14,3.5),("角落荒地惩罚","corner_zone",0.06,1.0),
("对角线通行费","diagonal_zone",0.10,2.0),("偶数格点税","even_position",0.04,0.8),
("奇数格点税","odd_position",0.04,0.8),("远方征途惩罚","far_from_center",0.12,2.5)]

class XORShift32:
    def __init__(s,seed=42):s.state=max(seed&0xFFFFFFFF,1)
    def next(s):x=s.state;x^=(x<<13)&0xFFFFFFFF;x^=x>>17;x^=(x<<5)&0xFFFFFFFF;s.state=x;return x
    def randint(s,lo,hi):return lo+(s.next()%(hi-lo))
    def random(s):return s.next()/0x100000000

@dataclass
class Substance:
    uid:int; cx:int; cy:int; w:int=MIN_WINDOW; h:int=MIN_WINDOW
    matrix:np.ndarray=field(default=None); birth_tick:int=0
    dissolve_tick:int=-1; signature:str=""; signed_tick:int=-1
    phase:PhaseState=PhaseState.SOLID; phase_transition_tick:int=-1
    drift_vx:float=0.0; drift_vy:float=0.0
    plasma_fragments:List[int]=field(default_factory=list)
    fused_from:List[int]=field(default_factory=list); fusion_count:int=0
    # v0.6: 形状物理属性
    density:float=0.0         # 1的占比
    surface_area:int=0        # 边界长度(01过渡数)
    is_ring:bool=False        # 环形(1包0)

    @property
    def alive(s):return s.dissolve_tick<0
    @property
    def is_signed(s):return s.signature!=""
    @property
    def total_energy(s):return float(np.sum(s.matrix)) if s.matrix is not None else 0
    @property
    def area(s):return s.w*s.h
    @property
    def region(s):return slice(s.cy-s.h//2,s.cy+s.h//2+s.h%2),slice(s.cx-s.w//2,s.cx+s.w//2+s.w%2)

    def compute_shape(s,matrix):
        """从矩阵计算形状物理属性。01排列决定命运——不需要规则。"""
        thresh=np.mean(matrix);binary=(matrix>thresh).astype(int)
        s.density=float(np.mean(binary))
        # 比表面积: 01之间的边界数
        edges=0;r,c=binary.shape
        for y in range(r):
            for x in range(c):
                if y>0 and binary[y,x]!=binary[y-1,x]:edges+=1
                if x>0 and binary[y,x]!=binary[y,x-1]:edges+=1
        s.surface_area=edges
        # 环形检测: 1围住0
        if 0<binary.sum()<binary.size:
            try:
                from scipy.ndimage import binary_fill_holes
                filled=binary_fill_holes(binary.astype(bool)).astype(int)
                s.is_ring=(filled.sum()>binary.sum()+2)
            except ImportError:
                s.is_ring=False

    def update_phase(s,tick):
        e=s.total_energy;cells=s.area
        old=s.phase
        if e>PHASE_LIQUID_BASE*cells/9:s.phase=PhaseState.PLASMA
        elif e>PHASE_SOLID_BASE*cells/9:s.phase=PhaseState.LIQUID
        else:s.phase=PhaseState.SOLID
        if old!=s.phase:s.phase_transition_tick=tick

    def sign(s,pid,tick,cm=None):
        s.signature=pid;s.signed_tick=tick
        if cm is not None:s.matrix=cm.copy();s.compute_shape(cm)

    def to_dict(s):return{"uid":s.uid,"cx":s.cx,"cy":s.cy,"w":s.w,"h":s.h,
        "density":round(s.density,3),"surface":s.surface_area,"ring":s.is_ring,
        "birth_tick":s.birth_tick,"signed_tick":s.signed_tick,"phase":s.phase.value,
        "energy_sum":round(s.total_energy,1),"fusion_count":s.fusion_count,
        "fused_from":s.fused_from}

@dataclass
class DarkRule:
    rid:int;name:str;condition:str;trigger_prob:float;penalty:float;hit_count:int=0
    def check(s,px,py,field,emu):
        v=field[py,px]
        if s.condition=="high_energy" and v>emu*1.8:return True
        elif s.condition=="low_energy" and v<emu*0.6:return True
        elif s.condition in("edge_zone",):return(px<10 or px>89 or py<10 or py>89)
        elif s.condition in("center_zone",):return(30<px<70 and 30<py<70)
        elif s.condition in("corner_zone",):return((px<20 or px>79)and(py<20 or py>79))
        elif s.condition in("diagonal_zone",):return abs(px-py)<15
        elif s.condition in("even_position",):return(px+py)%2==0
        elif s.condition in("odd_position",):return(px+py)%2==1
        elif s.condition in("far_from_center",):return(px-50)**2+(py-50)**2>1600
        return False

class DarkConstraintSystem:
    def __init__(s,seed=999):
        s.rng=XORShift32(seed);s.total_triggers=0;s.revealed=[]
        s.rules=[DarkRule(rid=i,name=n,condition=c,trigger_prob=p,penalty=pe)for i,(n,c,p,pe)in enumerate(DARK_RULE_DEFS)]
    def apply(s,px,py,field,emu,ins,gs,cd):
        total=0.0
        for r in s.rules:
            if r.condition=="near_signed" and not ins:continue
            if r.condition=="gradient_steep" and not gs:continue
            if r.condition=="cluster_dense" and not cd:continue
            if r.condition not in("near_signed","gradient_steep","cluster_dense"):
                if not r.check(px,py,field,emu):continue
            if s.rng.random()<r.trigger_prob:total+=r.penalty;r.hit_count+=1
        s.total_triggers+=1 if total>0 else 0
        return total
    def try_reveal(s):
        if s.rng.random()<DARK_REVEAL_CHANCE:
            ur=[r for r in s.rules if r.rid not in s.revealed]
            if ur:rule=ur[s.rng.randint(0,len(ur))];s.revealed.append(rule.rid);return rule
        return None

class DarkSkeleton:
    def __init__(s,gs):s.gs=gs;s.residue=np.zeros((gs,gs),dtype=np.float64);s.nodes=[];s.edges=[]
    def deposit(s,x,y):s.residue[y,x]+=RESIDUE_PER_ACTION
    def decay(s):s.residue-=RESIDUE_DECAY;s.residue=np.maximum(s.residue,0)
    def scan_skeleton(s,field):
        s.nodes.clear();s.edges.clear()
        rgx=np.abs(np.diff(s.residue,axis=1,append=s.residue[:,-1:]));rgy=np.abs(np.diff(s.residue,axis=0,append=s.residue[-1:,:]))
        egx=np.abs(np.diff(field,axis=1,append=field[:,-1:]));egy=np.abs(np.diff(field,axis=0,append=field[-1:,:]))
        combined=(rgx+rgy)*0.3+(egx+egy)*0.7;th=np.percentile(combined,95)
        candidates=np.argwhere(combined>th)
        if len(candidates)>SKELETON_NODES_MAX:
            sc=combined[combined>th];idx=np.argsort(sc)[-SKELETON_NODES_MAX:];candidates=candidates[idx]
        for cy,cx in candidates:s.nodes.append((int(cx),int(cy)))
        for i,(x1,y1)in enumerate(s.nodes):
            for x2,y2 in s.nodes[i+1:]:
                if np.sqrt((x2-x1)**2+(y2-y1)**2)<15:s.edges.append((x1,y1,x2,y2))
        return len(s.nodes)
    def residue_attraction(s,x,y,field):
        bx,by,best=x,y,s.residue[y,x]
        for dy in range(-1,2):
            for dx in range(-1,2):
                if dx==0 and dy==0:continue
                nx,ny=(x+dx)%s.gs,(y+dy)%s.gs
                sc=s.residue[ny,nx]+0.3*field[ny,nx]
                if sc>best:best=sc;bx,by=nx,ny
        return bx,by

@dataclass
class HotspotTracker:
    cx:int;cy:int;w:int=MIN_WINDOW;h:int=MIN_WINDOW
    history:deque=field(default_factory=lambda:deque(maxlen=STABILITY_WINDOW))
    state:str="tracking";detected_tick:int=0;stable_mean:float=0.0
    pre_test_matrix:Optional[np.ndarray]=None;test_start_tick:int=0

class SubstanceDetector:
    def __init__(s,gs):
        s.gs=gs;s.trackers={};s.substances=[];s.next_uid=1;s._pending=[]
        s.total_fusions=0;s.total_energy_transferred=0.0;s.total_maintenance_consumed=0.0

    def _distance(s,a,b):return np.sqrt((a.cx-b.cx)**2+(a.cy-b.cy)**2)

    def _region_field(s,cx,cy,w,h,field):
        y0,y1=cy-h//2,cy+h//2+h%2;x0,x1=cx-w//2,cx+w//2+w%2
        return field[y0:y1,x0:x1]

    def _region_slice(s,cx,cy,w,h):
        return slice(cy-h//2,cy+h//2+h%2),slice(cx-w//2,cx+w//2+w%2)

    def scan_hotspots(s,field,tick):
        """用3×3快速扫描发现热点，确认后物质用动态窗口"""
        sz=s.gs-2;sums=np.zeros((sz,sz))
        for dy in range(3):
            for dx in range(3):sums+=field[dy:dy+sz,dx:dx+sz]
        th=float(np.mean(field))+HOTSPOT_SIGMA*float(np.std(field))
        hy,hx=np.where(np.logical_and(sums>th,sums>MIN_HOTSPOT_SUM))
        current=set()
        for i in range(len(hy)):
            cy,cx=int(hy[i]),int(hx[i]);current.add((cx,cy))
            es=float(sums[cy,cx])
            if(cx,cy)not in s.trackers:
                t=HotspotTracker(cx=cx,cy=cy,w=3,h=3,detected_tick=tick)
                t.history=deque([es],maxlen=STABILITY_WINDOW);s.trackers[(cx,cy)]=t
            else:
                t=s.trackers[(cx,cy)]
                if t.state not in("testing","confirmed","signed"):t.history.append(es)
        dead=[k for k,t in s.trackers.items()if k not in current and t.state not in("confirmed","signed")]
        for k in dead:del s.trackers[k]

    def check_stability(s,tick):
        for key,trk in list(s.trackers.items()):
            if trk.state!="tracking" or len(trk.history)<STABILITY_WINDOW:continue
            recent=list(trk.history)[-40:];m=np.mean(recent)
            if m<1e-6:continue
            if(np.max(recent)-np.min(recent))/m<STABILITY_THRESHOLD:trk.state="stable_candidate";trk.stable_mean=m

    def run_interference_tests(s,field,tick):
        for key,trk in list(s.trackers.items()):
            if trk.state!="stable_candidate":continue
            trk.state="testing";trk.test_start_tick=tick;w=max(1,trk.w);h=max(1,trk.h)
            y0=max(0,trk.cy-h//2);y1=min(s.gs,trk.cy+h//2+h%2)
            x0=max(0,trk.cx-w//2);x1=min(s.gs,trk.cx+w//2+w%2)
            if y1<=y0 or x1<=x0:trk.state="tracking";continue
            trk.pre_test_matrix=field[y0:y1,x0:x1].copy()
            p=np.random.RandomState(tick).uniform(-PERTURB_AMPLITUDE,PERTURB_AMPLITUDE,(y1-y0,x1-x0))
            field[y0:y1,x0:x1]+=p;field[y0:y1,x0:x1]=np.maximum(field[y0:y1,x0:x1],0)
    def check_recovery(s,field,tick):
        w=MIN_WINDOW;confirmed_this_tick=0
        for key,trk in list(s.trackers.items()):
            if trk.state!="testing":continue
            if confirmed_this_tick>=20:break  # 防爆发
            if tick-trk.test_start_tick<PERTURB_RECOVERY:continue
            w,h=trk.w,trk.h
            pre=float(np.sum(trk.pre_test_matrix))
            cur=float(np.sum(s._region_field(trk.cx,trk.cy,w,h,field)))
            if pre<1e-6:trk.state="tracking";continue
            if abs(cur-pre)/pre<STABILITY_THRESHOLD:
                matrix=s._region_field(trk.cx,trk.cy,w,h,field).copy()
                sub=Substance(uid=s.next_uid,cx=trk.cx,cy=trk.cy,w=w,h=h,matrix=matrix,birth_tick=tick)
                sub.compute_shape(matrix);s.next_uid+=1;s.substances.append(sub);s._pending.append(sub);trk.state="confirmed"
                confirmed_this_tick+=1
            else:trk.state="tracking";trk.history.clear()

    def apply_maintenance(s,field,tick):
        total=0.0
        for sub in s.substances:
            if not sub.alive:continue
            region=s._region_field(sub.cx,sub.cy,sub.w,sub.h,field);te=float(np.sum(region))
            cost=sub.area*MAINTENANCE_COST*(te/max(500.0,sub.area*50))
            if sub.is_signed:cost*=0.5
            # 密度奖励：高密度结构维护更便宜
            if sub.density>0.6:cost*=0.7
            if te>cost:reduction=cost*(region/max(te,1e-6))
            y0,y1=sub.cy-sub.h//2,sub.cy+sub.h//2+sub.h%2;x0,x1=sub.cx-sub.w//2,sub.cx+sub.w//2+sub.w%2
            field[y0:y1,x0:x1]-=reduction;field[y0:y1,x0:x1]=np.maximum(field[y0:y1,x0:x1],0);total+=cost
        s.total_maintenance_consumed+=total

    def apply_radiation(s,field,tick):
        """恒星辐射：大型结构向外发散能量，在周围形成能量环"""
        for sub in s.substances:
            if not sub.alive or sub.area<25:continue  # 小于5×5不辐射
            te=float(np.sum(s._region_field(sub.cx,sub.cy,sub.w,sub.h,field)))
            if te<sub.area*80:continue  # 能量不够不辐射
            # 辐射强度 = 面积 × 0.3%
            radiate=te*0.003
            # 在结构周围一圈均匀辐射
            r=max(sub.w,sub.h)//2+2
            for angle in np.linspace(0,2*np.pi,8,endpoint=False):
                rx=int(sub.cx+r*np.cos(angle));ry=int(sub.cy+r*np.sin(angle))
                rx=max(0,min(s.gs-1,rx));ry=max(0,min(s.gs-1,ry))
                field[ry,rx]+=radiate/8
            # 结构自身损失辐射能量
            y0,y1=sub.cy-sub.h//2,sub.cy+sub.h//2+sub.h%2;x0,x1=sub.cx-sub.w//2,sub.cx+sub.w//2+sub.w%2
            field[y0:y1,x0:x1]-=radiate/sub.area
            field[y0:y1,x0:x1]=np.maximum(field[y0:y1,x0:x1],0)
        s.total_maintenance_consumed+=radiate if 'radiate' in dir() else 0

    def apply_competition(s,field,tick):
        total=0.0;alive=[sub for sub in s.substances if sub.alive]
        if len(alive)<2:return
        buckets={}
        for sub in alive:
            bx,by=sub.cx//8,sub.cy//8;key=(bx,by)
            if key not in buckets:buckets[key]=[]
            buckets[key].append(sub)
        checked=set()
        for(bx,by),bucket in buckets.items():
            for dbx in range(-1,2):
                for dby in range(-1,2):
                    nkey=(bx+dbx,by+dby)
                    if nkey not in buckets:continue
                    for a in bucket:
                        for b in buckets[nkey]:
                            if a.uid>=b.uid:continue
                            pair=(a.uid,b.uid)
                            if pair in checked:continue
                            checked.add(pair)
                            if s._distance(a,b)>MERGE_DISTANCE+max(a.w,a.h)//2:continue
                            ea=float(np.sum(s._region_field(a.cx,a.cy,a.w,a.h,field)))
                            eb=float(np.sum(s._region_field(b.cx,b.cy,b.w,b.h,field)))
                            diff=abs(ea-eb)
                            if diff<COMPETITION_THRESHOLD:continue
                            transfer=diff*COMPETITION_RATE
                            if ea>eb:
                                # b→a: 从b均匀扣除，加到a
                                y0,y1=b.cy-b.h//2,b.cy+b.h//2+b.h%2;x0,x1=b.cx-b.w//2,b.cx+b.w//2+b.w%2
                                rb=s._region_field(b.cx,b.cy,b.w,b.h,field);tb=float(np.sum(rb))
                                if tb>transfer:
                                    field[y0:y1,x0:x1]-=transfer/tb*rb
                                    field[y0:y1,x0:x1]=np.maximum(field[y0:y1,x0:x1],0)
                                    y0a,y1a=a.cy-a.h//2,a.cy+a.h//2+a.h%2;x0a,x1a=a.cx-a.w//2,a.cx+a.w//2+a.w%2
                                    field[y0a:y1a,x0a:x1a]+=transfer/a.area
                                    total+=transfer
                            else:
                                # a→b
                                y0,y1=a.cy-a.h//2,a.cy+a.h//2+a.h%2;x0,x1=a.cx-a.w//2,a.cx+a.w//2+a.w%2
                                ra=s._region_field(a.cx,a.cy,a.w,a.h,field);ta=float(np.sum(ra))
                                if ta>transfer:
                                    field[y0:y1,x0:x1]-=transfer/ta*ra
                                    field[y0:y1,x0:x1]=np.maximum(field[y0:y1,x0:x1],0)
                                    y0b,y1b=b.cy-b.h//2,b.cy+b.h//2+b.h%2;x0b,x1b=b.cx-b.w//2,b.cx+b.w//2+b.w%2
                                    field[y0b:y1b,x0b:x1b]+=transfer/b.area
                                    total+=transfer
        s.total_energy_transferred+=total

    def apply_fusion(s,field,tick):
        alive=[sub for sub in s.substances if sub.alive]
        for sub in list(alive):
            if not sub.alive:continue
            es=float(np.sum(s._region_field(sub.cx,sub.cy,sub.w,sub.h,field)))
            if es>=FUSION_THRESHOLD:continue
            neighbors=[(s._distance(sub,n),n)for n in alive
                       if n.uid!=sub.uid and n.alive
                       and s._distance(sub,n)<=MERGE_DISTANCE+max(sub.w,sub.h)//2]
            if not neighbors:continue
            neighbors.sort(key=lambda x:x[0]);_,winner=neighbors[0]

            # v0.6: 融合扩张——胜者窗口扩大为包围两人的最小矩形
            w_left=min(sub.cx-sub.w//2,winner.cx-winner.w//2)
            w_right=max(sub.cx+sub.w//2+sub.w%2,winner.cx+winner.w//2+winner.w%2)
            h_top=min(sub.cy-sub.h//2,winner.cy-winner.h//2)
            h_bot=max(sub.cy+sub.h//2+sub.h%2,winner.cy+winner.h//2+winner.h%2)
            new_w=min(w_right-w_left,MAX_WINDOW)
            new_h=min(h_bot-h_top,MAX_WINDOW)
            new_cx=w_left+new_w//2;new_cy=h_top+new_h//2

            # 败者能量归胜者（映射到新窗口的正确位置）
            loser_e=s._region_field(sub.cx,sub.cy,sub.w,sub.h,field).copy()
            # 清零败者
            y0b,y1b=sub.cy-sub.h//2,sub.cy+sub.h//2+sub.h%2;x0b,x1b=sub.cx-sub.w//2,sub.cx+sub.w//2+sub.w%2
            field[y0b:y1b,x0b:x1b]=BACKGROUND
            # 胜者新窗口
            y0,y1=new_cy-new_h//2,new_cy+new_h//2+new_h%2;x0,x1=new_cx-new_w//2,new_cx+new_w//2+new_w%2
            # 找到败者在胜者新窗口中的偏移
            offset_y=y0b-y0;offset_x=x0b-x0
            # 确保不越界
            lh,lw=loser_e.shape
            py0=max(0,offset_y);px0=max(0,offset_x)
            py1=min(new_h,offset_y+lh);px1=min(new_w,offset_x+lw)
            ly0=max(0,-offset_y);lx0=max(0,-offset_x)
            ly1=ly0+(py1-py0);lx1=lx0+(px1-px0)
            field[y0+py0:y0+py1,x0+px0:x0+px1]+=loser_e[ly0:ly1,lx0:lx1]*0.6
            field[y0:y1,x0:x1]=np.minimum(field[y0:y1,x0:x1],MAX_FIELD_ENERGY)

            # 更新胜者
            winner.cx,winner.cy=new_cx,new_cy;winner.w,winner.h=new_w,new_h
            nm=s._region_field(winner.cx,winner.cy,winner.w,winner.h,field).copy()
            winner.matrix=nm;winner.compute_shape(nm)
            winner.fusion_count+=1;winner.fused_from.append(sub.uid)
            sub.dissolve_tick=tick;s.total_fusions+=1;s._pending.append(winner)

    def check_dissolution(s,field,tick):
        for sub in s.substances:
            if not sub.alive:continue
            cur=float(np.sum(s._region_field(sub.cx,sub.cy,sub.w,sub.h,field)))
            birth=float(np.sum(sub.matrix)) if sub.matrix is not None else cur+1
            if birth<1e-6:continue
            th=0.50 if sub.is_signed else STABILITY_THRESHOLD*2
            if cur<birth*(1-th):sub.dissolve_tick=tick

    def update_phases(s,tick):
        pl=[]
        for sub in s.substances:
            if not sub.is_signed or not sub.alive:continue
            sub.update_phase(tick)
            if sub.phase==PhaseState.PLASMA and not sub.plasma_fragments:pl.append(sub)
        return pl

    def handle_plasma_split(s,sub,field,tick):
        if sub.plasma_fragments:return[]
        frags=[];matrix=s._region_field(sub.cx,sub.cy,sub.w,sub.h,field)
        te=float(np.sum(matrix))
        if te<300:return[]
        rng=np.random.RandomState(tick)
        fw,fh=max(3,sub.w//2),max(3,sub.h//2)
        for i in range(3):
            fx=sub.cx+rng.randint(-sub.w,sub.w);fy=sub.cy+rng.randint(-sub.h,sub.h)
            fx=max(fw//2,min(s.gs-fw//2-1,fx));fy=max(fh//2,min(s.gs-fh//2-1,fy))
            fm=rng.uniform(0.3,0.7,(fh,fw))*matrix.mean()
            y0,y1=fy-fh//2,fy+fh//2+fh%2;x0,x1=fx-fw//2,fx+fw//2+fw%2
            field[y0:y1,x0:x1]+=fm;field[y0:y1,x0:x1]=np.maximum(field[y0:y1,x0:x1],0)
            frag=Substance(uid=s.next_uid,cx=fx,cy=fy,w=fw,h=fh,matrix=fm.copy(),birth_tick=tick,signature="",signed_tick=-1)
            frag.compute_shape(fm);s.next_uid+=1;s.substances.append(frag);s._pending.append(frag)
            frags.append(frag);sub.plasma_fragments.append(frag.uid)
        y0,y1=sub.cy-sub.h//2,sub.cy+sub.h//2+sub.h%2;x0,x1=sub.cx-sub.w//2,sub.cx+sub.w//2+sub.w%2
        field[y0:y1,x0:x1]*=0.5
        return frags

    def apply_liquid_drift(s,sub,field):
        if sub.phase!=PhaseState.LIQUID or not sub.alive:return
        region=s._region_field(sub.cx,sub.cy,sub.w,sub.h,field);gy,gx=np.gradient(region)
        tg=np.sqrt(np.mean(gx)**2+np.mean(gy)**2)
        if tg>0.01:sub.drift_vx+=PHASE_DRIFT_RATE*np.mean(gx)/tg;sub.drift_vy+=PHASE_DRIFT_RATE*np.mean(gy)/tg
        if abs(sub.drift_vx)>=1.0:
            sx=int(sub.drift_vx);ncx=max(sub.w//2,min(s.gs-sub.w//2-1,sub.cx+sx))
            if ncx!=sub.cx:
                old=s._region_field(sub.cx,sub.cy,sub.w,sub.h,field).copy()
                y0,y1=sub.cy-sub.h//2,sub.cy+sub.h//2+sub.h%2;x0,x1=sub.cx-sub.w//2,sub.cx+sub.w//2+sub.w%2
                field[y0:y1,x0:x1]=BACKGROUND
                y0,y1=sub.cy-sub.h//2,sub.cy+sub.h//2+sub.h%2;x0,x1=ncx-sub.w//2,ncx+sub.w//2+sub.w%2
                field[y0:y1,x0:x1]+=old;sub.cx=ncx
            sub.drift_vx-=sx
        if abs(sub.drift_vy)>=1.0:
            sy=int(sub.drift_vy);ncy=max(sub.h//2,min(s.gs-sub.h//2-1,sub.cy+sy))
            if ncy!=sub.cy:
                old=s._region_field(sub.cx,sub.cy,sub.w,sub.h,field).copy()
                y0,y1=sub.cy-sub.h//2,sub.cy+sub.h//2+sub.h%2;x0,x1=sub.cx-sub.w//2,sub.cx+sub.w//2+sub.w%2
                field[y0:y1,x0:x1]=BACKGROUND
                y0,y1=ncy-sub.h//2,ncy+sub.h//2+sub.h%2;x0,x1=sub.cx-sub.w//2,sub.cx+sub.w//2+sub.w%2
                field[y0:y1,x0:x1]+=old;sub.cy=ncy
            sub.drift_vy-=sy

    def try_sign(s,px,py,pid,tick,field):
        for sub in s.substances:
            if not sub.alive or sub.is_signed:continue
            if(sub.cx-sub.w//2<=px<=sub.cx+sub.w//2+sub.w%2 and sub.cy-sub.h//2<=py<=sub.cy+sub.h//2+sub.h%2):
                current=s._region_field(sub.cx,sub.cy,sub.w,sub.h,field).copy();sub.sign(pid,tick,current)
                key=(sub.cx-MIN_WINDOW//2,sub.cy-MIN_WINDOW//2)
                if key in s.trackers:s.trackers[key].state="signed"
                return sub
        return None

    def tick(s,field,tick):
        s._pending.clear();confirmed=set()
        if tick%SCAN_INTERVAL==0 and tick>0:s.scan_hotspots(field,tick);s.check_stability(tick);s.run_interference_tests(field,tick)
        s.check_recovery(field,tick)
        if tick%COMPETITION_INTERVAL==0:s.apply_maintenance(field,tick);s.apply_competition(field,tick);s.apply_fusion(field,tick);s.apply_radiation(field,tick)
        s.check_dissolution(field,tick)
        pl=s.update_phases(tick)
        for sub in pl:s.handle_plasma_split(sub,field,tick)
        for sub in s.substances:
            if sub.phase==PhaseState.LIQUID and sub.alive:s.apply_liquid_drift(sub,field)
        unique=[sub for sub in s._pending if sub.uid not in confirmed and not confirmed.add(sub.uid)]
        return unique

    @property
    def alive_count(s):return sum(1 for sub in s.substances if sub.alive)
    @property
    def total_born(s):return len(s.substances)
    @property
    def signed_count(s):return sum(1 for sub in s.substances if sub.is_signed and sub.alive)
    def phase_counts(s):
        c={p:0 for p in PhaseState}
        for sub in s.substances:
            if sub.alive:c[sub.phase]+=1
        return{k.value:v for k,v in c.items()}
    def export_templates(s):return[sub.to_dict()for sub in s.substances if sub.is_signed]
    def save_templates(s,path):
        with open(path,"w",encoding="utf-8")as f:json.dump({"version":"v0.6","total_signed":s.signed_count,"total_fusions":s.total_fusions,"templates":s.export_templates()},f,ensure_ascii=False,indent=2)

# Particle (unchanged from v0.5)
class Particle:
    def __init__(s,gs,pid="PC-1",seed=123456789):
        s.gs=gs;s.pid=pid;s.rng=XORShift32(seed);s.x=s.rng.randint(0,gs);s.y=s.rng.randint(0,gs)
        s.step_count=0;s.raises=0;s.lowers=0;s.signs=0;s.moves=0
        s._dx=[0,1,0,-1];s._dy=[-1,0,1,0];s._ddx=[0,1,0,-1,1,-1,-1,1];s._ddy=[-1,0,1,0,-1,-1,1,1]
        s.dark_hits=0;s.revealed_rules=[];s.last_penalty=0.0
    def _neighbors(s,field):
        return[((s.x+s._dx[d])%s.gs,(s.y+s._dy[d])%s.gs,float(field[(s.y+s._dy[d])%s.gs,(s.x+s._dx[d])%s.gs]))for d in range(4)]
    def _substep_move(s,field,skel):
        r=s.rng.random()
        if r<0.50:nbrs=s._neighbors(field);best=max(nbrs,key=lambda n:n[2]);s.x,s.y=best[0],best[1]
        elif r<0.75:s.x,s.y=skel.residue_attraction(s.x,s.y,field)
        else:d=s.rng.randint(0,8);s.x=(s.x+s._ddx[d])%s.gs;s.y=(s.y+s._ddy[d])%s.gs
        s.moves+=1
    def _in_signed_zone(s,detector):
        for sub in detector.substances:
            if sub.is_signed and sub.alive:
                if(sub.cx-sub.w//2<=s.x<=sub.cx+sub.w//2+sub.w%2 and sub.cy-sub.h//2<=s.y<=sub.cy+sub.h//2+sub.h%2):return True
        return False
    def _check_gradient_steep(s,field):
        if s.x<1 or s.x>=s.gs-1 or s.y<1 or s.y>=s.gs-1:return False
        return(abs(field[s.y,s.x+1]-field[s.y,s.x-1])+abs(field[s.y+1,s.x]-field[s.y-1,s.x]))>30
    def _check_cluster_dense(s,detector,radius=8):
        return sum(1 for sub in detector.substances if sub.alive and abs(sub.cx-s.x)<=radius and abs(sub.cy-s.y)<=radius)>=3
    def step(s,field,detector,skel,dark_rules,tick):
        s.step_count+=1;m=r=l=sg=0;sr=None;tdp=0.0
        emu=float(np.mean(field));ins=s._in_signed_zone(detector);gs=s._check_gradient_steep(field);cd=s._check_cluster_dense(detector)
        for _ in range(SUBSTEPS_PER_TICK):
            s._substep_move(field,skel);m+=1;skel.deposit(s.x,s.y)
            roll=s.rng.random()
            if roll<RAISE_PROB:
                pn=dark_rules.apply(s.x,s.y,field,emu,ins,gs,cd);tdp+=pn
                if pn>0:s.dark_hits+=1
                field[s.y,s.x]+=SUB_RAISE_AMOUNT;s.raises+=1;r+=1
            elif roll<RAISE_PROB+LOWER_PROB:
                pn=dark_rules.apply(s.x,s.y,field,emu,ins,gs,cd);tdp+=pn
                if pn>0:s.dark_hits+=1
                if not s._in_signed_zone(detector):field[s.y,s.x]=max(0,field[s.y,s.x]-10);s.lowers+=1;l+=1
            elif roll<RAISE_PROB+LOWER_PROB+SIGN_PROB:
                s.signs+=1;sg+=1;result=detector.try_sign(s.x,s.y,s.pid,tick,field)
                if result:sr=result
        s.last_penalty=tdp
        return m,r,l,sg,sr,tdp

class ShanhaiWorld:
    def __init__(s,seed=42):
        s.gs=GRID_SIZE;s.tick=0
        rng=np.random.RandomState(seed);s.field=np.full((GRID_SIZE,GRID_SIZE),INIT_ENERGY,dtype=np.float64)
        s.field+=rng.uniform(-INIT_NOISE,INIT_NOISE,(GRID_SIZE,GRID_SIZE));s.field=np.maximum(s.field,0)
        s.particle=Particle(GRID_SIZE,seed=seed+1);s.detector=SubstanceDetector(GRID_SIZE)
        s.dark_rules=DarkConstraintSystem(seed=seed+100);s.skeleton=DarkSkeleton(GRID_SIZE)
        s.log=[];s.total_moves=0;s.total_raises=0;s.total_lowers=0;s.total_signs=0;s.total_dark_penalty=0.0
    def _diffuse(s):
        rolled=[np.roll(s.field,(0,1)),np.roll(s.field,(0,-1)),np.roll(s.field,(1,0)),np.roll(s.field,(-1,0))]
        na=sum(rolled)/4.0;s.field+=DIFFUSE_RATE*(na-s.field)
        s.field-=DECAY_RATE*(s.field-BACKGROUND)
        for sub in s.detector.substances:
            if sub.is_signed and sub.alive:
                y0,y1=sub.cy-sub.h//2,sub.cy+sub.h//2+sub.h%2;x0,x1=sub.cx-sub.w//2,sub.cx+sub.w//2+sub.w%2
                s.field[y0:y1,x0:x1]+=DECAY_RATE*(s.field[y0:y1,x0:x1]-BACKGROUND)
                s.field[y0:y1,x0:x1]+=0.02*(sub.matrix-s.field[y0:y1,x0:x1])
        s.field=np.maximum(s.field,0);s.field=np.minimum(s.field,MAX_FIELD_ENERGY)
    def step(s):
        s.tick+=1
        m,r,l,sg,sr,dp=s.particle.step(s.field,s.detector,s.skeleton,s.dark_rules,s.tick)
        s.total_moves+=m;s.total_raises+=r;s.total_lowers+=l;s.total_signs+=sg;s.total_dark_penalty+=dp
        s.skeleton.decay();s._diffuse()
        if s.tick%SKELETON_SCAN_INTERVAL==0:s.skeleton.scan_skeleton(s.field)
        ns=s.detector.tick(s.field,s.tick)
        for sub in ns:s.log.append({"event":"substance_born","tick":s.tick,"uid":sub.uid,"w":sub.w,"h":sub.h,"density":sub.density,"ring":sub.is_ring})
        if sr:s.log.append({"event":"substance_signed","tick":s.tick,"uid":sr.uid})
        return ns if ns else None
    def run(s,max_ticks=100000,verbose=True,target=1):
        all_subs=[];announced=set()
        for _ in range(max_ticks):
            ns=s.step()
            if ns:
                for sub in ns:
                    if sub.uid not in announced:
                        announced.add(sub.uid);all_subs.append(sub)
                        if verbose:
                            tag="⚡"if sub.phase==PhaseState.PLASMA else("🔄"if sub.fusion_count>0 else"⛰️")
                            ring="💍"if sub.is_ring else""
                            xtra=f" {sub.w}x{sub.h}"+(f" 融合×{sub.fusion_count}"if sub.fusion_count>0 else"")
                            print(f"\n  {tag}{ring} #{sub.uid} t={s.tick} ({sub.cx},{sub.cy}) d={sub.density:.2f}{xtra}")
            if verbose and s.tick%5000==0:
                d=s.detector;sizes=Counter(f"{sub.w}x{sub.h}"for sub in d.substances if sub.alive)
                rings=sum(1 for sub in d.substances if sub.alive and sub.is_ring)
                print(f"  [{s.tick:6d}] μ={s.field.mean():.1f} 物质={d.alive_count} ✍={d.signed_count} "
                      f"融合={d.total_fusions} 环={rings} 尺寸={dict(sizes)}")
            if s.detector.total_born>=target:break
        return all_subs

class Visualizer:
    def __init__(s,w):s.w=w;s.fig=None;s.ax=None;s.im=None;s.paused=False;s.speed=1
    def setup(s):
        import matplotlib;matplotlib.use("TkAgg");import matplotlib.pyplot as plt
        s.fig,s.ax=plt.subplots(figsize=(10,9));s.fig.canvas.manager.set_window_title("山海 v0.6 — 变长二进制")
        s.im=s.ax.imshow(s.w.field,cmap=CMAP,aspect="equal",vmin=0,vmax=100,origin="upper",interpolation="bilinear")
        s.pt,=s.ax.plot([],[],"o",color="white",markersize=10,markeredgecolor="black",zorder=10)
        s.fig.canvas.mpl_connect("key_press_event",lambda e:(setattr(s,'paused',not s.paused)if e.key==" "else sys.exit(0)if e.key=="q"else setattr(s,'speed',min(s.speed*2,64))if e.key=="+"else setattr(s,'speed',max(s.speed//2,1))if e.key=="-"else None))
        plt.colorbar(s.im,ax=s.ax,label="能量");plt.ion();plt.show()
    def update(s):
        if s.fig is None:return
        import matplotlib.pyplot as plt;s.im.set_array(s.w.field);s.im.set_clim(vmin=0,vmax=max(100,float(np.max(s.w.field))))
        s.pt.set_data([s.w.particle.x],[s.w.particle.y])
        [r.remove()for r in getattr(s,'rects',[])if r in s.ax.patches]
        [a.remove()for a in getattr(s,'anns',[])]
        s.rects=[];s.anns=[]
        for sub in s.w.detector.substances:
            if not sub.alive:continue
            c=COLOR_PHASE.get(sub.phase,"#00ff88");lw=2+2*sub.is_signed+2*sub.is_ring;style="-"if sub.is_signed else"--"
            rect=plt.Rectangle((sub.cx-sub.w/2,sub.cy-sub.h/2),sub.w,sub.h,fill=False,edgecolor=c,linewidth=lw,linestyle=style)
            s.ax.add_patch(rect);s.rects.append(rect)
            lbl=f"#{sub.uid} {sub.w}x{sub.h}"
            if sub.fusion_count>0:lbl+=f"∪{sub.fusion_count}"
            if sub.is_ring:lbl="💍"+lbl
            ann=s.ax.annotate(lbl,(sub.cx,sub.cy-sub.h/2-1),color=c,fontsize=6,ha="center");s.anns.append(ann)
        d=s.w.detector;s.ax.set_title(f"山海 v0.6 · t={s.w.tick} · 物质={d.alive_count} ✍{d.signed_count} 融合={d.total_fusions}")
        s.fig.canvas.draw_idle();s.fig.canvas.flush_events()
    def run(s,max_ticks=100000):
        s.setup()
        for _ in range(max_ticks):
            if s.fig is None:break
            if not s.paused:
                for _ in range(s.speed):s.w.step()
                if s.w.tick%100==0:s.update()
            try:s.fig.canvas.flush_events()
            except:break
            time.sleep(0.01)

def main():
    import argparse
    p=argparse.ArgumentParser();p.add_argument("--no-viz",action="store_true");p.add_argument("--ticks",type=int,default=100000)
    p.add_argument("--seed",type=int,default=42);p.add_argument("--target",type=int,default=1)
    a=p.parse_args()
    print("="*60);print("  山海 v0.6 — 变长二进制 · 物理涌现");print(f"  窗口: {MIN_WINDOW}×{MIN_WINDOW}→动态增长 上限={MAX_WINDOW}");print("="*60)
    w=ShanhaiWorld(seed=a.seed)
    if a.no_viz:
        t0=time.time();subs=w.run(max_ticks=a.ticks,verbose=True,target=a.target)
        d=w.detector;el=time.time()-t0
        print(f"\n  tick={w.tick} ({el:.1f}s) born={d.total_born} alive={d.alive_count} signed={d.signed_count} fusions={d.total_fusions}")
        print(f"  相态: {d.phase_counts()}")
        sizes=Counter(f"{sub.w}x{sub.h}"for sub in d.substances if sub.alive)
        rings=sum(1 for sub in d.substances if sub.alive and sub.is_ring)
        print(f"  尺寸分布: {dict(sizes)}")
        print(f"  环形结构: {rings} 个")
        if d.substances:
            alive=[sub for sub in d.substances if sub.alive]
            dens=[sub.density for sub in alive];surf=[sub.surface_area for sub in alive]
            print(f"  密度: {min(dens):.3f}-{max(dens):.3f}  比表面积: {min(surf)}-{max(surf)}")
            fused=[sub for sub in d.substances if sub.fusion_count>0]
            if fused:
                print(f"\n  融合物质 ({len(fused)}):")
                for sub in fused[:8]:print(f"    #{sub.uid}: {sub.w}x{sub.h} ×{sub.fusion_count} ring={sub.is_ring} d={sub.density:.2f} E={sub.total_energy:.0f}")
    else:
        viz=Visualizer(w);viz.run(max_ticks=a.ticks)

if __name__=="__main__":main()
