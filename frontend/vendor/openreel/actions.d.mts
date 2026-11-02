export interface CoreAction {type:string;id:string;timestamp:number;params:Record<string,unknown>}
export interface CoreResult {success:boolean;actionId?:string;error?:{code:string;message:string}}
export class ActionHistory {canUndo():boolean;canRedo():boolean;clear():void;startGroup(description:string):void;endGroup():void}
export class ActionExecutor {
 constructor(history?:ActionHistory);
 execute(action:CoreAction,project:object):Promise<CoreResult>;
 undo(project:object):Promise<CoreResult>;
 redo(project:object):Promise<CoreResult>;
 getHistory():ActionHistory;
}
