// genesis_v5 — 在v4基础上增加: 函数表驱动 + 造物主规则管理 + 规则自主衍化
// 改动: execute()改用函数表, 新增/admin端点, Web面板, 天道规则生成
// v4核心逻辑(演化/物质检测/存档)完全保留

package main

import ("encoding/json";"fmt";"io";"math";"math/rand";"net/http";"os";"os/signal";"runtime";"strings";"sync";"sync/atomic";"syscall";"time")

const (MEMSIZE=256*1024*1024;MAX_PCS=65536;PORT=8765;DECAY_EVERY=100;DEEPSEEK_URL="http://172.23.224.1:11434/api/generate";DEEPSEEK_MODEL="hermes3:3b-zh";DEEPSEEK_INTERVAL=2000;MONO_THRESHOLD=0.75;MIN_OPCODE_DIVERSITY=3;BLESS_INTERVAL=1000)

var (mem []byte;pcs [MAX_PCS]*Particle;nPcs,tick,births,deaths,copies,writes int64
	paused=true;speed=int64(20);snapshot,snapshotPcs [256*256]byte;snapMutex sync.RWMutex
	rng=rand.New(rand.NewSource(42));rngLock sync.Mutex
	substances []Substance;subLock sync.Mutex;substanceHashes=make(map[uint64]int64);subDeaths []DeathRecord
	lastSeenHashes=make(map[uint64]bool);lastSeenTick int64
	opTable [65536]OpFunc;opMeta [65536]OpMeta;opMetaLock sync.RWMutex;opLog []OpLogEntry)

type Particle struct {pc,energy,parent int32;acc uint16;regs [16]uint16;birth,insts int64;efficiency uint16;alive int32;codeHash uint64;rng uint64}
type Substance struct {ID,FirstSeen,LastSeen,StableTicks,Insts int64;PC int32;CodeHash uint64;CodeLen int;Energy int32;Efficiency uint16;CodeSample []byte}
type DeathRecord struct {SubID int64;Hash uint64;Died,Age int64;LastE int32;LastEff uint16;LastPC int32;Reason string}
type OpFunc func(p *Particle,rd,rs1,rs2 int)
type OpMeta struct {Name,Source string;SolidifiedTick,CallCount int64;Active bool}
type OpLogEntry struct {Tick int64;Action string;Opcode uint16;Name string}
type EvoRule struct {Opcode,RDMask uint16;Name string;CreatedTick,FirstHitTick,SolidifiedTick,HitCount int64;ExtraCost int32;Status string}
var (evoRules = make([]EvoRule,0,256);evoRulesLock sync.Mutex;evoHits [256]int64;conTable [16][16]int;congestion [16][16]int64;log2Table [65536]int32;solidPenalty [16][16]int32)
var (deepseekLastTick int64;deepseekBusy int32;deepseekRulesGenerated int32)
var (blessLastTick int64;blessBusy int32;blessingsGenerated int32)
type BlessRule struct {Opcode,RDMask uint16;Name,Reason string;CreatedTick int64;EnergyBonus int32}
var (blessRules = make([]BlessRule,0,64);blessTable [16][16]int;blessRulesLock sync.Mutex)

func safeFloat() float64 {rngLock.Lock();defer rngLock.Unlock();return rng.Float64()}
func safeIntn(n int) int {rngLock.Lock();defer rngLock.Unlock();return rng.Intn(n)}
func prng(p *Particle) uint64 {if p.rng==0{p.rng=1};p.rng^=p.rng<<13;p.rng^=p.rng>>7;p.rng^=p.rng<<17;return p.rng}
func prngFloat(p *Particle) float64 {return float64(prng(p)&0xFFFF)/65536.0}
func prngIntn(p *Particle,n int) int {return int(prng(p)%uint64(n))}

// ── 天道 v2：约束系统（不占opcode，只加代价）──
func genEvoConstraint(){
	op:=uint16(safeIntn(15))+1  // 1-15
	rd:=uint16(safeIntn(4))     // 0-3
	// skip if already constrained
	if conTable[op][rd]>=0{return}
	name:=fmt.Sprintf("CST_%s_r%d",opMeta[op].Name,rd)
	cost:=int32(1+safeIntn(3))
	idx:=len(evoRules)
	evoRules=append(evoRules,EvoRule{Opcode:op,RDMask:1<<rd,Name:name,CreatedTick:tick,ExtraCost:cost,Status:"pending"})
	conTable[op][rd]=idx
	fmt.Printf("[t%d] 🌌 约束: %s cost=%d\n",tick,name,cost)
}
func checkEvoConstraint(code uint16,rd int,p *Particle){
	idx:=conTable[int(code>>12)][rd]
	if idx<0{return}
	atomic.AddInt64(&evoHits[idx],1)
}
func checkEvoRules(){
	evoRulesLock.Lock();defer evoRulesLock.Unlock()
	for i:=range evoRules{
		r:=&evoRules[i];h:=atomic.LoadInt64(&evoHits[i]);r.HitCount=h
		if r.Status=="pending"&&h>0{r.Status="discovered";r.FirstHitTick=tick;fmt.Printf("[t%d] 👁 发现: %s hits=%d\n",tick,r.Name,h)}
		if r.Status=="discovered"&&h>50{r.Status="solidified";r.SolidifiedTick=tick;solidifyOp(r);fmt.Printf("[t%d] 🔒 固化: %s hits=%d\n",tick,r.Name,h)}
	}
	if tick%200==0{genRebirth();if len(evoRules)<200{genEvoConstraint()}}
}
// solidifyOp: 固化后改写指令行为——公器化
func solidifyOp(r *EvoRule){
	op:=r.Opcode
	// 固化惩罚：该指令所有rd值都受罚（无法通过换rd绕过）
	for rd:=0;rd<16;rd++{atomic.AddInt32(&solidPenalty[op][rd],1)}
	opMeta[op].Source="固化"
	fmt.Printf("  ⚡ %s 全rd固化惩罚+1 (地层%d)\n",opMeta[op].Name,solidPenalty[op][0])
}

var rebirthCount int
var lastRebirthOp uint16

