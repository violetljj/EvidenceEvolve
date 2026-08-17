[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("doctor", "bootstrap", "test", "run", "rebuild")]
    [string]$Command = "doctor",

    [ValidateSet("default", "dev", "shinka", "onnx", "algotune", "engine-selection")]
    [string]$Profile = "dev",

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Arguments,

    [string]$Module,
    [string]$Script,
    [string]$Code,
    [string[]]$TargetArguments
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$VersionFile = Join-Path $ProjectRoot ".python-version"
$LockFile = Join-Path $ProjectRoot "uv.lock"
$EnvironmentPath = Join-Path $ProjectRoot ".venv"
$PythonPath = Join-Path $EnvironmentPath "Scripts\python.exe"

function Resolve-Uv {
    $preferred = "E:\codex-tools\bin\uv.cmd"
    if (Test-Path -LiteralPath $preferred) {
        return $preferred
    }
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    throw "ENV_BLOCKED: uv was not found. Install uv or restore E:\codex-tools\bin\uv.cmd."
}

function Invoke-Uv {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$UvArguments)
    & $script:UvPath @UvArguments
    if ($LASTEXITCODE -ne 0) {
        throw "ENV_BLOCKED: uv failed with exit code $LASTEXITCODE."
    }
}

function Get-ExpectedPythonVersion {
    if (-not (Test-Path -LiteralPath $VersionFile)) {
        throw "ENV_BLOCKED: missing .python-version."
    }
    return (Get-Content -Raw -LiteralPath $VersionFile).Trim()
}

function Get-ProfileArguments {
    switch ($Profile) {
        "default" { return @() }
        "dev" { return @("--extra", "dev") }
        "shinka" { return @("--extra", "dev", "--extra", "shinka") }
        "onnx" { return @("--extra", "dev", "--extra", "onnx-canary") }
        "algotune" { return @("--extra", "dev", "--extra", "algotune-portfolio") }
        "engine-selection" { return @("--extra", "dev", "--extra", "engine-selection-r1") }
    }
}

function Get-ProfileImports {
    switch ($Profile) {
        "default" { return @("evidence_evolve", "pydantic", "yaml") }
        "dev" { return @("evidence_evolve", "pydantic", "yaml", "pytest") }
        "shinka" { return @("evidence_evolve", "pydantic", "yaml", "pytest", "shinka") }
        "onnx" { return @("evidence_evolve", "pydantic", "yaml", "pytest", "numpy", "onnx", "onnxruntime") }
        "algotune" { return @("evidence_evolve", "pydantic", "yaml", "pytest", "networkx", "numpy", "ortools", "pysat", "scipy") }
        "engine-selection" { return @("evidence_evolve", "pydantic", "yaml", "pytest", "networkx", "numpy", "ortools", "pysat", "scipy", "shinka", "skydiscover") }
    }
}

function Get-CoreTestTargets {
    return @(
        "tests/test_async_autonomous.py",
        "tests/test_autonomous_discovery.py",
        "tests/test_benchmark.py",
        "tests/test_budget_and_receipts.py",
        "tests/test_codex_backend.py",
        "tests/test_evidence_policy.py",
        "tests/test_gate_engine.py",
        "tests/test_graph_coloring_live.py",
        "tests/test_hashing.py",
        "tests/test_m2_escape.py",
        "tests/test_m2_r2_escape.py",
        "tests/test_population.py",
        "tests/test_protocol_lock.py",
        "tests/test_r1_discovery.py",
        "tests/test_remote_cpu.py",
        "tests/test_replay.py",
        "tests/test_research_actions.py",
        "tests/test_research_memory.py",
        "tests/test_scope_and_closure.py",
        "tests/test_throughput.py",
        "tests/test_worktrees.py"
    )
}

function Get-TestTargets {
    $targets = [System.Collections.Generic.List[string]]::new()
    foreach ($target in (Get-CoreTestTargets)) {
        $targets.Add($target)
    }

    switch ($Profile) {
        "default" {
            throw "USAGE: tests require -Profile dev, shinka, onnx, algotune, or engine-selection."
        }
        "dev" { }
        "onnx" {
            $targets.Add("tests/test_onnx_campaign.py")
        }
        "shinka" {
            $targets.Add("tests/test_shinka_native.py")
            $targets.Add("tests/test_shinka_native_semantic_parity.py")
        }
        "algotune" {
            if ($IsWindows) {
                throw "ENV_BLOCKED: the AlgoTune suite imports Linux-only pwd/resource/fcntl modules. Run this profile inside WSL/Linux."
            }
            return @("tests")
        }
        "engine-selection" {
            $targets.Add("tests/test_shinka_native.py")
            $targets.Add("tests/test_shinka_native_semantic_parity.py")
            $targets.Add("tests/test_engine_selection_r3.py")
            $targets.Add("tests/test_engine_selection_r3_continuation.py")
            $targets.Add("tests/test_engine_selection_shinka_postfix.py")
            $targets.Add("tests/test_shinka_selection_audit.py")
        }
    }
    return $targets.ToArray()
}

