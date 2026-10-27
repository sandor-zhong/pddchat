; pddchat 安装程序 —— Inno Setup 脚本
;
; 构建顺序（在仓库根目录下执行）：
;   1. .\packaging\build_runtime.ps1     构建两份 embeddable Python 运行时
;   2. .\packaging\stage_app.ps1         暂存应用源码（不含本机 .env / API 密钥）
;   3. 用 Inno Setup 6 的 ISCC.exe 编译本文件：
;        & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\pddchat.iss
;      默认精简打包，产出：项目根目录 dist\pddchatSetup-<版本号>-lite.exe
;
; 安装包不包含构建机的 .env 和 API 密钥；升级安装沿用目标目录已有的
; .env，全新安装需自行配置。打包文件清单另行排除 .env，防止暂存残留泄漏。
;
; Chromium 浏览器二进制随包打入（build_runtime.ps1 构建时联网下载好，见
; [Files] 里的 stage\browsers\ 那一条），装的时候不需要再联网——安装包
; 本身体积会明显更大（大概 600MB+，压缩前），但同事那边真正做到"双击装完
; 就能用"，不用等一次几分钟的浏览器下载、也不用担心安装时网络不稳定。
;
; 同一批改动里还随包带了官方 Visual C++ Redistributable（VC_redist.x64.exe
; ，build_runtime.ps1 从微软官方下载，见该脚本"4. 下载 Visual C++
; Redistributable"一节）并在安装时静默装一次——Chromium 二进制本身不带
; vcruntime140.dll / msvcp140.dll 这几个系统级运行库（Google 的 Chromium
; 构建假定操作系统已经有），全新机器上缺了会导致 Chromium 启动失败、
; 报出一个语焉不详的退出码（同事那边曾经实测复现过一次）。同一批改动
; 还把浏览器目录下 Playwright 自己写的 DEPENDENCIES_VALIDATED 标记文件
; 从 [Files] 里排除掉了（见下面 stage\browsers\* 那条 Source 的 Excludes
; 说明）——不排除的话这个从构建机器上原样带过去的陈旧标记文件会让
; Playwright 误以为"已经验证过运行库，不用再查"，即使目标机器真的缺
; DLL 也不会报出任何清晰错误。
;
; 装到 {localappdata}（用户目录下）而不是 Program Files：同事可能没有本机
; 管理员权限；且这个程序会持续往自己目录里写数据（数据库、店铺登录
; cookie、日志），装在用户自己的目录下不会碰到 Program Files 的写保护/
; UAC 虚拟化问题。

; MyAppName（"pddchat"）只管技术性的东西（安装目录路径）；MyAppDisplayName
; （"拼多多申诉"）管所有用户会看到的文本（向导标题、开始菜单/桌面快捷方式、
; 卸载入口等）。两者分开是刻意的——安装目录继续用英文路径，规避中文路径
; 在 Playwright 自带的 Node 驱动/Chromium 二进制这类第三方组件上历史上
; 偶发过的边缘问题，不为了纯展示层的诉求去动已经反复测通的安装机制。
#define MyAppName "pddchat"
#define MyAppDisplayName "拼多多申诉"
#define MyAppVersion "0.1.0"
#ifndef BundledBrowser
#ifndef FirefoxOnly
#define NoBundledBrowser
#endif
#endif
; ↑ 出新版本时这里要跟 pyproject.toml / gui/pyproject.toml 的 version 一起改。

