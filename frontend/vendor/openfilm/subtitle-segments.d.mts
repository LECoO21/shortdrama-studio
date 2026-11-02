export type SubtitleMark = {index: number; startMs: number};
export type SegmentLine = {text: string; startMs: number; endMs: number; marks?: readonly SubtitleMark[]};
export type SubtitlePiece = {
  text: string; startMs: number; endMs: number; marks?: SubtitleMark[];
  part: number; from: number; to: number;
};
export type SubtitleSegmentRules = Readonly<{
  maxCells: number; maxChars: number; minUnits: number; pauseMs: number;
  minMs: number; maxMs: number; holdMs: number; closeGapMs: number; msPerCell: number;
  weight: Readonly<Record<string, number>>; cost: Readonly<Record<string, number>>;
}>;
export const SUBTITLE_SEGMENT_RULES: SubtitleSegmentRules;
export function segmentLine(line: SegmentLine, rules?: SubtitleSegmentRules): SubtitlePiece[];
export function subtitleCells(text: string): number;
export function subtitleEnd(text: string): string;
export function alignTranslation(text: string, pieces: readonly {startMs: number; endMs: number}[], rules?: SubtitleSegmentRules): {text: string; from: number; to: number}[];
export function replacePiece(line: SegmentLine, piece: {from: number; to: number}, text: string, startMs?: number): {text: string; marks?: SubtitleMark[]};
