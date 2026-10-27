--!nonstrict
-- One short-lived bulk status command every two seconds. The CLI reads host
-- resources; this entry only computes CPU deltas and publishes the shared
-- "webapps" snapshot consumed by the panel, desktop tile and launcher.
--
-- The bulk command is a Python process costing ~60 ms whether or not anything
-- is live. While every app is stopped this entry instead uses two cheap checks:
-- a pure-Lua wake flag the panel and launcher bump when they mutate an app, and
-- a sparse shell probe that notices services started from outside the plugin
-- (desktop entries, the CLI). Either one promotes the next tick back to a full
-- status, so the panel never shows stale state.

local HISTORY_MAX = 60
local IDLE_PROBE_EVERY = 15 -- ticks, i.e. 30 s, between backstop shell probes
local IDLE_PROBE = 'for f in "$HOME"/.local/state/webapp-maker/*/run.json; do '
    .. '[ -r "$f" ] || continue; '
    .. 'p=$(sed -n \'s/.*"pid": *\\([0-9][0-9]*\\).*/\\1/p\' "$f" | head -1); '
    .. '[ -n "$p" ] || continue; '
    .. '[ -d "/proc/$p" ] && { echo 1; exit 0; }; '
    .. 'done; echo 0'
local rev = 0
local scanning = false
local lastApps = {}
local idle = false -- the last successful status found every app stopped
local idleTicks = 0 -- ticks spent idle, to space out the backstop probe
local wakeRev = nil -- last "webapps-wake" revision this entry has seen
local samples = {} -- safe -> the preceding PID/start-tick/counter sample
local histories = {} -- safe -> { cpu = {}, mem = {} }, known samples only

local function shellQuote(value)
    return "'" .. tostring(value):gsub("'", "'\\''") .. "'"
end

local function runnerPath()
    local dir = noctalia.expandPath("~/.local/share/webapp-maker")
    if dir ~= nil and noctalia.fileExists(dir .. "/webapp-run") then
        return dir .. "/webapp-run"
    end
    return (noctalia.pluginDir() or ".") .. "/scripts/webapp-run"
end

local function number(value)
    if type(value) ~= "number" or value ~= value or value == math.huge or value == -math.huge then
        return nil
    end
    return value
end

local function text(value, fallback)
    return type(value) == "string" and value or fallback
end

local function append(series, value)
    if value ~= nil then
        table.insert(series, math.min(math.max(value, 0), 1))
        if #series > HISTORY_MAX then
            table.remove(series, 1)
        end
    end
end

local function toRecord(data)
    local safe = data.safe
    local state = text(data.state, "unknown")
    if state ~= "starting" and state ~= "running" and state ~= "stopped" then
        state = "unknown"
    end
    local pid = number(data.pid)
    local startTicks = number(data.pid_start_ticks)
    local ticks = number(data.cpu_ticks)
    local clockTicks = number(data.clock_ticks)
    local at = number(data.sampled_at)
    local previous = samples[safe]
    local history = histories[safe] or { cpu = {}, mem = {} }
    local sameProcess = previous ~= nil and pid == previous.pid and startTicks == previous.startTicks
    if not sameProcess then
        history = { cpu = {}, mem = {} }
    end
    local cpu = nil
    local live = state == "starting" or state == "running"
    if live and pid ~= nil and pid > 0 and startTicks ~= nil and startTicks > 0
        and ticks ~= nil and clockTicks ~= nil and clockTicks > 0 and at ~= nil then
        if sameProcess and clockTicks == previous.clockTicks
            and at > previous.at and ticks >= previous.ticks then
            cpu = (ticks - previous.ticks) / clockTicks / (at - previous.at) * 100
        end
        samples[safe] = { pid = pid, startTicks = startTicks, ticks = ticks, clockTicks = clockTicks, at = at }
    else
        samples[safe] = nil
    end
    local mem = live and number(data.mem_mb) or nil
    append(history.cpu, cpu ~= nil and cpu / 100 or nil)
    append(history.mem, mem ~= nil and mem / 2048 or nil)
    histories[safe] = history
    return {
        safe = safe,
        name = text(data.name, safe),
        state = state,
        pid = pid,
        pgid = number(data.pgid),
        port = number(data.port),
        resolved_url = text(data.resolved_url, ""),
        started_epoch = number(data.started_epoch),
        updated_epoch = number(data.updated_epoch),
        exit_code = number(data.exit_code),
        error = text(data.error, ""),
        cpu_pct = cpu,
        mem_mb = mem,
        cpu_ticks = ticks,
        clock_ticks = clockTicks,
        sampled_at = at,
        pid_start_ticks = startTicks,
        metrics = { cpu = table.clone(history.cpu), mem = table.clone(history.mem) },
    }
