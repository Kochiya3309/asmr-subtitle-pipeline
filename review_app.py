# SPDX-License-Identifier: GPL-3.0-or-later
"""Loopback-only browser UI for bilingual ASR evidence review."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import webbrowser

from human_review import (
    build_review_bundle,
    migrate_legacy_review_submission,
    validate_completed_review_outputs,
    write_review_outputs,
)
from pipeline_inputs import discover_active_audio


def _write_audio_chunks(output, handle, length: int) -> None:
    remaining = length
    while remaining:
        chunk = handle.read(min(64 * 1024, remaining))
        if not chunk:
            break
        try:
            output.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            break
        remaining -= len(chunk)


MAX_REQUEST_BYTES = 4 * 1024 * 1024
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    match = _RANGE_RE.fullmatch(value.strip())
    if not match or size <= 0:
        raise ValueError("invalid Range header")
    left, right = match.groups()
    if not left and not right:
        raise ValueError("empty Range header")
    if left:
        start = int(left)
        end = int(right) if right else size - 1
    else:
        suffix = int(right)
        if suffix <= 0:
            raise ValueError("invalid suffix range")
        start = max(0, size - suffix)
        end = size - 1
    if start < 0 or end < start or start >= size:
        raise ValueError("unsatisfiable Range header")
    return start, min(end, size - 1)


_INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ASMR 字幕人工复核</title>
<style nonce="__NONCE__">
:root{color-scheme:dark;--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#e6edf3;--muted:#9da7b3;--blue:#58a6ff;--green:#3fb950;--amber:#d29922;--red:#f85149;--mark:#9e3f18}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,"Microsoft YaHei",sans-serif}button,input,textarea{font:inherit}summary{cursor:pointer;padding:12px 16px;color:var(--muted)}
header{position:sticky;top:0;z-index:5;padding:14px max(18px,calc((100% - 1240px)/2));background:rgba(13,17,23,.97);border-bottom:1px solid var(--line)}h1{margin:0;font-size:21px}header p{margin:3px 0;color:var(--muted)}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px}progress{width:240px;accent-color:var(--green)}
main{max-width:1240px;margin:auto;padding:20px}.notice{padding:12px 14px;border:1px solid #1f6feb;background:#0d2440;border-radius:10px}.warning{border-color:var(--amber);background:#2b210c}.hidden{display:none!important}
#playerBar{position:sticky;top:103px;z-index:4;margin:14px 0;padding:10px 13px;border:1px solid var(--line);border-radius:10px;background:rgba(22,27,34,.97)}audio{width:100%;height:38px}#nowPlaying{color:var(--muted);margin-top:3px}
.card{margin:15px 0;border:1px solid var(--line);background:var(--panel);border-radius:12px;overflow:hidden}.card.done{border-color:#238636}.card.excluded{border-color:#8b5e13}.head{display:flex;justify-content:space-between;gap:12px;padding:13px 16px;border-bottom:1px solid var(--line)}h2{font-size:17px;margin:0}.time{color:var(--muted);white-space:nowrap}.time.changed{color:#e3b341}.body{padding:14px 16px 17px}.flags{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 10px}.flag{padding:2px 7px;border:1px solid #8b5e13;border-radius:999px;color:#e3b341;font-size:12px}.flag.timing{border-color:#1f6feb;color:#79c0ff}.flag.text{border-color:#8b5e13;color:#e3b341}.flag.info{border-color:#30363d;color:var(--muted)}
.context{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:10px 0}.contextBox,.current,.candidate{border:1px solid var(--line);border-radius:9px;padding:10px}.contextBox{color:var(--muted)}.label{font-size:12px;color:var(--muted);margin-top:6px;text-transform:uppercase}.jp{font-size:16px}.zh,.romaji{color:#c9d1d9}.models{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:9px;margin:10px 0}.candidate strong{color:var(--blue)}.source{font-size:12px;color:var(--muted)}mark.diff{background:var(--mark);color:#fff;padding:1px 2px;border-radius:3px}
.play,.smallButton{border:0;border-radius:7px;padding:8px 12px;color:white;background:#1f6feb;font-weight:650;cursor:pointer}.smallButton{padding:5px 8px;margin:3px 5px 3px 0;background:#30363d}.review{margin-top:12px;padding-top:11px;border-top:1px solid var(--line)}fieldset{border:0;padding:0;margin:0 0 12px}legend{font-weight:650}.choices{display:flex;gap:7px;flex-wrap:wrap}.choices label{border:1px solid var(--line);border-radius:999px;padding:5px 9px;cursor:pointer}.choices label.selected{border-color:var(--green);background:#12351e}.choices label.disabled{opacity:.45;cursor:not-allowed}.evidenceChecks{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}.editGrid,.timeGrid,.splitGrid{display:grid;grid-template-columns:1fr 1fr;gap:9px}.timeEditor,.structureEditor{border:1px solid var(--line);border-radius:9px;padding:10px;margin:8px 0}.timeEditor input[type=text],.structureEditor input[type=text]{width:100%;color:var(--text);background:#0d1117;border:1px solid var(--line);border-radius:7px;padding:7px}.timeButtons{margin-top:6px}.timingWarning{padding:7px 9px;margin-top:7px;border:1px solid var(--amber);border-radius:7px;background:#2b210c;color:#e3b341}.timingWarning.danger{border-color:var(--red);background:#341417;color:#ff7b72}textarea{width:100%;min-height:62px;color:var(--text);background:#0d1117;border:1px solid var(--line);border-radius:7px;padding:8px;resize:vertical}.note{margin-top:9px}
.actions{position:sticky;bottom:0;z-index:6;display:flex;gap:9px;align-items:center;flex-wrap:wrap;padding:12px;background:rgba(22,27,34,.97);border:1px solid var(--line);border-radius:11px}.actions button{border:0;border-radius:7px;padding:9px 13px;color:#fff;background:#238636;font-weight:650;cursor:pointer}.actions button.secondary{background:#30363d}#message{color:var(--muted)}
@media(max-width:760px){.head{display:block}.time{display:block;margin-top:3px}.context,.editGrid{grid-template-columns:1fr}#playerBar{top:126px}}
</style></head><body>
<header><h1>ASMR 字幕人工复核</h1><p id="subtitle">正在读取审核数据……</p><div class="toolbar"><progress id="progress" max="1" value="0"></progress><span id="progressText">0 / 0</span><label><input id="showAll" type="checkbox">显示无异常字幕和已自动隔离内容</label></div></header>
<main><div class="notice">页面只展示本机数据，不加载外部脚本。异常片段由你手动播放/暂停，不会循环；前后文仅用于判断，不会被连写进当前字幕。</div><div id="romajiWarning" class="notice warning hidden">当前未安装罗马音生成组件；日文仍可审核，但罗马音栏暂缺。</div>
<div id="playerBar"><audio id="player" controls preload="metadata" src="/media/audio"></audio><div id="nowPlaying">尚未选择片段</div></div>
<div id="cards"></div>
<div class="actions"><button id="submit">提交审核并生成双语 SRT</button><button id="download" class="secondary">下载审核 JSON</button><button id="clear" class="secondary">清除本地草稿</button><span id="message">审核草稿仅保存在本浏览器 localStorage；可能含敏感文本。</span></div></main>
<script nonce="__NONCE__">
"use strict";
const SESSION_TOKEN=__TOKEN_JSON__;let bundle=null,draft={},stopAt=null;
const $=id=>document.getElementById(id),cardsEl=$("cards"),player=$("player"),message=$("message");
const itemMap=()=>new Map(bundle.items.map(x=>[x.id,x]));
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}
function fmt(ms){const s=Math.floor(ms/1000),m=Math.floor(s/60)%60,h=Math.floor(s/3600);return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s%60).padStart(2,"0")}.${String(ms%1000).padStart(3,"0")}`}
function parseMs(value){const text=String(value).trim();let match=text.match(/^(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?$/);if(match){const [,hours="0",minutes,seconds,fraction="0"]=match;if(+minutes>=60||+seconds>=60)throw new Error("时间的分或秒不能超过 59");return ((+hours*60+ +minutes)*60+ +seconds)*1000+ +(fraction+"00").slice(0,3)}if(/^\d+(?:\.\d{1,3})?$/.test(text))return Math.round(+text*1000);throw new Error("时间格式应为 HH:MM:SS.mmm")}
const FLAG_LABELS={uncertain:"文字证据不确定",uncertain_asr_evidence:"ASR 证据不确定",model_disagreement:"模型文字不同",model_disagreement_resolved:"模型文字不同，但当前字幕已有候选支持",overlap_partitioned:"证据时间窗经过重叠分区",review_only_rescue_suppression:"已自动隔离的救援候选",low_confidence:"低置信度标记",low_text_similarity:"台本与识别文本相似度低",low_script_classification_confidence:"台本分类置信度低",short_text_match:"短文本匹配",alternative_path_ambiguous:"存在多个对齐方案",repeated_text_ambiguous:"重复台词位置不确定",script_alignment_unmatched:"台本中无对应内容",timeline_requires_review:"时间轴需要确认",shared_timeline_window:"多句共用时间窗口",group_level_timing_only:"只能确定整组时间",overlap:"证据重叠且字幕边界存在小间隙",boundary_disagreement:"文本相符的候选与当前字幕边界差超过 1.5 秒"};
Object.assign(FLAG_LABELS,{long_cue_alignment:"异常长字幕已局部对齐",long_cue_alignment_review:"局部对齐结果需要确认"});
function diffMask(base,text){if(!text)return[];if(!base||base.length>500||text.length>500)return text.split("").map(()=>true);const a=[...base],b=[...text],dp=Array.from({length:a.length+1},()=>new Uint16Array(b.length+1));for(let i=a.length-1;i>=0;i--)for(let j=b.length-1;j>=0;j--)dp[i][j]=a[i]===b[j]?dp[i+1][j+1]+1:Math.max(dp[i+1][j],dp[i][j+1]);const same=new Set;let i=0,j=0;while(i<a.length&&j<b.length){if(a[i]===b[j]){same.add(j);i++;j++}else if(dp[i+1][j]>=dp[i][j+1])i++;else j++}return b.map((_,k)=>!same.has(k))}
function appendDiff(node,base,text){const chars=[...(text||"")],mask=diffMask(base,text);let run="",marked=null;function flush(){if(!run)return;const n=el(marked?"mark":"span",marked?"diff":"",run);node.appendChild(n);run=""}chars.forEach((ch,i)=>{if(marked!==mask[i]){flush();marked=mask[i]}run+=ch});flush();if(!chars.length)node.textContent="（空）"}
function addTextBlock(parent,label,text,cls=""){parent.appendChild(el("div","label",label));parent.appendChild(el("div",cls,text||"（无）"))}
function contextBox(id,label){const box=el("div","contextBox"),item=itemMap().get(id);box.appendChild(el("strong","",label));if(!item){box.appendChild(el("div","","（无）"));return box}addTextBlock(box,"日文",item.texts.ja,"jp");addTextBlock(box,"中文",item.texts.zh,"zh");return box}
function candidateCard(row,baselineJa,baselineRoma){const box=el("div","candidate");box.appendChild(el("strong","",`${row.model_role||row.model||"ASR"}`));box.appendChild(el("div","source",`${row.channel}/${row.view} · ${row.evidence_id}`));box.appendChild(el("div","source",`时间：${fmt(row.start_ms)}–${fmt(row.end_ms)}`));box.appendChild(el("div","label","日文差异"));const jp=el("div","jp");appendDiff(jp,baselineJa,row.text_ja);box.appendChild(jp);box.appendChild(el("div","label","罗马音差异"));const ro=el("div","romaji");if(row.romaji)appendDiff(ro,baselineRoma,row.romaji);else ro.textContent="（未生成）";box.appendChild(ro);const c=row.confidence||{};box.appendChild(el("div","source",`avg_logprob=${c.avg_logprob??"?"} · no_speech=${c.no_speech_prob??"?"}`));return box}
function dimensions(item){const value=item.review_dimensions;return value&&typeof value==="object"?{text_required:!!value.text_required,timing_required:!!value.timing_required}:{text_required:item.kind!=="subtitle"||(item.flags||[]).length>0,timing_required:false}}
function isFlagged(item){const d=dimensions(item);return d.text_required||d.timing_required}
function defaultReview(item){const d=dimensions(item),fallback=item.kind==="subtitle"?"keep":item.default_action||"";return{action:d.text_required?"":fallback,selected_evidence_ids:[],edited_ja:item.texts.ja||bundle.evidence[item.evidence_ids[0]]?.text_ja||"",edited_zh:item.texts.zh||"",note:"",timing_action:d.timing_required?"":"keep",edited_start_ms:item.start_ms,edited_end_ms:item.end_ms,link_next_start:false,structure_action:"keep",split_at_ms:Math.round((item.start_ms+item.end_ms)/2),split_ja_before:"",split_ja_after:"",split_zh_before:"",split_zh_after:""}}
function reviewFor(item){return draft[item.id]||(draft[item.id]=defaultReview(item))}
function subtitleNeighbor(item,offset){const subtitles=bundle.items.filter(x=>x.kind==="subtitle"),index=subtitles.findIndex(x=>x.id===item.id);return subtitles[index+offset]||null}
function effectiveTimes(item){const review=reviewFor(item);return review.timing_action==="edit"?{start_ms:review.edited_start_ms,end_ms:review.edited_end_ms}:{start_ms:item.start_ms,end_ms:item.end_ms}}
function setStart(item,value){const review=reviewFor(item);review.timing_action="edit";review.edited_start_ms=Math.round(value);if(!Number.isInteger(review.edited_end_ms))review.edited_end_ms=item.end_ms;saveDraft()}
function setEnd(item,value){const review=reviewFor(item);review.timing_action="edit";review.edited_end_ms=Math.round(value);if(!Number.isInteger(review.edited_start_ms))review.edited_start_ms=item.start_ms;if(review.link_next_start){const next=subtitleNeighbor(item,1);if(next){const nextReview=reviewFor(next);nextReview.timing_action="edit";nextReview.edited_start_ms=Math.round(value);if(!Number.isInteger(nextReview.edited_end_ms))nextReview.edited_end_ms=next.end_ms}}saveDraft()}
function timingIssues(item){const current=effectiveTimes(item),issues=[];if(current.start_ms<0)issues.push(["起点不能小于 0",true]);if(current.start_ms>=current.end_ms)issues.push(["起点必须早于终点",true]);if(bundle.audio.duration_ms&&current.end_ms>bundle.audio.duration_ms)issues.push(["终点超过音频时长",true]);const prev=subtitleNeighbor(item,-1),next=subtitleNeighbor(item,1);if(prev){const overlap=effectiveTimes(prev).end_ms-current.start_ms;if(overlap>0)issues.push([`与上一条重叠 ${(overlap/1000).toFixed(3)} 秒`,overlap>2000])}if(next){const overlap=current.end_ms-effectiveTimes(next).start_ms;if(overlap>0)issues.push([`与下一条重叠 ${(overlap/1000).toFixed(3)} 秒`,overlap>2000])}return issues}
async function playItem(item){const current=effectiveTimes(item),end=current.end_ms/1000;if(!player.paused&&stopAt===end){player.pause();return}player.currentTime=current.start_ms/1000;stopAt=end;$("nowPlaying").textContent=`${item.id}｜${fmt(current.start_ms)}–${fmt(current.end_ms)}`;try{await player.play()}catch(e){message.textContent=`播放失败：${e.message}`}}
function renderReview(card,item){const review=reviewFor(item),wrap=el("div","review"),d=dimensions(item),fs=el("fieldset"),legend=el("legend","",d.text_required||item.kind!=="subtitle"?"文字审核（必须判断）":"文字审核（无自动异常，默认保留）"),choices=el("div","choices");fs.append(legend,choices);const options=item.kind==="subtitle"?[["keep","保留当前字幕"],["edit","修改字幕"],["reject","删除该字幕"]]:[["accept","确认遗漏并补入"],["edit","修改后补入"],["reject","确认为幻觉/无词汇"]];for(const [value,label] of options){const lab=el("label",review.action===value?"selected":""),input=document.createElement("input");input.type="radio";input.name=`action-${item.id}`;input.value=value;input.checked=review.action===value;input.addEventListener("change",()=>{review.action=value;if(value==="reject")review.timing_action="keep";saveDraft();render()});lab.append(input,document.createTextNode(label));choices.appendChild(lab)}wrap.appendChild(fs);if(item.evidence_ids.length){wrap.appendChild(el("div","label","采用的证据（可多选）"));const checks=el("div","evidenceChecks");for(const id of item.evidence_ids){const lab=el("label"),input=document.createElement("input");input.type="checkbox";input.checked=review.selected_evidence_ids.includes(id);input.addEventListener("change",()=>{review.selected_evidence_ids=input.checked?[...new Set([...review.selected_evidence_ids,id])]:review.selected_evidence_ids.filter(x=>x!==id);saveDraft()});lab.append(input,document.createTextNode(bundle.evidence[id]?.model_role||id));checks.appendChild(lab)}wrap.appendChild(checks)}const grid=el("div","editGrid"),ja=document.createElement("textarea"),zh=document.createElement("textarea");ja.value=review.edited_ja;zh.value=review.edited_zh;ja.placeholder="修正后的完整日文（选择修改/补入时必填）";zh.placeholder="修正后的完整中文（选择修改/补入时必填）";ja.addEventListener("input",()=>{review.edited_ja=ja.value;saveDraft()});zh.addEventListener("input",()=>{review.edited_zh=zh.value;saveDraft()});grid.append(ja,zh);wrap.appendChild(grid);const timing=el("fieldset","timeEditor"),timingLegend=el("legend","",d.timing_required?"时间轴审核（必须确认）":"时间轴审核（可选调整）"),timingChoices=el("div","choices");timing.append(timingLegend,timingChoices);for(const [value,label] of [["keep","确认当前时间"],["edit","使用调整后的时间"]]){const lab=el("label",review.timing_action===value?"selected":""),input=document.createElement("input");input.type="radio";input.name=`timing-${item.id}`;input.value=value;input.checked=review.timing_action===value;input.addEventListener("change",()=>{review.timing_action=value;if(value==="edit"){if(!Number.isInteger(review.edited_start_ms))review.edited_start_ms=item.start_ms;if(!Number.isInteger(review.edited_end_ms))review.edited_end_ms=item.end_ms}saveDraft();render()});lab.append(input,document.createTextNode(label));timingChoices.appendChild(lab)}const timeGrid=el("div","timeGrid");for(const [kind,label] of [["start","起始时间"],["end","结束时间"]]){const cell=el("div",""),input=document.createElement("input");input.type="text";input.value=fmt(kind==="start"?review.edited_start_ms:review.edited_end_ms);input.addEventListener("change",()=>{try{const value=parseMs(input.value);if(kind==="start")setStart(item,value);else setEnd(item,value);render()}catch(e){message.textContent=e.message;input.value=fmt(kind==="start"?review.edited_start_ms:review.edited_end_ms)}});cell.append(el("div","label",label),input);const button=el("button","smallButton",`用当前播放位置设为${kind==="start"?"起点":"终点"}`);button.type="button";button.addEventListener("click",()=>{if(kind==="start")setStart(item,player.currentTime*1000);else setEnd(item,player.currentTime*1000);render()});cell.appendChild(button);timeGrid.appendChild(cell)}timing.appendChild(timeGrid);const modelButtons=el("div","timeButtons");for(const id of item.evidence_ids){const row=bundle.evidence[id];if(!row)continue;const role=row.model_role||row.model||id;for(const [kind,label,value] of [["start","起点",row.start_ms],["end","终点",row.end_ms]]){const button=el("button","smallButton",`使用 ${role} ${label} ${fmt(value)}`);button.type="button";button.addEventListener("click",()=>{if(kind==="start")setStart(item,value);else setEnd(item,value);render()});modelButtons.appendChild(button)}}timing.appendChild(modelButtons);const linkLabel=el("label",""),link=document.createElement("input");link.type="checkbox";link.checked=!!review.link_next_start;link.addEventListener("change",()=>{review.link_next_start=link.checked;saveDraft()});linkLabel.append(link,document.createTextNode(" 修改终点时同步下一条字幕起点"));timing.appendChild(linkLabel);for(const [text,danger] of timingIssues(item))timing.appendChild(el("div",`timingWarning ${danger?"danger":""}`,text));wrap.appendChild(timing);const note=document.createElement("textarea");note.className="note";note.placeholder="备注：听感、说话者、犹豫点、时间轴依据等";note.value=review.note;note.addEventListener("input",()=>{review.note=note.value;saveDraft()});wrap.appendChild(note);card.querySelector(".body").appendChild(wrap)}
const baseRenderReview=renderReview;renderReview=function(card,item){baseRenderReview(card,item);if(!dimensions(item).text_required){const legend=card.querySelector(".review fieldset legend");if(legend)legend.textContent="文字审核（可选，已有安全默认值）"}}
function seedSplit(review){if(review.split_ja_before||review.split_ja_after)return;for(const [source,before,after] of [[review.edited_ja,"split_ja_before","split_ja_after"],[review.edited_zh,"split_zh_before","split_zh_after"]]){const middle=Math.floor(source.length/2),offset=source.slice(middle).search(/[。！？!?，,、]/),cut=offset>=0?middle+offset+1:middle;review[before]=source.slice(0,cut);review[after]=source.slice(cut)}}
function renderStructureEditor(card,item){if(item.kind!=="subtitle")return;const review=reviewFor(item),wrap=card.querySelector(".review"),field=el("fieldset","structureEditor"),choices=el("div","choices"),next=subtitleNeighbor(item,1);field.append(el("legend","","字幕结构（可选）"),choices);for(const [value,label] of [["keep","保持一条"],["split","拆成两条"],["merge_next","与下一条合并"]]){const disabled=value==="merge_next"&&!next,lab=el("label",`${review.structure_action===value?"selected":""} ${disabled?"disabled":""}`.trim()),input=document.createElement("input");input.type="radio";input.name=`structure-${item.id}`;input.value=value;input.checked=review.structure_action===value;input.disabled=disabled;input.addEventListener("change",()=>{review.structure_action=value;if(value==="split"){const current=effectiveTimes(item);review.split_at_ms=Math.round((current.start_ms+current.end_ms)/2);seedSplit(review)}saveDraft();render()});lab.append(input,document.createTextNode(label));choices.appendChild(lab)}if(review.structure_action==="split"){const row=el("div","timeGrid"),cell=el("div",""),input=document.createElement("input");input.type="text";input.value=fmt(review.split_at_ms);input.addEventListener("change",()=>{try{review.split_at_ms=parseMs(input.value);saveDraft()}catch(e){message.textContent=e.message;input.value=fmt(review.split_at_ms)}});cell.append(el("div","label","拆分时间"),input);const button=el("button","smallButton","用当前播放位置设为拆分点");button.type="button";button.addEventListener("click",()=>{review.split_at_ms=Math.round(player.currentTime*1000);saveDraft();render()});cell.appendChild(button);row.appendChild(cell);field.appendChild(row);const grid=el("div","splitGrid");for(const [key,label] of [["split_ja_before","前半日文"],["split_ja_after","后半日文"],["split_zh_before","前半中文"],["split_zh_after","后半中文"]]){const cell=el("div",""),area=document.createElement("textarea");area.value=review[key];area.placeholder=label;area.addEventListener("input",()=>{review[key]=area.value;saveDraft()});cell.append(el("div","label",label),area);grid.appendChild(cell)}field.appendChild(grid)}else if(review.structure_action==="merge_next"){field.appendChild(el("div","timingWarning",next?`将与 ${next.id} 合并；输出时间覆盖两条字幕，双语文本按顺序连接。`:"当前已经是最后一条字幕，不能合并。"))}wrap.insertBefore(field,wrap.querySelector(".note"))}
const structureRenderReview=renderReview;renderReview=function(card,item){structureRenderReview(card,item);renderStructureEditor(card,item)};
function itemDone(item){const review=reviewFor(item),d=dimensions(item),textDone=!d.text_required||!!review.action,timingDone=review.action==="reject"||!d.timing_required||!!review.timing_action;return textDone&&timingDone}
function renderCard(item){const review=reviewFor(item),current=effectiveTimes(item),changed=review.timing_action==="edit"&&(current.start_ms!==item.start_ms||current.end_ms!==item.end_ms),card=el("section",`card ${item.kind==="excluded_evidence"?"excluded":""} ${itemDone(item)?"done":""}`);card.dataset.id=item.id;const head=el("div","head"),title=el("h2","",`${item.id}｜${item.kind==="subtitle"?"现有字幕":"被隔离候选"}`),time=el("span",`time ${changed?"changed":""}`,`${fmt(current.start_ms)}–${fmt(current.end_ms)}${changed?`（原 ${fmt(item.start_ms)}–${fmt(item.end_ms)}）`:""}`);head.append(title,time);const body=el("div","body"),flags=el("div","flags");for(const flag of item.text_flags||[])flags.appendChild(el("span","flag text",`文字：${FLAG_LABELS[flag]||flag}`));for(const flag of item.timing_flags||[])flags.appendChild(el("span","flag timing",`时间：${FLAG_LABELS[flag]||flag}`));if(!(item.text_flags||item.timing_flags))for(const flag of item.flags||[])flags.appendChild(el("span","flag",FLAG_LABELS[flag]||flag));body.appendChild(flags);const play=el("button","play","播放当前时间段");play.type="button";play.addEventListener("click",()=>playItem(item));body.appendChild(play);const ctx=el("div","context");ctx.append(contextBox(item.context.prev_id,"上一条上下文"),contextBox(item.context.next_id,"下一条上下文"));body.appendChild(ctx);const currentText=el("div","current");addTextBlock(currentText,"当前日文",item.texts.ja||"（被隔离，尚未写入字幕）","jp");addTextBlock(currentText,"当前罗马音",item.texts.romaji||"（未生成）","romaji");addTextBlock(currentText,"当前中文",item.texts.zh||"（尚无翻译）","zh");body.appendChild(currentText);const models=el("div","models"),rows=item.evidence_ids.map(id=>bundle.evidence[id]).filter(Boolean);for(const row of rows)models.appendChild(candidateCard(row,item.texts.ja,item.texts.romaji));if(!rows.length)models.appendChild(el("div","candidate","该条没有结构化 ASR 候选（旧缓存或时间轴未启用）。"));body.appendChild(models);card.append(head,body);renderReview(card,item);return card}
const baseRenderCard=renderCard;renderCard=function(item){const card=baseRenderCard(item),flags=card.querySelector(".flags");for(const flag of item.informational_flags||[])flags.appendChild(el("span","flag info",`信息：${FLAG_LABELS[flag]||flag}`));if(item.review_policy==="optional_suppressed"){const title=card.querySelector("h2");if(title)title.textContent=`${item.id}｜自动隔离候选（可选复核）`}return card}
function suppressedGroupKey(item){const evidence=bundle.evidence[item.evidence_ids[0]]||{};return `${item.text_flags.join("|")}\u0000${evidence.text_ja||item.id}`}
function renderSuppressedGroup(group){const first=group[0],evidence=bundle.evidence[first.evidence_ids[0]]||{},card=el("section","card excluded"),head=el("div","head"),title=el("h2","",`自动隔离组｜${evidence.text_ja||"无文本"} × ${group.length}`),details=document.createElement("details"),summary=document.createElement("summary"),inside=el("div","body");summary.textContent="展开候选明细并按需恢复";for(const item of group)inside.appendChild(renderCard(item));details.append(summary,inside);head.appendChild(title);card.append(head,details);return card}
function render(){cardsEl.replaceChildren();const showAll=$("showAll").checked,groups=new Map;for(const item of bundle.items){if(item.review_policy==="optional_suppressed"){if(showAll){const key=suppressedGroupKey(item);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(item)}continue}if(showAll||isFlagged(item))cardsEl.appendChild(renderCard(item))}for(const group of groups.values())cardsEl.appendChild(renderSuppressedGroup(group));updateProgress()}
function updateProgress(){let total=0,done=0;for(const item of bundle.items){const review=reviewFor(item),d=dimensions(item);if(d.text_required){total++;if(review.action)done++}if(d.timing_required&&review.action!=="reject"){total++;if(review.timing_action)done++}}$("progress").max=Math.max(1,total);$("progress").value=done;$("progressText").textContent=`${done} / ${total} 个审核维度已完成`}
function storageKey(){return `asmr-human-review-${bundle.bundle_id}`}
function cleanDraft(raw){const out={};if(!raw||typeof raw!=="object"||Array.isArray(raw))return out;for(const item of bundle.items){const value=raw[item.id];if(!value||typeof value!=="object"||Array.isArray(value))continue;const base=defaultReview(item),allowed=item.kind==="subtitle"?["keep","edit","reject"]:["accept","edit","reject"],action=allowed.includes(value.action)?value.action:base.action,timingAction=["keep","edit"].includes(value.timing_action)?value.timing_action:base.timing_action;out[item.id]={action,selected_evidence_ids:Array.isArray(value.selected_evidence_ids)?[...new Set(value.selected_evidence_ids.filter(x=>typeof x==="string"&&item.evidence_ids.includes(x)))]:[],edited_ja:typeof value.edited_ja==="string"?value.edited_ja.slice(0,20000):base.edited_ja,edited_zh:typeof value.edited_zh==="string"?value.edited_zh.slice(0,20000):base.edited_zh,note:typeof value.note==="string"?value.note.slice(0,20000):"",timing_action:timingAction,edited_start_ms:Number.isInteger(value.edited_start_ms)?value.edited_start_ms:item.start_ms,edited_end_ms:Number.isInteger(value.edited_end_ms)?value.edited_end_ms:item.end_ms,link_next_start:!!value.link_next_start}}return out}
function loadDraft(){try{const prior=cleanDraft(bundle.prior_review||{}),local=cleanDraft(JSON.parse(localStorage.getItem(storageKey())||"{}"));draft={...prior,...local}}catch{draft=cleanDraft(bundle.prior_review||{});message.textContent="本地草稿损坏，已忽略并载入上次提交结果。"}}
function saveDraft(){try{localStorage.setItem(storageKey(),JSON.stringify(draft));updateProgress()}catch(e){message.textContent=`草稿未保存：${e.message}`}}
function collect(){const items={};for(const item of bundle.items){const r=reviewFor(item),d=dimensions(item);if((item.kind!=="subtitle"||d.text_required)&&!r.action)throw new Error(`请先完成 ${item.id} 的文字审核`);if(d.timing_required&&r.action!=="reject"&&!r.timing_action)throw new Error(`请先完成 ${item.id} 的时间轴审核`);if(r.timing_action==="edit"){if(!Number.isInteger(r.edited_start_ms)||!Number.isInteger(r.edited_end_ms)||r.edited_start_ms<0||r.edited_start_ms>=r.edited_end_ms)throw new Error(`${item.id} 的时间轴无效`);if(bundle.audio.duration_ms&&r.edited_end_ms>bundle.audio.duration_ms)throw new Error(`${item.id} 的终点超过音频时长`)}items[item.id]={action:r.action||"keep",selected_evidence_ids:r.selected_evidence_ids||[],edited_ja:r.edited_ja||"",edited_zh:r.edited_zh||"",note:r.note||"",timing_action:r.action==="reject"?"keep":r.timing_action||"keep",edited_start_ms:r.timing_action==="edit"?r.edited_start_ms:null,edited_end_ms:r.timing_action==="edit"?r.edited_end_ms:null}}return{schema_version:2,bundle_id:bundle.bundle_id,items}}
const baseCleanDraft=cleanDraft;cleanDraft=function(raw){const out=baseCleanDraft(raw);for(const item of bundle.items){const value=raw&&raw[item.id],review=out[item.id];if(!review||!value||typeof value!=="object")continue;review.structure_action=item.kind==="subtitle"&&["keep","split","merge_next"].includes(value.structure_action)?value.structure_action:"keep";review.split_at_ms=Number.isInteger(value.split_at_ms)?value.split_at_ms:Math.round((item.start_ms+item.end_ms)/2);for(const key of ["split_ja_before","split_ja_after","split_zh_before","split_zh_after"])review[key]=typeof value[key]==="string"?value[key].slice(0,20000):""}return out};
const baseCollect=collect;collect=function(){const result=baseCollect();for(const item of bundle.items){const review=reviewFor(item),target=result.items[item.id],structure=item.kind==="subtitle"&&review.action!=="reject"?review.structure_action||"keep":"keep";if(structure==="split"){const current=effectiveTimes(item);if(!Number.isInteger(review.split_at_ms)||review.split_at_ms<=current.start_ms||review.split_at_ms>=current.end_ms)throw new Error(`${item.id} 的拆分点必须位于当前时间段内部`);if(!review.split_ja_before||!review.split_ja_after||!review.split_zh_before||!review.split_zh_after)throw new Error(`${item.id} 拆分后的两组双语文本均不能为空`)}target.structure_action=structure;target.split_at_ms=structure==="split"?review.split_at_ms:null;for(const key of ["split_ja_before","split_ja_after","split_zh_before","split_zh_after"])target[key]=structure==="split"?review[key]:""}return result};
function download(){let data;try{data=collect()}catch(e){message.textContent=e.message;return}const blob=new Blob([JSON.stringify(data,null,2)],{type:"application/json"}),a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download=`${bundle.source.input_srt_name.replace(/_zh\.srt$/i,"")}_human_review.json`;a.click();URL.revokeObjectURL(a.href)}
async function submit(){let data;try{data=collect()}catch(e){message.textContent=e.message;return}const button=$("submit");button.disabled=true;message.textContent="正在提交……";try{const response=await fetch("/api/submit",{method:"POST",headers:{"Content-Type":"application/json","X-Review-Token":SESSION_TOKEN},body:JSON.stringify(data)});const result=await response.json();if(!response.ok)throw new Error(result.error||`HTTP ${response.status}`);message.textContent=`已生成 ${result.output_srt_name}；审核完成标记已写入。`}catch(e){button.disabled=false;message.textContent=`提交失败：${e.message}`}}
player.addEventListener("timeupdate",()=>{if(stopAt!==null&&player.currentTime>=stopAt){player.pause();stopAt=null}});player.addEventListener("ended",()=>{stopAt=null});
$("showAll").addEventListener("change",render);$("download").addEventListener("click",download);$("submit").addEventListener("click",submit);$("clear").addEventListener("click",()=>{if(confirm("确认清除当前作品的本地草稿？")){localStorage.removeItem(storageKey());draft={};render()}});
fetch("/api/bundle",{cache:"no-store"}).then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}).then(data=>{bundle=data;const required=bundle.items.filter(isFlagged).length,suppressed=bundle.items.filter(x=>x.review_policy==="optional_suppressed").length;$("subtitle").textContent=`${bundle.source.audio_name} · ${required} 条需审核${suppressed?`；${suppressed} 条已自动隔离，可选查看`:""}`;$("romajiWarning").classList.toggle("hidden",bundle.romaji_available);loadDraft();render()}).catch(e=>{message.textContent=`加载失败：${e.message}`});
</script></body></html>"""


