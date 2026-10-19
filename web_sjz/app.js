'use strict';
const $ = id => document.getElementById(id);
const state = {
  dataset:null, snapshot:null, scene:emptyScene(), layout:{},
  selected:null, color:'partition', layers:{main:true,interface:true,distribution:true,microgrid:true},
  scale:1, offset:{x:0,y:0}, search:'', running:false, importing:false, flow:false, labels:false,
  log:[], run:null, source:'等待上传数据', phase:'等待上传数据', activePartition:null, task:null, dragging:false,
  focusArea:null, elementPage:0, partitionPage:0, clientElapsedMs:null,
};
const canvas = $('topology'), ctx = canvas.getContext('2d');
let size = {w:0,h:0}, nodes = new Map(), edgeLookup = new Map(), stream = null, timer = null, started = null;
let lastFrame = 0, lastPointer = null;
let paintPending = false, flowStats={eligible:0,animated:0,zero:0};
let areaNodes=new Map(), areaEdges=new Map(), visibleBuses=[], visibleLines=[], visibleTrafos=[], interties=[], analysisEdges=null;
function requestDraw(){if(paintPending)return;paintPending=true;requestAnimationFrame(now=>{paintPending=false;draw(now);});}
function selectedSnapshot(){return state.dataset?.snapshots.find(s=>s.id===state.snapshot);}
function datasetNote(){const snapshot=selectedSnapshot();$('datasetNote').textContent=!state.dataset?'尚未上传数据 · 一个文件夹作为一套数据集':snapshot?`当前数据集：${state.dataset.label} · 计算时刻／断面：${snapshot.label} · T 卡按 100 MVA 假设解释；原文件不变，过载完整保留`:'一套数据集已上传 · 请选择其中的计算时刻，再运行潮流校核';}
const colors = ['#358abd','#419d89','#bd914b','#9273b9','#c96c82','#5197a6','#849c4a','#b980aa','#5d80a8','#b78465'];
const fmt = (v,d=2) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('zh-CN',{maximumFractionDigits:d});
const exp = v => v == null ? '—' : Number(v).toExponential(3);
function emptyScene(){return {label:'未计算',result_status:'waiting',buses:[],lines:[],trafos:[],partitions:[],microgrids:[],layers:{},summary:{}};}
function element(kind,id){return kind==='bus' ? nodes.get(id) : edgeLookup.get(`${kind}:${id}`);}
function largeScene(){return state.scene.buses.length>20000&&state.scene.areas?.length>0;}
function overview(){return largeScene()&&!state.focusArea;}
function rebuildIndex(){
  nodes=new Map(state.scene.buses.map(b=>[b.id,b]));edgeLookup=new Map();areaNodes=new Map();areaEdges=new Map();analysisEdges=null;
  for(const area of state.scene.areas||[]){areaNodes.set(area.id,[]);areaEdges.set(area.id,{lines:[],trafos:[]});}
  for(const b of state.scene.buses)areaNodes.get(b.area_id)?.push(b);
  for(const [kind,edges] of [['line',state.scene.lines],['trafo',state.scene.trafos]])for(const e of edges){
    e.kind=kind;edgeLookup.set(`${kind}:${e.id}`,e);
    const area=nodes.get(e.from)?.area_id;
    if(area&&area===nodes.get(e.to)?.area_id)areaEdges.get(area)?.[kind==='line'?'lines':'trafos'].push(e);
  }
  interties=state.scene.lines.filter(e=>e.is_intertie);
  indexTrace();
  if(!largeScene()||!areaNodes.has(state.focusArea))state.focusArea=null;
  const selector=$('areaSelect');selector.replaceChildren(new Option('全部区域 · 互联总览',''));
  for(const area of state.scene.areas||[])selector.add(new Option(`${area.label} · ${fmt(area.bus_count,0)} 母线`,area.id));
  syncView();
}
function syncView(){
  const focus=largeScene()&&state.focusArea;
  visibleBuses=focus?areaNodes.get(focus):state.scene.buses;
  visibleLines=focus?areaEdges.get(focus).lines:state.scene.lines;
  visibleTrafos=focus?areaEdges.get(focus).trafos:state.scene.trafos;
  $('areaControl').hidden=!largeScene();$('areaSelect').value=state.focusArea||'';
  const s=state.scene;
  $('mapMeta').textContent=`${fmt(s.buses.length,0)} 母线 / ${fmt(s.lines.length+s.trafos.length,0)} 支路 · ${overview()?'区域总览，点击区域展开':focus?`${focus} 区 ${fmt(visibleBuses.length,0)} 母线，跨区联络见总览`:'拖动平移 / 滚轮缩放'}`;
}
function focusArea(id){state.focusArea=id||null;syncView();updateVisibleFlows();fit();}
function setScene(payload,source){const same=state.scene.buses.length===payload.scenario.buses.length&&state.scene.buses.every((b,i)=>b.name===payload.scenario.buses[i].name&&b.kv===payload.scenario.buses[i].kv);state.scene=payload.scenario;state.layout=payload.layout;state.source=source;rebuildIndex();if(state.selected&&!element(state.selected.kind,state.selected.id))state.selected=null;render();resize();if(!same)fit();}
let pendingLogRows=[],logFrame=null;
function flushLogs(){
  if(logFrame!==null)cancelAnimationFrame(logFrame);logFrame=null;
  if(!pendingLogRows.length)return;
  const box=$('log'),follow=box.scrollTop+box.clientHeight>=box.scrollHeight-45;
  const fragment=document.createDocumentFragment();
  for(const row of pendingLogRows)fragment.appendChild(row);pendingLogRows=[];
  box.appendChild(fragment);while(box.childElementCount>1000)box.firstElementChild.remove();
  if(follow)box.scrollTop=box.scrollHeight;
  $('logFollow').textContent=state.log.length>1000?'显示最近 1000 条 · 下载包含全部日志':'自动跟随最新日志';
  $('logCount').textContent=state.log.length;
}
function addLog(message,kind='log',event={}){
  if(!message)return;
  const record={message,kind,timestamp:event.timestamp||new Date().toISOString(),elapsed_ms:event.elapsed_ms};
  state.log.push(record);
  const row=document.createElement('div'),stamp=document.createElement('span');
  row.className=`log-row event-${kind}`;stamp.className='time';
  stamp.textContent=new Date(record.timestamp).toLocaleTimeString('zh-CN',{hour12:false})+(record.elapsed_ms!=null?` +${(record.elapsed_ms/1000).toFixed(3)}s`:'');
  row.append(stamp,document.createTextNode(message));
  pendingLogRows.push(row);if(logFrame===null)logFrame=requestAnimationFrame(flushLogs);
}
function stage(text,sub='',failed=false){$('stageText').textContent=text;$('stageSub').textContent=sub;$('stageDot').className=`stage-dot${state.running?' running':''}${failed?' failed':''}`;state.phase=text;$('runPhase').textContent=text;}
function origin(text,kind=''){const node=$('resultOrigin');node.textContent=text;node.className=`result-origin ${kind}`;}
function metrics(target,rows){const root=$(target);root.replaceChildren();for(const [label,value,wide] of rows){const row=document.createElement('div'),name=document.createElement('span'),val=document.createElement('strong');row.className=`detail-metric${wide?' wide':''}`;name.textContent=label;val.textContent=value;row.append(name,val);root.appendChild(row);}}
function renderSelection(){
  const selected=state.selected,item=selected&&element(selected.kind,selected.id);
  const iterative=state.running&&trace.current||trace.viewing;
  $('detailUpdated').textContent=`${iterative?`${trace.viewing?'迭代回放':'计算中'} · 第 ${trace.current?.iteration??0} 轮 · P/Q 为合并母线组净注入`:state.source} · ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}`;
  $('detailEmpty').hidden=!!item;$('detailGrid').hidden=!item;
  if(!item){$('detailTitle').textContent='节点与元件状态';$('detailTag').textContent='等待选择';renderPartitionTrace();return;}
  $('detailTitle').textContent=item.name;
  let rows;
  if(selected.kind==='bus'){
    $('detailTag').textContent=item.microgrid_name?'假设微网 PCC':item.layer==='main'?'主网母线':'配网母线';
    rows=[['母线 / 电压等级',`${item.id} / ${fmt(item.kv)} kV · ${item.type||'PQ'}`],['所属分区',item.partition_id?`P${String(item.partition_id).padStart(2,'0')}`:'主网 / 协调器'],['求解角色',item.solver_role==='coordinator'?'主网协调变量':item.is_boundary?'分区边界节点':'分区内部节点'],['相邻分区',[...(item.neighbor_partition_ids||[]).map(id=>`P${id}`),...(item.has_main_interface?['主网']:[])].join('、')||'无跨区连接'],['电压幅值',`${fmt(item.vm,6)} pu`],['电压相角',`${fmt(item.va,5)}°`],[iterative?'组有功净注入':'有功净注入',`${fmt(item.p,4)} MW`],[iterative?'组无功净注入':'无功净注入',`${fmt(item.q,4)} Mvar`]];
    if(item.merged_group_count>1)rows.push(['理想连接合并节点数',String(item.merged_group_count)]);
  }else{
    $('detailTag').textContent=selected.kind==='line'?'线路':'变压器';
    rows=[['起点',nodes.get(item.from)?.name||item.from],['终点',nodes.get(item.to)?.name||item.to],['电流负载率',`${fmt(item.loading,3)} %`],['视在功率容量比',`${fmt(item.apparent_power_loading_percent,3)} %`],['首端有功',`${fmt(item.p,4)} MW`],['首端无功',`${fmt(item.q,4)} Mvar`],['末端有功',`${fmt(item.p_to,4)} MW`],['末端无功',`${fmt(item.q_to,4)} Mvar`],['输入额定容量',`${fmt(item.rate_mva,3)} MVA`],['R / X（原文件标幺值）',`${fmt(item.input_r_pu,6)} / ${fmt(item.input_x_pu,6)}`],['原阻抗解释基准',`${fmt(item.impedance_base_mva)} MVA${selected.kind==='trafo'&&state.scene.impedance_policy?.is_assumption?' · 假设':''}`],['换算倍率',`${fmt(item.impedance_scale)} 倍`],['R / X（系统基准标幺值）',`${fmt(item.r_pu,6)} / ${fmt(item.x_pu,6)}`],['本次输入 DAT 行号',String(item.source_line)]];
  }
  if(item.area_id)rows.unshift(['复制区域 / 原始名称',`${item.area_id} 区 / ${item.original_name}`]);
  if(item.is_intertie)rows.unshift(['支路用途','研究扩展区域联络线']);
  metrics('detailGrid',rows);
  renderPartitionTrace();
}
function render(){
  datasetNote();
  const s=state.scene,edges=[...s.lines,...s.trafos],rated=edges.filter(e=>e.loading!=null),ratedT=s.trafos.filter(e=>e.loading!=null),ratedL=s.lines.filter(e=>e.loading!=null),maxT=ratedT.reduce((v,e)=>Math.max(v,e.loading),0),maxL=ratedL.reduce((v,e)=>Math.max(v,e.loading),0),q=s.summary;
  const selected=selectedSnapshot();
  $('busKpi').textContent=s.buses.length?fmt(s.buses.length,0):selected?fmt(selected.bus_count,0):'—';$('busSub').textContent=s.buses.length?`母线 · 原始有功负荷 ${fmt(s.load_mw,1)} MW`:selected?'已识别母线 · 等待现场计算':'上传后识别网络规模';
  $('edgeKpi').textContent=s.buses.length?`${fmt(s.lines.length,0)} / ${s.trafos.length}`:'—';
  $('loadingKpi').textContent=ratedT.length?`${fmt(maxT,2)}%`:'—';$('loadingSub').textContent=rated.length?`线路最高 ${ratedL.length?fmt(maxL,2)+'%':'—'} · ${rated.filter(e=>e.loading>100).length} 个元件越限`:'线路与 T 支路分别统计';
  $('partitionKpi').textContent=s.buses.length?`${q.converged||0} / ${q.parts||0}`:'—';
  const recorded=state.run;
  $('partitionSub').textContent=recorded?.algorithm==='partitioned-nr'?`${recorded.workers} 个 CPU 线程 · ${recorded.boundary_iterations} 轮边界协调`:recorded?.partition_check==='not requested'?'普通全网算法；未另做局部校核':selected?'等待本次求解':'上传后计算分区';
  $('mainCount').textContent=s.layers.main_bus_count||0;$('interfaceCount').textContent=s.layers.interface_trafo_count||0;$('distributionCount').textContent=q.parts||0;$('microCount').textContent=s.microgrids.length;
  syncView();
  $('mapEmpty').hidden=!!s.buses.length;
  $('mapEmpty').textContent=selected?`已选择 ${selected.label} 运行断面，点击“运行潮流校核”生成拓扑与结果`:state.dataset?'数据集已上传，请先选择本次计算时刻':'请先点击“选择数据文件夹”，上传一套原始数据集';
  $('exportBtn').disabled=!state.run;
  $('flowToggle').disabled=!s.buses.length;
  renderSelection();renderAnalysis();requestDraw();
}
function renderAnalysis(){
  if(!$('view-analysis').classList.contains('active'))return;
  const r=state.run;
  const partitioning=r?.partitioning||state.scene.partitioning;
  const method=$('partitionMethod');method.replaceChildren();
  if(partitioning){
    traceText(method,'div',`加权 METIS：${partitioning.distribution_components} 个连通块 → ${partitioning.partition_count} 个计算分区；每区 ${partitioning.size_min}～${partitioning.size_max} 节点，中位 ${partitioning.size_median}。CPU 可用 ${partitioning.available_cpus}。分区为电气计算域，不代表供电公司行政分区。`);
    traceText(method,'div','边权：归一化串联导纳的平方根；点权：交流未知量数 × (连接度 + 1)²。保持连通性；小分区成批调度。');
    for(const c of partitioning.candidates){const n=traceText(method,'span',`粒度 ${c.target_size}：${c.partition_count} 区 / ${c.interface_unknowns} 边界未知量 / 预计负载比 ${fmt(c.estimated_schedule_imbalance,3)} / 成本估计 ${fmt(c.estimated_cost,0)}${c.target_size===partitioning.target_size?' ✓':''}`,'candidate');if(c.target_size===partitioning.target_size)n.style.color='#247cae';}
    traceText(method,'div',`结构估计用于选型；分区准备 ${fmt(partitioning.elapsed_ms,2)} ms，实测时间见运行记录。`);
  }else method.textContent=state.dataset?'现场运行后显示加权分区依据和候选评价。':'请先上传原始数据文件夹。';
  const storedRun=r;
  $('partitionExplain').textContent=storedRun?.algorithm==='partitioned-nr'?'各分区并行消元 → 交流边界方程协调 → 电压修正回传':storedRun?.partition_check==='not requested'?'本次使用全网算法；未追加固定边界局部校核':'现场运行后显示本次分区求解记录';
  metrics('runRecord',r?[['求解算法',r.algorithm_label],['牛顿迭代次数',String(r.iterations)],['边界协调轮数',String(r.boundary_iterations||0)],['CPU 计算线程',String(r.workers||1)],['最终残差 MVA',exp(r.residual_mva)],['数值求解耗时（含结构准备）',`${fmt(r.solver_ms,2)} ms`],['页面总用时（含排队与传输）',state.clientElapsedMs===null?'—':`${(state.clientElapsedMs/1000).toFixed(3)} s`],['后端任务耗时（不含排队与网络）',`${fmt(r.total_ms,2)} ms`],['分区失败',String(r.partition_failed)],['开始时间',new Date(r.started_at).toLocaleString('zh-CN')],['结束时间',new Date(r.completed_at).toLocaleString('zh-CN')],['数据集',r.dataset_name||'—'],['计算时刻／断面',r.snapshot_label||'—'],['输入范围',r.reference_file_count?`所选 DAT；${r.reference_file_count} 份 Excel 仅保存，未接入模型`:'所选 DAT'],['本次输入',r.source_name],['输入 SHA-256（求解前后相同）',r.source_sha256,true]]:[['结果来源',state.source],['现场计算',selectedSnapshot()?'点击运行开始新任务':state.dataset?'请先选择计算时刻':'请先上传原始数据集文件夹']]);
  $('runRecordTag').textContent=r?'本次现场运行':selectedSnapshot()?'等待运行':state.dataset?'等待选择时刻':'等待上传';
  if(r?.impedance_policy){const row=document.createElement('div');row.className='detail-metric wide';traceText(row,'span','阻抗基准设置');traceText(row,'strong',r.impedance_policy.label);$('runRecord').append(row);}
  if(r?.algorithm==='partitioned-nr'&&r.solver_details){
    const d=r.solver_details;
    const rows=[['加权分区准备',`${fmt(r.partitioning.elapsed_ms,2)} ms${r.partitioning.structure_cache_hit?' · 复用已核对结构':''}`],['消元结构准备',`${fmt(d.setup_ms,2)} ms · ${d.structure_cache_hit?'复用结构索引':'本次构造'}`],['牛顿迭代及遥测',`${fmt(d.iteration_ms,2)} ms`]];
    for(const [name,value] of rows){const row=document.createElement('div');row.className='detail-metric';traceText(row,'span',name);traceText(row,'strong',value);$('runRecord').append(row);}
    traceText($('runRecord'),'div','仅复用已核对的分区与消元结构索引；每次重新读取输入、组装数值矩阵，并从平坦 PQ 电压开始求解。首次构造和重复运行分别计时。','detail-metric wide');
  }
  if(r?.solver_details?.steps){const loads=Array(r.workers).fill(0);for(const step of r.solver_details.steps)(step.worker_local_ms||[]).forEach((v,i)=>loads[i]=(loads[i]||0)+v);const mean=loads.reduce((a,b)=>a+b,0)/loads.length;const row=document.createElement('div');row.className='detail-metric wide';row.textContent=`实际线程局部消元耗时最大值 / 均值：${fmt(Math.max(...loads)/mean,3)}；阶段统计不包含边界协调、结构准备及界面传输。`;$('runRecord').append(row);}
  if(r?.expansion){const e=r.expansion;const row=document.createElement('div');row.className='detail-metric wide';row.textContent=`研究扩展：${e.copies} 个复制区域；${e.interties.length} 条高压联络线；源 SHA-256 ${e.source_sha256}`;$('runRecord').append(row);}
  const edges=analysisEdges||(analysisEdges=[...state.scene.lines,...state.scene.trafos].sort((a,b)=>(b.loading??-1)-(a.loading??-1)));
  const query=$('elementSearch').value.trim().toLowerCase();
  const body=$('elementTable');body.replaceChildren();
  const filtered=query?edges.filter(e=>`${e.name} ${nodes.get(e.from)?.name} ${nodes.get(e.to)?.name}`.toLowerCase().includes(query)):edges;
  const pageSize=edges.length>20000?500:Math.max(1,edges.length);
  const page=pagedRows(filtered,pageSize,'element');
  for(const e of page){
    const a=nodes.get(e.from)?.name||e.from,b=nodes.get(e.to)?.name||e.to;
    const row=body.insertRow();
    const values=[e.kind==='line'?'线路':'变压器',e.name,`${a} → ${b}`,fmt(e.rate_mva,3),fmt(e.p,3),fmt(e.q,3),e.loading==null?'—':`${fmt(e.loading,2)}%`];
    values.forEach((v,i)=>{const cell=row.insertCell();cell.textContent=v;if(i===6)cell.className=e.loading>100?'danger':'';});
    row.onclick=()=>{select(e.kind,e.id);activateTab('topology');};
  }
  const parts=$('partitionTable');parts.replaceChildren();
  for(const p of pagedRows(state.scene.partitions,state.scene.partitions.length>1000?250:Math.max(1,state.scene.partitions.length),'partition')){const row=parts.insertRow();[p.display_name,p.internal_buses.length,`${p.boundary_internal_buses?.length??'—'} / ${p.boundary_buses.length}`,p.converged==null?'未做局部校核':p.converged?'收敛':'未通过',exp(p.max_equation_residual_mva),p.iterations??'—',p.solve_mode==='coupled Schur Newton'?'并行边界协调':p.converged==null?'全网算法':'固定边界校核'].forEach(v=>{row.insertCell().textContent=v;});row.onclick=()=>{select('bus',p.internal_buses[0]);activateTab('topology');};}
  const bins=[['≤50%',0,50,'#69ad9a'],['50–80%',50,80,'#6d9cbe'],['80–100%',80,100,'#c4a366'],['>100%',100,Infinity,'#d2808a']];
  const counts=bins.map(([,lo,hi])=>edges.filter(e=>e.loading!=null&&e.loading>lo&&e.loading<=hi).length);
  counts[0]+=edges.filter(e=>e.loading===0).length;
  const dist=$('loadingDistribution');dist.replaceChildren();
  bins.forEach(([label,,,color],i)=>{const row=document.createElement('div'),name=document.createElement('span'),bar=document.createElement('div'),fill=document.createElement('i'),count=document.createElement('b');row.className='dist-row';name.textContent=label;fill.style.width=`${counts[i]/Math.max(1,...counts)*100}%`;fill.style.background=color;count.textContent=counts[i];bar.append(fill);row.append(name,bar,count);dist.append(row);});
}
function pagedRows(rows,pageSize,key){
  const pages=Math.max(1,Math.ceil(rows.length/pageSize)),field=`${key}Page`;
  state[field]=Math.max(0,Math.min(state[field],pages-1));
  const start=state[field]*pageSize,end=Math.min(start+pageSize,rows.length);
  $(`${key}PageInfo`).textContent=`共 ${fmt(rows.length,0)} 条 · 显示 ${rows.length?start+1:0}–${end} · 第 ${state[field]+1} / ${pages} 页`;
  $(`${key}Prev`).disabled=state[field]===0;$(`${key}Next`).disabled=state[field]>=pages-1;
  $(`${key}Prev`).hidden=$(`${key}Next`).hidden=pages===1;
  return rows.slice(start,end);
}
function screen(id){const p=state.layout[String(id)]||[0,0];return {x:state.offset.x+p[0]*state.scale,y:state.offset.y-p[1]*state.scale};}
function areaScreen(id){const p=state.scene.areas.find(a=>a.id===id).center;return {x:state.offset.x+p[0]*state.scale,y:state.offset.y-p[1]*state.scale};}
function resize(){const box=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;if(size.w&&size.h&&box.width&&box.height){state.offset.x+=(box.width-size.w)/2;state.offset.y+=(box.height-size.h)/2;}size={w:box.width,h:box.height};canvas.width=Math.round(size.w*dpr);canvas.height=Math.round(size.h*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);requestDraw();}
function fit(){const pts=overview()?state.scene.areas.map(a=>a.center):visibleBuses.map(b=>state.layout[String(b.id)]).filter(Boolean);if(!pts.length)return;let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;for(const [x,y] of pts){minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y);}state.scale=Math.min(Math.max(size.w-70,20)/Math.max(maxX-minX,1e-8),Math.max(size.h-96,20)/Math.max(maxY-minY,1e-8));state.offset={x:(size.w-(minX+maxX)*state.scale)/2,y:(size.h+(minY+maxY)*state.scale)/2+6};requestDraw();}
function busVisible(b){return b.layer==='main'?state.layers.main:state.layers.distribution||(b.is_boundary&&state.layers.interface);}
function edgeVisible(e){return e.from_layer!==e.to_layer?state.layers.interface:e.from_layer==='main'?state.layers.main:state.layers.distribution;}
function nodeColor(b){if(state.color==='vm')return b.vm==null?'#acbac6':b.vm<.95||b.vm>1.05?'#ce5969':b.vm<.98?'#c99e45':'#43a389';if(state.color==='voltage')return b.kv>=200?'#ce7f65':b.kv>=100?'#bba24d':b.kv>=35?'#3289bb':b.kv>=10?'#4aa28c':'#9a82bc';return b.layer==='main'?'#267cb8':colors[((b.partition_id||1)-1)%colors.length];}
function setFlow(enabled){state.flow=enabled;$('flowToggle').checked=enabled;requestDraw();}
function branchCurrent(e){
  if(!Number.isFinite(e.p)||!Number.isFinite(e.q))return null;
  const from=nodes.get(e.from),to=nodes.get(e.to);
  const sf=Math.hypot(e.p,e.q),st=Math.hypot(e.p_to||0,e.q_to||0);
  if(Math.max(sf,st)<1e-7)return null;
  const a=from?.vm>0&&from?.kv>0?sf*1000/(Math.sqrt(3)*from.kv*from.vm):0;
  const b=to?.vm>0&&to?.kv>0?st*1000/(Math.sqrt(3)*to.kv*to.vm):0;
  if(!Number.isFinite(a+b)||Math.max(a,b)<1e-7)return null;
  const direction=Math.abs(e.p)>1e-7?Math.sign(e.p):Math.abs(e.q)>1e-7?Math.sign(e.q):Math.sign(-(e.p_to||e.q_to||0));
  return {amps:Math.max(a,b),direction:direction||1};
}
function drawCurrent(e,a,b,now,current,active=false){
  const dx=b.x-a.x,dy=b.y-a.y,length=Math.hypot(dx,dy);if(length<.25)return false;
  const count=Math.max(1,Math.min(12,Math.ceil(length/28))),speed=38+Math.min(25,Math.log1p(current.amps)*3);
  const phase=(now/1000*speed/Math.max(length,28)+(Number(e.source_line)||Number(e.id)||0)*.618034)%1;
  const radius=(active?1.55:1.1)+Math.min(.7,Math.log1p(current.amps)/12);
  ctx.globalAlpha=.95;ctx.fillStyle=active?'#096cae':'#238fc3';ctx.strokeStyle=active?'#096cae':'#238fc3';ctx.lineWidth=1.7;
  ctx.beginPath();
  for(let k=0;k<count;k++){
    const progress=(phase+k/count)%1,t=current.direction<0?1-progress:progress;
    const tail=Math.max(0,Math.min(1,t-current.direction*Math.min(.22,5/length)));
    ctx.moveTo(a.x+dx*tail,a.y+dy*tail);ctx.lineTo(a.x+dx*t,a.y+dy*t);
  }
  ctx.stroke();ctx.beginPath();
  for(let k=0;k<count;k++){const progress=(phase+k/count)%1,t=current.direction<0?1-progress:progress,x=a.x+dx*t,y=a.y+dy*t;ctx.moveTo(x+radius,y);ctx.arc(x,y,radius,0,Math.PI*2);}
  ctx.fill();return true;
}
function drawOverview(now){
  const s=state.scene,activePart=trace.partIndex.get(selectedPartition()||state.activePartition);
  const activeArea=activePart?nodes.get(activePart.internal_buses[0])?.area_id:null;
  for(const edge of interties){
    if(!edge||!state.layers.main)continue;
    const a=areaScreen(nodes.get(edge.from).area_id),b=areaScreen(nodes.get(edge.to).area_id),active=state.selected?.kind==='line'&&state.selected.id===edge.id;
    ctx.strokeStyle=active?'#0d78b4':state.color==='loading'&&edge.loading>100?'#cf6471':'#159b8e';ctx.lineWidth=active?3:1.8;
    ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
    if(state.flow&&edge.p!=null){const progress=(now/2200+edge.id*.137)%1,t=edge.p<0?1-progress:progress;ctx.fillStyle='#168b9c';ctx.beginPath();ctx.arc(a.x+(b.x-a.x)*t,a.y+(b.y-a.y)*t,Math.min(4,1.5+Math.sqrt((edge.loading||0)/100)),0,Math.PI*2);ctx.fill();}
  }
  for(const area of s.areas){
    const p=areaScreen(area.id),active=activeArea===area.id;
    ctx.fillStyle=active?'#e4f4ff':'#fff';ctx.strokeStyle=active?'#1684bd':'#6c9fb9';ctx.lineWidth=active?2.5:1.3;
    ctx.beginPath();ctx.arc(p.x,p.y,active?13:11,0,Math.PI*2);ctx.fill();ctx.stroke();
    ctx.fillStyle='#326880';ctx.font='600 9px system-ui';ctx.textAlign='center';ctx.fillText(area.id,p.x,p.y+3);
  }
  ctx.fillStyle='#315c76';ctx.font='600 15px system-ui';ctx.textAlign='center';
  ctx.fillText(`${fmt(s.buses.length,0)} 母线 · ${s.areas.length} 个复制区域`,size.w/2,size.h/2);
  ctx.fillStyle='#7a91a3';ctx.font='11px system-ui';ctx.fillText('完整交流环网求解 · 此处按区域聚合显示',size.w/2,size.h/2+24);
  ctx.fillText('点击区域展开母线 · 点击联络线查看潮流',size.w/2,size.h/2+45);ctx.textAlign='start';
}
function draw(now=performance.now()){
  if(!size.w||!$('view-topology').classList.contains('active'))return;
  ctx.clearRect(0,0,size.w,size.h);ctx.lineCap='round';if(overview()){drawOverview(now);return;}const s=state.scene,selected=state.selected,points=new Map(visibleBuses.map(b=>[b.id,screen(b.id)])),query=state.search.toLowerCase(),currents=[];
  flowStats={eligible:0,animated:0,zero:0};
  const selectedBus=selected?.kind==='bus'?selected.id:null;
  if(s.areas){ctx.fillStyle='#708b9e';ctx.font='600 14px system-ui';ctx.textAlign='center';for(const area of s.areas){if(state.focusArea&&area.id!==state.focusArea)continue;const x=state.offset.x+area.center[0]*state.scale,y=state.offset.y-area.center[1]*state.scale;ctx.fillText(`${area.label} · ${area.bus_count.toLocaleString()} 节点`,x,y-1.14*state.scale);}ctx.textAlign='start';}
  const selectedPart=selectedPartition()||state.activePartition,highlightPart=trace.partIndex.get(selectedPart);
  const highlighted=new Set(highlightPart?.internal_buses||[]),neighbors=new Set(highlightPart?.boundary_buses||[]);
  if(highlightPart){const pts=highlightPart.internal_buses.map(id=>points.get(id)).filter(Boolean);if(pts.length){const xs=pts.map(p=>p.x),ys=pts.map(p=>p.y);ctx.fillStyle='#2789c012';ctx.strokeStyle='#2789c080';ctx.lineWidth=1.4;ctx.beginPath();ctx.roundRect(Math.min(...xs)-9,Math.min(...ys)-9,Math.max(...xs)-Math.min(...xs)+18,Math.max(...ys)-Math.min(...ys)+18,8);ctx.fill();ctx.stroke();ctx.fillStyle='#286b95';ctx.font='600 10px system-ui';ctx.fillText(`${highlightPart.display_name} · ${highlightPart.internal_buses.length} 节点`,Math.min(...xs)-5,Math.min(...ys)-15);}}
  for(const [kind,edges] of [['line',visibleLines],['trafo',visibleTrafos]]){
    for(let index=0;index<edges.length;index++){
      const e=edges[index];if(!edgeVisible(e))continue;const a=points.get(e.from),b=points.get(e.to);if(!a||!b)continue;
      const active=(selected?.kind===kind&&selected.id===e.id)||selectedBus===e.from||selectedBus===e.to||(query&&e.name.toLowerCase().includes(query))||(highlighted.has(e.from)&&highlighted.has(e.to));
      ctx.strokeStyle=active?'#0d78b4':state.color==='loading'?(e.loading==null?'#c6d1db':e.loading>100?'#cf6471':e.loading>80?'#c6a159':'#74ab9c'):e.from_layer!==e.to_layer?'#d0a267':e.from_layer==='main'?'#6d9fbe':e.is_cut_edge?'#cba463':'#bdcbd7';
      ctx.globalAlpha=active?1:.8;ctx.lineWidth=active?2.2:kind==='trafo'?1.2:e.from_layer==='main'?1:.65;
      if(e.is_intertie){ctx.strokeStyle=active?'#0d78b4':'#159b8e';ctx.lineWidth=active?3:2;ctx.globalAlpha=1;}
      ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
      const current=branchCurrent(e);
      if(current&&Math.hypot(b.x-a.x,b.y-a.y)>=.25){flowStats.eligible++;currents.push({e,a,b,current,active});}
      else if(Number.isFinite(e.p)&&Number.isFinite(e.q)&&!current)flowStats.zero++;
    }
  }
  ctx.globalAlpha=1;
  for(const b of visibleBuses){if(!busVisible(b))continue;const p=points.get(b.id),match=query&&(b.name.toLowerCase().includes(query)||String(b.id)===query),active=selectedBus===b.id||match,r=active?4.5:b.layer==='main'?2.5:1.5;ctx.fillStyle=nodeColor(b);ctx.beginPath();ctx.arc(p.x,p.y,r,0,Math.PI*2);ctx.fill();
    if(active||b.is_boundary&&state.layers.interface){ctx.strokeStyle=active?'#176c9c':'#c59e60';ctx.lineWidth=active?1.4:.7;ctx.beginPath();ctx.arc(p.x,p.y,r+2.2,0,Math.PI*2);ctx.stroke();}
    if(highlighted.has(b.id)||neighbors.has(b.id)){ctx.strokeStyle=highlighted.has(b.id)?'#157bab':'#ab7cc2';ctx.lineWidth=1.2;ctx.beginPath();ctx.arc(p.x,p.y,r+3.2,0,Math.PI*2);ctx.stroke();}
    if(active||state.labels&&(b.kv>=100||b.microgrid_name)){ctx.font='10px system-ui';ctx.fillStyle='#44647c';ctx.fillText(`${b.name} · ${b.kv}kV`,p.x+6,p.y-6);}
  }
  if(state.flow){for(const {e,a,b,current,active} of currents)if(drawCurrent(e,a,b,now,current,active))flowStats.animated++;}
  ctx.globalAlpha=1;
  if(state.layers.microgrid){for(const mg of s.microgrids){const p=points.get(mg.pcc_bus);if(!p)continue;ctx.strokeStyle='#9975c1';ctx.fillStyle='#9a77c317';ctx.lineWidth=1.3;ctx.beginPath();ctx.arc(p.x,p.y,7,0,Math.PI*2);ctx.fill();ctx.stroke();if(state.labels){ctx.fillStyle='#8663b3';ctx.font='9px system-ui';ctx.fillText(mg.name,p.x+10,p.y+9);}}}
  const status=state.flow?`${state.running?'计算中':'结果电流示意'} · ${flowStats.animated} 条支路`:state.scene.buses.length?`电流动画已停止 · ${flowStats.eligible} 条有流支路`:'等待计算电流';
  if($('flowStatus').textContent!==status)$('flowStatus').textContent=status;
}
function animation(now){if((state.running||state.flow||trace.playing)&&now-lastFrame>=32){draw(now);lastFrame=now;}requestAnimationFrame(animation);}
function select(kind,id){
  state.selected={kind,id};
  if(largeScene()){const item=element(kind,id),area=kind==='bus'?item?.area_id:item?.is_intertie?null:nodes.get(item?.from)?.area_id;if(area!==state.focusArea)focusArea(area);}
  renderSelection();requestDraw();
}
function segmentDistance(x,y,a,b){const dx=b.x-a.x,dy=b.y-a.y,t=Math.max(0,Math.min(1,((x-a.x)*dx+(y-a.y)*dy)/(dx*dx+dy*dy||1)));return Math.hypot(x-a.x-t*dx,y-a.y-t*dy);}
function pick(x,y){
  if(overview()){
    for(const area of state.scene.areas){const p=areaScreen(area.id);if(Math.hypot(x-p.x,y-p.y)<15){focusArea(area.id);return;}}
    if(state.layers.main){let nearest=null,distance=7;for(const e of interties){const d=segmentDistance(x,y,areaScreen(nodes.get(e.from).area_id),areaScreen(nodes.get(e.to).area_id));if(d<distance){nearest=e;distance=d;}}if(nearest)select('line',nearest.id);}
    return;
  }
  let closest=null,distance=10;
  for(const b of visibleBuses){if(!busVisible(b))continue;const p=screen(b.id),d=Math.hypot(x-p.x,y-p.y);if(d<distance){distance=d;closest={kind:'bus',id:b.id};}}
  if(!closest){for(const [kind,edges] of [['trafo',visibleTrafos],['line',visibleLines]])for(const e of edges){if(!edgeVisible(e))continue;const d=segmentDistance(x,y,screen(e.from),screen(e.to));if(d<distance&&d<6){distance=d;closest={kind,id:e.id};}}}
  if(closest)select(closest.kind,closest.id);
}
function activateTab(name){if(name==='analysis'&&trace.viewing)restoreFinal();document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id===`view-${name}`));if(name==='analysis')renderAnalysis();if(name==='topology')requestAnimationFrame(()=>{resize();});}
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
function disableControls(locked){const ready=!!selectedSnapshot();$('runBtn').disabled=locked||!ready;$('runBtn').title=ready?'使用所选计算时刻的 DAT 现场求解':state.dataset?'请先选择计算时刻':'请先上传数据集文件夹';$('algorithmSelect').disabled=locked||!ready;$('uploadBtn').disabled=locked;document.querySelectorAll('.snapshot-option').forEach(button=>button.disabled=locked);}
function lockControls(locked){disableControls(locked);$('runText').textContent=locked?'正在计算…':'运行潮流校核';$('runIcon').textContent=locked?'◌':'▶';$('runIcon').className=locked?'spinner':'';}
function updateElapsed(){
  if(!state.running||started===null)return;
  state.clientElapsedMs=performance.now()-started;
  $('timeKpi').textContent=`${(state.clientElapsedMs/1000).toFixed(2)} s`;
  $('runElapsed').textContent=`页面用时 ${(state.clientElapsedMs/1000).toFixed(3)} s`;
}
function finish(){flushLogs();updateElapsed();state.running=false;setFlow(false);state.activePartition=null;setPhase(null);lockControls(false);if(timer)clearInterval(timer);timer=null;if(stream)stream.close();stream=null;renderSelection();requestDraw();}
function failRun(message){state.source=state.scene.buses.length?(state.scene.result_status==='waiting'?'未收敛迭代值（非最终结果）':'本次失败 · 上次运行结果'):'本次失败 · 无计算结果';finish();addLog(message,'failed');origin('本次失败 · 未生成新结果','failed');stage('本次计算失败',message,true);$('timeSub').textContent='本次失败；原有结果不代表本次计算';}
async function run(){
  if(!selectedSnapshot()){addLog(state.dataset?'请先选择这套数据集的计算时刻。':'请先上传原始数据集文件夹。','failed');return;}
  if(state.running||state.importing)return;if(trace.viewing)restoreFinal();resetTrace();setFlow(false);state.running=true;state.run=null;state.task=null;state.clientElapsedMs=0;started=performance.now();$('uploadStatus').hidden=true;updateElapsed();$('timeSub').textContent='本次任务进行中 · 含排队与传输';$('timeSub').title='';lockControls(true);activateTab('topology');stage('正在创建计算任务','读取所选数据与算法');origin('现场计算中');
  addLog(`提交数据集 ${state.dataset.label} · ${selectedSnapshot().label} · ${$('algorithmSelect').selectedOptions[0].textContent}`);
  timer=setInterval(updateElapsed,80);
  try{
    const task=await api('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset_id:state.dataset.id,snapshot_id:state.snapshot,algorithm:$('algorithmSelect').value})});
    state.task=task.task_id;stream=new EventSource(`/api/runs/${task.task_id}/events`);
    stream.onmessage=async message=>{
      if(!state.running||state.task!==task.task_id)return;
      try{
        const e=JSON.parse(message.data);if(e.message)addLog(e.message,e.type,e);
        if(e.type==='network'){setScene(e,'本次计算 · 等待收敛');setFlow(true);stage('全网潮流求解中','节点电压跟随真实迭代更新');}
        if(e.type==='phase'){
          setPhase(e.stage);stage(`第 ${e.iteration} 轮 · ${phaseNames[e.stage]}`,`${e.partition_count} 个分区 / ${e.workers} 个 CPU 线程 · ${e.interface_unknowns} 个边界未知量`);
        }
        if(e.type==='iteration'){
          receiveFrame(e);
          const coupled=e.stage==='partition-schur';
          stage(`${coupled?'并行边界协调':'全网求解'} · 第 ${e.iteration} ${coupled?'轮':'步'}`,`${coupled?`${e.partition_count} 分区 / ${e.workers} 个 CPU 线程 · `:''}最大不平衡 ${exp(e.residual_mva)} MVA`);renderSelection();requestDraw();
          if(coupled)$('partitionKpi').textContent=`${e.partition_states.filter(p=>p.residual_mva<1e-4).length} / ${e.partition_count}`;
        }
        if(e.type==='partition'){
          state.activePartition=e.partition_id;stage(`分区校核 · P${String(e.partition_id).padStart(2,'0')}`,e.status==='running'?'使用本次全网边界电压':'局部 AC 校核已返回');
          if(e.result){const p=state.scene.partitions.find(p=>p.id===e.partition_id);if(p)Object.assign(p,e.result);$('partitionKpi').textContent=`${state.scene.partitions.filter(p=>p.converged===true).length} / ${state.scene.partitions.length}`;}requestDraw();
        }
        if(e.type==='completed'){
          stream.close();stream=null;
          const result=await api(`/api/runs/${task.task_id}/result`);if(state.task!==task.task_id)return;
          state.run=result.run;setScene(result,'本次现场计算结果');captureFinal();finish();
          $('timeSub').textContent=`后端 ${fmt(result.run.total_ms,1)} ms · 求解 ${fmt(result.run.solver_ms,1)} ms`;
          $('timeSub').title='后端耗时不含排队和网络；求解耗时为后端耗时的一部分。主卡片始终显示从点击运行到结果就绪的页面总用时。';
          const hasFailures=result.run.partition_failed>0;
          stage(hasFailures?'全网收敛 · 部分分区未通过':'本次潮流计算完成',`残差 ${exp(result.run.residual_mva)} MVA · ${result.run.iterations} 次迭代`,hasFailures);
          origin(`${state.scene.label} · 本次结果`,hasFailures?'failed':'live');renderAnalysis();
          $('replayCount').textContent=`${result.run.iterations} 轮已保存 · 手动查看`;
        }
        if(e.type==='failed')failRun(e.message);
      }catch(error){failRun(`结果处理失败：${error.message}`);}
    };
    stream.onerror=()=>{if(state.running)failRun('实时连接中断；本次完成状态未确认，任务日志保存在后端。');};
  }catch(error){failRun(`任务未能执行：${error.message}`);}
}
function renderDataset(){
  const dataset=state.dataset, snapshot=selectedSnapshot();
  $('datasetInfoBtn').disabled=!dataset;
  $('snapshotGroup').hidden=!dataset;$('datasetComposition').hidden=!dataset;$('dataUsageNote').hidden=!dataset;
  $('fileList').replaceChildren();$('snapshotChoices').replaceChildren();$('datasetComposition').replaceChildren();
  if(!dataset){
    $('folderName').textContent='尚未上传';$('folderSummary').textContent='请选择数据集文件夹，一个文件夹作为一套数据集';$('fileCount').textContent='(0)';return;
  }
  const counts=dataset.summary;
  $('folderName').textContent=dataset.label;
  $('folderSummary').textContent=`已上传 1 套数据集 · ${dataset.label} · 共 ${counts.file_count} 个文件`;
  for(const [label,value] of [['运行断面 DAT',counts.snapshot_count],['10kV 馈线表',counts.feeder_file_count],['馈线目录',counts.catalog_file_count],['其他配套表',counts.supplement_file_count]]){
    if(value)traceText($('datasetComposition'),'span',`${label} ${value} 份`);
  }
  $('fileCount').textContent=`(${dataset.files.length})`;
  for(const file of dataset.files){
    const row=traceText($('fileList'),'li',`${file.role} · ${file.path} · ${fmt(file.size_bytes/1024,1)} KiB`);
    const usage=file.kind==='snapshot'?(snapshot?.relative_path===file.path?'本次求解输入':snapshot?'本次未选用':'按计算时刻选择'):'已保存原文件 · 未参与本次求解';
    traceText(row,'div',usage,'file-usage');
  }
  for(const item of dataset.snapshots){
    const button=document.createElement('button');button.type='button';button.className=`snapshot-option${item.id===state.snapshot?' active':''}`;
    button.setAttribute('aria-pressed',String(item.id===state.snapshot));button.dataset.snapshotId=item.id;
    traceText(button,'strong',item.label);button.title=`${item.filename} · ${fmt(item.bus_count,0)} 母线 / ${fmt(item.branch_count,0)} 支路${item.time?'':' · 未标注时刻'}`;
    button.onclick=()=>changeSnapshot(item.id);button.disabled=state.running||state.importing;$('snapshotChoices').append(button);
  }
  $('snapshotHint').textContent=snapshot?`本次计算：${snapshot.label} · ${snapshot.filename} · ${fmt(snapshot.bus_count,0)} 母线 / ${fmt(snapshot.branch_count,0)} 支路`:'请选择本次计算时刻；各时刻属于上面同一套数据集。';
  const references=counts.feeder_file_count+counts.catalog_file_count+counts.supplement_file_count;
  $('dataUsageNote').textContent=references?'本次求解读取所选时刻的 DAT。馈线 Excel 和目录属于本数据集，已按原文件保存；当前未接入潮流模型，与 DAT 的对应关系待核对。':'本数据集包含 DAT 运行断面；本次按所选 DAT 的原始参数计算。';
}
function changeSnapshot(id){
  if(!state.dataset||state.running||(id!=null&&!state.dataset.snapshots.some(s=>s.id===id)))return;
  if(trace.viewing)restoreFinal();resetTrace();setFlow(false);$('replayDetails').open=false;state.snapshot=id;state.run=null;state.selected=null;state.activePartition=null;
  state.elementPage=0;state.partitionPage=0;state.search='';$('search').value='';$('elementSearch').value='';
  started=null;state.clientElapsedMs=null;$('timeKpi').textContent='—';$('timeSub').textContent='等待现场运行';$('timeSub').title='';$('runElapsed').textContent='页面用时 0.000 s';state.task=null;
  const snapshot=selectedSnapshot();
  setScene({scenario:emptyScene(),layout:{}},snapshot?'已选择运行断面 · 尚未求解':'数据集已上传 · 等待选择计算时刻');
  stage(snapshot?`已选择 ${snapshot.label} 运行断面`:'数据集已上传','选择计算时刻 → 运行潮流校核');
  origin(`${state.dataset.label} · ${snapshot?snapshot.label+' · 尚未求解':'请选择计算时刻'}`);
  $('replayLabel').textContent='运行后可逐轮查看';$('workerStatus').textContent='CPU 资源在运行时自动识别';
  renderDataset();disableControls(state.importing);
  if(snapshot)addLog(`数据集 ${state.dataset.label} · 选择计算时刻／断面：${snapshot.label}（${snapshot.filename}）`);
}
function transferSize(bytes){return bytes<1024**2?`${(bytes/1024).toFixed(1)} KiB`:`${(bytes/1024**2).toFixed(2)} MiB`;}
function setUploadStatus(phase,title,detail,percent=null){
  $('uploadStatus').hidden=false;$('uploadStatus').dataset.phase=phase;
  $('uploadStage').textContent=title;$('uploadDetail').textContent=detail;$('uploadDetail').title=detail;
  if(percent===null)$('uploadProgress').removeAttribute('value');else $('uploadProgress').value=Math.max(0,Math.min(100,percent));
  $('uploadPercent').textContent=phase==='failed'?'失败':percent===null?(phase==='recognizing'?'识别中':phase==='uploading'?'上传中':'准备中'):`${Math.floor(percent)}%`;
  $('uploadProgress').setAttribute('aria-valuetext',`${title}，${$('uploadPercent').textContent}，${detail}`);
}
function uploadDataset(files){
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();xhr.open('POST','/api/datasets/import');xhr.responseType='json';xhr.timeout=120000;
    xhr.setRequestHeader('Content-Type','application/json');
    xhr.upload.onprogress=event=>{
      const detail=event.lengthComputable?`${transferSize(event.loaded)} / ${transferSize(event.total)}（传输数据）`:`已传输 ${transferSize(event.loaded)}`;
      setUploadStatus('uploading','正在上传数据集',detail,event.lengthComputable?event.loaded/event.total*100:null);
    };
    xhr.upload.onload=()=>setUploadStatus('recognizing','文件已上传，正在识别','服务端正在校验原文件并识别 DAT 运行断面');
    xhr.onload=()=>{
      if(xhr.status<200||xhr.status>=300){reject(new Error(xhr.response?.error||`上传服务返回 HTTP ${xhr.status}`));return;}
      if(!xhr.response?.dataset?.snapshots?.length){reject(new Error('服务端未返回有效的数据集，请确认计算服务正常后重试'));return;}
      resolve(xhr.response);
    };
    xhr.onerror=()=>reject(new Error('上传连接中断，请检查网络后重试'));
    xhr.ontimeout=()=>reject(new Error('上传或识别超时，请检查网络及服务状态后重试'));
    xhr.onabort=()=>reject(new Error('上传已取消'));
    setUploadStatus('uploading','正在上传数据集','正在建立上传连接',0);
    xhr.send(JSON.stringify({files}));
  });
}
async function importFolder(event){
  if(!event.target.files.length)return;
  if(state.running||state.importing){event.target.value='';return;}
  state.importing=true;disableControls(true);
  try{
    const files=[...event.target.files].filter(f=>/\.(dat|xls|xlsx)$/i.test(f.name));
    if(!files.some(f=>/\.dat$/i.test(f.name)))throw new Error('所选数据集文件夹中没有 DAT 运行断面');
    if(files.length>500)throw new Error('文件数量超过 500 个');
    if(files.reduce((n,f)=>n+f.size,0)>32*1024**2||files.some(f=>f.size>16*1024**2))throw new Error('上传总大小限 32 MiB，单文件限 16 MiB');
    setUploadStatus('preparing','准备文件',`0 / ${files.length} 个文件`);
    await new Promise(requestAnimationFrame);
    addLog(`正在上传 1 套数据集，共 ${files.length} 个 DAT / Excel 文件，保留原始字节`);
    const payload=[];
    for(const file of files){
      setUploadStatus('preparing','准备文件',`${payload.length} / ${files.length} 个文件 · ${file.name}`);
      const bytes=new Uint8Array(await file.arrayBuffer());let raw='';
      for(let i=0;i<bytes.length;i+=16384)raw+=String.fromCharCode(...bytes.subarray(i,i+16384));
      payload.push({path:file.webkitRelativePath||file.name,content:btoa(raw)});
    }
    const result=await uploadDataset(payload);
    state.dataset=result.dataset;
    changeSnapshot(state.dataset.snapshots.length===1?state.dataset.snapshots[0].id:null);
    setUploadStatus('completed','导入完成',`${state.dataset.summary.file_count} 个文件 · ${state.dataset.snapshots.length} 个运行断面`,100);
    addLog(`成功导入 1 套数据集：${state.dataset.label}，含 ${state.dataset.snapshots.length} 个运行断面。${selectedSnapshot()?'已选择唯一断面。':'请选择计算时刻。'}`,'completed');
    if(state.dataset.files.some(f=>f.usage==='reference_only'))addLog('馈线表与目录属于本数据集；原文件已保存，当前不参与 DAT 潮流求解。');
  }catch(error){setUploadStatus('failed','导入失败',error.message,0);addLog(`导入失败：${error.message}`,'failed');}
  finally{state.importing=false;disableControls(false);$('folderInput').value='';}
}
function download(name,text,type='text/plain;charset=utf-8'){const url=URL.createObjectURL(new Blob([text],{type})),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),500);}
function exportCSV(){const quote=v=>`"${String(v??'').replaceAll('"','""')}"`;const rows=[['type','source_line','name','from','to','rate_mva','p_from_mw','q_from_mvar','current_loading_percent','apparent_power_loading_percent','input_r_pu','input_x_pu','input_impedance_base_mva','impedance_scale','system_r_pu','system_x_pu','system_base_mva','model_assumption'],...[...state.scene.lines.map(e=>({...e,type:'L'})),...state.scene.trafos.map(e=>({...e,type:'T'}))].map(e=>[e.type,e.source_line,e.name,nodes.get(e.from)?.name,nodes.get(e.to)?.name,e.rate_mva,e.p,e.q,e.loading,e.apparent_power_loading_percent,e.input_r_pu,e.input_x_pu,e.impedance_base_mva,e.impedance_scale,e.r_pu,e.x_pu,state.scene.base_mva,state.run.impedance_policy?.label||state.run.model_policy])];download(`sjz_${state.dataset.id}_${(selectedSnapshot()?.time||selectedSnapshot()?.filename||'result').replaceAll(':','-')}_powerflow.csv`,'\ufeff'+rows.map(r=>r.map(quote).join(',')).join('\n'),'text/csv;charset=utf-8');}
$('datasetInfoBtn').onclick=()=>$('datasetDialog').showModal();$('closeDatasetInfo').onclick=()=>$('datasetDialog').close();
$('runBtn').onclick=run;$('uploadBtn').onclick=()=>$('folderInput').click();$('folderInput').onchange=importFolder;
$('elementSearch').oninput=()=>{state.elementPage=0;renderAnalysis();};$('search').oninput=e=>{state.search=e.target.value.trim();requestDraw();};$('colorSelect').onchange=e=>{state.color=e.target.value;requestDraw();};$('flowToggle').onchange=e=>setFlow(e.target.checked);$('labelToggle').onchange=e=>{state.labels=e.target.checked;requestDraw();};
$('areaSelect').onchange=e=>focusArea(e.target.value);
$('search').onkeydown=e=>{if(e.key!=='Enter'||!state.search)return;const query=state.search.toLowerCase(),bus=state.scene.buses.find(b=>b.name.toLowerCase()===query||String(b.id)===query)||state.scene.buses.find(b=>b.name.toLowerCase().includes(query));if(bus){select('bus',bus.id);return;}const edge=[...state.scene.lines,...state.scene.trafos].find(e=>e.name.toLowerCase()===query)||[...state.scene.lines,...state.scene.trafos].find(e=>e.name.toLowerCase().includes(query));if(edge)select(edge.kind,edge.id);};
for(const key of ['element','partition'])for(const [suffix,delta] of [['Prev',-1],['Next',1]])$(`${key}${suffix}`).onclick=()=>{state[`${key}Page`]+=delta;renderAnalysis();};
$('exportBtn').onclick=()=>{if(trace.viewing)restoreFinal();exportCSV();};$('clearLog').onclick=()=>{pendingLogRows=[];if(logFrame!==null)cancelAnimationFrame(logFrame);logFrame=null;$('log').replaceChildren();$('logCount').textContent=0;};$('downloadLog').onclick=()=>{if(state.task){const a=document.createElement('a');a.href=`/api/runs/${state.task}/trace`;a.download=`powerflow-${state.task}-trace.json`;a.click();}else download('sjz_session_log.json',JSON.stringify(state.log,null,2),'application/json');};
$('replayPlay').onclick=playReplay;$('replayLive').onclick=restoreFinal;
$('replaySlider').oninput=e=>{pauseReplay();showReplayFrame(Number(e.target.value));};
$('replayPrev').onclick=()=>{pauseReplay();showReplayFrame(trace.index-1);};$('replayNext').onclick=()=>{pauseReplay();showReplayFrame(trace.index+1);};
document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>activateTab(button.dataset.tab));document.querySelectorAll('[data-layer]').forEach(button=>button.onclick=()=>{const layer=button.dataset.layer;state.layers[layer]=!state.layers[layer];button.classList.toggle('active',state.layers[layer]);requestDraw();});
$('fitBtn').onclick=fit;$('zoomIn').onclick=()=>zoom(1.25,size.w/2,size.h/2);$('zoomOut').onclick=()=>zoom(.8,size.w/2,size.h/2);
function zoom(factor,x,y){state.offset.x=x+(state.offset.x-x)*factor;state.offset.y=y+(state.offset.y-y)*factor;state.scale*=factor;requestDraw();}
canvas.addEventListener('wheel',event=>{event.preventDefault();const box=canvas.getBoundingClientRect();zoom(event.deltaY<0?1.13:.885,event.clientX-box.left,event.clientY-box.top);},{passive:false});
canvas.addEventListener('pointerdown',event=>{lastPointer={x:event.clientX,y:event.clientY,ox:state.offset.x,oy:state.offset.y};state.dragging=true;canvas.classList.add('dragging');canvas.setPointerCapture(event.pointerId);});
canvas.addEventListener('pointermove',event=>{if(!state.dragging||!lastPointer)return;state.offset={x:lastPointer.ox+event.clientX-lastPointer.x,y:lastPointer.oy+event.clientY-lastPointer.y};requestDraw();});
canvas.addEventListener('pointerup',event=>{if(lastPointer&&Math.hypot(event.clientX-lastPointer.x,event.clientY-lastPointer.y)<5){const box=canvas.getBoundingClientRect();pick(event.clientX-box.left,event.clientY-box.top);}state.dragging=false;canvas.classList.remove('dragging');});canvas.addEventListener('pointercancel',()=>{state.dragging=false;canvas.classList.remove('dragging');});
window.addEventListener('resize',resize);new ResizeObserver(resize).observe($('mapWrap'));
rebuildIndex();render();resize();fit();requestAnimationFrame(animation);
disableControls(false);window.SJZ_BOOT?.ready();
stage('等待上传原始数据','选择数据文件夹 → 识别 DAT → 运行潮流校核');
renderDataset();
addLog('等待上传原始数据文件夹；当前未加载任何数据集或计算结果。');
addLog('建模设置：T 卡阻抗按 100 MVA 假设解释，L 卡采用文件系统基准；原始文件、负荷、容量和控制不变，过载完整保留。');
api('/health').then(()=>{$('connectionDot').className='online';$('connectionText').textContent='本地计算服务已连接';}).catch(error=>{$('connectionDot').className='offline';$('connectionText').textContent='计算服务未连接';addLog(error.message,'failed');});