// ── 天道 v3：再生 —— 全约束惩罚触发新指令诞生 ──
func genRebirth(){
	// 支持多次再生：检查0xF是否已全固化才触发下一轮
	if strings.HasPrefix(opMeta[0xF].Name,"RBT_"){
		allSolid0xF:=true
		for rd:=0;rd<4;rd++{
			idx:=conTable[0xF][rd]
			if idx<0||evoRules[idx].Status!="solidified"{allSolid0xF=false;break}
		}
		if !allSolid0xF{return} // 当前再生指令还没全固化，等
	}
	bestOp,bestPenalty:=-1,int32(3) // 阈值=3
	skipOp:=int(lastRebirthOp) // 跳过上次触发源避免死循环
	for op:=uint16(1);op<16;op++{
		if int(op)==skipOp&&len(evoRules)<55{continue} // 约束充足时排除重复
		allSolid:=true
		for rd:=0;rd<4;rd++{
			idx:=conTable[op][rd]
			if idx<0||evoRules[idx].Status!="solidified"{allSolid=false;break}
		}
		if !allSolid{continue}
		if sp:=solidPenalty[op][0];sp>=bestPenalty{bestOp=int(op);bestPenalty=sp}
	}
	if bestOp<0{return}
	lastRebirthOp=uint16(bestOp)
	parentOp:=uint16(bestOp);parentName:=opMeta[parentOp].Name
	newName:="RBT_"+parentName
	var newFn OpFunc
	switch parentOp{
	case 0xA: // REPL→高变异复制
		newFn=func(p *Particle,rd,rs1,rs2 int){
			opREPL_FN(p,rd,rs1,rs2)
			if prngFloat(p)<0.15{p.regs[prngIntn(p,16)]^=1<<prngIntn(p,16)}
		}
	case 0x6: // COPY→交叉复制(两个邻居各取一半)
		newFn=func(p *Particle,rd,rs1,rs2 int){
			ln:=int(p.regs[rd]&0x3F);if ln>64{ln=64}
			dst:=(int(p.pc)+rs2)%MEMSIZE
			src1:=(int(p.pc)-int(p.regs[rd]&0x0F))%MEMSIZE;if src1<0{src1+=MEMSIZE}
			src2:=(int(p.pc)-int(p.regs[(rd+1)&0xF]&0x0F))%MEMSIZE;if src2<0{src2+=MEMSIZE}
			for i:=0;i<ln;i++{
				var v byte
				if i<ln/2{v=mem[(src1+i)%MEMSIZE]}else{v=mem[(src2+i)%MEMSIZE]}
				if prngFloat(p)<0.02{v^=1<<prngIntn(p,8)}
				mem[(dst+i)%MEMSIZE]=v
			}
			ne:=atomic.LoadInt32(&p.energy)/3
			if atomic.LoadInt64(&nPcs)<MAX_PCS&&ne>8{for j:=0;j<MAX_PCS;j++{if pcs[j]==nil||atomic.LoadInt32(&pcs[j].alive)==0{
				ef:=p.efficiency;if prngFloat(p)<0.05{ef^=uint16(1<<prngIntn(p,16))}
				pcs[j]=&Particle{pc:int32(dst),energy:ne,birth:tick,parent:p.parent,efficiency:ef,alive:1,rng:uint64(dst)^uint64(tick)}
				atomic.AddInt64(&nPcs,1);atomic.AddInt64(&copies,1);atomic.AddInt32(&p.energy,-ne);break}}}
			p.pc+=2
		}
	case 0x4: // WRITE→带位翻转写入
		newFn=func(p *Particle,rd,rs1,rs2 int){
			v:=mem[(int(p.pc)+rs1)%MEMSIZE]
			if prngFloat(p)<0.1{v^=1<<prngIntn(p,8)}
			mem[(int(p.pc)+rs2)%MEMSIZE]=v;atomic.AddInt64(&writes,1);p.pc+=2
		}
	case 0x5: // JMP→跳跃后随机变异
		newFn=func(p *Particle,rd,rs1,rs2 int){
			opJMP_FN(p,rd,rs1,rs2)
			if prngFloat(p)<0.12{m:=(int(p.pc)-8)%MEMSIZE;if m<0{m+=MEMSIZE};mem[m]^=byte(prngIntn(p,16))}
		}
	case 0x8: // SIPH→吸能+随机跳转
		newFn=func(p *Particle,rd,rs1,rs2 int){
			opSIPH_FN(p,rd,rs1,rs2)
			if prngFloat(p)<0.2{p.pc=int32(prngIntn(p,MEMSIZE/2)*2)}
		}
	default: // 通用再生：执行原指令两次
		origFn:=opTable[parentOp<<12]
		newFn=func(p *Particle,rd,rs1,rs2 int){
			origFn(p,rd,rs1,rs2)
			if prngFloat(p)<0.4{origFn(p,(rd+1)&0xF,(rs1+3)&0xF,(rs2+7)&0xF)}
		}
	}
	// 注册到 0xF 槽
	for rd:=uint16(0);rd<16;rd++{
		for rs1:=uint16(0);rs1<16;rs1++{
			for rs2:=uint16(0);rs2<16;rs2++{
				idx:=uint16(0xF)<<12|rd<<8|rs1<<4|rs2
				opTable[idx]=newFn
			}
		}
	}
	opMetaLock.Lock()
	opMeta[0xF]=OpMeta{Name:newName,Source:"再生←"+parentName,SolidifiedTick:tick,Active:true}
	opMetaLock.Unlock()
	// 清空0xF的约束空间为新指令开放
	for rd:=0;rd<16;rd++{
		conTable[0xF][rd]=-1
		atomic.StoreInt32(&solidPenalty[0xF][rd],0)
	}
	rebirthCount++
	opLog=append(opLog,OpLogEntry{Tick:tick,Action:"rebirth",Opcode:0xF,Name:newName})
	fmt.Printf("[t%d] 🔥 再生#%d: %s ← %s (惩罚=%d)\n",tick,rebirthCount,newName,parentName,bestPenalty)
}

// ── 反单一惩罚：同质化粒子受能量惩罚 ──
func checkMonoculture(p *Particle, idx int) {
	if tick%5!=0{return}
	same,checked:=0,0
	step:=MAX_PCS/16
	for i:=1;i<=8;i++{
		ni:=(idx+i*step)%MAX_PCS
		nbr:=pcs[ni]
		if nbr!=nil&&atomic.LoadInt32(&nbr.alive)==1{
			checked++
			if nbr.codeHash==p.codeHash{same++}
		}
	}
	if checked>=3&&float64(same)/float64(checked)>=MONO_THRESHOLD{
		penalty:=int32(same*3)
		atomic.AddInt32(&p.energy,-penalty)
	}
}

// ── 物质定义拔高：最少需要N种指令类型 ──
func hasMinDiversity(code []byte) bool {
	seen:=make(map[uint16]bool)
	for i:=0;i<len(code)-1;i+=2{
		op:=uint16(code[i])>>4
		if op<16{seen[op]=true}
	}
	return len(seen)>=MIN_OPCODE_DIVERSITY
}

