# music-workflow 使用指南（人人可用 · 各用各账号）

> 这套工具帮你把「AI 写歌 → 生成音频 → 做封面 → 上传到番茄音频创作平台 → 发布」
> 的整个流程自动化。它**不绑定任何人的账号**，谁用谁登录，互不干扰。

---

## 一、它解决了「人人各用各账号」的问题

原来的脚本把登录态、手机号、歌单都写死在作者电脑上。**这个版本改成了「账户无关」：**

| 原来（作者专用） | 现在（人人可用） |
|---|---|
| 登录 cookie 存在作者机器固定目录，写死路径 | 登录态存在**你自己电脑的系统每用户私有目录** `%LOCALAPPDATA%/music-workflow/profiles/`（妙响=`douyin/`、番茄=`fanqie/`），首次运行自动创建、为空 |
| 手机号 `PHONE = "138xxxxxxxx"` 写死在代码里 | 不内置任何手机号；想用自动填号再在 `config.json` 写 `fanqie_phone` 或设环境变量 `FANQIE_PHONE` |
| 歌单 `SONGS = [...]` 写死在代码里 | 自动**扫描 `library/` 文件夹**，你生成过什么就发什么 |
| 已发布名单是代码里一串歌名 | 变成工作目录里的 `published.json` 数据文件，每人的记录各自独立 |

**结论：把工具拷给任何人，他自己第一次运行时打开浏览器登录自己的账号即可，绝不会用到你的账号——因为登录态存在他自己电脑的「系统每用户私有目录」（`%LOCALAPPDATA%/music-workflow/profiles/`），根本不在工具文件夹里，拷贝工具不会带走登录态。**

---

## 一之二、哪些步骤需要你亲自参与？

整条流水线里**只有 5 个环节需要你动手**，其余全自动：

| # | 环节 | 频率 | 说明 |
|---|---|---|---|
| 1 | 安装依赖 | 一次性 | `pip install` + **`playwright install chromium`**（最容易漏的一步） |
| 2 | 首次登录妙响 | 一次性 | 抖音扫码/手机号。登录态存**系统每用户私有目录** `%LOCALAPPDATA%/music-workflow/profiles/douyin`，**之后长期自动免登**（Cookie 一般撑数周~数月，过期重跑 `--login` 即可） |
| 3 | 首次登录番茄 | 一次性 | 同上，登录态存 `%LOCALAPPDATA%/music-workflow/profiles/profile_fanqie` |
| 4 | 写歌单和歌词 | 每批歌 | `tasks.csv` + `lyrics/*.txt`——这是创作，当然得你来 |
| 5 | **签电子合同** | 每批一次 | 需你本人收**短信验证码**。**番茄没有「发布」按钮，签完合同 = 发布成功**。脚本会开着浏览器等你最多 30 分钟 |

**完全不用你管的**：生成音乐、等转码、下载、做封面、番茄 6 项填表（断网也不怕，断点续传）、
独家授权、签约个人、确认签署、跳转授权、签完自动记入 `published.json` 防重复发。

---

## 二、第一次使用（约 20 分钟）

### 1）准备 Python 环境
需要 `playwright` 和 `pillow`。如果你在 WorkBuddy 里，推荐用自带的托管 Python：

```bat
"你的WorkBuddy托管Python路径\python.exe" -m pip install playwright pillow
"你的WorkBuddy托管Python路径\python.exe" -m playwright install chromium
```

（托管 Python 通常在 `C:\Users\你的用户名\.workbuddy\binaries\python\envs\default\Scripts\python.exe`；
没有就装一个 Python 3.11+，然后 `pip install playwright pillow && playwright install chromium`。）

### 2）建立自己的工作目录（二选一）

**方式 A · 一键初始化（推荐，零配置）：**
直接运行 skill 里的初始化脚本，它会自动帮你复制脚本、建好空目录（`library/` `lyrics/`）、
生成空的 `published.json` 和 `tasks.csv` 模板（登录态目录不在这里，见下方隐私说明）：

```bat
python init_workdir.py                 # 在当前目录新建 ./music-workflow/
python init_workdir.py D:\我的音乐      # 或在指定目录建
python init_workdir.py --check         # 只检查依赖是否装好，不建目录
```

