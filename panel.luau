--!nonstrict
-- Webapp Maker: simple browser launchers and local-service app windows.
-- Managed actions use the short-lived webapp-run CLI; one service entry
-- publishes bulk status on the shared "webapps" channel every two seconds.
-- Setup prepares stable scripts and the uv environment before installation,
-- so desktop entries continue working across plugin updates.
--
-- ui.input is uncontrolled: value seeds once per formRev, edits flow through
-- named onChange handlers into form. Toggles and selects are value-driven.

local URL_SOURCES = { "fixed", "regex", "script" }
local LOG_TAIL_BYTES = 16000 -- app.log tail shown in the log drawer

local form = {
    name = "", url = "", flags = "", icon = "",
    -- managed-app section (visible when the toggle is on)
    start_cmd = "", url_source = "fixed", url_regex = "", log_sample = "",
    extract_script = "", url_timeout = "15", cwd = "", auto_open = true,
}
local formRev = 0
local managedOpen = false -- collapsed managed section of the form
local busy = false
local status = nil -- { ok = boolean, text = string } create-form result

local apps = {} -- installed launchers: { name = basename, exec = Exec line }
local appsLoaded = false -- false until the first .desktop scan lands; empty is not loading
local webapps = nil -- "webapps" state value: { apps = {...}, rev = n }
local lastRev = nil
local uiState = {
    view = "list", query = "", queryRev = 0, selectedSafe = nil,
    detailTab = "overview", optionalOpen = false, formErrors = {}, panelEpoch = 0,
}
local logReads = {}
local logStatus = {}
local logFinalPending = {}
local configOps = {} -- safe -> load/save token; survives panel close
local edit = nil -- isolated snapshot/draft for the current configuration view
local configRev = 0
local scanToken = nil

local deleting = {} -- stable command owner -> true while removal runs
local acting = {} -- safe|action -> true while a CLI action runs
local logs = {} -- safe -> tail of the app's app.log
local cardStatus = {} -- safe -> { ok, text, state } last card-action result, shown on its card
local confirmRemove = {} -- safe -> true, never a sorted row index

local launcherReady = false
local launcherPreparing = false
local launcherCallbacks = {}
local regexPreview = nil -- { ok, text }
local regexBusy = false
local regexRev = 0
local opened = false

local render
local showList
local openDetail
local openConfiguration
local showDetailTab

local function tr(key, args)
    return noctalia.tr(key, args)
end

-- A rejected submission never gets a host callback. Complete that failure
-- locally so every caller releases its normal in-flight guard exactly once.
local function runAsync(cmd, callback)
    local settled = false
    local function finish(result)
        if settled then
            return
        end
        settled = true
        callback(result)
    end
    local accepted = noctalia.runAsync(cmd, finish)
    if accepted == false then
        local detail = tr("err_async_rejected")
        finish({ exitCode = 1, timedOut = false, stdout = detail, stderr = detail })
    end
    return accepted
end

local function cfg(key, fallback)
    -- getConfig returns nil until the setting is materialized in
    -- settings.toml; the manifest default is NOT substituted by the runtime.
    local value = noctalia.getConfig(key)
    if value == nil then
        return fallback
    end
    return value
end

local function trim(value)
    return (tostring(value or ""):gsub("^%s+", ""):gsub("%s+$", ""))
end

local function shellQuote(value)
    return "'" .. tostring(value):gsub("'", "'\\''") .. "'"
end

local function normalizeUrl(url)
    if url ~= "" and url:match("^%a[%w+.%-]*:") == nil then
        return "https://" .. url
    end
    return url
end

