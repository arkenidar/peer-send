local wx = require("wx")

local function normalize_path(path)
    local absolute = path:sub(1, 1) == "/"
    local parts = {}
    for part in path:gmatch("[^/]+") do
        if part == ".." then
            parts[#parts] = nil
        elseif part ~= "." and part ~= "" then
            parts[#parts + 1] = part
        end
    end

    local prefix = absolute and "/" or ""
    return prefix .. table.concat(parts, "/")
end

local function dirname(path)
    local match = path:match("^(.*)/[^/]+$")
    return match or "."
end

local script_dir = dirname(arg[0] or "client/main.lua")
local base_url = arg[1] or "https://127.0.0.1:8443"
local cert_path = arg[2] or normalize_path(script_dir .. "/../backend/certs/cert.pem")
local author_name = os.getenv("USER") or "guest"
local last_id = 0

local function shell_escape(value)
    return "'" .. tostring(value):gsub("'", "'\\''") .. "'"
end

local function run_command(command)
    -- Lua 5.1's handle:close() does not report the child's exit status, so we
    -- append it to stdout via a sentinel and parse it back out. Returns the
    -- command output and its exit code (0 on success).
    local handle = io.popen(command .. ' 2>/dev/null; printf "\\n__EXIT__%d" "$?"')
    if not handle then
        return nil
    end
    local output = handle:read("*a")
    handle:close()
    local body, code = output:match("^(.*)\n__EXIT__(%d+)%s*$")
    if not body then
        return output, nil
    end
    return body, tonumber(code)
end

local function json_escape(value)
    local escaped = tostring(value)
    escaped = escaped:gsub("\\", "\\\\")
    escaped = escaped:gsub('"', '\\"')
    escaped = escaped:gsub("\b", "\\b")
    escaped = escaped:gsub("\f", "\\f")
    escaped = escaped:gsub("\n", "\\n")
    escaped = escaped:gsub("\r", "\\r")
    escaped = escaped:gsub("\t", "\\t")
    -- JSON forbids unescaped control characters (U+0000..U+001F).
    escaped =
        escaped:gsub(
        "[%z\1-\31]",
        function(char)
            return string.format("\\u%04x", string.byte(char))
        end
    )
    return escaped
end

local function url_decode(value)
    return (value:gsub(
        "%%(%x%x)",
        function(hex)
            return string.char(tonumber(hex, 16))
        end
    ))
end

local function parse_messages(payload)
    local messages = {}
    for line in payload:gmatch("[^\r\n]+") do
        local id, author, text, timestamp = line:match("^(%d+)\t([^\t]*)\t([^\t]*)\t(.+)$")
        if id then
            messages[#messages + 1] = {
                id = tonumber(id),
                author = url_decode(author),
                text = url_decode(text),
                timestamp = timestamp
            }
        end
    end
    return messages
end

local app = wx.wxApp()
local frame = wx.wxFrame(wx.NULL, wx.wxID_ANY, "wxsend", wx.wxDefaultPosition, wx.wxSize(760, 520))
local panel = wx.wxPanel(frame, wx.wxID_ANY)
local main_sizer = wx.wxBoxSizer(wx.wxVERTICAL)

local status_label = wx.wxStaticText(panel, wx.wxID_ANY, "Disconnected")
main_sizer:Add(status_label, 0, wx.wxALL + wx.wxEXPAND, 8)

local transcript =
    wx.wxTextCtrl(
    panel,
    wx.wxID_ANY,
    "",
    wx.wxDefaultPosition,
    wx.wxDefaultSize,
    wx.wxTE_MULTILINE + wx.wxTE_READONLY + wx.wxTE_RICH2
)
main_sizer:Add(transcript, 1, wx.wxLEFT + wx.wxRIGHT + wx.wxBOTTOM + wx.wxEXPAND, 8)

local controls = wx.wxBoxSizer(wx.wxHORIZONTAL)
local author_input = wx.wxTextCtrl(panel, wx.wxID_ANY, author_name, wx.wxDefaultPosition, wx.wxSize(140, -1))
local message_input =
    wx.wxTextCtrl(panel, wx.wxID_ANY, "", wx.wxDefaultPosition, wx.wxDefaultSize, wx.wxTE_PROCESS_ENTER)
local send_button = wx.wxButton(panel, wx.wxID_ANY, "Send")
controls:Add(author_input, 0, wx.wxRIGHT, 8)
controls:Add(message_input, 1, wx.wxRIGHT + wx.wxEXPAND, 8)
controls:Add(send_button, 0)
main_sizer:Add(controls, 0, wx.wxLEFT + wx.wxRIGHT + wx.wxBOTTOM + wx.wxEXPAND, 8)

panel:SetSizer(main_sizer)
frame:CreateStatusBar(1)
frame:SetStatusText(base_url)

local function append_message(item)
    transcript:AppendText(string.format("[%s] %s: %s\n", item.timestamp, item.author, item.text))
end

local function refresh_messages()
    local command =
        string.format(
        "curl --silent --show-error --fail --cacert %s %s",
        shell_escape(cert_path),
        shell_escape(base_url .. "/messages?after=" .. tostring(last_id) .. "&format=tsv")
    )
    local payload, code = run_command(command)
    if not payload or code ~= 0 then
        status_label:SetLabel("Unable to reach backend")
        return
    end

    local items = parse_messages(payload)
    if #items > 0 then
        for _, item in ipairs(items) do
            append_message(item)
            last_id = item.id
        end
    end
    status_label:SetLabel("Connected")
end

local function send_message()
    local author = author_input:GetValue()
    local text = message_input:GetValue()
    if text == "" then
        return
    end

    local body = string.format('{"author":"%s","text":"%s"}', json_escape(author), json_escape(text))
    local command =
        string.format(
        "curl --silent --show-error --fail --cacert %s -H 'Content-Type: application/json' -d %s %s",
        shell_escape(cert_path),
        shell_escape(body),
        shell_escape(base_url .. "/messages")
    )

    local payload, code = run_command(command)
    if not payload or code ~= 0 then
        status_label:SetLabel("Send failed")
        return
    end

    message_input:SetValue("")
    refresh_messages()
end

send_button:Connect(wx.wxEVT_COMMAND_BUTTON_CLICKED, send_message)
message_input:Connect(wx.wxEVT_COMMAND_TEXT_ENTER, send_message)

local timer = wx.wxTimer(frame)
frame:Connect(
    wx.wxEVT_TIMER,
    function()
        refresh_messages()
    end
)
timer:Start(2000)

frame:Connect(
    wx.wxEVT_CLOSE_WINDOW,
    function(event)
        timer:Stop()
        event:Skip()
    end
)

frame:Show(true)
refresh_messages()
app:SetTopWindow(frame)
app:MainLoop()