**方式 B · 手动复制：**
把 skill 里的 `scripts/` 整个文件夹复制到你想放歌的地方，例如 `D:\我的音乐\music-workflow\`，
以后所有生成物、歌词、发布记录都在这个目录里。**登录态不在这个目录里**（见下）。

> 两种方式都不会带任何人的账号：登录态统一存在系统每用户私有目录
> `%LOCALAPPDATA%/music-workflow/profiles/`（妙响=`douyin/`，番茄=`profile_fanqie/`），
> 拷贝工作目录或 skill 都不会带走登录态。

### 3）首次登录两个平台（各自用自己的账号）
打开**两个**真实浏览器窗口，分别登录：

```bat
python miaoxiang.py --login
python fanqie_upload.py --login
```

- `miaoxiang.py --login`：弹出**妙响（抖音音乐创作实验室）**页面，用你的抖音账号扫码登录，
  **登录后脚本自动检测并保存登录态、无需回车**。
- `fanqie_upload.py --login`：弹出番茄上传页，用你的番茄账号登录（手机号或抖音扫码），
  登录成功看到「添加歌曲」按钮后，脚本自动退出并保存登录态。

> 登录态存在系统每用户私有目录 `%LOCALAPPDATA%/music-workflow/profiles/`：
> 妙响 = `douyin/`，番茄 = `profile_fanqie/`。**这个目录不要发给别人、不要上传到公开仓库。**

> 只想验证登录态有没有过期、不想走完整流程时，用 `python login_check.py douyin` / `python login_check.py fanqie`，
> 它只打开页面看一眼登录状态就退出。

---

## 二之三、把歌曲存到别的盘（不占系统盘）★

批量下载的音频 + 封面会越攒越多（实测 45 首 ≈ 300 MB），默认全落在系统盘（C 盘）。
**一条命令就能把它挪到 D 盘，以后所有脚本自动跟着走，不用加任何参数：**

```bat
python miaoxiang.py --set-workdir "D:\music-workflow"
```

它会：建好 `library/` `lyrics/` `.tmp/` → 从 skill 拷一份歌单模板 → 把位置写进本机设置
→ 打印新路径和 D 盘剩余空间给你核对。**只建目录、不动任何已有歌曲。**

设置文件在 `%LOCALAPPDATA%/music-workflow/settings.json`（本机私有，不会跟着仓库分发）：

```json
{
  "workdir":  "D:\\music-workflow",
  "temp_dir": "D:\\music-workflow\\.tmp"
}
```

> 为什么要连 `.tmp` 一起挪：下载时音频会**先落到中转目录再进曲库**，
> 中转目录默认也在 C 盘。一起挪走，批量下载连峰值占用都不进系统盘。

### 已经攒在 C 盘的歌，怎么搬过去

```bat
python migrate_library.py --to "D:\music-workflow" --dry-run   # 先看会搬什么，不复制
python migrate_library.py --to "D:\music-workflow"             # 真正搬
```

搬完核对无误，要清掉 C 盘原件时再跑（**这一步不可逆，确认好再执行**）：

```bat
python migrate_library.py --to "D:\music-workflow" --purge
```

**它怎么安排**：当前在用的那份曲库（有发布记录的）→ `D:\music-workflow\library\`；
其余历史目录 → `D:\music-workflow\_archive\<来源>\`。

> ⚠️ 为什么不干脆全合到一个曲库里：老目录里的歌**可能早就发布过**，只是当时还没有
> `published.json` 记录。一股脑合并 → 上传脚本会以为「都没发过」→ **重复投稿**。
> `_archive/` 不在曲库扫描路径上，上传脚本看不见它，所以安全。

搬完会生成 `D:\music-workflow\_迁移报告.json`，里面是每个目录的来历和目标位置。

---

## 三、日常使用

### 1）写歌单 `tasks.csv`
用 Excel 打开工作目录里的 `tasks.csv`，每行一首歌：

| 歌名 | 风格描述 | 歌词文件 | 生成数量 | 纯音乐 |
|---|---|---|---|---|
| 晚风寄的信 | 校园 怀旧 青春 民谣 木吉他 82BPM 男声 | 晚风寄的信.txt | 2 | 否 |

- **歌词文件**放到工作目录的 `lyrics/` 里（`.txt`）；留空 = 让 AI 自己写词。
- **生成数量和纯音乐这两列，妙响端不读**（留着只是为了兼容老表格）——
  妙响的规则是**一份歌词点一次生成，平台固定出 2 个版本，数量改不了**（见下）。

### 2）批量生成 + 下载 + 出封面（妙响）

```bat
python miaoxiang.py --gen                       # 生成前会问你要几个版本
python miaoxiang.py --gen --download-mode both  # 不问，直接两版都下
python miaoxiang.py --gen --download-mode first # 不问，只下第一版
```

**妙响的硬规则（平台限制，绕不过）：**

- **1 份歌词 → 点 1 次「生成歌曲」→ 固定出 2 个不同版本，数量不可改。**
  想要 N 首歌，就得有 N 份不同歌词。
- 两个版本落盘时**默认不同名**，番茄端靠 `meta.title` 取歌名，所以两版必须能区分：
  第 1 版 = 原歌名（文件夹 `<歌名>-01`），第 2 版 = `<歌名>（动听版）`（文件夹 `<歌名>-02`）。
- **下载策略在生成前问你**：只下第一版（省时间）还是两版都下（多一首可发）。
  用 `--download-mode first|both` 可以跳过询问。
- 妙响的下载链路是**四跳**：原始生成卡 → 编辑 → 编辑器里导出 → 产出「编辑器导出」卡 →
  **只有「编辑器导出」卡上的下载按钮有效**，原始卡是下不了的。脚本会自动走完这四跳，
  并在导出前把「歌曲名」填成你的歌名（妙响默认叫「新项目」，不改就分不清谁是谁）。
- 产出格式和以前完全一致：`library/<歌名>-NN/` 下的 `audio.mp3` / `lyrics.txt` /
  `cover.png`（1440×1440）/ `meta.json`。

> 生成端换过：早期用 MiniMax 网页端（`generate.py`），**2026-09 起改成妙响（`miaoxiang.py`）**。
> MiniMax 的代码保留着但**已停用**，当前流程请走 `miaoxiang.py`。

### 3）上传到番茄（填表 + 授权，发布手动点）
```bat
python fanqie_upload.py
```
脚本会：打开浏览器 → 确认已登录 → 把 `library/` 里**还没发布过**的歌逐张填表
（音频/歌词/歌名/词曲制作人歌手「添加自己」/封面/AI类型）→ 第一步「下一步」→
独家授权 → 签约个人 → 确认签署 → 等合同生成 → 点「跳转授权」→ 打开合同签署页后停下等你。

> ★ **番茄上传页没有「发布」按钮。** 流程终点就是**签电子合同——签完合同 = 发布成功**。
> 脚本不会去找「发布」按钮（那个按钮不存在，硬等只会空转）。

### 4）你本人签署电子合同（= 发布）
合同签署需要你本人收**短信验证码**，脚本代劳不了，所以浏览器会保持打开等你（最多 30 分钟）。
签完后脚本检测到「签署成功 / 签署完成 / 签约成功 / 合同已生效」等字样，会**自动写入 `published.json`**。

如果自动检测没生效，告诉 AI「已发布」，让它运行：

```bat
python fanqie_upload.py --mark-published 晚风寄的信-01,晚风寄的信-02
```

这些文件夹名会写进 `published.json`，下次再跑 `fanqie_upload.py` 时自动跳过它们。
（也可以只发某几首：`python fanqie_upload.py --songs 晚风寄的信-01,晚风寄的信-02`。）

---

## 四、排错

| 现象 | 解决 |
|---|---|
| 妙响登录态过期 | 重跑 `python miaoxiang.py --login`；或先 `python login_check.py douyin` 确认是否真过期 |
| 妙响找不到卡片 / 点不到按钮 | 妙响是**固定选择器、不读 `config.json`**。先跑 `python miaoxiang.py --probe` 抓当前真实结构（存 `_mx_probe.json` + `_mx_shot.png`），再改 `miaoxiang.py` |
| 妙响「等不到导出卡」 | 妙响的下载要**四跳**（原始卡→编辑→编辑器→导出），只有「编辑器导出」卡可下载。原始卡上没有可用下载按钮，属正常 |
| 妙响下载下来的是同一首（张冠李戴） | 已加 MD5 去重守卫，会直接报错而不是静默覆盖。用 `python miaoxiang.py --list-cards` 看资产页真实卡片名，再 `--redownload --card "卡片签名=歌名"` 精确补下 |
| 番茄一直停在登录页 | 重跑 `python fanqie_upload.py --login` 重新登录；或检查 `profile_fanqie/` 是否被误删 |
| 提示「library/ 下没有待发布的新歌」 | 说明都发过了（在 `published.json` 里）；要重发就删对应记录或换文件夹 |
| 一直等「发布」按钮 | 番茄**没有发布按钮**，正确终点是签电子合同；脚本已停在合同签署页，你收验证码签完即可 |
| 签完合同后脚本没反应 | **正常**。番茄签署完成后页面会**自动关闭并跳回重新登录页**——这就是「已发布」的信号。脚本已按「签署页消失 / 变成登录页」判定成功，会自动写 `published.json` |
| 脚本等签署太久 | 默认等 30 分钟。签完若脚本没自动记录，手动跑 `--mark-published <文件夹名>` |
| 封面不是 PNG / 小于 1440 | 番茄只要 PNG 且 ≥1440×1440；脚本生成的就是 1440×1440，别手改后缀 |
| 备选端（MiniMax）选择器点不到 | 打开 `config.json`，在 `selectors` 对应项最前面加新候选（不用改代码）。⚠️ 这条**只对已停用的 MiniMax 端有效** |

---

## 五、音乐来源与平台变动（重要背景）

### 当前主力生成端：妙响（抖音音乐创作实验室）

**2026-09 起，生成端已从 MiniMax 换成妙响。** 地址：
`https://music.douyin.com/studio`（资产页 `/studio/assets`，创作页 `/studio/create`）。

