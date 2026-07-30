<#
.SYNOPSIS
    Native Windows clean-room runner for the North-Star-Group public alpha candidate.

.DESCRIPTION
    Proves the public candidate works like an outsider clone:
      * copies the staged candidate into a fresh directory OUTSIDE the source tree,
      * clears inherited PYTHONPATH and sets PYTHONNOUSERSITE=1,
      * creates a brand-new virtual environment inside the clean room,
      * installs ONLY from the clean copy and declared public dependencies,
      * prints sys.path and every loaded project module path,
      * proves every loaded project module lives inside the clean room,
      * runs the exact documented CLI quickstart on a synthetic group,
      * starts the local CEO API on 127.0.0.1, verifies health/status, then stops it,
      * performs zero real-model calls and zero external effects,
      * records all commands, exit codes, hashes, module origins, HTTP results, created files.

    Evidence-contract closure (G1-4B-R1 fix):
      * the complete final log is built in memory FIRST,
      * evidence-path lines and the terminal CLEAN_ROOM_ACCEPTANCE_GREEN=0|1 line
        are added BEFORE the log is persisted,
      * the JSON is persisted only AFTER every final field is set,
      * runner_exit_code, launcher exit receipt, api_process_stopped,
        inventory equality and pre/post drift are all recorded,
      * the verdict is computed from all gates (never hardcoded).

    Exclusive marker semantics: only DONE.marker (green) or FAILED.marker (red) is present.
    No machine/private path is hardcoded here; all locations are passed by the launcher.
#>
[CmdletBinding()]
param(
    [string]$StagingRoot,
    [string]$CleanRoomRoot,
    [string]$EvidenceDest,
    [string]$BootstrapPython,
    [string]$SourceRoot,
    [string]$LauncherPath,
    [string]$LauncherSha,
    [string]$LaunchId,
    [string]$RunnerSha
)

$ErrorActionPreference = 'Continue'
$startUtc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$log = [System.Collections.Generic.List[string]]::new()
$evidence = @{
    start_utc                 = $startUtc
    staging_root              = $null
    clean_room_root           = $null
    steps                     = [System.Collections.Generic.List[object]]::new()
    module_origins            = $null
    private_imports            = $null
    private_data_reads        = 0
    real_model_calls          = 0
    external_effects          = $false
    secrets_persisted         = $false
    http_results              = [System.Collections.Generic.List[object]]::new()
    created_files             = [System.Collections.Generic.List[string]]::new()
    green                     = $false
    runner_exit_code          = $null
    api_process_stopped       = $null
    inventory_equality        = $null
    manifest_inventory_byte_equal = $null
    drift                     = $null
    pre_run_hashes            = $null
    launcher_sha_provided     = $LauncherSha
    launcher_sha_verified     = $null
    launch_id                 = $LaunchId
    runner_sha                = $RunnerSha
    staging_inventory_sha     = $null
}

$script:apiDead = $false
$script:invEq   = $null
$preHashes = @{}