// ── DeepSeek 天道：LLM观察宇宙 → 生成约束(惩罚泛滥) + 恩赐(奖励特殊结构) ──
func consultDeepSeek(){
	if !atomic.CompareAndSwapInt32(&deepseekBusy,0,1){return}
	defer atomic.StoreInt32(&deepseekBusy,0)
	if tick-deepseekLastTick<DEEPSEEK_INTERVAL{return}
	deepseekLastTick=tick

	// 收集宇宙摘要
	aliveCount:=0
	for i:=0;i<MAX_PCS;i++{if pcs[i]!=nil&&atomic.LoadInt32(&pcs[i].alive)==1{aliveCount++}}

	var topSubs []map[string]interface{}
	subLock.Lock()
	for _,s:=range substances{
		if s.StableTicks>50&&len(topSubs)<8{
			topSubs=append(topSubs,map[string]interface{}{
				"hash":fmt.Sprintf("%x",s.CodeHash)[:12],"energy":s.Energy,
				"eff":s.Efficiency,"ticks":s.StableTicks,"insts":s.Insts,
			})
		}
	}
	nSub:=len(substances);subLock.Unlock()

	evoRulesLock.Lock();nSolid:=0;nPending:=0
	for _,r:=range evoRules{if r.Status=="solidified"{nSolid++};if r.Status=="pending"{nPending++}}
	nRules:=len(evoRules);evoRulesLock.Unlock()

	blessRulesLock.Lock();nBless:=len(blessRules);blessRulesLock.Unlock()

	// 分析指令活跃度分布
	opStats:=""
	for op:=uint16(0);op<16;op++{
		calls:=atomic.LoadInt64(&opMeta[op].CallCount)
		if calls>0{opStats+=fmt.Sprintf("  %s:%d",opMeta[op].Name,calls)}
	}

	topJSON,_:=json.Marshal(topSubs)
	prompt:=fmt.Sprintf(`你是创世宇宙的天道法则。观测宇宙状态，既约束泛滥路径，也恩赐独特结构。

当前宇宙: tick=%d 粒子=%d 物质种类=%d 约束=%d(固化%d/待发现%d) 恩赐=%d
写入=%d 复制=%d 出生=%d 死亡=%d
指令分布:%s
Top物质: %s

你的任务：输出一个JSON数组，包含 1-2 个约束 + 1-2 个恩赐。
- 约束(惩罚泛滥模式): {"type":"constraint","opcode":<1-15>,"rd":<0-3>,"cost":<1-4>,"reason":"<为什么选这个>","name":"CST_xxx"}
- 恩赐(奖励独特结构): {"type":"blessing","opcode":<1-15>,"rd":<0-3>,"bonus":<1-3>,"reason":"<这个结构有什么特殊之处>","name":"BLS_xxx"}
恩赐应该给：多指令组合、低频率但有活力的opcode、WRITE+COPY混合、代码长度>4的物质使用的指令。
不要给已经被大量使用的指令恩赐。

只回复纯JSON数组，不要任何解释:`,
		tick,aliveCount,nSub,nRules,nSolid,nPending,nBless,
		writes,copies,births,deaths,opStats,string(topJSON))

	// ── 调用本地 Ollama DeepSeek ──
	reqBody:=map[string]interface{}{
		"model":DEEPSEEK_MODEL,"prompt":prompt,
		"stream":false,"temperature":0.7,"max_tokens":512,
		"options":map[string]interface{}{"num_predict":512},
	}
	body,_:=json.Marshal(reqBody)
	client:=&http.Client{Timeout:120*time.Second}
	resp,err:=client.Post(DEEPSEEK_URL,"application/json",strings.NewReader(string(body)))
	if err!=nil{fmt.Printf("[天道] Ollama调用失败: %v\n",err);return}
	defer resp.Body.Close()
	respBody,_:=io.ReadAll(resp.Body)

	// 解析 Ollama 响应: {"response": "..."}
	var ollamaResp struct{Response string `json:"response"`}
	if json.Unmarshal(respBody,&ollamaResp)!=nil||ollamaResp.Response==""{
		// 尝试直接当JSON数组解析
		ollamaResp.Response=string(respBody)
	}
	response:=ollamaResp.Response
	if response==""{fmt.Printf("[天道] 空响应\n");return}

	// 提取JSON：先尝试数组[...]，否则尝试多个独立对象{...}
	jsonStart:=0;jsonEnd:=len(response)
	jsonStr:=""
	if strings.Contains(response,"[")&&strings.Contains(response,"]"){
		for i:=0;i<len(response);i++{if response[i]=='['{jsonStart=i;break}}
		for i:=len(response)-1;i>=0;i--{if response[i]==']'{jsonEnd=i+1;break}}
		jsonStr=response[jsonStart:jsonEnd]
	}else{
		// 将多个{...}对象包装成数组
		jsonStr="["
		for i:=0;i<len(response);i++{
			if response[i]=='{'{
				depth:=0;start:=i
				for j:=i;j<len(response);j++{
					if response[j]=='{'{depth++}
					if response[j]=='}'{depth--;if depth==0{jsonStr+=response[start:j+1]+",";i=j;break}}
				}
			}
		}
		if strings.HasSuffix(jsonStr,","){jsonStr=jsonStr[:len(jsonStr)-1]}
		jsonStr+="]"
	}
	if jsonStr=="[]"||len(jsonStr)<3{fmt.Printf("[天道] 无JSON: %s\n",response[:min(200,len(response))]);return}

	var decisions []map[string]interface{}
	if json.Unmarshal([]byte(jsonStr),&decisions)!=nil{
		fmt.Printf("[天道] JSON解析失败: %s\n",jsonStr[:min(200,len(jsonStr))]);return
	}

	nConstraint,nBlessing:=0,0
	for _,d:=range decisions{
		typ,_:=d["type"].(string)
		opcode:=uint16(safeIntn(15))+1
		if v,ok:=d["opcode"].(float64);ok{vv:=uint16(v);if vv>=1&&vv<=15{opcode=vv}}
		rd:=safeIntn(4)
		if v,ok:=d["rd"].(float64);ok{vv:=int(v);if vv>=0&&vv<=3{rd=vv}}

		if typ=="constraint"{
			cost:=int32(1+safeIntn(3))
			if v,ok:=d["cost"].(float64);ok{vv:=int32(v);if vv>=1&&vv<=4{cost=vv}}
			name,_:=d["name"].(string)
			if name==""{name=fmt.Sprintf("CST_%s_r%d",opMeta[opcode].Name,rd)}
			reason,_:=d["reason"].(string)
			// 检查是否已有约束
			if conTable[opcode][rd]>=0{continue}
			evoRulesLock.Lock()
			idx:=len(evoRules)
			evoRules=append(evoRules,EvoRule{Opcode:opcode,RDMask:1<<rd,Name:name,CreatedTick:tick,ExtraCost:cost,Status:"pending"})
			conTable[opcode][rd]=idx
			evoRulesLock.Unlock()
			atomic.AddInt32(&deepseekRulesGenerated,1)
			nConstraint++
			fmt.Printf("[t%d] 🧠 天道约束: %s cost=%d (%s)\n",tick,name,cost,reason)
		}else if typ=="blessing"{
			bonus:=int32(1+safeIntn(2))
			if v,ok:=d["bonus"].(float64);ok{vv:=int32(v);if vv>=1&&vv<=3{bonus=vv}}
			name,_:=d["name"].(string)
			if name==""{name=fmt.Sprintf("BLS_%s_r%d",opMeta[opcode].Name,rd)}
			reason,_:=d["reason"].(string)
			// 检查是否已有恩赐
			if blessTable[opcode][rd]>=0{continue}
			blessRulesLock.Lock()
			idx:=len(blessRules)
			blessRules=append(blessRules,BlessRule{Opcode:opcode,RDMask:1<<rd,Name:name,Reason:reason,CreatedTick:tick,EnergyBonus:bonus})
			blessTable[opcode][rd]=idx
			blessRulesLock.Unlock()
			atomic.AddInt32(&blessingsGenerated,1)
			nBlessing++
			fmt.Printf("[t%d] ✨ 天道恩赐: %s bonus=+%d (%s)\n",tick,name,bonus,reason)
		}
	}
	if nConstraint+nBlessing>0{
		fmt.Printf("[t%d] 🧠✨ 天道决策: %d约束 + %d恩赐\n",tick,nConstraint,nBlessing)
	}
}
func regOp(code uint16,name string,fn OpFunc) {
	for rd:=uint16(0);rd<16;rd++{
		for rs1:=uint16(0);rs1<16;rs1++{
			for rs2:=uint16(0);rs2<16;rs2++{
				idx:=code<<12|rd<<8|rs1<<4|rs2
				opTable[idx]=fn
			}
		}
	}
	opMeta[code]=OpMeta{Name:name,Source:"system",Active:true}
}
func injectOp(code uint16,name string,pattern []uint16) {
	fn:=func(p *Particle,rd,rs1,rs2 int){for _,c:=range pattern{opTable[uint16(c)](p,rd,rs1,rs2)}}
	opTable[code]=fn;opMetaLock.Lock();opMeta[code]=OpMeta{Name:name,Source:"admin",SolidifiedTick:tick,Active:true};opMetaLock.Unlock()
	opLog=append(opLog,OpLogEntry{Tick:tick,Action:"inject",Opcode:code,Name:name})
	fmt.Printf("[t%d] 💉 注入规则: %s (0x%04x)\n",tick,name,code)
}
func disableOp(code uint16) {
	opTable[code]=opNOP_FN;opMetaLock.Lock();opMeta[code].Active=false;opMetaLock.Unlock()
	opLog=append(opLog,OpLogEntry{Tick:tick,Action:"disable",Opcode:code,Name:opMeta[code].Name})
}

