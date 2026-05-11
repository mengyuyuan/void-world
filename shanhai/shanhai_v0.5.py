#!/usr/bin/env python3
"""
山海 v0.5 — 大空间 · 融合纪元
================================
v0.5 核心升级：
  物质窗口 3×3 → 5×5（形状空间 9→25 格）
  融合参数调优（更近距、更低阈值、更易融合）
  保留 v0.4 全部机制：暗约束+暗骨架+相变+竞争+维持

用法: python shanhai_v0.5.py
      python shanhai_v0.5.py --no-viz --target 1
"""

import numpy as np, time, sys, os, json
from collections import deque, Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

WINDOW = 5                       # 物质窗口大小（5×5）
GRID_SIZE = 100
INIT_ENERGY = 50.0; INIT_NOISE = 10.0; BACKGROUND = 50.0
DIFFUSE_RATE = 0.02; DECAY_RATE = 0.003
SUBSTEPS_PER_TICK = 3; SUB_RAISE_AMOUNT = 12.0
SCAN_INTERVAL = 100; STABILITY_WINDOW = 100; STABILITY_THRESHOLD = 0.20
PERTURB_AMPLITUDE = 5.0; PERTURB_RECOVERY = 50
HOTSPOT_SIGMA = 2.0; MIN_HOTSPOT_SUM = 1500.0
RAISE_PROB = 0.45; LOWER_PROB = 0.10; SIGN_PROB = 0.10

MAINTENANCE_COST = 0.005; COMPETITION_RATE = 0.08
COMPETITION_THRESHOLD = 200.0; FUSION_THRESHOLD = 800.0
MERGE_DISTANCE = 5; COMPETITION_INTERVAL = 10
MAX_FIELD_ENERGY = 1500.0
DARK_REVEAL_CHANCE = 0.02
RESIDUE_PER_ACTION = 0.02; RESIDUE_DECAY = 0.001
SKELETON_SCAN_INTERVAL = 500; SKELETON_NODES_MAX = 20
PHASE_SOLID_ENERGY = 1500; PHASE_LIQUID_ENERGY = 3000
PHASE_DRIFT_RATE = 0.1; PHASE_PLASMA_SPLIT = 3

DARK_RULE_DEFS = [("高压区惩罚","high_energy",0.12,2.5),("低压区惩罚","low_energy",0.10,1.5),
("签名领地税","near_signed",0.15,3.0),("陡峭梯度过路费","gradient_steep",0.09,2.0),
("密集区拥挤费","cluster_dense",0.11,2.5),("边疆开拓税","edge_zone",0.07,1.5),
("中心区繁华税","center_zone",0.14,3.5),("角落荒地惩罚","corner_zone",0.06,1.0),
("对角线通行费","diagonal_zone",0.10,2.0),("偶数格点税","even_position",0.04,0.8),
("奇数格点税","odd_position",0.04,0.8),("远方征途惩罚","far_from_center",0.12,2.5)]

class PhaseState(Enum): SOLID="固态"; LIQUID="液态"; PLASMA="等离子态"
TYPE_ROUND="原生圆"; TYPE_ELONGATED="椭圆"; TYPE_IRREGULAR="不规则"
CMAP="inferno"
COLOR_PHASE={PhaseState.SOLID:"#00ff88",PhaseState.LIQUID:"#4488ff",PhaseState.PLASMA:"#ff44ff"}

class XORShift32:
    def __init__(s,seed=42): s.state=max(seed&0xFFFFFFFF,1)
    def next(s): x=s.state;x^=(x<<13)&0xFFFFFFFF;x^=x>>17;x^=(x<<5)&0xFFFFFFFF;s.state=x;return x
    def randint(s,lo,hi): return lo+(s.next()%(hi-lo))
    def random(s): return s.next()/0x100000000

