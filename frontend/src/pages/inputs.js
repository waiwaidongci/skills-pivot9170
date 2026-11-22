import {ARCHIVE} from '../constants/enums.js';
import {feedback, field} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

const ARCHIVE_COLUMNS = [
  {key:'period',label:'统计月份'}, {key:'channel',label:'招生渠道'}, {key:'program',label:'培训项目'},
  {key:'intake_count',label:'去重受理人数'}, {key:'confirmed_count',label:'确认报名人数'},
];

async function loadProgress(id) {
  try { return await workspace.archiveProgress(id); }
  catch (error) { return null; }
}

function archivePanel(detail, progress, refresh, error) {
  const archived = detail.archived;
  const job = progress;
  const active = job && ['pending', 'reconciling', 'switched', 'purging'].includes(job.status);
  const startBtn = button(archived ? '已归档' : '发起归档', {
    class:'primary', disabled:archived || Boolean(active), 'data-testid':'start-archive'});
  startBtn.addEventListener('click', handler(startBtn, error, async () => {
    const confirmed = window.confirm(
      '归档将在人数核对一致后清除该批次的逐笔受理明细，仅保留月度人数聚合。\n'
      + '原批仍可按任意月份和披露门槛重算同样的月报，但学员级明细将不可恢复。\n\n确认发起归档？');
    if (!confirmed) return;
    await workspace.archive(detail.id);
    await refresh();
  }));
  const retryBtn = job?.status === 'failed' && button('重试归档', {
    class:'quiet', 'data-testid':'retry-archive'});
  if (retryBtn) retryBtn.addEventListener('click', handler(retryBtn, error,
    async () => { await workspace.retryArchive(detail.id); await refresh(); }));
  const statusText = job ? (ARCHIVE[job.status] || job.status)
    : (ARCHIVE[detail.archive_status] || '未归档');
  const rows = [
    ['归档状态', pill(statusText, job?.status || detail.archive_status || 'idle')],
    ['进度说明', detail.archive_note || '—'],
    ['失败原因', job?.error || '—'],
    ['归档时间', detail.archived_at ? date(detail.archived_at) : '—'],
  ];
  return panel('受理批归档',
    p(archived
      ? '逐笔明细已清除，页面仅展示月 × 渠道 × 项目人数聚合；新任务直接读取归档人数，月报口径保持不变。'
      : '到期批可发起归档：核对人数一致后一次性切换，存量任务结束再清除原明细；同内容文件改名重传也无法找回。', 'subtle'),
    el('dl', {class:'dictionary'}, rows.map(([label, value]) => el('div', {}, el('dt', {}, label), el('dd', {}, value)))),
    el('div', {class:'actions'}, startBtn, retryBtn), error);
}

export async function inputs(route, refresh) {
  const {spec,datasets} = workspace;
  if (!session.canWrite) return panel('源批次权限', empty('报名明细由分析员和复核主管管理。业务读者可查看已签发的聚合报表。','#delivery',spec.pages[5]));
  const selected = datasets.find(item => item.id === route.id) || datasets[0];
  const error = feedback();
  const sample = button('下载示例 CSV', {class:'quiet',onClick:event=>handler(sample,error,()=>workspace.download('/template',spec.sample_name))(event)});
  const detail = selected && await workspace.getDataset(selected.id,route.offset);
  const progress = selected ? await loadProgress(selected.id) : null;
  const columns = detail?.archive_mode ? ARCHIVE_COLUMNS : spec.fields.map(field => ({key:field.key,label:field.label}));
  const preview = detail ? panel(detail.archive_mode ? '归档人数（逐笔明细已清除）' : '批次明细', el('div',{class:'actions'},
    el('label',{for:'dataset-id',class:'sr'}, '选择数据批次'),
    el('select',{id:'dataset-id','aria-label':'选择数据批次',onChange:event=>{location.hash=`#inputs/${event.target.value}`;}},
      datasets.map(item=>el('option',{value:item.id,selected:item.id === selected.id},
        item.archived ? `${item.name}（已归档）` : item.name))),
    !detail.archive_mode && link('使用此批次运行',`#run/${detail.id}`,{class:'quiet'}),
    detail.archive_mode && link('用归档人数运行',`#run/${detail.id}`,{class:'quiet'})),
    p(`${detail.filename} · ${detail.row_count} 行 · ${date(detail.created_at)}`,'filename'),
    table(columns,detail.rows,{caption:detail.archive_mode ? '归档人数' : '批次明细',pageSize:16}),
    el('div',{class:'pagination'},
      route.offset > 0 && link('上一批明细',`#inputs/${detail.id}/${Math.max(0,route.offset-16)}`,{class:'quiet'}),
      p(`${detail.rows.length ? route.offset+1 : route.offset}–${route.offset+detail.rows.length} / ${detail.row_count}`),
      route.offset+16 < detail.row_count && link('下一批明细',`#inputs/${detail.id}/${route.offset+16}`,{class:'quiet'})))
    : empty('没有可用的批次。请导入示例或业务 CSV。');
  const submit = button('校验并导入', {type:'submit',class:'primary'});
  const upload = session.canWrite && el('form',{class:'fields',onSubmit:handler(submit,error,async event=>{
    const data = new FormData(event.target);
    const file = data.get('source');
    if (!file?.size) throw new Error('请选择一个 CSV 文件');
    if (file.size > 2_000_000) throw new Error('CSV 最多 2 MB');
    const item = await workspace.upload({name:data.get('batch_name'),filename:file.name,csv_text:await file.text()});
    const target = `#inputs/${item.id}`;
    if (location.hash === target) await refresh();
    else location.hash = target;
  })},field({key:'batch_name',label:'批次名称',type:'string',max_length:120}),
    el('label',{class:'field',for:'source'}, el('span',{},'业务 CSV'),el('input',{id:'source',type:'file',name:'source',accept:'.csv,text/csv',required:true})), submit);
  const archive = selected && archivePanel(detail, progress, refresh, feedback());
  // 归档进行中时自动轮询进度，状态落定后停止并刷新整页。
  if (progress && ['pending', 'reconciling', 'switched', 'purging'].includes(progress.status)) {
    poll(() => workspace.archiveProgress(selected.id),
      item => ['complete', 'failed'].includes(item.status),
      () => location.hash.includes(`#inputs/${selected.id}`))
      .then(item => { if (item) refresh(); });
  }
  return el('div',{class:'two-col'},el('div',{class:'wide-panel'},preview),el('aside',{class:'sidebar-panel'},
    panel('数据导入',p('导入后保留原批次和校验指纹，可用同一份数据复跑。','subtle'),sample,upload,error),
    archive,
    panel('字段说明',el('dl',{class:'dictionary'},spec.fields.map(item=>el('div',{},el('dt',{},item.label),
      el('dd',{},item.key,item.required === false ? ' · 可空' : ' · 必填',item.options ? ` · ${item.options.join(' / ')}` : '')))))));
}