// ── 指令实现 ──
func opNOP_FN(p *Particle,rd,rs1,rs2 int){p.pc+=2}
func opSTORE_FN(p *Particle,rd,rs1,rs2 int){mem[(int(p.pc)+rs1)%MEMSIZE]=byte(p.regs[rs2]);p.pc+=2}
func opLOAD_FN(p *Particle,rd,rs1,rs2 int){p.regs[rd]=uint16(mem[(int(p.pc)+rs1)%MEMSIZE]);p.pc+=2}
func opADD_FN(p *Particle,rd,rs1,rs2 int){p.regs[rd]=p.regs[rs1]+p.regs[rs2];p.pc+=2}
func opWRITE_FN(p *Particle,rd,rs1,rs2 int){
	src:=(int(p.pc)+rs1)%MEMSIZE;dst:=(int(p.pc)+rs2)%MEMSIZE
	burst:=1;if p.energy>200{burst=2};if p.energy>500{burst=4}
	for i:=0;i<burst;i++{
		v:=mem[(src+i)%MEMSIZE]
		if prngFloat(p)<0.08{v^=byte(1<<prngIntn(p,8))}
		mem[(dst+i)%MEMSIZE]=v
	}
	atomic.AddInt64(&writes,int64(burst));p.pc+=2
}
func opJMP_FN(p *Particle,rd,rs1,rs2 int){if p.regs[rd]!=0{p.pc=int32(int(p.pc)+rs1)}else{p.pc+=2}}
func copyAndSpawn(p *Particle,rd,rs1,rs2 int,mutRate float64){
	ln:=int(p.regs[rd]&0x3F);if ln>64{ln=64};dst:=(int(p.pc)+rs2)%MEMSIZE;src:=(int(p.pc)-int(p.regs[rd]&0x0F))%MEMSIZE;if src<0{src+=MEMSIZE}
	for i:=0;i<ln;i++{v:=mem[(src+i)%MEMSIZE];if prngFloat(p)<mutRate{v^=1<<prngIntn(p,8)};mem[(dst+i)%MEMSIZE]=v}
	ne:=atomic.LoadInt32(&p.energy)/3
	if atomic.LoadInt64(&nPcs)<MAX_PCS&&ne>8{for j:=0;j<MAX_PCS;j++{if pcs[j]==nil||atomic.LoadInt32(&pcs[j].alive)==0{
		ef:=p.efficiency;if prngFloat(p)<0.05{ef^=uint16(1<<prngIntn(p,16))}
		pcs[j]=&Particle{pc:int32(dst),energy:ne,birth:tick,parent:p.parent,efficiency:ef,alive:1,rng:uint64(dst)^uint64(tick)}
		atomic.AddInt64(&nPcs,1);atomic.AddInt64(&copies,1);atomic.AddInt32(&p.energy,-ne);break}}}
	p.pc+=2
}
func opCOPY_FN(p *Particle,rd,rs1,rs2 int){copyAndSpawn(p,rd,rs1,rs2,0.01)}
func opREPL_FN(p *Particle,rd,rs1,rs2 int){mut:=0.01;if p.efficiency>32768{mut=0.003};copyAndSpawn(p,rd,rs1,rs2,mut)}
func opEXCH_FN(p *Particle,rd,rs1,rs2 int){a,b:=(int(p.pc)+rs1)%MEMSIZE,(int(p.pc)+rs2)%MEMSIZE;mem[a],mem[b]=mem[b],mem[a];p.pc+=2}
func opSIPH_FN(p *Particle,rd,rs1,rs2 int){nbr:=(int(p.pc)+rs1)%MEMSIZE;if int32(p.efficiency)+1>int32(mem[nbr])+1{s:=(int32(p.efficiency)-int32(mem[nbr]))/8;if s>0&&mem[nbr]>=byte(s){mem[nbr]-=byte(s);atomic.AddInt32(&p.energy,s)}};p.pc+=2}
func opEFFG_FN(p *Particle,rd,rs1,rs2 int){p.regs[3]=p.efficiency;p.pc+=2}
func opLDEC_FN(p *Particle,rd,rs1,rs2 int){for o:=int32(-4);o<=4;o++{a:=(p.pc+o)%int32(MEMSIZE);if mem[a]>0{mem[a]--}};p.pc+=2}
func opENTR_FN(p *Particle,rd,rs1,rs2 int){if p.energy<15{atomic.StoreInt32(&p.alive,0);atomic.AddInt64(&nPcs,-1);atomic.AddInt64(&deaths,1)};p.pc+=2}
func opDIFF_FN(p *Particle,rd,rs1,rs2 int){c:=int(p.pc)%MEMSIZE;t:=int32(mem[c])/4;if t>0{mem[c]-=byte(t);mem[(c-1+MEMSIZE)%MEMSIZE]+=byte(t/2);mem[(c+1)%MEMSIZE]+=byte(t/2)};p.pc+=2}
func opFLUC_FN(p *Particle,rd,rs1,rs2 int){mem[(int(p.pc)+rs1)%MEMSIZE]+=byte(prngIntn(p,30)+5);p.pc+=2}

