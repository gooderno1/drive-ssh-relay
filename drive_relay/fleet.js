"use strict";
const by = id => document.getElementById(id);
const password = by("password");
let activation = new URLSearchParams(location.hash.slice(1)).get("activate");
let overview = null, selectedId = null, loading = 0;
const loginCaption = by("loginIntro").textContent;
if (activation) {
  history.replaceState(null, "", location.pathname);
  by("loginTitle").textContent = "设置管理员密码";
  by("loginIntro").textContent = "密码长度为 8 至 256 个字符，至少包含一个字母和一个数字。";
  by("loginButton").textContent = "设置密码并登录";
  password.autocomplete = "new-password";
  password.minLength = 8;
  password.pattern = String.raw`(?=[\s\S]*\p{L})(?=[\s\S]*\p{Nd})[\s\S]{8,256}`;
  password.title = by("loginIntro").textContent;
}
function make(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function notice(text, bad=false, target="message") {
  const box = by(target);
  box.textContent = text;
  box.classList.toggle("error", bad);
  box.hidden = !text;
}
function showLogin() {
  overview = null; selectedId = null;
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  by("devices").replaceChildren(); by("detail").replaceChildren();
  by("board").hidden = true; by("logout").hidden = true; by("login").hidden = false;
}
async function api(path, data) {
  const opts = data === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data)};
  const response = await fetch("/api/admin/"+path, opts);
  const value = await response.json();
  if (response.status === 401) showLogin();
  if (!response.ok) throw Error(value.error || "请求失败");
  return value;
}
function action(fn, target="message") {
  return async ev => {
    ev.preventDefault();
    const button = ev.submitter || ev.currentTarget;
    button.disabled = true;
    try { await fn(); }
    catch (e) { notice(e.message, true, by("login").hidden ? target : "message"); }
    finally { button.disabled = false; }
  };
}
const routeNames = {lan:"家庭直连", vps:"VPS 中继"};
const routeModes = {auto:"自动 · 在家直连，异地走 VPS", relay:"手动 · VPS 中继", lan:"手动 · 仅 NAS 直连"};
const platforms = {darwin:"macOS", win32:"Windows", linux:"Linux"};
const onlineText = d => d.disabled ? "已停用" : d.online ? "在线" : d.seen ? "离线" : "待上报";
const localDate = t => t ? new Date(t*1000).toLocaleString("zh-CN",{hour12:false}) : "尚未上报";
function routeOf(d) {
  if(d.status.lan_issue) return d.status.lan_issue==="nas_identity_mismatch"?"NAS 身份校验失败":"NAS 直连不可达";
  return routeNames[d.status.route] || ({idle:"入口空闲",other:"待核实"})[d.status.kind] || "待上报";
}
function modeOf(d) {
  if (d.status.kind === "legacy") return "固定中继程序";
  if (d.status.kind === "managed") return "选路程序";
  return ({idle:"入口空闲",other:"其他程序持有入口"})[d.status.kind] || "待设备确认";
}
function presence(d) { return make("span",onlineText(d),"chip"+(d.online?"":" off")); }
function hero(d) {
  by("routeLabel").textContent = d ? d.name : "所选设备的线路";
  by("presence").textContent = d ? onlineText(d) : "待接入";
  by("presence").classList.toggle("off", !d?.online);
  by("routeTitle").textContent = d ? routeOf(d) : "等待设备接入";
  const route = d?.status.route;
  const explanation = d?.status.kind==="legacy" ? "固定中继程序负责传输" :
    d?.status.kind==="managed" ? "选路程序负责传输" : null;
  const parts = [];
  if (d && !d.online && d.seen) parts.push("上次上报的线路");
  if (explanation) parts.push(explanation);
  if (d && Number.isInteger(d.status.connections)) parts.push("Drive 活动连接 "+d.status.connections+" 条");
  by("routeNote").textContent = parts.join(" · ") || (d ? "等待设备确认传输线路。" : "点击「接入设备」连接第一台电脑。");
  by("path").hidden = !(route && routeNames[route]);
  by("middle").textContent = route==="lan" ? "家庭局域网" : "VPS 中继";
}
function tableCell(label, value, cls) {
  const td=make("td",undefined,cls);
  if (typeof value === "string") td.textContent=value; else td.append(value);
  if(label) td.dataset.label=label;
  return td;
}
function row(d) {
  const tr=make("tr"); tr.dataset.id=d.id;tr.classList.toggle("selected",d.id===selectedId);
  const info=make("div");info.append(make("span",d.name,"device-title"),make("small",[platforms[d.status.platform],d.status.version&&"v"+d.status.version].filter(Boolean).join(" · ")||"客户端待上报"));
  const open=make("button","详情","linkbtn");
  open.type="button";open.setAttribute("aria-label","查看 "+d.name+" 的详情");open.onclick=()=>openDevice(d);
  const last=make("span",localDate(d.seen));last.title=localDate(d.seen);
  const route = d.online || !d.seen ? routeOf(d) : routeOf(d)+"（上次）";
  tr.append(tableCell("",info),tableCell("",presence(d)),tableCell("实际线路",route),
    tableCell("Drive 连接",Number.isInteger(d.status.connections)?String(d.status.connections):"—","metric"),
    tableCell("最后上报",last),tableCell("",open));
  return tr;
}
function render(v) {
  overview=v;
  by("login").hidden=true; by("board").hidden=false;by("logout").hidden=false;
  const ds=v.devices, active=ds.filter(d=>d.online).length, pending=ds.filter(d=>!d.disabled&&d.ack!==d.rev).length;
  let selected=ds.find(d=>d.id===selectedId) || ds.find(d=>d.online) || ds[0];
  selectedId=selected?.id || null;
  by("total").textContent=ds.length;by("online").textContent=active;by("pending").textContent=pending;
  by("pendingDot").classList.toggle("attention",pending>0);
  by("updated").textContent="刷新于 "+new Date().toLocaleTimeString("zh-CN",{hour12:false});
  by("listCount").textContent=ds.length+" 台设备";
  by("version").textContent="Drive Relay · v"+v.version;
  by("devices").replaceChildren(...ds.map(row));by("empty").hidden=!!ds.length;
  by("download").hidden=!v.download;hero(selected);
}
async function refresh() {
  const request=++loading;
  const data=await api("overview");
  if(request===loading)render(data);
}
function fact(list,a,b) {list.append(make("dt",a),make("dd",b));}
function disclosure(label) {const el=make("details");el.append(make("summary",label));return el;}
function field(label,input) {const el=make("label",label);el.append(input);return el;}
function policyForm(d) {
  const section=disclosure("线路策略"),form=make("form"),mode=make("select");
  for(const [value,label] of Object.entries(routeModes)){
    const supported = value!=="lan" || d.status.supports_manual_lan;
    const opt=make("option",supported ? label : label+"（需更新客户端）");
    opt.value=value;opt.disabled=!supported;mode.append(opt);
  }
  mode.value=d.policy.mode;form.append(field("保存的策略",mode));
  const inputs={}, advanced=disclosure("家庭 NAS 参数");
  for(const [key,label] of Object.entries({lan_ip:"NAS 局域网 IPv4",nas_name:"NAS 证书名称",cert_sha256:"证书 SHA-256",drive_port:"Drive 端口",verify_port:"DSM HTTPS 端口"})){
    const input=make("input");input.value=d.policy[key]??"";
    if(key.endsWith("port")){input.type="number";input.min=1;input.max=65535;}
    inputs[key]=input;advanced.append(field(label,input));
  }
  form.append(advanced,make("button","保存策略","primary"));
  form.onsubmit=action(async()=>{
    const policy={mode:mode.value};
    for(const [key,el] of Object.entries(inputs))policy[key]=key.endsWith("port")?Number(el.value):el.value.trim();
    await api("policy",{id:d.id,revision:d.rev,policy});await refresh();openDevice(overview.devices.find(x=>x.id===d.id));
    notice("策略已保存，等待这台设备上报确认。",false,"detailStatus");
  },"detailStatus");section.append(form);return section;
}
function permissions(d) {
  const section=disclosure("SSH 公钥与设备权限");
  section.append(make("pre",d.pub));
  const grant=make("button",d.ssh_granted?"撤销新连接授权":"授权 SSH 传输","secondary");
  grant.onclick=action(async()=>{await api("grant",{id:d.id,allow:!d.ssh_granted});await refresh();openDevice(overview.devices.find(x=>x.id===d.id));notice("SSH 授权已更新。",false,"detailStatus");},"detailStatus");
  const off=make("button","停用设备","danger");
  off.onclick=action(async()=>{if(confirm("停用这台设备的管理和新 SSH 连接授权？")){await api("disable",{id:d.id});await refresh();openDevice(overview.devices.find(x=>x.id===d.id));}},"detailStatus");
  section.append(grant,off);return section;
}
function openDevice(d) {
  if(!d)return;
  selectedId=d.id;hero(d);
  for(const tr of by("devices").rows)tr.classList.toggle("selected",tr.dataset.id===selectedId);
  by("deviceName").textContent=d.name;
  const facts=make("dl",undefined,"facts");
  fact(facts,"上报状态",onlineText(d));fact(facts,d.online?"实际线路":"上次线路",routeOf(d));fact(facts,"传输程序",modeOf(d));
  fact(facts,"Drive 活动连接",Number.isInteger(d.status.connections)?d.status.connections+" 条":"未上报");
  fact(facts,"最后上报",localDate(d.seen));fact(facts,"策略确认",d.ack===d.rev?"设备已保存 v"+d.rev:"等待保存 v"+d.rev);
  fact(facts,"新设备 SSH 入口",d.disabled?"已停用":d.ssh_granted?"已授权":"待授权");
  if(d.status.mode && d.status.kind==="managed") fact(facts,"正在执行",routeModes[d.status.mode]);
  const detail=by("detail");detail.replaceChildren(facts);
  if(d.status.kind==="legacy")detail.append(make("p","这台电脑固定经 VPS 传输。家庭直连和外出自动切换要在这台电脑启用选路程序后才生效。","equipment-note"));
  if(!d.disabled)detail.append(policyForm(d),permissions(d));
  notice("",false,"detailStatus");
  if(!by("deviceDialog").open)by("deviceDialog").showModal();
}
by("loginForm").onsubmit=action(async()=>{
  const value=password.value;
  if(activation){
    await api("activate",{code:activation,password:value});activation=null;
    for(const attr of ["minlength","pattern","title"])password.removeAttribute(attr);
    password.autocomplete="current-password";by("loginTitle").textContent="管理员登录";by("loginIntro").textContent=loginCaption;by("loginButton").textContent="登录管理台";
  }
  await api("login",{password:value});password.value="";notice("");await refresh();
});
by("refresh").onclick=action(async()=>{await refresh();notice("");});
by("logout").onclick=action(async()=>{await api("logout",{});++loading;showLogin();notice("");});
by("openPair").onclick=()=>{notice("",false,"pairStatus");by("pairDialog").showModal();};
for(const el of document.querySelectorAll("dialog .close"))el.onclick=()=>el.closest("dialog").close();
by("pairDialog").onclose=()=>{by("ticket").hidden=true;by("code").textContent="";by("name").value="";};
by("invite").onsubmit=action(async()=>{
  const result=await api("invite",{name:by("name").value});
  by("code").textContent=result.code;by("expiry").textContent="有效至 "+localDate(result.expires)+"，限一台设备。";
  by("ticket").hidden=false;notice("",false,"pairStatus");
},"pairStatus");
by("copy").onclick=action(async()=>{await navigator.clipboard.writeText(overview.origin+"\n"+by("code").textContent);notice("已复制管理台地址和配对码。",false,"pairStatus");},"pairStatus");
if(activation)showLogin();
else refresh().catch(e=>{showLogin();if(!e.message.includes("登录"))notice(e.message,true);});
setInterval(()=>{if(overview&&!document.hidden&&!document.querySelector("dialog[open]"))refresh().catch(e=>notice(e.message,true));},45000);
