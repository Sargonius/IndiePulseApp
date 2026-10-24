<#
Releases IndiePulse: tags main as v<version> and pushes it. GitHub Actions (.github\workflows\release.yml) then
builds IndiePulse-<version>.zip + latest.json and publishes the release, which installed copies offer to update to.

  .\tools\release.ps1                     # the version main is at, without -dev (2.0.0-dev -> 2.0.0)
  .\tools\release.ps1 2.1.0               # or name it (a bigger step than that)
  .\tools\release.ps1 2.1.0 -Notes "Calendar is faster. Shorts board fixed."
  .\tools\release.ps1 2.1.0-rc.1          # a prerelease: installed copies don't take it, it is for trying out

Stops at the first problem; nothing changes before you type yes:
  1. on main, nothing uncommitted, nothing on origin/main that isn't here;
  2. the version is X.Y.Z or X.Y.Z-rc.N, newer than every v* tag, the tag is free (here and on origin);
  3. pyflakes (tools\lint.py) and the tests pass;
  4. shows what it will do and asks;
  5. commits __version__ = "X.Y.Z" (Release X.Y.Z), tags that commit vX.Y.Z with the notes as the tag message (they
     become the release notes), commits the next -dev version (Start X.Y.Z+1-dev), pushes main and the tag at once.
     If any of that fails, the two commits and the tag are undone: run it again.
Notes default to the commit subjects since the last tag.
Windows PowerShell 5.1 compatible; ASCII only (5.1 reads a script without a BOM in the ANSI code page).
#>
param(
	[Parameter(Position = 0)][string]$Version = '',
	[string]$Notes = '',
	[string]$Python = ''   # default: .venv\Scripts\python.exe of this checkout
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$versionFile = Join-Path $root 'app\__init__.py'
if (-not $Python) { $Python = Join-Path $root '.venv\Scripts\python.exe' }
$utf8 = New-Object System.Text.UTF8Encoding $false

function Invoke-Git {
	# stderr is merged and turned into text: 5.1 makes every stderr line of a native command an error under 'Stop'.
	# safe.directory: the checkout may belong to another Windows account of the same person.
	$saved = $ErrorActionPreference
	$ErrorActionPreference = 'Continue'
	try { $out = @(& git -c 'safe.directory=*' -C $root @args 2>&1 | ForEach-Object { "$_" }) }
	finally { $ErrorActionPreference = $saved }
	if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') failed:`n$($out -join "`n")" }
	return $out
}

function Get-GitLine {
	# the first line of git's output; @() because a one-line answer would come back as a bare string
	return @(Invoke-Git @args)[0]
}

function Get-VersionKey([string]$v) {
	# a sortable string; the same order as app/services/updates.py: 2.0.0-dev < 2.0.0-rc.1 < 2.0.0 < 2.0.1-dev
	if ($v -notmatch '^v?(\d+)\.(\d+)\.(\d+)(?:-(dev|rc)(?:\.(\d+))?)?$') { return $null }
	$stage = switch ($Matches[4]) { 'dev' { 0 } 'rc' { 1 } default { 2 } }
	$n = 0
	if ($Matches[5]) { $n = [int]$Matches[5] }
	return '{0:D6}.{1:D6}.{2:D6}.{3}.{4:D6}' -f [int]$Matches[1], [int]$Matches[2], [int]$Matches[3], $stage, $n
}

function Set-AppVersion([string]$v) {
	$text = [IO.File]::ReadAllText($versionFile)
	$text = [regex]::Replace($text, '(?m)^(__version__\s*=\s*")[^"]+(")', "`${1}$v`${2}")
	[IO.File]::WriteAllText($versionFile, $text, $utf8)
}

# 1. the checkout
$branch = Get-GitLine rev-parse --abbrev-ref HEAD
if ($branch -ne 'main') { throw "Releases are made from main; this checkout is on '$branch'." }
$dirty = @(Invoke-Git status --porcelain | Where-Object { $_ })
if ($dirty.Count) { throw "Commit or stash these first:`n$($dirty -join "`n")" }
Write-Host 'Fetching origin...'
Invoke-Git fetch --quiet --tags origin main | Out-Null
$behind = [int](Get-GitLine rev-list --count 'HEAD..origin/main')
if ($behind) { throw "origin/main has $behind commit(s) that this checkout doesn't: git pull first." }
$ahead = [int](Get-GitLine rev-list --count 'origin/main..HEAD')
$start = Get-GitLine rev-parse HEAD

# 2. the version
$m = [regex]::Match([IO.File]::ReadAllText($versionFile), '(?m)^__version__\s*=\s*"([^"]+)"')
if (-not $m.Success) { throw "No __version__ in $versionFile" }
$current = $m.Groups[1].Value
if (-not $Version) { $Version = $current -replace '-dev$', '' }
$Version = $Version -replace '^v', ''
if ($Version -notmatch '^\d+\.\d+\.\d+(-rc\.\d+)?$') { throw "'$Version' isn't a release version: X.Y.Z or X.Y.Z-rc.N" }
$tag = "v$Version"
$last = @(Invoke-Git tag --list 'v*') | Where-Object { Get-VersionKey $_ } |
	Sort-Object { Get-VersionKey $_ } | Select-Object -Last 1
if ($last -and ((Get-VersionKey $Version) -le (Get-VersionKey $last))) { throw "$tag isn't newer than $last." }
if (@(Invoke-Git ls-remote --tags origin "refs/tags/$tag" | Where-Object { $_ }).Count) {
	throw "$tag already exists on origin."
}
$core = $Version -replace '-rc\.\d+$', ''
if ($Version -ne $core) {
	$nextDev = "$core-dev"   # after an rc main goes on towards the same release
} else {
	$parts = $core.Split('.')
	$nextDev = '{0}.{1}.{2}-dev' -f $parts[0], $parts[1], ([int]$parts[2] + 1)
}
if (-not $Notes) {
	$range = 'HEAD'
	if ($last) { $range = "$last..HEAD" }
	$Notes = (@(Invoke-Git log --no-merges '--pretty=format:- %s' $range) | Select-Object -First 40) -join "`n"
}
if (-not $Notes.Trim()) { $Notes = "IndiePulse $Version" }

# 3. lint and tests
if (-not (Test-Path $Python)) { throw "No $Python - make the .venv first (README, Development)." }
Write-Host 'pyflakes...'
& $Python (Join-Path $root 'tools\lint.py')
if ($LASTEXITCODE -ne 0) { throw 'pyflakes found problems.' }
Write-Host 'Tests (a minute or two)...'
Push-Location $root
try { & $Python -m pytest -q -p no:cacheprovider } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }

