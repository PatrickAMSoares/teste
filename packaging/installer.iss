; Instalador do PhotoDedupe para Windows (Inno Setup 6).
;
; Compilar com:
;     ISCC.exe packaging\installer.iss
;
; Pré-requisito: a pasta dist\PhotoDedupe já criada pelo PyInstaller
; (use packaging\build_windows.ps1 para fazer tudo de uma vez).

#define AppName "PhotoDedupe"
#define AppVersion "1.0.0"
#define AppPublisher "PhotoDedupe"
#define AppExe "PhotoDedupe.exe"

[Setup]
AppId={{7C1B4F6E-3E6A-4D2C-9B2F-5A0D3C8E1A47}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\instalador
OutputBaseFilename=PhotoDedupe-{#AppVersion}-instalador
SetupIconFile=..\src\photodedupe\resources\photodedupe.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Instalação por usuário: não exige senha de administrador.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
MinVersion=10.0
AppComments=Localiza e organiza fotos duplicadas. Funciona totalmente offline.

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar um atalho na área de trabalho"; GroupDescription: "Atalhos adicionais:"; Flags: unchecked

[Files]
Source: "..\dist\PhotoDedupe\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; DestName: "LEIA-ME.txt"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENCA.txt"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Organizador de fotos duplicadas"
Name: "{group}\Desinstalar {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Abrir o {#AppName} agora"; Flags: nowait postinstall skipifsilent

[Messages]
brazilianportuguese.WelcomeLabel2=Este assistente vai instalar o [name/ver] no seu computador.%n%nO aplicativo analisa suas fotos localmente: nenhuma imagem é enviada para a internet, e nenhum arquivo é apagado sem a sua confirmação.

[Code]
// Na desinstalação, perguntamos sobre os dados locais. As FOTOS do usuário e a
// pasta de quarentena nunca são apagadas pelo desinstalador.
//
// Observação de sintaxe: nenhuma linha deste bloco pode COMEÇAR com "#", porque
// o pré-processador do Inno Setup trataria isso como uma diretiva. Por isso as
// mensagens são montadas em variáveis, com as quebras de linha no fim da linha.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DadosApp: string;
  Quarentena: string;
  Pergunta: string;
  Aviso: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DadosApp := ExpandConstant('{localappdata}\PhotoDedupe');
    Quarentena := DadosApp + '\quarentena';

    if DirExists(DadosApp) then
    begin
      Pergunta := 'Deseja remover também o banco de dados e as miniaturas geradas pelo PhotoDedupe?' + #13#10 + #13#10 +
        'Suas fotos NÃO serão apagadas. A pasta de quarentena também será mantida,' + #13#10 +
        'caso ainda existam arquivos aguardando sua decisão em:' + #13#10 +
        Quarentena;

      if MsgBox(Pergunta, mbConfirmation, MB_YESNO) = IDYES then
      begin
        DelTree(DadosApp + '\thumbnails', True, True, True);
        DeleteFile(DadosApp + '\photodedupe.db');
        DeleteFile(DadosApp + '\photodedupe.db-wal');
        DeleteFile(DadosApp + '\photodedupe.db-shm');
        DelTree(DadosApp + '\logs', True, True, True);
      end;

      if DirExists(Quarentena) then
      begin
        Aviso := 'Atenção: ainda existem arquivos na pasta de quarentena:' + #13#10 + #13#10 +
          Quarentena + #13#10 + #13#10 +
          'Eles não foram apagados. Verifique se deseja restaurá-los ou excluí-los manualmente.';
        MsgBox(Aviso, mbInformation, MB_OK);
      end;
    end;
  end;
end;
