'use client';

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {mediaUrl} from '../lib/api';
import type {Asset, Clip, ProjectDocument, Rational} from '../lib/types';
import {clipAtFrame, clipGain, enabledClips, previewClockTick, previewTimecode, sourceTime} from './CompositionPreview.time';

type Props = {
  document: ProjectDocument;
  assets: Asset[];
  frame: number;
  playing: boolean;
  onFrame: (frame: number) => void;
  onPlayingChange: (playing: boolean) => void;
};

type LayerProps = {
  clip: Clip;
  asset: Asset;
  kind: 'video' | 'audio';
  frame: number;
  fps: Rational;
  active: boolean;
  playing: boolean;
  context: AudioContext | null;
  onProblem: (message: string) => void;
};

function MediaLayer({clip, asset, kind, frame, fps, active, playing, context, onProblem}: LayerProps) {
  const element = useRef<HTMLVideoElement | HTMLAudioElement | null>(null);
  const graph = useRef<{context: AudioContext; source: MediaElementAudioSourceNode; gain: GainNode} | null>(null);
  const source = mediaUrl(asset.media_url);
  const outputGain = active && (kind === 'audio' || clip.include_source_audio) ? clipGain(clip, frame) : 0;

  useEffect(() => {
    const media = element.current;
    if (!media || !context) return;
    try {
      // A media element can have only one MediaElementSource. Reuse the node
      // when React Strict Mode repeats an effect setup / cleanup cycle.
      const existing = graph.current?.context === context ? graph.current : null;
      const sourceNode = existing?.source ?? context.createMediaElementSource(media);
      const gain = existing?.gain ?? context.createGain();
      gain.gain.value = 0;
      sourceNode.connect(gain).connect(context.destination);
      graph.current = {context, source: sourceNode, gain};
      return () => {
        sourceNode.disconnect();
        gain.disconnect();
      };
    } catch {
      // A blocked/unsupported AudioContext leaves the native volume path
      // usable; the transport will disclose the unavailable boost.
      graph.current = null;
    }
  }, [context]);

  const synchronize = useCallback(() => {
    const media = element.current;
    if (!media || media.readyState < 1) return;
    const positionFrame = active ? frame : clip.timeline_start_frame;
    const desired = sourceTime(clip, asset, positionFrame, fps);
    const latest = Number.isFinite(media.duration) ? Math.max(0, media.duration - 0.001) : desired;
    const target = Math.min(desired, latest);
    // Paused seeks follow every scrubbed frame. While playing the element
    // advances itself; only correct a meaningful clock drift.
    if (Math.abs(media.currentTime - target) > (active && playing ? 0.16 : 0.0005)) {
      try { media.currentTime = target; } catch { /* Retry on loadedmetadata. */ }
    }
    if (graph.current) {
      media.volume = 1;
      graph.current.gain.gain.setTargetAtTime(outputGain, context?.currentTime ?? 0, 0.005);
    } else {
      media.volume = Math.min(1, outputGain);
    }
    if (active && playing) {
      if (media.paused) {
        void media.play().catch(() => onProblem('浏览器暂未允许播放，请再次点击播放。'));
      }
    } else if (!media.paused) {
      media.pause();
    }
  }, [active, playing, frame, clip, asset, fps, outputGain, context, onProblem]);

  useEffect(() => { synchronize(); }, [synchronize]);
  useEffect(() => {
    const media = element.current;
    return () => { media?.pause(); };
  }, []);

  const shared = {
    crossOrigin: 'anonymous' as const,
    src: source,
    preload: 'auto',
    onLoadedMetadata: synchronize,
    onCanPlay: synchronize,
    onError: () => onProblem(`无法试听「${asset.filename}」：浏览器可能不支持此媒体编码，请检查服务端导出。`),
    'data-preview-media': clip.id,
    'data-preview-active': active,
  };
  if (kind === 'audio') return <audio {...shared} ref={node => {element.current = node;}} style={{display: 'none'}} />;
  return <video {...shared} ref={node => {element.current = node;}} playsInline
    className="preview-video" aria-hidden="true"
    style={{position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: clip.fit,
      visibility: active ? 'visible' : 'hidden', pointerEvents: 'none'}} />;
}

