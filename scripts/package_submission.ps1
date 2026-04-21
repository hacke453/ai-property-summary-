param(
  [Parameter(Mandatory = $true)][string]$InspectionPdf,
  [Parameter(Mandatory = $true)][string]$ThermalPdf,
  [string]$OutDir = ".\out",
  [string]$ZipPath = ".\submission_bundle.zip"
)

$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)  # repo root

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
  throw "Virtualenv not found at .\.venv. Run setup first (see README.md)."
}

if (-not (Test-Path $InspectionPdf)) { throw "Inspection PDF not found: $InspectionPdf" }
if (-not (Test-Path $ThermalPdf)) { throw "Thermal PDF not found: $ThermalPdf" }

Write-Host "Generating DDR into $OutDir ..."
.\.venv\Scripts\python.exe .\scripts\generate_ddr.py --inspection "$InspectionPdf" --thermal "$ThermalPdf" --outdir "$OutDir"

if (Test-Path $ZipPath) {
  try {
    Remove-Item $ZipPath -Force
  }
  catch {
    $base = [System.IO.Path]::GetFileNameWithoutExtension($ZipPath)
    $ext = [System.IO.Path]::GetExtension($ZipPath)
    $ts = (Get-Date).ToString("yyyyMMdd_HHmmss")
    $ZipPath = ".\${base}_${ts}${ext}"
    Write-Host "Existing zip is locked; writing to $ZipPath"
  }
}

$tmpDir = Join-Path $env:TEMP ("ddr_submission_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmpDir | Out-Null

try {
  # Bundle outputs
  Copy-Item -Recurse -Force "$OutDir" (Join-Path $tmpDir "out")

  # Bundle the minimal code needed to reproduce
  New-Item -ItemType Directory -Path (Join-Path $tmpDir "project") | Out-Null
  Copy-Item -Force ".\requirements.txt" (Join-Path $tmpDir "project\requirements.txt")
  Copy-Item -Force ".\README.md" (Join-Path $tmpDir "project\README.md")
  Copy-Item -Recurse -Force ".\scripts" (Join-Path $tmpDir "project\scripts")

  # Create zip
  Compress-Archive -Path (Join-Path $tmpDir "*") -DestinationPath $ZipPath -Force
  Write-Host "Created: $ZipPath"
}
finally {
  Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
}

