package main

import (
	"encoding/json"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

const (
	MEMSIZE     = 256 * 1024 * 1024
	MAX_PCS     = 65536
	PORT        = 8765
	DECAY_EVERY = 20
	SHARDS      = 16
	POOL_SIZE   = 256
)

var (
	mem    []byte
	pcs    [MAX_PCS]*Particle
	nPcs   int64
	tick   int64
	births int64
	deaths int64
	copies int64
	writes int64
	paused = true
	speed  = int64(20)

	snapshot    [256 * 256]byte
	snapshotPcs [256 * 256]byte
	snapMutex   sync.RWMutex
	rng         = rand.New(rand.NewSource(42))
	rngLock     sync.Mutex

	substances      []Substance
	subLock         sync.Mutex
	substanceHashes = make(map[uint64]int64)
	subDeaths       []DeathRecord // 物质死亡日志

	pool   = make(chan struct{}, POOL_SIZE)
	poolWg sync.WaitGroup
)

func safeFloat() float64 { rngLock.Lock(); defer rngLock.Unlock(); return rng.Float64() }
func safeIntn(n int) int { rngLock.Lock(); defer rngLock.Unlock(); return rng.Intn(n) }

type Particle struct {
	pc, energy, parent int32; acc uint16; regs [16]uint16
	birth, insts int64; efficiency uint16; alive int32; codeHash uint64
}

type Substance struct {
	ID, FirstSeen, LastSeen, StableTicks, Insts int64
	PC int32; CodeHash uint64; CodeLen int; Energy int32; Efficiency uint16
	CodeSample []byte
}

type DeathRecord struct {
	SubID int64  `json:"sub_id"`
	Hash  uint64 `json:"hash"`
	Died  int64  `json:"died_tick"`
	Age   int64  `json:"age_ticks"`
	LastE int32  `json:"last_energy"`
	LastEff uint16 `json:"last_efficiency"`
	LastPC int32  `json:"last_pc"`
	Reason string `json:"reason"`
}

const (
	OP_N = 0x0; OP_S = 0x1; OP_L = 0x2; OP_A = 0x3; OP_W = 0x4
	OP_J = 0x5; OP_C = 0x6; OP_X = 0x7; OP_H = 0x8; OP_E = 0x9
	OP_R = 0xA; OP_D = 0xB; OP_T = 0xC; OP_F = 0xD; OP_U = 0xE; OP_Z = 0xF
)

func initUniverse() {
	fmt.Println("💥 256MB 大爆炸...")
	mem = make([]byte, MEMSIZE)
	for i := 0; i < MEMSIZE/4; i++ { mem[safeIntn(MEMSIZE)] = byte(safeIntn(200) + 5) }
	n := 0
	for i := 0; i < MEMSIZE && n < 32; i++ {
		if mem[i] >= 150 { pcs[n] = &Particle{pc: int32(i), energy: int32(mem[i]) * 40, birth: 0, parent: -1, alive: 1}; n++ }
	}
	atomic.StoreInt64(&nPcs, int64(n)); atomic.StoreInt64(&births, int64(n))
	for i := 0; i < POOL_SIZE; i++ { pool <- struct{}{} }
	fmt.Printf("  粒子:%d 池:%d 分片:%d\n", n, POOL_SIZE, SHARDS)
}

func decayParallel() {
	chunk := MEMSIZE / SHARDS; var wg sync.WaitGroup
	for s := 0; s < SHARDS; s++ {
		start, end := s*chunk, (s+1)*chunk
		if s == SHARDS-1 { end = MEMSIZE }
		wg.Add(1)
		go func(f, t int) { defer wg.Done(); for i := f; i < t; i++ { if mem[i] > 0 { mem[i]-- } } }(start, end)
	}
	wg.Wait()
}

func codeHash(p *Particle) uint64 {
	start := (int(p.pc) - 16) % MEMSIZE; if start < 0 { start += MEMSIZE }
	var h uint64 = 14695981039346656037
	for i := 0; i < 32; i++ { h ^= uint64(mem[(start+i)%MEMSIZE]); h *= 1099511628211 }
	return h
}

// ── 存档/恢复 ──
const SAVE_DIR = "genesis_v4_save"

func saveState() {
	os.MkdirAll(SAVE_DIR, 0755)
	// 内存
	f, _ := os.Create(SAVE_DIR + "/mem.bin")
	f.Write(mem)
	f.Close()
	// 状态JSON
	state := map[string]interface{}{
		"tick": atomic.LoadInt64(&tick), "nPcs": atomic.LoadInt64(&nPcs),
		"births": atomic.LoadInt64(&births), "deaths": atomic.LoadInt64(&deaths),
		"copies": atomic.LoadInt64(&copies), "writes": atomic.LoadInt64(&writes),
	}
	// 粒子
	var particles []map[string]interface{}
	for i := 0; i < MAX_PCS; i++ {
		p := pcs[i]
		if p != nil && atomic.LoadInt32(&p.alive) == 1 {
			particles = append(particles, map[string]interface{}{
				"i": i, "pc": p.pc, "acc": p.acc, "energy": atomic.LoadInt32(&p.energy),
				"birth": p.birth, "parent": p.parent, "insts": p.insts,
				"efficiency": p.efficiency, "regs": p.regs,
			})
		}
	}
	state["particles"] = particles
	subLock.Lock()
	state["substances"] = substances
	state["substanceHashes"] = substanceHashes
	subLock.Unlock()
	data, _ := json.Marshal(state)
	os.WriteFile(SAVE_DIR+"/state.json", data, 0644)
}

func restoreState() bool {
	if _, err := os.Stat(SAVE_DIR + "/mem.bin"); os.IsNotExist(err) { return false }
	// 内存
	f, err := os.Open(SAVE_DIR + "/mem.bin")
	if err != nil { return false }
	data, _ := io.ReadAll(f); f.Close()
	if len(data) != MEMSIZE { return false }
	copy(mem, data)
	// 状态JSON
	js, err := os.ReadFile(SAVE_DIR + "/state.json")
	if err != nil { return false }
	var state map[string]interface{}
	json.Unmarshal(js, &state)
	atomic.StoreInt64(&tick, int64(state["tick"].(float64)))
	atomic.StoreInt64(&nPcs, int64(state["nPcs"].(float64)))
	atomic.StoreInt64(&births, int64(state["births"].(float64)))
	atomic.StoreInt64(&deaths, int64(state["deaths"].(float64)))
	atomic.StoreInt64(&copies, int64(state["copies"].(float64)))
	atomic.StoreInt64(&writes, int64(state["writes"].(float64)))
	// 粒子
	particles, _ := state["particles"].([]interface{})
	for _, pi := range particles {
		pm := pi.(map[string]interface{})
		i := int(pm["i"].(float64))
		regs := [16]uint16{}
		if r, ok := pm["regs"].([]interface{}); ok {
			for j := 0; j < 16 && j < len(r); j++ { regs[j] = uint16(r[j].(float64)) }
		}
		pcs[i] = &Particle{
			pc: int32(pm["pc"].(float64)), acc: uint16(pm["acc"].(float64)),
			energy: int32(pm["energy"].(float64)), birth: int64(pm["birth"].(float64)),
			parent: int32(pm["parent"].(float64)), insts: int64(pm["insts"].(float64)),
			efficiency: uint16(pm["efficiency"].(float64)), alive: 1, regs: regs,
		}
	}
	// 物质
	subLock.Lock()
	if s, ok := state["substances"]; ok {
		js2, _ := json.Marshal(s)
		json.Unmarshal(js2, &substances)
	}
	if sh, ok := state["substanceHashes"]; ok {
		js3, _ := json.Marshal(sh)
		json.Unmarshal(js3, &substanceHashes)
	}
	subLock.Unlock()
	fmt.Printf("🌍 恢复存档: tick=%d 粒子=%d 物质=%d\n", atomic.LoadInt64(&tick), atomic.LoadInt64(&nPcs), len(substances))
	return true
}

func execute(p *Particle, idx int) {
	if atomic.LoadInt32(&p.alive) == 0 { return }
	pc := int(p.pc)
	b0, b1 := uint16(mem[pc%MEMSIZE]), uint16(mem[(pc+1)%MEMSIZE])
	op := (b0 >> 4) & 0xF
	rd := int(b0 & 0xF)
	rs1 := int((b1 >> 4) & 0xF)
	rs2 := int(b1 & 0xF)

	if atomic.LoadInt32(&p.energy) <= 0 { atomic.StoreInt32(&p.alive, 0); atomic.AddInt64(&nPcs, -1); atomic.AddInt64(&deaths, 1); return }

	switch op {
	case OP_N, OP_Z: p.pc += 2
	case OP_S: mem[(pc+rs1)%MEMSIZE] = byte(p.regs[rs2]); p.pc += 2
	case OP_L: p.regs[rd] = uint16(mem[(pc+rs1)%MEMSIZE]); p.pc += 2
	case OP_A: p.regs[rd] = p.regs[rs1] + p.regs[rs2]; p.pc += 2
	case OP_W: mem[(pc+rs2)%MEMSIZE] = mem[(pc+rs1)%MEMSIZE]; atomic.AddInt64(&writes, 1); p.pc += 2
	case OP_J: if p.regs[rd] != 0 { p.pc = int32(pc + rs1) } else { p.pc += 2 }
	case OP_C, OP_R:
		mut := 0.01; if op == OP_R && p.efficiency > 32768 { mut = 0.003 }
		ln := int(p.regs[rd] & 0x3F); if ln > 64 { ln = 64 }
		dst := (pc + rs2) % MEMSIZE; src := (pc - int(p.regs[rd]&0x0F)) % MEMSIZE
		if src < 0 { src += MEMSIZE }
		for i := 0; i < ln; i++ { v := mem[(src+i)%MEMSIZE]; if safeFloat() < mut { v ^= 1 << safeIntn(8) }; mem[(dst+i)%MEMSIZE] = v }
		ne := atomic.LoadInt32(&p.energy) / 3
		if atomic.LoadInt64(&nPcs) < MAX_PCS && ne > 8 {
			for j := 0; j < MAX_PCS; j++ {
				if pcs[j] == nil || atomic.LoadInt32(&pcs[j].alive) == 0 {
					ef := p.efficiency; if safeFloat() < 0.05 { ef ^= uint16(1 << safeIntn(16)) }
					pcs[j] = &Particle{pc: int32(dst), energy: ne, birth: tick, parent: int32(idx), efficiency: ef, alive: 1}
					atomic.AddInt64(&nPcs, 1); atomic.AddInt64(&copies, 1); atomic.AddInt32(&p.energy, -ne); break
				}
			}
		}
		p.pc += 2
	case OP_X: a, b := (pc+rs1)%MEMSIZE, (pc+rs2)%MEMSIZE; mem[a], mem[b] = mem[b], mem[a]; p.pc += 2
	case OP_H: nbr := (pc + rs1) % MEMSIZE; if int32(p.efficiency)+1 > int32(mem[nbr])+1 { s := (int32(p.efficiency)-int32(mem[nbr]))/8; if s > 0 && mem[nbr] >= byte(s) { mem[nbr] -= byte(s); atomic.AddInt32(&p.energy, s) } }; p.pc += 2
	case OP_E: p.regs[3] = p.efficiency; p.pc += 2
	case OP_D: for o := int32(-4); o <= 4; o++ { a := (p.pc+o)%int32(MEMSIZE); if mem[a] > 0 { mem[a]-- } }; p.pc += 2
	case OP_T: if p.energy < 15 { atomic.StoreInt32(&p.alive, 0); atomic.AddInt64(&nPcs, -1); atomic.AddInt64(&deaths, 1) }; p.pc += 2
	case OP_F: c := pc % MEMSIZE; t := int32(mem[c])/4; if t>0 { mem[c]-=byte(t); mem[(c-1+MEMSIZE)%MEMSIZE]+=byte(t/2); mem[(c+1)%MEMSIZE]+=byte(t/2) }; p.pc += 2
	case OP_U: mem[(pc+rs1)%MEMSIZE] += byte(safeIntn(30) + 5); p.pc += 2
	}
	atomic.AddInt32(&p.energy, -1); p.insts++
	p.pc %= int32(MEMSIZE); if p.pc < 0 { p.pc += int32(MEMSIZE) }
}

func evolveRound() {
	t := atomic.LoadInt64(&tick)
	var wg sync.WaitGroup
	for i := 0; i < MAX_PCS; i++ {
		p := pcs[i]
		if p == nil || atomic.LoadInt32(&p.alive) == 0 { continue }
		pr, ix := p, i; wg.Add(1)
		go func() { defer wg.Done()
			b := int(atomic.LoadInt32(&pr.energy))/50+1; if b > 6 { b = 6 }
			for j := 0; j < b; j++ { if atomic.LoadInt32(&pr.alive) == 0 { break }; execute(pr, ix) }
		}()
	}
	wg.Wait()

	if t%int64(DECAY_EVERY) == 0 { decayParallel() }
	for i := 0; i < 200; i++ { p := safeIntn(MEMSIZE); mem[p] += byte(safeIntn(120) + 10) }
	if atomic.LoadInt64(&nPcs) < MAX_PCS && t%7 == 0 {
		for k := 0; k < 2; k++ { p := safeIntn(MEMSIZE); if mem[p] >= 140 {
			for j := 0; j < MAX_PCS; j++ { if pcs[j] == nil || atomic.LoadInt32(&pcs[j].alive) == 0 { pcs[j] = &Particle{pc: int32(p), energy: int32(mem[p])*25, birth: t, parent: -2, alive: 1}; atomic.AddInt64(&nPcs,1); atomic.AddInt64(&births,1); break } }
		}}
	}
	if t%50 == 0 { detectSubstances(); for i:=0;i<MAX_PCS;i++ { if pcs[i]!=nil && atomic.LoadInt32(&pcs[i].alive)==0 { pcs[i]=nil } } }
	atomic.AddInt64(&tick, 1)
	if t%30 == 0 { updateSnapshot() }
}

// 上次检测时存活的物质哈希集合
var lastSeenHashes = make(map[uint64]bool)
var lastSeenTick int64

func detectSubstances() {
	t := atomic.LoadInt64(&tick)
	subLock.Lock(); defer subLock.Unlock()

	thisRound := make(map[uint64]bool)

	for i := 0; i < MAX_PCS; i++ {
		p := pcs[i]; if p == nil || atomic.LoadInt32(&p.alive) == 0 || p.insts < 100 || p.energy < 30 { continue }
		h := codeHash(p); p.codeHash = h; thisRound[h] = true
		if ft, ok := substanceHashes[h]; ok {
			for s := range substances { if substances[s].CodeHash == h {
				substances[s].LastSeen = t; substances[s].StableTicks = t - ft
				substances[s].Energy = p.energy; substances[s].Insts = p.insts
				substances[s].Efficiency = p.efficiency; substances[s].PC = p.pc
				break
			}}
		} else if p.insts > 500 && p.energy > 100 {
			substanceHashes[h] = t; cd := make([]byte, 32); st := (int(p.pc)-16)%MEMSIZE; if st < 0 { st += MEMSIZE }
			for j := 0; j < 32; j++ { cd[j] = mem[(st+j)%MEMSIZE] }
			sub := Substance{ID: int64(len(substances)), FirstSeen: t, LastSeen: t, PC: p.pc, CodeHash: h, CodeLen: 32, Energy: p.energy, Insts: p.insts, Efficiency: p.efficiency, CodeSample: cd}
			substances = append(substances, sub)
			fmt.Printf("[t%d] 🧬 物质#%d @%x E=%d eff=%d\n", t, sub.ID, p.pc, p.energy, p.efficiency)
		}
	}

	// 检测消失的物质
	if lastSeenTick > 0 {
		for _, sub := range substances {
			if sub.StableTicks > 50 && !thisRound[sub.CodeHash] && lastSeenHashes[sub.CodeHash] {
				// 推断死因
				reason := "未知"
				if sub.Energy < 50 { reason = "能量枯竭" }
				if sub.Efficiency < 10000 { reason = "效率过低" }
				if sub.StableTicks > 500 { reason = "代码漂移(变异累积)" }
				dr := DeathRecord{
					SubID: sub.ID, Hash: sub.CodeHash, Died: t,
					Age: sub.StableTicks, LastE: sub.Energy,
					LastEff: sub.Efficiency, LastPC: sub.PC, Reason: reason,
				}
				subDeaths = append(subDeaths, dr)
				fmt.Printf("[t%d] 💀 物质#%d 死亡 hash=%016x 存活%d tick 原因:%s E=%d eff=%d\n",
					t, sub.ID, sub.CodeHash, sub.StableTicks, reason, sub.Energy, sub.Efficiency)
			}
		}
	}

	// 更新追踪
	lastSeenHashes = thisRound
	lastSeenTick = t
}

func updateSnapshot() {
	step := MEMSIZE/(256*256); if step<1 { step=1 }
	snapMutex.Lock(); defer snapMutex.Unlock()
	for i:=0;i<256*256;i++ { s:=0; base:=i*step; end:=base+step; if end>MEMSIZE { end=MEMSIZE }; for j:=base;j<end;j++ { s+=int(mem[j]) }; snapshot[i]=byte(s/(end-base)) }
	for i:=range snapshotPcs { snapshotPcs[i]=0 }
	for i:=0;i<MAX_PCS;i++ { if pcs[i]!=nil && atomic.LoadInt32(&pcs[i].alive)==1 { pos:=int(pcs[i].pc%int32(MEMSIZE))/step; if pos<256*256 && snapshotPcs[pos]<250 { snapshotPcs[pos]+=45 } } }
}

func startServer() {
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) { w.Header().Set("Content-Type","text/html;charset=utf-8"); w.Write([]byte(htmlPage)) })
	http.HandleFunc("/state", func(w http.ResponseWriter, r *http.Request) {
		alive:=[]map[string]interface{}{}
		for i:=0;i<MAX_PCS&&len(alive)<25;i++ { p:=pcs[i]; if p!=nil && atomic.LoadInt32(&p.alive)==1 { alive=append(alive,map[string]interface{}{"pc":p.pc,"energy":atomic.LoadInt32(&p.energy),"insts":p.insts,"eff":p.efficiency,"hash":p.codeHash}) } }
		subLock.Lock(); subs:=substances; subLock.Unlock()
		json.NewEncoder(w).Encode(map[string]interface{}{"tick":atomic.LoadInt64(&tick),"n_pcs":atomic.LoadInt64(&nPcs),"max_pcs":MAX_PCS,"births":atomic.LoadInt64(&births),"deaths":atomic.LoadInt64(&deaths),"copies":atomic.LoadInt64(&copies),"writes":atomic.LoadInt64(&writes),"paused":paused,"speed":speed,"alive":alive,"substances":subs})
	})
	http.HandleFunc("/map", func(w http.ResponseWriter, r *http.Request) { snapMutex.RLock(); defer snapMutex.RUnlock(); json.NewEncoder(w).Encode(map[string]interface{}{"energy":snapshot,"pcs":snapshotPcs}) })
	http.HandleFunc("/deaths", func(w http.ResponseWriter, r *http.Request) {
		subLock.Lock(); defer subLock.Unlock()
		json.NewEncoder(w).Encode(subDeaths)
	})
	http.HandleFunc("/save", func(w http.ResponseWriter, r *http.Request) { saveState(); w.Write([]byte("saved")) })
	http.HandleFunc("/pause", func(w http.ResponseWriter, r *http.Request) { paused = !paused })
	http.HandleFunc("/speed/", func(w http.ResponseWriter, r *http.Request) { fmt.Sscanf(r.URL.Path,"/speed/%d",&speed); if speed<1 { speed=1 } })
	fmt.Printf("🌏 http://localhost:%d\n", PORT)
	http.ListenAndServe(fmt.Sprintf(":%d",PORT), nil)
}

