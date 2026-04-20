Get-CimInstance Win32_Process -Filter "name='python.exe'" | ForEach-Object {
    Write-Host "PID $($_.ProcessId) | Path: $($_.ExecutablePath)"
    Write-Host "         Cmd : $($_.CommandLine)"
    Write-Host ""
}
