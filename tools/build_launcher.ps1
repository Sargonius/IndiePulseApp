<#
Builds IndiePulse.exe (launcher\*.cs) with the C# compiler that ships inside Windows (.NET Framework 4.8): no
Visual Studio, SDK or NuGet. That compiler knows C# 5 only: no $"...", ?., nameof, => members, auto-property
initialisers.

  .\tools\build_launcher.ps1                          # IndiePulse.exe in the checkout's root (git ignores it)
  .\tools\build_launcher.ps1 -Out dist\IndiePulse.exe

The version is __version__ of app\__init__.py, the icon app\static\indiepulse.ico (also inside, for the window).
The release workflow runs this before tools\build_release.py, which puts the exe into the zip.
Windows PowerShell 5.1 and PowerShell 7; ASCII only (5.1 reads a script without a BOM in the ANSI code page).
#>
param([string]$Out = '')

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root 'launcher'
$obj = Join-Path $src 'obj'
$fw = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$csc = Join-Path $fw 'csc.exe'
if (-not (Test-Path $csc)) { throw "No C# compiler at $csc (.NET Framework 4.8 is part of Windows 10 and 11)." }
if (-not $Out) { $Out = Join-Path $root 'IndiePulse.exe' }
# relative to where the script was called from, not .NET's idea of the current folder
$Out = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Out)
New-Item -ItemType Directory -Force $obj, (Split-Path -Parent $Out) | Out-Null

$init = [IO.File]::ReadAllText((Join-Path $root 'app\__init__.py'))
$m = [regex]::Match($init, '(?m)^__version__\s*=\s*"([^"]+)"')
if (-not $m.Success) { throw 'No __version__ in app\__init__.py' }
$version = $m.Groups[1].Value
$n = [regex]::Match($version, '^(\d+)\.(\d+)\.(\d+)')
if (-not $n.Success) { throw "__version__ '$version' isn't X.Y.Z..." }
$numeric = '{0}.{1}.{2}.0' -f $n.Groups[1].Value, $n.Groups[2].Value, $n.Groups[3].Value
$versionCs = Join-Path $obj 'Version.cs'
$lines = @(
	'// written by tools\build_launcher.ps1 from app\__init__.py',
	'using System.Reflection;',
	"[assembly: AssemblyVersion(`"$numeric`")]",
	"[assembly: AssemblyFileVersion(`"$numeric`")]",
	"[assembly: AssemblyInformationalVersion(`"$version`")]"
)
[IO.File]::WriteAllText($versionCs, ($lines -join "`r`n") + "`r`n", (New-Object System.Text.UTF8Encoding $false))

$refs = @('System.dll', 'System.Core.dll', 'System.Drawing.dll', 'System.Windows.Forms.dll', 'System.Management.dll',
	'System.IO.Compression.dll', 'System.IO.Compression.FileSystem.dll') | ForEach-Object { '/r:' + (Join-Path $fw $_) }
$ico = Join-Path $root 'app\static\indiepulse.ico'
$sources = @(Get-ChildItem (Join-Path $src '*.cs') | Sort-Object Name | ForEach-Object { $_.FullName }) + $versionCs
$cscArgs = @('/nologo', '/codepage:65001', '/langversion:5', '/target:winexe', '/optimize+', '/platform:anycpu',
	'/warnaserror+', "/out:$Out", "/win32icon:$ico", "/win32manifest:$(Join-Path $src 'app.manifest')",
	"/resource:$ico,IndiePulse.ico") + $refs + $sources
& $csc @cscArgs | Write-Host
if ($LASTEXITCODE -ne 0) { throw "csc failed with exit code $LASTEXITCODE" }
$exe = Get-Item $Out
Write-Host ('IndiePulse.exe {0}: {1} ({2:N0} KB)' -f $version, $exe.FullName, ($exe.Length / 1KB))