[Setup]
; AppId 是这个安装包身份的唯一标识——生成一次之后【永远不能变】，往后每次
; 出新版本都必须原样保留这个 GUID，这是"新安装包会被识别成同一个 app 的
; 升级"（覆盖装到同一个 {app} 目录、保留 state\ 下同事自己的数据）的关键。
AppId={{472960E3-2391-4159-B305-994EE40A7AF6}
AppName={#MyAppDisplayName}
AppVersion={#MyAppVersion}
AppPublisher=pddchat
; 默认建议装到 D 盘根目录（{code:...} 在向导启动时调用 [Code] 段的
; GetDefaultInstallDir 函数，运行时判断 D 盘存不存在——D 盘不存在时自动
; 退回原来的 {localappdata}\Programs\pddchat，不会因为同事那台电脑没有
; D 盘就装不上。这只是"默认建议值"，交互式安装时同事仍然可以在向导里
; 手动改成别的路径。
DefaultDirName={code:GetDefaultInstallDir}
DefaultGroupName={#MyAppDisplayName}
DisableProgramGroupPage=yes
; 不需要管理员权限，全程装在当前用户自己的目录下，不弹 UAC。
PrivilegesRequired=lowest
OutputDir=..\dist
#ifdef FirefoxOnly
OutputBaseFilename=pddchatSetup-{#MyAppVersion}-firefox
#else
#ifdef NoBundledBrowser
OutputBaseFilename=pddchatSetup-{#MyAppVersion}-lite
#else
OutputBaseFilename=pddchatSetup-{#MyAppVersion}-full
#endif
#endif
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
; 安装包本身（Setup.exe，在资源管理器里看到的这个文件图标）用的图标。
SetupIconFile=app_icon.ico

[Languages]
; 只保留简体中文一种语言——同事是中文用户，本脚本里其它自己写的文案
; （[Run] 的下载提示、卸载提示、[Tasks] 描述等）本来就是中文，只有一种
; 语言可选还有个好处：安装一开始不会多弹一个"选择语言"对话框。
;
; 语言文件不是引用 compiler:Languages\ChineseSimplified.isl（那样只在
; "开发机的 Inno Setup 恰好装过这个语言包"时才能编译成功，换一台机器
; 构建就会失败）——而是把这个文件本身下载了一份放进仓库
; packaging\ChineseSimplified.isl，用相对路径引用，任何装了原版 Inno
; Setup 6（不需要额外装语言包）的机器都能直接编译成功。文件来源：Inno
; Setup 官方仓库 https://github.com/jrsoftware/issrc ，
; Files/Languages/ChineseSimplified.isl。
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Files]
; runtime\ 下是 build_runtime.ps1 产出的两份 embeddable Python
; （pipeline\ 3.12 + gui\ 3.11，含各自 pip 装好的依赖）。
Source: "stage\runtime\*"; DestDir: "{app}\runtime"; Flags: recursesubdirs ignoreversion
; app\ 下是 stage_app.ps1 产出的应用源码（不含 .env；gui\config.json 从
; 一开始就没有被暂存进这份 stage 目录，见 stage_app.ps1 的说明，这里不需要
; 额外写排除规则）。
Source: "stage\app\*"; DestDir: "{app}\app"; Excludes: ".env,.env.*"; Flags: recursesubdirs ignoreversion
; Chromium 浏览器二进制，build_runtime.ps1 用 PLAYWRIGHT_BROWSERS_PATH
; 下载到这里。目标路径必须是 {app}\app\browsers——跟 paths.py 里
; `_BUNDLED_BROWSERS_DIR = BASE_DIR / "browsers"` 的计算方式对上
; （BASE_DIR 在装好之后就是 {app}\app\，因为 paths.py 本身也在这份
; app\ 源码树里），这样主流程进程一启动就能自动发现随包带走的浏览器，
; 不需要额外配置。
;
; Excludes 排除 DEPENDENCIES_VALIDATED：这是 Playwright 自己在构建机器上
; 首次启动浏览器、跑完"系统缺不缺 Visual C++ 运行库/Media Foundation
; 组件"这项检查之后写的一个空标记文件（30 天内有效，实测这次构建产出的
; stage\browsers\ 下这个文件在 chromium-*\、chromium_headless_shell-*\、
; ffmpeg-*\、winldd-*\ 四个子目录下各有一份）。如果原样把它也打进安装包，
; Playwright 在同事那台全新机器上看到这个文件就会直接跳过检查、当作
; "已经验证过、放心启动"——但检查其实从来没在那台机器上真正跑过，缺
; DLL 时不会有任何清晰报错，直接在浏览器启动那一步失败，只留下一个语焉
; 不详的退出码。排除这一个文件之后 Playwright 会照常跑一次这项检查——
; 正常情况下不会缺（见上面新增的 Visual C++ Redistributable 静默安装
; 步骤），万一还是缺，至少会报一条讲清楚"装 Visual C++ Redistributable"
; 的清晰错误，而不是无声失败。
;
; 没有一并排除同目录下的 INSTALLATION_COMPLETE：那个标记文件的语义是
; Playwright 自己"下载+解压是否完整"的记账，跟这里要修的依赖校验问题
; 无关，删掉它可能会让 Playwright 误以为浏览器没装完整、触发不必要的
; 重新下载或者报错——保守起见只动理解清楚、确认安全的这一个文件。
; 默认不含浏览器；显式传 /DBundledBrowser 才附带 Chromium。
#ifdef FirefoxOnly
Source: "stage\firefox_browsers\*"; DestDir: "{app}\app\browsers"; Excludes: "DEPENDENCIES_VALIDATED,.links\*"; Flags: recursesubdirs ignoreversion
#else
#ifndef NoBundledBrowser
Source: "stage\browsers\*"; DestDir: "{app}\app\browsers"; Excludes: "DEPENDENCIES_VALIDATED"; Flags: recursesubdirs ignoreversion
#endif
#endif
; Visual C++ Redistributable 安装包（build_runtime.ps1 从微软官方 aka.ms
; 永久链接下载好，见该脚本"4. 下载 Visual C++ Redistributable"一节）。
; 放 {tmp} 而不是 {app}——这是个一次性安装期用完即扔的东西，装完之后
; 没有任何运行时代码还会用到它，不占同事那份安装目录的空间，也不需要
; 卸载时额外处理清理逻辑（{tmp} 下的文件 Inno 装完自己会清）。
Source: "stage\VC_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
; 开始菜单/桌面快捷方式用的图标——pythonw.exe 本身只有 Python 的通用图标，
; 单独把图标文件也装进去，[Icons] 段里指定 IconFilename 引用它。
Source: "app_icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加图标:"

[Icons]
; pythonw.exe 是 Python 自带的无控制台窗口版本，双击图标不会弹出黑色终端
; 窗口——这是选它而不是 python.exe 的唯一原因，不需要额外的启动器程序。
; -m app.main 要求当前工作目录是 gui\ 本身（app 包才能被正确当成顶层模块
; 导入），跟开发环境下 `uv run python -m app.main` 是同一种跑法。
Name: "{group}\{#MyAppDisplayName}"; Filename: "{app}\runtime\gui\pythonw.exe"; \
    Parameters: "-m app.main"; WorkingDir: "{app}\app\gui"; \
    IconFilename: "{app}\app_icon.ico"; Comment: "拼多多商家申诉工具"
Name: "{group}\{cm:UninstallProgram,{#MyAppDisplayName}}"; Filename: "{uninstallexe}"
; 安装器使用 PrivilegesRequired=lowest，只能写当前用户桌面。{commondesktop}
; 指向所有用户的公共桌面，需要管理员权限，会在普通账号上触发
; IPersistFile::Save / 0x80070005（拒绝访问）。
Name: "{userdesktop}\{#MyAppDisplayName}"; Filename: "{app}\runtime\gui\pythonw.exe"; \
    Parameters: "-m app.main"; WorkingDir: "{app}\app\gui"; \
    IconFilename: "{app}\app_icon.ico"; Tasks: desktopicon; Comment: "拼多多商家申诉工具"

[Run]
; Visual C++ Redistributable——见 [Files] 段说明。必须放在这一条、且
; 不能带 postinstall 标记：postinstall 的条目会被 Inno 推迟到用户点了
; 向导最后"完成"页面上的可选复选框才执行，这个补丁必须是安装流程本身
; 无条件的一部分，不能是"装完之后可选的一步"。
;
; /install /quiet /norestart 是官方安装包自己的静默参数（不是 Inno 的
; 参数）——/install 明确走安装分支（避免误触发它自带的 /uninstall 等
; 其它动作）、/quiet 不弹自己的向导 UI、/norestart 就算需要重启也不
; 自动重启这台机器（避免同事这台机器上其它正在跑的东西被意外打断）。
;
; 官方安装包本身是幂等的：机器上已经有同版本或更新版本时，这一步几秒内
; 就会自己判断完毕直接返回（已知会返回 1638 这个"已经有更新版本"的
; 代码，Inno 默认不会把 [Run] 条目的非零退出码当成安装失败，不需要额外
; 处理）——所以不管这是全新安装还是同一个 AppId 的覆盖升级，都无条件跑
; 这一步就行，不需要在 [Code] 段里先检测"是不是已经装过"。
;
; ⚠ 已知风险、这次没有进一步处理，实机安装时留意一下：这个官方安装包
; 自己的清单要求管理员权限（往系统目录装运行库，微软自己就是这么设计
; 的），跟本文件顶部 PrivilegesRequired=lowest 的既定选择（同事可能没有
; 本机管理员权限）存在真实的张力。行业内其它项目的经验通常是 Windows
; 会单独为这一步弹一次 UAC 确认框（同事的账号只要本身在管理员组里、
; 点一下"是"就能过，不会连累让整个安装程序都要求提权）——但这个具体
; 行为没有在这个项目里实机验证过。万一同事那台机器的账号真的完全没有
; 管理员权限，这一步会在 /quiet 下静默失败、不会有任何弹窗，但也不会
; 导致安装程序其它部分跟着失败；那种情况下这次改动仍然优于现状——配合
; 上面 main.py 的 try/except 加固，登录失败时至少能看到具体报错信息，
; 而不是单纯一个退出码 120。
Filename: "{tmp}\VC_redist.x64.exe"; Parameters: "/install /quiet /norestart"; \
    StatusMsg: "正在安装 Visual C++ 运行库（通常几秒钟）…"; Flags: waituntilterminated

; 浏览器已经在构建阶段随包打进 [Files] 的 stage\browsers\ 里了（见上），
; 安装阶段不需要再联网下载任何东西——这里只剩"安装完成页给同事一个立即
; 启动的勾选项"，体验上更接近真正的"装完就能直接用"。
Filename: "{app}\runtime\gui\pythonw.exe"; Parameters: "-m app.main"; \
    WorkingDir: "{app}\app\gui"; Description: "立即启动 {#MyAppDisplayName}"; \
    Flags: postinstall skipifsilent nowait

[Code]
function GetDefaultInstallDir(Param: string): string;
begin
  // D 盘存在就默认建议装到 D 盘根目录（用户偏好），不存在就退回原来的
  // {localappdata}\Programs\pddchat——两条路径都实测验证过能正常工作，
  // 不会因为同事那台电脑没有 D 盘（比如只有一个 C 盘的笔记本，或者 D
  // 盘是光驱盘符）就导致静默安装失败。这只是"默认建议值"，交互式安装
  // 时同事仍然可以在向导的目录选择页里手动改成别的路径。
  if DirExists('D:\') then
    Result := 'D:\{#MyAppName}'
  else
    Result := ExpandConstant('{localappdata}\Programs\{#MyAppName}');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ConfigPath: string;
  ConfigContent: string;
begin
  if CurStep = ssPostInstall then
  begin
    // gui/app/services/project_paths.py 读这个文件解析 pddchat_root /
    // pddchat_python，相对路径是相对 config.json 自己所在目录（gui\）
    // 解析的，不是 cwd——两个路径值分别对应：
    //   pddchat_root   = ".."                          -> {app}\app\
    //   pddchat_python = "..\..\runtime\pipeline\python.exe" -> {app}\runtime\pipeline\python.exe
    // 仓库里 gui\config.json 那份（指向开发环境的 ..\.venv\ 相对路径）
    // 从一开始就没有被 stage_app.ps1 拷进安装包，这里生成的是唯一一份。
    ConfigPath := ExpandConstant('{app}\app\gui\config.json');
    ConfigContent :=
      '{' + #13#10 +
      '    "pddchat_root": "..",' + #13#10 +
      '    "pddchat_python": "..\\..\\runtime\\pipeline\\python.exe"' + #13#10 +
      '}';
    if not SaveStringToFile(ConfigPath, ConfigContent, False) then
      MsgBox('生成 gui\config.json 失败，安装可能不完整，请联系技术支持。' + #13#10 +
             '目标路径：' + ConfigPath, mbError, MB_OK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // 卸载不需要（也刻意不）删除 {app}\app\state\ 和 {app}\app\gui\state\——
  // 这两个目录从来没有被 [Files] 列过，只是程序自己运行时创建的，Inno 的
  // 默认卸载器本来就只删自己装过的文件，天然会跳过它们、不用额外写
  // [UninstallDelete] 排除逻辑。这正是"卸载重装/版本升级都保留同事自己
  // 积累的店铺登录态和历史订单数据"这个效果的来源，在这里给同事一个
  // 明确提示，而不是让这份数据悄无声息地留在磁盘上。
  if CurUninstallStep = usPostUninstall then
    MsgBox('提示：卸载不会删除店铺登录数据和历史订单记录（安装目录下的 app\state\ 文件夹），' + #13#10 +
           '如需彻底清除请手动删除该文件夹；重新安装会自动继续使用这些数据。',
           mbInformation, MB_OK);
end;