@dataclass
class Substance:
    uid:int; cx:int; cy:int; matrix:np.ndarray; birth_tick:int
    dissolve_tick:int=-1; signature:str=""; signed_tick:int=-1; decay_mult:float=1.0
    structure_type:str=""; circularity:float=0.0; phase:PhaseState=PhaseState.SOLID
    phase_transition_tick:int=-1; drift_vx:float=0.0; drift_vy:float=0.0
    plasma_fragments:List[int]=field(default_factory=list)
    energy_history:deque=field(default_factory=lambda:deque(maxlen=100))
    fused_from:List[int]=field(default_factory=list); fusion_count:int=0
    @property
    def alive(s): return s.dissolve_tick<0
    @property
    def is_signed(s): return s.signature!=""
    @property
    def total_energy(s): return float(np.sum(s.matrix))
    @property
    def window(s): return WINDOW
    def update_phase(s,tick):
        e=s.total_energy;old=s.phase
        if e>PHASE_LIQUID_ENERGY:s.phase=PhaseState.PLASMA
        elif e>PHASE_SOLID_ENERGY:s.phase=PhaseState.LIQUID
        else:s.phase=PhaseState.SOLID
        if old!=s.phase:s.phase_transition_tick=tick
    def sign(s,pid,tick,cm=None):
        s.signature=pid;s.signed_tick=tick;s.decay_mult=0.0
        if cm is not None:s.matrix=cm.copy()
    def to_dict(s):return{"uid":s.uid,"position":[int(s.cx),int(s.cy)],"birth_tick":s.birth_tick,
        "signed_tick":s.signed_tick,"phase":s.phase.value,"structure_type":s.structure_type,
        "circularity":round(s.circularity,4),"energy_sum":round(s.total_energy,1),
        "fusion_count":s.fusion_count,"fused_from":s.fused_from,
        "matrix":[[round(float(v),1)for v in row]for row in s.matrix]}

class ShapeAnalyzer:
    @staticmethod
    def analyze(matrix):
        thresh=np.mean(matrix);binary=(matrix>thresh).astype(int)
        area=int(np.sum(binary))
        if area==0 or area==matrix.size:return TYPE_IRREGULAR,0.0
        perimeter=0;r,c=binary.shape
        for y in range(r):
            for x in range(c):
                if binary[y,x]==0:continue
                for dy,dx in[(-1,0),(1,0),(0,-1),(0,1)]:
                    ny,nx=y+dy,x+dx
                    if ny<0 or ny>=r or nx<0 or nx>=c:perimeter+=1
                    elif binary[ny,nx]==0:perimeter+=1
        if perimeter==0:return TYPE_IRREGULAR,0.0
        circ=(4.0*np.pi*area)/(perimeter*perimeter)
        if circ>0.75:return TYPE_ROUND,circ
        elif circ>0.50:return TYPE_ELONGATED,circ
        return TYPE_IRREGULAR,circ

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
    def apply(s,px,py,field,emu,in_signed,grad_steep,cluster_dense):
        total=0.0;triggered=[]
        for r in s.rules:
            if r.condition=="near_signed" and not in_signed:continue
            if r.condition=="gradient_steep" and not grad_steep:continue
            if r.condition=="cluster_dense" and not cluster_dense:continue
            if r.condition not in("near_signed","gradient_steep","cluster_dense"):
                if not r.check(px,py,field,emu):continue
            if s.rng.random()<r.trigger_prob:
                total+=r.penalty;r.hit_count+=1;triggered.append(r.rid)
        s.total_triggers+=len(triggered)
        return total,triggered
    def try_reveal(s):
        if s.rng.random()<DARK_REVEAL_CHANCE:
            ur=[r for r in s.rules if r.rid not in s.revealed]
            if ur:rule=ur[s.rng.randint(0,len(ur))];s.revealed.append(rule.rid);return rule
        return None

class DarkSkeleton:
    def __init__(s,gs):s.grid_size=gs;s.residue=np.zeros((gs,gs),dtype=np.float64);s.nodes=[];s.edges=[]
    def deposit(s,x,y):s.residue[y,x]+=RESIDUE_PER_ACTION
    def decay(s):s.residue-=RESIDUE_DECAY;s.residue=np.maximum(s.residue,0)
    def scan_skeleton(s,field):
        s.nodes.clear();s.edges.clear()
        rgx=np.abs(np.diff(s.residue,axis=1,append=s.residue[:,-1:]));rgy=np.abs(np.diff(s.residue,axis=0,append=s.residue[-1:,:]))
        egx=np.abs(np.diff(field,axis=1,append=field[:,-1:]));egy=np.abs(np.diff(field,axis=0,append=field[-1:,:]))
        combined=(rgx+rgy)*0.3+(egx+egy)*0.7;th=np.percentile(combined,95)
        candidates=np.argwhere(combined>th)
        if len(candidates)>SKELETON_NODES_MAX:
            scores=combined[combined>th];idx=np.argsort(scores)[-SKELETON_NODES_MAX:];candidates=candidates[idx]
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
                nx,ny=(x+dx)%s.grid_size,(y+dy)%s.grid_size
                sc=s.residue[ny,nx]+0.3*field[ny,nx]
                if sc>best:best=sc;bx,by=nx,ny
        return bx,by

