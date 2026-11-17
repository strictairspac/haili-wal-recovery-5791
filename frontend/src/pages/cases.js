import {store} from '../stores/entities.js';
import {session} from '../stores/session.js';
import {crashCasesAPI} from '../api/entities.js';
import {table} from '../components/common/table.js';
import {esc,formObject} from '../utils/html.js';
import {action} from '../hooks/action.js';

const flushText=c=>{const f=c.page_flushes||{};const parts=Object.entries(f).map(([k,v])=>`${k}@${v}`);return parts.length?esc(parts.join(' · ')):'—';};
const executed=c=>c.executed_through??c.crash_after;
const flushed=c=>c.log_flushed_through??c.crash_after;

export function casesPage(){return `<div class="section-title"><h2>故障注入点</h2><span>执行位置 / 日志落盘位置 / 各键页写回时刻</span></div>${table(['场景','日志集','执行到','日志落盘到','页写回'],store.cases.map(c=>[esc(c.name),esc(store.logs.find(l=>l.id===c.log_set_id)?.name),executed(c),flushed(c),flushText(c)]))}<section class="split"><div><h2>选择故障发生时刻</h2><p>执行位置N表示崩溃前执行了前N条日志；日志落盘位置M表示只有前M条日志真正落盘，M以后执行的日志与提交在崩溃后不可见。页写回用JSON给出每个键在哪条日志执行后落盘，例如 <code>{"sample_B": 10}</code>；0表示从未写回。</p><p>写回位置或已执行的检查点若越过日志落盘位置，场景保存时即拒绝。保存时会冻结对应的合法页映像。</p><p class="big-note">日志先落盘，页面才能写回。</p></div>${session.role!=='viewer'?`<form id="case-form"><label>场景名称<input name="name" value="晚班未提交页写回" required></label><label>日志集<select name="log_set_id">${store.logs.map(l=>`<option value="${l.id}">${esc(l.name)} · ${l.records.length}条</option>`).join('')}</select></label><div class="fields"><label>执行位置<input name="executed_through" type="number" min="0" max="200" value="10" required></label><label>日志落盘位置<input name="log_flushed_through" type="number" min="0" max="200" value="10" required></label></div><label>页写回时刻 JSON（键 → LSN）<textarea name="page_flushes" rows="3">{"sample_B": 10}</textarea></label><button type="submit">保存故障场景</button></form>`:'<p class="readonly">观察员可复核故障场景。</p>'}</section>`;}
export function bindCases(render){action(document.querySelector('#case-form'),async f=>{const d=formObject(f);const payload={name:d.name,log_set_id:Number(d.log_set_id),executed_through:Number(d.executed_through)};payload.log_flushed_through=d.log_flushed_through===''?undefined:Number(d.log_flushed_through);const raw=(d.page_flushes||'').trim();payload.page_flushes=raw?JSON.parse(raw):{};await crashCasesAPI.create(payload);await render();});}