export default function CompositionPreview({document, assets, frame, playing, onFrame, onPlayingChange}: Props) {
  const [context, setContext] = useState<AudioContext | null>(null);
  const contextRef = useRef<AudioContext | null>(null);
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const [viewportSize, setViewportSize] = useState({width: 0, height: 0});
  const [problem, setProblem] = useState('');
  const canvasAspect = document.canvas.width / document.canvas.height;
  const stageWidth = Math.max(0, Math.min(viewportSize.width, viewportSize.height * canvasAspect));
  const stageHeight = stageWidth / canvasAspect;
  const fps = document.fps.num / document.fps.den;
  const lastFrame = Math.max(0, document.duration_frames - 1);
  const currentFrame = Math.max(0, Math.min(lastFrame, Math.round(frame)));
  const clock = useRef({frame: currentFrame, time: 0, emitted: currentFrame});
  const callbacks = useRef({onFrame, onPlayingChange});
  const videoClips = useMemo(() => enabledClips(document, 'video'), [document]);
  const audioClips = useMemo(() => enabledClips(document, 'audio'), [document]);
  const assetMap = useMemo(() => new Map(assets.map(asset => [asset.id, asset])), [assets]);
  const video = clipAtFrame(videoClips, currentFrame);
  const audio = clipAtFrame(audioClips, currentFrame);
  const nextVideo = videoClips.find(clip => clip.timeline_start_frame > currentFrame);
  const nextAudio = audioClips.find(clip => clip.timeline_start_frame > currentFrame);
  const subtitleTracks = new Set(document.tracks.filter(track => track.kind === 'subtitle' && track.enabled).map(track => track.id));
  const subtitles = document.subtitles.filter(subtitle => subtitleTracks.has(subtitle.track_id)
    && currentFrame >= subtitle.start_frame && currentFrame < subtitle.end_frame);
  const missingAsset = [video, audio].some(clip => clip && !assetMap.has(clip.source.asset_id));
  const overlap = [videoClips, audioClips].some(clips => clips.filter(clip => currentFrame >= clip.timeline_start_frame
    && currentFrame < clip.timeline_start_frame + clip.duration_frames).length > 1);
  const otherStream = [video, audio].some(clip => {
    if (!clip) return false;
    const asset = assetMap.get(clip.source.asset_id);
    const kind = document.tracks.find(track => track.id === clip.track_id)?.kind;
    return asset && clip.source.stream_index !== (kind === 'video' ? asset.metadata.video_stream_index : asset.metadata.audio_stream_index);
  });

  const primeAudio = useCallback(() => {
    try {
      if (!contextRef.current && typeof AudioContext !== 'undefined') {
        contextRef.current = new AudioContext();
        setContext(contextRef.current);
      }
      const audioContext = contextRef.current;
      if (audioContext?.state === 'suspended') {
        void audioContext.resume().catch(() => setProblem('请再次点击播放，以启用声音试听。'));
      }
    } catch {
      setProblem('当前浏览器无法启用音频混合，正增益试听可能受限，请以导出为准。');
    }
  }, []);

  useEffect(() => {
    // Also primes audio for the page's keyboard shortcuts / timeline gestures.
    // The page owns playback toggling. This listener only unlocks audio inside
    // the trusted Space key event, and leaves text editing completely alone.
    const handleKeydown = (event: KeyboardEvent) => {
      const target = event.target;
      const editing = target instanceof HTMLElement && (target.isContentEditable
        || Boolean(target.closest('input,textarea,select,[contenteditable="true"],[role="textbox"]')));
      if (!editing && (event.code === 'Space' || event.key === ' ')) primeAudio();
    };
    window.addEventListener('pointerdown', primeAudio);
    window.addEventListener('keydown', handleKeydown);
    return () => {
      window.removeEventListener('pointerdown', primeAudio);
      window.removeEventListener('keydown', handleKeydown);
      void contextRef.current?.close();
    };
  }, [primeAudio]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const observer = new ResizeObserver(entries => {
      const rect = entries[0]?.contentRect;
      if (!rect) return;
      setViewportSize(previous => Math.abs(previous.width - rect.width) < 0.1
        && Math.abs(previous.height - rect.height) < 0.1 ? previous : {width: rect.width, height: rect.height});
    });
    observer.observe(viewport);
    return () => observer.disconnect();
  }, []);

  useEffect(() => { callbacks.current = {onFrame, onPlayingChange}; }, [onFrame, onPlayingChange]);
  useEffect(() => {
    if (currentFrame !== clock.current.emitted) {
      clock.current.frame = currentFrame;
      clock.current.time = performance.now();
      clock.current.emitted = currentFrame;
    }
  }, [currentFrame]);
  useEffect(() => {
    if (!playing) return;
    clock.current.frame = clock.current.emitted;
    clock.current.time = performance.now();
    let id = 0;
    const tick = (time: number) => {
      const {frame: next, ended} = previewClockTick(clock.current.frame, clock.current.time, time, fps, document.duration_frames);
      if (next !== clock.current.emitted) {
        clock.current.emitted = next;
        callbacks.current.onFrame(next);
      }
      if (ended) {
        callbacks.current.onPlayingChange(false);
        return;
      }
      id = requestAnimationFrame(tick);
    };
    id = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(id);
  }, [playing, fps, lastFrame, document.duration_frames]);

  const togglePlaying = () => {
    primeAudio();
    setProblem('');
    if (!playing && currentFrame >= lastFrame) onFrame(0);
    onPlayingChange(!playing);
  };
  const step = (amount: number) => {
    onPlayingChange(false);
    onFrame(Math.max(0, Math.min(lastFrame, currentFrame + amount)));
  };
  const renderLayer = (clip: Clip, kind: 'video' | 'audio', active: boolean) => {
    const asset = assetMap.get(clip.source.asset_id);
    return asset ? <MediaLayer key={`${clip.id}:${asset.id}:${asset.version}`} clip={clip} asset={asset}
      kind={kind} active={active} frame={currentFrame} fps={document.fps} playing={playing}
      context={context} onProblem={setProblem} /> : null;
  };

  return <div className="composition-preview">
    <div className="preview-viewport" ref={viewportRef}
      style={{flex: '1 1 0%', minHeight: 0, width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden'}}>
    <div className="preview-screen" data-preview-frame={currentFrame} data-preview-source-clip={video?.id ?? ''}
      aria-label="工程画面预览" style={{position: 'relative', aspectRatio: `${document.canvas.width} / ${document.canvas.height}`,
        flex: 'none', width: stageWidth, height: stageHeight, overflow: 'hidden', background: '#060709', containerType: 'inline-size'}}>
      {!video && <div className="preview-empty" style={{position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', color: '#86909e'}}>此处暂无画面</div>}
      {video && renderLayer(video, 'video', true)}
      {nextVideo && renderLayer(nextVideo, 'video', false)}
      {audio && renderLayer(audio, 'audio', true)}
      {nextAudio && renderLayer(nextAudio, 'audio', false)}
      {subtitles.map(subtitle => <div className="preview-subtitle" key={subtitle.id}
        style={{position: 'absolute', width: '90%', left: '5%', bottom: `${subtitle.bottom_percent}%`,
          color: subtitle.color, fontSize: `${subtitle.font_size / document.canvas.width * 100}cqw`,
          lineHeight: 1.2, textAlign: 'center', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere',
          fontFamily: '"Hiragino Sans GB", "Arial Unicode MS", sans-serif',
          textShadow: '0 1px 2px #000, 1px 0 2px #000, -1px 0 2px #000', pointerEvents: 'none'}}>{subtitle.text}</div>)}
    </div>
    </div>
    <div className="preview-transport">
      <div className="preview-buttons">
        <button type="button" title="上一帧" aria-label="上一帧" onClick={() => step(-1)} disabled={currentFrame === 0}>‹</button>
        <button type="button" className="preview-play" title={playing ? '暂停工程' : '播放工程'}
          aria-label={playing ? '暂停工程' : '播放工程'} onClick={togglePlaying}>{playing ? 'Ⅱ' : '▶'}</button>
        <button type="button" title="下一帧" aria-label="下一帧" onClick={() => step(1)} disabled={currentFrame >= lastFrame}>›</button>
      </div>
      <span className="preview-timecode"><b>{previewTimecode(currentFrame, document.fps)}</b><span> / {previewTimecode(document.duration_frames, document.fps)}</span></span>
      <input className="preview-progress" type="range" aria-label="预览进度" min={0} max={lastFrame} step={1}
        value={currentFrame} onChange={event => {
          onPlayingChange(false);
          onFrame(Number(event.target.value));
        }} />
      <span className="preview-frame-count">{currentFrame + 1} / {document.duration_frames} 帧</span>
    </div>
    <div className="preview-note" role="status">
      {problem || (missingAsset ? '当前片段的素材未找到，请重新导入。' : overlap ? '同轨片段重叠，当前试听首个片段；请调整后导出。'
        : otherStream ? '浏览器试听使用媒体默认音视频流，所选其他流请以导出为准。'
          : '工程即时预览 · 字体、切点与混音细节以导出成片为准')}
    </div>
  </div>;
}
