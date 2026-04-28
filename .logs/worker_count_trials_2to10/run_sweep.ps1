$ErrorActionPreference = 'Stop'
$root = 'P:\packages\yt-is\.logs\worker_count_trials_2to10'
New-Item -ItemType Directory -Force -Path $root | Out-Null
$cohort = Join-Path $root 'cohort.json'
$counts = 2,4,6,8,10
$launcherLog = Join-Path $root 'launcher.log'
Add-Content -Path $launcherLog -Value ("[{0}] starting worker-count sweep" -f (Get-Date).ToString('s'))
foreach ($w in $counts) {
  $out = Join-Path $root ("w{0:d2}" -f $w)
  New-Item -ItemType Directory -Force -Path $out | Out-Null
  $log = Join-Path $root ("w{0:d2}.log" -f $w)
  Add-Content -Path $launcherLog -Value ("[{0}] running workers={1} output={2}" -f (Get-Date).ToString('s'), $w, $out)
  & 'C:\Python314\python.exe' P:\packages\yt-is\bin\csf-fallback-crossover-benchmark --cohort-shape mixed --cohort-json $cohort --workers $w --limit 400 --batch-size 50 --policy notebooklm_route_plus_fallback_30s_1w --sample-label mixed_lane --output-root $out *>&1 | Tee-Object -FilePath $log
  Add-Content -Path $launcherLog -Value ("[{0}] finished workers={1}" -f (Get-Date).ToString('s'), $w)
}
Add-Content -Path $launcherLog -Value ("[{0}] sweep complete" -f (Get-Date).ToString('s'))