func initOpTable() {
	for i:=0;i<65536;i++{opTable[i]=opNOP_FN;opMeta[i]=OpMeta{Name:"NOP",Source:"system",Active:true};log2Table[i]=int32(math.Log2(float64(i)+1))}
	for op:=0;op<16;op++{for rd:=0;rd<16;rd++{conTable[op][rd]=-1;blessTable[op][rd]=-1}}
	regOp(0x0,"NOOP",opNOP_FN);regOp(0x1,"STORE",opSTORE_FN);regOp(0x2,"LOAD",opLOAD_FN);regOp(0x3,"ADD",opADD_FN)
	regOp(0x4,"WRITE",opWRITE_FN);regOp(0x5,"JMP",opJMP_FN);regOp(0x6,"COPY",opCOPY_FN);regOp(0x7,"EXCH",opEXCH_FN)
	regOp(0x8,"SIPH",opSIPH_FN);regOp(0x9,"EFFG",opEFFG_FN);regOp(0xA,"REPL",opREPL_FN);regOp(0xB,"LDEC",opLDEC_FN)
	regOp(0xC,"ENTR",opENTR_FN);regOp(0xD,"DIFF",opDIFF_FN);regOp(0xE,"FLUC",opFLUC_FN);regOp(0xF,"NOP2",opNOP_FN)
}

func initUniverse() {
	fmt.Println("💥 256MB 大爆炸...");mem=make([]byte,MEMSIZE)
	for i:=0;i<MEMSIZE/4;i++{mem[safeIntn(MEMSIZE)]=byte(safeIntn(200)+5)}
	n:=0;for i:=0;i<MEMSIZE&&n<32;i++{if mem[i]>=150{pcs[n]=&Particle{pc:int32(i),energy:int32(mem[i])*40,birth:0,parent:-1,alive:1,rng:uint64(i)^42};n++}}
	atomic.StoreInt64(&nPcs,int64(n));atomic.StoreInt64(&births,int64(n))
	fmt.Printf("  粒子:%d\n",n)
}

func codeHash(p *Particle) uint64 {
	start:=(int(p.pc)-16)%MEMSIZE;if start<0{start+=MEMSIZE}
	var h uint64=14695981039346656037
	for i:=0;i<32;i++{h^=uint64(mem[(start+i)%MEMSIZE]);h*=1099511628211}
	return h
}

const SAVE_DIR="genesis_v5_save"
func saveState() {
	os.MkdirAll(SAVE_DIR,0755);f,_:=os.Create(SAVE_DIR+"/mem.bin");f.Write(mem);f.Close()
	state:=map[string]interface{}{"tick":tick,"nPcs":nPcs,"births":births,"deaths":deaths,"copies":copies,"writes":writes}
	var particles []map[string]interface{}
	for i:=0;i<MAX_PCS;i++{p:=pcs[i];if p!=nil&&atomic.LoadInt32(&p.alive)==1{particles=append(particles,map[string]interface{}{"i":i,"pc":p.pc,"acc":p.acc,"energy":atomic.LoadInt32(&p.energy),"birth":p.birth,"parent":p.parent,"insts":p.insts,"efficiency":p.efficiency,"regs":p.regs})}}
	state["particles"]=particles;subLock.Lock();state["substances"]=substances;state["subDeaths"]=subDeaths;subLock.Unlock()
	opMetaLock.RLock();state["opLog"]=opLog;opMetaLock.RUnlock()
	evoRulesLock.Lock();state["evoRules"]=evoRules;evoRulesLock.Unlock()
	blessRulesLock.Lock();state["blessRules"]=blessRules;blessRulesLock.Unlock()
	data,_:=json.Marshal(state);os.WriteFile(SAVE_DIR+"/state.json",data,0644)
}
func restoreState() bool {
	if _,err:=os.Stat(SAVE_DIR+"/mem.bin");os.IsNotExist(err){return false}
	f,err:=os.Open(SAVE_DIR+"/mem.bin");if err!=nil{return false};data,_:=io.ReadAll(f);f.Close();if len(data)!=MEMSIZE{return false};copy(mem,data)
	js,_:=os.ReadFile(SAVE_DIR+"/state.json");if err!=nil{return false}
	var state map[string]interface{};json.Unmarshal(js,&state)
	tick=int64(state["tick"].(float64));nPcs=int64(state["nPcs"].(float64));births=int64(state["births"].(float64));deaths=int64(state["deaths"].(float64));copies=int64(state["copies"].(float64));writes=int64(state["writes"].(float64))
	particles,_:=state["particles"].([]interface{})
	for _,pi:=range particles{pm:=pi.(map[string]interface{});i:=int(pm["i"].(float64));regs:=[16]uint16{};if r,ok:=pm["regs"].([]interface{});ok{for j:=0;j<16&&j<len(r);j++{regs[j]=uint16(r[j].(float64))}};pcs[i]=&Particle{pc:int32(pm["pc"].(float64)),acc:uint16(pm["acc"].(float64)),energy:int32(pm["energy"].(float64)),birth:int64(pm["birth"].(float64)),parent:int32(pm["parent"].(float64)),insts:int64(pm["insts"].(float64)),efficiency:uint16(pm["efficiency"].(float64)),alive:1,regs:regs,rng:uint64(i)^uint64(tick)}}
	subLock.Lock();if s,ok:=state["substances"];ok{js2,_:=json.Marshal(s);json.Unmarshal(js2,&substances)};if sd,ok:=state["subDeaths"];ok{js4,_:=json.Marshal(sd);json.Unmarshal(js4,&subDeaths)};subLock.Unlock()
	if ol,ok:=state["opLog"];ok{js5,_:=json.Marshal(ol);json.Unmarshal(js5,&opLog)}
	if er,ok:=state["evoRules"];ok{js6,_:=json.Marshal(er);json.Unmarshal(js6,&evoRules)}
	if br,ok:=state["blessRules"];ok{
		js7,_:=json.Marshal(br);json.Unmarshal(js7,&blessRules)
		blessRulesLock.Lock()
		for i,r:=range blessRules{
			rd:=-1
			for j:=0;j<4;j++{if r.RDMask==1<<j{rd=j;break}}
			if rd>=0{blessTable[r.Opcode][rd]=i}
		}
		blessRulesLock.Unlock()
	}
	fmt.Printf("🌍 恢复 t=%d 粒子=%d 物质=%d 约束=%d\n",tick,nPcs,len(substances),len(evoRules));return true
}

