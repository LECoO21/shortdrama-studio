import {ActionExecutor} from '../vendor/openreel/actions.mjs';
import type {ProjectDocument,Asset,Clip,Rational,Source} from './types';
import {reconcileSubtitleBindings, retimeSubtitle, validateSubtitleBindings} from './subtitle-binding';
export {ActionExecutor};
const clone=<T>(value:T):T=>structuredClone(value);
export const seconds=(frames:number,fps:Rational)=>frames*fps.den/fps.num;
export function frames(value:number,fps:Rational):number {const n=value*fps.num/fps.den;const rounded=Math.round(n);if(!Number.isFinite(n)||Math.abs(n-rounded)>1e-5)throw new Error('编辑结果没有落在工程帧边界，已拒绝写入。');return rounded;}
function halfUp(n:bigint,d:bigint){return n>=0n?(n*2n+d)/(d*2n):-((-n*2n+d)/(d*2n));}
export function frameToPts(frame:number,fps:Rational,tb:Rational):bigint {if(!Number.isSafeInteger(frame))throw new Error('帧号必须为安全整数');return halfUp(BigInt(frame)*BigInt(fps.den)*BigInt(tb.den),BigInt(fps.num)*BigInt(tb.num));}
export function ptsSeconds(pts:string,tb:Rational):number{return Number(BigInt(pts))*tb.num/tb.den;}
export function ptsFrame(pts:string,tb:Rational,fps:Rational):number{return Number(halfUp(BigInt(pts)*BigInt(tb.num)*BigInt(fps.num),BigInt(tb.den)*BigInt(fps.den)));}
function sourceStart(source:Source,assets:Asset[]):string {const asset=assets.find(a=>a.id===source.asset_id&&a.version===source.version);const stream=asset?.metadata.streams.find(s=>s.index===source.stream_index);if(!stream)throw new Error('工程引用的素材或媒体流不存在');return stream.start_pts;}
interface CoreClip {id:string;mediaId:string;trackId:string;startTime:number;duration:number;inPoint:number;outPoint:number;volume:number;fade:{fadeIn:number;fadeOut:number};transform:{position:{x:number;y:number};scale:{x:number;y:number};rotation:number;anchor:{x:number;y:number};opacity:number;fitMode:string};effects:unknown[];audioEffects:unknown[];keyframes:unknown[];metadata:{platform:Clip;sourceStart:string}}
interface CoreTrack {id:string;type:string;name:string;clips:CoreClip[];transitions:unknown[];locked:boolean;hidden:boolean;muted:boolean;solo:boolean}
interface CoreSubtitle {id:string;text:string;startTime:number;endTime:number;style?:Record<string,unknown>}
export interface CoreProject {id:string;name:string;createdAt:number;modifiedAt:number;settings:{width:number;height:number;frameRate:number;sampleRate:number;channels:number};mediaLibrary:{items:{id:string;name:string;type:string;fileHandle:null;blob:null;metadata:Record<string,unknown>;thumbnailUrl:null;waveformData:null}[]};timeline:{tracks:CoreTrack[];subtitles:CoreSubtitle[];duration:number;markers:unknown[]}}
export function toOpenReel(document:ProjectDocument,assets:Asset[]):CoreProject{
 const fps=document.fps;
 return {id:document.project_id,name:document.name,createdAt:0,modifiedAt:0,settings:{...document.canvas,frameRate:fps.num/fps.den,sampleRate:document.sample_rate,channels:2},mediaLibrary:{items:assets.map(a=>({id:a.id,name:a.filename,type:a.metadata.video_stream_index!=null?'video':'audio',fileHandle:null,blob:null,thumbnailUrl:null,waveformData:null,metadata:{duration:a.metadata.duration_seconds??0,width:a.metadata.width??0,height:a.metadata.height??0,frameRate:fps.num/fps.den,codec:'',sampleRate:document.sample_rate,channels:2,fileSize:0}}))},timeline:{duration:seconds(document.duration_frames,fps),markers:[],subtitles:document.subtitles.map(s=>({id:s.id,text:s.text,startTime:seconds(s.start_frame,fps),endTime:seconds(s.end_frame,fps)})),tracks:document.tracks.map(t=>({id:t.id,type:t.kind==='subtitle'?'text':t.kind,name:t.id,locked:false,hidden:!t.enabled,muted:!t.enabled,solo:false,transitions:[],clips:document.clips.filter(c=>c.track_id===t.id).map(c=>{const start=sourceStart(c.source,assets);return {id:c.id,mediaId:c.source.asset_id,trackId:c.track_id,startTime:seconds(c.timeline_start_frame,fps),duration:seconds(c.duration_frames,fps),inPoint:ptsSeconds((BigInt(c.source.in_pts)-BigInt(start)).toString(),c.source.time_base),outPoint:ptsSeconds((BigInt(c.source.out_pts)-BigInt(start)).toString(),c.source.time_base),volume:c.muted?0:10**(c.gain_db/20),fade:{fadeIn:seconds(c.fade_in_frames,fps),fadeOut:seconds(c.fade_out_frames,fps)},transform:{position:{x:0,y:0},scale:{x:1,y:1},rotation:0,anchor:{x:.5,y:.5},opacity:1,fitMode:c.fit},effects:[],audioEffects:[],keyframes:[],metadata:{platform:clone(c),sourceStart:start}};})}))}};
}
function updatedPts(sec:number,previous:string,start:string,tb:Rational):string {const oldSec=ptsSeconds((BigInt(previous)-BigInt(start)).toString(),tb);if(Math.abs(sec-oldSec)<1e-10)return previous;if(!Number.isFinite(sec)||sec<0)throw new Error('非法源时间');const decimal=BigInt(sec.toFixed(12).replace('.',''));const ticks=halfUp(decimal*BigInt(tb.den),1_000_000_000_000n*BigInt(tb.num));return (BigInt(start)+ticks).toString();}
export function fromOpenReel(core:CoreProject,base:ProjectDocument):ProjectDocument{
 const document=clone(base);const fps=document.fps;
 document.clips=core.timeline.tracks.flatMap(t=>t.clips.map(c=>{
 const prior=c.metadata?.platform;if(!prior)throw new Error('片段缺少平台来源信息，已拒绝导入');
 if(c.effects.length||c.audioEffects.length||c.keyframes.length)throw new Error('本阶段不支持的效果不可写入工程');
 const priorVolume=prior.muted?0:10**(prior.gain_db/20);const sameVolume=Math.abs(c.volume-priorVolume)<1e-10;
 return {...clone(prior),id:c.id,track_id:c.trackId,timeline_start_frame:frames(c.startTime,fps),duration_frames:frames(c.duration,fps),source:{...prior.source,in_pts:updatedPts(c.inPoint,prior.source.in_pts,c.metadata.sourceStart,prior.source.time_base),out_pts:updatedPts(c.outPoint,prior.source.out_pts,c.metadata.sourceStart,prior.source.time_base)},gain_db:sameVolume?prior.gain_db:c.volume===0?prior.gain_db:Math.round(20*Math.log10(c.volume)*1e6)/1e6,muted:sameVolume?prior.muted:c.volume===0,fade_in_frames:frames(c.fade?.fadeIn??0,fps),fade_out_frames:frames(c.fade?.fadeOut??0,fps)};
 }));
 document.subtitles=core.timeline.subtitles.map(s=>{const prior=base.subtitles.find(p=>p.id===s.id);if(!prior)throw new Error('字幕缺少平台样式信息');return {...clone(prior),text:s.text,start_frame:frames(s.startTime,fps),end_frame:frames(s.endTime,fps)};});
 // The platform's frozen canvas duration is independent from upstream's auto-sized duration.
 return document;
}
const ALLOWED=new Set(['clip/move','clip/trim','clip/split','clip/remove','audio/setVolume','audio/setFade','subtitle/update']);
export async function runCoreAction(document:ProjectDocument,assets:Asset[],type:string,params:Record<string,unknown>):Promise<ProjectDocument>{
 if(!ALLOWED.has(type))throw new Error('本阶段未开放此编辑动作');
 const splitClip=type==='clip/split'?document.clips.find(c=>c.id===params.clipId):undefined;
 if(splitClip&&typeof params.time==='number'){
  const offset=frames(params.time,document.fps)-splitClip.timeline_start_frame;
  if(offset>0&&offset<splitClip.duration_frames&&(offset<splitClip.fade_in_frames||offset>splitClip.duration_frames-splitClip.fade_out_frames)){
   throw new Error('拆分点位于淡入或淡出区间内；本阶段无法无损表达截断的音量曲线，请先调整淡入淡出或选择区间外的帧。');
  }
 }
 const core=toOpenReel(document,assets);const executor=new ActionExecutor();const result=await executor.execute({id:crypto.randomUUID(),timestamp:Date.now(),type,params},core);if(!result.success)throw new Error(result.error?.message??'编辑失败');
 if(splitClip){
  // Upstream duplicates the entire fade object. Preserve only the original
  // outer edges; inserting a new fade at the cut would change audible content.
  const clips=core.timeline.tracks.flatMap(t=>t.clips);
  const beforeIds=new Set(document.clips.map(c=>c.id));
  const left=clips.find(c=>c.id===splitClip.id);
  const added=clips.filter(c=>!beforeIds.has(c.id));
  if(!left||added.length!==1)throw new Error('拆分结果不符合双片段契约，已拒绝写入');
  left.fade={fadeIn:seconds(splitClip.fade_in_frames,document.fps),fadeOut:0};
  added[0].fade={fadeIn:0,fadeOut:seconds(splitClip.fade_out_frames,document.fps)};
 }
 let next=fromOpenReel(JSON.parse(JSON.stringify(core)) as CoreProject,document);
 if(type==='subtitle/update'){
  const previous=document.subtitles.find(s=>s.id===params.subtitleId);
  const updated=next.subtitles.find(s=>s.id===params.subtitleId);
  if(previous?.binding&&updated&&(previous.start_frame!==updated.start_frame||previous.end_frame!==updated.end_frame)){
   const timed=retimeSubtitle(document,previous.id,updated.start_frame,updated.end_frame).subtitles.find(s=>s.id===previous.id)!;
   updated.binding=timed.binding;
  }
  validateSubtitleBindings(next);return next;
 }
 if(splitClip){
  const beforeIds=new Set(document.clips.map(c=>c.id));
  const right=next.clips.find(c=>!beforeIds.has(c.id));
  if(!right||typeof params.time!=='number')throw new Error('拆分结果缺少来源片段，修改未应用');
  return reconcileSubtitleBindings(document,next,{split:{clipId:splitClip.id,rightClipId:right.id,offset:frames(params.time,document.fps)-splitClip.timeline_start_frame}});
 }
 if(type==='clip/trim'){
  const previous=document.clips.find(c=>c.id===params.clipId),updated=next.clips.find(c=>c.id===params.clipId);
  if(previous&&updated){
   const shift=ptsFrame((BigInt(updated.source.in_pts)-BigInt(previous.source.in_pts)).toString(),previous.source.time_base,document.fps);
   return reconcileSubtitleBindings(document,next,{trim:{clipId:previous.id,shiftFrames:shift}});
  }
 }
 next=reconcileSubtitleBindings(document,next);return next;
}
export function replaceSource(document:ProjectDocument,clipId:string,asset:Asset):ProjectDocument{
 if(document.subtitles.some(s=>s.binding?.spans.some(span=>span.clip_id===clipId)))throw new Error('这个片段有关联字幕，请先解除字幕联动再替换素材，避免沿用错误对白。');
 const next=clone(document);const c=next.clips.find(c=>c.id===clipId);if(!c)throw new Error('未找到片段');const kind=next.tracks.find(t=>t.id===c.track_id)?.kind;const stream=asset.metadata.streams.find(s=>s.index===(kind==='audio'?asset.metadata.audio_stream_index:asset.metadata.video_stream_index));if(!stream)throw new Error('替换素材不包含对应类型的流');
 const out=BigInt(stream.start_pts)+frameToPts(c.duration_frames,next.fps,stream.time_base);if(out>BigInt(stream.end_pts))throw new Error('替换素材不足以覆盖当前片段时长');
 c.source={asset_id:asset.id,version:asset.version,stream_index:stream.index,time_base:stream.time_base,in_pts:stream.start_pts,out_pts:out.toString()};return next;
}
