; Instalador base do Legendaí (Inno Setup 6). Gerado por build.ps1, que define Versao, Build e Dist.
#ifndef Versao
  #define Versao "1.0.0"
#endif

[Setup]
AppId={{7C1D2B9E-5F3A-4C8B-9E21-6A4F0D3B8C11}
AppName=Legendaí
AppVersion={#Versao}
AppVerName=Legendaí {#Versao}
AppPublisher=Legendaí
DefaultDirName={localappdata}\Programs\Legendai
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir={#Dist}
OutputBaseFilename=Legendai-Setup-{#Versao}
SetupIconFile={#Build}\app\assets\icon.ico
UninstallDisplayIcon={app}\assets\icon.ico
UninstallDisplayName=Legendaí
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=110
WizardImageFile={#Build}\wizard_100.bmp,{#Build}\wizard_200.bmp
WizardSmallImageFile={#Build}\wizard_small_100.bmp,{#Build}\wizard_small_200.bmp
ShowLanguageDialog=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
CloseApplications=yes

[Languages]
Name: "pt"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Messages]
pt.WelcomeLabel1=Bem-vindo ao Legendaí
pt.WelcomeLabel2=Legendas ao vivo do inglês para o português do Brasil, em qualquer programa do seu PC.%n%nEste instalador coloca só o programa base (leve). Depois, dentro do Legendaí, você escolhe os módulos — reconhecimento de fala e tradutores — ou faz a instalação completa recomendada para o seu PC.
pt.FinishedHeadingLabel=Programa base instalado
pt.FinishedLabel=Ao abrir o Legendaí, vá em “Módulos” e escolha “Instalação completa” para baixar o que é recomendado para este PC.

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na Área de Trabalho"; GroupDescription: "Atalhos:"

[Files]
Source: "{#Build}\app\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Legendaí"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"; Comment: "Legendas ao vivo do inglês para o português"
Name: "{autodesktop}\Legendaí"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; Description: "Abrir o Legendaí e escolher os módulos"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; remove tambem os modulos baixados depois (modelos, bibliotecas, configuracoes)
Type: filesandordirs; Name: "{app}"