-- Fallback only: the CLI publishes each app's real directory name as
-- st.safe and every card prefers that (buildCards). This is reached solely
-- for a managed launcher that never started, which has no state dir, no
-- log and no conf lookup keyed on it -- the CLI is handed the DISPLAY name
-- instead, so the authoritative fold stays where it belongs (one
-- implementation, in webapp-run's safe_name).
--
-- It cannot be the same rule here: Lua's %w is ASCII-only and :lower() is
-- byte-wise, so this folds "DSH 工作" correctly only because the CJK bytes
-- survive the class; non-ASCII capitals ("Ünïcode App") still differ from
-- the CLI. Keeping that out of the CLI argument path is what makes it
-- harmless.
local function safeName(name)
    local folded = tostring(name or ""):lower():gsub("[^%w\128-\255]+", "-")
    return (folded:gsub("^-+", ""):gsub("-+$", ""))
end

-- A cold launcher has only the Lua display fallback until status publishes
-- the runner's authoritative Unicode safe. Keep each in-flight command's
-- owner stable while moving its visible selection to the authoritative safe.
local safeByName = {}
local guardOwners = {}

local function ownerSafe(safe)
    return guardOwners[safe] or safe
end

local function rememberIdentity(name, safe)
    local previous = safeByName[name]
    if previous ~= nil and previous ~= safe then
        guardOwners[safe] = ownerSafe(previous)
        if uiState.selectedSafe == previous then uiState.selectedSafe = safe end
        if confirmRemove[previous] then
            confirmRemove[safe] = true
            confirmRemove[previous] = nil
        end
    end
    safeByName[name] = safe
end

local function scriptPath(name)
    return (noctalia.pluginDir() or ".") .. "/scripts/" .. name
end

-- Stable DATA_ROOT is shared by setup and the generated desktop entries.
local function dataRoot()
    return noctalia.expandPath("~/.local/share/webapp-maker")
end

local function launcherPath()
    local dir = dataRoot()
    return dir ~= nil and (dir .. "/webapp-launch") or nil
end

local function runnerInstallPath()
    local dir = dataRoot()
    return dir ~= nil and (dir .. "/webapp-run") or nil
end

local function runnerPath()
    local stable = runnerInstallPath()
    if stable ~= nil and noctalia.fileExists(stable) then
        return stable
    end
    return scriptPath("webapp-run")
end

-- Keep the optional installation environment (for example icon-download
-- proxies) around both setup and installation, outside launch actions.
local function envPrefix()
    local envFile = trim(tostring(cfg("env_file", "")))
    if envFile == "" then
        return ""
    end
    return ". " .. shellQuote(envFile) .. " 2>/dev/null; "
end

local function ensureLauncher(callback)
    if launcherReady then
        if callback ~= nil then
            callback(true, "")
        end
        return
    end
    if callback ~= nil then
        table.insert(launcherCallbacks, callback)
    end
    if launcherPreparing then
        return
    end
    launcherPreparing = true
    local cmd = "{ " .. envPrefix() .. "bash " .. shellQuote(scriptPath("webapp-setup.sh")) .. "; } 2>&1"
    runAsync(cmd, function(result)
        launcherPreparing = false
        launcherReady = result.timedOut ~= true and (result.exitCode or 1) == 0
        local detail = trim(result.stdout)
        if detail == "" then
            detail = trim(result.stderr)
        end
        if #detail > 200 then
            detail = detail:sub(-200)
        end
        if not launcherReady then
            status = { ok = false, text = tr("err_setup", { detail = detail }) }
        end
        local callbacks = launcherCallbacks
        launcherCallbacks = {}
        for _, ready in ipairs(callbacks) do
            ready(launcherReady, detail)
        end
        if opened then
            render()
        end
    end)
end

-- ── Live state (published by the supervisor service entry) ──────────────────

-- The supervisor stops running the bulk status once every app is stopped, so a
-- mutation made here has to announce itself or the fresh state would never
-- appear. Bumping this revision costs no subprocess: it is pure shared state.
local wakeRev = 0
local function wakeSupervisor()
    wakeRev += 1
    noctalia.state.set("webapps-wake", { rev = wakeRev })
end

-- Numbers may arrive as strings depending on the service's JSON decoding;
-- every numeric field goes through tonumber before use.
local function num(value, fallback)
    local n = tonumber(value)
    if n == nil then
        return fallback
    end
    return n
end

local function stateApps()
    if type(webapps) == "table" and type(webapps.apps) == "table" then
        return webapps.apps
    end
    return {}
end

-- Mirrors desktop_widget.luau: a live service may still be waiting for its URL.
local LIVE = { starting = true, running = true }

local function fleetMetrics()
    local alive = 0
    local cpu = 0
    local mem = 0
    local cpuKnown = true
    local memKnown = true
    for _, st in pairs(stateApps()) do
        if type(st) == "table" and LIVE[tostring(st.state or "")] then
            alive += 1
            local c = num(st.cpu_pct, nil)
            local m = num(st.mem_mb, nil)
            if c == nil then
                cpuKnown = false
            else
                cpu += c
            end
            if m == nil then
                memKnown = false
            else
                mem += m
            end
        end
    end
    return alive, alive > 0 and cpuKnown and cpu / alive or nil,
        alive > 0 and memKnown and mem or nil
end

-- The app's current state word for a safe name, or nil when the bridge has
-- no entry for it; used to stamp and retire card action feedback.
local function appState(safe)
    for _, st in pairs(stateApps()) do
        if type(st) == "table"
            and (tostring(st.safe or "") == safe or safeName(st.name) == safe) then
            return tostring(st.state or "unknown")
        end
    end
    return nil
end

-- ── Log drawer ──────────────────────────────────────────────────────────────

local function logPath(safe)
    return "~/.local/state/webapp-maker/" .. safe .. "/app.log"
end

-- Read-only receipts are accepted only by their exact visible target. Business
-- commands below deliberately do not use the view epoch as their guard.
local readLog
readLog = function(safe)
    if not opened or uiState.view ~= "detail" or uiState.detailTab ~= "logs"
        or uiState.selectedSafe ~= safe or logReads[safe] ~= nil then
        return
    end
    local path = noctalia.expandPath(logPath(safe))
    if path == nil then
        logStatus[safe] = "failed"
        return
    end
    local token = {}
    local epoch = uiState.panelEpoch
    logReads[safe] = token
    if logStatus[safe] == nil then logStatus[safe] = "loading" end
    -- Exit 44 is our explicit missing-file marker. Permission/command failures
    -- retain their nonzero status and are never displayed as an empty log.
    local probe = "import os,sys\ntry: os.stat(sys.argv[1])\nexcept FileNotFoundError: sys.exit(44)\nexcept OSError: sys.exit(1)"
    local cmd = "python3 -c " .. shellQuote(probe) .. " " .. shellQuote(path)
        .. " && tail -c " .. tostring(LOG_TAIL_BYTES) .. " -- " .. shellQuote(path)
    runAsync(cmd, function(result)
        if logReads[safe] ~= token then return end
        logReads[safe] = nil
        if not opened or epoch ~= uiState.panelEpoch or uiState.view ~= "detail"
            or uiState.selectedSafe ~= safe or uiState.detailTab ~= "logs" then
            logFinalPending[safe] = nil
            return
        end
        if result.timedOut ~= true and result.exitCode == 44 then
            logs[safe] = nil
            logStatus[safe] = "empty"
        elseif result.timedOut == true or (result.exitCode or 1) ~= 0 then
            logs[safe] = nil
            logStatus[safe] = "failed"
        else
            logs[safe] = tostring(result.stdout or "")
            logStatus[safe] = logs[safe] == "" and "empty" or "content"
        end
        render()
        if logFinalPending[safe] then
            logFinalPending[safe] = nil
            readLog(safe)
        end
    end)
end

noctalia.state.watch("webapps", function(value)
    local previousState = uiState.selectedSafe ~= nil and appState(uiState.selectedSafe) or nil
    webapps = value
    for name, st in pairs(stateApps()) do
        if type(st) == "table" and trim(st.safe) ~= "" then
            rememberIdentity(tostring(st.name or name), tostring(st.safe))
        end
    end
    for safe, cs in pairs(cardStatus) do
        if appState(safe) ~= cs.state then cardStatus[safe] = nil end
    end
    local rev = type(value) == "table" and value.rev or nil
    if opened then
        if rev ~= lastRev and uiState.view == "detail" and uiState.detailTab == "logs"
            and uiState.selectedSafe ~= nil then
            local safe = uiState.selectedSafe
            local currentState = appState(safe)
            local justStopped = LIVE[previousState] == true and currentState == "stopped"
            if justStopped and logReads[safe] ~= nil then
                logFinalPending[safe] = true
            elseif LIVE[currentState] == true or justStopped then
                readLog(safe)
            end
        end
        render()
    end
    lastRev = rev
end)

local function leaveView()
    uiState.panelEpoch += 1
    confirmRemove = {}
end

showList = function()
    if busy and uiState.view == "create" then return end
    leaveView()
    uiState.view = "list"
    uiState.selectedSafe = nil
    edit = nil
    render()
end

local function showCreate()
    leaveView()
    uiState.view = "create"
    uiState.selectedSafe = nil
    render()
end

openDetail = function(safe)
    leaveView()
    uiState.view = "detail"
    uiState.selectedSafe = safe
    uiState.detailTab = "overview"
    edit = nil
    render()
end

showDetailTab = function(safe, tab)
    if uiState.selectedSafe ~= safe then return end
    leaveView()
    uiState.detailTab = tab
    if tab == "logs" then readLog(safe) end
    render()
end

function onFld_search(value)
    uiState.query = tostring(value or "")
    render()
end

-- ── Installed list (from .desktop files, like upstream) ─────────────────────

local function desktopValue(value)
    local escapes = { s = " ", n = "\n", t = "\t", r = "\r", ["\\"] = "\\" }
    return (tostring(value or ""):gsub("\\(.)", function(c) return escapes[c] or ("\\" .. c) end))
end

local function localIcon(value)
    local path = desktopValue(value)
    if path == "" or path:find("[%c]") ~= nil then return nil end
    if path:sub(1, 1) ~= "/" then
        if path:find("/", 1, true) ~= nil or path == "." or path == ".." then return nil end
        path = noctalia.expandPath("~/.local/share/icons/hicolor/256x256/apps/") .. path .. ".png"
    end
    return noctalia.fileExists(path) and path or nil
end

local function refreshApps()
    local token = {}
    scanToken = token
    local cmd = [[for f in "$HOME"/.local/share/applications/*.desktop; do ]] ..
        [[grep -q '^Exec=.*webapp-' "$f" 2>/dev/null || continue; ]] ..
        [[base=${f##*/}; printf '%s\t' "${base%.desktop}"; ]] ..
        [[ex=$(sed -n 's/^Exec=//p' "$f" | head -1); ]] ..
        [[ic=$(sed -n 's/^Icon=//p' "$f" | head -1); printf '%s\t%s\n' "$ex" "$ic"; done]]
    runAsync(cmd, function(result)
        if scanToken ~= token then return end
        scanToken = nil
        appsLoaded = true
        if result.timedOut == true or (result.exitCode or 1) ~= 0 then
            local detail = trim(result.stderr)
            if detail == "" then detail = trim(result.stdout) end
            status = { ok = false, text = detail ~= "" and detail or tr("err_cli") }
            render()
            return
        end
        apps = {}
        for line in (result.stdout or ""):gmatch("[^\n]+") do
            local name, exec, icon = line:match("^([^\t]+)\t([^\t]+)\t(.*)$")
            if name == nil then name, exec = line:match("^([^\t]+)\t(.+)$") end
            if name ~= nil and exec ~= nil then
                table.insert(apps, { name = name, exec = exec, iconPath = localIcon(icon) })
            end
        end
        table.sort(apps, function(a, b) return a.name:lower() < b.name:lower() end)
        render()
    end)
end

-- ── App actions (supervisor CLI / scripts, all runAsync) ────────────────────

local function shortOutput(result)
    local out = trim(result.stdout or "")
    if #out > 200 then
        out = out:sub(-200)
    end
    return out
end

-- actKey names the in-flight guard (buttons disable on it); it differs
-- from the CLI subcommand for launch, whose guard key is "open".
-- The CLI is handed card.cli (the display name) and the CLI folds it:
-- the panel never guesses the on-disk directory name, so an app whose name
-- the two foldings would spell differently can still be driven.
local function actionBlocked(card, allowOpen)
    local safe = card.safe
    return deleting[ownerSafe(safe)] == true or configOps[ownerSafe(safe)] ~= nil
        or acting[ownerSafe(safe) .. "|stop"] == true or acting[ownerSafe(safe) .. "|restart"] == true
        or (not allowOpen and acting[ownerSafe(safe) .. "|open"] == true)
end

local function runnerAction(action, card, actKey)
    local safe = card.safe
    local key = ownerSafe(safe) .. "|" .. actKey
    if acting[key] or actionBlocked(card, action == "stop") then
        return
    end
    acting[key] = true
    render()
    runAsync(shellQuote(runnerPath()) .. " " .. action .. " "
        .. shellQuote(card.cli or card.name) .. " 2>&1", function(result)
        acting[key] = nil
        if result.timedOut then
            cardStatus[safe] = { ok = false, text = tr("err_cli"), state = appState(safe) }
        elseif (result.exitCode or 1) ~= 0 then
            local out = shortOutput(result)
            cardStatus[safe] = { ok = false, text = out ~= "" and out or tr("err_cli"),
                state = appState(safe) }
        else
            cardStatus[safe] = { ok = true, text = tr("ok_done"), state = appState(safe) }
        end
        render()
    end)
end

