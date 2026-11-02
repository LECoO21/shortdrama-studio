import {test} from 'node:test';
import assert from 'node:assert/strict';
import {toOpenReel,fromOpenReel,runCoreAction,replaceSource,ActionExecutor,seconds,frameToPts,frames} from '../lib/editor';
import type {ProjectDocument,Asset} from '../lib/types';
import {detachSubtitleBinding} from '../lib/subtitle-binding';
const assets:Asset[]=[{id:'a',version:1,sha256:'hash-original',filename:'A.mp4',media_url:'/api/v1/assets/a/media',metadata:{duration_seconds:20,video_stream_index:0,streams:[{index:0,type:'video',time_base:{num:1,den:90000},start_pts:'9007199254740995',end_pts:'9007199256540995'}]}}];
function fixture():ProjectDocument{return {schema_version:1,project_id:'p',name:'test',canvas:{width:1280,height:720},fps:{num:30000,den:1001},sample_rate:48000,duration_frames:300,tracks:[{id:'v',kind:'video',enabled:true},{id:'s',kind:'subtitle',enabled:true}],clips:[{id:'c',track_id:'v',source:{asset_id:'a',version:1,stream_index:0,time_base:{num:1,den:90000},in_pts:'9007199254740995',out_pts:(9007199254740995n+frameToPts(300,{num:30000,den:1001},{num:1,den:90000})).toString()},timeline_start_frame:0,duration_frames:300,gain_db:-3,muted:false,fade_in_frames:0,fade_out_frames:0,fit:'contain',include_source_audio:true}],subtitles:[{id:'s1',track_id:'s',text:'人工修正',start_frame:0,end_frame:30,font_size:32,color:'#ffffff',bottom_percent:8}]}}
test('no-op roundtrip preserves high PTS, rational fps, source identity and subtitle style exactly',()=>{const source=fixture();const core=toOpenReel(source,assets);const result=fromOpenReel(JSON.parse(JSON.stringify(core)),source);assert.deepEqual(result,source);assert.equal(source.clips[0].source.in_pts,'9007199254740995')});
test('100 JSON reopen cycles at 30000/1001 introduce no drift',()=>{let doc=fixture();for(let i=0;i<100;i++)doc=fromOpenReel(JSON.parse(JSON.stringify(toOpenReel(doc,assets))),doc);assert.deepEqual(doc,fixture())});
test('real upstream move, trim and split preserve source tick identity through sidecar',async()=>{let doc=fixture();doc=await runCoreAction(doc,assets,'clip/move',{clipId:'c',startTime:seconds(30,doc.fps)});assert.equal(doc.clips[0].timeline_start_frame,30);doc=await runCoreAction(doc,assets,'clip/trim',{clipId:'c',inPoint:seconds(10,doc.fps),outPoint:seconds(200,doc.fps)});assert.equal(doc.clips[0].duration_frames,190);doc=await runCoreAction(doc,assets,'clip/split',{clipId:'c',time:seconds(100,doc.fps)});assert.equal(doc.clips.length,2);assert.equal(doc.clips[0].duration_frames,70);assert.equal(doc.clips[1].duration_frames,120);assert.equal(doc.clips[0].source.out_pts,doc.clips[1].source.in_pts);assert.equal(doc.clips[1].source.asset_id,'a');assert.equal(doc.clips[1].source.version,1)});
test('actual OpenReel undo and redo restore split source timing',async()=>{const original=fixture(),core=toOpenReel(original,assets),executor=new ActionExecutor();assert.equal((await executor.execute({type:'clip/split',id:'split',timestamp:Date.now(),params:{clipId:'c',time:seconds(110,original.fps)}},core)).success,true);assert.equal(fromOpenReel(core,original).clips.length,2);assert.equal((await executor.undo(core)).success,true);assert.deepEqual(fromOpenReel(core,original),original);assert.equal((await executor.redo(core)).success,true);assert.equal(fromOpenReel(core,original).clips.length,2)});
test('real upstream audio and subtitle actions survive reopen while platform properties remain',async()=>{let doc=fixture();doc=await runCoreAction(doc,assets,'audio/setVolume',{clipId:'c',volume:10**(-6/20)});doc=await runCoreAction(doc,assets,'audio/setFade',{clipId:'c',fadeIn:seconds(5,doc.fps),fadeOut:seconds(7,doc.fps)});doc=await runCoreAction(doc,assets,'subtitle/update',{subtitleId:'s1',text:'新字幕',startTime:seconds(3,doc.fps),endTime:seconds(33,doc.fps)});assert.equal(doc.clips[0].gain_db,-6);assert.equal(doc.clips[0].fade_out_frames,7);assert.equal(doc.subtitles[0].text,'新字幕');assert.equal(doc.subtitles[0].bottom_percent,8);assert.equal(doc.subtitles[0].start_frame,3)});
test('split VFR-like non-frame-aligned source inpoint maps at source tick precision',async()=>{const doc=fixture();doc.clips[0].source.in_pts=(BigInt(doc.clips[0].source.in_pts)+33n).toString();doc.clips[0].source.out_pts=(BigInt(doc.clips[0].source.out_pts)+33n).toString();const next=await runCoreAction(doc,assets,'clip/split',{clipId:'c',time:seconds(100,doc.fps)});assert.equal(next.clips[0].source.out_pts,next.clips[1].source.in_pts);assert.equal(BigInt(next.clips[1].source.in_pts)-BigInt(doc.clips[0].source.in_pts),frameToPts(100,doc.fps,doc.clips[0].source.time_base))});
test('invalid core action and fractional engineering frame are rejected',async()=>{await assert.rejects(()=>runCoreAction(fixture(),assets,'clip/split',{clipId:'missing',time:1}));await assert.rejects(()=>runCoreAction(fixture(),assets,'project/registerGeneratedShader',{}));assert.throws(()=>frames(.013,{num:25,den:1}),/帧边界/)});
test('replacement preserves timeline, gain, mute and fit; short replacement is rejected',()=>{const original=fixture();original.clips[0].muted=true;original.clips[0].fit='cover';const replacement=structuredClone(assets[0]);replacement.id='b';replacement.version=2;const next=replaceSource(original,'c',replacement);assert.equal(next.clips[0].source.asset_id,'b');assert.equal(next.clips[0].source.version,2);assert.equal(next.clips[0].gain_db,-3);assert.equal(next.clips[0].muted,true);assert.equal(next.clips[0].fit,'cover');assert.equal(next.clips[0].duration_frames,300);replacement.metadata.streams[0].end_pts=(BigInt(replacement.metadata.streams[0].start_pts)+100n).toString();assert.throws(()=>replaceSource(original,'c',replacement),/不足/)});
test('deleting a clip never destroys immutable asset records or rewrites canvas duration',async()=>{const doc=fixture();const before=structuredClone(assets);const next=await runCoreAction(doc,assets,'clip/remove',{clipId:'c'});assert.equal(next.clips.length,0);assert.equal(next.duration_frames,300);assert.deepEqual(assets,before)});

