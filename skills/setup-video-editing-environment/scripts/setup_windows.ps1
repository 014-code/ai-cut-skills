[CmdletBinding()]
param(
    [ValidateSet("base-video", "soda-scripted-render", "soda-timeline-render", "soda-detect-pauses")]
    [string]$Profile = "soda-scripted-render",

    [ValidateSet("auto", "off", "required")]
    [string]$MotionEffects = "auto",

    [switch]$Install,
    [string]$EnvironmentName = "ai-video-editing",
    [string]$EnvironmentPath,
    [string]$ReportPath,
    [string]$FfmpegBinDir,
    [string]$FfmpegArchive,
    [string]$FfmpegDownloadUrl,
    [string]$ToolsDirectory
)

$ErrorActionPreference = "Stop"

function Refresh-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Add-ProcessPath {
    param([Parameter(Mandatory = $true)][string]$Directory)
    $resolved = [IO.Path]::GetFullPath($Directory)
    $entries = $env:Path -split [IO.Path]::PathSeparator
    if ($entries -notcontains $resolved) {
        $env:Path = "$resolved;$env:Path"
    }
}

function Get-PythonExecutable {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($selector in @("-3.11", "-3")) {
            $path = & $launcher.Source $selector -c "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $path) {
                return $path.Trim()
            }
        }
    }
    foreach ($command in @("python", "python3")) {
        $python = Get-Command $command -ErrorAction SilentlyContinue
        if ($python) {
            $path = & $python.Source -c "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $path) {
                return $path.Trim()
            }
        }
    }
    return $null
}

function Test-PythonBootstrapCapabilities {
    param([Parameter(Mandatory = $true)][string]$PythonExecutable)
    & $PythonExecutable -c "import ensurepip, venv" 2>$null
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    & $PythonExecutable -m pip --version 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

function Install-WingetPackage {
    param([Parameter(Mandatory = $true)][string]$Id)
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "WinGet is required for automatic Windows setup. Install or repair Microsoft App Installer first."
    }
    & $winget.Source install --id $Id --exact --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "WinGet failed to install $Id. Run 'winget search $Id' and inspect the package source."
    }
    Refresh-ProcessPath
}

function Resolve-FfmpegBinDirectory {
    param([Parameter(Mandatory = $true)][string]$Root)
    $resolved = [IO.Path]::GetFullPath($Root)
    $directFfmpeg = Join-Path $resolved "ffmpeg.exe"
    $directFfprobe = Join-Path $resolved "ffprobe.exe"
    if ((Test-Path $directFfmpeg) -and (Test-Path $directFfprobe)) {
        return $resolved
    }
    $candidate = Get-ChildItem -Path $resolved -Filter "ffmpeg.exe" -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.DirectoryName "ffprobe.exe") } |
        Select-Object -First 1
    if ($candidate) {
        return $candidate.DirectoryName
    }
    throw "Could not find ffmpeg.exe and ffprobe.exe under $resolved"
}

function Expand-ValidatedFfmpegArchive {
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    $archive = [IO.Path]::GetFullPath($ArchivePath)
    if (-not (Test-Path $archive -PathType Leaf)) {
        throw "FFmpeg archive not found: $archive"
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    try {
        $zip = [IO.Compression.ZipFile]::OpenRead($archive)
        $hasFfmpeg = $null -ne ($zip.Entries | Where-Object { $_.FullName -match '(^|/)ffmpeg\.exe$' } | Select-Object -First 1)
        $hasFfprobe = $null -ne ($zip.Entries | Where-Object { $_.FullName -match '(^|/)ffprobe\.exe$' } | Select-Object -First 1)
        $zip.Dispose()
    }
    catch {
        throw "FFmpeg archive is incomplete or invalid: $archive. $($_.Exception.Message)"
    }
    if (-not $hasFfmpeg -or -not $hasFfprobe) {
        throw "FFmpeg archive does not contain both ffmpeg.exe and ffprobe.exe: $archive"
    }
    $destinationPath = [IO.Path]::GetFullPath($Destination)
    New-Item -ItemType Directory -Path $destinationPath -Force | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $destinationPath -Force
    return Resolve-FfmpegBinDirectory -Root $destinationPath
}

function Download-FfmpegArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) {
        throw "curl.exe is required for resumable FFmpeg downloads. Provide -FfmpegArchive or -FfmpegBinDir instead."
    }
    $target = [IO.Path]::GetFullPath($Destination)
    New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force | Out-Null
    & $curl.Source -L --fail --retry 5 --retry-delay 2 -C - --output $target $Url
    if ($LASTEXITCODE -ne 0) {
        throw "Resumable FFmpeg download failed. Keep the partial file and rerun the same command: $target"
    }
    return $target
}

