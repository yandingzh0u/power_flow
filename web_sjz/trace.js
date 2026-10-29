'use strict';
// Measurements and replay share the same frames delivered by the solver.
const trace = {frames:[], current:null, parts:new Map(), partIndex:new Map(), edgeIndex:new Map(),
  partEdges:new Map(), centers:new Map(), viewing:false, playing:false, phase:null, index:0,
  phaseIndex:0, playbackTimer:null, finalNodes:null, finalEdges:null, selectedPart:null};
const phaseNames = {'local-elimination':'各区并行消元','boundary-coordination':'边界等值方程协调','back-substitution':'边界修正回传 / 局部回代'};

function indexTrace(){
  trace.partIndex=new Map(state.scene.partitions.map(p=>[p.id,p]));trace.edgeIndex=new Map();
  trace.partEdges=new Map();trace.centers=new Map();
  for(const p of state.scene.partitions){
    const coords=p.internal_buses.map(id=>state.layout[String(id)]).filter(Boolean);
    trace.centers.set(p.id,coords.length?[coords.reduce((s,v)=>s+v[0],0)/coords.length,coords.reduce((s,v)=>s+v[1],0)/coords.length]:[0,0]);
    trace.partEdges.set(p.id,[]);
  }
  for(const e of [...state.scene.lines,...state.scene.trafos]){
    trace.edgeIndex.set(e.id,e);
    const a=nodes.get(e.from)?.partition_id,b=nodes.get(e.to)?.partition_id;
    if(a!==b){if(a)trace.partEdges.get(a)?.push(e);if(b)trace.partEdges.get(b)?.push(e);}
  }
}
function setPhase(name, replay=false){
  trace.phase=name;
  if(replay)$('replayLabel').textContent=`回放第 ${trace.frames[trace.index]?.iteration??0} 轮 · ${phaseNames[name]||'初始状态'}（顶部为最终统计）`;
  requestDraw();
}
function resetTrace(){
  pauseReplay();trace.frames=[];trace.current=null;trace.parts.clear();trace.viewing=false;
  trace.finalNodes=null;trace.finalEdges=null;trace.selectedPart=null;trace.index=0;trace.phaseIndex=0;
  setPhase(null);$('replaySlider').value=0;$('replaySlider').max=0;
  ['replayPrev','replayPlay','replayNext','replaySlider','replayLive'].forEach(id=>$(id).disabled=true);
  $('replayLabel').textContent='正在记录真实迭代';$('replayCount').textContent='运行后可手动查看';renderPartitionTrace();
}
function decodeFrame(event){
  let values;
  if(event.node_state_base64){
    const raw=atob(event.node_state_base64),bytes=new Uint8Array(raw.length);
    for(let i=0;i<raw.length;i++)bytes[i]=raw.charCodeAt(i);
    values=new Float64Array(bytes.buffer);
  }else{
    const rows=event.node_states||event.voltages.map(v=>[v[0],v[1],NaN,NaN]);
    values=new Float64Array(rows.length*4);
    rows.forEach((row,i)=>values.set(row,i*4));
  }
  return {iteration:event.iteration,stage:event.stage,residual_mva:event.residual_mva,
    workers:event.workers,available_cpus:event.available_cpus,coordination:event.coordination,
    partition_states:event.partition_states||[],boundary_flows:event.boundary_flows||[],microgrid_states:event.microgrid_states||[],values};
}
function receiveFrame(event){
  const frame=decodeFrame(event);trace.frames.push(frame);trace.index=trace.frames.length-1;
  applyFrame(frame);setPhase('back-substitution');
  $('replaySlider').max=trace.frames.length-1;$('replaySlider').value=trace.index;
  $('replayLabel').textContent=`已记录 ${trace.frames.length} 个迭代状态`;
  $('workerStatus').textContent=event.workers?`可用 ${event.available_cpus??event.workers} 个逻辑 CPU · 配置 ${event.workers} 线程 · 本轮实际 ${event.coordination?.active_worker_threads??'—'} 线程`:'本次采用全网算法 · 已记录逐轮状态';
}
function applyFrame(frame){
  trace.current=frame;trace.parts=new Map(frame.partition_states.map(p=>[p.id,p]));
  for(let i=0;i<state.scene.buses.length;i++){
    const b=state.scene.buses[i],offset=i*4;b.vm=frame.values[offset];b.va=frame.values[offset+1];
    b.p=frame.values[offset+2];b.q=frame.values[offset+3];
  }
  // Boundary values are computed by Python from the original pi/tap stamps.
  for(const row of frame.boundary_flows){const e=trace.edgeIndex.get(row[0]);if(e){[e.p,e.q,e.p_to,e.q_to,e.loading]=row.slice(1);e.iteration=true;}}
  updateVisibleFlows();renderSelection();renderPartitionTrace();requestDraw();
}
function iterationFlow(e){
  if(e.ideal){e.p=e.q=e.p_to=e.q_to=e.loading=null;return;}
  const a=nodes.get(e.from),b=nodes.get(e.to);
  if(a?.vm==null||b?.vm==null)return;
  const angleA=a.va*Math.PI/180,angleB=b.va*Math.PI/180;
  const ax=a.vm*Math.cos(angleA),ay=a.vm*Math.sin(angleA),bx=b.vm*Math.cos(angleB),by=b.vm*Math.sin(angleB);
  const z=e.r_pu**2+e.x_pu**2;if(!z)return;
  const g=e.r_pu/z,h=-e.x_pu/z,t=e.tap||1,hg=e.half_g_pu||0,hb=e.half_b_pu||0,base=state.scene.base_mva||1000;
  const ix=((g+hg)*ax-(h+hb)*ay)/t**2-(g*bx-h*by)/t;
  const iy=((g+hg)*ay+(h+hb)*ax)/t**2-(g*by+h*bx)/t;
  const jx=(g+hg)*bx-(h+hb)*by-(g*ax-h*ay)/t;
  const jy=(g+hg)*by+(h+hb)*bx-(g*ay+h*ax)/t;
  e.p=base*(ax*ix+ay*iy);e.q=base*(ay*ix-ax*iy);e.p_to=base*(bx*jx+by*jy);e.q_to=base*(by*jx-bx*jy);
  e.loading=e.rate_mva?Math.max(Math.hypot(ix,iy),Math.hypot(jx,jy))*base/e.rate_mva*100:null;e.iteration=true;
}
function updateVisibleFlows(){
  if(!trace.current||(!state.running&&!trace.viewing))return;
  const edges=overview()?interties:[...visibleLines,...visibleTrafos];
  for(const e of edges)iterationFlow(e);
  if(state.selected&&state.selected.kind!=='bus'){const e=element(state.selected.kind,state.selected.id);if(e)iterationFlow(e);}
}
function captureFinal(){
  trace.finalNodes=new Float64Array(state.scene.buses.length*4);
  state.scene.buses.forEach((b,i)=>trace.finalNodes.set([b.vm,b.va,b.p,b.q],i*4));
  const edges=[...state.scene.lines,...state.scene.trafos];trace.finalEdges=new Float64Array(edges.length*5);
  edges.forEach((e,i)=>trace.finalEdges.set([e.p,e.q,e.p_to,e.q_to,e.loading??NaN],i*5));
  ['replayPlay','replaySlider','replayLive'].forEach(id=>$(id).disabled=!trace.frames.length);
  $('replayPrev').disabled=$('replayNext').disabled=trace.frames.length<2;
  $('replayLabel').textContent='最终结果 · 迭代记录仅手动播放';
}
function pauseReplay(){
  trace.playing=false;if(trace.playbackTimer)clearTimeout(trace.playbackTimer);trace.playbackTimer=null;
  $('replayPlay').textContent='▶ 迭代回放';
}
function restoreFinal(){
  pauseReplay();trace.viewing=false;setPhase(null);
  if(trace.finalNodes){
    state.scene.buses.forEach((b,i)=>{[b.vm,b.va,b.p,b.q]=trace.finalNodes.subarray(i*4,i*4+4);});
    [...state.scene.lines,...state.scene.trafos].forEach((e,i)=>{[e.p,e.q,e.p_to,e.q_to,e.loading]=trace.finalEdges.subarray(i*5,i*5+5);if(Number.isNaN(e.loading))e.loading=null;e.iteration=false;});
  }
  trace.current=trace.frames.at(-1)||null;trace.parts=new Map((trace.current?.partition_states||[]).map(p=>[p.id,p]));
  trace.index=Math.max(0,trace.frames.length-1);$('replaySlider').value=trace.index;
  $('replayLabel').textContent='最终结果 · 回放不计入求解耗时';
  if(state.run)stage('本次潮流计算完成',`残差 ${exp(state.run.residual_mva)} MVA · ${state.run.iterations} 次迭代`);
  renderSelection();renderPartitionTrace();requestDraw();
}
function showReplayFrame(index,phase='back-substitution'){
  if(state.running||!trace.frames.length)return;
  trace.viewing=true;trace.index=Math.max(0,Math.min(index,trace.frames.length-1));
  // Local elimination uses the preceding iterate; the new voltage is applied
  // only when the correction is returned and back-substitution is complete.
  const frame=trace.frames[phase==='back-substitution'?trace.index:Math.max(0,trace.index-1)];
  applyFrame(frame);setPhase(phase,true);$('replaySlider').value=trace.index;
  stage('计算已完成 · 真实迭代回放',`第 ${trace.frames[trace.index].iteration} 轮 · ${phaseNames[phase]}`);
}
function playReplay(){
  if(trace.playing){pauseReplay();return;}
  if(state.running||trace.frames.length<2)return;
  trace.playing=true;trace.index=0;trace.phaseIndex=0;$('replayPlay').textContent='Ⅱ 暂停回放';
  function tick(){
    if(!trace.playing)return;
    showReplayFrame(trace.index);trace.index++;
    if(trace.index>=trace.frames.length){trace.playbackTimer=setTimeout(restoreFinal,300/Number($('replaySpeed').value));return;}
    trace.playbackTimer=setTimeout(tick,300/Number($('replaySpeed').value));
  }
  tick();
}
function selectedPartition(){
  const item=state.selected&&element(state.selected.kind,state.selected.id);
  if(state.selected?.kind==='bus')return item?.partition_id||null;
  return nodes.get(item?.from)?.partition_id||nodes.get(item?.to)?.partition_id||trace.selectedPart;
}
function traceText(parent,tag,text,className=''){const el=document.createElement(tag);el.textContent=text;if(className)el.className=className;parent.append(el);return el;}
function selectedMicrogrid(){
  const item=state.selected&&element(state.selected.kind,state.selected.id);
  const id=item?.microgrid_id||item?.connected_microgrid_id;
  return state.scene.microgrids.find(m=>m.id===id);
}
function renderPartitionTrace(){
  const root=$('partitionTrace');if(!root)return;root.replaceChildren();
  const mg=selectedMicrogrid();
  if(mg){
    const m=(state.running||trace.viewing)?trace.current?.microgrid_states.find(m=>m.id===mg.id):mg;
    traceText(root,'p',`${mg.name} · 独立微网子系统 · ${mg.internal_buses.length} 个内部母线 · PCC ${mg.pcc_name}`);
    if(state.microgridFocus!==mg.id)traceText(root,'button','查看微网内部网架','text-button').onclick=()=>focusMicrogrid(mg);
    const details=traceText(root,'div','','trace-metrics');
    for(const text of [`配网 → 微网：V=${fmt(m?.pcc_vm_pu,6)} pu，θ=${fmt(m?.pcc_va_degree,5)}°`,
      `微网 → 配网：购电 ${fmt(m?.import_p_mw,6)} MW / ${fmt(m?.import_q_mvar,6)} Mvar`,
      `入口 P/Q 残差 ${exp(m?.boundary_mismatch_p_mw)} / ${exp(m?.boundary_mismatch_q_mvar)}`,
      `微网方程残差 ${exp(m?.max_equation_residual_mva)} MVA`,
      `有功平衡误差 ${exp(m?.balance_p_mw)} MW`,
      `内部最低电压 ${fmt(m?.voltage_min_pu,6)} pu`])traceText(details,'span',text);
    traceText(root,'div','内部网架、负荷、光伏与储能 PQ 参与求解；独立消元并反馈 Schur 响应，与配网共同迭代。','trace-note');
  }
  const id=selectedPartition(),part=trace.partIndex.get(id),measurement=trace.parts.get(id);
  if(!part){root.textContent='点击配网节点或分区结果行，查看分区、工作线程和真实边界交换。主网节点由边界协调器统一求解。';return;}
  traceText(root,'p',`${part.display_name} · ${part.subsystem_type==='microgrid'?'微网计算域':'配网计算域'} · ${part.internal_buses.length} 节点 · 自身边界 ${part.boundary_internal_buses?.length??'—'} · 邻区 ${(part.neighbor_partition_ids||[]).map(p=>trace.partIndex.get(p)?.display_name||`P${p}`).join('、')||'无'}${part.has_main_interface?' · 连接主网':''}`);
  const grid=traceText(root,'div','','trace-metrics');
  for(const text of [`第 ${trace.current?.iteration??'—'} 轮 · 线程 ${measurement?.worker_id??'—'}（从 0 编号）`,
    `局部消元 ${fmt(measurement?.local_ms,4)} ms`,`分区残差 ${exp(measurement?.residual_mva)} MVA`,
    `区内最大 ΔV ${exp(measurement?.delta_vm_pu)} pu`,`区内最大 Δθ ${exp(measurement?.delta_va_deg)}°`,
    `全接口 ΔV ${exp(trace.current?.coordination?.boundary_max_delta_voltage_pu)} pu`])traceText(grid,'span',text);
  const edges=trace.partEdges.get(id)||[],scroll=traceText(root,'div','','trace-scroll'),table=traceText(scroll,'table','');
  const head=table.createTHead().insertRow();['接口 / 正向','首端 P / Q','末端 P / Q','两端和 P / Q'].forEach(s=>traceText(head,'th',s));
  const body=table.createTBody();
  for(const e of edges.slice(0,12)){
    if(state.running||trace.viewing)iterationFlow(e);
    const row=body.insertRow();[`${nodes.get(e.from)?.name} → ${nodes.get(e.to)?.name}`,`${fmt(e.p,3)} / ${fmt(e.q,3)}`,`${fmt(e.p_to,3)} / ${fmt(e.q_to,3)}`,e.p==null?'—':`${fmt(e.p+e.p_to,4)} / ${fmt(e.q+e.q_to,4)}`].forEach(s=>traceText(row,'td',s));
    row.onclick=()=>select(e.kind,e.id);
  }
  traceText(root,'div',`P：MW；Q：Mvar。两端和包含串联损耗与并联充电，不应强制为零。显示 ${Math.min(12,edges.length)} / ${edges.length} 个接口，完整逐轮数据可下载。`,'trace-note');
  if(trace.frames.length){
    const history=traceText(root,'details','');traceText(history,'summary','该分区逐轮收敛记录');
    for(const frame of trace.frames){const p=frame.partition_states.find(p=>p.id===id);if(p)traceText(history,'div',`第 ${frame.iteration} 轮：${exp(p.residual_mva)} MVA · ΔV ${exp(p.delta_vm_pu)} pu · Δθ ${exp(p.delta_va_deg)}°`);}
  }
}