def _render_index_html(nonce: str, session_token: str) -> bytes:
    html = _INDEX_HTML.replace("__NONCE__", nonce).replace(
        "__TOKEN_JSON__", json.dumps(session_token)
    )
    return html.encode("utf-8")


class _ReviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def create_review_server(
    *,
    bundle: dict,
    audio_path: os.PathLike | str,
    output_srt: os.PathLike | str,
    result_json: os.PathLike | str,
    host: str = "127.0.0.1",
    port: int = 0,
    session_token: str | None = None,
) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("review server must bind to 127.0.0.1")
    audio = Path(audio_path).resolve(strict=True)
    output_path = Path(output_srt).resolve()
    result_path = Path(result_json).resolve()
    token = session_token or secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)
    bundle_bytes = (json.dumps(bundle, ensure_ascii=False) + "\n").encode("utf-8")
    index_bytes = _render_index_html(nonce, token)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ASMRReview/1"

        def log_message(self, format, *args):
            return

        def _headers(self, status: int, content_type: str, length: int | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "media-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            if length is not None:
                self.send_header("Content-Length", str(length))

        def _json_response(self, status: int, payload: dict):
            body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(body))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Host") != getattr(self.server, "allowed_host"):
                self._json_response(421, {"error": "invalid host"})
                return
            path = urlsplit(self.path).path
            if path == "/":
                self._headers(200, "text/html; charset=utf-8", len(index_bytes))
                self.end_headers()
                self.wfile.write(index_bytes)
            elif path == "/api/bundle":
                self._headers(200, "application/json; charset=utf-8", len(bundle_bytes))
                self.end_headers()
                self.wfile.write(bundle_bytes)
            elif path == "/media/audio":
                self._serve_audio()
            else:
                self._json_response(404, {"error": "not found"})

        def _serve_audio(self):
            size = audio.stat().st_size
            try:
                byte_range = _parse_byte_range(self.headers.get("Range"), size)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            start, end = byte_range if byte_range else (0, size - 1)
            length = end - start + 1
            content_type = mimetypes.guess_type(audio.name)[0] or "application/octet-stream"
            self._headers(206 if byte_range else 200, content_type, length)
            self.send_header("Accept-Ranges", "bytes")
            if byte_range:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(audio, "rb") as handle:
                handle.seek(start)
                _write_audio_chunks(self.wfile, handle, length)

        def do_POST(self):
            if self.headers.get("Host") != getattr(self.server, "allowed_host"):
                self._json_response(421, {"error": "invalid host"})
                return
            if urlsplit(self.path).path != "/api/submit":
                self._json_response(404, {"error": "not found"})
                return
            expected_origin = getattr(self.server, "expected_origin")
            origin = self.headers.get("Origin")
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if self.headers.get("X-Review-Token") != token or (origin and origin != expected_origin):
                self._json_response(403, {"error": "forbidden"})
                return
            if content_type != "application/json":
                self._json_response(415, {"error": "application/json required"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_REQUEST_BYTES:
                self._json_response(413, {"error": "invalid request size"})
                return
            submit_lock = getattr(self.server, "submit_lock")
            if not submit_lock.acquire(blocking=False):
                self._json_response(409, {"error": "review submission already in progress"})
                return
            try:
                if getattr(self.server, "submit_state") == "submitted":
                    self._json_response(409, {"error": "review was already submitted"})
                    return
                self.server.submit_state = "in_progress"
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                result = write_review_outputs(
                    bundle, payload, output_srt=output_path, result_json=result_path,
                )
                validate_completed_review_outputs(
                    output_path,
                    result_path,
                    expected_bundle_id=bundle["bundle_id"],
                    expected_bundle_fingerprint=bundle["bundle_fingerprint"],
                )
                self.server.submit_state = "submitted"
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, OSError) as exc:
                self.server.submit_state = "ready"
                self._json_response(400, {"error": str(exc)[:500]})
                return
            except Exception:
                self.server.submit_state = "ready"
                self._json_response(500, {"error": "internal review submission failure"})
                return
            finally:
                submit_lock.release()
            self._json_response(
                200,
                {
                    "ok": True,
                    "output_srt_name": result["output_srt_name"],
                    "reviewed_at": result["reviewed_at"],
                },
            )
            if getattr(self.server, "exit_after_submit", False):
                threading.Thread(target=self.server.shutdown, daemon=True).start()

    server = _ReviewServer((host, port), Handler)
    bound_host, bound_port = server.server_address[:2]
    display_host = bound_host
    server.allowed_host = f"{display_host}:{bound_port}"
    server.expected_origin = f"http://{display_host}:{bound_port}"
    server.exit_after_submit = False
    server.submit_lock = threading.Lock()
    server.submit_state = "ready"
    return server


def _find_audio(audio_dir: Path, base: str) -> Path:
    active = discover_active_audio(audio_dir)
    if base not in active:
        raise FileNotFoundError(f"当前音频输入中没有作品：{base}")
    return active[base]


def _optional_romanizer():
    try:
        from pykakasi import kakasi
    except ImportError:
        return None
    converter = kakasi()

    def romanize(text: str) -> str:
        return " ".join(part["hepburn"] for part in converter.convert(text)).strip()

    return romanize


def _probe_audio_duration_ms(audio_path: Path) -> int:
    command = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=30,
        )
        duration_ms = int(round(float(completed.stdout.strip()) * 1000))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"无法读取音频时长：{audio_path.name}") from exc
    if duration_ms <= 0:
        raise ValueError(f"音频时长必须为正数：{audio_path.name}")
    return duration_ms