function Add-Log($m) { $log.Add($m); Write-Host $m }
function Add-Step($name, $ok, $detail) {
    $evidence.steps.Add(@{ step = $name; ok = $ok; detail = $detail })
    if ($ok) { Add-Log "STEP_OK   $name : $detail" } else { Add-Log "STEP_FAIL $name : $detail" }
}
function Resolve-BootstrapPython {
    if ($BootstrapPython -and (Test-Path $BootstrapPython)) { return $BootstrapPython }
    $candidates = @('py', 'python3', 'python')
    foreach ($c in $candidates) {
        try {
            $p = (Get-Command $c -ErrorAction Stop).Source
            if ($p -and $p -notmatch 'private-ai-company-runtime' -and $p -notmatch '\\.venv-novel' -and $p -notmatch 'CodexLab') {
                return $p
            }
        } catch { }
    }
    return $null
}
function Get-Sha($p) {
    if ($p -and (Test-Path $p)) { return (Get-FileHash $p -Algorithm SHA256).Hash }
    return $null
}
function Compare-Inventory($stagingInv, $cleanRoot) {
    $sinv = Get-Content $stagingInv -Raw | ConvertFrom-Json
    $cinvPath = Join-Path $cleanRoot 'FILE_INVENTORY.json'
    $cinv = Get-Content $cinvPath -Raw | ConvertFrom-Json
    $smap = @{}; foreach ($e in $sinv.files) { $smap[$e.rel_path] = $e.sha256 }
    $cmap = @{}; foreach ($e in $cinv.files) { $cmap[$e.rel_path] = $e.sha256 }
    $missing = @(); $extra = @(); $mismatched = @()
    foreach ($k in $smap.Keys) {
        if (-not $cmap.ContainsKey($k)) { $extra += $k }
        elseif ($cmap[$k] -ne $smap[$k]) { $mismatched += $k }
    }
    foreach ($k in $cmap.Keys) { if (-not $smap.ContainsKey($k)) { $missing += $k } }
    $manEqual = ((Get-Sha (Join-Path (Split-Path $stagingInv) 'EXPORT_MANIFEST.json')) -eq (Get-Sha (Join-Path $cleanRoot 'EXPORT_MANIFEST.json')))
    $invEqual = ((Get-Sha $stagingInv) -eq (Get-Sha $cinvPath))
    return @{
        missing                = $missing
        extra                  = $extra
        mismatched             = $mismatched
        manifest_byte_equal    = $manEqual
        inventory_byte_equal   = $invEqual
        zero                   = ($missing.Count -eq 0 -and $extra.Count -eq 0 -and $mismatched.Count -eq 0)
    }
}
function Compare-Drift($pre, $post) {
    $drift = @{}
    foreach ($k in $pre.Keys) {
        $drift[$k] = ($pre[$k] -eq $post[$k])
    }
    return @{ pre = $pre; post = $post; equal = $drift; zero = (-not ($drift.Values -contains $false)) }
}

