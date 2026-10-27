#!/usr/bin/env python3
"""Offline UI checks: locale coverage, Lua-compatible syntax and mocked APIs.

Run: python3 tests/check-ui.py [PLUGIN_DIRECTORY]
Requires the existing lua, luac and noctalia executables. No apps are started,
no plugin is reloaded, and all generated files live in a temporary directory.
This adapts Luau += syntax for Lua; it is not a real Noctalia runtime test.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

LUA_HARNESS = r"""
local passed = 0
local function expect(value, message)
    assert(value, message)
    passed = passed + 1
end
local function clone(t)
    local result = {}
    for k, v in pairs(t) do result[k] = v end
    return result
end
local function harness(file, initial)
    local h = { queue = {}, rejected = {}, renders = {}, values = {}, decoded = nil, watchers = {}, notices = {} }
    h.values.webapps = initial
    local env = setmetatable({}, {__index = _G})
    env.table = clone(table)
    env.table.clone = clone
    env.noctalia = {
        expandPath = function(p) return p:gsub('~', '/home/test') end,
        fileExists = function(p)
            if h.files then return h.files[p] == true end
            return h.stable ~= false
        end,
        pluginDir = function() return '/test/plugin dir' end,
        pluginDataDir = function() error('must use stable DATA_ROOT') end,
        getConfig = function(k) return k == 'env_file' and "/tmp/proxy's env" or nil end,
        tr = function(k, args)
            local text = k
            local keys = {}
            for a in pairs(args or {}) do table.insert(keys,a) end
            table.sort(keys)
            for _,a in ipairs(keys) do text = text .. '|' .. a .. '=' .. tostring(args[a]) end
            return text
        end,
        runAsync = function(cmd, cb)
            if h.reject ~= nil and h.reject(cmd) then
                table.insert(h.rejected, {cmd=cmd, cb=cb})
                return false -- deliberately no callback and no queued work
            end
            table.insert(h.queue, {cmd=cmd, cb=cb})
            -- Keep nil acceptance for the original async-success coverage.
        end,
        setUpdateInterval = function(ms) h.interval=ms end,
        state = {
            get = function(k) return h.values[k] end,
            set = function(k,v) h.values[k]=v end,
            watch = function(k,cb) h.watchers[k]=cb end,
        },
        json = {decode = function()
            if h.decodeError then error('invalid JSON') end
            return h.decoded
        end, encode = function(value) h.encoded=value; return '{"fields":{}}' end},
        fuzzyScore = function(needle, value) return value:find(needle, 1, true) and 1 or nil end,
        notifyError = function(...) table.insert(h.notices,{...}) end,
        togglePanel = function() end,
    }
    env.panel = {render = function(tree) h.tree=tree; table.insert(h.renders,tree) end, close=function() h.closed=true end}
    env.desktopWidget = {render = function(tree) h.tree=tree end}
    env.launcher = {setResults=function(text,results) h.results=results end}
    env.ui = setmetatable({}, {__index=function(_,kind)
        return function(props,children) return {kind=kind, props=props or {}, children=children or {}} end
    end})
    assert(loadfile(arg[1] .. '/' .. file .. '.lua', 't', env))()
    h.env=env
    function h:respond(index, result, decoded)
        self.decoded=decoded
        local command = table.remove(self.queue,index or 1)
        assert(command, 'no queued command')
        if command.cb then command.cb(result or {exitCode=0,stdout='',stderr=''}) end
        return command
    end
    function h:publish(value)
        self.values.webapps=value
        self.watchers.webapps(value)
    end
    return h
end
local function walk(node, fn)
    if type(node) ~= 'table' then return end
    fn(node)
    for _, child in ipairs(node.children or {}) do walk(child,fn) end
end
local function nodeWhere(h, fn)
    local found
    walk(h.tree,function(n) if fn(n) then found=n end end)
    return found
end
local function key(h,key)
    return nodeWhere(h,function(n) return n.props.key==key end)
end
local function labelContains(h,text)
    return nodeWhere(h,function(n) return n.kind=='label' and type(n.props.text)=='string' and n.props.text:find(text,1,true) end)
end
local function countKind(h,kind)
    local count=0
    walk(h.tree,function(n) if n.kind==kind then count=count+1 end end)
    return count
end
local function click(h,id)
    local node = assert(key(h,id), 'missing node '..id)
    assert(node.props.enabled ~= false, 'disabled '..id)
    assert(node.props.onClick, 'not clickable '..id)()
end
local function record(overrides)
    local app={safe='unicode-app',name='Ünicode 工作',state='running',pid=123,pgid=123,
        port=3000,pid_start_ticks=1000,cpu_ticks=20,clock_ticks=100,sampled_at=5,
        mem_mb=12,started_epoch=1,updated_epoch=2,error=''}
    for k,v in pairs(overrides or {}) do app[k]=v end
    return app
end
local function status(h, app)
    h.env.update()
    h:respond(1, {exitCode=0,stdout='json'}, {apps=app and {app} or {}})
    return h.values.webapps
