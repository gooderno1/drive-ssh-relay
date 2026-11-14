"use strict";
const $ = id => document.getElementById(id);
const sessionKey = location.hash.slice(1) || sessionStorage.getItem("relay-session");
if (location.hash) {
  sessionStorage.setItem("relay-session", sessionKey);
  history.replaceState(null, "", location.pathname);
}
const reasonText = {home_verified:"已核验局域网 NAS 身份和 Drive 端口",lan_unavailable:"局域网 NAS 不可达",nas_identity_mismatch:"局域网证书与可信身份不匹配",relay_selected:"配置为始终走 VPS",invalid_routing_settings:"线路配置无效，已回退",checking:"正在检查",key_rejected:"VPS 未授权设备公钥",host_key_mismatch:"VPS 主机指纹不符",local_port_conflict:"本机端口被占用",connection_refused:"连接被拒绝"};
let loaded = false;
function note(text, bad=false) { $("message").textContent=text; $("message").className=bad?"error":""; }
async function api(action, data={}) {
  const response = await fetch("/api/"+action,{method:"POST",headers:{"Content-Type":"application/json","Authorization":"Bearer "+sessionKey},body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw Error(result.error || "操作失败");
  return result;
}
function showChecks(checks) {
  const names={python:"Python",pythonw:"无窗口运行组件",scheduler:"任务计划程序",launchctl:"启动服务",drive_client:"Synology Drive Client",local_port:"本机入口端口"};
  $("checks").replaceChildren(...checks.map(c=>{const li=document.createElement("li");li.textContent=(names[c.id]||c.id)+" · "+(c.ok?"已就绪":c.action);return li;}));
}
function display(data, force=false) {
  $("version").textContent="Drive Relay · v"+data.version;
  $("localAdmin").hidden=!data.enrolled;
  const fleet=data.fleet||{};
  if(fleet.url && /^https:\/\//i.test(fleet.url)){
    $("hubLink").href=fleet.url;$("hubLink").hidden=false;
    if(!$("fleetUrl").value)$("fleetUrl").value=fleet.url;
  }
  $("pairForm").hidden=!!fleet.linked;
  if(fleet.linked)$("fleetInfo").textContent="已配对设备："+fleet.name+"。在设备管理台查看上报与配置版本。";
  $("onboarding").hidden=data.enrolled;
  $("statusCard").hidden=!data.enrolled;
  $("routing").hidden=!data.enrolled||data.role!=="client";
  if (!data.enrolled) return;
  const runtime=data.runtime;
  $("route").textContent=({lan:"局域网直连",vps:"VPS 中继",pending:"正在判断"})[runtime.route]||"—";
  $("service").textContent=({routing:"运行中",ssh_running:"运行中",inactive:"未运行",blocked:"等待处理",retrying:"重试中"})[runtime.phase]||runtime.phase;
  $("connections").textContent=runtime.connections??"—";
  $("reason").textContent=[runtime.route_reason,runtime.detail].filter(x=>x&&x!=="supervisor_not_running_or_stale").map(x=>reasonText[x]||x).join(" · ")||"—";
  $("switched").textContent=runtime.switched_at?new Date(runtime.switched_at).toLocaleString():"—";
  $("endpoint").textContent="127.0.0.1:"+data.local_port; $("vps").textContent=data.vps;
  $("deviceKey").textContent=data.device_public_key||"—";
  const actual=data.transport||{}, external=actual.kind==="legacy", other=actual.kind==="other";
  $("route").textContent=({vps:"VPS 中继",lan:"家庭局域网直连"})[actual.route]||(other?"入口由其他程序占用":"尚无可确认的线路");
  $("via").textContent=actual.route==="lan"?"家庭局域网":actual.route==="vps"?"VPS 中继":"等待连接";
  $("ownerBadge").textContent=({legacy:"固定中继程序",managed:"选路程序运行中",idle:"入口空闲",other:"入口已占用"})[actual.kind]||"状态待确认";
  $("ownerBadge").className="badge"+(other||actual.kind==="idle"?" wait":"");
  $("connections").textContent=actual.connections??"—";
  if(external)$("reason").textContent="本机固定通过 VPS 连接 NAS。";
  if(other)$("reason").textContent="检测到监听程序，其传输线路尚未核实。";
  if(external)$("service").textContent="固定中继";
  $("serviceHelp").textContent=external?"固定中继程序正在传输。启用选路程序后，才会执行家庭直连优先等策略。":other?"先确认现有入口的用途，再切换服务。":"选路程序可独立于此页面运行。";
  for(const btn of document.querySelectorAll("[data-op]"))btn.disabled=external||other||(btn.dataset.op==="stop"&&runtime.phase==="inactive");
  if (!loaded||force) { for(const [key,val] of Object.entries(data.routing)) $(key).value=val; loaded=true; toggle(); }
}
function toggle(){ $("lanFields").hidden=$("mode").value!=="auto"; }
$("mode").addEventListener("change",toggle);
const guarded=fn=>async event=>{event.preventDefault();const control=event.submitter||event.currentTarget;if(control.tagName==="BUTTON")control.disabled=true;try{await fn();}catch(e){note(e.message,true);}finally{if(control.tagName==="BUTTON")control.disabled=false;}};
const args=()=>({local_port:Number($("joinPort").value),drive_path:$("drivePath").value||null});
$("pairForm").onsubmit=guarded(async()=>{const r=await api("fleet-join",{url:$("fleetUrl").value.trim(),code:$("pairCode").value.trim(),local_port:Number($("joinPort").value)});$("pairCode").value="";display(r,true);note("设备已配对，管理后台已启动。");});
$("refresh").onclick=guarded(async()=>{display(await api("status"));note("状态已刷新");});
$("preflight").onclick=guarded(async()=>{const r=await api("preflight",args());showChecks(r.checks);note(r.ready?"本机环境已就绪":"请处理下列缺失项",!r.ready);});
$("join").onsubmit=guarded(async()=>{const file=$("invite").files[0];if(!file||file.size>16384)throw Error("请选择不超过 16 KiB 的管理员接入文件");const r=await api("setup",{...args(),invite:JSON.parse((await file.text()).replace(/^\uFEFF/,""))});if(r.checks){showChecks(r.checks);note("请先完成环境检查",true);return;}$("publicKey").hidden=false;$("key").textContent=r.device_public_key;display(await api("status"),true);note("接入配置已保存，请授权此设备公钥");});
$("form").onsubmit=guarded(async()=>{const data={};for(const key of ["mode","lan_ip","nas_name","cert_sha256","drive_port","verify_port"])data[key]=key.endsWith("port")?Number($(key).value):$(key).value.trim();const saved=await api("config",data);display(saved,true);note(saved.transport?.kind==="managed"?"配置已保存，后台将在下一轮检查应用":"配置已保存，新版路由服务启动后生效。");});
for(const b of document.querySelectorAll("[data-op]")) b.onclick=guarded(async()=>{display(await api(b.dataset.op));note("后台服务操作已完成");});
async function refresh(){try{display(await api("status"));}catch(e){note(e.message,true);}}
refresh();const polling=setInterval(()=>{if(!document.hidden)refresh();},4000);
$("closePanel").onclick=guarded(async()=>{await api("close");clearInterval(polling);for(const control of document.querySelectorAll("section,footer"))control.hidden=true;note("控制台已关闭。后台同步服务独立运行。");});