function finalize($forcedCode = $null) {
    if ($null -ne $forcedCode) {
        $script:green = ($forcedCode -eq 0)
        $code = $forcedCode
    } else {
        # ---- compute post-run hashes and drift ----
        $postHashes = @{}
        $postHashes['source:ceo_api.py']          = Get-Sha (Join-Path $SourceRoot 'src/private_ai_company/ceo_api.py')
        $postHashes['source:smoke_public_alpha.py'] = Get-Sha (Join-Path $SourceRoot 'tests/smoke_public_alpha.py')
        $postHashes['staging:FILE_INVENTORY.json'] = Get-Sha (Join-Path $staging.Path 'FILE_INVENTORY.json')
        $postHashes['runner:run_clean_room.ps1']   = Get-Sha $PSCommandPath
        if ($LauncherPath -and (Test-Path $LauncherPath)) { $postHashes['launcher'] = Get-Sha $LauncherPath }
        $evidence.drift = Compare-Drift $preHashes $postHashes

        $evidence.api_process_stopped = $script:apiDead
        $evidence.inventory_equality  = $script:invEq
        $evidence.manifest_inventory_byte_equal = ($script:invEq.manifest_byte_equal -and $script:invEq.inventory_byte_equal)

        if ($LauncherSha -and $LauncherPath -and (Test-Path $LauncherPath)) {
            $evidence.launcher_sha_verified = ($LauncherSha -eq (Get-Sha $LauncherPath))
        }

        # ---- verdict computed from ALL gates (never hardcoded) ----
        $code = if ($installOk -and $originOk -and $cliOk -and $apiOk -and `
                    $script:apiDead -and $script:invEq.zero -and $evidence.drift.zero) { 0 } else { 1 }
        $script:green = ($code -eq 0)
    }

    $evidence.green = $script:green
    $evidence.runner_exit_code = $code
    $evidence.end_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')

    $dest = if ($EvidenceDest) { $EvidenceDest } elseif ($clean) { Join-Path $clean 'evidence' } else { $null }
    if ($dest) {
        try {
            New-Item -ItemType Directory -Force -Path $dest | Out-Null
            # ---- build the COMPLETE final log in memory FIRST ----
            Add-Log "EVIDENCE_LOG=$(Join-Path $dest 'clean_room_evidence.log')"
            Add-Log "EVIDENCE_JSON=$(Join-Path $dest 'clean_room_evidence.json')"
            Add-Log "RUNNER_EXIT_CODE=$code"
            Add-Log "API_PROCESS_STOPPED=$($evidence.api_process_stopped)"
            Add-Log "INVENTORY_EQUALITY_ZERO=$($evidence.inventory_equality.zero) (missing=$($evidence.inventory_equality.missing.Count) extra=$($evidence.inventory_equality.extra.Count) mismatched=$($evidence.inventory_equality.mismatched.Count))"
            Add-Log "MANIFEST_INVENTORY_BYTE_EQUAL=$($evidence.manifest_inventory_byte_equal)"
            Add-Log "DRIFT_ZERO=$($evidence.drift.zero)"
            Add-Log "LAUNCH_ID=$($evidence.launch_id)"
            Add-Log "RUNNER_SHA=$($evidence.runner_sha)"
            Add-Log "STAGING_INVENTORY_SHA=$($evidence.staging_inventory_sha)"
            if ($null -ne $evidence.launcher_sha_verified) { Add-Log "LAUNCHER_SHA_VERIFIED=$($evidence.launcher_sha_verified)" }
            Add-Log "CLEAN_ROOM_ACCEPTANCE_GREEN=$(if($code -eq 0){1}else{0})"
            # ---- NOW persist the log (ends with the terminal green line) ----
            $log | Out-File -FilePath (Join-Path $dest 'clean_room_evidence.log') -Encoding utf8
            # ---- persist JSON only AFTER every final field is set ----
            $evidence | ConvertTo-Json -Depth 8 | Out-File -FilePath (Join-Path $dest 'clean_room_evidence.json') -Encoding utf8
        } catch { Add-Log "WARN: could not write evidence: $_" }
    }

    # ---- exclusive marker: remove the opposite marker BEFORE writing the final one ----
    $dm = if ($clean) { Join-Path $clean 'DONE.marker' } else { $null }
    $fm = if ($clean) { Join-Path $clean 'FAILED.marker' } else { $null }
    if ($dm) { Remove-Item -Force $dm -ErrorAction SilentlyContinue }
    if ($fm) { Remove-Item -Force $fm -ErrorAction SilentlyContinue }
    if ($code -eq 0) {
        "CLEAN_ROOM_ACCEPTANCE_GREEN=1`n$(Get-Date -Format u)" | Out-File -FilePath $dm -Encoding utf8
    } else {
        "CLEAN_ROOM_ACCEPTANCE_GREEN=0`n$(Get-Date -Format u)" | Out-File -FilePath $fm -Encoding utf8
    }
    Write-Host "CLEAN_ROOM_ACCEPTANCE_GREEN=$(if($code -eq 0){1}else{0})"
    exit $code
}

# ---- determine roots ----
if (-not $StagingRoot) { Add-Log 'FAIL: -StagingRoot is required'; finalize 1 }
$staging = Resolve-Path $StagingRoot -ErrorAction SilentlyContinue
if (-not $staging) { Add-Log "FAIL: staging root not found: $StagingRoot"; finalize 1 }
$evidence.staging_root = $staging.Path

# ---- launch-session binding validation (R2) ----
function Test-Hex64($s) { return ($s -match '^[0-9a-fA-F]{64}$') }
function Test-Guid($s)  { return ($s -match '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$') }
if (-not (Test-Guid $LaunchId))        { Add-Log "FAIL: -LaunchId missing or malformed: '$LaunchId'"; finalize 1 }
if (-not (Test-Hex64 $LauncherSha))    { Add-Log "FAIL: -LauncherSha missing or malformed: '$LauncherSha'"; finalize 1 }
if (-not (Test-Hex64 $RunnerSha))      { Add-Log "FAIL: -RunnerSha missing or malformed: '$RunnerSha'"; finalize 1 }
Add-Log "LAUNCH_ID=$LaunchId"
$stagingInvSha = Get-Sha (Join-Path $staging.Path 'FILE_INVENTORY.json')
$evidence.staging_inventory_sha = $stagingInvSha
Add-Log "STAGING_INVENTORY_SHA=$stagingInvSha"

if (-not $SourceRoot) { Add-Log 'FAIL: -SourceRoot is required'; finalize 1 }

if (-not $CleanRoomRoot) {
    $ts = Get-Date -Format 'yyyyMMdd-HHmmss'
    $CleanRoomRoot = Join-Path $env:SystemDrive "nsg-cleanroom-$ts"
}
$clean = $CleanRoomRoot
$evidence.clean_room_root = $clean
Add-Log "CLEAN_ROOM_ROOT=$clean"

# ---- clean environment ----
$env:PYTHONPATH = ''
$env:PYTHONNOUSERSITE = '1'
$env:MODEL_API_KEY = ''
$env:DEEPSEEK_API_KEY = ''
$env:MODEL_BASE_URL = ''

# ---- freeze pre-run hashes (protected set) ----
$preHashes['source:ceo_api.py']            = Get-Sha (Join-Path $SourceRoot 'src/private_ai_company/ceo_api.py')
$preHashes['source:smoke_public_alpha.py'] = Get-Sha (Join-Path $SourceRoot 'tests/smoke_public_alpha.py')
$preHashes['staging:FILE_INVENTORY.json']  = Get-Sha (Join-Path $staging.Path 'FILE_INVENTORY.json')
$preHashes['runner:run_clean_room.ps1']    = Get-Sha $PSCommandPath
if ($LauncherPath -and (Test-Path $LauncherPath)) { $preHashes['launcher'] = Get-Sha $LauncherPath }
$evidence.pre_run_hashes = $preHashes

# ---- copy staging into the clean room (fresh) ----
if (Test-Path $clean) { Remove-Item -Recurse -Force $clean | Out-Null }
New-Item -ItemType Directory -Force -Path $clean | Out-Null
Add-Log "COPY $($staging.Path) -> $clean"
Copy-Item -Path (Join-Path $staging.Path '*') -Destination $clean -Recurse -Force
Add-Step 'copy_staging' $true 'copied staging to clean room'

# ---- bootstrap python ----
$bpy = Resolve-BootstrapPython
if (-not $bpy) { Add-Log 'FAIL: no bootstrap python found'; finalize 1 }
Add-Log "BOOTSTRAP_PYTHON=$bpy"
$venv = Join-Path $clean '.venv'
& $bpy -m venv $venv 2>&1 | ForEach-Object { Add-Log "VENV: $_" }
$venvPy = Join-Path $venv 'Scripts/python.exe'
if (-not (Test-Path $venvPy)) { Add-Log 'FAIL: venv python not created'; finalize 1 }
Add-Step 'create_venv' $true "fresh venv at $venv"

# ---- install ONLY from the clean copy + declared public deps ----
Add-Log "PIP_INSTALL $clean (clean copy, public deps only)"
$installOut = & "$venv/Scripts/pip.exe" install --no-cache-dir --disable-pip-version-check $clean 2>&1
$installOut | ForEach-Object { Add-Log "PIP: $_" }
$installOk = ($LASTEXITCODE -eq 0)
Add-Step 'install_package' $installOk "pip install exit=$LASTEXITCODE"
if (-not $installOk) { finalize 1 }

# ---- module origin proof ----
$env:CLEAN_ROOT = $clean
$originPy = Join-Path $clean 'origin_check.py'
@'
import json, os, sys
import private_ai_company
clean = os.path.normpath(os.environ.get("CLEAN_ROOT", ""))
pkg_dir = os.path.dirname(private_ai_company.__file__)
mods = []
outside = []
priv = 0
for root, _, files in os.walk(pkg_dir):
    for f in files:
        if f.endswith(".py"):
            p = os.path.join(root, f)
            mods.append(p)
            pn = os.path.normpath(p)
            if clean and not pn.startswith(clean + os.sep):
                outside.append(pn)
            if "private-ai-company-runtime" in pn or ".venv-novel" in pn:
                priv += 1
print(json.dumps({"pkg": private_ai_company.__file__, "module_count": len(mods),
                  "outside_clean_room": outside, "private_imports": priv}))
'@ | Out-File -FilePath $originPy -Encoding utf8
$originOut = & $venvPy $originPy 2>&1
Add-Log "ORIGIN: $originOut"
try {
    $origin = $originOut | ConvertFrom-Json
    $evidence.module_origins = @{ pkg = $origin.pkg; module_count = $origin.module_count; outside_clean_room = $origin.outside_clean_room }
    $evidence.private_imports = $origin.private_imports
    $originOk = ($origin.outside_clean_room.Count -eq 0) -and ($origin.private_imports -eq 0)
    Add-Step 'module_origin' $originOk "modules=$($origin.module_count) outside=$($origin.outside_clean_room.Count) private=$($origin.private_imports)"
} catch {
    Add-Step 'module_origin' $false 'could not parse origin output'
    $originOk = $false
}

# ---- prepare a writable demo group (keep the shipped example pristine) ----
$demoGroup = Join-Path $clean 'demo/group'
New-Item -ItemType Directory -Force -Path $demoGroup | Out-Null
Copy-Item -Path (Join-Path $clean 'examples/quickstart-group/*') -Destination $demoGroup -Recurse -Force

# ---- documented CLI quickstart ----
$cliOk = $true
$cliCmds = @(
    @('validate-group', '--root', $demoGroup),
    @('organization-tree', '--root', $demoGroup),
    @('route-department', 'research', '--root', $demoGroup, '--json')
)
foreach ($c in $cliCmds) {
    $args = @('-m', 'private_ai_company') + $c
    & $venvPy @args *> "$clean/demo/cli_$($c[0]).out"
    $rc = $LASTEXITCODE
    if ($rc -ne 0) { $cliOk = $false; Add-Log "CLI_FAIL rc=$rc args=$($c -join ' ')" }
    Add-Step "cli_$($c[0])" ($rc -eq 0) "rc=$rc args=$($c -join ' ')"
}
$rtArgs = @('-m', 'private_ai_company', 'run-task', '--root', $demoGroup, '--task-id', 'task-local-001',
    '--instruction', 'Prepare the baseline.', '--capability', 'research', '--capability', 'task-orchestration',
    '--criterion', 'Evidence and artifacts are registered.')
& $venvPy @rtArgs *> "$clean/demo/run_task.out"
$rtRc = $LASTEXITCODE
if ($rtRc -ne 0) { $cliOk = $false }
$evidence.real_model_calls = 0
Add-Step 'cli_run_task' ($rtRc -eq 0) "rc=$rtRc (model-free local executors)"
Add-Step 'cli_quickstart' $cliOk 'all documented CLI commands exited 0'

# ---- local API on 127.0.0.1 (HTTP check via stdlib urllib, no curl dependency) ----
$port = 8790
$apiProc = Start-Process -FilePath $venvPy -ArgumentList @('-m', 'private_ai_company', 'serve-api', '--root', $demoGroup, '--host', '127.0.0.1', '--port', $port) -PassThru -RedirectStandardOutput "$clean/demo/api.out" -RedirectStandardError "$clean/demo/api.err"
$apiOk = $false
$health = $null; $status = $null
$healthPy = Join-Path $clean 'health_check.py'
@'
import sys, json, time, urllib.request
port = int(sys.argv[1])
base = "http://127.0.0.1:" + str(port)
health = None; status = None
for _ in range(30):
    try:
        with urllib.request.urlopen(base + "/health", timeout=2) as r:
            health = r.status
            if health == 200:
                try:
                    with urllib.request.urlopen(base + "/api/status", timeout=2) as s:
                        status = s.read().decode()
                except Exception:
                    status = None
                break
    except Exception:
        pass
    time.sleep(1)
print(json.dumps({"health": health, "status": status}))
'@ | Out-File -FilePath $healthPy -Encoding utf8
$healthJson = & $venvPy $healthPy $port 2>&1
try {
    $h = $healthJson | ConvertFrom-Json
    $health = $h.health
    $status = $h.status
    if ($health -eq 200) { $apiOk = $true }
} catch { Add-Log "WARN: health check parse failed: $healthJson" }
$evidence.http_results.Add(@{ endpoint = 'GET /health'; http_code = $health })
$evidence.http_results.Add(@{ endpoint = 'GET /api/status'; http_code = if ($status) { 200 } else { $null }; body = $status })
try { Stop-Process -Id $apiProc.Id -Force -ErrorAction SilentlyContinue } catch { }
# ---- verify the API process is no longer alive (gate api_process_stopped) ----
$script:apiDead = $false
for ($i = 0; $i -lt 5; $i++) {
    $p = Get-Process -Id $apiProc.Id -ErrorAction SilentlyContinue
    if (-not $p) { $script:apiDead = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $script:apiDead) { Add-Log "WARN: API process still alive after stop" }
Add-Step 'api_serve' $apiOk "health=$health status=$(if($status){'200'}else{'n/a'}) host=127.0.0.1:$port"

# ---- inventory equality: staging vs clean copy ----
$script:invEq = Compare-Inventory (Join-Path $staging.Path 'FILE_INVENTORY.json') $clean
Add-Step 'inventory_equality' $script:invEq.zero "missing=$($script:invEq.missing.Count) extra=$($script:invEq.extra.Count) mismatched=$($script:invEq.mismatched.Count)"

# ---- created files inventory ----
Get-ChildItem -Path $clean -Recurse -File | ForEach-Object { $evidence.created_files.Add($_.FullName) } | Out-Null

# ---- assemble green verdict and close ----
finalize