function Test-FfmpegCapabilities {
    $ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
    $ffprobe = Get-Command ffprobe -ErrorAction SilentlyContinue
    if (-not $ffmpeg -or -not $ffprobe) {
        return $false
    }
    $filters = (& $ffmpeg.Source -hide_banner -filters 2>&1 | Out-String)
    $encoders = (& $ffmpeg.Source -hide_banner -encoders 2>&1 | Out-String)
    return (
        $filters.Contains("subtitles") -and
        $filters.Contains("loudnorm") -and
        $filters.Contains("ebur128") -and
        $encoders.Contains("libx264") -and
        $encoders.Contains("aac")
    )
}

function Find-ReusableEnvironment {
    param([Parameter(Mandatory = $true)][string]$BootstrapPython)
    $discoverScript = Join-Path $PSScriptRoot "discover_environments.py"
    $temporaryReport = [IO.Path]::GetTempFileName()
    try {
        & $BootstrapPython $discoverScript `
            --profile $Profile `
            --motion-effects $MotionEffects `
            --output-json $temporaryReport | Out-Null
        $discoveryExitCode = $LASTEXITCODE
        $discoveryReport = Get-Content $temporaryReport -Raw | ConvertFrom-Json
        return [PSCustomObject]@{
            ExitCode = $discoveryExitCode
            Report = $discoveryReport
        }
    }
    finally {
        Remove-Item $temporaryReport -Force -ErrorAction SilentlyContinue
    }
}

$bundledWhisperModel = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\assets\whisper\tiny.pt"))
if (Test-Path $bundledWhisperModel -PathType Leaf) {
    $env:WHISPER_MODEL_DIR = Split-Path $bundledWhisperModel -Parent
}
if (-not $ReportPath) {
    $ReportPath = Join-Path (Get-Location) "video_environment.json"
}
if (-not $ToolsDirectory) {
    $ToolsDirectory = Join-Path $HOME ".ai-video-editing-tools"
}
if ($FfmpegBinDir) {
    Add-ProcessPath -Directory (Resolve-FfmpegBinDirectory -Root $FfmpegBinDir)
}

$bootstrapPython = Get-PythonExecutable
$pythonExe = $null
$discovery = $null
$setupActions = New-Object System.Collections.Generic.List[string]
$ffmpegSource = if (Test-FfmpegCapabilities) { "existing-path" } else { $null }

if ($bootstrapPython) {
    $discovery = Find-ReusableEnvironment -BootstrapPython $bootstrapPython
    if ($discovery.Report.ok) {
        $pythonExe = [string]$discovery.Report.selected_environment.python_executable
        Write-Host "Reusing capability-validated environment: $pythonExe"
    }
    elseif (-not $Install) {
        $resolvedDiscoveryReport = [IO.Path]::GetFullPath($ReportPath)
        New-Item -ItemType Directory -Path (Split-Path $resolvedDiscoveryReport -Parent) -Force | Out-Null
        $discovery.Report | ConvertTo-Json -Depth 30 |
            Set-Content -LiteralPath $resolvedDiscoveryReport -Encoding UTF8
        $discovery.Report | ConvertTo-Json -Depth 20
        [Console]::Error.WriteLine("No existing environment passed profile '$Profile'. Re-run with -Install to create '$EnvironmentName'.")
        exit 2
    }
}
elseif (-not $Install) {
    $resolvedMissingPythonReport = [IO.Path]::GetFullPath($ReportPath)
    New-Item -ItemType Directory -Path (Split-Path $resolvedMissingPythonReport -Parent) -Force | Out-Null
    [PSCustomObject]@{
        ok = $false
        status = "blocked"
        profile = $Profile
        motion_effects = $MotionEffects
        checks = [PSCustomObject]@{
            python = [PSCustomObject]@{
                ok = $false
                required = $true
                error = "No Python 3.10+ bootstrap was found"
            }
        }
        errors = @("python")
        warnings = @()
    } | ConvertTo-Json -Depth 30 |
        Set-Content -LiteralPath $resolvedMissingPythonReport -Encoding UTF8
    [Console]::Error.WriteLine("No Python 3.10+ bootstrap was found. Re-run with -Install to create '$EnvironmentName'.")
    exit 2
}

if (-not $pythonExe) {
    Write-Host "No reusable environment passed the capability checks. Installing '$EnvironmentName'."

    if (-not $bootstrapPython -or -not (Test-PythonBootstrapCapabilities -PythonExecutable $bootstrapPython)) {
        Install-WingetPackage -Id "Python.Python.3.11"
        $setupActions.Add("installed-python-3.11")
        if ($FfmpegBinDir) {
            Add-ProcessPath -Directory (Resolve-FfmpegBinDirectory -Root $FfmpegBinDir)
        }
        $bootstrapPython = Get-PythonExecutable
        if (-not $bootstrapPython) {
            throw "Python was installed but is not visible in the current PowerShell. Reopen PowerShell and rerun this script."
        }
        if (-not (Test-PythonBootstrapCapabilities -PythonExecutable $bootstrapPython)) {
            throw "Python 3.11 is present but venv, ensurepip, or pip is unavailable."
        }
    }

    if (-not (Test-FfmpegCapabilities)) {
        if ($FfmpegArchive) {
            $binDir = Expand-ValidatedFfmpegArchive `
                -ArchivePath $FfmpegArchive `
                -Destination (Join-Path $ToolsDirectory "ffmpeg")
            Add-ProcessPath -Directory $binDir
            $ffmpegSource = "portable-archive"
            $setupActions.Add("expanded-portable-ffmpeg")
        }
        elseif ($FfmpegDownloadUrl) {
            $downloadedArchive = Download-FfmpegArchive `
                -Url $FfmpegDownloadUrl `
                -Destination (Join-Path $ToolsDirectory "downloads\ffmpeg.zip")
            $binDir = Expand-ValidatedFfmpegArchive `
                -ArchivePath $downloadedArchive `
                -Destination (Join-Path $ToolsDirectory "ffmpeg")
            Add-ProcessPath -Directory $binDir
            $ffmpegSource = "resumable-portable-download"
            $setupActions.Add("downloaded-and-expanded-portable-ffmpeg")
        }
        else {
            Install-WingetPackage -Id "Gyan.FFmpeg"
            $ffmpegSource = "winget"
            $setupActions.Add("installed-ffmpeg-with-winget")
        }
        if (-not (Test-FfmpegCapabilities)) {
            throw "FFmpeg is installed or extracted but lacks ffprobe, subtitles, loudnorm, ebur128, libx264, or aac."
        }
    }

    if (-not $EnvironmentPath) {
        $EnvironmentPath = Join-Path (Join-Path $HOME ".virtualenvs") $EnvironmentName
    }
    $resolvedEnvironment = [IO.Path]::GetFullPath($EnvironmentPath)
    & $bootstrapPython -m venv $resolvedEnvironment
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create virtual environment at $resolvedEnvironment"
    }
    $pythonExe = Join-Path $resolvedEnvironment "Scripts\python.exe"

    & $pythonExe -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to bootstrap pip with ensurepip in $pythonExe"
    }
    & $pythonExe -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upgrade pip in $pythonExe"
    }

    if ($Profile -eq "soda-scripted-render") {
        & $pythonExe -m pip install -U openai-whisper
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install openai-whisper in $pythonExe"
        }
        if (Test-Path $bundledWhisperModel -PathType Leaf) {
            & $pythonExe -c "import os, whisper; whisper.load_model('tiny', download_root=os.environ['WHISPER_MODEL_DIR'])"
            $setupActions.Add("used-bundled-whisper-tiny")
        }
        else {
            & $pythonExe -c "import whisper; whisper.load_model('tiny')"
            $setupActions.Add("downloaded-whisper-tiny")
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Whisper installed but the tiny model could not be loaded"
        }
    }

    if ($MotionEffects -eq "required") {
        if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
            Install-WingetPackage -Id "OpenJS.NodeJS.LTS"
        }
        $chromeCandidates = @(
            "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
            "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
        )
        if (-not ($chromeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1)) {
            Install-WingetPackage -Id "Google.Chrome"
        }
    }
}

$scriptsDir = & $pythonExe -c "import sysconfig; print(sysconfig.get_path('scripts'))"
if ($LASTEXITCODE -ne 0 -or -not $scriptsDir) {
    throw "Could not resolve the Scripts directory for $pythonExe"
}
Add-ProcessPath -Directory ($scriptsDir.Trim())

$checkScript = Join-Path $PSScriptRoot "check_environment.py"
$resolvedReportPath = [IO.Path]::GetFullPath($ReportPath)
$arguments = @(
    $checkScript,
    "--profile", $Profile,
    "--motion-effects", $MotionEffects,
    "--output-json", $resolvedReportPath
)

Write-Host "Using Python: $pythonExe"
Write-Host "Current Scripts PATH: $($scriptsDir.Trim())"
& $pythonExe @arguments
$checkExitCode = $LASTEXITCODE
if (Test-Path $resolvedReportPath -PathType Leaf) {
    $report = Get-Content $resolvedReportPath -Raw | ConvertFrom-Json
    $bundledWhisperModelForReport = $null
    if (Test-Path $bundledWhisperModel -PathType Leaf) {
        $bundledWhisperModelForReport = $bundledWhisperModel
    }
    $setupMetadata = [PSCustomObject]@{
        setup_script = [IO.Path]::GetFullPath($MyInvocation.MyCommand.Path)
        environment_name = $EnvironmentName
        python_executable = $pythonExe
        ffmpeg_source = $ffmpegSource
        tools_directory = [IO.Path]::GetFullPath($ToolsDirectory)
        bundled_whisper_model = $bundledWhisperModelForReport
        actions = @($setupActions)
    }
    $report | Add-Member -NotePropertyName setup -NotePropertyValue $setupMetadata -Force
    $report | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $resolvedReportPath -Encoding UTF8
}
Write-Host "Environment report: $resolvedReportPath"
exit $checkExitCode