| 项 | 说明 |
|---|---|
| 登录方式 | 抖音账号扫码 / 手机号 |
| 一次出几首 | **固定 2 个版本，不可改**（MiniMax 那边是可控数量，妙响不行） |
| 模型 | 需要**显式选 Sway v5.5**（不选会走「自动」，结果不稳定） |
| 填歌词的位置 | 创作页 → 专业模式 → `contenteditable` 编辑器（Quill）；曲风填旁边的 `textarea` |
| 出歌时间 | 比 MiniMax 快，但仍需等转码，脚本会轮询卡片 |
| 走到番茄的衔接 | 妙响产出的 `library/` 格式与 MiniMax 完全一致，番茄端脚本**一行没改** |

**为什么换**：MiniMax 音乐 API（付费/免费）已于 2026-08-20 起停止对新用户服务，网页端也频繁改版
（按钮文案从「限时免费」漂到「创作」、生成数量控件形态更换过两次）。妙响是字节自家产品，
和番茄音频、汽水音乐同生态，衔接更顺。

### 历史备选生成端：MiniMax 网页端（已停用）

早期走的是 MiniMax 网页端 `https://www.minimaxi.com/audio/music`，
对应脚本 `scripts/generate.py`，让页面改版时只更新 `config.json` 的 selectors 即可修。
**现在这段代码保留但不再维护，只在妙响临时不可用时才考虑回退。**

### 备选音源：海绵音乐（字节系，与番茄同生态）

如果你想要更自然的中文咬字，可考虑「海绵音乐出歌 → 番茄上传」的打法（海绵音乐、番茄音乐、
汽水音乐、抖音曲库同在字节生态）。短板：每首约 1 分钟（妙响一般 2–4 分钟），番茄按播放时长计费，
短歌单曲收益较少。海绵音乐作为备选**不内置**在流水线里。

---

## 六、安全提醒

- **不要把 `%LOCALAPPDATA%/music-workflow/profiles/` 整个目录（含 `douyin/`、`profile_fanqie/`）发给别人**——那里面是你的登录态。
- 番茄音乐要求：音乐类型选【原创】、是否 AI 作品如实勾【是】、签约选【独家授权】。
- 合同里的银行开户手机号要和实名身份证一致，否则收不到收益。
