"use strict";
const by=id=>document.getElementById(id);
let model=null;
let activation=new URLSearchParams(location.hash.slice(1)).get("activate");
const passwordInput = by("password");
const passwordHint = "密码长度为 8 至 256 个字符，至少包含一个字母和一个数字。";
if (activation) {
  history.replaceState(null, "", location.pathname);
  document.querySelector("#login h1").textContent = "设置管理员密码";
  document.querySelector("#login p").textContent = passwordHint;
  document.querySelector("#loginForm button").textContent = "设置密码并登录";
  passwordInput.autocomplete = "new-password";
  passwordInput.minLength = 8;
  passwordInput.pattern = String.raw`(?=[\s\S]*\p{L})(?=[\s\S]*\p{Nd})[\s\S]{8,256}`;
  passwordInput.title = passwordHint;
}
function element(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function message(text,err=false){by("message").textContent=text;by("message").className=err?"error":"";}
function layout(authed){by("login").hidden=authed;by("board").hidden=!authed;by("logout").hidden=!authed;}
async function api(path,data){const opts=data===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)};const r=await fetch("/api/admin/"+path,opts);const v=await r.json();if(r.status===401)layout(false);if(!r.ok)throw Error(v.error||"请求失败");return v;}
function guarded(fn){return async ev=>{ev.preventDefault();const ctl=ev.submitter||ev.currentTarget;ctl.disabled=true;try{await fn();}catch(e){message(e.message,true);}finally{ctl.disabled=false;}};}
function field(text,input){const label=element("label",text);label.append(input);return label;}
function device(d){
 const entry=element("details",undefined,"device"),title=element("summary"),badge=element("span",d.disabled?"已停用":d.online?"在线":"离线","badge"+(d.online?"":" wait"));title.append(element("span",d.name),badge);entry.append(title);
 const summary=element("dl");const fact=(a,b)=>summary.append(element("dt",a),element("dd",b));
 const r=d.status||{};fact("实际线路",({lan:"家庭直连",vps:"VPS 中继"})[r.route]||"待设备上报");
 fact("传输程序",({legacy:"旧中继",managed:"新版",idle:"入口空闲",other:"其他程序"})[r.kind]||"待确认");
 fact("Drive 活动连接",Number.isInteger(r.connections)?String(r.connections):"待设备上报");
 fact("客户端",[({win32:"Windows",darwin:"macOS",linux:"Linux"})[r.platform],r.version&&"v"+r.version].filter(Boolean).join(" · ")||"待设备上报");
 fact("最后上报",d.seen?new Date(d.seen*1000).toLocaleString():"尚未上报");
 fact("配置版本",d.ack===d.rev?"设备已保存 v"+d.rev:"等待设备确认 v"+d.rev);
 fact("新设备 SSH 入口",d.ssh_granted?"已授权":"待授权");entry.append(summary);
 if(!d.disabled){const frm=element("form"), mode=element("select");
 for(const [v,t] of [["auto","家庭直连优先"],["relay","始终走 VPS"]]){const opt=element("option",t);opt.value=v;mode.append(opt);}mode.value=d.policy.mode;frm.append(field("线路策略",mode));
 const inputs={};const advanced=element("details");advanced.append(element("summary","家庭网络参数"));
 for(const [key,label] of Object.entries({lan_ip:"NAS 局域网 IPv4",nas_name:"NAS 证书名称",cert_sha256:"证书 SHA-256",drive_port:"Drive 端口",verify_port:"DSM HTTPS 端口"})){const el=element("input");el.value=d.policy[key];if(key.endsWith("port")){el.type="number";el.min=1;el.max=65535;}inputs[key]=el;advanced.append(field(label,el));}
 frm.append(advanced,element("button","保存设备策略"));frm.onsubmit=guarded(async()=>{const policy={mode:mode.value};for(const [k,v] of Object.entries(inputs))policy[k]=k.endsWith("port")?Number(v.value):v.value.trim();await api("policy",{id:d.id,revision:d.rev,policy});await refresh();message("配置已保存，设备下次上报时接收。");});entry.append(frm);
 const permissions=element("details");permissions.append(element("summary","SSH 公钥与设备权限"),element("pre",d.pub));
 const grant=element("button",d.ssh_granted?"撤销新连接授权":"授权 SSH 传输","secondary");
 grant.onclick=guarded(async()=>{await api("grant",{id:d.id,allow:!d.ssh_granted});await refresh();message("已保存，下次 SSH 认证时生效。");});
 const off=element("button","停用设备管理","quiet");off.onclick=guarded(async()=>{if(confirm("停用管理凭证和新入口的 SSH 授权？已建立的连接会在断开后结束。")){await api("disable",{id:d.id});await refresh();}});permissions.append(grant,off);entry.append(permissions);}
 return entry;
}
async function refresh(){model=await api("overview");layout(true);const devs=model.devices;const active=devs.filter(d=>d.online).length;by("summary").textContent=devs.length?active+" 台设备在线":"等待第一台设备";by("total").textContent=devs.length;by("online").textContent=active;by("pending").textContent=devs.filter(d=>!d.disabled&&d.rev!==d.ack).length;by("version").textContent="Drive Relay · v"+model.version;by("download").hidden=!model.download;by("devices").replaceChildren(...(devs.length?devs.map(device):[element("p","生成配对码即可接入第一台电脑。","empty")]));}
by("loginForm").onsubmit = guarded(async () => {
  const password = passwordInput.value;
  if (activation) {
    await api("activate", {code: activation, password});
    activation = null;
    for (const attribute of ["minlength", "pattern", "title"]) passwordInput.removeAttribute(attribute);
    passwordInput.autocomplete = "current-password";
    document.querySelector("#login p").textContent = "登录以查看设备、设置线路和添加新电脑。";
    document.querySelector("#login h1").textContent = "管理员登录";
    document.querySelector("#loginForm button").textContent = "登录管理台";
  }
  await api("login", {password});
  passwordInput.value = "";
  message("");
  await refresh();
});
by("logout").onclick=guarded(async()=>{await api("logout",{});by("ticket").hidden=true;by("code").textContent="";by("devices").replaceChildren();layout(false);message("");});
by("invite").onsubmit=guarded(async()=>{const r=await api("invite",{name:by("name").value});by("code").textContent=r.code;by("expiry").textContent="有效至 "+new Date(r.expires*1000).toLocaleTimeString()+"，限一台设备。";by("ticket").hidden=false;});
by("copy").onclick=guarded(async()=>{await navigator.clipboard.writeText(model.origin+"\n"+by("code").textContent);message("已复制管理台地址和配对码。");});
by("refresh").onclick=guarded(async()=>{await refresh();message("已刷新。");});
refresh().catch(e=>{layout(false);if(!e.message.includes("登录"))message(e.message,true);});
