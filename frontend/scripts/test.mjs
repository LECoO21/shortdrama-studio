import {build} from 'esbuild';
import {spawnSync} from 'node:child_process';
import {mkdtemp,rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
const dir=await mkdtemp(path.join(os.tmpdir(),'studio-editor-tests-'));
try{await build({entryPoints:['tests/editor.test.ts','tests/timeline-editing.test.ts','components/CompositionPreview.time.test.ts','tests/chatgpt.test.ts','tests/transcripts.test.ts','tests/library.test.ts','tests/uploads.test.ts','tests/subtitle-segmentation.test.ts','tests/subtitle-binding.test.ts'],bundle:true,platform:'node',format:'esm',target:'node22',outdir:dir,outExtension:{'.js':'.mjs'},entryNames:'[name]'});const result=spawnSync(process.execPath,['--test',...['editor.test.mjs','timeline-editing.test.mjs','CompositionPreview.time.test.mjs','chatgpt.test.mjs','transcripts.test.mjs','library.test.mjs','uploads.test.mjs','subtitle-segmentation.test.mjs','subtitle-binding.test.mjs'].map(name=>path.join(dir,name))],{stdio:'inherit'});process.exitCode=result.status??1;}finally{await rm(dir,{recursive:true,force:true})}