func main() {
	runtime.GOMAXPROCS(runtime.NumCPU())
	fmt.Println("══ Genesis v4 · Go · 256MB · 存档恢复 ══")

	// 分配内存(必须在restore或init之前)
	mem = make([]byte, MEMSIZE)

	// 尝试恢复存档
	if !restoreState() {
		initUniverse()
	}
	fmt.Printf("  衰减:%dtick\n", DECAY_EVERY)

	// 信号处理 — Ctrl+C存档退出
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sig
		paused = true
		time.Sleep(200 * time.Millisecond)
		saveState()
		fmt.Printf("\n💾 存档t%d 粒子:%d 物质:%d\n", atomic.LoadInt64(&tick), atomic.LoadInt64(&nPcs), len(substances))
		os.Exit(0)
	}()

	// 主循环+自动存档(每5000 tick)
	go func() {
		lastSave := atomic.LoadInt64(&tick)
		for {
			if !paused {
				for s := int64(0); s < speed; s++ { evolveRound() }
				t := atomic.LoadInt64(&tick)
				if t-lastSave >= 5000 {
					saveState()
					lastSave = t
					fmt.Printf("[t%d] 💾 自动存档 粒子:%d 物质:%d\n", t, atomic.LoadInt64(&nPcs), len(substances))
				}
				time.Sleep(3 * time.Millisecond)
			} else { time.Sleep(200 * time.Millisecond) }
		}
	}()
	startServer()
}

