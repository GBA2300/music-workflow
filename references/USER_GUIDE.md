# music-workflow 使用指南（人人可用 · 各用各账号）

> 这套工具帮你把「AI 写歌 → 生成音频 → 做封面 → 上传到番茄音频创作平台 → 发布」
> 的整个流程自动化。它**不绑定任何人的账号**，谁用谁登录，互不干扰。

---

## 一、它解决了「人人各用各账号」的问题

原来的脚本把登录态、手机号、歌单都写死在作者电脑上。**这个版本改成了「账户无关」：**

| 原来（作者专用） | 现在（人人可用） |
|---|---|
| 登录 cookie 存在作者机器固定目录，写死路径 | 登录态存在**你自己的工作目录** `profile/` 和 `profile_fanqie/`，首次运行自动创建、为空 |
| 手机号 `PHONE = "138xxxxxxxx"` 写死在代码里 | 不内置任何手机号；想用自动填号再在 `config.json` 写 `fanqie_phone` 或设环境变量 `FANQIE_PHONE` |
| 歌单 `SONGS = [...]` 写死在代码里 | 自动**扫描 `library/` 文件夹**，你生成过什么就发什么 |
| 已发布名单是代码里一串歌名 | 变成工作目录里的 `published.json` 数据文件，每人的记录各自独立 |

**结论：把工具拷给任何人，他自己第一次运行时打开浏览器登录自己的账号即可，绝不会用到你的账号——因为登录态存在他自己电脑的「系统每用户私有目录」（`%LOCALAPPDATA%/music-workflow/profiles/`），根本不在工具文件夹里，拷贝工具不会带走登录态。**

---

## 一之二、哪些步骤需要你亲自参与？

整条流水线里**只有 4 个环节需要你动手**，其余全自动：

| # | 环节 | 频率 | 说明 |
|---|---|---|---|
| 1 | 安装依赖 | 一次性 | `pip install` + **`playwright install chromium`**（最容易漏的一步） |
| 2 | 首次登录 MiniMax | 一次性 | 扫码/账号密码。登录态存**系统每用户私有目录** `%LOCALAPPDATA%/music-workflow/profiles/profile`，**之后长期自动免登**（Cookie 一般撑数周~数月，过期重跑 `--login` 即可） |
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
直接运行 skill 里的初始化脚本，它会自动帮你复制脚本、建好所有空目录（`library/` `lyrics/`
`profile/` `profile_fanqie/`）、生成空的 `published.json` 和 `tasks.csv` 模板：

```bat
python init_workdir.py                 # 在当前目录新建 ./music-workflow/
python init_workdir.py D:\我的音乐      # 或在指定目录建
python init_workdir.py --check         # 只检查依赖是否装好，不建目录
```

**方式 B · 手动复制：**
把 skill 里的 `scripts/` 整个文件夹复制到你想放歌的地方，例如 `D:\我的音乐\music-workflow\`，
以后所有生成物、登录态、记录都在这个目录里。

> 两种方式都会**保留登录态目录为空**：不会带任何人的账号，等你自己的账号登录。

### 3）首次登录两个平台（各自用自己的账号）
打开**两个**真实浏览器窗口，分别登录：

```bat
python generate.py --login
python fanqie_upload.py --login
```

- `generate.py --login`：弹出 MiniMax 音乐页，用你的微信/手机号登录，**登录后脚本自动检测并保存登录态、无需回车**。
- `fanqie_upload.py --login`：弹出番茄上传页，用你的番茄账号登录（手机号或抖音扫码），
  登录成功看到「添加歌曲」按钮后，脚本自动退出并保存登录态。

> 登录态会保存在工作目录的 `profile/`、`profile_fanqie/`。**这些目录不要发给别人、不要上传到公开仓库。**

---

## 三、日常使用

### 1）写歌单 `tasks.csv`
用 Excel 打开工作目录里的 `tasks.csv`，每行一首歌：

| 歌名 | 风格描述 | 歌词文件 | 生成数量 | 纯音乐 |
|---|---|---|---|---|
| 晚风寄的信 | 校园 怀旧 青春 民谣 木吉他 82BPM 男声 | 晚风寄的信.txt | 2 | 否 |

- **生成数量 = 2**：会出 2 个版本，文件夹命名为 `晚风寄的信-01`（原版）、`晚风寄的信-02`。
  想让第二版叫「歌名（动听版）」，生成后把 `library/晚风寄的信-02/meta.json` 里的 `title`
  改成 `晚风寄的信（动听版）` 即可。
- **歌词文件**放到工作目录的 `lyrics/` 里（`.txt`）；留空 = 让 AI 自己写词。

### 2）批量生成 + 下载 + 出封面
```bat
python generate.py
```
按歌单填表 → 点生成 → 嗅探音频链接下载 → 每首自动生成 1440×1440 PNG 封面，
全部存进 `library/<歌名>-NN/`。

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
| 选择器点不到 / 页面改版 | 打开 `config.json`，在 `selectors` 对应项最前面加新候选（不用改代码） |
| 番茄一直停在登录页 | 重跑 `python fanqie_upload.py --login` 重新登录；或检查 `profile_fanqie/` 是否被误删 |
| 提示「library/ 下没有待发布的新歌」 | 说明都发过了（在 `published.json` 里）；要重发就删对应记录或换文件夹 |
| 一直等「发布」按钮 | 番茄**没有发布按钮**，正确终点是签电子合同；脚本已停在合同签署页，你收验证码签完即可 |
| 脚本等签署太久 | 默认等 30 分钟。签完若脚本没自动记录，手动跑 `--mark-published <文件夹名>` |
| 封面不是 PNG / 小于 1440 | 番茄只要 PNG 且 ≥1440×1440；脚本生成的就是 1440×1440，别手改后缀 |

---

## 五、音乐来源与平台变动（重要背景）

### MiniMax 网页端是当前唯一可用入口
MiniMax 的**音乐 API（付费/免费）已于 2026-08-20 起停止对新用户服务**，只能走网页端自动化：
`https://www.minimaxi.com/audio/music`（Music 3.0 内测，免费额度充足）。
`generate.py` 就是走这条网页端路径，请留意页面改版时只更新 `config.json` 的 selectors。

### 备选音源：海绵音乐（字节系，与番茄同生态）
如果你想要更自然的中文咬字，可考虑「海绵音乐出歌 → 番茄上传」的打法（海绵音乐、番茄音乐、
汽水音乐、抖音曲库同在字节生态）。短板：每首约 1 分钟（MiniMax 3–4 分钟），番茄按播放时长计费，
短歌单曲收益较少。本 skill 默认引擎仍是 MiniMax 网页端，海绵音乐作为备选不内置。

---

## 六、安全提醒

- **不要把 `profile/`、`profile_fanqie/` 发给别人**——那里面是你的登录态。
- 番茄音乐要求：音乐类型选【原创】、是否 AI 作品如实勾【是】、签约选【独家授权】。
- 合同里的银行开户手机号要和实名身份证一致，否则收不到收益。
