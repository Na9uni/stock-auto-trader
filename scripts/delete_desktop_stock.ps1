Add-Type -AssemblyName Microsoft.VisualBasic
$target = 'C:\Users\640jj\Desktop\stock'
if (-not (Test-Path $target)) {
    Write-Host "ALREADY GONE: $target"
    exit 0
}
try {
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
        $target,
        [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
        [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
    )
    Write-Host "SUCCESS: moved to Recycle Bin -> $target"
} catch {
    Write-Host ("FAIL: " + $_.Exception.Message)
    exit 1
}