function Invoke-Bootstrap {
    $expectedVersion = Get-ExpectedPythonVersion
    $profileArguments = Get-ProfileArguments
    Push-Location $ProjectRoot
    try {
        Invoke-Uv sync --locked --python $expectedVersion @profileArguments
    }
    finally {
        Pop-Location
    }
}

function Invoke-Doctor {
    $failures = [System.Collections.Generic.List[string]]::new()
    $expectedVersion = Get-ExpectedPythonVersion
    Write-Host "PASS project root: $ProjectRoot"
    Write-Host "PASS profile: $Profile"
    Write-Host "PASS uv: $(& $script:UvPath --version)"

    if (-not (Test-Path -LiteralPath $LockFile)) {
        $failures.Add("missing uv.lock")
    }
    else {
        Push-Location $ProjectRoot
        try {
            & $script:UvPath lock --check | Out-Host
            if ($LASTEXITCODE -ne 0) {
                $failures.Add("uv.lock does not match pyproject.toml")
            }
            else {
                Write-Host "PASS uv.lock is current"
            }
        }
        finally {
            Pop-Location
        }
    }

    if (-not (Test-Path -LiteralPath $PythonPath)) {
        $failures.Add("project environment is missing; run bootstrap -Profile $Profile")
    }
    else {
        $actualVersion = (& $PythonPath -c "import platform; print(platform.python_version())").Trim()
        if ($actualVersion -ne $expectedVersion) {
            $failures.Add("Python $actualVersion does not match required $expectedVersion; run rebuild")
        }
        else {
            Write-Host "PASS Python $actualVersion"
        }

        $imports = (Get-ProfileImports) -join ","
        & $PythonPath -c "import importlib; modules='$imports'.split(','); [importlib.import_module(name) for name in modules]; print('PASS imports ' + ', '.join(modules))"
        if ($LASTEXITCODE -ne 0) {
            $failures.Add("one or more imports for profile $Profile failed; run bootstrap -Profile $Profile")
        }
    }

    if ($failures.Count -gt 0) {
        foreach ($failure in $failures) {
            Write-Error "FAIL $failure" -ErrorAction Continue
        }
        throw "ENV_BLOCKED: doctor found $($failures.Count) problem(s)."
    }
}

function Remove-ProjectEnvironment {
    if (-not (Test-Path -LiteralPath $EnvironmentPath)) {
        return
    }
    $environmentItem = Get-Item -LiteralPath $EnvironmentPath -Force
    if ($null -ne $environmentItem.LinkType) {
        throw "REFUSED: .venv is a reparse point ($($environmentItem.LinkType))."
    }
    $resolvedEnvironment = (Resolve-Path -LiteralPath $EnvironmentPath).Path
    $expectedEnvironment = Join-Path $ProjectRoot ".venv"
    if (-not [string]::Equals($resolvedEnvironment, $expectedEnvironment, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "REFUSED: resolved environment is outside the expected project path: $resolvedEnvironment"
    }
    Remove-Item -LiteralPath $resolvedEnvironment -Recurse -Force
}

$UvPath = Resolve-Uv

switch ($Command) {
    "doctor" {
        Invoke-Doctor
    }
    "bootstrap" {
        Invoke-Bootstrap
        Invoke-Doctor
    }
    "test" {
        $testTargets = Get-TestTargets
        Invoke-Bootstrap
        Push-Location $ProjectRoot
        try {
            if ($IsWindows) {
                Write-Host "SKIP frozen-evidence byte checks: test_p2_r1_execution.py, test_p2_r1_protocol.py"
                Write-Host "SKIP LF-sensitive fixture hash check: test_proposal_mechanics.py"
            }
            Write-Host "PASS test surface: $Profile ($($testTargets.Count) target files)"
            & $PythonPath -m pytest @testTargets @Arguments
            if ($LASTEXITCODE -ne 0) {
                throw "TEST_FAILED: pytest exited with $LASTEXITCODE."
            }
        }
        finally {
            Pop-Location
        }
    }
    "run" {
        $targets = @($Module, $Script, $Code) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        $targetCount = @($targets).Count
        if ($targetCount -ne 1) {
            throw "USAGE: project.ps1 run requires exactly one of -Module, -Script, or -Code."
        }
        Invoke-Bootstrap
        Push-Location $ProjectRoot
        try {
            if ($Module) {
                & $PythonPath -m $Module @TargetArguments
            }
            elseif ($Script) {
                & $PythonPath $Script @TargetArguments
            }
            else {
                & $PythonPath -c $Code @TargetArguments
            }
            if ($LASTEXITCODE -ne 0) {
                throw "RUN_FAILED: Python exited with $LASTEXITCODE."
            }
        }
        finally {
            Pop-Location
        }
    }
    "rebuild" {
        Remove-ProjectEnvironment
        Invoke-Bootstrap
        Invoke-Doctor
    }
}