-- Restart = stop (wait out the signal ladder) then the full launch
-- pipeline again; the two phases chain through the runAsync callbacks.
local function restartApp(card)
    local safe = card.safe
    local cli = card.cli or card.name
    local key = ownerSafe(safe) .. "|restart"
    if acting[key] or actionBlocked(card, true) then
        return
    end
    acting[key] = true
    render()
    local sup = shellQuote(runnerPath())
    runAsync(sup .. " stop " .. shellQuote(cli) .. " 2>&1", function(stopResult)
        if stopResult.timedOut == true or (stopResult.exitCode or 1) ~= 0 then
            acting[key] = nil
            local out = shortOutput(stopResult)
            cardStatus[safe] = { ok = false, text = out ~= "" and out or tr("err_cli"),
                state = appState(safe) }
            render()
            return
        end
        runAsync(sup .. " launch " .. shellQuote(cli) .. " 2>&1", function(launchResult)
            acting[key] = nil
            if launchResult.timedOut == true or (launchResult.exitCode or 1) ~= 0 then
                local out = shortOutput(launchResult)
                cardStatus[safe] = { ok = false, text = out ~= "" and out or tr("err_cli"),
                    state = appState(safe) }
            else
                cardStatus[safe] = { ok = true, text = tr("ok_done"), state = appState(safe) }
            end
            render()
        end)
    end)
end

local function openApp(card)
    if actionBlocked(card, false) then return end
    if card.managed then
        -- launch opens a window for a live app and runs the whole
        -- start command and URL extraction for a stopped one.
        runnerAction("launch", card, "open")
    elseif card.exec ~= nil then
        -- Desktop Entry escaping is not shell escaping. Let GLib decode
        -- Exec and its field codes instead of evaluating the line as shell.
        -- The guard only prevents double-fire (every exec would open
        -- another window); the appearing window itself is the feedback, so
        -- no status line is written.
        local key = ownerSafe(card.safe) .. "|open"
        if acting[key] then
            return
        end
        acting[key] = true
        render()
        local desktop = noctalia.expandPath("~/.local/share/applications/") .. card.name .. ".desktop"
        runAsync("gio launch " .. shellQuote(desktop), function(result)
            acting[key] = nil
            if result.timedOut == true or (result.exitCode or 1) ~= 0 then
                local out = shortOutput(result)
                cardStatus[card.safe] = { ok = false, text = out ~= "" and out or tr("err_cli"),
                    state = appState(card.safe) }
            else
                cardStatus[card.safe] = nil
            end
            render()
        end)
    end
end

local function removeApp(name, safe)
    if deleting[ownerSafe(safe)] == true or configOps[ownerSafe(safe)] ~= nil
        or acting[ownerSafe(safe) .. "|open"] or acting[ownerSafe(safe) .. "|stop"] or acting[ownerSafe(safe) .. "|restart"] then
        return
    end
    confirmRemove[safe] = nil
    deleting[ownerSafe(safe)] = true
    logs[safe] = nil
    render()
    runAsync("bash " .. shellQuote(scriptPath("webapp-remove.sh")) .. " " .. shellQuote(name) .. " 2>&1", function(result)
        deleting[ownerSafe(safe)] = nil
        if result.timedOut == true or (result.exitCode or 1) ~= 0 then
            local out = trim(result.stdout)
            cardStatus[safe] = { ok = false, text = out ~= "" and out or tr("err_remove_failed"),
                state = appState(safe) }
        else
            status = { ok = true, text = tr("ok_removed", { name = name }) }
            if opened and uiState.view == "configuration" and uiState.selectedSafe == safe then
                showList()
            end
        end
        render()
        refreshApps()
        wakeSupervisor()
    end)
end

-- ── Field handlers (uncontrolled inputs accumulate into `form`) ─────────────

local function clearFieldError(key)
    uiState.formErrors[key] = nil
end

function onFld_name(v) form.name = tostring(v or ""); clearFieldError("name") end
function onFld_url(v) form.url = tostring(v or ""); clearFieldError("url") end
function onFld_flags(v) form.flags = tostring(v or ""); clearFieldError("flags") end
function onFld_icon(v) form.icon = tostring(v or ""); clearFieldError("icon") end
function onFld_start_cmd(v) form.start_cmd = tostring(v or ""); clearFieldError("start_cmd") end
function onFld_extract_script(v) form.extract_script = tostring(v or ""); clearFieldError("extract_script") end
function onFld_url_timeout(v) form.url_timeout = tostring(v or ""); clearFieldError("url_timeout") end
function onFld_cwd(v) form.cwd = tostring(v or ""); clearFieldError("cwd") end

function onFld_url_regex(v)
    form.url_regex = tostring(v or "")
    clearFieldError("url_regex")
    regexRev += 1
    regexPreview = nil
end

function onFld_log_sample(v)
    form.log_sample = tostring(v or "")
    regexRev += 1
    regexPreview = nil
end

function onTgl_autoOpen(v)
    form.auto_open = v == true or v == "true"
end

function onTgl_managed(v)
    managedOpen = v == true or v == "true"
    render()
end

function onSel_urlSource(v)
    regexRev += 1
    regexPreview = nil
    local idx = tonumber(v)
    if idx ~= nil then
        form.url_source = URL_SOURCES[math.floor(idx) + 1] or "fixed"
    end
    render()
end

local function previewRegex()
    if regexBusy then
        return
    end
    local pattern = trim(form.url_regex)
    if pattern == "" or form.log_sample == "" then
        regexPreview = { ok = false, text = tr("err_preview_required") }
        render()
        return
    end
    local sample = form.log_sample
    local revision = regexRev
    local currentForm = formRev
    local epoch = uiState.panelEpoch
    regexBusy = true
    regexPreview = nil
    render()
    ensureLauncher(function(ready, detail)
        if not ready then
            regexBusy = false
            regexPreview = { ok = false, text = tr("err_setup", { detail = detail }) }
            render()
            return
        end
        local cmd = shellQuote(runnerPath()) .. " match --regex " .. shellQuote(pattern)
            .. " --line " .. shellQuote(sample)
        runAsync(cmd, function(result)
            regexBusy = false
            if revision ~= regexRev or currentForm ~= formRev or epoch ~= uiState.panelEpoch then
                render()
                return
            end
            if result.timedOut == true or (result.exitCode or 1) ~= 0 then
                local err = trim(result.stderr)
                regexPreview = { ok = false, text = err ~= "" and err or tr("err_preview") }
            else
                local ok, data = pcall(noctalia.json.decode, result.stdout or "")
                if not ok or type(data) ~= "table" then
                    regexPreview = { ok = false, text = tr("err_preview") }
                elseif type(data.url) == "string" and data.url ~= "" then
                    regexPreview = { ok = true, text = tr("preview_url", { url = data.url }) }
                else
                    regexPreview = { ok = false, text = tr("preview_no_match") }
                end
            end
            render()
        end)
    end)
end

-- ── Creating ────────────────────────────────────────────────────────────────

local function create()
    if busy then
        return
    end
    local name = trim(form.name)
    local source = form.url_source or "fixed"
    local managed = managedOpen
    local url = (not managed or source == "fixed") and normalizeUrl(trim(form.url)) or ""
    local startCmd = trim(form.start_cmd)
    uiState.formErrors = {}
    local errorFields = { err_name_required = "name", err_slash = "name", err_required = "url",
        err_start_required = "start_cmd", err_regex_required = "url_regex",
        err_script_required = "extract_script", err_number = "url_timeout" }
    local function reject(key)
        if errorFields[key] ~= nil then uiState.formErrors[errorFields[key]] = tr(key) end
        status = { ok = false, text = tr(key) }
        render()
    end
    if name == "" then
        reject("err_name_required")
        return
    end
    if (not managed or source == "fixed") and url == "" then
        reject("err_required")
        return
    end
    if name:find("/", 1, true) ~= nil then
        reject("err_slash")
        return
    end
    local timeout = trim(form.url_timeout)
    if timeout == "" then
        timeout = "15"
    end
    if managed then
        if startCmd == "" then
            reject("err_start_required")
            return
        end
        if source == "regex" and trim(form.url_regex) == "" then
            reject("err_regex_required")
            return
        end
        if source == "script" and trim(form.extract_script) == "" then
            reject("err_script_required")
            return
        end
        local seconds = tonumber(timeout)
        if seconds == nil or seconds ~= seconds or seconds == math.huge or seconds <= 0 then
            reject("err_number")
            return
        end
    end
    if dataRoot() == nil then
        reject("err_failed")
        return
    end

    local args = {}
    if managed then
        table.insert(args, "--runner " .. shellQuote(runnerInstallPath()))
        table.insert(args, "--start-cmd " .. shellQuote(startCmd))
        table.insert(args, "--url-source " .. shellQuote(source))
        if source == "regex" then
            table.insert(args, "--url-regex " .. shellQuote(trim(form.url_regex)))
        elseif source == "script" then
            table.insert(args, "--extract-script " .. shellQuote(trim(form.extract_script)))
        end
        table.insert(args, "--url-timeout " .. shellQuote(timeout))
        if trim(form.cwd) ~= "" then
            table.insert(args, "--cwd " .. shellQuote(trim(form.cwd)))
        end
        table.insert(args, "--auto-open=" .. (form.auto_open == true and "true" or "false"))
    else
        table.insert(args, "--launcher " .. shellQuote(launcherPath()))
    end
    if trim(form.flags) ~= "" then
        table.insert(args, "--flags " .. shellQuote(trim(form.flags)))
    end
    table.insert(args, shellQuote(name))
    table.insert(args, shellQuote(url))
    if trim(form.icon) ~= "" then
        table.insert(args, shellQuote(trim(form.icon)))
    end
    -- Snapshot form arguments before setup's asynchronous callback. Installation
    -- may start only after the stable runner and environment are ready.
    local cmd = "{ " .. envPrefix() .. "bash " .. shellQuote(scriptPath("webapp-install.sh")) .. " "
        .. table.concat(args, " ") .. "; } 2>&1"
    local epoch = uiState.panelEpoch
    busy = true
    status = nil
    render()
    ensureLauncher(function(ready, detail)
        if not ready then
            busy = false
            status = { ok = false, text = tr("err_setup", { detail = detail }) }
            render()
            return
        end
        runAsync(cmd, function(result)
            busy = false
            if result.timedOut then
                status = { ok = false, text = tr("err_timeout") }
            elseif (result.exitCode or 1) ~= 0 then
                local out = trim(result.stdout)
                if #out > 200 then
                    out = out:sub(-200)
                end
                status = { ok = false, text = out ~= "" and out or tr("err_failed") }
            else
                status = { ok = true, text = tr("ok_created", { name = name }) }
                if opened and uiState.view == "create" and uiState.panelEpoch == epoch then
                    uiState.view = "list"
                end
                uiState.optionalOpen = false
                uiState.formErrors = {}
                cardStatus[safeName(name)] = nil
                form = {
                    name = "", url = "", flags = "", icon = "",
                    start_cmd = "", url_source = "fixed", url_regex = "", log_sample = "",
                    extract_script = "", url_timeout = "15", cwd = "", auto_open = true,
                }
                regexPreview = nil
                regexRev += 1
                formRev += 1
                wakeSupervisor() -- a new app exists; make the supervisor notice
            end
            render()
            refreshApps()
        end)
    end)
