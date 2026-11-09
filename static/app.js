'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const state = {task:null, step:'scan', page:0, pageSize:8, pending:{}, saving:null, saveTimer:null, job:null, preview:null, receipt:null, visible:[], diagnostic:null, adoptAll:null};
let toastTimer;
function toast(message, error=false) {
  clearTimeout(toastTimer); $('toast').textContent=message; $('toast').classList.remove('hidden'); $('toast').classList.toggle('error',error);
  toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),error?8500:4000);
}
async function api(path,body) {
  const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'X-Workbench-Token':window.WORKBENCH_TOKEN,...(body===undefined?{}:{'Content-Type':'application/json'})},body:body===undefined?undefined:JSON.stringify(body)});
  const data=await response.json(); if(!response.ok || data.error) throw new Error(data.error || '请求失败'); return data;
}
function action(fn){return async event=>{try{await fn(event);}catch(error){toast(error.message,true);}};}
function setStep(step) {
  state.step=step;
  for(const pane of document.querySelectorAll('.pane')) pane.classList.toggle('hidden',pane.id!=='pane-'+step);
  for(const button of document.querySelectorAll('[data-step]')) button.classList.toggle('active',button.dataset.step===step);
  $('breadcrumb').textContent='工作流程 / '+({scan:'扫描分析',model:'模型建议',review:'对比与审核',apply:'预览与落地'}[step]);
  if(step==='review') renderReview();
  window.scrollTo({top:0});
}
async function refreshTasks(){
  const info=await api('/api/info'); $('version').textContent='v'+info.version;
  $('task-list').innerHTML=info.tasks.map(task=>`<button class="task-link ${state.task?.id===task.id?'selected':''}" data-task="${esc(task.id)}" title="${esc(task.title)}">${esc(task.title)}<small>${task.count} 项 · ${esc(new Date(task.createdAt).toLocaleDateString())}</small></button>`).join('') || '<div class="task-link">暂无任务</div>';
  return info;
}
async function loadTask(id){
  await flushReview();
  state.task=await api('/api/task?task='+encodeURIComponent(id)); state.page=0;state.preview=null;state.receipt=null;state.pending={};
  $('apply-root').value=state.task.local.projectRoot || '';
  $('task-badge').textContent=state.task.local.title || '已导入任务';
  $('export-task').disabled=false;
  localStorage.setItem('jwb-current-task',id);
  for(const section of document.querySelectorAll('.needs-task')) section.classList.remove('hidden');
  for(const id of ['model-empty','review-empty','apply-empty']) $(id).classList.add('hidden');
  $('preview-panel').classList.add('hidden');$('receipt-panel').classList.add('hidden');
  $('batch-result').classList.add('hidden');
  $('git-status').className='status-panel';$('git-status').textContent='写入前请检查本机项目的 Git 状态。';
  renderSummary();renderRuns();renderReview();renderReceipts();await refreshTasks();
}
function renderSummary(){
  if(!state.task)return;
  const items=state.task.analysis.items; const review=state.task.review;
  const totalRuns=state.task.runs.length;const adopted=items.filter(i=>review[i.id]?.use).length;
  $('model-summary').innerHTML=[['待补说明',items.length],['模型结果',totalRuns],['已审核采用',adopted]].map(([label,value])=>`<div class="stat-card"><strong>${value}</strong><span>${label}</span></div>`).join('');
  $('adopted-count').textContent=`${adopted} 项采用 / ${items.length} 项`;
  const analysis=state.task.analysis;
  $('warning-count').textContent=`${analysis.warnings.length} 条警告 · ${analysis.skipped.length} 项未自动定位`;
  $('scan-warnings').textContent=[...analysis.warnings,...analysis.skipped.map(s=>`${s.target}：${s.reason}${s.candidates?'\n  '+s.candidates.join('\n  '):''}`)].join('\n') || '没有扫描警告。';
}
function renderRuns(){
  if(!state.task)return;
  const selected=$('batch-source').value;
  $('batch-source').innerHTML='<option value="auto">唯一建议自动填入</option>'+state.task.runs.map(run=>`<option value="${esc(run.id)}">来源：${esc(run.profile.model)} · ${esc(run.id.slice(0,6))}</option>`).join('');
  if([...$('batch-source').options].some(option=>option.value===selected))$('batch-source').value=selected;
  $('model-runs').innerHTML=state.task.runs.map(run=>`<div class="run-card"><strong>${esc(run.profile.model)}</strong><small>${esc(run.profile.baseUrl)}</small><small>${run.items.length} / ${state.task.analysis.items.length} 项已生成 · ${esc(new Date(run.createdAt).toLocaleString())}</small>${run.errors.length?`<small class="run-errors">最近错误：${esc(run.errors.at(-1).message)}</small>`:''}</div>`).join('') || '<p class="hint">尚无模型建议。填写连接信息后开始生成；也可以先导出分析包，在另一台电脑继续。</p>';
}
function getRow(item){return state.task.review[item.id] || {text:'',use:false,runId:'manual'};}
function proposals(item){return state.task.runs.flatMap(run=>run.items.filter(row=>row.id===item.id).map(row=>({...row,model:run.profile.model,runId:run.id})));}
function filteredItems(){
  if(!state.task)return [];
  const query=$('review-search').value.trim().toLowerCase(),kind=$('review-kind').value,status=$('review-status').value;
  return state.task.analysis.items.filter(item=>(!kind || item.kind===kind)&&(!query || (item.target+' '+item.file).toLowerCase().includes(query))&&(!status || Boolean(getRow(item).use)===(status==='yes')));
}
function renderReview(){
  if(!state.task)return;
  const filtered=filteredItems(),pages=Math.max(1,Math.ceil(filtered.length/state.pageSize)); state.page=Math.min(state.page,pages-1);
  state.visible=filtered.slice(state.page*state.pageSize,(state.page+1)*state.pageSize);
  $('review-count').textContent=`${filtered.length} 项`;
  $('page-count').textContent=`第 ${state.page+1} / ${pages} 页`;
  $('prev-page').disabled=state.page===0;$('next-page').disabled=state.page>=pages-1;
  $('review-items').innerHTML=state.visible.map(item=>{
    const row=getRow(item),choices=proposals(item);
    return `<article class="review-item ${row.use?'adopted':''}" data-item="${esc(item.id)}"><div class="item-header"><div><div class="item-target"><span class="item-type">${esc(item.label)}</span>${esc(item.target)}</div><div class="item-location">${esc(item.file)}:${item.line}</div></div><label class="check-line"><input type="checkbox" data-use="${esc(item.id)}" ${row.use?'checked':''}>采用</label></div><div class="item-grid"><div>${choices.length?choices.map(choice=>`<div class="suggestion-card"><strong>${esc(choice.model)}</strong><pre>${esc(choice.suggestion)}</pre><small>${esc(choice.reason)}</small><button class="button secondary small" data-choose="${esc(item.id)}" data-run="${esc(choice.runId)}">采用此建议 →</button></div>`).join(''):'<div class="suggestion-card"><strong>尚无模型建议</strong><p class="hint">可先到模型页生成，也可以直接填写实际写入内容并勾选采用。</p></div>'}</div><div><label class="final-editor">实际写入内容<textarea rows="${item.kind==='javadoc'?7:4}" data-text="${esc(item.id)}" placeholder="采用模型建议后自动填入，也可以手动编辑。">${esc(row.text)}</textarea></label><small>这里的文字将写入源码；只有勾选“采用”才会落地。${item.kind==='javadoc'?' JavaDoc 只写内文，保留与源码一致的标签。':''}</small></div></div><details class="item-source"><summary>查看源码片段与模型上下文</summary><pre>${esc(item.snippet || '扫描时未携带源码片段')}\n\n${esc(JSON.stringify(item.context,null,2))}</pre></details></article>`;
  }).join('') || '<div class="empty-state">当前筛选下没有说明项。</div>';
  renderSummary();
}
function patchRow(id,patch){
  const item=state.task.analysis.items.find(i=>i.id===id);
  if(!item)return;
  const row={...getRow(item),...patch};state.task.review[id]=row;state.pending[id]=row;
  $('save-state').textContent='有更改待保存…';
  clearTimeout(state.saveTimer);state.saveTimer=setTimeout(()=>flushReview().catch(error=>toast(error.message,true)),600);
  state.preview=null;$('apply-button').disabled=true;renderSummary();
}
async function flushReview(){
  clearTimeout(state.saveTimer);
  if(state.saving) await state.saving;
  if(!state.task || !Object.keys(state.pending).length)return;
  const rows=state.pending,task=state.task.id;state.pending={};$('save-state').textContent='保存中…';
  const operation=api('/api/review',{task,rows});state.saving=operation;
  try{await operation;$('save-state').textContent='已保存';}
  catch(error){state.pending={...rows,...state.pending};$('save-state').textContent='保存失败，请检查说明';throw error;}
  finally{state.saving=null;}
  if(Object.keys(state.pending).length)await flushReview();
}
async function download(name,path){
  await flushReview();
  const response=await fetch(path,{headers:{'X-Workbench-Token':window.WORKBENCH_TOKEN}});
  if(!response.ok){const data=await response.json();throw new Error(data.error || '下载失败');}
  const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),5000);
}
const taskPath=(route,extra='')=>route+'?task='+encodeURIComponent(state.task.id)+extra;
async function watchJob(id){
  state.job=id;$('global-progress').classList.remove('hidden');
  try{
    while(true){
      const job=await api('/api/job?id='+encodeURIComponent(id));
      $('progress-text').textContent=job.message;$('progress-detail').textContent=job.total?`${job.completed} / ${job.total} 项已保存`:'分析和连接测试不修改项目源码';
      $('cancel-job').disabled=!job.task || job.cancel || job.status!=='running';
      if(job.status!=='running'){
        if(job.result?.task)await loadTask(job.result.task);
        else if(job.task)await loadTask(job.task);
        if(job.status==='error')throw new Error(job.error);
        toast(job.status==='cancelled'?'已停止，已完成批次已保留':'处理完成，结果已保存');
        return job;
      }
      await new Promise(resolve=>setTimeout(resolve,900));
    }
  }finally{state.job=null;$('global-progress').classList.add('hidden');}
}
function renderGit(status){
  $('git-status').className='status-panel '+(status.ready?'success':'warning');
  $('git-status').innerHTML=`<strong>${status.ready?'✓ Git 工作区干净':'Git 提醒（不强制提交）'}</strong><p>${esc(status.reason)}</p>${status.repo?`<small>${esc(status.repo)} · HEAD ${esc(status.head?.slice(0,10) || '尚无提交')}</small>`:''}${status.changes.length?`<pre>${esc(status.changes.join('\n'))}</pre><p>可先提交或整理；也可直接查看差异，在最后确认框中确认提示后继续写入。</p>`:''}`;
}
function diffHtml(diff){return diff.split('\n').map(line=>`<span class="diff-line ${line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')?'meta':line.startsWith('+')?'add':line.startsWith('-')?'remove':''}">${esc(line)}</span>`).join('');}
function renderReceipts(){
  if(!state.task)return;
  $('receipt-list').innerHTML=state.task.receipts.map(receipt=>`<div class="receipt-row"><span>${receipt.status==='restored'?'已恢复':receipt.status==='applied'?'已写入':'未完成'} · ${esc(new Date(receipt.createdAt).toLocaleString())}<small>Git 基线 ${esc(receipt.head?.slice(0,10) || '无可用 HEAD')} · ${esc(receipt.id.slice(0,8))}</small></span><div><button class="button secondary small" data-history-diff="${esc(receipt.id)}">下载 diff</button>${receipt.status==='applied'?` <button class="button secondary small" data-restore="${esc(receipt.id)}">从备份恢复</button>`:''}</div></div>`).join('') || '<p class="hint">尚未写入过源码。</p>';
}
async function confirmWrite(){
  await flushReview();
  if(!state.preview)throw new Error('审核已变化，请重新生成差异预览');
  const status=await api('/api/git-status',{projectRoot:state.preview.projectRoot});
  if(status.head!==state.preview.git.head)throw new Error('Git HEAD 已变化，请重新生成差异预览');
  state.preview.git=status;renderGit(status);
  $('confirm-description').textContent=`将修改 ${state.preview.files.length} 个文件、写入 ${state.preview.files.reduce((n,f)=>n+f.ids.length,0)} 项说明。`;
  $('git-warning-confirm').classList.toggle('hidden',status.ready);
  $('git-warning-detail').textContent=status.reason;
  $('acknowledge-git-warning').checked=false;$('confirm-apply').disabled=!status.ready;
  $('confirm-dialog').showModal();
}
document.querySelectorAll('[data-step]').forEach(button=>button.addEventListener('click',action(async()=>{await flushReview();setStep(button.dataset.step);}))); 
$('task-list').addEventListener('click',action(async event=>{const target=event.target.closest('[data-task]');if(target){await loadTask(target.dataset.task);setStep('review');}}));
$('refresh-tasks').addEventListener('click',action(refreshTasks));
$('scan-form').addEventListener('submit',action(async event=>{
  event.preventDefault();if(state.job)throw new Error('请等待当前处理完成');await flushReview();
  const file=$('openapi-file').files[0];const spec=file?JSON.parse(await file.text()):null;
  const modes=[$('mode-swagger').checked?'swagger':null,$('mode-javadoc').checked?'javadoc':null].filter(Boolean);
  const result=await api('/api/scan',{title:$('scan-title').value.trim(),projectRoot:$('project-root').value.trim(),roots:$('source-roots').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),modes,spec,source:file?'':$('openapi-url').value.trim(),includeSnippets:$('include-snippets').checked});
  const job=await watchJob(result.job);if(job.status==='done')setStep('model');
}));
$('openapi-file').addEventListener('change',()=>{$('openapi-filename').textContent=$('openapi-file').files[0]?.name || '未选择文件';});
$('clear-openapi').addEventListener('click',()=>{$('openapi-file').value='';$('openapi-filename').textContent='未选择文件';});
$('demo-button').addEventListener('click',action(async()=>{await flushReview();const result=await api('/api/demo',{});await loadTask(result.task);setStep('review');toast('已创建隔离示例，包含模拟建议，未调用付费模型');}));
$('import-task').addEventListener('click',()=>$('bundle-file').click());
$('bundle-file').addEventListener('change',action(async()=>{
  const file=$('bundle-file').files[0];if(!file)return;await flushReview();if(file.size>80_000_000)throw new Error('文件超过 80MB');
  const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
  const result=await api('/api/import',{task:state.task?.id || '',data:btoa(binary),filename:file.name});await loadTask(result.task);setStep('review');$('bundle-file').value='';toast('导入成功，可独立生成、审核或重新选择本机项目落地');
}));
$('export-task').addEventListener('click',action(async()=>{if(state.task)await download('java-description-task.zip',taskPath('/api/export'));}));
document.querySelectorAll('[data-stage-file]').forEach(button=>button.addEventListener('click',action(async()=>{if(state.task)await download(button.dataset.stageFile,taskPath('/api/download','&name='+button.dataset.stageFile));})));
$('provider').addEventListener('change',()=>{
  const provider=$('provider').value;
  if(provider==='deepseek'){$('api-base').value='https://api.deepseek.com';$('model-name').value='deepseek-flash';}
  if(provider==='ollama'){$('api-base').value='http://127.0.0.1:11434';$('model-name').value='qwen2.5:7b';}
  if(provider==='compatible'){$('api-base').value='';$('model-name').value='';}
  $('api-key').required=provider!=='ollama';
});
function readProfile(){return {provider:$('provider').value,baseUrl:$('api-base').value.trim(),model:$('model-name').value.trim(),batchSize:Number($('batch-size').value),jsonMode:$('json-mode').checked,proxyMode:$('proxy-mode').value,timeoutSeconds:Number($('timeout-seconds').value)};}
$('test-connection').addEventListener('click',action(async()=>{
  if(state.job)throw new Error('请等待当前处理完成');
  $('test-connection').disabled=true;state.diagnostic=null;$('download-diagnostic').classList.add('hidden');
  try{
    const profile=readProfile();localStorage.setItem('jwb-profile',JSON.stringify(profile));
    const result=await api('/api/test-connection',{profile,apiKey:$('api-key').value});
    const job=await watchJob(result.job);state.diagnostic=job.result;
    $('connection-result').className='status-panel '+(job.result.success?'success':'warning');
    $('connection-result').textContent=job.result.message+' 此测试只查询模型列表，不代表生成一定成功。';
    $('download-diagnostic').classList.remove('hidden');
  }finally{$('test-connection').disabled=false;}
}));
$('download-diagnostic').addEventListener('click',()=>{
  if(!state.diagnostic)return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(state.diagnostic,null,2)],{type:'application/json'}));
  const link=document.createElement('a');link.href=url;link.download='connection-diagnostic.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),5000);
});
$('model-form').addEventListener('submit',action(async event=>{
  event.preventDefault();if(!state.task)throw new Error('请先选择任务');if(state.job)throw new Error('请等待当前生成完成');await flushReview();
  const profile=readProfile();
  localStorage.setItem('jwb-profile',JSON.stringify(profile));
  const scope=$('generation-kind').value;
  const itemIds=scope?state.task.analysis.items.filter(i=>scope==='javadoc'?i.kind==='javadoc':i.kind!=='javadoc').map(i=>i.id):null;
  const result=await api('/api/translate',{task:state.task.id,profile,apiKey:$('api-key').value,itemIds});$('api-key').value='';
  await watchJob(result.job);setStep('review');
}));
$('cancel-job').addEventListener('click',action(async()=>{if(state.job){const data=await api('/api/cancel',{job:state.job});toast(data.message);}}));
for(const id of ['review-search','review-kind','review-status'])$(id).addEventListener(id==='review-search'?'input':'change',()=>{state.page=0;renderReview();});
$('review-items').addEventListener('input',event=>{if(event.target.dataset.text){patchRow(event.target.dataset.text,{text:event.target.value,runId:'manual'});}});
$('review-items').addEventListener('change',event=>{if(event.target.dataset.use){patchRow(event.target.dataset.use,{use:event.target.checked});event.target.closest('.review-item').classList.toggle('adopted',event.target.checked);}});
$('review-items').addEventListener('click',action(async event=>{
  const target=event.target.closest('[data-choose]');if(!target)return;
  const item=state.task.analysis.items.find(i=>i.id===target.dataset.choose);const choice=proposals(item).find(c=>c.runId===target.dataset.run);
  patchRow(item.id,{text:choice.suggestion,runId:choice.runId,use:true});await flushReview();renderReview();
}));
async function adoptScope(itemIds,sourceRun,label,task){
  if(!state.task || state.task.id!==task)throw new Error('任务已切换，请重新选择采用范围');
  $('adopt-visible').disabled=true;$('adopt-all').disabled=true;
  try{
    await flushReview();
    const result=await api('/api/adopt-page',{task,itemIds,sourceRun});
    state.task.review={...result.review,...state.pending};state.preview=null;$('apply-button').disabled=true;
    renderReview();$('save-state').textContent=Object.keys(state.pending).length?'有更改待保存…':'已保存';
    $('batch-result').className='status-panel '+(result.skipped.length?'warning':'success');
    $('batch-result').innerHTML=`<strong>${label}采用 ${result.adopted} 项（新增 ${result.newlyAdopted} 项），跳过 ${result.skipped.length} 项。已保存，尚未写入源码。</strong>`+(result.skipped.length?'<ul>'+result.skipped.map(item=>`<li>${esc(item.target)}：${esc(item.reason)}</li>`).join('')+'</ul>':'');
    toast(`${label}采用 ${result.adopted} 项，跳过 ${result.skipped.length} 项`);
  }finally{$('adopt-visible').disabled=false;$('adopt-all').disabled=false;}
}
$('adopt-visible').addEventListener('click',action(async()=>{
  if(state.task)await adoptScope(state.visible.map(item=>item.id),$('batch-source').value,'本页',state.task.id);
}));
$('adopt-all').addEventListener('click',action(async()=>{
  if(!state.task)return;await flushReview();
  state.adoptAll={task:state.task.id,itemIds:state.task.analysis.items.map(item=>item.id),sourceRun:$('batch-source').value};
  $('adopt-all-description').textContent=`将处理本任务全部 ${state.adoptAll.itemIds.length} 项。批量来源：${$('batch-source').selectedOptions[0].textContent}。`;
  $('adopt-all-dialog').showModal();
}));
$('adopt-all-cancel').addEventListener('click',()=>{$('adopt-all-dialog').close();state.adoptAll=null;});
$('adopt-all-confirm').addEventListener('click',action(async()=>{
  const request=state.adoptAll;$('adopt-all-dialog').close();state.adoptAll=null;
  if(request)await adoptScope(request.itemIds,request.sourceRun,'全任务',request.task);
}));
$('reject-visible').addEventListener('click',action(async()=>{for(const item of state.visible)patchRow(item.id,{use:false});await flushReview();renderReview();}));
$('prev-page').addEventListener('click',action(async()=>{await flushReview();state.page--;renderReview();}));
$('next-page').addEventListener('click',action(async()=>{await flushReview();state.page++;renderReview();}));
$('download-review').addEventListener('click',action(async()=>{if(state.task)await download('review.md',taskPath('/api/download','&name=review.md'));}));
$('review-file').addEventListener('change',action(async()=>{const file=$('review-file').files[0];if(!file || !state.task)return;await flushReview();await api('/api/import-review',{task:state.task.id,content:await file.text()});await loadTask(state.task.id);$('review-file').value='';toast('已导入审核表，缺失的行默认不采用');}));
$('git-check').addEventListener('click',action(async()=>{const status=await api('/api/git-status',{projectRoot:$('apply-root').value.trim()});renderGit(status);state.preview=null;$('apply-button').disabled=true;}));
$('apply-root').addEventListener('input',()=>{state.preview=null;$('apply-button').disabled=true;});
$('preview-button').addEventListener('click',action(async()=>{
  if(!state.task)throw new Error('请先选择任务');await flushReview();
  const preview=await api('/api/preview',{task:state.task.id,projectRoot:$('apply-root').value.trim()});state.preview=preview;renderGit(preview.git);
  $('preview-panel').classList.remove('hidden');$('receipt-panel').classList.add('hidden');
  $('preview-errors').innerHTML=preview.errors.map(e=>`<div class="status-panel warning"><strong>${esc(e.target)}</strong><p>${esc(e.reason)}</p></div>`).join('');
  $('preview-files').innerHTML=preview.files.map(file=>`<details class="diff-card" open><summary>${esc(file.file)} · ${file.ids.length} 项</summary><pre class="diff-view">${diffHtml(file.diff)}</pre></details>`).join('') || '<p class="hint">没有可写入项。先在审核页填写说明并勾选采用。</p>';
  $('apply-button').disabled=Boolean(preview.errors.length || !preview.files.length);
}));
$('download-diff').addEventListener('click',action(()=>download('changes.diff',taskPath('/api/download','&name=changes.diff'))));
$('apply-button').addEventListener('click',action(confirmWrite));
$('confirm-cancel').addEventListener('click',()=>$('confirm-dialog').close());
$('acknowledge-git-warning').addEventListener('change',()=>{$('confirm-apply').disabled=!$('acknowledge-git-warning').checked;});
$('confirm-apply').addEventListener('click',action(async()=>{
  $('confirm-dialog').close();if(!state.preview)throw new Error('请重新预览');$('apply-button').disabled=true;
  const receipt=await api('/api/apply',{task:state.task.id,token:state.preview.token,acknowledgeGitWarning:$('acknowledge-git-warning').checked});state.receipt=receipt;state.preview=null;
  $('git-status').className='status-panel warning';$('git-status').textContent='已写入，本次变更与原文件备份已保存。请查看下方 diff，并自行编译、验证；工具未执行 Git 提交。';
  $('receipt-panel').classList.remove('hidden');$('git-diff').innerHTML=diffHtml(receipt.gitDiff);
  $('git-diff-note').textContent=receipt.gitDiffNote || '';$('tool-diff').innerHTML=diffHtml(receipt.toolDiff || '');
  $('receipt-message').textContent=`已写入 ${receipt.files.length} 个文件。原始文件已备份；尚未提交 Git。语法解析已通过，仍需编译并验证接口文档。`;
  state.task=await api('/api/task?task='+encodeURIComponent(state.task.id));renderReceipts();toast('写入完成，Git diff 已保存');
}));
$('download-git-diff').addEventListener('click',action(()=>{if(state.receipt)return download('git.diff',taskPath('/api/download','&name=git.diff&run='+state.receipt.id));}));
$('download-tool-diff').addEventListener('click',action(()=>{if(state.receipt)return download('tool.diff',taskPath('/api/download','&name=tool.diff&run='+state.receipt.id));}));
$('receipt-list').addEventListener('click',action(async event=>{
  const diff=event.target.closest('[data-history-diff]');if(diff)return download('git.diff',taskPath('/api/download','&name=git.diff&run='+diff.dataset.historyDiff));
  const target=event.target.closest('[data-restore]');if(!target)return;
  if(!window.confirm('仅恢复本工具写入的文件。若有后续编辑、暂存或提交，将拒绝自动恢复。继续？'))return;
  const result=await api('/api/restore',{task:state.task.id,run:target.dataset.restore});await loadTask(state.task.id);toast(`已恢复 ${result.restored} 个文件`);
}));
window.addEventListener('beforeunload',event=>{if(Object.keys(state.pending).length || state.saving){event.preventDefault();event.returnValue='';}});
async function init(){
  for(const section of document.querySelectorAll('.needs-task'))section.classList.add('hidden');
  const info=await refreshTasks();const current=localStorage.getItem('jwb-current-task');
  if(current && info.tasks.some(t=>t.id===current))await loadTask(current);
  try{const profile=JSON.parse(localStorage.getItem('jwb-profile') || 'null');if(profile){$('provider').value=profile.provider;$('api-base').value=profile.baseUrl;$('model-name').value=profile.model;$('batch-size').value=profile.batchSize;$('json-mode').checked=profile.jsonMode;$('proxy-mode').value=profile.proxyMode || 'system';$('timeout-seconds').value=profile.timeoutSeconds || 180;}}catch{}
  if(info.jobs?.length)watchJob(info.jobs[0].id).catch(error=>toast(error.message,true));
}
init().catch(error=>toast(error.message,true));