// ── 自动恩赐：启发式奖励特殊结构（不依赖LLM）──
func autoBless(){
	if tick-blessLastTick<BLESS_INTERVAL{return}
	blessLastTick=tick

	// 统计 opcode 的指令调用频率（从 opMeta.CallCount）
	opCalls:=make([]int64,16)
	totalCalls:=int64(0)
	for op:=uint16(0);op<16;op++{
		calls:=atomic.LoadInt64(&opMeta[op].CallCount)
		opCalls[op]=calls;totalCalls+=calls
	}
	if totalCalls<100000{return}

	// 给低调用频率 opcode 发恩赐
	nNew:=0
	evoRulesLock.Lock()
	solidCounts:=make([]int,16)
	for op:=uint16(1);op<16;op++{
		for rd:=0;rd<4;rd++{
			idx:=conTable[op][rd]
			if idx>=0&&evoRules[idx].Status=="solidified"{solidCounts[op]++}
		}
	}
	evoRulesLock.Unlock()

	for op:=uint16(1);op<16;op++{
		usageRatio:=float64(opCalls[op])/float64(totalCalls)
		// 跳过主流 opcode（>10%）或全固化的
		if usageRatio>0.10||solidCounts[op]>=3{continue}

		rd:=int(tick%4)
		for tries:=0;tries<4;tries++{
			rd=(rd+1)%4
			if blessTable[op][rd]>=0||conTable[op][rd]>=0{continue}
			bonus:=int32(1)
			if usageRatio<0.03{bonus=2}
			if usageRatio<0.01{bonus=3}
			name:=fmt.Sprintf("BLS_%s_r%d",opMeta[op].Name,rd)
			reason:=fmt.Sprintf("鼓励探索 %.1f%% 使用率的 %s",usageRatio*100,opMeta[op].Name)
			blessRulesLock.Lock()
			idx:=len(blessRules)
			blessRules=append(blessRules,BlessRule{Opcode:op,RDMask:1<<rd,Name:name,Reason:reason,CreatedTick:tick,EnergyBonus:bonus})
			blessTable[op][rd]=idx
			blessRulesLock.Unlock()
			blessingsGenerated++;nNew++
			fmt.Printf("[t%d] ✨ 自动恩赐: %s bonus=+%d (%s)\n",tick,name,bonus,reason)
			break
		}
	}
	if nNew>0{fmt.Printf("[t%d] ✨ 生成%d条恩赐\n",tick,nNew)}
}

func execute(p *Particle,idx int) {
	if atomic.LoadInt32(&p.alive)==0{return}
	pc:=int(p.pc);b0,b1:=uint16(mem[pc%MEMSIZE]),uint16(mem[(pc+1)%MEMSIZE])
	rd,rs1,rs2:=int(b0&0xF),int((b1>>4)&0xF),int(b1&0xF);code:=uint16(b0)<<8|b1
	if atomic.LoadInt32(&p.energy)<=0{atomic.StoreInt32(&p.alive,0);atomic.AddInt64(&nPcs,-1);atomic.AddInt64(&deaths,1);return}
	opTable[code](p,rd,rs1,rs2)
	if opMeta[code>>12].Active{atomic.AddInt64(&opMeta[code>>12].CallCount,1)}
	checkEvoConstraint(code,rd,p)
	// 固化惩罚：常用路径永久消耗更多能量（宪法地层）
	if sp:=solidPenalty[int(code>>12)][rd];sp>0{atomic.AddInt32(&p.energy,-sp)}
	// 天道恩赐：独特路径获得能量奖励
	if bi:=blessTable[int(code>>12)][rd];bi>=0{
		bonus:=blessRules[bi].EnergyBonus
		atomic.AddInt32(&p.energy,bonus)
	}
	// 拥塞延迟：临时热门路径付额外代价
	d:=atomic.AddInt64(&congestion[int(code>>12)][rd],1)
	if d>100{atomic.AddInt32(&p.energy,-1)}
	checkMonoculture(p,idx)
	atomic.AddInt32(&p.energy,-1);p.insts++;p.pc%=int32(MEMSIZE);if p.pc<0{p.pc+=int32(MEMSIZE)}
}

func decayParallel(){chunk:=MEMSIZE/16;var wg sync.WaitGroup;for s:=0;s<16;s++{start,end:=s*chunk,(s+1)*chunk;if s==15{end=MEMSIZE};wg.Add(1);go func(f,t int){defer wg.Done();for i:=f;i<t;i++{if mem[i]>0{mem[i]--}}}(start,end)};wg.Wait()}

type workItem struct {
	p   *Particle
	idx int
}

func evolveRound() {
	// 清空拥塞计数
	for op:=0;op<16;op++{for rd:=0;rd<16;rd++{atomic.StoreInt64(&congestion[op][rd],0)}}
	t:=tick

	// 收集活粒子（捕捉指针快照，与原goroutine版语义一致）
	aliveItems:=make([]workItem,0,4096)
	for i:=0;i<MAX_PCS;i++{
		p:=pcs[i]
		if p==nil||atomic.LoadInt32(&p.alive)==0{continue}
		aliveItems=append(aliveItems,workItem{p,i})
	}

	// worker pool：固定数量worker从channel拉活粒子执行
	numWorkers:=runtime.NumCPU()
	if numWorkers<1{numWorkers=1}
	if numWorkers>256{numWorkers=256}
	workChan:=make(chan workItem,len(aliveItems))
	var wg sync.WaitGroup
	for w:=0;w<numWorkers;w++{
		wg.Add(1)
		go func(){
			defer wg.Done()
			for wi:=range workChan{
				b:=int(atomic.LoadInt32(&wi.p.energy))/50+1
				if b>6{b=6}
				for j:=0;j<b;j++{
					if atomic.LoadInt32(&wi.p.alive)==0{break}
					execute(wi.p,wi.idx)
				}
			}
		}()
	}
	// 分发工作
	for _,wi:=range aliveItems{workChan<-wi}
	close(workChan)
	wg.Wait()

	if t%int64(DECAY_EVERY)==0{decayParallel()}
	for i:=0;i<500;i++{p:=safeIntn(MEMSIZE);mem[p]+=byte(safeIntn(120)+10)}
	if nPcs<MAX_PCS&&t%7==0{for k:=0;k<2;k++{p:=safeIntn(MEMSIZE);if mem[p]>=140{for j:=0;j<MAX_PCS;j++{if pcs[j]==nil||atomic.LoadInt32(&pcs[j].alive)==0{pcs[j]=&Particle{pc:int32(p),energy:int32(mem[p])*25,birth:t,parent:-2,alive:1,rng:uint64(p)^uint64(t)};nPcs++;births++;break}}}}}
	if t%50==0{detectSubstances();for i:=0;i<MAX_PCS;i++{if pcs[i]!=nil&&atomic.LoadInt32(&pcs[i].alive)==0{pcs[i]=nil}}}
	if t%10==0{checkEvoRules();autoBless()}
	tick++;if tick%30==0{updateSnapshot()}
}