end
-- R2.2 navigation and literal filtering: a regex/fuzzy search or issuing a
-- CLI while typing would break this observable contract.
local nav=harness('panel.luau',{apps={
    a=record({safe='dsh',name='DSH 工作'}),
    b=record({safe='literal',name='100%.[ demo',state='stopped'}),
    c=record({safe='other',name='Other',state='starting'}),
},rev=1,available=true})
nav.env.onOpen(); nav:respond(2,{exitCode=0,stdout=''}); nav:respond(1,{exitCode=0,stdout=''})
expect(key(nav,'search-1') and key(nav,'card-dsh') and key(nav,'card-other'),'initial merged list has search and orphan records')
nav.env.onFld_search('  dSh ')
expect(key(nav,'card-dsh') and not key(nav,'card-other'),'literal_name_filter: trim and ASCII casefold')
expect(#nav.queue==0 and labelContains(nav,'apps_count|shown=1|total=3'),'filter changes count without CLI')
nav.env.onFld_search('工作'); expect(key(nav,'card-dsh'),'Chinese substring matches')
nav.env.onFld_search('absent'); expect(not key(nav,'card-literal'),'no matching literal is empty')
nav.env.onFld_search('%.[')
expect(key(nav,'card-literal') and not key(nav,'card-dsh'),'percent dot bracket are literal characters')
click(nav,'clear-search')
expect(key(nav,'card-dsh') and key(nav,'card-other'),'clear search restores source list')
click(nav,'form-toggle'); nav.env.onFld_name('draft 工作'); nav.env.onFld_flags('--incognito')
local formKey=assert(nodeWhere(nav,function(n) return n.props.key and n.props.key:find('fld%-name%-') end)).props.key
nav:publish({apps={a=record({safe='dsh',name='DSH 工作'})},rev=2,available=true})
expect(key(nav,formKey).props.value=='draft 工作','form_keys_stable_during_publish')
click(nav,'back-to-list'); click(nav,'form-toggle')
expect(key(nav,formKey).props.value=='draft 工作','navigation_retains_form')
click(nav,'optional-toggle')
expect(nodeWhere(nav,function(n) return n.kind=='input' and n.props.value=='--incognito' end),'optional_values_survive_navigation')
click(nav,'back-to-list'); click(nav,'card-dsh')
expect(key(nav,'detail-overview-dsh') and not key(nav,'search-2'),'detail is focused and separate from search')
expect(#nav.queue==0 and not key(nav,'del-dsh'),'details_have_no_remove and no overview log read')
click(nav,'open-dsh')
expect(#nav.queue==1,'one launch submitted')
nav.env.onClose(); nav.env.onOpen()
expect(#nav.queue==2,'reopen scans once, does not rerun setup or stop')
nav:respond(2,{exitCode=0,stdout=''}); click(nav,'card-dsh')
expect(key(nav,'open-dsh-off') and key(nav,'open-dsh-off').props.enabled==false,'reopen_preserves_guards')
expect(key(nav,'stop-dsh').props.enabled==true,'stop_allowed_during_launch')
nav:respond(1,{exitCode=0,stdout=''})
expect(key(nav,'open-dsh').props.enabled==true,'late_action_releases_own_guard')
-- Bulk monitor: once per tick, no overlapping process, real counter delta,
-- identity changes, bounded history and explicit failures.
local monitor=harness('supervisor.luau')
expect(monitor.interval==2000,'2-second bulk interval')
monitor.env.update(); monitor.env.update()
expect(#monitor.queue==1,'only one in-flight bulk command')
expect(monitor.queue[1].cmd=="'/home/test/.local/share/webapp-maker/webapp-run' status",'stable bulk runner path')
monitor:respond(1,{exitCode=0,stdout='json'},{apps={record()}})
local first=monitor.values.webapps.apps['Ünicode 工作']
expect(first.state=='running' and first.cpu_pct==nil,'first sample remains running with unknown CPU')
expect(first.mem_mb==12 and #first.metrics.cpu==0 and #first.metrics.mem==1,'known MEM retained without fake CPU')
local second=status(monitor,record({cpu_ticks=70,sampled_at=7})).apps['Ünicode 工作']
expect(second.cpu_pct==25 and second.metrics.cpu[1]==.25,'CPU counter delta uses CLK_TCK and monotonic seconds')
local changed=status(monitor,record({cpu_ticks=10,sampled_at=9,pid_start_ticks=2000})).apps['Ünicode 工作']
expect(changed.cpu_pct==nil and #changed.metrics.cpu==0 and #changed.metrics.mem==1,'PID start-tick changes reset CPU and history')
for i=1,65 do status(monitor,record({cpu_ticks=10+i*10,sampled_at=9+i*2,pid_start_ticks=2000})) end
local history=monitor.values.webapps.apps['Ünicode 工作'].metrics
expect(#history.cpu==60 and #history.mem==60,'history bounded to 60 known samples')
monitor.env.update(); monitor:respond(1,{exitCode=1,stderr='metrics unavailable'})
local bad=monitor.values.webapps
expect(bad.available==false and bad.apps['Ünicode 工作'].state=='unknown','failed monitor marks previous records unknown')
expect(bad.apps['Ünicode 工作'].cpu_pct==nil and bad.apps['Ünicode 工作'].mem_mb==nil,'failed monitor never emits healthy zero resources')
local recovered=status(monitor,record()).apps['Ünicode 工作']
expect(recovered.cpu_pct==nil and recovered.mem_mb==12,'recovery starts unknown CPU and known MEM')
monitor.env.update(); monitor:respond(1,{exitCode=0,stdout='json'},{apps={not_array=record()}})
expect(monitor.values.webapps.available==false,'non-array status rejected')
monitor.decodeError=true
monitor.env.update(); monitor:respond(1,{exitCode=0,stdout='bad'})
expect(monitor.values.webapps.available==false,'invalid JSON rejected')
monitor.decodeError=false
expect(next(status(monitor,nil).apps)==nil,'empty successful bulk status removes old apps')
local source=harness('supervisor.luau'); source.stable=false; source.env.update()
expect(source.queue[1].cmd=="'/test/plugin dir/scripts/webapp-run' status",'source runner fallback')
-- Idle suspension: with every app stopped the 2 s tick must not spawn the
-- Python bulk status. A wake flag from the panel/launcher promotes it back to a
-- full status on the very next tick, and a sparse shell probe still catches
-- services started from outside the plugin.
local idleMon=harness('supervisor.luau')
idleMon.env.update()
idleMon:respond(1,{exitCode=0,stdout='json'},{apps={record({state='stopped',pid=nil,
    pid_start_ticks=nil,cpu_ticks=nil,mem_mb=nil})}})
expect(idleMon.values.webapps.apps['Ünicode 工作'].state=='stopped','bulk status publishes stopped')
local spawned=0
for _=1,10 do
    idleMon.env.update()
    spawned=spawned+#idleMon.queue
    expect(#idleMon.queue==0,'idle tick spawns no subprocess')
end
expect(spawned==0,'ten idle ticks stay silent')
for _=1,5 do
    idleMon.env.update()
    if #idleMon.queue>0 then
        expect(not idleMon.queue[1].cmd:find('webapp%-run'),'backstop probe is not the Python bulk status')
        idleMon:respond(1,{exitCode=0,stdout='0'})
    end
end
idleMon.values['webapps-wake']={rev=1}
idleMon.env.update()
expect(#idleMon.queue==1 and idleMon.queue[1].cmd:find('webapp%-run')~=nil,
    'wake flag promotes the next tick back to the full bulk status')
local woke=idleMon:respond(1,{exitCode=0,stdout='json'},{apps={record()}})
expect(idleMon.values.webapps.apps['Ünicode 工作'].state=='running','woken status republishes live state')
-- A live service must also leave idle on its own, without any wake flag.
local liveMon=harness('supervisor.luau')
liveMon.env.update(); liveMon:respond(1,{exitCode=0,stdout='json'},{apps={record({state='stopped',pid=nil,
    pid_start_ticks=nil,cpu_ticks=nil,mem_mb=nil})}})
for _=1,20 do
    liveMon.env.update()
    if #liveMon.queue>0 then
        liveMon:respond(1,{exitCode=0,stdout='1'})
        break
    end
end
liveMon.env.update()
expect(liveMon.queue[1]~=nil and liveMon.queue[1].cmd:find('webapp%-run')~=nil,
    'a live PID found by the backstop probe restores the bulk status')
-- A failed status keeps the supervisor in full mode: it must retry, not idle.
local failMon=harness('supervisor.luau')
failMon.env.update(); failMon:respond(1,{exitCode=1,stderr='boom'})
failMon.env.update()
expect(failMon.queue[1]~=nil and failMon.queue[1].cmd:find('webapp%-run')~=nil,
    'a failed status never suspends the monitor')
-- Creation waits for setup; regex/script modes need no extra website URL.
local function newPanel(ready)
    local h=harness('panel.luau')
    h.env.onOpen()
    expect(#h.queue==2 and h.queue[1].cmd:find('webapp-setup.sh',1,true),'onOpen issues setup and desktop scan only')
    h:respond(2,{exitCode=0,stdout=''})
    if ready then h:respond(1,{exitCode=0,stdout=''}) end
    click(h,'form-toggle')
    return h
end
local p=newPanel(false)
p.env.onFld_name('Ünicode 工作'); p.env.onTgl_managed(true)
p.env.onFld_start_cmd("python server.py --title '工作'"); p.env.onSel_urlSource(1)
p.env.onFld_url_regex('(?P<url>https?://[^ ]+)'); p.env.onFld_cwd('/tmp/project path')
expect(key(p,'fld-url-1')==nil,'regex UI hides unused fixed website URL')
click(p,'create')
expect(#p.queue==1 and p.queue[1].cmd:find('webapp-setup.sh',1,true),'no install and no duplicate setup while setup pending')
p:respond(1,{exitCode=0,stdout=''})
expect(#p.queue==1 and p.queue[1].cmd:find('webapp-install.sh',1,true),'installation begins after setup callback only')
local cmd=p.queue[1].cmd
expect(cmd:find("--runner '/home/test/.local/share/webapp-maker/webapp-run'",1,true),'new installation points at stable runner')
expect(cmd:find("--url-source 'regex'",1,true) and cmd:find("--url-timeout '15'",1,true),'regex source and default timeout')
expect(cmd:find("'Ünicode 工作' ''",1,true),'regex mode passes empty optional website URL')
expect(cmd:find("--cwd '/tmp/project path'",1,true) and cmd:find('proxy',1,true),'cwd and env file installation prefix')
expect(not cmd:find('port-mode',1,true) and not cmd:find('url-template',1,true),'no old port/template arguments')
expect(cmd:find("--auto-open=true",1,true),'managed default auto-open retained')
local failure=newPanel(false)
failure.env.onFld_name('test'); failure.env.onFld_url('example.com'); click(failure,'create')
failure:respond(1,{exitCode=1,stdout='setup failed'})
expect(#failure.queue==0 and labelContains(failure,'err_setup'),'setup failure prevents install and surfaces error')
local fixed=newPanel(true)
fixed.env.onFld_name('fixed'); fixed.env.onTgl_managed(true); fixed.env.onFld_start_cmd('serve')
click(fixed,'create')
expect(#fixed.queue==0 and labelContains(fixed,'err_required'),'fixed URL mode requires URL')
fixed.env.onSel_urlSource(1); click(fixed,'create')
expect(#fixed.queue==0 and labelContains(fixed,'err_regex_required'),'regex mode requires regex')
fixed.env.onSel_urlSource(2); click(fixed,'create')
expect(#fixed.queue==0 and labelContains(fixed,'err_script_required'),'script mode requires extraction command')
fixed.env.onFld_extract_script('/tmp/extract url.sh'); click(fixed,'create')
expect(#fixed.queue==1 and fixed.queue[1].cmd:find("--url-source 'script'",1,true),'script mode does not require website URL')
local simple=newPanel(true)
simple.env.onFld_name('web'); simple.env.onFld_url('example.com'); click(simple,'create')
expect(simple.queue[1].cmd:find("--launcher '/home/test/.local/share/webapp-maker/webapp-launch'",1,true),'ordinary websites use original launcher')
expect(simple.queue[1].cmd:find("'https://example.com'",1,true),'ordinary website URL normalization')
-- Regex preview goes through the same Python matcher, with JSON success and stderr errors.
local preview=newPanel(true)
preview.env.onTgl_managed(true); preview.env.onSel_urlSource(1)
preview.env.onFld_url_regex("(?P<url>http://[^ ]+)")
preview.env.onFld_log_sample("ready 'http://127.0.0.1:3000/start#工作'")
click(preview,'regex-preview')
expect(preview.queue[1].cmd:find(' match --regex ',1,true) and preview.queue[1].cmd:find(' --line ',1,true),'preview uses CLI match')
preview:respond(1,{exitCode=0,stdout='json'},{url='http://127.0.0.1:3000/start#工作'})
expect(labelContains(preview,'preview_url|url=http://127.0.0.1:3000/start#工作'),'preview renders complete URL')
click(preview,'regex-preview'); preview:respond(1,{exitCode=0,stdout='json'},{})
expect(labelContains(preview,'preview_no_match'),'null URL produces no-match feedback')
click(preview,'regex-preview'); preview:respond(1,{exitCode=1,stderr='invalid regex'})
expect(labelContains(preview,'invalid regex'),'preview surfaces stderr')
click(preview,'regex-preview'); preview.env.onFld_url_regex('new pattern')
preview:respond(1,{exitCode=0,stdout='json'},{url='http://stale'})
expect(not labelContains(preview,'http://stale'),'stale preview cannot overwrite edited regex')
-- New desktop entry recognition and unknown resources in the panel.
local metrics=harness('panel.luau')
metrics.env.onOpen(); metrics:respond(2,{exitCode=0,stdout='Ünicode 工作\t"/home/test/.local/share/webapp-maker/webapp-run" launch "Ünicode 工作"\n'})
metrics:respond(1,{exitCode=0,stdout=''})
click(metrics,'card-Ünicode-工作')
expect(key(metrics,'restart-Ünicode-工作').props.tooltip=='btn_restart','new webapp-run entries recognized as managed')
expect(key(metrics,'stop-Ünicode-工作').props.enabled==true,'stop available before any monitor record')
local unknown=record(); unknown.cpu_pct=nil; unknown.mem_mb=nil; unknown.metrics={cpu={},mem={}}
metrics:publish({apps={['Ünicode 工作']=unknown},rev=1,available=true})
expect(key(metrics,'detail-overview-unicode-app'),'first authoritative safe preserves current detail')
click(metrics,'back-to-list')
expect(labelContains(metrics,'overview_cpu|pct=—') and labelContains(metrics,'overview_mem|mb=—'),'unknown overview readings show placeholders')
expect(countKind(metrics,'graph')==0 and countKind(metrics,'progress')==0,'unknown overview does not fabricate zero graphs')
click(metrics,'card-unicode-app')
expect(countKind(metrics,'graph')==0 and countKind(metrics,'progress')==0,'expanded unknown resources do not fabricate progress or graphs')
expect(#metrics.queue==0,'overview does not read a log')
click(metrics,'detail-logs-unicode-app')
expect(metrics.queue[1].cmd:find("tail -c 16000 -- '/home/test/.local/state/webapp-maker/unicode-app/app.log'",1,true),'log page uses bounded native tail')
metrics:publish({apps={['Ünicode 工作']=record({state='unknown',error='status failed'})},rev=2,available=false,error='status failed'})
expect(labelContains(metrics,'overview_no_bridge') and labelContains(metrics,'status failed'),'unavailable status surfaced')
-- Desktop tile distinguishes initial/failed metrics from real zero.
local tile=harness('desktop_widget.luau')
expect(labelContains(tile,'CPU —') and countKind(tile,'graph')==0,'initial desktop CPU unknown')
local app=record(); app.cpu_pct=nil
local channel={apps={x=app},rev=1,available=true}
tile:publish(channel)
expect(labelContains(tile,'1/1') and labelContains(tile,'CPU —') and countKind(tile,'graph')==0,'running first-sample desktop CPU unknown')
app=record(); app.cpu_pct=25; tile:publish({apps={x=app},rev=2,available=true})
expect(labelContains(tile,'CPU 25%') and countKind(tile,'graph')==1,'known desktop average CPU rendered')
app.state='unknown'; app.cpu_pct=nil; app.error='status failed'
tile:publish({apps={x=app},rev=3,available=false})
expect(labelContains(tile,'—/1') and labelContains(tile,'CPU —') and countKind(tile,'graph')==0,'failed desktop CPU unknown')
expect(key(tile,'dot-unicode-app').props.fill=='error','desktop error independent of runtime state')
-- Launcher command and Unicode name survive mapping through the channel.
local launchApp=record({resolved_url='http://127.0.0.1:3000'})
local launch=harness('launcher.luau',{apps={x=launchApp},rev=1,available=true})
launch.env.onQuery('')
expect(#launch.results==1 and launch.results[1].title=='Ünicode 工作','launcher retains Unicode display name')
launch.env.onActivate(launch.results[1].id)
expect(launch.queue[1].cmd=="'/home/test/.local/share/webapp-maker/webapp-run' launch 'unicode-app'",'launcher uses stable short-lived CLI')
-- Rejected command submission must release UI guards even without a callback.
-- Removing accepted==false handling from production must fail these checks.
print('Existing async-success coverage: '..passed..' assertions passed')
local function rejectContaining(fragment)
    return function(cmd) return cmd:find(fragment,1,true) ~= nil end
end
local setupRejected=harness('panel.luau')
setupRejected.reject=rejectContaining('webapp-setup.sh')
setupRejected.env.onOpen(); setupRejected:respond(1,{exitCode=0,stdout=''})
expect(labelContains(setupRejected,'err_setup'),'rejected setup displays an error instead of hanging preparation')
click(setupRejected,'form-toggle'); setupRejected.env.onFld_name('retry'); setupRejected.env.onFld_url('example.com')
click(setupRejected,'create')
expect((key(setupRejected,'create') ~= nil and key(setupRejected,'create').props.enabled==true),'rejected setup clears create busy and preparing guards')
setupRejected.reject=nil; click(setupRejected,'create')
expect(#setupRejected.queue==1 and setupRejected.queue[1].cmd:find('webapp-setup.sh',1,true),'setup can be retried after submission rejection')
local installRejected=newPanel(true)
installRejected.env.onFld_name('retry'); installRejected.env.onFld_url('example.com')
installRejected.reject=rejectContaining('webapp-install.sh'); click(installRejected,'create')
expect((key(installRejected,'create') ~= nil and key(installRejected,'create').props.enabled==true) and labelContains(installRejected,'err_async_rejected'),'rejected install clears busy and displays the failure')
installRejected:respond(1,{exitCode=0,stdout=''}) -- settle the normal desktop refresh
installRejected.reject=nil; click(installRejected,'create')
expect(#installRejected.queue==1,'creation can be retried after install rejection')
local previewRejected=newPanel(true)
previewRejected.env.onTgl_managed(true); previewRejected.env.onSel_urlSource(1)
previewRejected.env.onFld_url_regex('(http://[^ ]+)'); previewRejected.env.onFld_log_sample('ready http://localhost:3000')
previewRejected.reject=rejectContaining(' match '); click(previewRejected,'regex-preview')
expect((key(previewRejected,'regex-preview') ~= nil and key(previewRejected,'regex-preview').props.enabled==true) and labelContains(previewRejected,'err_async_rejected'),'rejected regex preview clears regexBusy and displays failure')
previewRejected.reject=nil; click(previewRejected,'regex-preview')
expect(#previewRejected.queue==1,'regex preview can be retried after submission rejection')
local function actionPanel()
    local h=harness('panel.luau',{apps={x=record()},rev=1,available=true})
    h.env.onOpen(); h:respond(2,{exitCode=0,stdout=''})
    h:respond(1,{exitCode=0,stdout=''})
    click(h,'card-unicode-app')
    return h
end
local actionRejected=actionPanel()
actionRejected.reject=rejectContaining(' launch '); click(actionRejected,'open-unicode-app')
expect((key(actionRejected,'open-unicode-app') ~= nil and key(actionRejected,'open-unicode-app').props.enabled==true) and labelContains(actionRejected,'err_async_rejected'),'rejected managed open clears acting guard and displays failure')
actionRejected.rejected[1].cb({exitCode=0,stdout='late success'})
expect(labelContains(actionRejected,'err_async_rejected') and not labelContains(actionRejected,'ok_done'),'late callback after rejected submission cannot complete a second time')
actionRejected.reject=rejectContaining(' stop '); click(actionRejected,'stop-unicode-app')
expect((key(actionRejected,'stop-unicode-app') ~= nil and key(actionRejected,'stop-unicode-app').props.enabled==true),'rejected stop clears its acting guard')
click(actionRejected,'restart-unicode-app')
expect((key(actionRejected,'restart-unicode-app') ~= nil and key(actionRejected,'restart-unicode-app').props.enabled==true),'rejected restart stop phase clears its acting guard')
actionRejected.reject=nil; click(actionRejected,'restart-unicode-app')
actionRejected.reject=rejectContaining(' launch '); actionRejected:respond(1,{exitCode=0,stdout=''})
expect((key(actionRejected,'restart-unicode-app') ~= nil and key(actionRejected,'restart-unicode-app').props.enabled==true) and #actionRejected.queue==0,'rejected restart launch phase clears its acting guard')
local simpleOpen=harness('panel.luau')
simpleOpen.env.onOpen(); simpleOpen:respond(2,{exitCode=0,stdout='Website\twebapp-launch https://example.com/$(bad)%%20x\n'})
simpleOpen:respond(1,{exitCode=0,stdout=''})
click(simpleOpen,'open-website')
expect(simpleOpen.queue[1].cmd=="gio launch '/home/test/.local/share/applications/Website.desktop'",'ordinary app uses Desktop Entry launch without shell-evaluating Exec or losing percent escaping')
local simpleRejected=harness('panel.luau')
simpleRejected.env.onOpen(); simpleRejected:respond(2,{exitCode=0,stdout='Website\twebapp-launch https://example.com\n'})
simpleRejected:respond(1,{exitCode=0,stdout=''})
simpleRejected.reject=rejectContaining('gio launch'); click(simpleRejected,'open-website')
expect((key(simpleRejected,'open-website') ~= nil and key(simpleRejected,'open-website').props.enabled==true) and labelContains(simpleRejected,'err_async_rejected'),'rejected ordinary website open clears guard and displays failure')
local logRejected=actionPanel()
logRejected.reject=rejectContaining('tail -c '); click(logRejected,'detail-logs-unicode-app')
expect(nodeWhere(logRejected,function(n) return n.kind=='markdown' and n.props.text:find('logs_read_failed',1,true) end),'rejected log read displays the failure in its log page')
click(logRejected,'detail-overview-unicode-app'); logRejected.reject=nil; click(logRejected,'detail-logs-unicode-app')
expect(#logRejected.queue==1,'log drawer can retry a rejected read')
local removeRejected=actionPanel()
click(removeRejected,'edit-unicode-app')
removeRejected:respond(1,{exitCode=0,stdout='json'},{schema_version=1,safe='unicode-app',kind='managed',revision='sha256:one',fields={start_cmd='serve',url_source='fixed',url='http://localhost:3000',url_regex='',extract_script='',cwd='',flags='',auto_open=true,url_timeout_s='15'}})
click(removeRejected,'danger-toggle-unicode-app')
removeRejected.reject=rejectContaining('webapp-remove.sh'); click(removeRejected,'del-unicode-app'); click(removeRejected,'confirm-unicode-app')
expect((key(removeRejected,'del-unicode-app') ~= nil and key(removeRejected,'del-unicode-app').props.enabled==true) and labelContains(removeRejected,'err_async_rejected'),'rejected remove immediately clears deleting without waiting for desktop refresh')
local scanRejected=harness('panel.luau')
scanRejected.reject=rejectContaining('for f in '); scanRejected.env.onOpen()
scanRejected:respond(1,{exitCode=0,stdout=''})
expect(labelContains(scanRejected,'err_async_rejected') and not labelContains(scanRejected,'installed_loading'),'rejected installed-list scan displays failure and clears loading')
local monitorRejected=harness('supervisor.luau')
status(monitorRejected,record()); monitorRejected.reject=function() return true end
monitorRejected.env.update()
local unavailable=monitorRejected.values.webapps
expect(unavailable.available==false and unavailable.apps['Ünicode 工作'].state=='unknown' and unavailable.error~='','rejected status publishes unknown and an error')
monitorRejected.reject=nil; monitorRejected.env.update()
expect(#monitorRejected.queue==1,'rejected status releases scanning for the next tick')
local rejectedRevision=unavailable.rev
monitorRejected.rejected[1].cb({exitCode=0,stdout='late json'})
expect(monitorRejected.values.webapps.rev==rejectedRevision,'late rejected status callback cannot publish a second result')
local launchRejected=harness('launcher.luau',{apps={x=record()},rev=1,available=true})
launchRejected.reject=function() return true end
launchRejected.env.onActivate('unicode-app')
expect(#launchRejected.notices==1 and launchRejected.notices[1][2]:find('err_async_rejected',1,true),'rejected launcher activation displays one failure notification')
launchRejected.rejected[1].cb({exitCode=1,stderr='late failure'})
expect(#launchRejected.notices==1,'late rejected launcher callback cannot notify twice')
-- R2.2 focused regression cases. These exercise rendered controls and command
-- receipts; the host APIs are the only mocked boundary.
do
    local h=actionPanel()
    click(h,'detail-logs-unicode-app')
    expect(#h.queue==1,'only_visible_logs_read')
    local commandFile=assert(io.open(arg[1] .. '/log-command.sh','w'))
    commandFile:write(h.queue[1].cmd); commandFile:close()
    expect(countKind(h,'scroll')==1 and key(h,'logscroll-unicode-app').props.flexGrow==1,'log viewport owns remaining height without nested scroll')
    expect(key(h,'log-page-unicode-app').props.flexGrow==1,'log page propagates available height to viewport')
    h:publish({apps={x=record()},rev=2,available=true})
    expect(#h.queue==1,'log_read_single_flight')
    click(h,'back-to-list')
    h:respond(1,{exitCode=0,stdout='LATE LOG'})
    expect(not nodeWhere(h,function(n) return n.kind=='markdown' end),'late_log_callback_discarded')
    click(h,'card-unicode-app'); click(h,'detail-logs-unicode-app')
    h:respond(1,{exitCode=0,stdout='before\n```\n# plain log\n````\nafter'})
    local markdown=nodeWhere(h,function(n) return n.kind=='markdown' end).props.text
    expect(markdown:sub(1,6)=='`````\n' and markdown:sub(-5)=='`````','log_backticks_are_literal')
    click(h,'retry-log-unicode-app'); h:respond(1,{exitCode=44,stdout=''})
    expect(nodeWhere(h,function(n) return n.kind=='markdown' end).props.text=='logs_empty','explicit missing file is empty')
    click(h,'retry-log-unicode-app'); h:respond(1,{exitCode=1,stderr='permission denied'})
    expect(nodeWhere(h,function(n) return n.kind=='markdown' end).props.text=='logs_read_failed','missing_vs_failed_log')
    click(h,'retry-log-unicode-app'); h:respond(1,{exitCode=0,stdout=''})
    expect(nodeWhere(h,function(n) return n.kind=='markdown' end).props.text=='logs_empty','zero-byte log is explicitly empty')
    h.env.onClose(); h:publish({apps={x=record()},rev=3,available=true})
    expect(#h.queue==0,'close_does_not_stop and no new log read after close')
end
do
    local h=actionPanel()
    h:publish({apps={x=record({cpu_pct=225,mem_mb=4096,metrics={cpu={1},mem={1}}})},rev=2,available=true})
    expect(labelContains(h,'225%') and labelContains(h,'4096 MB'),'metrics_above_scale: actual numbers not clamped')
    expect(key(h,'graph-cpu-unicode-app').props.values[1]==1 and key(h,'graph-mem-unicode-app').props.values[1]==1,'normalized curve scale remains bounded')
    expect(key(h,'graph-cpu-unicode-app').props.height==68,'short graph has explicit height')
    h:publish({apps={x=record({cpu_pct=0,mem_mb=0,metrics={cpu={0},mem={0}}})},rev=3,available=true})
    expect(labelContains(h,'0%') and labelContains(h,'0 MB') and key(h,'graph-cpu-unicode-app'),'true zero remains measured')
    h:publish({apps={x=record({cpu_pct=20,mem_mb=40,metrics={mem={.2}}})},rev=4,available=true})
    expect(not key(h,'graph-cpu-unicode-app') and key(h,'graph-mem-unicode-app'),'CPU curve cannot borrow MEM history')
    h:publish({apps={x=record({cpu_pct=90,metrics={cpu={.9},mem={.2}}})},rev=5,available=false})
    expect(countKind(h,'graph')==0 and labelContains(h,'overview_no_bridge'),'bridge failure hides previous curves')
end
do
    local h=harness('panel.luau')
    h.env.onOpen(); h:respond(2,{exitCode=0,stdout='Ünicode 工作\twebapp-run launch app\n'}); h:respond(1,{exitCode=0,stdout=''})
    click(h,'card-Ünicode-工作'); click(h,'open-Ünicode-工作')
    h:publish({apps={x=record({safe='ünicode-工作',state='starting'})},rev=1,available=true})
    expect(key(h,'detail-overview-ünicode-工作'),'authoritative Unicode safe preserves detail selection')
    expect(key(h,'open-ünicode-工作-off') and not key(h,'open-ünicode-工作-off').props.enabled,'authoritative safe cannot lose pending launch guard')
    expect(#h.queue==1 and key(h,'stop-ünicode-工作').props.enabled,'safe promotion keeps stop abort path without duplicate launch')
    h:respond(1,{exitCode=0,stdout=''})
    expect(key(h,'open-ünicode-工作').props.enabled,'original receipt releases promoted safe guard')
end
do
    local h=actionPanel()
    click(h,'detail-logs-unicode-app'); h:respond(1,{exitCode=0,stdout='first log'})
    local before=nodeWhere(h,function(n) return n.kind=='markdown' end).props.text
    h:publish({apps={x=record()},rev=2,available=true})
    expect(nodeWhere(h,function(n) return n.kind=='markdown' end).props.text==before,'background log refresh preserves visible content without loading flash')
    expect(#h.queue==1,'running log refresh remains single-flight')
    h:publish({apps={x=record({state='stopped'})},rev=3,available=true})
    h:respond(1,{exitCode=0,stdout='before exit'})
    expect(#h.queue==1,'stopping during a read schedules exactly one final tail')
    h:respond(1,{exitCode=0,stdout='last output before exit'})
    local final=nodeWhere(h,function(n) return n.kind=='markdown' end).props.text
    for rev=4,7 do h:publish({apps={x=record({state='stopped'})},rev=rev,available=true}) end
    expect(#h.queue==0 and nodeWhere(h,function(n) return n.kind=='markdown' end).props.text==final,'stopped logs stay stable without repeated reads')
    click(h,'retry-log-unicode-app')
    expect(#h.queue==1,'stopped logs still allow explicit manual retry')
end
do
    local h=actionPanel()
    local function pathTo(node,id,path)
        path=path or ''
        local here=path .. '/' .. node.kind .. ':' .. (node.props.key or '')
        if node.props.key==id then return here end
        for _,child in ipairs(node.children or {}) do
            local found=pathTo(child,id,here)
            if found then return found end
        end
    end
    local before=pathTo(h.tree,'open-unicode-app')
    local tabVariant=key(h,'detail-overview-unicode-app').props.variant
    click(h,'detail-logs-unicode-app')
    expect(pathTo(h.tree,'open-unicode-app')==before,'tab switching retains the action buttons under the same native parents')
    expect(key(h,'detail-overview-unicode-app').props.variant==tabVariant,'tab state does not remount or replace button palette')
end
local configFields={start_cmd='serve --original',url_source='fixed',url='http://original:3000/',
    url_regex='',extract_script='',cwd='/tmp/project',flags='',auto_open=true,url_timeout_s='15'}
local function enterConfig(h, fields)
    click(h,'edit-unicode-app')
    expect(h.queue[1].cmd:find("config-read 'Ünicode 工作'",1,true),'configuration uses original CLI name')
    h:respond(1,{exitCode=0,stdout='json'},{schema_version=1,safe='unicode-app',name='Ünicode 工作',kind='managed',revision='sha256:original',fields=fields or configFields})
end
local function cfgInput(h,field)
    return assert(nodeWhere(h,function(n) return n.kind=='input' and n.props.key:find('cfg%-unicode%-app%-'..field..'%-%d+$') end),'missing config field '..field)
end
do
    local h=actionPanel()
    expect(not key(h,'del-unicode-app'),'details_have_no_remove')
    enterConfig(h)
    expect(key(h,'save-config-unicode-app') and not key(h,'detail-overview-unicode-app'),'edit_opens_configuration_directly')
    expect(cfgInput(h,'url').props.value=='http://original:3000/','edit_uses_original_config')
    expect(not key(h,'del-unicode-app'),'danger zone initially collapsed')
    cfgInput(h,'url').props.onChange('http://changed:3001/')
    local fieldKey=cfgInput(h,'url').props.key
    h:publish({apps={x=record()},rev=2,available=true})
    expect(cfgInput(h,'url').props.key==fieldKey and cfgInput(h,'url').props.value=='http://changed:3001/','configuration fields stable during status refresh')
    click(h,'back-config')
    expect(key(h,'discard-changes') and key(h,'keep-editing'),'config_dirty_back_confirmation')
    click(h,'keep-editing'); click(h,'save-config-unicode-app')
    expect(h.encoded.fields.url=='http://changed:3001/' and h.encoded.fields.start_cmd=='serve --original','save sends draft original fields')
    expect(not key(h,'save-config-unicode-app').props.enabled and not key(h,'back-config').props.enabled,'save locks duplicate submission and navigation')
    h:respond(1,{exitCode=1,stdout='json'},{code='io',message='write failed'})
    expect(cfgInput(h,'url').props.value=='http://changed:3001/' and key(h,'save-config-unicode-app').props.enabled,'save_failure_keeps_draft')
    click(h,'save-config-unicode-app'); h:respond(1,{exitCode=1,stdout='json'},{code='conflict',message='external change'})
    expect(cfgInput(h,'url').props.value=='http://changed:3001/' and key(h,'reload-config-unicode-app'),'conflict preserves draft and offers reload')
    expect(not key(h,'save-config-unicode-app').props.enabled,'conflict cannot overwrite newer revision')
    click(h,'danger-toggle-unicode-app'); click(h,'del-unicode-app')
    expect(#h.queue==0 and key(h,'confirm-unicode-app'),'arming removal sends no CLI')
    click(h,'cancel-unicode-app')
    expect(#h.queue==0 and cfgInput(h,'url').props.value=='http://changed:3001/','cancel_remove_is_noop and preserves draft')
    click(h,'del-unicode-app')
    h:publish({apps={a=record({safe='aaa',name='AAA',state='starting'}),x=record()},rev=3,available=true})
    click(h,'confirm-unicode-app')
    expect(h.queue[1].cmd:find("webapp-remove.sh' 'Ünicode 工作'",1,true),'confirm_bound_to_safe_after_sort')
    h:respond(1,{exitCode=1,stdout='remove failed'})
    expect(labelContains(h,'remove failed') and cfgInput(h,'url'),'remove failure retains target and draft')
    h:respond(1,{exitCode=0,stdout=''}) -- installed scan after removal failure
    click(h,'back-config'); click(h,'discard-changes')
    expect(key(h,'detail-overview-unicode-app'),'discard returns to original detail tab')
end
do
    local h=actionPanel()
    click(h,'edit-unicode-app')
    click(h,'back-config')
    expect(not key(h,'edit-unicode-app').props.enabled,'old configuration load owns its operation guard')
    h:publish({apps={x=record(),y=record({safe='other',name='Other'})},rev=2,available=true})
    click(h,'back-to-list'); click(h,'card-other'); click(h,'edit-other')
    h:respond(1,{exitCode=0,stdout='json'},{schema_version=1,kind='managed',revision='sha256:stale',fields=configFields})
    expect(not nodeWhere(h,function(n) return n.kind=='input' end) and labelContains(h,'Other'),'config_callback_target_guard')
    h.decodeError=true; h:respond(1,{exitCode=0,stdout='not JSON'})
    expect(labelContains(h,'config_failed') and key(h,'reload-config-other').props.enabled,'malformed config releases load guard')
end
do
    local h=actionPanel(); enterConfig(h)
    cfgInput(h,'url').props.onChange('http://saved:3000/')
    click(h,'save-config-unicode-app')
    h.env.onClose(); h.env.onOpen(); h:respond(2,{exitCode=0,stdout=''})
    h:respond(1,{exitCode=0,stdout='json'},{saved=true,revision='sha256:saved',restart_required=true})
    expect(key(h,'search-2') and #h.queue==0,'late save receipt releases guard without navigating or launching')
    click(h,'card-unicode-app')
    expect(key(h,'open-unicode-app').props.enabled,'late save releases own business guard')
end
do
    local h=actionPanel()
    click(h,'restart-unicode-app')
    h:respond(1,{exitCode=0,timedOut=true,stdout='stop timed out'})
    expect(#h.queue==0 and key(h,'restart-unicode-app').props.enabled,'restart stop timeout must not launch')
end
do
    local h=newPanel(false)
    h.env.onFld_name('pending'); h.env.onFld_url('example.com'); click(h,'create')
    h.env.onClose(); h.env.onOpen(); h:respond(2,{exitCode=0,stdout=''})
    click(h,'form-toggle')
    expect(key(h,'create-off') and not key(h,'create-off').props.enabled,'reopen preserves create guard and submitted snapshot')
    h:respond(1,{exitCode=0,stdout=''})
    expect(#h.queue==1 and h.queue[1].cmd:find("'pending' 'https://example.com'",1,true),'setup finishes the original create snapshot exactly once')
    h.env.onClose(); h.env.onOpen(); h:respond(2,{exitCode=0,stdout=''})
    h:respond(1,{exitCode=0,stdout=''})
    expect(key(h,'search-3') and not key(h,'form-scroll'),'late creation receipt does not redirect current view')
    expect(#h.queue==1 and not h.queue[1].cmd:find(' launch ',1,true),'create_does_not_launch')
end
do
    local h=newPanel(true)
    h.env.onTgl_managed(true); h.env.onSel_urlSource(1)
    h.env.onFld_url_regex('(http://[^ ]+)'); h.env.onFld_log_sample('ready http://localhost:3000')
    click(h,'regex-preview'); h.env.onSel_urlSource(0)
    h:respond(1,{exitCode=0,stdout='json'},{url='http://old-source'})
    h.env.onSel_urlSource(1)
    expect(not labelContains(h,'http://old-source'),'source_switch_invalidates_preview')
end

print('PASS: '..passed..' UI/monitor/launcher assertions (mock Noctalia APIs; Lua syntax adapter only)')

"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin_directory", nargs="?", type=Path,
                        default=Path(__file__).resolve().parents[1])
    plugin = parser.parse_args().plugin_directory.resolve()
    for executable in ("lua", "luac", "noctalia"):
        if shutil.which(executable) is None:
            raise SystemExit(f"Required existing executable is missing: {executable}")
    files = ("panel.luau", "supervisor.luau", "launcher.luau", "desktop_widget.luau")
    keys = {"status_preparing"}
    keys.update("state_" + state for state in ("starting", "running", "stopped", "unknown"))
    locale_keys = {}
    with tempfile.TemporaryDirectory(prefix="light-launcher-ui-") as temporary:
        generated = Path(temporary)
        for name in files:
            source = (plugin / name).read_text()
            translated = re.sub(r"(?m)^(\s*)([A-Za-z_][A-Za-z0-9_.]*)\s*\+=\s*(.*)$",
                                r"\1\2 = \2 + \3", source)
            adapted = generated / (name + ".lua")
            adapted.write_text(translated)
            subprocess.run(["luac", "-p", str(adapted)], check=True)
            keys.update(re.findall(r'tr\("([^"\n]+)"', source))
            for pair in re.findall(r'field\("[^"]+", "([^"]+)", "([^"]+)"', source):
                keys.update(pair)
        for locale in sorted((plugin / "translations").glob("*.json")):
            values = json.loads(locale.read_text())
            missing = keys - values.keys()
            if missing:
                raise AssertionError(f"{locale}: missing translation keys {sorted(missing)}")
            locale_keys[locale.name] = set(values)
            print(f"{locale.name}: valid JSON; all {len(keys)} referenced translation keys present", flush=True)
        if len({frozenset(value) for value in locale_keys.values()}) != 1:
            raise AssertionError("Translation key sets differ")
        for field in ("port_mode", "token_pattern", "port_pattern", "url_template", "probe_timeout"):
            if field in (plugin / "panel.luau").read_text():
                raise AssertionError(f"Obsolete form field remains: {field}")
        bridge = (plugin / "supervisor.luau").read_text()
        if "listDir" in bridge or "readFileAsync" in bridge:
            raise AssertionError("The bulk bridge must not scan per-app state files")
        script = generated / "checks.lua"
        script.write_text(LUA_HARNESS)
        subprocess.run(["lua", str(script), str(generated)], check=True)
        log_directory = generated / "home/.local/state/webapp-maker/unicode-app"
        log_directory.mkdir(parents=True)
        log_file = log_directory / "app.log"
        log_file.write_text("known log line\n")
        command = (generated / "log-command.sh").read_text().replace("/home/test", str(generated / "home"))
        success = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        assert success.returncode == 0 and success.stdout == "known log line\n", "native log command reads content"
        log_directory.chmod(0)
        try:
            denied = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
            if os.geteuid() != 0:
                assert denied.returncode not in (0, 44), "permission denied must not masquerade as missing/empty log"
        finally:
            log_directory.chmod(0o700)
        log_file.unlink()
        missing = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        assert missing.returncode == 44, "missing log has explicit bounded-read receipt"
        print("PASS: native log read/missing/permission receipts", flush=True)
    subprocess.run(["noctalia", "plugins", "lint", str(plugin)], check=True)


if __name__ == "__main__":
    main()
