import type {Asset, Clip, ProjectDocument, Rational} from '../lib/types';

/** Convert integer PTS without first losing its low bits to a JS number. */
export function ptsSeconds(pts: string, base: Rational): number {
  const numerator = BigInt(pts) * BigInt(base.num);
  const denominator = BigInt(base.den);
  return Number(numerator / denominator) + Number(numerator % denominator) / base.den;
}

export function sourceTime(clip: Clip, asset: Asset, frame: number, fps: Rational): number {
  // HTML media uses the asset's common presentation origin, not the selected
  // stream's first PTS: subtracting stream.start_pts would erase A/V delay.
  const origin = typeof asset.metadata.start_seconds === 'number' ? asset.metadata.start_seconds : 0;
  return Math.max(0, ptsSeconds(clip.source.in_pts, clip.source.time_base) - origin
    + (frame - clip.timeline_start_frame) * fps.den / fps.num);
}

export function clipGain(clip: Clip, frame: number): number {
  const local = frame - clip.timeline_start_frame;
  if (clip.muted || local < 0 || local >= clip.duration_frames) return 0;
  let envelope = 1;
  if (clip.fade_in_frames > 0) envelope = Math.min(envelope, local / clip.fade_in_frames);
  if (clip.fade_out_frames > 0) envelope = Math.min(envelope, (clip.duration_frames - local) / clip.fade_out_frames);
  return Math.pow(10, clip.gain_db / 20) * Math.max(0, envelope);
}

export function enabledClips(document: ProjectDocument, kind: 'video' | 'audio'): Clip[] {
  const enabled = new Set(document.tracks.filter(track => track.enabled && track.kind === kind).map(track => track.id));
  return document.clips.filter(clip => enabled.has(clip.track_id))
    .sort((a, b) => a.timeline_start_frame - b.timeline_start_frame || a.id.localeCompare(b.id));
}

export function clipAtFrame(clips: Clip[], frame: number): Clip | undefined {
  return clips.find(clip => frame >= clip.timeline_start_frame && frame < clip.timeline_start_frame + clip.duration_frames);
}

export function previewTimecode(frame: number, fps: Rational): string {
  const milliseconds = Math.floor(Math.max(0, frame) * fps.den / fps.num * 1000);
  const minutes = Math.floor(milliseconds / 60000);
  const seconds = Math.floor(milliseconds / 1000) % 60;
  const centiseconds = Math.floor(milliseconds % 1000 / 10);
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(centiseconds).padStart(2, '0')}`;
}

/** rAF's timestamp can precede the performance.now() used to start playback. */
export function previewClockTick(startFrame: number, startTime: number, timestamp: number, fps: number, durationFrames: number) {
  const elapsed = Math.max(0, timestamp - startTime);
  const lastFrame = Math.max(0, durationFrames - 1);
  const origin = Math.max(0, Math.min(lastFrame, startFrame));
  const frame = Math.max(0, Math.min(lastFrame, origin + Math.floor(elapsed * fps / 1000)));
  return {frame, elapsed, ended: elapsed >= (durationFrames - origin) / fps * 1000};
}
