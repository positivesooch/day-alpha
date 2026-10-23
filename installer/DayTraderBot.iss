; Inno Setup 6 script for Day Trader Bot.
;
;   ISCC.exe /DMyAppVersion=2.4.1 installer\DayTraderBot.iss
;
; Expects the PyInstaller output in dist\DayTraderBot\ (see build.ps1).
; Writes installer\Output\DayTraderBot-Setup-<version>.exe.
;
; DayTraderBot.exe is the window; DayTraderBotCLI.exe is the console program
; (the same commands as bot.py) that the window drives and the PATH entry exposes.
;
; The app's own data (trader\config.json, trader\bot.json, the session logs,
; replays) lives in %USERPROFILE%\DayTraderBot, never in Program Files, and
; the uninstaller leaves it alone.

#ifndef MyAppVersion
  #define MyAppVersion "2.4.1"
#endif
#define MyAppName "Day Trader Bot"
#define MyAppPublisher "Day Trader skill"
#define MyAppURL "https://github.com/Jakeschincariol/day-trader-skill"
#define MyAppExeName "DayTraderBot.exe"
#define MyCliExeName "DayTraderBotCLI.exe"
#define DistDir "..\dist\DayTraderBot"

[Setup]
AppId={{7D2F3C7A-5B0E-4E2B-9B1A-0DA17BAD3B07}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile={#DistDir}\_internal\LICENSE
InfoBeforeFile=before-install.txt
OutputDir=Output
OutputBaseFilename=DayTraderBot-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ChangesEnvironment=yes
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "addtopath"; Description: "Add Day Trader Bot to the PATH (run DayTraderBotCLI from any Command Prompt)"; GroupDescription: "Command line:"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "The window: replay, run, status"
Name: "{group}\{#MyAppName} - console menu"; Filename: "{app}\{#MyCliExeName}"; Comment: "The same, as a console menu"
Name: "{group}\{#MyAppName} - replay the reel day"; Filename: "{app}\{#MyCliExeName}"; Parameters: "--pause replay --fixtures --fresh --mode auto"; Comment: "TSLA, Monday 2026-10-19, on the paper sim"
Name: "{group}\{#MyAppName} - run today (dry-run)"; Filename: "{app}\{#MyCliExeName}"; Parameters: "--pause run"; Comment: "Watch today's session, place nothing"
Name: "{group}\{#MyAppName} - my data folder"; Filename: "{%USERPROFILE}\DayTraderBot"; Comment: "trader\config.json, trader\bot.json, logs and replays"
Name: "{group}\README"; Filename: "{app}\_internal\README.md"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "{code:PathRegKey}"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Open {#MyAppName} now"; Flags: nowait postinstall skipifsilent

[Code]
function PathRegKey(Param: String): String;
begin
  if IsAdminInstallMode then
    Result := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
  else
    Result := 'Environment';
end;

function NeedsAddPath(Param: String): Boolean;
var
  OrigPath: String;
  RootKey: Integer;
begin
  if IsAdminInstallMode then
    RootKey := HKLM
  else
    RootKey := HKCU;
  if not RegQueryStringValue(RootKey, PathRegKey(''), 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;
