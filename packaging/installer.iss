#ifndef ReleaseDir
  #error ReleaseDir must point to the PyInstaller output directory.
#endif
#ifndef AppVersion
  #define AppVersion "2.0.4"
#endif
#ifndef InstallerAppId
  #define InstallerAppId "{{34648710-F74C-44F7-BB72-73CD0B346D4C}"
#endif
#ifndef OutputName
  #define OutputName "PowerBIAssistant-Setup-" + AppVersion
#endif

[Setup]
AppId={#InstallerAppId}
AppName=Power BI Assistant
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\PowerBIAssistant
DefaultGroupName=Power BI Assistant
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
DisableProgramGroupPage=yes
OutputDir={#ReleaseDir}
OutputBaseFilename={#OutputName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\PowerBIAssistant.exe
CloseApplications=yes
SetupLogging=yes

[Files]
Source: "{#ReleaseDir}\LicenseVerifier.exe"; Flags: dontcopy
Source: "{#ReleaseDir}\PowerBIAssistant\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{tmp}\pbi-license.key"; DestDir: "{app}"; DestName: "license.key"; Flags: external ignoreversion

[Icons]
Name: "{group}\Power BI Assistant"; Filename: "{app}\PowerBIAssistant.exe"

[Run]
Filename: "{app}\PowerBIAssistant.exe"; Description: "启动 Power BI Assistant"; Flags: nowait postinstall skipifsilent

[Code]
var
  LicensePage: TInputQueryWizardPage;
  LicensePath: String;
  VerifierPath: String;
  ResultPath: String;
  LicenseSummary: String;

function ReadUTF8File(const Path: String; var Value: String): Boolean;
var
  Bytes: AnsiString;
begin
  Result := LoadStringFromFile(Path, Bytes);
  if Result then
  begin
    Value := UTF8Decode(Bytes);
    if Copy(Value, 1, 1) = #$FEFF then
      Delete(Value, 1, 1);
    Value := Trim(Value);
  end;
end;

function RunVerifier(const Arguments: String; var Message: String): Boolean;
var
  ExitCode: Integer;
begin
  Message := '';
  DeleteFile(ResultPath);
  Result := Exec(VerifierPath, Arguments + ' --output ' + AddQuotes(ResultPath),
    '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
  if not Result then
  begin
    Message := '无法启动授权验证程序，请检查安全软件是否拦截安装程序。';
    Exit;
  end;
  Result := ReadUTF8File(ResultPath, Message);
  if not Result then
  begin
    Message := '无法读取授权验证结果，安装已停止。';
    Exit;
  end;
  Result := ExitCode = 0;
  if (not Result) and (Message = '') then
    Message := '授权验证失败，安装已停止。';
end;

function ValidateLicense(): String;
var
  Message: String;
begin
  Result := '';
  LicenseSummary := '';
  if Trim(LicensePage.Values[1]) = '' then
  begin
    Result := '请输入软件提供者为本机签发的 CD-KEY。';
    Exit;
  end;
  if not SaveStringToFile(LicensePath, UTF8Encode(Trim(LicensePage.Values[1])), False) then
  begin
    Result := '无法保存临时授权文件，安装已停止。';
    Exit;
  end;
  if not RunVerifier('verify --license-file ' + AddQuotes(LicensePath), Message) then
    Result := Message
  else
    LicenseSummary := Message;
end;

procedure InitializeWizard();
var
  Message: String;
  InputFile: String;
begin
  LicensePath := ExpandConstant('{tmp}\pbi-license.key');
  ResultPath := ExpandConstant('{tmp}\pbi-license-result.txt');
  ExtractTemporaryFile('LicenseVerifier.exe');
  VerifierPath := ExpandConstant('{tmp}\LicenseVerifier.exe');
  LicensePage := CreateInputQueryPage(wpWelcome, '软件授权', '请输入本机专用的永久或 7 天试用 CD-KEY',
    '将机器码复制给软件提供者，收到 CD-KEY 后粘贴到下方。试用期从发码时起算 7×24 小时，重装不会重置；无需联网激活。');
  LicensePage.Add('本机机器码（可选中后按 Ctrl+C 复制）：', False);
  LicensePage.Edits[0].ReadOnly := True;
  LicensePage.Add('CD-KEY（请完整粘贴）：', False);
  LicensePage.Edits[1].MaxLength := 2048;
  if not RunVerifier('machine-code', Message) then
    RaiseException(Message);
  LicensePage.Values[0] := Message;
  InputFile := ExpandConstant('{param:LICENSEFILE|}');
  if InputFile <> '' then
  begin
    if not ReadUTF8File(InputFile, Message) then
      RaiseException('无法读取 /LICENSEFILE 指定的 CD-KEY 文件。');
    LicensePage.Values[1] := Message;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Message: String;
begin
  Result := True;
  if CurPageID = LicensePage.ID then
  begin
    Message := ValidateLicense();
    Result := Message = '';
    if not Result then
      MsgBox(Message, mbError, MB_OK);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  { Also runs for /SILENT and /VERYSILENT: skipping wizard pages is not authorization. }
  Result := ValidateLicense();
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := MemoDirInfo + NewLine + MemoGroupInfo + NewLine + MemoTasksInfo +
    NewLine + NewLine + LicenseSummary;
end;