end

-- ── Render helpers ──────────────────────────────────────────────────────────

local function field(key, labelKey, phKey, grow)
    -- grow: inside a ui.row the column must claim an equal share, or the
    -- input shrinks to its placeholder width and clips the value.
    -- maxLines = 1: a wrapped label would make this column taller than its
    -- neighbour and push the two inputs out of alignment (Chinese labels are
    -- longer than the English they replaced).
    local children = {
        ui.label({ text = tr(labelKey), fontSize = 11, color = "on_surface_variant", maxLines = 1 }),
        ui.input({ key = "fld-" .. key .. "-" .. formRev, value = form[key],
            placeholder = tr(phKey), onChange = "onFld_" .. key, enabled = not busy }),
    }
    if uiState.formErrors[key] ~= nil then
        table.insert(children, ui.label({ text = uiState.formErrors[key], fontSize = 11, color = "error" }))
    end
    return ui.column({ gap = 4, align = "stretch", flexGrow = grow or 0 }, children)
end

local function stateColor(state)
    -- Runtime state and the independent error text share the desktop palette.
    if state == "running" then
        return "primary"
    elseif state == "starting" then
        return "secondary"
    end
    return "on_surface_variant" -- stopped / unknown
end

local function stateLabel(state)
    local key = "state_" .. tostring(state)
    local text = tr(key)
    return text ~= key and text or tostring(state)
end

local function fmtUptime(secs)
    secs = math.max(math.floor(num(secs, 0)), 0)
    if secs < 60 then
        return string.format("%ds", secs)
    end
    local m = math.floor(secs / 60)
    if m < 60 then
        return string.format("%dm %02ds", m, secs % 60)
    end
    local h = math.floor(m / 60)
    if h < 24 then
        return string.format("%dh %02dm", h, math.floor(m % 60))
    end
    return string.format("%dd %dh", math.floor(h / 24), math.floor(h % 24))
end

-- ui.select is index-based and 0-based like its QML ComboBox counterpart
-- (the docs do not spell the base out; onSel_urlSource tolerates either
-- string or number).
local function urlSourceIndex(mode)
    for i, m in ipairs(URL_SOURCES) do
        if m == mode then
            return i - 1
        end
    end
    return 0
end

-- metrics rings arrive as { cpu = {...}, mem = {...} } of 0..1 samples
-- (newest last); values are re-validated in case they decode as strings.
local function numSeries(arr)
    local out = {}
    if type(arr) == "table" then
        for i = 1, #arr do
            local n = tonumber(arr[i])
            if n ~= nil then
                table.insert(out, math.min(math.max(n, 0), 1))
            end
        end
    end
    return out
end

-- One card per installed launcher, merged with its live state entry. The
-- service indexes apps by name; to be key-agnostic the lookup accepts the
-- display name, the safe name and the raw key.
-- Unknown status and starts stay above running and stopped apps; names
-- break ties inside a band. Errors are displayed independently of state.
local function stateRank(state)
    if state == "unknown" then
        return 0
    elseif state == "starting" then
        return 1
    elseif state == "running" then
        return 2
    end
    return 3
end

local function buildCards()
    local live = stateApps()
    local byName = {}
    for key, st in pairs(live) do
        if type(st) == "table" then
            local disp = st.name ~= nil and st.name ~= "" and tostring(st.name) or tostring(key)
            byName[disp] = st
            if tostring(st.safe or "") ~= "" then
                byName[tostring(st.safe)] = st
            end
            byName[safeName(disp)] = st
            byName[key] = st
        end
    end

    local cards = {}
    local claimed = {}
    for _, entry in ipairs(apps) do
        local st = byName[entry.name] or byName[safeName(entry.name)]
        local managed = st ~= nil or entry.exec:find("webapp-run", 1, true) ~= nil
            or entry.exec:find("webapp-supervisor", 1, true) ~= nil
        -- Prefer the directory name the CLI published: it is the one
        -- authoritative fold, so the card's UI keys, log path and state
        -- lookup all stay on the exact string on disk. safeName() only
        -- covers a launcher that never started (no state entry at all).
        local safe = st ~= nil and tostring(st.safe or "") ~= ""
            and tostring(st.safe) or safeByName[entry.name] or safeName(entry.name)
        rememberIdentity(entry.name, safe)
        if st ~= nil then
            claimed[safe] = true
        end
        table.insert(cards, {
            -- cli: the argument every CLI call gets. The CLI folds it
            -- with safe_name itself, so the display name is always right --
            -- the panel never has to reproduce that rule.
            name = entry.name, cli = entry.name, safe = safe,
            exec = entry.exec, st = st, managed = managed, iconPath = entry.iconPath,
        })
    end
    -- State entries whose launcher is gone (removed .desktop) still show up:
    -- they can be stopped and their leftover directories pointed at.
    for key, st in pairs(live) do
        if type(st) == "table" then
            local disp = st.name ~= nil and st.name ~= "" and tostring(st.name) or tostring(key)
            local safe = tostring(st.safe or "") ~= "" and tostring(st.safe) or safeName(disp)
            rememberIdentity(disp, safe)
            if not claimed[safe] then
                claimed[safe] = true
                table.insert(cards, { name = disp, cli = disp, safe = safe, exec = nil, st = st, managed = true })
            end
        end
    end
    table.sort(cards, function(a, b)
        local ra = a.st ~= nil and stateRank(tostring(a.st.state or "unknown")) or 3
        local rb = b.st ~= nil and stateRank(tostring(b.st.state or "unknown")) or 3
        if ra ~= rb then
            return ra < rb
        end
        return a.name:lower() < b.name:lower()
    end)
    return cards
end

-- ── View composition ──────────────────────────────────────────────────────

local function visibleCards(cards, query)
    local needle = trim(query):lower()
    local result = {}
    for _, card in ipairs(cards) do
        if needle == "" or card.name:lower():find(needle, 1, true) ~= nil then
            table.insert(result, card)
        end
    end
    return result
end

local function hint(text, color)
    return ui.label({ text = text, fontSize = 11, color = color or "on_surface_variant" })
end

local function iconButton(key, glyph, label, enabled, callback, variant)
    -- Native Tabler vector glyphs preserve theming, disabled state and tooltips.
    -- No container tooltips (API 32) or context-menu APIs are required.
    return ui.button({ key = key, glyph = glyph, glyphSize = 20, width = 36, height = 36,
        tooltip = label, variant = variant or "ghost", enabled = enabled, onClick = callback })
end

local function closeNode()
    return iconButton("close-panel", "close", tr("tip_close"), true, function() panel.close() end)
end

