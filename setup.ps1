param([Parameter(Mandatory=$true)][string]$DeploymentRoot, [switch]$NoShortcut)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$botRoot = (Resolve-Path -LiteralPath $DeploymentRoot).Path
$pythonw = Join-Path $botRoot '.venv\Scripts\pythonw.exe'
foreach ($relative in @('manage.py','group_manager.py','sticker_manager.py','data\cmd_config.json','.venv\Scripts\pythonw.exe')) {
    if (-not (Test-Path -LiteralPath (Join-Path $botRoot $relative))) { throw "部署目录缺少 $relative，请查看 README 的接入要求。" }
}
@{deployment_root=$botRoot} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $projectRoot 'local.config.json') -Encoding UTF8
if (-not $NoShortcut) {
    $desktopPath = [Environment]::GetFolderPath('Desktop')
    $shortcutPath = Join-Path $desktopPath '大肥鱼管理台.lnk'
    $wshell = New-Object -ComObject WScript.Shell
    $shortcut = $wshell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = '"' + (Join-Path $projectRoot 'launch.py') + '" --root "' + $botRoot + '"'
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.Description = '打开本地网页管理台：机器人启停、微信、群聊、角色、表情与记忆'
    $shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,20"
    $shortcut.WindowStyle = 7
    $shortcut.Save()
    Write-Output '已创建桌面快捷方式：大肥鱼管理台'
}
Write-Output '配置完成。双击桌面快捷方式打开；管理台与机器人独立运行。'
