#define AppName "Символ — расчёт заработной платы"
#define AppVersion "0.1.0"
#define AppPublisher "Symbol"
#define AppExeName "SymbolPayroll.exe"

[Setup]
AppId={{7D2B9DA1-3F2D-4C9B-95B5-0A6D1D18C102}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Symbol Payroll
DefaultGroupName=Символ
OutputDir=..\dist\installer
OutputBaseFilename=SymbolPayroll-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#AppExeName}

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительные значки:"; Flags: unchecked

[Files]
Source: "..\dist\SymbolPayroll\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Символ"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\Символ"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Запустить Символ"; Flags: postinstall nowait skipifsilent
