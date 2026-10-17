[CmdletBinding()]
param(
    [string]$ComfyRoot,
    [string]$Python,
    [switch]$ConfigureOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$pluginRoot = $PSScriptRoot
$configPath = Join-Path $pluginRoot 'config.json'
if (Test-Path -LiteralPath $configPath -PathType Leaf) {
    $videoConfig = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    $videoConfig = Get-Content -LiteralPath (Join-Path $pluginRoot 'config.example.json') -Raw -Encoding UTF8 | ConvertFrom-Json
}
if (-not $ComfyRoot) { $ComfyRoot = $videoConfig.comfy_root }
if (-not $ComfyRoot) { throw 'Run install.ps1 -ComfyRoot <ComfyUI directory containing main.py> [-Python <ComfyUI python.exe>].' }
if (-not [System.IO.Path]::IsPathRooted($ComfyRoot)) { $ComfyRoot = Join-Path $pluginRoot $ComfyRoot }
$resolvedComfyRoot = (Resolve-Path -LiteralPath $ComfyRoot -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath (Join-Path $resolvedComfyRoot 'main.py') -PathType Leaf)) {
    throw 'ComfyRoot must be the ComfyUI directory containing main.py.'
}
if (-not $Python) {
    if ($PSBoundParameters.ContainsKey('ComfyRoot') -or -not $videoConfig.python) {
        $portableRoot = Split-Path -Parent $resolvedComfyRoot
        $pythonCandidates = @(
            (Join-Path $portableRoot 'python_embeded/python.exe'),
            (Join-Path $portableRoot 'python_embedded/python.exe'),
            (Join-Path $resolvedComfyRoot '.venv/Scripts/python.exe'),
            (Join-Path $resolvedComfyRoot 'venv/Scripts/python.exe')
        )
        foreach ($candidate in $pythonCandidates) {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { $Python = $candidate; break }
        }
    } else { $Python = $videoConfig.python }
}
if (-not $Python) { throw 'No ComfyUI Python found. Pass -Python <full path to the Python used by ComfyUI>.' }
if (-not [System.IO.Path]::IsPathRooted($Python)) { $Python = Join-Path $pluginRoot $Python }
$pythonPath = (Resolve-Path -LiteralPath $Python -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw "Missing installed Python: $pythonPath" }
if (-not $ConfigureOnly) { $null = Get-Command codex -ErrorAction Stop }
$videoConfig.comfy_root = $resolvedComfyRoot
$videoConfig.python = $pythonPath
$mcpConfig = @{
    mcpServers = @{
        minimax_comfy = @{
            command = $pythonPath
            args = @((Join-Path $pluginRoot 'mcp_server.py'))
            cwd = $pluginRoot
            env = @{ MINIMAX_VIDEO_CONFIG = $configPath }
            startup_timeout_sec = 20
            tool_timeout_sec = 120
        }
    }
}
$mcpPath = Join-Path $pluginRoot '.mcp.json'
[System.IO.File]::WriteAllText($configPath, ($videoConfig | ConvertTo-Json -Depth 12), [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText($mcpPath, ($mcpConfig | ConvertTo-Json -Depth 8), [System.Text.UTF8Encoding]::new($false))
if ($ConfigureOnly) {
    Write-Host 'Local config.json and .mcp.json are ready. Codex settings were not changed.'
    return
}
& codex plugin marketplace add $pluginRoot --json
if ($LASTEXITCODE -ne 0) { throw 'Could not register the local marketplace.' }
& codex plugin add 'minimax-comfy@local-minimax-video' --json
if ($LASTEXITCODE -ne 0) { throw 'Could not install the MiniMax plugin.' }
& codex plugin list --marketplace local-minimax-video --json
if ($LASTEXITCODE -ne 0) { throw 'Could not verify the plugin installation.' }
Write-Host 'Installed. Open a new Codex chat to load the plugin tools and minimax-video skill.'