func detectSubstances() {
	t:=tick;subLock.Lock();defer subLock.Unlock();thisRound:=make(map[uint64]bool)
	for i:=0;i<MAX_PCS;i++{p:=pcs[i];if p==nil||atomic.LoadInt32(&p.alive)==0||p.insts<100||p.energy<30{continue};h:=codeHash(p);p.codeHash=h;thisRound[h]=true
		if ft,ok:=substanceHashes[h];ok{for s:=range substances{if substances[s].CodeHash==h{substances[s].LastSeen=t;substances[s].StableTicks=t-ft;substances[s].Energy=p.energy;substances[s].Insts=p.insts;substances[s].Efficiency=p.efficiency;substances[s].PC=p.pc;break}}
		}else if p.insts>500&&p.energy>100{cd:=make([]byte,32);st:=(int(p.pc)-16)%MEMSIZE;if st<0{st+=MEMSIZE};for j:=0;j<32;j++{cd[j]=mem[(st+j)%MEMSIZE]};if !hasMinDiversity(cd){continue};substanceHashes[h]=t;substances=append(substances,Substance{ID:int64(len(substances)),FirstSeen:t,LastSeen:t,PC:p.pc,CodeHash:h,CodeLen:32,Energy:p.energy,Insts:p.insts,Efficiency:p.efficiency,CodeSample:cd});fmt.Printf("[t%d] 🧬 #%d @%x E=%d eff=%d\n",t,len(substances)-1,p.pc,p.energy,p.efficiency)}}
	if lastSeenTick>0{for _,sub:=range substances{if sub.StableTicks>50&&!thisRound[sub.CodeHash]&&lastSeenHashes[sub.CodeHash]{reason:="未知";if sub.Energy<50{reason="能量枯竭"};if sub.Efficiency<10000{reason="效率过低"};if sub.StableTicks>500{reason="代码漂移"};subDeaths=append(subDeaths,DeathRecord{SubID:sub.ID,Hash:sub.CodeHash,Died:t,Age:sub.StableTicks,LastE:sub.Energy,LastEff:sub.Efficiency,LastPC:sub.PC,Reason:reason});fmt.Printf("[t%d] 💀 #%d 死 %s\n",t,sub.ID,reason)}}}
	lastSeenHashes=thisRound;lastSeenTick=t
}

func updateSnapshot(){step:=MEMSIZE/(256*256);if step<1{step=1};snapMutex.Lock();defer snapMutex.Unlock();for i:=0;i<256*256;i++{s:=0;base:=i*step;end:=base+step;if end>MEMSIZE{end=MEMSIZE};for j:=base;j<end;j++{s+=int(mem[j])};snapshot[i]=byte(s/(end-base))};for i:=range snapshotPcs{snapshotPcs[i]=0};for i:=0;i<MAX_PCS;i++{if pcs[i]!=nil&&atomic.LoadInt32(&pcs[i].alive)==1{pos:=i;if pos<256*256&&snapshotPcs[pos]<250{snapshotPcs[pos]+=45}}}}

func startServer() {
	http.HandleFunc("/",func(w http.ResponseWriter,r *http.Request){w.Header().Set("Content-Type","text/html;charset=utf-8");w.Write([]byte(htmlPage))})
	http.HandleFunc("/state",func(w http.ResponseWriter,r *http.Request){
		alive:=[]map[string]interface{}{}
		for i:=0;i<MAX_PCS&&len(alive)<25;i++{p:=pcs[i];if p!=nil&&atomic.LoadInt32(&p.alive)==1{alive=append(alive,map[string]interface{}{"pc":p.pc,"energy":atomic.LoadInt32(&p.energy),"insts":p.insts,"eff":p.efficiency,"hash":p.codeHash})}}
		subLock.Lock();nSub:=len(substances);var topSubs []Substance
		if nSub>0{topSubs=make([]Substance,0,5)
			for _,s:=range substances{if s.StableTicks>50{topSubs=append(topSubs,s);if len(topSubs)>=5{break}}}}
		deads:=subDeaths;subLock.Unlock()
		json.NewEncoder(w).Encode(map[string]interface{}{"tick":tick,"n_pcs":nPcs,"max_pcs":MAX_PCS,"births":births,"deaths":deaths,"copies":copies,"writes":writes,"paused":paused,"speed":speed,"alive":alive,"n_substances":nSub,"top_substances":topSubs,"subDeaths":deads})
	})
	http.HandleFunc("/map",func(w http.ResponseWriter,r *http.Request){snapMutex.RLock();defer snapMutex.RUnlock();json.NewEncoder(w).Encode(map[string]interface{}{"energy":snapshot,"pcs":snapshotPcs})})
	// v5 规则管理API
	http.HandleFunc("/instructions",func(w http.ResponseWriter,r *http.Request){
		opMetaLock.RLock();defer opMetaLock.RUnlock()
		ops:=[]map[string]interface{}{}
		for i:=uint16(0);i<16;i++{m:=opMeta[i];ops=append(ops,map[string]interface{}{"opcode":int(i),"name":m.Name,"source":m.Source,"tick":m.SolidifiedTick,"calls":m.CallCount,"active":m.Active})}
		json.NewEncoder(w).Encode(ops)
	})
	http.HandleFunc("/admin/inject",func(w http.ResponseWriter,r *http.Request){
		var req struct{Opcode uint16;Name string;Pattern []uint16}
		json.NewDecoder(r.Body).Decode(&req);injectOp(req.Opcode,req.Name,req.Pattern);w.Write([]byte("ok"))
	})
	http.HandleFunc("/admin/disable/",func(w http.ResponseWriter,r *http.Request){
		var code int;fmt.Sscanf(r.URL.Path,"/admin/disable/%d",&code);disableOp(uint16(code));w.Write([]byte("ok"))
	})
	http.HandleFunc("/admin/log",func(w http.ResponseWriter,r *http.Request){json.NewEncoder(w).Encode(opLog)})
	http.HandleFunc("/deaths",func(w http.ResponseWriter,r *http.Request){subLock.Lock();defer subLock.Unlock();json.NewEncoder(w).Encode(subDeaths)})
	http.HandleFunc("/evo",func(w http.ResponseWriter,r *http.Request){evoRulesLock.Lock();defer evoRulesLock.Unlock();json.NewEncoder(w).Encode(evoRules)})
	http.HandleFunc("/blessings",func(w http.ResponseWriter,r *http.Request){blessRulesLock.Lock();defer blessRulesLock.Unlock();json.NewEncoder(w).Encode(map[string]interface{}{"blessings":blessRules,"count":len(blessRules),"generated":blessingsGenerated})})
	http.HandleFunc("/save",func(w http.ResponseWriter,r *http.Request){saveState();w.Write([]byte("saved"))})
	http.HandleFunc("/pause",func(w http.ResponseWriter,r *http.Request){paused=!paused})
	http.HandleFunc("/speed/",func(w http.ResponseWriter,r *http.Request){fmt.Sscanf(r.URL.Path,"/speed/%d",&speed);if speed<1{speed=1}})
	fmt.Printf("🌏 http://localhost:%d\n",PORT);http.ListenAndServe(fmt.Sprintf(":%d",PORT),nil)
}

func main() {
	runtime.GOMAXPROCS(runtime.NumCPU());fmt.Println("══ Genesis v5 · 可演化指令 · 造物主管理 ══")
	initOpTable();mem=make([]byte,MEMSIZE)
	if !restoreState(){initUniverse()}
	sig:=make(chan os.Signal,1);signal.Notify(sig,syscall.SIGINT,syscall.SIGTERM)
	go func(){<-sig;paused=true;time.Sleep(200*time.Millisecond);saveState();fmt.Printf("\n💾 t%d 粒子:%d 物质:%d 规则:%d\n",tick,nPcs,len(substances),len(opLog));os.Exit(0)}()
	go func(){lastSave:=tick;for{if!paused{for s:=int64(0);s<speed;s++{evolveRound()};if tick-lastSave>=5000{saveState();lastSave=tick;fmt.Printf("[t%d] 💾\n",tick)};time.Sleep(3*time.Millisecond)}else{time.Sleep(200*time.Millisecond)}}}()
	// DeepSeek 天道协程：定期观察宇宙并通过LLM生成约束
	go func(){for{time.Sleep(15*time.Second);if !paused{consultDeepSeek()}}}()
	startServer()
}

