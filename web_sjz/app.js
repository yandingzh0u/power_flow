'use strict';
const $ = id => document.getElementById(id);
const saved = window.SJZ_DATA;
if (!saved?.scenarios?.['13'] || !saved.layouts?.['13'] || !saved.layouts?.['20'] ||
    !Array.isArray(saved.scenarios['13'].partitions?.[0]?.internal_buses)) {
  throw new Error('初始结果文件缺失或版本不匹配');
}
const state = {
  dataset:'13', scene:saved.scenarios['13'] || emptyScene(), layout:saved.layouts?.['13'] || saved.layout || {},
  selected:null, color:'partition', layers:{main:true,interface:true,distribution:true,microgrid:true},
  scale:1, offset:{x:0,y:0}, search:'', running:false, importing:false, flow:true, labels:false,
  log:[], run:null, source:'已保存结果', phase:'就绪', activePartition:null, task:null, dragging:false,
};
const canvas = $('topology'), ctx = canvas.getContext('2d');
let size = {w:0,h:0}, nodes = new Map(), edgeLookup = new Map(), stream = null, timer = null, started = 0;
let lastFrame = 0, lastPointer = null;
const colors = ['#358abd','#419d89','#bd914b','#9273b9','#c96c82','#5197a6','#849c4a','#b980aa','#5d80a8','#b78465'];
const fmt = (v,d=2) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('zh-CN',{maximumFractionDigits:d});
const exp = v => v == null ? '—' : Number(v).toExponential(3);
function emptyScene(){return {label:'未计算',buses:[],lines:[],trafos:[],partitions:[],microgrids:[],layers:{},summary:{}};}
function element(kind,id){return kind==='bus' ? nodes.get(id) : edgeLookup.get(`${kind}:${id}`);}
function rebuildIndex(){nodes=new Map(state.scene.buses.map(b=>[b.id,b]));edgeLookup=new Map([...state.scene.lines.map(e=>[`line:${e.id}`,e]),...state.scene.trafos.map(e=>[`trafo:${e.id}`,e])]);}
function setScene(payload,source){const same=state.scene.buses.length===payload.scenario.buses.length&&state.scene.buses.every((b,i)=>b.name===payload.scenario.buses[i].name&&b.kv===payload.scenario.buses[i].kv);state.scene=payload.scenario;state.layout=payload.layout;state.source=source;rebuildIndex();if(state.selected&&!element(state.selected.kind,state.selected.id))state.selected=null;render();resize();if(!same)fit();}
function addLog(message,kind='log',event={}){
  if(!message)return;
  const record={message,kind,timestamp:event.timestamp||new Date().toISOString(),elapsed_ms:event.elapsed_ms};
  state.log.push(record);
  const box=$('log'),row=document.createElement('div'),stamp=document.createElement('span');
  row.className=`log-row event-${kind}`;stamp.className='time';
  stamp.textContent=new Date(record.timestamp).toLocaleTimeString('zh-CN',{hour12:false})+(record.elapsed_ms!=null?` +${(record.elapsed_ms/1000).toFixed(3)}s`:'');
  row.append(stamp,document.createTextNode(message));
  const follow=box.scrollTop+box.clientHeight>=box.scrollHeight-45;
  box.appendChild(row);if(follow)box.scrollTop=box.scrollHeight;
  $('logCount').textContent=state.log.length;
}
function stage(text,sub='',failed=false){$('stageText').textContent=text;$('stageSub').textContent=sub;$('stageDot').className=`stage-dot${state.running?' running':''}${failed?' failed':''}`;state.phase=text;$('runPhase').textContent=text;}
function origin(text,kind=''){const node=$('resultOrigin');node.textContent=text;node.className=`result-origin ${kind}`;}
function metrics(target,rows){const root=$(target);root.replaceChildren();for(const [label,value,wide] of rows){const row=document.createElement('div'),name=document.createElement('span'),val=document.createElement('strong');row.className=`detail-metric${wide?' wide':''}`;name.textContent=label;val.textContent=value;row.append(name,val);root.appendChild(row);}}
function renderSelection(){
  const selected=state.selected,item=selected&&element(selected.kind,selected.id);
  $('detailUpdated').textContent=`${state.running?'计算中 · 电压为当前迭代值，P/Q 待收敛':state.source} · ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}`;
  $('detailEmpty').hidden=!!item;$('detailGrid').hidden=!item;
  if(!item){$('detailTitle').textContent='节点与元件状态';$('detailTag').textContent='等待选择';return;}
  $('detailTitle').textContent=item.name;
  let rows;
  if(selected.kind==='bus'){
    $('detailTag').textContent=item.microgrid_name?'微网 PCC':item.layer==='main'?'主网母线':'配网母线';
    rows=[['母线 / 电压等级',`${item.id} / ${fmt(item.kv)} kV`],['所属分区',item.partition_id?`P${String(item.partition_id).padStart(2,'0')}`:'主网'],['电压幅值',`${fmt(item.vm,6)} pu`],['电压相角',`${fmt(item.va,5)}°`],['有功净注入',`${fmt(item.p,4)} MW`],['无功净注入',`${fmt(item.q,4)} Mvar`],['节点控制',item.type||'—'],['原始有功负荷',`${fmt(item.load_mw,3)} MW`]];
  }else{
    $('detailTag').textContent=selected.kind==='line'?'线路':'变压器';
    rows=[['起点',nodes.get(item.from)?.name||item.from],['终点',nodes.get(item.to)?.name||item.to],['首端有功',`${fmt(item.p,4)} MW`],['首端无功',`${fmt(item.q,4)} Mvar`],['末端有功',`${fmt(item.p_to,4)} MW`],['末端无功',`${fmt(item.q_to,4)} Mvar`],['电流负载率',`${fmt(item.loading,3)} %`],['原始额定容量',`${fmt(item.rate_mva,3)} MVA`],['R / X（原标幺值）',`${fmt(item.r_pu,6)} / ${fmt(item.x_pu,6)}`],['原始 DAT 行号',String(item.source_line)]];
  }
  metrics('detailGrid',rows);
}
function render(){
  const s=state.scene,edges=[...s.lines,...s.trafos],rated=edges.filter(e=>e.loading!=null),max=Math.max(0,...rated.map(e=>e.loading)),q=s.summary;
  $('busKpi').textContent=fmt(s.buses.length,0);$('busSub').textContent=`母线 · 原始有功负荷 ${fmt(s.load_mw,1)} MW`;
  $('edgeKpi').textContent=`${fmt(s.lines.length,0)} / ${s.trafos.length}`;
  $('loadingKpi').textContent=rated.length?`${fmt(max,2)}%`:'—';$('loadingSub').textContent=`${rated.filter(e=>e.loading>100).length} 个元件过载 · 电流口径`;
  $('partitionKpi').textContent=`${q.converged||0} / ${q.parts||0}`;
  $('mainCount').textContent=s.layers.main_bus_count||0;$('interfaceCount').textContent=s.layers.interface_trafo_count||0;$('distributionCount').textContent=q.parts||0;$('microCount').textContent=s.microgrids.length;
  $('mapMeta').textContent=`${s.label} · ${s.buses.length} 母线 / ${s.lines.length+s.trafos.length} 支路 · 拖动平移 / 滚轮缩放`;
  $('mapEmpty').hidden=!!s.buses.length;
  renderSelection();renderAnalysis();draw();
}
function renderAnalysis(){
  const r=state.run;
  metrics('runRecord',r?[['求解算法',r.algorithm_label],['牛顿迭代次数',String(r.iterations)],['最终残差 MVA',exp(r.residual_mva)],['全网阶段耗时',`${fmt(r.solver_ms,2)} ms`],['任务总耗时',`${fmt(r.total_ms,2)} ms`],['分区校核失败',String(r.partition_failed)],['开始时间',new Date(r.started_at).toLocaleString('zh-CN')],['结束时间',new Date(r.completed_at).toLocaleString('zh-CN')],['原始输入',r.source_name],['输入 SHA-256（求解前后相同）',r.source_sha256,true]]:[['结果来源',state.source],['现场计算','点击运行开始新任务']]);
  $('runRecordTag').textContent=r?'本次现场运行':'已保存结果';
  const edges=[...state.scene.lines.map(e=>({...e,kind:'line'})),...state.scene.trafos.map(e=>({...e,kind:'trafo'}))];
  const query=$('elementSearch').value.trim().toLowerCase();
  const body=$('elementTable');body.replaceChildren();
  for(const e of edges.sort((a,b)=>(b.loading??-1)-(a.loading??-1))){
    const a=nodes.get(e.from)?.name||e.from,b=nodes.get(e.to)?.name||e.to;
    if(query&&!`${e.name} ${a} ${b}`.toLowerCase().includes(query))continue;
    const row=body.insertRow();
    const values=[e.kind==='line'?'线路':'变压器',e.name,`${a} → ${b}`,fmt(e.rate_mva,3),fmt(e.p,3),fmt(e.q,3),e.loading==null?'—':`${fmt(e.loading,2)}%`];
    values.forEach((v,i)=>{const cell=row.insertCell();cell.textContent=v;if(i===6)cell.className=e.loading>100?'danger':'';});
    row.onclick=()=>{select(e.kind,e.id);activateTab('topology');};
  }
  const parts=$('partitionTable');parts.replaceChildren();
  for(const p of state.scene.partitions){const row=parts.insertRow();[p.display_name,p.internal_buses.length,p.boundary_buses.length,p.converged==null?'待校核':p.converged?'通过':'未通过',exp(p.max_vm_delta_vs_full_pu),exp(p.max_va_delta_vs_full_deg),fmt(p.elapsed_ms,2)].forEach(v=>{row.insertCell().textContent=v;});}
  const bins=[['≤50%',0,50,'#69ad9a'],['50–80%',50,80,'#6d9cbe'],['80–100%',80,100,'#c4a366'],['>100%',100,Infinity,'#d2808a']];
  const counts=bins.map(([,lo,hi])=>edges.filter(e=>e.loading!=null&&e.loading>lo&&e.loading<=hi).length);
  counts[0]+=edges.filter(e=>e.loading===0).length;
  const dist=$('loadingDistribution');dist.replaceChildren();
  bins.forEach(([label,,,color],i)=>{const row=document.createElement('div'),name=document.createElement('span'),bar=document.createElement('div'),fill=document.createElement('i'),count=document.createElement('b');row.className='dist-row';name.textContent=label;fill.style.width=`${counts[i]/Math.max(1,...counts)*100}%`;fill.style.background=color;count.textContent=counts[i];bar.append(fill);row.append(name,bar,count);dist.append(row);});
}
function screen(id){const p=state.layout[String(id)]||[0,0];return {x:state.offset.x+p[0]*state.scale,y:state.offset.y-p[1]*state.scale};}
function resize(){const box=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;size={w:box.width,h:box.height};canvas.width=Math.round(size.w*dpr);canvas.height=Math.round(size.h*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);draw();}
function fit(){const pts=state.scene.buses.map(b=>state.layout[String(b.id)]).filter(Boolean);if(!pts.length)return;const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]),minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys);state.scale=Math.min((size.w-90)/Math.max(maxX-minX,1e-8),(size.h-110)/Math.max(maxY-minY,1e-8));state.offset={x:(size.w-(minX+maxX)*state.scale)/2,y:(size.h+(minY+maxY)*state.scale)/2+12};draw();}
function busVisible(b){return b.layer==='main'?state.layers.main:state.layers.distribution||(b.is_boundary&&state.layers.interface);}
function edgeVisible(e){return e.from_layer!==e.to_layer?state.layers.interface:e.from_layer==='main'?state.layers.main:state.layers.distribution;}
function nodeColor(b){if(state.color==='vm')return b.vm==null?'#acbac6':b.vm<.95||b.vm>1.05?'#ce5969':b.vm<.98?'#c99e45':'#43a389';if(state.color==='voltage')return b.kv>=200?'#ce7f65':b.kv>=100?'#bba24d':b.kv>=35?'#3289bb':b.kv>=10?'#4aa28c':'#9a82bc';return b.layer==='main'?'#267cb8':colors[((b.partition_id||1)-1)%colors.length];}
function draw(now=performance.now()){
  if(!size.w)return;
  ctx.clearRect(0,0,size.w,size.h);ctx.lineCap='round';const s=state.scene,selected=state.selected,points=new Map(s.buses.map(b=>[b.id,screen(b.id)])),query=state.search.toLowerCase();
  const selectedBus=selected?.kind==='bus'?selected.id:null;
  // Active partition wash follows the actual local-verification event.
  if(state.activePartition){const p=s.partitions.find(p=>p.id===state.activePartition);if(p){const pts=p.internal_buses.map(id=>points.get(id)).filter(Boolean);if(pts.length){const xs=pts.map(p=>p.x),ys=pts.map(p=>p.y);ctx.fillStyle='#2789c00a';ctx.strokeStyle='#2789c035';ctx.lineWidth=1;ctx.beginPath();ctx.roundRect(Math.min(...xs)-9,Math.min(...ys)-9,Math.max(...xs)-Math.min(...xs)+18,Math.max(...ys)-Math.min(...ys)+18,8);ctx.fill();ctx.stroke();}}}
  for(const [kind,edges] of [['line',s.lines],['trafo',s.trafos]]){
    for(let index=0;index<edges.length;index++){
      const e=edges[index];if(!edgeVisible(e))continue;const a=points.get(e.from),b=points.get(e.to);if(!a||!b)continue;
      const active=(selected?.kind===kind&&selected.id===e.id)||selectedBus===e.from||selectedBus===e.to||(query&&e.name.toLowerCase().includes(query));
      ctx.strokeStyle=active?'#0d78b4':state.color==='loading'?(e.loading==null?'#c6d1db':e.loading>100?'#cf6471':e.loading>80?'#c6a159':'#74ab9c'):e.from_layer!==e.to_layer?'#d0a267':e.from_layer==='main'?'#6d9fbe':e.is_cut_edge?'#cba463':'#bdcbd7';
      ctx.globalAlpha=active?1:.8;ctx.lineWidth=active?2.2:kind==='trafo'?1.2:e.from_layer==='main'?1:.65;
      ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
      const animated=state.running||state.flow&&e.p!=null;
      if(animated&&(active||e.from_layer==='main'||e.from_layer!==e.to_layer||index%19===0)){
        const progress=((now/2200+index*.137)%1),t=e.p!=null&&e.p<0?1-progress:progress;
        ctx.globalAlpha=.8;ctx.fillStyle=active?'#117bb7':state.running?'#5ca6c7':'#7eabbd';ctx.beginPath();ctx.arc(a.x+(b.x-a.x)*t,a.y+(b.y-a.y)*t,active?2.5:1.5,0,Math.PI*2);ctx.fill();
      }
    }
  }
  ctx.globalAlpha=1;
  for(const b of s.buses){if(!busVisible(b))continue;const p=points.get(b.id),match=query&&(b.name.toLowerCase().includes(query)||String(b.id)===query),active=selectedBus===b.id||match,r=active?4.5:b.layer==='main'?2.5:1.5;ctx.fillStyle=nodeColor(b);ctx.beginPath();ctx.arc(p.x,p.y,r,0,Math.PI*2);ctx.fill();
    if(active||b.is_boundary&&state.layers.interface){ctx.strokeStyle=active?'#176c9c':'#c59e60';ctx.lineWidth=active?1.4:.7;ctx.beginPath();ctx.arc(p.x,p.y,r+2.2,0,Math.PI*2);ctx.stroke();}
    if(active||state.labels&&(b.kv>=100||b.microgrid_name)){ctx.font='10px system-ui';ctx.fillStyle='#44647c';ctx.fillText(`${b.name} · ${b.kv}kV`,p.x+6,p.y-6);}
  }
  if(state.layers.microgrid){for(const mg of s.microgrids){const p=points.get(mg.pcc_bus);if(!p)continue;const radius=state.running?7+Math.sin(now/220+mg.id)*2:7;ctx.strokeStyle='#9975c1';ctx.fillStyle='#9a77c317';ctx.lineWidth=1.3;ctx.beginPath();ctx.arc(p.x,p.y,radius,0,Math.PI*2);ctx.fill();ctx.stroke();if(state.labels){ctx.fillStyle='#8663b3';ctx.font='9px system-ui';ctx.fillText(mg.name,p.x+10,p.y+9);}}}
}
function animation(now){if((state.running||state.flow)&&now-lastFrame>=32){draw(now);lastFrame=now;}requestAnimationFrame(animation);}
function select(kind,id){state.selected={kind,id};renderSelection();draw();}
function pick(x,y){let closest=null,distance=10;for(const b of state.scene.buses){if(!busVisible(b))continue;const p=screen(b.id),d=Math.hypot(x-p.x,y-p.y);if(d<distance){distance=d;closest={kind:'bus',id:b.id};}}if(!closest){for(const [kind,edges] of [['trafo',state.scene.trafos],['line',state.scene.lines]])for(const e of edges){if(!edgeVisible(e))continue;const a=screen(e.from),b=screen(e.to),dx=b.x-a.x,dy=b.y-a.y,t=Math.max(0,Math.min(1,((x-a.x)*dx+(y-a.y)*dy)/(dx*dx+dy*dy||1))),d=Math.hypot(x-a.x-t*dx,y-a.y-t*dy);if(d<distance&&d<6){distance=d;closest={kind,id:e.id};}}}if(closest)select(closest.kind,closest.id);}
function activateTab(name){document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id===`view-${name}`));if(name==='topology')requestAnimationFrame(()=>{resize();});}
async function api(path,options={}){
  const {timeoutMs=15000,...request}=options,controller=new AbortController();
  const timeout=setTimeout(()=>controller.abort(),timeoutMs);
  try{
    const response=await fetch(path,{...request,signal:controller.signal,cache:'no-store'});
    let data;try{data=await response.json();}catch(error){if(error.name==='AbortError')throw error;throw new Error('计算服务未启动，或当前端口仅提供静态网页');}
    if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`);
    return data;
  }catch(error){if(error.name==='AbortError')throw new Error('计算服务响应超时，请确认本地服务已启动后重试');throw error;}
  finally{clearTimeout(timeout);}
}
function disableControls(locked){['runBtn','datasetSelect','algorithmSelect','uploadBtn'].forEach(id=>$(id).disabled=locked);}
function lockControls(locked){disableControls(locked);$('runText').textContent=locked?'正在计算…':'运行潮流校核';$('runIcon').textContent=locked?'◌':'▶';$('runIcon').className=locked?'spinner':'';}
function finish(){state.running=false;state.activePartition=null;lockControls(false);if(timer)clearInterval(timer);timer=null;if(stream)stream.close();stream=null;renderSelection();draw();}
function failRun(message){state.source=state.scene.result_status==='waiting'?'未收敛迭代值（非最终结果）':'本次失败 · 上次保存结果';finish();addLog(message,'failed');origin('本次失败 · 未生成新结果','failed');stage('本次计算失败',message,true);$('timeSub').textContent='本次失败；原有结果不代表本次计算';}
async function run(){
  if(state.running||state.importing)return;state.running=true;state.run=null;state.task=null;started=performance.now();lockControls(true);activateTab('topology');stage('正在创建计算任务','读取所选数据与算法');origin('现场计算中');
  addLog(`提交 ${$('datasetSelect').selectedOptions[0].textContent} · ${$('algorithmSelect').selectedOptions[0].textContent}`);
  timer=setInterval(()=>{const elapsed=(performance.now()-started)/1000;$('runElapsed').textContent=`${elapsed.toFixed(3)} s`;$('timeKpi').textContent=`${elapsed.toFixed(2)} s`;$('timeSub').textContent='本次任务进行中';},80);
  try{
    const task=await api('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset_id:state.dataset,algorithm:$('algorithmSelect').value})});
    state.task=task.task_id;stream=new EventSource(`/api/runs/${task.task_id}/events`);
    stream.onmessage=async message=>{
      if(!state.running||state.task!==task.task_id)return;
      try{
        const e=JSON.parse(message.data);if(e.message)addLog(e.message,e.type,e);
        if(e.type==='network'){setScene(e,'本次计算 · 等待收敛');stage('全网潮流求解中','节点电压跟随真实迭代更新');}
        if(e.type==='iteration'){
          e.voltages.forEach((v,i)=>{const b=state.scene.buses[i];if(b){b.vm=v[0];b.va=v[1];}});
          stage(`全网求解 · 第 ${e.iteration} 步`,`最大不平衡 ${exp(e.residual_mva)} MVA`);renderSelection();draw();
        }
        if(e.type==='partition'){
          state.activePartition=e.partition_id;stage(`分区校核 · P${String(e.partition_id).padStart(2,'0')}`,e.status==='running'?'使用本次全网边界电压':'局部 AC 校核已返回');
          if(e.result){const p=state.scene.partitions.find(p=>p.id===e.partition_id);if(p)Object.assign(p,e.result);$('partitionKpi').textContent=`${state.scene.partitions.filter(p=>p.converged===true).length} / ${state.scene.partitions.length}`;}draw();
        }
        if(e.type==='completed'){
          stream.close();stream=null;
          const result=await api(`/api/runs/${task.task_id}/result`);if(state.task!==task.task_id)return;
          state.run=result.run;setScene(result,'本次现场计算结果');finish();
          $('timeKpi').textContent=`${fmt(result.run.total_ms,1)} ms`;$('timeSub').textContent=`全网阶段 ${fmt(result.run.solver_ms,1)} ms · ${result.run.iterations} 次牛顿迭代`;$('runElapsed').textContent=`${(result.run.total_ms/1000).toFixed(3)} s`;
          const hasFailures=result.run.partition_failed>0;
          stage(hasFailures?'全网收敛 · 部分分区未通过':'本次潮流计算完成',`残差 ${exp(result.run.residual_mva)} MVA · ${result.run.iterations} 次迭代`,hasFailures);
          origin(`${state.scene.label} · 本次结果`,hasFailures?'failed':'live');renderAnalysis();
        }
        if(e.type==='failed')failRun(e.message);
      }catch(error){failRun(`结果处理失败：${error.message}`);}
    };
    stream.onerror=()=>{if(state.running)failRun('实时连接中断；本次完成状态未确认，任务日志保存在后端。');};
  }catch(error){failRun(`任务未能执行：${error.message}`);}
}
async function loadDatasets(prefer){const data=await api('/api/datasets');const select=$('datasetSelect');select.replaceChildren();for(const item of data.datasets){const option=document.createElement('option');option.value=item.id;option.textContent=item.label;select.appendChild(option);}select.value=prefer||state.dataset;}
function changeDataset(){state.dataset=$('datasetSelect').value;state.run=null;state.selected=null;state.activePartition=null;$('timeKpi').textContent='—';$('timeSub').textContent='等待现场运行';$('runElapsed').textContent='0.000 s';if(saved.scenarios[state.dataset]){setScene({scenario:saved.scenarios[state.dataset],layout:saved.layouts[state.dataset]},'已保存结果');stage('已加载保存结果','点击运行，以所选算法重新求解');origin(`${state.scene.label} · 已保存结果`);}else{setScene({scenario:emptyScene(),layout:{}},'已导入 · 尚未求解');stage('已选择导入数据','点击运行，解析原始 DAT 并计算');origin('导入数据 · 尚未求解');}addLog(`选择数据：${$('datasetSelect').selectedOptions[0].textContent}`);}
async function importFolder(event){const files=[...event.target.files].filter(f=>/\.(dat|xls|xlsx)$/i.test(f.name));if(!files.some(f=>/\.dat$/i.test(f.name))){addLog('导入失败：所选文件夹中没有 DAT 文件','failed');return;}if(files.reduce((n,f)=>n+f.size,0)>32*1024**2){addLog('导入失败：文件总大小超过 32 MiB','failed');return;}state.importing=true;disableControls(true);try{addLog(`正在上传 ${files.length} 个 DAT / 配套表格文件，原始字节保留`);const payload=[];for(const file of files){const bytes=new Uint8Array(await file.arrayBuffer());let raw='';for(let i=0;i<bytes.length;i+=16384)raw+=String.fromCharCode(...bytes.subarray(i,i+16384));payload.push({path:file.webkitRelativePath||file.name,content:btoa(raw)});}const result=await api('/api/datasets/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({files:payload})});await loadDatasets(result.datasets[0].id);changeDataset();addLog(`成功导入 ${result.datasets.length} 个断面；可在数据下拉框选择。配套表格保留，原始 DAT 负荷不自动覆写。`,'completed');}catch(error){addLog(`导入失败：${error.message}`,'failed');}finally{state.importing=false;disableControls(false);$('folderInput').value='';}}
function download(name,text,type='text/plain;charset=utf-8'){const url=URL.createObjectURL(new Blob([text],{type})),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),500);}
function exportCSV(){const quote=v=>`"${String(v??'').replaceAll('"','""')}"`;const rows=[['type','source_line','name','from','to','rate_mva','p_from_mw','q_from_mvar','current_loading_percent'],...[...state.scene.lines.map(e=>({...e,type:'L'})),...state.scene.trafos.map(e=>({...e,type:'T'}))].map(e=>[e.type,e.source_line,e.name,nodes.get(e.from)?.name,nodes.get(e.to)?.name,e.rate_mva,e.p,e.q,e.loading])];download(`sjz_${state.dataset}_powerflow.csv`,'\ufeff'+rows.map(r=>r.map(quote).join(',')).join('\n'),'text/csv;charset=utf-8');}
$('runBtn').onclick=run;$('datasetSelect').onchange=changeDataset;$('uploadBtn').onclick=()=>$('folderInput').click();$('folderInput').onchange=importFolder;
$('elementSearch').oninput=renderAnalysis;$('search').oninput=e=>{state.search=e.target.value.trim();draw();};$('colorSelect').onchange=e=>{state.color=e.target.value;draw();};$('flowToggle').onchange=e=>{state.flow=e.target.checked;draw();};$('labelToggle').onchange=e=>{state.labels=e.target.checked;draw();};
$('exportBtn').onclick=exportCSV;$('clearLog').onclick=()=>{$('log').replaceChildren();$('logCount').textContent=0;};$('downloadLog').onclick=()=>download(`sjz_${state.task||'session'}_log.json`,JSON.stringify(state.log,null,2),'application/json');
document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>activateTab(button.dataset.tab));document.querySelectorAll('[data-layer]').forEach(button=>button.onclick=()=>{const layer=button.dataset.layer;state.layers[layer]=!state.layers[layer];button.classList.toggle('active',state.layers[layer]);draw();});
$('fitBtn').onclick=fit;$('zoomIn').onclick=()=>zoom(1.25,size.w/2,size.h/2);$('zoomOut').onclick=()=>zoom(.8,size.w/2,size.h/2);
function zoom(factor,x,y){state.offset.x=x+(state.offset.x-x)*factor;state.offset.y=y+(state.offset.y-y)*factor;state.scale*=factor;draw();}
canvas.addEventListener('wheel',event=>{event.preventDefault();const box=canvas.getBoundingClientRect();zoom(event.deltaY<0?1.13:.885,event.clientX-box.left,event.clientY-box.top);},{passive:false});
canvas.addEventListener('pointerdown',event=>{lastPointer={x:event.clientX,y:event.clientY,ox:state.offset.x,oy:state.offset.y};state.dragging=true;canvas.classList.add('dragging');canvas.setPointerCapture(event.pointerId);});
canvas.addEventListener('pointermove',event=>{if(!state.dragging||!lastPointer)return;state.offset={x:lastPointer.ox+event.clientX-lastPointer.x,y:lastPointer.oy+event.clientY-lastPointer.y};draw();});
canvas.addEventListener('pointerup',event=>{if(lastPointer&&Math.hypot(event.clientX-lastPointer.x,event.clientY-lastPointer.y)<5){const box=canvas.getBoundingClientRect();pick(event.clientX-box.left,event.clientY-box.top);}state.dragging=false;canvas.classList.remove('dragging');});canvas.addEventListener('pointercancel',()=>{state.dragging=false;canvas.classList.remove('dragging');});
window.addEventListener('resize',resize);new ResizeObserver(resize).observe($('mapWrap'));
rebuildIndex();render();resize();fit();requestAnimationFrame(animation);
disableControls(false);window.SJZ_BOOT?.ready();
addLog('已加载石家庄 13 点保存结果；现场计算由本地 Python 求解器执行。');
addLog('所有原始阻抗、负荷、容量和控制设置保持不变；过载结果完整保留。');
loadDatasets().then(()=>{$('connectionDot').className='online';$('connectionText').textContent='本地计算服务已连接';}).catch(error=>{$('connectionDot').className='offline';$('connectionText').textContent='计算服务未连接';addLog(error.message,'failed');});
