import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

const edge = process.env.EDGE_PATH || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const extension = readFileSync(new URL("../web/unified_video.js", import.meta.url), "utf8")
    .replace('import { app } from "../../scripts/app.js";', "const app = window.app;");

// Exercise the actual extension in Chromium, with both supported textarea mounting styles.
const fixture = `<!doctype html><meta charset="utf-8"><title>Prompt picker test</title>
<style>body{background:#333;color:white;padding:40px}textarea{width:420px;height:120px}</style>
<pre id="results">pending</pre><script type="module">
const checks = [];
function check(condition, name) { if (!condition) throw Error(name); checks.push(name); }
const nodes = new Map();
window.app = { graph: { getNodeById: id => nodes.get(Number(id)) }, registerExtension(extension) { this.extension = extension; } };
try {
    await import('/extension.js');
    app.extension.setup();
    const profile = { ratios:['16:9'], resolutions:['720p'], duration:{min:4,max:15,default:4}, images:3, audio_limit:3, videos:3, sound:true, qiaomo:true };
    class Node {
        constructor(id, vue) {
            this.id = id; this.size = [420,600];
            this.inputs = ['参考图1','参考音频1','参考视频1'].map((name,i) => ({name,type:['IMAGE','AUDIO','VIDEO'][i],link:i+1}));
            this.graph = { links:{}, getLink(id) { return this.links[id]; }, getNodeById: id => ({title:'素材'+id,widgets:[{name:'image',value:'角色.png'}]}) };
            this.inputs.forEach((input,index) => {this.graph.links[input.link]={id:input.link,origin_id:100+index,target_slot:index};});
            this.widgets = [['模型','mini'],['比例','16:9'],['分辨率','720p'],['时长秒数',4],['生成声音',true],['提示词','']]
                .map(([name,value])=>({name,value,type:'text',options:{}}));
            this.root=document.createElement('div'); this.root.dataset.nodeId=id;
            this.root.innerHTML='<label for="prompt'+id+'">提示词</label><br><textarea id="prompt'+id+'"></textarea>';
            document.body.append(this.root); this.textarea=this.root.querySelector('textarea');
            const widget=this.widgets.find(w=>w.name==='提示词');
            if (!vue) widget.element=this.root;
            this.textarea.addEventListener('input',()=>{widget.value=this.textarea.value;});
            nodes.set(id,this); this.onNodeCreated();
        }
        addInput(name,type){this.inputs.push({name,type,link:null});}
        removeInput(i){this.inputs.splice(i,1);}
        setSize(size){this.size=size;} computeSize(){return [420,600];} setDirtyCanvas(){}
    }
    app.extension.beforeRegisterNodeDef(Node,{name:'ZiyuanUnifiedVideoNode',input:{required:{模型:[['mini'],{ziyuan_profiles:{mini:profile}}]},optional:{参考图1:['IMAGE'],参考图2:['IMAGE'],参考图3:['IMAGE']}}});
    const options=()=>[...document.querySelectorAll('.ziyuan-reference-picker [role=option]')];
    const key=(textarea,key)=>textarea.dispatchEvent(new KeyboardEvent('keydown',{key,bubbles:true,cancelable:true}));
    const type=(textarea,text,cursor=text.length)=>{textarea.focus();textarea.value=text;textarea.setSelectionRange(cursor,cursor);textarea.dispatchEvent(new InputEvent('input',{bubbles:true}));};
    for (const vue of [false,true]) {
        const mode=vue?'Vue':'legacy';
        const node=new Node(vue?2:1,vue);
        await Promise.resolve();
        node.onDrawForeground();
        const textarea=node.textarea;
        type(textarea,'用@和背景',2);
        check(options().length===3,mode+' lists connected image/audio/video only');
        check(options()[0].textContent.includes('素材100')&&options()[0].textContent.includes('角色.png'),mode+' shows source label');
        key(textarea,'ArrowDown'); key(textarea,'Enter');
        check(textarea.value==='用@音频1 和背景',mode+' keyboard insertion preserves suffix');
        check(textarea.selectionStart===6&&node.widgets.find(w=>w.name==='提示词').value===textarea.value,mode+' cursor and widget synchronize');
        check(!document.querySelector('.ziyuan-reference-picker'),mode+' picker closes after selection');
        type(textarea,'@视频');
        check(options().length===1&&options()[0].textContent.startsWith('@视频1'),mode+' filters by type');
        options()[0].dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,cancelable:true}));
        check(textarea.value==='@视频1 ',mode+' pointer selection');
        type(textarea,'@图片'); check(options().length===1,mode+' accepts image alias');
        key(textarea,'Escape'); check(!document.querySelector('.ziyuan-reference-picker'),mode+' escape closes');
        type(textarea,'@');
        node.inputs.find(i=>i.name==='参考音频1').link=null;
        node.onConnectionsChange(); await Promise.resolve();
        check(options().length===2,mode+' refreshes after disconnection');
        textarea.blur(); check(!document.querySelector('.ziyuan-reference-picker'),mode+' blur closes');
        type(textarea,'@');
        node.onRemoved(); check(!document.querySelector('.ziyuan-reference-picker'),mode+' removal cleans up');
        node.root.remove();
    }
    const empty = new Node(3,false); await Promise.resolve();
    empty.inputs.forEach(i=>i.link=null); empty.onConnectionsChange(); await Promise.resolve();
    type(empty.textarea,'@');
    check(options().length===0&&document.querySelector('.ziyuan-reference-picker').textContent.includes('请先连接'), 'empty connections show guidance');
    empty.onRemoved(); empty.root.remove();
    document.querySelector('#results').textContent=JSON.stringify({passed:checks.length,checks});
    document.querySelector('#results').dataset.status='passed';
} catch(error) {
    document.querySelector('#results').textContent=error.stack;
    document.querySelector('#results').dataset.status='failed';
}
</script>`;

test("prompt picker works in Chromium with legacy and Vue textareas", { skip: !existsSync(edge), timeout: 60000 }, async () => {
    const server = createServer((request,response) => {
        response.setHeader("Content-Type", request.url === "/extension.js" ? "text/javascript; charset=utf-8" : "text/html; charset=utf-8");
        response.end(request.url === "/extension.js" ? extension : fixture);
    });
    await new Promise(resolve => server.listen(0,"127.0.0.1",resolve));
    const profile = mkdtempSync(join(tmpdir(), "ziyuan-picker-test-"));
    try {
        const output = await new Promise((resolve,reject) => {
            const child = spawn(edge,["--headless=new","--disable-gpu","--no-first-run","--no-default-browser-check",
                "--user-data-dir="+profile,"--dump-dom","--virtual-time-budget=3000",`http://127.0.0.1:${server.address().port}`], {windowsHide:true});
            let stdout="",stderr="";
            const timer=setTimeout(()=>{child.kill();reject(Error('Browser test timed out'));},45000);
            child.stdout.on('data',chunk=>{stdout+=chunk;});
            child.stderr.on('data',chunk=>{stderr+=chunk;});
            child.on('error',reject);
            child.on('close',code=>{clearTimeout(timer);code===0?resolve(stdout):reject(Error(stderr));});
        });
        const result = /<pre id="results"[^>]*>([\s\S]*?)<\/pre>/.exec(output)?.[0];
        assert.match(result || output, /data-status="passed"/);
        assert.match(result, /"passed":25/);
    } finally {
        server.closeAllConnections();
        await new Promise(resolve=>server.close(resolve));
        // This is the isolated browser profile created above, never the user's browser data.
        rmSync(profile,{recursive:true,force:true,maxRetries:5,retryDelay:100});
    }
});