local function headerNode(title, back, disabled)
    local nodes = {}
    if back ~= nil then
        table.insert(nodes, iconButton("back-navigation", "chevron-left", tr("btn_back_to_list"),
            not disabled, back))
    end
    table.insert(nodes, ui.label({ text = title, fontSize = 16, fontWeight = "bold",
        color = "on_surface", flexGrow = 1, maxLines = 1 }))
    if uiState.view == "list" then
        table.insert(nodes, ui.button({ key = "form-toggle", text = tr("btn_new_app"),
            glyph = "plus", controlSize = "sm", onClick = showCreate }))
    end
    table.insert(nodes, closeNode())
    return ui.row({ height = 40, gap = 8, align = "center" }, nodes)
end

local function bridgeUnavailable()
    if type(webapps) == "table" then return webapps.available == false end
    for _, entry in ipairs(apps) do
        if entry.exec:find("webapp-run", 1, true) or entry.exec:find("webapp-supervisor", 1, true) then
            return true
        end
    end
    return false
end

local function overviewNode()
    if bridgeUnavailable() then
        local detail = type(webapps) == "table" and trim(webapps.error) or ""
        return ui.column({ minHeight = 64, padding = 10, radius = 12, fill = "surface_variant/0.6", gap = 4 }, {
            hint(tr("overview_no_bridge"), "secondary"), hint(detail, "secondary"),
        })
    end
    local count, cpu, mem = fleetMetrics()
    local function stat(caption, value)
        return ui.column({ flexGrow = 1, gap = 4 }, {
            hint(caption), ui.label({ text = value, fontSize = 18, fontWeight = "bold", color = "on_surface" }),
        })
    end
    return ui.row({ height = 64, padding = 10, radius = 12, fill = "surface_variant/0.6", gap = 8 }, {
        stat(tr("overview_active"), tostring(count)),
        stat(tr("overview_cpu_title"), tr("overview_cpu", { pct = cpu ~= nil and string.format("%.0f", cpu) or "—" })),
        stat(tr("overview_mem_title"), tr("overview_mem", { mb = mem ~= nil and string.format("%.0f", mem) or "—" })),
    })
end

local function formFeedbackNode()
    if busy then return hint(tr(launcherPreparing and "status_preparing" or "status_busy"), "secondary") end
    if status ~= nil then return hint(status.text, status.ok and "primary" or "error") end
    return nil
end

local function actionFeedbackNode(card)
    local cs = cardStatus[card.safe] or cardStatus[ownerSafe(card.safe)]
    return cs ~= nil and hint(tr("action_ctx", { name = card.name, detail = cs.text }),
        cs.ok and "primary" or "error") or nil
end

local function actionNodes(card, detail)
    local safe = card.safe
    local nodes = {}
    local openBusy = acting[ownerSafe(safe) .. "|open"] == true
    local restartBusy = acting[ownerSafe(safe) .. "|restart"] == true
    local stopBusy = acting[ownerSafe(safe) .. "|stop"] == true
    local blocked = deleting[ownerSafe(safe)] == true or configOps[ownerSafe(safe)] ~= nil
    table.insert(nodes, iconButton("open-" .. safe .. (openBusy and "-off" or ""), "external-link",
        tr("btn_open"), not blocked and not openBusy and not restartBusy and not stopBusy,
        function() openApp(card) end, "secondary"))
    if detail and card.managed then
        table.insert(nodes, iconButton("restart-" .. safe .. (restartBusy and "-off" or ""), "refresh",
            tr("btn_restart"), not blocked and not restartBusy and not stopBusy,
            function() restartApp(card) end))
        table.insert(nodes, iconButton("stop-" .. safe .. (stopBusy and "-off" or ""), "player-stop",
            tr("btn_stop"), not blocked and not stopBusy and not restartBusy,
            function() runnerAction("stop", card, "stop") end))
    end
    return nodes
end

local function editButtonNode(card)
    return iconButton("edit-" .. card.safe, "pencil", tr("btn_edit_configuration"),
        not actionBlocked(card, false), function() openConfiguration(card) end)
end

local function appRowNode(card)
    local st = card.st
    local state = st ~= nil and tostring(st.state or "unknown") or nil
    local text = { ui.label({ text = card.name, fontSize = 14, fontWeight = "bold", color = "on_surface",
        maxLines = card.managed and 1 or nil }) }
    local caption = state and stateLabel(state) or tr(card.managed and "state_installed" or "state_simple")
    if card.managed and st ~= nil and LIVE[state] and not bridgeUnavailable() then
        caption = caption .. " · " .. tr("overview_cpu", { pct = num(st.cpu_pct, nil) ~= nil
            and string.format("%.0f", num(st.cpu_pct, nil)) or "—" }) .. " · "
            .. tr("overview_mem", { mb = num(st.mem_mb, nil) ~= nil and string.format("%.0f", num(st.mem_mb, nil)) or "—" })
    end
    table.insert(text, hint(caption))
    if card.exec == nil then table.insert(text, hint(tr("missing_launcher"))) end
    local summary = ui.row({ key = "card-" .. card.safe, flexGrow = 1, gap = 10, align = "center",
        onClick = card.managed and function() openDetail(card.safe) end or nil }, {
        ui.glyph({ name = "world", size = 28, color = "on_surface_variant" }),
        ui.column({ flexGrow = 1, gap = 4 }, text),
        ui.box({ key = "dot-" .. card.safe, width = 8, height = 8, radius = 4,
            fill = st ~= nil and trim(st.error) ~= "" and "error" or stateColor(state) }),
    })
    -- Actions are siblings of the click target, so opening never also navigates.
    local row = { summary }
    for _, node in ipairs(actionNodes(card, false)) do table.insert(row, node) end
    if not card.managed then table.insert(row, editButtonNode(card)) end
    local children = { ui.row({ minHeight = 64, gap = 8, align = "center" }, row) }
    if st ~= nil and trim(st.error) ~= "" then table.insert(children, hint(tostring(st.error), "error")) end
    local feedback = actionFeedbackNode(card)
    if feedback then table.insert(children, feedback) end
    return ui.column({ key = "appcard-" .. card.safe, gap = 4, paddingH = 8,
        radius = 8, fill = "surface_variant/0.4" }, children)
end