def _load_prior_review_draft(bundle: dict, result_path: Path) -> dict[str, dict]:
    if not result_path.is_file():
        return {}
    try:
        with open(result_path, "r", encoding="utf-8-sig") as handle:
            prior = json.load(handle)
        return migrate_legacy_review_submission(bundle, prior)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def build_bundle_for_base(base: str, output_dir: Path, audio_dir: Path):
    output_dir = output_dir.resolve()
    audio_path = _find_audio(audio_dir.resolve(), base)
    zh_path = output_dir / f"{base}_zh.srt"
    if not zh_path.is_file():
        raise FileNotFoundError(zh_path)
    script_units_path = output_dir / f"{base}_script_units.json"
    script_alignment_path = output_dir / f"{base}_script_alignment.json"
    use_script_filter = os.environ.get(
        "ENABLE_SCRIPT_REVIEW_FILTER", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if use_script_filter:
        missing = [
            path for path in (script_units_path, script_alignment_path)
            if not path.is_file()
        ]
        if missing:
            raise FileNotFoundError(
                "台本审核过滤已开启，但缺少产物："
                + ", ".join(str(path) for path in missing)
            )
    bundle = build_review_bundle(
        zh_path,
        audio_path,
        ensemble_srt_path=output_dir / f"{base}_ensemble.srt",
        reviewed_srt_path=output_dir / f"{base}_reviewed.srt",
        candidates_path=output_dir / f"{base}_asr_candidates.json",
        timeline_path=output_dir / f"{base}_fusion_timeline.json",
        script_units_path=script_units_path if use_script_filter else None,
        script_alignment_path=script_alignment_path if use_script_filter else None,
        long_cue_alignment_path=output_dir / f"{base}_long_cue_alignment.json",
        romanizer=_optional_romanizer(),
    )
    bundle.setdefault("audio", {})["duration_ms"] = _probe_audio_duration_ms(audio_path)
    result_path = output_dir / f"{base}_human_review.json"
    prior_review = _load_prior_review_draft(bundle, result_path)
    if prior_review:
        bundle["prior_review"] = prior_review
    return {
        "base": base,
        "output_dir": output_dir,
        "audio_dir": audio_dir.resolve(),
        "bundle": bundle,
        "audio_path": audio_path,
        "input_srt": zh_path,
        "output_srt": output_dir / f"{base}_human_reviewed.srt",
        "result_json": result_path,
    }


def main() -> None:
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="启动本机 ASMR 字幕人工复核页面")
    parser.add_argument("base", help="作品基础名（不含 _zh.srt）")
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path(os.environ.get("OUTPUT_DIR", project / "output")),
    )
    parser.add_argument(
        "--audio-dir", type=Path,
        default=Path(os.environ.get("AUDIO_DIR", project / "audio")),
    )
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    job = build_bundle_for_base(args.base, args.output_dir, args.audio_dir)
    bundle = job["bundle"]
    server = create_review_server(
        bundle=bundle,
        audio_path=job["audio_path"],
        output_srt=job["output_srt"],
        result_json=job["result_json"],
        port=args.port,
    )
    server.exit_after_submit = True
    url = server.expected_origin + "/"
    print(f"人工复核页面：{url}")
    print("完成提交后按 Ctrl+C 关闭本地服务。")
    if not bundle["romaji_available"]:
        print("警告：未安装 pykakasi，当前页面不会生成罗马音。")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n人工复核服务已关闭。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
