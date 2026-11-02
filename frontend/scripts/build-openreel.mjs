import {build} from 'esbuild';
import {readFile,readdir,writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import path from 'node:path';
const root=path.resolve('vendor/openreel');
const source=path.join(root,'upstream/packages/core/src');
await build({stdin:{contents:`export {ActionExecutor} from './actions/action-executor'; export {ActionHistory} from './actions/action-history';`,resolveDir:source,loader:'ts'},bundle:true,format:'esm',platform:'browser',target:'es2022',outfile:path.join(root,'actions.mjs'),banner:{js:'/*! OpenReel MIT — Augustus Otu and Contributors. See upstream/LICENSE and PROVENANCE.md. */'},plugins:[{name:'stage-one-handlers',setup(b){b.onLoad({filter:/actions\/handlers\/index\.ts$/},()=>({contents:'import "./clip-timing";',loader:'ts'}));}}]});
async function files(dir){const out=[];for(const e of await readdir(dir,{withFileTypes:true})){const f=path.join(dir,e.name);if(e.isDirectory())out.push(...await files(f));else out.push(f);}return out;}
const manifest={commit:'367d7e90b61b5181f9861b852bb8b9d7a7d2954a',files:{}};
for(const f of await files(path.join(root,'upstream'))) manifest.files[path.relative(root,f)]=createHash('sha256').update(await readFile(f)).digest('hex');
manifest.files['actions.mjs']=createHash('sha256').update(await readFile(path.join(root,'actions.mjs'))).digest('hex');
await writeFile(path.join(root,'SHA256.json'),JSON.stringify(manifest,null,2)+'\n');