test('the sidecar preserves binding provenance without sharing nested mutable spans',()=>{
 const original=fixture();original.subtitles[0].binding={asset_id:'a',asset_version:1,stream_index:0,
  transcript_id:'dialogue',transcript_revision:2,segment_id:'line',review_required:true,
  spans:[{clip_id:'c',start_frame:0,end_frame:30}]};
 const next=fromOpenReel(JSON.parse(JSON.stringify(toOpenReel(original,assets))),original);
 assert.deepEqual(next,original);
 next.subtitles[0].binding!.spans[0].end_frame=20;
 assert.equal(original.subtitles[0].binding.spans[0].end_frame,30);
});

test('source replacement requires explicitly detaching linked dialogue and preserves the old input',()=>{
 const original=fixture();original.subtitles[0].binding={asset_id:'a',asset_version:1,stream_index:0,
  review_required:false,spans:[{clip_id:'c',start_frame:0,end_frame:30}]};
 const before=structuredClone(original),replacement=structuredClone(assets[0]);replacement.id='replacement';
 assert.throws(()=>replaceSource(original,'c',replacement),/解除字幕联动/);
 assert.deepEqual(original,before);
 const independent=detachSubtitleBinding(original,'s1');
 const replaced=replaceSource(independent,'c',replacement);
 assert.equal(replaced.clips[0].source.asset_id,'replacement');
 assert.deepEqual(replaced.subtitles,independent.subtitles);
 assert.deepEqual(original,before);
});

test('split preserves gain and outer fades without creating a new audible fade at the cut',async()=>{
 const original=fixture();original.clips[0].gain_db=-6;original.clips[0].fade_in_frames=10;original.clips[0].fade_out_frames=15;
 const next=await runCoreAction(original,assets,'clip/split',{clipId:'c',time:seconds(100,original.fps)});
 assert.deepEqual(next.clips.map(c=>[c.gain_db,c.fade_in_frames,c.fade_out_frames]),[[-6,10,0],[-6,0,15]]);
 assert.equal(next.clips[0].source.out_pts,next.clips[1].source.in_pts);
 const reopened=fromOpenReel(JSON.parse(JSON.stringify(toOpenReel(next,assets))),next);
 assert.deepEqual(reopened,next);
 const fadeGain=(at:number,duration:number,fadeIn:number,fadeOut:number)=>Math.min(fadeIn?Math.min(1,at/fadeIn):1,fadeOut?Math.min(1,(duration-at)/fadeOut):1);
 for(const at of [0,5,9,10,99.5,100,100.5,284,290,299]){
  const clip=at<100?next.clips[0]:next.clips[1];const local=at-clip.timeline_start_frame;
  assert.equal(fadeGain(local,clip.duration_frames,clip.fade_in_frames,clip.fade_out_frames),fadeGain(at,300,10,15));
 }
});
test('split refuses positions inside either fade and allows exact fade boundaries',async()=>{
 const doc=fixture();doc.clips[0].fade_in_frames=20;doc.clips[0].fade_out_frames=30;
 const before=structuredClone(doc);
 for(const frame of [10,19,271,290])await assert.rejects(()=>runCoreAction(doc,assets,'clip/split',{clipId:'c',time:seconds(frame,doc.fps)}),/淡入或淡出区间/);
 assert.deepEqual(doc,before);
 for(const frame of [20,270]){const next=await runCoreAction(doc,assets,'clip/split',{clipId:'c',time:seconds(frame,doc.fps)});assert.deepEqual(next.clips.map(c=>[c.fade_in_frames,c.fade_out_frames]),[[20,0],[0,30]]);}
});
