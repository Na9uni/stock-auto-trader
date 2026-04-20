Set sh = CreateObject("WScript.Shell")
desktop = sh.SpecialFolders("Desktop")

Set lnk1 = sh.CreateShortcut(desktop & "\Stock Start.lnk")
lnk1.TargetPath = "C:\stock\run_all.bat"
lnk1.WorkingDirectory = "C:\stock"
lnk1.IconLocation = "C:\Windows\System32\shell32.dll,137"
lnk1.Description = "Stock Auto Trader - Collector + Scheduler"
lnk1.Save

Set lnk2 = sh.CreateShortcut(desktop & "\Stock Dashboard.lnk")
lnk2.TargetPath = "C:\stock\run_dashboard.bat"
lnk2.WorkingDirectory = "C:\stock"
lnk2.IconLocation = "C:\Windows\System32\shell32.dll,165"
lnk2.Description = "Streamlit Dashboard"
lnk2.Save

Set lnk3 = sh.CreateShortcut(desktop & "\Stock Folder.lnk")
lnk3.TargetPath = "C:\stock"
lnk3.IconLocation = "C:\Windows\System32\shell32.dll,4"
lnk3.Description = "Open C:\stock folder"
lnk3.Save

WScript.Echo "SUCCESS: 3 shortcuts created"