const htmlPage = ` + "`" + `<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Genesis v4</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#020208;color:#aaa;font-family:monospace;overflow:hidden;user-select:none}
canvas{display:block;image-rendering:pixelated;position:absolute;top:0;left:0}
#hud{position:fixed;top:8px;left:8px;font-size:10px;line-height:1.6;z-index:10;background:rgba(2,2,8,.88);padding:8px 12px;border:1px solid #1a1a30}
#hud .lbl{color:#555}#hud .val{color:#aaa}
#hud .title{color:#555;font-size:9px;letter-spacing:2px;margin-bottom:3px}
#sub{position:fixed;top:8px;right:8px;font-size:9px;line-height:1.4;z-index:10;background:rgba(2,2,8,.9);padding:8px 12px;border:1px solid #1a1a30;max-width:320px;max-height:80vh;overflow-y:auto}
#sub .s{color:#575;border-bottom:1px solid #111;padding:3px 0}
#sub .s.new{color:#9a6}
#controls{position:fixed;bottom:10px;left:10px;display:flex;gap:5px;z-index:10}
button{background:#0a0a20;color:#777;border:1px solid #1a1a30;padding:5px 10px;cursor:pointer;font-family:monospace;font-size:10px}
button:hover{background:#1a1a30;color:#aaa}
button.active{background:#0a1a0a;color:#6a6;border-color:#262}
</style></head><body>
<canvas id="ce"></canvas><canvas id="cp"></canvas>
<div id="hud">
 <div class="title">Genesis v4 · 物质监测</div>
 <span class="lbl">tick</span> <span class="val" id="vt">0</span>
 <span class="lbl" style="margin-left:4px">粒子</span> <span class="val" id="vn">0</span>
 <span class="lbl" style="margin-left:4px">复制</span> <span class="val" id="vc">0</span>
 <span class="lbl" style="margin-left:4px">物质</span> <span class="val" id="vsub">0</span>
</div>
<div id="sub"><div class="title" style="color:#444">物质记录</div><div id="sublist"></div></div>
<div id="controls">
 <button onclick="f('/pause')" id="bp">⏯</button>
 <button onclick="f('/speed/1')">1x</button>
 <button onclick="f('/speed/20')">20x</button>
 <button onclick="f('/speed/100')" class="active">100x</button>
</div>
<script>
const ce=document.getElementById('ce'),ctxe=ce.getContext('2d');
const cp=document.getElementById('cp'),ctxp=cp.getContext('2d');
let energy=[],pmap=[];
function resize(){ce.width=cp.width=innerWidth;ce.height=cp.height=innerHeight}
window.onresize=resize;resize();
function drawE(){
 const S=256,img=ctxe.createImageData(S,S);
 for(let i=0;i<S*S;i++){const v=energy[i]||0,p=i*4;
  if(v<8){img.data[p]=1;img.data[p+1]=1;img.data[p+2]=4+v/2}
  else if(v<30){img.data[p]=v/2;img.data[p+1]=v/4;img.data[p+2]=14+v/3}
  else if(v<80){img.data[p]=v;img.data[p+1]=v/2;img.data[p+2]=20+v/4}
  else if(v<160){img.data[p]=200+v/3;img.data[p+1]=v;img.data[p+2]=10+v/6}
  else{img.data[p]=255;img.data[p+1]=160+v/3;img.data[p+2]=v>220?150:10+v/4}
  img.data[p+3]=255}
 const tmp=document.createElement('canvas');tmp.width=tmp.height=S;
 tmp.getContext('2d').putImageData(img,0,0);
 ctxe.imageSmoothingEnabled=false;ctxe.globalAlpha=0.85;
 ctxe.drawImage(tmp,0,0,ce.width,ce.height);ctxe.globalAlpha=1;
}
function drawP(){
 ctxp.clearRect(0,0,cp.width,cp.height);const S=256,img=ctxp.createImageData(S,S);
 for(let i=0;i<S*S;i++){const v=pmap[i]||0,p=i*4;
  if(v>0){img.data[p]=255;img.data[p+1]=100+v/2;img.data[p+2]=0;img.data[p+3]=Math.min(200,v*2)}
  else img.data[p+3]=0}
 const tmp=document.createElement('canvas');tmp.width=tmp.height=S;
 tmp.getContext('2d').putImageData(img,0,0);
 ctxp.imageSmoothingEnabled=false;ctxp.drawImage(tmp,0,0,cp.width,cp.height);
}
async function tickfn(){
 try{let r=await fetch('/map'),d=await r.json();energy=d.energy;pmap=d.pcs}catch(e){}
 drawE();drawP();
}
async function poll(){
 try{let r=await fetch('/state'),s=await r.json();
  document.getElementById('vt').textContent=Math.floor(s.tick/1000)+'K';
  document.getElementById('vn').textContent=s.n_pcs;
  document.getElementById('vc').textContent=Math.floor(s.copies/1000)+'K';
  document.getElementById('vsub').textContent=(s.substances||[]).length;
  if(s.substances&&s.substances.length){
   let h='';const ss=s.substances;
   for(let i=ss.length-1;i>=Math.max(0,ss.length-8);i--){
    const m=ss[i];
    h+='<div class="s'+(i>=ss.length-3?' new':'')+'">#'+m.id+' @'+m.pc.toString(16)+' E='+m.energy+' insts='+Math.floor(m.insts/1000)+'K t='+m.stable_ticks+'</div>';
   }
   document.getElementById('sublist').innerHTML=h;
  }
 }catch(e){}
}
async function f(url){await fetch(url)}
setInterval(tickfn,500);setInterval(poll,2000);tickfn();poll();
</script></body></html>` + "`" + `
`