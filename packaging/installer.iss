; GUET 畅课助手 · 安装包脚本（Inno Setup 6）
; 变体：/DVariant=full（内置浏览器）  /DVariant=lite（依赖系统 Edge）

#ifndef Variant
  #define Variant "full"
#endif
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#if Variant == "lite"
  #define BuildName "lite"
  #define AppDisplayName "GUET 畅课助手（精简版）"
  #define SourceDir "..\dist\lite\GuetShuake"
  #define OutputBase "GuetShuake-" + AppVersion + "-lite-setup"
  #define AppGuid "{{9C1E4A55-7C2B-4E0C-9F31-2A7B6D5E1A01}"
#else
  #define BuildName "full"
  #define AppDisplayName "GUET 畅课助手"
  #define SourceDir "..\dist\full\GuetShuake"
  #define OutputBase "GuetShuake-" + AppVersion + "-full-setup"
  #define AppGuid "{{9C1E4A55-7C2B-4E0C-9F31-2A7B6D5E1A01}"
#endif

[Setup]
AppId={#AppGuid}
AppName={#AppDisplayName}
AppVersion={#AppVersion}
AppPublisher=GUET Shuake
AppPublisherURL=https://github.com/
DefaultDirName={autopf}\GuetShuake
DefaultGroupName=GUET 畅课助手
DisableProgramGroupPage=yes
OutputDir=..\packaging\output
OutputBaseFilename={#OutputBase}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=app.ico
UninstallDisplayIcon={app}\GuetShuake.exe
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
AllowNoIcons=yes

[Languages]
Name: "chs"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"
Name: "autostart"; Description: "开机自动启动（可选）"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppDisplayName}"; Filename: "{app}\GuetShuake.exe"
Name: "{group}\卸载 {#AppDisplayName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppDisplayName}"; Filename: "{app}\GuetShuake.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "GuetShuake"; ValueData: """{app}\GuetShuake.exe"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\GuetShuake.exe"; Description: "立即启动 {#AppDisplayName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清程序目录，用户数据（%LOCALAPPDATA%\GuetShuake：账号、会话、进度）保留
Type: filesandordirs; Name: "{app}"