@dataclass
class HotspotTracker:
    cx:int;cy:int;history:deque=field(default_factory=lambda:deque(maxlen=STABILITY_WINDOW))
    state:str="tracking";detected_tick:int=0;stable_mean:float=0.0
    pre_test_matrix:Optional[np.ndarray]=None;test_start_tick:int=0

class SubstanceDetector:
    def __init__(s,gs):
        s.grid_size=gs;s.trackers:Dict={};s.substances:List[Substance]=[];s.next_uid=1
        s._pending=[];s.total_fusions=0;s.total_energy_transferred=0.0;s.total_maintenance_consumed=0.0

    def _region(s,sub):return slice(sub.cy-WINDOW//2,sub.cy+WINDOW//2+1),slice(sub.cx-WINDOW//2,sub.cx+WINDOW//2+1)
    def _distance(s,a,b):return np.sqrt((a.cx-b.cx)**2+(a.cy-b.cy)**2)

    def scan_hotspots(s,field,tick):
        w=WINDOW;gs=s.grid_size;sz=gs-w+1
        sums=np.zeros((sz,sz))
        for dy in range(w):
            for dx in range(w):
                sums+=field[dy:dy+sz,dx:dx+sz]
        th=float(np.mean(field))+HOTSPOT_SIGMA*float(np.std(field))
        hy,hx=np.where(np.logical_and(sums>th,sums>MIN_HOTSPOT_SUM))
        current=set()
        for i in range(len(hy)):
            cy,cx=int(hy[i]),int(hx[i]);current.add((cx,cy))
            es=float(sums[cy,cx])
            if(cx,cy)not in s.trackers:
                t=HotspotTracker(cx=cx,cy=cy,detected_tick=tick);t.history=deque([es],maxlen=STABILITY_WINDOW);s.trackers[(cx,cy)]=t
            else:
                t=s.trackers[(cx,cy)]
                if t.state not in("testing","confirmed","signed"):t.history.append(es)
        dead=[k for k,t in s.trackers.items()if k not in current and t.state not in("confirmed","signed")]
        for k in dead:del s.trackers[k]

    def check_stability(s,tick):
        for key,trk in list(s.trackers.items()):
            if trk.state!="tracking" or len(trk.history)<STABILITY_WINDOW:continue
            recent=list(trk.history)[-50:];m=np.mean(recent)
            if m<1e-6:continue
            if(np.max(recent)-np.min(recent))/m<STABILITY_THRESHOLD:trk.state="stable_candidate";trk.stable_mean=m

    def run_interference_tests(s,field,tick):
        w=WINDOW
        for key,trk in list(s.trackers.items()):
            if trk.state!="stable_candidate":continue
            trk.state="testing";trk.test_start_tick=tick
            trk.pre_test_matrix=field[trk.cy:trk.cy+w,trk.cx:trk.cx+w].copy()
            p=np.random.RandomState(tick).uniform(-PERTURB_AMPLITUDE,PERTURB_AMPLITUDE,(w,w))
            field[trk.cy:trk.cy+w,trk.cx:trk.cx+w]+=p
            field[trk.cy:trk.cy+w,trk.cx:trk.cx+w]=np.maximum(field[trk.cy:trk.cy+w,trk.cx:trk.cx+w],0)

    def check_recovery(s,field,tick):
        w=WINDOW
        for key,trk in list(s.trackers.items()):
            if trk.state!="testing":continue
            if tick-trk.test_start_tick<PERTURB_RECOVERY:continue
            pre=float(np.sum(trk.pre_test_matrix));cur=float(np.sum(field[trk.cy:trk.cy+w,trk.cx:trk.cx+w]))
            if pre<1e-6:trk.state="tracking";continue
            if abs(cur-pre)/pre<STABILITY_THRESHOLD:
                matrix=field[trk.cy:trk.cy+w,trk.cx:trk.cx+w].copy()
                st,circ=ShapeAnalyzer.analyze(matrix)
                sub=Substance(uid=s.next_uid,cx=trk.cx+w//2,cy=trk.cy+w//2,matrix=matrix,birth_tick=tick,structure_type=st,circularity=circ)
                s.next_uid+=1;s.substances.append(sub);s._pending.append(sub);trk.state="confirmed"
            else:trk.state="tracking";trk.history.clear()

    def apply_maintenance(s,field,tick):
        total=0.0
        for sub in s.substances:
            if not sub.alive:continue
            region=field[s._region(sub)];total_e=float(np.sum(region))
            cost=WINDOW*WINDOW*MAINTENANCE_COST*(total_e/1200.0)
            if sub.is_signed:cost*=0.5
            if total_e>cost:reduction=cost*(region/max(total_e,1e-6));field[s._region(sub)]-=reduction;field[s._region(sub)]=np.maximum(field[s._region(sub)],0);total+=cost
        s.total_maintenance_consumed+=total

    def apply_competition(s,field,tick):
        total=0.0;alive=[sub for sub in s.substances if sub.alive]
        if len(alive)<2:return
        buckets={}
        for sub in alive:
            bx,by=sub.cx//5,sub.cy//5;key=(bx,by)
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
                            dist=s._distance(a,b)
                            if dist>MERGE_DISTANCE+2:continue
                            ea=float(np.sum(field[s._region(a)]));eb=float(np.sum(field[s._region(b)]))
                            diff=abs(ea-eb)
                            if diff<COMPETITION_THRESHOLD:continue
                            transfer=diff*COMPETITION_RATE
                            if ea>eb:
                                rb=field[s._region(b)];tb=float(np.sum(rb))
                                if tb>transfer:drain=transfer*(rb/max(tb,1e-6));field[s._region(b)]-=drain;field[s._region(b)]=np.maximum(field[s._region(b)],0);field[s._region(a)]+=drain;total+=transfer
                            else:
                                ra=field[s._region(a)];ta=float(np.sum(ra))
                                if ta>transfer:drain=transfer*(ra/max(ta,1e-6));field[s._region(a)]-=drain;field[s._region(a)]=np.maximum(field[s._region(a)],0);field[s._region(b)]+=drain;total+=transfer
        s.total_energy_transferred+=total

    def apply_fusion(s,field,tick):
        alive=[sub for sub in s.substances if sub.alive]
        for sub in list(alive):
            if not sub.alive:continue
            es=float(np.sum(field[s._region(sub)]))
            if es>=FUSION_THRESHOLD:continue
            neighbors=[(s._distance(sub,n),n)for n in alive if n.uid!=sub.uid and n.alive and s._distance(sub,n)<=MERGE_DISTANCE+2]
            if not neighbors:continue
            neighbors.sort(key=lambda x:x[0]);_,winner=neighbors[0]
            field[s._region(winner)]+=field[s._region(sub)]*0.7
            field[s._region(sub)]=BACKGROUND
            nm=field[s._region(winner)].copy();st,circ=ShapeAnalyzer.analyze(nm)
            winner.matrix=nm;winner.structure_type=st;winner.circularity=circ;winner.fusion_count+=1;winner.fused_from.append(sub.uid)
            sub.dissolve_tick=tick;s.total_fusions+=1;s._pending.append(winner)

    def check_dissolution(s,field,tick):
        for sub in s.substances:
            if not sub.alive:continue
            cur=float(np.sum(field[s._region(sub)]));birth=float(np.sum(sub.matrix))
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
        frags=[];matrix=field[s._region(sub)];total_e=float(np.sum(matrix))
        if total_e<300:return[]
        rng=np.random.RandomState(tick)
        for i in range(PHASE_PLASMA_SPLIT):
            fx=sub.cx+rng.randint(-5,6);fy=sub.cy+rng.randint(-5,6)
            fx=max(WINDOW//2,min(s.grid_size-WINDOW//2-1,fx));fy=max(WINDOW//2,min(s.grid_size-WINDOW//2-1,fy))
            fm=rng.uniform(0.3,0.7,(WINDOW,WINDOW))*matrix.mean()
            field[s._region(Substance(uid=0,cx=fx,cy=fy,matrix=fm,birth_tick=0))]+=fm
            field[s._region(Substance(uid=0,cx=fx,cy=fy,matrix=fm,birth_tick=0))]=np.maximum(field[s._region(Substance(uid=0,cx=fx,cy=fy,matrix=fm,birth_tick=0))],0)
            st,circ=ShapeAnalyzer.analyze(fm)
            frag=Substance(uid=s.next_uid,cx=fx,cy=fy,matrix=fm.copy(),birth_tick=tick,structure_type=st,circularity=circ,signature=f"{sub.signature}-f{i}",signed_tick=tick)
            s.next_uid+=1;s.substances.append(frag);s._pending.append(frag);frags.append(frag);sub.plasma_fragments.append(frag.uid)
        field[s._region(sub)]*=0.5
        return frags

    def apply_liquid_drift(s,sub,field):
        if sub.phase!=PhaseState.LIQUID or not sub.alive:return
        region=field[s._region(sub)];gy,gx=np.gradient(region)
        tg=np.sqrt(np.mean(gx)**2+np.mean(gy)**2)
        if tg>0.01:sub.drift_vx+=PHASE_DRIFT_RATE*np.mean(gx)/tg;sub.drift_vy+=PHASE_DRIFT_RATE*np.mean(gy)/tg
        if abs(sub.drift_vx)>=1.0:
            sx=int(sub.drift_vx);ncx=max(WINDOW//2,min(s.grid_size-WINDOW//2-1,sub.cx+sx))
            if ncx!=sub.cx:
                old=field[s._region(sub)].copy();field[s._region(sub)]=BACKGROUND
                sc2=Substance(uid=0,cx=ncx,cy=sub.cy,matrix=old,birth_tick=0)
                field[s._region(sc2)]+=old;sub.cx=ncx
            sub.drift_vx-=sx
        if abs(sub.drift_vy)>=1.0:
            sy=int(sub.drift_vy);ncy=max(WINDOW//2,min(s.grid_size-WINDOW//2-1,sub.cy+sy))
            if ncy!=sub.cy:
                old=field[s._region(sub)].copy();field[s._region(sub)]=BACKGROUND
                sc2=Substance(uid=0,cx=sub.cx,cy=ncy,matrix=old,birth_tick=0)
                field[s._region(sc2)]+=old;sub.cy=ncy
            sub.drift_vy-=sy

    def try_sign(s,px,py,pid,tick,field):
        for sub in s.substances:
            if not sub.alive or sub.is_signed:continue
            if(sub.cx-WINDOW//2<=px<=sub.cx+WINDOW//2 and sub.cy-WINDOW//2<=py<=sub.cy+WINDOW//2):
                current=field[s._region(sub)].copy();sub.sign(pid,tick,current)
                key=(sub.cx-WINDOW//2,sub.cy-WINDOW//2)
                if key in s.trackers:s.trackers[key].state="signed"
                return sub
        return None

    def tick(s,field,tick):
        s._pending.clear();confirmed_uids=set()
        if tick%SCAN_INTERVAL==0 and tick>0:s.scan_hotspots(field,tick);s.check_stability(tick);s.run_interference_tests(field,tick)
        s.check_recovery(field,tick)
        if tick%COMPETITION_INTERVAL==0:s.apply_maintenance(field,tick);s.apply_competition(field,tick);s.apply_fusion(field,tick)
        s.check_dissolution(field,tick)
        pl=s.update_phases(tick)
        for sub in pl:s.handle_plasma_split(sub,field,tick)
        for sub in s.substances:
            if sub.phase==PhaseState.LIQUID and sub.alive:s.apply_liquid_drift(sub,field)
        unique=[]
        for sub in s._pending:
            if sub.uid not in confirmed_uids:confirmed_uids.add(sub.uid);unique.append(sub)
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
        with open(path,"w",encoding="utf-8")as f:json.dump({"version":"v0.5","total_signed":s.signed_count,"total_fusions":s.total_fusions,"templates":s.export_templates()},f,ensure_ascii=False,indent=2)

class Particle:
    def __init__(s,gs,pid="PC-1",seed=123456789):
        s.grid_size=gs;s.pid=pid;s.rng=XORShift32(seed);s.x=s.rng.randint(0,gs);s.y=s.rng.randint(0,gs)
        s.step_count=0;s.raises=0;s.lowers=0;s.signs=0;s.moves=0
        s._dx=[0,1,0,-1];s._dy=[-1,0,1,0];s._ddx=[0,1,0,-1,1,-1,-1,1];s._ddy=[-1,0,1,0,-1,-1,1,1]
        s.dark_hits=0;s.revealed_rules=[];s.last_penalty=0.0
    def _neighbors(s,field):
        return[((s.x+s._dx[d])%s.grid_size,(s.y+s._dy[d])%s.grid_size,float(field[(s.y+s._dy[d])%s.grid_size,(s.x+s._dx[d])%s.grid_size]))for d in range(4)]
    def _substep_move(s,field,skel):
        r=s.rng.random()
        if r<0.50:nbrs=s._neighbors(field);best=max(nbrs,key=lambda n:n[2]);s.x,s.y=best[0],best[1]
        elif r<0.75:s.x,s.y=skel.residue_attraction(s.x,s.y,field)
        else:d=s.rng.randint(0,8);s.x=(s.x+s._ddx[d])%s.grid_size;s.y=(s.y+s._ddy[d])%s.grid_size
        s.moves+=1
    def _in_signed_zone(s,detector):
        for sub in detector.substances:
            if sub.is_signed and sub.alive:
                if(sub.cx-WINDOW//2<=s.x<=sub.cx+WINDOW//2 and sub.cy-WINDOW//2<=s.y<=sub.cy+WINDOW//2):return True
        return False
    def _check_gradient_steep(s,field):
        if s.x<1 or s.x>=s.grid_size-1 or s.y<1 or s.y>=s.grid_size-1:return False
        return(abs(field[s.y,s.x+1]-field[s.y,s.x-1])+abs(field[s.y+1,s.x]-field[s.y-1,s.x]))>30
    def _check_cluster_dense(s,detector,radius=5):
        return sum(1 for sub in detector.substances if sub.alive and abs(sub.cx-s.x)<=radius and abs(sub.cy-s.y)<=radius)>=3
    def step(s,field,detector,skel,dark_rules,tick):
        s.step_count+=1;m=r=l=sg=0;sr=None;tdp=0.0
        emu=float(np.mean(field));ins=s._in_signed_zone(detector);gs=s._check_gradient_steep(field);cd=s._check_cluster_dense(detector)
        for _ in range(SUBSTEPS_PER_TICK):
            s._substep_move(field,skel);m+=1;skel.deposit(s.x,s.y)
            roll=s.rng.random()
            if roll<RAISE_PROB:
                pn,_=dark_rules.apply(s.x,s.y,field,emu,ins,gs,cd);tdp+=pn
                if pn>0:s.dark_hits+=1
                field[s.y,s.x]+=SUB_RAISE_AMOUNT;s.raises+=1;r+=1
            elif roll<RAISE_PROB+LOWER_PROB:
                pn,_=dark_rules.apply(s.x,s.y,field,emu,ins,gs,cd);tdp+=pn
                if pn>0:s.dark_hits+=1
                if not s._in_signed_zone(detector):field[s.y,s.x]=max(0,field[s.y,s.x]-10);s.lowers+=1;l+=1
            elif roll<RAISE_PROB+LOWER_PROB+SIGN_PROB:
                pn,_=dark_rules.apply(s.x,s.y,field,emu,ins,gs,cd);tdp+=pn
                if pn>0:s.dark_hits+=1
                s.signs+=1;sg+=1;result=detector.try_sign(s.x,s.y,s.pid,tick,field)
                if result:sr=result
        s.last_penalty=tdp
        return m,r,l,sg,sr,tdp

class ShanhaiWorld:
    def __init__(s,seed=42):
        s.grid_size=GRID_SIZE;s.tick=0
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
                y0,y1=sub.cy-WINDOW//2,sub.cy+WINDOW//2+1;x0,x1=sub.cx-WINDOW//2,sub.cx+WINDOW//2+1
                s.field[y0:y1,x0:x1]+=DECAY_RATE*(s.field[y0:y1,x0:x1]-BACKGROUND)
                s.field[y0:y1,x0:x1]+=0.05*(sub.matrix-s.field[y0:y1,x0:x1])
        s.field=np.maximum(s.field,0);s.field=np.minimum(s.field,MAX_FIELD_ENERGY)
    def step(s):
        s.tick+=1
        m,r,l,sg,sr,dp=s.particle.step(s.field,s.detector,s.skeleton,s.dark_rules,s.tick)
        s.total_moves+=m;s.total_raises+=r;s.total_lowers+=l;s.total_signs+=sg;s.total_dark_penalty+=dp
        s.skeleton.decay();s._diffuse()
        if s.tick%SKELETON_SCAN_INTERVAL==0:s.skeleton.scan_skeleton(s.field)
        ns=s.detector.tick(s.field,s.tick)
        for sub in ns:
            s.log.append({"event":"substance_born","tick":s.tick,"uid":sub.uid,"position":(sub.cx,sub.cy),"type":sub.structure_type,"phase":sub.phase.value,"fusion_count":sub.fusion_count})
        if sr:s.log.append({"event":"substance_signed","tick":s.tick,"uid":sr.uid})
        return ns if ns else None
    def run(s,max_ticks=100000,verbose=True,target_substances=1):
        all_subs=[];announced=set()
        for _ in range(max_ticks):
            ns=s.step()
            if ns:
                for sub in ns:
                    if sub.uid not in announced:
                        announced.add(sub.uid);all_subs.append(sub)
                        if verbose:
                            tag="⚡"if sub.phase==PhaseState.PLASMA else("🔄"if sub.fusion_count>0 else"⛰️")
                            xtra=f" 融合×{sub.fusion_count}"if sub.fusion_count>0 else""
                            print(f"\n  {tag} #{sub.uid} t={s.tick} ({sub.cx},{sub.cy}) {sub.structure_type} {sub.phase.value}{xtra}")
            if s.log and s.log[-1].get("event")=="substance_signed" and verbose:print(f"  🖊️  签名 #{s.log[-1]['uid']}")
            if verbose and s.tick%5000==0:
                d=s.detector;print(f"  [{s.tick:6d}] μ={s.field.mean():.1f} 物质={d.alive_count} ✍={d.signed_count} {d.phase_counts()} 融合={d.total_fusions}")
            if s.detector.total_born>=target_substances:break
        return all_subs

class Visualizer:
    def __init__(s,world):s.world=world;s.fig=None;s.ax=None;s.im=None;s.paused=False;s.speed=1;s.particle_dot=None;s.sub_elements=[];s.skel_lines=[]
    def setup(s):
        import matplotlib;matplotlib.use("TkAgg");import matplotlib.pyplot as plt
        s.fig,s.ax=plt.subplots(figsize=(10,9));s.fig.canvas.manager.set_window_title("山海 v0.5 — 大空间融合")
        s.im=s.ax.imshow(s.world.field,cmap=CMAP,aspect="equal",vmin=0,vmax=100,origin="upper",interpolation="bilinear")
        s.ax.set_title("山海 v0.5 · 大空间");plt.colorbar(s.im,ax=s.ax,label="能量")
        s.particle_dot,=s.ax.plot([],[],"o",color="white",markersize=10,markeredgecolor="black",zorder=10)
        s.fig.canvas.mpl_connect("key_press_event",s._on_key);plt.ion();plt.show()
    def _on_key(s,event):
        if event.key==" ":s.paused=not s.paused
        elif event.key=="q":sys.exit(0)
        elif event.key=="+":s.speed=min(s.speed*2,64)
        elif event.key=="-":s.speed=max(s.speed//2,1)
    def update(s):
        if s.fig is None:return
        import matplotlib.pyplot as plt
        s.im.set_array(s.world.field);s.im.set_clim(vmin=0,vmax=max(100,float(np.max(s.world.field))))
        s.particle_dot.set_data([s.world.particle.x],[s.world.particle.y])
        for rect,ann in s.sub_elements:rect.remove();ann.remove()
        s.sub_elements.clear()
        for line in s.skel_lines:line.remove()
        s.skel_lines.clear()
        for x1,y1,x2,y2 in s.world.skeleton.edges:
            line,=s.ax.plot([x1,x2],[y1,y2],color="#ff880055",linewidth=1,zorder=1);s.skel_lines.append(line)
        if s.world.skeleton.nodes:
            dots=s.ax.scatter([n[0]for n in s.world.skeleton.nodes],[n[1]for n in s.world.skeleton.nodes],c="#ff8800",s=20,marker="x",alpha=0.6,zorder=2);s.skel_lines.append(dots)
        for sub in s.world.detector.substances:
            if not sub.alive:continue
            color=COLOR_PHASE.get(sub.phase,"#00ff88");lw=4 if sub.is_signed else 2;style="-"if sub.is_signed else"--"
            rect=plt.Rectangle((sub.cx-WINDOW/2,sub.cy-WINDOW/2),WINDOW,WINDOW,fill=False,edgecolor=color,linewidth=lw,linestyle=style)
            s.ax.add_patch(rect)
            label=f"#{sub.uid}"
            if sub.fusion_count>0:label+=f"∪{sub.fusion_count}"
            if sub.phase!=PhaseState.SOLID:label+=f" {sub.phase.value[0]}"
            ann=s.ax.annotate(label,(sub.cx,sub.cy-WINDOW/2-1),color=color,fontsize=6,ha="center");s.sub_elements.append((rect,ann))
        d=s.world.detector;s.ax.set_title(f"山海 v0.5 · tick={s.world.tick} · 物质={d.alive_count} ✍{d.signed_count} 融合={d.total_fusions} · {d.phase_counts()}")
        s.fig.canvas.draw_idle();s.fig.canvas.flush_events()
    def run_interactive(s,max_ticks=100000):
        s.setup();print(f"\n  山海 v0.5 — 大空间 5×5 窗口\n")
        step=0
        while step<max_ticks and s.fig is not None:
            if not s.paused:
                for _ in range(s.speed):
                    if step>=max_ticks:break
                    s.world.step();step+=1
                if s.world.tick%100==0:s.update()
            try:s.fig.canvas.flush_events()
            except:break
            time.sleep(0.01)
        if s.fig:plt.ioff();plt.show()

def main():
    import argparse
    p=argparse.ArgumentParser(description="山海 v0.5");p.add_argument("--no-viz",action="store_true");p.add_argument("--ticks",type=int,default=100000)
    p.add_argument("--seed",type=int,default=42);p.add_argument("--target",type=int,default=1)
    p.add_argument("--templates",type=str,default="templates_v5.json")
    a=p.parse_args()
    print("="*60);print("  山海 v0.5 — 大空间 · 融合纪元");print(f"  {WINDOW}×{WINDOW} 窗口 | 融合阈值={FUSION_THRESHOLD} | 融合距离={MERGE_DISTANCE}");print("="*60)
    w=ShanhaiWorld(seed=a.seed)
    if a.no_viz:
        t0=time.time();subs=w.run(max_ticks=a.ticks,verbose=True,target_substances=a.target);el=time.time()-t0
        d=w.detector;print(f"\n  tick={w.tick} ({el:.1f}s)");print(f"  物质: {d.total_born} born / {d.alive_count} alive / {d.signed_count} signed")
        print(f"  相态: {d.phase_counts()}");print(f"  融合: {d.total_fusions} | 争能: {d.total_energy_transferred:.0f} | 消耗: {d.total_maintenance_consumed:.0f}")
        if d.substances:
            circs=[s.circularity for s in d.substances if s.alive]
            if circs:print(f"  圆形度: {min(circs):.4f}-{max(circs):.4f} unique={len(set(round(c,4)for c in circs))}")
            print(f"  类型: {dict(Counter(s.structure_type for s in d.substances if s.alive))}")
            fused=[s for s in d.substances if s.fusion_count>0]
            if fused:
                print(f"\n  融合物质 ({len(fused)}):")
                for s in fused[:8]:print(f"    #{s.uid}: ×{s.fusion_count} src={s.fused_from} {s.structure_type} ○={s.circularity:.3f} E={s.total_energy:.0f}")
        if d.signed_count>0:d.save_templates(os.path.join(os.path.dirname(os.path.abspath(__file__)),a.templates));print(f"\n  模板: {a.templates}")
    else:
        viz=Visualizer(w);viz.run_interactive(max_ticks=a.ticks)

if __name__=="__main__":main()
