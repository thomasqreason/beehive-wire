# Beehive Wire — move the editions to six and six Eastern, and push.
#   powershell -ExecutionPolicy Bypass -File .\retime.ps1

Set-Location -Path $PSScriptRoot

Write-Host ""
Write-Host "1/3  Retiming the schedule..." -ForegroundColor Cyan
$f = '.github\workflows\build.yml'
$t = [IO.File]::ReadAllText($f)
$t = $t.Replace('7:00 a.m. and 6:00 p.m. Mountain, expressed in UTC', '6:00 a.m. and 6:00 p.m. EASTERN, where four in five US readers are')
$t = $t.Replace('change this line to "0 14,1 * * *"', 'change this line to "50 10,22 * * *"')
$t = $t.Replace('"0 13,0 * * *"', '"50 9,21 * * *"')
[IO.File]::WriteAllText($f, $t)
Select-String -Path $f -Pattern 'cron:' -Context 0,0

Write-Host "2/3  Pulling anything the bot published..." -ForegroundColor Cyan
git pull --rebase --autostash
if ($LASTEXITCODE -ne 0) { Write-Host "     pull failed - stopping." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "3/3  Committing and pushing..." -ForegroundColor Cyan
git add -A
git commit -m "Editions at 6am and 6pm Eastern; sign-off quotes the readers' clock"
git push
if ($LASTEXITCODE -ne 0) { Write-Host "     push failed." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "Done. Next edition: 6:00 a.m. Eastern / 4:00 a.m. Mountain." -ForegroundColor Green
