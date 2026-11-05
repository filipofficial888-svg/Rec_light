; RecLight - instalátor (Inno Setup 6)
#define AppName "RecLight"
#define AppVersion "2.0"

[Setup]
AppId={{8F3A6C52-4B7D-4E1A-9A55-1D2C7E6B0A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=RecLight
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion=2.0.0.0
DefaultDirName={localappdata}\Programs\RecLight
DefaultGroupName=RecLight
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=RecLight-{#AppVersion}-Windows-Setup
SetupIconFile=reclight.ico
UninstallDisplayIcon={app}\RecLight.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
#if FileExists(CompilerPath + "Languages\Czech.isl")
Name: "czech"; MessagesFile: "compiler:Languages\Czech.isl"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Spustit RecLight automaticky po přihlášení do Windows (doporučeno, aby byl MIDI port vždy k dispozici)"
Name: "loopmidi"; Description: "Nainstalovat loopMIDI (ovladač virtuálního MIDI portu pro Studio One / Cubase; Windows se zeptá na souhlas správce)"; Check: not LoopMidiInstalled
Name: "desktopicon"; Description: "Vytvořit zástupce na ploše"; Flags: unchecked

[Files]
Source: "dist\RecLight.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "firmware\*"; DestDir: "{app}\firmware"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; DestName: "README.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\RecLight"; Filename: "{app}\RecLight.exe"
Name: "{autoprograms}\RecLight firmware (ESP32, Arduino)"; Filename: "{app}\firmware"
Name: "{autodesktop}\RecLight"; Filename: "{app}\RecLight.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "RecLight"; ValueData: """{app}\RecLight.exe"" --hidden"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\RecLight.exe"; Description: "Spustit RecLight"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /f /im RecLight.exe"; Flags: runhidden; RunOnceId: "KillRecLight"

[Code]
function LoopMidiInstalled: Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf32}\Tobias Erichsen\loopMIDI\loopMIDI.exe'))
         or FileExists(ExpandConstant('{commonpf}\Tobias Erichsen\loopMIDI\loopMIDI.exe'))
         or FileExists(ExpandConstant('{sys}\teVirtualMIDI64.dll'));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  { zavře běžící RecLight, aby šel přepsat soubor }
  Exec(ExpandConstant('{cmd}'), '/C taskkill /f /im RecLight.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('loopmidi') then
  begin
    WizardForm.StatusLabel.Caption := 'Instaluji loopMIDI (potvrďte okno správce)...';
    Exec(ExpandConstant('{cmd}'),
      '/C winget install -e --id TobiasErichsen.loopMIDI --silent --accept-package-agreements --accept-source-agreements',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    if not LoopMidiInstalled then
      MsgBox('loopMIDI se nepodařilo nainstalovat automaticky.' + #13#10 +
             'Stáhněte ho ručně z https://www.tobias-erichsen.de/software/loopmidi.html ' +
             'a spusťte RecLight znovu. Port RecLight si aplikace vytvoří sama.',
             mbInformation, MB_OK);
    if not LoopMidiInstalled then
      ShellExec('open', 'https://www.tobias-erichsen.de/software/loopmidi.html', '', '', SW_SHOWNORMAL, ewNoWait, ResultCode);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'RecLight');
end;