end

local function publish(apps, available, err)
    lastApps = apps
    rev += 1
    noctalia.state.set("webapps", { apps = apps, rev = rev, available = available, error = err })
end

local function unavailable(detail)
    local apps = {}
    for name, previous in pairs(lastApps) do
        local record = table.clone(previous)
        record.state = "unknown"
        record.cpu_pct = nil
        record.mem_mb = nil
        record.error = detail
        apps[name] = record
    end
    samples = {} -- a recovery starts with an unknown CPU sample again
    publish(apps, false, detail)
end

noctalia.setUpdateInterval(2000)

local function anyLive(apps)
    for _, record in pairs(apps) do
        local state = record.state
        if state == "starting" or state == "running" then
            return true
        end
    end
    return false
end

-- A wake flag published by the panel or launcher after it installs, starts or
-- removes an app. It is pure state, so noticing it costs no subprocess. An
-- absent channel counts as revision 0, so the first bump is never swallowed.
local function wakeRequested()
    local value = noctalia.state.get("webapps-wake")
    local stamp = type(value) == "table" and number(value.rev) or 0
    if stamp ~= (wakeRev or 0) then
        wakeRev = stamp
        return true
    end
    return false
end

local function probeSettled(result)
    if result.timedOut == true or (result.exitCode or 1) ~= 0 then
        idle = false -- cannot tell; pay for the real thing
        return
    end
    if tostring(result.stdout or ""):match("^%s*1") ~= nil then
        idle = false
    end
end

function update()
    if scanning then
        return
    end
    if idle then
        if wakeRequested() then
            idle = false -- an app was installed, started or removed: refresh now
        else
            idleTicks += 1
            if idleTicks % IDLE_PROBE_EVERY ~= 0 then
                return -- no subprocess at all this tick
            end
            scanning = true
            local accepted = noctalia.runAsync(IDLE_PROBE, function(result)
                scanning = false
                probeSettled(result)
            end)
            if accepted == false then
                scanning = false
                idle = false
            end
            return
        end
    end
    scanning = true
    local settled = false
    local function finish(result)
        if settled then
            return
        end
        settled = true
        scanning = false
        if result.timedOut == true or (result.exitCode or 1) ~= 0 then
            local detail = text(result.stderr, ""):gsub("^%s+", ""):gsub("%s+$", "")
            if detail == "" then
                detail = text(result.stdout, ""):gsub("^%s+", ""):gsub("%s+$", "")
            end
            unavailable(detail ~= "" and detail or "webapp-run status failed")
            return
        end
        local ok, data = pcall(noctalia.json.decode, result.stdout or "")
        if not ok or type(data) ~= "table" or type(data.apps) ~= "table" then
            unavailable("Invalid webapp-run status response")
            return
        end
        local count = 0
        for index, record in pairs(data.apps) do
            if type(index) ~= "number" or index < 1 or index % 1 ~= 0
                or type(record) ~= "table" or type(record.safe) ~= "string" or record.safe == "" then
                unavailable("Invalid webapp-run status record")
                return
            end
            count += 1
        end
        if count ~= #data.apps then
            unavailable("Invalid webapp-run status list")
            return
        end
        local apps = {}
        local present = {}
        for _, dataRecord in ipairs(data.apps) do
            local record = toRecord(dataRecord)
            apps[record.name] = record
            present[record.safe] = true
        end
        for safe in pairs(histories) do
            if not present[safe] then
                histories[safe] = nil
                samples[safe] = nil
            end
        end
        -- Only a successful status may declare the plugin idle; every failure
        -- path above leaves it off so the next tick retries in full.
        idle = not anyLive(apps)
        idleTicks = 0
        publish(apps, true, nil)
    end
    local accepted = noctalia.runAsync(shellQuote(runnerPath()) .. " status", finish)
    if accepted == false then
        finish({ exitCode = 1, stderr = "Noctalia did not accept webapp-run status" })
    end
end
