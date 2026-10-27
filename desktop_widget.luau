--!nonstrict
-- Desktop tile: live/total apps and average CPU of active services.
-- Everything renders from the shared bulk-status channel; missing samples
-- remain unknown instead of becoming a zero line.

local PANEL_ID = "moshe-233/webapp-pod:panel"
local HISTORY_MAX = 60 -- samples of average CPU, one per channel update
local DOTS_MAX = 8 -- the tile is tiny; the panel has the full list

local snapshot = nil -- latest "webapps" value (nil until the service talks)
local lastRev = -1
local cpuHistory = {} -- 0..1 samples of average CPU, newest last

-- Keep the live-state set identical to panel.luau.
local LIVE = { starting = true, running = true }

local function appsOf(value)
    if type(value) == "table" and type(value.apps) == "table" then
        return value.apps
    end
    return {}
end

local function dotColor(state, err)
    if type(err) == "string" and err ~= "" then
        return "error"
    elseif state == "running" then
        return "primary"
    elseif state == "starting" then
        return "secondary"
    end
    return "on_surface_variant"
end

local function averageCpu(value)
    if type(value) ~= "table" or value.available == false then
        return nil
    end
    local sum = 0
    local count = 0
    for _, app in pairs(appsOf(value)) do
        if type(app) == "table" and LIVE[tostring(app.state or "unknown")] then
            local cpu = tonumber(app.cpu_pct)
            if cpu == nil then
                return nil
            end
            sum += cpu
            count += 1
        end
    end
    return count > 0 and sum / count or nil
end

local function render()
    local total = 0
    local alive = 0
    local dots = {}
    for _, app in pairs(appsOf(snapshot)) do
        if type(app) == "table" then
            total += 1
            local state = tostring(app.state or "unknown")
            if LIVE[state] then
                alive += 1
            end
            if #dots < DOTS_MAX then
                table.insert(dots, ui.box({
                    key = "dot-" .. tostring(app.safe or total),
                    width = 6,
                    height = 6,
                    radius = 3,
                    fill = dotColor(state, app.error),
                }))
            end
        end
    end

    local available = type(snapshot) == "table" and snapshot.available ~= false
    local cpu = averageCpu(snapshot)
    local children = {
        ui.row({ gap = 6, align = "center" }, {
            ui.glyph({
                name = "world",
                size = 14,
                color = alive > 0 and "primary" or "on_surface_variant",
            }),
            ui.label({
                text = (available and tostring(alive) or "—") .. "/" .. total,
                fontSize = 13,
                fontWeight = "bold",
                color = "on_surface",
                flexGrow = 1,
            }),
            ui.label({
                text = "CPU " .. (cpu ~= nil and string.format("%.0f%%", cpu) or "—"),
                fontSize = 10, color = "on_surface_variant",
            }),
        }),
    }
    if cpu ~= nil and #cpuHistory > 0 then
        table.insert(children, ui.graph({
            key = "cpu", values = cpuHistory,
            color = "primary", lineWidth = 1.5, fillOpacity = 0.25,
            height = 22, flexGrow = 1,
        }))
    end
    if #dots > 0 then
        table.insert(children, ui.row({ gap = 4, align = "start" }, dots))
    end
    desktopWidget.render(ui.column({
        gap = 6,
        align = "stretch",
        flexGrow = 1,
        onClick = function()
            noctalia.togglePanel(PANEL_ID)
        end,
    }, children))
end

local function onWebapps(value)
    snapshot = value
    local nextRev = type(value) == "table" and tonumber(value.rev) or -1
    nextRev = nextRev or -1
    if nextRev == lastRev then
        return -- same frame (or a value published before the last render)
    end
    lastRev = nextRev
    local cpu = averageCpu(value)
    if cpu ~= nil then
        -- Average utilization across active apps, matching the panel.
        table.insert(cpuHistory, math.min(math.max(cpu / 100, 0), 1))
        if #cpuHistory > HISTORY_MAX then
            table.remove(cpuHistory, 1)
        end
    end
    render()
end

noctalia.state.watch("webapps", onWebapps)

-- Desktop widgets get no entry-level onClick callback (the entries docs
-- reserve pointer callbacks for bar widgets and shortcuts), so the root
-- column itself carries the documented clickable-container onClick.
-- togglePanel is the in-process form of the `noctalia msg panel-open`
-- IPC; from a closed panel (the usual case from a tile) it just opens.

-- Seed from the current channel value (the service may have published
-- before this tile existed); the direct render draws the idle tile even
-- when the channel is still empty - the rev guard above would skip it.
onWebapps(noctalia.state.get("webapps"))
render()
