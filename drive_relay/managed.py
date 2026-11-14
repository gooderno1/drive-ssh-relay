"""Outbound desktop management. The transport service has a separate lifecycle."""

from dataclasses import asdict
import json
import os
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPRedirectHandler, Request
from . import onboard, runtime, services
from .config import atomic, load
from .fleet import origin
from .routing import Routes, settings, save


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def write(path, value):
    atomic(path, (json.dumps(value, indent=2) + "\n").encode())


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def request(url, action, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request(origin(url) + "/api/device/" + action, data=json.dumps(body).encode(), headers=headers)
    try:
        with build_opener(NoRedirect()).open(req, timeout=7) as res:
            data = res.read(24577)
            if len(data) > 24576:
                raise ValueError("管理台响应过大")
            return json.loads(data)
    except HTTPError as e:
        if e.code in (401, 403):
            raise ValueError("配对码已过期、已使用或设备管理权限已撤销") from e
        raise ValueError("管理台请求失败，HTTP " + str(e.code)) from e


def join(state, url, code, port=16690):
    url = origin(url)
    if not isinstance(code, str) or not 24 <= len(code) <= 96:
        raise ValueError("请粘贴管理台生成的完整配对码")
    invitation = request(url, "preview", {"code": code})
    profile, routes = onboard.parse_invite(invitation, local_port=port)
    if (state / "profile.json").is_file():
        existing = load(state)
        same_target = (existing.role, existing.host, existing.host_key, existing.bridge_port) == (
            "client",
            profile.host,
            profile.host_key,
            profile.bridge_port,
        )
        preserved_legacy = (state / "legacy-client.json").is_file()
        if not same_target or (existing.ssh_port != profile.ssh_port and not preserved_legacy):
            raise ValueError("接入文件与本机现有 VPS 身份不一致")
    else:
        result = onboard.setup(state, invitation, port)
        if result["status"] != "ready_for_authorization":
            raise ValueError("请先完成本机环境检查")
    with runtime.exclusive(state / "fleet-enroll.lock"):
        if (state / "fleet.json").exists():
            current = read(state / "fleet.json")
            if current["url"] != url:
                raise ValueError("这台设备已配对其他管理台")
            return {"name": current["name"], "paired": True}
        pending = state / "fleet-pending.json"
        entry = read(pending) if pending.exists() else {}
        if entry.get("url") != url or entry.get("code") != code:
            entry = dict(url=url, code=code, token=secrets.token_urlsafe(32))
            write(pending, entry)
        pub = (state / "identity.pub").read_text().strip()
        result = request(url, "pair", dict(code=code, public_key=pub, token=entry["token"]))
        write(
            state / "fleet.json",
            dict(url=url, token=entry["token"], id=result["id"], name=result["name"]),
        )
        pending.unlink(missing_ok=True)
    return {"name": result["name"], "paired": True}


def sync(state):
    binding = read(state / "fleet.json")
    contract = state / "fleet-applied.json"
    previous = read(contract) if contract.exists() else {}
    current = onboard.describe(state)
    active = current.get("transport", {})
    report = {k: active.get(k) for k in ("kind", "route", "connections")}
    report.update(
        platform=sys.platform,
        phase=current.get("runtime", {}).get("phase", "unknown"),
        version=current["version"],
    )
    previous_revision = previous.get("revision", 0)
    result = request(
        binding["url"], "checkin", dict(status=report, ack=previous_revision), binding["token"]
    )
    revision = result["revision"]
    routes = Routes(**result["policy"])
    if revision != previous_revision:
        save(state, load(state), routes)
        write(contract, dict(revision=revision, applied_at=int(time.time())))
        request(binding["url"], "checkin", dict(status=report, ack=revision), binding["token"])
    return {"revision": revision, "transport": report.get("kind"), "route": report.get("route")}


def watch(state):
    with runtime.exclusive(state / "fleet.lock"):
        while True:
            try:
                value = sync(state)
                value["ok"] = True
            except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as e:
                value = {"ok": False, "reason": type(e).__name__}
            write(state / "fleet-last.json", dict(value, at=int(time.time())))
            time.sleep(45)


def service(state, action):
    if not (state / "fleet.json").is_file():
        raise ValueError("请先配对管理台")
    with runtime.exclusive(state / "fleet-service.lock"):
        archive = services.package(state) if action == "install" else None
        if os.name == "nt":
            services.win_service(action, state, archive, "management")
        elif sys.platform == "darwin":
            services.mac_service(action, state, archive, "management")
        else:
            raise ValueError("Linux 请使用服务管理器运行 fleet watch")
    return {"action": action, "component": "management"}
