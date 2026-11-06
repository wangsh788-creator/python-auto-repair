$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $repairRuntime = Join-Path $PSScriptRoot 'vendor\codex\codex.exe'
    foreach ($repairRequired in @($repairRuntime, 'vendor\codex\LICENSE', 'vendor\codex\NOTICE', 'vendor\codex\PROVENANCE.txt')) {
        if (-not (Test-Path -LiteralPath $repairRequired -PathType Leaf)) {
            throw "Required distribution file is missing: $repairRequired"
        }
    }
    $repairBuildArguments = @(
        '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
        '--name', 'PythonAutoRepair', '--paths', '.',
        '--distpath', 'dist', '--workpath', 'build', '--specpath', '.',
        '--add-binary', "$repairRuntime;codex-runtime",
        '--add-data', 'vendor\codex\LICENSE;codex-runtime',
        '--add-data', 'vendor\codex\NOTICE;codex-runtime',
        '--add-data', 'vendor\codex\PROVENANCE.txt;codex-runtime',
        'app.pyw'
    )
    python @repairBuildArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'PyInstaller build failed.'
    }
}
finally {
    Pop-Location
}
