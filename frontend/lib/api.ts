import type {Bootstrap,ProjectRecord,ProjectSummary,Snapshot,Job,QcReport,Asset,ProjectDocument,SubtitleProposal} from './types';
import type {LibraryContext} from './library';
export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? 'http://127.0.0.1:8031').replace(/\/$/,'');
export function mediaUrl(url:string){return url.startsWith('/api/v1/') ? `${API_BASE}${url}` : url;}
export class ApiError extends Error {constructor(public status:number,public code:string,message:string){super(message)}}
export async function request<T>(path:string, options:RequestInit={}):Promise<T>{
 const response=await fetch(`${API_BASE}/api/v1${path}`,{...options,headers:{'X-Studio-Client':'1',...(options.body instanceof FormData?{}:{'Content-Type':'application/json'}),...options.headers}});
 const body=await response.json().catch(()=>null);
 if(!response.ok){const details=Array.isArray(body?.error?.details)?body.error.details.map((d:{message?:string;msg?:string})=>d.message??d.msg).filter(Boolean).join('；'):'';throw new ApiError(response.status,body?.error?.code??'HTTP_ERROR',(body?.error?.message??`请求失败 (${response.status})`)+(details?`：${details}`:''));}
 return body as T;
}
const post=<T>(path:string,body:unknown={})=>request<T>(path,{method:'POST',body:JSON.stringify(body)});
export const api={
 bootstrap:()=>request<Bootstrap>('/bootstrap'),
 context:(id:string)=>request<LibraryContext>(`/library/context/${encodeURIComponent(id)}`),
 demo:()=>post<{project_id:string}>('/demo'),
 projects:()=>request<{projects:ProjectSummary[]}>('/projects'),
 createProject:(name?:string)=>post<ProjectRecord>('/projects',{...(name===undefined?{}:{name}),request_id:crypto.randomUUID()}),
 project:(id:string)=>request<ProjectRecord>(`/projects/${id}`),
 save:(id:string,document:ProjectDocument,revision:number,requestId:string)=>request<ProjectRecord>(`/projects/${id}`,{method:'PUT',body:JSON.stringify({document,expected_revision:revision,request_id:requestId})}),
 subtitleProposal:(id:string,revision:number,transcriptId:string,transcriptRevision:number,clipId:string,segmentIds:string[])=>post<SubtitleProposal>(`/projects/${id}/subtitle-proposals`,{expected_revision:revision,transcript_id:transcriptId,transcript_revision:transcriptRevision,clip_id:clipId,segment_ids:segmentIds}),
 snapshots:(id:string)=>request<{snapshots:Snapshot[]}>(`/projects/${id}/snapshots`),
 restore:(id:string,revision:number,expected_revision:number)=>post<ProjectRecord>(`/projects/${id}/restore`,{revision,expected_revision,request_id:crypto.randomUUID()}),
 qc:(id:string)=>post<QcReport>(`/projects/${id}/qc`),
 render:(id:string,revision:number)=>post<Job>(`/projects/${id}/renders`,{expected_revision:revision,request_id:crypto.randomUUID()}),
 jobs:()=>request<{jobs:Job[]}>('/jobs'),
 cancel:(id:string)=>post<Job>(`/jobs/${id}/cancel`),
 retry:(id:string)=>post<Job>(`/jobs/${id}/retry`),
 upload:(file:File)=>{const form=new FormData();form.append('file',file);return request<Asset>('/assets',{method:'POST',body:form})},
};
