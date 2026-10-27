--!nonstrict
-- Launcher provider (prefix "wa"): lists the managed web apps published on
-- the "webapps" state channel, fuzzy-matches the typed text against app
-- names (falling back to the resolved URL), and activates a result by
-- handing the app to `webapp-run launch`, which reuses a live service or
-- runs the original start command before opening its app window.
--
-- The provider only reads the shared channel; no subprocess runs until a
-- result is actually activated. Result rows carry no color field, so the
-- per-app status rides along in the subtitle ("<state> - <resolved_url>").

local function trim(value)
    return (tostring(value or ""):gsub("^%s+", ""):gsub("%s+$", ""))
end

local function shellQuote(value)
    return "'" .. tostring(value):gsub("'", "'\\''") .. "'"
end

local function tr(key, args)
    return noctalia.tr(key, args)
end

-- Same state vocabulary as the panel (its stateLabel): translated state
-- names, falling back to the raw token for unknown states.
local function stateLabel(state)
    local key = "state_" .. tostring(state)
    local text = noctalia.tr(key)
    return text ~= key and text or tostring(state)
end

-- Prefer the stable copy in the plugin data directory (the one .desktop
-- entries reference, kept fresh by the panel's ensureLauncher); fall back
-- to the bundled script for the window before the first panel open.
local function runnerPath()
    local dataDir = noctalia.expandPath("~/.local/share/webapp-maker")
    if dataDir ~= nil then
        local stable = dataDir .. "/webapp-run"
        if noctalia.fileExists(stable) then
            return stable
        end
    end
    return (noctalia.pluginDir() or ".") .. "/scripts/webapp-run"
end

local function sortedApps()
    local value = noctalia.state.get("webapps")
    local apps = {}
    if type(value) == "table" and type(value.apps) == "table" then
        for _, app in pairs(value.apps) do
            if type(app) == "table" then
                table.insert(apps, app)
            end
        end
    end
    table.sort(apps, function(a, b)
        return tostring(a.name or "") < tostring(b.name or "")
    end)
    return apps
end

function onQuery(text)
    -- `text` is echoed back verbatim in setResults (the host matches on it);
    -- matching itself runs against the trimmed form.
    local needle = trim(text)
    local results = {}
    for _, app in ipairs(sortedApps()) do
        local title = tostring(app.name or app.safe or "?")
        local score = 1 -- bare "wa " lists everything, sorted by name
        if needle ~= "" then
            score = noctalia.fuzzyScore(needle, title)
            if score == nil then
                local url = tostring(app.resolved_url or "")
                if url ~= "" then
                    score = noctalia.fuzzyScore(needle, url)
                end
            end
        end
        if score ~= nil then
            local subtitle = stateLabel(tostring(app.state or "unknown"))
            local err = tostring(app.error or "")
            if err ~= "" then
                subtitle = subtitle .. " - " .. err
            end
            local url = tostring(app.resolved_url or "")
            if url ~= "" then
                subtitle = subtitle .. " - " .. url
            end
            table.insert(results, {
                id = tostring(app.safe or title),
                title = title,
                subtitle = subtitle,
                glyph = "world",
                score = score,
            })
        end
    end
    launcher.setResults(text, results)
end

function onActivate(id)
    local name = trim(id)
    if name == "" then
        return
    end
    -- Fire-and-forget from the launcher's point of view: opening a window
    -- of a live app is instant, but a cold start runs the whole pipeline
    -- and can take seconds; the callback only surfaces a failure.
    --
    -- The supervisor only runs the bulk status once it believes something is
    -- live, so announce the launch up front; otherwise a cold start made from
    -- here would never show up in the panel or the desktop tile.
    local value = noctalia.state.get("webapps-wake")
    local wake = type(value) == "table" and tonumber(value.rev) or 0
    noctalia.state.set("webapps-wake", { rev = wake + 1 })
    local cmd = shellQuote(runnerPath()) .. " launch " .. shellQuote(name)
    local settled = false
    local function finish(result)
        if settled then
            return
        end
        settled = true
        if result.timedOut ~= true and (result.exitCode or 1) == 0 then
            return
        end
        local detail = trim(result.stderr)
        if detail == "" then
            detail = trim(result.stdout)
        end
        if detail ~= "" then
            noctalia.notifyError(tr("notify_app_title"), tr("notify_launch_failed", { detail = detail }))
        else
            noctalia.notifyError(tr("notify_app_title"), tr("notify_launch_failed_name", { name = name }))
        end
    end
    local accepted = noctalia.runAsync(cmd, finish)
    if accepted == false then
        finish({ exitCode = 1, stderr = tr("err_async_rejected") })
    end
end