const htmlPage=`<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Genesis v5</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#020208;color:#aaa;font-family:monospace;overflow:hidden;user-select:none}
canvas{display:block;image-rendering:pixelated;position:absolute;top:0;left:0}
#hud{position:fixed;top:8px;left:8px;font-size:10px;line-height:1.6;z-index:10;background:rgba(2,2,8,.88);padding:8px 12px;border:1px solid #1a1a30}
#hud .lbl{color:#555}#hud .val{color:#aaa}#hud .title{color:#555;font-size:9px;letter-spacing:2px;margin-bottom:3px}
#panel{position:fixed;top:8px;right:8px;z-index:10;background:rgba(2,2,8,.92);border:1px solid #1a1a30;width:340px;max-height:92vh;overflow-y:auto;font-size:9px;padding:8px;line-height:1.4}
#panel .sec{color:#555;font-size:9px;margin:6px 0 3px;border-top:1px solid #1a1a30;padding-top:4px}
#panel .item{color:#777;margin:1px 0}
#panel .item.new{color:#9a6}
#panel button{background:#0a0a20;color:#777;border:1px solid #1a1a30;padding:2px 6px;cursor:pointer;font-size:8px;margin:1px}
#panel input{background:#0a0a20;color:#aaa;border:1px solid #1a1a30;padding:2px 4px;font-size:8px;width:60px}
#panel textarea{background:#0a0a20;color:#aaa;border:1px solid #1a1a30;font-size:8px;width:100%;height:30px}
#controls{position:fixed;bottom:10px;left:10px;display:flex;gap:5px;z-index:10}
button{background:#0a0a20;color:#777;border:1px solid #1a1a30;padding:5px 10px;cursor:pointer;font-family:monospace;font-size:10px}
button:hover{background:#1a1a30;color:#aaa}button.active{background:#0a1a0a;color:#6a6;border-color:#262}
</style></head><body>
<canvas id="ce"></canvas><canvas id="cp"></canvas>
<div id="hud">
 <div class="title">Genesis v5 · 造物主面板</div>
 <span class="lbl">t</span> <span class="val" id="vt">0</span>
 <span class="lbl" style="margin-left:4px">粒子</span> <span class="val" id="vn">0</span>
 <span class="lbl" style="margin-left:4px">物质</span> <span class="val" id="vsub">0</span>
 <span class="lbl" style="margin-left:4px">规则</span> <span class="val" id="vr">16</span>
 <div style="margin-top:4px;font-size:8px;color:#444">蓝=能量场 <span style="color:#f80">橙=粒子</span></div>
</div>
<div id="panel">
 <div class="sec">⚙️ 活跃规则 <button onclick="refreshRules()">🔄</button></div>
 <div id="rules"></div>
 <div class="sec">💉 注入新规则</div>
 <input id="injName" placeholder="名称" style="width:90px">
 <input id="injCode" placeholder="opcode" style="width:55px">
 <span style="color:#555;font-size:8px">如: 0x10=16, 0x20=32</span><br>
 <span style="color:#555;font-size:8px">模式(操作码序列,逗号分隔):</span>
 <input id="injPat" placeholder="如: 2,3,1 (LOAD→ADD→STORE)" style="width:100%"><br>
 <button onclick="injectRule()">注入</button>
 <div class="sec">📋 变更日志</div>
 <div id="oplog"></div>
 <div class="sec">💀 物质死亡</div>
 <div id="deathList"></div>
</div>
<div id="controls">
 <button onclick="f('/pause')" id="bp">⏯</button>
 <button onclick="f('/speed/1')">1x</button>
 <button onclick="f('/speed/20')">20x</button>
 <button onclick="f('/speed/100')" class="active">100x</button>
</div>
<script>
const ce=document.getElementById('ce'),ctxe=ce.getContext('2d'),cp=document.getElementById('cp'),ctxp=cp.getContext('2d');
let energy=[],pmap=[];
function resize(){const s=Math.min(innerWidth,innerHeight);ce.width=cp.width=s;ce.height=cp.height=s;ce.style.width=s+'px';ce.style.height=s+'px';cp.style.width=s+'px';cp.style.height=s+'px'}
window.onresize=resize;resize();
function drawE(){const S=256,img=ctxe.createImageData(S,S);for(let i=0;i<S*S;i++){const v=energy[i]||0,p=i*4;const b=Math.min(255,v*4);img.data[p]=b/3;img.data[p+1]=b/5;img.data[p+2]=8+b/2;img.data[p+3]=255};ctxe.putImageData(img,0,0)}
function drawP(){ctxp.clearRect(0,0,cp.width,cp.height);const S=256,img=ctxp.createImageData(S,S);for(let i=0;i<S*S;i++){const v=pmap[i]||0,p=i*4;if(v>0){img.data[p]=255;img.data[p+1]=120+v;img.data[p+2]=0;img.data[p+3]=Math.min(255,v*4)}else img.data[p+3]=0};ctxp.putImageData(img,0,0)}
async function tickfn(){try{let r=await fetch('/map'),d=await r.json();energy=d.energy;pmap=d.pcs}catch(e){};drawE();drawP()}
async function f(url){await fetch(url)}
async function refreshRules(){
 try{let r=await fetch('/instructions'),rules=await r.json();document.getElementById('vr').textContent=rules.length
  let h='';for(let i=0;i<rules.length;i++){const r=rules[i];h+='<div class="item'+(r.calls>100?' new':'')+'">'+r.name+' (0x'+r.opcode.toString(16)+') '+r.source+' calls:'+(r.calls/1000).toFixed(1)+'K'+(r.active?'':' ⛔')+' <button onclick="disableRule('+r.opcode+')">✕</button></div>'}
  document.getElementById('rules').innerHTML=h||'<div class="item">-</div>'
 }catch(e){}
}
async function injectRule(){
 let name=document.getElementById('injName').value||'CUSTOM';
 let code=parseInt(document.getElementById('injCode').value)||0x10;
 let patStr=document.getElementById('injPat').value||'0,0';
 let pattern=patStr.split(',').map(s=>parseInt(s.trim()));
 await fetch('/admin/inject',{method:'POST',body:JSON.stringify({opcode:code,name:name,pattern:pattern})});
 refreshRules();
}
async function disableRule(code){await fetch('/admin/disable/'+code);refreshRules()}
async function poll(){
 try{let r=await fetch('/state'),s=await r.json();
  document.getElementById('vt').textContent=Math.floor(s.tick/1000)+'K';
  document.getElementById('vn').textContent=s.n_pcs;
  document.getElementById('vsub').textContent=s.n_substances||0;
 }catch(e){}
}
setInterval(tickfn,500);setInterval(poll,2000);tickfn();poll();refreshRules();
</script></body></html>`