# 4. ask
$remote = Get-GitLine remote get-url origin
Write-Host ''
Write-Host "Release IndiePulse $Version" -ForegroundColor Cyan
Write-Host "  main is at     $current"
Write-Host "  commit         Release $Version   (__version__ = `"$Version`")"
Write-Host "  tag            $tag   (annotated; its message is the release notes)"
Write-Host "  then commit    Start $nextDev"
Write-Host "  push           main + $tag to $remote ($ahead commit(s) not pushed yet go with them)"
Write-Host 'Release notes:'
Write-Host $Notes
Write-Host ''
$answer = Read-Host "Type yes to release $tag"
if ($answer -ne 'yes') { Write-Host 'Nothing changed.'; exit 1 }

# 5. commit, tag, push - undone as a whole if any step fails
$notesFile = [IO.Path]::GetTempFileName()
try {
	[IO.File]::WriteAllText($notesFile, $Notes + "`n", $utf8)
	Set-AppVersion $Version
	Invoke-Git add app/__init__.py | Out-Null
	Invoke-Git commit --quiet -m "Release $Version" -m "Tagged ${tag}: the release workflow builds IndiePulse-$Version.zip from this commit." | Out-Null
	Invoke-Git tag -a $tag -F $notesFile --cleanup=whitespace | Out-Null
	Set-AppVersion $nextDev
	Invoke-Git add app/__init__.py | Out-Null
	Invoke-Git commit --quiet -m "Start $nextDev" -m "main carries the next version with -dev, so a checkout is never taken for the release." | Out-Null
	Write-Host "Pushing main and $tag..."
	Invoke-Git push --atomic origin main $tag | Out-Null
} catch {
	Write-Host "Failed: $_" -ForegroundColor Red
	Write-Host 'Undoing the release commits and the tag; nothing reached origin (the push is atomic).'
	$saved = $ErrorActionPreference
	$ErrorActionPreference = 'Continue'
	& git -c 'safe.directory=*' -C $root tag -d $tag 2>&1 | Out-Null
	& git -c 'safe.directory=*' -C $root reset --hard --quiet $start 2>&1 | Out-Null
	$ErrorActionPreference = $saved
	exit 1
} finally {
	Remove-Item $notesFile -ErrorAction SilentlyContinue
}

$repo = [regex]::Match($remote, 'github\.com[:/](.+?)(\.git)?$').Groups[1].Value
Write-Host "Pushed $tag. GitHub Actions builds and publishes the release:" -ForegroundColor Green
if ($repo) { Write-Host "  https://github.com/$repo/actions" } else { Write-Host "  (the Actions tab of $remote)" }
