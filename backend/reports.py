"""Stable report identity, independent of Desktop's changing engine PID and port."""
import hashlib
import json
import ntpath
import os
import re
import subprocess

from .database import encode


def canonical(value):
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in sorted(value.items()) if k != 'cardinality'}
    if isinstance(value, list):
        return sorted((canonical(v) for v in value), key=encode)
    return value


def schema_signature(context):
    # Ordering and value-count hints can change on reopen/refresh without changing meaning.
    # The live-connection fingerprint is deliberately left unchanged.
    return hashlib.sha256(encode(canonical(context)).encode()).hexdigest()


def normalize_name(name):
    name = re.sub(r'\.(?:pbix|pbip)$', '', (name or '').strip(), flags=re.I)
    return name.casefold()


def normalize_path(path):
    return ntpath.normcase(ntpath.normpath(path)) if path else ''


def report_from_window(title, command_line='', desktop_pid=None, *, verified_desktop=False):
    title = (title or '').strip()
    name = re.sub(r'\s*[-–—]\s*(?:Microsoft\s+)?Power BI Desktop(?:\s*\([^)]*\))?\s*$', '', title, flags=re.I).strip(' *')
    if (not name or (name == title and not verified_desktop) or
        normalize_name(name) in ('power bi desktop','microsoft power bi desktop','power bi') or
        re.fullmatch(r'(?:untitled|无标题|未命名)(?:\s*\d+)?',normalize_name(name))):
        return {}
    paths = re.findall(r'"([^"\r\n]+\.(?:pbix|pbip))"|(?<!\S)((?:[A-Za-z]:[\\/]|\\\\)[^\s"]+\.(?:pbix|pbip))(?=\s|$)', command_line, re.I)
    # A Desktop process can open a different file later. Its original command line must
    # not override the report currently shown by the window title.
    path = next((a or b for a, b in paths if normalize_name(ntpath.basename(a or b)) == normalize_name(name)), '')
    return {'name': re.sub(r'\.(?:pbix|pbip)$', '', name, flags=re.I), 'path': path,
            'source': 'window_title', 'desktop_pid': desktop_pid}


def _report_snapshot():
    if os.name != 'nt':
        return {}
    script = r'''
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$reportNodes = @(Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,Name -ErrorAction SilentlyContinue | Select-Object ProcessId,ParentProcessId)
$reportWindows = @(Get-CimInstance Win32_Process -Filter "Name='PBIDesktop.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
    $reportProcess = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
    [PSCustomObject]@{id=$_.ProcessId; title=$reportProcess.MainWindowTitle; command=$_.CommandLine}
})
@{nodes=$reportNodes; desktops=$reportWindows} | ConvertTo-Json -Depth 4 -Compress
'''
    try:
        result = subprocess.run(['powershell', '-NoProfile', '-Command', script],
            capture_output=True, encoding='utf-8', errors='replace', timeout=8, creationflags=0x08000000)
        return json.loads(result.stdout) if result.returncode == 0 and result.stdout.strip() else {}
    except Exception:
        return {}


def reports_for_instances(instances):
    if not any(getattr(i,'pid',None) for i in instances):
        return {}
    snapshot = _report_snapshot()
    parents = {int(p['ProcessId']): int(p['ParentProcessId']) for p in snapshot.get('nodes', [])}
    desktops = {int(p['id']): p for p in snapshot.get('desktops', [])}
    reports = {}
    for instance in instances:
        pid = getattr(instance,'pid',None)
        if not pid:continue
        visited = set()
        while pid and pid not in visited and len(visited) < 20:
            visited.add(pid)
            if pid in desktops:
                window = desktops[pid]
                reports[instance.pid] = report_from_window(window.get('title'), window.get('command') or '', pid,verified_desktop=True)
                break
            pid = parents.get(pid)
    return reports


def same_report(left, right):
    if not left.get('name') or not right.get('name'):
        return False
    lp, rp = normalize_path(left.get('path')), normalize_path(right.get('path'))
    if lp and rp:
        return lp == rp
    return normalize_name(left['name']) == normalize_name(right['name'])
