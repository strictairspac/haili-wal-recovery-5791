import {table} from '../components/common/table.js';
import {badge} from '../components/common/status.js';
import {esc} from '../utils/html.js';
import {download} from '../api/client.js';
import {message} from '../hooks/action.js';

const phaseName={redo:'重做',redo_skipped:'跳过重做',redo_abort_undo:'重做撤销',redo_abort_skipped:'跳过撤销',undo_loser:'撤销未提交',undo_noop:'撤销无变化',lost:'未落盘丢失'};

function evidence(r){
 if(r.error)return `<section class="verdict"><div><p>恢复账本核对</p><h2>恢复失败</h2></div><p class="notice error">${esc(r.error)}</p></section>`;
 const pages=table(['账本键','初始值','页LSN','恢复前页值','恢复后页值','重做','跳过重做','撤销','跳过原因'],(r.pages||[]).map(p=>[esc(p.key),p.initial,p.page_lsn,p.before_recovery,p.after_recovery,p.redo_applied,p.redo_skipped,p.undo_applied,esc((p.skip_reasons||[]).join('；')||'—')]));
 const ledger=table(['账本键','检查点值','重做后','恢复后','独立提交结果'],Object.keys(r.recovered||{}).map(k=>[esc(k),r.durable_checkpoint[k],r.after_redo[k],r.recovered[k],r.oracle[k]]));
 const trace=table(['LSN','事务','阶段','键','before → after','原因'],(r.trace||[]).map(t=>{const reverse=t.phase==='undo_loser'||t.phase==='redo_abort_undo'||t.phase==='redo_abort_skipped'||t.phase==='undo_noop';const flow=t.before==null&&t.after==null?'—':reverse?`${t.after} → ${t.before}`:`${t.before} → ${t.after}`;return [t.lsn,esc(t.tx),esc(phaseName[t.phase]||t.phase),esc(t.key),flow,esc(t.reason||'')];}));
 return `<section class="verdict"><div><p>恢复账本核对</p><h2>${r.invariant_passed?'提交结果一致':'需核对'}</h2></div><p>执行到 LSN ${r.executed_through??r.crash_after??'—'} · 日志落盘到 LSN ${r.log_flushed_through??'—'} · 检查点LSN ${r.checkpoint_lsn??'—'}<br>重做 ${r.redo_count??0} 次 / 跳过重做 ${r.redo_skipped_count??0} 次 / 撤销 ${r.undo_count??0} 次（实际写入 ${r.undo_applied_count??0}）<br>未提交事务 ${esc((r.losers||[]).join(', ')||'无')} · 未落盘日志 ${(r.lost_records||[]).length} 条</p></section><h2>数据页恢复前后</h2>${pages}<h2>账本对照</h2>${ledger}<h2>日志恢复轨迹</h2>${trace}<p class="checksum">输入摘要 ${esc(r.input_sha256||'无')}<br>结果摘要 ${esc(r.result_sha256||'无')}</p>`;
}

export function detailPage(job){if(!job)return '<section class="empty"><h2>还没有恢复记录</h2><p>先选择故障场景并提交恢复任务，再逐条查看日志重做与撤销。</p><a href="#runs">进入恢复队列 →</a></section>';const r=job.result;return `<div class="section-title"><h2>${esc(job.label)} <small>#${job.id}</small></h2>${badge(job.status)}</div>${r?evidence(r):'<p class="notice">后台恢复计算中，请刷新状态。</p>'}<div class="toolbar"><button id="export">导出日志与恢复证据</button><button class="secondary" id="refresh">刷新状态</button></div><details><summary>查看冻结输入</summary><pre>${esc(JSON.stringify(job.snapshot,null,2))}</pre></details>`;}
export function bindDetail(job,render){document.querySelector('#refresh')?.addEventListener('click',render);document.querySelector('#export')?.addEventListener('click',()=>download(job.id).catch(e=>message(e.message,true)));}