local function listViewNode(cards)
    local filtered = visibleCards(cards, uiState.query)
    local children = { headerNode(tr("title")), overviewNode(),
        ui.row({ height = 36, gap = 8, align = "center" }, {
            ui.glyph({ name = "search", size = 18, color = "on_surface_variant" }),
            ui.input({ key = "search-" .. uiState.queryRev, value = uiState.query, height = 36,
                flexGrow = 1, placeholder = tr("search_apps"), onChange = "onFld_search" }),
            iconButton("clear-search", "close", tr("btn_clear_search"), uiState.query ~= "", function()
                uiState.query = ""; uiState.queryRev = uiState.queryRev + 1; render()
            end),
        }),
        ui.row({ gap = 8, align = "center" }, {
            ui.label({ text = tr("installed_title"), fontSize = 12, fontWeight = "bold", flexGrow = 1 }),
            hint(tr("apps_count", { shown = #filtered, total = #cards })),
        }),
    }
    if not appsLoaded and #cards == 0 then
        table.insert(children, hint(tr("installed_loading")))
        table.insert(children, ui.spacer({ flexGrow = 1 }))
    elseif #cards == 0 then
        table.insert(children, ui.column({ flexGrow = 1, gap = 12, align = "center", justify = "center" }, {
            ui.glyph({ name = "world-plus", size = 28, color = "on_surface_variant" }),
            hint(tr("installed_empty")), hint(tr("installed_empty_hint")),
            ui.button({ key = "empty-open-form", text = tr("btn_new_app"), onClick = showCreate }),
        }))
    elseif #filtered == 0 then
        table.insert(children, hint(tr("search_no_match")))
        table.insert(children, ui.spacer({ flexGrow = 1 }))
    else
        local rows = {}
        for _, card in ipairs(filtered) do table.insert(rows, appRowNode(card)) end
        table.insert(children, ui.scroll({ key = "apps", flexGrow = 1, gap = 8 }, rows))
    end
    local feedback = formFeedbackNode()
    if feedback then table.insert(children, feedback) end
    return ui.column({ flexGrow = 1, gap = 12 }, children)
end

local function optionalFieldsNode()
    local children = { ui.button({ key = "optional-toggle", text = tr("optional_settings"),
        glyph = uiState.optionalOpen and "chevron-up" or "chevron-down", variant = "ghost",
        onClick = function() uiState.optionalOpen = not uiState.optionalOpen; render() end }) }
    if uiState.optionalOpen then
        table.insert(children, field("flags", "field_flags", "field_flags_ph", 1))
        table.insert(children, field("icon", "field_icon", "field_icon_ph", 1))
    end
    return ui.column({ gap = 8, padding = 10, radius = 12, fill = "surface_variant/0.4" }, children)
end

local function createViewNode()
    local fields = { field("name", "field_name", "field_name_ph", 1),
        ui.row({ gap = 8, align = "center" }, {
            ui.label({ text = tr("managed_toggle"), fontSize = 12, flexGrow = 1 }),
            ui.toggle({ key = "tgl-managed-" .. formRev, checked = managedOpen, enabled = not busy,
                onChange = "onTgl_managed" }),
        }),
    }
    if managedOpen then
        table.insert(fields, field("start_cmd", "field_start_cmd", "field_start_cmd_ph", 1))
        table.insert(fields, hint(tr("field_url_source")))
        table.insert(fields, ui.select({ key = "sel-urlsource-" .. formRev,
            options = { tr("url_source_fixed"), tr("url_source_regex"), tr("url_source_script") },
            selectedIndex = urlSourceIndex(form.url_source), enabled = not busy, onChange = "onSel_urlSource" }))
    end
    if not managedOpen or form.url_source == "fixed" then
        table.insert(fields, field("url", "field_url", "field_url_ph", 1))
    elseif form.url_source == "regex" then
        table.insert(fields, field("url_regex", "field_url_regex", "field_url_regex_ph", 1))
        table.insert(fields, hint(tr("help_url_regex")))
        table.insert(fields, field("log_sample", "field_log_sample", "field_log_sample_ph", 1))
        table.insert(fields, ui.button({ key = "regex-preview" .. (regexBusy and "-off" or ""),
            text = tr("btn_preview_regex"), controlSize = "sm", enabled = not regexBusy and not busy, onClick = previewRegex }))
        if regexPreview then table.insert(fields, hint(regexPreview.text, regexPreview.ok and "primary" or "error")) end
    else
        table.insert(fields, field("extract_script", "field_extract_script", "field_extract_script_ph", 1))
        table.insert(fields, hint(tr("help_extract_script")))
    end
    if managedOpen then
        table.insert(fields, field("cwd", "field_cwd", "field_cwd_ph", 1))
        table.insert(fields, field("url_timeout", "field_url_timeout", "field_url_timeout_ph", 1))
        table.insert(fields, ui.row({ gap = 8, align = "center" }, {
            ui.label({ text = tr("field_auto_open"), fontSize = 12, flexGrow = 1 }),
            ui.toggle({ key = "tgl-auto-open-" .. formRev, checked = form.auto_open == true,
                enabled = not busy, onChange = "onTgl_autoOpen" }),
        }))
    end
    table.insert(fields, optionalFieldsNode())
    local feedback = formFeedbackNode()
    if feedback then table.insert(fields, 1, feedback) end
    return ui.column({ flexGrow = 1, gap = 12 }, {
        headerNode(tr("form_title"), showList, busy),
        ui.scroll({ key = "form-scroll", flexGrow = 1, gap = 12 }, fields),
        ui.row({ gap = 8 }, {
            ui.button({ key = "back-to-list", text = tr("btn_back_to_list"), enabled = not busy,
                variant = "ghost", flexGrow = 1, controlSize = "sm", onClick = showList }),
            ui.button({ key = "create" .. (busy and "-off" or ""), text = tr("btn_create"),
                enabled = not busy, flexGrow = 1, controlSize = "sm", onClick = create }),
        }),
    })
end

local function metricCard(card, cpu)
    local st = card.st or {}
    local valid = LIVE[tostring(st.state)] == true and not bridgeUnavailable()
    local value = valid and num(cpu and st.cpu_pct or st.mem_mb, nil) or nil
    -- Lua's `and/or` cannot select a nil CPU without falling through to MEM.
    if cpu then value = valid and num(st.cpu_pct, nil) or nil end
    local samples
    if type(st.metrics) == "table" then
        if cpu then samples = st.metrics.cpu else samples = st.metrics.mem end
    end
    local series = valid and value ~= nil and numSeries(samples) or {}
    local nodes = {
        hint(tr(cpu and "card_cpu" or "card_mem")),
        ui.label({ text = value ~= nil and string.format(cpu and "%.0f%%" or "%.0f MB", value) or "—",
            fontSize = 26, fontWeight = "bold", color = "on_surface", maxLines = 1 }),
    }
    if #series > 0 then
        table.insert(nodes, ui.graph({ key = "graph-" .. (cpu and "cpu-" or "mem-") .. card.safe,
            values = series, color = cpu and "primary" or "secondary", height = 68,
            flexGrow = 0, lineWidth = 1, fillOpacity = 0.15 }))
    else
        table.insert(nodes, ui.column({ height = 68, justify = "center" }, { hint(tr("metrics_no_samples")) }))
    end
    table.insert(nodes, hint(tr(cpu and "scale_cpu" or "scale_mem")))
    return ui.column({ height = 164, flexGrow = 1, padding = 10, gap = 4,
        radius = 12, fill = "surface_variant/0.6" }, nodes)
end

local function metricsNode(card)
    local st = card.st or {}
    local live = LIVE[tostring(st.state)] == true and not bridgeUnavailable()
    local url = trim(st.resolved_url)
    local pid = live and num(st.pid, 0) > 0 and tostring(st.pid) or "—"
    local port = live and num(st.port, 0) > 0 and tostring(st.port) or "—"
    local uptime = live and num(st.started_epoch, 0) > 0 and num(st.updated_epoch, 0) > num(st.started_epoch, 0)
        and fmtUptime(num(st.updated_epoch, 0) - num(st.started_epoch, 0)) or "—"
    return ui.column({ gap = 12 }, {
        ui.row({ gap = 8 }, { hint(tr("resource_usage")), ui.spacer({ flexGrow = 1 }), hint(tr("metrics_samples")) }),
        ui.row({ gap = 12 }, { metricCard(card, true), metricCard(card, false) }),
        ui.column({ minHeight = 142, padding = 12, gap = 8, radius = 12, fill = "surface_variant/0.4" }, {
            hint(tr("connection_url")),
            ui.label({ text = url ~= "" and url or tr("url_unavailable"), fontSize = 12, color = "on_surface" }),
            ui.separator({}),
            ui.row({ gap = 8 }, {
                ui.column({ flexGrow = 1, gap = 4 }, { hint(tr("process_pid")), hint(pid, "on_surface") }),
                ui.column({ flexGrow = 1, gap = 4 }, { hint(tr("process_port")), hint(port, "on_surface") }),
                ui.column({ flexGrow = 1, gap = 4 }, { hint(tr("process_uptime")), hint(uptime, "on_surface") }),
            }),
            hint(tr("service_window_independent")),
        }),
        hint(tr("metrics_scope")),
    })
end

local function logsNode(card)
    local text = logs[card.safe]
    local state = logStatus[card.safe]
    local body
    if state == "failed" then body = tr("logs_read_failed")
    elseif state == nil or state == "loading" then body = tr("logs_loading")
    elseif text == nil or text == "" then body = tr("logs_empty")
    else
        local longest = 2
        for ticks in text:gmatch("`+") do longest = math.max(longest, #ticks) end
        local fence = string.rep("`", longest + 1)
        body = fence .. "\n" .. text .. "\n" .. fence
    end
    return ui.column({ key = "log-page-" .. card.safe, flexGrow = 1, gap = 8 }, {
        ui.row({ gap = 8, align = "center" }, {
            ui.label({ text = tr("service_logs"), fontSize = 12, color = "on_surface", flexGrow = 1 }),
            ui.button({ key = "retry-log-" .. card.safe, text = tr("btn_retry"), variant = "ghost", controlSize = "sm",
                enabled = logReads[card.safe] == nil, onClick = function() readLog(card.safe); render() end }),
        }),
        hint(tr("logs_tail", { bytes = LOG_TAIL_BYTES })),
        -- Grow inside an exact-height column, never inside another scroll's
        -- unbounded content measurement. The log owns the remaining viewport.
        ui.scroll({ key = "logscroll-" .. card.safe, flexGrow = 1, stickToBottom = true,
            fill = "surface_variant/0.4", radius = 12, padding = 10 }, { ui.markdown({ text = body }) }),
    })
end

local function detailNode(card)
    local st = card.st or {}
    local state = st.state ~= nil and stateLabel(st.state) or tr("state_installed")
    local preview = card.iconPath ~= nil and ui.image({ path = card.iconPath, width = 112, height = 112, fit = "contain" })
        or ui.column({ width = 112, height = 112, align = "center", justify = "center", radius = 12,
            fill = "surface_variant/0.6" }, { ui.glyph({ name = "world", size = 72, color = "on_surface_variant" }) })
    local content = { ui.row({ key = "detail-hero-" .. card.safe, minHeight = 120, gap = 20, align = "center" }, {
        preview,
        ui.column({ flexGrow = 1, gap = 8 }, {
            ui.row({ minHeight = 40, gap = 8, align = "center" }, {
                ui.column({ flexGrow = 1, gap = 4 }, {
                    ui.label({ text = card.name, fontSize = 14, fontWeight = "bold", maxLines = 2, color = "on_surface" }),
                    hint(state .. " · " .. tr("kind_managed")),
                }), editButtonNode(card),
            }),
            ui.row({ height = 36, gap = 10 }, actionNodes(card, true)),
            ui.row({ height = 28, gap = 4 }, {
                ui.button({ key = "detail-overview-" .. card.safe, text = tr("tab_overview"), height = 28,
                    flexGrow = 1, variant = "ghost", selected = uiState.detailTab == "overview",
                    onClick = function() showDetailTab(card.safe, "overview") end }),
                ui.button({ key = "detail-logs-" .. card.safe, text = tr("tab_logs"), height = 28,
                    flexGrow = 1, variant = "ghost", selected = uiState.detailTab == "logs",
                    onClick = function() showDetailTab(card.safe, "logs") end }),
            }),
        }),
    }) }
    if bridgeUnavailable() then table.insert(content, hint(tr("overview_no_bridge"), "secondary")) end
    if trim(st.error) ~= "" then table.insert(content, hint(tostring(st.error), "error")) end
    local feedback = actionFeedbackNode(card)
    if feedback then table.insert(content, feedback) end
    local navigation = ui.row({ key = "detail-navigation", height = 36, align = "center" }, {
        iconButton("back-to-list", "chevron-left", tr("btn_back_to_list"), true, showList),
        ui.spacer({ flexGrow = 1 }), closeNode(),
    })
    -- Keep the hero and its buttons at the same native parent/key on both
    -- tabs. Reparenting the hero recreates every button and triggers the host's
    -- initial-variant animation (Noctalia upstream issue/PR 5012).
    local children = { navigation }
    for _, child in ipairs(content) do table.insert(children, child) end
    if uiState.detailTab == "logs" then
        table.insert(children, logsNode(card))
    else
        table.insert(children, ui.scroll({ key = "detail-metrics-" .. card.safe,
            flexGrow = 1, gap = 12 }, { metricsNode(card) }))
    end
    return ui.column({ flexGrow = 1, gap = 12 }, children)
end
-- ── Configuration: original fields, optimistic revision, isolated draft ────

local function configDirty(context)
    if context == nil or context.snapshot == nil then return false end
    if trim(context.icon or "") ~= "" or context.recoveryConfirmed == true then return true end
    for key, value in pairs(context.draft) do
        if value ~= context.snapshot[key] then return true end
    end
    return false
end

local function configError(result, data)
    if type(data) == "table" and data.code == "conflict" then return tr("config_conflict") end
    return tr("config_failed") .. (type(data) == "table" and type(data.message) == "string"
        and (": " .. data.message) or "")
end

local function readConfig(card)
    local context = edit
    if context == nil or ownerSafe(context.safe) ~= ownerSafe(card.safe) then return end
    if actionBlocked(card, false) then
        context.feedback = { ok = false, text = tr("config_busy") }
        render()
        return
    end
    local token = {}
    configOps[ownerSafe(card.safe)] = token
    context.loading = true
    context.feedback = nil
    local epoch = uiState.panelEpoch
    runAsync(shellQuote(runnerPath()) .. " config-read " .. shellQuote(card.cli or card.name), function(result)
        if configOps[ownerSafe(card.safe)] ~= token then return end
        configOps[ownerSafe(card.safe)] = nil
        context.loading = false
        if not opened or edit ~= context or epoch ~= uiState.panelEpoch then return end
        local ok, data = pcall(noctalia.json.decode, result.stdout or "")
        if result.timedOut == true or (result.exitCode or 1) ~= 0 or not ok or type(data) ~= "table"
            or data.schema_version ~= 1 or type(data.fields) ~= "table" or type(data.revision) ~= "string"
            or (data.kind ~= "managed" and data.kind ~= "website") then
            context.feedback = { ok = false, text = configError(result, ok and data or nil) }
        else
            -- safe belongs to the runner. The panel's pre-start UI fallback may
            -- differ for Unicode; never substitute that fallback in a CLI call.
            context.revision = data.revision
            context.kind = data.kind
            context.snapshot = table.clone(data.fields)
            context.draft = table.clone(data.fields)
            context.recovery = type(data.recovery_required) == "table" and #data.recovery_required > 0
            context.recoveryConfirmed = false
            context.icon = ""
            context.errors = {}
            context.conflict = false
            configRev += 1
            context.rev = configRev
        end
        render()
    end)
end

openConfiguration = function(card)
    local origin = { view = uiState.view, tab = uiState.detailTab }
    leaveView()
    uiState.view = "configuration"
    uiState.selectedSafe = card.safe
    edit = { safe = card.safe, origin = origin, loading = false, saving = false,
        danger = false, discard = false, draft = {}, errors = {} }
    readConfig(card)
    render()
end

local function leaveConfiguration(force)
    if edit == nil or edit.saving then return end
    if configDirty(edit) and not force then edit.discard = true; render(); return end
    local origin = edit.origin
    leaveView()
    uiState.view = origin.view == "detail" and "detail" or "list"
    uiState.detailTab = origin.tab
    if uiState.view == "list" then uiState.selectedSafe = nil end
    edit = nil
    if uiState.view == "detail" and uiState.detailTab == "logs" then readLog(uiState.selectedSafe) end
    render()
end

local function saveConfig(card)
    local context = edit
    if context == nil or context.snapshot == nil or context.loading or context.saving
        or confirmRemove[card.safe] or actionBlocked(card, false) then return end
    context.errors = {}
    local fields = table.clone(context.draft)
    local source = fields.url_source or "fixed"
    local fieldKey
    if context.kind == "managed" and trim(fields.start_cmd) == "" then fieldKey = "start_cmd"
    elseif (context.kind == "website" or source == "fixed") and trim(fields.url) == "" then fieldKey = "url"
    elseif source == "regex" and trim(fields.url_regex) == "" then fieldKey = "url_regex"
    elseif source == "script" and trim(fields.extract_script) == "" then fieldKey = "extract_script"
    elseif context.kind == "managed" then
        local timeout = tonumber(fields.url_timeout_s)
        if timeout == nil or timeout ~= timeout or timeout == math.huge or timeout <= 0 then fieldKey = "url_timeout_s" end
    end
    if fieldKey ~= nil then
        context.errors[fieldKey] = tr("config_required")
        context.feedback = { ok = false, text = tr("config_required") }; render(); return
    end
    if context.recovery and context.recoveryConfirmed ~= true then
        context.feedback = { ok = false, text = tr("config_recovery_confirm") }; render(); return
    end
    if context.kind == "website" or source == "fixed" then fields.url = normalizeUrl(trim(fields.url)) end
    if context.kind == "website" then fields.flags = fields.flags or "" end
    local request = { fields = fields, icon_ref = context.icon or "" }
    if context.recovery then request.recovery_confirmed = context.recoveryConfirmed end
    local encoded, body = pcall(noctalia.json.encode, request)
    if not encoded then context.feedback = { ok = false, text = tr("config_failed") }; render(); return end
    local token = {}
    configOps[ownerSafe(card.safe)] = token
    context.saving = true
    context.feedback = nil
    local epoch = uiState.panelEpoch
    render()
    runAsync(shellQuote(runnerPath()) .. " config-write " .. shellQuote(card.cli or card.name)
        .. " --revision " .. shellQuote(context.revision) .. " --json " .. shellQuote(body), function(result)
        if configOps[ownerSafe(card.safe)] ~= token then return end
        configOps[ownerSafe(card.safe)] = nil
        context.saving = false
        if not opened or edit ~= context or epoch ~= uiState.panelEpoch then return end
        local ok, data = pcall(noctalia.json.decode, result.stdout or "")
        if result.timedOut == true or (result.exitCode or 1) ~= 0 or not ok or type(data) ~= "table"
            or data.saved ~= true or type(data.revision) ~= "string" then
            context.conflict = ok and type(data) == "table" and data.code == "conflict"
            context.feedback = { ok = false, text = configError(result, ok and data or nil) }
        else
            context.snapshot = table.clone(context.draft)
            context.revision = data.revision
            context.recovery = false
            context.recoveryConfirmed = false
            context.icon = ""
            -- Inputs are uncontrolled: this is an actual reset after saving an
            -- icon, so give the config inputs a fresh seed only in this case.
            configRev += 1
            context.rev = configRev
            context.feedback = { ok = true, text = tr(data.restart_required and "config_saved_restart" or "config_saved") }
            refreshApps()
            wakeSupervisor()
        end
        render()
    end)
end

local function configField(context, key, labelKey, placeholderKey)
    local revision = context.rev
    local children = {
        hint(tr(labelKey)),
        ui.input({ key = "cfg-" .. context.safe .. "-" .. key .. "-" .. revision,
            value = key == "icon" and context.icon or tostring(context.draft[key] or ""),
            placeholder = tr(placeholderKey), enabled = not context.saving,
            onChange = function(value)
                if edit ~= context or context.rev ~= revision or context.saving then return end
                if key == "icon" then context.icon = tostring(value or "")
                else context.draft[key] = tostring(value or "") end
                context.errors[key] = nil
                render()
            end }),
    }
    if context.errors[key] then table.insert(children, hint(context.errors[key], "error")) end
    return ui.column({ gap = 4 }, children)
end

local function removeConfirmNode(card)
    local safe = card.safe
    return ui.column({ gap = 10, padding = 10, radius = 8, fill = "surface_variant/0.6" }, {
        hint(tr("remove_title", { name = card.name }), "error"),
        hint(tr("remove_effects_entry")),
        hint(tr(card.managed and "remove_effects_managed" or "remove_effects_website")),
        hint(tr("remove_irreversible")),
        ui.row({ gap = 8 }, {
            ui.button({ key = "cancel-" .. safe, text = tr("btn_cancel"), variant = "ghost", flexGrow = 1,
                onClick = function() confirmRemove[safe] = nil; render() end }),
            ui.button({ key = "confirm-" .. safe, text = tr("btn_confirm_remove"), variant = "destructive", flexGrow = 1,
                enabled = not actionBlocked(card, false), onClick = function() removeApp(card.name, safe) end }),
        }),
    })
end

local function configurationViewNode(card)
    local context = edit
    local fields = { ui.label({ text = card.name, fontSize = 14, fontWeight = "bold", color = "on_surface" }),
        hint(tr(card.managed and "kind_managed" or "kind_website")), hint(tr("config_name_readonly")) }
    if context.loading then table.insert(fields, hint(tr("config_loading"))) end
    if context.feedback then table.insert(fields, hint(context.feedback.text, context.feedback.ok and "primary" or "error")) end
    if context.conflict or (context.snapshot == nil and not context.loading) then
        table.insert(fields, ui.button({ key = "reload-config-" .. card.safe, text = tr("btn_reload_configuration"),
            enabled = configOps[ownerSafe(card.safe)] == nil, onClick = function() readConfig(card) end }))
    end
    if context.snapshot ~= nil then
        if context.recovery then
            table.insert(fields, hint(tr("config_recovery"), "secondary"))
        end
        if context.kind == "managed" then
            table.insert(fields, configField(context, "start_cmd", "field_start_cmd", "field_start_cmd_ph"))
            table.insert(fields, hint(tr("field_url_source")))
            table.insert(fields, ui.select({ key = "cfg-source-" .. card.safe .. "-" .. context.rev,
                options = { tr("url_source_fixed"), tr("url_source_regex"), tr("url_source_script") },
                selectedIndex = urlSourceIndex(context.draft.url_source), enabled = not context.saving,
                onChange = function(value)
                    if edit ~= context or context.saving then return end
                    context.draft.url_source = URL_SOURCES[math.floor(tonumber(value) or 0) + 1] or "fixed"; render()
                end }))
        end
        local source = context.draft.url_source or "fixed"
        if context.kind == "website" or source == "fixed" then
            table.insert(fields, configField(context, "url", "field_url", "field_url_ph"))
        elseif source == "regex" then
            table.insert(fields, configField(context, "url_regex", "field_url_regex", "field_url_regex_ph"))
            table.insert(fields, hint(tr("help_url_regex")))
        else
            table.insert(fields, configField(context, "extract_script", "field_extract_script", "field_extract_script_ph"))
            table.insert(fields, hint(tr("help_extract_script")))
        end
        if context.kind == "managed" then
            table.insert(fields, configField(context, "cwd", "field_cwd", "field_cwd_ph"))
            table.insert(fields, configField(context, "url_timeout_s", "field_url_timeout", "field_url_timeout_ph"))
            table.insert(fields, ui.row({ gap = 8, align = "center" }, {
                ui.label({ text = tr("field_auto_open"), fontSize = 12, flexGrow = 1 }),
                ui.toggle({ key = "cfg-auto-" .. card.safe .. "-" .. context.rev,
                    checked = context.draft.auto_open == true, enabled = not context.saving,
                    onChange = function(value)
                        if edit ~= context or context.saving then return end
                        context.draft.auto_open = value == true or value == "true"; render()
                    end }),
            }))
        end
        table.insert(fields, configField(context, "flags", "field_flags", "field_flags_ph"))
        if context.recovery then
            table.insert(fields, ui.row({ gap = 8, align = "center" }, {
                ui.label({ text = tr("config_recovery_confirm"), fontSize = 12, flexGrow = 1 }),
                ui.toggle({ key = "cfg-recovery-" .. card.safe, checked = context.recoveryConfirmed == true,
                    onChange = function(value)
                        if edit ~= context or context.saving then return end
                        context.recoveryConfirmed = value == true or value == "true"; render()
                    end }),
            }))
        end
        table.insert(fields, configField(context, "icon", "config_icon", "field_icon_ph"))
        table.insert(fields, hint(tr("config_save_timing")))
    end
    table.insert(fields, ui.separator({}))
    table.insert(fields, ui.button({ key = "danger-toggle-" .. card.safe, text = tr("danger_zone"),
        glyph = context.danger and "chevron-up" or "chevron-down", variant = "ghost",
        enabled = not context.loading and not context.saving,
        onClick = function() context.danger = not context.danger; confirmRemove[card.safe] = nil; render() end }))
    if context.danger then
        if confirmRemove[card.safe] then table.insert(fields, removeConfirmNode(card))
        else
            table.insert(fields, ui.button({ key = "del-" .. card.safe .. (deleting[ownerSafe(card.safe)] and "-off" or ""),
                text = tr("btn_confirm_remove"), variant = "destructive", enabled = not actionBlocked(card, false),
                onClick = function() confirmRemove[card.safe] = true; render() end }))
        end
    end
    local feedback = actionFeedbackNode(card)
    if feedback then table.insert(fields, feedback) end
    if context.discard then
        table.insert(fields, ui.column({ gap = 8 }, {
            hint(tr("discard_changes"), "secondary"),
            ui.row({ gap = 8 }, {
                ui.button({ key = "keep-editing", text = tr("btn_keep_editing"), variant = "ghost",
                    onClick = function() context.discard = false; render() end }),
                ui.button({ key = "discard-changes", text = tr("btn_discard_changes"), variant = "destructive",
                    onClick = function() leaveConfiguration(true) end }),
            }),
        }))
    end
    return ui.column({ flexGrow = 1, gap = 12 }, {
        headerNode(tr("configuration_title"), function() leaveConfiguration(false) end,
            context.saving or deleting[ownerSafe(card.safe)] == true),
        ui.scroll({ key = "config-scroll-" .. card.safe, flexGrow = 1, gap = 12 }, fields),
        ui.row({ gap = 8 }, {
            ui.button({ key = "back-config", text = tr("btn_cancel"), variant = "ghost", controlSize = "sm", flexGrow = 1,
                enabled = not context.saving and not deleting[ownerSafe(card.safe)], onClick = function() leaveConfiguration(false) end }),
            ui.button({ key = "save-config-" .. card.safe, text = tr("btn_save_changes"), controlSize = "sm", flexGrow = 1,
                enabled = context.snapshot ~= nil and configDirty(context) and not context.loading and not context.saving
                    and not context.discard and not context.conflict and not confirmRemove[card.safe] and not actionBlocked(card, false),
                onClick = function() saveConfig(card) end }),
        }),
    })
end

render = function()
    if not opened then return end
    local cards = buildCards()
    local selected
    for _, card in ipairs(cards) do if card.safe == uiState.selectedSafe then selected = card; break end end
    if (uiState.view == "detail" or uiState.view == "configuration") and selected == nil then
        leaveView()
        uiState.view = "list"
        uiState.selectedSafe = nil
        edit = nil
        status = { ok = false, text = tr("selected_app_missing") }
    end
    local node
    if uiState.view == "create" then node = createViewNode()
    elseif uiState.view == "detail" then node = detailNode(selected)
    elseif uiState.view == "configuration" then node = configurationViewNode(selected)
    else node = listViewNode(cards) end
    -- The host owns the outer panel inset; do not add another 16px here.
    panel.render(node)
end

function onClose()
    opened = false
    leaveView()
    uiState.selectedSafe = nil
    edit = nil
    scanToken = nil
    -- No stop command, no business-guard reset and no fake cancellation.
end

function onOpen(_context)
    leaveView()
    uiState.view = "list"
    uiState.query = ""
    uiState.queryRev += 1
    uiState.selectedSafe = nil
    uiState.detailTab = "overview"
    edit = nil
    if not busy then
        form = { name = "", url = "", flags = "", icon = "", start_cmd = "", url_source = "fixed",
            url_regex = "", log_sample = "", extract_script = "", url_timeout = "15", cwd = "", auto_open = true }
        managedOpen = false
        uiState.optionalOpen = false
        uiState.formErrors = {}
        regexPreview = nil
        regexRev += 1
        formRev += 1
        status = nil
    end
    appsLoaded = false
    opened = true
    webapps = noctalia.state.get("webapps")
    render()
    ensureLauncher()
    refreshApps()
end
